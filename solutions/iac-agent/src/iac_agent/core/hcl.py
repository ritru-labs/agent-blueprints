"""Minimal, exact HCL text handling for top-level `resource` blocks.

Only what the pipeline needs: find a block by address (or by line), replace or
remove it, render import blocks, and swap a quoted ID for a reference. Brace
matching skips quoted strings, comments and heredocs, so braces inside policy
JSON or user data never confuse it. Terraform itself validates the result.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .models import ScopeItem

_HEADER = re.compile(r'^resource\s+"([^"]+)"\s+"([^"]+)"\s*\{', re.M)
_HEREDOC = re.compile(r"<<-?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\n")


def _block_end(text: str, open_brace: int) -> int:
    """Index just past the `}` matching the `{` at open_brace."""
    depth, i, n = 0, open_brace, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif ch == "#" or text.startswith("//", i):
            i = text.find("\n", i)
            i = n if i < 0 else i
            continue
        elif text.startswith("/*", i):
            i = text.find("*/", i)
            i = n if i < 0 else i + 1
        elif text.startswith("<<", i) and (m := _HEREDOC.match(text, i)):
            end = re.compile(rf"^\s*{re.escape(m.group(1))}\s*$", re.M).search(text, m.end())
            i = n if end is None else end.end()
            continue
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced braces in HCL")


def blocks(text: str) -> dict[str, tuple[int, int]]:
    """address -> (start, end) of every top-level resource block."""
    found = {}
    for m in _HEADER.finditer(text):
        found[f"{m.group(1)}.{m.group(2)}"] = (m.start(), _block_end(text, m.end() - 1))
    return found


def get_block(text: str, address: str) -> str | None:
    span = blocks(text).get(address)
    return text[span[0] : span[1]] if span else None


def replace_block(text: str, address: str, new_block: str) -> str:
    start, end = blocks(text)[address]
    return text[:start] + new_block.strip() + text[end:]


def remove_block(text: str, address: str) -> str:
    start, end = blocks(text)[address]
    return (text[:start].rstrip() + "\n\n" + text[end:].lstrip()).strip() + "\n"


def address_at_line(text: str, line: int) -> str | None:
    """Address of the block that contains 1-based `line`."""
    offset = sum(len(row) + 1 for row in text.split("\n")[: max(line - 1, 0)])
    for address, (start, end) in blocks(text).items():
        if start <= offset < end:
            return address
    return None


def import_blocks(scope: Iterable[ScopeItem]) -> str:
    out = []
    for s in sorted(scope, key=lambda s: s.address):
        escaped = s.import_id.replace("\\", "\\\\").replace('"', '\\"').replace("${", "$${").replace("%{", "%%{")
        out.append(f'import {{\n  to = {s.address}\n  id = "{escaped}"\n}}\n')
    return "\n".join(out)


def use_reference(block: str, literal: str, reference: str) -> str:
    """Replaces the quoted literal ID with an unquoted reference, e.g. "vpc-1" -> aws_vpc.main.id."""
    return block.replace(f'"{literal}"', reference)


def single_block_address(text: str) -> str | None:
    """The address if `text` is exactly one resource block (what a repair must return)."""
    found = blocks(text)
    if len(found) != 1:
        return None
    ((address, (start, end)),) = found.items()
    return address if not text[:start].strip() and not text[end:].strip() else None


def has_attribute(block: str, names: Iterable[str]) -> bool:
    return any(re.search(rf"^\s*{re.escape(n)}\s*=", block, re.M) for n in names)
