"""One fetch of an evicted file is in flight at a time (ENG-160).

`_fetch` asks a file provider for an evicted file by reading it on a worker
thread, waited on for the fetch budget; a fetch that outruns the budget
keeps running. Each capture started another read on another thread, so a
provider that never answers gathered a blocked thread per capture. The
fetch is now a bounded read: a capture that finds one in flight waits on
it, and one past the budget defers at once.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from bettermemory import location, location_git


def test_a_fetch_in_flight_is_not_started_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two captures of a file whose provider does not answer make one read:
    both defer, the second at once; once the read lands, the next fetch
    reads again and finds the file."""
    f = tmp_path / "HEAD"
    f.write_text("ref: refs/heads/main\n")
    real = location._read_one
    reads: list[str] = []
    release = threading.Event()

    def hung(path: str) -> None:
        reads.append(path)
        if len(reads) == 1:
            assert release.wait(10)
        real(path)

    monkeypatch.setattr(location, "_read_one", hung)
    budget = 0.1
    try:
        first = location._fetch(str(f), budget)
        t0 = time.monotonic()
        second = location._fetch(str(f), budget)
        waited = time.monotonic() - t0
        started = len(reads)
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while str(f) in location_git._FLIGHTS and time.monotonic() < deadline:
        time.sleep(0.01)
    third = location._fetch(str(f), budget)
    assert started == 1, f"each capture started its own read: {started}"
    assert (first, second) == (False, False)
    assert waited < budget, waited
    assert third is True and len(reads) == 2
