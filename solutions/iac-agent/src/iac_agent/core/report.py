"""ADOPTION_REPORT.md and FINDINGS.md. Every discovered resource appears once, with a reason."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .models import Classification, Coverage, GateResult, ScopeItem


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table(header: list[str], rows: Iterable[Iterable[object]]) -> list[str]:
    rows = [list(r) for r in rows]
    if not rows:
        return ["_None._", ""]
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    lines += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return [*lines, ""]


def adoption_report(
    *,
    account: str,
    region: str,
    run_id: str,
    versions: Mapping[str, str],
    classifications: Iterable[Classification],
    scope: Iterable[ScopeItem],
    adopted: set[str],
    skipped: Mapping[str, str],
    coverage: Iterable[Coverage],
    gates: Iterable[GateResult],
) -> str:
    """adopted: addresses now in state. skipped: address -> reason, for in-scope items not adopted."""
    classifications, scope = list(classifications), list(scope)
    by_key = {(s.terraform_type, s.import_id): s for s in scope}
    not_ours = [c for c in classifications if not c.adoptable]
    unscoped = [c for c in classifications if c.adoptable and c.resource.key not in by_key]

    out = [
        "# Adoption report",
        "",
        f"Account `{account}`, region `{region}`, run `{run_id}`.",
        "",
        f"- Adopted: {len(adopted)}",
        f"- Skipped: {len(skipped)}",
        f"- Excluded: {len(not_ours)}",
        f"- Adoptable but not signed off: {len(unscoped)}",
        "",
        "## Adopted",
        "",
        *_table(
            ["Address", "Import ID"],
            ((s.address, s.import_id) for s in sorted(scope, key=lambda s: s.address) if s.address in adopted),
        ),
        "## Skipped",
        "",
        *_table(
            ["Address", "Import ID", "Reason"],
            (
                (s.address, s.import_id, skipped[s.address])
                for s in sorted(scope, key=lambda s: s.address)
                if s.address in skipped
            ),
        ),
        "## Excluded",
        "",
        *_table(
            ["Type", "ID", "Ownership", "Reason"],
            (
                (c.resource.terraform_type, c.resource.import_id, c.ownership.value, c.reason)
                for c in sorted(not_ours, key=lambda c: c.resource.key)
            ),
        ),
        "## Not signed off",
        "",
        *_table(["Type", "ID"], (c.resource.key for c in sorted(unscoped, key=lambda c: c.resource.key))),
        "## Discovery coverage",
        "",
        *_table(
            ["Type", "Complete", "Count", "Error"],
            (
                (c.terraform_type, "yes" if c.complete else "NO", c.count, c.error or "")
                for c in sorted(coverage, key=lambda c: c.terraform_type)
            ),
        ),
        "## Gates",
        "",
        *_table(
            ["Gate", "Outcome", "Findings"],
            ((g.gate, g.outcome.value, "; ".join(f.message for f in g.findings)) for g in gates),
        ),
        "## Tool versions",
        "",
        *_table(["Tool", "Version"], sorted(versions.items())),
    ]
    return "\n".join(out)


def findings_report(failed_checks: Iterable[Mapping]) -> str:
    """Checkov failures. Reported only: fixing them changes infrastructure."""
    checks = sorted(failed_checks, key=lambda c: (c.get("resource", ""), c.get("check_id", "")))
    return "\n".join(
        [
            "# Security findings",
            "",
            "Reported, **not fixed**: fixing a finding changes running infrastructure, which adoption never does.",
            "Each fix is a separate change for the client to approve after adoption.",
            "",
            *_table(
                ["Resource", "Check", "Severity", "Title"],
                ((c.get("resource"), c.get("check_id"), c.get("severity") or "", c.get("check_name")) for c in checks),
            ),
        ]
    )
