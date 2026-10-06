"""Thin, guarded wrapper around the pinned Terraform binary.

Every call goes through `_run`, which refuses commands that could change
infrastructure or bypass the gates. `apply` only accepts a saved plan whose
hash matches the one the plan gate passed.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from .models import GateResult

LOCK_FILE = Path(__file__).resolve().parents[3] / "tools.lock.json"

# Subcommands that write state or infra outside the plan gate.
FORBIDDEN_COMMANDS = {"destroy", "import", "taint", "untaint", "force-unlock", "console"}
FORBIDDEN_STATE_COMMANDS = {"rm", "mv", "push", "replace-provider"}
# Flags that hide a diff or narrow a plan.
FORBIDDEN_FLAGS = ("-target", "-replace", "-destroy", "-lock=false", "-refresh=false")

Runner = Callable[..., subprocess.CompletedProcess]


class TerraformError(RuntimeError):
    pass


class ForbiddenCommand(TerraformError):
    pass


def tool_versions() -> dict[str, str]:
    return {k: v for k, v in json.loads(LOCK_FILE.read_text()).items() if not k.startswith("_")}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_args(args: Sequence[str]) -> None:
    if not args:
        raise ForbiddenCommand("empty terraform command")
    if args[0] in FORBIDDEN_COMMANDS:
        raise ForbiddenCommand(f"terraform {args[0]} is never run by the agent")
    if args[0] == "state" and len(args) > 1 and args[1] in FORBIDDEN_STATE_COMMANDS:
        raise ForbiddenCommand(f"terraform state {args[1]} is never run by the agent")
    for a in args:
        if a.startswith(FORBIDDEN_FLAGS):
            raise ForbiddenCommand(f"flag {a} is never used by the agent")


class Terraform:
    def __init__(self, workdir: Path, binary: str = "terraform", runner: Runner = subprocess.run):
        self.workdir = Path(workdir)
        self.binary = binary
        self.runner = runner

    def _run(self, *args: str, ok_codes: Sequence[int] = (0,)) -> subprocess.CompletedProcess:
        check_args(args)
        env = {**os.environ, "TF_IN_AUTOMATION": "1", "TF_INPUT": "0", "CHECKPOINT_DISABLE": "1"}
        proc = self.runner([self.binary, *args], cwd=self.workdir, env=env, capture_output=True, text=True, check=False)
        if proc.returncode not in ok_codes:
            raise TerraformError(f"terraform {' '.join(args)} exited {proc.returncode}: {proc.stderr[-2000:]}")
        return proc

    def check_versions(self) -> None:
        """Terraform and the AWS provider must equal tools.lock.json (run after init)."""
        lock = tool_versions()
        got = json.loads(self._run("version", "-json").stdout)
        if got.get("terraform_version") != lock["terraform"]:
            raise TerraformError(f"terraform {got.get('terraform_version')} != pinned {lock['terraform']}")
        aws = got.get("provider_selections", {}).get("registry.terraform.io/hashicorp/aws")
        if aws is not None and aws != lock["terraform_provider_aws"]:
            raise TerraformError(f"aws provider {aws} != pinned {lock['terraform_provider_aws']}")

    def init(self) -> None:
        self._run("init", "-no-color", "-input=false")

    def fmt_write(self) -> None:
        """Rewrites .tf files into canonical format (text only)."""
        self._run("fmt", "-recursive", "-list=false")

    def fmt_unformatted(self) -> list[str]:
        proc = self._run("fmt", "-check", "-list=true", "-recursive", ok_codes=(0, 3))
        return [line for line in proc.stdout.splitlines() if line.strip()]

    def validate(self) -> dict:
        return json.loads(self._run("validate", "-json", "-no-color", ok_codes=(0, 1)).stdout)

    def plan(self, out: str = "tfplan", generate_config_out: str | None = None) -> Path:
        args = ["plan", "-no-color", "-input=false", f"-out={out}"]
        if generate_config_out:
            args.append(f"-generate-config-out={generate_config_out}")
        self._run(*args, ok_codes=(0, 2))
        return self.workdir / out

    def show_json(self, planfile: Path) -> dict:
        return json.loads(self._run("show", "-json", str(planfile)).stdout)

    def apply(self, planfile: Path, plan_gate: GateResult) -> None:
        """Applies exactly the saved plan that passed the plan gate, nothing else."""
        if plan_gate.gate != "plan" or not plan_gate.passed:
            raise ForbiddenCommand("apply needs a passed plan gate")
        if plan_gate.detail.get("plan_sha256") != sha256_file(planfile):
            raise ForbiddenCommand("plan file differs from the one the gate passed")
        self._run("apply", "-no-color", "-input=false", str(planfile))

    def detailed_exitcode(self, refresh_only: bool = False) -> int:
        args = ["plan", "-no-color", "-input=false", "-detailed-exitcode"]
        if refresh_only:
            args.append("-refresh-only")
        return self._run(*args, ok_codes=(0, 2)).returncode

    def state_list(self) -> list[str]:
        return self._run("state", "list").stdout.split()

    def state_pull(self) -> str:
        """Raw state, for the backup taken before every import."""
        return self._run("state", "pull").stdout
