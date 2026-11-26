"""Fixtures: the conflict scan (``conflicts.jsonl``,
``conflicts_unicode.jsonl``).

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
import struct
from datetime import datetime, timezone
from typing import Any
from unittest import mock

from bettermemory import conflicts as cf
from bettermemory.models import Confidence, Memory, Source

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
