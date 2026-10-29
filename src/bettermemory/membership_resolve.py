"""The ladder of `membership.py` (design 3.2): how a location resolves.

`resolve` runs the rungs for one `Location` in one store transaction:
`_tally` collects each project's witnesses and `_strength` counts them,
`_first_sight` takes a new checkout to `_join`, `_ask` or `_new`,
`_observe` adds what a sighting shows, and `_subproject` and
`_resolve_folder` read nested project files and plain folders.

Each function takes the model as `self: Membership` and reaches its state
(`_store`, `_rows`, `_remint`) and core methods through it; the class
binds every one by name. This module lives apart from `membership.py` to
keep each module under the package's 2,000-line bound (ENG-96). It
imports the standard library, the records, lines and rows modules,
`location` (for `view_path`) and `location_git` (for the bounded read of
a declared folder, `_prefetch_declared`), and `membership` only under
TYPE_CHECKING.
"""

from __future__ import annotations

import dataclasses
import json
from collections import defaultdict
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from .location import view_path
from .location_git import PROBE_BUDGET, bounded_reads
from .membership_lines import (
    line_across_boundary,
    line_content_crowded,
    line_deferred,
    line_file_contradiction,
    line_first_sight,
    line_folder_project,
    line_joined,
    line_new,
    line_new_folder,
    line_remote_diverged,
    line_subproject,
    line_unreadable,
)
from .membership_records import (
    ANSWERED,
    ASK,
    CONTENT_MATCH,
    CONTENT_MAX,
    DEFERRED,
    DIFFERENT,
    FILE_CONTRADICTION,
    FIRST_SIGHT,
    FOLDER,
    GLOBAL,
    GLOBAL_OUTCOME,
    HB,
    HF,
    INDETERMINATE,
    LIVE,
    NEW,
    OUTSIDE,
    PRIMARY,
    REMOTE_DIVERGED,
    SILENT,
    SUBPROJECT_CONTRADICTION,
    SUPERSEDED,
    UNREADABLE,
    UNRESOLVED,
    WALK_LIMIT,
    WORDS,
    B,
    C,
    Checkout,
    D,
    F,
    H,
    Location,
    R,
    Resolution,
    S,
    T,
    View,
    manifest_match,
    _HISTORY,
    _basename,
    _dir_key,
    _live_key,
    _norm,
    _same_number,
    _same_path,
)
from .membership_rows import _Tally, _proposal_from, _signature, _unwritten

if TYPE_CHECKING:
    from .membership import Membership


# -- the tally ----------------------------------------------------------------------


def _tally(
    self: Membership, loc: Location, tenant: str | None, exclude: Collection[str] = ()
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
        local = [k[len("local:") :] for k in loc.remote_keys if k.startswith("local:")]
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
            k for k in mine if k.machine == loc.machine and self.view_state(k) == LIVE
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


def _strength(
    self: Membership, loc: Location, project_id: str, codes: Collection[str]
) -> int:
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


def resolve(self: Membership, loc: Location, *, strict: bool = False) -> Resolution:
    """The ladder of design 3.2 for one location, in one store
    transaction. `strict` is the A3 switch (decision D8,
    ``membership.strict``): a join resting on history without F or B
    asks. The liveness of the recorded views the ladder may read is read
    first, outside the transaction and bounded (`prefetch_liveness`,
    ENG-156), and enters it as values: a view on a hung mount reads
    unreachable without holding the store's write lock. A declared
    folder's stored spelling is read the same way (`_prefetch_declared`)."""
    with self.liveness_held():
        if not self._store.in_batch:
            self.prefetch_liveness(self._liveness_plan(loc))
            _prefetch_declared(self, loc)
        with self._call():
            res = self._settle_unresolved(loc, self._resolve(loc, strict))
            rows = tuple(self._rows)
            remint = self._remint is not None
    return dataclasses.replace(res, rows=rows, remint_token=remint)


def _liveness_plan(self: Membership, loc: Location) -> list[tuple[Checkout, View]]:
    """The recorded views on this machine whose liveness the ladder may
    read for `loc`, read from the tables before its transaction (ENG-156):
    a known checkout's views when `loc` is a new path of it (a view added
    or moved); else the checkouts of its object store (S: git's pointers
    read a live view), those at its path (B), those carrying its token
    (T), those of the project its file names (the folder rungs) and, for a
    commit-less caller, every checkout of a project with no witness (C).
    A view the ladder reads that is not here is read in the transaction,
    bounded the same way; a question the client must answer first (the
    witness exchange) ends the plan where it stands."""
    if loc.state == UNREADABLE:
        return []
    pairs: dict[tuple[str, str], tuple[Checkout, View]] = {}

    def add(ks: Collection[Checkout]) -> None:
        for k in ks:
            if k.kind == UNRESOLVED:
                continue
            for v in k.views_on(self.machine):
                pairs.setdefault((k.id, v.path), (k, v))

    path = loc.root or loc.cwd
    try:
        self._projects = None
        k, _how = self._by_object(loc)
        if k is not None:
            if not any(_same_path(v.path, path) for v in k.views_on(loc.machine)):
                add([k])
            return list(pairs.values())
        common = loc.common_object_key
        if common is not None:
            for column in ("common", "object"):
                add(
                    self._checkouts(
                        where={
                            "machine": loc.machine,
                            f"{column}_device": common.device,
                            f"{column}_inode": common.inode,
                        }
                    )
                )
        rows = self._store.membership_rows(
            "checkout_views", where={"machine": loc.machine, "path": _norm(path)}
        )
        if rows:
            add(self._checkouts(column_in=("id", [r["checkout"] for r in rows])))
        if loc.token:
            add(self._checkouts(where={"token": loc.token}))
        rf = loc.root_file
        bound = self._file_binding(rf.id) if rf is not None else None
        if bound is not None:
            add(self._checkouts_of(bound))
        if not loc.born and not loc.token and loc.manifest is not None:
            witnessed = self._witnessed()
            bare = [p.id for p in self.projects() if p.id not in witnessed]
            for ks in self._checkouts_by_project(bare, machine=loc.machine).values():
                add(ks)
    except Exception:  # noqa: BLE001 - the ladder reads what the plan missed
        pass
    return list(pairs.values())


def prefetch_liveness(self: Membership, pairs: Sequence[tuple[Checkout, View]]) -> None:
    """Read the liveness of `pairs` now, outside any transaction, and keep
    each value for the span `liveness_held` opened (ENG-156): the
    resolution or the migration reads them as values inside its
    transaction. The default liveness reads them in turn on one thread,
    each bounded by `location_git.PROBE_BUDGET` (`location.path_liveness_many`:
    a thread per read cost more than the read); one given to the model is
    read in a pool. A read that raises (a client's view question not yet
    answered) is left to the transaction, where it raises as before."""
    live = self._live
    if live is None:
        return
    todo = list(
        {
            _live_key(k, v): (k, v) for k, v in pairs if _live_key(k, v) not in live
        }.values()
    )
    if not todo:
        return
    got: list[tuple[tuple[object, ...], str | None]]
    if self._liveness_many is not None:
        states = self._liveness_many(todo)
        got = [(_live_key(k, v), s) for (k, v), s in zip(todo, states, strict=True)]
    else:

        def read(pair: tuple[Checkout, View]) -> tuple[tuple[object, ...], str | None]:
            k, v = pair
            try:
                return _live_key(k, v), self._liveness(k, v)
            except Exception:  # noqa: BLE001 - read again in the transaction
                return _live_key(k, v), None

        with ThreadPoolExecutor(
            max_workers=min(_PREFETCH_WORKERS, len(todo)),
            thread_name_prefix="bettermemory-liveness",
        ) as pool:
            got = list(pool.map(read, todo))
    for key, state in got:
        if state is not None:
            live[key] = state


#: Reads of a given liveness `prefetch_liveness` runs at once.
_PREFETCH_WORKERS = 8


def _resolve(self: Membership, loc: Location, strict: bool) -> Resolution:
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


def _enclosing(
    self: Membership, loc: Location
) -> tuple[list[str], str | None, list[str]]:
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
            lines.append(line_folder_project(f.dir, p.name, boundary=f.boundary))
        else:
            lines.extend(self._file_flag(f, pid))
        pid = self._canon(pid)
        chain.append(pid)
        if self.project(pid).boundary:
            tenant = pid
    return list(reversed(chain)), tenant, lines


def _filed_tenant(self: Membership, loc: Location, tenant: str | None) -> str | None:
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
    self: Membership,
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
    self: Membership,
    loc: Location,
    enclosing: list[str],
    tenant: str | None,
    strict: bool,
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


def _folder_became_repository(self: Membership, loc: Location) -> Checkout | None:
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
    self: Membership,
    loc: Location,
    enclosing: list[str],
    tenant: str | None,
    strict: bool,
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
            line_across_boundary(
                loc.root or loc.cwd, [self._name(p) for p in t.dropped]
            )
        )
    witnesses = {p: sorted(c) for p, c in cands.items()}
    if t.contradictions:
        reason = t.contradictions[0][0]
        pids = sorted(
            {c[1] for c in t.contradictions} | {p for p in cands if _HISTORY & cands[p]}
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
    unordered = 0
    if len(strong) > 1:
        top = max(strength[p] for p in strong)
        best = [p for p in strong if strength[p] == top]
        if len(best) > 1:
            held = {p: t.held[p] for p in best if p in t.held}
            if len(held) == len(best):
                best = self._most_specific(loc, held)
                if len(best) > 1 and loc.history is not None:
                    unordered = len(set().union(*held.values()))
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
        if unordered:
            evidence += "; " + _unordered(unordered)
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
            line_content_crowded(loc.root or loc.cwd, len(content), CONTENT_MAX)
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
    self: Membership,
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
                None if loc.object_key is None else dataclasses.asdict(loc.object_key)
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


def _content_candidates(
    self: Membership, loc: Location, tenant: str | None
) -> list[str]:
    """C, for a commit-less checkout with no token: commit-less projects
    of this tenant with no live checkout here, whose recorded content
    has this folder name and these files (or both are empty). A
    checkout at its own path with other init stamps is another
    repository (A2), never a candidate."""
    mine = loc.manifest
    if loc.born or loc.token or mine is None:
        return []
    out: list[str] = []
    # One read of the witnesses and one of the checkouts for every project
    # (ENG-159), where each project read its own, and its members, again.
    witnessed = self._witnessed()
    bare = [p for p in self.projects() if p.tenant == tenant and p.id not in witnessed]
    held = self._checkouts_by_project([p.id for p in bare], machine=loc.machine)
    for p in bare:
        ks = held.get(p.id, [])
        if not ks or any(self.view_state(k) == LIVE for k in ks):
            continue
        for k in ks:
            if k.manifest is None:
                continue
            at_own_path = any(_same_path(v.path, loc.root or loc.cwd) for v in k.views)
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
    self: Membership,
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
    self: Membership,
    loc: Location,
    enclosing: list[str],
    tenant: str | None,
    lines: list[str],
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


def _display_name(self: Membership, loc: Location) -> str:
    rf = loc.root_file
    if rf is not None and rf.name:
        return rf.name
    return _basename(loc.root or loc.cwd)


# -- what a sighting adds -------------------------------------------------------------


def _observe(
    self: Membership,
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
    if not remint and loc.token_status != k.token_status and loc.token_status != "n/a":
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
        updated = dataclasses.replace(updated, last_head=loc.head, branch=loc.branch)
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
            strong = [q for q, c in t.cands.items() if self._strength(loc, q, c) >= 2]
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
    self: Membership, file_path: str, bound: str, base: str, parent: str
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


def _nested_answer(self: Membership, checkout_id: str, directory: str) -> str | None:
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
    self: Membership, loc: Location, k: Checkout, pid: str
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
    self: Membership, loc: Location, enclosing: list[str], tenant: str | None
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
        self._event("resolution_new", checkout=k.id, project=p.id, reused_path=False)
        return self._caller(
            k,
            loc,
            "new_folder",
            NEW,
            enclosing,
            tenant,
            [line_new_folder(loc.root or loc.cwd, p.name, p.id)],
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
        expected = self.project(self._canon(bp.parent)).tenant if bp.parent else None
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
                None if loc.object_key is None else dataclasses.asdict(loc.object_key)
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


def _declared_folder(
    self: Membership, file_id: str, project_id: str, loc: Location
) -> bool:
    """Whether the folder at `loc` is one a declaration made a part of
    a folder project (`project init` below its root, B17): the file
    there was bound by a declaration made in this folder, and the
    project it names has no checkout yet. The declaration's folder is
    compared as stored (from 9.0.0rc14 on, `declare` stores it as a view
    records it: `_location_probe.view_path`, ENG-152), else in the spelling
    `_prefetch_declared` read for it before the transaction: one a release
    before 9.0.0rc14 kept as its cwd was spelled, maybe through a symlink
    or a junction. Nothing here reads the filesystem, so a stored folder
    on a hung mount holds no writer (review M D2); one whose read did not
    answer in time matches nothing."""
    if self._checkouts_of(project_id):
        return False
    here = _dir_key(loc.root or loc.cwd)
    live = self._live
    for folder in _declared_dirs(self, file_id):
        if _dir_key(folder) == here:
            return True
        read = live.get((_DECLARED, folder)) if live is not None else None
        if read is not None and _dir_key(read) == here:
            return True
    return False


#: The key under which `_prefetch_declared` keeps a declared folder's
#: spelling for the span `liveness_held` opened, beside the views'
#: liveness.
_DECLARED = "declared_dir"


def _declared_dirs(self: Membership, file_id: str) -> list[str]:
    """The folders, as stored, of the declarations that bound the project
    file `file_id` (its ``project_file_bound`` rows whose evidence is a
    declaration)."""
    out: list[str] = []
    for row in self._store.membership_log(["project_file_bound"]):
        if file_id not in row.payload:
            continue
        payload = json.loads(row.payload)
        if (
            isinstance(payload, dict)
            and payload.get("file_id") == file_id
            and str(payload.get("evidence") or "").startswith("declaration:")
            and isinstance(payload.get("dir"), str)
        ):
            out.append(payload["dir"])
    return out


def _prefetch_declared(self: Membership, loc: Location) -> None:
    """Read, before the resolution's transaction, each folder a
    declaration bound `loc`'s project file in whose stored spelling is not
    `loc`'s, as a view records it (`_location_probe.view_path`): in turn on one
    thread, each bounded by `PROBE_BUDGET` (`bounded_reads`, ENG-156), and
    kept for the span `liveness_held` opened, where `_declared_folder`
    reads it as a value (review M D2). A folder that does not answer in
    time is not kept, so it matches nothing."""
    live = self._live
    f = loc.root_file
    if live is None or f is None or loc.state == UNREADABLE:
        return
    here = _dir_key(loc.root or loc.cwd)
    try:
        bound = self._file_binding(f.id)
        if bound is None or self._checkouts_of(bound):
            return
        todo = sorted(
            {
                d
                for d in _declared_dirs(self, f.id)
                if _dir_key(d) != here and (_DECLARED, d) not in live
            }
        )
    except Exception:  # noqa: BLE001 - the transaction compares as stored
        return
    if not todo:
        return

    def job(folder: str) -> tuple[str, str, Callable[[], str]]:
        return folder, _DECLARED, lambda: view_path(folder)

    got = bounded_reads([job(d) for d in todo], PROBE_BUDGET)
    for folder, read in zip(todo, got, strict=True):
        if isinstance(read, str):
            live[(_DECLARED, folder)] = read


def _unordered(count: int) -> str:
    """The words a several-projects question adds when the most specific
    line was read and not decided (ENG-151, review M G2): the traversal
    reads `WALK_LIMIT` commits in all, about ``WALK_LIMIT // count`` below
    each of the `count` tied witness commits, so witnesses further apart
    than that are not ordered and the ladder asks."""
    depth = max(1, WALK_LIMIT // max(1, count))
    return (
        f"none of their {count} witness commits descends from another's within "
        f"the {WALK_LIMIT} commits read (about {depth} below each)"
    )
