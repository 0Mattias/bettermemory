"""Tests for the corpus runner and the pre-registration scorecard.

The scorecard is the artifact a skeptic reads first, so the thing that
matters most here is that it can return MISSED — a grader that can only
say "hit" is decoration. Both directions are pinned for every prediction
that has a numeric threshold.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> ModuleType:
    key = f"rot_{name}"
    spec = importlib.util.spec_from_file_location(
        key, _ROOT / "bench" / "rot" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


corpus = _load("corpus")
scorecard = _load("scorecard")


def test_module_registration_matches_the_spec_name() -> None:
    """The bug that killed the first corpus run.

    `@dataclass` resolves `sys.modules[cls.__module__]` while processing a
    class, so a module executed under one name and registered under
    another dies inside dataclasses with an unhelpful AttributeError. The
    `rot_` prefix is separately load-bearing: `select` is a stdlib module
    and registering ours bare would shadow it process-wide.
    """
    assert "rot_run" in sys.modules
    assert "rot_select" in sys.modules
    import select as stdlib_select

    assert not hasattr(stdlib_select, "FRAME_SHA256"), (
        "the bench's select.py shadowed the stdlib select module"
    )


# ---------------------------------------------------------------------------
# Deletion spread — one bulk prune is one event
# ---------------------------------------------------------------------------


def _repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(root), "config", key, value], check=True)


def _commit(root: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", message], check=True)
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_deletion_spread_counts_commits_not_files(tmp_path: Path) -> None:
    """Twenty files removed in one commit is ONE event. Counting it as
    twenty is pseudo-replication, and it is exactly how a single bulk
    refactor would masquerade as a well-spread signal."""
    _repo(tmp_path)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    for i in range(6):
        (pkg / f"m{i}.py").write_text(f"X = {i}\n")
    t0 = _commit(tmp_path, "base")

    for i in range(6):
        (pkg / f"m{i}.py").unlink()
    t1 = _commit(tmp_path, "one big prune")

    commits, directories = corpus.deletion_spread(
        tmp_path, {"t0": t0, "t1": t1, "subdir": "pkg"}
    )
    assert commits == 1, "six files in one commit must count as one commit"
    assert directories == 1


def test_deletion_spread_ignores_excluded_paths(tmp_path: Path) -> None:
    """Deleting a test suite says nothing about the drift being measured,
    so it must not let a repository clear the deletion gate."""
    _repo(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    tests = tmp_path / "pkg" / "tests"
    tests.mkdir()
    for i in range(4):
        (tests / f"test_{i}.py").write_text("assert True\n")
    t0 = _commit(tmp_path, "base")
    for i in range(4):
        (tests / f"test_{i}.py").unlink()
    t1 = _commit(tmp_path, "drop tests")

    commits, _ = corpus.deletion_spread(tmp_path, {"t0": t0, "t1": t1, "subdir": "pkg"})
    assert commits == 0


# ---------------------------------------------------------------------------
# The repo-level sign test
# ---------------------------------------------------------------------------


def test_sign_test_is_symmetric_and_bounded() -> None:
    assert corpus.sign_test(0, 0) is None
    assert corpus.sign_test(5, 5) == 1.0
    assert corpus.sign_test(3, 7) == corpus.sign_test(7, 3)
    assert corpus.sign_test(15, 0) is not None
    p = corpus.sign_test(15, 0)
    assert p is not None and p < 0.001, "a 15-0 sweep must read as significant"
    weak = corpus.sign_test(6, 4)
    assert weak is not None and weak > 0.5, "6-4 is a coin"


# ---------------------------------------------------------------------------
# The scorecard must be able to say MISSED
# ---------------------------------------------------------------------------


def _report(**over: object) -> dict:
    """A minimal results shape; overrides let each prediction be flipped."""
    stats = {
        "n": 100,
        "actually_false": 10,
        "base_rate": 0.1,
        "flag_rate": 0.5,
        "unflagged_stale_rate": 0.0,
        "false_alarm_rate": 0.02,
        "precision": 0.5,
        "youden_j": 0.1,
        "fisher_p": 0.01,
        "alerts_per_catch": 5.0,
        "auroc": 0.55,
        "auroc_p": 0.01,
        "auroc_among_flagged": 0.5,
    }
    pooled = {
        name: {kind: dict(stats) for kind in ("path", "symbol", "literal", "ALL")}
        for name in (
            "file_level_incumbent",
            "claim_level_strict",
            "claim_level_weak",
            "path_drift_only",
        )
    }
    pooled["path_drift_only"]["ALL"]["flag_rate"] = 0.0
    pooled["oracle_replica"] = {"ALL": dict(stats, youden_j=1.0)}
    report = {
        "pooled": pooled,
        "path_drift_absolute_arm": {
            kind: dict(stats) for kind in ("path", "symbol", "literal", "ALL")
        },
        "per_repo": [
            {
                "repo": f"o/r{i}",
                "stratum": "D",
                "claims": 60,
                "file_level": dict(stats, base_rate=0.2),
            }
            for i in range(10)
        ],
        "walked_to_rank": 767,
    }
    report.update(over)  # type: ignore[arg-type]
    return report


def _corpus_for(report: dict) -> dict:
    return {
        "strata": {
            "D": [{"full_name": r["repo"], "py_files": 10} for r in report["per_repo"]],
            "R": [],
        }
    }


def test_every_prediction_can_be_graded() -> None:
    report = _report()
    rows = scorecard.grade(report, _corpus_for(report))
    assert [r["id"] for r in rows] == [f"P{i}" for i in range(1, 8)]
    assert all(r["verdict"] in ("hit", "MISSED") for r in rows), (
        f"a prediction was UNEVALUABLE: {[r for r in rows if r['verdict'] not in ('hit', 'MISSED')]}"
    )


def test_the_retraction_branch_fires_when_precision_is_perfect() -> None:
    """P5 is the one that matters. If the claim-level detector still
    reproduces the oracle exactly on a corpus nobody chose, the grader
    must say MISSED — that is the branch that forces a retraction rather
    than a celebration."""
    report = _report()
    report["pooled"]["claim_level_strict"]["symbol"]["precision"] = 1.0
    verdicts = {
        r["id"]: r["verdict"] for r in scorecard.grade(report, _corpus_for(report))
    }
    assert verdicts["P5"] == "MISSED"

    report["pooled"]["claim_level_strict"]["symbol"]["precision"] = 0.80
    verdicts = {
        r["id"]: r["verdict"] for r in scorecard.grade(report, _corpus_for(report))
    }
    assert verdicts["P5"] == "hit"


def test_p2_misses_on_any_nonzero_relative_path_flag() -> None:
    report = _report()
    report["pooled"]["path_drift_only"]["ALL"]["flag_rate"] = 0.0001
    verdicts = {
        r["id"]: r["verdict"] for r in scorecard.grade(report, _corpus_for(report))
    }
    assert verdicts["P2"] == "MISSED"


def test_p7_misses_when_pruned_repos_are_scarce() -> None:
    """The addendum's headline stratum. Relocations are excluded from the
    count, so a corpus of wholesale package moves reads as underpowered
    rather than as fifteen qualifying repositories."""
    report = _report()
    for entry in report["per_repo"]:
        entry["file_level"]["base_rate"] = 1.0  # every claim false = relocation
    graded = {r["id"]: r for r in scorecard.grade(report, _corpus_for(report))}
    assert graded["P7"]["verdict"] == "MISSED"
    assert graded["P7"]["facts"]["d_relocated"] == 10
    assert graded["P7"]["facts"]["d_pruned"] == 0
