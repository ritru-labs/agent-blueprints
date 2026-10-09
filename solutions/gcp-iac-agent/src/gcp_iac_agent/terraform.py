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


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Workspace:
    def __init__(self, directory: Path, timeout: int = 900):
        self.dir, self.timeout = directory, timeout

    def terraform(self, *args: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "TF_IN_AUTOMATION": "1", "TF_INPUT": "0"}
        return subprocess.run(
            ["terraform", *args], cwd=self.dir, env=env,
            capture_output=True, text=True, timeout=self.timeout, check=False,
        )  # fmt: skip

    # --- setup -------------------------------------------------------------

    def scaffold(self, project: str, resources: list[dict], provider_version: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "providers.tf").write_text(PROVIDERS_TF.format(version=provider_version, project=project))
        blocks = [
            f"import {{\n  to = {r['tf_type']}.{r['tf_name']}\n  id = {json.dumps(r['import_id'])}\n}}\n"
            for r in resources
        ]
        (self.dir / "imports.tf").write_text("\n".join(blocks))
        (self.dir / GENERATED).unlink(missing_ok=True)
        init = self.terraform("init", "-input=false", "-no-color")
        if init.returncode != 0:
            raise TerraformError(f"terraform init failed:\n{init.stderr.strip()}")

    def generate_config(self) -> list[str]:
        """Let Terraform write the HCL for every import block; errors in it are fine, repair fixes them."""
        result = self.terraform("plan", "-input=false", "-json", f"-generate-config-out={GENERATED}")
        if not (self.dir / GENERATED).exists():
            raise TerraformError(
                "terraform did not generate configuration:\n" + "\n".join(diagnostics(result.stdout))
            )
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

    def generated(self) -> str:
        return (self.dir / GENERATED).read_text()

    def apply_edits(self, edits: list[dict], allowed_types: set[str]) -> None:
        """Exact-string replacements on generated.tf; all-or-nothing."""
        text = self.generated()
        for edit in edits:
            count = text.count(edit["old"])
            if count != 1:
                raise UnsafeEdit(
                    f"edit target must appear exactly once, found {count}: {edit['old'][:120]!r}"
                )
            text = text.replace(edit["old"], edit["new"])
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
