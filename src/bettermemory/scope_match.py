"""Scope-mismatch detection for memory_write.

Same design family as `durability.py`: a write-time gate that flags a
candidate body whose declared `scopes` look out of step with what the
body talks about. Two heuristics:

1. **Project-name match**: the body cites the ``<name>`` portion of an
   existing ``projects:<name>`` scope as a word-boundary token, but
   that scope isn't in the declared scope list. Strong signal: the
   model is writing about Project Foo while only tagging the memory
   with ``tools``. The fix is usually to add the project scope.

2. **Project-root match**: the body cites a filesystem path under a
   project root that the store has previously associated with a
   ``projects:<name>`` scope (via memories' ``origin.cwd``). Catches
   the "I wrote a memory under projects:foo's tree but tagged it
   ``tools``" mistake. Quieter signal — paths are a noisy substrate
   — but useful when origin metadata is reliable.

The output is a `ScopeMismatchReport` carrying ``matches`` (the
specific evidence) and ``suggested_scopes`` (the deduplicated set of
scopes the writer should consider adding). When the suggested scope is
already declared we skip it: a multi-scope write that does carry the
relevant project tag is fine, the body just happens to also reference
another project legitimately (ports, dependencies, cross-cutting
notes).

Mirrors the `transient_warning` / `duplicate` write-time gates: the
caller can override via ``acknowledge_scope_mismatch=True`` on
`memory_write`, the override is logged, and the stats can be reviewed
in a future curation pass to see whether the heuristic is producing
too many false positives.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .models import Memory
from .verify import _fold_altsep


# Cap the per-write evidence list — a body that mentions five different
# projects is pathological enough that we don't need to enumerate every
# match for the response. The first hits are the most actionable; the
# tail just inflates the JSON.
_MAX_MATCHES = 5

# Project names that double as everyday English/dev nouns. Once a
# `projects:docs` scope exists, every body that says "inline docs" would
# bounce — far noisier than the 1-2 char names the length guard below
# already rejects. Same static-guard stance: skip the bare-token pass
# for these names and rely on the project-root pass, which stays
# precise for such repos (their filesystem root is still distinctive).
_NOISY_NAME_STOPLIST = frozenset(
    {
        "blog",
        "build",
        "config",
        "configs",
        "data",
        "doc",
        "docs",
        "note",
        "notes",
        "script",
        "scripts",
        "site",
        "src",
        "test",
        "tests",
    }
)


@dataclass(frozen=True)
class ScopeMismatchEntry:
    """One piece of evidence for a scope-mismatch.

    `kind` is ``"project_name"`` (a token in the body matched a known
    ``projects:<name>`` scope) or ``"project_root"`` (a path in the
    body falls under a directory associated with a known project
    scope). `evidence` is the matched substring (capped to keep the
    response bounded). `suggested_scope` is the scope the writer
    should consider adding.
    """

    kind: str
    evidence: str
    suggested_scope: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "evidence": self.evidence,
            "suggested_scope": self.suggested_scope,
        }


@dataclass(frozen=True)
class ScopeMismatchReport:
    """Aggregate verdict from `detect_scope_mismatch`.

    `has_mismatch` is the load-bearing field — `memory_write` branches
    on it to decide whether to gate the write or pass through.
    `matches` carries the evidence for the model so it can decide
    whether the suggestion is real or whether to override.
    `suggested_scopes` is the deduplicated, sorted set of scopes the
    matches imply.
    """

    matches: tuple[ScopeMismatchEntry, ...]
    suggested_scopes: tuple[str, ...]

    @property
    def has_mismatch(self) -> bool:
        return bool(self.matches)


def detect_scope_mismatch(
    *,
    body: str,
    declared_scopes: list[str],
    project_scopes: set[str],
    project_roots: dict[str, str],
) -> ScopeMismatchReport:
    """Scan `body` for evidence that `declared_scopes` is mis-tagged.

    `project_scopes` is the set of every ``projects:<name>`` scope the
    store has seen (active or tombstoned). `project_roots` maps each
    such scope to its inferred filesystem root prefix (the most
    common ``origin.cwd`` for memories under that scope). Either map
    being empty just disables the corresponding check; the function
    never raises.

    Returns a `ScopeMismatchReport`. An empty report (`has_mismatch`
    False) means no mismatch surfaced — the write should proceed.
    """
    declared = set(declared_scopes)
    matches: list[ScopeMismatchEntry] = []
    suggested: set[str] = set()

    if not body:
        return ScopeMismatchReport(matches=(), suggested_scopes=())

    # Pass 1: project-name token matches.
    #
    # For each `projects:<name>` scope, look for `<name>` as a
    # whole-token match in the body. The boundary semantics mirror the
    # project-root pass's trailing guard: alphanumerics plus `-`, `_`,
    # and a segment-continuing `.` extend a token (so `foo` must not
    # match inside `foo-bar` or `payments-api`'s `api` — those are
    # *different* projects that merely share characters), while `/`,
    # whitespace, and closing punctuation (including a sentence-final
    # `.`) are legitimate boundaries. Hyphens in the scope name match
    # any of `-`, `_`, `.` in the body: the scope grammar
    # (`models.py`'s `validate_scope`) only permits hyphens, so a repo
    # named `data_pipeline` can only ever be tagged
    # `projects:data-pipeline` and the body's natural spelling would
    # never match the literal tag. The loop is bounded by the number
    # of project scopes, which is small in practice (low tens).
    declared_root_spans = _declared_root_spans(body, declared, project_roots)
    for scope in sorted(project_scopes):
        if scope in declared:
            continue
        name = scope.split(":", 1)[1] if ":" in scope else ""
        if not name or len(name) < 3:
            # Two-character project names are too noisy to match safely.
            continue
        if name.lower() in _NOISY_NAME_STOPLIST:
            # Common-noun repo names fire on ordinary prose ("prefers
            # terse inline docs"); the project-root pass still covers
            # paths under such a repo's tree.
            continue
        token = "[-_.]".join(re.escape(seg) for seg in name.split("-"))
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_.-]){token}(?![A-Za-z0-9_-]|\.(?=\S))",
            re.IGNORECASE,
        )
        m: re.Match[str] | None = None
        for candidate in pattern.finditer(body):
            if any(
                lo <= candidate.start() and candidate.end() <= hi
                for lo, hi in declared_root_spans
            ):
                # The token sits inside a cited path that a declared
                # scope's root covers (nested project trees: the parent
                # name is a segment of every child path). The declared
                # tag already explains the reference.
                continue
            m = candidate
            break
        if m is None:
            continue
        evidence = _trim_evidence(body, m.start(), m.end())
        matches.append(
            ScopeMismatchEntry(
                kind="project_name",
                evidence=evidence,
                suggested_scope=scope,
            )
        )
        suggested.add(scope)
        if len(matches) >= _MAX_MATCHES:
            break

    # Pass 2: project-root path matches.
    #
    # Only run when we have headroom in the matches budget — a body
    # that already produced 5 project-name hits doesn't need more
    # evidence. We also short-circuit when no roots are available
    # (typical until enough memories have been written under
    # `projects:*` scopes for the inference to converge).
    if len(matches) < _MAX_MATCHES and project_roots:
        declared_roots = {project_roots[s] for s in declared if project_roots.get(s)}
        # Folded twin of `declared_roots` for the identity check below
        # only. The raw set keeps each declared root's ORIGINAL spelling
        # because `_declared_root_covers` probes the body verbatim with
        # it (and via `_home_alias`, whose tail is spelling-preserving).
        folded_declared_roots = {
            _fold_altsep(dr, os.sep, os.altsep) for dr in declared_roots
        }
        for scope, root in sorted(project_roots.items()):
            if scope in declared or scope in suggested:
                continue
            if not root:
                continue
            if _fold_altsep(root, os.sep, os.altsep) in folded_declared_roots:
                # The candidate's inferred root names the same directory
                # as a declared scope's root — byte-identical, or (on
                # Windows) the same path in the other separator family,
                # which a hand-edited or cross-machine-synced store can
                # carry. Monorepo sub-projects sharing one checkout, or
                # a foreign scope whose memories were written from the
                # declared project's cwd. The declared tag already
                # explains any path under that root; the colliding
                # scope carries zero discriminating signal. Byte
                # equality here lost the suppression whenever the two
                # spellings diverged — the same raw-comparison class
                # `_home_alias` shed, failing toward noise instead of
                # silence.
                continue
            # Substring match — if the body contains the project root as
            # a literal substring (absolute, or tilde-contracted when the
            # root lives under the home directory — the most common
            # prose spelling for home paths), that's the cue. Cheaper
            # than a path extractor that has to handle backticks vs.
            # bare paths (the durability module already does that for
            # path drift; duplicating it here would be over-engineering
            # for a quieter signal). False positives are tempered by the
            # boundary checks on either side (see
            # `_find_root_occurrence`): a root like `projects:foo`'s
            # `/.../foo` must not over-match a sibling tree
            # `/.../foobar/...` or `/.../foo-bar/...` whose last segment
            # merely shares the prefix.
            contracted = False
            hit = _find_root_occurrence(body, root)
            if hit is None and (alias := _home_alias(root)) is not None:
                hit = _find_root_occurrence(body, alias)
                contracted = True
            if hit is None:
                continue
            idx, end = hit
            if _declared_root_covers(
                body, idx, root, declared_roots, contracted=contracted
            ):
                # A declared scope's root extends the candidate root at
                # this very match (nested project trees): the cited path
                # is most specifically covered by a project the write
                # already carries, so demanding the parent/ancestor
                # scope adds nothing.
                continue
            evidence = _trim_evidence(body, idx, end)
            matches.append(
                ScopeMismatchEntry(
                    kind="project_root",
                    evidence=evidence,
                    suggested_scope=scope,
                )
            )
            suggested.add(scope)
            if len(matches) >= _MAX_MATCHES:
                break

    return ScopeMismatchReport(
        matches=tuple(matches),
        suggested_scopes=tuple(sorted(suggested)),
    )


def collect_project_scopes(memories: Iterable[Memory]) -> set[str]:
    """Distinct ``projects:<name>`` scopes across the store."""
    out: set[str] = set()
    for m in memories:
        for scope in m.scopes:
            if scope.startswith("projects:") and len(scope) > len("projects:"):
                out.add(scope)
    return out


def collect_project_roots(memories: Iterable[Memory]) -> dict[str, str]:
    """Map each ``projects:<name>`` scope to an inferred cwd prefix.

    For each scope, take the most common ``origin.cwd`` across
    memories tagged with it. We pick "most common" rather than
    "longest common prefix" for two reasons: most projects in
    practice live under one canonical cwd (LCS would just be that
    cwd anyway), and a stray write from inside a subdirectory
    shouldn't shrink the root. A scope with no origin info anywhere
    is omitted — we have no signal to populate the value.

    SEPARATORS: the degenerate-root guard folds both comparands
    through `verify._fold_altsep` (alternate separator → primary,
    identity on POSIX), so on Windows a home cwd spelled with the
    forward slashes the OS accepts (``C:/Users/me``, or a mixed
    spelling) is dropped exactly like the backslash-canonical form
    `Path.home()` renders. Byte equality let such a spelling — a
    hand-edited or cross-machine-synced store; `origin.capture`
    itself records ``str(Path.cwd().resolve())``, which is OS-native
    — through as a store-wide prefix-matching root: the exact
    fail-open cascade the guard exists to stop. Kept roots stay in
    the store's ORIGINAL spelling; only the guard comparison folds.
    """
    by_scope: dict[str, Counter[str]] = {}
    for m in memories:
        if m.origin is None or not m.origin.cwd:
            continue
        cwd = m.origin.cwd
        for scope in m.scopes:
            if not scope.startswith("projects:"):
                continue
            by_scope.setdefault(scope, Counter())[cwd] += 1
    out: dict[str, str] = {}
    home = _fold_altsep(str(Path.home()), os.sep, os.altsep)
    for scope, counter in by_scope.items():
        # `most_common(1)` returns [(value, count)]; pick the value.
        root = counter.most_common(1)[0][0]
        folded = _fold_altsep(root, os.sep, os.altsep)
        if folded == home or folded == os.sep:
            # Degenerate root: a dotfiles-style project worked from
            # `$HOME` (or a stray session at the filesystem root) would
            # prefix-match essentially every path the user ever cites,
            # bouncing unrelated writes store-wide. Dropping it trades a
            # quiet false negative for that cascade — the same
            # "quieter signal" bias the root pass already documents.
            # (`folded == os.sep` is the old `root == "/"` with both
            # sides folded: byte-identical on POSIX, and on Windows it
            # also catches the ``\`` spelling of the root marker.)
            continue
        out[scope] = root
    return out


def _home_alias(path: str) -> str | None:
    """Tilde-contracted spelling of `path`, or None when it isn't
    under the home directory.

    Bodies overwhelmingly cite home paths in `~` form, while
    `origin.cwd` (the substrate for `collect_project_roots`) is always
    absolute — both spellings have to be searchable.

    SEPARATORS: home and `path` are folded through `verify._fold_altsep`
    (alternate separator → primary) before the prefix check, so on
    Windows the forward-slash and mixed spellings the OS accepts
    (``C:/Users/me/work``, ``C:\\Users\\me/work``) read as home-rooted
    exactly like the backslash-canonical form. The raw ``home + os.sep``
    comparison used to misread those spellings as not home-rooted and
    silently skip the alias search — the same false-negative class
    `verify._is_under_home` shed. On POSIX ``os.altsep`` is None, the
    fold is the identity, and a ``\\`` stays an ordinary filename
    character, never a separator.

    The returned alias keeps the tail in `path`'s ORIGINAL spelling
    (the fold is length-preserving, so the slice offset agrees with the
    folded comparison). The body search still probes exactly one alias
    spelling — a body citing the tilde path in the other separator
    family stays unmatched, a narrower, pre-existing limitation this
    fold deliberately leaves in place (the same "quieter signal" bias
    the root pass already documents).
    """
    home = _fold_altsep(str(Path.home()), os.sep, os.altsep)
    folded = _fold_altsep(path, os.sep, os.altsep)
    if folded.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return None


def _boundary_after(body: str, end: int) -> bool:
    """True when position `end` is a legitimate trailing boundary for a
    root match ending there.

    A matched root that continues with an alphanumeric, `-`, or `_` is
    the prefix of a longer path segment (`/.../foo` inside
    `/.../foobar`, `/.../foo-bar`, `/.../foo_bar`) — the body is
    talking about a *different* project that merely shares the leading
    characters. A `.` continues a segment only when followed by a
    non-whitespace character (`/.../foo.bak`); a `.` followed by
    whitespace or end-of-string is sentence punctuation, which — like a
    segment boundary (`/`), whitespace, or closing punctuation — marks
    a real hit.
    """
    if end >= len(body):
        return True
    ch = body[end]
    if ch.isalnum() or ch in "-_":
        return False
    if ch == "." and end + 1 < len(body) and not body[end + 1].isspace():
        return False
    return True


def _find_root_occurrence(body: str, needle: str) -> tuple[int, int] | None:
    """First occurrence of `needle` in `body` that passes both boundary
    guards, as a `(start, end)` span; None when every occurrence fails.

    Iterates occurrences rather than giving up after the first so a
    guard-rejected early occurrence (`/.../foo.bak`) cannot mask a
    clean later one (`/.../foo/x.py`).
    """
    search_from = 0
    while True:
        idx = body.find(needle, search_from)
        if idx < 0:
            return None
        end = idx + len(needle)
        if idx > 0 and body[idx - 1].isalnum():
            # Leading characters of a larger identifier — false positive.
            search_from = idx + 1
            continue
        if not _boundary_after(body, end):
            search_from = idx + 1
            continue
        return idx, end


def _declared_root_spans(
    body: str, declared: set[str], project_roots: dict[str, str]
) -> list[tuple[int, int]]:
    """Spans of every occurrence of a declared scope's root in `body`
    (absolute and tilde-contracted spellings).

    Used to suppress project-name token hits that sit inside a path the
    write's own tags already cover — with nested project trees the
    parent project's name is a segment of every child path, so without
    this a correctly-tagged child write would always be gated to add
    the parent scope.
    """
    spans: list[tuple[int, int]] = []
    for scope in declared:
        root = project_roots.get(scope)
        if not root:
            continue
        needles = [root]
        alias = _home_alias(root)
        if alias is not None:
            needles.append(alias)
        for needle in needles:
            start = 0
            while True:
                idx = body.find(needle, start)
                if idx < 0:
                    break
                spans.append((idx, idx + len(needle)))
                start = idx + 1
    return spans


def _declared_root_covers(
    body: str,
    idx: int,
    root: str,
    declared_roots: set[str],
    *,
    contracted: bool,
) -> bool:
    """True when a declared scope's root extends the candidate `root`
    at the same match index `idx` — i.e. the cited path is most
    specifically covered by a project the write already declares.

    `contracted` says the candidate matched via its tilde-contracted
    spelling, in which case the declared root is contracted the same
    way before comparing. The control case stays intact: a path under
    the parent but *outside* the declared child's root fails the
    prefix check (or the body probe) and still flags the parent.

    SEPARATORS: both comparisons fold through `verify._fold_altsep`
    (alternate separator → primary, identity on POSIX). The
    store-vs-store prefix check (`dr` extends `root`) folds both
    stored spellings, so on Windows a declared child root recorded in
    the other separator family from the candidate parent still reads
    as nested. The body probe folds the cited slice and `spelled`
    alike — on Windows the two families spell one path, so a body
    citing the child in either family is covered. Raw `startswith` on
    both lost the nested-root suppression whenever spellings mixed,
    flagging the parent on a correctly-tagged child write — the noise
    twin of the silent-miss class `_home_alias` shed. The fold is
    length-preserving, so `idx`/`len` arithmetic and the
    `_boundary_after` check keep running against the RAW body, and
    `spelled` keeps its original spelling throughout.
    """
    folded_root = _fold_altsep(root, os.sep, os.altsep)
    for dr in sorted(declared_roots):
        folded_dr = _fold_altsep(dr, os.sep, os.altsep)
        if len(folded_dr) <= len(folded_root) or not folded_dr.startswith(folded_root):
            continue
        spelled = _home_alias(dr) if contracted else dr
        if spelled is None:
            continue
        cited = body[idx : idx + len(spelled)]
        if _fold_altsep(cited, os.sep, os.altsep) == _fold_altsep(
            spelled, os.sep, os.altsep
        ) and _boundary_after(body, idx + len(spelled)):
            return True
    return False


def _trim_evidence(body: str, start: int, end: int, *, padding: int = 24) -> str:
    """Carve out the matched span plus a little surrounding context.

    Mirrors the snippet-around helper in `durability.py` but kept
    private here so the shape of "evidence" can evolve independently
    if scope-mismatch wants more or less context than transient_warning.
    """
    lo = max(0, start - padding)
    hi = min(len(body), end + padding)
    chunk = body[lo:hi].replace("\n", " ").strip()
    chunk = re.sub(r"\s+", " ", chunk)
    prefix = "..." if lo > 0 else ""
    suffix = "..." if hi < len(body) else ""
    return f"{prefix}{chunk}{suffix}"


__all__ = [
    "ScopeMismatchEntry",
    "ScopeMismatchReport",
    "collect_project_roots",
    "collect_project_scopes",
    "detect_scope_mismatch",
]
