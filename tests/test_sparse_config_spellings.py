"""Sparse omissions stay unconfirmed under Git's boolean config spellings."""

from __future__ import annotations

from pathlib import Path

import pytest

from bettermemory.verify_anchoring import (
    ClaimAnchor,
    SPARSE_CHECKOUT,
    detect_path_drift,
    unverifiable_attestations,
)

from .test_origin_patch import _plain, neutral_git

__all__ = ["neutral_git"]


def _sparse_repo(repo: Path) -> Path:
    repo.mkdir()
    _plain(repo, "init", "-q")
    for folder in ("keep", "drop"):
        (repo / folder).mkdir()
        (repo / folder / "a.py").write_text("value = 1\n")
    _plain(repo, "add", ".")
    _plain(repo, "commit", "-qm", "sparse fixture")
    _plain(repo, "sparse-checkout", "set", "--cone", "keep")
    assert not (repo / "drop/a.py").exists()
    config = repo / ".git/config.worktree"
    assert config.exists()
    return config


@pytest.mark.parametrize(
    "setting",
    [
        "[core]\n sparseCheckout = true\n",
        "\ufeff[core]\n sparseCheckout = true\n",
        "[core] sparseCheckout = true\n",
        "[core]\n sparseCheckout = 2\n",
        "[core]\n sparseCheckout = -2\n",
        "[core]\n sparseCheckout = 2k\n",
        "[core]\n sparseCheckout = tr\\\nue\n",
        '[CoRe] sPaRsEcHeCkOuT = "TrUe"\n',
    ],
)
def test_git_boolean_spellings_keep_omitted_paths_unconfirmed(
    tmp_path: Path, setting: str
) -> None:
    repo = tmp_path / "repo"
    config = _sparse_repo(repo)
    config.write_text(setting, encoding="utf-8")
    assert _plain(repo, "config", "--bool", "core.sparseCheckout") == "true"
    anchor = ClaimAnchor(str(repo), "home_checkout")
    report = detect_path_drift(
        "The omitted source is `drop/a.py`.",
        verified_paths=["drop/a.py"],
        anchor=anchor,
    )
    assert not report.missing and not report.claim_anchored_missing
    assert report.unconfirmed_reasons == (SPARSE_CHECKOUT,)
    assert unverifiable_attestations(["drop/a.py"], anchor=anchor) == []


def test_a_deleted_file_kept_by_the_sparse_cone_still_reads_missing(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _sparse_repo(repo)
    (repo / "keep/a.py").unlink()
    anchor = ClaimAnchor(str(repo), "home_checkout")
    report = detect_path_drift(
        "The kept source is `keep/a.py`.",
        verified_paths=["keep/a.py"],
        anchor=anchor,
    )
    assert report.missing and report.claim_anchored_missing
    assert not report.unconfirmed
    assert unverifiable_attestations(["keep/a.py"], anchor=anchor)
