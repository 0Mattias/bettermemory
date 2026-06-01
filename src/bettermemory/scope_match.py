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

import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from .models import Memory


# Cap the per-write evidence list — a body that mentions five different
# projects is pathological enough that we don't need to enumerate every
# match for the response. The first hits are the most actionable; the
# tail just inflates the JSON.
_MAX_MATCHES = 5


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
    # whole-word token in the body. We compile a per-scope regex and
    # cache; the loop is bounded by the number of project scopes,
    # which is small in practice (low tens).
    for scope in sorted(project_scopes):
        if scope in declared:
            continue
        name = scope.split(":", 1)[1] if ":" in scope else ""
        if not name or len(name) < 3:
            # Two-character project names are too noisy to match safely.
            continue
        pattern = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
        m = pattern.search(body)
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
        for scope, root in sorted(project_roots.items()):
            if scope in declared or scope in suggested:
                continue
            if not root:
                continue
            # Substring match — if the body contains the project root as
            # a literal substring, that's the cue. Cheaper than a path
            # extractor that has to handle backticks vs. bare paths
            # (the durability module already does that for path drift;
            # duplicating it here would be over-engineering for a
            # quieter signal). False positives are tempered by the
            # boundary checks on either side: a path embedded in
            # unrelated prose is unlikely to be preceded by `(`, ` `, or
            # start-of-string, and a root like `projects:foo`'s
            # `/.../foo` must not over-match a sibling tree
            # `/.../foobar/...` whose last segment merely shares the
            # prefix.
            idx = body.find(root)
            if idx < 0:
                continue
            if idx > 0 and body[idx - 1].isalnum():
                # Leading characters of a larger identifier — false positive.
                continue
            end = idx + len(root)
            if end < len(body) and (body[end].isalnum() or body[end] in "-_."):
                # The matched root is the prefix of a longer path
                # segment (`/.../foo` inside `/.../foobar`,
                # `/.../foo-bar`, `/.../foo_bar`, `/.../foo.bak`), so
                # the body is talking about a *different* project that
                # merely shares the leading characters. A real hit is
                # followed by a segment boundary (`/`), whitespace,
                # closing punctuation, or end-of-string. Mirror the
                # leading guard's exact-segment intent on the trailing
                # side.
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
    for scope, counter in by_scope.items():
        # `most_common(1)` returns [(value, count)]; pick the value.
        out[scope] = counter.most_common(1)[0][0]
    return out


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
