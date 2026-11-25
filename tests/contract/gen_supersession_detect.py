"""Fixtures: the write-time supersession detector
(``supersession_detect.jsonl``, ``supersession_detect_unicode.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/supersession/detect.rs``. This pins
it to `supersession`'s `_one_claim_replaced`, `detect_supersession`,
`detect_supersession_in` and through them `_claims_among`, `_floor`,
`_best_first` (with `_SCORE_BATCH`), `_detect` and `_rule`, the order in
which they read a store, and what they leave in `raw_content_tokens`'
cache.

Each line after the header is one case, by ``k``:

- ``scan``: `detect_supersession(t, recs, exclude_ids=ex)` from an empty
  token cache; ``r`` the report.
- ``in``: `detect_supersession_in(store, t, exclude_ids=ex)` called ``n``
  times (default 1) on a scripted store, from an empty token cache. The
  store holds ``recs`` (``[id, body]``, what `load_all` and `bodies_of`
  answer, less the ids of ``nobody``); `digest_shares` answers None when
  ``shares`` is null, else each ``[id, claim, stamped, shared]`` as a
  `DigestShare` (``n`` 0); `digest_tokens_of` answers ``tok[id]`` where
  given, else the raw content token set of the record's body, for each
  asked id the store holds and ``gone`` does not name; `digest_memo` is
  ``memo``: ``"keep"`` holds each answer not None by key and computes
  otherwise, ``"none"`` answers None without computing. ``log`` is every
  store call in order: ``["memo", key[0], key[1] == t, sorted(key[2])]``,
  ``["shares", sorted(tokens), removed, floor bits, claims]``,
  ``["tokens", ids]``, ``["bodies", ids]``, ``["all"]``. ``r`` is each
  call's report.
- ``one``: `_one_claim_replaced` of ``ms``; ``r`` the matches kept.
- ``sweep`` (ASCII file only): `SWEEP_COUNT` stores drawn by `Lcg`
  (`gen_pyre_tree`) from `SWEEP_SEED`: per store `SWEEP_BODIES + 1`
  bodies, each ``3 + draw % 10`` words of ``words`` joined by
  ``seps[draw % len]``, given the stamp ``stamp`` when ``draw % 9 == 0``.
  The first is the new body; the rest are the records ``r00`` on. Each
  store's line is the compact JSON of ``[scan, in, log]``: the report of
  `detect_supersession` over the records, and the report and call log of
  `detect_supersession_in` over a store whose digest holds each record
  with a claim size above zero and a shared count above zero, as
  `store_digest._features` and `expanded` count them, in record order.
  ``sha256`` is the digest of the lines, each ended by a newline.

A report is ``{"el", "sup", "con"}`` or ``{"err": class, "msg": text}``;
a match is ``[memory_id, outcome, evidence, cue, new_value, old_value,
similarity bits, summary]``. ``keys`` is the token cache after the case,
least recently used first, each key named ``"@t"`` (the new body
stripped), the id of the first record whose stripped body it is, or
itself.

Bodies past ASCII go in ``_unicode``: `str.lower`, the tokenizer and the
features' patterns read the Unicode data there. No text holds a lone
surrogate.
"""

from __future__ import annotations

import hashlib
import json
import struct
from datetime import datetime, timezone
from typing import Any

from bettermemory import search_dedup as sd
from bettermemory import supersession as sp
from bettermemory.models import Confidence, Memory, Source
from bettermemory.store_digest import DigestShare, _features, expanded

from ._common import jsonl
from .gen_pyre_tree import Lcg

STAMP = "\n\n_(consolidate --llm --from-transcript: turn 3 of (a) chat)_"
SWEEP_SEED = 20261125
SWEEP_COUNT = 5_000
SWEEP_BODIES = 20
SWEEP_WORDS = [
    *("the auth service gateway port ingress cluster deploy worker".split()),
    *("pages pipeline billing export dashboard canary release train".split()),
    *("8443 9443 8125 3.11 3.13 v2 v3 2026-07-20 2026-08-01".split()),
    *("prod-hlx-1 prod-hlx-2 workers-medium workers-large eu-west-1".split()),
    *("gateway-canary-v1 gateway-canary-v2 release-gateway".split()),
    *("PagerDuty Opsgenie Airflow Postgres Tuesday Thursday Kafka".split()),
    *("moved switched now again replaced adopted renamed raised".split()),
    *("no longer the previous used to instead of".split()),
    *("to on through via at with is was runs listens".split()),
]
SWEEP_SEPS = [" ", " ", " ", " ", " ", ", ", "; ", ". ", " (", ") "]

_T = datetime(2024, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


def _bits(x: float) -> str:
    return f"{struct.unpack('>Q', struct.pack('>d', x))[0]:016x}"


def _memory(rid: str, body: str) -> Memory:
    """A `Memory` with a short id: the detector reads `id` and `body`
    alone, so the model's ULID check is not run."""
    return Memory.model_construct(
        id=rid,
        created=_T,
        updated=_T,
        scopes=["global"],
        confidence=Confidence.HIGH,
        source=Source.INFERRED,
        body=body,
    )


def _match(m: sp.SupersessionMatch) -> list[Any]:
    return [
        m.memory_id,
        m.outcome,
        m.evidence,
        m.cue,
        m.new_value,
        m.old_value,
        _bits(m.similarity),
        m.summary,
    ]


def _report(call: Any) -> Any:
    try:
        r = call()
    except TypeError as e:
        return {"err": type(e).__name__, "msg": str(e)}
    return {
        "el": r.eligible,
        "sup": [_match(m) for m in r.supersedes],
        "con": [_match(m) for m in r.conflicts],
    }


def _keys(t: str, recs: list[list[str]]) -> list[str]:
    names: dict[str, str] = {}
    for rid, body in recs:
        names.setdefault(sp._strip_provenance(body), rid)
    names[sp._strip_provenance(t)] = "@t"
    return [names.get(k, k) for k in sd._RAW_TOKEN_SETS]


class _Store:
    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case
        self.log: list[Any] = []
        self.held: dict[Any, Any] = {}
        self.bodies = {
            rid: body for rid, body in case["recs"] if rid not in case.get("nobody", [])
        }

    def digest_shares(
        self, tokens: Any, *, removed: bool, floor: float, claims: bool = False
    ) -> list[DigestShare] | None:
        self.log.append(["shares", sorted(tokens), removed, _bits(floor), claims])
        shares = self.case["shares"]
        if shares is None:
            return None
        return [DigestShare(i, 0, c, st, sh) for i, c, st, sh in shares]

    def digest_tokens_of(self, ids: Any) -> dict[str, frozenset[str]]:
        wanted = list(ids)
        self.log.append(["tokens", wanted])
        tok = self.case.get("tok", {})
        gone = self.case.get("gone", [])
        bodies = dict(self.case["recs"])
        out: dict[str, frozenset[str]] = {}
        for i in wanted:
            if i in gone:
                continue
            if i in tok:
                out[i] = frozenset(tok[i])
            elif i in bodies:
                out[i] = frozenset(sd._raw_content_token_set(bodies[i]))
        return out

    def bodies_of(self, ids: Any) -> dict[str, str]:
        wanted = list(ids)
        self.log.append(["bodies", wanted])
        return {i: self.bodies[i] for i in wanted if i in self.bodies}

    def digest_memo(self, key: Any, compute: Any) -> Any:
        self.log.append(["memo", key[0], key[1] == self.case["t"], sorted(key[2])])
        if self.case["memo"] == "none":
            return None
        if key in self.held:
            return self.held[key]
        answer = compute()
        if answer is not None:
            self.held[key] = answer
        return answer

    def load_all(self) -> list[Memory]:
        self.log.append(["all"])
        return [_memory(i, b) for i, b in self.case["recs"]]


def _case(c: dict[str, Any]) -> dict[str, Any]:
    sd.clear_raw_token_set_cache()
    out = dict(c)
    k = c["k"]
    ex = c.get("ex", [])
    if k == "scan":
        recs = [_memory(i, b) for i, b in c["recs"]]
        out["r"] = _report(lambda: sp.detect_supersession(c["t"], recs, exclude_ids=ex))
        out["keys"] = _keys(c["t"], c["recs"])
    elif k == "in":
        c.setdefault("memo", "keep")
        out["memo"] = c["memo"]
        store = _Store(c)
        out["r"] = [
            _report(lambda: sp.detect_supersession_in(store, c["t"], exclude_ids=ex))
            for _ in range(c.get("n", 1))
        ]
        out["log"] = store.log
        out["keys"] = _keys(c["t"], c["recs"])
    elif k == "one":
        ms = [
            sp.SupersessionMatch(
                memory_id=m[0],
                outcome=m[1],
                evidence=m[2],
                cue=m[3],
                new_value=m[4],
                old_value=m[5],
                similarity=0.5,
                summary="s",
            )
            for m in c["ms"]
        ]
        out["r"] = [[m.memory_id, m.old_value] for m in sp._one_claim_replaced(ms)]
    sd.clear_raw_token_set_cache()
    return out


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------

PORT_OLD = (
    "The auth service listens on port 8443 inside the cluster; the ingress "
    "terminates TLS and forwards to that port."
)
PORT_NEW = (
    "The auth service moved to port 9443 when the sidecar was introduced; the "
    "ingress forwards to 9443 and the old port is closed."
)
PORT_SILENT = (
    "The auth service accepts traffic on port 9443 from the ingress, which "
    "forwards after terminating TLS."
)
TRAIN_OLD = (
    "The weekly release train leaves on Tuesday; anything merged after the cut "
    "waits a week."
)
TRAIN_NEW = (
    "The release train moved to Thursday so that fixes from the Monday support "
    "review make the same week's release."
)
CANARY_OLD = (
    "The api gateway canary is watched on the gateway-canary-v1 dashboard "
    "during every rollout."
)
CANARY_REPLACED = (
    "The canary dashboard was replaced by gateway-canary-v2, which adds "
    "per-region error panels."
)
CANARY_BACK = (
    "The replacement dashboard was withdrawn after its queries proved too "
    "slow; rollouts are watched on gateway-canary-v1 again."
)
INVOICE_OLD = (
    "Invoice PDFs in the billing service are rendered with wkhtmltopdf from an "
    "HTML template."
)
INVOICE_DISTRACTOR = (
    "Invoice PDFs are archived for seven years under the finance retention "
    "policy, and the archive is read-only."
)
METRICS = "The metrics sidecar is scraped on port 8125 by the cluster agent."
LONG = " ".join(f"The service moved to port {9000 + i}." for i in range(8))
CLUSTERS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf")


def _cluster(name: str, port: str = "8443") -> str:
    return f"The auth service listens on port {port} inside cluster {name}."


def _id(i: int) -> str:
    return f"m{i:02d}"


def _recs(*bodies: str) -> list[list[str]]:
    return [[_id(i), b] for i, b in enumerate(bodies)]


def _shares(t: str, recs: list[list[str]]) -> list[list[Any]]:
    """What a store's digest holds for `recs`: each record with a claim
    size above zero and a shared count above zero, in record order."""
    new_raw = sd.raw_content_tokens(sp._strip_provenance(t))
    asked = expanded(new_raw)
    out = []
    for rid, body in recs:
        f = _features(body)
        shared = len(asked & expanded(f.tokens))
        if f.claim > 0 and shared > 0:
            out.append([rid, f.claim, f.stamped, shared])
    return out


def _in(t: str, recs: list[list[str]], **kw: Any) -> dict[str, Any]:
    c: dict[str, Any] = {"k": "in", "t": t, "recs": recs}
    c["shares"] = kw.pop("shares", None) if "shares" in kw else _shares(t, recs)
    c.update(kw)
    return c


def _scan(t: str, recs: list[list[str]], **kw: Any) -> dict[str, Any]:
    return {"k": "scan", "t": t, "recs": recs, **kw}


def _both(t: str, recs: list[list[str]], **kw: Any) -> list[dict[str, Any]]:
    return [_scan(t, recs, **kw), _in(t, recs, **kw)]


# Cue-less, over the conflict floor, no kin and no shared context: the
# pair falls back to the first value of each side.
SILENT_A = (
    "The nightly billing export job writes parquet files for finance; "
    "escalations page Ledger oncall directly."
)
SILENT_B = (
    "The nightly billing export job writes parquet files for finance; "
    "weekend runs use Jenkins agents exclusively."
)
SILENT_MANY_A = (
    "The nightly billing export job writes parquet files for finance; "
    "escalations page Ledger and Zephyr oncall directly via hlx-pager."
)
SILENT_MANY_B = (
    "The nightly billing export job writes parquet files for finance; "
    "weekend runs use Jenkins and Bamboo agents with ci-worker exclusively."
)
# A cue over the same pair: no kin and no context, so nothing.
CUE_NO_PAIR = (
    "The nightly billing export job moved; it writes parquet files for "
    "finance and escalations page Ledger oncall directly."
)
CUE_CONTEXT = (
    "The nightly billing export job moved; it writes parquet files for "
    "finance and Ledger reads them."
)
CONTEXT_OLD = (
    "The nightly billing export job writes parquet files for finance; the "
    "Jenkins host is shared."
)
CONTEXT_NEW = (
    "The nightly billing export job writes parquet files for finance; "
    "contact Ledger about failures."
)
# Against METRICS: 0.125 and 0.1 (the floor) with a cue, 0.125 without,
# 0.15 (the kin floor) and 0.1538 without.
FLOOR_CUE = (
    "The proxy sidecar moved to port 9125 for dashboards, alert routing, "
    "tracing and logs in staging."
)
FLOOR_CUE_AT = (
    "The proxy sidecar moved to port 9125 for dashboards, alert routing, "
    "tracing, logs, audits, quotas, billing and backups in staging."
)
FLOOR_NONE = (
    "The proxy sidecar listens on port 9125 for dashboards, alert routing, "
    "tracing and logs in staging."
)
FLOOR_KIN_AT = (
    "The proxy sidecar on port 9125 serves dashboards, alert routing, "
    "tracing, logs, audits, quotas, billing and backups for the cluster in "
    "staging."
)
FLOOR_KIN = "The proxy sidecar listens on port 9125 for dashboards and alert routing."
# Twelve raw tokens, a cue and a value: the crafted digest cases' body.
NEW12 = (
    "The auth service moved to port 9443 last week; the ingress now forwards "
    "there and the old port is closed."
)
PAGER_NEW = "Paging for the auth service moved to Opsgenie last week."
PAGER_A = "Paging for the auth service goes to PagerDuty."
PAGER_B = "Paging for the auth service is routed to PAGERDUTY today."
PAGER_C = "Paging for the auth service goes to Pagerduty-Bridge."


# Fifty-five raw tokens. Against `_padded(12, 58)` it scores 0.112069 and
# against `_padded(11, 49)` 0.112150: both round to 0.1121, so the two are
# read by id, not by score.
NEW55 = (
    "The auth service moved to port 9443 last week; "
    + " ".join(f"w{i}" for i in range(48))
    + "."
)


def _padded(shared: int, other: int) -> str:
    words = [f"w{i}" for i in range(shared)] + [f"zz{i}" for i in range(other)]
    return "Paging uses port 8443 for " + " ".join(words) + "."


def _filler(i: int, shared: int = 12, claim: int = 12) -> list[Any]:
    """A share whose record the digest no longer holds tokens for."""
    return [f"f{i:03d}", claim, False, shared]


def _ascii_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    # Every outcome path, by both entry points.
    cases += _both(PORT_NEW, _recs(PORT_OLD))
    cases += _both(TRAIN_NEW, _recs(TRAIN_OLD))
    cases += _both(PORT_SILENT, _recs(PORT_OLD))
    cases += _both(CONTEXT_NEW, _recs(CONTEXT_OLD))
    cases += _both(SILENT_A, _recs(SILENT_B))
    cases += _both(SILENT_MANY_A, _recs(SILENT_MANY_B))
    cases += _both(CUE_NO_PAIR, _recs(SILENT_B))
    cases += _both(CUE_CONTEXT, _recs(CONTEXT_OLD))
    cases += _both(CANARY_BACK, _recs(CANARY_OLD, CANARY_REPLACED))
    cases += _both(INVOICE_DISTRACTOR, _recs(INVOICE_OLD))
    # One claim replaced: the stronger, then only its old value (lowered).
    cases += _both(PORT_NEW, _recs(METRICS, PORT_OLD))
    cases += _both(PAGER_NEW, _recs(PAGER_A, PAGER_B, PAGER_C))
    # The floors, with and without a cue.
    for t in (FLOOR_CUE, FLOOR_CUE_AT, FLOOR_NONE, FLOOR_KIN_AT, FLOOR_KIN):
        cases += _both(t, _recs(METRICS))
    # Not claim-sized, on either side.
    cases += _both(LONG, _recs(PORT_OLD))
    cases += _both(PORT_NEW, _recs(LONG, PORT_OLD))
    cases.append(_scan(PORT_NEW, []))
    cases += _both("the and of", _recs(PORT_OLD))
    # exclude_ids.
    cases += _both(PORT_NEW, _recs(PORT_OLD, METRICS), ex=["m00"])
    cases += _both(PORT_NEW, _recs(PORT_OLD), ex=["m00", "m00", "zz"])
    # The caps, in each list.
    clusters = _recs(*(_cluster(n) for n in CLUSTERS))
    cases += _both(PORT_NEW, clusters)
    silent = "The auth service accepts traffic on port 9443 inside cluster zulu."
    cases += _both(silent, clusters)
    # Ties in rounded score, broken by id; one id repeated in the scan.
    tied = [["m09", PORT_OLD], ["m03", PORT_OLD], ["m05", PORT_OLD]]
    cases += _both(PORT_NEW, tied)
    cases.append(_scan(PORT_NEW, [["m01", PORT_OLD], ["m01", PORT_OLD + " "]]))
    cases += _both(NEW55, [["x1", _padded(11, 49)], ["x0", _padded(12, 58)]])
    # Stamped bodies: the new one, a stored one, both.
    cases += _both(PORT_NEW + STAMP, _recs(PORT_OLD))
    cases += _both(PORT_NEW, _recs(PORT_OLD + STAMP, METRICS))
    cases += _both(PORT_NEW + STAMP, _recs(_cluster("alpha") + STAMP, PORT_OLD))
    cases += _both(PORT_NEW, _recs(LONG + STAMP, PORT_OLD))
    # Tokens or a body gone; a stamped record whose body is gone.
    cases.append(_in(PORT_NEW, _recs(PORT_OLD, METRICS), gone=["m00"]))
    cases.append(_in(PORT_NEW, _recs(PORT_OLD, METRICS), nobody=["m00"]))
    cases.append(_in(PORT_NEW, _recs(PORT_OLD + STAMP, METRICS), nobody=["m00"]))
    # New values all in the old tokens: skipped before the body is read.
    cases.append(_in(PORT_NEW, _recs(PORT_OLD), tok={"m00": ["9443", "port", "auth"]}))
    # Old values all in the new tokens: skipped after the body is read.
    cases.append(_in(PORT_NEW, _recs(PORT_OLD), tok={"m00": ["8443", "port", "auth"]}))
    # No digest: the scan. The memo answering None: the scan, no shares.
    cases.append(_in(PORT_NEW, _recs(PORT_OLD, METRICS), shares=None))
    cases.append(_in(PORT_NEW, _recs(PORT_OLD), memo="none"))
    # The scan the fallback runs honours exclude_ids: m00 (8443) is left
    # out, so m01 (8125) is the replaced claim.
    cases.append(_in(PORT_NEW, _recs(PORT_OLD, METRICS), shares=None, ex=["m00"]))
    cases.append(_in(PORT_NEW, _recs(PORT_OLD, METRICS), memo="none", ex=["m00"]))
    # A memo hit: the second call reads nothing but the memo.
    cases.append(_in(PORT_NEW, _recs(PORT_OLD), n=2))
    cases.append(_in(PORT_NEW + STAMP, _recs(PORT_OLD), n=2, ex=["zz"]))
    cases.append(_in(PORT_NEW, _recs(PORT_OLD), shares=None, n=2))
    # A share under the floor read in the batch of one over it.
    cases.append(
        _in(
            NEW12,
            [["a", PORT_OLD], ["b", PORT_OLD]],
            shares=[["a", 13, False, 12], ["b", 13, False, 1]],
        )
    )
    # The ceiling: `a` scores 5/12, the 65th share's bound is 5/12 and
    # rounds up, so `a` is handed on only after that share is read.
    a_tok = ["auth", "servic", "ingress", "port", "forward", "8443"]
    a_tok += [f"zz{i}" for i in range(7)]
    fill = [_filler(i) for i in range(63)]
    cases.append(
        _in(
            NEW12,
            [["a", PORT_OLD]],
            tok={"a": a_tok},
            shares=[*fill, ["a", 13, False, 12], ["b", 12, False, 5]],
        )
    )
    # The same with the 65th bound under the score's rounding: handed on
    # before the second batch is read.
    cases.append(
        _in(
            NEW12,
            [["a", PORT_OLD]],
            tok={"a": a_tok},
            shares=[*fill, ["a", 13, False, 12], ["b", 13, False, 4]],
        )
    )
    # Bounds that tie: 65 and 129 shares, the order of the shares kept.
    fill65 = [_filler(i, 12 if i % 3 else 11) for i in range(64)]
    cases.append(
        _in(
            NEW12,
            [["a", PORT_OLD]],
            shares=[*fill65[:30], ["a", 12, False, 11], *fill65[30:]],
        )
    )
    fill129 = [_filler(i, (12, 11, 10)[i % 3]) for i in range(128)]
    cases.append(
        _in(
            NEW12,
            [["a", PORT_OLD]],
            shares=[*fill129[:100], ["a", 12, False, 10], *fill129[100:]],
        )
    )
    # The cap reached in the first batch: the second is never read.
    six = _recs(*(_cluster(n) for n in CLUSTERS[:6]))
    tail = [_filler(i, 3, 12) for i in range(64)]
    cases.append(_in(PORT_NEW, six, shares=[*_shares(PORT_NEW, six), *tail]))
    cases.append(_in(silent, six, shares=[*_shares(silent, six), *tail]))
    # A repeated id: in one batch with equal tokens the heap compares the
    # records' body readers (TypeError); in two batches it never does.
    cases.append(_in(PORT_NEW, _recs(PORT_OLD), shares=[["m00", 9, False, 5]] * 2))
    cases.append(
        _in(
            PORT_NEW,
            _recs(PORT_OLD),
            shares=[["m00", 9, False, 5], *fill[:63], ["m00", 9, False, 2]],
        )
    )
    # A repeated id, stamped and not, both at the containment ceiling
    # (0.575): the heap orders the two by their token sets, the proper
    # subset first, whichever was pushed first. The subset's old value is
    # 8443, the superset's 7443 (its first kin pair), so the order shows
    # in which one the write keeps. The unstamped share's body is read
    # through `bodies_of`, stamp and all.
    sub_body = (
        "The auth service listens on port 8443 and 7443 last week; the "
        "ingress now forwards there, old port closed, alpha beta gamma delta."
    )
    sup_body = sub_body + STAMP
    sub_tok = sorted(sd._raw_content_token_set(sub_body) - {"7443"})
    for order in ((False, True), (True, False)):
        cases.append(
            _in(
                NEW12,
                [["d", sup_body]],
                tok={"d": sub_tok},
                shares=[["d", 20, st, 11] for st in order],
            )
        )
    # The same id with token sets of one size, neither a subset of the
    # other, at one score: the heap finds them unordered and keeps them in
    # the order pushed, so the first share's value is the replaced one.
    level = sorted((set(sd._raw_content_token_set(sub_body)) - {"7443"}) | {"zzq"})
    for order in ((False, True), (True, False)):
        cases.append(
            _in(
                NEW12,
                [["d", sup_body]],
                tok={"d": level},
                shares=[["d", 20, st, 11] for st in order],
            )
        )
    # The same two behind a record popped first (`a`, read and skipped:
    # its tokens hold every new value) and before a weaker one (`e`): on
    # that pop `_siftup` moves up the right child of an unordered pair, so
    # the stamped share (7443) is read before the unstamped one (8443).
    x_tok = ["9443", "auth", "servic", "port", *(f"zz{i}" for i in range(10))]
    cases.append(
        _in(
            NEW12,
            [["d", sup_body]],
            tok={"a": sorted(sd._raw_content_token_set(NEW12)), "d": level, "e": x_tok},
            shares=[
                ["a", 20, False, 11],
                ["d", 20, False, 11],
                ["d", 20, True, 11],
                ["e", 20, False, 11],
            ],
        )
    )
    # The same id at scores that round alike (0.112069 unstamped, 0.112150
    # stamped): the lower raw score pops first whichever was pushed first,
    # so 8443 is the replaced value and 7443 is dropped.
    near = [f"w{i}" for i in range(11)] + [f"zz{i}" for i in range(48)]
    near_body = "Paging uses port 8443 and 7443 for " + " ".join(near) + "." + STAMP
    near_tok = ["8443", "paging", "port", "use"]
    near_tok += [f"w{i}" for i in range(12)] + [f"zz{i}" for i in range(58)]
    for order in ((False, True), (True, False)):
        cases.append(
            _in(
                NEW55,
                [["d", near_body]],
                tok={"d": sorted(near_tok)},
                shares=[["d", 63, st, 13] for st in order],
            )
        )
    # An unstamped share whose stored body carries a stamp: `bodies_of`'s
    # body is read whole, so the summary of a short claim holds the stamp.
    short = "The auth service listens on port 8443 inside the cluster."
    cases.append(
        _in(
            NEW12,
            [["d", short + STAMP]],
            tok={"d": sorted(sd._raw_content_token_set(short))},
            shares=[["d", 9, False, 6]],
        )
    )
    # One claim replaced, directly.
    cases.append({"k": "one", "ms": []})
    cases.append(
        {
            "k": "one",
            "ms": [
                ["a", "supersedes", "kin", "moved", "9443", "PagerDuty"],
                ["b", "supersedes", "kin", "moved", "9443", "pagerduty"],
                ["c", "supersedes", "kin", "moved", "9443", "8443"],
                ["d", "supersedes", "kin", "moved", "9443", "PAGERDUTY"],
            ],
        }
    )
    cases.append(_sweep())
    return cases


def _unicode_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    old = "Paging for the auth service goes through café-crème today."
    new = "Paging for the auth service moved to café-noir last week."
    cases += _both(new, _recs(old))
    # The replaced value lowered by str.lower: final sigma and dotted I.
    cases += _both(
        "Paging for the auth service moved to ΟΔΟΣ-new last week.",
        _recs(
            "Paging for the auth service goes through ΟΔΟΣ-old today.",
            "Paging for the auth service is sent through οδος-old today.",
            "Paging for the auth service is sent through οδοσ-old today.",
        ),
    )
    cases += _both(
        "Paging for the auth service moved to İstanbul-new last week.",
        _recs(
            "Paging for the auth service goes through İstanbul-old today.",
            "Paging for the auth service is sent through i̇stanbul-old today.",
        ),
    )
    # Ids and values ordered by code point.
    ids = [["é1", old], ["z1", old], ["\U0001f600", old], ["Z1", old]]
    cases += _both(new, ids)
    cases += _both(
        "The nightly billing export job writes parquet files for finance; "
        "escalations page élan-x and zeta-x oncall directly.",
        _recs(
            "The nightly billing export job writes parquet files for finance; "
            "weekend runs use ünder-y and alpha-y agents exclusively."
        ),
    )
    # Whitespace and digits past ASCII.
    cases += _both(
        "The auth service moved to port ٩٤٤٣ last week.",
        _recs("The auth service listens on port ٨٤٤٣ inside the cluster."),
    )
    return cases


def sweep_stores() -> list[list[str]]:
    lcg = Lcg(SWEEP_SEED)
    stores = []
    for _ in range(SWEEP_COUNT):
        bodies = []
        for _ in range(SWEEP_BODIES + 1):
            n = 3 + lcg.draw() % 10
            parts = [SWEEP_WORDS[lcg.draw() % len(SWEEP_WORDS)]]
            for _ in range(n - 1):
                parts.append(SWEEP_SEPS[lcg.draw() % len(SWEEP_SEPS)])
                parts.append(SWEEP_WORDS[lcg.draw() % len(SWEEP_WORDS)])
            body = "".join(parts)
            if lcg.draw() % 9 == 0:
                body += STAMP
            bodies.append(body)
        stores.append(bodies)
    return stores


def _sweep() -> dict[str, Any]:
    digest = hashlib.sha256()
    for bodies in sweep_stores():
        t, olds = bodies[0], bodies[1:]
        recs = [[f"r{i:02d}", b] for i, b in enumerate(olds)]
        scan = _case(_scan(t, recs))["r"]
        got = _case(_in(t, recs))
        line = [_compact(scan), _compact(got["r"][0]), got["log"]]
        text = json.dumps(line, separators=(",", ":"), ensure_ascii=True)
        digest.update(text.encode("ascii") + b"\n")
    return {
        "k": "sweep",
        "seed": SWEEP_SEED,
        "count": SWEEP_COUNT,
        "bodies": SWEEP_BODIES,
        "words": SWEEP_WORDS,
        "seps": SWEEP_SEPS,
        "stamp": STAMP,
        "sha256": digest.hexdigest(),
    }


def _compact(r: dict[str, Any]) -> list[Any]:
    return [r["el"], r["sup"], r["con"]]


def _file(name: str, cases: list[dict[str, Any]]) -> bytes:
    lines = [c if c["k"] == "sweep" else _case(c) for c in cases]
    header = {
        "fixture": name,
        "cases": len(lines),
        "batch": sp._SCORE_BATCH,
        "cap": sp.MAX_LINKS_PER_WRITE,
        "floors": [
            _bits(sp.MIN_JACCARD),
            _bits(sp.MIN_CONFLICT_JACCARD),
            _bits(sp.MIN_CONFLICT_KIN_JACCARD),
        ],
        "context": sp.MIN_CONTEXT_SHARED,
        "stamp": STAMP,
    }
    return jsonl([header, *lines])


def generate() -> dict[str, bytes]:
    return {"supersession_detect.jsonl": _file("supersession_detect", _ascii_cases())}


def generate_unicode() -> dict[str, bytes]:
    return {
        "supersession_detect_unicode.jsonl": _file(
            "supersession_detect_unicode", _unicode_cases()
        )
    }
