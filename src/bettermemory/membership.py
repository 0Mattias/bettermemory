"""Membership: which project a memory belongs to (RS-F6, design 2 and 3).

Every membership decision is made here. A caller hands in a `Location`
(what the capture read about its folder: the git directory's object, its
object store, HEAD and the ref tips, every remote, the checkout token, the
init stamps, the project files from the cwd up, the evicted-file state,
the content manifest, and a reader of the repository's history) and gets
a `Resolution`: SILENT with its project and checkout, ASK ONCE with a
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
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from . import membership_resolve
from .location import NO_MACHINE, continuity, path_liveness
from .membership_lines import (
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
    RETIRE_AFTER,
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
    _COMMANDS,
    _INITS,
    _LIVE_STATES,
    _basename,
    _common_by_pointer,
    _dir_key,
    _inside,
    _norm,
    _path_under,
    _rebase,
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
        self._token_writer = token_writer
        self._now = now
        self._session = session
        self._projects: dict[str, Project] | None = None
        self._rows: list[int] = []
        self._remint: str | None = None

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
            return [
                f"bettermemory: {f.path} now declares a boundary: "
                f"{_named(p.name, pid)} is a tenant from now on; nothing crosses it"
            ]
        if p.boundary and not f.boundary:
            return [
                f"bettermemory: {f.path} no longer declares a boundary; "
                f"{_named(p.name, pid)} stays one until it is lifted: bettermemory "
                f"project init --rebind (in {f.dir}) binds a fresh file without it"
            ]
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
        return self._liveness(k, newest)

    def live_view(self, k: Checkout) -> str | None:
        """The newest view of the checkout on this machine that holds it
        now, or None. A path written at while nothing could be read
        (decision B6) has none."""
        if k.kind == UNRESOLVED:
            return None
        for v in sorted(k.views_on(self.machine), key=lambda v: -v.order):
            if self._liveness(k, v) in _LIVE_STATES:
                return v.path
        return None

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
        live_old = [v for v in here if self._liveness(k, v) in _LIVE_STATES]
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
        witness descends from: the most specific line."""
        history = loc.history
        shas = sorted(set().union(*held.values()))
        if history is None:
            return sorted(held)
        top = {
            s
            for s in shas
            if not any(o != s and history.is_ancestor(s, o) for o in shas)
        }
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

    def declare(self, decl: Declaration) -> Declared:
        """Apply a declaration (design 8.8). ``join`` and ``new`` answer
        the open proposal named (or the checkout's first open one) or,
        with none, join or separate the checkout itself; ``init``,
        ``init_new`` and ``init_rebind`` bind the file id the command
        wrote, at the checkout's root to the checkout's project and below
        it to a part (decision B17); ``move`` re-homes one record;
        ``absorb`` merges the project named into the checkout's; ``unjoin``
        undoes the merge of the project named. Each is logged with the
        principal, and a join with the user's words it rests on."""
        with self._call():
            before = {pr.id for pr in self._open_proposals()}
            project = self._declare(decl)
            if not before - {pr.id for pr in self._open_proposals()}:
                self._declared(decl, project)
            rows = tuple(self._rows)
            remint = self._remint
        return Declared(
            action=decl.action,
            proposal=decl.proposal,
            project=project,
            rows=rows,
            remint_checkout=remint,
        )

    def _declared(self, decl: Declaration, project: str | None) -> None:
        """The row of a join or a separation that answered no question,
        naming the principal (decision D6; design 3.2 rung 1, a
        declaration decides the checkout's project). An answer names the
        principal in its ``proposal_answered`` row, and the bindings and
        moves of the other declarations in their own rows' evidence; an
        ``init_new`` below the root leaves the checkout where it is, and
        its part's rows (minted, bound) name the declaration."""
        if decl.action == "init_new" and decl.checkout is not None:
            if self._declared_below(decl, self.checkout(decl.checkout)):
                return
        if decl.action == "join":
            payload: dict[str, Any] = {
                "checkout": decl.checkout,
                "project": project,
                "witnesses": ["declaration"],
                "principal": decl.principal,
                "proposal": decl.proposal,
            }
            if decl.said is not None:
                payload["said"] = decl.said
            self._event("resolution_joined", **payload)
        elif decl.action in ("new", "init_new"):
            self._event(
                "resolution_new",
                checkout=decl.checkout,
                project=project,
                principal=decl.principal,
                proposal=decl.proposal,
                reused_path=False,
            )

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

    def _question_of(
        self, decl: Declaration, k: Checkout, below: bool
    ) -> Proposal | None:
        """The open question a declaration naming none answers. A part's
        question (L7) is answered in that folder: `project init --new`
        below the root answers only the one about its own folder, and
        neither `project new` nor `project init --new` at the root answers
        one. With no directory, `init --new` answers the checkout's first."""
        opened = self._open_proposals(k.id)
        if decl.action == "new" or (
            decl.action == "init_new" and decl.directory is not None and not below
        ):
            opened = [x for x in opened if x.kind != SUBPROJECT_CONTRADICTION]
        elif decl.action == "init_new" and below:
            assert decl.directory is not None
            folder = _dir_key(decl.directory)
            opened = [
                x
                for x in opened
                if x.kind == SUBPROJECT_CONTRADICTION
                and _dir_key(str(x.evidence.get("dir") or "")) == folder
            ]
        return opened[0] if opened else None

    def _declare(self, decl: Declaration) -> str | None:
        by = f"declaration:{decl.principal}"
        if decl.action == "move":
            if decl.record is None or decl.target is None:
                raise ValueError("memory move names a record and a project")
            return self._move_record(decl.record, decl.target, by)
        if decl.action == "unjoin":
            if decl.target is None:
                raise ValueError("unjoin names a project")
            return self._unmerge(decl.target, by)
        if decl.checkout is None:
            raise ValueError(f"{decl.action} is made in a checkout")
        k = self.checkout(decl.checkout)
        if decl.action == "absorb":
            if decl.target is None:
                raise ValueError("absorb names a project")
            return self._absorb(k, decl.target, by, said=decl.said)
        below = self._declared_below(decl, k)
        pr: Proposal | None = None
        if decl.proposal is not None:
            pr = self.proposal(decl.proposal)
            if pr.state != OPEN:
                raise ValueError(f"proposal {pr.id} is already answered")
        elif decl.action in ("join", "new", "init_new"):
            pr = self._question_of(decl, k, below)
        if decl.action == "join":
            if pr is not None:
                return self._answer_join(pr, k, decl, by)
            if decl.target is None:
                raise ValueError("project join names a project")
            return self._join_checkout(k, decl.target, by)
        if decl.action == "new":
            if pr is not None:
                return self._answer_new(pr, k, decl, by)
            return self._split(k, decl.name or self._checkout_name(k), by)
        if decl.action in _INITS:
            if decl.file_id is None:
                raise ValueError(f"{decl.action} names the file id it wrote")
            if decl.action == "init_new" and pr is not None:
                # The question's "separate" answer, the file the command
                # wrote bound to the checkout's project (E22 e), or below
                # the root to a part of its own (L7, E35 g).
                return self._answer_new(pr, k, decl, by)
            if below:
                return self._declare_below_root(decl, k, by)
            pid = self._canon(k.project)
            if decl.action == "init_new":
                # No question open: the checkout and the new file become a
                # project of their own.
                pid = self._split(k, decl.name or self._checkout_name(k), by)
            self._bind_file(
                decl.file_id,
                pid,
                evidence=f"{by}: bettermemory project {decl.action}",
                boundary=decl.boundary,
            )
            if decl.action == "init_rebind":
                if not decl.boundary:
                    # The fresh file sets the project's boundary (B12).
                    self._lift_boundary(pid, decl.outer_tenant)
                for x in self._open_proposals(k.id):
                    if x.kind == FILE_CONTRADICTION:
                        self._close(x, "rebind", by)
            return pid
        raise ValueError(f"unknown declaration {decl.action!r}")

    def _declare_below_root(self, decl: Declaration, k: Checkout, by: str) -> str:
        """`bettermemory project init [--new | --rebind]` in a folder below
        the checkout's root (decision B17; design 2.1, a file below the
        root names a part, D13): the file names a part of the project
        enclosing that folder, never the checkout, which keeps its project.
        ``init_rebind`` binds the fresh file to the part the replaced file
        named (its boundary set from the fresh file, B12), or makes a new
        part when that file named none; ``init`` and ``init_new`` with no
        question of that folder open make a new part."""
        assert decl.file_id is not None
        part = (
            self._file_binding(decl.replaced)
            if decl.action == "init_rebind" and decl.replaced
            else None
        )
        if part is None:
            return self._declare_subproject(decl, k, by)
        self._bind_file(
            decl.file_id,
            part,
            evidence=f"{by}: bettermemory project init --rebind in {decl.directory}",
            boundary=decl.boundary,
            checkout=k.id,
            directory=decl.directory,
        )
        if not decl.boundary:
            self._lift_boundary(part, decl.outer_tenant)
        return part

    def _declare_subproject(self, decl: Declaration, k: Checkout, by: str) -> str:
        """A new part for a folder below the checkout's root (decision D13;
        design 2.1, a project file below the root names a sub-project): a
        sub-project of ``decl.target``, the project enclosing that folder
        as the caller's resolution there found it, else of the checkout's
        project, in its tenant, with the file the command wrote bound to
        it, as the first sight of a nested file binds one (X5). Below a
        folder project's root the part is a folder project inside it
        (I31), registered at its folder's first sight
        (`_declared_folder`). The checkout keeps its project."""
        parent = self.canonical(decl.target) if decl.target is not None else None
        if parent is None or parent == GLOBAL or parent not in self._all_projects():
            parent = self._canon(k.project)
        assert decl.file_id is not None
        sp = self._mint_project(
            decl.name or _basename(decl.directory or "") or self._checkout_name(k),
            reason="declared_subproject",
            parent=parent,
            tenant=self.project(parent).tenant,
            folder=k.kind == FOLDER,
        )
        self._bind_file(
            decl.file_id,
            sp.id,
            evidence=f"{by}: bettermemory project {_COMMANDS[decl.action]} in "
            f"{decl.directory}",
            boundary=decl.boundary,
            checkout=k.id,
            directory=decl.directory,
        )
        return sp.id

    def _answer_join(
        self, pr: Proposal, k: Checkout, decl: Declaration, by: str
    ) -> str | None:
        if pr.kind == LEGACY and pr.evidence.get("unresolved_project"):
            return self._answer_unresolved(pr, k, decl, by, join=True)
        if pr.kind == IMPORT:
            return self._answer_import(pr, k, decl, by, join=True)
        mine = self._canon(k.project)
        target = self._canon(decl.target or pr.candidates[0])
        if pr.kind == REMOTE_DIVERGED and target == mine:
            self._add_remotes(mine, pr.evidence.get("new", ()))
        elif pr.kind == SUBPROJECT_CONTRADICTION:
            pass  # that folder is the other project: read by `_nested_answer`
        elif pr.kind == FILE_CONTRADICTION:
            self._move_checkout(k, target, by)
        else:
            evidence = set(pr.evidence.get(target) or ())
            shallow = (
                bool(pr.signature) and bool(pr.signature[-1]) if pr.signature else False
            )
            if (
                pr.kind == FIRST_SIGHT
                and shallow
                and R in evidence
                and not ({H, HF, HB, F} & evidence)
            ):
                # A question that rested on the remote alone in a shallow
                # clone: the answer declares that remote the project's for
                # shallow clones (witness D, D18).
                for key in k.remotes:
                    self._put(
                        "declared_remote",
                        {
                            "remote_key": key,
                            "project": target,
                            "principal": decl.principal,
                        },
                    )
                    self._event(
                        "remote_declared",
                        remote=key,
                        project=target,
                        principal=decl.principal,
                    )
            self._merge(mine, target, evidence=by)
        self._close(pr, f"join:{target}", by, said=decl.said)
        return target

    def _answer_new(
        self, pr: Proposal, k: Checkout, decl: Declaration, by: str
    ) -> str | None:
        if pr.kind == LEGACY and pr.evidence.get("unresolved_project"):
            return self._answer_unresolved(pr, k, decl, by, join=False)
        if pr.kind == IMPORT:
            return self._answer_import(pr, k, decl, by, join=False)
        mine = self._canon(k.project)
        if pr.kind == SUBPROJECT_CONTRADICTION:
            # `bettermemory project init --new` in that folder wrote a file
            # with an id of its own: a new sub-project of the enclosing one.
            if decl.file_id is None:
                raise ValueError("project init --new names the file id it wrote")
            parent = self._canon(str(pr.evidence.get("parent") or mine))
            name = (
                decl.name
                or str(pr.evidence.get("name") or "")
                or _basename(str(pr.evidence.get("dir") or ""))
            )
            sp = self._mint_project(
                name,
                reason="declared_subproject",
                parent=parent,
                tenant=self.project(parent).tenant,
            )
            self._bind_file(
                decl.file_id,
                sp.id,
                evidence=f"{by}: bettermemory project init --new in "
                f"{pr.evidence.get('dir')}",
                boundary=decl.boundary,
                checkout=k.id,
                directory=str(pr.evidence.get("dir") or "") or None,
            )
            self._close(pr, "new", by)
            return sp.id
        if pr.kind == REMOTE_DIVERGED:
            self._close(pr, "new", by)
            mine = self._split(k, decl.name or self._checkout_name(k), by)
            if decl.file_id is not None:
                self._bind_file(
                    decl.file_id,
                    mine,
                    evidence=f"{by}: bettermemory project init --new",
                    boundary=decl.boundary,
                )
            return mine
        p = self.project(mine)
        if decl.file_id is not None:
            self._bind_file(
                decl.file_id,
                mine,
                evidence=f"{by}: bettermemory project init --new",
                boundary=decl.boundary,
            )
        self._close(pr, "new", by)
        if p.provisional is not None:
            # The checkout's provisional project becomes its own, with a
            # token of its own, so it is not asked again (E22 a).
            self._put_project(dataclasses.replace(p, provisional=None))
            self._give_token(k)
        return mine

    def _checkout_name(self, k: Checkout) -> str:
        newest = k.newest_view
        return _basename(newest.path) if newest is not None else k.id

    def _rehome_checkout(
        self, k: Checkout, target: str, by: str, *, parts: Collection[str] = ()
    ) -> None:
        """A declared move of a checkout: the checkout row and its records'
        homes name the target, each a logged row naming the declaration.
        A record homed in one of `parts`, the parts the checkout takes
        along (B10), stays in its part."""
        self._put_checkout(dataclasses.replace(k, project=target))
        for row in self._store.membership_rows("homes", where={"checkout": k.id}):
            if self._canon(str(row["project"])) in parts:
                continue
            self._put("home", {**row, "project": target, "rule": f"{row['rule']}|{by}"})

    def _move_checkout(self, k: Checkout, target: str, by: str) -> None:
        """A declared move of a checkout to another project, its parts with
        it (B10)."""
        parts = self.parts_in(k.id)
        dst = self._canon(target)
        self._rehome_checkout(k, dst, by, parts={part.id for part, _f, _d in parts})
        self._reparent_parts(k, parts, dst, by, "project join")

    def _join_checkout(self, k: Checkout, target: str, by: str) -> str:
        """``project join`` with no question open: the checkout's project
        merged into the target when the checkout is its only one, else the
        checkout moved there."""
        mine, dst = self._canon(k.project), self._canon(target)
        if mine == dst:
            return dst
        if all(x.id == k.id for x in self._checkouts_of(mine)):
            self._merge(mine, dst, evidence=by)
        else:
            self._move_checkout(k, dst, by)
        return dst

    def _absorb(
        self, k: Checkout, target: str, by: str, *, said: str | None = None
    ) -> str:
        """The agent's ``join_project``: project `target` (a legacy or an
        imported project with no live checkout, whose question was closed
        as separate, or any project the user names) merged into the
        checkout's project, so its records are delivered here from now on;
        one project row and one ``project_absorbed`` row naming the
        declaration and the user's words. Reversed by ``unjoin``."""
        mine, src = self._canon(k.project), self._canon(target)
        if src == mine:
            return mine
        self._merge(src, mine, evidence=by)
        self._event(
            "project_absorbed",
            project=src,
            into=mine,
            checkout=k.id,
            evidence=by,
            said=said,
        )
        return mine

    def _unmerge(self, target: str, by: str) -> str:
        """The agent's ``unjoin_project``: the merge of project `target`
        undone, its records its own again; one project row and one
        ``project_unmerged`` row. A project not merged into another
        raises ValueError."""
        p = self.project(target)
        if p.merged_into is None:
            raise ValueError(f"project {target} is not merged into another")
        was = p.merged_into
        self._put_project(dataclasses.replace(p, merged_into=None))
        self._event("project_unmerged", project=p.id, was_into=was, evidence=by)
        return p.id

    def _split(self, k: Checkout, name: str, by: str) -> str:
        """``project new``: the checkout and what was written there become
        a project of their own, and the checkout gets a token of its own."""
        old = self._canon(k.project)
        parts = self.parts_in(k.id)
        p = self._mint_project(
            name, reason="declared_split", tenant=self.project(old).tenant
        )
        self._rehome_checkout(k, p.id, by, parts={part.id for part, _f, _d in parts})
        self._reparent_parts(k, parts, p.id, by, "project new")
        if self.project(old).provisional and not self._checkouts_of(old):
            self._merge(old, p.id, evidence=f"the placeholder of checkout {k.id}, {by}")
        self._give_token(self.checkout(k.id))
        return p.id

    def parts_in(self, checkout_id: str) -> list[tuple[Project, str, str | None]]:
        """The sub-projects of the checkout's project whose files were bound
        inside that checkout (a first sight there, or `project init`
        there), each with its file id and folder: what `project new` and a
        `project join` that moves the checkout take along (B10). A part
        first bound in another checkout stays with the project that holds
        it (one file id binds one project); the next session in that part
        here asks once, L7, whose two commands settle it."""
        k = self.checkout(checkout_id)
        mine = self._canon(k.project)
        out: list[tuple[Project, str, str | None]] = []
        seen: set[str] = set()
        for row in self._store.membership_log(["project_file_bound"]):
            payload = json.loads(row.payload)
            if not isinstance(payload, dict) or payload.get("checkout") != k.id:
                continue
            file_id = str(payload.get("file_id") or "")
            bound = self._file_binding(file_id) if file_id else None
            if bound is None or bound in seen:
                continue
            seen.add(bound)
            part = self.project(bound)
            if self.canonical(part.parent) == mine:
                folder = payload.get("dir")
                out.append((part, file_id, str(folder) if folder else None))
        return out

    def _reparent_parts(
        self,
        k: Checkout,
        parts: Sequence[tuple[Project, str, str | None]],
        new: str,
        by: str,
        command: str,
    ) -> None:
        """B10: the checkout's parts (`parts_in`, read before the checkout
        left its project) become parts of `new`, so a later session in a
        part resolves to it silently. Each is a logged project row and a
        ``project_reparented`` row naming the declaration (B13)."""
        for part, file_id, folder in parts:
            old = self.canonical(part.parent)
            self._put_project(dataclasses.replace(self.project(part.id), parent=new))
            self._event(
                "project_reparented",
                project=part.id,
                parent=new,
                previous=old,
                file_id=file_id,
                dir=folder,
                in_checkout=k.id,
                evidence=f"{by}: bettermemory {command} in checkout {k.id}",
            )

    def _move_record(self, record_id: str, target: str, by: str) -> str:
        rows = self._store.membership_rows("homes", where={"record_id": record_id})
        if not rows:
            raise KeyError(record_id)
        dst = target if target == GLOBAL else self._canon(target)
        if dst != GLOBAL:
            self.project(dst)
        row = rows[0]
        self._put("home", {**row, "project": dst, "rule": f"{row['rule']}|{by}:move"})
        return dst

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

    def unresolved_home(self, loc: Location, *, session: str | None) -> RecordHome:
        """The home of a write made where the resolution was INDETERMINATE
        (nothing readable) or DEFERRED (an evicted file at first sight).
        The write is neither refused nor made global: it lands in a
        provisional project of its own (reason ``unresolved_location``,
        provisional ``indeterminate`` or ``deferred``), on a checkout of
        kind ``unresolved`` keyed on the machine and the cwd as given,
        which every later write there finds again until a resolution at
        that path settles it (`resolve`). `session` is the writing
        session (``loc.session`` when None)."""
        if loc.state == UNREADABLE:
            state = INDETERMINATE
        elif loc.evicted:
            state = DEFERRED
        else:
            raise ValueError(
                f"{loc.cwd} was read: a write there takes its resolution's home"
            )
        who = session if session is not None else loc.session
        with self._call():
            k = self._unresolved_at(loc.machine, loc.cwd)
            if k is None:
                p = self._mint_project(
                    _basename(loc.cwd), reason=UNRESOLVED_LOCATION, provisional=state
                )
                k = self._register_unresolved(loc, p.id, state, who)
            elif who is not None and who not in k.sessions:
                k = self._put_checkout(
                    dataclasses.replace(k, sessions=(*k.sessions, who))
                )
        return RecordHome(k.project, k.id, f"unresolved:{state}")

    def _register_unresolved(
        self, loc: Location, project_id: str, state: str, session: str | None
    ) -> Checkout:
        kid = self._store.next_membership_id("checkouts", "K")
        stamp = self._stamp()
        view = View(
            checkout=kid,
            machine=loc.machine,
            path=_norm(loc.cwd),
            first_seen=stamp,
            last_seen=stamp,
            order=self._store.next_membership_number("checkout_views", "seen_order"),
        )
        k = Checkout(
            id=kid,
            project=project_id,
            machine=loc.machine,
            kind=UNRESOLVED,
            object_key=None,
            common_object_key=None,
            git_dir=None,
            token=None,
            token_status="n/a",
            stamps=(),
            last_head=None,
            branch=None,
            remotes=(),
            manifest=None,
            legacy=False,
            retired=False,
            registered=stamp,
            views=(view,),
            sessions=(session,) if session is not None else (),
        )
        self._put_checkout(k)
        self._put_view(view)
        self._event(
            "checkout_registered",
            checkout=kid,
            project=project_id,
            path=view.path,
            machine=loc.machine,
            checkout_kind=UNRESOLVED,
            rule=f"unresolved:{state}",
            token="n/a",
            session=session,
            detail=loc.detail,
        )
        return k

    def _unresolved_at(self, machine: str, path: str) -> Checkout | None:
        """The unsettled ``unresolved`` checkout at `path` on `machine`."""
        found = [
            k
            for k in (
                self.checkout(str(v["checkout"]))
                for v in self._store.membership_rows(
                    "checkout_views", where={"machine": machine, "path": _norm(path)}
                )
            )
            if k.kind == UNRESOLVED and k.replaced_by is None
        ]
        return max(found, key=lambda k: k.registered) if found else None

    def _unresolved_of(self, session: str, path: str) -> list[Checkout]:
        """The unsettled ``unresolved`` checkouts at `path` on any machine
        that `session` wrote at (decision B24)."""
        return [
            k
            for k in (
                self.checkout(str(v["checkout"]))
                for v in self._store.membership_rows(
                    "checkout_views", where={"path": _norm(path)}
                )
            )
            if k.kind == UNRESOLVED and k.replaced_by is None and session in k.sessions
        ]

    def session_resolution(
        self, loc: Location, prior: Resolution, at: str
    ) -> Resolution | None:
        """A write where nothing could be read, by a harness session whose
        latest hook resolution (`prior`, made at `at`) has a checkout with a
        view at that path or above it (decision B24): the resolution of that
        checkout, rule ``session_resolved``, SILENT, with one
        ``resolution_session`` row, so the write takes its home there and no
        provisional project is minted. At or below `at` the project is the
        one the hook resolved (a part where a nested file named one);
        elsewhere in the checkout, the checkout's project. None when the
        session's checkout has no view holding the path."""
        if (
            loc.state != UNREADABLE
            or loc.session is None
            or prior.checkout is None
            or prior.project in (None, GLOBAL)
        ):
            return None
        with self._call():
            try:
                k = self.checkout(prior.checkout)
            except KeyError:
                return None
            held = any(_inside(loc.cwd, v.path) for v in k.views_on(k.machine))
            if k.kind == UNRESOLVED or not held:
                return None
            hooked = [self._canon(c) for c in prior.chain]
            if _inside(loc.cwd, at) and prior.project is not None:
                project = self._canon(prior.project)
                chain = list(dict.fromkeys([project, *hooked]))
            else:
                project = self._canon(k.project)
                own = self.chain_of(project)
                around = hooked[len(self.chain_of(prior.project)) :]
                chain = list(dict.fromkeys([*own, *around]))
            tenant = next((c for c in chain if self.project(c).boundary), prior.tenant)
            self._event(
                "resolution_session",
                checkout=k.id,
                project=project,
                session=loc.session,
                path=loc.cwd,
            )
            rows = tuple(self._rows)
        return Resolution(
            outcome=SILENT,
            project=project,
            checkout=k.id,
            rule="session_resolved",
            chain=tuple(chain),
            tenant=tenant,
            rows=rows,
        )

    def _record_counts(self, project_id: str) -> tuple[int, int]:
        """(memories, episodes) whose homes name the project."""
        rows = self._store.membership_rows(
            "homes", column_in=("project", self._members(project_id))
        )
        memories = sum(1 for r in rows if r["kind"] == "memory")
        return memories, len(rows) - memories

    def _settle_unresolved(self, loc: Location, res: Resolution) -> Resolution:
        """A resolution ending with a checkout K at a path written at while
        nothing could be read there (its root or its cwd): the session that
        wrote there merges those writes into K's project and the unresolved
        checkout is replaced; any other session, or no session, meets a
        ``legacy`` question in K while the writes stay apart (decision B6).
        A write by a process with no machine identity registered its path
        under `location.NO_MACHINE`; a resolution on any machine finds it
        there too (decision B16). One the resolving session wrote is found
        under any machine, the session proving the namespace (a container
        client's path, written at through the daemon's machine; decision
        B24)."""
        if res.checkout is None:
            return res
        paths = {_norm(loc.cwd)}
        if loc.root:
            paths.add(_norm(loc.root))
        found: dict[str, Checkout] = {}
        for path in sorted(paths):
            for machine in dict.fromkeys((loc.machine, NO_MACHINE)):
                u = self._unresolved_at(machine, path)
                if u is not None:
                    found[u.id] = u
            if loc.session is not None:
                for u in self._unresolved_of(loc.session, path):
                    found.setdefault(u.id, u)
        unsettled = list(found.values())
        if not unsettled:
            return res
        k = self.checkout(res.checkout)
        target = self._canon(k.project)
        lines = list(res.lines)
        outcome, proposal = res.outcome, res.proposal
        for u in unsettled:
            mine = self._canon(u.project)
            if mine == target or (
                loc.session is not None and loc.session in u.sessions
            ):
                self._merge(
                    mine,
                    target,
                    evidence="written while the path could not be read; the same "
                    "session resolved it at the next prompt",
                )
                self._put_checkout(dataclasses.replace(u, replaced_by=k.id))
                continue
            memories, episodes = self._record_counts(mine)
            if memories + episodes == 0:
                continue
            opened = {x.id for x in self._open_proposals(k.id)}
            path = u.newest_view.path if u.newest_view is not None else loc.cwd
            pr = self._open_proposal(
                checkout=k.id,
                kind=LEGACY,
                candidates=[target],
                evidence={
                    "unresolved_project": mine,
                    "unresolved_checkout": u.id,
                    "path": path,
                    "memories": memories,
                    "episodes": episodes,
                    "sessions": list(u.sessions),
                },
                reason=UNRESOLVED_LOCATION,
                line=self._unresolved_line(path, memories, episodes, target),
                signature=None,
                distinct="unresolved_project",
            )
            if pr.id not in opened and pr.line not in lines:
                # Named as every open question of the checkout is (`_caller`).
                lines.append(pr.line)
                outcome = ASK if outcome == SILENT else outcome
                proposal = proposal or pr.id
        return dataclasses.replace(
            res, lines=tuple(lines), outcome=outcome, proposal=proposal
        )

    def _unresolved_line(
        self, path: str, memories: int, episodes: int, target: str
    ) -> Callable[[str], str]:
        """The B6 question's line, built from the question's id."""

        def line(q: str) -> str:
            return line_unresolved(
                path, memories, episodes, self.project(target).name, target, question=q
            )

        return line

    def _answer_unresolved(
        self, pr: Proposal, k: Checkout, decl: Declaration, by: str, *, join: bool
    ) -> str:
        """The answers to a B6 ``legacy`` question: ``join`` merges the
        writes' project into the project named (the checkout's by
        default); ``new`` makes it a project of its own with no live
        checkout. Either way the unresolved checkout is settled."""
        unresolved = self._canon(str(pr.evidence["unresolved_project"]))
        if join:
            target = self._canon(decl.target or k.project)
            self._merge(unresolved, target, evidence=by)
            answer, result = f"join:{target}", target
        else:
            p = self.project(unresolved)
            self._put_project(
                dataclasses.replace(p, provisional=None, name=decl.name or p.name)
            )
            answer, result = "new", unresolved
        u = self.checkout(str(pr.evidence["unresolved_checkout"]))
        if u.replaced_by is None:
            self._put_checkout(dataclasses.replace(u, replaced_by=k.id))
        self._close(pr, answer, by, said=decl.said)
        return result

    def _answer_import(
        self, pr: Proposal, k: Checkout, decl: Declaration, by: str, *, join: bool
    ) -> str:
        """The answers to an ``import`` question (design 8.7, line L10):
        ``join`` merges the imported project into the project named (the
        checkout's when none is, or when the imported project itself is
        named); ``new`` keeps it a project of its own. The other questions
        about the same imported project close with the same answer."""
        imported_id = str(pr.evidence.get("imported_project") or "")
        imported = self._canon(imported_id)
        mine = self._canon(k.project)
        if join:
            target = self._canon(decl.target) if decl.target else mine
            if target == imported:
                target = mine
            self._merge(imported, target, evidence=f"{by}: import question {pr.id}")
            answer, result = f"join:{target}", target
        else:
            p = self.project(imported)
            if p.provisional is not None:
                self._put_project(dataclasses.replace(p, provisional=None))
            answer, result = "new", mine
        self._close(pr, answer, by)
        for other in self._open_proposals():
            if other.kind == IMPORT and other.evidence.get("imported_project") == (
                imported_id
            ):
                self._close(other, answer, f"{by} (question {pr.id})")
        return result

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

    def home(self, record_id: str) -> Home | None:
        """The record's home as `relation` reads it, or None when it has
        none."""
        return self.homes([record_id]).get(record_id)

    def homes(self, record_ids: Sequence[str]) -> dict[str, Home]:
        """The homes of many records, one read, a global record's with the
        projects its scopes name (`named`) read in one more."""
        self._projects = None
        rows = self._store.membership_rows("homes", column_in=("record_id", record_ids))
        scopes = self._store.declared_scopes(
            [str(r["record_id"]) for r in rows if str(r["project"]) == GLOBAL]
        )
        return {
            str(r["record_id"]): self._home_from(r, scopes.get(str(r["record_id"]), ()))
            for r in rows
        }

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

    # -- surfaces -------------------------------------------------------------------------------

    def admit_recall(
        self,
        caller: Resolution,
        hits: Sequence[str],
        *,
        admission: str = PROMPT_RECALL,
    ) -> RecallAdmission:
        """A prompt recall's admission over its ranked hits (record ids),
        under the surface `admission` names: PROMPT_RECALL, or
        SEARCH_UNSCOPED where the recall is set to reach every project
        (``prompt_recall = "everywhere"``, decision B21). A turn whose
        hits were all suppressed writes ``recall_suppressed`` (design 4,
        failure 8), naming the surface and the admission: no silent
        suppression. Every hit is a candidate since rs-v6 (RS-F7 part 0);
        until then only the medium and high ones were."""
        with self._call():
            homes = self.homes(hits)
            delivered: list[str] = []
            suppressed: list[tuple[str, str]] = []
            for rid in hits:
                home = homes.get(rid)
                if home is None:
                    suppressed.append((rid, "no_home"))
                    continue
                if admitted(admission, home, caller):
                    delivered.append(rid)
                elif home.unresolved:
                    suppressed.append((rid, UNRESOLVED))
                else:
                    suppressed.append((rid, relation(home, caller)))
            if suppressed and not delivered:
                self._event(
                    "recall_suppressed",
                    surface=PROMPT_RECALL,
                    admission=admission,
                    checkout=caller.checkout,
                    caller_rule=caller.rule,
                    suppressed=[{"id": rid, "reason": why} for rid, why in suppressed],
                )
        return RecallAdmission(tuple(delivered), tuple(suppressed))

    def first_output(self, res: Resolution) -> list[str]:
        """The lines a session's first output carries: the resolution's,
        and every proposal still open on its checkout."""
        lines = list(res.lines)
        if res.checkout is not None:
            for pr in self._open_proposals(res.checkout):
                if pr.line not in lines:
                    lines.append(pr.line)
        return lines

    # -- run-state and anchoring (design 3.6) ------------------------------------------------

    def handoff(self, caller: Resolution, episodes: Sequence[EpisodeRef]) -> Handoff:
        """The prior session's episodes for the caller's checkout; a primary
        checkout with none of its own is offered the newest session of a
        primary checkout of its project that is not live here, labelled.
        Nothing takes a live sibling's run-state, and another machine's
        checkout is never live from here."""
        self._projects = None
        if caller.checkout is None:
            return Handoff(None, "no_checkout")
        homes = self.homes([e.id for e in episodes])
        own = [
            e
            for e in episodes
            if (h := homes.get(e.id)) and h.checkout == caller.checkout
        ]
        if own:
            session_id = max(own, key=lambda e: e.created).session_id
            return Handoff(
                session_id,
                "own_checkout",
                tuple(sorted(e.id for e in own if e.session_id == session_id)),
            )
        me = self.checkout(caller.checkout)
        if me.kind != PRIMARY or caller.project is None:
            return Handoff(None, "none")
        project = self._canon(me.project)
        pool: list[tuple[EpisodeRef, Checkout, str]] = []
        cache: dict[str, Checkout] = {}
        for e in episodes:
            h = homes.get(e.id)
            if h is None or h.checkout is None or h.project != project:
                continue
            k = cache.get(h.checkout) or self.checkout(h.checkout)
            cache[k.id] = k
            if k.kind != PRIMARY:
                continue
            state = self.view_state(k)
            if state in _LIVE_STATES:
                continue
            pool.append((e, k, state))
        if not pool:
            return Handoff(None, "none")
        e, k, state = max(pool, key=lambda item: item[0].created)
        last_view = k.newest_view
        label = (
            f"prior session of checkout {k.id} ({state}, last at "
            f"{last_view.path if last_view else k.id})"
        )
        return Handoff(
            e.session_id,
            "inherited_from_non_live_checkout",
            tuple(
                sorted(
                    x.id
                    for x, kk, _s in pool
                    if x.session_id == e.session_id and kk.id == k.id
                )
            ),
            label,
        )

    def anchor(
        self, record_id: str, caller: Resolution | None
    ) -> tuple[str | None, str]:
        """Where a record's relative claims resolve: its home checkout's
        live view (``home_checkout``, convicting); else the caller's
        checkout of the same project, else any live checkout of the project
        (stand-ins, which confirm by presence and never convict); else
        nowhere, said so."""
        home = self.home(record_id)
        if home is None or home.project == GLOBAL:
            return None, "global_record"
        if home.checkout is not None:
            live = self.live_view(self.checkout(home.checkout))
            if live is not None:
                return live, "home_checkout"
        if (
            caller is not None
            and caller.checkout is not None
            and caller.project is not None
            and self.canonical(caller.project) == home.project
        ):
            live = self.live_view(self.checkout(caller.checkout))
            if live is not None:
                return live, "caller_checkout_same_project"
        for k in sorted(
            self._checkouts_of(home.project), key=lambda k: (k.kind != PRIMARY, k.id)
        ):
            live = self.live_view(k)
            if live is not None:
                return live, "project_checkout"
        return None, "no_live_checkout"

    def translate(self, path: str, caller: Resolution | None = None) -> tuple[str, str]:
        """An absolute path under a recorded view of any checkout names a
        file of that checkout's tree: it resolves at that checkout's live
        view here (``checkout``, convicting), else at a live checkout of
        the same project here, the caller's first (stand-ins), else it is
        ``unanchored``. A path under no recorded view stays ``absolute``."""
        self._projects = None
        best: tuple[int, Checkout, View] | None = None
        for k in self._checkouts():
            if k.kind == UNRESOLVED:
                continue
            for v in k.views:
                if _inside(path, v.path):
                    if best is None or len(v.path) > best[0]:
                        best = (len(v.path), k, v)
        if best is None:
            return path, "absolute"
        _len, k, v = best
        target: str | None = None
        how = ""
        if k.machine == self.machine:
            target, how = self.live_view(k), "checkout"
        if target is None:
            project = self._canon(k.project)
            if (
                caller is not None
                and caller.checkout is not None
                and caller.project is not None
                and self.canonical(caller.project) == project
            ):
                target = self.live_view(self.checkout(caller.checkout))
                how = "caller_checkout_same_project"
            if target is None:
                for kk in sorted(
                    self._checkouts_of(project), key=lambda x: (x.kind != PRIMARY, x.id)
                ):
                    if kk.machine == self.machine:
                        target = self.live_view(kk)
                        if target is not None:
                            how = "project_checkout"
                            break
        if target is None:
            return path, "unanchored"
        return _rebase(path, v.path, target), how

    # -- export and import (design 8.7, E27) ----------------------------------------------

    def portable_identity(
        self, project_id: str, *, parent: int | None = None
    ) -> PortableIdentity:
        """The project's portable identity (design 8.7): its bound file ids,
        the oldest and newest halves of its witness commits by arrival (at
        most `EXPORT_WITNESSES`), its remote keys other than ``local:``
        ones, with the projects merged into it; `parent` is the position of
        its parent in the document it goes into."""
        pid = self._canon(project_id)
        p = self.project(pid)
        members = self._members(pid)
        files = sorted(
            {
                str(r["file_id"])
                for r in self._store.membership_rows(
                    "project_files", column_in=("project", members)
                )
            }
        )
        order = list(
            dict.fromkeys(str(r["commit_id"]) for r in self._witness_rows(pid))
        )
        half = EXPORT_WITNESSES // 2
        kept = order[:half] + [c for c in order[-half:] if c not in order[:half]]
        remotes = sorted(k for k in self._remotes_of(pid) if not k.startswith("local:"))
        return PortableIdentity(
            name=p.name,
            file_ids=tuple(files),
            witnesses=tuple(kept),
            remotes=tuple(remotes),
            folder=p.folder,
            boundary=p.boundary,
            parent=parent,
        )

    def export_document(
        self, record_ids: Collection[str] | None = None
    ) -> dict[str, Any]:
        """What an export carries (design 8.7): this store's id
        (``store_id``), the portable identity of every project, or with
        `record_ids` of those records' projects and the parents on their
        chains (``projects``, parents first), and each record's home keyed
        by record id (``homes``: its kind and its project's position in
        ``projects``, null for global). A home naming a project this store
        has no row for is left out."""
        self._projects = None
        projects = self._all_projects()
        if record_ids is None:
            rows = self._store.membership_rows("homes")
            wanted = [p.id for p in self.projects()]
        else:
            rows = self._store.membership_rows(
                "homes", column_in=("record_id", sorted(set(record_ids)))
            )
            wanted = sorted(
                {
                    self._canon(str(r["project"]))
                    for r in rows
                    if str(r["project"]) != GLOBAL and str(r["project"]) in projects
                }
            )
        position: dict[str, int] = {}
        identities: list[PortableIdentity] = []

        def place(pid: str, seen: frozenset[str]) -> int:
            if pid in position:
                return position[pid]
            parent = self.canonical(projects[pid].parent)
            at: int | None = None
            if parent is not None and parent in projects and parent not in seen:
                at = place(parent, seen | {pid})
            position[pid] = len(identities)
            identities.append(self.portable_identity(pid, parent=at))
            return position[pid]

        for pid in wanted:
            place(self._canon(pid), frozenset())
        homes: dict[str, dict[str, Any]] = {}
        for r in sorted(rows, key=lambda r: str(r["record_id"])):
            recorded = str(r["project"])
            if recorded == GLOBAL:
                homes[str(r["record_id"])] = {"kind": str(r["kind"]), "project": None}
            elif recorded in projects:
                homes[str(r["record_id"])] = {
                    "kind": str(r["kind"]),
                    "project": place(self._canon(recorded), frozenset()),
                }
        return {
            "store_id": self._store.membership_store_id,
            "projects": [i.to_json() for i in identities],
            "homes": homes,
        }

    def _imported_before(self, marker: str) -> str | None:
        """The project an earlier import minted under `marker` (its
        ``project_minted`` reason), merges followed."""
        found: str | None = None
        for row in self._store.membership_log(["project_minted"]):
            try:
                payload = json.loads(row.payload)
            except ValueError:
                continue
            if isinstance(payload, dict) and payload.get("reason") == marker:
                found = str(payload.get("project"))
        if found is None or found not in self._all_projects():
            return None
        return self._canon(found)

    def _evidence_checkout(self, project_id: str) -> Checkout | None:
        """The checkout an ``import`` question about the project is asked
        in: one of its checkouts, this machine's live ones first, then this
        machine's, then the others, retired ones last."""
        here = self.machine
        ranked = sorted(
            self._checkouts_of(project_id),
            key=lambda k: (
                k.retired,
                k.machine != here,
                k.machine != here or self.live_view(k) is None,
                k.id,
            ),
        )
        return ranked[0] if ranked else None

    def import_identity(
        self,
        identity: PortableIdentity,
        *,
        source: str,
        source_store: str | None = None,
        parent: str | None = None,
    ) -> ImportedProject:
        """Map an imported project to this store (design 8.7, E27, T16), the
        one place an import decides membership. A file id bound here to
        one project maps there (F), unless the identity's commits are
        another project's and none of that one's; else history and a
        remote together map to the one project showing both (H+R). Both
        are silent. One witness, several projects or that contradiction
        mints a project of the identity's own, kept apart and provisional,
        with an ``import`` question (line L10) in a checkout of each
        candidate project. No witness mints a project with no live
        checkout that a later checkout joins by the same evidence. A
        candidate across a boundary is dropped. `parent` is the local
        project the identity's parent was mapped to; `source` names the
        import in the line, `source_store` the store it came from. An
        identity an earlier import from the same store minted a project
        for maps to that project again."""
        with self._call():
            return self._import_identity(identity, source, source_store, parent)

    def _import_identity(
        self,
        identity: PortableIdentity,
        source: str,
        source_store: str | None,
        parent: str | None,
    ) -> ImportedProject:
        marker = f"import:{source_store or 'unknown'}:{identity.digest()}"
        earlier = self._imported_before(marker)
        if earlier is not None:
            asked = tuple(
                pr.id
                for pr in self._open_proposals()
                if pr.kind == IMPORT
                and self.canonical(str(pr.evidence.get("imported_project"))) == earlier
            )
            return ImportedProject(earlier, "import:earlier", {}, asked)
        parent_id = self.canonical(parent) if parent is not None else None
        tenant = self.project(parent_id).tenant if parent_id is not None else None
        cands: dict[str, set[str]] = defaultdict(set)
        for fid in identity.file_ids:
            bound = self._file_binding(fid)
            if bound is not None:
                cands[bound].add(F)
        if identity.witnesses:
            for row in self._store.membership_rows(
                "project_witnesses", column_in=("commit_id", identity.witnesses)
            ):
                cands[self._canon(str(row["project"]))].add(H)
        if identity.remotes:
            for row in self._store.membership_rows(
                "project_remotes", column_in=("remote_key", identity.remotes)
            ):
                cands[self._canon(str(row["project"]))].add(R)
        for pid in list(cands):
            known = self.project(pid)
            if known.boundary:
                outside = (
                    self.project(self._canon(known.parent)).tenant
                    if known.parent
                    else None
                )
            else:
                outside = known.tenant
            if known.provisional in UNRESOLVED_STATES or outside != tenant:
                # Writes made where nothing could be read witness nothing,
                # and nothing joins across a boundary (D17).
                del cands[pid]
        codes = {q: tuple(sorted(c)) for q, c in sorted(cands.items())}
        filed = sorted(q for q, c in cands.items() if F in c)
        strong = sorted(q for q, c in cands.items() if {H, R} <= c)
        contradiction = (
            len(filed) == 1
            and bool(identity.witnesses)
            and H not in cands[filed[0]]
            and any(H in c for q, c in cands.items() if q != filed[0])
        )
        target: str | None = None
        if len(filed) == 1 and not contradiction:
            target = filed[0]
        elif not filed and len(strong) == 1:
            target = strong[0]
        if target is not None:
            return ImportedProject(target, "import:" + "+".join(codes[target]), codes)
        if contradiction:
            reason = IMPORT_CONTRADICTION
        elif len(filed) > 1 or len(strong) > 1:
            reason = IMPORT_SEVERAL
        else:
            reason = IMPORT_ONE_WITNESS
        holders = [
            (q, k)
            for q in sorted(cands)
            if (k := self._evidence_checkout(q)) is not None
        ]
        p = self._mint_project(
            identity.name,
            reason=marker,
            provisional=IMPORT if holders else None,
            parent=parent_id,
            tenant=tenant,
            folder=identity.folder,
            boundary=identity.boundary,
        )
        for commit in identity.witnesses:
            self._add_witness(p.id, commit, source=f"import:{source}", history=None)
        self._add_remotes(p.id, identity.remotes)
        for fid in identity.file_ids:
            if self._file_binding(fid) is None:
                self._bind_file(fid, p.id, evidence=f"import from {source}")
        opened: list[str] = []
        for q, k in holders:
            pr = self._open_proposal(
                checkout=k.id,
                kind=IMPORT,
                candidates=[q],
                evidence={
                    "imported_project": p.id,
                    "source": source,
                    "source_store": source_store,
                    "codes": {q: list(codes[q])},
                },
                reason=reason,
                line=self._import_line(identity.name, source, q, codes[q]),
                signature=None,
                distinct="imported_project",
            )
            opened.append(pr.id)
        rule = f"import:ask:{reason}" if holders else "import:new:no_live_checkout"
        return ImportedProject(p.id, rule, codes, tuple(opened))

    def _import_line(
        self, name: str, source: str, candidate: str, codes: Sequence[str]
    ) -> Callable[[str], str]:
        """An import question's line (L10), built from the question's id."""

        def line(q: str) -> str:
            return line_import(
                name,
                source,
                [(self.project(candidate).name, candidate, codes)],
                question=q,
            )

        return line

    def import_home(
        self,
        record_id: str,
        kind: str,
        imported: ImportedProject | None,
        *,
        source: str,
        source_store: str | None = None,
    ) -> bool:
        """Home a record present in this store and homed nowhere by its
        imported identity's mapping (`import_identity`; None for a global
        record): its home row, with no checkout, and an ``import_homed``
        row naming the rule and the source. A record that already has a
        home keeps it (a home is written once, design 1); returns whether
        one was written."""
        if kind not in ("memory", "episode"):
            raise ValueError(f"a home is a memory's or an episode's, not {kind!r}")
        with self._call():
            if self._store.membership_rows("homes", where={"record_id": record_id}):
                return False
            if imported is None:
                project, rule = GLOBAL, "import:global"
            else:
                project, rule = self._canon(imported.project), imported.rule
            self._put(
                "home",
                {
                    "record_id": record_id,
                    "kind": kind,
                    "project": project,
                    "checkout": None,
                    "rule": rule,
                    "decided": self._stamp(),
                },
            )
            self._event(
                "import_homed",
                record=record_id,
                kind=kind,
                project=project,
                rule=rule,
                source=source,
                source_store=source_store,
            )
        return True

    # -- health ---------------------------------------------------------------------------------

    def retire_quiet(self, *, quiet: timedelta = RETIRE_AFTER) -> list[str]:
        """Checkouts unseen for `quiet` leave the health lists
        (``checkout_retired``); their rows stay, and no rule reads the flag
        (D15, X12). A sighting brings one back."""
        with self._call():
            cutoff = self._now() - quiet
            out: list[str] = []
            for k in self._checkouts():
                if k.retired:
                    continue
                seen = [parse_event_ts(v.last_seen) for v in k.views]
                newest = max((s for s in seen if s is not None), default=None)
                if newest is not None and newest < cutoff:
                    self._put_checkout(dataclasses.replace(k, retired=True))
                    self._event("checkout_retired", checkout=k.id)
                    out.append(k.id)
            return out

    def health(self) -> Health:
        """The membership section of health (design 8.10): open proposals
        with their lines, provisional projects, suppressed recall turns per
        checkout, indeterminate and deferred captures, the checkouts on
        this machine with no live view, retired ones excluded, and the
        provisional projects of writes made where nothing could be read,
        with their record counts (decision B6)."""
        self._projects = None
        suppressed: dict[str, int] = defaultdict(int)
        counts = {"resolution_indeterminate": 0, "resolution_deferred": 0}
        for row in self._store.membership_log(
            ["recall_suppressed", "resolution_indeterminate", "resolution_deferred"]
        ):
            if row.kind == "recall_suppressed":
                try:
                    payload = json.loads(row.payload)
                except ValueError:
                    payload = {}
                checkout = (
                    payload.get("checkout") if isinstance(payload, dict) else None
                )
                suppressed[str(checkout) if checkout else "none"] += 1
            else:
                counts[row.kind] += 1
        checkouts = self._checkouts()
        without = sorted(
            k.id
            for k in checkouts
            if k.machine == self.machine
            and k.kind != UNRESOLVED
            and not k.retired
            and self.live_view(k) is None
        )
        unresolved: list[UnresolvedProject] = []
        for p in self.projects():
            if p.provisional not in UNRESOLVED_STATES or p.merged_into is not None:
                continue
            k = next(
                (x for x in checkouts if x.kind == UNRESOLVED and x.project == p.id),
                None,
            )
            newest = k.newest_view if k is not None else None
            memories, episodes = self._record_counts(p.id)
            unresolved.append(
                UnresolvedProject(
                    project=p.id,
                    name=p.name,
                    state=str(p.provisional),
                    checkout=None if k is None else k.id,
                    path=None if newest is None else newest.path,
                    memories=memories,
                    episodes=episodes,
                )
            )
        return Health(
            open_proposals=tuple(self._open_proposals()),
            provisional=tuple(
                p
                for p in self.projects()
                if p.provisional and p.provisional not in UNRESOLVED_STATES
            ),
            suppressed_turns=dict(suppressed),
            indeterminate=counts["resolution_indeterminate"],
            deferred=counts["resolution_deferred"],
            without_live_view=tuple(without),
            unresolved=tuple(unresolved),
        )


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
