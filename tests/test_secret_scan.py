"""Tests for secret_scan.py: where a credential-shaped string persists.

Three contracts are pinned here:

1. Location. A secret planted in each place a store keeps text (a live
   record's body and free-text fields, a tombstone, an episode, a conflict
   quote, and every version the log keeps) is found there, with the right
   table or log seq, record, field, detector kind and record state.
2. No leak. No output, text or JSON, carries the secret value, nor the
   undetected partner that sits next to it.
3. Read-only. A scan appends no log row, moves no head, writes no key and
   creates no store.

Fixture note, as in tests/test_credentials.py: every secret-shaped value
is assembled from fragments via `_shaped(...)`, so no complete token
literal appears in this file for a push-protection scanner to match.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from bettermemory import secret_scan
from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.credentials import find_credential_markers
from bettermemory.log import iter_rows
from bettermemory.origin import Origin
from bettermemory.secret_scan import (
    LIVE,
    PRUNED,
    REMEDIATION,
    TOMBSTONED,
    SecretFinding,
    _strings,
    add_scan_parser,
    render_report,
    run_scan,
    scan_store,
)
from bettermemory.store import Store


def _shaped(*parts: str) -> str:
    """Join fragments into a secret-shaped value with no scannable literal."""
    return "".join(parts)


_AWS = _shaped("AKIA", "IOSFODNN7EXAMPLE")
# AWS's documented example secret access key: no detector matches it on
# its own, which is what makes it the partner the gate's snippet leaks.
_AWS_SECRET = _shaped("wJalrXUtnFEMI", "/K7MDENG/bPxRfiCY", "EXAMPLEKEY")
_GITHUB = _shaped("ghp_", "1234567890abcdefABCDEF1234567890abcd")
_GITHUB_OAUTH = _shaped("gho_", "Zyxwvu9876543210", "ABCDEFabcdef01234567")
_GENERIC_VALUE = _shaped("Hunter2", "Abc9XyZ12Q")
_URI_PASSWORD = _shaped("Zx9Qw8", "Er7Ty6Ui5Op4As")
_PEM_MATERIAL = _shaped("b3BlbnNzaC1rZXkt", "djEAAAAABG5vbmUAAAAEbm9uZQ")

# One planted body per detector kind: (kind, body, the value that must
# never be printed).
_PLANTS: tuple[tuple[str, str, str], ...] = (
    ("aws-access-key-id", f"deploy uses {_AWS} for prod", _AWS),
    (
        "openai-anthropic-key",
        f"model key {_shaped('sk-', 'abcdEFGH1234ijklMNOP5678')} here",
        _shaped("sk-", "abcdEFGH1234ijklMNOP5678"),
    ),
    (
        "stripe-key",
        f"billing {_shaped('sk_', 'live_4eC39HqLyjWDarjtT1zdp7dc')} live",
        _shaped("sk_", "live_4eC39HqLyjWDarjtT1zdp7dc"),
    ),
    ("github-token", f"ci token {_GITHUB} in actions", _GITHUB),
    (
        "slack-token",
        f"bot {_shaped('xoxb-', '1234567890-ABCDEFGHIJKLMNOP')} posts",
        _shaped("xoxb-", "1234567890-ABCDEFGHIJKLMNOP"),
    ),
    (
        "google-api-key",
        f"maps {_shaped('AIza', 'SyA1234567890abcdefghijklmnopqrstuv')} key",
        _shaped("AIza", "SyA1234567890abcdefghijklmnopqrstuv"),
    ),
    (
        "sendgrid-api-key",
        "mail "
        + _shaped(
            "SG.",
            "aBcDeFgH1234iJkLmNoP56",
            ".",
            "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfG",
        )
        + " key",
        _shaped("aBcDeFgH1234iJkLmNoP56", ".", "AbCdEfGhIjKlMnOpQrStUvWxYz0123"),
    ),
    (
        "vault-token",
        f"vault {_shaped('hvs.', 'AbCdEfGhIjKlMnOpQrStUvWx')} token",
        _shaped("hvs.", "AbCdEfGhIjKlMnOpQrStUvWx"),
    ),
    (
        "private-key-pem",
        "key:\n"
        + _shaped("-----BEGIN OPENSSH ", "PRIVATE KEY-----")
        + f"\n{_PEM_MATERIAL}\n"
        + _shaped("-----END OPENSSH ", "PRIVATE KEY-----"),
        _PEM_MATERIAL,
    ),
    (
        "jwt",
        "session "
        + _shaped(
            "eyJ",
            "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NSJ9.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g",
        )
        + " cookie",
        _shaped("dBjftJeZ4CVPmB92", "K27uhbUJU1p1r_wW1g"),
    ),
    (
        "generic-secret-assignment",
        f"the admin password = {_GENERIC_VALUE} for now",
        _GENERIC_VALUE,
    ),
    (
        "connection-uri-password",
        f"DATABASE_URL postgres://app:{_URI_PASSWORD}@db.internal/app",
        _URI_PASSWORD,
    ),
)


# Where each plant's detected span starts in its body: the token itself,
# or for the two guarded value rules the value after the keyword or the
# userinfo colon.
_STARTS = {
    "aws-access-key-id": _AWS,
    "openai-anthropic-key": "sk-",
    "stripe-key": "sk_",
    "github-token": "ghp_",
    "slack-token": "xoxb-",
    "google-api-key": "AIza",
    "sendgrid-api-key": "SG.",
    "vault-token": "hvs.",
    "private-key-pem": "-----BEGIN",
    "jwt": "eyJ",
    "generic-secret-assignment": _GENERIC_VALUE,
    "connection-uri-password": _URI_PASSWORD,
}


def _parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="bettermemory security")
    sub = root.add_subparsers(dest="security_cmd")
    add_scan_parser(sub)
    return root


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[Any, str, str]:
    """`security scan ARGV` through the real parser: (exit code, stdout,
    stderr). The command always exits."""
    args = _parser().parse_args(["scan", *argv])
    with pytest.raises(SystemExit) as exc:
        run_scan(args)
    captured = capsys.readouterr()
    return exc.value.code, captured.out, captured.err


def _put_seqs(store: Store, kind: str, key: str, record_id: str) -> list[int]:
    """The seqs of the `kind` rows whose payload[key] is `record_id`."""
    out = []
    for row in iter_rows(store.conn):
        if row.kind == kind and json.loads(row.payload)[key]["id"] == record_id:
            out.append(row.seq)
    return out


def _only(findings: list[SecretFinding], **match: Any) -> SecretFinding:
    hits = [
        f
        for f in findings
        if all(getattr(f, name) == value for name, value in match.items())
    ]
    assert len(hits) == 1, (match, findings)
    return hits[0]


# ---------------------------------------------------------------------------
# Clean stores
# ---------------------------------------------------------------------------


def test_clean_store_with_credential_prose_finds_nothing_and_exits_zero(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prose that names credential vocabulary is not a secret, in a body,
    a tombstone, an episode or the log."""
    store.write(
        content="The api_key config option defaults to None; rotate the password "
        "every 90 days and keep the access token in 1Password.",
        scopes=["projects:demo"],
    )
    gone = store.write(content="Password rotation policy is strict.", scopes=["x"])
    store.tombstone(gone.id, reason="duplicate")
    store.write_episode(session_id="s1", body="Talked about the api key policy.")

    report = scan_store(store, include_log=True)
    assert report.findings == []
    assert report.status == "clean"
    assert report.scanned["memories"] == 1
    assert report.scanned["tombstones"] == 1
    assert report.scanned["episodes"] == 1
    assert report.scanned["log"] == len(store.log_rows())

    code, out, _ = _run(capsys, "--log", "--store", str(store.path))
    assert code == 0
    assert out.startswith("bettermemory security scan: clean, 0 findings\n")
    code, out, _ = _run(capsys, "--json", "--store", str(store.path))
    assert code == 0
    parsed = json.loads(out)
    assert parsed["status"] == "clean"
    assert parsed["findings"] == []
    assert parsed["log_scanned"] is False
    assert "log" not in parsed["scanned"]


def test_an_empty_store_scans_clean(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(capsys, "--log", "--store", str(store.path))
    assert code == 0
    assert "scanned 0 memories, 0 tombstones, 0 episodes, 0 conflicts" in out


# ---------------------------------------------------------------------------
# Planted secrets are found where they sit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind, body, secret", _PLANTS, ids=[p[0] for p in _PLANTS])
def test_every_detector_kind_is_found_in_a_body_written_past_the_gate(
    store: Store, kind: str, body: str, secret: str
) -> None:
    """The Store API skips the handler's gate, as an import or an 8.x
    migration does: the scan finds the body live, with no gated write on
    record to say it was acknowledged."""
    memory = store.write(content=body, scopes=["projects:demo"])

    found = _only(scan_store(store).findings, location="memories")
    assert found.record_id == memory.id
    assert found.field == "body"
    assert found.kind == kind
    assert found.record_state == LIVE
    assert found.acknowledged is None
    assert found.seq is None and found.log_kind is None
    assert found.offset == memory.body.index(_STARTS[kind])
    assert found.occurrences == 1


def test_free_text_fields_beside_the_body_are_found(store: Store) -> None:
    """Claims, link notes and the origin's raw remote URL are stored
    beside the body and carry text the gate never reads."""
    target = store.write(content="The target record.", scopes=["x"])
    memory = store.write(
        content="A clean body.",
        scopes=["projects:demo"],
        claims=[f"ci uses {_GITHUB}"],
        links=[
            {
                "type": "extends",
                "target_id": target.id,
                "note": f"password = {_GENERIC_VALUE}",
            }
        ],
        origin=Origin(
            repo=f"https://x-access-token:{_GITHUB_OAUTH}@github.com/o/r.git"
        ),
    )

    findings = scan_store(store).findings
    # The row mirrors the remote into `origin_repo` beside `origin_json`:
    # one text, one finding, under the first column that holds it.
    assert {f.field for f in findings} == {"claims[0]", "links[0].note", "origin_repo"}
    assert all(f.record_id == memory.id for f in findings)
    assert _only(findings, field="claims[0]").kind == "github-token"
    assert _only(findings, field="links[0].note").kind == "generic-secret-assignment"

    logged = [f for f in scan_store(store, include_log=True).findings if f.seq]
    assert {f.field for f in logged} == {
        "memory.claims[0]",
        "memory.links[0].note",
        "memory.origin.repo",
    }


def test_an_edit_that_removes_the_secret_leaves_the_log_copy(store: Store) -> None:
    """The memory row is clean after the edit; the earlier `memory_put`
    still carries the body, and the record it names is still live."""
    memory = store.write(content=f"deploy uses {_AWS}", scopes=["projects:demo"])
    first_put = _put_seqs(store, "memory_put", "memory", memory.id)
    store.update(memory.model_copy(update={"body": "deploy uses the vault role\n"}))
    assert len(_put_seqs(store, "memory_put", "memory", memory.id)) == 2

    assert scan_store(store).findings == []
    found = _only(scan_store(store, include_log=True).findings, location="log")
    assert found.seq == first_put[0]
    assert found.log_kind == "memory_put"
    assert found.record_id == memory.id
    assert found.field == "memory.body"
    assert found.kind == "aws-access-key-id"
    assert found.record_state == LIVE


def test_remove_then_prune_leaves_only_the_log_copies(store: Store) -> None:
    """A removal keeps the body in the tombstone; a prune deletes the
    tombstone row, and only the log's `memory_put` and `tombstone_put`
    payloads still hold it, for a record no table holds."""
    memory = store.write(content=f"ci token {_GITHUB}", scopes=["projects:demo"])
    store.tombstone(memory.id, reason="leaked")

    found = _only(scan_store(store).findings, location="tombstones")
    assert found.record_id == memory.id
    assert found.record_state == TOMBSTONED
    assert found.field == "body"

    assert store.prune_tombstones(timedelta(0)) == [memory.id]
    report = scan_store(store, include_log=True)
    assert [f.location for f in report.findings] == ["log", "log"]
    assert [f.log_kind for f in report.findings] == ["memory_put", "tombstone_put"]
    assert [f.field for f in report.findings] == ["memory.body", "tombstone.body"]
    assert {f.record_state for f in report.findings} == {PRUNED}
    assert {f.record_id for f in report.findings} == {memory.id}


def test_episode_body_and_takeaway_are_found(store: Store) -> None:
    """Episodes skip the gate entirely: found in the table and in the
    log, never with an acknowledgement."""
    episode = store.write_episode(
        session_id="s1",
        body=f"pasted {_GITHUB_OAUTH} into the shell",
        takeaway=f"api_key = {_GENERIC_VALUE}",
    )

    report = scan_store(store, include_log=True)
    table = [f for f in report.findings if f.location == "episodes"]
    assert [(f.field, f.kind) for f in table] == [
        ("body", "github-token"),
        ("takeaway", "generic-secret-assignment"),
    ]
    logged = [f for f in report.findings if f.location == "log"]
    assert [f.field for f in logged] == ["episode.body", "episode.takeaway"]
    assert {f.log_kind for f in logged} == {"episode_put"}
    for finding in report.findings:
        assert finding.record_id == episode.id
        assert finding.record_state == LIVE
        assert finding.acknowledged is None


def test_a_conflict_quote_is_found(store: Store) -> None:
    """The conflict queue quotes the start of each record's body."""
    a = store.write(content="clean a", scopes=["x"])
    b = store.write(content="clean b", scopes=["x"])
    store.put_conflict(
        {
            "id": "conflict-1",
            "a_id": a.id,
            "b_id": b.id,
            "summary_a": f"deploy uses {_AWS}",
            "summary_b": "clean b",
            "similarity": 0.9,
            "method": "jaccard",
            "detector": "test",
            "created": "2026-01-01T00:00:00+00:00",
            "status": "pending",
        }
    )

    found = _only(scan_store(store).findings, location="conflicts")
    assert found.record_id == "conflict-1"
    assert found.field == "summary_a"
    assert found.acknowledged is None
    assert found.remediation == REMEDIATION["conflicts"]


def test_two_hits_of_one_kind_in_a_field_are_one_finding(store: Store) -> None:
    second = _shaped("AKIA", "QWERTYUIOPASDFGH")
    store.write(content=f"old {_AWS} and new {second}", scopes=["x"])

    found = _only(scan_store(store).findings, location="memories")
    assert found.occurrences == 2
    assert found.offset == len("old ")


# ---------------------------------------------------------------------------
# Acknowledgement, from the write and update events
# ---------------------------------------------------------------------------


def test_acknowledgement_reads_the_gated_write_events(store: Store) -> None:
    """True when a write or update event acknowledged the kind, False when
    the record has gated writes and none acknowledged it, None when the
    log holds no gated write for the record."""
    acked = store.write(content=f"a {_GITHUB}", scopes=["x"])
    store.record_event(
        "write",
        id=acked.id,
        status="committed",
        credentials_acknowledged=["github-token"],
    )
    unacked = store.write(content=f"b {_GITHUB}", scopes=["x"])
    store.record_event(
        "write", id=unacked.id, status="committed", credentials_acknowledged=[]
    )
    updated = store.write(content=f"c {_GITHUB}", scopes=["x"])
    store.record_event(
        "write", id=updated.id, status="committed", credentials_acknowledged=[]
    )
    store.record_event(
        "update", id=updated.id, credentials_acknowledged=["github-token"]
    )
    other_kind = store.write(content=f"d {_GITHUB}", scopes=["x"])
    store.record_event(
        "write",
        id=other_kind.id,
        status="committed",
        credentials_acknowledged=["aws-access-key-id"],
    )
    unknown = store.write(content=f"e {_GITHUB}", scopes=["x"])
    # A write event that does not carry the field (an 8.x import, a
    # refusal) says nothing about acknowledgement.
    store.record_event("write", id=unknown.id, status="duplicate")

    report = scan_store(store, include_log=True)
    table = {
        f.record_id: f.acknowledged for f in report.findings if f.location == "memories"
    }
    assert table == {
        acked.id: True,
        unacked.id: False,
        updated.id: True,
        other_kind.id: False,
        unknown.id: None,
    }
    logged = {
        f.record_id: f.acknowledged for f in report.findings if f.location == "log"
    }
    assert logged == table
    text = render_report(report)
    assert f"{acked.id} body: github-token at offset 2; acknowledged at write" in text
    assert f"{unacked.id} body: github-token at offset 2; not acknowledged" in text
    assert f"{unknown.id} body: github-token at offset 2; no gated write" in text


def test_a_telemetry_event_names_its_record_state_only_for_a_record(
    store: Store,
) -> None:
    """An event's `id` is a record's when a table holds it; otherwise the
    row names no record and its state is None, not `pruned`."""
    memory = store.write(content="clean", scopes=["x"])
    store.record_event("write", id=memory.id, claims=[f"uses {_GITHUB}"])
    store.record_event("probe_note", id="not-a-record", note=f"uses {_GITHUB}")

    logged = scan_store(store, include_log=True).findings
    assert [(f.log_kind, f.record_id, f.record_state) for f in logged] == [
        ("write", memory.id, LIVE),
        ("probe_note", "not-a-record", None),
    ]


def test_an_event_about_a_pruned_record_and_a_conflict_put_name_their_record(
    store: Store,
) -> None:
    """A write event outlives its record: once the tombstone is pruned it
    is about a record only the log still holds, `pruned`, not one that
    never existed. A `conflict_put` names the conflict it carries, live
    while the queue holds the row and pruned once it is deleted, as the
    table pass names it."""
    memory = store.write(content="clean", scopes=["x"])
    store.record_event(
        "write",
        id=memory.id,
        status="committed",
        credentials_acknowledged=[],
        claims=[f"uses {_GITHUB}"],
    )
    store.tombstone(memory.id, reason="leaked")
    assert store.prune_tombstones(timedelta(0)) == [memory.id]
    a = store.write(content="clean a", scopes=["x"])
    b = store.write(content="clean b", scopes=["x"])
    store.put_conflict(
        {
            "id": "cf-1",
            "a_id": a.id,
            "b_id": b.id,
            "summary_a": f"deploy uses {_AWS}",
            "summary_b": "clean b",
            "similarity": 0.9,
            "method": "jaccard",
            "detector": "test",
            "created": "2026-01-01T00:00:00+00:00",
            "status": "pending",
        }
    )

    def logged() -> list[tuple[str | None, str | None, str | None]]:
        return [
            (f.log_kind, f.record_id, f.record_state)
            for f in scan_store(store, include_log=True).findings
            if f.location == "log"
        ]

    assert logged() == [
        ("write", memory.id, PRUNED),
        ("conflict_put", "cf-1", LIVE),
    ]
    store.delete_conflict("cf-1")
    assert logged() == [
        ("write", memory.id, PRUNED),
        ("conflict_put", "cf-1", PRUNED),
    ]


# ---------------------------------------------------------------------------
# Never the value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind, body, secret", _PLANTS, ids=[p[0] for p in _PLANTS])
def test_no_output_contains_the_secret(
    store: Store,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    body: str,
    secret: str,
) -> None:
    """Planted in a live body, a claim, a tombstone and an episode, and so
    in the log: neither the text nor the JSON report carries the value or
    its first or last twelve characters."""
    store.write(content=body, scopes=["x"], claims=[body])
    gone = store.write(content=body, scopes=["x"])
    store.tombstone(gone.id, reason="leaked")
    store.write_episode(session_id="s1", body=body, takeaway=body)

    for flags in ((), ("--json",)):
        code, out, err = _run(capsys, "--log", *flags, "--store", str(store.path))
        assert code == 1
        assert kind in out
        for needle in (secret, secret[:12], secret[-12:]):
            assert needle not in out
            assert needle not in err


def test_the_partner_of_a_detected_secret_is_not_printed(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """An AWS key id followed by its secret access key: the gate's snippet
    redacts the id and keeps 23 characters of the secret key as context,
    which is why the scan prints an offset instead of a snippet."""
    body = f"prod creds {_AWS} {_AWS_SECRET} rotate quarterly"
    (marker,) = find_credential_markers(body)
    assert _AWS_SECRET[:20] in marker.snippet

    store.write(content=body, scopes=["x"])
    for flags in ((), ("--json",)):
        code, out, _ = _run(capsys, "--log", *flags, "--store", str(store.path))
        assert code == 1
        assert "aws-access-key-id" in out
        assert _AWS_SECRET[:8] not in out
        assert _AWS not in out


def test_a_secret_shaped_json_key_prints_as_a_mask(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """A field path is built from the payload's keys; a key that is a
    secret, or not a plain identifier, prints as `*`."""
    store.record_event(
        "probe_note",
        blob={_GITHUB: f"password = {_GENERIC_VALUE}", "two words": _AWS},
    )

    logged = scan_store(store, include_log=True).findings
    assert {(f.field, f.kind) for f in logged} == {
        ("blob.*", "generic-secret-assignment"),
        ("blob.*", "aws-access-key-id"),
    }
    assert {f.record_state for f in logged} == {None}
    code, out, _ = _run(capsys, "--log", "--json", "--store", str(store.path))
    assert code == 1
    assert _GITHUB not in out
    assert "two words" not in out


def test_a_secret_shaped_record_id_or_log_kind_prints_as_a_mask(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """The id column and an event's `id` are scanned text, and a row
    written outside the store can hold anything there or in a log row's
    kind: a finding about such a value must not print it as the record
    or the kind it is about. A terminal escape prints as `*` too."""
    store.record_event("probe_note", id=_GITHUB, note="clean")
    store.conn.execute(
        "INSERT INTO memories(id, created, updated, confidence, body, scopes_text, "
        "scopes_json, provenance, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_AWS, "x", "x", "x", "clean", "", "[]", "local", "explicit"),
    )
    store.conn.execute(
        "INSERT INTO memories(id, created, updated, confidence, body, scopes_text, "
        "scopes_json, provenance, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("\x1b[2Jforged", "x", "x", "x", f"uses {_GITHUB}", "", "[]", "l", "e"),
    )
    last = store.log_rows()[-1]
    store.conn.execute(
        "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) "
        "VALUES (?, ?, NULL, ?, ?, ?, ?)",
        (last.seq + 1, last.ts, _GITHUB, json.dumps({"note": _AWS}), last.mac, "0"),
    )

    report = scan_store(store, include_log=True)
    assert {(f.location, f.record_id, f.field) for f in report.findings} == {
        ("memories", "*", "id"),
        ("memories", "*", "body"),
        ("log", "*", "id"),
        ("log", None, "note"),
    }
    assert _only(report.findings, field="note").log_kind == "*"
    for flags in ((), ("--json",)):
        code, out, _ = _run(capsys, "--log", *flags, "--store", str(store.path))
        assert code == 1
        for needle in (_GITHUB, _AWS, "\x1b", "\\u001b"):
            assert needle not in out


# ---------------------------------------------------------------------------
# Rows the models refuse and payloads that are not JSON
# ---------------------------------------------------------------------------


def test_a_row_the_models_refuse_is_still_scanned(store: Store) -> None:
    """A row that entered outside the store, with a stamp the loader
    refuses and a JSON column that does not parse: `iter_memories` raises
    on it, the scan reads it as text."""
    store.conn.execute(
        "INSERT INTO memories(id, created, updated, confidence, body, scopes_text, "
        "scopes_json, provenance, source, claims_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "outside-1",
            "not a stamp",
            "not a stamp",
            "bogus",
            f"pasted {_AWS}",
            "",
            "[]",
            "local",
            "explicit",
            f"not json {_GITHUB}",
        ),
    )
    with pytest.raises(ValueError):
        list(store.iter_memories())

    findings = scan_store(store).findings
    assert {(f.field, f.kind) for f in findings} == {
        ("body", "aws-access-key-id"),
        ("claims", "github-token"),
    }
    assert {f.record_id for f in findings} == {"outside-1"}


def test_a_text_column_holding_a_blob_is_scanned(store: Store) -> None:
    """SQLite keeps the type a statement binds, so a row written outside
    the store can hold its body as a BLOB: the scan reads it as text."""
    store.conn.execute(
        "INSERT INTO memories(id, created, updated, confidence, body, scopes_text, "
        "scopes_json, provenance, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "outside-2",
            "x",
            "x",
            "x",
            f"pasted {_AWS}".encode(),
            "",
            "[]",
            "local",
            "explicit",
        ),
    )

    found = _only(scan_store(store).findings, location="memories")
    assert (found.record_id, found.field, found.kind) == (
        "outside-2",
        "body",
        "aws-access-key-id",
    )


def test_a_log_payload_that_is_not_json_is_scanned_as_text(store: Store) -> None:
    last = store.log_rows()[-1]
    store.conn.execute(
        "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) "
        "VALUES (?, ?, NULL, ?, ?, ?, ?)",
        (
            last.seq + 1,
            last.ts,
            "outside_note",
            f"free text password = {_GENERIC_VALUE}",
            last.mac,
            "0" * 64,
        ),
    )

    found = _only(scan_store(store, include_log=True).findings, location="log")
    assert found.field == "payload"
    assert found.seq == last.seq + 1
    assert found.log_kind == "outside_note"
    assert found.record_id is None
    assert found.record_state is None
    assert found.acknowledged is None


def test_field_walk_survives_deep_nesting() -> None:
    """A payload written outside the store can nest arbitrarily deep; the
    walk is iterative, so it cannot exhaust the stack."""
    value: Any = "leaf"
    for _ in range(5000):
        value = [value]
    ((path, text),) = list(_strings(value, "f"))
    assert text == "leaf"
    assert path == "f" + "[0]" * 5000


# ---------------------------------------------------------------------------
# Read-only, the report and the CLI
# ---------------------------------------------------------------------------


def test_scan_writes_nothing_and_never_rekeys(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With its key: no log row, the head checkpoint untouched. Without
    its key (a copy from another machine): no key written, no rekey row."""
    from bettermemory import config as _config
    from bettermemory import log as _log

    store.write(content=f"deploy uses {_AWS}", scopes=["x"])
    rows = len(store.log_rows())
    head = store.keyring.head_path()
    head_bytes = head.read_bytes()
    key_files = sorted(p.name for p in store.keys_dir.iterdir())

    assert _run(capsys, "--log", "--store", str(store.path))[0] == 1
    assert len(store.log_rows()) == rows
    assert head.read_bytes() == head_bytes
    assert sorted(p.name for p in store.keys_dir.iterdir()) == key_files

    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setattr(_log, "default_keys_dir", lambda: elsewhere)
    monkeypatch.setenv(_config.KEYS_DIR_ENV, str(elsewhere))
    code, out, _ = _run(capsys, "--log", "--json", "--store", str(store.path))
    assert code == 1
    assert json.loads(out)["counts"]["memories"] == 1
    assert not elsewhere.exists()
    assert len(store.log_rows()) == rows


def test_the_scan_reads_one_snapshot(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon writes while a scan runs. A restore landing between the
    memories and the tombstones pass would take the record out of the
    tombstones before that pass and into the memories after it, hiding
    it from both; one read transaction keeps the scan on the store as it
    was when the scan began."""
    memory = store.write(content=f"ci token {_GITHUB}", scopes=["x"])
    store.tombstone(memory.id, reason="leaked")
    daemon = store.reopen()
    scan_table = secret_scan._scan_table

    def restore_after_memories(
        scanned: Store, table: str, acks: Any
    ) -> tuple[int, list[SecretFinding]]:
        out = scan_table(scanned, table, acks)
        if table == "memories":
            daemon.restore(memory.id)
        return out

    monkeypatch.setattr(secret_scan, "_scan_table", restore_after_memories)
    try:
        report = scan_store(store, include_log=True)
    finally:
        daemon.close()
    assert store.count_memories() == 1
    assert [(f.location, f.record_state) for f in report.findings] == [
        ("tombstones", TOMBSTONED),
        ("log", TOMBSTONED),
        ("log", TOMBSTONED),
    ]
    assert not store.conn.in_transaction
    # A caller already in a transaction keeps it: the scan neither nests
    # a BEGIN nor ends the caller's snapshot.
    monkeypatch.undo()
    store.conn.execute("BEGIN")
    try:
        assert len(scan_store(store).findings) == 1
        assert store.conn.in_transaction
    finally:
        store.conn.execute("ROLLBACK")


def test_a_store_that_fails_mid_scan_exits_two(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A store whose header opens but whose pages do not read (a damaged
    copy) is a store the scan cannot read: exit 2 with a message, not a
    traceback whose exit status 1 a CI gate would take for a finding."""

    def damaged(scanned: Store, *, include_log: bool = False) -> None:
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(secret_scan, "scan_store", damaged)
    code, out, err = _run(capsys, "--store", str(store.path))
    assert code == 2
    assert out == ""
    assert "cannot read the store at" in err
    assert "malformed" in err


def test_no_store_exits_two_and_creates_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    code, out, err = _run(capsys, "--store", str(empty))
    assert code == 2
    assert out == ""
    assert "no bettermemory 9 store" in err
    assert not (empty / "memory.sqlite").exists()


def test_a_store_file_under_another_name_exits_two_with_the_rule(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every store opener reads a path not named memory.sqlite as the
    directory; the error says so instead of naming a path inside a file."""
    backup = store.path.parent / "backup.sqlite"
    backup.write_bytes(store.path.read_bytes())
    code, out, err = _run(capsys, "--store", str(backup))
    assert code == 2
    assert out == ""
    assert "copy backup.sqlite to memory.sqlite in a directory of its own" in err


def test_a_file_that_is_not_a_store_exits_two_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "memory.sqlite").write_bytes(b"not a database, just bytes" * 64)
    code, out, err = _run(capsys, "--store", str(junk))
    assert code == 2
    assert out == ""
    assert "cannot read the store at" in err


def test_default_store_is_the_configured_one(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store.write(content=f"deploy uses {_AWS}", scopes=["x"])
    config = Config(
        storage=StorageConfig(directory=str(store.path.parent)),
        behavior=BehaviorConfig(),
    )
    monkeypatch.setattr("bettermemory.config.load_config", lambda create=True: config)

    code, out, _ = _run(capsys)
    assert code == 1
    assert f"store {store.path}" in out


def test_store_flag_takes_the_file_itself(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    store.write(content=f"deploy uses {_AWS}", scopes=["x"])
    assert store.path.name == "memory.sqlite"
    assert _run(capsys, "--store", str(store.path))[0] == 1
    assert _run(capsys, "--store", str(store.path.parent))[0] == 1


def test_text_report_groups_findings_with_their_remediation(store: Store) -> None:
    live = store.write(content=f"deploy uses {_AWS}", scopes=["x"])
    gone = store.write(content=f"ci token {_GITHUB}", scopes=["x"])
    store.tombstone(gone.id, reason="leaked")

    text = render_report(scan_store(store))
    assert "found, 2 findings" in text
    assert "scanned 1 memory, 1 tombstone, 0 episodes, 0 conflicts" in text
    assert "log not scanned (pass --log)" in text
    assert f"{live.id} body: aws-access-key-id at offset 12; no gated write" in text
    assert REMEDIATION["memories"] in text
    assert REMEDIATION["tombstones"] in text
    assert REMEDIATION["log"] not in text
    assert "not scanned: freed pages" in text

    text = render_report(scan_store(store, include_log=True))
    assert REMEDIATION["log"] in text
    assert "no command erases a log row today" in text
    assert f"memory_put {live.id} memory.body: aws-access-key-id" in text
    assert "; record tombstoned;" in text


def test_saved_orphans_beside_the_store_are_named_not_scanned(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    orphan = store.path.parent / "memory.sqlite.orphaned-20260101T000000Z.sqlite"
    orphan.write_bytes(b"")

    report = scan_store(store)
    assert report.orphans == [orphan.name]
    assert f"saved orphan not scanned: {orphan.name}" in render_report(report)
    code, out, _ = _run(capsys, "--json", "--store", str(store.path))
    assert code == 0
    assert json.loads(out)["orphans"] == [orphan.name]


def test_help_documents_the_exit_codes_and_the_read_only_open() -> None:
    root = argparse.ArgumentParser(prog="bettermemory security")
    sub = root.add_subparsers(dest="security_cmd")
    parser = add_scan_parser(sub)
    description = parser.description or ""
    assert "Exit 0 when nothing is found, 1 when anything is, 2" in description
    assert "Read-only" in description
    args = parser.parse_args(["--log", "--json", "--store", "x"])
    assert (args.scan_log, args.scan_json, args.scan_store) == (True, True, "x")


def test_a_raw_sqlite_reader_sees_the_same_rows(store: Store) -> None:
    """The table pass reads every row the file holds: a second connection
    counts the same."""
    store.write(content="clean", scopes=["x"])
    store.write_episode(session_id="s1", body="clean")
    report = scan_store(store)
    raw = sqlite3.connect(str(store.path))
    try:
        for table in ("memories", "tombstones", "episodes", "conflicts"):
            (count,) = raw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            assert report.scanned[table] == count
    finally:
        raw.close()
