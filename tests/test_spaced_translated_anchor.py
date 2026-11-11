"""Claim 37: `_detect_anchored`'s spaced-bare prefix fallback cuts the
TRANSLATED target at its first space, not the cited path."""

from __future__ import annotations

from pathlib import Path

import pytest

from bettermemory import verify_anchoring
from bettermemory.verify_anchoring import ClaimAnchor, detect_path_drift


def _anchor(old: str, live: str) -> ClaimAnchor:
    """A home-checkout anchor whose absolute claims recorded under `old`
    translate to the live view `live` (what `Membership.translate` does)."""

    def translate(path: str) -> tuple[str, str]:
        if path == old or path.startswith(old + "/"):
            return live + path[len(old) :], "checkout"
        return path, "absolute"

    return ClaimAnchor(root=live, rule="home_checkout", translate=translate)


def test_control_live_root_without_space(tmp_path: Path) -> None:
    live = tmp_path / "x" / "MyCode" / "proj"
    live.mkdir(parents=True)
    (live / "hosts").write_text("h")
    old = "/old/place/proj"
    report = detect_path_drift(
        f"The file {old}/hosts TCP/IP keepalive overrides today",
        anchor=_anchor(old, str(live)),
    )
    assert tuple(map(Path, report.checked)) == (live / "hosts",)


def test_cited_file_is_checked_when_live_root_has_a_space(tmp_path: Path) -> None:
    live = tmp_path / "x" / "My Code" / "proj"
    live.mkdir(parents=True)
    (live / "hosts").write_text("h")
    old = "/old/place/proj"
    report = detect_path_drift(
        f"The file {old}/hosts TCP/IP keepalive overrides today",
        anchor=_anchor(old, str(live)),
    )
    assert tuple(map(Path, report.checked)) == (live / "hosts",)


def test_uncited_directory_is_not_reported(tmp_path: Path) -> None:
    live = tmp_path / "x" / "My Code" / "proj"
    live.mkdir(parents=True)
    (live / "hosts").write_text("h")
    (tmp_path / "x" / "My").mkdir()  # never cited by the memory
    old = "/old/place/proj"
    report = detect_path_drift(
        f"The file {old}/hosts TCP/IP keepalive overrides today",
        anchor=_anchor(old, str(live)),
    )
    assert tmp_path / "x" / "My" not in map(Path, report.checked)
    assert tuple(map(Path, report.checked)) == (live / "hosts",)


def test_existing_complete_spaced_filename_is_kept(tmp_path: Path) -> None:
    live = tmp_path / "My Code" / "proj"
    live.mkdir(parents=True)
    whole = live / "hosts TCP" / "IP"
    whole.parent.mkdir()
    whole.write_text("whole")
    (live / "hosts").write_text("prefix")
    old = "/old/place/proj"
    report = detect_path_drift(
        f"The file {old}/hosts TCP/IP", anchor=_anchor(old, str(live))
    )
    assert tuple(map(Path, report.checked)) == (whole,)


def test_unanchored_prefix_does_not_probe_a_shortened_live_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "My Code" / "proj"
    live.mkdir(parents=True)
    old = "/old/place/proj"
    asked: list[str] = []

    def translate(path: str) -> tuple[str, str]:
        if path == f"{old}/hosts":
            return path, "unanchored"
        return str(live) + path[len(old) :], "checkout"

    def exists(path: str) -> bool:
        asked.append(path)
        return False

    monkeypatch.setattr(verify_anchoring, "_path_exists", exists)
    report = detect_path_drift(
        f"The file {old}/hosts TCP/IP keepalive overrides today",
        anchor=ClaimAnchor(root=str(live), rule="home_checkout", translate=translate),
    )
    assert report.checked == ()
    assert asked and all(path.startswith(str(live)) for path in asked)
