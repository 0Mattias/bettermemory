"""Walk the pre-registered frame and draw the corpus.

Applies `select.py`'s committed rules, in rank order, until the per-stratum
quotas fill. Nothing here decides anything: every threshold, filter and
tie-break lives in `select.py` and was pushed before this script was first
run. What this file adds is only the plumbing — PyPI metadata, the GitHub
trees API, and a resumable cache so a network failure costs time rather
than integrity.

Screening touches ZERO clone bytes for a rejected candidate: both window
ends are read as trees over the API, and only survivors are cloned. That
matters because the qualifying rate is low and the frame is long.

    venv/bin/python bench/rot/screen.py --out bench/rot/corpus.json

Deletion SPREAD (commits, directories) is deliberately not computed here.
The trees give net absence exactly, which is the label-relevant quantity;
commit spread needs history, so it is measured after clone in `corpus.py`
and the stratum is finalised there. Splitting it that way keeps the
expensive half off the rejects.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent


def _load_select() -> Any:
    spec = importlib.util.spec_from_file_location("select", _HERE / "select.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["select_mod"] = module
    spec.loader.exec_module(module)
    return module


select = _load_select()

_TOKEN = ""
_UA = "bettermemory-rot-bench"


def _token() -> str:
    global _TOKEN
    if not _TOKEN:
        _TOKEN = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        ).stdout.strip()
    return _TOKEN


class RateLimited(Exception):
    """Raised rather than returned, and that distinction is the point.

    A rate-limited request that degraded to `None` would be indistinguishable
    from a repository that genuinely has no tree, no commits, or no PyPI
    entry — so exhausting the API budget would silently REJECT repositories
    that should have been screened, and the corpus would be shaped by when
    the quota ran out. That is precisely the class of silent corruption this
    benchmark keeps finding in itself, so the failure is made loud: the
    screen waits for the reset instead of guessing.
    """


def _get(url: str, *, auth: bool = True, retries: int = 4) -> Any:
    """One JSON GET. Returns None only for a genuine absence (404/451)."""
    for attempt in range(retries):
        request = urllib.request.Request(url, headers={"User-Agent": _UA})
        if auth:
            request.add_header("Authorization", f"Bearer {_token()}")
            request.add_header("Accept", "application/vnd.github+json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (404, 451):
                return None  # the resource really is not there
            if error.code in (403, 429):
                remaining = error.headers.get("x-ratelimit-remaining")
                reset = error.headers.get("x-ratelimit-reset")
                if remaining == "0" and reset:
                    wait = max(0, int(reset) - int(time.time())) + 5
                    if attempt == retries - 1:
                        raise RateLimited(f"core quota exhausted, resets in {wait}s")
                    print(f"  rate limited; sleeping {wait}s", flush=True)
                    time.sleep(wait)
                    continue
                time.sleep(20 * (attempt + 1))  # secondary limit
                continue
            if error.code >= 500:
                time.sleep(3 * (attempt + 1))
                continue
            return None
        except Exception:
            time.sleep(2 * (attempt + 1))
    raise RateLimited(f"gave up after {retries} attempts: {url}")


def _py_paths(tree: Any) -> list[str] | None:
    """`.py` blob paths from a recursive tree, or None if git truncated it."""
    if not tree or tree.get("truncated"):
        # A truncated tree would make "deleted" mean "absent from the part
        # of the tree GitHub chose to send", which is not a fact about the
        # repository. Reject rather than measure something else.
        return None
    return [
        entry["path"]
        for entry in tree.get("tree", [])
        if entry.get("type") == "blob" and entry["path"].endswith(".py")
    ]


def screen_one(rank: int, project: str, downloads: int) -> dict[str, Any]:
    """Map, resolve the window, fetch both trees, apply the committed screen."""
    row: dict[str, Any] = {
        "rank": rank,
        "project": project,
        "downloads": downloads,
        "stratum": None,
        "reject": "",
    }
    info = _get(f"https://pypi.org/pypi/{project}/json", auth=False)
    if not info:
        row["reject"] = "pypi_unavailable"
        return row
    owner, name, reason = select.repo_from_project_urls(info["info"])
    row["owner"], row["name"], row["reject"] = owner, name, reason
    if not owner:
        return row

    meta = _get(f"https://api.github.com/repos/{owner}/{name}")
    if not meta:
        row["reject"] = "repo_unavailable"
        return row
    if meta.get("archived"):
        row["reject"] = "archived"
        return row
    row["full_name"] = meta["full_name"]
    row["default_branch"] = meta["default_branch"]

    tip = _get(
        f"https://api.github.com/repos/{owner}/{name}/commits"
        f"?sha={meta['default_branch']}&per_page=1"
    )
    if not tip:
        row["reject"] = "no_commits"
        return row
    t1 = tip[0]["sha"]

    until = (datetime.now(timezone.utc) - timedelta(days=select.WINDOW_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    older = _get(
        f"https://api.github.com/repos/{owner}/{name}/commits"
        f"?sha={meta['default_branch']}&until={until}&per_page=1"
    )
    if not older:
        row["reject"] = "no_commit_at_window_start"
        return row
    t0 = older[0]["sha"]
    if t0 == t1:
        row["reject"] = "no_activity_in_window"
        return row
    row["t0"], row["t1"] = t0, t1

    tree0 = _py_paths(
        _get(f"https://api.github.com/repos/{owner}/{name}/git/trees/{t0}?recursive=1")
    )
    tree1 = _py_paths(
        _get(f"https://api.github.com/repos/{owner}/{name}/git/trees/{t1}?recursive=1")
    )
    if tree0 is None or tree1 is None:
        row["reject"] = "tree_unavailable_or_truncated"
        return row

    # Spread is unknown until the clone; pass values that cannot themselves
    # gate here, so this stage decides only what trees can decide.
    stratum, reject, facts = select.screen_trees(
        tree0,
        tree1,
        deletion_commits=select.MIN_DELETION_COMMITS,
        deletion_directories=select.MIN_DELETION_DIRECTORIES,
    )
    row.update(facts)
    row["stratum"] = stratum
    row["reject"] = reject
    row["deletion_dirs_from_trees"] = len(
        {
            p.rsplit("/", 1)[0]
            for p in tree0
            if p not in set(tree1) and not select.is_excluded_path(p) and "/" in p
        }
    )
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description="Draw the pre-registered corpus.")
    parser.add_argument("--out", default=str(_HERE / "corpus.json"))
    parser.add_argument("--cache", default=str(_HERE / ".screen-cache.json"))
    parser.add_argument("--max-rank", type=int, default=1200)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    cache_path = Path(args.cache)
    cache: dict[str, Any] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())

    frame = select.load_frame()
    walked = [row for row in frame if row[0] <= args.max_rank]
    todo = [row for row in walked if row[1] not in cache]
    print(f"frame {len(frame)} rows; walking {len(walked)}; {len(todo)} to screen")

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(screen_one, rank, project, downloads): project
            for rank, project, downloads in todo
        }
        for future in concurrent.futures.as_completed(futures):
            project = futures[future]
            try:
                cache[project] = future.result()
            except RateLimited:
                # NOT cached. A rate-limited candidate has not been screened,
                # and writing it as a reject would let the API budget decide
                # the corpus. It stays absent so a resumed run retries it.
                pass
            except Exception as error:  # pragma: no cover - network variance
                cache[project] = {"project": project, "reject": f"error:{error}"}
            done += 1
            if done % 50 == 0:
                cache_path.write_text(json.dumps(cache))
                print(f"  screened {done}/{len(todo)}", flush=True)
    cache_path.write_text(json.dumps(cache))

    # Dedupe by repository, EARLIEST RANK WINS, before quotas are filled --
    # `pydantic` and `pydantic-core` resolve to the same repository, and a
    # repo drawn twice would be pooled twice into a test that assumes
    # independent observations.
    candidates = [
        select.Candidate(
            rank=row["rank"],
            project=row["project"],
            downloads=row["downloads"],
            owner=row.get("owner"),
            name=row.get("name"),
            reject=row.get("reject") or None,
        )
        for row in cache.values()
        if row.get("rank") is not None
    ]
    kept, dropped = select.dedupe_by_repo(candidates)
    keep_projects = {c.project for c in kept}

    # WALK THE FRAME, NOT THE CACHE. Iterating the cache would silently skip
    # a rank that failed to screen, and a walk with holes in it is not a
    # prefix of the ranking — it is a subset chosen partly by which requests
    # happened to succeed. Stop at the first gap instead.
    chosen: dict[str, list[dict[str, Any]]] = {"D": [], "R": []}
    rejects: dict[str, int] = {}
    last_rank = 0
    unscreened_at = None
    for rank, project, _ in walked:
        if all(len(chosen[s]) >= select.QUOTA_PER_STRATUM[s] for s in select.STRATA):
            break
        row = cache.get(project)
        if row is None or row.get("rank") is None:
            unscreened_at = rank
            break
        last_rank = row["rank"]
        if row["project"] not in keep_projects:
            rejects["duplicate_repo"] = rejects.get("duplicate_repo", 0) + 1
            continue
        stratum = row.get("stratum")
        if stratum is None:
            reason = row.get("reject") or "unknown"
            rejects[reason] = rejects.get(reason, 0) + 1
            continue
        if len(chosen[stratum]) < select.QUOTA_PER_STRATUM[stratum]:
            chosen[stratum].append(row)
        else:
            rejects["quota_full"] = rejects.get("quota_full", 0) + 1

    corpus = {
        "frame_sha256": select.FRAME_SHA256,
        "frame_snapshot": select.FRAME_SNAPSHOT_DATE,
        "window_days": select.WINDOW_DAYS,
        "walked_to_rank": last_rank,
        # Non-null means the walk stopped at an unscreened candidate rather
        # than because quotas filled — the corpus is a SHORTER prefix than
        # intended, never a gappy one.
        "halted_unscreened_at_rank": unscreened_at,
        "screened": len(cache),
        "duplicate_repos_collapsed": len(dropped),
        "rejects": dict(sorted(rejects.items(), key=lambda kv: -kv[1])),
        "strata": {s: chosen[s] for s in select.STRATA},
    }
    Path(args.out).write_text(json.dumps(corpus, indent=2))
    print(f"\nwalked to rank {last_rank}; D={len(chosen['D'])} R={len(chosen['R'])}")
    if unscreened_at:
        print(f"HALTED: rank {unscreened_at} was never screened — resume and re-run")
    print("top rejects:", list(corpus["rejects"].items())[:8])
    return 0


if __name__ == "__main__":
    sys.exit(main())
