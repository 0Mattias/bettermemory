"""`bettermemory ingest` — import Claude Code's auto-memory directory."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ._common import cli_context, cli_recorder


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``ingest`` subparser on the parent parser."""
    help_text = (
        "Import Claude Code's auto-memory directory "
        "(~/.claude/projects/<sanitized-cwd>/memory/) into the "
        "bettermemory store. Maps the auto-memory `type` to a "
        "bettermemory category, dedups against the active store "
        "and tombstone log, and writes survivors as ordinary "
        "records carrying an `imported-from-claude-code` scope. "
        "The framing is 'consume rather than fight' the auto-"
        "memory feature: the user keeps the ergonomic capture and "
        "gains the verification surface."
    )
    parser = sub.add_parser("ingest", help=help_text, description=help_text)
    parser.add_argument(
        "--from",
        dest="source",
        type=str,
        default=None,
        help=(
            "Path to the source directory. When omitted, "
            "auto-detects the per-cwd auto-memory path; if no "
            "auto-memory exists for this cwd, exits with a hint."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Plan only — show what would be ingested without writing. "
            "Default is to commit (mirrors `bettermemory health` rather "
            "than `bettermemory consolidate`, which defaults to dry-run; "
            "ingest is symmetric with the cron-style `sync auto` and "
            "the bias is 'one shot, low cost, run it.')"
        ),
    )
    parser.add_argument(
        "--scope",
        action="append",
        default=[],
        dest="extra_scopes",
        help=(
            "Extra scope tag(s) to append to every ingested memory, on top "
            "of the default `imported-from-claude-code` and the type-derived "
            "tag. Repeat for multiple. These are the scopes `[scopes] "
            "allowed` is checked against when that list is non-empty — a "
            "value outside it skips the row, in the plan and at commit "
            "alike; the two tags above are stamped by ingest itself and "
            "are exempt."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Skip the active-store dedup gate. Tombstone dedup is still "
            "respected (re-importing something the user already chose to "
            "remove stays out of the active store). Parity with the "
            "`force=True` option on `memory_write` for the rare case of "
            "a legitimately-near auto-memory that should land alongside "
            "an existing record rather than being suppressed as duplicate."
        ),
    )
    parser.add_argument(
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
    """Dispatch handler for ``bettermemory ingest``.

    ``sub_parser`` is forwarded into ``_cli_ingest`` so ``parser.error(...)``
    on a missing source root points at the ingest subparser — matches
    the pre-extraction ``parser=ingest_parser`` argument.
    """
    _cli_ingest(
        source=args.source,
        dry_run=args.dry_run,
        extra_scopes=args.extra_scopes,
        force=args.force,
        json_out=args.json,
        parser=sub_parser,
    )


def _cli_ingest(
    *,
    source: str | None,
    dry_run: bool,
    extra_scopes: list[str],
    force: bool = False,
    json_out: bool,
    parser: Any,
) -> None:
    """`bettermemory ingest` — import Claude Code's auto-memory directory.

    Resolves the source root (explicit `--from` wins; otherwise tries
    the per-cwd auto-memory path), classifies every `.md` file via
    ``compute_ingest_plan``, and (unless `--dry-run`) commits the
    write actions via ``apply_ingest_plan``.

    Exit codes:
    - 0: ran successfully (even when 0 rows landed)
    - 1: source root not found / not a directory (the user-facing
      hint surfaces in the error message via parser.error)
    """
    import json as _json
    from pathlib import Path as _Path

    from ..ingest import (
        apply_ingest_plan,
        compute_ingest_plan,
        discover_default_source_root,
        render_ingest_text,
    )
    from ..models import validate_scope

    # Validate --scope up front (mirrors export.py / tombstones.py) so a
    # malformed scope fails fast and IDENTICALLY for --dry-run and commit.
    # Otherwise the scope is appended unchecked and only rejected per-row at
    # apply time, so a green dry-run ("would write N") was followed by an
    # all-skip_invalid commit — the dry-run lied about what would happen.
    for s in extra_scopes:
        try:
            validate_scope(s)
        except ValueError as exc:
            parser.error(str(exc))

    if source:
        source_root: _Path | None = _Path(source).expanduser()
    else:
        source_root = discover_default_source_root()
        if source_root is None:
            parser.error(
                "no --from given and no auto-memory directory found for "
                f"the current cwd ({_Path.cwd()}). Pass --from PATH to "
                "point at an existing auto-memory directory; on this "
                "machine the expected layout is "
                "~/.claude/projects/<sanitized-cwd>/memory/."
            )
            return  # pragma: no cover — parser.error raises SystemExit
    assert source_root is not None  # narrowed by both branches above

    ctx = cli_context()
    store = ctx.store
    existing = store.load_all()
    tombstoned = store.load_tombstones()

    try:
        plan = compute_ingest_plan(
            source_root,
            existing_memories=existing,
            existing_tombstones=tombstoned,
            extra_scopes=extra_scopes,
            # Same object the apply below gets, for the same reason the
            # dedup policy is resolved once above: `[scopes] allowed` is
            # read on BOTH sides, and a plan that did not know about it
            # printed "would write N" for rows the commit then refused
            # wholesale. Threading it here is what makes `--dry-run`
            # predict the commit rather than describe a different run.
            config=ctx.config,
            force=force,
        )
    except (FileNotFoundError, NotADirectoryError) as exc:
        parser.error(str(exc))
        return  # pragma: no cover

    if not dry_run:
        # The shared CLI recorder mirrors the server's construction
        # (builder.py) so the ingest events land in the same audit log
        # under the same telemetry posture: `[telemetry] enabled = false`
        # turns the event log off everywhere, this CLI included, and the
        # rotation cap and verbatim-query redaction follow the same
        # config the server reads. Before the kwargs were threaded, an
        # opted-out user still got `write` events on every ingest.
        recorder = cli_recorder(ctx, attribution="cli_ingest")
        # Thread the real config, not the `Config()` fallback — the CLI
        # is the one caller that always has it resolved, and passing the
        # same object to both phases keeps the plan and the commit on ONE
        # policy.
        #
        # The same object also carries the `[scopes] allowed` whitelist.
        # That one is enforced inside `apply_ingest_plan` (and predicted
        # by `compute_ingest_plan` above, which is why both calls get this
        # object) — never by a check written at this call site: the
        # library entry points are reachable without this CLI, and
        # enforcing at this site alone would make the CLI and the library
        # disagree about the policy, the exact divergence the shared chain
        # exists to end. `_validate_write_payload` (handlers/_shared.py) is
        # where every other write path enforces it, and ingest never calls
        # it (`apply_ingest_plan` builds its `Store.write` payload itself),
        # so `ingest --scope <not-in-allowlist>` used to land an
        # unsanctioned scope; `ingest._scope_allowlist_reason` now flips
        # that row to `skip_invalid` instead. It refuses per row, not per
        # run, so a conforming row in the same batch still commits.
        #
        # What it does NOT refuse: the constant `imported-from-claude-code`
        # and the type-derived scope. Ingest stamps those on every row, the
        # user never typed them and cannot opt out, so enforcing a
        # user-scope policy against them made every allowlist that omitted
        # them refuse every row — `--scope projects:demo` under
        # `allowed = ["projects:demo"]` imported nothing. They are exempt;
        # the list is checked against `--scope` values and anything a
        # source file carries.
        #
        # Still NOT enforced on this path, and `docs/ROADMAP.md` carries
        # them: the `max_content_bytes` / `min_content_tokens` /
        # `max_scopes_per_write` caps, which live in the same
        # `_validate_write_payload` and which no gate reads either.
        #
        # `force` has to arrive here too, not only at plan time — the apply
        # loop runs its own dedup gate, so a plan computed under --force was
        # refused right back at commit.
        apply_ingest_plan(
            plan, store, recorder=recorder, config=ctx.config, force=force
        )

    if json_out:
        sys.stdout.write(_json.dumps(plan.to_dict(), indent=2) + "\n")
    else:
        sys.stdout.write(render_ingest_text(plan, dry_run=dry_run))
