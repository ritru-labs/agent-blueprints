"""Deterministic Terraform addresses: <type>.<short type>_<Name tag>, else <short type>_<short id>.

The same inputs always give the same addresses, so a second run produces the
same code. Collisions get the short ID appended to every member.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable

from .models import Resource


def slug(text: str | None) -> str:
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")


def short_type(terraform_type: str) -> str:
    return terraform_type.split("_", 1)[1] if "_" in terraform_type else terraform_type


def short_id(import_id: str) -> str:
    return slug(import_id)[-8:] or "x"


def addresses(resources: Iterable[Resource]) -> dict[tuple[str, str], str]:
    candidates: dict[str, list[Resource]] = defaultdict(list)
    for r in resources:
        base = short_type(r.terraform_type)
        label = f"{base}_{slug(r.name)}" if slug(r.name) else f"{base}_{short_id(r.import_id)}"
        candidates[f"{r.terraform_type}.{label}"].append(r)
    result: dict[tuple[str, str], str] = {}
    for address, group in candidates.items():
        if len(group) == 1:
            result[group[0].key] = address
            continue
        suffixes = {r.key: short_id(r.import_id) for r in group}
        if len(set(suffixes.values())) < len(group):  # short IDs collide too: use the full ID
            suffixes = {r.key: slug(r.import_id) for r in group}
        for r in group:
            result[r.key] = f"{address}_{suffixes[r.key]}"
    return result
