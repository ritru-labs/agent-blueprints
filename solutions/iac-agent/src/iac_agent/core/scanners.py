"""tflint, gitleaks and checkov on the generated code, at the versions pinned in tools.lock.json."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .terraform import LOCK_FILE, tool_versions


class ScannerError(RuntimeError):
    pass


def _run(cmd: list[str], cwd: Path, ok_codes: tuple[int, ...] = (0,), env: dict | None = None):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False,
                          env={**os.environ, **(env or {})})  # fmt: skip
    if proc.returncode not in ok_codes:
        raise ScannerError(f"{' '.join(cmd)} exited {proc.returncode}: {proc.stderr[-2000:]}")
    return proc


def _check_version(tool: str, output: str) -> None:
    pinned = tool_versions()[tool]
    if not re.search(rf"\b{re.escape(pinned)}\b", output):
        raise ScannerError(f"{tool} is not the pinned {pinned}: {output.strip()[:200]}")


def _tf_only(workdir: Path, tmp: str) -> Path:
    """A copy of just the .tf files: never .terraform/ (providers) or state backups (secrets)."""
    out = Path(tmp) / "code"
    out.mkdir()
    for path in workdir.glob("*.tf"):
        (out / path.name).write_text(path.read_text())
    return out


PLUGIN_DIR = LOCK_FILE.parent / ".tools" / "tflint-plugins"  # filled by scripts/install-tools.sh


class Scanners:
    def __init__(self, tflint_config: str | None = None):
        """tflint_config: .tflint.hcl content from the cloud adapter (its pinned ruleset)."""
        self.tflint_config = tflint_config

    def tflint(self, workdir: Path) -> list[dict]:
        env = {"TFLINT_PLUGIN_DIR": str(PLUGIN_DIR)}
        with tempfile.TemporaryDirectory() as tmp:
            args = ["tflint"]
            if self.tflint_config:
                config = Path(tmp) / ".tflint.hcl"
                config.write_text(self.tflint_config)
                args.append(f"--config={config}")
            shown = _run([*args, "--version"], workdir, env=env).stdout
            _check_version("tflint", shown)
            for pinned in re.findall(r'version\s*=\s*"([^"]+)"', self.tflint_config or ""):
                if f"({pinned})" not in shown:  # e.g. "+ ruleset.aws (0.49.0)"
                    raise ScannerError(f"tflint ruleset {pinned} is not installed: {shown.strip()[:300]}")
            out = _run([*args, "--format=json", "--no-color"], workdir, ok_codes=(0, 2, 3), env=env).stdout
        report = json.loads(out or "{}")
        if report.get("errors"):  # e.g. plugin missing: never silently lint with fewer rules
            raise ScannerError(f"tflint: {report['errors']}")
        return report.get("issues") or []

    def gitleaks(self, workdir: Path) -> list[dict]:
        _check_version("gitleaks", _run(["gitleaks", "version"], workdir).stdout)
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "leaks.json"
            _run(["gitleaks", "dir", str(_tf_only(workdir, tmp)), "--no-banner", "--redact", "--report-format=json",
                  f"--report-path={report}", "--exit-code=0"], workdir)  # fmt: skip
            return json.loads(report.read_text() or "[]")

    def checkov(self, workdir: Path) -> list[dict]:
        _check_version("checkov", _run(["checkov", "--version"], workdir).stdout)
        with tempfile.TemporaryDirectory() as tmp:
            out = _run(["checkov", "-d", str(_tf_only(workdir, tmp)), "--framework", "terraform", "-o", "json",
                        "--quiet", "--soft-fail"], workdir).stdout  # fmt: skip
        data = json.loads(out or "{}")
        reports = data if isinstance(data, list) else [data]
        return [c for r in reports for c in r.get("results", {}).get("failed_checks", [])]
