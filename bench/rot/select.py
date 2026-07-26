"""Pre-registered repository selection for the multi-repo rot corpus.

READ THIS BEFORE READING ANY RESULT IT PRODUCES. The entire value of a
comparative artifact is that "they chose repositories where they score
well" must be *structurally* impossible to allege. Everything here is
committed BEFORE a single repository is screened, and every threshold is
a module-level constant with its justification beside it. Where a
threshold is arbitrary, it says so.

WHY NOT GITHUB STARS. The obvious frame — `language:Python stars:>=N`
via the search API — was designed first and rejected on two measured
grounds:

  1. IT IS NOT DETERMINISTIC. Two identical enumerations 15 minutes
     apart returned the same SET in a different ORDER, with star counts
     drifting by 1-5 on popular repositories. Over months, repos cross
     the star floor in both directions, get archived, renamed, or
     deleted, and `language:` is recomputed on every push. Any frame
     that has to be pinned by a manifest AFTER the fact is reproducible
     by courtesy, not by construction.
  2. A STAR-RANKED PYTHON FRAME IS SUBSTANTIALLY A FRAME OF MARKDOWN.
     Its top entries are curated lists — public-apis,
     free-programming-books, awesome-python — containing essentially no
     Python. Whatever rule removes them is then doing far more work than
     a reader would guess.

THE FRAME USED INSTEAD is a dated, static file: the top-PyPI-packages
download ranking, vendored at `frame/top-pypi-packages-2026-07-01.json`
and hashed by `FRAME_SHA256`. It is deterministic because it is a FILE,
not a query — a third party re-derives the identical ordering from the
committed bytes, with no API access and no trust in us. Every entry is
an installable Python package by definition, so the markdown problem
cannot arise. And download volume is dependency weight, which is much
closer to "code a memory tool is actually pointed at" than a star count.

IT IS STILL A BIASED FRAME, and the bias is stated rather than argued
away: heavily-downloaded packages are mature, well-staffed, and
conservative about deletion. The corpus this produces is "widely-depended-on
Python packages", not "Python code". Private, under-maintained code — the
kind this product most often runs against — is unrepresented here by
construction and no filter fixes that.

THE ORDER IS THE FRAME, AND IT IS WALKED, NOT CUT. There is deliberately
no "top N" constant, because N would be a tunable knob. Screening
proceeds strictly down the download ranking until the per-stratum quotas
fill, and the rank of the last repository examined is published. Moving
the stopping point cannot change which repositories precede it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# The frame
# --------------------------------------------------------------------------

FRAME_PATH = _HERE / "frame" / "top-pypi-packages-2026-07-01.json"
FRAME_SHA256 = "c40ccdde2a07d48c25c31a9d9d8fcbfe8c166987b1b43aa47e02b695a01c71f1"
FRAME_SNAPSHOT_DATE = "2026-07-01"
FRAME_ROWS = 15000

# --------------------------------------------------------------------------
# Thresholds. Each one is a degree of freedom, so each one is justified or
# is admitted to be arbitrary.
# --------------------------------------------------------------------------

# The roadmap's own requirement, inherited rather than derived: the path
# leg fired ZERO times across 675 claims because bettermemory never
# deletes, so the corpus must contain repositories that do. It is
# ARBITRARY in magnitude — nothing distinguishes 20 from 15 or 30 — and
# it is selection on a label-correlated quantity BY CONSTRUCTION, which
# is why a control stratum exists (see STRATA below).
MIN_DELETED_PY_FILES = 20

# Deletions must be NET ABSENCE at t1, not delete EVENTS in the log. A
# file deleted and re-added inside the window is present at t1, so
# `label_claim` reads `still_true` for it — an event-counting gate would
# admit repositories that supply no path positives at all, defeating the
# gate's only purpose.
DELETIONS_ARE_NET_ABSENCE = True

# Anti-domination. One 5,000-claim repository would own a corpus
# advertised as N repositories, and pooled significance would describe
# that repository rather than the population. Justified as a cost and
# weighting bound, both label-independent — but it does correlate with
# repo character (large repos are more monorepo-shaped, more generated,
# older), so the accepted corpus's file-count distribution is published.
MAX_PY_FILES_IN_SUBDIR = 800

# Below this a repository contributes too few claims for its per-repo
# row to mean anything, and per-repo rows are the headline unit.
MIN_PY_FILES_IN_SUBDIR = 10

# Deletions concentrated in one commit or one directory are ONE event,
# not twenty, and treating them as twenty independent observations is
# pseudo-replication. Arbitrary in magnitude; the point is that some
# spread is required, not that 5 and 3 are special.
MIN_DELETION_COMMITS = 5
MIN_DELETION_DIRECTORIES = 3

# Window length. 180 days rather than 365: `window_diff_text` holds the
# whole `git log -p -U0` in one string, measured at ~7.8 KB per commit,
# so a year on a busy repository is 75-100 MB of text and a
# multi-million-entry list. 180 also keeps the window recent enough that
# the repository still resembles what a user would point the tool at.
WINDOW_DAYS = 180

# --------------------------------------------------------------------------
# Strata — a case-control design, named as such
# --------------------------------------------------------------------------

# The deletion gate selects on a quantity correlated with the outcome
# being measured. This CANNOT be made unbiased, so it is not pretended
# away: stratum R applies every filter EXCEPT the deletion gate, drawn
# from the same walk of the same frame. D-vs-R measures how far the gate
# moved the base rate. It does not close the gap, and every prevalence
# figure the corpus produces is higher than the wild.
STRATA = ("D", "R")
QUOTA_PER_STRATUM = {"D": 15, "R": 15}

# --------------------------------------------------------------------------
# Package -> repository, and why this is not a one-liner
# --------------------------------------------------------------------------

# PyPI metadata is free text, and the obvious rule ("first URL containing
# github.com") is WRONG in a way that would silently corrupt the corpus:
# `pydantic` resolves to `https://github.com/sponsors/samuelcolvin`, a
# funding link. So keys are consulted in priority order and known
# non-source paths are rejected outright.
_SOURCE_KEY_PRIORITY = (
    "source",
    "source code",
    "repository",
    "code",
    "github",
    "homepage",
    "home",
)
_NOT_SOURCE_PATH = re.compile(
    r"^/(sponsors|users|orgs|apps|marketplace|topics|collections)(/|$)"
)
_REPO_URL = re.compile(
    r"^https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)"
    r"(?:\.git)?/?$"
)


@dataclass(frozen=True)
class Candidate:
    """One row of the frame, after mapping."""

    rank: int
    project: str
    downloads: int
    owner: str | None
    name: str | None
    reject: str | None

    @property
    def full_name(self) -> str | None:
        return f"{self.owner}/{self.name}" if self.owner and self.name else None


def load_frame(path: Path = FRAME_PATH) -> list[tuple[int, str, int]]:
    """The frame, in rank order, with its hash checked.

    The hash check is the whole reproducibility argument: a third party
    who has these bytes derives this ordering, full stop. If the file
    ever changes, every downstream number is from a different frame and
    must not be compared to the published one.
    """
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != FRAME_SHA256:
        raise ValueError(
            f"frame file hash mismatch: expected {FRAME_SHA256}, got {digest} — "
            "this is a DIFFERENT frame and its results are not comparable"
        )
    rows = json.loads(raw)["rows"]
    return [
        (rank, row["project"], row["download_count"])
        for rank, row in enumerate(rows, start=1)
    ]


def repo_from_project_urls(info: dict[str, Any]) -> tuple[str | None, str | None, str]:
    """Map a PyPI project's metadata to (owner, name, reason).

    Deterministic given the metadata: keys are tried in a fixed priority
    order, the first that yields a well-formed `github.com/<owner>/<repo>`
    wins, and funding / profile / topic paths are rejected before they can
    be mistaken for source. `reason` is '' on success and a machine-readable
    reject code otherwise, so the rejection census is publishable rather
    than inferred.
    """
    urls: dict[str, str] = {}
    for key, value in (info.get("project_urls") or {}).items():
        if isinstance(key, str) and isinstance(value, str):
            urls[key.strip().lower()] = value
    home = info.get("home_page")
    if isinstance(home, str) and home:
        urls.setdefault("home", home)

    saw_github = False
    for key in _SOURCE_KEY_PRIORITY:
        value = urls.get(key)
        if not value:
            continue
        match = _REPO_URL.match(value.strip())
        if not match:
            if "github.com" in value:
                saw_github = True
            continue
        owner, name = match.group(1), match.group(2)
        if _NOT_SOURCE_PATH.match(f"/{owner}"):
            saw_github = True
            continue
        return owner, name, ""
    return None, None, "github_url_unparseable" if saw_github else "no_github_url"


def dedupe_by_repo(
    candidates: list[Candidate],
) -> tuple[list[Candidate], list[Candidate]]:
    """Collapse frame rows that name the same repository. (kept, dropped)

    MEASURED, NOT ANTICIPATED — the first 40 rows of the real frame
    already contain the collision: `pydantic` (rank 20) and
    `pydantic-core` (rank 26) both resolve to `pydantic/pydantic`. A
    monorepo publishing several packages is common at the top of a
    download ranking, and without this the same repository would enter
    the corpus more than once, be cloned and scored more than once, and
    contribute its claims more than once to a pooled significance test
    that assumes independent observations. That is pseudo-replication of
    the crudest kind, and it would inflate exactly the corpus the
    multi-repo work exists to make trustworthy.

    The EARLIEST rank wins, so the rule is a pure function of the frame
    order and carries no discretion.
    """
    seen: set[str] = set()
    kept: list[Candidate] = []
    dropped: list[Candidate] = []
    for candidate in sorted(candidates, key=lambda c: c.rank):
        key = (candidate.full_name or "").lower()
        if not key:
            dropped.append(candidate)
            continue
        if key in seen:
            dropped.append(candidate)
            continue
        seen.add(key)
        kept.append(candidate)
    return kept, dropped


def elect_subdir(py_paths: list[str]) -> tuple[str | None, str]:
    """Choose the package directory mechanically. Returns (subdir, reason).

    A WRONG SUBDIR IS THE MOST DANGEROUS ERROR IN THIS PIPELINE — before
    the guard added to `collect_rows`, it produced a complete, well-formed
    report with n = 0. The rule is deliberately dull:

      1. drop excluded paths (tests, docs, vendored, generated, examples);
      2. take the shallowest directory that contains an `__init__.py`;
      3. if several tie at that depth, require exactly one to be the
         unique dominant holder of `.py` files — otherwise REJECT rather
         than guess, because a monorepo with several equal packages has no
         single right answer and picking one would be a hidden choice.

    Known failure modes, published rather than hidden: namespace packages
    with no `__init__.py` are rejected; a `src/` layout resolves to
    `src/<pkg>` rather than `src`, which is correct but differs from
    bettermemory's own `--subdir src`; single-module packages
    (`foo.py` at the root, no package dir) are rejected.
    """
    kept = [p for p in py_paths if not is_excluded_path(p)]
    if not kept:
        return None, "no_py_files_after_exclusions"
    packages: dict[str, int] = {}
    for path in kept:
        if not path.endswith("/__init__.py"):
            continue
        pkg = path.rsplit("/__init__.py", 1)[0]
        packages[pkg] = pkg.count("/")
    if not packages:
        return None, "no_package_directory"
    shallowest = min(packages.values())
    roots = sorted(p for p, depth in packages.items() if depth == shallowest)
    if len(roots) == 1:
        return roots[0], ""
    counts = {root: sum(1 for p in kept if p.startswith(root + "/")) for root in roots}
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None, "ambiguous_package_root"
    return ranked[0][0], ""


# Exclusions. EVERY ONE OF THESE HAS A SIGN ON THE INCUMBENT'S SCORE, and
# some of them flatter it — dropping tests and re-export shims removes
# high-churn/low-drift files, which are exactly the file-level signal's
# worst false-alarm generators. The set is committed before the frame is
# walked, and a reader can verify it predates the draw; a reader cannot
# verify it was not iterated privately, and that is a real hole.
_EXCLUDED_SEGMENTS = frozenset(
    {
        "test",
        "tests",
        "testing",
        "_test",
        "docs",
        "doc",
        "examples",
        "example",
        "samples",
        "benchmarks",
        "vendor",
        "vendored",
        "_vendor",
        "third_party",
        "thirdparty",
        "migrations",
        "node_modules",
        "site-packages",
        "build",
        "dist",
    }
)
_EXCLUDED_SUFFIXES = ("_pb2.py", "_pb2_grpc.py", "_generated.py", "_version.py")


def is_excluded_path(path: str) -> bool:
    """Vendored, generated, or non-library code. Mechanical, no judgement."""
    segments = path.split("/")
    if any(seg.lower() in _EXCLUDED_SEGMENTS for seg in segments[:-1]):
        return True
    if any(seg.startswith(".") for seg in segments):
        return True
    return path.endswith(_EXCLUDED_SUFFIXES)


def screen_trees(
    py_at_t0: list[str],
    py_at_t1: list[str],
    deletion_commits: int = 0,
    deletion_directories: int = 0,
) -> tuple[str | None, str, dict[str, Any]]:
    """Stage 2, from two tree listings. Returns (stratum, reason, facts).

    Runs on `GET /git/trees?recursive=1` at both window ends, so a
    rejected candidate costs ZERO clone bytes — which matters because the
    qualifying rate is low and the frame is long.

    Case collision is checked here and is not paranoia: the runner's
    filesystem is case-insensitive, verified directly — `Path.exists()`
    answers True for a differently-cased spelling of a module that is not
    there. So on a repository whose tree holds two modules differing only
    in case, deleting one still leaves `label_claim` reading `still_true`
    for it. That fabricates NEGATIVES in exactly the deleted class the
    deletion gate exists to create.
    """
    facts: dict[str, Any] = {}
    lowered = [p.lower() for p in py_at_t0]
    if len(set(lowered)) != len(lowered):
        return None, "case_collision", facts

    subdir, why = elect_subdir(py_at_t0)
    facts["subdir"] = subdir
    if subdir is None:
        return None, why, facts

    in_subdir = [
        p for p in py_at_t0 if p.startswith(subdir + "/") and not is_excluded_path(p)
    ]
    facts["py_files"] = len(in_subdir)
    if len(in_subdir) < MIN_PY_FILES_IN_SUBDIR:
        return None, "too_few_py_files", facts
    if len(in_subdir) > MAX_PY_FILES_IN_SUBDIR:
        return None, "too_many_py_files", facts

    # NET absence, not delete events — see DELETIONS_ARE_NET_ABSENCE.
    survivors = set(py_at_t1)
    deleted = [p for p in in_subdir if p not in survivors]
    facts["deleted_py_files"] = len(deleted)
    facts["deletion_commits"] = deletion_commits
    facts["deletion_directories"] = deletion_directories

    qualifies = (
        len(deleted) >= MIN_DELETED_PY_FILES
        and deletion_commits >= MIN_DELETION_COMMITS
        and deletion_directories >= MIN_DELETION_DIRECTORIES
    )
    return ("D" if qualifies else "R"), "", facts
