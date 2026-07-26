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
import json
import subprocess
import sys
import tempfile
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
_MODES = ("drift_only_relative_cite", "drift_only_absolute_cite", "shipped_default")


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
        the gap between them IS a finding about the product.
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
        except SyntaxError:
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
    try:
        parsed = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return "false"
    if claim.kind == "symbol":
        for node in parsed.body:
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == claim.name
            ):
                return "still_true"
        return "false"
    for node in parsed.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == claim.name:
                return (
                    "still_true" if _literal_of(node.value) == claim.value else "false"
                )
    return "false"


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


def verdict_for(
    claim: Claim,
    *,
    repo: Path,
    origin_repo: str,
    commits_touching: dict[str, int],
    calendar_fresh: bool,
    absolute: bool,
) -> tuple[str, int, int]:
    """Return (verdict, path_drift_missing, commit_drift_count)."""
    body = claim.body(repo if absolute else None)
    drift = detect_path_drift(body)
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
    verdict = compute_staleness_verdict(
        verification=verification,
        path_drift_missing=len(drift.missing),
        commit_drift_count=count,
    )
    return verdict, len(drift.missing), count


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Grade the staleness verdict against git-derived ground truth."
    )
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--subdir", default="src")
    parser.add_argument("--days", type=int, default=60, help="How far back t0 sits.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    t0 = _git(repo, "log", f"--until={args.days} days ago", "-1", "--format=%H")
    t1 = _git(repo, "rev-parse", "HEAD")
    if not t0:
        print(f"no commit {args.days} days back in {repo}", file=sys.stderr)
        return 1
    origin_repo = _git(repo, "config", "--get", "remote.origin.url")

    changed = _git(repo, "diff", "--name-only", t0, t1, "--", args.subdir).splitlines()
    commits_touching = {p: 1 for p in changed if p}

    workdir = Path(tempfile.mkdtemp(prefix="bm-rot-"))
    tree0 = workdir / "t0"
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
        claims = extract_claims(tree0, args.subdir)
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(tree0)],
            check=False,
            capture_output=True,
        )

    rows: list[dict[str, Any]] = []
    for claim in claims:
        truth = label_claim(claim, repo)
        for mode, absolute in (
            ("drift_only_relative_cite", False),
            ("drift_only_absolute_cite", True),
            ("shipped_default", False),
        ):
            verdict, missing, commits = verdict_for(
                claim,
                repo=repo,
                origin_repo=origin_repo,
                commits_touching=commits_touching,
                calendar_fresh=(mode != "shipped_default"),
                absolute=absolute,
            )
            rows.append(
                {
                    "kind": claim.kind,
                    "mode": mode,
                    "truth": truth,
                    "flagged": verdict != "fresh",
                    "path_drift": missing,
                    "commit_drift": commits,
                }
            )

    report: dict[str, Any] = {
        "repo": origin_repo or str(repo),
        "t0": t0[:12],
        "t1": t1[:12],
        "days": args.days,
        "claims": len(claims),
        "files_changed_in_window": len(commits_touching),
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
            false_claims = [r for r in sel if r["truth"] == "false"]
            true_claims = [r for r in sel if r["truth"] == "still_true"]
            flagged = [r for r in sel if r["flagged"]]
            block[kind] = {
                "n": len(sel),
                "actually_false": len(false_claims),
                "base_rate": _rate(len(false_claims), len(sel)),
                "flag_rate": _rate(len(flagged), len(sel)),
                "path_drift_flags": sum(1 for r in sel if r["path_drift"] > 0),
                "unflagged_stale_rate": _rate(
                    sum(1 for r in false_claims if not r["flagged"]), len(false_claims)
                ),
                "false_alarm_rate": _rate(
                    sum(1 for r in true_claims if r["flagged"]), len(true_claims)
                ),
                "precision": _rate(
                    sum(1 for r in flagged if r["truth"] == "false"), len(flagged)
                ),
            }
        report["modes"][mode] = block

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"repo {report['repo']}")
        print(f"t0 {report['t0']} -> t1 {report['t1']}  ({args.days} days)")
        print(
            f"{report['claims']} claims, "
            f"{report['files_changed_in_window']} files changed in window\n"
        )
        for mode, block in report["modes"].items():
            print(f"[{mode}]")
            print(
                "| class   |    n | false | base | flagged | unflagged_stale "
                "| false_alarm | prec |"
            )
            print(
                "|---------|------|-------|------|---------|-----------------"
                "|-------------|------|"
            )
            for kind in (*CLAIM_CLASSES, "ALL"):
                s = block[kind]

                def pc(v: float | None) -> str:
                    return "  n/a" if v is None else f"{100 * v:>4.0f}%"

                print(
                    f"| {kind:<7} | {s['n']:>4} | {s['actually_false']:>5} "
                    f"| {pc(s['base_rate'])} | {pc(s['flag_rate'])}   "
                    f"| {pc(s['unflagged_stale_rate'])}            "
                    f"| {pc(s['false_alarm_rate'])}       | {pc(s['precision'])} |"
                )
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
