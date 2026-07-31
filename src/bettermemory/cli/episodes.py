"""`bettermemory episodes` — inspect and prune the episode (journal) store.

Operator surface for the sibling-to-memory primitive added in the
loop story. Mirrors `bettermemory tombstones` in shape: `list` to
inspect, `prune` to clear by TTL. Same `cli_context` plumbing.

Episodes are journal-shaped run-state writes (loop-iteration
takeaways, "what we tried"), excluded from memory_search /
memory_health / memory_list. They auto-prune on each `episode_write`
call inside the running server; this CLI is the offline surface for
inspecting current contents and triggering a manual cleanup pass.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from .._response import isoformat
from ..episodes import DEFAULT_EPISODE_TTL_DAYS, EpisodeStore
from ._common import cli_context


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``episodes`` subparser (with list/prune sub-subparsers)."""
    help_text = (
        "Inspect and prune the episode (journal) store — the "
        "sibling-to-memory tier for loop-iteration takeaways. "
        "Subcommands: list, prune."
    )
    parser = sub.add_parser("episodes", help=help_text, description=help_text)
    ep_sub = parser.add_subparsers(dest="episodes_cmd")

    elist_help = (
        "Print episodes. Without arguments, prints every session's "
        "episodes oldest-first within each session."
    )
    elist_parser = ep_sub.add_parser("list", help=elist_help, description=elist_help)
    elist_parser.add_argument(
        "session_id",
        nargs="?",
        default=None,
        help="If given, only print episodes belonging to this session_id.",
    )
    elist_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )

    eprune_help = (
        "Hard-delete episode session directories whose newest file is "
        f"older than --ttl-days (default {DEFAULT_EPISODE_TTL_DAYS}). "
        "Episodes are normally pruned on each `episode_write` call; "
        "this is the manual surface for an offline cleanup pass."
    )
    eprune_parser = ep_sub.add_parser(
        "prune", help=eprune_help, description=eprune_help
    )
    eprune_parser.add_argument(
        "--ttl-days",
        type=int,
        default=DEFAULT_EPISODE_TTL_DAYS,
        metavar="DAYS",
        help=(
            f"TTL in days. Default {DEFAULT_EPISODE_TTL_DAYS}. Session "
            "directories whose newest file mtime is older than this are "
            "deleted in full."
        ),
    )
    eprune_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without touching disk.",
    )
    eprune_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )

    return parser


def run(
    args: argparse.Namespace,
    *,
    sub_parser: argparse.ArgumentParser,
) -> None:
    """Dispatch handler for ``bettermemory episodes``."""
    if args.episodes_cmd == "list":
        _cli_episodes_list(
            session_id=args.session_id,
            json_out=args.json,
        )
        return
    if args.episodes_cmd == "prune":
        _cli_episodes_prune(
            ttl_days=args.ttl_days,
            dry_run=args.dry_run,
            json_out=args.json,
        )
        return
    sub_parser.print_help()


def _cli_episodes_list(*, session_id: str | None, json_out: bool) -> None:
    """`bettermemory episodes list [session_id]` — print episodes."""
    import json as _json

    ctx = cli_context()
    ep_store = EpisodeStore(ctx.directory)

    rows: list[dict[str, Any]] = []
    if session_id is not None:
        for ep in ep_store.list_by_session(session_id):
            rows.append(_episode_row(ep))
    else:
        for sid in sorted(ep_store.iter_session_ids()):
            for ep in ep_store.list_by_session(sid):
                rows.append(_episode_row(ep))

    if json_out:
        sys.stdout.write(_json.dumps(rows, indent=2) + "\n")
        return

    if not rows:
        if session_id is not None:
            sys.stdout.write(f"No episodes for session {session_id!r}.\n")
        else:
            sys.stdout.write("No episodes.\n")
        return

    sys.stdout.write(f"Episodes ({len(rows)}):\n")
    for row in rows:
        takeaway = row["takeaway"] or "<no takeaway>"
        scopes = ",".join(row["scopes"]) or "<no scopes>"
        sys.stdout.write(
            f"  {row['id']} [session={row['session_id']}, "
            f"created={row['created']}] {scopes}\n"
            f"    {takeaway}\n"
        )


def _cli_episodes_prune(*, ttl_days: int, dry_run: bool, json_out: bool) -> None:
    """`bettermemory episodes prune` — hard-delete old session directories."""
    import json as _json

    ctx = cli_context()
    ep_store = EpisodeStore(ctx.directory)

    if dry_run:
        # `prunable_session_ids` is the shared predicate — read-only (it
        # stats, it never deletes). It is what this block used to
        # transcribe for itself, under a comment asking the next reader
        # to keep the two aligned. `memory_health`'s
        # `episode_volume.prunable_sessions` reports the same number but
        # reaches it inline inside `volume()`'s single directory pass
        # rather than by calling here — a parity test is what holds those
        # two together, not a shared call.
        # The store-side docstring records the two guards that matter
        # here: `ttl_days <= 0` is a no-op (a non-positive TTL never
        # means "delete everything" — without that guard the dry-run
        # would list every session while a real prune deletes nothing,
        # i.e. the dry-run would lie), and a directory holding no regular
        # file is collectable.
        candidates = ep_store.prunable_session_ids(ttl_days=ttl_days)

        if json_out:
            sys.stdout.write(
                _json.dumps(
                    {"would_delete": candidates, "ttl_days": ttl_days},
                    indent=2,
                )
                + "\n"
            )
            return
        if not candidates:
            sys.stdout.write(f"No episode sessions older than {ttl_days} days.\n")
            return
        sys.stdout.write(
            f"Would delete {len(candidates)} session director"
            f"{'y' if len(candidates) == 1 else 'ies'} older than {ttl_days} days:\n"
        )
        for sid in candidates:
            sys.stdout.write(f"  {sid}\n")
        sys.stdout.write("(Dry run — re-run without --dry-run to apply.)\n")
        return

    pruned = ep_store.prune_old_sessions(ttl_days=ttl_days)
    if json_out:
        sys.stdout.write(
            _json.dumps({"deleted": pruned, "ttl_days": ttl_days}, indent=2) + "\n"
        )
        return
    if not pruned:
        sys.stdout.write(f"No episode sessions older than {ttl_days} days.\n")
        return
    sys.stdout.write(
        f"Deleted {len(pruned)} episode session director"
        f"{'y' if len(pruned) == 1 else 'ies'}:\n"
    )
    for sid in pruned:
        sys.stdout.write(f"  {sid}\n")


def _episode_row(ep: Any) -> dict[str, Any]:
    """Serialise one Episode for the list outputs."""
    return {
        "id": ep.id,
        "session_id": ep.session_id,
        "created": isoformat(ep.created),
        "takeaway": ep.takeaway,
        "scopes": list(ep.scopes),
    }
