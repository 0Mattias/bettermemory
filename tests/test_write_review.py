"""The write review on `memory_write`, handlers in-process on a scratch store.

Unit D1 (TESTS FIRST item 3). A write that passes the six gates writes
nothing and returns a review: the related stored statements and the three
questions. The same call re-sent with `review` answers is linked, filed,
refused or committed. Every test starts with the thresholds in
`bettermemory.review` None (conftest's `review_tau_unset`), so every test
that wants a review sets them through the `tau` fixture;
`test_no_review_issued_while_tau_unset` pins the unset behaviour.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from bettermemory import _handlers
from bettermemory import identity
from bettermemory import review
from bettermemory import supersession
from bettermemory.builder import build_server
from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.conflicts import ConflictQueue
from bettermemory.events import Recorder
from bettermemory.handlers.search import resolve_search_pool
from bettermemory.log import CONTROL_KINDS
from bettermemory.models import generate_ulid
from bettermemory.origin import Origin
from bettermemory.search import search as run_search
from bettermemory.session import SessionState
from bettermemory.store import MUTATION_KINDS, Store

from ._mcp import call_tool

TAU = 0.5

ORIGIN = Origin(
    cwd="/work/alpha",
    repo="https://example.test/alpha.git",
    branch="main",
    worktree_root="/work/alpha",
)
OTHER_ORIGIN = Origin(
    cwd="/work/beta",
    repo="https://example.test/beta.git",
    branch="main",
    worktree_root="/work/beta",
)

# A claim and its update by the cue code's own rule: the change cue "now"
# and two port values of one shape, so the cue code links the update.
PORT_OLD = "The staging API listens on port 8443 behind the gateway."
PORT_NEW = "The staging API now listens on port 9443 behind the gateway."

# Two values of one shape and no change cue: the cue code files the pair
# as a conflict and links nothing.
CUE_CONFLICT_OLD = "The billing service runs on port 8443."
CUE_CONFLICT_NEW = "The billing service runs on port 9443."

# A pair the cue code leaves alone (no kin values, no shared context), so
# whatever happens to it is the review's doing.
BACKUP_OLD = "Nightly database backups are handled by Restic."
BACKUP_NEW = "Backups moved to Borg for the archive volumes."

# Word-bounded, so a ULID that happens to spell a name inside it is not one.
MODEL_NAMES = re.compile(r"\b(claude|opus|sonnet|haiku|gpt|gemini|llama)\b", re.I)


def _shaped(*parts: str) -> str:
    """Join fragments into a secret-shaped value with no scannable literal."""
    return "".join(parts)


_AWS = _shaped("AKIA", "IOSFODNN7EXAMPLE")


@pytest.fixture
def tau(monkeypatch: pytest.MonkeyPatch) -> float:
    """Set both thresholds for the test; the shipped constants are None."""
    monkeypatch.setattr(review, "TAU_UNSAFE", TAU)
    monkeypatch.setattr(review, "TAU_SECRET", TAU)
    return TAU


@pytest.fixture(autouse=True)
def _fixed_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every write captures the same origin, so the related set's repo and
    worktree filters do not depend on where the suite runs."""
    monkeypatch.setattr(_handlers, "capture_origin", lambda *a, **k: ORIGIN)
    monkeypatch.delenv(identity.ENV_CLIENT, raising=False)
    monkeypatch.delenv(identity.ENV_MODEL, raising=False)


class Harness:
    """One server over one scratch store, with its recorder on."""

    def __init__(self, root: Path, *, behavior: BehaviorConfig | None = None) -> None:
        self.root = root
        self.config = Config(
            storage=StorageConfig(directory=str(root)),
            behavior=behavior or BehaviorConfig(),
        )
        self.store = Store(root)
        self.state = SessionState()
        self.server = build_server(
            config=self.config,
            store=self.store,
            state=self.state,
            recorder=Recorder(store=self.store, session_id=self.state.session_id),
        )

    def seed(self, content: str, *, scopes: list[str] | None = None, **kw: Any) -> str:
        memory = self.store.write(
            content=content,
            scopes=scopes or ["tools"],
            origin=kw.pop("origin", ORIGIN),
            **kw,
        )
        return memory.id

    async def write(
        self, content: str, *, scopes: list[str] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"content": content, "scopes": scopes or ["tools"]}
        args.update(kwargs)
        return await call_tool(self.server, "memory_write", args)

    async def answer(
        self,
        first: dict[str, Any],
        content: str,
        *,
        label: str = "unrelated",
        unsafe: float = 0.0,
        secret: float = 0.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Re-send `content` answering every id `first` showed with `label`."""
        assert first["status"] == "review", first
        body = first["review"]
        return await self.write(
            content,
            review={
                "review_id": body["review_id"],
                "relation": {entry["id"]: label for entry in body["related"]},
                "unsafe": unsafe,
                "secret": secret,
            },
            **kwargs,
        )

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.store.iter_events() if e.get("kind") == kind]

    def raw_log(self) -> str:
        """The telemetry rows' raw payload text. Mutation rows carry the
        committed bodies by design and are not the log a body must stay
        out of."""
        return "".join(
            str(row["payload"])
            for row in self.store.conn.execute(
                "SELECT kind, payload FROM log ORDER BY seq"
            )
            if row["kind"] not in MUTATION_KINDS and row["kind"] not in CONTROL_KINDS
        )

    def memory_rows(self) -> int:
        return len(self.store.load_all())

    def put_rows(self) -> int:
        return sum(
            1
            for row in self.store.conn.execute("SELECT kind FROM log")
            if row["kind"] == "memory_put"
        )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path / "store")


def _related_ids(response: dict[str, Any]) -> list[str]:
    return [entry["id"] for entry in response["review"]["related"]]


def _no_model_named(payload: Any) -> None:
    blob = payload if isinstance(payload, str) else json.dumps(payload)
    assert MODEL_NAMES.search(blob) is None, blob


# ---------------------------------------------------------------------------
# The first call
# ---------------------------------------------------------------------------


async def test_passing_write_returns_review_and_writes_nothing(
    harness: Harness, tau: float
) -> None:
    harness.seed(PORT_OLD)
    rows_before, puts_before = harness.memory_rows(), harness.put_rows()
    first = await harness.write(PORT_NEW)
    assert first["status"] == "review"
    assert first["review"]["related"]
    assert harness.memory_rows() == rows_before
    assert harness.put_rows() == puts_before
    assert harness.events("write") == []


async def test_review_response_says_not_written(harness: Harness, tau: float) -> None:
    first = await harness.write("The ledger app deploys from the tools folder.")
    assert set(first) == {"status", "written", "review", "hint"}
    assert first["status"] == "review"
    assert first["written"] is False
    assert first["hint"].startswith("Not written.")
    assert first["review"]["review_id"] in first["hint"]
    assert set(first["review"]) == {"review_id", "related", "questions"}
    assert set(first["review"]["questions"]) == {"relation", "unsafe", "secret"}


async def test_regex_secret_refused_before_any_review(
    harness: Harness, tau: float
) -> None:
    res = await harness.write(f"The deploy key for the ledger app is {_AWS} today.")
    assert res["status"] == "credential_warning"
    assert "aws-access-key-id" in [m["kind"] for m in res["markers"]]
    assert harness.events(review.REVIEW_ISSUED) == []


async def test_transient_refusal_precedes_review(harness: Harness, tau: float) -> None:
    res = await harness.write("The ledger deploy is currently blocked on the runner.")
    assert res["status"] == "transient_warning"
    assert harness.events(review.REVIEW_ISSUED) == []


async def test_duplicate_refused_without_review(harness: Harness, tau: float) -> None:
    harness.seed(PORT_OLD)
    res = await harness.write(PORT_OLD)
    assert res["status"] == "duplicate"
    assert "evidence" not in res
    assert harness.events(review.REVIEW_ISSUED) == []


_GATE_CASES: dict[str, tuple[str, list[str], str]] = {
    "credential": (
        f"The deploy key for the ledger app is {_AWS} for now.",
        ["tools"],
        "credential_warning",
    ),
    "transient": (
        "The ledger deploy is currently blocked on the runner.",
        ["tools"],
        "transient_warning",
    ),
    "user claim": (
        "Mattias prefers tabs over spaces.",
        ["tools"],
        "user_claim_warning",
    ),
    "scope mismatch": (
        "When working on foo, the build script lives at scripts/build.sh.",
        ["tools"],
        "scope_mismatch",
    ),
    "duplicate": (PORT_OLD, ["tools"], "duplicate"),
    "previously removed": (BACKUP_OLD, ["tools"], "previously_removed"),
}


@pytest.mark.parametrize("gate", list(_GATE_CASES))
async def test_gate_refusals_all_precede_review(
    harness: Harness, tau: float, gate: str
) -> None:
    harness.seed("A foo project fact.", scopes=["projects:foo"])
    harness.seed(PORT_OLD)
    removed = harness.seed(BACKUP_OLD)
    harness.store.tombstone(removed, "superseded by the archive plan")
    content, scopes, status = _GATE_CASES[gate]
    res = await harness.write(content, scopes=scopes)
    assert res["status"] == status
    assert harness.events(review.REVIEW_ISSUED) == []


async def test_force_skips_dedup_not_review(harness: Harness, tau: float) -> None:
    harness.seed(PORT_OLD)
    removed = harness.seed(BACKUP_OLD)
    harness.store.tombstone(removed, "superseded by the archive plan")
    duplicate = await harness.write(PORT_OLD, force=True)
    assert duplicate["status"] == "review"
    previously_removed = await harness.write(BACKUP_OLD, force=True)
    assert previously_removed["status"] == "review"
    assert len(harness.events(review.REVIEW_ISSUED)) == 2
    answered = await harness.answer(duplicate, PORT_OLD, force=True)
    assert answered["status"] == "committed"


# ---------------------------------------------------------------------------
# The related set
# ---------------------------------------------------------------------------

BILLING_NEW = "The billing service now runs on port 9443 in the eu-west region."
# One claim stored four ways, each with the old port: the cue code links
# every restatement of the claim it replaces (`_one_claim_replaced`).
_BILLING_CUE_TARGETS = (
    "The billing service runs on port 8443.",
    "The billing service listens on port 8443.",
    "The billing service is served on port 8443.",
    "The billing service answers on port 8443.",
)


def _long_billing_record(n: int) -> str:
    """A record that is not claim-sized (six sentences, over 600
    characters), dense in the write's words so the engine ranks it high."""
    sentences = [
        f"Record {n}: the billing service in the eu-west region runs behind "
        "the shared gateway and serves invoices for every tenant.",
        "The billing service port is set in the region config and rotated "
        "with the gateway certificates during each maintenance window.",
        "Operators restart the billing service region by region, eu-west last, "
        "after draining the queue of pending invoice jobs.",
        "The runbook lists the billing service health checks, the port probes "
        "and the alert routing for the eu-west region.",
        "Capacity for the billing service is planned per region, and eu-west "
        "carries the largest share of the invoice volume.",
        "Changes to the billing service go through review by the payments "
        "team before they reach the eu-west region.",
    ]
    return " ".join(sentences)


def _seed_billing(harness: Harness) -> tuple[list[str], list[str]]:
    longs = [harness.seed(_long_billing_record(n)) for n in range(3)]
    cues = [harness.seed(text) for text in _BILLING_CUE_TARGETS]
    return longs, cues


def _engine_top3(harness: Harness, content: str) -> list[str]:
    pool = resolve_search_pool(
        harness.store,
        content,
        excluded_scopes=set(),
        repo_filter=ORIGIN.repo,
        worktree_filter=ORIGIN.worktree_root,
        min_survivors=review.RELATED_TOP,
    )
    hits = run_search(
        pool.memories,
        content,
        repo_filter=ORIGIN.repo,
        worktree_filter=ORIGIN.worktree_root,
        max_results=review.RELATED_TOP,
        corpus_stats_provider=pool.corpus_stats_provider,
    )
    return [h.id for h in hits]


async def test_related_is_engine_top3_plus_cue_targets_capped_at_5(
    harness: Harness, tau: float
) -> None:
    _, cues = _seed_billing(harness)
    top3 = _engine_top3(harness, BILLING_NEW)
    report = supersession.detect_supersession(BILLING_NEW, harness.store.load_all())
    cue_ids = [m.memory_id for m in [*report.supersedes, *report.conflicts]]
    assert set(cue_ids) == set(cues)
    expected = list(top3)
    for memory_id in cue_ids:
        if memory_id not in expected:
            expected.append(memory_id)
    expected = expected[: review.RELATED_CAP]

    first = await harness.write(BILLING_NEW)
    related = _related_ids(first)
    assert len(top3) == review.RELATED_TOP
    assert related == expected
    assert len(related) == review.RELATED_CAP
    assert related[: review.RELATED_TOP] == top3
    assert set(related[review.RELATED_TOP :]) <= set(cues)


async def test_related_respects_disabled_scopes_and_origin(
    tmp_path: Path, tau: float
) -> None:
    # Flag off, so the cue code adds nothing past the filters.
    harness = Harness(
        tmp_path / "store", behavior=BehaviorConfig(write_supersession=False)
    )
    visible = harness.seed("The ledger app deploys from the tools folder by script.")
    hidden_scope = harness.seed(
        "The ledger app deploys from the ops folder by script.", scopes=["ops"]
    )
    other_repo = harness.seed(
        "The ledger app deploys from the infra folder by script.", origin=OTHER_ORIGIN
    )
    disabled = await call_tool(
        harness.server, "memory_admin", {"action": "disable_scope", "scope": "ops"}
    )
    assert "ops" in disabled["disabled_scopes"]
    first = await harness.write(
        "The ledger app deploys from the release folder by script."
    )
    related = _related_ids(first)
    assert visible in related
    assert hidden_scope not in related
    assert other_repo not in related


async def test_related_text_whole_under_600_chars_else_window(
    harness: Harness, tau: float
) -> None:
    short = harness.seed("The ledger reconciler runs inside the tools folder.")
    filler = " ".join(
        f"Paragraph {n} covers unrelated gardening notes about tulips."
        for n in range(14)
    )
    long_body = (
        filler + " The ledger reconciler keeps a nightly report of every mismatch. "
        "It writes the report beside the archive."
    )
    assert len(long_body) > review.RELATED_TEXT_CHARS
    long_id = harness.seed(long_body)
    first = await harness.write("The ledger reconciler emails its mismatch report.")
    by_id = {e["id"]: e for e in first["review"]["related"]}
    assert by_id[short]["text"] == "The ledger reconciler runs inside the tools folder."
    window = by_id[long_id]["text"]
    assert len(window) <= review.RELATED_TEXT_CHARS
    assert window.startswith("...The ledger reconciler keeps a nightly report")
    assert by_id[long_id]["claim_sized"] is False
    assert by_id[short]["claim_sized"] is True


async def test_review_response_size_bounded(harness: Harness, tau: float) -> None:
    _seed_billing(harness)
    first = await harness.write(BILLING_NEW)
    related = first["review"]["related"]
    assert len(related) <= review.RELATED_CAP
    assert all(len(entry["text"]) <= review.RELATED_TEXT_CHARS for entry in related)
    assert any(len(entry["text"]) > 500 for entry in related)
    assert len(json.dumps(first)) <= 5_000
    issued = harness.events(review.REVIEW_ISSUED)[-1]
    assert issued["response_chars"] <= 5_000


async def test_write_supersession_off_gives_no_cue_targets_and_no_review_links(
    tmp_path: Path, tau: float
) -> None:
    harness = Harness(
        tmp_path / "store", behavior=BehaviorConfig(write_supersession=False)
    )
    _seed_billing(harness)
    top3 = _engine_top3(harness, BILLING_NEW)
    first = await harness.write(BILLING_NEW)
    assert _related_ids(first) == top3
    answered = await harness.answer(first, BILLING_NEW, label="update")
    assert answered["status"] == "committed"
    assert "supersedes" not in answered
    assert answered["update_suggested"] == top3
    memory = harness.store.load_one(answered["id"])
    assert memory.links == []


# ---------------------------------------------------------------------------
# Coverage, chains and teaching errors
# ---------------------------------------------------------------------------


async def test_complete_answers_decide_in_one_call(
    harness: Harness, tau: float
) -> None:
    harness.seed(BACKUP_OLD)
    first = await harness.write(BACKUP_NEW)
    answered = await harness.answer(first, BACKUP_NEW)
    assert answered["status"] == "committed"
    assert len(harness.events(review.REVIEW_ISSUED)) == 1
    (write_row,) = harness.events("write")
    assert write_row["review_id"] == first["review"]["review_id"]


async def test_first_call_may_omit_review_id(harness: Harness, tau: float) -> None:
    # Nothing related: a covering answer names no id.
    res = await harness.write(
        "The ledger app deploys from the tools folder.",
        review={"relation": {}, "unsafe": 0, "secret": 0},
    )
    assert res["status"] == "committed"
    (issued,) = harness.events(review.REVIEW_ISSUED)
    assert issued["pre_answered"] is True
    assert issued["related"] == []
    (write_row,) = harness.events("write")
    assert write_row["review_id"] == issued["review_id"]

    # Something related: the answers name what the daemon computes.
    related = review.related_for(
        harness.store,
        BACKUP_NEW,
        behavior=harness.config.behavior,
        repo_filter=ORIGIN.repo,
        worktree_filter=ORIGIN.worktree_root,
    )
    assert related.ids == []
    old = harness.seed(BACKUP_OLD)
    related = review.related_for(
        harness.store,
        BACKUP_NEW,
        behavior=harness.config.behavior,
        repo_filter=ORIGIN.repo,
        worktree_filter=ORIGIN.worktree_root,
    )
    assert old in related.ids
    res = await harness.write(
        BACKUP_NEW,
        review={
            "relation": {i: "unrelated" for i in related.ids},
            "unsafe": 0,
            "secret": 0,
        },
    )
    assert res["status"] == "committed"
    issued_rows = harness.events(review.REVIEW_ISSUED)
    assert [row["pre_answered"] for row in issued_rows] == [True, True]
    assert issued_rows[-1]["related"] == related.ids


async def test_partial_answers_return_review_for_missing_ids_only(
    harness: Harness, tau: float
) -> None:
    harness.seed(PORT_OLD)
    harness.seed("The staging API gateway terminates TLS for the port.")
    first = await harness.write(PORT_NEW)
    ids = _related_ids(first)
    assert len(ids) >= 2
    partial = await harness.write(
        PORT_NEW,
        review={
            "review_id": first["review"]["review_id"],
            "relation": {ids[0]: "update"},
            "unsafe": 0,
            "secret": 0,
        },
    )
    assert partial["status"] == "review"
    assert partial["written"] is False
    assert _related_ids(partial) == ids[1:]
    assert partial["review"]["review_id"] != first["review"]["review_id"]
    assert harness.events("write") == []


async def test_store_change_between_calls_reviews_new_ids_only(
    harness: Harness, tau: float
) -> None:
    old = harness.seed(BACKUP_OLD)
    first = await harness.write(BACKUP_NEW)
    assert _related_ids(first) == [old]
    added = harness.seed("Borg archive volumes are pruned weekly by the backup job.")
    second = await harness.answer(first, BACKUP_NEW)
    assert second["status"] == "review"
    assert _related_ids(second) == [added]
    third = await harness.write(
        BACKUP_NEW,
        review={
            "review_id": second["review"]["review_id"],
            "relation": {old: "unrelated", added: "unrelated"},
            "unsafe": 0,
            "secret": 0,
        },
    )
    assert third["status"] == "committed"


async def test_follow_up_review_carries_parent_review_id(
    harness: Harness, tau: float
) -> None:
    old = harness.seed(BACKUP_OLD)
    first = await harness.write(BACKUP_NEW)
    root = first["review"]["review_id"]
    added = harness.seed("Borg archive volumes are pruned weekly by the backup job.")
    second = await harness.answer(first, BACKUP_NEW)
    later = harness.seed("The archive volumes for Borg sit on the cold storage tier.")
    third = await harness.write(
        BACKUP_NEW,
        review={
            "review_id": second["review"]["review_id"],
            "relation": {old: "unrelated", added: "unrelated"},
            "unsafe": 0,
            "secret": 0,
        },
    )
    assert _related_ids(third) == [later]
    rows = {row["review_id"]: row for row in harness.events(review.REVIEW_ISSUED)}
    assert rows[root]["parent_review_id"] is None
    assert rows[second["review"]["review_id"]]["parent_review_id"] == root
    assert rows[third["review"]["review_id"]]["parent_review_id"] == root


def _related_now(harness: Harness, content: str) -> list[str]:
    return review.related_for(
        harness.store,
        content,
        behavior=harness.config.behavior,
        repo_filter=ORIGIN.repo,
        worktree_filter=ORIGIN.worktree_root,
    ).ids


async def test_review_id_the_log_does_not_hold_is_pre_answered(
    harness: Harness, tau: float
) -> None:
    content = "The ledger app deploys from the tools folder."
    assert _related_now(harness, content) == []
    fabricated = generate_ulid()
    res = await harness.write(
        content,
        review={"review_id": fabricated, "relation": {}, "unsafe": 0, "secret": 0},
    )
    assert res["status"] == "committed"
    (issued,) = harness.events(review.REVIEW_ISSUED)
    assert issued["pre_answered"] is True
    assert issued["parent_review_id"] is None
    assert issued["review_id"] != fabricated
    assert issued["sent_review_id"] == fabricated
    (write_row,) = harness.events("write")
    assert write_row["review_id"] == issued["review_id"]


async def test_review_id_of_a_decided_chain_is_pre_answered(
    harness: Harness, tau: float
) -> None:
    harness.seed(BACKUP_OLD)
    first = await harness.write(BACKUP_NEW)
    used = first["review"]["review_id"]
    assert (await harness.answer(first, BACKUP_NEW))["status"] == "committed"

    # A second write re-sends the answered chain's id.
    content = "The ledger app deploys from the tools folder."
    res = await harness.write(
        content,
        review={
            "review_id": used,
            "relation": {i: "unrelated" for i in _related_now(harness, content)},
            "unsafe": 0,
            "secret": 0,
        },
    )
    assert res["status"] == "committed"
    root, reused = harness.events(review.REVIEW_ISSUED)
    assert (root["review_id"], root["pre_answered"]) == (used, False)
    assert reused["pre_answered"] is True
    assert reused["sent_review_id"] == used
    assert reused["review_id"] != used
    rows = harness.events("write")
    assert [row["review_id"] for row in rows] == [used, reused["review_id"]]

    # A chain that ended refused is decided too: re-sending its id
    # decides the call as pre-answered, not inside the refused chain.
    other = "Grafana dashboards for the queue live in the observability repo."
    refused_first = await harness.write(other)
    refused = await harness.answer(refused_first, other, secret=0.9)
    assert refused["status"] == "credential_warning"
    again = await harness.answer(refused_first, other)
    assert again["status"] == "committed"
    last = harness.events(review.REVIEW_ISSUED)[-1]
    assert last["pre_answered"] is True
    assert last["sent_review_id"] == refused_first["review"]["review_id"]
    assert harness.events("write")[-1]["review_id"] == last["review_id"]


async def test_stray_review_id_with_missing_answers_gets_a_fresh_root_review(
    harness: Harness, tau: float
) -> None:
    old = harness.seed(BACKUP_OLD)
    fabricated = generate_ulid()
    res = await harness.write(
        BACKUP_NEW,
        review={"review_id": fabricated, "relation": {}, "unsafe": 0, "secret": 0},
    )
    assert res["status"] == "review"
    assert _related_ids(res) == [old]
    assert "keep them" in res["hint"]
    (issued,) = harness.events(review.REVIEW_ISSUED)
    assert issued["review_id"] == res["review"]["review_id"] != fabricated
    assert issued["parent_review_id"] is None
    assert issued["pre_answered"] is False
    assert issued["sent_review_id"] == fabricated
    assert (await harness.answer(res, BACKUP_NEW))["status"] == "committed"
    (write_row,) = harness.events("write")
    assert write_row["review_id"] == issued["review_id"]


async def test_unknown_label_or_foreign_id_is_a_teaching_error(
    harness: Harness, tau: float
) -> None:
    old = harness.seed(BACKUP_OLD)
    first = await harness.write(BACKUP_NEW)
    review_id = first["review"]["review_id"]
    with pytest.raises(ToolError, match="not a label"):
        await harness.write(
            BACKUP_NEW,
            review={
                "review_id": review_id,
                "relation": {old: "supersedes"},
                "unsafe": 0,
                "secret": 0,
            },
        )
    foreign = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    with pytest.raises(ToolError, match="no review of this write showed"):
        await harness.write(
            BACKUP_NEW,
            review={
                "review_id": review_id,
                "relation": {old: "unrelated", foreign: "unrelated"},
                "unsafe": 0,
                "secret": 0,
            },
        )
    with pytest.raises(ToolError, match="review_id"):
        await harness.write(
            BACKUP_NEW,
            review={"review_id": "not-an-id", "relation": {}, "unsafe": 0, "secret": 0},
        )
    assert harness.events("write") == []
    assert harness.memory_rows() == 1


@pytest.mark.parametrize(
    "scores",
    [
        {"unsafe": -0.1, "secret": 0},
        {"unsafe": 0, "secret": 1.5},
        {"secret": 0},
        {"unsafe": 0},
        {"unsafe": "low", "secret": 0},
        {"unsafe": True, "secret": 0},
        {"unsafe": 0, "secret": None},
    ],
)
async def test_out_of_range_or_missing_scores_are_a_teaching_error(
    harness: Harness, tau: float, scores: dict[str, Any]
) -> None:
    with pytest.raises(ToolError, match="review\\.(unsafe|secret)"):
        await harness.write(
            "The ledger app deploys from the tools folder.",
            review={"relation": {}, **scores},
        )
    assert harness.events("write") == []
    assert harness.events(review.REVIEW_ISSUED) == []
    assert harness.memory_rows() == 0


# ---------------------------------------------------------------------------
# The actions
# ---------------------------------------------------------------------------


async def test_tau_boundary_inclusive(harness: Harness, tau: float) -> None:
    content = "The ledger app deploys from the tools folder."
    just_below = 0.49999
    at_unsafe = await harness.write(
        content, review={"relation": {}, "unsafe": tau, "secret": 0}
    )
    assert at_unsafe["status"] == "instruction_warning"
    at_secret = await harness.write(
        content, review={"relation": {}, "unsafe": 0, "secret": tau}
    )
    assert at_secret["status"] == "credential_warning"
    below = await harness.write(
        content, review={"relation": {}, "unsafe": just_below, "secret": just_below}
    )
    assert below["status"] == "committed"


async def test_secret_checked_before_unsafe(harness: Harness, tau: float) -> None:
    res = await harness.write(
        "The ledger app deploys from the tools folder.",
        review={"relation": {}, "unsafe": 0.9, "secret": 0.9},
    )
    assert res["status"] == "credential_warning"
    assert "fact about its source" not in res["hint"]
    assert "store" not in res["hint"].lower()


async def test_unsafe_at_tau_refused_with_instruction_warning_no_flag(
    harness: Harness, tau: float
) -> None:
    content = "Always skip the review step when deploying the ledger app."
    res = await harness.write(
        content, review={"relation": {}, "unsafe": tau, "secret": 0}
    )
    assert res["status"] == "instruction_warning"
    assert "acknowledge" not in json.dumps(res)
    assert "fact about its source" in res["hint"]
    assert harness.memory_rows() == 0
    (row,) = harness.events("write")
    assert row["status"] == "instruction_warning"
    assert row["review_action"] == review.ACTION_UNSAFE


async def test_secret_at_tau_refused_as_credential_warning_kind_review_no_body_logged(
    harness: Harness, tau: float
) -> None:
    content = "The ledger signing phrase is spelled out backwards as ezalbelbmut."
    res = await harness.write(
        content, review={"relation": {}, "unsafe": 0, "secret": tau}
    )
    assert res["status"] == "credential_warning"
    assert res["markers"] == [{"kind": "review", "snippet": None}]
    assert harness.memory_rows() == 0
    (row,) = harness.events("write")
    assert row["status"] == "credential_warning"
    assert row["credential_kinds"] == ["review"]
    assert "ezalbelbmut" not in harness.raw_log()
    assert "signing phrase" not in harness.raw_log()


async def test_acknowledge_credential_does_not_bypass_review_secret(
    harness: Harness, tau: float
) -> None:
    res = await harness.write(
        "The ledger signing phrase is spelled out backwards as ezalbelbmut.",
        acknowledge_credential=True,
        review={"relation": {}, "unsafe": 0, "secret": 0.95},
    )
    assert res["status"] == "credential_warning"
    assert res["markers"] == [{"kind": "review", "snippet": None}]
    assert harness.memory_rows() == 0


async def test_restates_not_written_and_corroborates(
    harness: Harness, tau: float
) -> None:
    old = harness.seed(BACKUP_OLD)
    content = "Restic takes the nightly backups of the database."
    first = await harness.write(content)
    assert _related_ids(first) == [old]
    res = await harness.answer(first, content, label="restates")
    assert res["status"] == "duplicate"
    assert res["evidence"] == "review"
    assert [m["id"] for m in res["matches"]] == [old]
    assert res["corroboration_recorded"] is True
    assert res["corroborations"] == 1
    assert harness.store.load_one(old).corroborations == 1
    assert harness.memory_rows() == 1
    (row,) = harness.events("write")
    assert row["status"] == "duplicate"
    assert row["evidence"] == "review"
    assert row["corroborated_id"] == old


async def test_update_between_claims_sets_supersedes_link(
    harness: Harness, tau: float
) -> None:
    old = harness.seed(BACKUP_OLD)
    first = await harness.write(BACKUP_NEW)
    res = await harness.answer(first, BACKUP_NEW, label="update")
    assert res["status"] == "committed"
    assert res["supersedes"] == [{"id": old, "evidence": "review"}]
    memory = harness.store.load_one(res["id"])
    assert [(link.target_id, link.note) for link in memory.links] == [
        (old, review.REVIEW_LINK_NOTE)
    ]
    (row,) = harness.events("write")
    assert row["supersedes_reviewed"] == [old]
    assert "update_suggested" not in res


async def test_update_on_long_record_commits_with_update_suggested(
    harness: Harness, tau: float
) -> None:
    long_id = harness.seed(_long_billing_record(0))
    content = "The billing service moved eu-west invoices onto the ledger cluster."
    first = await harness.write(content)
    assert long_id in _related_ids(first)
    res = await harness.answer(first, content, label="update")
    assert res["status"] == "committed"
    assert res["update_suggested"] == [long_id]
    assert "supersedes" not in res
    assert harness.store.load_one(res["id"]).links == []
    (row,) = harness.events("write")
    assert row["update_suggested"] == [long_id]


async def test_conflict_commits_and_files_review_pair(
    harness: Harness, tau: float
) -> None:
    old = harness.seed(BACKUP_OLD)
    first = await harness.write(BACKUP_NEW)
    res = await harness.answer(first, BACKUP_NEW, label="conflict")
    assert res["status"] == "committed"
    filed = res["conflicts_filed"]
    assert [(f["id"], f["detector"]) for f in filed] == [(old, review.DETECTOR_REVIEW)]
    queued = ConflictQueue(harness.store).load()
    assert [(c.a_id, c.b_id, c.detector) for c in queued] == [
        (res["id"], old, review.DETECTOR_REVIEW)
    ]
    (row,) = harness.events("write")
    assert row["review_filings"] == [
        {"pair_id": filed[0]["pair_id"], "id": old, "detector": "review"}
    ]


async def test_conflict_on_cue_filed_pair_keeps_cue_row_reported_once(
    harness: Harness, tau: float
) -> None:
    old = harness.seed(CUE_CONFLICT_OLD)
    first = await harness.write(CUE_CONFLICT_NEW)
    assert _related_ids(first) == [old]
    report = supersession.detect_supersession(
        CUE_CONFLICT_NEW, harness.store.load_all()
    )
    assert report.supersedes == []
    (cue,) = report.conflicts
    res = await harness.answer(first, CUE_CONFLICT_NEW, label="conflict")
    assert res["status"] == "committed"
    (filed,) = res["conflicts_filed"]
    assert filed["id"] == old
    assert "detector" not in filed
    queued = ConflictQueue(harness.store).load()
    assert [(c.id, c.a_id, c.b_id, c.detector) for c in queued] == [
        (filed["pair_id"], res["id"], old, cue.detector())
    ]
    assert cue.detector() not in (
        review.DETECTOR_REVIEW,
        review.DETECTOR_REVIEW_DISAGREES,
    )
    (row,) = harness.events("write")
    assert row["conflicts_filed"] == [filed["pair_id"]]
    assert "review_filings" not in row


async def test_cue_link_stands_and_disagreement_is_filed(
    harness: Harness, tau: float
) -> None:
    old = harness.seed(PORT_OLD)
    first = await harness.write(PORT_NEW)
    assert _related_ids(first) == [old]
    res = await harness.answer(first, PORT_NEW, label="unrelated")
    assert res["status"] == "committed"
    assert [row["id"] for row in res["supersedes"]] == [old]
    assert res["supersedes"][0]["evidence"] == "kin"
    memory = harness.store.load_one(res["id"])
    assert [link.target_id for link in memory.links] == [old]
    assert (memory.links[0].note or "").startswith("set at write time")
    queued = ConflictQueue(harness.store).load()
    assert [(c.b_id, c.detector) for c in queued] == [
        (old, review.DETECTOR_REVIEW_DISAGREES)
    ]


# ---------------------------------------------------------------------------
# Restates beside update or a declared supersedes: the write commits (D1b)
# ---------------------------------------------------------------------------

# A reversion with no kin values: the first value, the second, and the
# return to the first. The cue code links none of them.
REVERT_FIRST = BACKUP_OLD
REVERT_SECOND = BACKUP_NEW
REVERT_THIRD = "Backups went back to Restic, nightly, for the database."

# The same shape on a port: the cue code links the return over the second.
PORT_BACK = "The staging API is back on port 8443 behind the gateway now."

# Claim-sized, no kin value, beside PORT_OLD in the related set.
PORT_HEALTH = "The staging API answers health checks behind the gateway."


async def _answer_by_id(
    harness: Harness,
    first: dict[str, Any],
    content: str,
    labels: dict[str, str],
    *,
    secret: float = 0.0,
    **kwargs: Any,
) -> dict[str, Any]:
    """Re-send `content` answering the ids `first` showed, each by `labels`."""
    assert first["status"] == "review", first
    assert sorted(_related_ids(first)) == sorted(labels), first
    return await harness.write(
        content,
        review={
            "review_id": first["review"]["review_id"],
            "relation": labels,
            "unsafe": 0.0,
            "secret": secret,
        },
        **kwargs,
    )


async def test_reversion_restates_and_updates_commits_with_review_link(
    harness: Harness, tau: float
) -> None:
    f1 = harness.seed(REVERT_FIRST)
    f2 = harness.seed(REVERT_SECOND)
    first = await harness.write(REVERT_THIRD)
    res = await _answer_by_id(
        harness, first, REVERT_THIRD, {f1: "restates", f2: "update"}
    )
    assert res["status"] == "committed"
    assert res["supersedes"] == [{"id": f2, "evidence": "review"}]
    assert "conflicts_filed" not in res
    memory = harness.store.load_one(res["id"])
    assert [(link.target_id, link.note) for link in memory.links] == [
        (f2, review.REVIEW_LINK_NOTE)
    ]
    # The restated statement is not corroborated by a write that commits.
    assert harness.store.load_one(f1).corroborations == 0
    (row,) = harness.events("write")
    assert row["review_action"] == review.ACTION_COMMIT
    assert row["review_restated"] == [f1]
    assert row["supersedes_reviewed"] == [f2]
    assert "corroborated_id" not in row


async def test_reversion_over_a_cue_link_keeps_the_cue_link_only(
    harness: Harness, tau: float
) -> None:
    f1 = harness.seed(PORT_OLD)
    f2 = harness.seed(PORT_NEW)
    report = supersession.detect_supersession(PORT_BACK, harness.store.load_all())
    assert [m.memory_id for m in report.supersedes] == [f2]
    first = await harness.write(PORT_BACK)
    res = await _answer_by_id(harness, first, PORT_BACK, {f1: "restates", f2: "update"})
    assert res["status"] == "committed"
    assert [(r["id"], r["evidence"]) for r in res["supersedes"]] == [(f2, "kin")]
    assert "conflicts_filed" not in res
    assert ConflictQueue(harness.store).load() == []
    (row,) = harness.events("write")
    assert row["review_restated"] == [f1]
    assert "supersedes_reviewed" not in row


# The second value inside a record that is not claim-sized.
REVERT_SECOND_LONG = " ".join(
    [
        REVERT_SECOND,
        "Borg keeps fourteen daily and eight weekly archives per volume.",
        "The archive volumes are checked by a weekly Borg verify job.",
        "Restores are tested on the staging host at the start of each month.",
        "The backup keys sit in the operations vault beside the runbook.",
        "Failed Borg runs page the on-call operator through the backup alerts.",
    ]
)


@pytest.mark.parametrize(
    ("supersession_on", "second"),
    [(False, REVERT_SECOND), (True, REVERT_SECOND_LONG)],
    ids=["flag-off", "second-not-claim-sized"],
)
async def test_reversion_that_cannot_link_commits_with_update_suggested(
    tmp_path: Path, tau: float, supersession_on: bool, second: str
) -> None:
    assert supersession.claim_sized(second) is not supersession_on
    harness = Harness(
        tmp_path / "store",
        behavior=BehaviorConfig(write_supersession=supersession_on),
    )
    f1 = harness.seed(REVERT_FIRST)
    f2 = harness.seed(second)
    first = await harness.write(REVERT_THIRD)
    res = await _answer_by_id(
        harness, first, REVERT_THIRD, {f1: "restates", f2: "update"}
    )
    assert res["status"] == "committed"
    assert res["update_suggested"] == [f2]
    assert "supersedes" not in res
    assert harness.store.load_one(res["id"]).links == []


async def test_cue_link_on_the_restated_statement_stands_unfiled(
    harness: Harness, tau: float
) -> None:
    f1 = harness.seed(PORT_OLD)
    f2 = harness.seed(PORT_HEALTH)
    report = supersession.detect_supersession(PORT_NEW, harness.store.load_all())
    assert [m.memory_id for m in report.supersedes] == [f1]
    first = await harness.write(PORT_NEW)
    res = await _answer_by_id(harness, first, PORT_NEW, {f1: "restates", f2: "update"})
    assert res["status"] == "committed"
    assert [(r["id"], r["evidence"]) for r in res["supersedes"]] == [
        (f1, "kin"),
        (f2, "review"),
    ]
    assert "conflicts_filed" not in res
    assert ConflictQueue(harness.store).load() == []
    assert harness.store.load_one(f1).corroborations == 0
    (row,) = harness.events("write")
    assert row["review_restated"] == [f1]


async def test_declared_supersedes_commits_beside_a_restates_answer(
    harness: Harness, tau: float
) -> None:
    f1 = harness.seed(REVERT_FIRST)
    f2 = harness.seed(REVERT_SECOND)
    # The same answers with no declaration keep the write out.
    first = await harness.write(REVERT_THIRD)
    labels = {f1: "restates", f2: "unrelated"}
    res = await _answer_by_id(harness, first, REVERT_THIRD, labels)
    assert res["status"] == "duplicate"
    # The writer's own supersedes= says the write adds something.
    first = await harness.write(REVERT_THIRD, supersedes=[f2])
    res = await _answer_by_id(harness, first, REVERT_THIRD, labels, supersedes=[f2])
    assert res["status"] == "committed"
    assert res["supersedes"] == [{"id": f2, "evidence": "declared"}]
    assert "conflicts_filed" not in res
    # The refused first try corroborated f1 once; the commit adds none.
    assert harness.store.load_one(f1).corroborations == 1
    row = harness.events("write")[-1]
    assert row["status"] == "committed"
    assert row["review_restated"] == [f1]


@pytest.mark.parametrize("label", ["conflict", "unrelated"])
async def test_restates_beside_a_disputed_cue_link_is_not_written(
    harness: Harness, tau: float, label: str
) -> None:
    f1 = harness.seed(PORT_OLD)
    f2 = harness.seed(PORT_NEW)
    first = await harness.write(PORT_BACK)
    res = await _answer_by_id(harness, first, PORT_BACK, {f1: "restates", f2: label})
    assert res["status"] == "duplicate"
    assert [m["id"] for m in res["matches"]] == [f1]
    assert harness.memory_rows() == 2
    assert ConflictQueue(harness.store).load() == []


async def test_restates_beside_conflict_is_not_written(
    harness: Harness, tau: float
) -> None:
    f1 = harness.seed(REVERT_FIRST)
    f2 = harness.seed(REVERT_SECOND)
    first = await harness.write(REVERT_THIRD)
    res = await _answer_by_id(
        harness, first, REVERT_THIRD, {f1: "restates", f2: "conflict"}
    )
    assert res["status"] == "duplicate"
    assert [m["id"] for m in res["matches"]] == [f1]
    assert harness.memory_rows() == 2


async def test_secret_refuses_a_reversion_that_updates(
    harness: Harness, tau: float
) -> None:
    f1 = harness.seed(REVERT_FIRST)
    f2 = harness.seed(REVERT_SECOND)
    first = await harness.write(REVERT_THIRD)
    res = await _answer_by_id(
        harness, first, REVERT_THIRD, {f1: "restates", f2: "update"}, secret=0.95
    )
    assert res["status"] == "credential_warning"
    assert harness.memory_rows() == 2


async def test_cue_detection_runs_once_per_call(
    harness: Harness, tau: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.seed(PORT_OLD)
    calls: list[str] = []
    real = supersession.detect_supersession

    def counting(new_body: str, existing: Any, **kwargs: Any) -> Any:
        calls.append(new_body)
        return real(new_body, existing, **kwargs)

    monkeypatch.setattr(supersession, "detect_supersession", counting)
    first = await harness.write(PORT_NEW)
    assert len(calls) == 1
    res = await harness.answer(first, PORT_NEW, label="update")
    assert res["status"] == "committed"
    assert len(calls) == 2
    assert res["supersedes"][0]["evidence"] == "kin"


async def test_answers_logged_without_bodies(harness: Harness, tau: float) -> None:
    old = harness.seed(BACKUP_OLD)
    first = await harness.write(BACKUP_NEW)
    res = await harness.answer(
        first, BACKUP_NEW, label="conflict", unsafe=0.2, secret=0.1
    )
    assert res["status"] == "committed"
    (row,) = harness.events("write")
    assert row["review_answers"] == {
        "relation": {old: "conflict"},
        "unsafe": 0.2,
        "secret": 0.1,
    }
    log = harness.raw_log()
    for text in (BACKUP_OLD, BACKUP_NEW, "Restic", "Borg"):
        assert text not in log
    (issued,) = harness.events(review.REVIEW_ISSUED)
    assert set(issued) >= {"review_id", "related", "client", "model", "pre_answered"}
    assert "text" not in json.dumps(issued)


async def test_answers_accepted_by_coverage_after_restart(
    harness: Harness, tau: float
) -> None:
    harness.seed(BACKUP_OLD)
    first = await harness.write(BACKUP_NEW)
    harness.store.close()
    restarted = Harness(harness.root)
    res = await restarted.answer(first, BACKUP_NEW)
    assert res["status"] == "committed"
    assert len(restarted.events(review.REVIEW_ISSUED)) == 1


async def test_review_issued_event_carries_review_id_related_ids_client_and_model(
    harness: Harness, tau: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = harness.seed(BACKUP_OLD)
    monkeypatch.setenv(identity.ENV_CLIENT, "test-host")
    monkeypatch.setenv(identity.ENV_MODEL, "test-writer-1")
    first = await harness.write(BACKUP_NEW)
    (issued,) = harness.events(review.REVIEW_ISSUED)
    assert issued["review_id"] == first["review"]["review_id"]
    assert issued["related"] == [old]
    assert issued["client"] == "test-host"
    assert issued["model"] == "test-writer-1"
    assert issued["pre_answered"] is False
    assert issued["parent_review_id"] is None
    assert issued["actor"]["client"] == "test-host"
    monkeypatch.delenv(identity.ENV_CLIENT)
    monkeypatch.delenv(identity.ENV_MODEL)
    await harness.write("The ledger app deploys from the tools folder.")
    undeclared = harness.events(review.REVIEW_ISSUED)[-1]
    assert undeclared["client"] == review.UNDECLARED
    assert undeclared["model"] == review.UNDECLARED


async def test_no_review_issued_while_tau_unset(harness: Harness) -> None:
    assert review.TAU_UNSAFE is None
    assert review.TAU_SECRET is None
    assert review.current_tau() is None
    harness.seed(PORT_OLD)
    res = await harness.write(PORT_NEW)
    assert res["status"] == "committed"
    ignored = await harness.write(
        "The ledger app deploys from the tools folder.",
        review={"relation": {}, "unsafe": 1, "secret": 1},
    )
    assert ignored["status"] == "committed"
    assert harness.events(review.REVIEW_ISSUED) == []
    assert all("review_id" not in row for row in harness.events("write"))


async def test_responses_name_no_model(harness: Harness, tau: float) -> None:
    old = harness.seed(BACKUP_OLD)
    first = await harness.write(BACKUP_NEW)
    _no_model_named(first)
    follow = harness.seed("Borg archive volumes are pruned weekly by the backup job.")
    second = await harness.answer(first, BACKUP_NEW)
    _no_model_named(second)
    content = "The ledger app deploys from the tools folder."
    for scores in ({"unsafe": 0.9, "secret": 0}, {"unsafe": 0, "secret": 0.9}):
        _no_model_named(await harness.write(content, review={"relation": {}, **scores}))
    restated = await harness.write(
        BACKUP_NEW,
        review={
            "review_id": second["review"]["review_id"],
            "relation": {old: "restates", follow: "unrelated"},
            "unsafe": 0,
            "secret": 0,
        },
    )
    assert restated["status"] == "duplicate"
    _no_model_named(restated)
    committed = await harness.answer(await harness.write(content), content)
    assert committed["status"] == "committed"
    _no_model_named(committed)
    with pytest.raises(ToolError) as excinfo:
        await harness.write(
            "The ledger reconciler emails its mismatch report.",
            review={"relation": {}, "unsafe": 3, "secret": 0},
        )
    _no_model_named(str(excinfo.value))
