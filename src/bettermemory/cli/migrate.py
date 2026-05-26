"""`bettermemory migrate` — one-shot data migrations."""

from __future__ import annotations

import argparse

from ._common import cli_context


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``migrate`` subparser (with ``origin`` sub-subparser)."""
    parser = sub.add_parser(
        "migrate",
        help=(
            "One-shot data migrations. Use `migrate origin` to backfill "
            "the origin field on memories written before that field "
            "existed."
        ),
    )
    migrate_sub = parser.add_subparsers(dest="migrate_cmd")
    origin_parser = migrate_sub.add_parser(
        "origin",
        help=(
            "Backfill origin frontmatter on legacy memories. Idempotent: "
            "memories that already have an origin field are skipped."
        ),
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
            "to --repo (if given) or are left untagged. The right tool "
            "for a global memory dir whose memories already use "
            "projects:<name> tags."
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
        _cli_migrate_origin(
            dry_run=args.dry_run,
            force_repo=args.repo,
            scope_repo_map=scope_repo_map,
        )
        return
    sub_parser.print_help()


def _cli_migrate_origin(
    *,
    dry_run: bool,
    force_repo: str | None,
    scope_repo_map: dict[str, str],
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
    report = migrate_origin_in_directory(
        memory_dir,
        force_repo=force_repo,
        scope_repo_map=scope_repo_map or None,
        dry_run=dry_run,
    )

    print("Results:")
    print(f"  Scanned:           {report.scanned}")
    print(f"  Already had origin: {report.already_had_origin}")
    print(f"  {'Would update' if dry_run else 'Updated':<18} {report.updated}")
    if report.malformed:
        print(f"  Malformed (skipped): {len(report.malformed)}")
        for path in report.malformed[:5]:
            print(f"    - {path}")
        if len(report.malformed) > 5:
            print(f"    ... and {len(report.malformed) - 5} more")

    if dry_run and report.updated:
        print()
        print("(Dry run — no changes written. Re-run without --dry-run to apply.)")
