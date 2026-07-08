"""`bettermemory proposals` — review the write-reflex proposal queue."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ..models import first_summary_line
from ..proposals import ProposalQueue
from ._common import cli_context


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``proposals`` subparser (with list/accept/dismiss)."""
    parser = sub.add_parser(
        "proposals",
        help=(
            "Review the write-reflex proposal queue — durable statements the "
            "Stop hook captured but never saved (opt-in [proposals] "
            "auto_propose). The CLI counterpart of the memory_proposals tool. "
            "Subcommands: list, accept, dismiss."
        ),
    )
    proposals_sub = parser.add_subparsers(dest="proposals_cmd")

    plist_parser = proposals_sub.add_parser("list", help="Print every queued proposal.")
    plist_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )

    paccept_parser = proposals_sub.add_parser(
        "accept",
        help=(
            "Write a queued proposal as a real memory and remove it from the "
            "queue. Requires --scope (a memory needs at least one scope)."
        ),
    )
    paccept_parser.add_argument(
        "id", metavar="ID", help="Id of the proposal to accept."
    )
    paccept_parser.add_argument(
        "--scope",
        action="append",
        default=[],
        metavar="SCOPE",
        help="Scope to tag the new memory with. Repeat for multiple; required.",
    )
    paccept_parser.add_argument(
        "--category",
        default=None,
        help=(
            "Override the proposal's suggested category "
            "(fact / user-inference / ambient)."
        ),
    )
    paccept_parser.add_argument(
        "--acknowledge-credential",
        action="store_true",
        help=(
            "Accept even though the body contains a secret-shaped token — "
            "only for a documented public/example credential, never a live "
            "secret (the store is plain-text and `sync` pushes it across "
            "hosts). Mirrors the acknowledge_credential escape hatch on the "
            "memory_write / memory_update / memory_proposals MCP tools. The "
            "forced override is recorded in the audit log (detector kind "
            "only, never the value)."
        ),
    )
    paccept_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )

    pdismiss_parser = proposals_sub.add_parser(
        "dismiss",
        help="Drop a proposal from the queue without writing it.",
    )
    pdismiss_parser.add_argument(
        "id", metavar="ID", help="Id of the proposal to dismiss."
    )
    pdismiss_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    return parser


def run(
    args: argparse.Namespace,
    *,
    root_parser: argparse.ArgumentParser,
    sub_parser: argparse.ArgumentParser,
) -> None:
    """Dispatch handler for ``bettermemory proposals``.

    ``root_parser`` is forwarded into the accept handler so a missing
    ``--scope`` or a bad scope/category surfaces through ``parser.error(...)``
    with the root prog prefix; ``sub_parser`` is used for ``.print_help()``
    when a bare ``bettermemory proposals`` is invoked.
    """
    if args.proposals_cmd == "list":
        _cli_proposals_list(json_out=args.json)
        return
    if args.proposals_cmd == "accept":
        _cli_proposals_accept(
            proposal_id=args.id,
            scopes=args.scope or None,
            category=args.category,
            acknowledge_credential=args.acknowledge_credential,
            json_out=args.json,
            parser=root_parser,
        )
        return
    if args.proposals_cmd == "dismiss":
        _cli_proposals_dismiss(proposal_id=args.id, json_out=args.json)
        return
    sub_parser.print_help()


def _cli_proposals_list(*, json_out: bool) -> None:
    """`bettermemory proposals list` — print the queue."""
    import json as _json

    ctx = cli_context()
    proposals = ProposalQueue(ctx.store.root).load()

    if json_out:
        sys.stdout.write(_json.dumps([p.to_dict() for p in proposals], indent=2) + "\n")
        return
    if not proposals:
        sys.stdout.write("No proposals queued.\n")
        return
    sys.stdout.write(f"Proposals ({len(proposals)}):\n")
    for p in proposals:
        sys.stdout.write(
            f"  {p.id} [{p.suggested_category}, created={p.created}]\n"
            f"    {first_summary_line(p.body)}\n"
        )


def _cli_proposals_accept(
    *,
    proposal_id: str,
    scopes: list[str] | None,
    category: str | None,
    acknowledge_credential: bool,
    json_out: bool,
    parser: Any,
) -> None:
    """`bettermemory proposals accept ID --scope …` — write a proposal as memory.

    Shares the `accept_proposal` core with the memory_proposals MCP tool, so
    the write-policy + atomic-claim contract is identical across both entry
    points — including the credential gate and its `--acknowledge-credential`
    escape hatch (the CLI spelling of the MCP tools' acknowledge_credential
    flag). A missing --scope or a bad scope/category surfaces through
    `parser.error` (clean exit 2); the `parser is None` fallback re-raises so
    programmatic callers still see the exception.

    The Recorder is constructed the same way the sibling CLI write paths
    (`ingest`, `consolidate`) build theirs, so the accept event — and above
    all a forced credential override (detector kind only, never the value) —
    lands in the SAME audit log the MCP server writes. `accept_proposal`
    records it at its single choke point; this function records nothing
    itself.
    """
    import json as _json

    from ..events import Recorder
    from ..handlers.proposals import accept_proposal
    from ..session import SessionState

    if not scopes:
        if parser is not None:
            parser.error(
                "--scope is required to accept a proposal (a memory needs at "
                "least one scope)"
            )
        raise ValueError("scopes is required to accept a proposal")

    ctx = cli_context()
    recorder = Recorder(
        root=ctx.store.root,
        session_id=SessionState().session_id,
        enabled=ctx.config.telemetry.enabled,
        max_bytes=ctx.config.telemetry.max_bytes,
        log_queries_verbatim=ctx.config.telemetry.log_queries_verbatim,
    )
    try:
        result = accept_proposal(
            store=ctx.store,
            config=ctx.config,
            recorder=recorder,
            proposal_id=proposal_id,
            scopes=scopes,
            category=category,
            acknowledge_credential=acknowledge_credential,
        )
    except ValueError as exc:
        # Bad scope/category — the proposal is still queued; the caller can
        # fix the inputs and retry.
        if parser is not None:
            parser.error(str(exc))
        raise
    except OSError as exc:
        # A disk-level failure. Surface a clean `bettermemory: error: …` +
        # exit 2 instead of a path-leaking traceback, matching the sibling
        # `tombstones restore` / `rename-scope` commands. The OSError can
        # come from EITHER the atomic queue claim (queue.remove rewrites the
        # queue file) OR the durable store.write after it, so do NOT assert
        # the entry is definitively gone — tell the user to re-check first.
        if parser is not None:
            parser.error(
                f"failed to accept proposal {proposal_id}: {exc} "
                "(it may have been removed from the queue — re-check with "
                "`bettermemory proposals list` before retrying)"
            )
        raise

    if json_out:
        sys.stdout.write(_json.dumps(result, indent=2) + "\n")
        return
    if result["status"] == "not_found":
        sys.stdout.write(f"No proposal with id {proposal_id}.\n")
        return
    sys.stdout.write(
        f"Accepted {proposal_id} -> memory {result['id']} "
        f"[{','.join(result['scopes'])}] ({result['category']}).\n"
    )


def _cli_proposals_dismiss(*, proposal_id: str, json_out: bool) -> None:
    """`bettermemory proposals dismiss ID` — drop a proposal unwritten."""
    import json as _json

    ctx = cli_context()
    removed = ProposalQueue(ctx.store.root).remove(proposal_id)

    if json_out:
        status = "dismissed" if removed is not None else "not_found"
        sys.stdout.write(
            _json.dumps({"status": status, "proposal_id": proposal_id}, indent=2) + "\n"
        )
        return
    if removed is None:
        sys.stdout.write(f"No proposal with id {proposal_id}.\n")
        return
    sys.stdout.write(f"Dismissed {proposal_id}.\n")
