"""Membership: which project a memory belongs to (RS-F6, design 2 and 3).

Every membership decision is made here or in the family this module
binds (its last paragraph). A caller hands in a `Location` (what the
capture read about its folder: the git directory's object, its object
store, HEAD and the ref tips, every remote, the checkout token, the init
stamps, the project files from the cwd up, the evicted-file state, the
content manifest, and a reader of the repository's history) and gets a
`Resolution`: SILENT with its project and checkout, ASK ONCE with a
durable proposal, NEW, or global, indeterminate or deferred, with the
witnesses and the log rows that decided it.

The ladder (design 3.2), the first rung that answers deciding:

0. nothing readable: nothing from any project, line L8 and a
   ``resolution_indeterminate`` row; an evicted HEAD or config at first
   sight: line L9 and a ``resolution_deferred`` row; outside any
   repository and any project file: global.
1. a declaration (an answered proposal, ``project init``, ``memory
   move``): already in the tables the rungs below read.
2. O, the same checkout; a new path adds a view.
3. S, another checkout of the same object store (a linked worktree);
   where no birth time proves the store, git's own pointers can
   (``common_object+pointer``).
4. two independent witnesses agreeing, nothing contradicting: joined
   (B re-keys the recorded checkout; ``git init`` in a folder project
   re-keys the folder checkout). Several projects qualifying: the stronger
   evidence, then the most specific line, else ASK ONCE. Under the strict
   switch (decision D8) a join resting on history without F or B asks.
5. a contradiction, one witness, or content alone: ASK ONCE.
6. no witness: NEW, line L2.

The witnesses (design 2.2): O the git directory's object, S the object
store, H the project's witness commits held (Hf with their fingerprints
under other ids, Hb their file contents; the three are one history
witness), R a remote key of the project, D a remote declared for shallow
clones, F a bound project file, T a checkout token of the project, B the
path of a non-live primary checkout with its commits, C the content.

`relation` and `admitted` are the one membership function and the fixed
surface policy of design 3.5; `Membership.handoff`, `anchor` and
`translate` are the run-state and anchoring rules of 3.6; `why` and
`health` are design 8.10's audit and health data. Lines L1 to L10 (design
5.4) are written by the `line_*` functions and nowhere else.

The model reads the repository only through the `HistoryReader` a
`Location` carries and the filesystem only through the `Liveness` check a
`Membership` is given (`location.path_liveness` by default), so every
rung runs on constructed locations. Witness O without birth time is one
rule, `location.continuity`, which rung 2 and the liveness of a view both
apply (decision B18). Every table change is a mutation row of the store
(design 8.5), every decision a membership row (design 8.10); a resolution
runs in one store transaction.

The model is a family of modules, each under the package's 2,000-line
bound (ENG-96), and this module re-exports every name callers import from
it. It keeps the `Membership` class: its state, plumbing, machines,
projects, evidence, checkouts, history, proposals and `why`, with the
method groups it binds by name from `membership_resolve` (the ladder),
`membership_declare` (declarations and the B6 writes),
`membership_queries` (homes, surfaces, run-state, anchoring and health)
and `membership_portable` (export and import). Below them,
`membership_records` holds the vocabulary, the records, `relation`,
`admitted` and the path helpers, `membership_lines` the lines and
`membership_rows` the row conversions.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from . import (
    membership_declare,
    membership_portable,
    membership_queries,
    membership_resolve,
)
from .location import continuity, path_liveness, path_liveness_many
from .membership_lines import (
    line_boundary_added,
    line_boundary_dropped,
    line_deferred,
    line_file_contradiction,
    line_first_sight,
    line_import,
    line_joined,
    line_legacy,
    line_new,
    line_remote_diverged,
    line_subproject,
    line_unreadable,
    line_unresolved,
    _named,
)
from .membership_records import (
    ANSWERED as ANSWERED,
    ASK,
    BARE as BARE,
    BLOBS_PER_WITNESS as BLOBS_PER_WITNESS,
    DEFERRED,
    EXPORT_FORMAT,
    EXPORT_FORMAT_VERSION,
    EXPORT_WITNESSES,
    FILE_CONTRADICTION,
    FIRST_SIGHT,
    FOLDER,
    GLOBAL,
    GLOBAL_NAMED,
    GLOBAL_OUTCOME,
    GLOBAL_RELATION,
    GONE as GONE,
    HB,
    HF,
    IMPORT,
    IMPORT_CONTRADICTION,
    IMPORT_ONE_WITNESS,
    IMPORT_SEVERAL,
    INDETERMINATE,
    IN_PROJECT_POLICY,
    KEEP_OLDEST as KEEP_OLDEST,
    LEGACY as LEGACY,
    LEGACY_NO_LIVE_CHECKOUT,
    LEGACY_SEPARATE,
    LINKED as LINKED,
    LINKS,
    LIVE as LIVE,
    LIVE_LEGACY as LIVE_LEGACY,
    NEW,
    OPEN as OPEN,
    OTHER_MACHINE,
    OTHER_PROJECT,
    OTHER_TENANT,
    OUTSIDE,
    OUTSIDE_POLICY,
    POOL_PREFILTER,
    PRIMARY,
    PROJECT_SCOPE,
    PROMPT_RECALL,
    REMOTE_DIVERGED,
    REPLACED as REPLACED,
    REPO,
    REVIEW_STRATA,
    SAME as SAME,
    SAME_PROJECT,
    SEARCH_AUTO_SCOPE,
    SEARCH_UNSCOPED,
    SESSION_BRIEF,
    SILENT,
    SUBPROJECT_CONTRADICTION,
    SUB_PROJECT,
    SUPERSEDED,
    SURFACES,
    UNKNOWN,
    UNPROVEN as UNPROVEN,
    UNREACHABLE as UNREACHABLE,
    UNREADABLE,
    UNRESOLVED,
    UNRESOLVED_LOCATION,
    UNRESOLVED_STATES,
    VIEW_REFRESH,
    WALK_LIMIT as WALK_LIMIT,
    WITNESS_BOUND as WITNESS_BOUND,
    B,
    C,
    Checkout,
    D,
    Declaration,
    Declared,
    EpisodeRef,
    F,
    H,
    Handoff,
    Health,
    HistoryReader,
    Home,
    ImportedProject,
    InitStamp,
    Liveness,
    Location,
    Machine,
    Manifest,
    O,
    ObjectKey,
    PortableIdentity,
    Project,
    ProjectFile,
    ProjectFileRef,
    Proposal,
    R,
    RecallAdmission,
    Resolution,
    S,
    T,
    TokenWriter,
    UnresolvedProject,
    View,
    Why,
    admitted,
    legacy_asks,
    manifest_match,
    relation,
    _INITS,
    _LIVE_STATES,
    _basename,
    _common_by_pointer,
    _live_key,
    _dir_key,
    _norm,
    _path_under,
    _same_number,
    _same_path,
)
from .membership_rows import (
    _checkout_from,
    _checkout_row,
    _jsonable,
    _project_from,
    _project_row,
    _proposal_from,
    _proposal_row,
    _view_from,
)
from .models import utcnow
from .store import RecordHome, Store
from .time_utils import isoformat_utc, parse_event_ts

if TYPE_CHECKING:
    from .log import LogRow
    from .machine import MachineIdentity

#: The location's own liveness, as imported: a model reading it reads many
#: views at once through `path_liveness_many`; a test that swaps the name
#: here for its own reader is read one view at a time.
_PATH_LIVENESS = path_liveness

# ---------------------------------------------------------------------------
# The model over the store
# ---------------------------------------------------------------------------


class Membership:
    """The membership model of one machine over one store. Not shared
    across threads: each worker keeps its own, over its own store
    connection. `liveness` reads what a view's path holds now
    (`location.path_liveness` by default, witness O read at the path);
    `token_writer` gives a checkout a token
    of its own where a rule asks for one (without it the resolution or
    declaration says so and the caller writes it, then calls
    `token_written`); `now` stamps rows and is read by no rule."""

    def __init__(
        self,
        store: Store,
        machine: str,
        *,
        liveness: Liveness | None = None,
        token_writer: TokenWriter | None = None,
        now: Callable[[], datetime] = utcnow,
        session: str | None = None,
    ) -> None:
        self._store = store
        self.machine = machine
        self._liveness: Liveness = liveness or path_liveness
        #: The default liveness reads many views on one thread, each bounded
        #: (`location.path_liveness_many`); any other is read in a pool.
        self._liveness_many: (
            Callable[[Sequence[tuple[Checkout, View]]], list[str | None]] | None
        ) = path_liveness_many if self._liveness is _PATH_LIVENESS else None
        self._token_writer = token_writer
        self._now = now
        self._session = session
        self._projects: dict[str, Project] | None = None
        self._rows: list[int] = []
        self._remint: str | None = None
        #: Liveness read before the write transaction (ENG-156), by what
        #: each read: None outside a resolution or a `liveness_held` span.
        self._live: dict[tuple[object, ...], str] | None = None

    @classmethod
    def open(
        cls,
        store: Store,
        identity: MachineIdentity,
        *,
        liveness: Liveness | None = None,
        token_writer: TokenWriter | None = None,
        now: Callable[[], datetime] = utcnow,
        session: str | None = None,
    ) -> Membership:
        """The model for this machine, its machine row written first: a
        new id carried from another machine writes ``machine_cloned``
        before any resolution (design 2.1, E26)."""
        model = cls(
            store,
            identity.id,
            liveness=liveness,
            token_writer=token_writer,
            now=now,
            session=session,
        )
        model.record_machine(identity)
        return model

    # -- plumbing --------------------------------------------------------

    @contextlib.contextmanager
    def _call(self) -> Iterator[None]:
        """One public call: one store transaction (the batch's, when the
        caller holds one), a fresh read of the projects, and the rows it
        writes collected."""
        self._projects = None
        self._rows = []
        self._remint = None
        if self._store.in_batch:
            yield
            return
        with self._store.batch():
            yield

    def _stamp(self) -> str:
        return isoformat_utc(self._now().astimezone(timezone.utc))

    def _put(self, kind: str, row: Mapping[str, Any]) -> None:
        written = self._store.put_membership(kind, row, session=self._session)
        self._rows.append(written.seq)

    def _delete(self, kind: str, key: Mapping[str, Any]) -> None:
        written = self._store.delete_membership(kind, key, session=self._session)
        self._rows.append(written.seq)

    def _event(self, event_kind: str, /, **payload: Any) -> None:
        written = self._store.record_membership(
            event_kind, _jsonable(payload), session=self._session
        )
        self._rows.append(written.seq)

    # -- machines ------------------------------------------------------------

    def record_machine(self, identity: MachineIdentity) -> None:
        """The machine row for `identity`, with ``machine_cloned`` naming
        its predecessor when it was minted for a state directory carried
        from another machine, or ``machine_facts_updated`` when a fact
        changed and the id stayed. Idempotent: a machine already recorded
        with these facts writes nothing."""
        facts = identity.facts.to_dict()
        with self._call():
            rows = self._store.membership_rows("machines", where={"id": identity.id})
            if not rows:
                self._put(
                    "machine",
                    {
                        "id": identity.id,
                        "facts_json": facts,
                        "predecessor": identity.predecessor,
                        "minted": self._stamp(),
                    },
                )
                if identity.predecessor is not None:
                    self._event(
                        "machine_cloned",
                        old=identity.predecessor,
                        new=identity.id,
                        recorded=(
                            None
                            if identity.recorded is None
                            else identity.recorded.to_dict()
                        ),
                        seen=facts,
                    )
                return
            row = rows[0]
            if row["facts_json"] != facts:
                self._put("machine", {**row, "facts_json": facts})
                self._event(
                    "machine_facts_updated",
                    machine=identity.id,
                    recorded=row["facts_json"],
                    seen=facts,
                )

    def machines(self) -> list[Machine]:
        return [
            Machine(
                id=str(r["id"]),
                facts=dict(r["facts_json"] or {}),
                predecessor=r["predecessor"],
                minted=str(r["minted"]),
            )
            for r in self._store.membership_rows("machines")
        ]

    # -- projects --------------------------------------------------------------

    def _all_projects(self) -> dict[str, Project]:
        if self._projects is None:
            self._projects = {
                str(r["id"]): _project_from(r)
                for r in self._store.membership_rows("projects")
            }
        return self._projects

    def project(self, project_id: str) -> Project:
        """The project row, as recorded (merges not followed)."""
        found = self._all_projects().get(project_id)
        if found is None:
            raise KeyError(project_id)
        return found

    def projects(self) -> list[Project]:
        """Every project that is not merged into another, by id."""
        return sorted(
            (p for p in self._all_projects().values() if p.merged_into is None),
            key=lambda p: p.id,
        )

    def canonical(self, project_id: str | None) -> str | None:
        """The project `project_id` stands for now: its merges followed."""
        seen: set[str] = set()
        projects = self._all_projects()
        while (
            project_id is not None and project_id != GLOBAL and project_id in projects
        ):
            nxt = projects[project_id].merged_into
            if nxt is None or project_id in seen:
                return project_id
            seen.add(project_id)
            project_id = nxt
        return project_id

    def _canon(self, project_id: str) -> str:
        found = self.canonical(project_id)
        return project_id if found is None else found

    def named(self, scopes: Iterable[str]) -> frozenset[str]:
        """The projects (merges followed) a record's ``projects:<name>``
        scopes name: every project whose name is ``<name>``, compared
        case-folded (RS-F7 part 2; Stage A's replay of the rule reads the
        same counts with the names compared exactly or folded). Empty when
        no scope names a project."""
        wanted = {
            scope[len(PROJECT_SCOPE) :].casefold()
            for scope in scopes
            if scope.startswith(PROJECT_SCOPE) and len(scope) > len(PROJECT_SCOPE)
        }
        if not wanted:
            return frozenset()
        return frozenset(
            self._canon(p.id)
            for p in self._all_projects().values()
            if p.name.casefold() in wanted
        )

    def chain_of(self, project_id: str | None) -> list[str]:
        """The project and its parents, nearest first."""
        out: list[str] = []
        pid = self.canonical(project_id)
        projects = self._all_projects()
        while pid is not None and pid != GLOBAL and pid not in out and pid in projects:
            out.append(pid)
            pid = self.canonical(projects[pid].parent)
        return out

    def _members(self, project_id: str) -> list[str]:
        """Every project id that stands for `project_id` (itself and the
        projects merged into it)."""
        target = self._canon(project_id)
        return sorted(p for p in self._all_projects() if self.canonical(p) == target)

    def _name(self, project_id: str) -> str:
        pid = self._canon(project_id)
        return _named(self.project(pid).name, pid)

    def _put_project(self, p: Project) -> None:
        self._put("project", _project_row(p))
        self._all_projects()[p.id] = p

    def _mint_project(
        self,
        name: str,
        *,
        reason: str,
        provisional: str | None = None,
        parent: str | None = None,
        tenant: str | None = None,
        folder: bool = False,
        boundary: bool = False,
    ) -> Project:
        pid = self._store.next_membership_id("projects", "P")
        p = Project(
            id=pid,
            name=name,
            merged_into=None,
            provisional=provisional,
            parent=parent,
            tenant=pid if boundary else tenant,
            folder=folder,
            boundary=boundary,
            minted=self._stamp(),
        )
        self._put_project(p)
        self._event(
            "project_minted",
            project=p.id,
            name=name,
            reason=reason,
            provisional=provisional,
            parent=parent,
            tenant=p.tenant,
        )
        return p

    def merge(self, source: str, target: str, *, evidence: str) -> None:
        """One project row (design 8.5): `source` merged into `target`.
        No record and no home is rewritten; every read follows the merge."""
        with self._call():
            self._merge(source, target, evidence=evidence)

    def _merge(self, source: str, target: str, *, evidence: str) -> None:
        src, dst = self._canon(source), self._canon(target)
        if src == dst:
            return
        p = self.project(src)
        self._put_project(dataclasses.replace(p, merged_into=dst, provisional=None))
        self._event("project_merged", project=src, into=dst, evidence=evidence)
        if p.boundary:
            # The file that made it a tenant names the merged project now.
            self._make_boundary(dst)

    # -- evidence rows -----------------------------------------------------------

    def _witness_rows(self, project_id: str | None = None) -> list[dict[str, Any]]:
        if project_id is None:
            return self._store.membership_rows(
                "project_witnesses", order_by=("arrival",)
            )
        return self._store.membership_rows(
            "project_witnesses",
            column_in=("project", self._members(project_id)),
            order_by=("arrival",),
        )

    def _has_witness(self, project_id: str, commit: str) -> bool:
        target = self._canon(project_id)
        return any(
            self.canonical(str(r["project"])) == target
            for r in self._store.membership_rows(
                "project_witnesses", where={"commit_id": commit}
            )
        )

    def _has_witnesses(self, project_id: str) -> bool:
        return bool(self._witness_rows(project_id))

    def add_witness(
        self,
        project_id: str,
        commit: str,
        *,
        source: str,
        history: HistoryReader | None = None,
    ) -> None:
        """Record `commit` as a witness of the project (HEAD at a sighting,
        a write, a verify), with its fingerprint and up to
        `BLOBS_PER_WITNESS` blob ids read through `history`. The project
        keeps at most `WITNESS_BOUND`: past it, the oldest witness after
        the `KEEP_OLDEST` first is released with its blobs (D15)."""
        with self._call():
            self._add_witness(project_id, commit, source=source, history=history)

    def _add_witness(
        self,
        project_id: str,
        commit: str,
        *,
        source: str,
        history: HistoryReader | None,
    ) -> None:
        pid = self._canon(project_id)
        if not commit or self._has_witness(pid, commit):
            return
        fingerprint = history.fingerprint(commit) if history is not None else None
        blobs = sorted(set(history.blobs(commit))) if history is not None else []
        self._put(
            "project_witness",
            {
                "project": pid,
                "commit_id": commit,
                "arrival": self._store.next_membership_number(
                    "project_witnesses", "arrival"
                ),
                "source": source,
                "fingerprint": fingerprint,
            },
        )
        kept = {
            str(r["blob"])
            for r in self._store.membership_rows(
                "project_blobs", where={"project": pid}
            )
        }
        for blob in [b for b in blobs if b not in kept][:BLOBS_PER_WITNESS]:
            self._put("project_blob", {"project": pid, "blob": blob, "witness": commit})
        self._event("witness_put", project=pid, commit=commit, source=source)
        own = self._store.membership_rows(
            "project_witnesses", where={"project": pid}, order_by=("arrival",)
        )
        if len(own) > WITNESS_BOUND:
            drop = str(own[KEEP_OLDEST]["commit_id"])
            self._delete("project_witness", {"project": pid, "commit_id": drop})
            for row in self._store.membership_rows(
                "project_blobs", where={"project": pid, "witness": drop}
            ):
                self._delete("project_blob", {"project": pid, "blob": row["blob"]})
            self._event(
                "witness_released", project=pid, commit=drop, bound=WITNESS_BOUND
            )

    def _remotes_of(self, project_id: str) -> set[str]:
        return {
            str(r["remote_key"])
            for r in self._store.membership_rows(
                "project_remotes", column_in=("project", self._members(project_id))
            )
        }

    def _add_remotes(self, project_id: str, keys: Iterable[str]) -> None:
        pid = self._canon(project_id)
        for key in sorted(set(keys) - self._remotes_of(pid)):
            self._put("project_remote", {"project": pid, "remote_key": key})

    def file_binding(self, file_id: str) -> str | None:
        """The project a project file's id is bound to (merges followed),
        or None when it is bound to none."""
        self._projects = None
        return self._file_binding(file_id)

    def _file_binding(self, file_id: str) -> str | None:
        rows = self._store.membership_rows("project_files", where={"file_id": file_id})
        return self.canonical(str(rows[0]["project"])) if rows else None

    def _bind_file(
        self,
        file_id: str,
        project_id: str,
        *,
        evidence: str,
        boundary: bool = False,
        checkout: str | None = None,
        directory: str | None = None,
    ) -> None:
        """Bind a project file's id to a project. A file carrying
        ``boundary = true`` makes that project a tenant wherever the file
        lies (B9). A file inside a repository below its root names the
        checkout and folder it was bound in, which `project new` reads to
        take the checkout's parts along (B10)."""
        pid = self._canon(project_id)
        self._put(
            "project_file", {"file_id": file_id, "project": pid, "evidence": evidence}
        )
        where: dict[str, str] = {}
        if checkout is not None:
            where["checkout"] = checkout
        if directory is not None:
            where["dir"] = directory
        self._event(
            "project_file_bound",
            file_id=file_id,
            project=pid,
            evidence=evidence,
            **where,
        )
        if boundary:
            self._make_boundary(pid)

    def _file_flag(self, f: ProjectFileRef, project_id: str) -> list[str]:
        """B12: a sighting of a bound file whose ``boundary`` flag differs
        from its project's. A flag added to the file makes the project a
        tenant now (a logged project row and a line); a flag gone from it
        does not lift the boundary by itself: the line names the command
        that does, `project init --rebind` in that folder, which binds a
        fresh file without it."""
        pid = self._canon(project_id)
        p = self.project(pid)
        if f.boundary and not p.boundary:
            self._make_boundary(pid)
            return [line_boundary_added(f.path, p.name, pid)]
        if p.boundary and not f.boundary:
            return [line_boundary_dropped(f.path, p.name, pid, f.dir)]
        return []

    def _lift_boundary(self, project_id: str, outer: str | None) -> None:
        """B12: `project init --rebind` bound a fresh file without the flag
        to a boundary project: it is no tenant any more, and it and the
        projects that sat inside it return to `outer`, the tenant around
        it (its parent's, or for a project with no parent the one the
        declaration read around the folder). Each changed row is logged."""
        pid = self._canon(project_id)
        p = self.project(pid)
        if not p.boundary:
            return
        if p.parent is not None:
            outer = self.project(self._canon(p.parent)).tenant
        self._put_project(dataclasses.replace(p, boundary=False, tenant=outer))
        for q in list(self._all_projects().values()):
            if q.merged_into is None and q.id != pid and q.tenant == pid:
                self._put_project(dataclasses.replace(q, tenant=outer))

    def _make_boundary(self, project_id: str) -> None:
        """The project becomes a tenant (B9; design 2.1, a file with
        ``boundary = true``): its records and its sub-projects' sit inside
        it, and nothing crosses it, D13's sharing included. Each changed
        project row is logged."""
        pid = self._canon(project_id)
        p = self.project(pid)
        if p.boundary:
            return
        self._put_project(dataclasses.replace(p, boundary=True, tenant=pid))
        for q in list(self._all_projects().values()):
            if q.merged_into is not None or q.id == pid:
                continue
            chain = self.chain_of(q.id)
            if pid not in chain:
                continue
            inner = chain[: chain.index(pid)]
            if any(self.project(x).boundary for x in inner) or q.tenant == pid:
                continue
            self._put_project(dataclasses.replace(q, tenant=pid))

    # -- checkouts -------------------------------------------------------------------

    def _checkouts(
        self,
        *,
        where: Mapping[str, Any] | None = None,
        column_in: tuple[str, Collection[Any]] | None = None,
    ) -> list[Checkout]:
        rows = self._store.membership_rows(
            "checkouts", where=where, column_in=column_in
        )
        if not rows:
            return []
        views: dict[str, list[View]] = defaultdict(list)
        for v in self._store.membership_rows(
            "checkout_views", column_in=("checkout", [r["id"] for r in rows])
        ):
            views[str(v["checkout"])].append(_view_from(v))
        return [_checkout_from(r, views[str(r["id"])]) for r in rows]

    def checkout(self, checkout_id: str) -> Checkout:
        found = self._checkouts(where={"id": checkout_id})
        if not found:
            raise KeyError(checkout_id)
        return found[0]

    def checkouts(self) -> list[Checkout]:
        return self._checkouts()

    def _checkouts_of(self, project_id: str) -> list[Checkout]:
        """The project's checkouts, merges followed; a path written at
        while nothing could be read (decision B6) is none of them."""
        return [
            k
            for k in self._checkouts(column_in=("project", self._members(project_id)))
            if k.kind != UNRESOLVED
        ]

    def _put_checkout(self, k: Checkout) -> Checkout:
        self._put("checkout", _checkout_row(k))
        return k

    def _refresh_object(self, k: Checkout, loc: Location) -> Checkout:
        """The checkout's object key, and its common key where that is the
        same object, taken as `loc` reads them now: the same device and
        inode number with another birth time (decision B20, a restore
        that got the number back). Nothing to do when the keys agree.
        Logged as ``checkout_object_refreshed``, which is no re-key: the
        object is the one recorded, read anew."""
        new = loc.object_key
        old = k.object_key
        if new is None or old is None or old == new:
            return k
        common = k.common_object_key
        if common is not None and _same_number(common, old):
            common = loc.common_object_key if loc.common_object_key else common
        k = self._put_checkout(
            dataclasses.replace(k, object_key=new, common_object_key=common)
        )
        self._event(
            "checkout_object_refreshed",
            checkout=k.id,
            path=loc.root,
            old_object=dataclasses.asdict(old),
            new_object=dataclasses.asdict(new),
        )
        return k

    def _put_view(self, v: View) -> None:
        self._put(
            "checkout_view",
            {
                "checkout": v.checkout,
                "machine": v.machine,
                "path": v.path,
                "first_seen": v.first_seen,
                "last_seen": v.last_seen,
                "seen_order": v.order,
            },
        )

    def _register(self, loc: Location, project_id: str, rule: str) -> Checkout:
        kid = self._store.next_membership_id("checkouts", "K")
        stamp = self._stamp()
        token = loc.token or loc.token_written
        view = View(
            checkout=kid,
            machine=loc.machine,
            path=_norm(loc.root or loc.cwd),
            first_seen=stamp,
            last_seen=stamp,
            order=self._store.next_membership_number("checkout_views", "seen_order"),
        )
        keep_manifest = (not loc.born and not token) or loc.kind == FOLDER
        k = Checkout(
            id=kid,
            project=project_id,
            machine=loc.machine,
            kind=loc.kind or PRIMARY,
            object_key=loc.object_key,
            common_object_key=loc.common_object_key,
            git_dir=loc.git_dir,
            token=token,
            token_status=loc.token_status,
            stamps=loc.stamps,
            last_head=loc.head,
            branch=loc.branch,
            remotes=tuple(sorted(loc.remote_keys)),
            manifest=loc.manifest if keep_manifest else None,
            legacy=False,
            retired=False,
            registered=stamp,
            views=(view,),
        )
        self._put_checkout(k)
        self._put_view(view)
        self._event(
            "checkout_registered",
            checkout=kid,
            project=project_id,
            path=view.path,
            machine=loc.machine,
            checkout_kind=k.kind,
            rule=rule,
            token=loc.token_status,
        )
        return k

    def view_state(self, k: Checkout, view: View | None = None) -> str:
        """What the checkout's newest view on this machine (or `view`)
        holds now; OTHER_MACHINE for a checkout seen only elsewhere, whose
        liveness nothing here can read."""
        if k.kind == UNRESOLVED:
            return UNREACHABLE
        views = [view] if view is not None else k.views_on(self.machine)
        if not views:
            return OTHER_MACHINE
        newest = max(views, key=lambda v: v.order)
        if newest.machine != self.machine:
            return OTHER_MACHINE
        return self._live_state(k, newest)

    def live_view(self, k: Checkout) -> str | None:
        """The newest view of the checkout on this machine that holds it
        now, or None. A path written at while nothing could be read
        (decision B6) has none."""
        if k.kind == UNRESOLVED:
            return None
        for v in sorted(k.views_on(self.machine), key=lambda v: -v.order):
            if self._live_state(k, v) in _LIVE_STATES:
                return v.path
        return None

    def _live_state(self, k: Checkout, v: View) -> str:
        """What the view's path holds now (`Liveness`): the value read
        before the transaction when there is one (`prefetch_liveness`,
        ENG-156), else read now through the model's liveness, which bounds
        a read of a path (`location.path_liveness`). A value is kept for
        the span that read it, keyed on what the read depends on
        (`_live_key`), so a checkout re-keyed meanwhile is read again."""
        key = _live_key(k, v)
        if self._live is not None:
            hit = self._live.get(key)
            if hit is not None:
                return hit
        state = self._liveness(k, v)
        if self._live is not None:
            self._live[key] = state
        return state

    @contextlib.contextmanager
    def liveness_held(self) -> Iterator[None]:
        """Keep the liveness this model reads (`prefetch_liveness` and
        every read after it) for the span, as values: a resolution and the
        migration read their views before their transaction and decide
        inside it (ENG-156). A span inside another keeps the outer one's."""
        if self._live is not None:
            yield
            return
        self._live = {}
        try:
            yield
        finally:
            self._live = None

    def _touch_view(self, k: Checkout, loc: Location) -> Checkout:
        """A sighting of a known checkout: a new path adds a view
        (``checkout_moved`` when no older view here is live, else
        ``checkout_view_added``); a known path is written again only when
        it is not the newest view or its sighting is stale."""
        if k.retired:
            k = self._put_checkout(dataclasses.replace(k, retired=False))
        path = _norm(loc.root or loc.cwd)
        stamp = self._stamp()
        here = k.views_on(loc.machine)
        newest = max((v.order for v in here), default=0)
        for v in here:
            if not _same_path(v.path, path):
                continue
            last = parse_event_ts(v.last_seen)
            stale = last is None or self._now() - last >= VIEW_REFRESH
            if v.order != newest or stale:
                fresh = dataclasses.replace(
                    v,
                    last_seen=stamp,
                    order=self._store.next_membership_number(
                        "checkout_views", "seen_order"
                    ),
                )
                self._put_view(fresh)
                k = dataclasses.replace(
                    k, views=tuple(fresh if x is v else x for x in k.views)
                )
            return k
        live_old = [v for v in here if self._live_state(k, v) in _LIVE_STATES]
        moved = bool(here) and not live_old
        self._event(
            "checkout_moved" if moved else "checkout_view_added",
            checkout=k.id,
            machine=loc.machine,
            path=path,
            previous=here[-1].path if here else None,
            evidence="same filesystem object",
        )
        view = View(
            checkout=k.id,
            machine=loc.machine,
            path=path,
            first_seen=stamp,
            last_seen=stamp,
            order=self._store.next_membership_number("checkout_views", "seen_order"),
        )
        self._put_view(view)
        return dataclasses.replace(k, views=(*k.views, view))

    def _continuity(self, k: Checkout, loc: Location) -> tuple[bool, str]:
        """Without birth time on one side (design 2.2, O): the token
        decides; without a recorded token, the last HEAD held; commit-less
        and token-less, the init stamps by equality (A2); where none of
        those can be read, another witness of the same object (B20,
        `_confirms`). The one rule the liveness of a view applies too
        (`location.continuity`, B18). The token here is the one found or,
        where none was, the one this sighting's capture wrote: of writers
        racing to a fresh git directory the winner's location carries its
        token as written, and another writer's resolution, serialized ahead
        of it, may have registered the checkout with that token (I34)."""
        return continuity(
            k.token,
            k.last_head,
            k.stamps,
            token_here=loc.token or loc.token_written,
            head_here=loc.head,
            stamps_here=loc.stamps,
            holds=lambda commits: self._holds(loc, commits),
            confirms=lambda: self._confirms(k, loc),
        )

    def _confirms(self, k: Checkout, loc: Location) -> bool:
        """Another witness that the object with the recorded device and
        inode number is the recorded one where birth time cannot say
        (decision B20): for a folder project F, its file bound to the
        checkout's project; for a repository S, a linked worktree's object
        store with the recorded device and inode, or H, witness commits of
        the checkout's project held."""
        pid = self._canon(k.project)
        if k.kind == FOLDER:
            rf = loc.root_file
            return rf is not None and self._file_binding(rf.id) == pid
        if (
            loc.common_object_key is not None
            and not _same_number(loc.common_object_key, loc.object_key)
            and _same_number(loc.common_object_key, k.common_object_key)
        ):
            return True
        commits = [str(r["commit_id"]) for r in self._witness_rows(pid)]
        return bool(commits) and bool(self._holds(loc, commits))

    def _by_object(self, loc: Location) -> tuple[Checkout | None, str]:
        """Rung 2, O: the checkout whose git directory (or folder) object
        this is, on this machine."""
        key = loc.object_key
        if key is None:
            return None, ""
        for k in self._checkouts(
            where={
                "machine": loc.machine,
                "object_device": key.device,
                "object_inode": key.inode,
            }
        ):
            if k.object_key is None:
                continue
            verdict = k.object_key.compare(key)
            if verdict == SAME:
                return k, "birth"
            if verdict == UNPROVEN:
                ok, how = self._continuity(k, loc)
                if ok:
                    return k, how
        return None, ""

    def _by_common(self, loc: Location) -> tuple[Checkout | None, str]:
        """Rung 3, S: a checkout of this object store (a linked
        worktree's common directory, or the primary's git directory), and
        the rule that says how it was proven. ``common_object``: by the
        store's object, its birth time or, with none on one side,
        `_continuity`. ``common_object+pointer``: neither can say (a new
        worktree carries no token of the checkout's, and its HEAD may name
        a commit the checkout never saw), and git's own pointers prove the
        link (`_by_pointer`, design 8.7)."""
        key = loc.common_object_key
        if key is None or loc.kind == FOLDER:
            return None, ""
        found = {
            k.id: k
            for column in ("common", "object")
            for k in self._checkouts(
                where={
                    "machine": loc.machine,
                    f"{column}_device": key.device,
                    f"{column}_inode": key.inode,
                }
            )
        }
        for kid in sorted(found):
            k = found[kid]
            verdicts = {
                theirs.compare(key)
                for theirs in (k.common_object_key, k.object_key)
                if theirs is not None
            }
            if SAME in verdicts:
                return k, "common_object"
            if UNPROVEN in verdicts:
                if self._continuity(k, loc)[0]:
                    return k, "common_object"
                if self._by_pointer(k, loc):
                    return k, "common_object+pointer"
        return None, ""

    def _by_pointer(self, k: Checkout, loc: Location) -> bool:
        """S without birth time, from git's own pointers, which need none
        (design 8.7, E15): one of the two is a linked worktree, whose git
        directory git keeps at ``<common>/worktrees/<name>`` (its ``.git``
        file and ``commondir`` point there, the ``gitdir`` file there back
        at the worktree), and both name one common directory
        (`_common_by_pointer`): this location's, and the one recorded for
        `k` or read off its live view. The store's object has the recorded
        device and inode number, and `k` holds a live view here: liveness
        reads witness O at its path (its token, last HEAD or init stamps),
        so a new repository given a deleted one's path and inode number is
        replaced there, and its worktrees are never taken for the old
        one's. Nothing is read but the location, the checkout and the
        views the liveness check reads."""
        if LINKED not in (loc.kind, k.kind):
            return False
        mine = _common_by_pointer(loc.kind, loc.git_dir)
        if mine is None or not _same_number(
            loc.common_object_key, k.common_object_key or k.object_key
        ):
            return False
        live = self.live_view(k)
        if live is None:
            return False
        recorded = [_common_by_pointer(k.kind, k.git_dir)]
        if k.kind == PRIMARY:
            recorded.append(_path_under(live, ".git"))
        elif k.kind == BARE:
            recorded.append(live)
        return any(r is not None and _same_path(r, mine) for r in recorded)

    # -- history --------------------------------------------------------------------

    @staticmethod
    def _holds(loc: Location, commits: Sequence[str]) -> set[str]:
        if loc.history is None or not commits:
            return set()
        return set(loc.history.holds(sorted(set(commits))))

    def _held(
        self, loc: Location, exclude: Collection[str] = ()
    ) -> dict[str, set[str]]:
        """{project: its witness commits this repository holds}: one batch
        over every project's bounded witness set."""
        if not loc.born or loc.history is None:
            return {}
        index: dict[str, set[str]] = defaultdict(set)
        for row in self._witness_rows():
            pid = self._canon(str(row["project"]))
            if pid not in exclude:
                index[str(row["commit_id"])].add(pid)
        if not index:
            return {}
        out: dict[str, set[str]] = defaultdict(set)
        for sha in self._holds(loc, sorted(index)):
            for pid in index.get(sha, ()):
                out[pid].add(sha)
        return dict(out)

    def _rewritten(
        self, loc: Location, exclude: Collection[str] = ()
    ) -> dict[str, int]:
        """{project: commits read from HEAD that carry one of its witness
        fingerprints under other ids}: the history witness after a rewrite
        (Hf; git filter-repo keeps authors, dates and messages)."""
        history = loc.history
        if not loc.born or history is None:
            return {}
        index: dict[str, set[str]] = defaultdict(set)
        for row in self._witness_rows():
            if row["fingerprint"]:
                pid = self._canon(str(row["project"]))
                if pid not in exclude:
                    index[str(row["fingerprint"])].add(pid)
        if not index:
            return {}
        out: dict[str, int] = defaultdict(int)
        seen: set[str] = set()
        todo = [loc.head] if loc.head else list(loc.ref_tips)
        while todo and len(seen) < WALK_LIMIT:
            sha = todo.pop()
            if sha is None or sha in seen:
                continue
            seen.add(sha)
            fingerprint = history.fingerprint(sha)
            for pid in index.get(fingerprint, ()) if fingerprint else ():
                out[pid] += 1
            todo.extend(history.parents(sha))
        return dict(out)

    def _most_specific(self, loc: Location, held: Mapping[str, set[str]]) -> list[str]:
        """The projects whose held witnesses include one no other held
        witness descends from: the most specific line. The ancestry is
        one bounded traversal from every held witness at once
        (`HistoryReader.reachable_among`, ENG-151), read inside the
        resolution's write transaction; a relation past its bound reads
        as none, which keeps more projects (and asks), never fewer."""
        history = loc.history
        shas = sorted(set().union(*held.values()))
        if history is None:
            return sorted(held)
        reach = history.reachable_among(shas)
        top = {s for s in shas if not reach.get(s.lower())}
        return sorted(p for p, ws in held.items() if ws & top)

    # The ladder of design 3.2 (membership_resolve.py).
    _tally = membership_resolve._tally
    _strength = membership_resolve._strength
    resolve = membership_resolve.resolve
    _resolve = membership_resolve._resolve
    _enclosing = membership_resolve._enclosing
    _filed_tenant = membership_resolve._filed_tenant
    _caller = membership_resolve._caller
    _resolve_repository = membership_resolve._resolve_repository
    _folder_became_repository = membership_resolve._folder_became_repository
    _first_sight = membership_resolve._first_sight
    _join = membership_resolve._join
    _content_candidates = membership_resolve._content_candidates
    _ask = membership_resolve._ask
    _new = membership_resolve._new
    _display_name = membership_resolve._display_name
    _observe = membership_resolve._observe
    _subproject_line = membership_resolve._subproject_line
    _nested_answer = membership_resolve._nested_answer
    _subproject = membership_resolve._subproject
    _resolve_folder = membership_resolve._resolve_folder
    _declared_folder = membership_resolve._declared_folder
    _liveness_plan = membership_resolve._liveness_plan
    prefetch_liveness = membership_resolve.prefetch_liveness

    # -- proposals ------------------------------------------------------------------------------

    def _open_proposals(self, checkout_id: str | None = None) -> list[Proposal]:
        where: dict[str, Any] = {"state": OPEN}
        if checkout_id is not None:
            where["checkout"] = checkout_id
        return [
            _proposal_from(r)
            for r in self._store.membership_rows("proposals", where=where)
        ]

    def proposal(self, proposal_id: str) -> Proposal:
        rows = self._store.membership_rows("proposals", where={"id": proposal_id})
        if not rows:
            raise KeyError(proposal_id)
        return _proposal_from(rows[0])

    def pending(self) -> list[Proposal]:
        """Every open proposal (``bettermemory project pending``)."""
        return self._open_proposals()

    def _put_proposal(self, pr: Proposal) -> None:
        self._put("proposal", _proposal_row(pr))

    def _open_proposal(
        self,
        *,
        checkout: str | None,
        kind: str,
        candidates: Sequence[str],
        evidence: Mapping[str, Any],
        reason: str,
        line: str | Callable[[str], str],
        signature: tuple[Any, ...] | None,
        distinct: str | None = None,
    ) -> Proposal:
        """One proposal per checkout and question (design 3.4): an open
        one of the same kind on the checkout is returned as it is; with
        `distinct`, only one whose evidence names the same value there
        (one ``legacy`` question per checkout and unresolved project).
        `line` is the question's line, or the function of the question's
        id that builds it, so the line names the call that answers it."""
        if checkout is not None:
            for existing in self._open_proposals(checkout):
                if existing.kind == kind and (
                    distinct is None
                    or existing.evidence.get(distinct) == evidence.get(distinct)
                ):
                    return existing
        qid = self._store.next_membership_id("proposals", "Q")
        pr = Proposal(
            id=qid,
            checkout=checkout,
            kind=kind,
            candidates=tuple(candidates),
            evidence=_jsonable(evidence),
            reason=reason,
            line=line(qid) if callable(line) else line,
            state=OPEN,
            answer=None,
            answered_by=None,
            signature=signature,
            opened=self._stamp(),
        )
        self._put_proposal(pr)
        self._event(
            "proposal_opened",
            proposal=pr.id,
            checkout=checkout,
            kind=kind,
            candidates=list(candidates),
            reason=reason,
            evidence=pr.evidence,
        )
        return pr

    def _close(
        self, pr: Proposal, answer: str, by: str, *, said: str | None = None
    ) -> None:
        if pr.state != OPEN:
            return
        current = self.proposal(pr.id)
        self._put_proposal(
            dataclasses.replace(current, state=ANSWERED, answer=answer, answered_by=by)
        )
        payload: dict[str, Any] = {
            "proposal": pr.id,
            "checkout": pr.checkout,
            "answer": answer,
            "by": by,
        }
        if said is not None:
            payload["said"] = said
        self._event("proposal_answered", **payload)

    def open_proposal(
        self,
        *,
        checkout: str | None,
        kind: str,
        candidates: Sequence[str],
        evidence: Mapping[str, Any],
        reason: str,
        line: str | Callable[[str], str],
        distinct: str | None = None,
    ) -> Proposal:
        """A proposal another rule opens (the migration's ``legacy``, an
        import's ``import``), with its line (or the function of the
        question's id that builds it); one per checkout and kind, or
        with `distinct` one per checkout, kind and value of that evidence
        key (the migration's one question per legacy project)."""
        with self._call():
            return self._open_proposal(
                checkout=checkout,
                kind=kind,
                candidates=candidates,
                evidence=evidence,
                reason=reason,
                line=line,
                signature=None,
                distinct=distinct,
            )

    def restate_proposal(
        self, proposal_id: str, *, line: str, evidence: Mapping[str, Any]
    ) -> Proposal:
        """An open proposal's line and evidence restated by the rule that
        opened it (the migration's counts, re-tallied at a later run);
        an answered one is returned as it is."""
        with self._call():
            pr = self.proposal(proposal_id)
            if pr.state != OPEN:
                return pr
            restated = dataclasses.replace(pr, line=line, evidence=_jsonable(evidence))
            if restated != pr:
                self._put_proposal(restated)
            return restated

    def settle_legacy_questions(self) -> list[Proposal]:
        """Close, as separate, every open ``legacy`` question of the
        migration whose evidence decides nothing (`legacy_asks`): the
        records' commits are held here by citation only under another
        name, or through a remote the candidate does not carry. The
        records stay a project of their own with no live checkout, as they
        were; the question is answered by the rule, not by anyone, in one
        ``proposal_answered`` row each. Returns the questions closed. Runs
        at every open of the store (design 8.6) and is idempotent."""
        with self._call():
            closed: list[Proposal] = []
            for pr in self._open_proposals():
                if pr.kind != LEGACY or pr.reason != LEGACY_NO_LIVE_CHECKOUT:
                    continue
                legacy = pr.evidence.get("unresolved_project")
                if not isinstance(legacy, str) or not pr.candidates:
                    continue
                candidate = self._canon(pr.candidates[0])
                try:
                    lp, cp = self.project(self._canon(legacy)), self.project(candidate)
                except KeyError:
                    continue
                mine, theirs = self._remotes_of(lp.id), self._remotes_of(cp.id)
                compatible = not mine or not theirs or bool(mine & theirs)
                if legacy_asks(
                    rule=str(pr.evidence.get("rule") or ""),
                    same_name=lp.name == cp.name,
                    compatible=compatible,
                ):
                    continue
                self._close(pr, "new", LEGACY_SEPARATE)
                closed.append(pr)
            return closed

    # -- declarations (design 8.8) ---------------------------------------------------------

    # Declarations and the B6 writes (membership_declare.py).
    declare = membership_declare.declare
    _declared = membership_declare._declared
    _question_of = membership_declare._question_of
    _declare = membership_declare._declare
    _declare_below_root = membership_declare._declare_below_root
    _declare_subproject = membership_declare._declare_subproject
    _answer_join = membership_declare._answer_join
    _answer_new = membership_declare._answer_new
    _rehome_checkout = membership_declare._rehome_checkout
    _move_checkout = membership_declare._move_checkout
    _join_checkout = membership_declare._join_checkout
    _absorb = membership_declare._absorb
    _unmerge = membership_declare._unmerge
    _split = membership_declare._split
    parts_in = membership_declare.parts_in
    _reparent_parts = membership_declare._reparent_parts
    _move_record = membership_declare._move_record
    unresolved_home = membership_declare.unresolved_home
    _register_unresolved = membership_declare._register_unresolved
    _unresolved_at = membership_declare._unresolved_at
    _unresolved_of = membership_declare._unresolved_of
    session_resolution = membership_declare.session_resolution
    _settle_unresolved = membership_declare._settle_unresolved
    _unresolved_line = membership_declare._unresolved_line
    _answer_unresolved = membership_declare._answer_unresolved
    _answer_import = membership_declare._answer_import

    def below_root(self, k: Checkout, directory: str) -> bool | None:
        """Whether `directory` lies below the root of checkout `k`, the
        root being its newest view on this machine (the one the latest
        sighting here saw; decision B17): False at the root, True below
        it, None when that view does not hold `directory` or the checkout
        was never seen here. `bettermemory project init` asks it, and a
        declaration's ``directory`` is read by it."""
        views = k.views_on(self.machine)
        if not views:
            return None
        here = _dir_key(directory)
        root = _dir_key(views[-1].path)
        if here == root:
            return False
        if here.startswith(root.rstrip(os.sep) + os.sep):
            return True
        return None

    def _declared_below(self, decl: Declaration, k: Checkout) -> bool:
        """Whether an ``init`` declaration was made below the checkout's
        root (decision B17); a declaration without a directory, or any
        other action, reads as the root's."""
        if decl.action not in _INITS or decl.directory is None:
            return False
        below = self.below_root(k, decl.directory)
        if below is None:
            raise ValueError(
                f"cannot tell where {decl.directory} stands in checkout {k.id} "
                f"(seen at {k.newest_view.path if k.newest_view else k.id})"
            )
        return below

    def _checkout_name(self, k: Checkout) -> str:
        newest = k.newest_view
        return _basename(newest.path) if newest is not None else k.id

    def _give_token(self, k: Checkout) -> None:
        """A token of its own for the checkout, so copies made from here on
        are told apart from the line it separated from."""
        if k.git_dir is None or k.kind == BARE:
            return
        if self._token_writer is None:
            # The caller writes it (`Resolution.remint_token`); until it
            # does, the checkout's status says its token is superseded
            # and every sighting asks again (decision B15).
            if k.token_status != SUPERSEDED:
                self._put_checkout(dataclasses.replace(k, token_status=SUPERSEDED))
            self._remint = k.id
            return
        token, status = self._token_writer(k)
        if token:
            self._put_checkout(dataclasses.replace(k, token=token, token_status=status))
        elif k.token_status == SUPERSEDED:
            self._put_checkout(dataclasses.replace(k, token_status=status))
        self._event("token_reminted", checkout=k.id, status=status, written=bool(token))

    def token_written(self, checkout_id: str, token: str | None, status: str) -> None:
        """The caller wrote the token a resolution or declaration asked for
        (`Resolution.remint_token`, `Declared.remint_checkout`). A
        superseded checkout takes the status even when nothing was written
        (a policy that writes no token, a read-only git directory), so its
        next sighting does not ask again (decision B15)."""
        with self._call():
            k = self.checkout(checkout_id)
            if token:
                self._put_checkout(
                    dataclasses.replace(k, token=token, token_status=status)
                )
            elif k.token_status == SUPERSEDED and status != SUPERSEDED:
                self._put_checkout(dataclasses.replace(k, token_status=status))
            self._event(
                "token_reminted", checkout=k.id, status=status, written=bool(token)
            )

    # -- writes where nothing could be read (decision B6) --------------------------------

    def _record_counts(self, project_id: str) -> tuple[int, int]:
        """(memories, episodes) whose homes name the project."""
        rows = self._store.membership_rows(
            "homes", column_in=("project", self._members(project_id))
        )
        memories = sum(1 for r in rows if r["kind"] == "memory")
        return memories, len(rows) - memories

    # -- homes, why --------------------------------------------------------------------------

    def _home_from(self, row: Mapping[str, Any], scopes: Iterable[str] = ()) -> Home:
        recorded = str(row["project"])
        chain: tuple[str, ...] = ()
        tenant: str | None = None
        unresolved = False
        named: frozenset[str] = frozenset()
        if recorded == GLOBAL:
            project = GLOBAL
            named = self.named(scopes)
        else:
            project = self._canon(recorded)
            chain = tuple(self.chain_of(project))
            known = self._all_projects().get(project)
            if known is not None:
                tenant = known.tenant
                unresolved = known.provisional in UNRESOLVED_STATES
        return Home(
            record_id=str(row["record_id"]),
            kind=str(row["kind"]),
            project=project,
            checkout=row["checkout"],
            rule=str(row["rule"]),
            decided=str(row["decided"]),
            recorded_project=recorded,
            chain=chain,
            tenant=tenant,
            unresolved=unresolved,
            named=named,
        )

    # Homes, surfaces, run-state, anchoring and health (membership_queries.py).
    home = membership_queries.home
    homes = membership_queries.homes
    admit_recall = membership_queries.admit_recall
    first_output = membership_queries.first_output
    handoff = membership_queries.handoff
    anchor = membership_queries.anchor
    translate = membership_queries.translate
    retire_quiet = membership_queries.retire_quiet
    health = membership_queries.health

    def why(self, record_id: str) -> Why:
        """``bettermemory project why <id>`` (design 8.10, E37 d): the
        record's home and every row that decided it, oldest first: its
        home rows, its checkout's registration, views, re-keys and token
        rows, the proposals there and their answers, and the mints, merges
        and file bindings of the projects on its chain."""
        self._projects = None
        home = self.home(record_id)
        if home is None:
            return Why(
                record_id, None, None, None, (), f"{record_id}: no home recorded"
            )
        cid = home.checkout
        projects = set(home.chain) | {home.recorded_project, home.project}
        projects |= {p for p in self._all_projects() if self.canonical(p) in projects}
        proposals = (
            {
                str(r["id"])
                for r in self._store.membership_rows(
                    "proposals", where={"checkout": cid}
                )
            }
            if cid is not None
            else set()
        )
        # The import questions about the record's project (design 8.7).
        proposals |= {
            str(r["id"])
            for r in self._store.membership_rows("proposals", where={"kind": IMPORT})
            if isinstance(r["evidence_json"], Mapping)
            and r["evidence_json"].get("imported_project") in projects
        }
        rows: list[LogRow] = []
        for row in self._store.membership_log():
            try:
                payload = json.loads(row.payload)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            if _decides(row.kind, payload, record_id, cid, proposals, projects):
                rows.append(row)
        if home.project == GLOBAL:
            text = (
                f"{record_id}: global (written outside any repository and any project "
                f"file); rule {home.rule}"
            )
        else:
            registered = next(
                (
                    json.loads(r.payload).get("rule")
                    for r in rows
                    if r.kind == "checkout_registered"
                ),
                None,
            )
            text = (
                f'{record_id}: project "{self.project(home.project).name}" '
                f"({home.project}), written in checkout {cid}; rule {home.rule}"
                + (f"; the checkout joined by {registered}" if registered else "")
                + f"; {len(rows)} rows"
            )
        return Why(record_id, home.project, cid, home.rule, tuple(rows), text)

    # Export and import, design 8.7 (membership_portable.py).
    portable_identity = membership_portable.portable_identity
    export_document = membership_portable.export_document
    _imported_before = membership_portable._imported_before
    _evidence_checkout = membership_portable._evidence_checkout
    import_identity = membership_portable.import_identity
    _import_identity = membership_portable._import_identity
    _import_line = membership_portable._import_line
    import_home = membership_portable.import_home


def portable_export(
    store: Store, record_ids: Collection[str] | None = None
) -> dict[str, Any]:
    """`Membership.export_document` over `store`: no machine enters an
    export, so none is opened."""
    return Membership(store, "").export_document(record_ids)


def _decides(
    kind: str,
    payload: Mapping[str, Any],
    record_id: str,
    checkout: str | None,
    proposals: Collection[str],
    projects: Collection[str],
) -> bool:
    """Whether a membership log row is one `why` lists for a record."""
    body = payload.get(kind[: -len("_put")]) if kind.endswith("_put") else None
    if isinstance(body, Mapping):
        if kind == "home_put":
            return body.get("record_id") == record_id
        if kind in ("checkout_put", "proposal_put"):
            return body.get("id") == checkout or body.get("id") in proposals
        if kind == "checkout_view_put":
            return body.get("checkout") == checkout
        if kind == "project_put":
            return body.get("id") in projects
        if kind == "project_file_put":
            return body.get("project") in projects
        return False
    if kind in ("recall_suppressed", "witness_put", "witness_released"):
        return False
    if kind == "import_homed":
        return payload.get("record") == record_id
    if kind in (
        "project_minted",
        "project_merged",
        "project_file_bound",
        "project_reparented",
    ):
        return payload.get("project") in projects or payload.get("into") in projects
    if checkout is not None and payload.get("checkout") == checkout:
        return True
    if payload.get("proposal") in proposals:
        return True
    return False


__all__ = [
    "ASK",
    "B",
    "C",
    "D",
    "DEFERRED",
    "EXPORT_FORMAT",
    "EXPORT_FORMAT_VERSION",
    "EXPORT_WITNESSES",
    "F",
    "FIRST_SIGHT",
    "FILE_CONTRADICTION",
    "FOLDER",
    "GLOBAL",
    "GLOBAL_NAMED",
    "GLOBAL_OUTCOME",
    "GLOBAL_RELATION",
    "H",
    "HB",
    "HF",
    "IMPORT",
    "IMPORT_CONTRADICTION",
    "IMPORT_ONE_WITNESS",
    "IMPORT_SEVERAL",
    "IN_PROJECT_POLICY",
    "INDETERMINATE",
    "LINKS",
    "NEW",
    "O",
    "OTHER_PROJECT",
    "OTHER_TENANT",
    "OUTSIDE",
    "OUTSIDE_POLICY",
    "POOL_PREFILTER",
    "PRIMARY",
    "PROJECT_SCOPE",
    "PROMPT_RECALL",
    "R",
    "REMOTE_DIVERGED",
    "REPO",
    "REVIEW_STRATA",
    "S",
    "SAME_PROJECT",
    "SEARCH_AUTO_SCOPE",
    "SEARCH_UNSCOPED",
    "SESSION_BRIEF",
    "SILENT",
    "SUBPROJECT_CONTRADICTION",
    "SUB_PROJECT",
    "SUPERSEDED",
    "SURFACES",
    "T",
    "UNKNOWN",
    "UNREADABLE",
    "UNRESOLVED",
    "UNRESOLVED_LOCATION",
    "UNRESOLVED_STATES",
    "Checkout",
    "Declaration",
    "Declared",
    "LEGACY_NO_LIVE_CHECKOUT",
    "LEGACY_SEPARATE",
    "EpisodeRef",
    "Handoff",
    "Health",
    "HistoryReader",
    "Home",
    "ImportedProject",
    "InitStamp",
    "Liveness",
    "Location",
    "Machine",
    "Manifest",
    "Membership",
    "ObjectKey",
    "PortableIdentity",
    "Project",
    "ProjectFile",
    "ProjectFileRef",
    "Proposal",
    "RecallAdmission",
    "RecordHome",
    "Resolution",
    "TokenWriter",
    "UnresolvedProject",
    "View",
    "Why",
    "admitted",
    "line_deferred",
    "line_file_contradiction",
    "legacy_asks",
    "line_first_sight",
    "line_import",
    "line_joined",
    "line_legacy",
    "line_new",
    "line_remote_diverged",
    "line_subproject",
    "line_unresolved",
    "line_unreadable",
    "manifest_match",
    "path_liveness",
    "portable_export",
    "relation",
]
