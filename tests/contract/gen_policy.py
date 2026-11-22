"""Fixtures: `policy.check_overrides` (``policy.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/policy.rs``. A duck-typed config
(``policy.forbid_force``, ``policy.forbid_acknowledge``, ``managed_keys``)
stands in for `Config`, so every switch reaches the rule.

The header holds a sweep, pinned as a digest: every combination of the
tool (``memory_write``, ``memory_update``), ``force``, the subset of
`ACKNOWLEDGE_NAMES` passed true (bit ``i`` for the name at ``i``; the
others absent), ``forbid_force``, ``forbid_acknowledge`` (one of
``FORBID``) and ``managed_keys`` (one of ``MANAGED``), in that nesting
order, outermost first. Each answer is one line, ``-`` for none, else the
refusal's text, then its flags, keys and managed keys each joined by
``,``, all four joined by a tab; the lines joined by newlines are
hashed with SHA-256.

Each case below the header names ``tool``, ``force``, ``ack`` (the
acknowledge mapping, as passed), ``ff``, ``fa`` and ``mk``, and answers
``r``: null, or the refusal's ``msg`` and ``ev`` (`event_fields()`).
"""

from __future__ import annotations

import hashlib
import itertools
import types
from typing import Any

from bettermemory.config import ACKNOWLEDGE_NAMES
from bettermemory.policy import PolicyRefusal, check_overrides

from ._common import jsonl

TOOLS = ("memory_write", "memory_update")
FORBID: tuple[list[str], ...] = (
    [],
    ["credential"],
    ["truncation", "credential", "credential"],
    [*ACKNOWLEDGE_NAMES, "bogus"],
)
MANAGED: tuple[list[str], ...] = (
    [],
    ["policy.forbid_force"],
    ["policy.forbid_acknowledge"],
    ["policy.forbid_force", "policy.forbid_acknowledge", "scopes.allowed"],
)


def _config(ff: bool, fa: list[str], mk: list[str]) -> Any:
    return types.SimpleNamespace(
        policy=types.SimpleNamespace(forbid_force=ff, forbid_acknowledge=tuple(fa)),
        managed_keys=frozenset(mk),
    )


def _run(
    tool: str, force: bool, ack: dict[str, Any], ff: bool, fa: list[str], mk: list[str]
) -> PolicyRefusal | None:
    try:
        check_overrides(_config(ff, fa, mk), tool, force=force, acknowledge=ack)
    except PolicyRefusal as refusal:
        return refusal
    return None


def _sweep() -> dict[str, Any]:
    lines: list[str] = []
    for tool, force, bits, ff, fa, mk in itertools.product(
        TOOLS, (False, True), range(32), (False, True), FORBID, MANAGED
    ):
        ack = {n: True for i, n in enumerate(ACKNOWLEDGE_NAMES) if bits >> i & 1}
        r = _run(tool, force, ack, ff, fa, mk)
        if r is None:
            lines.append("-")
        else:
            ev = r.event_fields()
            lines.append(
                "\t".join(
                    [
                        str(r),
                        ",".join(ev["policy_flags"]),
                        ",".join(ev["policy_keys"]),
                        ",".join(ev["policy_managed"]),
                    ]
                )
            )
    text = "\n".join(lines).encode("utf-8")
    return {"count": len(lines), "sha256": hashlib.sha256(text).hexdigest()}


def _cases() -> list[dict[str, Any]]:
    every = list(ACKNOWLEDGE_NAMES)
    all_true = dict.fromkeys(every, True)
    return [
        # Both switches at default: nothing is refused.
        dict(tool="memory_write", force=True, ack=all_true, ff=False, fa=[], mk=[]),
        # Force alone, unmanaged, both tools.
        dict(tool="memory_write", force=True, ack={}, ff=True, fa=[], mk=[]),
        dict(tool="memory_update", force=True, ack={}, ff=True, fa=[], mk=[]),
        dict(tool="memory_admin", force=True, ack={}, ff=True, fa=[], mk=[]),
        # Force passed false, or forbidden but not passed.
        dict(tool="memory_write", force=False, ack={}, ff=True, fa=[], mk=[]),
        # Force managed.
        dict(
            tool="memory_write",
            force=True,
            ack={},
            ff=True,
            fa=[],
            mk=["policy.forbid_force"],
        ),
        # One acknowledge flag, unmanaged and managed.
        dict(
            tool="memory_update",
            force=False,
            ack={"credential": True},
            ff=False,
            fa=["credential"],
            mk=[],
        ),
        dict(
            tool="memory_write",
            force=False,
            ack={"credential": True},
            ff=False,
            fa=["credential"],
            mk=["policy.forbid_acknowledge"],
        ),
        # Several acknowledge flags, named in ACKNOWLEDGE_NAMES order.
        dict(
            tool="memory_write",
            force=False,
            ack={"truncation": True, "transient": True, "credential": True},
            ff=False,
            fa=["truncation", "credential", "transient"],
            mk=[],
        ),
        # Force and acknowledge flags, one key managed.
        dict(
            tool="memory_update",
            force=True,
            ack={"user_claim": True},
            ff=True,
            fa=["user_claim"],
            mk=["policy.forbid_acknowledge"],
        ),
        dict(
            tool="memory_write",
            force=True,
            ack=all_true,
            ff=True,
            fa=every,
            mk=["policy.forbid_force", "policy.forbid_acknowledge"],
        ),
        # Force forbidden, flag passed only as an acknowledge.
        dict(
            tool="memory_write",
            force=False,
            ack={"scope_mismatch": True},
            ff=True,
            fa=["scope_mismatch"],
            mk=["policy.forbid_force"],
        ),
        # A flag passed false, a flag the policy does not name, a name
        # outside ACKNOWLEDGE_NAMES.
        dict(
            tool="memory_write",
            force=False,
            ack={"credential": False},
            ff=False,
            fa=["credential"],
            mk=[],
        ),
        dict(
            tool="memory_write",
            force=False,
            ack={"transient": True},
            ff=False,
            fa=["credential"],
            mk=[],
        ),
        dict(
            tool="memory_write",
            force=False,
            ack={"bogus": True},
            ff=False,
            fa=["bogus"],
            mk=[],
        ),
        # Force not forbidden but passed, with one acknowledge refused.
        dict(
            tool="memory_write",
            force=True,
            ack={"truncation": True},
            ff=False,
            fa=["truncation"],
            mk=["policy.forbid_force"],
        ),
    ]


def generate() -> dict[str, bytes]:
    lines: list[Any] = []
    for case in _cases():
        r = _run(
            case["tool"], case["force"], case["ack"], case["ff"], case["fa"], case["mk"]
        )
        out = None if r is None else {"msg": str(r), "ev": r.event_fields()}
        lines.append({**case, "r": out})
    header = {"fixture": "policy", "cases": len(lines), "sweep": _sweep()}
    return {"policy.jsonl": jsonl([header, *lines])}
