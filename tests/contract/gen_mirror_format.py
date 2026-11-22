"""Fixtures: `mirror_format` and the first unit of `native_text`
(``mirror_format.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/mirror_format.rs`` and
``native_text.rs``. A row is one of:

- ``k: "names"``: a name (``text_field`` ``n``) and its
  `active_filename_for_tombstone` (``a``) and `is_legacy_tombstone_name`
  (``l``); with ``id``, also `tombstone_filename(n, id)` (``t``);
- ``k: "origin"``: a mapping (``o``, `neutral`) and `origin_projection`'s
  pair (``r``, `neutral`), and `has_surrogates` of the mapping (``h``);
- ``k: "memory"`` or ``"episode"``: the fields the record is built from
  (``in``, `neutral`), its metadata (``out``, `neutral`) and, for a
  memory, `memory_filename` (``f``).

Bodies are ASCII and years are four digits, so `make_slug`'s lowering and
`build_filename`'s year padding (gated in ``models_text``) do not reach
this file, and nothing in it follows the Python release or the platform.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from bettermemory import mirror_format as mf
from bettermemory import native_text
from bettermemory.models import Episode, Memory

from ._common import jsonl, neutral, text_field

ID = "01HZX3K9QG7T2M5V8B4N6P1R0S"
ID2 = "01J0000000000000000000000Z"
SUR = "/w/\udcff\udc80x"


def _names() -> list[dict[str, Any]]:
    names = [
        "",
        "a.md",
        "a",
        f"a.{ID}.tombstone.md",
        f"a.b.c.{ID}.tombstone.md",
        f"2024-01-01-x-{ID.lower()}.{ID}.tombstone.md",
        f"a.{ID.lower()}.tombstone.md",
        f".{ID}.tombstone.md",
        f"{ID}.tombstone.md",
        f"..{ID}.tombstone.md",
        "a.tombstone.md",
        ".tombstone.md",
        "a.tombstone.md.md",
        f"a.{ID}.tombstone.md\n",
        f"a.{ID}.tombstone.md\n\n",
        f"a\nb.{ID}.tombstone.md",
        "a.tombstone.md\n",
        f"a.{ID[:-1]}.tombstone.md",
        f"a.{ID}0.tombstone.md",
        f"a{ID}.tombstone.md",
        f"a.{ID[:-1]}I.tombstone.md",
        f"a.{ID[:-1]}L.tombstone.md",
        f"a.{ID[:-1]}O.tombstone.md",
        f"a.{ID[:-1]}U.tombstone.md",
        f"a.{ID[:-1]}H.tombstone.md",
        f"a.{ID[:-1]}J.tombstone.md",
        f"a.{ID}.{ID2}.tombstone.md",
        f"a.{ID}.TOMBSTONE.md",
        f"\udcff.{ID}.tombstone.md",
        "é.tombstone.md",
        f"a.{ID}.tombstone.mdx",
    ]
    rows = []
    for n in names:
        row: dict[str, Any] = {"k": "names", **text_field("n", n)}
        row.update(text_field("a", mf.active_filename_for_tombstone(n)))
        row["l"] = mf.is_legacy_tombstone_name(n)
        rows.append(row)
    for active, mid in [
        ("x.md", ID),
        ("x", ID),
        ("x.md.md", ID2),
        (".md", ID),
        ("", ""),
        ("x.MD", "abc"),
        ("\udcff.md", ID),
    ]:
        row = {"k": "names", **text_field("n", active), "id": mid}
        row.update(text_field("a", mf.active_filename_for_tombstone(active)))
        row["l"] = mf.is_legacy_tombstone_name(active)
        row.update(text_field("t", mf.tombstone_filename(active, mid)))
        rows.append(row)
    return rows


def _origins() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = [
        {},
        {"cwd": "/w"},
        {"cwd": "/w", "repo": "r", "source": "header"},
        {"cwd": SUR, "repo": "r"},
        {"cwd": SUR, "repo": "\udc00", "source": "process-cwd"},
        {"\udcff": "v", "k": "w"},
        {"k": ["a", "\udcff"], "j": {"x": "y"}},
        {"k": {"\ud800": 1}},
        {"z": "é\U0001f600", "a": "\udcff"},
        {"n": None, "i": 3, "s": "\udfff"},
    ]
    rows = []
    for o in cases:
        legacy, native = native_text.origin_projection(o)
        rows.append(
            {
                "k": "origin",
                "o": neutral(o),
                "r": neutral((legacy, native)),
                "h": native_text.has_surrogates(o),
            }
        )
    return rows


T0 = datetime(2024, 3, 5, 6, 7, 8, 123456, tzinfo=UTC)
T1 = datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone(timedelta(hours=-5)))


def _memory_cases() -> list[dict[str, Any]]:
    base: dict[str, Any] = {
        "id": ID,
        "created": T0,
        "updated": T1,
        "scopes": ["global"],
        "confidence": "high",
        "source": "explicit-statement",
        "body": "Use the wrapper for cargo, always. Then more.",
    }
    full: dict[str, Any] = {
        **base,
        "scopes": ["repo:x", "global"],
        "confidence": "low",
        "source": "user-correction",
        "origin": {
            "cwd": "/w",
            "repo": "r",
            "branch": "b",
            "worktree_root": "/w",
            "source": "header",
        },
        "actor": {"client": "c", "model": "m", "sources": {"client": "header"}},
        "last_verified_at": T1,
        "category": "user-inference",
        "verified_paths": ["a.py", "b/c.rs"],
        "verified_commits": ["abc1234"],
        "verified_versions": ["pkg==1.0"],
        "verified_absent_paths": ["gone.py"],
        "verified_path_targets": {"a.py": "abc", "b/c.rs": None},
        "verified_absent_path_targets": {"gone.py": None},
        "claims": ["claim one"],
        "verified_head": "a" * 40,
        "links": [
            {"type": "supersedes", "target_id": ID2},
            {"type": "depends_on", "target_id": ID2, "note": "why"},
            {"type": "extends", "target_id": ID2, "note": ""},
        ],
        "corroborations": 7,
        "last_corroborated": T0,
    }
    cases = [base, full]
    for conf in ["medium"]:
        cases.append({**base, "confidence": conf, "source": "inferred"})
    for cat in ["fact", "ambient"]:
        cases.append({**base, "category": cat})
    origins = [
        {"cwd": "/w", "source": "process-cwd"},
        {"source": "process-cwd"},
        {"cwd": "/w"},
        {"source": "env"},
        {"source": "roots", "repo": "r"},
        {},
        {"cwd": SUR, "source": "process-cwd"},
        {"cwd": SUR, "repo": "r", "source": "header"},
        {"cwd": SUR},
        {"cwd": "/w", "source": "\udcff"},
    ]
    cases += [{**base, "origin": o} for o in origins]
    actors = [
        {},
        {"principal": "p"},
        {"sources": {"client": "env"}},
        {"client": "c", "session": "s"},
    ]
    cases += [{**base, "actor": a} for a in actors]
    singles: list[dict[str, Any]] = [
        {"corroborations": 1},
        {"corroborations": 2**70},
        {"verified_head": "b" * 64},
        {"verified_path_targets": {}},
        {"verified_absent_path_targets": {}},
        {"verified_paths": [], "claims": []},
        {"links": [{"type": "contradicts", "target_id": ID2, "note": None}]},
        {"last_corroborated": T1},
        {"body": "2024-01-01 Hello, World! Many words here to slug now please"},
        {"body": ""},
        {"body": "x" * 90},
        {"id": ID2, "body": "Short"},
    ]
    cases += [{**base, **s} for s in singles]
    return cases


def _episode_cases() -> list[dict[str, Any]]:
    base: dict[str, Any] = {
        "id": ID,
        "session_id": "sess-1",
        "created": T0,
        "body": "b",
    }
    return [
        base,
        {
            **base,
            "scopes": ["global", "repo:x"],
            "takeaway": "t",
            "origin": {"cwd": "/w", "source": "process-cwd"},
            "is_floor": True,
            "swarm_id": "sw_1",
        },
        {**base, "takeaway": ""},
        {**base, "origin": {}},
        {**base, "origin": {"source": "header"}},
        {**base, "origin": {"cwd": SUR, "source": "process-cwd"}},
        {**base, "origin": {"cwd": SUR}},
        {**base, "is_floor": False, "swarm_id": None},
    ]


def generate() -> dict[str, bytes]:
    """``mirror_format.jsonl``: not gated."""
    rows = _names() + _origins()
    for fields in _memory_cases():
        memory = Memory(**fields)
        rows.append(
            {
                "k": "memory",
                "in": neutral(fields),
                "out": neutral(mf.memory_metadata(memory)),
                **text_field("f", mf.memory_filename(memory)),
            }
        )
    for fields in _episode_cases():
        episode = Episode(**fields)
        rows.append(
            {
                "k": "episode",
                "in": neutral(fields),
                "out": neutral(mf.episode_metadata(episode)),
            }
        )
    rows.append(
        {
            "k": "constants",
            "v": [
                mf.TOMBSTONE_DIR,
                mf.EPISODES_DIR,
                mf.TOMBSTONE_SUFFIX,
                list(mf.MEMORY_METADATA_KEYS),
                list(mf.PATH_TARGET_METADATA_KEYS),
                list(mf.TOMBSTONE_METADATA_KEYS),
                list(mf.EPISODE_METADATA_KEYS),
            ],
        }
    )
    return {"mirror_format.jsonl": jsonl(rows)}
