"""Log hygiene (9.0.0rc12): what reaches the signed log.

- Query redaction reads the write gate's own detector list
  (`credentials.secret_spans`), so a `gho_`, `sk-proj-`, `sk_live_`,
  `xoxb-` or `AIza` token no longer survives in a log preview, the five
  legacy patterns keep their markers, and a query with no secret shape is
  byte-for-byte what it was.
- The detectors stay linear in their input, now that every query and
  every prompt the recall hook redacts runs through them, and report
  exactly what their plain patterns did.
- The standing guard's row and the override evidence on a write or an
  update are audit rows: recorded with `[telemetry] enabled = false`,
  while telemetry proper stays off.

Fixture note: secret-SHAPED fixtures are assembled from fragments via
`_shaped(...)` so the complete token literal never appears in source (see
tests/test_credentials.py).
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import random
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bettermemory import credentials, events
from bettermemory.builder import build_server
from bettermemory.config import Config, StorageConfig, TelemetryConfig
from bettermemory.credentials import find_credential_markers, secret_spans
from bettermemory.events import Recorder, is_audit_row, redact_query
from bettermemory.recall import WORDS
from bettermemory.session import SessionState
from bettermemory.store import StandingAnswer, Store

from ._mcp import call_tool
from ._review_rows import FACTS, Harness, fix_origin_and_identity, set_tau
from .test_credentials import (
    _ANTHROPIC,
    _ASIA,
    _AWS,
    _GITHUB,
    _GITHUB_PAT,
    _GOOGLE,
    _JWT,
    _OPENAI,
    _PEM_HEADER,
    _SENDGRID,
    _SLACK,
    _STRIPE,
    _VAULT,
)
from .test_write_standing import QUOTE, RULE, UPDATING


def _shaped(*parts: str) -> str:
    """Join fragments into a secret-shaped value with no scannable literal."""
    return "".join(parts)


# The shapes the five legacy patterns never knew (survey: security-core
# weakness 5, policy-identity weakness 4), each with what the second pass
# leaves of it and a run of its body that must not reach the log.
_GHO = _shaped("gho_", "1234567890abcdefABCDEF1234567890abcd")
_GHS = _shaped("ghs_", "abcdefABCDEF1234567890abcdefABCD1234")
_SK_PROJ = _shaped("sk-proj-", "AbCdEfGh1234IjKlMnOp5678QrStUv")
_ASSIGNMENT = _shaped("password=", "Hunter2Abc9XyZ12Q")
_URI = _shaped("postgres://deploy:", "S3cretPassw0rdXy", "@db.internal/app")

_NEW_SHAPES: tuple[tuple[str, str, str], ...] = (
    (_GHO, "[REDACTED:github-token]", _GHO[4:20]),
    (_GHS, "[REDACTED:github-token]", _GHS[4:20]),
    (_SK_PROJ, "[REDACTED:openai-anthropic-key]", _SK_PROJ[8:24]),
    (_STRIPE, "[REDACTED:stripe-key]", _STRIPE[8:24]),
    (_SLACK, "[REDACTED:slack-token]", _SLACK[5:20]),
    (_GOOGLE, "[REDACTED:google-api-key]", _GOOGLE[4:20]),
    # The generic rule's span is the value: the keyword stays readable.
    (_ASSIGNMENT, "password=[REDACTED:generic-secret-assignment]", "Hunter2Abc9"),
)

# One body per detector the write gate knows, keyed by the kind it fires
# as. `test_every_detector_has_a_redaction_fixture` keeps the list whole,
# so a detector added later is redacted from the log's previews and this
# file says so.
_BY_KIND: dict[str, str] = {
    "aws-access-key-id": f"the key {_AWS} and {_ASIA} for the deploy",
    "openai-anthropic-key": f"the keys {_OPENAI} and {_ANTHROPIC} and {_SK_PROJ}",
    "stripe-key": f"billing uses {_STRIPE} in prod",
    "github-token": f"tokens {_GITHUB} {_GHO} {_GHS} {_GITHUB_PAT}",
    "slack-token": f"the bot posts with {_SLACK} daily",
    "google-api-key": f"maps load with {_GOOGLE} today",
    "sendgrid-api-key": f"mail goes out with {_SENDGRID} now",
    "vault-token": f"vault login {_VAULT} first",
    "private-key-pem": f"{_PEM_HEADER}\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSj\n",
    "jwt": f"the session cookie is {_JWT} here",
    "generic-secret-assignment": f"in .env set {_ASSIGNMENT} for the app",
    "connection-uri-password": f"DATABASE_URL={_URI} on staging",
}


def _legacy_redaction(text: str) -> dict[str, Any]:
    """`redact_query` as it was before the second pass: the five legacy
    patterns, then the hash, the preview and the length."""
    stripped = text
    for pattern, replacement in events._SECRET_PATTERNS:
        stripped = pattern.sub(replacement, stripped)
    digest = hashlib.sha256(stripped.encode("utf-8", errors="replace")).hexdigest()
    return {"hash": digest[:16], "preview": stripped[:32], "len": len(text)}


def _rows(store: Store, kind: str) -> list[dict[str, Any]]:
    return [e for e in store.iter_events() if e.get("kind") == kind]


# ---------------------------------------------------------------------------
# Query redaction reads the write gate's detector list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("secret", "stripped", "body_run"), _NEW_SHAPES)
def test_a_shape_the_legacy_patterns_missed_leaves_no_trace_in_the_preview(
    secret: str, stripped: str, body_run: str
) -> None:
    query = f"{secret} where is this used"
    assert events._strip_known_secrets(query) == f"{stripped} where is this used"
    redacted = redact_query(query)
    assert redacted["preview"] == f"{stripped} where is this used"[:32]
    assert body_run not in redacted["preview"]
    assert redacted["len"] == len(query), "len stays the pre-strip length"
    # The hash is taken on the stripped text, so the secret's bytes do not
    # feed it.
    digest = hashlib.sha256(f"{stripped} where is this used".encode()).hexdigest()
    assert redacted["hash"] == digest[:16]


def test_two_secrets_of_one_shape_hash_alike() -> None:
    other = _shaped("gho_", "zyxwvu9876543210ZYXWVU9876543210zyxw")
    assert (
        redact_query(f"rotate {_GHO} now")["hash"]
        == (redact_query(f"rotate {other} now")["hash"])
    )


@pytest.mark.parametrize(
    ("secret", "marker"),
    [
        (_ANTHROPIC, "[REDACTED:anthropic-key]"),
        (_OPENAI, "[REDACTED:openai-key]"),
        (_GITHUB, "[REDACTED:github-token]"),
        (_GITHUB_PAT, "[REDACTED:github-pat]"),
        (_AWS, "[REDACTED:aws-access-key]"),
        # Wider than the detectors: a masked run and an all-lowercase body
        # are no secret to the write gate, and the log still strips them.
        (_shaped("AKIA", "X" * 16), "[REDACTED:aws-access-key]"),
        (_shaped("sk-", "abcdefghijklmnopqrstuvwx"), "[REDACTED:openai-key]"),
    ],
)
def test_the_legacy_patterns_keep_their_markers(secret: str, marker: str) -> None:
    query = f"where is {secret} kept"
    assert redact_query(query) == _legacy_redaction(query)
    assert marker in events._strip_known_secrets(query)


@pytest.mark.parametrize(
    "query",
    [
        "",
        "kubernetes networking",
        "how is the password rotation policy enforced",
        "api_key defaults to None in the client",
        "where does the deploy token live",
        "sk-image-processing-toolkit-v3 build flags",
        "postgres://user:password@localhost/db is the docs example",
        "the password was rotated 2026-01-15T00:30:00Z",
        "ünïcödé query with émojis 🧪 and CJK 記憶",
        "x" * 400,
    ],
)
def test_a_query_with_no_secret_shape_is_byte_for_byte_unchanged(query: str) -> None:
    """No pass strips anything, so the row is what an unredacted hash and
    a plain slice would give: same hash, preview and length as before the
    second pass existed."""
    digest = hashlib.sha256(query.encode("utf-8", errors="replace")).hexdigest()
    expected = {"hash": digest[:16], "preview": query[:32], "len": len(query)}
    assert redact_query(query) == expected
    assert redact_query(query) == _legacy_redaction(query)


def test_every_detector_has_a_redaction_fixture() -> None:
    kinds = {kind for kind, _ in credentials._PREFIXED_DETECTORS}
    kinds |= {"generic-secret-assignment", "connection-uri-password"}
    assert set(_BY_KIND) == kinds


@pytest.mark.parametrize("kind", sorted(_BY_KIND))
def test_nothing_the_write_gate_detects_survives_redaction(kind: str) -> None:
    """The property the shared list buys: whatever a detector fires on in
    a query, the redacted text holds no span any detector still sees."""
    text = _BY_KIND[kind]
    assert kind in {hit.kind for hit in find_credential_markers(text)}
    stripped = events._strip_known_secrets(text)
    assert secret_spans(stripped) == []
    assert find_credential_markers(stripped) == []


def test_secret_spans_are_disjoint_sorted_and_named_by_kind() -> None:
    text = f"api_key = {_AWS} then {_GHO} then {_SLACK}"
    spans = secret_spans(text)
    assert [kind for _, _, kind in spans] == [
        "aws-access-key-id",
        "github-token",
        "slack-token",
    ]
    for (_, end, _), (start, _, _) in itertools.pairwise(spans):
        assert end <= start
    assert secret_spans("nothing to see here") == []


def test_a_signed_row_carries_no_new_shape_secret(store: Store) -> None:
    store.record_event(
        "search",
        session="sess_a",
        query=f"{_GHO} which repo",
        probe_query=f"{_STRIPE} is the billing key",
    )
    row = store.log_rows()[-1]
    assert _GHO not in row.payload and _GHO[4:20] not in row.payload
    assert _STRIPE not in row.payload and _STRIPE[8:24] not in row.payload
    payload = json.loads(row.payload)
    assert payload["query"]["preview"].startswith("[REDACTED:github-token]")
    assert payload["probe_query"]["preview"].startswith("[REDACTED:stripe-key]")
    assert store.log_verify()["status"] == "ok"


# ---------------------------------------------------------------------------
# The detectors stay linear, and report what their plain patterns did
# ---------------------------------------------------------------------------

# The four detectors whose body runs on through their prefix's characters,
# spelled as they were before the `spent` alternative (credentials.py):
# the reference both match sites are checked against, keyed by the start
# of the pattern that replaced each.
_PLAIN_PATTERNS: dict[str, re.Pattern[str]] = {
    r"\bsk-": re.compile(
        r"\bsk-(?:ant-|proj-)?"
        r"(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*[0-9])"
        r"[A-Za-z0-9_-]{20,}\b"
    ),
    r"\bxox": re.compile(r"\bxox[abceprs]-(?=[0-9A-Za-z-]*[0-9])[0-9A-Za-z-]{10,}\b"),
    r"\bxapp": re.compile(r"\bxapp-(?=[0-9A-Za-z-]*[0-9])[0-9A-Za-z-]{10,}\b"),
    r"\beyJ": re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    ),
}

# What the fuzz below strings together: whole tokens of the four shapes,
# their bare prefixes, the edges of the body classes (`_`, `-`, an
# uppercase letter, a digit), what ends a run (a dot, a space, a newline)
# and a non-ASCII word character, which ends a run without making a word
# boundary.
_FRAGMENTS: tuple[str, ...] = (
    _shaped("sk-", "Abcd1234efgh5678ijkl"),
    _shaped("sk-", "proj-", "Q7", "abcdefghijklmnopqrstu"),
    _shaped("xoxb-", "1234567890-abcdef"),
    _shaped("xapp-", "1-A0B1C2D3E4"),
    _shaped("eyJ", "hbGciOiJI.Abcd1234efgh.sig_Abcd1234"),
    # A body too short after the `ant-` prefix and long enough without it.
    _shaped("sk-", "ant-", "Abcd1234efgh5678"),
    "sk-",
    "ant-",
    "proj-",
    "xoxp-",
    "xapp-",
    "eyJ",
    "abcdefgh",
    "Abcd1234",
    ".",
    "-",
    "_",
    " ",
    "\n",
    "A",
    "7",
    "z",
    "é",
)


def _plain_detectors() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """`credentials._PREFIXED_DETECTORS` with every detector that carries
    a `spent` alternative put back to its plain pattern."""
    plain: list[tuple[str, re.Pattern[str]]] = []
    for kind, regex in credentials._PREFIXED_DETECTORS:
        if "spent" in regex.groupindex:
            (start,) = [s for s in _PLAIN_PATTERNS if regex.pattern.startswith(s)]
            regex = _PLAIN_PATTERNS[start]
        plain.append((kind, regex))
    return tuple(plain)


def test_the_spent_alternative_changes_no_span_and_no_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both match sites (`secret_spans`, `find_credential_markers`), over
    strings built at random from every prefix, whole tokens and the edges
    of every body class, report the same spans, kinds and snippets as the
    plain patterns do."""
    plain = _plain_detectors()
    swapped = [
        kind
        for (kind, now), (_, was) in zip(
            credentials._PREFIXED_DETECTORS, plain, strict=True
        )
        if now is not was
    ]
    assert sorted(swapped) == [
        "jwt",
        "openai-anthropic-key",
        "slack-token",
        "slack-token",
    ]
    rng = random.Random(20261028)
    texts = [
        "".join(rng.choices(_FRAGMENTS, k=rng.randint(1, 30))) for _ in range(3000)
    ]

    def scan() -> list[tuple[Any, Any]]:
        return [
            (secret_spans(t), credentials.find_credential_markers(t)) for t in texts
        ]

    linear = scan()
    monkeypatch.setattr(credentials, "_PREFIXED_DETECTORS", plain)
    assert linear == scan()
    # Not vacuous: the fuzz finds each changed shape many times, and
    # misses it many times too.
    found = [kind for spans, _ in linear for _, _, kind in spans]
    for kind in ("jwt", "openai-anthropic-key", "slack-token"):
        assert found.count(kind) > 100, kind
    assert sum(1 for spans, _ in linear if not spans) > 100


@pytest.mark.parametrize("unit", ["sk-", "sk-ant-", "xoxb-", "xapp-", "eyJ-"])
def test_a_run_of_prefixes_is_read_in_linear_time(unit: str) -> None:
    """300 KB of one prefix over and over: through the plain `sk-` pattern
    this took over a minute (each prefix's failed attempt read to the end
    of the run), and a search query or a prompt the recall hook redacts
    can be that long. The bound is generous for a slow runner and far
    below the quadratic cost."""
    text = unit * (300_000 // len(unit))
    started = time.perf_counter()
    assert secret_spans(text) == []
    assert credentials.find_credential_markers(text) == []
    assert redact_query(text)["len"] == len(text)
    assert time.perf_counter() - started < 5.0


# ---------------------------------------------------------------------------
# Audit rows are recorded with telemetry off
# ---------------------------------------------------------------------------


def test_is_audit_row_names_the_guard_and_the_escape_hatches() -> None:
    assert is_audit_row("standing_guard", {})
    assert is_audit_row("write", {"forced": True})
    assert is_audit_row("write", {"forced": False, "credentials_acknowledged": ["jwt"]})
    assert is_audit_row("write", {"markers_acknowledged": ["currently"]})
    assert is_audit_row("write", {"user_claims_acknowledged": ["prefers"]})
    assert is_audit_row("update", {"truncation_acknowledged": True})
    assert is_audit_row("update", {"credentials_acknowledged": ["aws-access-key-id"]})
    # No hatch used: telemetry.
    assert not is_audit_row(
        "write",
        {
            "forced": False,
            "markers_acknowledged": [],
            "credentials_acknowledged": [],
            "user_claims_acknowledged": [],
        },
    )
    assert not is_audit_row("update", {"truncation_acknowledged": False})
    # Other kinds never carry override evidence.
    assert not is_audit_row("search", {"forced": True})
    assert not is_audit_row("episode_write", {"credentials_acknowledged": ["jwt"]})


def test_a_disabled_recorder_still_writes_audit_rows(store: Store) -> None:
    recorder = Recorder(store=store, session_id="sess_off", enabled=False)
    recorder.record("search", query="anything", returned=[])
    recorder.record("write", status="committed", forced=False, markers_acknowledged=[])
    recorder.record(
        "standing_guard",
        door="memory_remove",
        standing=["01ARZ3NDEKTSV4RRFFQ69G5FAV"],
        said="drop the rule",
        said_checked=False,
    )
    recorder.record("write", status="committed", forced=True)
    recorder.record("update", id="x", truncation_acknowledged=True)
    kinds = [(e["kind"], e.get("forced")) for e in store.iter_events()]
    assert kinds == [("standing_guard", None), ("write", True), ("update", None)]
    (guard,) = _rows(store, "standing_guard")
    assert guard["session"] == "sess_off"
    assert store.log_verify()["status"] == "ok"


async def test_telemetry_off_keeps_the_override_evidence_and_nothing_else(
    memory_dir: Path,
) -> None:
    store = Store(memory_dir)
    server = build_server(
        config=Config(
            storage=StorageConfig(directory=str(memory_dir)),
            telemetry=TelemetryConfig(enabled=False),
        ),
        store=store,
        state=SessionState(),
    )

    async def write(content: str, **args: Any) -> dict[str, Any]:
        return await call_tool(
            server, "memory_write", {"content": content, "scopes": ["tools"], **args}
        )

    assert (await write(FACTS[1]))["status"] == "committed"
    await call_tool(server, "memory_search", {"query": "dashboards"})
    assert list(store.iter_events()) == [], "telemetry proper stays off"

    forced = await write(FACTS[2], force=True)
    assert forced["status"] == "committed"
    acknowledged = await write(
        f"The staging deploy reads {_AWS} from the vault.", acknowledge_credential=True
    )
    assert acknowledged["status"] == "committed"

    rows = _rows(store, "write")
    assert [(r["id"], r["forced"]) for r in rows] == [
        (forced["id"], True),
        (acknowledged["id"], False),
    ]
    assert rows[1]["credentials_acknowledged"] == ["aws-access-key-id"]
    assert [e["kind"] for e in store.iter_events()] == ["write", "write"]


@pytest.fixture
def guarded(
    workspace: Path,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Harness]:
    """The standing-guard harness (tests/test_standing_guard.py) with the
    server rebuilt under `[telemetry] enabled = false`."""
    fix_origin_and_identity(monkeypatch, tmp_path_factory.mktemp("checkout"))
    set_tau(monkeypatch)
    WORDS.clear()
    h = Harness(tmp_path / "store", monkeypatch)
    h.config = dataclasses.replace(h.config, telemetry=TelemetryConfig(enabled=False))
    h.server = build_server(config=h.config, store=h.store, state=h.state)
    yield h
    WORDS.clear()


async def test_telemetry_off_a_guarded_change_still_writes_its_standing_guard_row(
    guarded: Harness,
) -> None:
    h = guarded
    old = h.seed(RULE)
    h.store.put_standing(old, StandingAnswer(0.9, "asked", "test/test"))
    done = await call_tool(
        h.server,
        "memory_update",
        {"id": old, "content": UPDATING, "standing": 0.9, "said": QUOTE},
    )
    assert done["status"] == "committed", done
    (row,) = h.events("standing_guard")
    assert (row["door"], row["standing"]) == ("memory_update", [old])
    assert (row["said"], row["said_checked"]) == (QUOTE, False)
    # The update itself used no escape hatch: its row is telemetry, off.
    assert h.events("update") == []
