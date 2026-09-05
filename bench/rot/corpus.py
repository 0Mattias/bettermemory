"""Run the drawn corpus and score it against the pre-registration.

Takes `corpus.json` from `screen.py`, clones each survivor at its pinned
shas, finalises the stratum with the deletion-spread gate that needs
history, runs the harness per repository, and POOLS the rows.

    venv/bin/python bench/rot/corpus.py --corpus bench/rot/corpus.json

POOLING, NOT AVERAGING. Per-repo rates averaged together give a ten-claim
repository the same weight as a thousand-claim one, and the significance
tests would then describe no actual population. Every claim is one
observation. The per-repo breakdown is kept beside the pooled numbers,
because an aggregate that cannot be decomposed hides exactly the
single-repo artifact this corpus exists to escape.

Clones are FULL clones, deliberately: the harness needs the whole commit
history (`compute_commit_drift` reads every author timestamp) and checks
out two worktrees per repository. A `--filter=blob:none` partial clone
was tried first and measured to be wrong — see `clone`, which records
the repack storm it caused.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import platform
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent


def _load(name: str) -> Any:
    """Import a sibling bench module by path.

    The registered key must MATCH the spec name: `@dataclass` resolves
    `sys.modules[cls.__module__]` while processing the class, so a module
    executed under one name and registered under another blows up inside
    dataclasses with an unhelpful AttributeError. The `rot_` prefix is
    also load-bearing — `select` is a stdlib module, and registering ours
    under that name would shadow it for everything in the process.
    """
    key = f"rot_{name}"
    spec = importlib.util.spec_from_file_location(key, _HERE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


run = _load("run")
select = _load("select")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    ).stdout.strip()


def clone(entry: dict[str, Any], root: Path) -> Path | None:
    """Full clone at the pinned window. Returns the path, or None.

    A `--filter=blob:none` partial clone was the obvious choice and was
    MEASURED TO BE WRONG here. The harness checks out two full worktrees
    per repository, so a partial clone has to lazy-fetch every blob in
    both trees; each fetch lands loose objects, git's automatic GC kicks
    in, and the run spends its time in `repack`/`pack-objects` rather
    than in the benchmark. Observed directly on scipy: no progress, no
    `git log` running, and a repack storm underneath.

    A full clone downloads more once and then touches the network zero
    times. `gc.auto=0` keeps git from deciding to repack mid-run whatever
    the object count looks like.
    """
    target = root / entry["full_name"].replace("/", "__")
    if not (target / ".git").exists():
        result = subprocess.run(
            [
                "git",
                "-c",
                "gc.auto=0",
                "clone",
                "--no-checkout",
                "--quiet",
                f"https://github.com/{entry['full_name']}.git",
                str(target),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        subprocess.run(
            ["git", "-C", str(target), "config", "gc.auto", "0"],
            capture_output=True,
            check=False,
        )
    # The pinned shas must exist in what we fetched; a force-push since
    # screening would otherwise silently move the window.
    for sha in (entry["t0"], entry["t1"]):
        if _git(target, "cat-file", "-t", sha) != "commit":
            return None
    return target


def deletion_spread(repo: Path, entry: dict[str, Any]) -> tuple[int, int]:
    """(commits, directories) that deleted a non-excluded .py in the window.

    Needs history, so it cannot run at screen time. Twenty files removed
    in one commit is ONE event, not twenty independent observations —
    counting it as twenty is pseudo-replication.
    """
    out = _git(
        repo,
        "log",
        "--format=\x01%H",
        "--diff-filter=D",
        "--name-only",
        "--no-renames",
        f"{entry['t0']}..{entry['t1']}",
    )
    commits: set[str] = set()
    directories: set[str] = set()
    sha = ""
    for line in out.splitlines():
        if line.startswith("\x01"):
            sha = line[1:]
            continue
        path = line.strip()
        if not path.endswith(".py") or select.is_excluded_path(path):
            continue
        subdir = entry.get("subdir") or ""
        if subdir and not path.startswith(subdir + "/"):
            continue
        commits.add(sha)
        directories.add(path.rsplit("/", 1)[0] if "/" in path else ".")
    return len(commits), len(directories)


def window_facts(repo: Path, entry: dict[str, Any]) -> dict[str, int]:
    """What the window holds that separates the two basis arms.

    `commits` is the reachable range `t0..t1`; `merges` the merge commits
    in it; `authored_before_t0` the commits in it whose AUTHOR date is at
    or before t0's commit instant — the population the author-date count
    cannot see, because it bisects author dates against the stamp. A
    corpus whose windows hold none of those cannot exercise the defect,
    which is why the number is recorded beside the arms it explains.
    """
    t0, t1 = entry["t0"], entry["t1"]
    t0_instant = datetime.fromisoformat(_git(repo, "show", "-s", "--format=%cI", t0))
    commits = merges = before = 0
    for line in _git(repo, "rev-list", "--format=%aI %P", f"{t0}..{t1}").splitlines():
        if line.startswith("commit "):
            commits += 1
            continue
        stamp, _, parents = line.partition(" ")
        if len(parents.split()) > 1:
            merges += 1
        try:
            if datetime.fromisoformat(stamp) <= t0_instant:
                before += 1
        except ValueError:
            continue
    return {"commits": commits, "merges": merges, "authored_before_t0": before}


_BASIS_ARMS = (
    "drift_only_relative_cite_author_date",
    "drift_only_relative_cite_reachability",
)


def basis_disagreements(rows: list[dict[str, Any]]) -> int:
    """Claims the author-date arm counted as zero and the reachability
    arm counted — the defect, per claim. The two arms' rows come off
    `_score_claims` in claim order, one row per arm per claim."""
    author = [r for r in rows if r["mode"] == _BASIS_ARMS[0]]
    reach = [r for r in rows if r["mode"] == _BASIS_ARMS[1]]
    assert len(author) == len(reach)
    return sum(
        1
        for a, b in zip(author, reach)
        if a["commit_drift"] == 0 and b["commit_drift"] > 0
    )


def provenance() -> dict[str, Any]:
    """Version, commit and platform stamp for the artifact, the shape the
    integrity and retrieval benches write. `tree_dirty` counts tracked
    modifications only; read at launch, before any arm runs."""
    commit: str | None = None
    tree_dirty: bool | None = None
    try:
        commit = _git(_HERE, "rev-parse", "--short", "HEAD") or None
        tree_dirty = bool(_git(_HERE, "status", "--porcelain", "--untracked-files=no"))
    except OSError:
        pass
    version: str | None = None
    try:
        import bettermemory

        version = bettermemory.__version__
    except ImportError:
        pass
    return {
        "bettermemory_version": version,
        "commit": commit,
        "tree_dirty": tree_dirty,
        "date": date.today().isoformat(),
        "machine": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
        },
    }


def sign_test(wins: int, losses: int) -> float | None:
    """Two-sided exact binomial p for a paired repo-level comparison.

    The repo, not the claim, is the unit here: it asks whether the
    claim-level detector beats the file-level one in MORE REPOSITORIES
    than a coin would, which no single large repository can carry.
    """
    n = wins + losses
    if not n:
        return None
    tail = sum(math.comb(n, k) for k in range(min(wins, losses) + 1))
    return round(min(1.0, 2 * tail / (2**n)), 5)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run and score the corpus.")
    parser.add_argument("--corpus", default=str(_HERE / "corpus.json"))
    parser.add_argument("--clones", default=str(Path.home() / ".cache" / "bm-rot"))
    parser.add_argument("--out", default=str(_HERE / "results" / "multirepo.json"))
    args = parser.parse_args()

    corpus = json.loads(Path(args.corpus).read_text())
    root = Path(args.clones)
    root.mkdir(parents=True, exist_ok=True)
    stamp = provenance()

    entries = [e for s in select.STRATA for e in corpus["strata"][s]]
    pooled: list[dict[str, Any]] = []
    per_repo: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    for i, entry in enumerate(entries, start=1):
        label = entry["full_name"]
        print(f"[{i}/{len(entries)}] {label}", flush=True)
        repo = clone(entry, root)
        if repo is None:
            failures.append({"repo": label, "why": "clone_or_sha_unavailable"})
            continue
        commits, directories = deletion_spread(repo, entry)
        stratum = (
            "D"
            if (
                entry.get("deleted_py_files", 0) >= select.MIN_DELETED_PY_FILES
                and commits >= select.MIN_DELETION_COMMITS
                and directories >= select.MIN_DELETION_DIRECTORIES
            )
            else "R"
        )
        try:
            rows, meta = run.collect_rows(
                repo,
                entry["subdir"],
                entry["t0"],
                entry["t1"],
                f"https://github.com/{label}.git",
            )
        except Exception as error:
            failures.append({"repo": label, "why": f"{type(error).__name__}: {error}"})
            continue

        for row in rows:
            row["stratum"] = stratum
        pooled.extend(rows)

        arm = [r for r in rows if r["mode"] == run._MODES[0]]
        basis_rows = {
            name: [r for r in rows if r["mode"] == name] for name in _BASIS_ARMS
        }
        summary = {
            "repo": label,
            "rank": entry["rank"],
            "stratum": stratum,
            "screened_stratum": entry.get("stratum"),
            "deletion_commits": commits,
            "deletion_directories": directories,
            **{
                k: meta[k]
                for k in ("subdir", "t0", "t1", "claims", "claims_false_at_t0")
            },
            "file_level": run._detector_stats(
                arm, lambda r: r["flagged"], score=lambda r: r["commit_drift"]
            ),
            "claim_weak": run._detector_stats(
                arm, lambda r: r["claim_weak"], score=lambda r: r["cite_commits"]
            ),
            "claim_strict": run._detector_stats(
                arm, lambda r: r["claim_strict"], score=lambda r: r["cite_commits"]
            ),
            # The basis arms, per repository: the same file-level flag,
            # counted from the shipped function on each axis, beside what
            # the window held for the axes to disagree about.
            "window": window_facts(repo, entry),
            "author_date": run._detector_stats(
                basis_rows[_BASIS_ARMS[0]],
                lambda r: r["flagged"],
                score=lambda r: r["commit_drift"],
            ),
            "reachability": run._detector_stats(
                basis_rows[_BASIS_ARMS[1]],
                lambda r: r["flagged"],
                score=lambda r: r["commit_drift"],
            ),
            "author_date_zero_reachability_positive": basis_disagreements(rows),
        }
        per_repo.append(summary)
        print(
            f"    {stratum}  {meta['claims']} claims, "
            f"{summary['file_level']['actually_false']} false, "
            f"alerts/catch {summary['file_level']['alerts_per_catch']} -> "
            f"{summary['claim_weak']['alerts_per_catch']}",
            flush=True,
        )

    # Pooled scoring, on the informative arm.
    arm = [r for r in pooled if r["mode"] == run._MODES[0]]
    detectors = {
        "file_level_incumbent": (lambda r: r["flagged"], lambda r: r["commit_drift"]),
        "claim_level_strict": (
            lambda r: r["claim_strict"],
            lambda r: r["cite_commits"],
        ),
        "claim_level_weak": (lambda r: r["claim_weak"], lambda r: r["cite_commits"]),
        "path_drift_only": (lambda r: r["path_drift"] > 0, lambda r: r["path_drift"]),
    }
    pooled_stats: dict[str, Any] = {}
    for name, (flag, score) in detectors.items():
        block = {}
        for kind in (*run.CLAIM_CLASSES, "ALL"):
            sel = [r for r in arm if kind == "ALL" or r["kind"] == kind]
            block[kind] = run._detector_stats(sel, flag, score=score)
        pooled_stats[name] = block
    pooled_stats["oracle_replica"] = {
        "ALL": run._detector_stats(arm, run.BASELINES["oracle_replica"])
    }
    for constant in ("always_flag", "never_flag"):
        pooled_stats[constant] = {
            "ALL": run._detector_stats(arm, run.BASELINES[constant])
        }

    # Absolute arm, where path drift can actually fire.
    absolute = [r for r in pooled if r["mode"] == run._MODES[1]]
    path_absolute = {
        kind: run._detector_stats(
            [r for r in absolute if kind == "ALL" or r["kind"] == kind],
            lambda r: r["path_drift"] > 0,
        )
        for kind in (*run.CLAIM_CLASSES, "ALL")
    }

    # Anchored-relative arm — APPENDED, not folded into anything above.
    # The three arms scored so far are what PREREGISTRATION.md describes
    # and what the published scorecard grades; this one measures behaviour
    # that did not exist when those predictions were written, so it gets
    # its own block and its own name. Selected BY NAME rather than by
    # `_MODES[3]`: adding a fourth positional consumer is how the
    # positional coupling that already makes reordering dangerous would
    # spread.
    anchored = [r for r in pooled if r["mode"] == "drift_only_relative_cite_anchored"]
    path_anchored = {
        kind: run._detector_stats(
            [r for r in anchored if kind == "ALL" or r["kind"] == kind],
            lambda r: r["path_drift"] > 0,
        )
        for kind in (*run.CLAIM_CLASSES, "ALL")
    }

    # The basis arms, pooled — APPENDED like the anchored arm, selected by
    # name. Same rows, same oracle; the arms differ only in the axis the
    # shipped function counted on. P1 of the reachability unit is graded
    # here, from the artifact, on its two pre-registered clauses: the
    # pooled J of the reachability arm is at least the author-date arm's
    # (MISSED if lower by more than 0.01), and at least one repository
    # whose window holds a commit authored before t0 has a claim the
    # author-date arm counted as zero and the reachability arm counted
    # (MISSED if no such repository exists, which would mean the corpus
    # cannot exercise the defect).
    basis_pooled: dict[str, Any] = {}
    for name in _BASIS_ARMS:
        sel = [r for r in pooled if r["mode"] == name]
        basis_pooled[name] = {
            kind: run._detector_stats(
                [r for r in sel if kind == "ALL" or r["kind"] == kind],
                lambda r: r["flagged"],
                score=lambda r: r["commit_drift"],
            )
            for kind in (*run.CLAIM_CLASSES, "ALL")
        }
    author_j = basis_pooled[_BASIS_ARMS[0]]["ALL"]["youden_j"]
    reach_j = basis_pooled[_BASIS_ARMS[1]]["ALL"]["youden_j"]
    exercised = [
        r["repo"]
        for r in per_repo
        if r["window"]["authored_before_t0"] > 0
        and r["author_date_zero_reachability_positive"] > 0
    ]
    p1_first = (
        author_j is not None and reach_j is not None and reach_j >= author_j - 0.01
    )
    p1 = {
        "author_date_j": author_j,
        "reachability_j": reach_j,
        "j_delta": (
            round(reach_j - author_j, 4)
            if author_j is not None and reach_j is not None
            else None
        ),
        "repos_with_commits_authored_before_t0": sum(
            1 for r in per_repo if r["window"]["authored_before_t0"] > 0
        ),
        "repos_where_author_date_reads_zero_and_reachability_counts": exercised,
        "claims_author_date_zero_reachability_positive": sum(
            r["author_date_zero_reachability_positive"] for r in per_repo
        ),
        "verdict": "hit" if p1_first and exercised else "MISSED",
    }

    # Repo-level paired comparison on alerts-per-catch.
    wins = losses = ties = 0
    for summary in per_repo:
        a = summary["file_level"]["alerts_per_catch"]
        b = summary["claim_weak"]["alerts_per_catch"]
        if a is None or b is None:
            continue
        if b < a:
            wins += 1
        elif b > a:
            losses += 1
        else:
            ties += 1

    report = {
        "provenance": stamp,
        "frame_sha256": corpus["frame_sha256"],
        "walked_to_rank": corpus["walked_to_rank"],
        "window_days": corpus["window_days"],
        "repos_scored": len(per_repo),
        "repos_failed": failures,
        "strata_counts": {
            s: sum(1 for r in per_repo if r["stratum"] == s) for s in select.STRATA
        },
        "pooled_claims": len(arm),
        "pooled_false": sum(1 for r in arm if r["truth"] == "false"),
        "python": sys.version.split()[0],
        "pooled": pooled_stats,
        "path_drift_absolute_arm": path_absolute,
        "path_drift_anchored_relative_arm": path_anchored,
        "repo_level_paired": {
            "claim_weak_better": wins,
            "file_level_better": losses,
            "tied": ties,
            "sign_test_p": sign_test(wins, losses),
        },
        "basis_arms": {"pooled": basis_pooled, "P1": p1},
        "per_repo": per_repo,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))

    print(f"\npooled {len(arm)} claims, {report['pooled_false']} false")
    for name in ("file_level_incumbent", "claim_level_weak", "claim_level_strict"):
        s = pooled_stats[name]["ALL"]
        print(
            f"  {name:<22} J={s['youden_j']}  alerts/catch={s['alerts_per_catch']}  "
            f"prec={s['precision']}  AUROC={s['auroc']}"
        )
    print(
        f"  oracle_replica         J={pooled_stats['oracle_replica']['ALL']['youden_j']}"
    )
    # The B1 acceptance numbers, printed beside the arm they came from
    # rather than graded in scorecard.py: the scorecard grades
    # PRE-registered predictions, and a threshold written after the
    # behaviour exists is not one of those. Bar for the item: path-leg J
    # materially > 0 at precision >= 0.9, against the relative arm's
    # pre-registered zero.
    rel_j = pooled_stats["path_drift_only"]["ALL"]
    anc = path_anchored["ALL"]
    print(
        f"  path leg, relative arm  J={rel_j['youden_j']}  "
        f"prec={rel_j['precision']}  flag_rate={rel_j['flag_rate']}"
    )
    print(
        f"  path leg, ANCHORED arm  J={anc['youden_j']}  "
        f"prec={anc['precision']}  flag_rate={anc['flag_rate']}  "
        f"alerts/catch={anc['alerts_per_catch']}"
    )
    print(
        f"  repo-level paired: {wins}-{losses}-{ties}, p={report['repo_level_paired']['sign_test_p']}"
    )
    print(
        f"  basis arms: author-date J={author_j}  reachability J={reach_j}  "
        f"claims author-date zero / reachability positive="
        f"{p1['claims_author_date_zero_reachability_positive']} "
        f"in {len(exercised)} of "
        f"{p1['repos_with_commits_authored_before_t0']} exercised repos  "
        f"-> P1 {p1['verdict']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
