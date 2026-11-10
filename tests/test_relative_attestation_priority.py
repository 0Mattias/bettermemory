"""Claim 7: a body citing a file by its ABSOLUTE path, with the same file
attested by its RELATIVE spelling, must be read as the attestation
(verified / claim_anchored_missing / expected_absent /
present_but_attested_absent), under an anchor and under worktree_root."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bettermemory.verify_anchoring import ClaimAnchor, detect_path_drift


def _root(tmp_path):
    root = tmp_path / "R"
    (root / "src").mkdir(parents=True)
    (root / "src" / "here.py").write_text("x = 1\n")
    return os.path.realpath(root)


def _read(mode, body, root, **attest):
    if mode == "anchor":
        return detect_path_drift(
            body, anchor=ClaimAnchor(root=root, rule="home_checkout"), **attest
        )
    return detect_path_drift(body, worktree_root=root, **attest)


MODES = ["anchor", "worktree_root"]


@pytest.mark.parametrize("mode", MODES)
def test_control_absolute_attestation_escalates(mode, tmp_path):
    """Control: same body, the attestation spelled absolutely."""
    root = _root(tmp_path)
    gone = f"{root}/src/gone.py"
    report = _read(mode, f"The config lives at `{gone}`.", root, verified_paths=[gone])
    assert report.missing == (gone,)
    assert report.claim_anchored_missing == (gone,)


@pytest.mark.parametrize("mode", MODES)
def test_control_no_body_citation_relative_attestation(mode, tmp_path):
    """Control: relative attestation alone escalates / reads expected_absent."""
    root = _root(tmp_path)
    gone = f"{root}/src/gone.py"
    report = _read(mode, "No paths here.", root, verified_paths=["src/gone.py"])
    assert report.claim_anchored_missing == (str(Path(gone)),)
    report = _read(mode, "No paths here.", root, absent_paths=["src/gone.py"])
    assert report.expected_absent == (str(Path(gone)),)
    assert report.missing == ()


@pytest.mark.parametrize("mode", MODES)
def test_verified_then_deleted_relative_attestation_escalates(mode, tmp_path):
    root = _root(tmp_path)
    gone = f"{root}/src/gone.py"
    report = _read(
        mode, f"The config lives at `{gone}`.", root, verified_paths=["src/gone.py"]
    )
    assert report.missing == (str(Path(gone)),)
    assert report.claim_anchored_missing == (str(Path(gone)),)
    assert report.has_claim_anchored_drift


@pytest.mark.parametrize("mode", MODES)
def test_attested_absent_relative_is_expected_absent(mode, tmp_path):
    root = _root(tmp_path)
    gone = f"{root}/src/gone.py"
    report = _read(
        mode, f"The config lives at `{gone}`.", root, absent_paths=["src/gone.py"]
    )
    assert report.expected_absent == (str(Path(gone)),)
    assert report.missing == ()


@pytest.mark.parametrize("mode", MODES)
def test_existing_relative_attestation_is_verified(mode, tmp_path):
    root = _root(tmp_path)
    here = f"{root}/src/here.py"
    report = _read(
        mode, f"The config lives at `{here}`.", root, verified_paths=["src/here.py"]
    )
    assert report.checked == (str(Path(here)),)
    assert report.verified == (str(Path(here)),)


def test_present_but_attested_absent_relative_under_anchor(tmp_path):
    root = _root(tmp_path)
    here = f"{root}/src/here.py"
    report = _read(
        "anchor", f"The config lives at `{here}`.", root, absent_paths=["src/here.py"]
    )
    assert report.present_but_attested_absent == (str(Path(here)),)


@pytest.mark.parametrize("home", [True, False])
@pytest.mark.parametrize("absent", [True, False])
def test_a_translated_absolute_citation_defers_to_the_relative_claim(
    tmp_path: Path, home: bool, absent: bool
) -> None:
    root = _root(tmp_path)
    old = str(tmp_path / "old-checkout")
    gone = f"{root}/src/gone.py"

    def translated(path: str) -> tuple[str, str]:
        return (
            path.replace(old, root, 1),
            "checkout" if home else "project_checkout",
        )

    anchor = ClaimAnchor(
        root=root,
        rule="home_checkout" if home else "project_checkout",
        translate=translated,
    )
    paths = ["src/gone.py"]
    report = detect_path_drift(
        f"The config lives at `{old}/src/gone.py`.",
        anchor=anchor,
        verified_paths=[] if absent else paths,
        absent_paths=paths if absent else [],
    )
    if absent:
        assert report.expected_absent == (str(Path(gone)),)
        assert report.missing == report.unconfirmed == ()
    elif home:
        assert report.claim_anchored_missing == (str(Path(gone)),)
        assert report.unconfirmed == ()
    else:
        assert report.missing == report.claim_anchored_missing == ()
        assert report.unconfirmed == (str(Path(gone)),)
        assert report.unconfirmed_reasons == ("project_checkout",)


@pytest.mark.parametrize("mode", MODES)
def test_an_unrelated_citation_is_still_advisory(mode, tmp_path: Path) -> None:
    root = _root(tmp_path)
    other = f"{root}/src/other.py"
    gone = f"{root}/src/gone.py"
    report = _read(
        mode,
        f"The config lives at `{other}`.",
        root,
        verified_paths=["src/gone.py:2"],
    )
    assert set(report.missing) == {other, str(Path(gone))}
    assert report.claim_anchored_missing == (str(Path(gone)),)
