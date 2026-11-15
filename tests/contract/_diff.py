"""Where a produced golden file differs from the committed one: shared by
`tests/test_contract_golden.py` and the as-found mode of
`tests.contract.generate` (its manifest's per-file detail)."""

from __future__ import annotations

import json


def _differing(data: bytes, committed: bytes, shown: int = 12) -> str:
    """Which lines of a golden file differ, by number, with the first few
    named by their record's ``name`` or ``case`` where a line holds one."""
    ours, theirs = data.splitlines(), committed.splitlines()
    lines = [
        i + 1
        for i in range(max(len(ours), len(theirs)))
        if (ours[i] if i < len(ours) else None)
        != (theirs[i] if i < len(theirs) else None)
    ]
    names = []
    for number in lines[:shown]:
        label = str(number)
        if number <= len(theirs):
            try:
                record = json.loads(theirs[number - 1])
            except ValueError:
                record = None
            if isinstance(record, dict):
                for key in ("name", "case", "text", "query", "id"):
                    if isinstance(record.get(key), str):
                        label = f"{number} ({key} {record[key]!r:.60})"
                        break
        names.append(label)
    more = f", and {len(lines) - shown} more" if len(lines) > shown else ""
    detail = []
    for number in lines[:3]:
        if number <= len(ours) and number <= len(theirs):
            try:
                a, b = json.loads(theirs[number - 1]), json.loads(ours[number - 1])
            except ValueError:
                continue
            paths = _paths(a, b)
            detail.append(
                f"line {number}: {len(paths)} values differ, first "
                + "; ".join(
                    f"{where} committed {x} produced {y}" for where, x, y in paths[:3]
                )
            )
    return (
        f"{len(lines)} of {len(theirs)} lines, at {', '.join(names)}{more}"
        f"; produced {len(ours)} lines" + (". " + " | ".join(detail) if detail else "")
    )


def _paths(a: object, b: object, at: str = "") -> list[tuple[str, str, str]]:
    """Where two JSON values differ: each differing leaf's path with both
    values, cut to 48 characters."""
    if isinstance(a, dict) and isinstance(b, dict):
        out: list[tuple[str, str, str]] = []
        for key in sorted(set(a) | set(b)):
            out += _paths(a.get(key), b.get(key), f"{at}.{key}")
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            out += _paths(x, y, f"{at}[{i}]")
        return out
    if a == b:
        return []
    return [(at or ".", f"{json.dumps(a)!s:.48}", f"{json.dumps(b)!s:.48}")]
