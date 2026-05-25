"""`bettermemory sync` — git-based sync for the memory directory."""

from __future__ import annotations

import argparse
import sys

from ._common import cli_context


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``sync`` subparser (with init/status/push/pull/auto)."""
    parser = sub.add_parser(
        "sync",
        help=(
            "Sync the memory directory across hosts via git. Subcommands: "
            "init (set up the dir as a git repo + sensible .gitignore), "
            "status (show pending changes and remote tracking), "
            "push (commit + push), pull (rebase-pull + rebuild the index), "
            "auto (pull then push — the shell-alias / cron one-shot)."
        ),
    )
    sync_sub = parser.add_subparsers(dest="sync_cmd")

    sync_init_parser = sync_sub.add_parser(
        "init", help="Initialise the memory dir as a git repo."
    )
    sync_init_parser.add_argument(
        "--remote",
        type=str,
        default=None,
        metavar="URL",
        help=(
            "Add (or update) `origin` to this remote URL. Without the "
            "flag, init only creates the repo + .gitignore — you can "
            "set the remote later with `git remote add origin <url>`."
        ),
    )
    sync_init_parser.add_argument(
        "--default-branch",
        type=str,
        default="main",
        help='Initial branch name. Default: "main".',
    )
    sync_init_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )

    sync_status_parser = sync_sub.add_parser(
        "status", help="Show pending changes and remote tracking."
    )
    sync_status_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )

    sync_push_parser = sync_sub.add_parser(
        "push", help="Stage everything, commit (if changes), push."
    )
    sync_push_parser.add_argument(
        "--remote",
        type=str,
        default="origin",
        help='Remote name. Default: "origin".',
    )
    sync_push_parser.add_argument(
        "--message",
        "-m",
        type=str,
        default=None,
        help=(
            "Commit message. Default: `bettermemory: sync`. Override "
            "when scripting a sync after a known set of edits."
        ),
    )
    sync_push_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )

    sync_pull_parser = sync_sub.add_parser(
        "pull", help="Rebase-pull + rebuild the FTS5 index."
    )
    sync_pull_parser.add_argument(
        "--remote",
        type=str,
        default="origin",
        help='Remote name. Default: "origin".',
    )
    sync_pull_parser.add_argument(
        "--no-reindex",
        action="store_true",
        help=(
            "Skip the post-pull `reindex`. Useful in scripts that batch "
            "multiple sync operations and want to defer index rebuild "
            "to the end."
        ),
    )
    sync_pull_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )

    sync_auto_parser = sync_sub.add_parser(
        "auto", help="Pull-rebase, then push. The shell-alias one-shot."
    )
    sync_auto_parser.add_argument(
        "--remote",
        type=str,
        default="origin",
        help='Remote name. Default: "origin".',
    )
    sync_auto_parser.add_argument(
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
    """Dispatch handler for ``bettermemory sync``.

    ``sub_parser`` is used for ``.print_help()`` when a bare
    ``bettermemory sync`` is invoked without a sub-sub-command.
    """
    if args.sync_cmd == "init":
        _cli_sync_init(
            remote=args.remote,
            default_branch=args.default_branch,
            json_out=args.json,
        )
        return
    if args.sync_cmd == "status":
        _cli_sync_status(json_out=args.json)
        return
    if args.sync_cmd == "push":
        _cli_sync_push(
            remote=args.remote,
            message=args.message,
            json_out=args.json,
        )
        return
    if args.sync_cmd == "pull":
        _cli_sync_pull(
            remote=args.remote,
            reindex=not args.no_reindex,
            json_out=args.json,
        )
        return
    if args.sync_cmd == "auto":
        _cli_sync_auto(remote=args.remote, json_out=args.json)
        return
    sub_parser.print_help()


def _cli_sync_init(*, remote: str | None, default_branch: str, json_out: bool) -> None:
    """`bettermemory sync init` — set up the memory dir as a git repo."""
    import json as _json

    from .. import sync as _sync

    ctx = cli_context()
    directory = ctx.directory

    try:
        result = _sync.init(directory, remote=remote, default_branch=default_branch)
    except _sync.SyncError as exc:
        sys.stderr.write(f"sync init failed: {exc}\n")
        raise SystemExit(2) from exc

    if json_out:
        sys.stdout.write(_json.dumps(result, indent=2) + "\n")
        return

    sys.stdout.write(f"Initialised sync in {result['root']}.\n")
    actions = result.get("actions", []) or []
    if isinstance(actions, list):
        for action in actions:
            sys.stdout.write(f"  - {action}\n")


def _cli_sync_status(*, json_out: bool) -> None:
    """`bettermemory sync status` — show pending changes + remote
    tracking."""
    import json as _json

    from .. import sync as _sync

    ctx = cli_context()
    directory = ctx.directory
    st = _sync.status(directory)

    if json_out:
        sys.stdout.write(_json.dumps(st.to_dict(), indent=2) + "\n")
        return

    if not st.is_repo:
        sys.stdout.write(
            f"{directory} is not a git repo. Run `bettermemory sync init` "
            "to set up sync.\n"
        )
        return

    sys.stdout.write(f"Memory directory: {directory}\n")
    sys.stdout.write(f"  branch: {st.branch or '<detached>'}\n")
    sys.stdout.write(f"  remote: {st.remote_url or '<none>'}\n")
    if st.remote_url:
        sys.stdout.write(f"  ahead: {st.ahead}  behind: {st.behind}\n")
    sys.stdout.write(
        f"  untracked: {len(st.untracked)}  modified: {len(st.modified)}\n"
    )
    if st.modified:
        sys.stdout.write("  modified files:\n")
        for path in st.modified[:10]:
            sys.stdout.write(f"    {path}\n")
        if len(st.modified) > 10:
            sys.stdout.write(f"    ... and {len(st.modified) - 10} more\n")


def _cli_sync_push(*, remote: str, message: str | None, json_out: bool) -> None:
    """`bettermemory sync push` — stage, commit, push."""
    import json as _json

    from .. import sync as _sync

    ctx = cli_context()
    directory = ctx.directory
    eff_message = message or _sync.DEFAULT_COMMIT_MESSAGE
    try:
        result = _sync.push(directory, remote=remote, message=eff_message)
    except _sync.SyncError as exc:
        sys.stderr.write(f"sync push failed: {exc}\n")
        raise SystemExit(2) from exc

    if json_out:
        sys.stdout.write(_json.dumps(result, indent=2) + "\n")
        return

    if result["committed"]:
        sys.stdout.write(f"Committed and pushed to {remote}.\n")
    else:
        sys.stdout.write(
            f"No local changes to commit; pushed prior commits to {remote}.\n"
        )


def _cli_sync_pull(*, remote: str, reindex: bool, json_out: bool) -> None:
    """`bettermemory sync pull` — rebase-pull + rebuild index."""
    import json as _json

    from .. import sync as _sync

    ctx = cli_context()
    directory = ctx.directory
    try:
        result = _sync.pull(directory, remote=remote, reindex=reindex)
    except _sync.SyncError as exc:
        sys.stderr.write(f"sync pull failed: {exc}\n")
        raise SystemExit(2) from exc

    if json_out:
        sys.stdout.write(_json.dumps(result, indent=2) + "\n")
        return

    sys.stdout.write(f"Pulled from {remote}.\n")
    if reindex:
        sys.stdout.write(f"  reindexed {result.get('indexed_count', 0)} memories\n")
    else:
        sys.stdout.write(
            "  --no-reindex passed: run `bettermemory reindex` when ready\n"
        )


def _cli_sync_auto(*, remote: str, json_out: bool) -> None:
    """`bettermemory sync auto` — pull then push, one-shot."""
    import json as _json

    from .. import sync as _sync

    ctx = cli_context()
    directory = ctx.directory
    try:
        result = _sync.auto(directory, remote=remote)
    except _sync.SyncError as exc:
        sys.stderr.write(f"sync auto failed: {exc}\n")
        raise SystemExit(2) from exc

    if json_out:
        sys.stdout.write(_json.dumps(result, indent=2) + "\n")
        return

    sys.stdout.write(f"Auto-sync complete (remote={remote}).\n")
