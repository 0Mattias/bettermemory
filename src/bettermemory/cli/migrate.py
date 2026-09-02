"""`bettermemory migrate` — one-shot data migrations."""

from __future__ import annotations

import argparse

from ._common import cli_context, cli_recorder


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``migrate`` subparser (with ``origin`` sub-subparser)."""
    help_text = (
        "One-shot data migrations. Use `migrate origin` to backfill "
        "the origin field on memories written before that field "
        "existed."
    )
    parser = sub.add_parser("migrate", help=help_text, description=help_text)
    migrate_sub = parser.add_subparsers(dest="migrate_cmd")
    origin_help = (
        "Backfill origin frontmatter on legacy memories. Idempotent: "
        "memories that already have an origin field are skipped."
    )
    origin_parser = migrate_sub.add_parser(
        "origin", help=origin_help, description=origin_help
    )
    origin_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing.",
    )
    origin_parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help=(
            "Force-tag every legacy memory with this remote URL. Use "
            "when the auto-inference from the parent directory isn't "
            "right (e.g. global memory dir that you know belongs to one "
            "repo)."
        ),
    )
    origin_parser.add_argument(
        "--scope-repo",
        action="append",
        default=[],
        metavar="SCOPE=URL",
        help=(
            "Route memories by scope: tag any memory carrying SCOPE "
            "with the given remote URL. Repeat for multiple scopes "
            "(e.g. --scope-repo projects:foo=git@github.com:me/foo.git "
            "--scope-repo projects:bar=git@github.com:me/bar.git). "
            "Memories whose scopes match nothing in the map fall through "
            "to --repo, then to the auto-inferred parent-repo origin "
            "(when the memory dir sits inside a checkout); only when no "
            "rule applies is the memory left untagged. The right tool "
            "for a global memory dir whose memories already use "
            "projects:<name> tags — on a global dir there is no parent "
            "to infer, so unmatched memories genuinely stay untagged."
        ),
    )
    origin_parser.add_argument(
        "--repair",
        action="store_true",
        help=(
            "Also fix memories that ALREADY have an origin block but "
            "whose repo was captured wrong — the usual cause is writing "
            "from a parent directory or $HOME, outside any checkout, "
            "which records repo=null and silently makes the memory "
            "global. Requires --scope-repo. Two rules: a null repo whose "
            "scopes name exactly one mapped repo is anchored to it; a "
            "repo that contradicts one of the memory's own mapped scopes "
            "(scoped projects:a, anchored to repo b — invisible from a) "
            "is cleared back to global. Only the origin block is "
            "rewritten. Pair with --dry-run first."
        ),
    )
    origin_parser.add_argument(
        "--keep-global",
        action="append",
        default=[],
        metavar="SCOPE",
        help=(
            "Cross-cutting scope that must never be anchored to one repo "
            "(e.g. --keep-global infrastructure --keep-global tools). A "
            "memory carrying SCOPE is left global by --repair's anchor "
            "rule, since anchoring a genuinely project-spanning memory "
            "would hide it from every other project. Never causes a "
            "demote. No effect without --repair."
        ),
    )
    return parser


def run(
    args: argparse.Namespace,
    *,
    root_parser: argparse.ArgumentParser,
    sub_parser: argparse.ArgumentParser,
) -> None:
    """Dispatch handler for ``bettermemory migrate``.

    ``root_parser`` is used for ``.error(...)`` so the program-name
    prefix on validation failures matches the pre-extraction output;
    ``sub_parser`` is used for ``.print_help()`` so a bare
    ``bettermemory migrate`` invocation prints the migrate-scoped help
    instead of the root help.
    """
    if args.migrate_cmd == "origin":
        scope_repo_map: dict[str, str] = {}
        for entry in args.scope_repo:
            if "=" not in entry:
                root_parser.error(f"--scope-repo expects SCOPE=URL, got: {entry!r}")
            scope, url = entry.split("=", 1)
            scope = scope.strip()
            url = url.strip()
            if not scope or not url:
                root_parser.error(
                    f"--scope-repo expects non-empty SCOPE and URL, got: {entry!r}"
                )
            scope_repo_map[scope] = url
        if args.repair and not scope_repo_map:
            root_parser.error(
                "--repair needs --scope-repo to know which scope maps to "
                "which repo; with no map there is nothing to check an "
                "existing origin against."
            )
        if args.keep_global and not args.repair:
            root_parser.error("--keep-global has no effect without --repair.")
        _cli_migrate_origin(
            dry_run=args.dry_run,
            force_repo=args.repo,
            scope_repo_map=scope_repo_map,
            repair=args.repair,
            keep_global=frozenset(s.strip() for s in args.keep_global if s.strip()),
        )
        return
    sub_parser.print_help()


def _cli_migrate_origin(
    *,
    dry_run: bool,
    force_repo: str | None,
    scope_repo_map: dict[str, str],
    repair: bool = False,
    keep_global: frozenset[str] = frozenset(),
) -> None:
    """`bettermemory migrate origin` — backfill origin on legacy memories."""
    from ..migrate import (
        infer_origin_for_memory_dir,
        migrate_origin_in_directory,
    )

    ctx = cli_context()
    memory_dir = ctx.directory

    print(f"Scanning {memory_dir}...")
    print()

    if scope_repo_map:
        print("Routing by scope:")
        for scope, url in scope_repo_map.items():
            print(f"  {scope:<32} -> {url}")
        print()

    if force_repo is not None:
        print(f"Fallback: untagged memories -> {force_repo!r}")
    else:
        inferred = infer_origin_for_memory_dir(memory_dir)
        if scope_repo_map and inferred is None:
            print(
                "Fallback: untagged memories left alone "
                "(no --repo and no auto-inference)."
            )
        elif scope_repo_map is None or not scope_repo_map:
            if inferred is None:
                print(
                    f"  Parent of memory dir: {memory_dir.parent}\n"
                    f"  No git remote detected.\n"
                    f"\n"
                    f"This appears to be a global memory directory — "
                    f"memories here probably came from many projects, "
                    f"and tagging them all with one repo would be "
                    f"misinformation. Nothing to do.\n"
                    f"\n"
                    f"Options:\n"
                    f"  --repo <url>                       "
                    f"force-tag every memory\n"
                    f"  --scope-repo projects:foo=<url>    "
                    f"route by scope (multi)"
                )
                return
            print(f"  Inferred repo:   {inferred.repo}")
            print(f"  cwd:             {inferred.cwd}")
            print("  branch:          (left null — original branch unknown)")

    print()
    if repair:
        print("Repair: ON — existing origin blocks are checked, not skipped.")
        if keep_global:
            print(f"  Never anchored: {', '.join(sorted(keep_global))}")
        print()

    report = migrate_origin_in_directory(
        memory_dir,
        force_repo=force_repo,
        scope_repo_map=scope_repo_map or None,
        repair=repair,
        keep_global=keep_global,
        dry_run=dry_run,
    )
    # A backfill rewrites records outside every Store mutator, so nothing
    # else puts it on the audit trail; the provenance derivation reads
    # rewrites from events. One event per run, naming every id, and none
    # for a dry run or a run that changed nothing.
    if not dry_run and report.updated_ids:
        cli_recorder(ctx).record(
            "migrate",
            action="origin",
            ids=list(report.updated_ids),
            updated=report.updated,
            repaired_anchored=report.repaired_anchored,
            repaired_demoted=report.repaired_demoted,
            via="cli",
        )

    print("Results:")
    print(f"  Scanned:           {report.scanned}")
    print(f"  Already had origin: {report.already_had_origin}")
    print(f"  {'Would update' if dry_run else 'Updated':<18} {report.updated}")
    if repair:
        verb = "Would anchor" if dry_run else "Anchored"
        print(f"    {verb:<16} {report.repaired_anchored}  (null repo -> repo)")
        verb = "Would demote" if dry_run else "Demoted"
        print(f"    {verb:<16} {report.repaired_demoted}  (wrong repo -> global)")
    if report.malformed:
        print(f"  Malformed (skipped): {len(report.malformed)}")
        for path in report.malformed[:5]:
            print(f"    - {path}")
        if len(report.malformed) > 5:
            print(f"    ... and {len(report.malformed) - 5} more")

    if dry_run and report.updated:
        print()
        print("(Dry run — no changes written. Re-run without --dry-run to apply.)")
