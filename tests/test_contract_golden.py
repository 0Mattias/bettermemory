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
import warnings
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.contract._diff import _differing
from tests.contract._sandbox import Scrubber
from tests.contract.kinds import call_sites, writable_kinds
from tests.contract.payloads import LEGACY_SHAPES
from tests.contract.generate import (
    GENERATORS,
    GOLDEN,
    PYDANTIC_CORE_VERSION,
    PYDANTIC_GATED,
    PYTHON_GATED,
    PLATFORM,
    PLATFORM_GATED,
    PYTHON_VERSION,
    SQLITE_GATED,
    SQLITE_VERSION,
    UNICODE_GATED,
    UNIDATA_VERSION,
    platform_key,
    pydantic_core_version,
    python_version,
)

#: The tool-response fixture records the review the package ships
#: (`review.TAU_UNSAFE`, `TAU_SECRET`), which the suite turns off by default.
pytestmark = pytest.mark.shipped_tau


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
    if name in PYTHON_GATED and python_version() != PYTHON_VERSION:
        out.append(f"Python {python_version()} (files: {PYTHON_VERSION})")
    if name in PYDANTIC_GATED and pydantic_core_version() != PYDANTIC_CORE_VERSION:
        out.append(
            f"pydantic-core {pydantic_core_version()} (files: {PYDANTIC_CORE_VERSION})"
        )
    if name in PLATFORM_GATED and platform_key() != PLATFORM:
        out.append(f"platform {platform_key()} (files: {PLATFORM})")
    return out


@pytest.mark.parametrize("name", sorted(GENERATORS))
def test_golden_files_match_the_reference(name: str) -> None:
    drift = _drift(name)
    try:
        produced = GENERATORS[name]()
    except Exception as exc:
        # Under the pinned versions a generator that raises is a failure.
        # Under others it has met something those versions do otherwise,
        # which is the difference the gate exists to skip.
        if not drift:
            raise
        reason = f"{name} raised {type(exc).__name__}: {exc} under {', '.join(drift)}"
        warnings.warn(reason, stacklevel=1)
        pytest.skip(reason)
    assert produced, name
    for filename, data in sorted(produced.items()):
        committed = (GOLDEN / filename).read_bytes()
        if data != committed and drift:
            where = _differing(data, committed)
            # A warning as well as the skip reason: a quiet run prints the
            # warnings summary and not the reasons, and which records
            # another version changes is worth reading in a CI log.
            warnings.warn(
                f"{filename} differs under {', '.join(drift)}: {where}", stacklevel=1
            )
            pytest.skip(f"{filename} differs under {', '.join(drift)}: {where}")
        assert data == committed, (
            f"{filename} no longer matches the reference: {_differing(data, committed)}"
        )


def test_every_golden_file_has_a_generator() -> None:
    made: set[str] = set()
    unknown = False
    for name, generate in GENERATORS.items():
        try:
            made |= set(generate())
        except Exception:
            # A gated generator that raises under another version than its
            # files' names no files here; under the pinned ones it fails.
            if not _drift(name):
                raise
            unknown = True
    on_disk = {p.name for p in GOLDEN.iterdir() if p.is_file()}
    if unknown:
        assert made <= on_disk
    else:
        assert on_disk == made


def test_no_golden_file_carries_a_host_fact() -> None:
    from tests.contract.gen_store_files import host_fact

    leaks = {
        path.name: leak
        for path in sorted(GOLDEN.iterdir())
        if path.is_file() and (leak := host_fact(path.read_bytes()))
    }
    assert leaks == {}


@pytest.mark.parametrize(
    ("data", "caught"),
    [
        (rb'"/tmp/a b"', False),
        (rb'"/tmp/a bc"', True),
        (rb'"/tmp/x y/zz"', True),
        (rb'"/home/fixture/other"', True),
        (rb'"C:\\Users\\example\\file.txt"', False),
        (rb'"C:\\Users\\example\\file.txt.bak"', True),
        (rb"C:\Users\x", True),
        (rb"\\?\C:\x", True),
        (rb"/Volumes/Data/x", True),
        (rb"D:/Users/x", True),
        (b"\x00/private/var\xff", True),
    ],
)
def test_the_host_fact_guard(data: bytes, caught: bool) -> None:
    from tests.contract.gen_store_files import host_fact

    assert (host_fact(data) is not None) is caught


def test_golden_files_stay_small() -> None:
    total = sum(p.stat().st_size for p in GOLDEN.iterdir() if p.is_file())
    # 5.6 MB at N2b: the stores search_store.jsonl rebuilds joined the set.
    assert total < 8 * 1024 * 1024


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


# ---------------------------------------------------------------------------
# The placeholders (`tests/contract/_sandbox.Scrubber`), on every platform's
# spelling of a scratch path, since each CI leg only meets its own.
# ---------------------------------------------------------------------------

_SCRUB_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("root", "text", "expected"),
    [
        (
            "/tmp/abc",
            "/tmp/abc/mem/store/memory.sqlite",
            "<scratch>/mem/store/memory.sqlite",
        ),
        (
            "/private/var/folders/x/T/abc",
            "cwd /private/var/folders/x/T/abc/alpha.",
            "cwd <scratch>/alpha.",
        ),
        (
            "C:\\Users\\RUNNER~1\\AppData\\Local\\Temp\\abc",
            "C:\\Users\\RUNNER~1\\AppData\\Local\\Temp\\abc\\mem\\store\\memory.sqlite",
            "<scratch>/mem/store/memory.sqlite",
        ),
        (
            "C:\\Users\\RUNNER~1\\AppData\\Local\\Temp\\abc",
            '{"cwd": "C:\\\\Users\\\\RUNNER~1\\\\AppData\\\\Local\\\\Temp\\\\abc\\\\alpha\\\\x.md"}',
            '{"cwd": "<scratch>/alpha/x.md"}',
        ),
        (
            "C:\\Users\\RUNNER~1\\AppData\\Local\\Temp\\abc",
            "C:/Users/RUNNER~1/AppData/Local/Temp/abc/alpha",
            "<scratch>/alpha",
        ),
    ],
)
def test_scrubber_writes_every_scratch_path_alike(
    root: str, text: str, expected: str
) -> None:
    assert Scrubber(root, now=_SCRUB_NOW).text(text) == expected


def test_scrubber_keeps_what_the_run_did_not_mint() -> None:
    scrub = Scrubber("/tmp/abc", now=_SCRUB_NOW)
    fixed = "01ARZ3NDEKTSV4RRFFQ69G5FAV 2016-07-30 2026-01-01T00:00:00Z"
    assert scrub.text(fixed) == fixed
    minted = "01KT1GW6G00000000000000000 2026-06-01 2026-06-02T03:04:05.6Z"
    assert scrub.text(minted) == "<ulid:1> <date> <timestamp>"
    assert scrub.text("8081851b1353b84897f648a4c366460539280d68") == (
        "8081851b1353b84897f648a4c366460539280d68"
    )
