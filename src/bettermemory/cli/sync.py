"""`bettermemory sync` — git-based sync for the memory directory."""

from __future__ import annotations

import argparse
import sys

from ._common import cli_context, cli_recorder


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``sync`` subparser (init/status/push/pull/auto/quarantine)."""
    help_text = (
        "Sync the memory directory across hosts via git. Subcommands: "
        "init (set up the dir as a git repo + sensible .gitignore), "
        "status (show pending changes and remote tracking), "
        "push (commit + push), pull (rebase-pull, admission, then rebuild "
        "the index), auto (commit, pull-rebase, then push — the shell-alias "
        "/ cron one-shot), quarantine (list the pulled files admission "
        "refused, or release one)."
    )
    # `description=` gets the longer form: it is what `sync --help` prints,
    # while `help=` is the one-liner in the top-level subcommand table.
    # The refusal is spelled out here because it is the behaviour a user
    # hits first on a store left mid-conflict, and an unexplained error is
    # what sends people reaching for `push` — the one command that would
    # commit the markers.
    description = help_text + (
        " push, pull and auto all refuse to run while a merge, rebase, "
        "cherry-pick, revert or stash pop has left unresolved conflicts in "
        "the memory directory. push and auto stage with `git add -A`, which "
        "would commit the conflict markers into your memories; pull refuses "
        "so that it never sends you to push on that state. Resolve the files "
        "by hand and finish the operation first."
    )
    parser = sub.add_parser("sync", help=help_text, description=description)
    sync_sub = parser.add_subparsers(dest="sync_cmd")

    sync_init_help = "Initialise the memory dir as a git repo."
    sync_init_parser = sync_sub.add_parser(
        "init", help=sync_init_help, description=sync_init_help
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

    sync_status_help = "Show pending changes and remote tracking."
    sync_status_parser = sync_sub.add_parser(
        "status", help=sync_status_help, description=sync_status_help
    )
    sync_status_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )

    sync_push_help = "Stage everything, commit (if changes), push."
    sync_push_parser = sync_sub.add_parser(
        "push",
        help=sync_push_help,
        description=(
            sync_push_help + " Refuses while any file has unresolved merge "
            "conflicts: staging runs `git add -A`, which would mark them "
            "resolved without resolving them and push the `<<<<<<<` markers "
            "to every clone."
        ),
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

    sync_pull_help = "Rebase-pull + rebuild the FTS5 index."
    sync_pull_parser = sync_sub.add_parser(
        "pull",
        help=sync_pull_help,
        description=(
            sync_pull_help + " Refuses, naming the files, when tracked "
            "memories have uncommitted edits — `git pull --rebase` will not "
            "run against a dirty worktree. Setting `rebase.autoStash` in git "
            "config lifts that: git stashes and restores the edits around "
            "the rebase, and this command stops pre-checking. Unresolved "
            "merge conflicts are refused either way."
        ),
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

    sync_auto_help = (
        "Commit local edits, pull-rebase, then push. The shell-alias one-shot."
    )
    sync_auto_parser = sync_sub.add_parser(
        "auto",
        help=sync_auto_help,
        description=(
            sync_auto_help + " The commit comes FIRST because a live store is "
            "normally dirty when you reach for this, and `git pull --rebase` "
            "will not run against a dirty worktree unless `rebase.autoStash` "
            "is set; the push step staged and committed everything anyway, so "
            "only the order changed. Refuses before committing anything if "
            "any file has unresolved merge conflicts."
        ),
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

    sync_quarantine_help = (
        "List the pulled files the admission chain refused, or release one."
    )
    sync_quarantine_parser = sync_sub.add_parser(
        "quarantine",
        help=sync_quarantine_help,
        description=(
            sync_quarantine_help + " A pull judges every memory file it brings "
            "down (size cap, parser, id alias, credential gate) and quarantines "
            "a refusal: the file stays on disk under git, and this host's store "
            "skips it. Every later pull judges the quarantined files again and "
            "releases one that passes. `--release NAME` runs the same chain by "
            "hand; `--force` admits a credential refusal as it is. Structural "
            "refusals (oversize, unparseable, id alias) cannot be forced."
        ),
    )
    sync_quarantine_parser.add_argument(
        "--release",
        type=str,
        default=None,
        metavar="NAME",
        help="Admit this quarantined file (its filename in the memory directory).",
    )
    sync_quarantine_parser.add_argument(
        "--force",
        action="store_true",
        help="With --release: admit the file even if the credential gate still refuses it.",
    )
    sync_quarantine_parser.add_argument(
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
    if args.sync_cmd == "quarantine":
        _cli_sync_quarantine(release=args.release, force=args.force, json_out=args.json)
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
    if st.quarantined:
        sys.stdout.write(
            f"  quarantined: {st.quarantined} (pulled files the admission "
            "chain refused; `bettermemory sync quarantine` lists them)\n"
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
        result = _sync.pull(
            directory,
            remote=remote,
            reindex=reindex,
            recorder=cli_recorder(ctx, attribution="cli_sync_pull"),
            config=ctx.config,
        )
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
    _write_admission_lines(result)


def _write_admission_lines(result: dict[str, object]) -> None:
    """The admission outcome of one pull, one line per file. Quarantined
    files are named with their reason because the user's only other
    signals are `sync status` and doctor; flagged files are named so a
    transient or user-claim body that was admitted is not mistaken for
    one that was checked."""
    quarantined = result.get("quarantined")
    if isinstance(quarantined, list):
        for entry in quarantined:
            if isinstance(entry, dict):
                detail = f": {entry['detail']}" if entry.get("detail") else ""
                sys.stdout.write(
                    f"  quarantined {entry.get('file')} ({entry.get('reason')}{detail})\n"
                )
        if quarantined:
            sys.stdout.write(
                "  quarantined files stay on disk and out of the store; "
                "`bettermemory sync quarantine` lists them\n"
            )
    flagged = result.get("flagged")
    if isinstance(flagged, list):
        for entry in flagged:
            if isinstance(entry, dict):
                gates = entry.get("gates")
                joined = ", ".join(gates) if isinstance(gates, list) else str(gates)
                sys.stdout.write(
                    f"  flagged {entry.get('file')} ({joined}); admitted\n"
                )
    released = result.get("released")
    if isinstance(released, list):
        for name in released:
            sys.stdout.write(f"  released {name} from quarantine\n")


def _cli_sync_auto(*, remote: str, json_out: bool) -> None:
    """`bettermemory sync auto` — commit, pull, then push, one-shot."""
    import json as _json

    from .. import sync as _sync

    ctx = cli_context()
    directory = ctx.directory
    try:
        result = _sync.auto(
            directory,
            remote=remote,
            recorder=cli_recorder(ctx, attribution="cli_sync_auto"),
            config=ctx.config,
        )
    except _sync.SyncError as exc:
        sys.stderr.write(f"sync auto failed: {exc}\n")
        raise SystemExit(2) from exc

    if json_out:
        sys.stdout.write(_json.dumps(result, indent=2) + "\n")
        return

    sys.stdout.write(f"Auto-sync complete (remote={remote}).\n")
    pull_result = result.get("pull")
    if isinstance(pull_result, dict):
        _write_admission_lines(pull_result)


def _cli_sync_quarantine(*, release: str | None, force: bool, json_out: bool) -> None:
    """`bettermemory sync quarantine` — list the refused files, or
    release one through the same admission chain a pull runs."""
    import json as _json

    from .. import sync as _sync

    ctx = cli_context()
    directory = ctx.directory
    if release is None:
        entries = _sync.quarantine_entries(directory)
        if json_out:
            payload = [{"file": e.filename, **e.to_dict()} for e in entries]
            sys.stdout.write(_json.dumps(payload, indent=2) + "\n")
            return
        if not entries:
            sys.stdout.write("No quarantined files.\n")
            return
        noun = "file" if len(entries) == 1 else "files"
        sys.stdout.write(f"{len(entries)} quarantined {noun} in {directory}:\n")
        for entry in entries:
            detail = f": {entry.detail}" if entry.detail else ""
            sys.stdout.write(
                f"  {entry.filename}  {entry.reason}{detail}  "
                f"(pulled {entry.pulled_at} from {entry.remote})\n"
            )
        sys.stdout.write(
            "Quarantined files stay on disk and out of the store. Fix one on the "
            "host that wrote it and pull again, or `bettermemory sync quarantine "
            "--release NAME` here (`--force` admits a credential refusal as it is).\n"
        )
        return
    try:
        result = _sync.release(
            directory,
            release,
            force=force,
            recorder=cli_recorder(ctx, attribution="cli_sync_release"),
            config=ctx.config,
        )
    except _sync.SyncError as exc:
        sys.stderr.write(f"sync quarantine failed: {exc}\n")
        raise SystemExit(2) from exc
    if json_out:
        sys.stdout.write(_json.dumps(result, indent=2) + "\n")
        return
    suffix = " (forced)" if result.get("forced") else ""
    sys.stdout.write(f"Released {release} from quarantine{suffix}.\n")
    sys.stdout.write(f"  reindexed {result.get('indexed_count', 0)} memories\n")
    _write_admission_lines({"flagged": result.get("flagged", [])})
