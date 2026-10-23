"""One request's admission (design 3.5, 8.2): the surface a site delivers
through, the caller's resolution, and the membership model that reads the
records' homes.

`membership.admitted` decides every home; an `Admission` only caches its
answers for the request, keyed on what `membership.relation` reads (the
home's project, chain and tenant, whether it is unresolved, and for a
global record the projects its scopes name), so the readers that walk
every record (`Store.brief_rows`, `scope_counts`, `category_rows`,
`document_frequencies`) and the ranked set decide by one predicate
(`admits_row`). They decide on the project a record's ``homes`` row names,
which the model reads into a `membership.Home` once per project, and for a
record written outside any project on the projects its scopes name
(`membership.Membership.named`, RS-F7 part 2). A record with no home row
is admitted nowhere. A caller
whose resolution raised, or whose location nothing could be read for
(INDETERMINATE) or is still being read (DEFERRED), is admitted nothing on
every surface, and `nothing` names the outcome for the row that says so.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .membership import (
    GLOBAL,
    SAME_PROJECT,
    SILENT,
    SUB_PROJECT,
    SURFACES,
    UNKNOWN,
    UNRESOLVED_STATES,
    Home,
    Membership,
    Resolution,
    admitted,
    relation,
)

if TYPE_CHECKING:
    from .location import Caller

__all__ = [
    "NO_RESOLUTION",
    "Admission",
    "CountedRecords",
    "DriftScope",
    "admitted_nothing",
    "recorded_caller",
]

#: The outcome an admission names when the caller's resolution raised.
NO_RESOLUTION = "no_resolution"

_Key = tuple[str, tuple[str, ...], str | None, bool, frozenset[str]]


class DriftScope(Protocol):
    """What a commit-drift pass reads under (`_response._DriftPass`): which
    records' commit drift counts in the caller's repository. An
    `Admission`, or the `CountedRecords` one decided."""

    def prime(self, record_ids: Iterable[str]) -> None: ...

    def drift_counts(self, record_id: str) -> bool: ...


@dataclass(frozen=True)
class CountedRecords:
    """The records whose commit drift counts for a caller, decided once
    (`Admission.counted`) and frozen: what a drift pass on another thread
    reads under, since the membership model is not shared across threads
    (the session brief's background fill), and what an offline reader
    names when it counts the records it holds."""

    ids: frozenset[str]

    def prime(self, record_ids: Iterable[str]) -> None:
        """Nothing to read: the records were decided."""

    def drift_counts(self, record_id: str) -> bool:
        return record_id in self.ids


def admitted_nothing(outcome: str) -> str:
    """The text a row carries when its caller was admitted nothing."""
    return f"admitted nothing: {outcome}"


def recorded_caller(model: Membership, path: str) -> Resolution | None:
    """The caller standing at `path` as the store recorded it, read without
    writing a row: the checkout with a recorded view at `path` or at the
    nearest directory above it (its newest view there), that checkout's
    project with merges followed, the project's chain and tenant
    (outcome SILENT, rule ``recorded_view``). None when no view was
    recorded at `path` or above it. For a reader that replays what a
    caller saw over a store it does not resolve in (the recall review,
    the parity bench)."""
    wanted = os.path.normpath(path)
    found: tuple[int, int, str, str] | None = None
    for k in model.checkouts():
        for v in k.views:
            root = os.path.normpath(v.path)
            if wanted != root and not wanted.startswith(root.rstrip(os.sep) + os.sep):
                continue
            candidate = (len(root), v.order, k.id, k.project)
            if found is None or candidate[:2] > found[:2]:
                found = candidate
    if found is None:
        return None
    _length, _order, checkout, recorded = found
    project = model.canonical(recorded) or recorded
    try:
        tenant = model.project(project).tenant
    except KeyError:
        tenant = None
    return Resolution(
        outcome=SILENT,
        project=project,
        checkout=checkout,
        rule="recorded_view",
        chain=tuple(model.chain_of(project)),
        tenant=tenant,
    )


class Admission:
    """The admission of one surface for one caller, for one request.

    `caller` is the caller's resolution (None when it raised); `model` the
    membership model whose rows that resolution read. Not shared across
    threads, as the model is not."""

    def __init__(
        self, surface: str, caller: Resolution | None, model: Membership | None
    ) -> None:
        if surface not in SURFACES:
            raise ValueError(f"unknown surface {surface!r}")
        self.surface = surface
        self.caller = caller
        self.model = model
        self._by_home: dict[_Key, bool] = {}
        self._project_homes: dict[str, Home] = {}
        self._by_project: dict[str, bool] = {}
        self._record_homes: dict[str, Home | None] = {}
        self._named: dict[tuple[str, ...], frozenset[str]] = {}

    @classmethod
    def of(cls, surface: str, caller: Caller) -> Admission:
        """The admission of `surface` for a caller `location.resolve_caller`
        resolved."""
        return cls(surface, caller.resolution, caller.model)

    def on(self, surface: str) -> Admission:
        """The same caller's admission on another surface, sharing the homes
        this one has read."""
        other = Admission(surface, self.caller, self.model)
        other._project_homes = self._project_homes
        other._record_homes = self._record_homes
        other._named = self._named
        return other

    # -- the caller -----------------------------------------------------------

    @property
    def nothing(self) -> str | None:
        """The caller's outcome when it is admitted nothing on any surface
        (its resolution raised, or read nothing, or is deferred); None
        otherwise."""
        if self.caller is None or self.model is None:
            return NO_RESOLUTION
        if self.caller.outcome in UNRESOLVED_STATES or self.caller.project is None:
            return self.caller.outcome
        return None

    @property
    def in_project(self) -> bool:
        """Whether the caller stands in a project: its resolution names one
        that is not ``global``."""
        return (
            self.nothing is None
            and self.caller is not None
            and self.caller.project not in (None, GLOBAL)
        )

    # -- homes ----------------------------------------------------------------

    def project_home(self, recorded: str) -> Home:
        """The home a record whose ``homes`` row names `recorded` has, as
        `Membership.homes` reads it: the project it stands for now (merges
        followed), that project's chain and tenant, and whether it is the
        provisional project of an unresolved location (decision B6)."""
        found = self._project_homes.get(recorded)
        if found is not None:
            return found
        project = recorded
        chain: tuple[str, ...] = ()
        tenant: str | None = None
        unresolved = False
        if recorded != GLOBAL and self.model is not None:
            project = self.model.canonical(recorded) or recorded
            chain = tuple(self.model.chain_of(project))
            try:
                known = self.model.project(project)
            except KeyError:
                known = None
            if known is not None:
                tenant = known.tenant
                unresolved = known.provisional in UNRESOLVED_STATES
        home = Home(
            record_id="",
            kind="",
            project=project,
            checkout=None,
            rule="",
            decided="",
            recorded_project=recorded,
            chain=chain,
            tenant=tenant,
            unresolved=unresolved,
        )
        self._project_homes[recorded] = home
        return home

    def prime(self, record_ids: Iterable[str]) -> None:
        """Read the home rows of `record_ids` not read yet, in one read
        (`Membership.homes`); a record with none is remembered as having
        none."""
        wanted = [
            rid for rid in dict.fromkeys(record_ids) if rid not in self._record_homes
        ]
        if not wanted:
            return
        found = self.model.homes(wanted) if self.model is not None else {}
        for rid in wanted:
            self._record_homes[rid] = found.get(rid)

    def home(self, record_id: str) -> Home | None:
        """The record's home, or None when it has no home row."""
        self.prime((record_id,))
        return self._record_homes.get(record_id)

    # -- decisions ------------------------------------------------------------

    def admits(self, home: Home | None) -> bool:
        """`membership.admitted` for this surface and caller; False for a
        record with no home and for a caller admitted nothing."""
        if home is None or self.caller is None or self.model is None:
            return False
        key: _Key = (home.project, home.chain, home.tenant, home.unresolved, home.named)
        found = self._by_home.get(key)
        if found is None:
            found = admitted(self.surface, home, self.caller)
            self._by_home[key] = found
        return found

    def admits_project(self, recorded: str | None) -> bool:
        """Whether a record whose ``homes`` row names `recorded` is admitted
        (None: the record has no home row, admitted nowhere)."""
        if recorded is None:
            return False
        found = self._by_project.get(recorded)
        if found is None:
            found = self.admits(self.project_home(recorded))
            self._by_project[recorded] = found
        return found

    def admits_row(self, scopes: Sequence[str], recorded: str | None) -> bool:
        """Whether a record with `scopes` whose ``homes`` row names
        `recorded` is admitted: `admits_project`, except that a record
        written outside any project is decided with the projects its
        scopes name (`membership.relation`'s ``global_named``)."""
        if recorded != GLOBAL or self.model is None:
            return self.admits_project(recorded)
        key = tuple(scopes)
        named = self._named.get(key)
        if named is None:
            named = self.model.named(scopes)
            self._named[key] = named
        if not named:
            return self.admits_project(recorded)
        return self.admits(dataclasses.replace(self.project_home(GLOBAL), named=named))

    def admits_id(self, record_id: str) -> bool:
        """Whether the record is admitted, read from its home row (a global
        record's with the projects its scopes name)."""
        home = self.home(record_id)
        if home is not None and home.named:
            return self.admits(home)
        return self.admits_project(home.recorded_project if home is not None else None)

    def admitted_ids(self, record_ids: Sequence[str]) -> list[str]:
        """The admitted ones of `record_ids`, in their order, their home rows
        read in one read."""
        self.prime(record_ids)
        return [rid for rid in record_ids if self.admits_id(rid)]

    def relation(self, home: Home | None) -> str:
        """`membership.relation` of the home to the caller; ``unknown`` for
        a record with no home and for a caller nothing was read for."""
        if home is None or self.caller is None:
            return UNKNOWN
        return relation(home, self.caller)

    def same_project(self, record_id: str) -> bool:
        """Whether the record is of the caller's project: its home on the
        caller's chain or a sub-project of it (design 3.5, D13)."""
        if self.nothing is not None:
            return False
        home = self.home(record_id)
        if home is None:
            return False
        return self.relation(home) in (SAME_PROJECT, SUB_PROJECT)

    def drift_counts(self, record_id: str) -> bool:
        """Whether the record's commit drift is counted in the caller's
        repository (`_response._DriftPass.lookup`): this surface admits its
        home, and that home is a project's; a record written outside any
        project (``global``) has no repository whose commits speak for it."""
        home = self.home(record_id)
        if home is None or not self.admits_project(home.recorded_project):
            return False
        return home.project != GLOBAL

    def counted(self, record_ids: Sequence[str]) -> CountedRecords:
        """`drift_counts` decided now for `record_ids`, their homes read in
        one read, as a `CountedRecords` another thread can read."""
        self.prime(record_ids)
        return CountedRecords(
            frozenset(rid for rid in record_ids if self.drift_counts(rid))
        )

    def admitted_projects(self) -> frozenset[str] | None:
        """The projects (merges followed) whose records this surface admits
        for the caller, read once from the project table through
        `membership.admitted`; None when the caller is admitted nothing.
        Whether ``global`` records are admitted is `admits_global`."""
        if self.nothing is not None or self.model is None:
            return None
        return frozenset(
            p.id for p in self.model.projects() if self.admits_project(p.id)
        )

    def admits_global(self) -> bool:
        """Whether records written outside any project are admitted."""
        return self.admits_project(GLOBAL)
