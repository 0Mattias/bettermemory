"""Fixtures: the conflict scan (``conflicts.jsonl``,
``conflicts_unicode.jsonl``) and the queue (``conflicts_queue.jsonl``,
``conflicts_queue_store.jsonl``; their section below says what they hold).

The native build reads them in
``native/crates/bettermemory-core/src/conflicts.rs``. This pins it to
`conflicts`' `_body_hash`, `_pair_id`, `ConflictCandidate` (`to_dict`,
`from_dict`), `DedupCandidate.to_dict`, `PolaritySkippedPair.to_dict`,
`_has_negation`, `_clause_polarity`, `_body_signals`, `_polarity_flip`,
`_numeric_divergence`, `_conflict_signal`, `_pick_keeper`,
`_find_dedup_with_skips` (through `_find_dedup_jaccard` and
`_polarity_skip`), `skip_to_candidate` and `find_conflict_candidates`.

Each line after the header is one case, by ``k``. A text is ``x`` (or
``x_cps``, its code points, where it holds a lone surrogate); an answer's
text is a string, or ``{"cps": [...]}`` where it holds one.

- ``hash``: `_body_hash(x)`; ``r`` the hex, or ``{"err": class}``.
- ``pid``: `_pair_id(a, b)` (``a``/``a_cps``, ``b``/``b_cps``).
- ``neg``: `_has_negation(x)`.
- ``clause``: `_clause_polarity(x)`: ``[asserted, negated]``, each sorted.
- ``sig``: `_body_signals(x)`: ``[has_negation, asserted, negated,
  numbers]``, each set sorted.
- ``pair``: over ``[x, y]``'s signals: ``[_polarity_flip,
  _numeric_divergence, _conflict_signal]``.
- ``keep``: `_pick_keeper(a, b)` of two memories (``m``): ``"ab"`` for
  ``(a, b)``, ``"ba"`` for ``(b, a)``, else ``{"err": class}`` with the
  ``det``ector of a ``ConflictingPair``.
- ``from``: `ConflictCandidate.from_dict` of ``raw`` (`_common.neutral`):
  ``r`` the ``repr`` of the row's fields as a tuple in declaration order,
  or ``{"err": class}``.
- ``dict``: ``repr(x.to_dict())`` of ``c``, a ``"cand"`` (fields in
  declaration order, the four optional ones null for None), ``"dedup"``
  or ``"skip"``; similarities as ``_common.neutral`` floats.
- ``scan``: over the memories ``m`` at ``th`` (its bits; null for the
  default):
  ``d`` `_find_dedup_with_skips` (``[cands, skips, method]``: a candidate
  ``[keeper_id, duplicate_id, similarity bits, keeper_summary,
  duplicate_summary]``, a skip ``[id_a, id_b, similarity bits, detector,
  summary_a, summary_b]``) and ``f`` `find_conflict_candidates` at the
  header's ``now`` (each row's ``repr(to_dict())``), else ``{"err":
  class}``.
- ``sweep`` (ASCII file only): ``count`` memory sets drawn by `Lcg`
  (`gen_pyre_tree`) from ``seed``, each as `_sweep_set` draws it, at every
  threshold of ``ths``; each line the compact JSON of ``[d, f]`` as a
  ``scan`` spells them, ``sha256`` over the lines each ended by a newline.

A memory ``m`` is ``{"i": id, "b": body, "u": updated, "v":
verified_paths, "l": last_verified_at}`` (``u`` default ``U``, ``v``
``[]``, ``l`` null; a datetime by `datetime.fromisoformat`, naive where
it has no offset; ``i_cps``/``b_cps`` where they hold a surrogate).

Text past ASCII that `str.lower`, the tokenizer, `str.strip`, a float's
digits or ``repr`` read through the Unicode data goes in ``_unicode``.
The ASCII file holds curly quotes, a code point past the BMP in a hash or
an id and lone surrogates, which no Unicode release reads differently.
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import conflicts as cf
from bettermemory.models import Confidence, Memory, Source
from bettermemory.store_schema import _CONFLICT_COLUMNS

from ._common import has_surrogate, jsonl, neutral
from .gen_pyre_tree import Lcg

U = "2024-03-01T12:00:00+00:00"
NOW = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
STAMP = "\n\n_(consolidate --llm --from-transcript: turn 3 of (a) chat)_"
NEG_STAMP = "\n\n_(consolidate --llm --from-transcript: do not use sudo, never)_"

SWEEP_SEED = 20261126
SWEEP_COUNT = 2_000
SWEEP_THRESHOLDS = [0.5, 0.75, 0.9]
SWEEP_WORDS = [
    *("use sudo deploy the blue-green strategy in-place port cluster".split()),
    *("migrations cli hand squash-merge rebase worker gateway".split()),
    *("not no never none without cannot don't won't isn’t nor".split()),
    *("5432 5433 3.27.0 3.27.1 v2 v3 2026-07-20 8443".split()),
    *("always run with on for by and".split()),
]
SWEEP_SEPS = [" ", " ", " ", " ", ", ", "; ", ". ", "\n", "! ", "? "]
SWEEP_UPDATED = [
    "2024-03-01T12:00:00+00:00",
    "2024-03-01T13:00:00+01:00",
    "2024-03-01T12:00:00.000001+00:00",
    "2024-02-01T00:00:00+00:00",
]
SWEEP_IDS = ["m0", "m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8", "m9"]


def _bits(x: float) -> str:
    return f"{struct.unpack('>Q', struct.pack('>d', x))[0]:016x}"


def _t(text: str) -> Any:
    """An answer's text."""
    if has_surrogate(text):
        return {"cps": [ord(c) for c in text]}
    return text


def _put(out: dict[str, Any], name: str, text: str) -> None:
    """A case's text field."""
    if has_surrogate(text):
        out[f"{name}_cps"] = [ord(c) for c in text]
    else:
        out[name] = text


def _get(case: dict[str, Any], name: str) -> str:
    if name in case:
        return case[name]
    return "".join(chr(c) for c in case[f"{name}_cps"])


def _err(e: Exception) -> dict[str, Any]:
    return {"err": type(e).__name__}


def _memory(m: dict[str, Any]) -> Memory:
    """A `Memory` built without validation: the scan reads `id`, `body`,
    `updated`, `verified_paths` and `last_verified_at` alone."""
    last = m.get("l")
    return Memory.model_construct(
        id=_get(m, "i"),
        created=datetime.fromisoformat(U),
        updated=datetime.fromisoformat(m.get("u", U)),
        scopes=["global"],
        confidence=Confidence.HIGH,
        source=Source.INFERRED,
        body=_get(m, "b"),
        verified_paths=list(m.get("v", [])),
        last_verified_at=None if last is None else datetime.fromisoformat(last),
    )


def M(i: str, b: str, **kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    _put(out, "i", i)
    _put(out, "b", b)
    out.update(kw)
    return out


def _sorted(s: Any) -> list[str]:
    return sorted(s)


def _scan(memories: list[Memory], th: float | None) -> list[Any]:
    try:
        cands, skips, method = cf._find_dedup_with_skips(memories, threshold=th)
        d: Any = [
            [
                [
                    _t(c.keeper_id),
                    _t(c.duplicate_id),
                    _bits(c.similarity),
                    _t(c.keeper_summary),
                    _t(c.duplicate_summary),
                ]
                for c in cands
            ],
            [
                [
                    _t(p.memory_id_a),
                    _t(p.memory_id_b),
                    _bits(p.similarity),
                    p.detector,
                    _t(p.summary_a),
                    _t(p.summary_b),
                ]
                for p in skips
            ],
            method,
        ]
    except (TypeError, UnicodeEncodeError) as e:
        d = _err(e)
    try:
        with mock.patch.object(cf, "utcnow", lambda: NOW):
            rows = cf.find_conflict_candidates(memories, threshold=th)
        f: Any = [repr(c.to_dict()) for c in rows]
    except (TypeError, UnicodeEncodeError) as e:
        f = _err(e)
    return [d, f]


def _fields(c: cf.ConflictCandidate) -> tuple[Any, ...]:
    return (
        c.id,
        c.a_id,
        c.b_id,
        c.summary_a,
        c.summary_b,
        c.similarity,
        c.method,
        c.detector,
        c.created,
        c.status,
        c.verdict_ts,
        c.note,
        c.verdict_hash_a,
        c.verdict_hash_b,
    )


def _float(v: Any) -> float:
    return struct.unpack(">d", bytes.fromhex(v["bits"]))[0]


def _case(c: dict[str, Any]) -> dict[str, Any]:
    out = dict(c)
    k = c["k"]
    if k == "hash":
        try:
            out["r"] = cf._body_hash(_get(c, "x"))
        except UnicodeEncodeError as e:
            out["r"] = _err(e)
    elif k == "pid":
        try:
            out["r"] = cf._pair_id(_get(c, "a"), _get(c, "b"))
        except UnicodeEncodeError as e:
            out["r"] = _err(e)
    elif k == "neg":
        out["r"] = cf._has_negation(_get(c, "x"))
    elif k == "clause":
        a, n = cf._clause_polarity(_get(c, "x"))
        out["r"] = [_sorted(a), _sorted(n)]
    elif k == "sig":
        s = cf._body_signals(_get(c, "x"))
        out["r"] = [
            s.has_negation,
            _sorted(s.asserted),
            _sorted(s.negated),
            _sorted(s.numbers),
        ]
    elif k == "pair":
        sa = cf._body_signals(_get(c, "x"))
        sb = cf._body_signals(_get(c, "y"))
        out["r"] = [
            cf._polarity_flip(sa, sb),
            cf._numeric_divergence(sa.numbers, sb.numbers),
            cf._conflict_signal(sa, sb),
        ]
    elif k == "keep":
        ma, mb = (_memory(m) for m in c["m"])
        try:
            kept, dup = cf._pick_keeper(ma, mb)
            assert {id(kept), id(dup)} == {id(ma), id(mb)}
            out["r"] = "ab" if kept is ma else "ba"
        except cf.ConflictingPair as e:
            out["r"] = {"err": "ConflictingPair", "det": e.detector}
        except TypeError as e:
            out["r"] = _err(e)
    elif k == "from":
        try:
            row = cf.ConflictCandidate.from_dict(c["raw_py"])
            out["r"] = repr(_fields(row))
        except (KeyError, TypeError, ValueError, OverflowError) as e:
            out["r"] = _err(e)
        del out["raw_py"]
    elif k == "dict":
        kind, args = c["c"][0], list(c["c"][1:])
        args = [_float(a) if isinstance(a, dict) else a for a in args]
        obj: Any
        if kind == "cand":
            obj = cf.ConflictCandidate(*args)
        elif kind == "dedup":
            obj = cf.DedupCandidate(*args)
        else:
            obj = cf.PolaritySkippedPair(*args)
        out["r"] = repr(obj.to_dict())
    elif k == "scan":
        th = None if c["th"] is None else _float({"bits": c["th"]})
        out["d"], out["f"] = _scan([_memory(m) for m in c["m"]], th)
    return out


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def X(k: str, x: str, **kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"k": k}
    _put(out, "x", x)
    out.update(kw)
    return out


def _pid(a: str, b: str) -> dict[str, Any]:
    out: dict[str, Any] = {"k": "pid"}
    _put(out, "a", a)
    _put(out, "b", b)
    return out


def _pair(x: str, y: str) -> dict[str, Any]:
    out: dict[str, Any] = {"k": "pair"}
    _put(out, "x", x)
    _put(out, "y", y)
    return out


def _keep(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    return {"k": "keep", "m": [a, b]}


def _from(raw: dict[str, Any], spelled: Any = None) -> dict[str, Any]:
    """``spelled`` stands in for ``neutral(raw)`` where `str` of an int
    refuses it."""
    return {"k": "from", "raw": spelled or neutral(raw), "raw_py": raw}


def _scan_case(ms: list[dict[str, Any]], th: float | None = None) -> dict[str, Any]:
    return {"k": "scan", "m": ms, "th": None if th is None else _bits(th)}


SUDO = "Do not use sudo for deploys."
SUDO_POS = "Use sudo for deploys."
INVERSE_A = "Deploy with the blue-green strategy; never do in-place."
INVERSE_B = "Never deploy with the blue-green strategy; do in-place."
SQUASH_A = "Always squash-merge; do not rebase."
SQUASH_B = "Never squash-merge; always rebase."
CLI_A = "Run migrations with the CLI, not by hand."
CLI_B = "Run migrations with the CLI by hand."
PORT_A = "The auth service listens on port 5432 in the cluster."
PORT_B = "The auth service listens on port 5433 in the cluster."
PORT_DETAIL = "The auth service listens on port 5432 in the cluster since 2026-07-20."
BOTH_SIDES = "Use sudo for deploys; never use sudo for backups."
LONG = "Deploy " + "word " * 40 + "with the blue-green strategy and never in-place."
FULL = {
    "id": "cf-1",
    "a_id": "a",
    "b_id": "b",
    "summary_a": "sa",
    "summary_b": "sb",
    "similarity": 0.81234567,
    "method": "jaccard",
    "detector": "numeric",
    "created": "2026-01-01T00:00:00+00:00",
    "status": "dismissed",
    "verdict_ts": "2026-01-02T00:00:00+00:00",
    "note": "fine",
    "verdict_hash_a": "0123456789abcdef",
    "verdict_hash_b": "fedcba9876543210",
}
BASE = {"id": "cf-2", "a_id": "a", "b_id": "b"}
# 4,301 ones: one digit past `str`'s limit on an int, so `neutral` cannot
# spell it and `_huge` does.
HUGE_INT = (10**4301 - 1) // 9
HUGE = {"t": "int", "v": "1" * 4301}


def _with(**kw: Any) -> dict[str, Any]:
    out = dict(BASE)
    out.update(kw)
    return out


def _without(*keys: str, **kw: Any) -> dict[str, Any]:
    out = {k: v for k, v in FULL.items() if k not in keys}
    out.update(kw)
    return out


def _huge(raw: dict[str, Any], key: str) -> dict[str, Any]:
    """A ``from`` case of ``raw`` with ``HUGE_INT`` at ``key``."""
    raw = {**raw, key: HUGE_INT}
    items = [[neutral(k), HUGE if k == key else neutral(v)] for k, v in raw.items()]
    return _from(raw, {"t": "dict", "v": items})


def _ascii_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    # The hashes: strict UTF-8, a surrogate raises; ids in code point order.
    for x in ("", "abc", "Do not use sudo.\n", "\U0001f600 past the BMP", "a\ud800b"):
        cases.append(X("hash", x))
    for a, b in (
        ("a", "b"),
        ("b", "a"),
        ("a", "a"),
        ("Z", "a"),
        ("\U0001f600", "￿"),
        ("￿", "\U0001f600"),
        ("01J0", "01J0 "),
        ("a:b", "c"),
        ("a", "b:c"),
        ("\ud800", "a"),
        ("a", "\udfff"),
    ):
        cases.append(_pid(a, b))
    # _has_negation: each marker, contractions with both apostrophes,
    # markers inside words, digits, case, a left quote that stays.
    for x in (
        "",
        "Use sudo.",
        "Do NOT use sudo.",
        "no",
        "None of it",
        "nor that",
        "neither",
        "without a lock",
        "cannot",
        "can not",
        "don't",
        "DON’T",
        "won't",
        "isn’t",
        "aren't",
        "wasn't weren't",
        "shouldn't",
        "wouldn't",
        "couldn't",
        "didn't",
        "doesn't",
        "can't",
        "don‘t",
        "don``t",
        "nothing notable",
        "knot",
        "not5",
        "no.",
        "_no_",
        "no-op",
        "never2",
        "never",
        "a\ud800not",
        "n'o't",
    ):
        cases.append(X("neg", x))
    # _clause_polarity: the clause boundaries, a comma that is none, empty
    # and stopword-only clauses, a term on both sides.
    for x in (
        "",
        SUDO,
        SUDO_POS,
        INVERSE_A,
        INVERSE_B,
        SQUASH_A,
        SQUASH_B,
        CLI_A,
        CLI_B,
        BOTH_SIDES,
        "Use A, B, or C; do not use D.",
        "Do not use A, B, or C.",
        "a...b;;;c!!!d???e\n\nf",
        "Use sudo\nnever sudo",
        "the; and; of.",
        "Never.",
        "not the cache! use the cache?",
        "Use x. Don't use y. Use y.",
        "isn’t fast; is slow",
        "a\ud800b; not \udfffc",
    ):
        cases.append(X("clause", x))
    # _body_signals: the stamp stripped first, a stamp carrying a
    # negator, numbers one- and two-sided.
    for x in (
        SUDO_POS,
        SUDO_POS + NEG_STAMP,
        SUDO_POS + STAMP,
        SUDO + STAMP,
        SUDO_POS + NEG_STAMP + " ",
        SUDO_POS + "\n_(consolidate --llm --from-transcript: not)_",
        PORT_A,
        PORT_DETAIL,
        "version 3.27.0 and v2 on 2026-07-20; not 01ARZ3NDEKTSV4RRFFQ69G5FAV.",
        "port 5432. Port 5432!",
        "",
    ):
        cases.append(X("sig", x))
    # The guards over pairs: whole-body, clause-mutual, one-sided,
    # numeric mutual and one-sided, both signals (polarity first).
    for x, y in (
        (SUDO, SUDO_POS),
        (SUDO_POS, SUDO),
        (INVERSE_A, INVERSE_B),
        (SQUASH_A, SQUASH_B),
        (CLI_A, CLI_B),
        (CLI_A, "Run migrations with the CLI."),
        (PORT_A, PORT_B),
        (PORT_A, PORT_DETAIL),
        (PORT_DETAIL, PORT_A),
        (PORT_A + " Not on 5434.", PORT_B),
        ("It is fast.", "It is not slow."),
        (BOTH_SIDES, SUDO_POS),
        (SUDO_POS + NEG_STAMP, SUDO_POS),
        ("Use the cache; never the disk.", "Never the cache; use the disk."),
        ("Use the cache; never the disk.", "Use the disk; never the cache!"),
        ("Use the cache, never the disk.", "Use the disk, never the cache."),
        ("", ""),
    ):
        cases.append(_pair(x, y))
    # _pick_keeper: the guard before tier 0, each tier both ways and its
    # ties, naive and aware.
    attested = {"v": ["src/a.py"]}
    checked = {"l": "2024-01-01T00:00:00+00:00"}
    cases += [
        _keep(M("a", SUDO, **attested), M("b", SUDO_POS)),
        _keep(M("a", PORT_A, **attested), M("b", PORT_B)),
        _keep(M("a", PORT_A, **checked), M("b", PORT_A)),
        _keep(M("a", PORT_A), M("b", PORT_A, **checked)),
        _keep(M("a", PORT_A, **attested), M("b", PORT_A, **checked)),
        _keep(M("a", PORT_A, u="2024-03-02T00:00:00+00:00"), M("b", PORT_A)),
        _keep(M("a", PORT_A), M("b", PORT_A, u="2024-03-02T00:00:00+00:00")),
        # One instant at two offsets: equal, so the later tiers decide.
        _keep(M("a", PORT_A, u="2024-03-01T13:00:00+01:00"), M("b", PORT_A)),
        _keep(M("b", PORT_A, u="2024-03-01T13:00:00+01:00"), M("a", PORT_A)),
        # The later wall clock is the earlier instant.
        _keep(M("a", PORT_A, u="2024-03-01T12:30:00+01:00"), M("b", PORT_A)),
        _keep(M("a", PORT_A, u="2024-03-01T12:00:00.000001+00:00"), M("b", PORT_A)),
        # Naive against naive by the wall clock; naive against aware raises
        # only once the tiers reach `updated`.
        _keep(
            M("a", PORT_A, u="2024-03-02T00:00:00"),
            M("b", PORT_A, u="2024-03-01T00:00:00"),
        ),
        _keep(
            M("a", PORT_A, u="2024-03-01T00:00:00"),
            M("b", PORT_A, u="2024-03-01T00:00:00"),
        ),
        _keep(M("a", PORT_A, u="2024-03-01T12:00:00"), M("b", PORT_A)),
        _keep(M("a", PORT_A, u="2024-03-01T12:00:00", **attested), M("b", PORT_A)),
        _keep(M("a", SUDO, u="2024-03-01T12:00:00"), M("b", SUDO_POS)),
        _keep(M("a", PORT_A, v=["x", "y"], l=U), M("b", PORT_A, v=["x"])),
        _keep(M("a", PORT_A, v=["x"]), M("b", PORT_A, v=["x", "y"])),
        _keep(M("a", PORT_A), M("b", PORT_A)),
        _keep(M("b", PORT_A), M("a", PORT_A)),
        _keep(M("a", PORT_A), M("a", PORT_A)),
        _keep(M("\U0001f600", PORT_A), M("￿", PORT_A)),
        _keep(M("Z", PORT_A), M("a", PORT_A)),
        _keep(M("a", PORT_A, v=[]), M("b", PORT_A, l="2024-03-01T12:00:00")),
    ]
    # from_dict: every field shape and each raise, in the order of reads.
    cases += [
        _from(FULL),
        _from(dict(BASE)),
        _from(_without("id")),
        _from(_without("a_id")),
        _from(_without("b_id")),
        _from(_without("b_id", similarity=None)),
        _huge(_without("id"), "status"),
        _huge(_with(similarity=None), "verdict_ts"),
        _huge(_with(), "id"),
        _huge(_with(), "note"),
        _huge(_with(), "verdict_hash_b"),
        _from(_with(id=None, a_id=5, b_id=1.0)),
        _from(_with(id=True, a_id=1e16, b_id=-0.0)),
        _from(_with(id=[1, "a", None], a_id={"k": [True]}, b_id=[])),
        _from(_with(id="a\ud800", a_id="\udfff", summary_a="s\ud800")),
        _from(_with(id="it's", a_id='say "x"', b_id="tab\there\n")),
    ]
    for status in (
        "pending",
        "confirmed",
        "dismissed",
        "bogus",
        "Pending",
        "",
        None,
        1,
    ):
        cases.append(_from(_with(status=status)))
    sim: Any
    for sim in (
        3,
        -7,
        True,
        False,
        0.5,
        -0.0,
        10**308,
        10**309,
        -(10**400),
        2**1024 - 2**970,
        2**1024 - 2**970 - 1,
        " 1_000.5 ",
        "\t\n\x0b\x0c\r1.5\r",
        # A trailing vertical tab alone: `float()` strips it as ASCII
        # whitespace (conflicts-1's review, B1).
        "1.5\x0b",
        "\x1c1",
        "1\x00",
        "inf",
        "-Infinity",
        "+INF",
        "nan",
        "-NaN",
        "1e400",
        "1e-400",
        ".5",
        "5.",
        "1_0",
        "1__0",
        "_1",
        "1_",
        "1_.5",
        "1._5",
        "1e1_0",
        "0x10",
        "abc",
        "",
        " ",
        "1\ud800",
        None,
        [],
        {},
    ):
        cases.append(_from(_with(similarity=sim)))
    for key in ("verdict_ts", "note", "verdict_hash_a", "verdict_hash_b"):
        for v in (None, "", 0, False, "x", [None]):
            cases.append(_from(_with(**{key: v})))
    for key, default_case in (
        ("summary_a", None),
        ("summary_b", 0),
        ("method", None),
        ("detector", 2.5),
        ("created", None),
    ):
        cases.append(_from(_with(**{key: default_case})))
    # to_dict: with and without the optional keys; the similarity rounded.
    nan = neutral(float("nan"))
    cases += [
        {
            "k": "dict",
            "c": [
                "cand",
                *(neutral(v) if k == "similarity" else v for k, v in FULL.items()),
            ],
        },
        {
            "k": "dict",
            "c": ["cand", "i", "a", "b", "", "", neutral(0.12345), "m", "d", "c", "s"]
            + [None, None, None, None],
        },
        {
            "k": "dict",
            "c": ["cand", "i", "a", "b", "", "", nan, "m", "d", "c", "pending"]
            + [None, "n", None, "h"],
        },
        {
            "k": "dict",
            "c": ["cand", "i", "a", "b", "", "", neutral(0.0), "m", "d", "c", "x"]
            + ["", None, "", None],
        },
    ]
    for sim in (0.99995, 0.00005, 0.12345, 2.675e-5, -0.0, 1e300, float("inf"), 1 / 3):
        cases.append(
            {"k": "dict", "c": ["dedup", "k", "ks", "d", "ds", neutral(sim), "jaccard"]}
        )
        cases.append(
            {
                "k": "dict",
                "c": ["skip", "a", "sa", "b", "sb", neutral(sim), "jaccard", "numeric"],
            }
        )
    # The scan: fewer than two, the thresholds, both detectors, ties kept
    # in scan order, the stamp, empty token sets, one pair twice.
    sudo2 = "Never use sudo for deploys."
    cases += [
        _scan_case([]),
        _scan_case([M("a", SUDO)]),
        _scan_case([M("a", "\ud800 not"), M("b", "\ud800 not")], 0.1),
        _scan_case([M("a", SUDO), M("b", SUDO_POS)]),
        _scan_case([M("a", SUDO_POS), M("b", SUDO_POS + " ")]),
        _scan_case([M("a", INVERSE_A), M("b", INVERSE_B)], 0.9),
        _scan_case([M("a", SQUASH_A), M("b", SQUASH_B)]),
        _scan_case([M("a", CLI_A), M("b", CLI_B)]),
        _scan_case([M("a", PORT_A), M("b", PORT_B), M("c", PORT_DETAIL)]),
        _scan_case([M("a", PORT_A), M("b", PORT_B), M("c", PORT_DETAIL)], 0.9),
        _scan_case([M("a", PORT_A), M("b", PORT_B), M("c", PORT_DETAIL)], 0.0),
        _scan_case([M("a", PORT_A), M("b", "zzz qqq")], 0.0),
        _scan_case([M("a", PORT_A), M("b", "zzz qqq")], float("nan")),
        _scan_case([M("a", PORT_A), M("b", "zzz qqq")], -1.0),
        _scan_case([M("a", PORT_A), M("b", PORT_A)], float("inf")),
        _scan_case([M("a", PORT_A), M("b", PORT_A)], 1.0),
        _scan_case([M("a", "the and of"), M("b", "the and of"), M("c", PORT_A)], 0.0),
        _scan_case([M("a", SUDO_POS + NEG_STAMP), M("b", SUDO_POS)]),
        _scan_case([M("a", SUDO + STAMP), M("b", SUDO_POS)]),
        _scan_case([M("a", LONG), M("b", LONG.replace("never", "always"))]),
        # Equal similarities keep scan order; two memories with one id give
        # one pair id for two pairs, and the first is kept.
        _scan_case([M("a", SUDO), M("b", SUDO_POS), M("c", sudo2), M("d", SUDO_POS)]),
        _scan_case([M("a", SUDO), M("a", SUDO_POS), M("a", SUDO), M("a", SUDO_POS)]),
        _scan_case([M("b", SUDO), M("a", SUDO_POS), M("a", sudo2)]),
        _scan_case([M("x", PORT_A), M("y", PORT_B), M("x", PORT_B)], 0.5),
        # A keeper by every tier inside the scan, and the naive raise.
        _scan_case(
            [
                M("a", PORT_A, u="2024-03-02T00:00:00+00:00"),
                M("b", PORT_A, v=["p"]),
                M("c", PORT_A, v=["p", "q"]),
                M("d", PORT_A),
            ]
        ),
        _scan_case([M("a", PORT_A, u="2024-03-01T12:00:00"), M("b", PORT_A)]),
        _scan_case([M("a", PORT_A, u="2024-03-01T12:00:00"), M("b", PORT_B)]),
        _scan_case([M("a", SUDO), M("b\ud800", SUDO_POS)]),
        _scan_case([M("a", PORT_A), M("b\ud800", PORT_A)]),
    ]
    cases.append(_sweep())
    return cases


def _unicode_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for x in (
        "İSNT",
        "ﬁ not",
        "NOT ﬁ",
        "ΝΟ not",
        "ＮＯＴ",
        "naïve: not",
        "nö",
        "é not é",
        "DON’T ÉCRIRE",
    ):
        cases.append(X("neg", x))
    for x in (
        "Utilisez sudo; ne pas utiliser sudo, not ici.",
        "Ἀθῆναι; never Ἀθῆναι",
        "naïve café. NOT café",
    ):
        cases.append(X("clause", x))
    for x in (
        "port ٥٤٣٢ and 5432",
        "port ５４３２; not 5433",
        "version Ⅻ and 3.27.0",
        "Café NOT 5432",
    ):
        cases.append(X("sig", x))
    for x, y in (
        ("Café listens on 5432.", "Café listens on 5433."),
        ("Café listens on ٥٤٣٢.", "Café listens on ٥٤٣٣."),
        ("İSNT café", "café"),
        ("ﬁ not café", "ﬁ café"),
    ):
        cases.append(_pair(x, y))
    cases += [
        _keep(M("é", "Café listens on 5432."), M("e", "Café listens on 5432.")),
        _keep(M("a", "Café listens on 5432."), M("b", "Café listens on 5433.")),
    ]
    for sim in (" 1.5　", "١٢٫٥", "١٢.٥", " 1e2", " "):
        cases.append(_from(_with(similarity=sim)))
    cases.append(_from(_with(id="café", a_id=["é"], b_id={"ü": "ÿ"})))
    cases.append(
        {
            "k": "dict",
            "c": ["skip", "é", "s é", "b", "sb", neutral(0.5), "jaccard", "x"],
        }
    )
    cases += [
        _scan_case([M("a", "Café listens on 5432."), M("b", "Café listens on 5433.")]),
        _scan_case(
            [M("a", "Ne pas utiliser sudo café."), M("b", "Utiliser sudo café.")]
        ),
        _scan_case([M("a", "  " + "é" * 150 + " fin"), M("b", "é" * 150 + " fin")]),
        _scan_case([M("a", "İSNT café thé"), M("b", "café thé")]),
    ]
    return cases


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def _sweep_body(lcg: Lcg) -> str:
    n = 3 + lcg.draw() % 8
    parts = [SWEEP_WORDS[lcg.draw() % len(SWEEP_WORDS)]]
    for _ in range(n - 1):
        parts.append(SWEEP_SEPS[lcg.draw() % len(SWEEP_SEPS)])
        parts.append(SWEEP_WORDS[lcg.draw() % len(SWEEP_WORDS)])
    return "".join(parts)


def _sweep_set(lcg: Lcg) -> list[dict[str, Any]]:
    """``2 + draw % 11`` memories: a shared base body, each member the base
    with up to two words replaced (the word drawn before its place, as
    Python evaluates an assignment's value before its target) and a
    draw-picked stamp, id, `updated`, `verified_paths` and
    `last_verified_at`."""
    base = _sweep_body(lcg).split(" ")
    out = []
    for _ in range(2 + lcg.draw() % 11):
        words = list(base)
        for _ in range(lcg.draw() % 3):
            words[lcg.draw() % len(words)] = SWEEP_WORDS[lcg.draw() % len(SWEEP_WORDS)]
        body = " ".join(words)
        stamp = lcg.draw() % 6
        if stamp == 0:
            body += STAMP
        elif stamp == 1:
            body += NEG_STAMP
        m: dict[str, Any] = {
            "i": SWEEP_IDS[lcg.draw() % len(SWEEP_IDS)],
            "b": body,
            "u": SWEEP_UPDATED[lcg.draw() % len(SWEEP_UPDATED)],
            "v": ["p", "q"][: lcg.draw() % 3],
        }
        if lcg.draw() % 5 == 0:
            m["l"] = U
        out.append(m)
    return out


def sweep_sets() -> list[list[dict[str, Any]]]:
    lcg = Lcg(SWEEP_SEED)
    return [_sweep_set(lcg) for _ in range(SWEEP_COUNT)]


def _sweep() -> dict[str, Any]:
    digest = hashlib.sha256()
    lines = 0
    for ms in sweep_sets():
        memories = [_memory(m) for m in ms]
        for th in SWEEP_THRESHOLDS:
            line = _scan(memories, th)
            text = json.dumps(line, separators=(",", ":"), ensure_ascii=True)
            digest.update(text.encode("ascii") + b"\n")
            lines += 1
    return {
        "k": "sweep",
        "seed": SWEEP_SEED,
        "count": SWEEP_COUNT,
        "ths": SWEEP_THRESHOLDS,
        "words": SWEEP_WORDS,
        "seps": SWEEP_SEPS,
        "updated": SWEEP_UPDATED,
        "ids": SWEEP_IDS,
        "stamps": [STAMP, NEG_STAMP],
        "lines": lines,
        "sha256": digest.hexdigest(),
    }


def _file(name: str, cases: list[dict[str, Any]]) -> bytes:
    lines = [c if c["k"] == "sweep" else _case(c) for c in cases]
    header = {
        "fixture": name,
        "cases": len(lines),
        "statuses": list(cf._VALID_STATUSES),
        "threshold": _bits(cf._DEFAULT_JACCARD_THRESHOLD),
        "markers": sorted(cf._NEGATION_MARKERS),
        "patterns": [cf._NEGATION_TOKEN_RE.pattern, cf._CLAUSE_SPLIT_RE.pattern],
        "now": NOW.isoformat(),
        "u": U,
    }
    return jsonl([header, *lines])


def generate() -> dict[str, bytes]:
    return {"conflicts.jsonl": _file("conflicts", _ascii_cases())}


def generate_unicode() -> dict[str, bytes]:
    return {"conflicts_unicode.jsonl": _file("conflicts_unicode", _unicode_cases())}


# ---------------------------------------------------------------------------
# The queue (`conflicts_queue.jsonl`, `conflicts_queue_store.jsonl`)
# ---------------------------------------------------------------------------
#
# The native build reads them in
# ``native/crates/bettermemory-core/src/conflicts/queue.rs``. This pins it to
# `ConflictQueue` (`load`, `pending`, `file_pair`, `upsert_scan`,
# `_judged_content_changed`, `resolve`, `_replace_all`), `scan_conflicts`,
# `split_judgeable` and `conflicts_pending_count`. ``conflicts_queue.jsonl``
# runs each case (by ``q``) over `Stub`, the store as the queue duck-types
# it, and records its calls, the answer (``r``) or the error (`_outcome`),
# the warnings and the table where it changed; its ``sweep`` row draws
# ``count`` sequences of ``ops`` calls by `Lcg` from ``seed`` (`_sweep_op`)
# and pins their lines by count and SHA-256. ``conflicts_queue_store.jsonl``
# runs cases of steps over stores `Store.create` makes, as
# ``store_rows_written.jsonl``'s are, and records per call what that file
# records of a step's clocks, the log rows appended and the table after it.
# A ``nested`` step runs its ``calls`` inside one ``store.batch()`` the
# caller holds open (each call joins it; an error is that call's outcome)
# and records the batch as one step. Its header counts the ``cases`` and
# the ``calls`` (every step that is not SQL).
#
# A value of the queue's rows and calls is written compactly by `_v`: null,
# a bool, a str (or ``{"cps": [...]}`` where it holds a surrogate),
# ``{"i": digits}``, ``{"f": bits}``, a list, ``{"tu": [...]}`` for a
# tuple, ``{"d": [[k, v], ...]}`` for a dict in its order and, in the
# store's tables, ``{"b": hex}`` for bytes. A dict whose keys are some of
# the table's fourteen columns in their order is ``{"c": [...]}``, a value
# per column up to the last present, ``{}`` for a column it lacks. A queue
# row (`_row`) is its fourteen fields in declaration order, the similarity
# as its bits and an unset optional field as null.

COLUMNS = list(_CONFLICT_COLUMNS)
STUB_PATH = "stub/memory.sqlite"
QUEUE_NOW = datetime(2026, 2, 3, 4, 5, 6, 789012, tzinfo=timezone.utc)
C0 = "c0"
C1 = "c1"
VERDICT = "2026-01-10T00:00:00+00:00"
EARLIER = "2026-01-09T00:00:00+00:00"
LATER = "2026-01-11T00:00:00+00:00"
STAMP_RECORD = {
    "id": "cf-stamp",
    "a_id": "x",
    "b_id": "y",
    "summary_a": "",
    "summary_b": "",
    "similarity": 0.5,
    "method": "jaccard",
    "detector": "polarity",
    "created": C0,
    "status": "pending",
}
_RAISES: dict[str, type[Exception]] = {
    "RuntimeError": RuntimeError,
    "KeyError": KeyError,
    "OverflowError": OverflowError,
}

QUEUE_SWEEP_SEED = 20261127
QUEUE_SWEEP_COUNT = 1_000
QUEUE_SWEEP_OPS = 4
Q_IDS = ["m0", "m1", "m2", "m3", "m4"]
Q_PAIRS = ["cf-a", "cf-b", "cf-c", "cf-d", "cf-e", "cf-f"]
Q_BODIES = ["alpha", "beta", "gamma", "alpha "]
Q_UPDATED = [
    "2026-01-01T00:00:00+00:00",
    "2026-01-03T00:00:00+00:00",
    "2026-01-02T00:30:00+01:00",
]
Q_VERDICT_TS = [
    None,
    "2026-01-02T00:00:00+00:00",
    "2026-01-02T00:00:00Z",
    "2026-01-02T00:00:00",
    "garbage",
]
Q_STATUSES = ["pending", "confirmed", "dismissed", "bogus"]
Q_VERDICTS = ["confirmed", "dismissed", "pending"]
Q_DETECTORS = ["polarity", "numeric", ""]
Q_SIMS = [0.5, 0.81234, 0.8123, float("nan"), -0.0, 0.75]
Q_SUMMARIES = ["s1", "s2"]
Q_CREATED = [C0, C1]
Q_NOTES = [None, "n"]


def _v(x: Any) -> Any:
    if x is None or isinstance(x, bool):
        return x
    if isinstance(x, int):
        return {"i": str(x)}
    if isinstance(x, float):
        return {"f": _bits(x)}
    if isinstance(x, str):
        return _t(x)
    if isinstance(x, bytes):
        return {"b": x.hex()}
    if isinstance(x, list):
        return [_v(i) for i in x]
    if isinstance(x, tuple):
        return {"tu": [_v(i) for i in x]}
    if isinstance(x, dict):
        keys = list(x)
        at = [COLUMNS.index(k) if k in COLUMNS else -1 for k in keys]
        if keys and -1 not in at and at == sorted(at):
            return {"c": [_v(x[c]) if c in x else {} for c in COLUMNS[: at[-1] + 1]]}
        return {"d": [[_v(k), _v(v)] for k, v in x.items()]}
    raise TypeError(type(x).__name__)


def _dv(j: Any) -> Any:
    """`_v`'s inverse."""
    if j is None or isinstance(j, (bool, str)):
        return j
    if isinstance(j, list):
        return [_dv(i) for i in j]
    ((tag, inner),) = j.items()
    if tag == "i":
        return int(inner)
    if tag == "f":
        return struct.unpack(">d", bytes.fromhex(inner))[0]
    if tag == "cps":
        return "".join(chr(c) for c in inner)
    if tag == "tu":
        return tuple(_dv(i) for i in inner)
    if tag == "d":
        return {_dv(k): _dv(v) for k, v in inner}
    if tag == "c":
        return {c: _dv(v) for c, v in zip(COLUMNS, inner) if v != {}}
    raise ValueError(tag)


def _opt(x: str | None) -> Any:
    return None if x is None else _t(x)


def _row(c: cf.ConflictCandidate) -> list[Any]:
    return [
        _t(c.id),
        _t(c.a_id),
        _t(c.b_id),
        _t(c.summary_a),
        _t(c.summary_b),
        _bits(c.similarity),
        _t(c.method),
        _t(c.detector),
        _t(c.created),
        _t(c.status),
        _opt(c.verdict_ts),
        _opt(c.note),
        _opt(c.verdict_hash_a),
        _opt(c.verdict_hash_b),
    ]


def _cand(r: list[Any]) -> cf.ConflictCandidate:
    """`_row`'s inverse."""
    texts = [_dv(x) for x in r]
    texts[5] = struct.unpack(">d", bytes.fromhex(r[5]))[0]
    return cf.ConflictCandidate(*texts)


def R(id: str, a: str, b: str, **kw: Any) -> dict[str, Any]:
    """A stored row as `to_dict` writes it, with `kw` over the defaults."""
    out: dict[str, Any] = {
        "id": id,
        "a_id": a,
        "b_id": b,
        "summary_a": "sa",
        "summary_b": "sb",
        "similarity": 0.8,
        "method": "jaccard",
        "detector": "polarity",
        "created": C0,
        "status": "pending",
    }
    out.update(kw)
    return out


def F(id: str, a: str, b: str, **kw: Any) -> list[Any]:
    """A fresh candidate (`_row`), with `kw` over the defaults."""
    fields: dict[str, Any] = {
        "summary_a": "fa",
        "summary_b": "fb",
        "similarity": 0.9,
        "method": "jaccard",
        "detector": "numeric",
        "created": C1,
        "status": "pending",
    }
    fields.update(kw)
    return _row(
        cf.ConflictCandidate(
            id,
            a,
            b,
            fields["summary_a"],
            fields["summary_b"],
            fields["similarity"],
            fields["method"],
            fields["detector"],
            fields["created"],
            fields["status"],
            fields.get("verdict_ts"),
            fields.get("note"),
            fields.get("verdict_hash_a"),
            fields.get("verdict_hash_b"),
        )
    )


class Stub:
    """The store as `ConflictQueue` duck-types it, recording each call:
    ``["batch"]`` then ``["commit"]`` or ``["rollback", class]``,
    ``["list"]``, ``["count"]``, ``["put", record, session]`` and
    ``["delete", id, session]``. The table is a list of dicts: a put
    replaces the first row whose ``id`` is that str, or appends; a delete
    removes every such row; a batch that raises puts the table back. The
    ``n``-th call of ``fail["op"]`` (counted from the stub's start) raises
    ``fail["class"]``; `count_memories` answers ``counts`` in turn, the
    last again once they run out."""

    path = STUB_PATH

    def __init__(self, spec: dict[str, Any]) -> None:
        self.table: list[dict[Any, Any]] = [_dv(r) for r in spec.get("rows", [])]
        self.counts: list[int] = list(spec.get("counts", [0]))
        self.fail: dict[str, Any] | None = spec.get("fail")
        self.n: dict[str, int] = {}
        self.calls: list[Any] = []

    def _call(self, op: str, *args: Any) -> None:
        self.calls.append([op, *args])
        self.n[op] = self.n.get(op, 0) + 1
        f = self.fail
        if f is not None and f["op"] == op and f["at"] == self.n[op]:
            raise _RAISES[f["class"]]("stub")

    def list_conflicts(self) -> list[dict[Any, Any]]:
        self._call("list")
        return [dict(r) for r in self.table]

    def count_memories(self) -> int:
        self._call("count")
        return self.counts[min(self.n["count"], len(self.counts)) - 1]

    def put_conflict(
        self, record: dict[str, Any], *, session: str | None = None
    ) -> None:
        self._call("put", _v(record), session)
        for i, r in enumerate(self.table):
            if r.get("id") == record["id"]:
                self.table[i] = dict(record)
                return
        self.table.append(dict(record))

    def delete_conflict(self, conflict_id: str, *, session: str | None = None) -> None:
        self._call("delete", _t(conflict_id), session)
        self.table = [r for r in self.table if r.get("id") != conflict_id]

    @contextmanager
    def batch(self) -> Iterator[Stub]:
        self.calls.append(["batch"])
        saved = [dict(r) for r in self.table]
        try:
            yield self
        except Exception as e:
            self.table = saved
            self.calls.append(["rollback", type(e).__name__])
            raise
        self.calls.append(["commit"])


class _Warnings(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.seen.append(record.getMessage())


@contextmanager
def _heard() -> Iterator[list[str]]:
    """`conflicts`' log warnings as their messages, kept off stderr."""
    handler = _Warnings()
    cf.log.addHandler(handler)
    propagate = cf.log.propagate
    cf.log.propagate = False
    try:
        with mock.patch.object(cf, "utcnow", lambda: QUEUE_NOW):
            yield handler.seen
    finally:
        cf.log.removeHandler(handler)
        cf.log.propagate = propagate


def _stamp(store: Any, actions: list[str]) -> Any:
    """`before_stamp` doing ``actions`` in order: ``log`` (``["stamp"]``
    into the stub's calls), ``put`` (`STAMP_RECORD` through the store),
    ``raise`` (RuntimeError)."""
    if not actions:
        return None

    def run() -> None:
        for a in actions:
            if a == "log":
                store.calls.append(["stamp"])
            elif a == "put":
                store.put_conflict(dict(STAMP_RECORD), session="s-stamp")
            elif a == "mem":
                with store._transaction() as conn:
                    store._mutate(
                        conn, "memory_put", _store_memory(9), session="s-stamp"
                    )
            else:
                raise RuntimeError("stamp")

    return run


def _bodies(pairs: list[Any]) -> dict[str, str]:
    return {_dv(k): _dv(v) for k, v in pairs}


def _counters(c: dict[str, int]) -> list[list[Any]]:
    return [[k, v] for k, v in c.items()]


def _queue_call(store: Any, c: dict[str, Any]) -> Any:
    """What a queue case's call answers (``q``), as the fixture spells it."""
    q = cf.ConflictQueue(store)
    k = c["q"]
    if k == "load":
        return [_row(r) for r in q.load()]
    if k == "pending":
        return [_row(r) for r in q.pending()]
    if k == "count":
        return cf.conflicts_pending_count(store)
    if k == "file":
        return _t(q.file_pair(_cand(c["cand"]), session=c.get("session")))
    if k == "upsert":
        ms = [_memory(m) for m in c["m"]]
        fresh = [_cand(x) for x in c["fresh"]]
        return _counters(
            q.upsert_scan(fresh, {m.id: m for m in ms}, session=c.get("session"))
        )
    if k == "scan":
        th = None if c["th"] is None else _float({"bits": c["th"]})
        ms = [_memory(m) for m in c["m"]]
        return _counters(
            cf.scan_conflicts(store, ms, threshold=th, session=c.get("session"))
        )
    if k == "resolve":
        hit = q.resolve(
            _dv(c["id"]),
            status=_dv(c["status"]),
            note=_dv(c["note"]),
            member_bodies=_bodies(c["bodies"]) if c["bodies"] is not None else None,
            before_stamp=_stamp(store, c.get("stamp", [])),
            session=c.get("session"),
        )
        return None if hit is None else _row(hit)
    raise AssertionError(k)


def _outcome(e: Exception) -> dict[str, Any]:
    """An error as the rows record it: its class, and its text where the
    text is the product's own (a ValueError or NotFoundError the queue or
    the store words); an interpreter's or SQLite's wording is left out."""
    out: dict[str, Any] = {"err": type(e).__name__}
    if type(e).__name__ in ("ValueError", "NotFoundError"):
        out["text"] = _t(str(e))
    return out


def _split(c: dict[str, Any]) -> dict[str, Any]:
    asked: list[Any] = []
    active = set(c["active"])

    def is_active(mid: str) -> bool:
        asked.append(_t(mid))
        if mid == c.get("raise"):
            raise RuntimeError("is_active")
        return mid in active

    try:
        judgeable, omitted = cf.split_judgeable(
            (_cand(x) for x in c["rows"]), is_active
        )
        r: Any = [[_t(j.id) for j in judgeable], omitted]
    except RuntimeError as e:
        r = _outcome(e)
    return {**c, "r": r, "asked": asked}


def _queue_case(c: dict[str, Any]) -> dict[str, Any]:
    if c["q"] == "split":
        return _split(c)
    if c["q"] == "judged":
        ms = [_memory(m) for m in c["m"]]
        try:
            r: Any = cf.ConflictQueue._judged_content_changed(
                _cand(c["c"]), {m.id: m for m in ms}
            )
        except (TypeError, UnicodeEncodeError) as e:
            r = _outcome(e)
        return {**c, "r": r}
    stub = Stub(c["stub"])
    with _heard() as warns:
        try:
            r = _queue_call(stub, c)
        except Exception as e:  # noqa: BLE001 - the raise is the record
            r = _outcome(e)
    out = {**c, "r": r, "calls": stub.calls}
    table = [_v(t) for t in stub.table]
    if table != c["stub"]["rows"]:
        out["table"] = table
    if warns:
        out["warn"] = warns
    return out


def _stub(rows: Any = (), counts: Any = (0,), fail: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {"rows": [_v(r) for r in rows], "counts": list(counts)}
    if fail is not None:
        out["fail"] = fail
    return out


def _fail(op: str, at: int = 1, cls: str = "RuntimeError") -> dict[str, Any]:
    return {"op": op, "at": at, "class": cls}


def Q(q: str, stub: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"q": q}
    if stub is not None:
        out["stub"] = stub
    out.update(kw)
    return out


def _mem(i: str, b: str = "", u: str = U) -> dict[str, Any]:
    m = M(i, b or f"body of {i}")
    m["u"] = u
    return m


def _up(
    rows: Any, fresh: Any, ms: Any, counts: Any = None, **kw: Any
) -> dict[str, Any]:
    if counts is None:
        counts = [len({m.get("i") for m in ms})]
    return Q(
        "upsert",
        _stub(rows, counts, kw.pop("fail", None)),
        fresh=fresh,
        m=ms,
        session=kw.pop("session", "s-u"),
        **kw,
    )


def _res(rows: Any, id: str, status: str, **kw: Any) -> dict[str, Any]:
    bodies = kw.pop("bodies", None)
    return Q(
        "resolve",
        _stub(rows, [0], kw.pop("fail", None)),
        id=id,
        status=status,
        note=kw.pop("note", None),
        bodies=None if bodies is None else [[_t(k), _t(b)] for k, b in bodies],
        stamp=kw.pop("stamp", []),
        session=kw.pop("session", "s-r"),
    )


def _queue_cases() -> list[dict[str, Any]]:
    ha = cf._body_hash("body of a")
    hb = cf._body_hash("body of b")
    ab = [_mem("a"), _mem("b")]
    abc = [*ab, _mem("c")]
    dis = R("cf-1", "a", "b", status="dismissed", verdict_ts=VERDICT, note="judged")
    cases: list[dict[str, Any]] = [
        # load: every row through from_dict; KeyError, TypeError and
        # ValueError skip a row, anything else (and the listing's own
        # raise, even a KeyError) goes on.
        Q("load", _stub()),
        Q(
            "load",
            _stub(
                [
                    R("cf-1", "a", "b"),
                    {"a_id": "a", "b_id": "b"},
                    R("cf-2", "a", "b", similarity="high"),
                    R("cf-3", "a", "b", similarity=[]),
                    R("cf-4", "a", "b", similarity=" 0.25\x0b", status="bogus"),
                    {"id": 7, "a_id": None, "b_id": 1.5, "status": None},
                    R("cf-5", "a", "b", verdict_ts=3, note=None, verdict_hash_a=""),
                    R("cf-1", "c", "d", status="confirmed"),
                ]
            ),
        ),
        Q("load", _stub([R("cf-1", "a", "b")], fail=_fail("list", 1, "KeyError"))),
        Q(
            "load",
            _stub([R("cf-1", "a", "b"), R("cf-2", "a", "b", similarity=10**400)]),
        ),
        Q("load", _stub([R("cf-1", "a", "b", similarity=float("nan"))])),
        Q(
            "pending",
            _stub(
                [
                    R("cf-1", "a", "b"),
                    R("cf-2", "a", "b", status="confirmed"),
                    R("cf-3", "a", "b", status="dismissed"),
                    R("cf-4", "a", "b", status="Pending"),
                    {"id": "cf-5", "a_id": "a", "b_id": "b"},
                    R("cf-6", "a", "b", similarity="x"),
                ]
            ),
        ),
        Q("pending", _stub(fail=_fail("list"))),
        # file_pair: the first loaded row of the id answers; else a put.
        Q("file", _stub(), cand=F("cf-1", "a", "b"), session="s-f"),
        Q("file", _stub([R("cf-1", "a", "b")]), cand=F("cf-1", "a", "b")),
        Q(
            "file",
            _stub([dis, R("cf-1", "a", "b")]),
            cand=F("cf-1", "a", "b"),
        ),
        Q(
            "file",
            _stub([R("cf-1", "a", "b", similarity="high")]),
            cand=F("cf-1", "a", "b"),
        ),
        Q(
            "file",
            _stub([R("cf-2", "a", "c")]),
            cand=F("cf-1", "a", "b", status="confirmed"),
        ),
        Q("file", _stub(fail=_fail("put")), cand=F("cf-1", "a", "b")),
        Q("file", _stub(fail=_fail("list")), cand=F("cf-1", "a", "b")),
        Q(
            "file",
            _stub([R("cf-9", "a", "b", similarity=10**400)]),
            cand=F("cf-1", "a", "b"),
        ),
        # upsert_scan.
        _up([], [], []),
        _up([], [F("cf-1", "a", "b"), F("cf-2", "b", "c")], abc),
        _up([R("cf-1", "a", "b")], [F("cf-1", "a", "b")], ab),
        _up([R("cf-1", "a", "b", detector="")], [F("cf-1", "a", "b")], ab),
        _up(
            [R("cf-1", "a", "b", summary_a="fa", summary_b="fb", similarity=0.9)],
            [F("cf-1", "a", "b", detector="value")],
            ab,
        ),
        # Unchanged at four places, and -0.0 against 0.0: no write.
        _up(
            [R("cf-1", "a", "b", summary_a="fa", summary_b="fb", similarity=0.8123)],
            [F("cf-1", "a", "b", similarity=0.81234)],
            ab,
        ),
        _up(
            [R("cf-1", "a", "b", summary_a="fa", summary_b="fb", similarity=-0.0)],
            [F("cf-1", "a", "b", similarity=0.0)],
            ab,
        ),
        # A NaN equals nothing: the row is put again on every scan.
        _up([R("cf-1", "a", "b", similarity=float("nan"))], [], ab),
        _up(
            [R("cf-1", "a", "b")],
            [F("cf-1", "a", "b", similarity=float("nan"))],
            ab,
        ),
        # A row whose keys come in another order is still unchanged.
        _up([dict(reversed(list(R("cf-1", "a", "b").items())))], [], ab),
        # Dismissed: a stored hash decides, whatever `updated` did.
        _up(
            [{**dis, "verdict_hash_a": ha, "verdict_hash_b": hb}],
            [F("cf-1", "a", "b")],
            [_mem("a", u=LATER), _mem("b", u=LATER)],
        ),
        _up(
            [{**dis, "verdict_hash_a": ha, "verdict_hash_b": hb}],
            [F("cf-1", "a", "b")],
            [_mem("a", "body of a changed"), _mem("b")],
        ),
        _up(
            [{**dis, "verdict_hash_a": ha, "verdict_hash_b": "0" * 16}],
            [F("cf-1", "a", "b")],
            ab,
        ),
        # Without hashes, `updated > verdict_ts`; no verdict time says changed.
        _up([dis], [F("cf-1", "a", "b")], [_mem("a", u=EARLIER), _mem("b", u=VERDICT)]),
        _up([dis], [F("cf-1", "a", "b")], [_mem("a", u=EARLIER), _mem("b", u=LATER)]),
        _up(
            [dis],
            [F("cf-1", "a", "b")],
            [_mem("a", u="2026-01-10T00:00:00.000001+00:00"), _mem("b", u=EARLIER)],
        ),
        _up([{**dis, "verdict_ts": None}], [F("cf-1", "a", "b")], ab),
        _up([{**dis, "verdict_ts": "garbage"}], [F("cf-1", "a", "b")], ab),
        _up(
            [{**dis, "verdict_ts": "2026-01-10T00:00:00Z"}],
            [F("cf-1", "a", "b")],
            [_mem("a", u="2026-01-10T00:00:00+00:00"), _mem("b", u=EARLIER)],
        ),
        # A naive verdict time reads as UTC.
        _up(
            [{**dis, "verdict_ts": "2026-01-10T00:00:00"}],
            [F("cf-1", "a", "b")],
            [_mem("a", u="2026-01-10T00:30:00+01:00"), _mem("b", u=EARLIER)],
        ),
        # A side outside the snapshot is passed over; a hash on one side.
        _up(
            [{**dis, "verdict_hash_a": ha}],
            [F("cf-1", "a", "b")],
            [_mem("a"), _mem("c")],
            counts=[2],
        ),
        _up(
            [{**dis, "verdict_hash_a": ha}],
            [F("cf-1", "a", "b")],
            [_mem("a"), _mem("b", u=LATER)],
        ),
        _up(
            [{**dis, "verdict_hash_b": hb}],
            [F("cf-1", "a", "b")],
            [_mem("c")],
            counts=[5],
        ),
        # A naive `updated` against the verdict, a hashed surrogate: raised,
        # rolled back.
        _up(
            [R("cf-0", "a", "c", detector=""), dis],
            [F("cf-0", "a", "c"), F("cf-1", "a", "b")],
            [_mem("a", u="2026-01-11T00:00:00"), _mem("b")],
        ),
        _up(
            [{**dis, "verdict_hash_a": ha}],
            [F("cf-1", "a", "b")],
            [M("a", "a\ud800b"), _mem("b")],
        ),
        # A confirmed row stands; a resurrected row keeps its detector.
        _up([R("cf-1", "a", "b", status="confirmed")], [F("cf-1", "a", "b")], ab),
        _up(
            [{**dis, "detector": "review"}],
            [F("cf-1", "a", "b", detector="numeric")],
            [_mem("a", u=LATER), _mem("b")],
        ),
        # Collection: the deletes in `before`'s order, then the puts.
        _up(
            [
                R("cf-3", "a", "z"),
                R("cf-1", "a", "b"),
                R("cf-2", "y", "b", status="dismissed"),
            ],
            [F("cf-4", "a", "b"), F("cf-1", "a", "b")],
            ab,
        ),
        # Deferred: the snapshot holds fewer than the store; the warning
        # reads the count a second time.
        _up(
            [R("cf-3", "a", "z"), R("cf-1", "a", "b")],
            [F("cf-4", "a", "b")],
            ab,
            counts=[5, 7],
        ),
        _up(
            [R("cf-3", "a", "z")],
            [],
            [_mem("a"), _mem("b"), _mem("a", "body of a again")],
            counts=[3],
        ),
        _up([R("cf-3", "a", "z")], [], [_mem("a"), _mem("b"), _mem("c")], counts=[2]),
        # Repeated ids: a dict keeps the first place and the last value.
        _up(
            [
                R("cf-1", "a", "b", summary_a="first"),
                R("cf-2", "a", "b"),
                R("cf-1", "a", "b", status="dismissed", verdict_ts=VERDICT),
            ],
            [F("cf-1", "a", "b"), F("cf-2", "a", "b")],
            ab,
        ),
        _up(
            [],
            [
                F("cf-1", "a", "b", detector=""),
                F("cf-1", "a", "b", summary_a="again", detector="value"),
                F("cf-1", "a", "b", detector="other"),
            ],
            ab,
        ),
        _up([], [F("cf-1", "a", "b", status="dismissed")], ab),
        # A repeated memory id is read as its last memory where a dismissed
        # row's hash is checked against it.
        _up(
            [{**dis, "verdict_hash_a": ha, "verdict_hash_b": hb}],
            [F("cf-1", "a", "b")],
            [_mem("a"), _mem("b"), _mem("a", "an edited body")],
        ),
        _up(
            [{**dis, "verdict_hash_a": ha, "verdict_hash_b": hb}],
            [F("cf-1", "a", "b")],
            [_mem("a", "an edited body"), _mem("b"), _mem("a")],
        ),
        # The store raising at each call: rolled back.
        _up([R("cf-1", "a", "b")], [F("cf-1", "a", "b")], ab, fail=_fail("count")),
        _up(
            [R("cf-1", "a", "z"), R("cf-2", "a", "b")],
            [F("cf-2", "a", "b")],
            ab,
            fail=_fail("delete"),
        ),
        _up([], [F("cf-1", "a", "b"), F("cf-2", "a", "b")], ab, fail=_fail("put", 2)),
        _up([R("cf-1", "a", "b", similarity=10**400)], [], ab),
        # scan_conflicts: the scan, then the fold. Inverse version pairs
        # (Linear ENG-427) are read as duplicates and filed nowhere.
        Q(
            "scan",
            _stub([], [2]),
            m=[M("a", SUDO), M("b", SUDO_POS)],
            th=None,
            session="s-scan",
        ),
        Q(
            "scan",
            _stub([], [2]),
            m=[
                M("a", "18.x is not supported; 20.x is supported."),
                M("b", "18.x is supported; 20.x is not supported."),
            ],
            th=None,
            session=None,
        ),
        Q(
            "scan",
            _stub([], [2]),
            m=[
                M("a", "Deploy with the blue-green strategy.", u="2024-03-01T12:00:00"),
                M("b", "Deploy with the blue-green strategy!"),
            ],
            th=None,
        ),
        Q(
            "scan",
            _stub([R("cf-x", "a", "q")], [2]),
            m=[M("a", PORT_A), M("b", PORT_B)],
            th=_bits(0.5),
        ),
        # resolve.
        _res([], "cf-1", "bogus"),
        _res([R("cf-1", "a", "b")], "cf-1", "pending"),
        _res([], "cf-1", "it's"),
        _res([], "cf-1", 'say "x" it\'s'),
        _res([R("cf-1", "a", "b")], "cf-2", "confirmed", stamp=["log"]),
        _res([R("cf-1", "a", "b", status="confirmed")], "cf-1", "dismissed"),
        _res(
            [R("cf-1", "a", "b", status="confirmed"), R("cf-1", "c", "d")],
            "cf-1",
            "dismissed",
            bodies=[["c", "body c"], ["d", "body d"]],
        ),
        _res(
            [R("cf-1", "a", "b", note="old")],
            "cf-1",
            "confirmed",
            note="real",
            bodies=[["a", "body of a"], ["b", "body of b"], ["c", "x"]],
            stamp=["log"],
        ),
        _res(
            [R("cf-1", "a", "b", note="old")], "cf-1", "dismissed", bodies=[["a", "A"]]
        ),
        _res([R("cf-1", "a", "b")], "cf-1", "dismissed", bodies=[]),
        _res([R("cf-1", "a", "b")], "cf-1", "dismissed", stamp=["log", "raise"]),
        _res(
            [R("cf-1", "a", "b")],
            "cf-1",
            "dismissed",
            bodies=[["b", "b\udfff"]],
            stamp=["log", "put"],
        ),
        _res(
            [R("cf-1", "a", "b")], "cf-1", "confirmed", fail=_fail("put"), stamp=["put"]
        ),
        _res(
            [R("cf-1", "a", "b"), R("cf-2", "a", "b", similarity=10**400)],
            "cf-1",
            "confirmed",
        ),
        # split_judgeable: left to right, short-circuit, order kept.
        Q(
            "split",
            rows=[
                F("p1", "a", "b"),
                F("p2", "a", "c"),
                F("p3", "d", "b"),
                F("p4", "b", "a"),
            ],
            active=["a", "b"],
        ),
        Q("split", rows=[], active=[]),
        {
            **Q(
                "split",
                rows=[F("p1", "a", "b"), F("p2", "c", "x"), F("p3", "a", "b")],
                active=["a", "b"],
            ),
            "raise": "x",
        },
        {
            **Q(
                "split",
                rows=[F("p1", "a", "c"), F("p2", "x", "b")],
                active=["a", "b", "c"],
            ),
            "raise": "x",
        },
        # conflicts_pending_count: any raise answers 0 and the warning.
        Q(
            "count",
            _stub(
                [
                    R("cf-1", "a", "b"),
                    R("cf-2", "a", "b", status="dismissed"),
                    R("cf-3", "a", "b"),
                ]
            ),
        ),
        Q("count", _stub([R("cf-1", "a", "b", similarity="x")])),
        Q("count", _stub([R("cf-1", "a", "b")], fail=_fail("list"))),
        Q("count", _stub([R("cf-1", "a", "b")], fail=_fail("list", 1, "KeyError"))),
        Q("count", _stub([R("cf-1", "a", "b", similarity=10**400)])),
    ]
    # _judged_content_changed directly.
    for c, ms in (
        (F("cf-1", "a", "b", status="dismissed"), ab),
        (F("cf-1", "a", "b", status="dismissed", verdict_ts=VERDICT), ab),
        (F("cf-1", "a", "b", verdict_ts=VERDICT), [_mem("b", u=LATER)]),
        (
            F("cf-1", "a", "b", verdict_ts=VERDICT, verdict_hash_a=ha),
            [_mem("a", u=LATER)],
        ),
        (
            F("cf-1", "a", "b", verdict_ts=VERDICT, verdict_hash_b=hb),
            [_mem("a", u=VERDICT), _mem("b")],
        ),
        (F("cf-1", "a", "b", verdict_hash_a=ha, verdict_hash_b=hb), ab),
        (F("cf-1", "a", "b", verdict_hash_a=ha.upper()), ab),
        (F("cf-1", "a", "b", verdict_ts=VERDICT), [_mem("a", u="2026-01-10T00:00:00")]),
        (
            F("cf-1", "a", "b", verdict_ts=VERDICT, verdict_hash_a=ha),
            [M("a", "\ud800")],
        ),
        (F("cf-1", "a", "b", verdict_ts=VERDICT, verdict_hash_a=""), [_mem("a")]),
        (F("cf-1", "a", "b"), []),
        # A repeated id: its last memory is the one read.
        (
            F("cf-1", "a", "b", verdict_hash_a=ha, verdict_hash_b=hb),
            [_mem("a"), _mem("b"), _mem("a", "an edited body")],
        ),
        (
            F("cf-1", "a", "b", verdict_hash_a=ha, verdict_hash_b=hb),
            [_mem("a", "an edited body"), _mem("b"), _mem("a")],
        ),
    ):
        cases.append(Q("judged", c=c, m=ms))
    return cases


def _sweep_row(lcg: Lcg) -> dict[str, Any]:
    pick = lambda xs: xs[lcg.draw() % len(xs)]  # noqa: E731
    row: dict[str, Any] = {
        "id": pick(Q_PAIRS),
        "a_id": pick(Q_IDS),
        "b_id": pick(Q_IDS),
        "summary_a": pick(Q_SUMMARIES),
        "summary_b": pick(Q_SUMMARIES),
        "similarity": pick(Q_SIMS),
        "method": "jaccard",
        "detector": pick(Q_DETECTORS),
        "created": pick(Q_CREATED),
        "status": pick(Q_STATUSES),
    }
    for key, pool in (
        ("verdict_ts", Q_VERDICT_TS),
        ("note", Q_NOTES),
        ("verdict_hash_a", [None, *QUEUE_HASHES]),
        ("verdict_hash_b", [None, *QUEUE_HASHES]),
    ):
        value = pick(pool)
        if value is not None:
            row[key] = value
    return row


QUEUE_HASHES = [cf._body_hash("alpha"), cf._body_hash("beta"), "0" * 16]


def _sweep_memories(lcg: Lcg) -> list[dict[str, Any]]:
    out = []
    for mid in Q_IDS:
        if lcg.draw() % 4:
            body = Q_BODIES[lcg.draw() % len(Q_BODIES)]
            updated = Q_UPDATED[lcg.draw() % len(Q_UPDATED)]
            out.append({"i": mid, "b": body, "u": updated})
    return out


def _sweep_op(lcg: Lcg, stub: Stub) -> list[Any]:
    """One drawn queue call on ``stub``: ``[calls, answer, warnings,
    table]`` as a case writes them."""
    kind = lcg.draw() % 4
    if lcg.draw() % 8 == 0:
        op = ["put", "delete", "list", "count"][lcg.draw() % 4]
        stub.fail = _fail(op, stub.n.get(op, 0) + 1)
    else:
        stub.fail = None
    stub.calls = []
    c: dict[str, Any]
    if kind == 0:
        c = {"q": "file", "cand": _row(cf.ConflictCandidate.from_dict(_sweep_row(lcg)))}
    elif kind == 1:
        resolve_id = Q_PAIRS[lcg.draw() % len(Q_PAIRS)]
        status = Q_VERDICTS[lcg.draw() % len(Q_VERDICTS)]
        bodies = [
            [mid, Q_BODIES[lcg.draw() % len(Q_BODIES)]]
            for mid in Q_IDS
            if lcg.draw() % 2
        ]
        c = {
            "q": "resolve",
            "id": resolve_id,
            "status": status,
            "note": Q_NOTES[lcg.draw() % len(Q_NOTES)],
            "bodies": bodies,
            "stamp": ["log"] if lcg.draw() % 2 else [],
        }
    else:
        fresh = [
            _row(
                cf.ConflictCandidate.from_dict({**_sweep_row(lcg), "status": "pending"})
            )
            for _ in range(lcg.draw() % 4)
        ]
        ms = _sweep_memories(lcg)
        stub.counts = [len(ms) + lcg.draw() % 3 - 1]
        c = {"q": "upsert", "fresh": fresh, "m": ms}
    with _heard() as warns:
        try:
            r = _queue_call(stub, c)
        except Exception as e:  # noqa: BLE001 - the raise is the record
            r = _outcome(e)
    return [stub.calls, r, warns, [_v(t) for t in stub.table]]


def _queue_sweep() -> dict[str, Any]:
    lcg = Lcg(QUEUE_SWEEP_SEED)
    digest = hashlib.sha256()
    lines = 0
    for _ in range(QUEUE_SWEEP_COUNT):
        stub = Stub({"rows": [], "counts": [0]})
        stub.table = [_sweep_row(lcg) for _ in range(lcg.draw() % 5)]
        for _ in range(QUEUE_SWEEP_OPS):
            line = _sweep_op(lcg, stub)
            text = json.dumps(line, separators=(",", ":"), ensure_ascii=True)
            digest.update(text.encode("ascii") + b"\n")
            lines += 1
    return {
        "q": "sweep",
        "seed": QUEUE_SWEEP_SEED,
        "count": QUEUE_SWEEP_COUNT,
        "ops": QUEUE_SWEEP_OPS,
        "ids": Q_IDS,
        "pairs": Q_PAIRS,
        "bodies": Q_BODIES,
        "updated": Q_UPDATED,
        "verdict_ts": Q_VERDICT_TS,
        "statuses": Q_STATUSES,
        "verdicts": Q_VERDICTS,
        "detectors": Q_DETECTORS,
        "sims": [_bits(x) for x in Q_SIMS],
        "summaries": Q_SUMMARIES,
        "created": Q_CREATED,
        "notes": Q_NOTES,
        "hashes": QUEUE_HASHES,
        "lines": lines,
        "sha256": digest.hexdigest(),
    }


def generate_queue() -> dict[str, bytes]:
    lines = [_queue_case(c) for c in _queue_cases()]
    lines.append(_queue_sweep())
    header = {
        "fixture": "conflicts_queue",
        "cases": len(lines),
        "filename": cf.CONFLICTS_FILENAME,
        "now": QUEUE_NOW.isoformat(),
        "u": U,
        "path": STUB_PATH,
        "stamp_record": _v(STAMP_RECORD),
    }
    return {"conflicts_queue.jsonl": jsonl([header, *lines])}


# -- the real store ---------------------------------------------------------


def _store_memory(n: int) -> dict[str, Any]:
    from .gen_store_write import memory

    return memory(n, body=f"memory {n} of the queue")


def _cell(v: Any) -> Any:
    return _v(v)


def _table(store: Any) -> Any:
    """The conflicts table as `list_conflicts` reads it, or the class of
    what it raised."""
    try:
        return [[_cell(row[c]) for c in row] for row in store.list_conflicts()]
    except Exception as e:  # noqa: BLE001 - the raise is the record
        return {"err": type(e).__name__}


def _store_cases() -> list[dict[str, Any]]:
    from .gen_store_write import ulid

    m1, m2, m3, m4 = (ulid(n) for n in (1, 2, 3, 4))
    put = [{"call": "memory_put", "payload": _store_memory(n)} for n in (1, 2, 3)]
    snap = [_mem(m1), _mem(m2), _mem(m3)]
    c12 = cf._pair_id(m1, m2)
    c13 = cf._pair_id(m1, m3)
    return [
        {
            "name": "lifecycle",
            "steps": [
                *put,
                {"q": "load"},
                {"q": "count"},
                {"q": "file", "cand": F(c12, m1, m2), "session": "s-f"},
                {"q": "file", "cand": F(c12, m1, m2), "session": "s-f"},
                {"q": "pending"},
                {
                    "q": "upsert",
                    "fresh": [F(c12, m1, m2, summary_a="new a"), F(c13, m1, m3)],
                    "m": snap,
                    "session": "s-u",
                },
                {
                    "q": "upsert",
                    "fresh": [F(c12, m1, m2, summary_a="new a"), F(c13, m1, m3)],
                    "m": snap,
                    "session": "s-u",
                },
                {
                    "q": "resolve",
                    "id": c12,
                    "status": "dismissed",
                    "note": "compatible",
                    "bodies": [[m1, "body of " + m1], [m2, "body of " + m2]],
                    "stamp": [],
                    "session": "s-r",
                },
                {
                    "q": "resolve",
                    "id": c12,
                    "status": "confirmed",
                    "note": None,
                    "bodies": None,
                    "stamp": [],
                    "session": "s-r",
                },
                {
                    "q": "upsert",
                    "fresh": [F(c12, m1, m2)],
                    "m": snap[:2],
                    "session": "s-u",
                },
                {
                    "q": "upsert",
                    "fresh": [F(c12, m1, m2, summary_b="again")],
                    "m": [_mem(m1, "an edited body"), snap[1], snap[2]],
                    "session": None,
                },
                {
                    "q": "resolve",
                    "id": c13,
                    "status": "confirmed",
                    "note": None,
                    "bodies": [[m3, "body of " + m3]],
                    "stamp": ["put", "mem"],
                    "session": "s-r",
                },
                {
                    "q": "resolve",
                    "id": c12,
                    "status": "confirmed",
                    "note": "n",
                    "bodies": None,
                    "stamp": ["put", "raise"],
                    "session": "s-r",
                },
                {"q": "count"},
                {
                    "q": "upsert",
                    "fresh": [],
                    "m": [*snap[:2], _mem(m4), _mem("m-extra")],
                    "session": "s-gc",
                },
                {
                    "q": "scan",
                    "m": [
                        M(m1, SUDO),
                        M(m2, SUDO_POS),
                        M(m3, "18.x is not supported; 20.x is supported."),
                        M(m4, "18.x is supported; 20.x is not supported."),
                    ],
                    "th": None,
                    "session": "s-scan",
                },
            ],
        },
        {
            "name": "refusals",
            "steps": [
                *put,
                {"q": "file", "cand": F("cf-1", "", m2)},
                {"q": "file", "cand": F("", m1, m2)},
                {
                    "q": "upsert",
                    "fresh": [F("cf-1", m1, m2), F("cf-2", m1, "")],
                    "m": snap[:1],
                },
                {
                    "q": "resolve",
                    "id": "cf-1",
                    "status": "bogus",
                    "note": None,
                    "bodies": None,
                },
                {"q": "load"},
                # `count_memories` failing inside the batch: sqlite3's
                # OperationalError, the batch rolled back.
                {"sql": "ALTER TABLE memories RENAME TO memories_gone"},
                {"q": "upsert", "fresh": [F("cf-1", m1, m2)], "m": snap[:1]},
                {"q": "count"},
                {"sql": "ALTER TABLE memories_gone RENAME TO memories"},
            ],
        },
        {
            "name": "cells",
            "steps": [
                *put,
                {"sql": _insert("'cf-t'", "'a'", "'b'", "'high'", "'2026-01-01'")},
                {
                    "sql": _insert(
                        "CAST('cf-b' AS BLOB)",
                        f"'{m1}'",
                        f"'{m2}'",
                        "CAST('0.5' AS BLOB)",
                        "'2026-01-02'",
                        summary_a="X'00ff'",
                        status="CAST('dismissed' AS BLOB)",
                        verdict_ts="7",
                        note="2.5",
                    )
                },
                {"sql": _insert("'cf-i'", "'a'", "'b'", "3", "'2026-01-03'")},
                {
                    "sql": _insert(
                        "CAST('cf-z' AS BLOB)", "'a'", "'b'", "0.5", "'2026-01-04'"
                    )
                },
                {"q": "load"},
                {"q": "pending"},
                {"q": "count"},
                {"q": "upsert", "fresh": [], "m": [*snap, _mem("a"), _mem("b")]},
                # The bytes id reads as its repr, which names no row.
                {"q": "upsert", "fresh": [], "m": snap},
                {"sql": "DELETE FROM conflicts WHERE id = CAST('cf-z' AS BLOB)"},
                {"q": "upsert", "fresh": [], "m": snap},
                {"sql": "DELETE FROM conflicts WHERE id = CAST('cf-b' AS BLOB)"},
                {
                    "sql": _insert(
                        "'cf-n'", f"'{m1}'", f"'{m2}'", "'nan'", "'2026-01-05'"
                    )
                },
                {"q": "load"},
                # A NaN read back is put again, and SQLite stores it as NULL.
                {"q": "upsert", "fresh": [], "m": snap},
                {
                    "q": "resolve",
                    "id": "cf-n",
                    "status": "dismissed",
                    "note": None,
                    "bodies": None,
                },
                {"sql": "UPDATE conflicts SET similarity = 0.25 WHERE id = 'cf-n'"},
                {"q": "upsert", "fresh": [], "m": snap},
                {
                    "sql": _insert(
                        "'cf-u'",
                        "'a'",
                        "'b'",
                        "0.5",
                        "'2026-01-06'",
                        summary_a="CAST(X'ff' AS TEXT)",
                    )
                },
                {"q": "load"},
                {"q": "count"},
                {"q": "upsert", "fresh": [], "m": snap},
                {"sql": "DELETE FROM conflicts WHERE id = 'cf-u'"},
                # A BLOB similarity past ASCII: `float()` refuses the bytes
                # (ValueError), and the row is skipped.
                {
                    "sql": _insert(
                        "'cf-nb'", "'a'", "'b'", "X'a0302e35'", "'2026-01-07'"
                    )
                },
                {"q": "load"},
            ],
        },
        {
            "name": "nested",
            "steps": [
                *put,
                {
                    "q": "nested",
                    "calls": [
                        {"q": "file", "cand": F(c12, m1, m2), "session": "s-f"},
                        # The second pair is refused after the first is put:
                        # this call's savepoint takes the put back.
                        {
                            "q": "upsert",
                            "fresh": [F(c13, m1, m3), F("", m1, m2)],
                            "m": snap,
                            "session": "s-u",
                        },
                        # `before_stamp` writes, then raises: taken back.
                        {
                            "q": "resolve",
                            "id": c12,
                            "status": "dismissed",
                            "note": None,
                            "bodies": None,
                            "stamp": ["mem", "raise"],
                            "session": "s-r",
                        },
                        {
                            "q": "resolve",
                            "id": c12,
                            "status": "dismissed",
                            "note": "kept",
                            "bodies": [[m1, "body of " + m1]],
                            "stamp": ["put", "raise"],
                            "session": "s-r",
                        },
                        {"q": "load"},
                        {
                            "q": "resolve",
                            "id": c12,
                            "status": "confirmed",
                            "note": "kept",
                            "bodies": None,
                            "stamp": ["put"],
                            "session": "s-r",
                        },
                        {"q": "upsert", "fresh": [F(c13, m1, m3)], "m": snap},
                        {"q": "count"},
                    ],
                },
                {"q": "load"},
            ],
        },
    ]


def _insert(
    id: str,
    a: str,
    b: str,
    similarity: str,
    created: str,
    *,
    summary_a: str = "'x'",
    status: str = "'pending'",
    verdict_ts: str = "NULL",
    note: str = "NULL",
) -> str:
    """A conflicts row written past the log, its cells as SQL spells them."""
    cells = [id, a, b, summary_a, "'y'", similarity, "'m'", "'d'", created, status]
    cells += [verdict_ts, note, "NULL", "NULL"]
    return f"INSERT INTO conflicts VALUES ({', '.join(cells)})"


def _queue_step(store: Any, step: dict[str, Any]) -> dict[str, Any]:
    step = {"stamp": [], "session": None, **step}
    try:
        return {"r": _queue_call(store, step)}
    except Exception as e:  # noqa: BLE001 - the raise is the record
        return _outcome(e)


def _store_step(store: Any, step: dict[str, Any], root: Path) -> dict[str, Any]:
    if step.get("call") == "memory_put":
        with store._transaction() as conn:
            store._mutate(conn, "memory_put", step["payload"], session="s-m")
        return {"r": None}
    with _heard() as warns:
        if step["q"] == "nested":
            # The calls inside one batch the caller holds open: each joins
            # it, an error caught is that call's outcome, and the batch
            # commits after the last.
            with store.batch():
                out: dict[str, Any] = {
                    "r": [_queue_step(store, c) for c in step["calls"]]
                }
        else:
            out = _queue_step(store, step)
    if warns:
        out["warn"] = [w.replace(str(store.path), "<store>") for w in warns]
    return out


def _run_store_case(case_def: dict[str, Any]) -> dict[str, Any]:
    from bettermemory.store import Store

    from ._fixed import fixed, ulid_mint
    from .gen_store_open import _engine, _made
    from .gen_store_rows_written import Read
    from .gen_store_tx import LOG0, LOG_STEP, WALL0, WALL_STEP
    from .gen_store_write import _last_seq, log_rows

    steps_out: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        case = root / "c"
        _made("created", {}, case)
        file = case / "memory.sqlite"
        with _engine():
            store = Store.open(file, keys_dir=case / "keys", allow_rekey=True)
        try:
            for i, step in enumerate(case_def["steps"]):
                if "sql" in step:
                    store.conn.execute(step["sql"])
                    store.conn.commit()
                    steps_out.append({"sql": step["sql"]})
                    continue
                reads: list[str] = []
                log = Read(LOG0 + timedelta(minutes=i), LOG_STEP, "log", reads)
                wall = Read(WALL0 + timedelta(hours=i), WALL_STEP, "wall", reads)
                minted: list[str] = []
                inner = ulid_mint(1000 * i)

                def mint(
                    inner: Any = inner,
                    minted: list[str] = minted,
                    reads: list[str] = reads,
                ) -> str:
                    reads.append("mint")
                    minted.append(inner())
                    return minted[-1]

                before = _last_seq(file)
                with fixed(log=log, wall=wall, mint=mint):
                    outcome = _store_step(store, step, root)
                steps_out.append(
                    {
                        "step": i,
                        "call": step,
                        "log_clock": log.stamps(),
                        "wall_clock": wall.stamps(),
                        "minted": minted,
                        "reads": reads,
                        "outcome": outcome,
                        "rows": log_rows(file, before),
                        "table": _table(store),
                    }
                )
        finally:
            store.close()
    return {"case": case_def["name"], "steps": steps_out}


def generate_queue_store() -> dict[str, bytes]:
    from ._sandbox import world
    from .gen_store_open import ENGINE

    with world():
        runs = [_run_store_case(c) for c in _store_cases()]
    header = {
        "fixture": "conflicts_queue_store",
        "cases": len(runs),
        "calls": sum(1 for r in runs for s in r["steps"] if "sql" not in s),
        "engine_version": ENGINE,
        "now": QUEUE_NOW.isoformat(),
        "u": U,
        "stamp_record": _v(STAMP_RECORD),
        "stamp_memory": _store_memory(9),
    }
    return {"conflicts_queue_store.jsonl": jsonl([header, *runs])}
