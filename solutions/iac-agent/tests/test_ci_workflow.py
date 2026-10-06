"""The CI workflow must parse; GitHub silently skips every job of an invalid one."""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "iac-agent.yml"


def test_workflow_parses_and_runs_every_check():
    wf = yaml.safe_load(WORKFLOW.read_text())
    steps = " ".join(str(s.get("run", "")) for s in wf["jobs"]["checks"]["steps"])
    for command in ["pytest", "ruff check", "ruff format --check", "shellcheck"]:
        assert command in steps, command
