"""Memory-rot benchmark — does the staleness verdict flag claims that
actually went false, and spare the ones that did not?

WHY THIS EXISTS. The per-hit `staleness_verdict` is the mechanism the
README leads with and the thing the whole trust-layer pitch rests on. It
has had **no accuracy measurement of any kind** — not comparative, not
even self-measured. The project publishes Wilson intervals on three
secondary telemetry rates and nothing at all on its headline signal.
This is the first number.

GROUND TRUTH COMES FROM GIT, NOT FROM A MODEL. Pick a repository and two
commits (t0, t1). Extract fact-shaped claims from the tree at t0 purely
mechanically — a path exists, a top-level symbol is defined in a named
file, a module constant holds a literal. Then re-evaluate each claim
against the tree at t1 with a checker, not a judge. Nothing here asks a
language model whether a claim is still true, which is what makes the
labels un-dismissable: no party the result favours authored either the
corpus or the grading.

WHAT IS BEING GRADED. Each claim becomes a memory body citing it, with
`verified_paths` and a `last_verified_at` anchored at t0. At t1 the same
three signals production uses are computed — calendar age, path drift,
commit drift — and fed to the real `compute_staleness_verdict`. So the
function under test is the shipped one, not a reimplementation.

THE METRIC THAT MATTERS, AND ITS COUNTERWEIGHT. Reported with equal
prominence, because either alone is misleading:

  unflagged_stale_rate  of claims FALSE at t1, the fraction the verdict
                        called `fresh` — memories served as current that
                        were not. This is the failure the product exists
                        to prevent.
  false_alarm_rate      of claims still TRUE at t1, the fraction the
                        verdict flagged — noise that trains a reader to
                        ignore the signal.

A verdict that flags everything scores a perfect unflagged_stale_rate and
is worthless. A verdict that flags nothing scores a perfect
false_alarm_rate and is worthless. Publishing only the first would be
choosing the flattering half.

PER-CLASS BREAKDOWN IS THE POINT. Path claims are structurally
detectable: a deleted file is observable. Symbol and literal claims are
NOT — the file still exists, so `path_drift` sees nothing and only
`commit_drift` can fire, which knows that *something* changed in the file
but not *what*. Reporting the aggregate would hide that. The last class
is the one the design structurally cannot see, and it is named rather
than omitted.

Usage:

    venv/bin/python bench/rot/run.py --days 60
    venv/bin/python bench/rot/run.py --days 30 --json
    venv/bin/python bench/rot/run.py --repo /path/to/other --days 90
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import json
import math
import random
import re
import subprocess
import sys
import tempfile
from collections.abc import Generator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bettermemory.origin import Origin  # noqa: E402
from bettermemory.verify import (  # noqa: E402
    compute_commit_drift,
    compute_staleness_verdict,
    compute_verification_status,
    detect_path_drift,
)

CLAIM_CLASSES = ("path", "symbol", "literal")
# APPEND-ONLY. These strings are consumed POSITIONALLY downstream —
# `_MODES[0]` is "the informative arm" for pooled scoring and the per-repo
# summaries (`corpus.py`, and the detector/baseline blocks below),
# `_MODES[1]` is the absolute path-drift arm (`corpus.py`). Inserting or
# reordering silently rescopes statistics that are already published, and
# the first three arms are what the frozen PREREGISTRATION.md describes.
# A new behaviour gets a NEW NAME at the END; it never regrades an
# existing arm. Pinned by `test_mode_arms_are_append_only`.
_MODES = (
    "drift_only_relative_cite",
    "drift_only_absolute_cite",
    "shipped_default",
    "drift_only_relative_cite_anchored",
    # The BASIS arms. The four arms above take their commit count from
    # `commit_counts_touching`, a `git log t0..t1` the harness runs — the
    # window's own reachability range — and consult the shipped function
    # only for applicability. These two take the count FROM the shipped
    # function, for a memory stamped at t0's commit instant and read at
    # t1: the author-date arm hands it no anchor, so it counts the
    # commits AUTHORED after the stamp (what every release through 7.3.0
    # counted), and the reachability arm hands it t0 as `verified_head`,
    # so it counts `t0..t1`. Same claims, same oracle, same calendar
    # setting; the only difference is the axis.
    "drift_only_relative_cite_author_date",
    "drift_only_relative_cite_reachability",
)


# ---------------------------------------------------------------------------
# Extraction at t0 — mechanical, no model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    kind: str
    rel_path: str
    name: str
    value: str

    def body(self, root: Path | None = None) -> str:
        """Render the claim as a memory body.

        `root` selects the CITATION STYLE, which turns out to decide
        whether the claim gets any path checking at all. `detect_path_drift`
        excludes relative paths by design (see verify.py's module
        docstring: without an anchor, checking them would mean checking
        the cwd at retrieval time). So `src/pkg/mod.py` — the way a
        developer naturally writes it — is invisible to the path leg,
        while the same file cited absolutely is checked.

        Both styles are measured rather than one being chosen, because
        the gap between them IS a finding about the product. The gap is
        what the anchored arm exists to close: same relative body, but
        `detect_path_drift` is handed the worktree the memory was written
        in, so the citation resolves. The unanchored arms are untouched —
        the anchor is a caller argument, not a change to the citation.
        """
        cited = str(root / self.rel_path) if root else self.rel_path
        if self.kind == "path":
            return f"The module `{cited}` is part of this package."
        if self.kind == "symbol":
            return f"`{self.name}` is defined at the top level of `{cited}`."
        return f"`{self.name}` in `{cited}` is set to `{self.value}`."


def _literal_of(node: ast.AST) -> str | None:
    """Render a module-level constant's value, or None if not a literal."""
    try:
        value = ast.literal_eval(node)
    except Exception:
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return repr(value)
    return None


def extract_claims(tree_root: Path, subdir: str) -> list[Claim]:
    """Derive fact-shaped claims from a source tree. No model in the loop."""
    claims: list[Claim] = []
    base = tree_root / subdir
    for path in sorted(base.rglob("*.py")):
        rel = path.relative_to(tree_root).as_posix()
        claims.append(Claim("path", rel, rel, ""))
        try:
            parsed = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, RecursionError):
            # Not just SyntaxError: a NUL byte raised ValueError before
            # 3.12, and deeply nested literals raise RecursionError. On
            # one hand-picked repository every file parses; across a
            # corpus of unfamiliar codebases one of these would abort the
            # run partway through, which is the worst possible time.
            continue
        for node in parsed.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                claims.append(Claim("symbol", rel, node.name, ""))
            elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id.isupper():
                    literal = _literal_of(node.value)
                    if literal is not None:
                        claims.append(Claim("literal", rel, target.id, literal))
    return claims


# ---------------------------------------------------------------------------
# Oracle at t1 — mechanical, no judge
# ---------------------------------------------------------------------------


# One parse per FILE, not per claim. A file defining forty symbols yields
# forty claims, and re-parsing it forty times per tree made `compile` 30%
# of the corpus run (measured on scipy: 15,982 parses behind 10,305
# `label_claim` calls, 45s of 150s). Keyed on the tree root, which is a
# fresh `mkdtemp` per repository, so a key can never outlive the tree it
# names and collide with the next repo's identical rel_paths. Caches the
# two derived lookups rather than the AST — same answers, a fraction of
# the resident size.
_TOPLEVEL_CACHE: dict[
    tuple[str, str], tuple[frozenset[str], dict[str, str | None]]
] = {}


def _toplevel_index(
    path: Path, tree_root: Path, rel_path: str
) -> tuple[frozenset[str], dict[str, str | None]] | None:
    """Top-level def/class names and constant literals, or None if unparsable."""
    key = (str(tree_root), rel_path)
    cached = _TOPLEVEL_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        parsed = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, RecursionError):
        # See `extract_claims`: a NUL byte raised ValueError before 3.12,
        # and deep nesting raises RecursionError. A file that no longer
        # parses cannot support a claim about a symbol it defines, so
        # "false" is the right label — but it must be a LABEL, not an
        # uncaught exception that kills the run on repository nine of
        # fifteen.
        return None
    symbols = frozenset(
        node.name
        for node in parsed.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )
    literals: dict[str, str | None] = {}
    for node in parsed.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                # `setdefault`, not assignment: the uncached form returned
                # on the FIRST matching Assign, so a name rebound at module
                # level must keep resolving to its first binding.
                literals.setdefault(target.id, _literal_of(node.value))
    index = (symbols, literals)
    _TOPLEVEL_CACHE[key] = index
    return index


def label_claim(claim: Claim, tree_root: Path) -> str:
    """Re-evaluate a claim against a tree. Returns still_true | false.

    The oracle IS the benchmark: if it is wrong, everything downstream is
    noise. It therefore does the dullest possible thing — existence, an
    AST lookup, a literal comparison — and never infers.
    """
    path = tree_root / claim.rel_path
    if claim.kind == "path":
        return "still_true" if path.exists() else "false"
    if not path.exists():
        return "false"
    index = _toplevel_index(path, tree_root, claim.rel_path)
    if index is None:
        return "false"
    symbols, literals = index
    if claim.kind == "symbol":
        return "still_true" if claim.name in symbols else "false"
    if claim.name not in literals:
        return "false"
    return "still_true" if literals[claim.name] == claim.value else "false"


# ---------------------------------------------------------------------------
# Claim-level drift — "did the commit touch the thing this memory CITES?"
# ---------------------------------------------------------------------------
#
# The file-level signal knows that SOMETHING in a cited file changed, never
# WHAT. On this corpus that costs 25 alerts per genuine catch. What follows
# asks the narrower question, and the answer turns out to say more about the
# BENCHMARK than about the detector — see the ceiling discussion in
# `oracle_replica` and the README.
#
# TWO RULES KEEP THIS FROM QUIETLY BECOMING THE ORACLE:
#
#   1. `build_binding_index` takes the diff text and NOTHING else. It cannot
#      see a claim, so it cannot look one up.
#   2. The claim side enters only as `claim.body(root)` — the rendered
#      STRING — parsed by `parse_claim_citation`, exactly the information a
#      production implementation reads off a real memory body. Passing the
#      `Claim` dataclass would hand the detector structured truth the product
#      never has, and would make the value comparison privileged rather than
#      legitimate.
#
# Deliberately NOT used: git's `@@ ... @@ <section heading>`. This repo ships
# no Python diff driver, so git falls back to its default funcname heuristic,
# which yields headings like `class Store:` (136x in the 60-day window),
# `__all__ = [` (61x) and `def add_subparser(` (84x). Every method-body edit
# inside `Store` reports `class Store:`, so a heading-keyed detector is a
# body-churn amplifier wearing a def-shaped label. Column-0 matching on the
# changed lines themselves is what excludes methods, nested defs, keyword
# arguments (`foo(TIMEOUT=30)`) and dict entries (`"TIMEOUT": 30`) — that
# discipline is the entire difference between this and a name-grep.

# THE DETECTOR NOW LIVES IN THE PRODUCT. `build_binding_index`,
# `claim_level_drift` and their helpers were authored here, measured on
# the 30-repository corpus, and then PROMOTED to
# `src/bettermemory/claims.py` when claims-at-write shipped — the same
# promote-don't-reimplement rule the t1 verdict section below has always
# followed ("the shipped function, not a reimplementation"). The bench
# imports the shipped functions, underscore names included: what this
# file measures from now on is the exact code production runs, and a
# product-side change that moves the numbers shows up HERE rather than
# in a silently diverging copy. `Citation` is the product's `Claim`
# under the name this file has always used — field-for-field the same
# four slots, so `parse_claim_citation` below constructs the shipped
# class directly.
from bettermemory.claims import (  # noqa: E402
    COMMIT_MARK as _COMMIT_MARK,
)
from bettermemory.claims import (  # noqa: E402
    Claim as Citation,
)
from bettermemory.claims import (  # noqa: E402
    anchors_from_value,
    build_binding_index,
    claim_level_drift,
)

# Re-exported for the test suite: `tests/test_bench_rot.py` exercises
# the detector's helpers through THIS module (`rot._binding_token`,
# `rot.string_fragment`, …) — the module the measurements were
# published under — so the promoted names stay reachable here.
from bettermemory.claims import (  # noqa: E402, F401
    _MIN_ANCHOR_CHARS,
    _binding_token,
    _rhs_repr,
    string_fragment,
)

# Claim-body templates, parsed back out of the rendered string. This is the
# firewall: whatever the detector knows about a claim, it learned here, from
# text a real memory body would also carry.
_CITE_PATH = re.compile(r"^The module `([^`]+)` is part of this package\.$")
_CITE_SYMBOL = re.compile(r"^`([^`]+)` is defined at the top level of `([^`]+)`\.$")
_CITE_LITERAL = re.compile(r"^`([^`]+)` in `([^`]+)` is set to `(.+)`\.$", re.DOTALL)


def parse_claim_citation(body: str, repo_root: Path | None = None) -> Citation | None:
    """Recover (kind, path, name, value) from a rendered memory body.

    Resolving an absolute citation against the repo root is not a leak —
    it is exactly what `resolve_repo_pathspecs` does in production at the
    git boundary. Returns None when the body cites nothing parseable,
    which is the honest majority case for real memories and is counted
    rather than assumed away (`citation_resolved_rate`).
    """

    def _rel(cited: str) -> str:
        path = Path(cited)
        if path.is_absolute() and repo_root is not None:
            try:
                return path.relative_to(repo_root).as_posix()
            except ValueError:
                return path.as_posix()
        return path.as_posix()

    text = body.strip()
    match = _CITE_PATH.match(text)
    if match:
        rel = _rel(match.group(1))
        return Citation("path", rel, rel, "")
    match = _CITE_SYMBOL.match(text)
    if match:
        return Citation("symbol", _rel(match.group(2)), match.group(1), "")
    match = _CITE_LITERAL.match(text)
    if match:
        return Citation("literal", _rel(match.group(2)), match.group(1), match.group(3))
    return None


# ---------------------------------------------------------------------------
# Verdict at t1 — the shipped function, not a reimplementation
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


def commit_counts_touching(repo: Path, t0: str, t1: str, subdir: str) -> dict[str, int]:
    """Real per-path commit counts over the window — not a boolean.

    The first version of this harness wrote ``{p: 1 for p in changed}``:
    one bit meaning "this file was touched at some point". That is
    faithful to what `compute_staleness_verdict` *tests* — it only asks
    ``> 0`` — but it makes the detector's score a two-valued variable, and
    a two-valued score has a degenerate ROC curve. AUROC over {0,1} is
    just a rescaled version of the accuracy already reported, so the
    question "does the magnitude of the churn carry information the
    current threshold throws away?" could not even be asked.

    With real counts it can. The verdict is unchanged — thresholding a
    count at ``> 0`` gives exactly the boolean back, so no published
    flag/miss rate moves — but AUROC now ranks claims by how hard their
    file was hit, and answers whether a *better* threshold exists at all.
    A count that carries no signal is itself a finding, and one this
    benchmark previously had no way to state.

    One ``git log`` pass, not one per claim: 368 commits touch `src` in
    the 60-day window and there are 675 claims x 3 arms.
    """
    out = _git(
        repo,
        "log",
        f"--format={_COMMIT_MARK}%H",
        "--name-only",
        "--no-renames",
        f"{t0}..{t1}",
        "--",
        subdir,
    )
    counts: dict[str, int] = {}
    seen_this_commit: set[str] = set()
    for line in out.splitlines():
        if line.startswith(_COMMIT_MARK):
            seen_this_commit = set()
            continue
        path = line.strip()
        if not path or path in seen_this_commit:
            continue
        # A path can be named twice inside one commit (e.g. a rename pair
        # when --no-renames splits it into delete+add). Count the COMMIT,
        # not the mention, or churny renames inflate the score.
        seen_this_commit.add(path)
        counts[path] = counts.get(path, 0) + 1
    return counts


def window_diff_text(repo: Path, t0: str, t1: str, subdir: str) -> str:
    """The window's hunks, in one pass, at zero context.

    `-U0` rather than `-U3` on purpose: with no context lines a hunk
    contains exactly `b` removed and `d` added lines, so the parser can
    consume an exact count and VERIFY it, instead of consuming greedily
    and hoping the file's own content never looks like a diff header.
    `--no-renames` splits a rename into delete+add, which is the outcome
    the claim-level detector wants anyway — a claim about the old path
    should flag when that path stops existing.

    The `-c` overrides pin the output shape against a user's global
    gitconfig: `diff.noprefix=true` would break every `+++ b/` parse, and
    an external differ or textconv filter would substitute content
    wholesale.
    """
    return _git(
        repo,
        "-c",
        "diff.noprefix=false",
        "-c",
        "diff.mnemonicPrefix=false",
        "log",
        f"--format={_COMMIT_MARK}%H",
        "-p",
        "-U0",
        "--no-renames",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        f"{t0}..{t1}",
        "--",
        subdir,
    )


def verdict_for(
    claim: Claim,
    *,
    repo: Path,
    tree1: Path,
    origin_repo: str,
    commits_touching: dict[str, int],
    calendar_fresh: bool,
    absolute: bool,
    anchored: bool = False,
    since: datetime | None = None,
    anchor_sha: str | None = None,
) -> tuple[str, int, int]:
    """Return (verdict, path_drift_missing, commit_drift_count).

    `since` switches the commit count's SOURCE: with it the count comes
    from the shipped `compute_commit_drift` over the window's own ends —
    the memory stamped at `since` (t0's commit instant), the caller
    standing in `tree1` (so HEAD is t1) — instead of from the harness's
    `commits_touching` log, and `anchor_sha` is what the stamp recorded:
    t0 for the reachability arm, None for the author-date arm. The
    calendar leg is held fresh by evaluating it one day after the stamp,
    the same way the drift-only arms hold it, so the two basis arms
    measure the commit leg alone.

    `tree1` is the t1 END OF THE WINDOW as a materialised tree, and it is
    what absolute citations point at. `repo` is only the git directory,
    used for history. Keeping them separate is what makes `--t1` mean
    something: the path-drift leg must stat the same tree the oracle
    labels against, or a "missing file" verdict is about the developer's
    current checkout rather than about the window.

    `anchored` hands that same tree to `detect_path_drift` as the
    memory's recorded `origin.worktree_root`, which is what lets a
    RELATIVE citation be existence-checked. Defaults off so the three
    pre-registered arms keep calling the shipped function exactly as
    before — P2 ("relative arm: exactly zero path-drift flags") is graded
    off `_MODES[0]`, and an anchor leaking into it would falsify a
    published prediction with a code change.

    Deliberately NOT passing `verified_paths` in the anchored arm: that
    would exercise the attestation path, which already worked, and the
    arm would measure the wrong mechanism while looking like it measured
    the right one. The claim under measurement is the BODY citation.
    """
    body = claim.body(tree1 if absolute else None)
    drift = detect_path_drift(body, worktree_root=tree1 if anchored else None)
    if since is not None:
        verification = compute_verification_status(since, now=since + timedelta(days=1))
        caller = Origin(repo=origin_repo, cwd=str(tree1), branch="main")
        drift_status = compute_commit_drift(
            since,
            origin_repo,
            caller_origin=caller,
            verified_paths=[claim.rel_path],
            body=body,
            verified_head=anchor_sha,
        )
        count = drift_status.commits_since_verify if drift_status is not None else 0
        verdict = compute_staleness_verdict(
            verification=verification,
            path_drift_missing=len(drift.missing),
            commit_drift_count=count if drift_status is not None else None,
        )
        return verdict, len(drift.missing), count
    # Anchor inside the staleness window when isolating the drift legs, and
    # outside it when measuring the shipped default. Calendar age is not a
    # claim about the world, so folding it in silently would let a timer
    # take credit for detection it did not do.
    now = datetime.now(timezone.utc)
    anchor = now - timedelta(days=1 if calendar_fresh else 400)
    verification = compute_verification_status(anchor, now=now)
    caller = Origin(repo=origin_repo, cwd=str(repo), branch="main")
    drift_status = compute_commit_drift(
        anchor,
        origin_repo,
        caller_origin=caller,
        verified_paths=[claim.rel_path],
        body=body,
    )
    commits = commits_touching.get(claim.rel_path, 0)
    count = commits if drift_status is not None else 0
    # The ROW keeps an int (`count`) so the scoring schema is unchanged,
    # but the VERDICT must receive `None` when the commit leg was silent.
    # The two are not the same input: since 3.30.0 a measured zero stands
    # the calendar leg down on a stale memory, while `None` — "the leg
    # could not ask" — deliberately does not. Passing 0 for both would
    # manufacture exactly the false green the demotion's guard exists to
    # prevent, and would do it inside the instrument that is supposed to
    # measure the guard. Harmless before that change (the calendar
    # pre-empted every drift input, so 0 and None were indistinguishable
    # here); load-bearing after it.
    verdict = compute_staleness_verdict(
        verification=verification,
        path_drift_missing=len(drift.missing),
        commit_drift_count=count if drift_status is not None else None,
    )
    return verdict, len(drift.missing), count


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def fisher_one_sided(tp: int, fn: int, fp: int, tn: int) -> float | None:
    """Right-tail Fisher exact p for a 2x2 detector table.

    Answers the only question that makes a detection number mean anything:
    could a detector that flagged at THIS RATE, but chose at random, have
    caught at least this many? At a 4% base rate a 97% flag rate catches
    everything by construction, so a raw recall figure is not evidence.
    Hand-rolled via `math.comb` rather than adding a scipy dependency to a
    bench script.
    """
    n_false = tp + fn
    n_flagged = tp + fp
    total = tp + fn + fp + tn
    if not total or not n_false or not n_flagged:
        return None
    upper = min(n_false, n_flagged)
    denom = math.comb(total, n_false)
    if denom == 0:
        return None
    tail = sum(
        math.comb(n_flagged, i) * math.comb(total - n_flagged, n_false - i)
        for i in range(tp, upper + 1)
    )
    return round(tail / denom, 4)


def _midranks(pool: list[float]) -> dict[float, float]:
    """Map each distinct score to its midrank within `pool`.

    Every member of a tied run gets the mean of the ranks that run
    spans, which is what makes a tied pair contribute exactly 0.5 to
    AUROC instead of an arbitrary 0 or 1.
    """
    combined = sorted(pool)
    ranks: dict[float, float] = {}
    i = 0
    while i < len(combined):
        j = i
        while j + 1 < len(combined) and combined[j + 1] == combined[i]:
            j += 1
        ranks[combined[i]] = (i + j) / 2 + 1  # 1-based
        i = j + 1
    return ranks


def _auroc_from_rank_sum(rank_sum: float, n_pos: int, n_neg: int) -> float:
    """Mann-Whitney U, normalised. Unrounded — callers decide precision."""
    u = rank_sum - n_pos * (n_pos + 1) / 2
    return u / (n_pos * n_neg)


def auroc(positive: list[float], negative: list[float]) -> float | None:
    """Rank-based AUROC (Mann-Whitney U) with MIDRANK tie handling.

    `positive` are scores for claims that actually went false, `negative`
    for claims still true. Returns P(score_false > score_true) +
    0.5*P(equal) — the probability the detector ranks a genuinely rotten
    claim above a fresh one, 0.5 for a coin.

    The tie term is not a detail here, it is the whole point. Under the
    old boolean model every score was 0 or 1, so nearly every pair was a
    tie and AUROC collapsed toward 0.5 by construction rather than by
    measurement. Handling ties as midranks is what makes the number mean
    "does churn magnitude carry information" instead of silently
    re-reporting the flag rate. Hand-rolled rather than adding scipy to a
    bench script, same as `fisher_one_sided`.
    """
    if not positive or not negative:
        return None
    ranks = _midranks(positive + negative)
    return round(
        _auroc_from_rank_sum(
            sum(ranks[s] for s in positive), len(positive), len(negative)
        ),
        4,
    )


_PERMUTATIONS = 20000
_PERMUTATION_SEED = 0


def auroc_permutation_p(positive: list[float], negative: list[float]) -> float | None:
    """One-sided permutation p for an AUROC, ties handled exactly.

    Necessary, not decorative: the symbol class has SIX actually-false
    claims out of 485. An AUROC of 0.72 on six positives is well within
    what a coin produces at that n, and publishing the point estimate
    alone would repeat exactly the mistake this benchmark already
    retracted once — a flattering number shipped without the test that
    could kill it.

    Permutation rather than the normal approximation to Mann-Whitney
    because the score distribution is dominated by ties on small integer
    commit counts, which that approximation handles badly here. Seeded,
    so the published number is reproducible.

    THE OPTIMISATION IS ALSO THE CORRECTNESS ARGUMENT. Permuting labels
    never changes the pooled score multiset, so the midranks are
    invariant across all 20,000 draws. Computing them once and then
    sampling `n_pos` of them turns each draw from an O(n log n) re-sort
    into an O(n_pos) sum — and, more importantly, guarantees every draw
    is scored on exactly the same rank scale as the observed statistic,
    which a re-sort per draw only gets right by accident.
    """
    if not positive or not negative:
        return None
    pool = positive + negative
    ranks = _midranks(pool)
    all_ranks = [ranks[s] for s in pool]
    n_pos, n_neg = len(positive), len(negative)
    # Compare unrounded against unrounded: testing a permuted value
    # against a 4-dp-rounded observed would count draws that merely round
    # to the same figure, biasing p upward or downward by a hair for free.
    observed = _auroc_from_rank_sum(sum(ranks[s] for s in positive), n_pos, n_neg)
    rng = random.Random(_PERMUTATION_SEED)
    at_least = 0
    for _ in range(_PERMUTATIONS):
        drawn = _auroc_from_rank_sum(sum(rng.sample(all_ranks, n_pos)), n_pos, n_neg)
        if drawn >= observed - 1e-12:
            at_least += 1
    # +1/+1 is the standard unbiased permutation estimator: it forbids a
    # reported p of exactly 0, which 20,000 samples can never justify.
    # Rounded to 5 places, not 4, precisely so the floor survives printing —
    # at 4 places the smallest attainable value, 1/20001, renders as
    # "0.0000" and the guarantee this estimator exists to provide would be
    # destroyed by the formatter on its way to the page.
    return round((at_least + 1) / (_PERMUTATIONS + 1), 5)


def youden_j(tp: int, fn: int, fp: int, tn: int) -> float | None:
    """TPR - FPR. Exactly 0.0 for EVERY constant classifier.

    `always_flag` scores TPR=1, FPR=1 -> J=0; `never_flag` scores 0-0 -> J=0.
    That property is the whole reason this is the primary metric: it makes
    "flag everything and claim perfect recall" arithmetically worthless,
    rather than something prose has to argue against.
    """
    if not (tp + fn) or not (fp + tn):
        return None
    return round(tp / (tp + fn) - fp / (fp + tn), 4)


def _detector_stats(
    sel: list[dict[str, Any]], flag: Any, score: Any = None
) -> dict[str, Any]:
    """Score one detector over one slice.

    `flag` maps a row to a binary decision — that is what the shipped
    verdict actually emits, and what J / Fisher / precision grade.
    `score`, when given, maps a row to a CONTINUOUS quantity, graded
    separately by AUROC. The two answer different questions: J asks
    "is the decision this detector makes better than a coin?", AUROC
    asks "is there information in the underlying quantity that a
    better-chosen threshold could reach?". A detector can score J≈0
    and AUROC>0.5 — that combination means the signal is real but the
    operating point is wrong, which is a different repair than "the
    signal isn't there".
    """
    tp = sum(1 for r in sel if r["truth"] == "false" and flag(r))
    fn = sum(1 for r in sel if r["truth"] == "false" and not flag(r))
    fp = sum(1 for r in sel if r["truth"] == "still_true" and flag(r))
    tn = sum(1 for r in sel if r["truth"] == "still_true" and not flag(r))
    stats_auroc = None
    auroc_p = None
    auroc_flagged = None
    if score is not None:
        pos = [float(score(r)) for r in sel if r["truth"] == "false"]
        neg = [float(score(r)) for r in sel if r["truth"] == "still_true"]
        stats_auroc = auroc(pos, neg)
        auroc_p = auroc_permutation_p(pos, neg)
        # AUROC AMONG THE ALREADY-FLAGGED. Plain AUROC is inflated by the
        # easy half of the job — separating flagged from unflagged, which
        # the binary decision already did. The question a user actually
        # faces is "of the 654 things you flagged, which should I look at
        # first?", and only this number answers it. Reporting the plain
        # figure alone would be choosing the flattering half, in exactly
        # the sense this module's docstring forbids.
        flagged = [r for r in sel if flag(r)]
        auroc_flagged = auroc(
            [float(score(r)) for r in flagged if r["truth"] == "false"],
            [float(score(r)) for r in flagged if r["truth"] == "still_true"],
        )
    return {
        "auroc": stats_auroc,
        "auroc_p": auroc_p,
        "auroc_among_flagged": auroc_flagged,
        "n": len(sel),
        "actually_false": tp + fn,
        "base_rate": _rate(tp + fn, len(sel)),
        "flag_rate": _rate(tp + fp, len(sel)),
        "unflagged_stale_rate": _rate(fn, tp + fn),
        "false_alarm_rate": _rate(fp, fp + tn),
        "precision": _rate(tp, tp + fp),
        # Never report J without these two beside it. J says whether the
        # detector beats a coin; p says whether that margin is real; and
        # alerts_per_catch is what the user actually lives with.
        "youden_j": youden_j(tp, fn, fp, tn),
        "fisher_p": fisher_one_sided(tp, fn, fp, tn),
        "alerts_per_catch": round((tp + fp) / tp, 1) if tp else None,
    }


# Reference classifiers. Any detector that cannot beat the two constants
# has not been shown to work, however good its recall looks in isolation —
# and any detector that MATCHES `oracle_replica` has not been shown to work
# either, for the opposite reason.
BASELINES: dict[str, Any] = {
    "always_flag": lambda r: True,
    "never_flag": lambda r: False,
    # THE CEILING, MADE VISIBLE. This peeks at the answer, so it scores a
    # perfect J = 1.000 by construction. It is printed beside the real
    # detectors because the claim-level detector also reaches ~1.000, and
    # a reader has to be able to see in one glance that this is a
    # TRIVIALLY REACHABLE ceiling on this corpus rather than a hard-won
    # result. The window's diff IS the transformation from t0 to t1, and
    # the oracle's question ("is the symbol still defined at t1?") is
    # nearly decidable from the hunks alone — so a hunk-level detector
    # approaching 1.0 is evidence about the BENCHMARK's claim classes,
    # not evidence that the product's problem is solved.
    "oracle_replica": lambda r: r["truth"] == "false",
}


@contextlib.contextmanager
def memoized_git_reads() -> Generator[dict[str, int]]:
    """Cache the whole-history git reads for the duration of one run.

    WHY THIS IS NECESSARY, MEASURED RATHER THAN ASSUMED. Profiling the
    60-day window found 98.5% of the harness in a single call:
    `verdict_for` invokes the shipped `compute_commit_drift` once per
    row, which calls `commit_author_timestamps(cwd)` — `git log
    --format=%aI HEAD` over the repository's ENTIRE history — then sorts
    it and parses a datetime per commit. When the count is positive it
    additionally pays `repo_toplevel` and a path-filtered full-history
    log. So the cost law is

        T ~= files_in_subdir x TOTAL_COMMITS_IN_REPO_HISTORY x 1e-3 s

    which does not scale with the window, the diff, or the claim classes.
    It scales with how much HISTORY a repository has — precisely the axis
    a corpus of established projects maximises. At 500 files and 30,000
    commits that is hours per arm, and it is why the multi-repo corpus
    needs this before it needs anything else.

    WHY IT DOES NOT COMPROMISE WHAT IS BEING GRADED. Every cached
    function is a PURE READ of git state that cannot change while the
    bench runs — both window ends are pinned commits, and nothing here
    writes to the repository. None of `compute_staleness_verdict`,
    `compute_commit_drift`, `compute_verification_status`,
    `detect_path_drift` or `resolve_commit_drift_count` is touched, so
    the claim that the function under test is the SHIPPED one survives
    intact. The cache is installed on `bettermemory.verify`'s own module
    globals — the names its callers resolve at call time — and removed
    again on exit, so it cannot leak into anything else in the process.

    `tests/test_bench_rot.py` pins the only guarantee that matters: a
    memoized and an unmemoized run of the same pinned window produce
    identical reports. A speedup that changed a number would be a defect,
    not an optimisation.
    """
    from bettermemory import verify as _verify

    calls = {"hits": 0, "misses": 0}
    originals = {
        name: getattr(_verify, name)
        for name in (
            "commit_author_timestamps",
            "commit_author_timestamps_touching_pathspecs",
            "repo_toplevel",
            "repo_toplevel_and_head",
            "resolve_repo_pathspecs",
        )
    }

    def _memoize(func: Any) -> Any:
        cache: dict[Any, Any] = {}

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Lists are unhashable and every one of these takes at most a
            # list of pathspecs, so freeze them positionally.
            key = (
                tuple(tuple(a) if isinstance(a, list) else a for a in args),
                tuple(sorted(kwargs.items())),
            )
            if key in cache:
                calls["hits"] += 1
                return cache[key]
            calls["misses"] += 1
            cache[key] = func(*args, **kwargs)
            return cache[key]

        return wrapper

    # ONE PASS INSTEAD OF ONE PER PATH. Plain memoization cannot help the
    # path-filtered log, because every claim cites a DIFFERENT path, so
    # every call is a cache miss: `git log --format=%aI HEAD -- <one file>`
    # walks the repository's whole history, once per file. On scipy — 564
    # files, ~35k commits — that is hours, and it is the reason the first
    # corpus run had to be killed.
    #
    # The same information comes from a single `--name-only` pass over the
    # history, indexed by path. Same source (`%aI` author dates, same
    # HEAD), same answers, one walk instead of hundreds. `resolve_
    # commit_drift_count` is untouched and still does the bisect, so the
    # shipped policy is unchanged; only the query plan is.
    path_index: dict[Path, dict[str, list[datetime]] | None] = {}

    def _build_index(cwd: Path) -> dict[str, list[datetime]] | None:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(cwd),
                "log",
                f"--format={_COMMIT_MARK}%aI",
                "--name-only",
                "--no-renames",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode != 0:
            return None
        index: dict[str, list[datetime]] = {}
        stamp: datetime | None = None
        for line in out.stdout.split("\n"):
            if line.startswith(_COMMIT_MARK):
                try:
                    stamp = datetime.fromisoformat(line[1:].strip())
                except ValueError:
                    stamp = None
                continue
            path = line.strip()
            if path and stamp is not None:
                index.setdefault(path, []).append(stamp)
        return index

    def _touching(
        cwd: Path | None, pathspecs: Any, *, toplevel: Path | None = None
    ) -> list[datetime] | None:
        if cwd is None:
            return originals["commit_author_timestamps_touching_pathspecs"](
                cwd, pathspecs, toplevel=toplevel
            )
        if cwd not in path_index:
            path_index[cwd] = _build_index(cwd)
        index = path_index[cwd]
        if index is None:
            # Never under-count on infrastructure failure: fall back to the
            # shipped implementation rather than inventing an empty answer.
            return originals["commit_author_timestamps_touching_pathspecs"](
                cwd, pathspecs, toplevel=toplevel
            )
        calls["hits"] += 1
        stamps: list[datetime] = []
        for spec in pathspecs:
            exact = index.get(spec)
            if exact is not None:
                # A path is a file OR a directory in git, never both, so an
                # exact hit needs no prefix scan. Skipping it matters: the
                # scan is O(paths in history) and this is the common case,
                # which made the first attempt slower than the per-path git
                # calls it replaced.
                stamps.extend(exact)
                continue
            prefix = spec.rstrip("/") + "/"
            for path, values in index.items():
                if path.startswith(prefix):
                    stamps.extend(values)
        # Sort on the instant, not the datetime. `%aI` keeps each author's
        # own UTC offset, so the stamps carry thousands of DISTINCT tzinfo
        # objects (17,100 in a sample of scipy's) and CPython's
        # same-tzinfo fast path never fires — every one of the O(n log n)
        # comparisons then makes a Python-level `utcoffset()` call. One key
        # per element instead: 12,958 of these sorts were 84s of a 150s
        # scipy run; measured 7x faster on that data with the ordering
        # bit-for-bit identical (both are the absolute instant, and sort
        # stability breaks ties the same way).
        return sorted(stamps, key=lambda stamp: stamp.timestamp())

    try:
        for name, func in originals.items():
            setattr(_verify, name, _memoize(func))
        # Memoized too, not just replaced: the index makes one CALL cheap,
        # memoization makes the thousands of repeat calls free.
        setattr(
            _verify, "commit_author_timestamps_touching_pathspecs", _memoize(_touching)
        )
        yield calls
    finally:
        for name, func in originals.items():
            setattr(_verify, name, func)


def collect_rows(
    repo: Path, subdir: str, t0: str, t1: str, origin_repo: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Everything one repository contributes to the corpus.

    Split out from `main` so a multi-repo corpus can POOL rows rather than
    average per-repo rates. The distinction matters: averaging rates gives
    a ten-claim repository the same weight as a thousand-claim one, and
    the significance tests would then be computed on a contingency table
    that describes no actual population. Pooling keeps every claim one
    observation, which is what Fisher and the permutation test assume.

    Each row is tagged with its repo so per-repo breakdowns stay
    available — an aggregate that cannot be decomposed hides exactly the
    kind of single-repo artifact this corpus exists to escape.
    """
    # The parse cache is keyed by tree root, so entries from the previous
    # repository can never be READ here — but they would sit resident for
    # the whole corpus. Dropping them per repo keeps the ceiling at one
    # repository's files instead of thirty.
    _TOPLEVEL_CACHE.clear()
    commits_touching = commit_counts_touching(repo, t0, t1, subdir)
    index = build_binding_index(window_diff_text(repo, t0, t1, subdir))

    workdir = Path(tempfile.mkdtemp(prefix="bm-rot-"))
    tree0, tree1 = workdir / "t0", workdir / "t1"
    rows: list[dict[str, Any]] = []
    claims: list[Claim] = []
    never_true: list[Claim] = []
    unresolved_citations = empty_anchor_literals = 0
    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "worktree",
                "add",
                "--detach",
                "-q",
                str(tree0),
                t0,
            ],
            check=True,
            capture_output=True,
        )
        # MATERIALISE t1 TOO, rather than labelling against whatever is
        # checked out. Reading the live working tree was a silent
        # corruption waiting to happen: `--t1` moved the reported sha and
        # the diff range while the oracle kept grading the developer's
        # current checkout, so a pinned window could be scored against the
        # wrong tree with no error anywhere.
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "worktree",
                "add",
                "--detach",
                "-q",
                str(tree1),
                t1,
            ],
            check=True,
            capture_output=True,
        )
        claims = extract_claims(tree0, subdir)
        if not claims:
            # A WRONG `--subdir` IS OTHERWISE A SILENT NO-OP, and that is
            # the most dangerous failure mode in a multi-repo run.
            # `rglob` on a missing directory returns [] without raising,
            # `_detector_stats` on an empty slice returns all-None, and
            # the report prints a complete, well-formed table with n = 0
            # and "n/a" everywhere. A repository that contributed nothing
            # would look exactly like one that contributed cleanly.
            raise ValueError(
                f"no claims extracted from {subdir!r} at {t0[:12]} in {repo} — "
                "the subdir is wrong, empty, or holds no parseable Python"
            )

        # DROP CLAIMS THAT WERE NEVER TRUE. A module that rebinds a
        # constant (`X = 1` then `X = 2`) yields one claim per binding,
        # but `label_claim` returns on the FIRST matching assignment — so
        # the second claim reads `false` against its OWN t0 tree. A claim
        # that was already false before the window opened is not drift,
        # it is an extraction artifact, and counting it inflates the
        # positive base rate with rot that never happened. Counted rather
        # than silently filtered: if this number is ever large, the
        # extractor needs fixing instead of a filter.
        never_true = [c for c in claims if label_claim(c, tree0) == "false"]
        dropped = set(never_true)
        claims = [c for c in claims if c not in dropped]

        # The basis arms stamp the memory at t0's COMMIT instant — the
        # moment the verified tree state existed on its branch — and read
        # it with HEAD at t1, which the materialised t1 worktree is.
        t0_instant = datetime.fromisoformat(
            _git(repo, "show", "-s", "--format=%cI", t0)
        )
        with memoized_git_reads():
            rows, unresolved_citations, empty_anchor_literals = _score_claims(
                claims,
                repo=repo,
                tree1=tree1,
                origin_repo=origin_repo,
                commits_touching=commits_touching,
                index=index,
                t0=t0,
                t0_instant=t0_instant,
            )
    finally:
        for tree in (tree0, tree1):
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", str(tree)],
                check=False,
                capture_output=True,
            )
    return rows, _repo_meta(
        repo=repo,
        subdir=subdir,
        t0=t0,
        t1=t1,
        origin_repo=origin_repo,
        claims=claims,
        rows=rows,
        commits_touching=commits_touching,
        index=index,
        never_true=len(never_true),
        unresolved_citations=unresolved_citations,
        empty_anchor_literals=empty_anchor_literals,
    )


def _score_claims(
    claims: list[Claim],
    *,
    repo: Path,
    tree1: Path,
    origin_repo: str,
    commits_touching: dict[str, int],
    index: dict[str, Any],
    t0: str,
    t0_instant: datetime,
) -> tuple[list[dict[str, Any]], int, int]:
    """Grade every claim under every arm. Pure scoring, no git setup."""
    rows: list[dict[str, Any]] = []
    unresolved_citations = 0
    empty_anchor_literals = 0
    for claim in claims:
        truth = label_claim(claim, tree1)
        for mode, absolute, anchored, basis in (
            ("drift_only_relative_cite", False, False, None),
            ("drift_only_absolute_cite", True, False, None),
            ("shipped_default", False, False, None),
            ("drift_only_relative_cite_anchored", False, True, None),
            ("drift_only_relative_cite_author_date", False, False, "author-date"),
            ("drift_only_relative_cite_reachability", False, False, "reachability"),
        ):
            verdict, missing, commits = verdict_for(
                claim,
                repo=repo,
                tree1=tree1,
                origin_repo=origin_repo,
                commits_touching=commits_touching,
                calendar_fresh=(mode != "shipped_default"),
                absolute=absolute,
                anchored=anchored,
                since=t0_instant if basis is not None else None,
                anchor_sha=t0 if basis == "reachability" else None,
            )
            # The claim-level channels come from the RENDERED BODY, not the
            # Claim object — the firewall that stops this from becoming a
            # second copy of the oracle. An unparseable body degrades to the
            # file-level count and is counted, never silently absorbed.
            cite = parse_claim_citation(claim.body(repo if absolute else None), repo)
            if cite is None:
                unresolved_citations += 1
                claim_drift = {
                    "cite_commits": commits,
                    "cite_edit_lines": 0,
                    "strict": commits > 0,
                    "weak": commits > 0,
                }
            else:
                claim_drift = claim_level_drift(cite, index)
                if (
                    mode == _MODES[0]
                    and cite.kind == "literal"
                    and "\\n" in cite.value
                    and not anchors_from_value(cite.value)
                ):
                    empty_anchor_literals += 1
            rows.append(
                {
                    "repo": origin_repo or str(repo),
                    "kind": claim.kind,
                    "mode": mode,
                    "truth": truth,
                    "flagged": verdict != "fresh",
                    "path_drift": missing,
                    "commit_drift": commits,
                    "cite_commits": claim_drift["cite_commits"],
                    "cite_edit_lines": claim_drift["cite_edit_lines"],
                    "claim_strict": claim_drift["strict"],
                    "claim_weak": claim_drift["weak"],
                }
            )

    return rows, unresolved_citations, empty_anchor_literals


def _repo_meta(
    *,
    repo: Path,
    subdir: str,
    t0: str,
    t1: str,
    origin_repo: str,
    claims: list[Claim],
    rows: list[dict[str, Any]],
    commits_touching: dict[str, int],
    index: dict[str, Any],
    never_true: int,
    unresolved_citations: int,
    empty_anchor_literals: int,
) -> dict[str, Any]:
    """Per-repo provenance, carried beside the numbers it explains."""
    return {
        "repo": origin_repo or str(repo),
        "subdir": subdir,
        # Full shas, not abbreviations: the window has to be reconstructable
        # from the published artifact alone.
        "t0": t0,
        "t1": t1,
        "claims": len(claims),
        # Claims discarded because they were ALREADY FALSE at t0 — an
        # extraction artifact, not drift. Published because a filter whose
        # size is unreported is indistinguishable from one tuned to taste.
        "claims_false_at_t0": never_true,
        "files_changed_in_window": len(commits_touching),
        "diff_index": {
            "commits": index["commits"],
            "hunks": index["hunks"],
            "files": len(index["files"]),
            "bindings": len(index["bindings"]),
            "deleted_paths": len(index["deleted"]),
            # A non-zero mismatch count means the -U0 parse desynchronised
            # and every claim-level number below is suspect.
            "parse_mismatches": index["parse_mismatches"],
        },
        "citation_resolution": {
            "unresolved": unresolved_citations,
            # 100% by construction here — every body is machine-generated and
            # names its target in backticks. Real memory bodies are not like
            # that (bench/claims.py measures the checkable/judgement split at
            # roughly 64/36), so real-world performance is bounded by
            # J_resolved x resolution_rate and only the first factor is
            # measured on this corpus.
            "resolved_rate": _rate(len(rows) - unresolved_citations, len(rows)),
            "empty_anchor_literals": empty_anchor_literals,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Grade the staleness verdict against git-derived ground truth."
    )
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--subdir", default="src")
    parser.add_argument("--days", type=int, default=60, help="How far back t0 sits.")
    parser.add_argument(
        "--t0",
        default=None,
        help=(
            "Pin t0 to an explicit commit. Without this, t0 is resolved from "
            "--days against the WALL CLOCK and therefore slides between runs, "
            "which silently confounds any before/after comparison."
        ),
    )
    parser.add_argument(
        "--t1",
        default=None,
        help=(
            "Pin t1 to an explicit commit. Defaults to HEAD, which moves every "
            "time you commit — pinning BOTH ends is what makes a published run "
            "reproducible."
        ),
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    # BOTH ends are reported as full shas, pinned or not: a result whose
    # window cannot be reconstructed is not reproducible. `--until=N days
    # ago` moves every time the clock does, and HEAD moves every time the
    # author commits — the second one bit during this benchmark's own
    # development, when a re-run differed from the published JSON in exactly
    # one field because a commit had landed in between.
    if args.t0:
        t0 = _git(repo, "rev-parse", args.t0)
    else:
        t0 = _git(repo, "log", f"--until={args.days} days ago", "-1", "--format=%H")
    t1 = _git(repo, "rev-parse", args.t1 or "HEAD")
    if not t0:
        print(f"no commit {args.days} days back in {repo}", file=sys.stderr)
        return 1
    origin_repo = _git(repo, "config", "--get", "remote.origin.url")

    rows, meta = collect_rows(repo, args.subdir, t0, t1, origin_repo)

    report: dict[str, Any] = {
        **meta,
        "t0_pinned": bool(args.t0),
        "t1_pinned": bool(args.t1),
        # The oracle is `ast.parse`, so the interpreter is part of the
        # measuring instrument: a file that parses on one version and not
        # on another changes labels without any code changing.
        "python": sys.version.split()[0],
        "git": _git(repo, "--version").replace("git version ", ""),
        "days": args.days,
        "modes": {},
    }
    for mode in _MODES:
        block: dict[str, Any] = {}
        for kind in (*CLAIM_CLASSES, "ALL"):
            sel = [
                r
                for r in rows
                if r["mode"] == mode and (kind == "ALL" or r["kind"] == kind)
            ]
            stats = _detector_stats(
                sel, lambda r: r["flagged"], score=lambda r: r["commit_drift"]
            )
            stats["path_drift_flags"] = sum(1 for r in sel if r["path_drift"] > 0)
            stats["max_commit_drift"] = max((r["commit_drift"] for r in sel), default=0)
            block[kind] = stats
        report["modes"][mode] = block

    # The claim-level detectors, scored on the SAME rows as the incumbent so
    # the comparison is like-for-like. `file_level` is the shipped signal
    # repeated here as the control: the new detectors have to beat it on
    # these exact claims, not on a differently-sliced corpus.
    detectors: dict[str, Any] = {}
    for name, flag, score in (
        ("file_level_incumbent", lambda r: r["flagged"], lambda r: r["commit_drift"]),
        (
            "claim_level_strict",
            lambda r: r["claim_strict"],
            lambda r: r["cite_commits"],
        ),
        ("claim_level_weak", lambda r: r["claim_weak"], lambda r: r["cite_commits"]),
    ):
        block = {}
        for kind in (*CLAIM_CLASSES, "ALL"):
            sel = [
                r
                for r in rows
                if r["mode"] == _MODES[0] and (kind == "ALL" or r["kind"] == kind)
            ]
            block[kind] = _detector_stats(sel, flag, score=score)
        detectors[name] = block
    report["detectors"] = detectors

    # Score the reference classifiers on the same claims. If the shipped
    # detector cannot beat the constants, the recall number is an artifact of
    # its flag rate and not evidence that it works — and if a detector merely
    # MATCHES `oracle_replica`, it has reached a ceiling this corpus makes
    # cheap rather than solved the problem.
    baselines: dict[str, Any] = {}
    for name, flag in BASELINES.items():
        sel = [r for r in rows if r["mode"] == _MODES[0]]
        baselines[name] = _detector_stats(sel, flag)
    report["baselines"] = baselines

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    def _table(block: dict[str, Any]) -> None:
        print(
            "| class   |    n | false | flagged | unflagged_stale | prec "
            "|      J | Fisher p | alerts/catch |  AUROC | AUROC p | AUROC|flag |"
        )
        print(
            "|---------|------|-------|---------|-----------------|------"
            "|--------|----------|--------------|--------|---------|------------|"
        )
        for kind in (*CLAIM_CLASSES, "ALL"):
            s = block[kind]

            def pc(v: float | None) -> str:
                return "  n/a" if v is None else f"{100 * v:>4.0f}%"

            def num(v: float | None, width: int, places: int) -> str:
                return "n/a".rjust(width) if v is None else f"{v:>{width}.{places}f}"

            print(
                f"| {kind:<7} | {s['n']:>4} | {s['actually_false']:>5} "
                f"| {pc(s['flag_rate'])}   | {pc(s['unflagged_stale_rate'])}"
                f"            | {pc(s['precision'])} "
                f"| {num(s['youden_j'], 6, 3)} | {num(s['fisher_p'], 8, 3)} "
                f"| {num(s['alerts_per_catch'], 12, 1)} "
                f"| {num(s['auroc'], 6, 3)} | {num(s['auroc_p'], 7, 5)} "
                f"| {num(s['auroc_among_flagged'], 10, 3)} |"
            )
        print()

    print(f"repo {report['repo']}")
    ends = []
    if not report["t0_pinned"]:
        ends.append("t0 CLOCK-RELATIVE")
    if not report["t1_pinned"]:
        ends.append("t1 = HEAD")
    pinned = (
        "both ends pinned" if not ends else ", ".join(ends) + " — slides between runs"
    )
    print(
        f"t0 {report['t0'][:12]} -> t1 {report['t1'][:12]}  ({args.days} days, {pinned})"
    )
    idx = report["diff_index"]
    print(
        f"{report['claims']} claims, "
        f"{report['files_changed_in_window']} files changed in window, "
        f"{idx['commits']} commits / {idx['hunks']} hunks indexed"
    )
    if idx["parse_mismatches"]:
        print(
            f"WARNING: {idx['parse_mismatches']} hunk parse mismatches — "
            "claim-level numbers are UNSAFE"
        )
    cr = report["citation_resolution"]
    print(
        f"citations resolved {cr['resolved_rate']} "
        f"({cr['unresolved']} unresolved), "
        f"{cr['empty_anchor_literals']} literals with no usable anchor\n"
    )

    for mode, block in report["modes"].items():
        print(f"[{mode}]")
        _table(block)

    print("[detectors — same claims, same arm, like for like]")
    for name, block in report["detectors"].items():
        print(f"  {name}")
        _table(block)

    print("[reference classifiers on the same claims]")
    for name, s in report["baselines"].items():
        jv = "n/a" if s["youden_j"] is None else f"{s['youden_j']:.3f}"
        print(
            f"  {name:<15} flag_rate={s['flag_rate']}  "
            f"unflagged_stale={s['unflagged_stale_rate']}  J={jv}"
        )
    print(
        "\n  oracle_replica peeks at the label. A detector that matches it has "
        "reached\n  a ceiling this corpus makes cheap — not solved the problem."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
