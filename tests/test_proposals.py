"""Tests for the write-reflex proposal queue + heuristic extractor.

Covers extraction precision (what gets proposed vs rejected), the
on-disk queue round-trip + flock-protected mutation, and the
enqueue/dedup/cap behaviour of `propose_from_exchange`. The MCP review
surface (`memory_proposals`) and the Stop-hook wiring are exercised in
`test_server.py` / `test_hook.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from bettermemory.proposals import (
    Proposal,
    ProposalQueue,
    extract_proposals,
    propose_from_exchange,
)


_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# extract_proposals — precision
# ---------------------------------------------------------------------------


def test_extract_catches_first_person_preference() -> None:
    props = extract_proposals(
        "I prefer terse code-driven explanations over long prose paragraphs.",
        now=_NOW,
    )
    assert len(props) == 1
    assert props[0].suggested_category == "user-inference"
    assert "terse code-driven" in props[0].body
    assert props[0].source_excerpt == props[0].body


def test_extract_catches_explicit_remember_request_even_if_command_shaped() -> None:
    # Opens like a request to the assistant ("can you"), but the explicit
    # "remember that" marker overrides the question/command reject.
    props = extract_proposals(
        "Can you remember that we deploy to fly.io for all production releases?",
        now=_NOW,
    )
    assert len(props) == 1
    assert props[0].suggested_category == "fact"


def test_extract_catches_my_setup_fact() -> None:
    props = extract_proposals(
        "My editor is neovim with a heavily customised lua config.",
        now=_NOW,
    )
    assert len(props) == 1
    assert props[0].suggested_category == "user-inference"


def test_extract_rejects_questions() -> None:
    assert (
        extract_proposals("What is the best database for this workload?", now=_NOW)
        == []
    )


def test_extract_rejects_task_requests_to_the_assistant() -> None:
    assert (
        extract_proposals("Could you refactor the auth module for me here?", now=_NOW)
        == []
    )


def test_extract_rejects_transient_state() -> None:
    # Matches a preference pattern but trips a transient marker → not durable.
    assert (
        extract_proposals(
            "I prefer to currently run everything against the staging cluster.",
            now=_NOW,
        )
        == []
    )


def test_extract_rejects_sentences_without_a_durable_marker() -> None:
    assert (
        extract_proposals("The weather today is quite pleasant outside.", now=_NOW)
        == []
    )


def test_extract_rejects_too_short() -> None:
    assert extract_proposals("I like it.", now=_NOW) == []


def test_extract_handles_empty_and_none() -> None:
    assert extract_proposals(None, now=_NOW) == []
    assert extract_proposals("   ", now=_NOW) == []


def test_extract_caps_at_max_proposals() -> None:
    text = (
        "I prefer dark mode for every editor I use. "
        "I always run the linter before committing my code. "
        "We use postgres for the primary datastore everywhere. "
        "My shell of choice is zsh with starship configured."
    )
    props = extract_proposals(text, now=_NOW, max_proposals=2)
    assert len(props) == 2


# ---------------------------------------------------------------------------
# ProposalQueue — persistence
# ---------------------------------------------------------------------------


def _proposal(body: str, *, pid: str = "01J0", cat: str = "fact") -> Proposal:
    return Proposal(
        id=pid,
        body=body,
        source_excerpt=body,
        suggested_category=cat,
        created=_NOW.isoformat(),
    )


def test_queue_empty_when_no_file(tmp_path: Path) -> None:
    assert ProposalQueue(tmp_path).load() == []


def test_queue_append_and_load_round_trip(tmp_path: Path) -> None:
    q = ProposalQueue(tmp_path)
    q.append(
        [
            _proposal("first body here", pid="a1"),
            _proposal("second body here", pid="a2"),
        ]
    )
    loaded = q.load()
    assert [p.id for p in loaded] == ["a1", "a2"]
    assert loaded[0].body == "first body here"
    # 0o600 on the queue file (carries the user's words — same privacy bar).
    assert (tmp_path / ".write_proposals.jsonl").exists()


def test_queue_append_empty_is_noop(tmp_path: Path) -> None:
    q = ProposalQueue(tmp_path)
    q.append([])
    assert not (tmp_path / ".write_proposals.jsonl").exists()


def test_queue_remove_returns_and_drops(tmp_path: Path) -> None:
    q = ProposalQueue(tmp_path)
    q.append(
        [_proposal("alpha body text", pid="a1"), _proposal("beta body text", pid="a2")]
    )
    removed = q.remove("a1")
    assert removed is not None and removed.id == "a1"
    assert [p.id for p in q.load()] == ["a2"]


def test_queue_remove_unknown_is_none(tmp_path: Path) -> None:
    q = ProposalQueue(tmp_path)
    q.append([_proposal("alpha body text", pid="a1")])
    assert q.remove("nope") is None
    assert len(q.load()) == 1


def test_queue_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / ".write_proposals.jsonl"
    good = _proposal("good body text", pid="g1")
    import json

    path.write_text(
        "not json at all\n"
        + json.dumps(good.to_dict())
        + "\n"
        + '{"missing": "id and body"}\n',
        encoding="utf-8",
    )
    loaded = ProposalQueue(tmp_path).load()
    assert [p.id for p in loaded] == ["g1"]


# ---------------------------------------------------------------------------
# propose_from_exchange — enqueue / dedup / cap
# ---------------------------------------------------------------------------


def test_propose_from_exchange_enqueues_new(tmp_path: Path) -> None:
    fresh = propose_from_exchange(
        tmp_path,
        user_text="I prefer hands-on tutorials with runnable code, not screenshots.",
        now=_NOW,
    )
    assert len(fresh) == 1
    assert len(ProposalQueue(tmp_path).load()) == 1


def test_propose_from_exchange_dedups_against_queue(tmp_path: Path) -> None:
    text = "I prefer hands-on tutorials with runnable code, not screenshots."
    propose_from_exchange(tmp_path, user_text=text, now=_NOW)
    # Same sentence again → nothing new appended (dedup by source_excerpt).
    again = propose_from_exchange(tmp_path, user_text=text, now=_NOW)
    assert again == []
    assert len(ProposalQueue(tmp_path).load()) == 1


def test_propose_from_exchange_respects_max_pending(tmp_path: Path) -> None:
    q = ProposalQueue(tmp_path)
    q.append([_proposal("already queued body", pid="x1")])
    fresh = propose_from_exchange(
        tmp_path,
        user_text="I always squash my commits before opening a pull request.",
        max_pending=1,  # queue already full
        now=_NOW,
    )
    assert fresh == []
    assert len(q.load()) == 1


# ---------------------------------------------------------------------------
# append_within_cap — cap + dedup enforced under the lock (TOCTOU guard)
# ---------------------------------------------------------------------------


def test_append_within_cap_enforces_room_and_dedup(tmp_path: Path) -> None:
    """The cap and the source_excerpt dedup are computed against the
    under-lock snapshot, not a stale pre-lock read — so a batch larger than
    the remaining room is trimmed and queue-duplicates are dropped."""
    q = ProposalQueue(tmp_path)
    q.append([_proposal("existing one", pid="e1")])
    appended = q.append_within_cap(
        [
            _proposal("existing one", pid="dup"),  # dups e1 by excerpt → dropped
            _proposal("brand new two", pid="n2"),
            _proposal("brand new three", pid="n3"),  # over the room of 1 → trimmed
        ],
        max_pending=2,
    )
    assert [p.id for p in appended] == ["n2"]
    assert [p.id for p in q.load()] == ["e1", "n2"]


def test_append_within_cap_returns_empty_when_full(tmp_path: Path) -> None:
    """A full queue admits nothing and leaves the file untouched."""
    q = ProposalQueue(tmp_path)
    q.append([_proposal("a body", pid="a1"), _proposal("b body", pid="b1")])
    assert q.append_within_cap([_proposal("c body", pid="c1")], max_pending=2) == []
    assert [p.id for p in q.load()] == ["a1", "b1"]


def test_append_within_cap_empty_candidates_is_noop(tmp_path: Path) -> None:
    q = ProposalQueue(tmp_path)
    assert q.append_within_cap([], max_pending=5) == []
    assert not (tmp_path / ".write_proposals.jsonl").exists()
