"""The recall surfaces' shared pieces: the caller's admission and the row helpers.

`caller_admission` resolves a hook's caller (`_resolve_caller`, over
`location.resolve_hook`) and builds a surface's admission from it
(`admission_for`, over `membership_model`); `membership_lines` and
`standing_line` are the lines a session's first output carries. Both
surfaces' rows and pointers read the helpers here: `summarize`,
`disputed_marks`, the open-contradiction gate, the row's membership
keys, the rc3_admitted shadow column (`_rc3_*`, a prefix the membership
guard exempts) and `_open_store` for the in-process path. `recall`
re-exports every name here a caller imports from it. This module lives
apart from `recall.py` to keep each module under the package's
2,000-line bound (ENG-96); from the family it imports `recall_state`
alone, never `recall`, and `recall` imports it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from . import identity
from . import standing as _standing
from .admission import NO_RESOLUTION, Admission
from .config import Config
from .location import Exchange
from .membership import SEARCH_AUTO_SCOPE, Membership, Resolution
from .models import Memory
from .origin import (
    Origin,
    repos_match,
    should_include_for_caller,
    worktrees_match,
)
from .recall_state import SUMMARY_CHARS
from .store import STORE_FILENAME, Store
from .time_utils import ensure_utc

log = logging.getLogger("bettermemory.recall")


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------


def _rc3_in_project(memory_origin: Origin | None, caller: Origin) -> bool:
    """rc3's rule for whether a memory was written in the caller's
    project, read from the origin columns (rc3's `in_project`). It exists
    only for the rc3_admitted column (`_rc3_admitted`, design 8.11): the
    answer recorded beside membership's, never a delivery. A memory with
    a remote matches a caller with one by `repos_match`, narrowed by
    `worktrees_match`; a caller with no remote matches by worktree root
    alone, and only memories that carry no remote either."""
    if memory_origin is None:
        return False
    if caller.repo is not None:
        if memory_origin.repo is None:
            return False
        if not repos_match(memory_origin.repo, caller.repo):
            return False
        return worktrees_match(memory_origin.worktree_root, caller.worktree_root)
    if caller.worktree_root is None:
        return False
    return (
        memory_origin.repo is None
        and memory_origin.worktree_root == caller.worktree_root
    )


def _rc3_admitted(
    memories: Iterable[Memory], caller: Origin, *, project_only: bool
) -> list[str]:
    """The ids of `memories`, in their order, that rc3's rule would have
    delivered to `caller`, read from the origin columns: the rc3_admitted
    column of the `session_brief` and `prompt_recall` rows (design 8.11).
    It exists only for that column; no surface delivers by it.

    rc3 delivered nothing to a caller git could not read. Under
    `project_only` (the brief, and the recall under ``prompt_recall =
    "project"``) it delivered only inside a repository and only what
    `_rc3_in_project` admits; under ``"everywhere"`` what the search's
    auto-scope admitted (`origin.should_include_for_caller` against the
    caller's remote, every record for a caller with none)."""
    if caller.git_indeterminate:
        return []
    if project_only and caller.repo is None and caller.worktree_root is None:
        return []
    admitted: list[str] = []
    for memory in memories:
        if project_only:
            ok = _rc3_in_project(memory.origin, caller)
        else:
            ok = caller.repo is None or should_include_for_caller(
                memory.origin,
                caller.repo,
                caller_worktree_root=caller.worktree_root,
            )
        if ok:
            admitted.append(memory.id)
    return admitted


def _delivery_membership(
    resolution: Resolution | None, admission: Admission
) -> dict[str, Any]:
    """The membership keys a `session_brief` or `prompt_recall` row carries
    from rs-v5 (design 8.11): ``checkout``, the caller's checkout (None
    where its resolution names none), and ``membership_rule``, the rule its
    resolution named, or the outcome that admitted it nothing (a caller
    nothing could be read for, a deferred read, a resolution that
    raised)."""
    if admission.nothing is not None:
        rule = admission.nothing
    else:
        rule = resolution.rule if resolution is not None else NO_RESOLUTION
    return {
        "checkout": resolution.checkout if resolution is not None else None,
        "membership_rule": rule,
    }


def summarize(body: str, limit: int = SUMMARY_CHARS) -> str:
    """The body, whitespace collapsed, cut to `limit` characters with an
    ellipsis when it was longer."""
    text = " ".join(body.split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def disputed_marks(pairs: Iterable[tuple[str, str, str]]) -> dict[str, str]:
    """``{memory id: mark}`` for every member of a pending conflict pair.

    A memory is disputed when it is either side of any conflicts row
    whose status is ``pending``, whatever the detector. The mark names one
    pair (the lowest id, so the mark is stable) and, when the memory sits
    in several, how many more: ``[disputed: cf-0123456789ab]`` or
    ``[disputed: cf-0123456789ab +2]``, never more than 31 characters.
    Disputed rows are delivered with the mark, not held back: the
    disagreement is itself worth knowing, and the queue is where it is
    settled. A dismissed pair a later scan resurrects is pending again,
    so it marks again."""
    by_member: dict[str, list[str]] = {}
    for a_id, b_id, pair_id in pairs:
        for member in (a_id, b_id):
            listed = by_member.setdefault(member, [])
            if pair_id not in listed:
                listed.append(pair_id)
    out: dict[str, str] = {}
    for member, pair_ids in by_member.items():
        pair_ids.sort()
        more = len(pair_ids) - 1
        suffix = f" +{min(more, 99)}" if more else ""
        out[member] = f"[disputed: {pair_ids[0][:15]}{suffix}]"
    return out


def _unless_standing(
    store: Store, admission: Admission, contradicted: dict[str, datetime]
) -> dict[str, datetime]:
    """`contradicted` less the standing records': a ``contradicted`` use
    is the agent's word, and it does not take the user's rule out of
    delivery (unit RS-F7 part 5). Health still counts the mark."""
    if not contradicted:
        return contradicted
    standing = set(_standing.standing_ids(store, admission, list(contradicted)))
    return {k: v for k, v in contradicted.items() if k not in standing}


def _contradiction_open(memory: Memory, contradicted: Mapping[str, datetime]) -> bool:
    """Whether a `use` event with outcome ``contradicted`` names `memory`
    and no update or verify of it came later. An update bumps `updated`
    and a verify stamps `last_verified_at`, so the later of the two is
    when the record was last answered for (the rule `health` reads for
    its contradicted bucket)."""
    stamp = contradicted.get(memory.id)
    if stamp is None:
        return False
    answered = ensure_utc(memory.updated)
    verified = ensure_utc(memory.last_verified_at)
    if verified is not None and (answered is None or verified > answered):
        answered = verified
    return answered is None or stamp > answered


def _resolve_caller(
    *,
    store: Store,
    cfg: Config,
    cwd: Path | None,
    origin: Origin | None,
    posted: Mapping[str, Any] | None,
    session_id: str | None,
) -> Resolution | Exchange | None:
    """The caller's membership resolution (design 3.2) from what its hook
    posted (`location.resolve_hook`): an `Exchange` when the daemon cannot
    read the caller's namespace and asks its client for the next answer.
    The caller stands at `cwd`, else at the cwd of the `origin` it was
    given, else (in-process) where the request declared
    (`identity.workspace_declaration`) or at the process cwd. None, with
    a warning, when the resolution raised: a hook never fails its turn."""
    from .location import resolve_hook

    where: Path | None = cwd
    if where is None and origin is not None and origin.cwd:
        where = Path(origin.cwd)
    if where is None:
        declared = identity.workspace_declaration()
        if declared is not None:
            where = declared[0]
    try:
        return resolve_hook(
            store,
            cfg,
            None if where is None else str(where),
            posted,
            session=session_id,
        )
    except Exception as exc:  # noqa: BLE001 - reported, never raised at the hook
        log.warning("membership resolution failed: %s: %s", exc.__class__.__name__, exc)
        return None


def membership_model(store: Store) -> Membership:
    """A membership model over `store` for this process's machine, which
    reads the homes and projects a hook's resolution is judged against
    (`location.resolve_hook` keeps its own model), and the recall review's
    silent stratum reads."""
    from .location import NO_MACHINE, process_machine

    try:
        machine = process_machine().id
    except OSError:
        # The caller was resolved without a machine
        # (`location_git.NO_MACHINE`).
        machine = NO_MACHINE
    return Membership(store, machine)


def admission_for(
    surface: str, store: Store, resolution: Resolution | None
) -> Admission:
    """The admission of `surface` (design 3.5) for a hook's caller: one per
    request, built from its resolution. None (the resolution raised) is
    admitted nothing."""
    model = membership_model(store) if resolution is not None else None
    return Admission(surface, resolution, model)


def caller_admission(
    surface: str,
    *,
    store: Store,
    cfg: Config,
    cwd: Path | None,
    origin: Origin | None,
    posted: Mapping[str, Any] | None,
    session_id: str | None,
) -> tuple[Admission, Resolution | Exchange | None]:
    """The caller's resolution (`_resolve_caller`) and the admission of
    `surface` built from it. A namespace the daemon cannot read (an
    `Exchange`) and a resolution that raised are admitted nothing."""
    resolved = _resolve_caller(
        store=store,
        cfg=cfg,
        cwd=cwd,
        origin=origin,
        posted=posted,
        session_id=session_id,
    )
    resolution = resolved if isinstance(resolved, Resolution) else None
    return admission_for(surface, store, resolution), resolved


def membership_lines(store: Store, caller: Resolution | None) -> list[str]:
    """The lines a session's first output carries for its caller
    (`Membership.first_output`: L1 to L10, every open proposal of the
    checkout), or none."""
    if caller is None:
        return []
    return membership_model(store).first_output(caller)


def standing_line(store: Store, caller: Resolution | None) -> str:
    """The first-output line naming how many records the caller's
    auto-scoped search reads wait for a standing answer, and the call that
    shows them (`standing.backlog_line`, unit RS-F7 part 1); "" for
    none, and while the question is not asked (`standing.asking`)."""
    if caller is None or not _standing.asking():
        return ""
    waiting = _standing.backlog(store, admission_for(SEARCH_AUTO_SCOPE, store, caller))
    return _standing.backlog_line(waiting.count)


def _open_store(cfg: Config) -> Store | None:
    """The store the config names, opened for a caller that passes none
    (`hook.prompt_main`, which no command reaches, and the tests; the
    daemon passes its own), or None when there is none (a hook never
    creates one). A store holding records without homes is migrated at
    this open, once, when no daemon serves the hook (design 8.6); the
    migration logs its own lines."""
    from .cli._common import run_migration_at_open

    root = cfg.resolved_directory()
    if not (root / STORE_FILENAME).is_file():
        return None
    store = Store.open_or_create(root / STORE_FILENAME)
    run_migration_at_open(store, cfg)
    return store
