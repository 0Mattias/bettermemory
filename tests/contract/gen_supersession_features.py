"""Fixtures: the per-body features of write-time supersession
(``supersession_features.jsonl``, ``supersession_features_unicode.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/supersession/features.rs``. This
pins it to `supersession`'s `change_cues`, `anchored_values`,
`_proper_nouns`, `value_tokens`, `anchor_tokens`, `_numeric_token_set`,
`_numeric_shape`, `values_are_kin`, `_content_stream`, `_context`,
`_surface_forms`, `_surface_form`, `SupersessionMatch.note` and
`detector`, and through them `_CUE_RE` (whose text the header holds),
`_CLAUSE_RE`, `_WORD_RE`, `_PROPER_NOUN_RE`, `_word_tokens` and
`_CUE_TOKENS` (sorted, in the header).

Each line after the header is one case, by ``k``:

- ``b``: a body ``t`` (or ``rep``, the body as ``[piece, times]`` runs
  joined); ``raw`` is `raw_content_tokens` of `_strip_provenance(t)`, and
  every feature is of ``t`` itself: ``cues`` (`change_cues`, in order),
  ``anch`` (`anchored_values`), ``pn`` (`_proper_nouns`), ``vt`` and
  ``at`` (`value_tokens` and `anchor_tokens` against ``raw``), ``num``
  (`_numeric_token_set`), ``sf`` (`_surface_forms`, ``[token, form]`` in
  first-seen order), ``claim`` (`claim_sized`); ``ctx`` maps each token of
  ``vt`` and ``anch`` to `_context(_content_stream(t), token)` and ``one``
  maps each token of ``vt`` and ``zz-none`` to `_surface_form(t, token)`.
  Sets are sorted. A ``rep`` body's ``raw``, ``sf`` and ``ctx`` are left
  out (the robustness of those is the Rust tests').
- ``kin``: ``a`` and ``b``; ``r`` `values_are_kin(a, b)`, ``sa`` and
  ``sb`` `_numeric_shape` of each.
- ``note``: a `SupersessionMatch` with ``cue`` (null for None), ``new``,
  ``old`` and ``ev``; ``note`` and ``det`` its `note()` and `detector()`.
- ``sweep`` (ASCII file only): `SWEEP_COUNT` bodies drawn from the
  header's ``words`` and ``seps`` by `Lcg` (`gen_pyre_tree`) from
  `SWEEP_SEED`: per body ``2 + draw % 14`` words, each ``words[draw %
  len]``, and between two words ``seps[draw % len]``. Each body's line is
  the compact JSON of ``[cues, anch, pn, vt, at, num, sf]`` (``vt`` and
  ``at`` against `raw_content_tokens` of the body), and ``sha256`` is the
  digest of the lines, each ended by a newline.

Bodies past ASCII go in ``_unicode``: `str.lower`, `str.split`,
`str.isdigit`, `\\b`, `\\d` and the tokenizer read the Unicode data there.
No body holds a lone surrogate.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from bettermemory import supersession as sp
from bettermemory.search_dedup import raw_content_tokens

from ._common import jsonl
from .gen_pyre_tree import Lcg

SWEEP_SEED = 20261125
SWEEP_COUNT = 20_000
STAMP = "\n\n_(consolidate --llm --from-transcript: turn 3 of (a) chat)_"

SWEEP_WORDS = [
    *("no longer cut over the previous used to instead of back on once more".split()),
    *("moved switched renamed raised adopted replaces now again migrated".split()),
    *("deprecated removed unmoved replacement retired formerly reverted".split()),
    *("to through on in at with by via into onto".split()),
    *("8443 9443 3.11 3.13 v2 2026-07-20 74f625d a1b2c3d4e5f6a7b8c9".split()),
    *("prod-hlx-1 hlx-exports-v2 runners-medium runners-large eu-west-1".split()),
    *("release-gateway on-call read-only billing-db-green prod_hlx_2".split()),
    *("PagerDuty Opsgenie Airflow API AWS Postgres K8s Monday iPhone".split()),
    *("yarn pnpm port service pages the a is it was new old".split()),
    *("(Airflow) `yarn` 'Redis' \"Kafka\" x. 3. v1.2.3, 9443; PagerDuty.".split()),
]
SWEEP_SEPS = [" ", " ", " ", ", ", ". ", "; ", ": ", "\n", "! ", " (", ") ", "\t"]


def _sorted(values: Any) -> list[str]:
    return sorted(values)


def _body(t: str, *, full: bool = True) -> dict[str, Any]:
    raw = raw_content_tokens(sp._strip_provenance(t))
    vt = sp.value_tokens(t, raw)
    anch = sp.anchored_values(t)
    out: dict[str, Any] = {
        "cues": sp.change_cues(t),
        "anch": _sorted(anch),
        "pn": _sorted(sp._proper_nouns(t)),
        "vt": _sorted(vt),
        "at": _sorted(sp.anchor_tokens(t, raw)),
        "num": _sorted(sp._numeric_token_set(t)),
        "claim": sp.claim_sized(t),
    }
    if full:
        stream = sp._content_stream(t)
        out["raw"] = _sorted(raw)
        out["sf"] = [[k, v] for k, v in sp._surface_forms(t).items()]
        out["ctx"] = {v: _sorted(sp._context(stream, v)) for v in sorted(vt | anch)}
        out["one"] = {v: sp._surface_form(t, v) for v in [*sorted(vt), "zz-none"]}
    return out


def _b(t: str) -> dict[str, Any]:
    return {"k": "b", "t": t, **_body(t)}


def _rep(runs: list[list[Any]]) -> dict[str, Any]:
    t = "".join(piece * times for piece, times in runs)
    return {"k": "b", "rep": runs, **_body(t, full=False)}


def _kin(a: str, b: str) -> dict[str, Any]:
    return {
        "k": "kin",
        "a": a,
        "b": b,
        "r": sp.values_are_kin(a, b),
        "sa": sp._numeric_shape(a),
        "sb": sp._numeric_shape(b),
    }


def _note(cue: str | None, new: str, old: str, ev: str) -> dict[str, Any]:
    m = sp.SupersessionMatch(
        memory_id="m1",
        outcome="supersedes",
        evidence=ev,
        cue=cue,
        new_value=new,
        old_value=old,
        similarity=0.5,
        summary="s",
    )
    return {
        "k": "note",
        "cue": cue,
        "new": new,
        "old": old,
        "ev": ev,
        "note": m.note(),
        "det": m.detector(),
    }


def sweep_bodies() -> list[str]:
    lcg = Lcg(SWEEP_SEED)
    bodies = []
    for _ in range(SWEEP_COUNT):
        n = 2 + lcg.draw() % 14
        parts = [SWEEP_WORDS[lcg.draw() % len(SWEEP_WORDS)]]
        for _ in range(n - 1):
            parts.append(SWEEP_SEPS[lcg.draw() % len(SWEEP_SEPS)])
            parts.append(SWEEP_WORDS[lcg.draw() % len(SWEEP_WORDS)])
        bodies.append("".join(parts))
    return bodies


def _sweep() -> dict[str, Any]:
    digest = hashlib.sha256()
    for t in sweep_bodies():
        raw = raw_content_tokens(t)
        line = [
            sp.change_cues(t),
            _sorted(sp.anchored_values(t)),
            _sorted(sp._proper_nouns(t)),
            _sorted(sp.value_tokens(t, raw)),
            _sorted(sp.anchor_tokens(t, raw)),
            _sorted(sp._numeric_token_set(t)),
            [[k, v] for k, v in sp._surface_forms(t).items()],
        ]
        text = json.dumps(line, separators=(",", ":"), ensure_ascii=True)
        digest.update(text.encode("ascii") + b"\n")
    return {
        "k": "sweep",
        "seed": SWEEP_SEED,
        "count": SWEEP_COUNT,
        "words": SWEEP_WORDS,
        "seps": SWEEP_SEPS,
        "sha256": digest.hexdigest(),
    }


def _ascii_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    # Every cue, with a preposition slot and with a direct object.
    for phrase in sp.CHANGE_CUES:
        cases.append(_b(f"Team {phrase} to port 9443 for Airflow."))
        cases.append(_b(f"Builds {phrase} yarn-berry for CI"))
    bodies = [
        # Cues inside words and overlapping cues; \b cuts them.
        "The script removed the old flag.",
        "We are moving forward with replacement parts.",
        "nowhere now unmoved moved-to moves_on",
        "cut overall; used to-do; rolled back on Monday",
        "the previously named API: the former name",
        "Switched to Postgres. Switched to Redis; switched",
        "MOVED TO PORT 9443 FROM 8443",
        # A cue at the end of its clause, or with nothing after it.
        "Paging moved.",
        "We switched; the rest stays",
        "Port moved to.",
        "It was replaced",
        "now",
        "again and again",
        # Prepositions within and past three words.
        "Paging moved quickly and quietly to Opsgenie.",
        "Paging moved over to Opsgenie.",
        "Paging moved to the new PagerDuty.",
        "Paging moved to the a an PagerDuty.",
        "Builds adopted the a an yarn.",
        "Moved from Jenkins onto GitHub Actions",
        "Paging now runs via release-gateway",
        "The cluster migrated into eu-west-1 at 3.13.",
        # Digits: versions, dates, ports, ids past 16 characters.
        "Postgres moved to 5432 from 5433.",
        "Python bumped to 3.13 from 3.11.",
        "Released on 2026-07-20 as v2.",
        "Commit 74f625d replaced abc1234.",
        "Record 01HXYZABCDEF1234567890ABCD and a1b2c3d4e5f6a7b8 differ.",
        "Edges: -5- ._9_. 3.27.0. v1.2.3, 9443; 1234567890123456 12345678901234567",
        "No digits at all here",
        "port 8443, port 9443 and port 8443 again",
        # A value twice within CONTEXT_WIDTH: its context leaves it out.
        "Ports 9443 and 9443 differ.",
        # Compounds of two and three parts.
        "Pages go on-call and read-only.",
        "Traffic uses billing-db-green and prod-hlx-2.",
        "The runners-medium pool and runners-large pool.",
        "Exports land in hlx_exports and hlx.exports and a-b and x-.",
        "A two-part deploy-gateway beside a three-part deploy-gateway-v2.",
        # Capitalised words first in a clause, acronyms, odd shapes.
        "Airflow runs nightly. Postgres too; Redis now: Kafka!",
        "We use AWS and GCP, not K8s or OK.",
        "Call the API v2, A, I and Go.",
        "Brands: PagerDuty iPhone McDonald ABCdef A1b AB1 Ab A1",
        "We use (Airflow), 'Redis', \"Kafka\" and `Postgres`.",
        "We use [Airflow] {Redis} ((Kafka)) '`Go`'",
        # ASCII whitespace that is not a space.
        "use\tAirflow\x0bnow\x0cRedis\x1cKafka\x1fPostgres",
        "first line\nAirflow second\rRedis",
        # A provenance stamp: the raw tokens are the stripped body's.
        "Port moved to 9443 for API." + STAMP,
        # Edge bodies.
        "",
        "   ",
        "the a an of",
        "...;;;:::!!!???",
        "v3.13 is out. Next: v3.14.",
    ]
    cases.extend(_b(t) for t in bodies)
    # Hostile bodies for _PROPER_NOUN_RE (quadratic in the reference).
    for runs in (
        [["We use ", 1], ["A", 3000], ["+", 1]],
        [["We use ", 1], ["Aa", 1500], ["+", 1]],
        [["We use ", 1], ["A", 1], ["a", 3000], ["+", 1]],
        [["We use ", 1], ["A", 300], [".", 1]],
        [["moved to ", 100]],
    ):
        cases.append(_rep(runs))
    for a, b in (
        ("8443", "9443"),
        ("8443", "443"),
        ("3.11", "3.13"),
        ("3.11", "3.1.1"),
        ("3.11", "8443"),
        ("8443", "8443"),
        ("runners-medium", "runners-large"),
        ("prod-hlx-1", "hlx-exports-v2"),
        ("hlx-exports-prod", "hlx-exports-v2"),
        ("eu-west-1", "eu-central-1"),
        ("the-api", "the-web"),
        ("a-b", "a-c"),
        ("db-green", "db-blue"),
        ("x", "y-z"),
        ("a.b", "a.c"),
        ("8443", "a-b"),
        ("v2", "v3"),
        ("x-y-z", "x-q-r-s"),
        ("x-y-z", "x-y-r-s"),
        ("prod-hlx-1", "prod-hlx-2"),
        ("x_y", "x-z"),
        ("1.2", "1.2.3"),
        ("01", "10"),
        ("", "1"),
        ("", ""),
    ):
        cases.append(_kin(a, b))
    notes: list[tuple[str | None, str, str, str]] = [
        ("moved", "9443", "8443", "kin"),
        (None, "3.13", "3.11", "similarity"),
        ("switched to", "Opsgenie", "PagerDuty", "context"),
        ("now", "it's", 'say "hi"', "kin"),
        ("replaced", "tab\there", "nl\nhere", "kin"),
        ("again", "V2", "v3", "kin"),
        ("again", "9443", "port", "kin"),
        ("again", "1.2", "3.4", "kin"),
    ]
    for cue, new, old, ev in notes:
        cases.append(_note(cue, new, old, ev))
    cases.append(_sweep())
    return cases


def _unicode_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    bodies = [
        # str.split() on whitespace past ASCII.
        "We use\xa0Airflow now.",
        "We use\u2003Redis\u3000and\u2028Kafka\u0085Postgres",
        "moved\u2003to\u2003Opsgenie",
        # \b past ASCII: a word character beside a cue.
        "\xe9moved to port 9443",
        "moved\xe9 to port 9443",
        "\xdcmoved to Opsgenie; moved\u0663 to Redis",
        "\u0663moved to port 9443 and \u4e2dnow",
        # str.lower past ASCII, final sigma, a lowering that grows.
        "\u0130stanbul moved to Ankara",
        "\u039f\u0394\u039f\u03a3 MOVED TO \u03a3\u039f\u03a6\u0399\u0391",
        "\u212a8s moved to K9s at port 9443",
        "\u0130\u0130\u0130 now runs via release-gateway",
        # str.isdigit and \d past ASCII.
        "Builds moved to build-\u0663 from build-\u0664.",
        "Ports \u0668\u0664\u0664\u0663 and 9443 and \u0663.\u0661\u0661",
        "rel-\xb2 and v\xb2 and x-y\xb2",
        # Capitals and scripts past ASCII.
        "We use \xc4rzte and Z\xfcrich and \xc5ngstr\xf6m",
        "\u79fb\u884c moved to \u6771\u4eac via \u5927\u962a-\u7b2c\u4e8c",
        "Paging moved to \xabOpsgenie\xbb from \u201cPagerDuty\u201d",
    ]
    cases.extend(_b(t) for t in bodies)
    for a, b in (
        ("\u0668\u0664\u0664\u0663", "9443"),
        ("\u0663.\u0661\u0661", "3.13"),
        ("\xb2", "2"),
        ("build-\u0663", "build-\u0664"),
        ("\u0661\u0662", "\uff11\uff12"),
    ):
        cases.append(_kin(a, b))
    unicode_notes: list[tuple[str | None, str, str, str]] = [
        ("moved", "\u0668\u0664\u0664\u0663", "8443", "kin"),
        ("moved", "\xe9t\xe9", "\u200bzero", "context"),
        ("now", "\u2028", "\U0001f600", "kin"),
        ("again", "\u0130", "\uff11\uff12", "kin"),
    ]
    for cue, new, old, ev in unicode_notes:
        cases.append(_note(cue, new, old, ev))
    return cases


def _file(name: str, cases: list[dict[str, Any]]) -> bytes:
    header = {
        "fixture": name,
        "cases": len(cases),
        "cue_pattern": sp._CUE_RE.pattern,
        "cue_tokens": sorted(sp._CUE_TOKENS),
        "cues": [[c, v] for c, v in sp.CHANGE_CUES.items()],
        "preps": sorted(sp._VALUE_PREPS),
        "entries": sp._WORD_TOKEN_ENTRIES,
        "consts": [
            sp.MAX_CLAIM_TOKENS,
            sp.MAX_CLAIM_SENTENCES,
            sp.CONTEXT_WIDTH,
            sp.MIN_CONTEXT_SHARED,
            sp.MAX_LINKS_PER_WRITE,
        ],
        "floors": [
            sp.MIN_JACCARD,
            sp.MIN_CONFLICT_JACCARD,
            sp.MIN_CONFLICT_KIN_JACCARD,
        ],
    }
    return jsonl([header, *cases])


def generate() -> dict[str, bytes]:
    return {
        "supersession_features.jsonl": _file("supersession_features", _ascii_cases())
    }


def generate_unicode() -> dict[str, bytes]:
    return {
        "supersession_features_unicode.jsonl": _file(
            "supersession_features_unicode", _unicode_cases()
        )
    }
