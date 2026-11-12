"""`Store.events_of_kinds(..., naming=...)` and the search that reads by it.

A search annotates each hit with its rejections of the last thirty days.
The rows that can say so are the ``use`` rows of that window that name a
hit, a few among everything the store logged in a month. Two things are
held here: the narrowed read returns every row the full read returns that
names one of the strings, in the same order, and a search parses the rows
that name its hits and no others, so its cost does not follow the window's.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import store as store_module
from bettermemory import store_events
from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from .test_server_negative_outcomes import _call, _seed, _unwrap

SINCE = datetime.now(timezone.utc) - timedelta(days=1)


def _use(store: Store, ids: Any, **fields: Any) -> None:
    store.record_event("use", session="s", ids=ids, outcome="ignored", **fields)


def _named(event: dict[str, Any], wanted: set[str]) -> bool:
    ids = event.get("ids") or event.get("memory_ids") or []
    return bool(wanted & {i for i in ids if isinstance(i, str)})


def test_naming_keeps_every_row_that_names_one(store: Store) -> None:
    _use(store, ["A1", "B2"])
    store.record_event("search", session="s", query="A1")
    _use(store, ["C3"])
    store.record_event("use", session="s", memory_ids=["A1"], outcome="applied")
    _use(store, ["B2"], note="unlike A1")
    _use(store, ["D4"])

    every = list(store.events_of_kinds(("use",), since=SINCE))
    assert len(every) == 5
    for wanted in ({"A1"}, {"B2", "D4"}, {"C3"}, {"A1", "B2", "C3", "D4"}):
        narrowed = list(store.events_of_kinds(("use",), since=SINCE, naming=wanted))
        # In order, a part of the full read, and no row that names one left out.
        assert [e for e in every if e in narrowed] == narrowed
        assert [e for e in every if _named(e, wanted)] == [
            e for e in narrowed if _named(e, wanted)
        ]
    # The same characters in another field are kept: the reader decides.
    assert len(list(store.events_of_kinds(("use",), since=SINCE, naming=["A1"]))) == 3


def test_naming_nothing_reads_nothing_and_a_long_list_reads_the_kinds(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(store, ["A1"])
    _use(store, ["B2"])
    assert list(store.events_of_kinds(("use",), since=SINCE, naming=[])) == []
    assert list(store.events_of_kinds(("use",), since=SINCE, naming=["Z9"])) == []
    monkeypatch.setattr(store_events, "NAMING_BOUND", 1)
    assert (
        len(list(store.events_of_kinds(("use",), since=SINCE, naming=["Z8", "Z9"])))
        == 2
    )


def test_naming_spells_a_string_as_the_log_does(store: Store) -> None:
    odd = 'quoted "id" with a \\ and an é'
    _use(store, [odd])
    _use(store, ["plain"])
    found = list(store.events_of_kinds(("use",), since=SINCE, naming=[odd]))
    assert [event["ids"] for event in found] == [[odd]]


async def test_a_search_parses_only_the_rows_that_name_its_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config(storage=StorageConfig(directory=str(tmp_path / "store")))
    state = SessionState()
    with Store(cfg.resolved_directory()) as store:
        recorder = Recorder(store=store, session_id=state.session_id, enabled=True)
        server = build_server(config=cfg, store=store, state=state, recorder=recorder)
        memory_id = await _seed(server, "python package dependency records")
        before = _unwrap(await _call(server, "memory_search", query="python"))
        assert "recent_negative_outcomes" not in before[0]
        for index in range(150):
            store.record_event("search", session="other", query=f"q{index}")
            _use(store, [f"01OTHER{index:019d}"])
        await _call(
            server,
            "memory_record_use",
            memory_ids=[memory_id],
            outcome="ignored",
            note="not now",
        )

        parsed: list[str] = []
        real = store_module._event_from_columns

        def counting(ts: str, session: str | None, kind: str, payload: str) -> Any:
            parsed.append(kind)
            return real(ts, session, kind, payload)

        monkeypatch.setattr(store_module, "_event_from_columns", counting)
        hits = _unwrap(await _call(server, "memory_search", query="python"))
        hit = next(row for row in hits if row["id"] == memory_id)
        assert hit["recent_negative_outcomes"][0]["outcome"] == "ignored"
        assert hit["recent_negative_outcomes"][0]["note"] == "not now"
        # 300 telemetry rows stand in the window; the search parses the
        # one that names its hit and whatever else it reads of its own
        # session, not the window.
        assert len(parsed) < 20, len(parsed)
