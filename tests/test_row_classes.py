"""The two classes of row the event log holds (ENG-108).

`events.STATE_FIELDS` is the one table of the state class: every kind a
product module reads back, each with the fields its readers read. A kind
it does not name is measurement (`events.MEASUREMENT_KINDS`: opt-out and
prunable). The writer (`Recorder.record`) and a pruning of the log
(`events.prunable`) read `events.row_class`, which reads that table.
Every kind the src records is listed under exactly one class, so a new
kind cannot land unclassified; every kind a product module reads is
state, so no reader depends on a row telemetry off drops; and every
recorder the src builds reads the switch.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

from bettermemory import events, review
from bettermemory.events import (
    MEASUREMENT_KINDS,
    ROW_KINDS,
    STATE_FIELDS,
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
    ("handlers/standing_review.py", "deps"): None,
}


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
    assert state == STATE_KINDS == frozenset(STATE_FIELDS)
    assert measurement == MEASUREMENT_KINDS
    # The audit trail is state.
    assert events.AUDIT_KINDS | events.OVERRIDE_EVIDENCE_KINDS <= state


# The product modules: what decides what the product does next, as the
# ENG-108 fold names them (recall, review, audit, health, membership,
# session, conflicts and episode modules), and the other modules a scan of
# the src found reading the log back (the Stop hook, the shared tool
# plumbing, memory_acknowledge_miss, the search handler and response, the
# secret scan and the contradiction index). The reports among them (the
# recall and review counters, `bettermemory recall review`) read only state
# kinds today, so none is set aside. The reports over the whole log
# (`memory history`, eval, the log and audit exports) show whatever rows it
# holds and decide nothing, so they are not here: with telemetry off
# `memory history` shows a remove or a restore by its record rows alone.
_PRODUCT_MODULES: tuple[str, ...] = (
    "recall*.py",
    "review*.py",
    "audit*.py",
    "health*.py",
    "membership*.py",
    "session*.py",
    "conflicts*.py",
    "handlers/conflicts.py",
    "episode*.py",
    "handlers/episode*.py",
    "hook.py",
    "handlers/_shared.py",
    "handlers/acknowledge_miss.py",
    "handlers/search.py",
    "_response.py",
    "secret_scan.py",
    "store_index.py",
)

_SQL_KIND = re.compile(r"\bkind\s*=\s*'([a-z_]+)'")


def _strings(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [s for elt in node.elts for s in _strings(elt)]
    if isinstance(node, ast.Call) and getattr(node.func, "id", "") in (
        "frozenset",
        "set",
        "tuple",
    ):
        return [s for arg in node.args for s in _strings(arg)]
    return []


def _kinds_read(tree: ast.AST) -> set[str]:
    """The kind names a module reads rows by: a comparison with something
    named for a kind, a constant named for kinds, a `*events_of_kinds`
    call's kinds, a dispatch table of `_handle_*` methods, and the kind in
    a SQL text (`kind = 'verify'`)."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            sides = [node.left, *node.comparators]
            if any("kind" in ast.unparse(side) for side in sides):
                for side in sides:
                    out.update(_strings(side))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr.endswith("events_of_kinds"):
                for arg in node.args:
                    out.update(_strings(arg))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            named = any("KIND" in ast.unparse(t).upper() for t in targets)
            if named and node.value is not None:
                out.update(_strings(node.value))
        elif isinstance(node, ast.Dict):
            if node.values and all(
                isinstance(v, (ast.Name, ast.Attribute)) and "handle" in ast.unparse(v)
                for v in node.values
            ):
                for key in node.keys:
                    if key is not None:
                        out.update(_strings(key))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.update(_SQL_KIND.findall(node.value))
    return out


def _product_reads() -> dict[str, set[str]]:
    recorded = STATE_KINDS | MEASUREMENT_KINDS
    reads: dict[str, set[str]] = {}
    for pattern in _PRODUCT_MODULES:
        for path in sorted(SRC.glob(pattern)):
            rel = path.relative_to(SRC).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            reads[rel] = _kinds_read(tree) & recorded
    return reads


def test_every_kind_a_product_module_reads_is_state() -> None:
    """The cold review's M1 to M3 went through because a reader's kind was
    checked against a hand list: the Stop settled from `search` rows, the
    seen gate read `write` and `update`, the handoff read the journal's
    stamps and the membership migration read `verify`, all measurement,
    so telemetry off dropped what they read and a pruning could drop it at
    any age. Now a product module that reads a measurement kind fails
    here."""
    reads = _product_reads()
    # The scan sees what it must: one read of each shape it covers.
    assert "search" in reads["hook.py"]
    assert {"write", "update"} <= reads["recall.py"]
    assert "episode_write" in reads["handlers/episode_handoff.py"]
    assert "verify" in reads["membership_migrate.py"]
    assert "use" in reads["store_index.py"]
    assert "silent_miss_cutoff" in reads["health_stats.py"]
    measurement = {
        rel: sorted(kinds - STATE_KINDS)
        for rel, kinds in reads.items()
        if kinds - STATE_KINDS
    }
    assert measurement == {}, "product modules reading measurement kinds"


def test_every_recorder_the_src_builds_reads_the_switch() -> None:
    """The interrupted turn's recorder was built with `enabled=True`, so its
    `turn_audited` row kept the prompt's preview with telemetry off (H1).
    Every `Recorder(...)` in the src names `enabled`, and from the config's
    switch or a parameter that carries it."""
    loose: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if name != "Recorder":
                continue
            kw = {k.arg: ast.unparse(k.value) for k in node.keywords}
            enabled = kw.get("enabled")
            if enabled is None or not re.fullmatch(
                r"(\w+\.)*telemetry(\.enabled)?", enabled
            ):
                loose.append(f"{rel}:{node.lineno} enabled={enabled}")
    assert loose == []


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("write", RowClass.STATE),
        ("update", RowClass.STATE),
        ("search", RowClass.STATE),
        ("verify", RowClass.STATE),
        ("search_miss", RowClass.STATE),
        ("episode_write", RowClass.STATE),
        ("standing_guard", RowClass.STATE),
        ("show", RowClass.STATE),
        ("recall_held", RowClass.MEASUREMENT),
        ("remove", RowClass.MEASUREMENT),
        ("a_kind_nobody_listed", RowClass.MEASUREMENT),
    ],
)
def test_a_rows_class_reads_its_kind(kind: str, expected: RowClass) -> None:
    assert row_class(kind) is expected
    assert prunable(kind) is (expected is RowClass.MEASUREMENT)


def test_no_state_kind_is_prunable_and_every_measurement_kind_is() -> None:
    for kind in ROW_KINDS[RowClass.STATE]:
        assert not prunable(kind), kind
    for kind in MEASUREMENT_KINDS:
        assert prunable(kind), kind


def test_a_pruning_keeps_every_row_a_reader_reads(store: Store) -> None:
    """A pruning reads the class off the row's kind, so it keeps exactly
    the rows the writer recorded as state, a plain search and a plain
    write among them."""
    written: list[tuple[str, dict[str, Any]]] = [
        ("search", {"query": "a question", "returned": ["m1"]}),
        ("write", {"status": "committed", "id": "m1", "forced": False}),
        ("update", {"status": "updated", "id": "m1"}),
        ("verify", {"status": "verified", "id": "m1", "verified_head": "abc"}),
        ("recall_held", {"ids": ["m1"]}),
        ("show", {"id": "m1"}),
        ("remove", {"id": "m1", "reason": "stale"}),
        ("episode_write", {"id": "e1", "status": "written"}),
    ]
    recorder = Recorder(store=store, session_id="s1", enabled=True)
    for kind, fields in written:
        recorder.record(kind, **dict(fields))
    rows = [e for e in store.iter_events() if e["kind"] != "segment_opened"]
    assert [e["kind"] for e in rows] == [kind for kind, _ in written]
    kept = [e["kind"] for e in rows if not prunable(e["kind"])]
    assert kept == ["search", "write", "update", "verify", "show", "episode_write"]


def test_with_telemetry_off_the_writer_records_by_class(store: Store) -> None:
    recorder = Recorder(store=store, session_id="s1", enabled=False)
    for kind in sorted(MEASUREMENT_KINDS):
        assert not recorder.records(kind), kind
        recorder.record(kind)
    for kind in sorted(STATE_KINDS):
        assert recorder.records(kind), kind
    recorder.record("show", id="m1")
    recorder.record("search", query="q", returned=["m1"])
    recorder.record("write", status="committed", id="m2")
    assert [e["kind"] for e in store.iter_events()] == ["show", "search", "write"]
    with events.suppressed():
        assert not recorder.records("show")


def test_with_telemetry_on_the_writer_records_both_classes(store: Store) -> None:
    recorder = Recorder(store=store, session_id="s1", enabled=True)
    assert recorder.records("recall_held")
    recorder.record("recall_held", ids=[])
    recorder.record("show", id="m1")
    assert [e["kind"] for e in store.iter_events()] == ["recall_held", "show"]
