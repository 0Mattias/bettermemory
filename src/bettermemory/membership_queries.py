"""The read side of `membership.py`: homes, surfaces, run-state, health.

`home` and `homes` read records' homes as `relation` reads them,
`admit_recall` and `first_output` serve the prompt recall and a
session's first output (design 3.5), `handoff`, `anchor` and `translate`
are the run-state and anchoring rules of design 3.6, and `retire_quiet`
and `health` the health data of design 8.10.

Each function takes the model as `self: Membership` and reaches its state
(`_store`, `_projects`, `machine`, `_now`) and core methods through it;
the class binds every one by name. This module lives apart from
`membership.py` to keep each module under the package's 2,000-line bound
(ENG-96). It imports the standard library, `time_utils` and the records
module, and `membership` only under TYPE_CHECKING.
"""

from __future__ import annotations

import dataclasses
import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import timedelta
from typing import TYPE_CHECKING

from .membership_records import (
    GLOBAL,
    PRIMARY,
    PROMPT_RECALL,
    RETIRE_AFTER,
    UNRESOLVED,
    UNRESOLVED_STATES,
    Checkout,
    EpisodeRef,
    Handoff,
    Health,
    Home,
    RecallAdmission,
    Resolution,
    UnresolvedProject,
    View,
    admitted,
    relation,
    _LIVE_STATES,
    _inside,
    _rebase,
)
from .time_utils import parse_event_ts

if TYPE_CHECKING:
    from .membership import Membership


def home(self: Membership, record_id: str) -> Home | None:
    """The record's home as `relation` reads it, or None when it has
    none."""
    return self.homes([record_id]).get(record_id)


def homes(self: Membership, record_ids: Sequence[str]) -> dict[str, Home]:
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


# -- surfaces -----------------------------------------------------------------------------------


def admit_recall(
    self: Membership,
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
        hit_homes = self.homes(hits)
        delivered: list[str] = []
        suppressed: list[tuple[str, str]] = []
        for rid in hits:
            hit_home = hit_homes.get(rid)
            if hit_home is None:
                suppressed.append((rid, "no_home"))
                continue
            if admitted(admission, hit_home, caller):
                delivered.append(rid)
            elif hit_home.unresolved:
                suppressed.append((rid, UNRESOLVED))
            else:
                suppressed.append((rid, relation(hit_home, caller)))
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


def first_output(self: Membership, res: Resolution) -> list[str]:
    """The lines a session's first output carries: the resolution's,
    and every proposal still open on its checkout."""
    lines = list(res.lines)
    if res.checkout is not None:
        for pr in self._open_proposals(res.checkout):
            if pr.line not in lines:
                lines.append(pr.line)
    return lines


# -- run-state and anchoring (design 3.6) ----------------------------------------------------


def handoff(
    self: Membership, caller: Resolution, episodes: Sequence[EpisodeRef]
) -> Handoff:
    """The prior session's episodes for the caller's checkout; a primary
    checkout with none of its own is offered the newest session of a
    primary checkout of its project that is not live here, labelled.
    Nothing takes a live sibling's run-state, and another machine's
    checkout is never live from here."""
    self._projects = None
    if caller.checkout is None:
        return Handoff(None, "no_checkout")
    episode_homes = self.homes([e.id for e in episodes])
    own = [
        e
        for e in episodes
        if (h := episode_homes.get(e.id)) and h.checkout == caller.checkout
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
        h = episode_homes.get(e.id)
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
    self: Membership, record_id: str, caller: Resolution | None
) -> tuple[str | None, str]:
    """Where a record's relative claims resolve: its home checkout's
    live view (``home_checkout``, convicting); else the caller's
    checkout of the same project, else any live checkout of the project
    (stand-ins, which confirm by presence and never convict); else
    nowhere, said so."""
    record_home = self.home(record_id)
    if record_home is None or record_home.project == GLOBAL:
        return None, "global_record"
    if record_home.checkout is not None:
        live = self.live_view(self.checkout(record_home.checkout))
        if live is not None:
            return live, "home_checkout"
    if (
        caller is not None
        and caller.checkout is not None
        and caller.project is not None
        and self.canonical(caller.project) == record_home.project
    ):
        live = self.live_view(self.checkout(caller.checkout))
        if live is not None:
            return live, "caller_checkout_same_project"
    for k in sorted(
        self._checkouts_of(record_home.project), key=lambda k: (k.kind != PRIMARY, k.id)
    ):
        live = self.live_view(k)
        if live is not None:
            return live, "project_checkout"
    return None, "no_live_checkout"


def translate(
    self: Membership, path: str, caller: Resolution | None = None
) -> tuple[str, str]:
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


# -- health -------------------------------------------------------------------------------------


def retire_quiet(self: Membership, *, quiet: timedelta = RETIRE_AFTER) -> list[str]:
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


def health(self: Membership) -> Health:
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
            checkout = payload.get("checkout") if isinstance(payload, dict) else None
            suppressed[str(checkout) if checkout else "none"] += 1
        else:
            counts[row.kind] += 1
    checkouts = self._checkouts()
    here = [
        k
        for k in checkouts
        if k.machine == self.machine and k.kind != UNRESOLVED and not k.retired
    ]
    with self.liveness_held():
        # Every view's liveness in one batch, each read bounded (ENG-159).
        self.prefetch_liveness([(k, v) for k in here for v in k.views_on(self.machine)])
        without = sorted(k.id for k in here if self.live_view(k) is None)
    gone = [
        p
        for p in self.projects()
        if p.provisional in UNRESOLVED_STATES and p.merged_into is None
    ]
    # One read of the homes for every such project (ENG-159).
    records = self._records_by_project([p.id for p in gone])
    unresolved: list[UnresolvedProject] = []
    for p in gone:
        k = next(
            (x for x in checkouts if x.kind == UNRESOLVED and x.project == p.id),
            None,
        )
        newest = k.newest_view if k is not None else None
        memories, episodes = records.get(p.id, (0, 0))
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
