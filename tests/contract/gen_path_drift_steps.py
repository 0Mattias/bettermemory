"""Fixtures: `path_drift_steps._check_body_candidates`
(``path_drift_steps.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/path_drift_steps.rs``. Nothing here
reads the machine: `_path_exists` and `_is_multi_segment_routelike` are
replaced by recorded sets, and the claim key function is a recorded table
(a text not in it is its own key). A line is one case:

- ``c``: the candidates, each ``[path, drop_if_missing, bare_spaced]``;
- ``v`` and ``x``: the verified and the attested-absent key sets;
- ``e``: the paths that exist; ``rt``: the paths that read as routes;
- ``k``: the key table; ``s``: the seen set handed in;
- ``r``: the buckets after the pass, ``[checked, missing, claim_anchored,
  verified, expected_absent, dropped_as_route, sorted(seen)]``;
- ``log``: every closure call in order, ``[name, argument]`` with the
  name ``e`` (exists), ``k`` (keys) or ``rt`` (routelike).

The first line holds the count of cases. The single-candidate rows are
every combination of the flags, existence, attestation, route reading,
prefix existence and a seen key handed in.
"""

from __future__ import annotations

import itertools
from typing import Any
from unittest.mock import patch

from bettermemory import path_drift_steps as pds

from ._common import jsonl


def _case(
    c: list[list[Any]],
    v: list[str],
    x: list[str],
    e: list[str],
    rt: list[str],
    k: dict[str, str],
    s: list[str],
) -> dict[str, Any]:
    log: list[list[str]] = []

    def exists(p: str) -> bool:
        log.append(["e", p])
        return p in e

    def routelike(p: str) -> bool:
        log.append(["rt", p])
        return p in rt

    def keys(p: str) -> str:
        log.append(["k", p])
        return k.get(p, p)

    buckets: list[list[str]] = [[], [], [], [], [], []]
    seen = set(s)
    with (
        patch.object(pds, "_path_exists", exists),
        patch.object(pds, "_is_multi_segment_routelike", routelike),
    ):
        pds._check_body_candidates(
            [(p, bool(d), bool(sp)) for p, d, sp in c],
            set(v),
            set(x),
            checked=buckets[0],
            missing=buckets[1],
            claim_anchored=buckets[2],
            verified=buckets[3],
            expected_absent=buckets[4],
            dropped_as_route=buckets[5],
            seen=seen,
            keys=keys,
        )
    return {
        "c": c,
        "v": v,
        "x": x,
        "e": e,
        "rt": rt,
        "k": k,
        "s": s,
        "r": [*buckets, sorted(seen)],
        "log": log,
    }


def _single() -> list[dict[str, Any]]:
    rows = []
    path, prefix = "/p/a B/c", "/p/a"
    for drop, spaced, ex, att, route, pex, pre in itertools.product(
        (0, 1), (0, 1), (0, 1), ("", "v", "x"), (0, 1), (0, 1), ("", "path", "prefix")
    ):
        if ex and (route or pex):
            continue  # neither is read when the path exists
        e = [path] * ex + [prefix] * pex
        rows.append(
            _case(
                [[path, drop, spaced]],
                [path] if att == "v" else [],
                [path] if att == "x" else [],
                e,
                [path] * route,
                {},
                {"": [], "path": [path], "prefix": [prefix]}[pre],
            )
        )
    return rows


def _multi() -> list[dict[str, Any]]:
    k = {"~/x": "/h/x", "/h/x": "/h/x", "/q": "/h/x"}
    return [
        _case([["~/x", 0, 0], ["/h/x", 0, 0]], [], [], ["~/x", "/h/x"], [], k, []),
        _case([["~/x", 0, 0], ["/h/x", 0, 0]], ["/h/x"], [], [], [], k, []),
        _case([["/q r", 0, 1], ["/q", 0, 0]], ["/h/x"], [], ["/q"], [], k, []),
        _case([["/q r", 0, 1], ["/q t", 0, 1]], ["/h/x"], [], ["/q"], [], k, []),
        _case([["/a b", 0, 1]], [], [], [], ["/a b"], {}, []),
        _case([["/a  b", 0, 1]], [], [], ["/a"], [], {}, []),
        _case([[" /a", 0, 1]], [], [], [""], [], {}, []),
        _case([["/a\tb c", 0, 1]], ["/a\tb"], [], ["/a\tb"], [], {}, []),
        _case([["/r/s", 0, 0], ["/r/s", 0, 0]], [], [], [], ["/r/s"], {}, []),
        _case([["/m", 0, 0], ["/n", 0, 0]], [], [], [], [], {"/n": "/m"}, []),
        _case([["/m", 0, 0], ["/n", 0, 0]], ["/m"], ["/m"], [], [], {"/n": "/m"}, []),
        _case([["/m", 1, 0], ["/n", 0, 0]], ["/m"], [], ["/n"], [], {}, ["/m"]),
        _case([], ["/m"], [], [], [], {}, ["/z"]),
    ]


def generate() -> dict[str, bytes]:
    rows = [*_single(), *_multi()]
    return {"path_drift_steps.jsonl": jsonl([{"cases": len(rows)}, *rows])}
