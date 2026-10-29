"""Path drift for `verify.py`: `detect_path_drift`, its report, the anchors.

`PathDriftReport`, the anchoring vocabulary (`CONVICTING_ANCHORS`,
`UNANCHORED`, `SPARSE_CHECKOUT`, `UNCONFIRMED_COMMIT`), `ClaimAnchor` and
`Anchoring` (where a record's claims are read, design 3.6), the
held-commit and sparse-checkout probes, `detect_path_drift` with its
anchored reads of attestations and relative citations,
`_worktree_root_is_live` and `unverifiable_attestations`. The candidate
scans, the caps and `_path_exists` come from `verify_candidates`, so a
test that patches `Path` for the existence check patches that module.
It lives apart from `verify.py` to keep each module under the package's
2,000-line bound (ENG-96). From the verify family it imports
`verify_candidates` alone; `verify.py` imports it and re-exports the
names callers import from it.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._paths import inside
from .githead import GitDir, locate
from .githead import head_sha as _files_head_sha
from .gitobjects import ObjectReader
from .verify_candidates import (
    _CHECKABLE_CITATION_EXTENSIONS,
    _MAX_ANCHORED_CITATION_STATS,
    _RELATIVE_CITATION_RE,
    _bounded_scan_text,
    _claim_key,
    _ClaimKeys,
    _escapes_root,
    _extract_candidates,
    _is_multi_segment_routelike,
    _is_placeholder_path,
    _normalize_candidate,
    _normalize_for_compare,
    _path_exists,
    _refused_spelling,
)

if TYPE_CHECKING:
    from .membership import Home, Membership, Resolution


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PathDriftReport:
    """Result of a path-drift scan.

    `checked` is the deduplicated set of paths the scanner extracted and
    actually attempted to stat. `missing` is the subset whose `exists()`
    returned False (or raised, suppressed). Empty `missing` with non-empty
    `checked` means the memory's path claims look healthy as of right now.
    Both empty means no path-shaped tokens were found in the body — the
    memory makes no checkable filesystem claims.

    `verified` is the subset of `checked` that the caller previously
    attested via `memory_verify(verified_paths=[...])` AND that still
    exists on disk. Membership in `verified` does NOT exempt a path
    from `missing` if it has since disappeared — verified-then-deleted
    is a real drift signal — but it lets downstream logic distinguish
    "the body cites a path that exists" from "the body cites a path
    the user spot-checked and which still exists." Empty when the
    caller passed no `verified_paths` or none of them appeared in the
    body's candidate set.

    `expected_absent` is the subset of `checked` that does NOT exist on
    disk but was attested via `memory_verify(verified_absent_paths=[...])`
    as *intentionally* absent here — a path on a remote host, a
    platform-conditional location (`~/.config/...` cited for Linux while
    running on macOS), or a path the body cites precisely because it is
    NOT the real location. These are excluded from `missing` (no drift
    signal — absence is the attested, expected state) but surfaced in
    their own bucket so the report stays honest about what it skipped.
    An attested-absent path that EXISTS again is treated as a normal
    healthy candidate — presence never raises a flag.

    `claim_anchored_missing` is the PROVENANCE split over `missing`: the
    subset whose absence is evidence about a claim the memory actually
    makes, as opposed to a path shape scraped out of prose. Three
    producers qualify, and only these three:

      * an absolute body candidate the caller ATTESTED via
        `memory_verify(verified_paths=[...])` and which has since
        disappeared (verified-then-deleted);
      * a relative attestation resolved against the memory's recorded
        `origin.worktree_root` (`_check_anchored_attestations`);
      * a filtered relative citation resolved against that same live
        worktree (`_check_anchored_citations`).

    Everything else in `missing` is an unattested absolute token the
    extractor lifted out of prose. The split exists because the two
    halves were measured and they are not the same instrument: on the
    2026-07-26 store sweep, ~0 of 15 prose-extracted missing-path alerts
    were real drift (remote-host paths, `/etc/nope`-style placeholders,
    documentation examples) against 3 of 3 for anchored attestations.
    Merged into one bucket, the noisy half drove the verdict.

    So `missing` stays the FULL set — every surface that showed a prose
    miss before still shows it, as advisory evidence the caller can act
    on — while `claim_anchored_missing` is what the staleness verdict
    escalates on (`has_claim_anchored_drift`). Evidence stays visible;
    only what ESCALATES narrowed. A path here is always also in
    `missing`; the bucket is a subset marker, never a separate list of
    paths the other buckets lack.

    `dropped_as_route` is the SUPPRESSED set: candidates the scanner
    extracted, found absent, and then declined to stat-report because
    `_is_multi_segment_routelike` judged them application routes rather
    than filesystem citations. They are deliberately NOT in `checked`
    ("we looked and it wasn't there" is a meaningless statement about a
    URL path) and NOT in `missing` (no drift signal). Before this bucket
    existed they were readable from nowhere on this object at all, which
    is how 3.25.2's over-broad route rule swallowed real missing paths
    without the report showing a trace.

    HOW FAR THIS REACHES TODAY — read before relying on it. The bucket
    is populated by `detect_path_drift` and serialised by `to_dict()`,
    so an IN-PROCESS caller holding a `PathDriftReport` can always
    inspect the suppression. A TOOL caller sees it only when the report
    has some OTHER reason to be emitted; a report whose ONLY non-empty
    bucket is `dropped_as_route` — which is what a body citing nothing
    but suppressed paths produces, the case this bucket was added for —
    stays invisible to it, because every surface gates its `path_drift`
    block on the missing / verified / expected-absent buckets, which a
    route-only report fails:

      * `memory_show` and `memory_search`'s expanded top hit gate on
        `has_drift or verified or expected_absent` (`handlers/show.py`,
        `handlers/search.py`) and emit `to_dict()` wholesale once the
        gate fires — a report emitted for any other reason carries the
        suppressed set with it.
      * per-hit `path_drift` on non-top-ranked search hits is rebuilt
        from `MemoryHit` fields (`_response.py`), gated on the same
        three buckets' path lists. The hit carries this bucket as
        `path_drift_dropped_as_route_paths`, and the response builder
        folds a non-empty suppressed set into the emitted dict —
        additively, the key present only when the rule ate something —
        so a firing per-hit block has value-parity with the other two
        surfaces.

    Widening those gate expressions is all a route-only-visibility
    change still needs. Until that is done, the escape hatch for a
    citation you believe is wrongly suppressed remains
    `memory_verify(verified_absent_paths=[...])`, which routes it to
    `expected_absent` before the route rule is consulted — and which a
    caller must reach for without being shown that the suppression
    happened.

    Read through a `ClaimAnchor` (design 3.6), the report carries three
    more fields. `anchor` is the anchor's rule. `unconfirmed` holds the
    claims that could not be read where they would count, each with its
    reason at the same index of `unconfirmed_reasons`: a path absent from
    a stand-in checkout (the rule of the stand-in), an attested claim of a
    project with no live view here (``no_live_checkout``, or the
    translation's ``unanchored``), a path a sparse checkout leaves out of
    its working tree (``sparse_checkout``). An unconfirmed path is never in
    `missing` and never in `claim_anchored_missing`, so it moves no
    verdict. `present_but_attested_absent` holds the paths attested absent
    that are present in the checkout they were attested in.
    """

    checked: tuple[str, ...]
    missing: tuple[str, ...]
    verified: tuple[str, ...] = ()
    expected_absent: tuple[str, ...] = ()
    dropped_as_route: tuple[str, ...] = ()
    claim_anchored_missing: tuple[str, ...] = ()
    unconfirmed: tuple[str, ...] = ()
    unconfirmed_reasons: tuple[str, ...] = ()
    present_but_attested_absent: tuple[str, ...] = ()
    anchor: str | None = None

    @property
    def has_drift(self) -> bool:
        """True when a candidate failed its disk check unattested.

        Strictly `missing`-only, and deliberately so: a suppressed route
        is by definition NOT drift.

        This is the VISIBILITY term, not the escalation term. It used to
        be both — `has_drift` was the sole path input to the staleness
        verdict — and that is what put prose-scraped absences in charge
        of a tier the caller is told to act on. The verdict now reads
        `has_claim_anchored_drift`; this property keeps its old meaning
        and its old job of deciding whether the caller gets to SEE the
        path-drift block at all.

        NOT the emit gate on its own. `memory_show` and `memory_search`'s
        expanded top hit decide whether to emit a `path_drift` block with
        their own inline expression, `has_drift or verified or
        expected_absent` (`handlers/show.py`, `handlers/search.py`) —
        which this property is only one term of. Adding a bucket here
        therefore does NOT widen those gates; see the `dropped_as_route`
        note above for what that currently costs.
        """
        return bool(self.missing)

    @property
    def has_claim_anchored_drift(self) -> bool:
        """True when a miss is backed by a claim the memory itself makes.

        The escalation term: what `path_drift_missing` means at every
        `compute_staleness_verdict` / `verdict_from_signals` call site.
        See `claim_anchored_missing` for the provenance rule and the
        measurement behind it.

        Deliberately NOT folded into `has_drift`: widening that property
        is the lazy edit that would put prose back in charge of the
        verdict, and narrowing it is the lazy edit that would make prose
        misses invisible. The two questions are separate and each has
        exactly one answer here.
        """
        return bool(self.claim_anchored_missing)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "checked": list(self.checked),
            "missing": list(self.missing),
            "verified": list(self.verified),
            "expected_absent": list(self.expected_absent),
            "dropped_as_route": list(self.dropped_as_route),
            # Unconditional, like every other bucket here: `to_dict()` is
            # the wholesale serialisation two handlers emit, and a caller
            # reading `missing` needs to know which entries drove the
            # verdict WITHOUT having to infer it from the verdict. An
            # empty list next to a non-empty `missing` is the honest,
            # readable statement "we saw these, none of them escalated".
            "claim_anchored_missing": list(self.claim_anchored_missing),
        }
        if self.anchor is not None:
            # Only a report read through a `ClaimAnchor` carries these, so
            # a report read at a recorded root serialises as it did.
            out["anchor"] = self.anchor
            out["unconfirmed"] = [
                {"path": path, "reason": reason}
                for path, reason in zip(
                    self.unconfirmed, self.unconfirmed_reasons, strict=True
                )
            ]
            out["present_but_attested_absent"] = list(self.present_but_attested_absent)
        return out


# ---------------------------------------------------------------------------
# Anchoring (design 3.6; design 4, failure 6)
# ---------------------------------------------------------------------------
#
# A claim (an attested or cited path, a path::symbol binding, a cited
# commit) is read at its record's home checkout's live view: the path that
# checkout is at now, wherever it was written. An absolute path recorded
# under a checkout's view translates to that checkout's live view. Where
# the home checkout has no live view here, another live checkout of the
# same project stands in: a path present there confirms the claim, a path
# absent there leaves it unconfirmed, never missing and never drift. Where
# the project has no live view on this machine, every path claim reads
# unconfirmed with the reason and no verdict moves. `Membership.anchor` and
# `Membership.translate` decide where a claim is read; this module reads it
# there. A record with no home row is read where its origin recorded it
# (`worktree_root`), as before the migration homes it (design 8.6).

#: The anchor rules under which an absence is evidence about the claim:
#: the home checkout's live view, the live view of the checkout an absolute
#: path was recorded under, and an absolute path under no recorded view (an
#: observation of this machine, read as it always was).
CONVICTING_ANCHORS = frozenset({"home_checkout", "checkout", "absolute"})

#: `Membership.translate`'s rule for an absolute path under a view whose
#: checkout has no live view here, nor any checkout of its project.
UNANCHORED = "unanchored"

#: The reason an absent path reads unconfirmed in a sparse checkout whose
#: HEAD tracks it outside the cone (E18 c).
SPARSE_CHECKOUT = "sparse_checkout"

#: The rule of a cited commit no live store here holds (design 3.6).
UNCONFIRMED_COMMIT = "unconfirmed"


@dataclass(frozen=True)
class ClaimAnchor:
    """Where one record's claims are read (design 3.6).

    `root` is the live view relative claims resolve at, None when the
    record's project has no live view on this machine (or the record is
    homed outside any project); `rule` is `Membership.anchor`'s rule for
    it: ``home_checkout`` convicts, ``caller_checkout_same_project`` and
    ``project_checkout`` are stand-ins, ``no_live_checkout`` and
    ``global_record`` read nothing. `translate` maps an absolute path to
    the live view of the checkout it was recorded under and names the
    rule (`Membership.translate`). `caller_root` is the live view of the
    caller's checkout, where a cited commit is resolved when the home
    checkout has no live view here (`commit_root`)."""

    root: str | None
    rule: str
    translate: Callable[[str], tuple[str, str]] | None = None
    caller_root: str | None = None

    @property
    def convicting(self) -> bool:
        """Whether an absence at `root` is evidence: the home checkout's
        live view."""
        return self.root is not None and self.rule in CONVICTING_ANCHORS

    @property
    def declared_root(self) -> str | None:
        """The root a fresh declaration (declared claims, a fresh
        attestation list) is checked at: the home checkout's live view, or
        the caller's own checkout of the project, the tree the caller
        stands in. None for a stand-in the caller is not in, and where no
        live view is here."""
        if self.convicting or self.rule == "caller_checkout_same_project":
            return self.root
        return None

    def absolute(self, path: str) -> tuple[str | None, str]:
        """An absolute claim read through the recorded views: the path to
        stat and the rule it is read under, or None with the rule when no
        live view here holds the checkout it was recorded under."""
        if self.translate is None:
            return path, "absolute"
        target, rule = self.translate(path)
        if rule == UNANCHORED:
            return None, rule
        return target, rule

    def commit_root(self, commit: str | None) -> tuple[str | None, str]:
        """Where a cited commit is resolved (design 3.6): the home
        checkout's object store when its live view is here; else the
        caller's, when it holds the commit; else nowhere, and the commit
        reads unconfirmed."""
        if self.root is not None and self.rule == "home_checkout":
            return self.root, "home_checkout"
        if (
            commit
            and self.caller_root is not None
            and holds_commit(self.caller_root, commit)
        ):
            return self.caller_root, "caller_checkout"
        return None, UNCONFIRMED_COMMIT


class Anchoring:
    """The claim anchors of one request: the membership model and the
    caller's resolution (`location.resolve_caller`), from which each
    record's `ClaimAnchor` is read once. Not shared across threads, as the
    model is not.

    `for_record` is None for a record with no home row, which the callers
    read at its recorded root as before. Records sharing a home checkout
    share an anchor, and an absolute path is translated once per request;
    a path under no recorded view never reaches the model."""

    def __init__(self, model: Membership, caller: Resolution | None) -> None:
        self.model = model
        self.caller = caller
        self._homes: dict[str, Home | None] = {}
        self._anchors: dict[tuple[str, str | None], ClaimAnchor] = {}
        self._translated: dict[str, tuple[str, str]] = {}
        self._views: tuple[str, ...] | None = None
        self._caller_root: str | None = None
        self._caller_root_read = False

    def prime(self, record_ids: Sequence[str]) -> None:
        """Read the homes of `record_ids` in one query."""
        wanted = [rid for rid in record_ids if rid not in self._homes]
        if not wanted:
            return
        found = self.model.homes(wanted)
        for rid in wanted:
            self._homes[rid] = found.get(rid)

    def home(self, record_id: str) -> Home | None:
        if record_id not in self._homes:
            self._homes[record_id] = self.model.home(record_id)
        return self._homes[record_id]

    @property
    def caller_root(self) -> str | None:
        """The live view of the caller's checkout, or None."""
        if not self._caller_root_read:
            self._caller_root_read = True
            checkout = self.caller.checkout if self.caller is not None else None
            if checkout is not None:
                try:
                    self._caller_root = self.model.live_view(
                        self.model.checkout(checkout)
                    )
                except KeyError:
                    self._caller_root = None
        return self._caller_root

    def for_record(self, record_id: str) -> ClaimAnchor | None:
        """The record's anchor, or None when it has no home row."""
        home = self.home(record_id)
        if home is None:
            return None
        key = (home.project, home.checkout)
        found = self._anchors.get(key)
        if found is None:
            root, rule = self.model.anchor(record_id, self.caller)
            found = ClaimAnchor(
                root=root,
                rule=rule,
                translate=self.translate,
                caller_root=self.caller_root,
            )
            self._anchors[key] = found
        return found

    def translate(self, path: str) -> tuple[str, str]:
        """`Membership.translate` for `path`, with ``~`` expanded; a path
        under no recorded view reads ``absolute`` as it is spelled.

        "Under a view" is `_paths.inside`, the rule the model's own
        translation reads, so a path the model would translate reaches
        it on every host: a view recorded on Windows (``C:\\w\\app``) holds
        a backslash path below it when the record is read on POSIX too."""
        hit = self._translated.get(path)
        if hit is None:
            expanded = os.path.expanduser(path)
            if any(inside(expanded, view) for view in self._view_paths()):
                hit = self.model.translate(expanded, self.caller)
            else:
                hit = (path, "absolute")
            self._translated[path] = hit
        return hit

    def _view_paths(self) -> tuple[str, ...]:
        if self._views is None:
            self._views = tuple(
                view.path for k in self.model.checkouts() for view in k.views
            )
        return self._views


def holds_commit(root: str | Path, sha: str) -> bool:
    """Whether the object store of the checkout at `root` holds the commit
    `sha`, read from its files (`gitobjects.ObjectReader`, design 3.1): no
    git process. False when the store cannot say (an object format or a
    pack the reader does not know, an evicted file), so an unread commit
    reads unconfirmed rather than held.

    The store is found by `githead.locate` at `root` itself, which reads
    the ``.git`` entry on every platform. `githead.gitdir_at` declines off
    POSIX, where git's ownership check reads security descriptors, and
    through it no caller's store would be read on Windows: every cited
    commit the home cannot resolve would read unconfirmed there. Reading
    objects runs nothing the repository configures, so that check guards
    nothing here."""
    found = locate(Path(root), walk=False)
    if found is None:
        return False
    reader = ObjectReader(found.commondir)
    try:
        return reader.has(sha)
    except (OSError, ValueError):
        return False


def _config_switch(text: str, section: str, key: str) -> bool | None:
    """A boolean of a git configuration file's plain `section` (no
    subsection), the last assignment winning, or None when unset."""
    current = ""
    value: bool | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        if line.startswith("["):
            current = line.strip("[]").strip().lower()
            continue
        if current != section:
            continue
        name, eq, setting = line.partition("=")
        if name.strip().lower() != key:
            continue
        word = setting.strip().strip('"').lower() if eq else "true"
        value = word in ("true", "yes", "on", "1")
    return value


def _sparse_enabled(found: GitDir) -> bool:
    """Whether `core.sparseCheckout` is on for the working tree: the
    repository's config, then the worktree's own (`config.worktree`)."""
    enabled = False
    for config in (found.commondir / "config", found.gitdir / "config.worktree"):
        try:
            text = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        value = _config_switch(text, "core", "sparsecheckout")
        if value is not None:
            enabled = value
    return enabled


def _parts_below(path: str, root: str | Path) -> list[str] | None:
    """The components of `path` below the directory `root`, or None when
    `path` is neither `root` nor under it. Both sides are resolved
    (``realpath``) and compared component by component as this platform
    compares names (``os.path.normcase``: case and either slash on
    Windows), so two spellings of one directory agree: a symbolic link
    (macOS's ``/var`` for ``/private/var``), and on Windows a short 8.3
    name or another case. The components keep `path`'s resolved spelling,
    which a tree lookup matches byte for byte."""
    inner = Path(os.path.realpath(path)).parts
    outer = Path(os.path.realpath(root)).parts
    if len(inner) < len(outer):
        return None
    folded = [os.path.normcase(part) for part in inner[: len(outer)]]
    if folded != [os.path.normcase(part) for part in outer]:
        return None
    return list(inner[len(outer) :])


def _absent_by_sparsity(path: str) -> bool:
    """True when `path`, absent from the disk, sits in a sparse checkout
    whose HEAD tracks it: the cone leaves it out of the working tree, it was
    not deleted (E18 c). Read from the repository's files: the
    ``core.sparseCheckout`` switch, then HEAD's tree (no git process).

    The repository is found by `githead.locate`, which answers on every
    platform; `githead.find_gitdir` declines off POSIX, and through it a
    path a cone leaves out would read missing on Windows. The path's place
    in the working tree is read by `_parts_below`, on resolved paths in
    this platform's case."""
    expanded = os.path.expanduser(path)
    tail: list[str] = []
    start = expanded
    while not os.path.isdir(start):
        parent, name = os.path.split(start)
        if not name or parent == start:
            return False
        tail.append(name)
        start = parent
    found = locate(Path(start))
    if found is None or not _sparse_enabled(found):
        return False
    base = _parts_below(start, found.worktree_root)
    if base is None:
        return False
    parts = [*base, *reversed(tail)]
    head = _files_head_sha(found)
    if head is None or not parts:
        return False
    reader = ObjectReader(found.commondir)
    try:
        commit = reader.commit(head)
        if commit is None:
            return False
        tree = commit.tree
        for index, part in enumerate(parts):
            entries = reader.tree(tree)
            if entries is None:
                return False
            match = next((e for e in entries if e.name == os.fsencode(part)), None)
            if match is None:
                return False
            if index == len(parts) - 1:
                return True
            if match.mode != 0o40000:
                return False
            tree = match.sha
    except (OSError, ValueError):
        return False
    return False


def detect_path_drift(
    body: str,
    *,
    verified_paths: tuple[str, ...] | list[str] = (),
    absent_paths: tuple[str, ...] | list[str] = (),
    worktree_root: str | Path | None = None,
    anchor: ClaimAnchor | None = None,
) -> PathDriftReport:
    """Extract path-shaped tokens from `body` and check them on disk.

    Returns an empty report when the body is empty, contains no path
    candidates, or every candidate failed validation. Existence checks
    swallow OSError (permission denied, ELOOP, etc.) and treat them as
    "missing" — the caller should treat a `missing` entry as "the path
    couldn't be confirmed", not as "definitely deleted". The semantic
    difference doesn't matter for the staleness signal.

    Order in `checked` and `missing` is deterministic: backtick-wrapped
    paths come first (in body order), then bare paths from the
    backtick-masked body (in body order). It is NOT a single
    "first-encountered" pass — a bare path that appears textually before
    a backtick-wrapped one still sorts after it, because extraction runs
    the backtick pass to completion before the bare pass (see
    `_extract_candidates`). The ordering is still fully deterministic,
    which is what keeps the report stable for snapshot tests.

    `verified_paths` is an optional set of paths the caller has
    previously attested via `memory_verify`. When a candidate from the
    body is in that set AND still exists, it lands in `report.verified`
    (in addition to the usual `checked` slot). A verified path that's
    since disappeared still lands in `missing` — verification doesn't
    paper over deletion.

    `worktree_root` anchors RELATIVE claims — both attestations
    (`_check_anchored_attestations`) and body citations
    (`_check_anchored_citations`). Body extraction drops relative paths on
    purpose (checking them would otherwise mean checking the reader's
    cwd), but resolved against the memory's own `origin.worktree_root` —
    captured at WRITE time — a relative claim names one file in one tree,
    so it gets a real existence check. Without an anchor a memory citing
    `src/pkg/mod.py`, which is how developers actually write it, receives
    no deletion detection at all; a bare `detect_path_drift(body)` call
    keeps returning nothing for relative paths, unchanged.

    The anchor is used only when it is LIVE on this machine
    (`_worktree_root_is_live`). A store synced from another host records a
    worktree this machine never had, and joining citations to it would
    mark every one of them missing — fabricated drift, on every memory
    from that host at once.

    `absent_paths` is the mirror attestation (`memory_verify(
    verified_absent_paths=[...])`): paths the caller confirmed are
    *intentionally* not present on this machine. A candidate in that
    set that fails the disk check lands in `report.expected_absent`
    instead of `missing` — no drift signal, but the skip stays visible.

    Every miss lands in `report.missing`; the subset backed by an
    attestation or by an anchored citation ALSO lands in
    `report.claim_anchored_missing`, which is the only bucket the
    staleness verdict escalates on. See `PathDriftReport` for the
    measurement that split them.

    A candidate the route rule suppresses lands in
    `report.dropped_as_route` — not `checked`, not `missing`, but
    readable off the returned report instead of nowhere at all. See the
    reach note on `PathDriftReport` for how far the bucket travels
    beyond this return value: each MCP surface's `path_drift` block
    carries a non-empty suppressed set once the block fires, but a
    route-ONLY report still fires no block anywhere.

    `anchor` reads the claims where design 3.6 says they are read, in
    place of `worktree_root`: relative claims at the anchor's root (the
    home checkout's live view, else a stand-in), absolute ones at the live
    view of the checkout they were recorded under (`ClaimAnchor.absolute`).
    Only a read that convicts reports a miss; a stand-in's absence, an
    anchorless claim and a path a sparse checkout leaves out of its tree
    land in `unconfirmed` with their reasons, and a path attested absent
    that is present where it convicts lands in
    `present_but_attested_absent` (see `PathDriftReport`).
    """
    if anchor is not None:
        return _detect_anchored(body, verified_paths, absent_paths, anchor)
    candidates = _extract_candidates(body)
    # Resolve the anchor ONCE, for both relative passes. A recorded
    # worktree that is not a live directory here disables them entirely —
    # see `_worktree_root_is_live` for why that direction is right.
    anchor_root = (
        Path(worktree_root)
        if worktree_root is not None and _worktree_root_is_live(worktree_root)
        else None
    )
    if not candidates and anchor_root is None:
        return PathDriftReport(checked=(), missing=(), verified=())

    # Run verified_paths through the same trim/validate pipeline the body
    # candidates pass through (`_normalize_candidate`) before the
    # `_claim_key` pass that handles `~`-expansion. Without
    # this, an attestation like `/etc/foo.conf,` (extracted from prose
    # with a trailing comma) would not match the body candidate
    # `/etc/foo.conf` (already trimmed) — the audit flagged the
    # asymmetry between the two normalisation paths. We keep the
    # validator's `None` rejection in the same step so `verified_paths`
    # can't introduce shapes the extractor would itself have dropped.
    # `absent_paths` goes through the identical pipeline for the same
    # reason — the two attestation lists must match body candidates by
    # the same rules or the asymmetry bug returns on the absent axis.
    keys = _ClaimKeys()
    verified_set = _normalize_attestations(verified_paths, keys)
    absent_set = _normalize_attestations(absent_paths, keys)

    checked: list[str] = []
    missing: list[str] = []
    # Parallel provenance list, not a filter applied afterwards: by the
    # time a path is a string in `missing` there is nothing left on it to
    # tell prose from attestation, so the split has to be recorded where
    # the decision is made. Every append here is paired with an append to
    # `missing` — the bucket is a subset marker, and the two helpers below
    # take it for the same reason.
    claim_anchored: list[str] = []
    verified: list[str] = []
    expected_absent: list[str] = []
    dropped_as_route: list[str] = []
    # The claims read so far, by `_claim_key`: two spellings of one file
    # (through a link, or the scan's lexical dedup missing an alias) are
    # one claim and land once. Every pass below dedupes against this one
    # set, and every key comes from the read's one `_ClaimKeys`.
    seen: set[str] = set()
    for path, drop_if_missing, bare_spaced in candidates:
        exists = _path_exists(path)
        norm = keys(path)
        # An attestation pins the citation: a path the caller explicitly
        # named in `verified_paths` / `verified_absent_paths` is proven to
        # be the complete, intended claim, which resolves any extraction
        # ambiguity — the drops below never apply to attested candidates
        # (otherwise a verified-then-deleted path would produce NO drift
        # signal, breaking the documented contract).
        attested = norm in verified_set or norm in absent_set
        if not exists and not attested:
            if bare_spaced:
                # Existence-arbitrated fallback for spaced bare-scan
                # candidates: the continuation rule can glue a prose
                # acronym pair (`TCP/IP`, `CI/CD`) onto a real path
                # (`/etc/hosts TCP/IP keepalive`). If the prefix up to
                # the first space exists, the spaced run was prose —
                # check the prefix instead, so the real path still gets
                # its drift check. If neither form exists, the extraction
                # is too ambiguous to trust; drop rather than flag a
                # claim we may have manufactured. (Backticked spaced
                # paths never take this branch — the author delimited
                # those precisely, so a miss there is real drift.)
                prefix = path.split(" ", 1)[0]
                prefix_key = keys(prefix)
                if _path_exists(prefix) and prefix_key not in seen:
                    seen.add(prefix_key)
                    checked.append(prefix)
                    if prefix_key in verified_set:
                        verified.append(prefix)
                continue
            if drop_if_missing:
                # Ambiguous truncation: the bare scan stopped at
                # ` Capitalized…` (or ` (2).pdf`-style continuations)
                # right after this candidate, so the real citation may
                # continue past the space. A missing-flag here would be
                # manufactured by our own truncation, not by drift.
                # An existing candidate is kept: existence proves the
                # prefix is a real path regardless of the prose after it.
                continue
            if _is_multi_segment_routelike(path):
                # An application route, not a deleted file. `_is_route`
                # upstream can only suppress routes when the SAME body
                # also happens to carry a domain-qualified URL to learn a
                # vocabulary from, so a memory citing bare routes
                # (`/api/v1/events/presence`, `/admin/macros`) had every
                # one of them stat'd and reported missing. Kept out of
                # `checked` and `missing` — "we looked and it wasn't
                # there" is a meaningless statement about a URL path —
                # but recorded in `dropped_as_route` so the suppression
                # is at least readable off the returned report instead of
                # leaving no trace anywhere. (Leaving no trace is what let
                # 3.25.2's over-broad rule swallow real missing paths.)
                # NOTE: a report whose ONLY non-empty bucket is this one
                # still reaches no MCP surface — see the reach note on
                # `PathDriftReport`.
                #
                # Deliberately LAST in this block: the spaced-bare and
                # ambiguous-truncation arms above must arbitrate first,
                # or a prose-glued candidate (`/tmp/real-dir TCP/IP`)
                # would read as a route on its manufactured tail and skip
                # the prefix-existence fallback that recovers the real
                # path.
                #
                # Appended unguarded, unlike `checked` below. Every value
                # that lands here is a candidate `path`, and
                # `_extract_candidates` already dedupes those (it keys an
                # index on the comparison form and appends only on a
                # miss), so one route cannot arrive twice — a
                # `not in dropped_as_route` guard would be unreachable.
                # `checked` needs its guard for a reason that does not
                # apply here: the spaced-bare arm above appends a DERIVED
                # `prefix`, which a later, genuinely distinct candidate
                # can equal.
                dropped_as_route.append(path)
                continue
        if norm in seen:
            continue
        seen.add(norm)
        checked.append(path)
        if not exists:
            if norm in absent_set:
                expected_absent.append(path)
            else:
                # Known limitation: a bare absolute path that legitimately
                # lives on a REMOTE host (`/opt/gophish`, `/data/compose/.env`
                # on a homelab board) is stat'd against the LOCAL filesystem,
                # so absence here says nothing about the host that owns it.
                # Two outcomes, split by the route rule above:
                #
                #   * Reaches this line and reads `missing` — a perpetual
                #     drift signal — when the citation carries an extension
                #     (`/data/compose/.env`) or its parent happens to exist
                #     locally (`/opt/gophish`, on a host that has `/opt`).
                #   * Never reaches this line at all, since 3.25.2: any
                #     extensionless non-home citation whose parent is absent
                #     locally (`/srv/docker/gitea`, `/mnt/tank/media`) reads
                #     as an application route and is DROPPED — not reported
                #     missing, not even `checked`. It IS recorded in
                #     `dropped_as_route`, which an in-process caller can
                #     read. These shapes produce a route-ONLY report, so
                #     no MCP surface emits them (a report that also
                #     carries drift does ship the bucket via `to_dict()`).
                #
                # Either way `memory_verify(verified_absent_paths=[...])` is
                # the intended escape hatch: it routes the citation to
                # `expected_absent` above and pins it against BOTH outcomes
                # (attested candidates skip the route drop entirely). There
                # is no local-only heuristic that can tell a legitimately-
                # remote path from a genuinely-deleted local one without
                # also suppressing real local drift, so an unattested
                # absence that gets this far stays `missing`.
                missing.append(path)
                if norm in verified_set:
                    # Verified-then-deleted: the caller named this exact
                    # path in `memory_verify(verified_paths=[...])`, so
                    # its absence is evidence about a REVIEWED claim, not
                    # about a token the extractor lifted out of a
                    # sentence. That is the 3-of-3-real class in the
                    # sweep, and the only shape in this loop that earns
                    # escalation. (An absent-attested path never reaches
                    # here — it took the `expected_absent` arm above.)
                    claim_anchored.append(path)
            continue
        if norm in verified_set:
            verified.append(path)

    _check_anchored_attestations(
        anchor_root,
        verified_paths,
        absent_paths,
        checked=checked,
        missing=missing,
        claim_anchored=claim_anchored,
        verified=verified,
        expected_absent=expected_absent,
        seen=seen,
        keys=keys,
    )
    # Attestations FIRST, citations second: an attested path is the
    # caller's reviewed claim and carries the `verified` /
    # `expected_absent` semantics a bare citation cannot, so when a body
    # both cites and attests one file the attestation must be the entry
    # that lands. The citation pass dedupes against everything already
    # checked, so it can only ever ADD claims neither earlier pass saw.
    if anchor_root is not None:
        _check_anchored_citations(
            anchor_root,
            body,
            checked=checked,
            missing=missing,
            claim_anchored=claim_anchored,
            seen=seen,
            keys=keys,
        )
    return PathDriftReport(
        checked=tuple(checked),
        missing=tuple(missing),
        verified=tuple(verified),
        expected_absent=tuple(expected_absent),
        dropped_as_route=tuple(dropped_as_route),
        claim_anchored_missing=tuple(claim_anchored),
    )


class _AnchoredRead:
    """The buckets of one anchored read (`_detect_anchored`), with the
    seen set every pass dedupes against, so a claim the body cites and
    attests lands once whichever pass reads it first."""

    def __init__(self) -> None:
        self.checked: list[str] = []
        self.missing: list[str] = []
        self.claim_anchored: list[str] = []
        self.verified: list[str] = []
        self.expected_absent: list[str] = []
        self.dropped_as_route: list[str] = []
        self.unconfirmed: list[str] = []
        self.reasons: list[str] = []
        self.present_absent: list[str] = []
        self.seen: set[str] = set()
        self.keys = _ClaimKeys()

    def first(self, path: str) -> bool:
        """Mark `path` read; False when a pass already read it, under any
        spelling of it (`_claim_key`, through the read's `keys`)."""
        norm = self.keys(path)
        if norm in self.seen:
            return False
        self.seen.add(norm)
        return True

    def unconfirm(self, path: str, reason: str) -> None:
        self.unconfirmed.append(path)
        self.reasons.append(reason)

    def miss(self, path: str, *, convicting: bool, reason: str, claim: bool) -> None:
        """An absent path: a miss where the read convicts and the path is
        not left out by a sparse checkout, unconfirmed otherwise. `claim`
        says whether the miss is claim-anchored."""
        if not convicting:
            self.unconfirm(path, reason)
        elif _absent_by_sparsity(path):
            self.unconfirm(path, SPARSE_CHECKOUT)
        else:
            self.checked.append(path)
            self.missing.append(path)
            if claim:
                self.claim_anchored.append(path)

    def report(self, rule: str) -> PathDriftReport:
        return PathDriftReport(
            checked=tuple(self.checked),
            missing=tuple(self.missing),
            verified=tuple(self.verified),
            expected_absent=tuple(self.expected_absent),
            dropped_as_route=tuple(self.dropped_as_route),
            claim_anchored_missing=tuple(self.claim_anchored),
            unconfirmed=tuple(self.unconfirmed),
            unconfirmed_reasons=tuple(self.reasons),
            present_but_attested_absent=tuple(self.present_absent),
            anchor=rule,
        )


def _detect_anchored(
    body: str,
    verified_paths: tuple[str, ...] | list[str],
    absent_paths: tuple[str, ...] | list[str],
    anchor: ClaimAnchor,
) -> PathDriftReport:
    """`detect_path_drift` read through `anchor` (design 3.6).

    Three passes, in `detect_path_drift`'s order, over the same buckets:
    the body's absolute candidates, each read at the live view of the
    checkout it was recorded under; the attestations; the body's relative
    citations at the anchor's root. A candidate the extractor cannot trust
    is dropped by the same three rules as there (the spaced bare prefix,
    the ambiguous truncation, the route), whose reasons are written down
    in that loop. What changes is only where a claim is read and whether
    the read convicts."""
    out = _AnchoredRead()
    verified_set = _normalize_attestations(verified_paths, out.keys)
    absent_set = _normalize_attestations(absent_paths, out.keys)

    for path, drop_if_missing, bare_spaced in _extract_candidates(body):
        norm = out.keys(path)
        attested = norm in verified_set or norm in absent_set
        target, rule = anchor.absolute(path)
        if target is None:
            # Recorded under a checkout of a project with no live view here.
            # An attestation is a claim and reads unconfirmed; a path the
            # extractor lifted from prose has nothing to say and is passed
            # over, as the read would have passed over a dead root.
            if attested and out.first(path):
                out.unconfirm(path, rule)
            continue
        convicting = rule in CONVICTING_ANCHORS
        exists = _path_exists(target)
        if not exists and not attested:
            if bare_spaced:
                prefix = target.split(" ", 1)[0]
                if _path_exists(prefix) and out.first(prefix):
                    out.checked.append(prefix)
                    if out.keys(path.split(" ", 1)[0]) in verified_set:
                        out.verified.append(prefix)
                continue
            if drop_if_missing:
                continue
            if _is_multi_segment_routelike(target):
                out.dropped_as_route.append(path)
                continue
        if not out.first(target):
            continue
        if not exists:
            if norm in absent_set:
                out.checked.append(target)
                out.expected_absent.append(target)
            else:
                out.miss(
                    target,
                    convicting=convicting,
                    reason=rule,
                    claim=norm in verified_set,
                )
            continue
        if norm in absent_set:
            if convicting:
                out.checked.append(target)
                out.present_absent.append(target)
            else:
                out.unconfirm(target, rule)
            continue
        out.checked.append(target)
        if norm in verified_set:
            out.verified.append(target)

    _read_attestations(anchor, verified_paths, absent_paths, out)
    if anchor.root is not None:
        _read_citations(anchor, body, out)
    return out.report(anchor.rule)


def _read_attestations(
    anchor: ClaimAnchor,
    verified_paths: tuple[str, ...] | list[str],
    absent_paths: tuple[str, ...] | list[str],
    out: _AnchoredRead,
) -> None:
    """The attestations read through `anchor`: relative ones at its root,
    absolute ones at the live view of the checkout they were recorded under
    (`_check_anchored_attestations` is the same pass at a recorded root).

    An absolute attestation under no recorded view keeps its old reading:
    checked here only when it resolves inside the root, else by the body
    pass when the body cites it. With no root, every relative attestation
    reads unconfirmed with the anchor's rule. Whether an attestation is an
    absence is read by its target's `_claim_key`, so an absence attested
    under one spelling of a file is the absence of the file under any."""
    root = Path(anchor.root) if anchor.root is not None else None
    reads: list[tuple[_AttestationRead, bool]] = []
    for listed_absent, paths in ((False, verified_paths), (True, absent_paths)):
        for raw in paths:
            found = _attestation_read(anchor, root, raw)
            if found is not None:
                reads.append((found, listed_absent))
    absent = {
        out.keys(found.path) for found, listed in reads if listed and found.readable
    }
    for found, _listed in reads:
        if not found.readable:
            if out.first(found.path):
                out.unconfirm(found.path, found.rule)
            continue
        target, rule, convicting = found.path, found.rule, found.convicting
        if not out.first(target):
            continue
        is_absent = out.keys(target) in absent
        if _path_exists(target):
            if not is_absent:
                out.checked.append(target)
                out.verified.append(target)
            elif convicting:
                out.checked.append(target)
                out.present_absent.append(target)
            else:
                out.unconfirm(target, rule)
        elif is_absent:
            out.checked.append(target)
            out.expected_absent.append(target)
        else:
            out.miss(target, convicting=convicting, reason=rule, claim=True)


@dataclass(frozen=True)
class _AttestationRead:
    """Where one attestation is read (`_attestation_read`): the path, the
    rule it is read under and whether an absence there convicts; or, when
    not `readable`, the spelling that reads unconfirmed with `rule`."""

    path: str
    rule: str
    convicting: bool
    readable: bool = True


def _attestation_read(
    anchor: ClaimAnchor, root: Path | None, raw: str
) -> _AttestationRead | None:
    """Where `_read_attestations` reads the attestation `raw`, or None when
    it reads nothing there (an empty or refused spelling, or an absolute
    path under no recorded view that does not resolve inside the root).
    The path gate runs first, before the root is read: with no root a
    relative attestation reads unconfirmed under its own spelling, keyed
    by `_claim_key`, so a refused one would be reported and, on Windows,
    resolved (ENG-164)."""
    spelled = raw.strip() if raw else ""
    if not spelled or _refused_spelling(spelled):
        return None
    if _is_absolute_attestation(spelled):
        normalized = _normalize_candidate(spelled)
        if normalized is None:
            return None
        target, rule = anchor.absolute(normalized)
        if target is None:
            return _AttestationRead(normalized, rule, False, readable=False)
        if rule == "absolute" and (root is None or not _resolves_under(target, root)):
            return None
        return _AttestationRead(target, rule, rule in CONVICTING_ANCHORS)
    if _escapes_root(spelled):
        return None
    if root is None:
        return _AttestationRead(spelled, anchor.rule, False, readable=False)
    joined = _join_under(root, spelled)
    if joined is None:
        return None
    return _AttestationRead(joined, anchor.rule, anchor.convicting)


def _read_citations(anchor: ClaimAnchor, body: str, out: _AnchoredRead) -> None:
    """The body's relative citations at the anchor's root, behind the
    filter and the live-parent rule `_check_anchored_citations` applies at
    a recorded root, with its budget. A miss convicts only at the home
    checkout's live view; a stand-in leaves it unconfirmed."""
    assert anchor.root is not None
    root = Path(anchor.root)
    stats = 0
    for match in _RELATIVE_CITATION_RE.finditer(_bounded_scan_text(body)):
        if stats >= _MAX_ANCHORED_CITATION_STATS:
            break
        cited = match.group(1)
        if not _is_checkable_citation(cited):
            continue
        resolved = _join_under(root, cited)
        if resolved is None or not out.first(resolved):
            continue
        stats += 1
        if _path_exists(resolved):
            out.checked.append(resolved)
            continue
        parent = os.path.expanduser(os.path.dirname(resolved))
        if not os.path.isdir(parent):
            continue
        out.miss(resolved, convicting=anchor.convicting, reason=anchor.rule, claim=True)


def _check_anchored_attestations(
    worktree_root: str | Path | None,
    verified_paths: tuple[str, ...] | list[str],
    absent_paths: tuple[str, ...] | list[str],
    *,
    checked: list[str],
    missing: list[str],
    claim_anchored: list[str],
    verified: list[str],
    expected_absent: list[str],
    seen: set[str],
    keys: Callable[[str], str],
) -> None:
    """Existence-check the attestations anchored to the memory's worktree:
    relative spellings, plus absolute ones that resolve inside it.

    Body extraction drops relative paths on purpose, and the reason given
    is that without an anchor, checking them would mean checking the cwd
    at RETRIEVAL time — which would make a memory's verdict depend on
    where the reader happens to stand. That objection does not reach an
    attestation resolved against `origin.worktree_root`: the worktree is
    captured at WRITE time and stored on the memory, so the check is
    anchored to the tree the author actually attested in.

    Measured before this existed, on a 206-memory store: 104 memories
    attested relative paths, 72 of them received NO path check of any
    kind, and three attested files were already gone with nothing
    surfacing it — one genuinely deleted, one moved, and one that never
    existed (a false attestation). All three read clean.

    Scoped to `verified_paths` / `verified_absent_paths`. An attestation
    is a caller's explicit, reviewed claim that a path IS the citation,
    so it needs no filtering; body-prose citations are checked separately
    and behind a much stricter filter (`_check_anchored_citations`).

    `worktree_root` arrives ALREADY vetted as live by `detect_path_drift`
    — a `None` here means either "no anchor recorded" or "the recorded
    anchor is not a directory on this machine", and both must leave this
    check inert. See `_worktree_root_is_live`.

    Every miss this function records is claim-anchored by construction —
    an attestation IS the reviewed claim — so `claim_anchored` gets the
    same append `missing` does. This is the 3-of-3-real half of the
    measurement that motivated the provenance split.
    """
    if worktree_root is None:
        return
    root = Path(worktree_root)

    def _anchored(raw: str) -> str | None:
        # Delegate the absolute/relative split to `_is_absolute_attestation`
        # — the ONE predicate whose docstring mandates mirroring
        # `_normalize_candidate`'s accepted anchors — rather than testing
        # `startswith(("/", "~"))` locally. The local test classified the
        # env-var home spellings (`$HOME/…`, `${HOME}/…` — forms
        # `_normalize_candidate`, `_BARE_RE`, and the write gate all
        # support) as worktree-RELATIVE, joined them onto the root
        # (`<root>/$HOME/…`), stat-failed, and appended the phantom to
        # `claim_anchored_missing` — permanently escalating the verdict of
        # a memory whose attestation `unverifiable_attestations` had
        # ACCEPTED via `~` canonicalization.
        #
        # An absolute attestation that resolves INSIDE this worktree is
        # the same reviewed claim as its relative spelling and seeds
        # `checked` / `verified` / `expected_absent` the same way.
        # Skipping it (the shape through 6.4.1) left the citation pass
        # below to stat the body's RELATIVE citation of that file with
        # no absent set to consult, so an attested absence in absolute
        # form escalated to `claim_anchored_missing` while the identical
        # attestation spelled relatively routed to `expected_absent`.
        # An absolute form that resolves ELSEWHERE stays out of this
        # pass — there is no worktree claim to anchor, and the main
        # `detect_path_drift` loop already existence-checks it via
        # `_normalize_attestations` set-membership when the body cites
        # it.
        rel = raw.strip() if raw else ""
        if not rel:
            return None
        if _is_absolute_attestation(rel):
            normalized = _normalize_candidate(rel)
            if normalized is None or not _resolves_under(normalized, root):
                return None
            return normalized
        return _join_under(root, rel)

    # Built from the ANCHORED form, not from `_normalize_attestations`:
    # that helper runs the bare relative path through `_normalize_candidate`,
    # which rejects relative paths by design, so the absent set would come
    # back empty and every intentionally-absent attestation would read as
    # drift — the escape hatch inverted into a permanent false alarm.
    absent_set = {
        keys(anchored)
        for anchored in (_anchored(raw) for raw in absent_paths)
        if anchored is not None
    }
    # Anchor BEFORE validating (see `_anchored`): `_normalize_candidate` is
    # the gate that enforces the relative exclusion, so running the bare
    # relative form through it would drop every attestation this function
    # exists to check. Joining to the worktree root first produces the
    # absolute form the validator is built for, which is also the honest
    # object of the check — the file as it stands in the tree the author
    # attested in.
    for raw in (*verified_paths, *absent_paths):
        resolved = _anchored(raw)
        if resolved is None:
            continue
        norm = keys(resolved)
        if norm in seen:
            continue
        seen.add(norm)
        checked.append(resolved)
        if _path_exists(resolved):
            if norm not in absent_set:
                verified.append(resolved)
        elif norm in absent_set:
            expected_absent.append(resolved)
        else:
            missing.append(resolved)
            claim_anchored.append(resolved)


def _join_under(root: str | Path, rel: str) -> str | None:
    """The relative claim `rel` joined onto `root` and validated, or None
    when it names nothing in that tree: a spelling that climbs out of the
    root or is rooted elsewhere (`_escapes_root`), or one the validator
    refuses once joined (`_normalize_candidate`, the path gate included).

    The one join every relative citation and attestation takes, on the
    read side and the write side alike (ENG-164): `src/../../../../x`
    joined and stat'd as it was spelled named a file outside the tree
    the claim is about, and nothing stopped a relative attestation from
    climbing to a network or device path. Joined BEFORE validating, for
    the reason `_check_anchored_attestations` gives: the validator is
    itself the relative-path gate."""
    if _escapes_root(rel):
        return None
    return _normalize_candidate(str(Path(root) / rel))


def _resolves_under(path: str, root: Path) -> bool:
    """True when `path` — absolute or ``~``-anchored, already normalized —
    resolves to a location inside `root`.

    Symlinks are resolved on both sides so a worktree recorded as
    ``/var/...`` and an attestation spelled ``/private/var/...`` (or the
    reverse) still meet. Non-strict: the path need not exist — an
    attested ABSENCE is exactly a path that does not. A spelling this
    platform cannot treat as absolute (a Windows drive path read on
    POSIX, which `_is_absolute_attestation` accepts for the synced
    case) is not under any root here; joining it onto the cwd would
    manufacture a phantom.
    """
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        return False
    try:
        candidate = Path(expanded).resolve(strict=False)
        return candidate.is_relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False


def _worktree_root_is_live(worktree_root: str | Path) -> bool:
    """True when the memory's recorded worktree is a directory HERE.

    REVERSES A RECORDED DECISION, so the argument is written down.
    `origin.py`'s auto-scope filter already degrades on a dead worktree
    (`worktrees_match` / `_worktree_root_is_gone`) and explicitly says
    verify takes the OPPOSITE bias: an indeterminate stat there "folds
    into the `missing` path-drift bucket, i.e. toward MORE signal". That
    bias is right for a path the caller attested ON THIS MACHINE and
    which then disappeared — absence is the evidence.

    It is wrong for the root itself. A store synced from another host
    carries that host's `worktree_root`, so `root/rel` cannot exist here
    for ANY relative claim; every one lands in `missing` and every memory
    from that host escalates at once. That is not more signal, it is a
    constant function — the same failure mode as the `always_flag`
    detector the rot benchmark exists to distinguish real detection from.
    A machine that has never seen the checkout has no evidence either
    way, and "no evidence" must read as silence, not as drift.

    So the fail-open is scoped exactly to the thing the reversal is about:
    the ROOT's own liveness, checked once per call. Everything below the
    root keeps the original bias — an unstattable file under a live
    worktree still folds to `missing` via `_path_exists`.

    `os.path.isdir` rather than `Path.is_dir()`: it swallows OSError and
    ValueError internally and keeps this independent of the module-level
    `Path` symbol, which the suite patches wholesale when exercising the
    stat-failure path (same reasoning as `_is_multi_segment_routelike`).
    """
    try:
        return os.path.isdir(os.path.expanduser(str(worktree_root)))
    except (OSError, ValueError):
        return False


def _is_checkable_citation(rel: str) -> bool:
    """True when a `_RELATIVE_CITATION_RE` match may be stat'd once anchored.

    The regex is deliberately OVER-MATCHY and is only safe that way for
    commit drift, where a phantom anchor touches no commit and is
    verdict-neutral. Existence checking inverts the asymmetry — a phantom
    stats as missing and FABRICATES drift — so every citation crosses
    this gate first. Three rules, each closing a measured shape:

    * **At least one directory segment.** A bare filename (`run.py`,
      `CHANGELOG.md`) is the single largest false-positive class: prose
      names a file without its directory constantly ("the `_MODES` tuple
      in run.py"), and joined to the worktree ROOT almost none of those
      exist. It is also what makes the whole bare-domain class
      (`pypi.org`, `example.com`, `fly.io` — zero-dir matches the regex
      admits on purpose) unreachable here, since a domain WITH a route
      never matches the regex at all. The cost is real root-file
      citations (`CHANGELOG.md`) losing their check; that is the cheap
      direction, and an attestation still checks them.

    * **A first segment that is not host-shaped.** A schemeless URL
      (`www.example.com/a/b.md`, `docs.rs/serde/latest/index.html`) DOES
      match the regex — the lookahead only rejects the domain-with-route
      form when the tail has no extension. A dot inside the first segment
      is the tell; a LEADING dot is not (`.github/workflows/ci.yml`,
      `plugin/.claude-plugin/plugin.json`), so only dots past position 0
      disqualify.

    * **A real file extension** (`_CHECKABLE_CITATION_EXTENSIONS`), which
      the regex itself cannot check — it accepts any 2-8 letter-first run.

    Placeholders are refused through the existing `_is_placeholder_path`
    machinery, applied to the ROOT-SLASHED form: `path/to/config.yaml` is
    the same documentation placeholder as `/path/to/config.yaml`, and
    once joined to a worktree root the prefix test would no longer see
    it. Glob/template shapes need no test here — their characters are
    outside the regex's own character class.
    """
    first, slash, _ = rel.partition("/")
    if not slash:
        return False
    if "." in first[1:]:
        return False
    if rel.rsplit(".", 1)[-1].lower() not in _CHECKABLE_CITATION_EXTENSIONS:
        return False
    return not _is_placeholder_path("/" + rel)


def _check_anchored_citations(
    root: Path,
    body: str,
    *,
    checked: list[str],
    missing: list[str],
    claim_anchored: list[str],
    seen: set[str],
    keys: Callable[[str], str],
) -> None:
    """Existence-check RELATIVE citations in body prose against `root`.

    The counterpart to `_check_anchored_attestations` for the citations
    nobody attested — which is most of them. Measured on the rot
    benchmark before this existed: the relative-citation arm produced
    EXACTLY ZERO path-drift flags, i.e. the citation style developers
    actually write got no path protection at all, while the same claims
    written absolutely were checked. That gap, not the prose-swamp risk,
    is what the anchor closes: `origin.worktree_root` is captured at
    write time, so the check is against the tree the author wrote in
    rather than against the reader's cwd.

    `_is_checkable_citation` is the filter that makes it safe. Two more
    rules live here because they need the resolved path:

    * `_normalize_candidate` runs on the ANCHORED form (as in
      `_check_anchored_attestations`) — it is itself the relative gate,
      so validating the bare form would drop everything.

    * **A missing file is only reported when its immediate parent
      directory exists.** An existing parent means the neighbourhood is
      real and the absence is genuine drift; a missing parent means the
      citation was probably never root-relative in the first place (prose
      noise, or a path written relative to a subdirectory the author was
      standing in). This is the same "existing parent proves the
      neighbourhood" test `_is_multi_segment_routelike` already uses, and
      it inherits the same bound: a whole-directory rename or delete
      takes its citations down with it and they are silently dropped
      rather than flagged. False negative over fabricated drift, as
      everywhere else in this module.

    Dropped citations leave no trace on the report, matching the
    ambiguous-truncation and spaced-prefix drops in `detect_path_drift`.
    A citation that EXISTS lands in `checked` only — never in `verified`,
    which means "attested AND present" — so a healthy body adds evidence
    without firing any surface's emit gate.

    Budget: `_MAX_ANCHORED_CITATION_STATS` stats per call, counted at the
    stat and not at the match, so filtered-out noise cannot exhaust the
    budget a real citation later in the body needs.

    Misses here ARE claim-anchored, and the filter above is what earns
    that. A raw `_RELATIVE_CITATION_RE` match is prose noise and would
    belong on the advisory side; what reaches the stat has survived the
    directory-segment, host-shape, extension, placeholder and
    live-parent rules AND resolves inside a worktree the memory itself
    recorded. That is a claim about one file in one tree, which is
    exactly the thing the verdict is allowed to escalate on. If the
    filter is ever loosened, this append is what has to be re-argued.
    """
    stats = 0
    for match in _RELATIVE_CITATION_RE.finditer(_bounded_scan_text(body)):
        if stats >= _MAX_ANCHORED_CITATION_STATS:
            break
        cited = match.group(1)
        if not _is_checkable_citation(cited):
            continue
        resolved = _join_under(root, cited)
        if resolved is None:
            continue
        norm = keys(resolved)
        if norm in seen:
            continue
        seen.add(norm)
        stats += 1
        if _path_exists(resolved):
            checked.append(resolved)
            continue
        parent = os.path.expanduser(os.path.dirname(resolved))
        if not os.path.isdir(parent):
            continue
        checked.append(resolved)
        missing.append(resolved)
        claim_anchored.append(resolved)


def _is_absolute_attestation(s: str) -> bool:
    """True when `s` is already anchored and needs no worktree to resolve.

    Mirrors `_normalize_candidate`'s own accepted anchors, and must keep
    mirroring them: a form this call treats as RELATIVE gets skipped when
    there is no worktree to join it to, so a disagreement between the two
    silently disables the check rather than erroring.

    Two consumers, one predicate, by design: `unverifiable_attestations`
    (the write gate) uses it to decide which attestations resolve
    without a root, and `_check_anchored_attestations`' `_anchored`
    (the read side) uses it to decide which attestations must NOT be
    joined onto the worktree root. A local restatement in either place
    re-opens the split-brain this docstring warns about — the read side
    once tested only ``startswith(("/", "~"))``, so a `$HOME/`-spelled
    attestation the write gate had accepted was root-joined into a
    phantom and escalated as `claim_anchored_missing` on every
    retrieval.

    That is not hypothetical. The first version tested only
    `startswith(("/", "~"))`, which classifies every Windows drive-absolute
    path (`C:\\Users\\me\\thing.toml`, and the forward-slash spelling
    `C:/Users/me/thing.toml` that `Path.as_posix()` produces) as relative —
    so on Windows every absolute attestation fell through the unanchored
    skip and the whole existence check was inert. Caught by CI on
    windows-latest, never by a POSIX developer machine.

    Deliberately NOT platform-gated: a drive path attested on Linux is
    refused there, which is the correct reading of "attest only what you
    checked here". The READ side stays lenient for the synced case.
    """
    if s.startswith(("/", "~")):
        return True
    # `$HOME/` / `${HOME}/` — the env-var spellings of `~/`, which
    # `_normalize_candidate` canonicalizes to `~` before validating.
    # Home-anchored, so no worktree is needed to resolve them.
    if s.startswith(("$HOME/", "${HOME}/")):
        return True
    # Windows drive-absolute, both separators — the same test
    # `_normalize_candidate` applies in its own trailing branch.
    return len(s) >= 3 and s[0].isalpha() and s[1] == ":" and s[2] in "/\\"


def unverifiable_attestations(
    verified_paths: tuple[str, ...] | list[str],
    *,
    worktree_root: str | Path | None = None,
    anchor: ClaimAnchor | None = None,
    stored: bool = False,
) -> list[str]:
    """Which `verified_paths` the ATTESTING machine cannot see right now.

    Write-side counterpart to `detect_path_drift`, and the asymmetry
    between them is the whole point. `_normalize_for_compare` documents
    why the READ side tolerates an attested path that isn't on disk: "it
    could have been verified from a different machine", which is true and
    load-bearing for `sync` — a memory attested against a path on host A
    is legitimately read on host B where that path does not exist.

    That tolerance does not transfer to the moment of attestation. A
    caller running `memory_verify(id, verified_paths=[...])` is claiming
    it checked reality HERE and NOW, so a path it cannot stat is not
    evidence of anything — and `Store.mark_verified` previously accepted
    it and stamped `last_verified_at`, which is how a memory reaches
    `fresh` on an attestation that was never true. The read side cannot
    recover this on its own: an ABSOLUTE attested path is only ever
    existence-checked when the body also names it (see
    `_normalize_attestations`' set-membership role in
    `detect_path_drift`), so an attestation the prose never references is
    inert forever.

    Returns the offending paths in input order, resolved to the form that
    was checked, so the caller can name them in an error. Relative paths
    with no `worktree_root` to anchor them are SKIPPED rather than
    reported: unanchored means "could not ask", and the project's
    standing rule is that could-not-ask never manufactures a negative
    verdict (the same distinction `compute_commit_drift` draws by
    returning None).

    `anchor` reads the attestations where design 3.6 reads them, in place
    of `worktree_root` (`_unverifiable_at`): relative ones at the anchor's
    root. `stored` says the list is the record's own (a preserving
    re-verify, a restore) rather than one the caller just passed: an
    absolute entry is then read at the live view of the checkout it was
    recorded under, and only a read that convicts can refuse it.
    """
    if anchor is not None:
        return _unverifiable_at(verified_paths, anchor, stored=stored)
    root = Path(worktree_root) if worktree_root is not None else None
    bad: list[str] = []
    for raw in verified_paths:
        stripped = raw.strip() if raw else ""
        if not stripped:
            continue
        # Documentation placeholders (`/etc/foo`, `/path/to/thing`) are
        # rejected by `_normalize_candidate` — correctly, for PROSE, where
        # they illustrate path shape rather than cite a file. That veto must
        # not carry over to an explicit attestation: these strings are
        # perfectly stat-able, they simply never name a real file, so
        # attesting one is definitionally the fabricated attestation this
        # function exists to catch. Checked before `_normalize_candidate`
        # gets a chance to silently drop them.
        if _is_placeholder_path(stripped):
            bad.append(stripped)
            continue
        if _is_absolute_attestation(stripped):
            resolved = _normalize_candidate(stripped)
        elif root is not None:
            # Anchor before validating, for the reason `_anchored` gives:
            # `_normalize_candidate` is itself the relative-path gate.
            resolved = _join_under(root, stripped)
        else:
            continue
        if resolved is None:
            # Everything `_normalize_candidate` still rejects here is a
            # shape that cannot be stat'd as a claim at all: a glob or
            # template (`/var/log/*.log`, `/opt/{svc}/data`), a URL, an
            # SSH remote (`user@host:/path`), an SMB share, or a
            # single-segment route (`/healthz`). Refusing those would
            # manufacture a failure out of a caller naming a shape; only a
            # concrete path can be present or absent. Skipped, not refused
            # — and the placeholder branch above is what keeps that
            # exemption from becoming a way to launder a bogus attestation.
            continue
        if not _path_exists(_normalize_for_compare(resolved)):
            bad.append(resolved)
    return bad


def _unverifiable_at(
    verified_paths: tuple[str, ...] | list[str],
    anchor: ClaimAnchor,
    *,
    stored: bool,
) -> list[str]:
    """`unverifiable_attestations` read through `anchor`.

    A fresh list is what the caller says it looked at here: an absolute
    entry is read as spelled, a relative one at the anchor's root, and a
    miss refuses where the read convicts or the root is the caller's own
    checkout (the tree the caller stands in). A stored list was attested
    in the home checkout: an absolute entry is read at the live view of the
    checkout it was recorded under, and only a read that convicts refuses
    it, since a stand-in's absence is no evidence about the home's file.
    Never a refusal: an entry with no live view to read it at, and a path
    a sparse checkout leaves out of its working tree."""
    bad: list[str] = []
    for raw in verified_paths:
        spelled = raw.strip() if raw else ""
        if not spelled:
            continue
        if _is_placeholder_path(spelled):
            bad.append(spelled)
            continue
        if _is_absolute_attestation(spelled):
            normalized = _normalize_candidate(spelled)
            if normalized is None:
                continue
            if stored:
                target, rule = anchor.absolute(normalized)
                if target is None:
                    continue
                refuses = rule in CONVICTING_ANCHORS
            else:
                target, refuses = normalized, True
        else:
            if anchor.root is None:
                continue
            joined = _join_under(anchor.root, spelled)
            if joined is None:
                continue
            target = joined
            refuses = anchor.convicting or (
                not stored and anchor.declared_root is not None
            )
        if _path_exists(_normalize_for_compare(target)):
            continue
        if refuses and not _absent_by_sparsity(target):
            bad.append(target)
    return bad


def _normalize_attestations(
    paths: tuple[str, ...] | list[str], keys: Callable[[str], str] = _claim_key
) -> set[str]:
    """Trim/validate an attestation list into a set of claim keys
    (`_claim_key`, through a read's `keys`) — the shared pipeline for
    `verified_paths` and `absent_paths` (see the call-site comment in
    `detect_path_drift` for why both lists must run through the exact same
    normalisation as the body candidates)."""
    out: set[str] = set()
    for raw in paths:
        if not raw:
            continue
        candidate = _normalize_candidate(raw)
        if candidate is None:
            continue
        out.add(keys(candidate))
    out.discard("")
    return out
