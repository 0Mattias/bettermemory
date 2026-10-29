"""The two classes of row the event log holds (ENG-108).

`events.RowClass` is the one place the classes are listed: state (what
the product reads back, and the audit trail) and measurement (opt-out
and prunable). The writer (`Recorder.record`), health's override rate
and a pruning of the log (`events.prunable`) read `events.row_class`
and nothing else. Every kind the src records is listed under exactly
one class, so a new kind cannot land unclassified.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from bettermemory import events, review
from bettermemory.events import (
    MEASUREMENT_KINDS,
    ROW_KINDS,
    STATE_KINDS,
    Recorder,
    RowClass,
    prunable,
    row_class,
)
from bettermemory.store import Store

SRC = Path(events.__file__).resolve().parent

# The recording sites whose kind is not a string literal, by file and
# expression: what each records, or None for a site that is not an event
# write (the Recorder's own call into the store, the standing guard's
# helper, which records `standing_guard` itself).
_NON_LITERAL_SITES: dict[tuple[str, str], str | None] = {
    ("events.py", "kind"): None,
    ("handlers/write.py", "_review.REVIEW_ISSUED"): review.REVIEW_ISSUED,
    ("handlers/write.py", "deps"): None,
}

_REVIEW_ID = "01M5Z0000000000000000000AA"


def _recorded_kinds() -> set[str]:
    """Every kind a `.record(...)` or `.record_event(...)` call in the src
    names; a non-literal kind must be one `_NON_LITERAL_SITES` knows."""
    kinds: set[str] = set()
    unknown: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("record", "record_event")
                and node.args
            ):
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                kinds.add(first.value)
                continue
            site = (rel, ast.unparse(first))
            if site not in _NON_LITERAL_SITES:
                unknown.append(f"{rel}:{node.lineno} {site[1]}")
                continue
            kind = _NON_LITERAL_SITES[site]
            if kind is not None:
                kinds.add(kind)
    assert not unknown, f"recording sites whose kind is not known: {unknown}"
    return kinds


def test_every_kind_the_src_records_is_listed_under_one_class() -> None:
    recorded = _recorded_kinds()
    state = ROW_KINDS[RowClass.STATE]
    measurement = ROW_KINDS[RowClass.MEASUREMENT]
    assert set(ROW_KINDS) == set(RowClass)
    assert not state & measurement
    assert recorded - (state | measurement) == set(), "a kind with no class"
    # No stale entry: a listed kind is one the src records.
    assert (state | measurement) - recorded == set()
    assert state == STATE_KINDS | events.AUDIT_KINDS
    assert measurement == MEASUREMENT_KINDS


@pytest.mark.parametrize(
    ("kind", "fields", "expected"),
    [
        ("write", {"status": "committed", "forced": False}, RowClass.MEASUREMENT),
        ("write", {"status": "committed", "forced": True}, RowClass.STATE),
        ("write", {"credentials_acknowledged": ["aws"]}, RowClass.STATE),
        ("write", {"credentials_acknowledged": []}, RowClass.MEASUREMENT),
        ("write", {"review_id": _REVIEW_ID}, RowClass.STATE),
        ("update", {"truncation_acknowledged": True}, RowClass.STATE),
        ("update", {"id": "x"}, RowClass.MEASUREMENT),
        ("search", {"query": "q"}, RowClass.MEASUREMENT),
        ("search", {"review_id": _REVIEW_ID}, RowClass.MEASUREMENT),
        ("standing_guard", {}, RowClass.STATE),
        ("show", {}, RowClass.STATE),
        ("a_kind_nobody_listed", {}, RowClass.MEASUREMENT),
    ],
)
def test_a_rows_class_reads_its_kind_and_its_evidence(
    kind: str, fields: dict[str, Any], expected: RowClass
) -> None:
    assert row_class(kind, fields) is expected
    assert prunable(kind, fields) is (expected is RowClass.MEASUREMENT)


def test_no_state_kind_is_prunable_and_every_plain_measurement_kind_is() -> None:
    for kind in ROW_KINDS[RowClass.STATE]:
        assert not prunable(kind, {}), kind
    for kind in MEASUREMENT_KINDS:
        assert prunable(kind, {}), kind


def test_a_pruning_reads_the_same_class_off_the_row_the_writer_wrote(
    store: Store,
) -> None:
    """The class is read on the fields, so a pruning that reads a row back
    from the log keeps exactly the rows the writer recorded as state."""
    written: list[tuple[str, dict[str, Any]]] = [
        ("search", {"query": "a question", "returned": []}),
        ("write", {"status": "committed", "id": "m1", "forced": False}),
        ("write", {"status": "committed", "id": "m2", "forced": True}),
        ("write", {"status": "committed", "id": "m3", "review_id": _REVIEW_ID}),
        ("update", {"status": "updated", "id": "m1", "markers_acknowledged": ["x"]}),
        ("show", {"id": "m1"}),
        ("use", {"ids": ["m1"], "outcome": "contradicted"}),
        ("turn_audited", {"returned": 0}),
        ("search_miss", {"probe_query": "another"}),
    ]
    recorder = Recorder(store=store, session_id="s1", enabled=True)
    for kind, fields in written:
        recorder.record(kind, **dict(fields))
    rows = [e for e in store.iter_events() if e["kind"] != "segment_opened"]
    assert [e["kind"] for e in rows] == [kind for kind, _ in written]
    for (kind, fields), row in zip(written, rows, strict=True):
        assert row_class(row["kind"], row) is row_class(kind, fields), kind
    kept = [(e["kind"], e.get("id")) for e in rows if not prunable(e["kind"], e)]
    assert kept == [
        ("write", "m2"),
        ("write", "m3"),
        ("update", "m1"),
        ("show", "m1"),
        ("use", None),
        ("turn_audited", None),
    ]


def test_with_telemetry_off_the_writer_records_by_class(store: Store) -> None:
    recorder = Recorder(store=store, session_id="s1", enabled=False)
    for kind in sorted(MEASUREMENT_KINDS):
        assert not recorder.records(kind), kind
        recorder.record(kind)
    assert recorder.records("write", {"forced": True})
    assert recorder.records("show")
    recorder.record("show", id="m1")
    recorder.record("write", status="committed", id="m2", forced=True)
    assert [e["kind"] for e in store.iter_events()] == ["show", "write"]
    with events.suppressed():
        assert not recorder.records("show")


def test_with_telemetry_on_the_writer_records_both_classes(store: Store) -> None:
    recorder = Recorder(store=store, session_id="s1", enabled=True)
    assert recorder.records("search")
    recorder.record("search", query="q", returned=[])
    recorder.record("show", id="m1")
    assert [e["kind"] for e in store.iter_events()] == ["search", "show"]
