"""Tests for the pre-registered corpus selection.

Selection is the part of a comparative benchmark that a skeptic attacks
first, so it is tested before it is run. The interesting cases here are
the ones where a plausible implementation silently selects the wrong
thing — a funding link mistaken for a source repo, a monorepo whose
package root is guessed rather than elected, a delete-then-re-add counted
as a deletion.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "bench_rot_select", _ROOT / "bench" / "rot" / "select.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_rot_select"] = module
    spec.loader.exec_module(module)
    return module


select = _load()


# ---------------------------------------------------------------------------
# The frame is a file, and that is the whole reproducibility argument
# ---------------------------------------------------------------------------


def test_frame_is_pinned_by_hash_and_loads_in_rank_order() -> None:
    """A frame that can drift is reproducible by courtesy, not by
    construction. The GitHub search API was rejected for exactly this:
    identical queries 15 minutes apart returned the same set in a
    different order. A hashed file cannot do that."""
    rows = select.load_frame()
    assert len(rows) == select.FRAME_ROWS
    ranks = [r[0] for r in rows]
    assert ranks == sorted(ranks)
    assert ranks[0] == 1
    downloads = [r[2] for r in rows]
    assert downloads == sorted(downloads, reverse=True), (
        "the frame must be in descending download order — the ORDER is the "
        "frame, and it is walked rather than cut"
    )


def test_a_changed_frame_file_is_refused(tmp_path: Path) -> None:
    """If the bytes change it is a different frame, and its numbers are not
    comparable to the published ones. Failing loudly is the only safe
    behaviour."""
    fake = tmp_path / "frame.json"
    fake.write_text('{"rows": [{"project": "x", "download_count": 1}]}')
    with pytest.raises(ValueError, match="hash mismatch"):
        select.load_frame(fake)


# ---------------------------------------------------------------------------
# Package -> repository
# ---------------------------------------------------------------------------


def test_a_funding_link_is_not_a_source_repository() -> None:
    """The measured trap. `pydantic`'s PyPI metadata yields
    `https://github.com/sponsors/samuelcolvin`, so the obvious rule
    ("first URL containing github.com") maps a real package to a
    sponsors page — which would then be screened, cloned and scored as
    if it were the library."""
    owner, name, reason = select.repo_from_project_urls(
        {"project_urls": {"Funding": "https://github.com/sponsors/samuelcolvin"}}
    )
    assert (owner, name) == (None, None)
    # Rejected either way; the code records that no SOURCE url was found,
    # because "Funding" is not a key the priority order consults at all.
    assert reason == "no_github_url"

    # And when the sponsors link IS under a consulted key, it is refused
    # rather than mistaken for the repository.
    owner, name, reason = select.repo_from_project_urls(
        {"project_urls": {"Homepage": "https://github.com/sponsors/samuelcolvin"}}
    )
    assert (owner, name) == (None, None)
    assert reason == "github_url_unparseable"


def test_source_keys_win_over_homepage() -> None:
    """Priority order is fixed, so the mapping is a function of the
    metadata rather than of dict iteration order."""
    owner, name, reason = select.repo_from_project_urls(
        {
            "project_urls": {
                "Homepage": "https://github.com/someone/docs-site",
                "Source": "https://github.com/psf/requests",
            }
        }
    )
    assert (owner, name, reason) == ("psf", "requests", "")


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/psf/requests",
        "https://github.com/psf/requests/",
        "https://github.com/psf/requests.git",
        "http://www.github.com/psf/requests",
    ],
)
def test_repo_url_shapes_all_normalise(url: str) -> None:
    owner, name, reason = select.repo_from_project_urls(
        {"project_urls": {"Source": url}}
    )
    assert (owner, name, reason) == ("psf", "requests", "")


def test_missing_and_non_github_urls_are_classified_not_guessed() -> None:
    assert select.repo_from_project_urls({})[2] == "no_github_url"
    assert (
        select.repo_from_project_urls(
            {"project_urls": {"Source": "https://gitlab.com/a/b"}}
        )[2]
        == "no_github_url"
    )


# ---------------------------------------------------------------------------
# Electing the source directory — the silent-corruption risk
# ---------------------------------------------------------------------------


def test_src_layout_elects_the_package_not_src() -> None:
    subdir, reason = select.elect_subdir(
        ["src/pkg/__init__.py", "src/pkg/core.py", "setup.py"]
    )
    assert (subdir, reason) == ("src/pkg", "")


def test_flat_layout_elects_the_package() -> None:
    subdir, reason = select.elect_subdir(
        ["pkg/__init__.py", "pkg/core.py", "tests/test_core.py"]
    )
    assert (subdir, reason) == ("pkg", "")


def test_a_monorepo_with_two_equal_packages_is_rejected_not_guessed() -> None:
    """No single right answer exists, so picking one would be a hidden
    choice made by the party being measured. Rejecting is the only
    defensible behaviour."""
    subdir, reason = select.elect_subdir(
        [
            "alpha/__init__.py",
            "alpha/a.py",
            "beta/__init__.py",
            "beta/b.py",
        ]
    )
    assert subdir is None
    assert reason == "ambiguous_package_root"


def test_a_dominant_package_wins_a_tie_on_depth() -> None:
    subdir, reason = select.elect_subdir(
        [
            "alpha/__init__.py",
            "alpha/a.py",
            "alpha/b.py",
            "alpha/c.py",
            "beta/__init__.py",
        ]
    )
    assert (subdir, reason) == ("alpha", "")


def test_namespace_packages_are_rejected_rather_than_mis_elected() -> None:
    """A known, published failure mode: no `__init__.py` means no elected
    root. Better to lose the repository than to score the wrong tree."""
    subdir, reason = select.elect_subdir(["pkg/core.py", "pkg/other.py"])
    assert subdir is None
    assert reason == "no_package_directory"


@pytest.mark.parametrize(
    "path",
    [
        "pkg/tests/test_a.py",
        "pkg/_vendor/six.py",
        "docs/conf.py",
        "pkg/migrations/0001_initial.py",
        "pkg/api_pb2.py",
        "pkg/_version.py",
        ".tox/x/y.py",
    ],
)
def test_excluded_paths(path: str) -> None:
    assert select.is_excluded_path(path)


@pytest.mark.parametrize("path", ["pkg/core.py", "pkg/sub/handler.py", "src/pkg/a.py"])
def test_kept_paths(path: str) -> None:
    assert not select.is_excluded_path(path)


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------


def _tree(n: int, prefix: str = "pkg") -> list[str]:
    return [f"{prefix}/__init__.py"] + [f"{prefix}/m{i}.py" for i in range(n)]


def test_deletions_are_net_absence_not_delete_events() -> None:
    """A file deleted and re-added inside the window is PRESENT at t1, so
    `label_claim` reads still_true for it. An event-counting gate would
    admit repositories that supply no path positives at all — defeating
    the gate's only purpose."""
    t0 = _tree(40)
    # Everything survives: the churn happened, the files came back.
    stratum, reason, facts = select.screen_trees(
        t0, t0, deletion_commits=30, deletion_directories=9
    )
    assert facts["deleted_py_files"] == 0
    assert stratum == "R", "re-added files must not qualify as deletions"
    assert reason == ""


def test_a_genuinely_pruned_repo_reaches_the_enriched_stratum() -> None:
    t0 = _tree(60)
    t1 = t0[:30]
    stratum, reason, facts = select.screen_trees(
        t0, t1, deletion_commits=8, deletion_directories=4
    )
    assert facts["deleted_py_files"] >= select.MIN_DELETED_PY_FILES
    assert (stratum, reason) == ("D", "")


def test_deletions_concentrated_in_one_commit_do_not_qualify() -> None:
    """Twenty files removed in one commit is ONE event, not twenty
    independent observations. Counting it as twenty is pseudo-replication."""
    t0 = _tree(60)
    t1 = t0[:30]
    stratum, _, _ = select.screen_trees(
        t0, t1, deletion_commits=1, deletion_directories=1
    )
    assert stratum == "R", "a single bulk prune must not reach stratum D"


def test_case_colliding_repos_are_rejected() -> None:
    """The runner's filesystem is case-insensitive, so `Path('Foo.py').exists()`
    is True when only `foo.py` exists. On such a repo a deleted module reads
    still_true — fabricating negatives in exactly the class the deletion gate
    exists to create."""
    stratum, reason, _ = select.screen_trees(
        ["pkg/__init__.py", "pkg/handler.py", "pkg/Handler.py"], []
    )
    assert stratum is None
    assert reason == "case_collision"


def test_size_bounds_reject_at_both_ends() -> None:
    tiny = ["pkg/__init__.py", "pkg/a.py"]
    assert select.screen_trees(tiny, tiny)[1] == "too_few_py_files"
    huge = _tree(select.MAX_PY_FILES_IN_SUBDIR + 50)
    assert select.screen_trees(huge, huge)[1] == "too_many_py_files"


def test_excluded_files_do_not_count_toward_size_or_deletions() -> None:
    """Otherwise a repo could clear the deletion gate by dropping its test
    suite, which says nothing about the drift the benchmark measures."""
    t0 = _tree(30) + [f"pkg/tests/test_{i}.py" for i in range(40)]
    t1 = _tree(30)
    stratum, _, facts = select.screen_trees(
        t0, t1, deletion_commits=9, deletion_directories=5
    )
    assert facts["deleted_py_files"] == 0
    assert stratum == "R", "deleting only tests must not qualify as pruning"


def test_packages_sharing_a_repository_are_deduped_by_rank() -> None:
    """Measured on the real frame, not anticipated: `pydantic` (rank 20)
    and `pydantic-core` (rank 26) both resolve to `pydantic/pydantic`.

    Without this the same repository is cloned and scored twice and
    contributes its claims twice to a pooled test that assumes
    independent observations — pseudo-replication that would inflate
    exactly the corpus this work exists to make trustworthy. Earliest
    rank wins, so the rule carries no discretion.
    """
    C = select.Candidate
    kept, dropped = select.dedupe_by_repo(
        [
            C(26, "pydantic-core", 5, "pydantic", "pydantic", None),
            C(20, "pydantic", 9, "pydantic", "pydantic", None),
            C(40, "typing-inspection", 1, "pydantic", "typing-inspection", None),
            C(99, "unmapped", 1, None, None, "no_github_url"),
        ]
    )
    assert [c.project for c in kept] == ["pydantic", "typing-inspection"]
    assert {c.project for c in dropped} == {"pydantic-core", "unmapped"}


def test_dedupe_is_case_insensitive_on_the_repo_name() -> None:
    """GitHub owner/name are case-insensitive, so two spellings are one
    repository and must not both be drawn."""
    C = select.Candidate
    kept, dropped = select.dedupe_by_repo(
        [
            C(1, "a", 9, "Encode", "HTTPX", None),
            C(2, "b", 8, "encode", "httpx", None),
        ]
    )
    assert len(kept) == 1 and len(dropped) == 1
    assert kept[0].rank == 1
