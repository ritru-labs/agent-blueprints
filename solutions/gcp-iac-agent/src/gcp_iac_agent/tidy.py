"""After import: split generated.tf into files per service and use references instead of literal IDs.

Deterministic, no model. Every change must keep `terraform plan` at zero changes, or all files are restored.
"""

import json
import re
import shutil
from pathlib import Path

from .terraform import GENERATED, TerraformError, Workspace

FILES = {
    "network.tf": (
        "google_compute_network",
        "google_compute_subnetwork",
        "google_compute_firewall",
        "google_compute_address",
        "google_compute_router",
        "google_compute_router_nat",
    ),
    "compute.tf": ("google_compute_instance", "google_compute_disk", "google_compute_resource_policy"),
    "iam.tf": ("google_service_account", "google_project_iam_member"),
    "secrets.tf": ("google_secret_manager_secret",),
    "storage.tf": ("google_storage_bucket",),
    "apis.tf": ("google_project_service",),
}
# Attributes that hold another resource's *name* (not its self link), and which type that name belongs to.
NAME_KEYS = {
    "network": "google_compute_network",
    "subnetwork": "google_compute_subnetwork",
    "router": "google_compute_router",
}
HEADER = re.compile(r'^resource "([a-z0-9_]+)" "([^"]+)" \{$')
QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
KEY = re.compile(r"^\s*([a-z0-9_]+)\s*=")


def split_blocks(text: str) -> list[tuple[str, str]]:
    """(address, block text) for each resource; Terraform writes headers and closing braces at column 0."""
    blocks, current, lines = [], None, []
    for line in text.splitlines():
        match = HEADER.match(line)
        if match:
            current, lines = f"{match.group(1)}.{match.group(2)}", [line]
        elif current:
            lines.append(line)
            if line == "}":
                blocks.append((current, "\n".join(lines)))
                current = None
    return blocks


def reference_maps(state: dict) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Literal id/self_link -> reference, and per type: unique name -> reference."""
    literal, names, seen = {}, {}, {}
    for r in state.get("values", {}).get("root_module", {}).get("resources", []):
        values = r.get("values") or {}
        for attr in ("self_link", "id"):
            if isinstance(values.get(attr), str) and "/" in values[attr]:  # long, path-like IDs only
                literal.setdefault(values[attr], f"{r['address']}.{attr}")
        name = values.get("name")
        if isinstance(name, str) and name:
            seen.setdefault((r["type"], name), []).append(r["address"])
    for (tf_type, name), addresses in seen.items():
        if len(addresses) == 1:  # ambiguous names are left as literals
            names.setdefault(tf_type, {})[name] = f"{addresses[0]}.name"
    return literal, names


def rewrite_block(address: str, block: str, project: str, literal: dict, names: dict) -> str:
    out = []
    for line in block.splitlines():
        key_match = KEY.match(line)
        key = key_match.group(1) if key_match else None

        def replace(match, key=key):
            value = match.group(1)
            ref = literal.get(value)
            if ref and not ref.startswith(address + "."):  # never point a resource at itself
                return ref
            if key in NAME_KEYS and value in names.get(NAME_KEYS[key], {}):
                return names[NAME_KEYS[key]][value]
            if key == "project" and value == project:
                return "var.project"
            return match.group(0)

        out.append(line if line.startswith("resource ") else QUOTED.sub(replace, line))
    return "\n".join(out)


def tidy(workspace: Workspace, project: str) -> dict:
    directory = workspace.dir
    generated = directory / GENERATED
    if not generated.exists():
        raise TerraformError(f"{GENERATED} not found: this workspace is already tidied or was never imported")
    if workspace.verify_no_changes() is not True:
        raise TerraformError("plan is not clean before tidying; resolve that first")

    shown = workspace.terraform("show", "-json")
    if shown.returncode != 0:
        raise TerraformError(f"terraform show failed:\n{shown.stderr.strip()}")
    literal, names = reference_maps(json.loads(shown.stdout))

    backup = directory / ".tidy-backup"
    shutil.rmtree(backup, ignore_errors=True)
    backup.mkdir()
    originals = [p for p in directory.glob("*.tf")]
    for path in originals:
        shutil.copy2(path, backup / path.name)

    grouped: dict[str, list[str]] = {}
    for address, block in split_blocks(generated.read_text()):
        tf_type = address.split(".", 1)[0]
        name = next((f for f, types in FILES.items() if tf_type in types), "other.tf")
        grouped.setdefault(name, []).append(rewrite_block(address, block, project, literal, names))

    try:
        for name, blocks in grouped.items():
            (directory / name).write_text("\n\n".join(blocks) + "\n")
        (directory / "variables.tf").write_text(
            f'variable "project" {{\n  description = "GCP project ID"\n  type        = string\n'
            f'  default     = "{project}"\n}}\n'
        )
        providers = directory / "providers.tf"
        providers.write_text(providers.read_text().replace(f'project = "{project}"', "project = var.project"))
        generated.unlink()
        (directory / "imports.tf").unlink(missing_ok=True)  # imports are done; the blocks are history now
        workspace.terraform("fmt")
        if workspace.verify_no_changes() is not True:
            raise TerraformError("tidied configuration no longer plans clean")
    except Exception:
        for path in directory.glob("*.tf"):
            path.unlink()
        for path in backup.glob("*.tf"):
            shutil.copy2(path, directory / path.name)
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)

    text = "".join((directory / n).read_text() for n in grouped)
    return {
        "files": sorted(grouped) + ["variables.tf"],
        "references": len(re.findall(r"\bgoogle_[a-z0-9_]+\.[A-Za-z0-9_-]+\.(?:self_link|id|name)\b", text)),
    }


def remaining_literals(directory: Path) -> list[str]:
    """Self links that still point at other resources: those live in another state (cross-state links)."""
    text = "".join(p.read_text() for p in directory.glob("*.tf"))
    return sorted(set(re.findall(r'"(https://www\.googleapis\.com/compute/v1/[^"]+)"', text)))
