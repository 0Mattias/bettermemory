"""A broken episode subtree must not take the memory-health report down.

`report_for_directory` computes every memory bucket first and only then
reaches the sibling episode tier for its size gauge
(`EpisodeStore.volume`). That gauge walks `<root>/episodes` with a bare
`iterdir` in `iter_session_ids`, so the subtree arriving in a shape the
process cannot walk raises out of the LAST line of a function whose
other twenty rollups already succeeded — and out of both of its
callers: the `memory_health` MCP tool and `bettermemory health`. (A
third, the web dashboard, was the reason this said "all three" until
5.0.0 removed it.)

Two shapes reach it, and neither is exotic in a store that syncs:

* a regular FILE where the directory belongs (a bad export, a conflict
  copy) -> `NotADirectoryError`
* a directory this process cannot read -> `PermissionError`

Both are `OSError`. The contract these tests pin is that the report
survives with its memory half intact and the gauge degraded to None —
"no reading" — rather than to a zeroed `EpisodeVolume`, which would
assert an empty subtree over episodes nobody could count.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bettermemory.health import render_text, report_for_directory
from bettermemory.store import Store


_BODY = "Releases are cut from main only after the full CI matrix is green."


def _seeded(memory_dir: Path) -> Store:
    """A store with one real memory, so the assertions below can tell
    "the memory half survived" from "the report came back empty"."""
    store = Store(memory_dir)
    store.write(content=_BODY, scopes=["tools"])
    return store


def test_report_survives_an_episodes_path_that_is_a_file(
    memory_dir: Path,
) -> None:
    """`<root>/episodes` as a regular file: `exists()` is True, so the
    gauge walks straight into `NotADirectoryError`."""
    _seeded(memory_dir)
    (memory_dir / "episodes").write_text("stray export\n", encoding="utf-8")

    report = report_for_directory(memory_dir)

    assert report.total_active_memories == 1
    assert report.episode_volume is None
    # The whole surface, not just the dataclass: the text renderer drops
    # its "Episodes:" line on a None gauge rather than printing zeroes.
    assert "Episodes:" not in render_text(report)
    assert report.to_dict()["episode_volume"] is None


def test_report_survives_an_unreadable_episodes_directory(
    memory_dir: Path,
) -> None:
    """Mode 0o000 on a NON-empty episode subtree — the case where zeroes
    would be an outright false reading, not merely an unknown one."""
    _seeded(memory_dir)
    episodes = memory_dir / "episodes"
    (episodes / "sess_a").mkdir(parents=True)
    (episodes / "sess_a" / "01.md").write_text("body\n", encoding="utf-8")

    os.chmod(episodes, 0o000)
    try:
        try:
            list(episodes.iterdir())
        except OSError:
            pass
        else:
            pytest.skip("a 0o000 directory is still readable (running as root?)")

        report = report_for_directory(memory_dir)
    finally:
        # Restore before tmp_path teardown needs to walk back in.
        os.chmod(episodes, 0o700)

    assert report.total_active_memories == 1
    assert report.episode_volume is None
