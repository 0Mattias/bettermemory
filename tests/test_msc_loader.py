"""Pins on the MSC loader (`bench/msc/load.py`).

The corpus is NOT committed (no redistribution grant; see the module
docstring and `bench/THIRD_INSTRUMENT.md`), so these tests split along
that line: the pure construction functions are pinned unconditionally —
they are what a future census's determinism rests on — and anything
touching the data skips when the download is absent, which is every CI
run. The skip is the same posture `bench/longmemeval/data/` already
has: invisible to CI, reproducible for a holder of the pinned bytes.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest

_BENCH = Path(__file__).resolve().parents[1] / "bench"


def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _BENCH / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


msc = _load("bench_msc_load", "msc/load.py")

_HAVE_DATA = msc.TARBALL.exists()


# ---------------------------------------------------------------------------
# Pure construction — pinned unconditionally
# ---------------------------------------------------------------------------


def test_tarball_pin_is_a_sha256() -> None:
    """The pin is the whole provenance story for an uncommitted corpus."""
    assert re.fullmatch(r"[0-9a-f]{64}", msc.TARBALL_SHA256)


def test_rounds_pair_alternating_turns() -> None:
    rounds = msc._rounds(["a", "b", "c", "d"])
    assert rounds == ["Speaker 1: a\nSpeaker 2: b", "Speaker 1: c\nSpeaker 2: d"]


def test_rounds_keep_a_trailing_unpaired_turn() -> None:
    """Dropping the tail would remove evidence a question could target —
    the same rule the LongMemEval runner's `rounds_of` records."""
    assert msc._rounds(["a", "b", "c"])[-1] == "Speaker 1: c"


def test_date_format_matches_the_longmemeval_prefix_shape() -> None:
    """Store bodies must carry the same bracket-prefix shape the other
    conversational bench writes, so downstream tooling reads both."""
    text = msc._fmt(datetime(2023, 5, 20, 2, 21))
    assert text == "2023/05/20 (Sat) 02:21"
    assert re.fullmatch(r"\d{4}/\d{2}/\d{2} \(\w{3}\) \d{2}:\d{2}", text)


def test_gap_hours_covers_exactly_the_observed_units() -> None:
    assert msc._gap_hours({"time_num": 3, "time_unit": "days"}) == 72
    assert msc._gap_hours({"time_num": 1, "time_unit": "hour"}) == 1


def test_gap_hours_refuses_an_unknown_unit() -> None:
    """A new unit spelling must fail loudly, not guess — a silently
    misread gap would shift every synthetic date after it."""
    with pytest.raises(SystemExit):
        msc._gap_hours({"time_num": 2, "time_unit": "weeks"})


def test_epoch_is_fixed() -> None:
    """The anchor is part of every derived store's bytes; moving it is
    a corpus change and must show up here as a deliberate edit."""
    assert msc.EPOCH == datetime(2023, 5, 20, 10, 0)


# ---------------------------------------------------------------------------
# Data-dependent — skipped wherever the download is absent (all of CI)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAVE_DATA, reason="MSC tarball not fetched")
def test_test_split_shape() -> None:
    eps = msc.episodes("test")
    assert len(eps) == 501
    first = eps[0]
    assert first["episode_id"] == "test_0"
    assert [s["index"] for s in first["sessions"]] == [1, 2, 3, 4, 5]
    # The final session sits on the anchor; earlier ones precede it.
    dates = [s["date"] for s in first["sessions"]]
    assert dates[-1] == msc._fmt(msc.EPOCH)
    assert dates == sorted(dates)


@pytest.mark.skipif(not _HAVE_DATA, reason="MSC tarball not fetched")
def test_episode_store_round_trip(tmp_path: Path) -> None:
    eps = msc.episodes("test")
    mapping, n = msc.build_episode_store(tmp_path / "one", eps[0])
    assert n == sum(len(s["rounds"]) for s in eps[0]["sessions"])
    assert set(mapping.values()) == {"s1", "s2", "s3", "s4", "s5"}
