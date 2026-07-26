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
import math
import random
import re
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
# no `.gitattributes`, so git falls back to its default funcname heuristic,
# which yields headings like `class Store:` (136x in the 60-day window),
# `__all__ = [` (61x) and `def add_subparser(` (84x). Every method-body edit
# inside `Store` reports `class Store:`, so a heading-keyed detector is a
# body-churn amplifier wearing a def-shaped label. Column-0 matching on the
# changed lines themselves is what excludes methods, nested defs, keyword
# arguments (`foo(TIMEOUT=30)`) and dict entries (`"TIMEOUT": 30`) — that
# discipline is the entire difference between this and a name-grep.

# Record separator for the `git log` streams. A control character rather
# than a text marker so it can never collide with source content — note
# this makes the stream binary to `grep` and friends when debugging.
_COMMIT_MARK = "\x01"

_DEF_RE = re.compile(r"^(?:async[ \t]+)?(?:def|class)[ \t]+([A-Za-z_]\w*)[ \t]*[(\[:]")
_ASSIGN_RE = re.compile(
    r"^([A-Za-z_]\w*)[ \t]*(?::[^=\n]+)?"
    r"(?:\+|-|\*|/|//|%|\*\*|>>|<<|&|\^|\|)?=(?!=)"
)
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# Claim-body templates, parsed back out of the rendered string. This is the
# firewall: whatever the detector knows about a claim, it learned here, from
# text a real memory body would also carry.
_CITE_PATH = re.compile(r"^The module `([^`]+)` is part of this package\.$")
_CITE_SYMBOL = re.compile(r"^`([^`]+)` is defined at the top level of `([^`]+)`\.$")
_CITE_LITERAL = re.compile(r"^`([^`]+)` in `([^`]+)` is set to `(.+)`\.$", re.DOTALL)

# An anchor line must carry this many non-whitespace characters to be treated
# as a content address. Short interior lines (`}`, `],`, `"name",`) recur all
# over a file and would attribute unrelated edits to the literal.
_MIN_ANCHOR_CHARS = 12


@dataclass(frozen=True)
class Citation:
    """What the detector is allowed to know about a claim."""

    kind: str
    rel_path: str
    name: str
    value: str


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


def _binding_token(content: str) -> tuple[str, str] | None:
    """Map a changed line to at most one column-0 binding, or None.

    No `lstrip`. An indented `def` is a method or a nested function and is
    not what a top-level-symbol claim asserts — `label_claim` only walks
    `parsed.body`, and `test_nested_definitions_are_not_top_level_claims`
    pins that. Matching indented lines here would flag every method edit
    in a class as drift on the class's own claim.
    """
    match = _DEF_RE.match(content)
    if match:
        return ("def", match.group(1))
    match = _ASSIGN_RE.match(content)
    if match:
        return ("assign", match.group(1))
    return None


def _rhs_repr(content: str) -> str | None:
    """The assignment's right-hand side, normalised to `repr()` form.

    Matching `Claim.value`, which is itself `repr(ast.literal_eval(...))`,
    is what makes the comparison type-sensitive: `30` and `30.0` must not
    compare equal, because `test_changed_literal_is_drift` treats that
    change as genuine drift.
    """
    _, sep, rhs = content.partition("=")
    if not sep:
        return None
    try:
        return repr(ast.literal_eval(rhs.strip()))
    except Exception:
        return None


def string_fragment(content: str) -> str | None:
    """The decoded text of a source line that is a bare string literal.

    THE DIRECTION OF THE TEST IS THE WHOLE TRICK, and getting it backwards
    is what a first implementation does. The obvious move is to split the
    claimed value into lines and look for those lines in the diff. It
    finds almost nothing, and the reason is structural: Python's implicit
    concatenation means a long constant is written as

        DESC = (
            "one clause of the sentence "
            "and the next clause\\n"
        )

    so the value's LOGICAL lines and the file's PHYSICAL lines are
    different objects. A logical line routinely spans several physical
    ones, and a value with no `\\n` at all still occupies twelve lines of
    source. Measured on this corpus, whole-line anchors missed 12 of the
    20 literal claims that actually went false — every one of them a
    multi-line tool description.

    So invert it: decode each CHANGED PHYSICAL LINE back to the text it
    contributes, and ask whether that text appears ANYWHERE in the claimed
    value. `ast.literal_eval` rather than string surgery because the
    source carries escapes (`\\"`, `\\n`) the value does not — comparing
    raw source text against a decoded value fails on precisely the lines
    that contain interesting content.

    Returns None for a line that is not a self-contained string literal.
    """
    stripped = content.strip().rstrip(",")
    # A trailing `)` closes the enclosing parenthesised concatenation, not
    # the string; leading `(` opens it. Neither belongs to the literal.
    stripped = stripped.removesuffix(")").removeprefix("(").strip()
    if not stripped or stripped[0] not in "\"'":
        return None
    try:
        decoded = ast.literal_eval(stripped)
    except Exception:
        return None
    return decoded if isinstance(decoded, str) else None


def anchors_from_value(value: str) -> tuple[str, ...]:
    """Whole-line content addresses for a multi-line literal.

    Kept alongside `string_fragment` because it catches the case that one
    misses: a value whose physical and logical lines DO coincide (a
    triple-quoted block), where the changed line is not a self-contained
    string literal and so decodes to nothing.

    Derived from the claimed VALUE rather than from the t0 tree, which is
    what keeps the firewall intact: the value is in the memory body, so a
    production implementation has the same material. Lines shorter than
    `_MIN_ANCHOR_CHARS` are dropped as non-distinctive.
    """
    try:
        literal = ast.literal_eval(value)
    except Exception:
        return ()
    if not isinstance(literal, str) or "\n" not in literal:
        return ()
    seen: dict[str, None] = {}
    for line in literal.split("\n"):
        stripped = line.strip()
        if len(stripped) >= _MIN_ANCHOR_CHARS:
            seen[stripped] = None
    return tuple(seen)


def build_binding_index(diff_text: str) -> dict[str, Any]:
    """Parse one `git log -p -U0` stream into a claim-agnostic index.

    THE SIGNATURE IS THE GUARANTEE: one argument, the diff text. This
    function cannot see a claim, so it cannot be accused of looking one
    up. Every claim-specific decision happens later, against this index.

    At `-U0` a hunk contains exactly `b` removed and `d` added lines and
    no context, so consumption is exact rather than greedy, and the
    `minus == b and plus == d` check turns a malformed parse into a loud
    failure instead of a quietly under-counting detector. That matters
    because file content can itself contain `diff --git` and `@@` lines.
    """
    bindings: dict[tuple[str, str, str], dict[str, Any]] = {}
    changed_text: dict[str, dict[str, set[str]]] = {}
    changed_fragments: dict[str, dict[str, set[str]]] = {}
    deleted: set[str] = set()
    files: set[str] = set()
    commits: set[str] = set()
    hunks = 0
    mismatches = 0

    sha = ""
    path = ""
    lines = diff_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith(_COMMIT_MARK):
            sha = line[1:].strip()
            commits.add(sha)
            path = ""
            continue
        if line.startswith("diff --git "):
            path = ""
            continue
        if line.startswith("--- ") and not line.startswith("--- a/"):
            continue
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                # Deletion: the authoritative path is the a/ side, which
                # we already recorded when we saw it.
                if path:
                    deleted.add(path)
            else:
                path = target[2:] if target.startswith("b/") else target
                files.add(path)
            continue
        if line.startswith("--- a/"):
            path = line[6:].strip()
            files.add(path)
            continue
        if not line.startswith("@@"):
            continue
        match = _HUNK_RE.match(line)
        if not match or not path:
            continue
        hunks += 1
        removed = int(match.group(2) or 1)
        added = int(match.group(4) or 1)
        minus = plus = 0
        for _ in range(removed + added):
            if i >= len(lines):
                break
            body_line = lines[i]
            i += 1
            if body_line.startswith("\\"):
                continue
            if body_line.startswith("-"):
                minus += 1
                side = "removed"
            elif body_line.startswith("+"):
                plus += 1
                side = "added"
            else:
                # Not a hunk body line at -U0 — the stream is not shaped
                # the way this parser assumes. Back up and let the outer
                # loop resynchronise on the next header.
                i -= 1
                break
            content = body_line[1:]
            stripped = content.strip()
            if len(stripped) >= _MIN_ANCHOR_CHARS:
                changed_text.setdefault(path, {}).setdefault(stripped, set()).add(sha)
            fragment = string_fragment(content)
            if fragment is not None and len(fragment.strip()) >= _MIN_ANCHOR_CHARS:
                changed_fragments.setdefault(path, {}).setdefault(fragment, set()).add(
                    sha
                )
            token = _binding_token(content)
            if token is None:
                continue
            key = (path, token[0], token[1])
            entry = bindings.setdefault(
                key,
                {
                    "commits": set(),
                    "adds": 0,
                    "removes": 0,
                    "edit_lines": 0,
                    "rhs_added": set(),
                    "rhs_removed": set(),
                },
            )
            entry["commits"].add(sha)
            entry["edit_lines"] += 1
            if side == "added":
                entry["adds"] += 1
            else:
                entry["removes"] += 1
            if token[0] == "assign":
                rhs = _rhs_repr(content)
                if rhs is not None:
                    entry[f"rhs_{side}"].add(rhs)
        if minus != removed or plus != added:
            mismatches += 1

    return {
        "bindings": bindings,
        "changed_text": changed_text,
        "changed_fragments": changed_fragments,
        "deleted": deleted,
        "files": files,
        "commits": len(commits),
        "hunks": hunks,
        "parse_mismatches": mismatches,
    }


def claim_level_drift(cite: Citation, index: dict[str, Any]) -> dict[str, Any]:
    """Score one claim against the index. Two tiers, both reported.

    STRICT is the verdict channel: the binding NET-DISAPPEARED (more
    removals than re-additions), the asserted value was removed and not
    put back, a content anchor moved, or the file is gone. Every route by
    which `label_claim` can return "false" — rename, delete, de-top-level
    by indentation, move-and-re-export — puts the `def`/`class` line
    itself into a hunk, so narrowing this far costs no recall.

    WEAK is "the binding was touched at all". It is reported because the
    gap between the tiers IS the measurement: weak fires on signature
    reflows and in-file relocations that leave the claim true, and those
    are precisely the false alarms the file-level signal cannot tell
    apart from real drift.

    A BODY-ONLY EDIT IS DELIBERATELY NOT DRIFT. `label_claim` matches a
    definition by `.name` and never inspects its contents, so a body edit
    leaves the label `still_true` BY CONSTRUCTION — pinned by
    `test_pure_reformat_is_not_drift`. Counting body churn could
    therefore only manufacture false positives, never a catch.
    """
    path_gone = cite.rel_path in index["deleted"]
    empty: dict[str, Any] = {
        "commits": set(),
        "adds": 0,
        "removes": 0,
        "edit_lines": 0,
        "rhs_added": set(),
        "rhs_removed": set(),
    }
    anchor_commits = 0
    value_gone = False

    if cite.kind == "path":
        entry = empty
        strict = path_gone
        weak = path_gone
    elif cite.kind == "symbol":
        entry = index["bindings"].get((cite.rel_path, "def", cite.name), empty)
        strict = path_gone or (entry["removes"] - entry["adds"]) > 0
        weak = path_gone or bool(entry["commits"])
    else:
        entry = index["bindings"].get((cite.rel_path, "assign", cite.name), empty)
        value_gone = (
            cite.value in entry["rhs_removed"] and cite.value not in entry["rhs_added"]
        )
        per_file = index["changed_text"].get(cite.rel_path, {})
        hit: set[str] = set()
        for anchor in anchors_from_value(cite.value):
            hit |= per_file.get(anchor, set())
        # Substring containment, the direction that survives implicit
        # concatenation — see `string_fragment`.
        try:
            claimed = ast.literal_eval(cite.value)
        except Exception:
            claimed = None
        if isinstance(claimed, str):
            for fragment, shas in (
                index["changed_fragments"].get(cite.rel_path, {}).items()
            ):
                if fragment in claimed:
                    hit |= shas
        anchor_commits = len(hit)
        strict = path_gone or value_gone or anchor_commits > 0
        weak = path_gone or bool(entry["commits"]) or anchor_commits > 0

    return {
        "cite_commits": len(entry["commits"]) + anchor_commits,
        "cite_edit_lines": entry["edit_lines"],
        "anchor_commits": anchor_commits,
        "value_gone": value_gone,
        "strict": strict,
        "weak": weak,
    }


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
    commits_touching = commit_counts_touching(repo, t0, t1, subdir)
    index = build_binding_index(window_diff_text(repo, t0, t1, subdir))

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
        claims = extract_claims(tree0, subdir)
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(tree0)],
            check=False,
            capture_output=True,
        )

    rows: list[dict[str, Any]] = []
    unresolved_citations = 0
    empty_anchor_literals = 0
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

    meta = {
        "repo": origin_repo or str(repo),
        "subdir": subdir,
        # Full shas, not abbreviations: the window has to be reconstructable
        # from the published artifact alone.
        "t0": t0,
        "t1": t1,
        "claims": len(claims),
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
    return rows, meta


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
