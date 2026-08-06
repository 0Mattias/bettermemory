"""`bettermemory session-start` — Claude Code SessionStart-hook context block.

Prints a three-line "what is stored for this repository" hint on stdout.
Claude Code injects a SessionStart hook's stdout verbatim into the
model's context, so the model opens every session already knowing the
per-scope counts it would otherwise have spent a `memory_scope_overview`
tool call to learn — or, far more often, never learned at all because
retrieval is opt-in and, between prompts the recall hook's high bar
doesn't fire on (the ~98% common case), nothing prompted it.

THE NEGATIVE MANDATE — this command constructs no `Recorder` and calls
no `.record()`, ever. Two independent failures follow from breaking it:

1. Anchor hijack. `hook._latest_in_process_session` picks the most
   recent session in the event log whose `triggered_from` is not in
   `hook._OUT_OF_PROCESS_TRIGGERS` — it never reads `attribution`. A
   session-start row
   would therefore become the anchor the Stop hook's turn audit
   attributes against, no matter how the row is stamped (this command
   has no roster entry to hide behind — the recall hook records, but
   it records AS a roster member). The familiar
   "just mark it `cli_*` like consolidate does" workaround fixes
   doctor's census and does NOT fix this.
2. Phantom sessions. Every session open would publish a fresh session id
   into the log with no turn that could ever produce `turn_audited`,
   which is the exact denominator corruption `ADMIN_RECORDED_EVENT_KINDS`
   was introduced to stop.

`tests/test_cli_smoke.py::test_session_start_records_nothing` is the
standing guard: it asserts a byte-identical event log across a run.
Without that test this docstring is only a wish.

The read is deliberately cheap, and every step of it is a gate rather
than a fallback:

* `load_config().resolved_directory()` instead of `cli_context()` —
  `Store.__post_init__` mkdirs, chmods, and can kick off a full index
  rebuild. None of that belongs in the critical path of opening a
  session.
* Counts come from the FTS index's columnar scan
  (`index.scope_counts`), never from `Store.load_all()`. `load_all` is
  ~74 % of the equivalent handler's cost, all of it per-file opens and
  YAML parsing for bodies this surface does not print.
* Anything unusable — no store, empty store, absent/corrupt index, an
  index whose row count disagrees with what is on disk, or one whose
  rows name a different SET of files than the listing does (equal
  counts are the weaker claim: swap one memory for another out of band
  and both sides still read N) — degrades to EMPTY stdout, never to the
  expensive path. A session-start hook that stalls the session is worse
  than one that says nothing.

THE STANDING TIER (`[behavior] standing_tier`, default OFF) is the one
deliberate exception to the bodies-stay-on-disk rule above, and it is
paid only by users who opted in. Opt-in retrieval cannot serve
knowledge whose trigger condition is not knowing you need it, so when
the flag is on, the hint carries the caller-scoped `ambient` memories
whose staleness verdict computes ``fresh`` — bodies, not pointers,
because a pointer still requires the model to know to dereference it.
The discipline that protects the surface:

* Verification is the admission ticket. The verdict is computed by the
  SAME chain a `memory_show` runs — `compute_verification_status` +
  `detect_path_drift` (claim-anchored subset) + `compute_commit_drift`
  — no relaxed session-start variant. Anything not ``fresh`` is never
  delivered; it collapses into one aggregate "N standing memories are
  stale — verify to restore delivery" line, which converts the tier's
  verification debt into visible pressure to pay it.
* Hard byte budget (`_STANDING_BUDGET_BYTES`), whole-memory truncation
  only. Entries go newest-verified first; a body that does not fit is
  counted in the "…and K more" overflow, never split — a truncated
  fact is a different fact. A body larger than the entire budget is
  skipped (it can never fit) so it cannot head-of-line-block smaller
  memories behind it.
* The candidate read stays index-first: `index.category_rows` names
  the ambient files, and only THOSE are parsed — the flag does not buy
  a `load_all`. The parse re-checks category and admission against the
  parsed truth, because the index-trust gates establish file identity,
  not file content.
* The negative mandate is untouched: delivery records nothing, so
  adoption is unmeasured in v1 by decision (both instrumentation
  shapes considered would have re-corrupted the cadence census the
  mandate exists to protect; the ROADMAP entry records them).
* A failure anywhere in the standing computation degrades to a stderr
  note and the base hint ships without the section — the proven half
  of the block never rides on the new half.

Diagnostics go to stderr (Claude Code routes it to the debug log, not
the model's context) and the process always exits 0. `hooks.json` still
appends `|| true` as belt-and-suspenders, because a `uvx` network
failure or an older published wheel without this subcommand would
otherwise surface as a hook-error banner.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Type-only so the runtime import cost stays where it belongs: inside
    # `run`. `cli/__init__.py` imports this module for every `bettermemory
    # <anything>` invocation, so a top-level `from ..origin import Origin`
    # would tax every other subcommand for a symbol only this one uses.
    from collections.abc import Callable
    from pathlib import Path

    from ..config import Config
    from ..models import Memory
    from ..origin import Origin

# How many ids `_indexed_filenames` hands to one `filenames_for_ids`
# call. That helper binds every id it is given as a SQL parameter
# (`WHERE id IN (?,?,…)`), and SQLite refuses a statement carrying more
# parameters than SQLITE_LIMIT_VARIABLE_NUMBER — measured at 32766 on
# this machine's sqlite 3.50.4, but 999 on any build compiled before
# SQLite 3.32, which a 3.11 interpreter on an older distro can still be
# linked against. Over the limit the read raises
# `sqlite3.OperationalError: too many SQL variables`, which `run` would
# absorb into a stderr note — i.e. the hint would quietly stop appearing
# for exactly the largest stores, the ones with most to say. Batching
# below the lowest of those ceilings keeps the gate answerable at any
# store size; a store under 900 memories still pays one query, the same
# as before.
_ID_BATCH = 900

# How many scopes the context block names before collapsing the tail into
# "+N more". The block is injected into EVERY session, so its cost is
# paid per session forever; five is enough to characterise a store's
# shape and short enough that a 40-scope store cannot flood the opening
# context. The total is always exact — only the enumeration is capped.
_MAX_SCOPES_SHOWN = 5

# The standing tier's whole-section byte ceiling, measured over the
# rendered memory entries (UTF-8). The framing lines around them —
# header, overflow count, stale aggregate — are small bounded constants
# and deliberately outside the budget: the stale line is the tier's
# verification-pressure mechanism and must appear even when the budget
# is spent. ~1 KB is the ROADMAP-settled figure: the SessionStart block
# already carries the scope counts, Claude Code truncates oversized
# injected blocks wholesale, and a tier that grows past a few short
# bodies has stopped being "standing context" and become an unread wall.
_STANDING_BUDGET_BYTES = 1024


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``session-start`` subparser on the parent parser."""
    help_text = (
        "Print the session-start memory hint on stdout. Intended as a "
        "Claude Code SessionStart hook target: emits the per-scope "
        "counts for the current repository so a fresh session starts "
        "with them in context instead of spending a "
        "`memory_scope_overview` call to get them (or never learning "
        "them at all). Reads the search index, never the memory bodies "
        "— unless `[behavior] standing_tier` is on, which additionally "
        "delivers the fresh-verified ambient memories for this "
        "repository (whole bodies, newest-verified first, ~1 KB budget) "
        "and names the stale remainder in one aggregate line. Records "
        "NOTHING either way — no event, no session. Prints nothing "
        "when the store is empty or the index cannot be trusted, and "
        "always exits 0 so a hook misfire never breaks session start."
    )
    parser = sub.add_parser("session-start", help=help_text, description=help_text)
    return parser


def run(args: argparse.Namespace) -> None:
    """Dispatch handler for ``bettermemory session-start``.

    Always exits 0. The broad `except Exception` is the point: this runs
    on the session-open path of every conversation, so ANY unanticipated
    failure — an unreadable config, a permissions change under the store,
    a sqlite build without FTS5 — must degrade to a quiet stderr note
    rather than a hook-error banner in the user's face.

    TWO guarded arms, not one, because the WRITE can fail on its own and
    an unguarded one made the paragraph above false: under
    `PYTHONIOENCODING=ascii` the block's em dash raised
    `UnicodeEncodeError` out of `print` and the command exited 1 with a
    traceback. A closed pipe and a full disk are the same shape. The arms
    stay separate so each degrade says which half broke — "the read found
    nothing usable" and "the answer could not be delivered" are different
    problems for whoever reads the debug log.
    """
    import sys

    # `args` is unused: the subcommand takes no flags. It stays in the
    # signature because `cli/__init__.py`'s dispatch calls every `run`
    # the same way.
    try:
        block = _build_context_block()
    except Exception as exc:  # noqa: BLE001
        _note_degraded(f"skipped: {exc.__class__.__name__}: {exc}")
        raise SystemExit(0) from None

    if block:
        try:
            # Stdout is the context block and NOTHING else — Claude Code
            # feeds it to the model verbatim, so a stray progress line
            # would be indistinguishable from content the model should
            # act on.
            print(block)
            # Flushed HERE, while the guard is still up. A `print` alone
            # only fills the buffer, so an OS-level write failure (closed
            # pipe, full disk) would surface in the interpreter's
            # shutdown flush instead — past every handler, where CPython
            # prints "Exception ignored on flushing sys.stdout" and exits
            # **120** whatever `SystemExit` said. This line is what makes
            # such a failure catchable at all; `_blackhole_stdout` is
            # what then stops the shutdown retry from exiting 120 anyway.
            sys.stdout.flush()
        except Exception as exc:  # noqa: BLE001
            _note_degraded(
                f"could not write the context block: {exc.__class__.__name__}: {exc}"
            )
            _blackhole_stdout()
            raise SystemExit(0) from None
    raise SystemExit(0)


def _note_degraded(detail: str) -> None:
    """Report a degrade on stderr — and swallow any failure to do so.

    The note is a courtesy to the debug log; the exit code is the
    contract. Whatever broke stdout has usually broken stderr too (one
    `PYTHONIOENCODING`, one full disk, one closed terminal), and a
    diagnostic that raised on its way out would turn a clean degrade
    back into the hook-error banner this whole module exists to avoid.
    """
    import sys

    try:
        print(f"[bettermemory] session-start {detail}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def _blackhole_stdout() -> None:
    """Point stdout's descriptor at /dev/null after a failed write.

    A flush that fails leaves the data in the buffer, so CPython's
    shutdown flush retries it, fails again, and exits 120 — overruling
    the caller's `SystemExit(0)`. Catching the flush error is therefore
    not enough on its own; redirecting the underlying descriptor (the
    recipe from the `signal` docs' SIGPIPE note) is what makes the retry
    a silent no-op. Measured down a closed pipe: catch-only still exits
    120, catch-plus-redirect exits 0.

    Every failure here is swallowed and none is reported: `sys.stdout`
    may have no real descriptor at all (a test harness's capture object,
    an embedding host), and a salvage that raised would recreate exactly
    the failure it exists to prevent. Reached only on the write-failure
    path, one statement before the process exits, so redirecting a
    descriptor out from under the rest of the program is not a hazard
    here the way it would be in a library.
    """
    import os
    import sys

    try:
        fd = sys.stdout.fileno()
        devnull = os.open(os.devnull, os.O_WRONLY)
    except Exception:  # noqa: BLE001
        return
    try:
        os.dup2(devnull, fd)
    except OSError:
        pass
    finally:
        os.close(devnull)


def _indexed_filenames(directory: Path) -> set[str] | None:
    """The set of on-disk filenames the index's rows name, or `None`
    when at least one row cannot be resolved to a filename.

    Built from two public index reads — `indexed_ids` for the row keys,
    `filenames_for_ids` for the `filename` column — rather than a raw
    SELECT, so this surface cannot drift from the schema the rest of the
    package reads through. `filenames_for_ids` drops rows whose filename
    column is empty (a pre-v2 row), so a short result is the signal that
    the comparison is not answerable; the caller declines on `None`
    instead of comparing against a set it knows is incomplete.

    The ids go in `_ID_BATCH`-sized chunks because `filenames_for_ids`
    binds each one as a SQL parameter and SQLite caps how many a single
    statement may carry — see that constant. `indexed_ids` needs no such
    care: with `ids=None` it is an unparameterised full-column scan.

    Errors propagate. `run`'s guard turns any of them into a quiet
    stderr note, which is the right degradation for a hint."""
    from .. import index as _index

    ids = sorted(_index.indexed_ids(directory))
    resolved: dict[str, str] = {}
    for start in range(0, len(ids), _ID_BATCH):
        resolved.update(
            _index.filenames_for_ids(directory, ids[start : start + _ID_BATCH])
        )
    if len(resolved) != len(ids):
        return None
    return set(resolved.values())


def _build_context_block() -> str | None:
    """Return the block to print, or None to stay silent.

    Split from `run` so the gates are testable without capturing stdout,
    and so `run` holds nothing but the never-raise / always-exit-0
    contract.
    """
    import sys

    from .. import index as _index
    from ..config import load_config
    from ..origin import capture as capture_origin
    from ..search import candidate_admitted
    from ..store import active_memory_filenames

    config = load_config()
    directory = config.resolved_directory()
    if not directory.exists():
        # First run, or a store the user has not created yet. Not a
        # problem to report — there is simply nothing to say.
        return None

    # Cheapest possible "is there anything here at all" probe: a bare
    # directory listing, no parsing. Also the disk side of the
    # index-trust comparison below, so it is read once and used twice —
    # as a count first, then as the filename SET, which is the stronger
    # of the two claims and costs the same listing.
    disk_files = active_memory_filenames(directory)
    disk_count = len(disk_files)
    if disk_count == 0:
        return None

    status = _index.status(directory)
    # Mirrors `_handlers.load_search_candidates`' index-trust gate:
    # absent, corrupt, or `needs_rebuild` (a schema migration dropped the
    # data tables and only incrementally-touched memories are back) all
    # mean the index cannot be believed. Deliberately WITHOUT that gate's
    # `indexed_count < resolve_index_threshold()` arm — that threshold is
    # a search-performance tradeoff, not a correctness signal, and a
    # 40-memory store's counts are exactly as true as a 4000-memory
    # store's.
    if not status.get("exists") or status.get("corrupt") or status.get("needs_rebuild"):
        print(
            "[bettermemory] session-start: search index unusable "
            f"({_index.index_path(directory)}) — skipping the hint. "
            "`bettermemory reindex` rebuilds it.",
            file=sys.stderr,
        )
        return None
    indexed_count = int(status.get("indexed_count", 0) or 0)
    if indexed_count != disk_count:
        # The index is a derived cache and this surface publishes a
        # COUNT, so "close enough" is not available: a number the model
        # then contradicts with `memory_search` is worse than no number.
        # Note this also declines on a store holding unparseable `.md`
        # files (they can never enter the index, so the two counts can
        # never agree) — deliberately, because proving that requires
        # parsing every file, which is the exact cost this command
        # exists to avoid. `bettermemory doctor`'s index-health check
        # already reports that case, and pays the parse to do it.
        print(
            f"[bettermemory] session-start: index holds {indexed_count} row(s) "
            f"but {disk_count} memory file(s) are on disk — skipping the "
            "hint rather than publishing a count that may be wrong. "
            "`bettermemory reindex` reconciles them.",
            file=sys.stderr,
        )
        return None

    # Equal counts are not the claim this surface needs. Remove one
    # memory and add another out of band — the workflow the store's own
    # one-file-per-memory design invites — and both sides still read N
    # while the index describes a store that no longer exists; the scope
    # table below would then be computed from the departed memory's
    # scopes. The index carries each row's on-disk filename, so the set
    # comparison needs no parse and no extra directory walk: it reuses
    # the listing already taken above.
    #
    # TWO declines, not one, because they rest on different evidence and
    # a single message would have to overclaim for one of them. `None`
    # says a row could not be resolved to a filename at all (the column
    # is empty, as on a pre-v2 row), so NOTHING was compared: reporting
    # "a different set of files" there would assert a comparison this
    # never performed — the exact overclaiming this command's gates
    # exist to refuse. A mismatch is the stronger, evidenced case, and
    # it gets the stronger sentence.
    indexed_files = _indexed_filenames(directory)
    if indexed_files is None:
        print(
            "[bettermemory] session-start: at least one of the index's "
            f"{indexed_count} row(s) does not record which file it came "
            "from, so its file set could not be compared against the "
            f"{disk_count} on disk — skipping the hint rather than "
            "publishing a scope table on a check that could not run. "
            "`bettermemory reindex` restamps the rows.",
            file=sys.stderr,
        )
        return None
    if indexed_files != disk_files:
        print(
            f"[bettermemory] session-start: the index's {indexed_count} row(s) "
            f"name a different set of files than the {disk_count} on disk — "
            "skipping the hint rather than publishing a scope table built "
            "from memories that are no longer there. "
            "`bettermemory reindex` reconciles them.",
            file=sys.stderr,
        )
        return None

    # Same auto-scope filter `memory_search` and `memory_scope_overview`
    # apply, via the same predicate, so the number the model sees here
    # cannot differ from those surfaces' by disagreeing about ADMISSION.
    # What the two gates above establish is narrower than that and worth
    # stating exactly: the index holds one row per file on disk, named
    # for that file. They do not establish that each row's stored scopes
    # still match its file's — a hand-edit leaves the id, the filename
    # and the count untouched, and only a parse of every file catches
    # it. That parse is the cost this command exists to avoid, so it
    # stays `bettermemory doctor`'s job (its index-health check
    # reconciles the content and pays the parse to do it).
    # No scope exclusions: session-disabled scopes live in
    # `SessionState`, and no session exists yet when this runs.
    current = capture_origin()

    def _admit(scopes: list[str], memory_origin: "Origin | None") -> bool:
        return candidate_admitted(
            scopes,
            memory_origin,
            scope_filter=None,
            excluded=set(),
            repo_filter=current.repo,
            worktree_filter=current.worktree_root,
        )

    resolved = _index.scope_counts(directory, admit=_admit)
    if resolved is None:
        print(
            "[bettermemory] session-start: could not read scope counts from "
            "the index — skipping the hint.",
            file=sys.stderr,
        )
        return None
    total, scopes = resolved
    if total == 0:
        # A populated store with nothing in scope for this repository.
        # Saying "0 memories" would be true and useless; staying silent
        # costs the model nothing, because opt-in retrieval is already
        # the default it operates under.
        return None

    print(
        f"[bettermemory] session-start: {total} in scope out of {disk_count} "
        f"stored in {directory} (repo={current.repo!r}).",
        file=sys.stderr,
    )

    standing: str | None = None
    if config.behavior.standing_tier:
        # Guarded on its own, narrower than `run`'s catch-all: the scope
        # table above is the proven half of this surface, and the
        # standing computation — file parses, stat calls, git
        # subprocesses — must never take it down. On failure the base
        # hint ships without the section and stderr says why.
        try:
            standing = _standing_section(directory, config, current, _admit)
        except Exception as exc:  # noqa: BLE001
            print(
                "[bettermemory] session-start: standing tier skipped: "
                f"{exc.__class__.__name__}: {exc}",
                file=sys.stderr,
            )
    return _render_block(total, scopes, standing=standing)


def _standing_section(
    directory: Path,
    config: "Config",
    current: "Origin",
    admit: "Callable[[list[str], Origin | None], bool]",
) -> str | None:
    """The standing tier's lines, or None when there is nothing to say.

    Candidates come from `index.category_rows` — the index names which
    files hold caller-admitted ambient memories, and only those files
    are parsed. The parse then RE-checks category and admission against
    the parsed truth: the caller's index-trust gates establish that the
    index's rows name exactly the files on disk, not that each file's
    content still matches its row (a hand-edit that flips a category or
    a scope leaves id, filename and count intact). For a count that
    residue is acceptable; for delivering a body it is not.

    Per admitted memory, the staleness verdict is computed by the same
    chain `handlers/show.memory_show` runs — calendar leg, claim-anchored
    path drift, commit drift against this caller's checkout — minus that
    handler's `.record()`, which the negative mandate forbids here. The
    commit leg is the expensive one (a git subprocess per anchored
    memory); it stays because it is the leg that lets a calendar-stale
    memory prove itself still fresh AND the one that catches a
    calendar-fresh memory whose repo moved on — an admission ticket
    checked at the gate, not a cached stamp.

    A row that fails to parse, no longer matches its id, or no longer
    reads ambient/admitted is skipped silently — the same shape
    `Store.load_all` gives an unparseable file, and `bettermemory
    doctor` is the surface that reports it.
    """
    from .. import index as _index
    from ..models import Category, utcnow
    from ..store import _parse_memory_file
    from ..verify import (
        compute_commit_drift,
        compute_staleness_verdict,
        compute_verification_status,
        detect_path_drift,
    )

    rows = _index.category_rows(directory, category=Category.AMBIENT.value, admit=admit)
    if not rows:
        return None

    now = utcnow()
    fresh: list[Memory] = []
    stale = 0
    for memory_id, filename in rows:
        if not filename:
            # Pre-v2 row with no recorded filename. The caller's
            # filename-set gate declines the whole hint for such stores,
            # so this arm is defensive against racing an out-of-band
            # reindex mid-run.
            continue
        try:
            memory = _parse_memory_file(directory / filename)
        except (OSError, ValueError):
            continue
        if (
            memory.id != memory_id
            or memory.category is not Category.AMBIENT
            or not admit(memory.scopes, memory.origin)
        ):
            continue
        drift = detect_path_drift(
            memory.body,
            verified_paths=memory.verified_paths,
            absent_paths=memory.verified_absent_paths,
            worktree_root=memory.origin.worktree_root if memory.origin else None,
        )
        verification = compute_verification_status(
            memory.last_verified_at,
            now=now,
            stale_after_days=config.behavior.verification_stale_days,
        )
        commit_drift = compute_commit_drift(
            memory.last_verified_at,
            memory.origin.repo if memory.origin else None,
            caller_origin=current,
            verified_paths=memory.verified_paths,
            body=memory.body,
            claims=memory.claims,
        )
        verdict = compute_staleness_verdict(
            verification=verification,
            # Claim-anchored subset only — same rule as every other
            # verdict site. See `verdict_from_signals`.
            path_drift_missing=len(drift.claim_anchored_missing),
            commit_drift_count=(
                commit_drift.commits_since_verify if commit_drift is not None else None
            ),
        )
        if verdict == "fresh":
            fresh.append(memory)
        else:
            stale += 1

    if not fresh and stale == 0:
        return None
    return _render_standing(fresh, stale)


def _render_standing(fresh: "list[Memory]", stale_count: int) -> str:
    """Render the standing lines: entries under budget, then pressure.

    Delivery order is newest-verified first (`last_verified_at`
    descending; every fresh verdict implies the field is set — the
    ladder pins ``status == "never"`` to ``spot_check_required`` — but
    the `or m.created` fallback keeps the sort total rather than
    trusting that invariant with a TypeError). Ties break on id,
    descending, so two same-second verifies order deterministically.

    The budget walk implements the ROADMAP's two distinct non-fit
    cases: an entry larger than the WHOLE budget can never be delivered,
    so it is skipped and the walk continues — otherwise one oversized
    body would permanently starve everything verified before it — while
    an entry that merely exceeds the REMAINING budget stops the walk,
    because delivering an older body after declining a newer one would
    invert the priority order the sort just established. Both cases,
    and everything behind a stop, land in the same "…and K more" count:
    undelivered is undelivered, and the model's remedy for all of them
    is the same `memory_list` call.

    The stale aggregate renders even when nothing else does — it is the
    tier's pressure mechanism, not decoration — but when NOTHING was
    admitted at all the caller returns None before reaching here, so an
    ambient-free store adds no lines.
    """
    fresh.sort(key=lambda m: ((m.last_verified_at or m.created), m.id), reverse=True)
    delivered: list[str] = []
    remaining = _STANDING_BUDGET_BYTES
    overflow = 0
    stopped = False
    for memory in fresh:
        if stopped:
            overflow += 1
            continue
        entry = f"- {memory.id} ({', '.join(memory.scopes)}): {memory.body.strip()}"
        size = len(entry.encode("utf-8"))
        if size > _STANDING_BUDGET_BYTES:
            # Whole-budget oversize: skipped, never trimmed — a
            # truncated fact is a different fact — and never a blocker
            # for the smaller bodies behind it.
            overflow += 1
            continue
        if size > remaining:
            overflow += 1
            stopped = True
            continue
        delivered.append(entry)
        remaining -= size

    lines: list[str] = []
    if delivered:
        lines.append(
            "Standing memories (ambient, verified fresh at delivery — "
            "bodies below are already in context, no retrieval needed):"
        )
        lines.extend(delivered)
        if overflow:
            noun = "memory" if overflow == 1 else "memories"
            lines.append(
                f"…and {overflow} more fresh standing {noun} over the "
                "delivery budget (memory_list)."
            )
    elif overflow:
        noun = "memory" if overflow == 1 else "memories"
        lines.append(
            f"{overflow} fresh standing {noun} exceeded the delivery "
            "budget entirely (memory_list)."
        )
    if stale_count:
        noun = "memory is" if stale_count == 1 else "memories are"
        lines.append(
            f"{stale_count} standing {noun} stale — verify to restore "
            "delivery (memory_search, then memory_verify)."
        )
    return "\n".join(lines)


def _render_block(
    total: int, scopes: dict[str, int], standing: str | None = None
) -> str:
    """Format the context block the model actually sees.

    Ordering is count-descending then name-ascending — the same
    determinism `memory_scope_overview` sorts by, so a model that sees
    both surfaces in one session cannot find them disagreeing about
    which scope is "top".

    The wording earns its length twice over: it states what the numbers
    are NOT (no bodies, no ids), so the model doesn't treat this as
    retrieval already performed, and it restates the opt-in rule, so a
    non-zero count doesn't read as an invitation to search.

    With a standing section present, the closing disclaimer swaps to a
    wording that carves the delivered bodies out of the "no bodies"
    claim — the two halves of the block must not contradict each other
    about what is in context. Without one (`standing=None`, the flag-off
    default), the returned text is byte-identical to what this surface
    printed before the tier existed;
    `test_standing_tier_off_keeps_block_byte_identical` holds that pin.
    """
    ordered = sorted(scopes.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = ordered[:_MAX_SCOPES_SHOWN]
    rendered = ", ".join(f"{name} ({count})" for name, count in shown)
    remaining = len(ordered) - len(shown)
    if remaining > 0:
        rendered += f", +{remaining} more"
    noun = "memory is" if total == 1 else "memories are"
    head = (
        f"bettermemory: {total} {noun} in scope for this repository.\n"
        f"Top scopes: {rendered}.\n"
    )
    if standing is None:
        return head + (
            "Per-scope counts only — no bodies, no ids. This is the cheap half "
            "of memory_scope_overview; call that tool when you also need the "
            "curation / proposals rollups. Retrieval stays opt-in: reach for "
            "memory_search when a request leans on shared context or is "
            "ambiguous, not for self-contained questions."
        )
    return (
        head
        + standing
        + "\n"
        + "Beyond the standing section above, per-scope counts only — no "
        "other bodies or ids are in context. This is the cheap half "
        "of memory_scope_overview; call that tool when you also need the "
        "curation / proposals rollups. Retrieval stays opt-in: reach for "
        "memory_search when a request leans on shared context or is "
        "ambiguous, not for self-contained questions."
    )
