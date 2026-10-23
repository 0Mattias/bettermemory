"""Review rows built through the real handlers, on a scratch store, at
times the test sets.

The eval counters and the samples command read `review_issued` and `write`
rows; these helpers make them the way a client does, by calling
`memory_write` on an in-process server, so a counter can never be tested
against a row shape the daemon does not write. `Clock` stands in for the
log's own timestamp source (`log._utcnow_iso`), so a test can put thirty
minutes between two calls without waiting.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _handlers, identity, review
from bettermemory import log as bm_log
from bettermemory import models
from bettermemory.admission import Admission
from bettermemory.builder import build_server
from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.handlers import write as write_handler
from bettermemory.location import resolve_caller
from bettermemory.membership import REVIEW_STRATA
from bettermemory.origin import Origin
from bettermemory.session import SessionState
from bettermemory.store import Store
from bettermemory.time_utils import isoformat_utc

from ._mcp import call_tool
from .test_membership_security import stand_in_checkout

TAU = 0.5

ORIGIN = Origin(
    cwd="/work/alpha",
    repo="https://example.test/alpha.git",
    branch="main",
    worktree_root="/work/alpha",
)

# A claim pair the cue code leaves alone, so what happens to it is the
# review's doing.
BACKUP_OLD = "Nightly database backups are handled by Restic."
BACKUP_NEW = "Backups moved to Borg for the archive volumes."
BACKUP_RESTATED = "Restic takes the nightly backups of the database."

# A second cue-free claim pair, for a review filing beside a review link.
STATUS_OLD = "The public status page is hosted on Statuspage."
STATUS_NEW = "The status page for customers is served by Instatus."

# A claim and its update by the cue code's own rule, so the cue code links.
PORT_OLD = "The staging API listens on port 8443 behind the gateway."
PORT_NEW = "The staging API now listens on port 9443 behind the gateway."

#: Distinct statements no gate refuses and no dedup pass folds together.
FACTS: tuple[str, ...] = (
    "The ledger app deploys from the tools folder.",
    "Grafana dashboards for the queue live in the observability repo.",
    "The mobile client reads feature flags from the remote config service.",
    "Terraform state for the data platform sits in a versioned bucket.",
    "The search indexer rebuilds its shards every Sunday at midnight.",
    "Invoice PDFs are rendered by a headless browser worker pool.",
    "The design system publishes its tokens as a separate npm package.",
    "Kafka topics for audit events keep seven days of retention.",
    "The onboarding wizard stores its drafts in browser local storage.",
    "Load tests for the checkout flow run from a dedicated runner.",
    "The translation pipeline pulls strings from the content repository.",
    "Postgres replicas for reporting lag behind the primary by design.",
    "The CLI release notes are generated from conventional commit titles.",
    "Image thumbnails are cached at the edge for one week.",
    "The scheduler service retries failed cron jobs three times.",
    "Webhook signatures use a per-tenant signing secret rotated yearly.",
    "The analytics warehouse loads clickstream data every hour.",
    "Customer exports are zipped and offered through a signed link.",
    "The chat widget loads lazily after the first user interaction.",
    "Docs previews are built for every pull request in the docs repo.",
    "The payroll integration syncs employee records each weekday morning.",
    "Error budgets for the API are reviewed at the monthly ops meeting.",
)


class Clock:
    """The time the log stamps on every row, moved by the test."""

    def __init__(self, base: datetime) -> None:
        self.now = base.astimezone(timezone.utc)

    def iso(self) -> str:
        return isoformat_utc(self.now)

    def advance(self, **delta: float) -> datetime:
        self.now += timedelta(**delta)
        return self.now

    def ulid(self) -> str:
        """A ULID minted at the clock's time, as the daemon mints a
        review_id at the time its row is stamped."""
        real = models.generate_ulid()
        ts_ms = int(self.now.timestamp() * 1000) & ((1 << 48) - 1)
        return models._encode_crockford(ts_ms, 10) + real[10:]


def install_clock(monkeypatch: pytest.MonkeyPatch, *, offset: timedelta) -> Clock:
    """A clock starting at the real time plus `offset`, installed as the
    log's timestamp source and as the time the write handler mints
    review_ids at, so the chain lookup, which reads back to a review_id's
    own time, finds the rows the test wrote. A negative offset puts a
    whole window in the past for the CLI, which judges as of the real
    now."""
    base = datetime.now(timezone.utc).replace(microsecond=0) + offset
    clock = Clock(base)
    monkeypatch.setattr(bm_log, "_utcnow_iso", clock.iso)
    monkeypatch.setattr(write_handler, "generate_ulid", clock.ulid)
    return clock


def fix_origin_and_identity(monkeypatch: pytest.MonkeyPatch, base: Path) -> Path:
    """Stand the process in a fresh checkout under `base` (a directory of
    its own) and make every write capture one origin there, and let no
    declared client or model leak in from the environment the suite runs
    in. The write is resolved at its origin's cwd (design 8.1) and its
    related set is the review strata of that checkout's project (design
    3.5), which a caller outside any project is admitted nothing of.
    Returns the checkout."""
    repo = stand_in_checkout(base, monkeypatch)
    here = Origin(
        cwd=str(repo), repo=ORIGIN.repo, branch=ORIGIN.branch, worktree_root=str(repo)
    )
    monkeypatch.setattr(_handlers, "capture_origin", lambda *a, **k: here)
    monkeypatch.delenv(identity.ENV_CLIENT, raising=False)
    monkeypatch.delenv(identity.ENV_MODEL, raising=False)
    return repo


def set_tau(monkeypatch: pytest.MonkeyPatch, value: float = TAU) -> float:
    monkeypatch.setattr(review, "TAU_UNSAFE", value)
    monkeypatch.setattr(review, "TAU_SECRET", value)
    return value


class Harness:
    """One in-process server over one scratch store, recorder on.

    The caller is the checkout the process stands in
    (`fix_origin_and_identity`), resolved once here so its first sight is
    behind the writes; a seeded record is homed in its project."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.monkeypatch = monkeypatch
        self.config = Config(
            storage=StorageConfig(directory=str(root)),
            behavior=BehaviorConfig(),
        )
        self.store = Store(root)
        self.home = resolve_caller(self.store, self.config, os.getcwd()).home()
        self.state = SessionState()
        self.server = build_server(
            config=self.config,
            store=self.store,
            state=self.state,
            recorder=Recorder(store=self.store, session_id=self.state.session_id),
        )

    def as_host(self, client: str | None, model: str | None = None) -> None:
        """Declare the client and model of the next calls (None: none)."""
        for env, value in ((identity.ENV_CLIENT, client), (identity.ENV_MODEL, model)):
            if value is None:
                self.monkeypatch.delenv(env, raising=False)
            else:
                self.monkeypatch.setenv(env, value)

    def seed(self, content: str) -> str:
        return self.store.write(
            content=content, scopes=["tools"], origin=ORIGIN, home=self.home
        ).id

    async def write(self, content: str, **kwargs: Any) -> dict[str, Any]:
        args: dict[str, Any] = {"content": content, "scopes": ["tools"], **kwargs}
        return await call_tool(self.server, "memory_write", args)

    async def answer(
        self,
        first: dict[str, Any],
        content: str,
        *,
        label: str = "unrelated",
        labels: dict[str, str] | None = None,
        unsafe: float = 0.0,
        secret: float = 0.0,
        standing: float = 0.0,
        only: list[str] | None = None,
    ) -> dict[str, Any]:
        """Re-send `content` answering the ids `first` showed (or `only`
        those) with `label`, overridden per id by `labels`."""
        assert first["status"] == "review", first
        shown = [entry["id"] for entry in first["review"]["related"]]
        ids = shown if only is None else only
        relation = {i: (labels or {}).get(i, label) for i in ids}
        return await self.write(
            content,
            review={
                "review_id": first["review"]["review_id"],
                "relation": relation,
                "unsafe": unsafe,
                "secret": secret,
                "standing": standing,
            },
        )

    def related_ids(self, content: str) -> list[str]:
        """The related set the daemon computes for `content` now: the
        REVIEW_STRATA admission of a caller resolved where the process
        stands."""
        caller = resolve_caller(self.store, self.config, os.getcwd())
        return review.related_for(
            self.store,
            content,
            behavior=self.config.behavior,
            admission=Admission.of(REVIEW_STRATA, caller),
        ).ids

    async def pre_answer(
        self,
        content: str,
        *,
        label: str = "unrelated",
        labels: dict[str, str] | None = None,
        unsafe: float = 0.0,
        secret: float = 0.0,
        standing: float = 0.0,
    ) -> dict[str, Any]:
        """Decide `content` in one call, without a review_id."""
        relation = {i: (labels or {}).get(i, label) for i in self.related_ids(content)}
        return await self.write(
            content,
            review={
                "relation": relation,
                "unsafe": unsafe,
                "secret": secret,
                "standing": standing,
            },
        )

    async def reviewed_commit(self, content: str, **kwargs: Any) -> dict[str, Any]:
        """A two-call round trip that commits."""
        first = await self.write(content)
        res = await self.answer(first, content, **kwargs)
        assert res["status"] == "committed", res
        return res

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.store.iter_events() if e.get("kind") == kind]
