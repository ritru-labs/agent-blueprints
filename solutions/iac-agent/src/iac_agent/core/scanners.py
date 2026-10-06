"""tflint, gitleaks and checkov on the generated code, at the versions pinned in tools.lock.json."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from .terraform import tool_versions


class ScannerError(RuntimeError):
    pass


def _run(cmd: list[str], cwd: Path, ok_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode not in ok_codes:
        raise ScannerError(f"{' '.join(cmd)} exited {proc.returncode}: {proc.stderr[-2000:]}")
    return proc


def _check_version(tool: str, output: str) -> None:
    pinned = tool_versions()[tool]
    if not re.search(rf"\b{re.escape(pinned)}\b", output):
        raise ScannerError(f"{tool} is not the pinned {pinned}: {output.strip()[:200]}")


class Scanners:
    def tflint(self, workdir: Path) -> list[dict]:
        _check_version("tflint", _run(["tflint", "--version"], workdir).stdout)
        out = _run(["tflint", "--format=json", "--no-color"], workdir, ok_codes=(0, 2, 3)).stdout
        return json.loads(out or "{}").get("issues", [])

    def gitleaks(self, workdir: Path) -> list[dict]:
        _check_version("gitleaks", _run(["gitleaks", "version"], workdir).stdout)
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "leaks.json"
            _run(["gitleaks", "dir", str(workdir), "--no-banner", "--redact", "--report-format=json",
                  f"--report-path={report}", "--exit-code=0"], workdir)  # fmt: skip
            return json.loads(report.read_text() or "[]")

    def checkov(self, workdir: Path) -> list[dict]:
        _check_version("checkov", _run(["checkov", "--version"], workdir).stdout)
        out = _run(["checkov", "-d", str(workdir), "--framework", "terraform", "-o", "json", "--quiet",
                    "--soft-fail"], workdir).stdout  # fmt: skip
        data = json.loads(out or "{}")
        reports = data if isinstance(data, list) else [data]
        return [c for r in reports for c in r.get("results", {}).get("failed_checks", [])]
