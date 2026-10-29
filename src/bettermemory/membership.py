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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

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
    CONTENT_MATCH,
    CONTENT_MAX,
    DEFERRED,
    DIFFERENT,
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
    WORDS,
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
    _HISTORY,
    _INITS,
    _LIVE_STATES,
    _TOKEN_NAME,
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
from .models import utcnow
from .store import RecordHome, Store
from .time_utils import isoformat_utc, parse_event_ts

if TYPE_CHECKING:
    from .log import LogRow
    from .machine import MachineIdentity

# ---------------------------------------------------------------------------
# Row conversions
# ---------------------------------------------------------------------------


def _key_columns(prefix: str, key: ObjectKey | None) -> dict[str, Any]:
    return {
        f"{prefix}_device": None if key is None else key.device,
        f"{prefix}_inode": None if key is None else key.inode,
        f"{prefix}_birth": None if key is None else key.birth,
    }


def _key_from(row: Mapping[str, Any], prefix: str) -> ObjectKey | None:
    device, inode = row[f"{prefix}_device"], row[f"{prefix}_inode"]
    if device is None or inode is None:
        return None
    birth = row[f"{prefix}_birth"]
    return ObjectKey(str(device), str(inode), None if birth is None else str(birth))


def _stamps_from(raw: Any) -> tuple[InitStamp, ...]:
    out: list[InitStamp] = []
    for item in raw or ():
        if isinstance(item, (list, tuple)) and len(item) == 5:
            out.append(
                InitStamp(
                    str(item[0]), int(item[1]), int(item[2]), int(item[3]), int(item[4])
                )
            )
    return tuple(out)


def _view_from(row: Mapping[str, Any]) -> View:
    return View(
        checkout=str(row["checkout"]),
        machine=str(row["machine"]),
        path=str(row["path"]),
        first_seen=str(row["first_seen"]),
        last_seen=str(row["last_seen"]),
        order=int(row["seen_order"]),
    )


def _checkout_from(row: Mapping[str, Any], views: Sequence[View]) -> Checkout:
    raw = row.get("unresolved_json")
    unresolved: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    return Checkout(
        id=str(row["id"]),
        project=str(row["project"]),
        machine=str(row["machine"]),
        kind=str(row["kind"]),
        object_key=_key_from(row, "object"),
        common_object_key=_key_from(row, "common"),
        git_dir=row["git_dir"],
        token=row["token"],
        token_status=str(row["token_status"]),
        stamps=_stamps_from(row["stamps_json"]),
        last_head=row["last_head"],
        branch=row["branch"],
        remotes=tuple(str(r) for r in row["remotes_json"] or ()),
        manifest=Manifest.from_json(row["manifest_json"]),
        legacy=bool(row["legacy"]),
        retired=bool(row["retired"]),
        registered=str(row["registered"]),
        views=tuple(sorted(views, key=lambda v: v.order)),
        sessions=tuple(str(x) for x in unresolved.get("sessions") or ()),
        replaced_by=unresolved.get("replaced_by"),
    )


def _checkout_row(k: Checkout) -> dict[str, Any]:
    return {
        "id": k.id,
        "project": k.project,
        "machine": k.machine,
        "kind": k.kind,
        **_key_columns("object", k.object_key),
        **_key_columns("common", k.common_object_key),
        "git_dir": k.git_dir,
        "token": k.token,
        "token_status": k.token_status,
        "stamps_json": [list(s) for s in k.stamps],
        "last_head": k.last_head,
        "branch": k.branch,
        "remotes_json": list(k.remotes),
        "manifest_json": None if k.manifest is None else k.manifest.to_json(),
        "legacy": 1 if k.legacy else 0,
        "retired": 1 if k.retired else 0,
        "registered": k.registered,
        "unresolved_json": (
            {"sessions": list(k.sessions), "replaced_by": k.replaced_by}
            if k.kind == UNRESOLVED
            else None
        ),
    }


def _project_from(row: Mapping[str, Any]) -> Project:
    return Project(
        id=str(row["id"]),
        name=str(row["name"]),
        merged_into=row["merged_into"],
        provisional=row["provisional"],
        parent=row["parent"],
        tenant=row["tenant"],
        folder=bool(row["folder"]),
        boundary=bool(row["boundary"]),
        minted=str(row["minted"]),
    )


def _project_row(p: Project) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "merged_into": p.merged_into,
        "provisional": p.provisional,
        "parent": p.parent,
        "tenant": p.tenant,
        "folder": 1 if p.folder else 0,
        "boundary": 1 if p.boundary else 0,
        "minted": p.minted,
    }


def _proposal_from(row: Mapping[str, Any]) -> Proposal:
    signature = row["signature_json"]
    return Proposal(
        id=str(row["id"]),
        checkout=row["checkout"],
        kind=str(row["kind"]),
        candidates=tuple(str(c) for c in row["candidates_json"] or ()),
        evidence=dict(row["evidence_json"] or {}),
        reason=str(row["reason"]),
        line=str(row["line"]),
        state=str(row["state"]),
        answer=row["answer"],
        answered_by=row["answered_by"],
        signature=None if signature is None else tuple(signature),
        opened=str(row["opened"]),
    )


def _proposal_row(pr: Proposal) -> dict[str, Any]:
    return {
        "id": pr.id,
        "checkout": pr.checkout,
        "kind": pr.kind,
        "candidates_json": list(pr.candidates),
        "evidence_json": _jsonable(pr.evidence),
        "reason": pr.reason,
        "line": pr.line,
        "state": pr.state,
        "answer": pr.answer,
        "answered_by": pr.answered_by,
        "signature_json": None if pr.signature is None else list(pr.signature),
        "opened": pr.opened,
    }


def _jsonable(value: Any) -> Any:
    """`value` as plain JSON values: tuples and sets become sorted or
    ordered lists, mappings dicts."""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _signature(loc: Location) -> tuple[Any, ...]:
    """A checkout's evidence signature (design 3.2): HEAD, remotes, root
    file, token, shallow. A first-sight proposal re-tallies when it
    changes. The token is the one the checkout carries after this
    sighting, so a token the capture wrote at first sight is no change."""
    rf = loc.root_file
    return (
        loc.head,
        sorted(loc.remote_keys),
        None if rf is None else rf.id,
        loc.token or loc.token_written,
        loc.shallow,
    )


def _unwritten(loc: Location) -> str | None:
    """The token file and status a line names when the capture could not
    write the token (read-only, another owner, the policy switch)."""
    if not loc.token_status.startswith(("failed", "skipped")):
        return None
    where = _path_under(loc.git_dir or "", "info", _TOKEN_NAME)
    return f"{where}: {loc.token_status}"


@dataclass
class _Tally:
    cands: dict[str, set[str]] = field(default_factory=dict)
    contradictions: list[tuple[str, str, str]] = field(default_factory=list)
    rekey: dict[str, str] = field(default_factory=dict)
    superseded: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    held: dict[str, set[str]] = field(default_factory=dict)


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

    # -- the tally ----------------------------------------------------------------------

    def _tally(
        self, loc: Location, tenant: str | None, exclude: Collection[str] = ()
    ) -> _Tally:
        """Every witness the location shows for each project, with the
        contradictions of design 3.3, the boundary and the token's own
        rules applied."""
        t = _Tally()
        cands: dict[str, set[str]] = defaultdict(set)
        t.held = self._held(loc, exclude)
        for pid in t.held:
            cands[pid].add(H)
        if not t.held and loc.born:
            for pid in self._rewritten(loc, exclude):
                cands[pid].add(HF)
        # R: every remote, and a path remote naming where a checkout was seen.
        if loc.remote_keys:
            for row in self._store.membership_rows(
                "project_remotes", column_in=("remote_key", sorted(loc.remote_keys))
            ):
                pid = self._canon(str(row["project"]))
                if pid not in exclude:
                    cands[pid].add(R)
            local = [
                k[len("local:") :] for k in loc.remote_keys if k.startswith("local:")
            ]
            for path in local:
                for v in self._store.membership_rows(
                    "checkout_views", where={"path": _norm(path)}
                ):
                    seen = self.checkout(str(v["checkout"]))
                    if seen.kind == UNRESOLVED:
                        continue
                    pid = self._canon(seen.project)
                    if pid not in exclude:
                        cands[pid].add(R)
        # D: a declared remote stands in for history in a shallow clone only.
        if loc.shallow:
            for row in self._store.membership_rows(
                "declared_remotes", column_in=("remote_key", sorted(loc.remote_keys))
            ):
                pid = self._canon(str(row["project"]))
                if pid not in exclude and R in cands.get(pid, ()):
                    cands[pid].add(D)
        rf = loc.root_file
        bound_file = self._file_binding(rf.id) if rf is not None else None
        if bound_file is not None and bound_file not in exclude:
            cands[bound_file].add(F)
        carriers: list[Checkout] = []
        if loc.token:
            carriers = self._checkouts(where={"token": loc.token})
            for k in carriers:
                pid = self._canon(k.project)
                if pid not in exclude:
                    cands[pid].add(T)
        # B: this machine only; a checkout of the machine this one was copied
        # from may still be live there, so it joins as a new checkout instead.
        if loc.kind == PRIMARY and loc.born:
            for v in self._store.membership_rows(
                "checkout_views",
                where={"machine": loc.machine, "path": _norm(loc.root or loc.cwd)},
            ):
                k = self.checkout(str(v["checkout"]))
                if k.kind != PRIMARY or k.machine != loc.machine:
                    continue
                if k.object_key == loc.object_key:
                    continue
                pid = self._canon(k.project)
                if pid in exclude or pid not in t.held:
                    continue
                if k.token and loc.token != k.token:
                    continue  # another lineage at the old path: a re-clone
                if self.live_view(k) is None:
                    cands[pid].add(B)
                    t.rekey[pid] = k.id
        # Hb: the file contents of the witness commits, for a project the
        # file or the remote names where the history did not speak.
        named = [p for p, c in cands.items() if ({F, R} & c) and not ({H, HF} & c)]
        if named and loc.born and loc.history is not None:
            index: dict[str, set[str]] = defaultdict(set)
            per_witness: dict[str, dict[str, set[str]]] = {}
            for pid in named:
                per: dict[str, set[str]] = defaultdict(set)
                for row in self._store.membership_rows(
                    "project_blobs", column_in=("project", self._members(pid))
                ):
                    index[str(row["blob"])].add(pid)
                    per[str(row["witness"])].add(str(row["blob"]))
                per_witness[pid] = per
            got = set(loc.history.holds_objects(sorted(index))) if index else set()
            for pid in named:
                for blobs in per_witness[pid].values():
                    held_here = len(blobs & got)
                    if held_here >= 2 and 2 * held_here >= len(blobs):
                        cands[pid].add(HB)
                        break
        # The boundary: nothing joins across it.
        for pid in list(cands):
            if self.project(pid).tenant != tenant:
                t.dropped.append(pid)
                del cands[pid]
        # The token's own rules.
        for pid in list(cands):
            if T not in cands[pid]:
                continue
            mine = [k for k in carriers if self._canon(k.project) == pid]
            live = [
                k
                for k in mine
                if k.machine == loc.machine and self.view_state(k) == LIVE
            ]
            shares = pid in t.held
            others_hold = any(q != pid and (_HISTORY & cands[q]) for q in cands)
            if (
                loc.born
                and not shares
                and not ({HF, HB} & cands[pid])
                and (self._has_witnesses(pid) or others_hold)
            ):
                # History outranks the token: this line has another history.
                cands[pid].discard(T)
                t.superseded.append(pid)
                if not cands[pid]:
                    del cands[pid]
                continue
            if live and not shares:
                where = live[0].newest_view
                t.contradictions.append(
                    (
                        "token_shared_no_commit",
                        pid,
                        f"carries the checkout token of "
                        f"{where.path if where else live[0].id}, a live checkout with "
                        "no commit in common",
                    )
                )
        # The project file's claim against the history.
        if bound_file is not None and bound_file in cands:
            if not (_HISTORY & cands[bound_file]):
                others = [q for q in cands if q != bound_file and ({H, HF} & cands[q])]
                if others:
                    t.contradictions.append(
                        (
                            "file_names_other_history",
                            bound_file,
                            f'its project file names "{self.project(bound_file).name}" '
                            "but it holds the commits of "
                            + ", ".join(self._name(q) for q in others),
                        )
                    )
        t.cands = {p: set(c) for p, c in cands.items()}
        return t

    def _strength(self, loc: Location, project_id: str, codes: Collection[str]) -> int:
        """How many independent witnesses the codes count as (design 2.2):
        H, Hf and Hb are one history witness, Hb counts only beside H, Hf
        or R, F with R counts as two only where history cannot speak, and
        T alone counts as two only for a commit-less caller and project."""
        c = set(codes) - {C}
        if HB in c and not ({H, HF, R} & c):
            c.discard(HB)
        if c & {HF, HB}:
            c = (c - {HF, HB}) | {H}
        n = len(c)
        if {F, R} <= c and H not in c and loc.born and not loc.shallow:
            n -= 1
        if c == {T}:
            n = 2 if (not loc.born and not self._has_witnesses(project_id)) else 1
        return n

    # -- the ladder ----------------------------------------------------------------------

    def resolve(self, loc: Location, *, strict: bool = False) -> Resolution:
        """The ladder of design 3.2 for one location, in one store
        transaction. `strict` is the A3 switch (decision D8,
        ``membership.strict``): a join resting on history without F or B
        asks."""
        with self._call():
            res = self._settle_unresolved(loc, self._resolve(loc, strict))
            rows = tuple(self._rows)
            remint = self._remint is not None
        return dataclasses.replace(res, rows=rows, remint_token=remint)

    def _resolve(self, loc: Location, strict: bool) -> Resolution:
        if loc.state == UNREADABLE:
            self._event(
                "resolution_indeterminate",
                cwd=loc.cwd,
                machine=loc.machine,
                detail=loc.detail,
            )
            return Resolution(
                outcome=INDETERMINATE,
                project=None,
                checkout=None,
                rule="indeterminate",
                lines=(line_unreadable(loc.cwd, loc.detail),),
            )
        enclosing, tenant, enclosing_lines = self._enclosing(loc)
        tenant = self._filed_tenant(loc, tenant)
        if loc.evicted:
            # A known checkout needs neither HEAD nor config: its object
            # decides, as on every prompt. First sight waits for the file.
            k, how = self._by_object(loc)
            if k is not None:
                k = self._touch_view(k, loc)
                return self._caller(
                    k,
                    loc,
                    f"object:{how}:file_evicted",
                    SILENT,
                    enclosing,
                    tenant,
                    enclosing_lines,
                )
            self._event(
                "resolution_deferred",
                cwd=loc.cwd,
                machine=loc.machine,
                detail=loc.detail,
            )
            return Resolution(
                outcome=DEFERRED,
                project=None,
                checkout=None,
                rule="deferred:file_evicted",
                lines=(
                    *enclosing_lines,
                    line_deferred(loc.root or loc.cwd, loc.detail or "HEAD"),
                ),
            )
        if loc.state == OUTSIDE and loc.kind != FOLDER:
            return Resolution(
                outcome=GLOBAL_OUTCOME,
                project=GLOBAL,
                checkout=None,
                rule="outside_repository",
                chain=tuple(enclosing),
                tenant=tenant,
                lines=tuple(enclosing_lines),
            )
        if loc.kind == FOLDER:
            res = self._resolve_folder(loc, enclosing, tenant)
        else:
            res = self._resolve_repository(loc, enclosing, tenant, strict)
        return dataclasses.replace(res, lines=(*enclosing_lines, *res.lines))

    def _enclosing(self, loc: Location) -> tuple[list[str], str | None, list[str]]:
        """The folder projects above the repository (workspaces), each
        bound from its file at first sight, nearest first; the nearest
        boundary among them is the tenant."""
        chain: list[str] = []
        tenant: str | None = None
        lines: list[str] = []
        outer = list(reversed(loc.enclosing_files))  # outermost first
        for depth, f in enumerate(outer):
            pid = self._file_binding(f.id)
            if pid is None:
                p = self._mint_project(
                    f.name or _basename(f.dir),
                    reason="folder_file_first_sight",
                    folder=True,
                    boundary=f.boundary,
                    tenant=tenant,
                    parent=chain[-1] if chain else None,
                )
                self._bind_file(f.id, p.id, evidence=f"first sight of {f.path}")
                pid = p.id
                if f.object_key is not None:
                    # The folder is that project's checkout from here on, so
                    # a session in the folder resolves by its object.
                    folder = Location(
                        machine=loc.machine,
                        cwd=f.dir,
                        state=OUTSIDE,
                        root=f.dir,
                        kind=FOLDER,
                        object_key=f.object_key,
                        files=(f, *reversed(outer[:depth])),
                    )
                    self._register(folder, p.id, "folder_file_first_sight")
                lines.append(
                    f'bettermemory: {f.dir} is a project of its own ("{p.name}", its '
                    "file)" + ("; a boundary: nothing crosses it" if f.boundary else "")
                )
            else:
                lines.extend(self._file_flag(f, pid))
            pid = self._canon(pid)
            chain.append(pid)
            if self.project(pid).boundary:
                tenant = pid
        return list(reversed(chain)), tenant, lines

    def _filed_tenant(self, loc: Location, tenant: str | None) -> str | None:
        """A repository whose root file is bound to a tenant sits inside
        it (B9): its clones are tallied there, so they join it, and nothing
        outside it is a candidate."""
        rf = loc.root_file if loc.kind != FOLDER else None
        if rf is None:
            return tenant
        bound = self._file_binding(rf.id)
        if bound is None or not self.project(bound).boundary:
            return tenant
        return bound

    def _caller(
        self,
        k: Checkout,
        loc: Location,
        rule: str,
        outcome: str,
        enclosing: Sequence[str],
        tenant: str | None,
        lines: Sequence[str] = (),
        *,
        proposal: str | None = None,
        candidates: Sequence[str] = (),
        witnesses: Mapping[str, Collection[str]] | None = None,
    ) -> Resolution:
        """The caller's resolution at checkout `k`: its sub-project where a
        nested file names one, its chain and tenant, and every open
        proposal of the checkout named (which turns SILENT into ASK)."""
        pid = self._canon(k.project)
        own_file: list[str] = []
        rf = loc.root_file
        if rf is not None and self._file_binding(rf.id) == pid:
            own_file = self._file_flag(rf, pid)
        sub, sub_lines = self._subproject(loc, k, pid)
        sub_lines = [*own_file, *sub_lines]
        caller = sub or pid
        own = self.chain_of(caller)
        chain = own + [e for e in enclosing if e not in own]
        for c in chain:
            if self.project(c).boundary:
                tenant = c
                break
        out = [*lines, *sub_lines]
        for pr in self._open_proposals(k.id):
            if pr.line not in out:
                out.append(pr.line)
            if outcome == SILENT:
                outcome = ASK
            proposal = proposal or pr.id
        return Resolution(
            outcome=outcome,
            project=caller,
            checkout=k.id,
            rule=rule,
            proposal=proposal,
            candidates=tuple(candidates),
            witnesses={p: tuple(sorted(c)) for p, c in (witnesses or {}).items()},
            chain=tuple(chain),
            tenant=tenant,
            lines=tuple(out),
        )

    def _resolve_repository(
        self, loc: Location, enclosing: list[str], tenant: str | None, strict: bool
    ) -> Resolution:
        k, how = self._by_object(loc)
        if k is not None:
            k = self._touch_view(k, loc)
            k, lines = self._observe(k, loc, tenant, strict=strict)
            rule = "object" if how == "birth" else f"object+{how}"
            return self._caller(k, loc, rule, SILENT, enclosing, tenant, lines)
        k = self._folder_became_repository(loc)
        if k is not None:
            k, lines = self._observe(k, loc, tenant, strict=strict)
            return self._caller(k, loc, "rekeyed:F+O", SILENT, enclosing, tenant, lines)
        base, rule = self._by_common(loc)
        if base is not None:
            pid = self._canon(base.project)
            k = self._register(loc, pid, rule)
            k, lines = self._observe(k, loc, tenant, strict=strict)
            self._event("resolution_joined", checkout=k.id, project=pid, witnesses=[S])
            return self._caller(
                k,
                loc,
                rule,
                SILENT,
                enclosing,
                tenant,
                lines,
                witnesses={pid: {S}},
            )
        res, superseded = self._first_sight(loc, enclosing, tenant, strict)
        if superseded and res.checkout is not None:
            # The token belongs to another line: this checkout gets its own,
            # so its copies are not taken for that line's (E21 g).
            self._give_token(self.checkout(res.checkout))
        return res

    def _folder_became_repository(self, loc: Location) -> Checkout | None:
        """``git init`` in a folder project (E23 j): the working tree's root
        is the folder checkout's own object (O of the folder) and its file
        names that checkout's project (F); the folder checkout becomes the
        repository's checkout, re-keyed to the git directory."""
        rf = loc.root_file
        key = loc.root_object_key
        if rf is None or key is None or loc.kind != PRIMARY:
            return None
        bound = self._file_binding(rf.id)
        if bound is None:
            return None
        for k in self._checkouts(
            where={
                "machine": loc.machine,
                "object_device": key.device,
                "object_inode": key.inode,
            }
        ):
            if (
                k.kind != FOLDER
                or k.object_key is None
                or k.object_key.compare(key) == DIFFERENT
                or self._canon(k.project) != bound
            ):
                continue
            old = k.object_key
            k = self._put_checkout(
                dataclasses.replace(
                    k,
                    kind=PRIMARY,
                    object_key=loc.object_key,
                    common_object_key=loc.common_object_key,
                    git_dir=loc.git_dir,
                    stamps=loc.stamps,
                    remotes=tuple(sorted(loc.remote_keys)),
                )
            )
            if not _same_number(old, loc.object_key):
                # B20: a re-key row only where the device or the inode changed.
                self._event(
                    "checkout_rekeyed",
                    checkout=k.id,
                    path=loc.root,
                    old_object=dataclasses.asdict(old),
                    new_object=(
                        None
                        if loc.object_key is None
                        else dataclasses.asdict(loc.object_key)
                    ),
                    evidence="git init in a folder project: the folder's own object "
                    "and its project file",
                )
            return k
        return None

    def _first_sight(
        self, loc: Location, enclosing: list[str], tenant: str | None, strict: bool
    ) -> tuple[Resolution, bool]:
        """Rungs 4 to 6 for a checkout seen for the first time. Returns the
        resolution and whether the token it carries was superseded."""
        t = self._tally(loc, tenant)
        cands = t.cands
        lines: list[str] = []
        for pid in t.superseded:
            self._event(
                "token_superseded",
                project=pid,
                path=loc.root,
                evidence="history disagrees",
            )
        superseded = bool(t.superseded) and loc.token is not None
        if t.dropped:
            lines.append(
                f"bettermemory: {loc.root} matches "
                + ", ".join(self._name(p) for p in t.dropped)
                + " across a boundary; kept apart"
            )
        witnesses = {p: sorted(c) for p, c in cands.items()}
        if t.contradictions:
            reason = t.contradictions[0][0]
            pids = sorted(
                {c[1] for c in t.contradictions}
                | {p for p in cands if _HISTORY & cands[p]}
            )
            evidence = "; ".join(c[2] for c in t.contradictions)
            res = self._ask(
                loc, pids, witnesses, reason, evidence, enclosing, tenant, lines
            )
            return res, superseded
        strength = {p: self._strength(loc, p, c) for p, c in cands.items()}
        strong = [p for p, n in strength.items() if n >= 2]
        if strict:
            strong = [
                p
                for p in strong
                if not (_HISTORY & cands[p]) or F in cands[p] or B in cands[p]
            ]
        if len(strong) > 1:
            top = max(strength[p] for p in strong)
            best = [p for p in strong if strength[p] == top]
            if len(best) > 1:
                held = {p: t.held[p] for p in best if p in t.held}
                if len(held) == len(best):
                    best = self._most_specific(loc, held)
            if len(best) == 1:
                strong = best
        if len(strong) == 1:
            return self._join(
                loc, strong[0], t, witnesses, enclosing, tenant, lines
            ), superseded
        if len(strong) > 1:
            evidence = "; ".join(
                f"{self._name(p)}: "
                + ", ".join(WORDS[c] for c in sorted(cands[p]) if c in WORDS)
                for p in sorted(strong)
            )
            res = self._ask(
                loc,
                sorted(strong),
                witnesses,
                "several_projects",
                evidence,
                enclosing,
                tenant,
                lines,
            )
            return res, superseded
        weak = sorted(p for p, n in strength.items() if n >= 1 or (strict and cands[p]))
        content = [] if weak else self._content_candidates(loc, tenant)
        if len(content) > CONTENT_MAX:
            lines.append(
                f"bettermemory: {loc.root} has the folder name and files of "
                f"{len(content)} commit-less projects with no live checkout, more "
                f"than the {CONTENT_MAX} a question names, so none is offered; "
                "`bettermemory project list` shows them, `bettermemory project "
                "join <id>` joins one"
            )
            content = []
        if weak or content:
            pids = weak or content
            for p in content:
                witnesses.setdefault(p, []).append(C)
            evidence = "; ".join(
                f"{self._name(p)}: "
                + ", ".join(WORDS[c] for c in witnesses.get(p, []) if c in WORDS)
                for p in pids
            )
            if len(pids) == 1:
                evidence = ", ".join(
                    WORDS[c] for c in witnesses.get(pids[0], []) if c in WORDS
                )
            reason = "one_witness" if weak else "content_only"
            if strict and any(strength.get(p, 0) >= 2 for p in pids):
                reason = "a3_history_join"
            res = self._ask(
                loc, pids, witnesses, reason, evidence, enclosing, tenant, lines
            )
            return res, superseded
        return self._new(loc, enclosing, tenant, lines), superseded

    def _join(
        self,
        loc: Location,
        pid: str,
        t: _Tally,
        witnesses: Mapping[str, Collection[str]],
        enclosing: list[str],
        tenant: str | None,
        lines: list[str],
    ) -> Resolution:
        """Rung 4: joined silently; with B the recorded checkout is
        re-keyed to this object instead of a new one registered."""
        codes = sorted(set(t.cands[pid]) - {C})
        rule = "join:" + "+".join(codes)
        if B in codes and pid in t.rekey:
            k = self.checkout(t.rekey[pid])
            old = k.object_key
            if _same_number(old, loc.object_key):
                # B20: the same device and inode, confirmed by the others
                # (H): the same object, no re-key row. The key takes the
                # birth time read now, so the view is live by it from here
                # on (`view_state` reads another birth time as replaced).
                k = self._refresh_object(k, loc)
                k, more = self._observe(k, loc, tenant)
                return self._caller(
                    k,
                    loc,
                    "same_object:" + "+".join(codes),
                    SILENT,
                    enclosing,
                    tenant,
                    [*lines, *more],
                    witnesses=witnesses,
                )
            self._event(
                "checkout_rekeyed",
                checkout=k.id,
                path=loc.root,
                old_object=None if old is None else dataclasses.asdict(old),
                new_object=(
                    None
                    if loc.object_key is None
                    else dataclasses.asdict(loc.object_key)
                ),
                evidence="same path, its commits, its own object live nowhere",
            )
            k = self._put_checkout(
                dataclasses.replace(
                    k,
                    object_key=loc.object_key,
                    common_object_key=loc.common_object_key,
                    kind=loc.kind or k.kind,
                    git_dir=loc.git_dir,
                )
            )
            k, more = self._observe(k, loc, tenant)
            return self._caller(
                k,
                loc,
                "rekeyed:" + "+".join(codes),
                SILENT,
                enclosing,
                tenant,
                [*lines, *more],
                witnesses=witnesses,
            )
        k = self._register(loc, pid, rule)
        k, more = self._observe(k, loc, tenant)
        self._event("resolution_joined", checkout=k.id, project=pid, witnesses=codes)
        return self._caller(
            k,
            loc,
            rule,
            SILENT,
            enclosing,
            tenant,
            [*lines, *more],
            witnesses=witnesses,
            candidates=sorted(t.cands),
        )

    def _content_candidates(self, loc: Location, tenant: str | None) -> list[str]:
        """C, for a commit-less checkout with no token: commit-less projects
        of this tenant with no live checkout here, whose recorded content
        has this folder name and these files (or both are empty). A
        checkout at its own path with other init stamps is another
        repository (A2), never a candidate."""
        mine = loc.manifest
        if loc.born or loc.token or mine is None:
            return []
        out: list[str] = []
        for p in self.projects():
            if p.tenant != tenant or self._has_witnesses(p.id):
                continue
            ks = [k for k in self._checkouts_of(p.id) if k.machine == loc.machine]
            if not ks or any(self.view_state(k) == LIVE for k in ks):
                continue
            for k in ks:
                if k.manifest is None:
                    continue
                at_own_path = any(
                    _same_path(v.path, loc.root or loc.cwd) for v in k.views
                )
                if (
                    at_own_path
                    and k.stamps
                    and loc.stamps
                    and not set(k.stamps) & set(loc.stamps)
                ):
                    continue
                score = manifest_match(mine, k.manifest)
                both_empty = (
                    not mine.entries
                    and not k.manifest.entries
                    and mine.name == k.manifest.name
                )
                if score >= CONTENT_MATCH or both_empty:
                    out.append(p.id)
                    break
        return sorted(out)

    def _ask(
        self,
        loc: Location,
        pids: Sequence[str],
        witnesses: Mapping[str, Collection[str]],
        reason: str,
        evidence: str,
        enclosing: list[str],
        tenant: str | None,
        lines: list[str],
    ) -> Resolution:
        """Rung 5: a provisional project of its own for the checkout and
        one durable proposal; nothing of another project is delivered here
        and what is written here stays with it until answered."""
        p = self._mint_project(
            self._display_name(loc), reason="pending", provisional=reason, tenant=tenant
        )
        k = self._register(loc, p.id, f"ask:{reason}")
        k, more = self._observe(k, loc, tenant, first=True)
        rf = loc.root_file
        filed = rf is not None and self._file_binding(rf.id) not in (None, p.id)
        candidates = [self._canon(x) for x in pids]
        named = [(self.project(x).name, x) for x in candidates]
        unwritten = _unwritten(loc)
        pr = self._open_proposal(
            checkout=k.id,
            kind=FIRST_SIGHT,
            candidates=candidates,
            evidence={x: sorted(c) for x, c in witnesses.items()},
            reason=reason,
            line=lambda q: line_first_sight(
                loc.root or loc.cwd,
                named,
                evidence,
                unwritten=unwritten,
                filed=filed,
                question=q,
            ),
            signature=_signature(loc),
        )
        return self._caller(
            k,
            loc,
            f"ask:{reason}",
            ASK,
            enclosing,
            tenant,
            [*lines, *more],
            proposal=pr.id,
            candidates=candidates,
            witnesses=witnesses,
        )

    def _new(
        self, loc: Location, enclosing: list[str], tenant: str | None, lines: list[str]
    ) -> Resolution:
        """Rung 6: a new project, said so, naming any checkout seen at this
        path before and the command that joins it."""
        path = loc.root or loc.cwd
        previous = [
            x
            for x in (
                self.checkout(str(v["checkout"]))
                for v in self._store.membership_rows(
                    "checkout_views",
                    where={"machine": loc.machine, "path": _norm(path)},
                )
            )
            if x.kind != UNRESOLVED
        ]
        p = self._mint_project(self._display_name(loc), reason="new", tenant=tenant)
        k = self._register(loc, p.id, "new")
        k, more = self._observe(k, loc, tenant, first=True)
        line = line_new(
            path,
            p.name,
            p.id,
            previous=[
                (
                    x.id,
                    self.project(self._canon(x.project)).name,
                    self._canon(x.project),
                )
                for x in previous
            ],
            unwritten=None if _unwritten(loc) is None else loc.token_status,
        )
        self._event(
            "resolution_new", checkout=k.id, project=p.id, reused_path=bool(previous)
        )
        return self._caller(
            k,
            loc,
            "new:reused_path" if previous else "new",
            NEW,
            enclosing,
            tenant,
            [*lines, *more, line],
        )

    def _display_name(self, loc: Location) -> str:
        rf = loc.root_file
        if rf is not None and rf.name:
            return rf.name
        return _basename(loc.root or loc.cwd)

    # -- what a sighting adds -------------------------------------------------------------

    def _observe(
        self,
        k: Checkout,
        loc: Location,
        tenant: str | None,
        *,
        first: bool = False,
        strict: bool = False,
    ) -> tuple[Checkout, list[str]]:
        """Every sighting adds its HEAD as a witness, its remotes and its
        root file (design 3.2); a remote replaced on a known checkout while
        the project's other repositories keep the old one asks (L5); a
        root file naming another project asks (L3); an open first-sight
        proposal re-tallies when the evidence signature changed and is
        answered by evidence when two witnesses now agree (L4)."""
        lines: list[str] = []
        pid = self._canon(k.project)
        p = self.project(pid)
        updated = k
        token = loc.token or loc.token_written
        # A superseded token still in place asks for a token of its own
        # again (decision B15); a new token seen here settles it.
        remint = k.token_status == SUPERSEDED and token in (None, k.token)
        if token and token != k.token:
            updated = dataclasses.replace(updated, token=token)
        if (
            not remint
            and loc.token_status != k.token_status
            and loc.token_status != "n/a"
        ):
            updated = dataclasses.replace(updated, token_status=loc.token_status)
        if loc.head and not self._has_witness(pid, loc.head):
            self._add_witness(pid, loc.head, source=f"head@{k.id}", history=loc.history)
        remotes = set(loc.remote_keys)
        last = set(k.remotes)
        if (
            not first
            and remotes
            and last
            and not (remotes & last)
            and not (remotes & self._remotes_of(pid))
        ):
            others = [
                o
                for o in self._checkouts_of(pid)
                if o.id != k.id
                and o.common_object_key != k.common_object_key
                and (set(o.remotes) & last)
            ]
            rf = loc.root_file
            filed = rf is not None and self._file_binding(rf.id) == pid
            if others and not filed and not self._open_proposals(k.id):
                old, new = sorted(last)[0], sorted(remotes)[0]
                self._open_proposal(
                    checkout=k.id,
                    kind=REMOTE_DIVERGED,
                    candidates=[pid],
                    evidence={"old": sorted(last), "new": sorted(remotes)},
                    reason=REMOTE_DIVERGED,
                    line=lambda q: line_remote_diverged(
                        loc.root or loc.cwd, new, p.name, pid, old, question=q
                    ),
                    signature=_signature(loc),
                )
        diverged = any(x.kind == REMOTE_DIVERGED for x in self._open_proposals(k.id))
        if remotes and not diverged:
            self._add_remotes(pid, remotes)
            if tuple(sorted(remotes)) != updated.remotes:
                updated = dataclasses.replace(updated, remotes=tuple(sorted(remotes)))
        if (loc.head, loc.branch) != (updated.last_head, updated.branch):
            updated = dataclasses.replace(
                updated, last_head=loc.head, branch=loc.branch
            )
        rf = loc.root_file
        if rf is not None:
            bound = self._file_binding(rf.id)
            if bound is None:
                if not p.provisional or first:
                    self._bind_file(
                        rf.id,
                        pid,
                        evidence=f"checkout {k.id} at {loc.root}"
                        + (" (tracked at HEAD)" if loc.file_tracked else ""),
                        boundary=rf.boundary,
                    )
            elif bound != pid and not p.provisional:
                if not any(
                    x.kind == FILE_CONTRADICTION for x in self._open_proposals(k.id)
                ):
                    self._open_proposal(
                        checkout=k.id,
                        kind=FILE_CONTRADICTION,
                        candidates=[bound],
                        evidence={"file": rf.id},
                        reason=FILE_CONTRADICTION,
                        line=lambda q: line_file_contradiction(
                            loc.root or loc.cwd,
                            p.name,
                            pid,
                            self.project(bound).name,
                            bound,
                            question=q,
                        ),
                        signature=_signature(loc),
                    )
        if not first:
            for pr in self._open_proposals(k.id):
                if pr.kind == REMOTE_DIVERGED and rf is not None:
                    if self._file_binding(rf.id) == pid:
                        self._close(pr, "join", "project_file")
                        continue
                sig = _signature(loc)
                if pr.kind != FIRST_SIGHT or pr.signature == sig:
                    continue
                self._put_proposal(dataclasses.replace(pr, signature=sig))
                t = self._tally(loc, tenant, exclude={pid})
                strong = [
                    q for q, c in t.cands.items() if self._strength(loc, q, c) >= 2
                ]
                if len(strong) == 1 and not t.contradictions and not strict:
                    codes = t.cands[strong[0]]
                    by = (
                        "project_file"
                        if F in codes
                        else "evidence:" + "+".join(sorted(codes))
                    )
                    self._merge(pid, strong[0], evidence=f"{by} for proposal {pr.id}")
                    self._close(pr, "join:" + strong[0], by)
                    lines.append(
                        line_joined(
                            loc.root or loc.cwd,
                            self.project(strong[0]).name,
                            strong[0],
                            codes,
                        )
                    )
        if (not loc.born and not updated.token) or loc.kind == FOLDER:
            if loc.manifest is not None and loc.manifest != updated.manifest:
                updated = dataclasses.replace(updated, manifest=loc.manifest)
        if updated != k:
            self._put_checkout(updated)
        if remint:
            self._give_token(updated)
            updated = self.checkout(k.id)
        return updated, lines

    # -- nested project files ---------------------------------------------------------------

    def _subproject_line(
        self, file_path: str, bound: str, base: str, parent: str
    ) -> Callable[[str], str]:
        """A part's question's line (L7), built from the question's id."""

        def line(q: str) -> str:
            return line_subproject(
                file_path,
                self.project(bound).name,
                bound,
                self.project(base).name,
                self.project(parent).name,
                question=q,
            )

        return line

    def _nested_answer(self, checkout_id: str, directory: str) -> str | None:
        for row in self._store.membership_rows(
            "proposals",
            where={"checkout": checkout_id, "kind": SUBPROJECT_CONTRADICTION},
        ):
            pr = _proposal_from(row)
            if (
                pr.state == ANSWERED
                and pr.answer is not None
                and pr.answer.startswith("join:")
                and _same_path(str(pr.evidence.get("dir", "")), directory)
            ):
                return pr.answer[len("join:") :]
        return None

    def _subproject(
        self, loc: Location, k: Checkout, pid: str
    ) -> tuple[str | None, list[str]]:
        """A project file inside the repository, between the cwd and the
        root, names a sub-project of the repository's project (D13):
        bound at first sight with the repository's project (or the next
        file up) as its parent. A file bound outside that chain asks (L7)."""
        nested = list(reversed(loc.nested_files))  # outermost first
        if not nested or loc.kind == FOLDER:
            return None, []
        parent = base = pid
        lines: list[str] = []
        for f in nested:
            accepted = self._nested_answer(k.id, f.dir)
            bound = self._file_binding(f.id)
            if (
                accepted is not None
                and bound is not None
                and bound == self._canon(accepted)
            ):
                lines.extend(self._file_flag(f, bound))
                parent = base = bound
                continue
            if bound is None:
                sp = self._mint_project(
                    f.name or _basename(f.dir),
                    reason="nested_file",
                    parent=parent,
                    tenant=self.project(parent).tenant,
                )
                self._bind_file(
                    f.id,
                    sp.id,
                    evidence=f"nested file {f.path} in checkout {k.id}",
                    boundary=f.boundary,
                    checkout=k.id,
                    directory=f.dir,
                )
                parent = sp.id
                continue
            if base in self.chain_of(bound):
                lines.extend(self._file_flag(f, bound))
                parent = bound
                continue
            if not any(
                x.kind == SUBPROJECT_CONTRADICTION for x in self._open_proposals(k.id)
            ):
                self._open_proposal(
                    checkout=k.id,
                    kind=SUBPROJECT_CONTRADICTION,
                    candidates=[bound],
                    evidence={
                        "file": f.id,
                        "dir": f.dir,
                        "path": f.path,
                        "parent": parent,
                        "name": f.name,
                    },
                    reason=SUBPROJECT_CONTRADICTION,
                    line=self._subproject_line(f.path, bound, base, parent),
                    signature=None,
                )
            break
        return (parent if parent != pid else None), lines

    # -- plain folders -----------------------------------------------------------------------

    def _resolve_folder(
        self, loc: Location, enclosing: list[str], tenant: str | None
    ) -> Resolution:
        """A folder project (a plain folder with a project file): O of the
        folder; at first sight F with C counts as two (E23 d, e), F alone
        asks, an unbound file is a new folder project."""
        f = loc.root_file
        if f is None:
            return Resolution(
                outcome=GLOBAL_OUTCOME,
                project=GLOBAL,
                checkout=None,
                rule="outside_repository",
                chain=tuple(enclosing),
                tenant=tenant,
            )
        k, _how = self._by_object(loc)
        if k is not None and k.kind == FOLDER:
            k = self._touch_view(k, loc)
            if loc.manifest is not None and loc.manifest != k.manifest:
                k = self._put_checkout(dataclasses.replace(k, manifest=loc.manifest))
            return self._caller(k, loc, "folder_object", SILENT, enclosing, tenant)
        bound = self._file_binding(f.id)
        if bound is None:
            p = self._mint_project(
                f.name or _basename(loc.root or loc.cwd),
                reason="folder_file_first_sight",
                folder=True,
                tenant=tenant,
                boundary=f.boundary,
                parent=enclosing[0] if enclosing else None,
            )
            self._bind_file(f.id, p.id, evidence=f"first sight of {f.path}")
            k = self._register(loc, p.id, "new_folder")
            self._event(
                "resolution_new", checkout=k.id, project=p.id, reused_path=False
            )
            return self._caller(
                k,
                loc,
                "new_folder",
                NEW,
                enclosing,
                tenant,
                [
                    f"bettermemory: {loc.root} is a new project {_named(p.name, p.id)}, "
                    "its file"
                ],
            )
        if self._declared_folder(f.id, bound, loc):
            # Rung 1: `project init` below a folder project's root made this
            # folder a part (B17, I31); its first sight is its checkout.
            k = self._register(loc, bound, "declared_folder")
            self._event(
                "resolution_joined",
                checkout=k.id,
                project=bound,
                witnesses=[F, "declaration"],
            )
            return self._caller(k, loc, "declared_folder", SILENT, enclosing, tenant)
        codes = {F}
        rekey: Checkout | None = None
        for kk in self._checkouts_of(bound):
            if kk.kind != FOLDER or kk.machine != loc.machine:
                continue
            if manifest_match(loc.manifest, kk.manifest) >= CONTENT_MATCH:
                codes.add(C)
            at_path = any(_same_path(v.path, loc.root or loc.cwd) for v in kk.views)
            if at_path and self.live_view(kk) is None:
                codes.add(B)
                rekey = kk
        bp = self.project(bound)
        if bp.boundary:
            expected = (
                self.project(self._canon(bp.parent)).tenant if bp.parent else None
            )
        else:
            expected = bp.tenant
        if expected != tenant:
            return self._ask(
                loc,
                [bound],
                {bound: codes},
                "boundary",
                "a project of another boundary",
                enclosing,
                tenant,
                [],
            )
        if rekey is not None and _same_number(rekey.object_key, loc.object_key):
            # B20: the folder's own object, its file confirming it: no
            # re-key row and no key change.
            k = self._touch_view(rekey, loc)
            return self._caller(k, loc, "folder_object", SILENT, enclosing, tenant)
        if rekey is not None:
            old = rekey.object_key
            rekey = self._put_checkout(
                dataclasses.replace(rekey, object_key=loc.object_key)
            )
            self._event(
                "checkout_rekeyed",
                checkout=rekey.id,
                path=loc.root,
                old_object=None if old is None else dataclasses.asdict(old),
                new_object=(
                    None
                    if loc.object_key is None
                    else dataclasses.asdict(loc.object_key)
                ),
                evidence="file and same path",
            )
            return self._caller(
                rekey,
                loc,
                "rekeyed:" + "+".join(sorted(codes)),
                SILENT,
                enclosing,
                tenant,
            )
        if len(codes) >= 2:
            rule = "join:" + "+".join(sorted(codes))
            k = self._register(loc, bound, rule)
            self._event(
                "resolution_joined",
                checkout=k.id,
                project=bound,
                witnesses=sorted(codes),
            )
            return self._caller(
                k, loc, rule, SILENT, enclosing, tenant, witnesses={bound: codes}
            )
        return self._ask(
            loc,
            [bound],
            {bound: codes},
            "one_witness",
            WORDS[F] + " and none of its files",
            enclosing,
            tenant,
            [],
        )

    def _declared_folder(self, file_id: str, project_id: str, loc: Location) -> bool:
        """Whether the folder at `loc` is one a declaration made a part of
        a folder project (`project init` below its root, B17): the file
        there was bound by a declaration made in this folder, and the
        project it names has no checkout yet."""
        if self._checkouts_of(project_id):
            return False
        here = _dir_key(loc.root or loc.cwd)
        for row in self._store.membership_log(["project_file_bound"]):
            if file_id not in row.payload:
                continue
            payload = json.loads(row.payload)
            if (
                isinstance(payload, dict)
                and payload.get("file_id") == file_id
                and str(payload.get("evidence") or "").startswith("declaration:")
                and isinstance(payload.get("dir"), str)
                and _dir_key(payload["dir"]) == here
            ):
                return True
        return False

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
