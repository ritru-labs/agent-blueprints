"""Deterministic gates. Tools decide pass/fail here; the LLM never does.

Plan JSON format: Terraform "JSON Output Format" doc, v1.16 (change
representation: `actions`, `importing.id`, `after_sensitive`, `replace_paths`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .models import Finding, GateOutcome, GateResult, ScopeItem

# Text that must never appear in generated HCL. Provisioners and these
# resources would run commands on apply; ignore_changes hides a real diff.
FORBIDDEN_HCL = {
    r"\bignore_changes\b": "ignore_changes hides a real diff",
    r"\bprovisioner\s+\"": "provisioners run commands on apply",
    r"\bdata\s+\"external\"": "external data sources run commands",
    r"\bresource\s+\"(null_resource|terraform_data)\"": "no helper resources in adopted code",
}

GO = GateOutcome


# --- Plan gate (step 7) -------------------------------------------------------


def _changed_paths(change: dict) -> list[str]:
    """Top-level attributes that differ, without their values (they may be sensitive)."""
    before, after = change.get("before") or {}, change.get("after") or {}
    unknown = change.get("after_unknown") or {}
    keys = set(before) | set(after) | set(unknown)
    return sorted(k for k in keys if before.get(k) != after.get(k) or unknown.get(k))


def plan_gate(plan: Mapping[str, Any], scope: Iterable[ScopeItem], plan_sha256: str | None = None) -> GateResult:
    """Passes only if every in-scope resource is imported with action no-op, and nothing else changes."""
    scope_by_addr = {s.address: s for s in scope}
    findings: list[Finding] = []

    if plan.get("errored"):
        findings.append(Finding(outcome=GO.FAIL, message="plan errored"))

    seen: set[str] = set()
    for rc in plan.get("resource_changes", []):
        if rc.get("mode") != "managed":
            continue  # data sources read, they never write
        addr, change = rc["address"], rc["change"]
        actions, importing = change.get("actions", []), change.get("importing")
        seen.add(addr)
        item = scope_by_addr.get(addr)

        if "delete" in actions or rc.get("deposed"):
            kind = "replace" if "create" in actions else "delete"
            findings.append(
                Finding(
                    outcome=GO.HARD_STOP,
                    address=addr,
                    message=f"{kind} planned: human review, never auto-fixed",
                    detail={
                        "actions": actions,
                        "action_reason": rc.get("action_reason"),
                        "replace_paths": change.get("replace_paths", []),
                    },
                )
            )
        elif item is None:
            findings.append(
                Finding(
                    outcome=GO.REPAIR,
                    address=addr,
                    message="resource is not on the signed-off scope list",
                    detail={"actions": actions},
                )
            )
        elif importing is None:
            findings.append(Finding(outcome=GO.FAIL, address=addr, message="in-scope resource is not imported"))
        elif importing.get("id") != item.import_id:
            findings.append(
                Finding(
                    outcome=GO.FAIL,
                    address=addr,
                    message="import ID differs from the scope list",
                    detail={"planned": importing.get("id"), "scope": item.import_id},
                )
            )
        elif actions != ["no-op"]:
            findings.append(
                Finding(
                    outcome=GO.REPAIR,
                    address=addr,
                    message=f"import would {'/'.join(actions)}, not no-op",
                    detail={"actions": actions, "changed": _changed_paths(change)},
                )
            )

    for addr in scope_by_addr.keys() - seen:
        findings.append(Finding(outcome=GO.REPAIR, address=addr, message="in-scope resource missing from plan"))

    return GateResult(gate="plan", findings=findings, detail={"plan_sha256": plan_sha256})


# --- Configuration gate (also step 7, on the plan's configuration block) -----


def _constants(expr: Any):
    """Yields every constant string inside an expressions tree."""
    if isinstance(expr, dict):
        if "constant_value" in expr:
            yield from _leaf_strings(expr["constant_value"])
        else:
            for v in expr.values():
                yield from _constants(v)
    elif isinstance(expr, list):
        for v in expr:
            yield from _constants(v)


def _leaf_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _leaf_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _leaf_strings(v)


def config_gate(
    plan: Mapping[str, Any],
    scope: Iterable[ScopeItem],
    inline_blocks: Mapping[str, set[str]],
    identity_attrs: Mapping[str, set[str]],
) -> GateResult:
    """One style only (no inline blocks the adapter forbids), references instead of hardcoded IDs.

    identity_attrs: per type, the attributes that hold the resource's own ID
    (e.g. a bucket's `bucket`), which are literals by nature.
    """
    scope = list(scope)
    owner_of: dict[str, str] = {}
    for s in sorted(scope, key=lambda s: (len(s.terraform_type), s.address)):
        owner_of.setdefault(s.import_id, s.address)  # bucket name -> aws_s3_bucket, not its sub-resources
    own_id = {s.address: s.import_id for s in scope}
    findings: list[Finding] = []

    for res in plan.get("configuration", {}).get("root_module", {}).get("resources", []):
        if res.get("mode") != "managed":
            continue
        addr, rtype, expressions = res["address"], res["type"], res.get("expressions", {})
        if res.get("provisioners"):
            findings.append(Finding(outcome=GO.FAIL, address=addr, message="provisioners are forbidden"))
        for block in sorted(inline_blocks.get(rtype, set()) & expressions.keys()):
            findings.append(
                Finding(
                    outcome=GO.REPAIR,
                    address=addr,
                    message=f"inline '{block}' is not allowed; use separate resources",
                )
            )
        for attr, expr in sorted(expressions.items()):
            for value in sorted(set(_constants(expr))):
                target = owner_of.get(value)
                if not target or target == addr:
                    continue
                if value == own_id.get(addr) and attr in identity_attrs.get(rtype, set()):
                    continue
                findings.append(
                    Finding(
                        outcome=GO.REPAIR,
                        address=addr,
                        message=f"{attr}: hardcoded ID; reference {target} instead",
                        detail={"attribute": attr, "id": value, "reference": target},
                    )
                )
    return GateResult(gate="config", findings=findings)


# --- Static checks (step 6) -----------------------------------------------------


def static_gate(
    hcl_files: Mapping[str, str],
    unformatted: list[str],
    validate: Mapping[str, Any],
    tflint_issues: list[dict] | None = None,
    secret_findings: list[dict] | None = None,
) -> GateResult:
    findings: list[Finding] = []
    for name, text in sorted(hcl_files.items()):
        for pattern, why in FORBIDDEN_HCL.items():
            if re.search(pattern, text):
                findings.append(Finding(outcome=GO.REPAIR, message=f"{name}: {why}"))
    for name in unformatted:
        findings.append(Finding(outcome=GO.REPAIR, message=f"{name}: not terraform fmt formatted"))
    if not validate.get("valid", False):
        for d in validate.get("diagnostics", []):
            findings.append(
                Finding(
                    outcome=GO.REPAIR,
                    message=f"validate: {d.get('summary')}",
                    detail={"detail": d.get("detail"), "range": d.get("range")},
                )
            )
        if not validate.get("diagnostics"):
            findings.append(Finding(outcome=GO.REPAIR, message="validate: invalid"))
    for issue in tflint_issues or []:
        if issue.get("rule", {}).get("severity", "error") == "error":
            findings.append(Finding(outcome=GO.REPAIR, message=f"tflint: {issue.get('message')}"))
    for leak in secret_findings or []:
        # The secret itself is never copied into the finding.
        findings.append(
            Finding(
                outcome=GO.REPAIR,
                message=f"secret in {leak.get('File')}:{leak.get('StartLine')} ({leak.get('RuleID')})",
            )
        )
    return GateResult(gate="static", findings=findings)


# --- Coverage (step 2) and verify (step 10) --------------------------------------


def coverage_gate(incomplete: Iterable[Any]) -> GateResult:
    findings = [
        Finding(outcome=GO.BLOCKED, message=f"coverage incomplete for {c.terraform_type}: {c.error}")
        for c in incomplete
    ]
    return GateResult(gate="discover", findings=findings)


def fingerprint_gate(scanned: Mapping[str, str], rescanned: Mapping[str, str]) -> GateResult:
    """Step 8: anything added, removed or changed since the scan sends the run back to discovery."""
    changed = sorted(k for k in scanned.keys() | rescanned.keys() if scanned.get(k) != rescanned.get(k))
    findings = [Finding(outcome=GO.BLOCKED, message=f"changed since scan: {k}") for k in changed]
    return GateResult(gate="fingerprint", findings=findings, detail={"changed": changed})


def verify_gate(
    detailed_exitcode: int, refresh_only_exitcode: int, state: list[str], scope: Iterable[ScopeItem]
) -> GateResult:
    findings: list[Finding] = []
    if detailed_exitcode != 0:
        findings.append(Finding(outcome=GO.FAIL, message=f"plan -detailed-exitcode = {detailed_exitcode}"))
    if refresh_only_exitcode != 0:
        findings.append(Finding(outcome=GO.FAIL, message=f"plan -refresh-only exit code = {refresh_only_exitcode}"))
    managed = [a for a in state if not a.startswith("data.") and ".data." not in a]
    want = {s.address for s in scope}
    for a in sorted({a for a in managed if managed.count(a) > 1}):
        findings.append(Finding(outcome=GO.FAIL, address=a, message="in state more than once"))
    for a in sorted(want - set(managed)):
        findings.append(Finding(outcome=GO.FAIL, address=a, message="in scope but not in state"))
    for a in sorted(set(managed) - want):
        findings.append(Finding(outcome=GO.FAIL, address=a, message="in state but not in scope"))
    return GateResult(gate="verify", findings=findings)
