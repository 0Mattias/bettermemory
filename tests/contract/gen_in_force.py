"""Fixtures: what is in force at delivery (``in_force.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/in_force.rs``. This pins it to
`in_force.in_force` and `InForce`, with the walk's inner functions
(`newest`, `newest_head`, `admitted_sources`, `head`).

The store and the admission are the case's: `Store.inbound_links`
answers from the case's table, `standing.held_ids` (imported by
`in_force` when it runs) from the case's held set, and the admission
admits the case's set. Every call the walk makes on them is recorded in
order, so the order of reads is pinned too.

The first line is a header with the count of rows and of the swept
cases. Then one row per case:

- ``links``: ``{target: [[type, source, updated], ...]}``, what
  `inbound_links` answers for each target asked (rows in the given
  order, a target missing answering nothing);
- ``admit``: the admitted ids, or null for every id;
- ``held``: the held ids (`held_ids` answers those of them it is asked);
- ``ids``: the asked ids.

Each answers ``out`` (per asked id, first occurrence first:
``[asked, id, replaces, extended_by, held]``) and ``calls``: ``["l",
frontier]`` (`inbound_links`, its types always ``_WALKED``), ``["p",
ids]`` (`prime`), ``["a", id]`` (`admits_id`), ``["h", ids]``
(`held_ids`).

The last line is the sweep: ``count`` cases drawn by the rule `_draw`
(a 64-bit linear congruential generator from ``seed``), and ``sha256``,
the digest of each case's ``[out, calls]`` as compact JSON, one per line.
Ids and stamps are ASCII and comparisons are of code points, so the file
is the same under every interpreter and platform and is not gated.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from typing import Any
from unittest import mock

from bettermemory import in_force as in_force_module
from bettermemory import standing

from ._common import jsonl

SEED = 20261119
SWEEP = 3000
_MASK = (1 << 64) - 1


class _World:
    def __init__(
        self,
        links: dict[str, list[list[str]]],
        admit: list[str] | None,
        held: list[str],
    ) -> None:
        self.links = links
        self.admit = admit
        self.held = held
        self.calls: list[list[Any]] = []

    def inbound_links(
        self, target_ids: Iterable[str], types: Sequence[str]
    ) -> dict[str, list[tuple[str, str, str]]]:
        frontier = list(target_ids)
        assert tuple(types) == in_force_module._WALKED
        self.calls.append(["l", frontier])
        return {
            t: [(r[0], r[1], r[2]) for r in self.links[t]]
            for t in frontier
            if t in self.links
        }

    def prime(self, record_ids: Iterable[str]) -> None:
        self.calls.append(["p", list(record_ids)])

    def admits_id(self, record_id: str) -> bool:
        self.calls.append(["a", record_id])
        return self.admit is None or record_id in self.admit

    def admits_row(self, scopes: Sequence[str], recorded: str | None) -> bool:
        raise AssertionError("not read")

    def held_ids(self, store: Any, ids: Sequence[str] | None = None) -> set[str]:
        assert store is self and ids is not None
        asked = list(ids)
        self.calls.append(["h", asked])
        return {rid for rid in self.held if rid in asked}


def answer(c: dict[str, Any]) -> list[Any]:
    world = _World(c["links"], c["admit"], c["held"])
    with mock.patch.object(standing, "held_ids", world.held_ids):
        got = in_force_module.in_force(world, c["ids"], world)  # type: ignore[arg-type]
    out = [[f.asked, f.id, f.replaces, f.extended_by, f.held] for f in got.values()]
    for key, f in got.items():
        assert key == f.asked
        assert f.history == (f.replaces is not None)
    return [out, world.calls]


def C(ids: list[str], links: Any = None, admit: Any = None, held: Any = None) -> Any:
    return {"ids": ids, "links": links or {}, "admit": admit, "held": held or []}


S, E = "supersedes", "extends"

CASES = [
    # Nothing asked: no read of held, one empty walk step.
    C([]),
    C(["a"]),
    C(["a", "a", "b"]),
    # A plain supersession, and a chain followed to its end.
    C(["a"], {"a": [[S, "b", "2"]]}),
    C(["a"], {"a": [[S, "b", "2"]], "b": [[S, "c", "3"]]}),
    C(["a", "b"], {"a": [[S, "b", "2"]], "b": [[S, "c", "3"]]}),
    # Two superseders: the newest by updated, then by id.
    C(["a"], {"a": [[S, "b", "2"], [S, "c", "1"]]}),
    C(["a"], {"a": [[S, "c", "1"], [S, "b", "1"]]}),
    C(["a"], {"a": [[S, "b", "10"], [S, "c", "9"]]}),
    # A cycle closes the walk where it closes; a self link.
    C(["a"], {"a": [[S, "b", "2"]], "b": [[S, "a", "1"]]}),
    C(["a"], {"a": [[S, "a", "1"]]}),
    C(
        ["a", "b", "c"],
        {"a": [[S, "b", "2"]], "b": [[S, "c", "3"]], "c": [[S, "a", "1"]]},
    ),
    # Not admitted: changes nothing and is not walked.
    C(["a"], {"a": [[S, "b", "2"]]}, admit=["a"]),
    C(["a"], {"a": [[S, "b", "2"], [S, "c", "1"]]}, admit=["c"]),
    C(["a"], {"a": [[E, "b", "2"]]}, admit=[]),
    # Held: the head not held is preferred, else the newest held.
    C(["a"], {"a": [[S, "b", "2"], [S, "c", "1"]]}, held=["b"]),
    C(["a"], {"a": [[S, "b", "2"], [S, "c", "1"]]}, held=["b", "c"]),
    C(["a"], held=["a"]),
    C(["a"], {"a": [[S, "b", "2"]]}, held=["a"]),
    C(["a"], {"a": [[S, "b", "2"]]}, held=["b"]),
    # Extenders: the newest in force, its own head; held ones never named.
    C(["a"], {"a": [[E, "b", "2"]]}),
    C(["a"], {"a": [[E, "b", "2"], [E, "c", "3"]]}),
    C(["a"], {"a": [[E, "b", "2"], [E, "c", "3"]]}, held=["c"]),
    C(["a"], {"a": [[E, "b", "2"]]}, held=["b"]),
    C(["a"], {"a": [[E, "b", "2"]], "b": [[S, "c", "3"]]}),
    C(["a"], {"a": [[E, "b", "2"]], "b": [[S, "a", "3"]]}),
    C(["a"], {"a": [[E, "a", "2"]]}),
    C(["a"], {"a": [[S, "b", "2"]], "b": [[E, "c", "3"]]}),
    C(["a"], {"a": [[S, "b", "2"], [E, "c", "3"]]}),
    # Both kinds from one source.
    C(["a"], {"a": [[S, "b", "2"], [E, "b", "2"]]}),
    # The stamp read is the last row's for a source.
    C(["a"], {"a": [[S, "b", "5"], [S, "c", "4"]], "x": [[S, "b", "1"]]}),
    C(["a", "x"], {"a": [[S, "b", "5"], [S, "c", "4"]], "x": [[S, "b", "1"]]}),
    # Another link type the store answers: walked, never followed.
    C(["a"], {"a": [["contradicts", "b", "2"]], "b": [[S, "c", "3"]]}),
    # The empty id: falsy in `newest_head`'s `or`.
    C(["a"], {"a": [[S, "", "2"], [S, "c", "1"]]}, held=["c"]),
    C(["a"], {"a": [[S, "", "2"]]}),
    C([""], {"": [[S, "b", "2"]]}),
    C(["a"], {"a": [[E, "", "2"]]}),
    # An empty stamp sorts first.
    C(["a"], {"a": [[S, "b", ""], [S, "c", "0"]]}),
    # A record already walked as a target is not asked again.
    C(["a", "b"], {"a": [[S, "b", "2"]], "b": [[E, "a", "1"]]}),
    # Unicode ids compare by code point.
    C(["a"], {"a": [[S, "é", "1"], [S, "z", "1"], [S, "\U0001f600", "1"]]}),
]


def _draw(seed: int, count: int) -> Iterable[dict[str, Any]]:
    """The sweep's cases. `nxt` steps x = x * 6364136223846793005 +
    1442695040888963407 mod 2**64 and gives x >> 33; `pick(n)` is
    `nxt() % n`."""
    x = seed

    def nxt() -> int:
        nonlocal x
        x = (x * 6364136223846793005 + 1442695040888963407) & _MASK
        return x >> 33

    def pick(n: int) -> int:
        return nxt() % n

    names = ["", "a", "b", "c", "d", "e", "f", "g"]
    stamps = ["", "1", "2", "3"]
    kinds = [S, S, S, E, E, "contradicts"]
    for _ in range(count):
        links: dict[str, list[list[str]]] = {}
        for target in names[1:]:
            rows = [
                [kinds[pick(6)], names[pick(8)], stamps[pick(4)]]
                for _ in range(pick(4))
            ]
            if rows:
                links[target] = rows
        admit = None if pick(3) == 0 else [n for n in names if pick(4) != 0]
        held = [n for n in names if pick(5) == 0]
        ids = [names[pick(8)] for _ in range(pick(5))]
        yield C(ids, links, admit, held)


def sweep_digest(seed: int, count: int) -> str:
    h = hashlib.sha256()
    for c in _draw(seed, count):
        h.update(json.dumps(answer(c), separators=(",", ":")).encode() + b"\n")
    return h.hexdigest()


def generate() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    for c in CASES:
        out, calls = answer(c)
        rows.append({**c, "out": out, "calls": calls})
    sweep = {
        "sweep": "lcg64",
        "seed": SEED,
        "count": SWEEP,
        "sha256": sweep_digest(SEED, SWEEP),
    }
    header = {"fixture": "in_force", "count": len(rows), "swept": SWEEP}
    return {"in_force.jsonl": jsonl([header, *rows, sweep])}
