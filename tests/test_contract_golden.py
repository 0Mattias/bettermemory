"""The contract golden files (tests/contract/golden) match the Python
reference byte for byte.

Each generator in `tests.contract.generate` runs in memory and its output
is compared with the committed files. A generator whose output reads the
Unicode database or the linked SQLite is pinned to the versions the files
were made under: under those it must match; under others a match passes
and a difference skips, naming the drift, since the files then describe
the pinned versions and not this interpreter's.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unicodedata
from pathlib import Path

import pytest

from tests.contract.kinds import call_sites, writable_kinds
from tests.contract.payloads import LEGACY_SHAPES
from tests.contract.generate import (
    GENERATORS,
    GOLDEN,
    SQLITE_GATED,
    SQLITE_VERSION,
    UNICODE_GATED,
    UNIDATA_VERSION,
)


@pytest.fixture(autouse=True)
def _scratch_under_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The generators' scratch directories (stores, repositories) live
    under the test's own directory, where the suite allows a location
    capture."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def _drift(name: str) -> list[str]:
    out = []
    if name in UNICODE_GATED and unicodedata.unidata_version != UNIDATA_VERSION:
        out.append(f"Unicode {unicodedata.unidata_version} (files: {UNIDATA_VERSION})")
    if name in SQLITE_GATED and sqlite3.sqlite_version != SQLITE_VERSION:
        out.append(f"SQLite {sqlite3.sqlite_version} (files: {SQLITE_VERSION})")
    return out


@pytest.mark.parametrize("name", sorted(GENERATORS))
def test_golden_files_match_the_reference(name: str) -> None:
    produced = GENERATORS[name]()
    assert produced, name
    drift = _drift(name)
    for filename, data in sorted(produced.items()):
        committed = (GOLDEN / filename).read_bytes()
        if data != committed and drift:
            pytest.skip(f"{filename} differs under {', '.join(drift)}")
        assert data == committed, f"{filename} no longer matches the reference"


def test_every_golden_file_has_a_generator() -> None:
    made = {f for generate in GENERATORS.values() for f in generate()}
    on_disk = {p.name for p in GOLDEN.iterdir() if p.is_file()}
    assert on_disk == made


def test_golden_files_stay_small() -> None:
    total = sum(p.stat().st_size for p in GOLDEN.iterdir() if p.is_file())
    assert total < 6 * 1024 * 1024


def _fixture_kinds() -> tuple[set[str], set[str]]:
    lines = (GOLDEN / "log_chain.jsonl").read_text(encoding="ascii").splitlines()
    header = json.loads(lines[0])
    rows = {json.loads(line)["row"]["kind"] for line in lines[1:] if '"row"' in line}
    return set(header["kinds"]) | {"store_created", "rekey"}, rows


def test_the_log_fixture_has_a_row_of_every_kind_the_code_writes() -> None:
    """A kind a call site or a table adds fails here until the log fixture
    is generated again with a row of it."""
    listed, rows = _fixture_kinds()
    missing = writable_kinds() - listed
    assert not missing, f"kinds the code writes that log_chain.jsonl lacks: {missing}"
    assert listed <= rows


def test_the_legacy_kinds_are_ones_the_code_no_longer_writes() -> None:
    assert not set(LEGACY_SHAPES) & writable_kinds()


def test_every_kind_site_is_read_or_forwarded_by_a_bounded_writer() -> None:
    """Each call that passes a kind it does not spell forwards one of the
    tables `kinds.table_kinds` bounds; a new forwarding site is listed
    here once it is known to forward only those."""
    _, forwarded = call_sites()
    sites = {
        entry.split(" ", 1)[0].rsplit(":", 1)[0] + " " + entry.split(" ", 1)[1]
        for entry in forwarded
    }
    assert sites <= _FORWARDING, sites - _FORWARDING


_FORWARDING = frozenset(
    {
        "events.py record_event(kind)",
        "handlers/_guard.py record(deps)",
        "handlers/standing_review.py record(deps)",
        "handlers/write.py record(deps)",
        "key_lifecycle.py _append(kind)",
        "log.py _append(kind)",
        "membership.py record_membership(event_kind)",
        "membership.py _event('checkout_moved' if moved else 'checkout_view_added')",
        "membership_migrate.py record_membership(kind)",
        "recall.py record([])",
        "recall.py record(ids)",
        "store.py _append(kind)",
        "store_events.py _append(kind)",
        "store_membership.py _mutate(f'{spec.kind}_put')",
        "store_membership.py _mutate(f'{spec.kind}_delete')",
        "store_membership.py _append(kind)",
    }
)
