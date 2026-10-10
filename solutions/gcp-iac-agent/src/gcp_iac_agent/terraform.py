"""Terraform workspace: scaffold, plan, diff summary, guarded edits and import-only apply."""

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

GENERATED = "generated.tf"  # the only file the model may edit
PLAN_FILE = "tfplan"

PROVIDERS_TF = """terraform {{
  required_version = ">= 1.5"
  required_providers {{
    google = {{
      source  = "hashicorp/google"
      version = "{version}"
    }}
  }}
}}

provider "google" {{
  project = "{project}"
}}
"""

BACKEND_TF = """terraform {{
  backend "gcs" {{
    bucket = "{bucket}"
    prefix = "{prefix}"
  }}
}}
"""

# Block forms that would hide a diff or run code during plan/apply.
FORBIDDEN = [
    (re.compile(r"^\s*lifecycle\s*\{", re.MULTILINE), "lifecycle blocks (ignore_changes hides real drift)"),
    (re.compile(r"\bignore_changes\b"), "ignore_changes"),
    (re.compile(r"^\s*provisioner\s", re.MULTILINE), "provisioners"),
    (re.compile(r"^\s*connection\s*\{", re.MULTILINE), "connection blocks"),
]
TOP_LEVEL = re.compile(r'^resource\s+"([a-z0-9_]+)"\s+"[A-Za-z_][A-Za-z0-9_-]*"\s*\{\s*$')


class TerraformError(RuntimeError):
    pass


class UnsafeEdit(ValueError):
    pass


@dataclass
class Change:
    address: str
    actions: list[str]
    importing: bool
    diff: dict[str, dict] = field(default_factory=dict)  # attribute -> {"config": .., "cloud": ..}


@dataclass
class Plan:
    errors: list[str]
    changes: list[Change]
    expected_imports: int
    sha256: str | None = None  # hash of the saved plan file, set only when the plan succeeded

    @property
    def zero_change(self) -> bool:
        imported = sum(c.importing for c in self.changes)
        return (
            not self.errors
            and imported == self.expected_imports
            and all(c.actions in (["no-op"], ["read"]) for c in self.changes)
        )

    def summary(self) -> dict:
        return {
            "zero_change": self.zero_change,
            "errors": self.errors,
            "imports": sum(c.importing for c in self.changes),
            "expected_imports": self.expected_imports,
            "changes": [
                {"address": c.address, "actions": c.actions, "diff": c.diff}
                for c in self.changes
                if c.actions not in (["no-op"], ["read"])
            ],
            "plan_sha256": self.sha256,
        }


def _short(value) -> str:
    text = json.dumps(value, sort_keys=True)
    return text if len(text) <= 400 else text[:400] + "…"


def attribute_diff(change: dict) -> dict[str, dict]:
    """Top-level attributes where the config (after) differs from the cloud (before)."""
    before, after = change.get("before") or {}, change.get("after") or {}
    unknown = change.get("after_unknown") or {}
    diff = {}
    for key in sorted(set(before) | set(after)):
        if unknown.get(key) is True:
            continue  # computed by the provider; not a config problem
        if before.get(key) != after.get(key):
            diff[key] = {"config": _short(after.get(key)), "cloud": _short(before.get(key))}
    return diff


def parse_plan_json(plan: dict) -> list[Change]:
    changes = []
    for rc in plan.get("resource_changes", []):
        change = rc["change"]
        actions = change["actions"]
        changes.append(
            Change(
                address=rc["address"],
                actions=actions,
                importing="importing" in change,
                diff=attribute_diff(change) if actions not in (["no-op"], ["read"]) else {},
            )
        )
    return changes


def diagnostics(stream: str) -> list[str]:
    """Error diagnostics from `terraform ... -json` machine-readable output."""
    errors = []
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("@level") == "error":
            d = event.get("diagnostic") or {}
            where = d.get("address") or (d.get("range") or {}).get("filename", "")
            errors.append(
                f"{where}: {d.get('summary', event.get('@message', ''))} {d.get('detail', '')}".strip()
            )
    return errors


def check_generated(text: str, allowed_types: set[str]) -> None:
    for pattern, what in FORBIDDEN:
        if pattern.search(text):
            raise UnsafeEdit(f"{GENERATED} may not contain {what}")
    for line in text.splitlines():
        if not line or line[0].isspace() or line.startswith(("}", "#", "//")):
            continue
        match = TOP_LEVEL.match(line)
        if not match or match.group(1) not in allowed_types:
            raise UnsafeEdit(f"{GENERATED} may only contain resource blocks of imported types: {line!r}")


def safe_defaults(text: str) -> str:
    """Fixed policy, not a model decision: removing an API from Terraform must never disable it in GCP."""
    out, in_service = [], False
    for line in text.splitlines(keepends=True):
        if line.startswith("resource "):
            in_service = line.startswith('resource "google_project_service" ')
        if in_service and re.match(r"\s+disable_on_destroy\s*=", line):
            line = re.sub(r"=\s*\S+", "= false", line, count=1)
        out.append(line)
    return "".join(out)


def resource_block(text: str, address: str) -> tuple[int, int]:
    """Span of `resource "TYPE" "NAME" { ... }`. Terraform writes the closing brace at column 0."""
    tf_type, _, name = address.partition(".")
    header = re.compile(rf'^resource\s+"{re.escape(tf_type)}"\s+"{re.escape(name)}"\s*\{{\s*$', re.MULTILINE)
    match = header.search(text)
    end = text.find("\n}", match.end()) if match else -1
    if not match or end == -1:
        raise UnsafeEdit(f"resource {address} not found in {GENERATED}")
    return match.start(), end + 2


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def gcloud_access_token() -> str | None:
    """Short-lived token for the active gcloud login, so Terraform reads as the same identity as discovery."""
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token"], capture_output=True, text=True, timeout=60, check=False
    )
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


class Workspace:
    def __init__(self, directory: Path, timeout: int = 900, access_token=None):
        # access_token: optional callable returning a GCP OAuth token; fetched per run because tokens expire.
        self.dir, self.timeout, self.access_token = directory, timeout, access_token

    def terraform(self, *args: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "TF_IN_AUTOMATION": "1", "TF_INPUT": "0"}
        token = self.access_token() if self.access_token else None
        if token:
            env["GOOGLE_OAUTH_ACCESS_TOKEN"] = token
        return subprocess.run(
            ["terraform", *args], cwd=self.dir, env=env,
            capture_output=True, text=True, timeout=self.timeout, check=False,
        )  # fmt: skip

    # --- setup -------------------------------------------------------------

    def scaffold(
        self, project: str, resources: list[dict], provider_version: str, state_bucket: str | None = None
    ) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "providers.tf").write_text(PROVIDERS_TF.format(version=provider_version, project=project))
        if state_bucket:
            # One state object per workspace, so separate runs never share (or overwrite) state.
            prefix = f"gcp-iac-agent/{self.dir.resolve().name}"
            (self.dir / "backend.tf").write_text(BACKEND_TF.format(bucket=state_bucket, prefix=prefix))
        init = self.terraform("init", "-input=false", "-no-color")
        if init.returncode != 0:
            raise TerraformError(f"terraform init failed:\n{init.stderr.strip()}")
        managed = self.terraform("state", "list").stdout.split()
        if managed:
            # Regenerating config here would put already-managed resources up for deletion.
            raise TerraformError(
                f"this workspace's state already manages {len(managed)} resources; use a new --workspace"
            )
        self.write_imports(resources)
        (self.dir / GENERATED).unlink(missing_ok=True)

    def generate_config(self) -> list[str]:
        """Let Terraform write the HCL for every import block; errors in it are fine, repair fixes them."""
        result = self.terraform("plan", "-input=false", "-json", f"-generate-config-out={GENERATED}")
        if not (self.dir / GENERATED).exists():
            (self.dir / GENERATED).write_text("")  # nothing readable; the caller drops every import
        (self.dir / GENERATED).write_text(safe_defaults(self.generated()))
        return diagnostics(result.stdout)

    # --- the loop ----------------------------------------------------------

    def plan(self, expected_imports: int) -> Plan:
        (self.dir / PLAN_FILE).unlink(missing_ok=True)
        result = self.terraform("plan", "-input=false", "-json", f"-out={PLAN_FILE}")
        errors = diagnostics(result.stdout)
        if result.returncode != 0 or errors:
            return Plan(errors or [result.stderr.strip() or "terraform plan failed"], [], expected_imports)
        shown = self.terraform("show", "-json", PLAN_FILE)
        if shown.returncode != 0:
            raise TerraformError(f"terraform show failed:\n{shown.stderr.strip()}")
        changes = parse_plan_json(json.loads(shown.stdout))
        return Plan([], changes, expected_imports, file_sha256(self.dir / PLAN_FILE))

    def generated_addresses(self) -> set[str]:
        return {
            f"{t}.{n}"
            for t, n in re.findall(r'^resource\s+"([a-z0-9_]+)"\s+"([^"]+)"', self.generated(), re.MULTILINE)
        }

    def write_imports(self, resources: list[dict]) -> None:
        blocks = [
            f"import {{\n  to = {r['tf_type']}.{r['tf_name']}\n  id = {json.dumps(r['import_id'])}\n}}\n"
            for r in resources
        ]
        (self.dir / "imports.tf").write_text("\n".join(blocks))

    def generated(self) -> str:
        return (self.dir / GENERATED).read_text()

    def apply_edits(self, edits: list[dict], allowed_types: set[str]) -> None:
        """Exact-string replacements, each inside one named resource block; all-or-nothing."""
        text = self.generated()
        for edit in edits:
            start, end = resource_block(text, edit["resource"])
            block = text[start:end]
            count = block.count(edit["old"])
            if count != 1:
                raise UnsafeEdit(
                    f"edit target must appear exactly once in {edit['resource']}, found {count}: "
                    f"{edit['old'][:120]!r}"
                )
            text = text[:start] + block.replace(edit["old"], edit["new"]) + text[end:]
        text = safe_defaults(text)  # the model cannot undo the fixed policy
        check_generated(text, allowed_types)
        (self.dir / GENERATED).write_text(text)

    # --- after human approval ----------------------------------------------

    def import_reviewed_plan(self, approved_sha: str, expected_imports: int) -> None:
        path = self.dir / PLAN_FILE
        if not path.exists() or file_sha256(path) != approved_sha:
            raise TerraformError("Plan file changed after review; re-run the agent")
        shown = self.terraform("show", "-json", PLAN_FILE)
        plan = Plan([], parse_plan_json(json.loads(shown.stdout)), expected_imports, approved_sha)
        if not plan.zero_change:
            raise TerraformError("Reviewed plan is not import-only; refusing to apply")
        result = self.terraform("apply", "-input=false", "-no-color", PLAN_FILE)
        if result.returncode != 0:
            raise TerraformError(f"terraform apply (import) failed:\n{result.stderr.strip()}")

    def verify_no_changes(self) -> bool:
        # Exit code 0 = no changes, 2 = changes, 1 = error.
        result = self.terraform("plan", "-input=false", "-no-color", "-detailed-exitcode")
        if result.returncode == 1:
            raise TerraformError(f"verification plan failed:\n{result.stderr.strip()}")
        return result.returncode == 0
