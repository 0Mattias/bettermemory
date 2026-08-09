"""`bettermemory consolidate` — offline curation pass."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ..config import Config
from ..store import Store
from ._common import cli_context


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``consolidate`` subparser on the parent parser."""
    help_text = (
        "Offline consolidation: dedup near-duplicates, demote "
        "never-applied memories to ambient, suggest cold-scope "
        "archival and scope-typo renames. Dry-run by default; "
        "--apply commits dedup tombstones and demotions. Cold-"
        "scope and scope-typo passes stay suggest-only."
    )
    parser = sub.add_parser("consolidate", help=help_text, description=help_text)
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Actually commit dedup tombstones and category demotions "
            "to disk. Without this flag, the command prints what it "
            "would do without touching the store."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=30,
        help=(
            "Demotion window in days. Memories created more than this "
            "many days ago with retrieval count greater than zero and "
            "applied count of zero are proposed for demotion to ambient. "
            "Default: 30 (matches the dead-weight rule in memory_health)."
        ),
    )
    parser.add_argument(
        "--cold-scope-days",
        type=int,
        default=180,
        help=(
            "Cold-scope cutoff in days. A scope whose newest memory is "
            "older than this AND with no applied events on any memory "
            "in the scope is suggested for archival. Suggest-only; "
            "auto-archiving a scope is too blunt without review. "
            "Default: 180."
        ),
    )
    parser.add_argument(
        "--dedup-threshold",
        type=float,
        default=None,
        help=(
            "Jaccard similarity cutoff for the dedup pass (default 0.75). "
            "Raising it makes dedup stricter — fewer near-duplicates "
            "tombstoned."
        ),
    )
    parser.add_argument(
        "--typo-distance",
        type=int,
        default=2,
        help=(
            "Accepted and ignored; kept so existing scripts keep "
            "parsing. The scope-typo detector shares health.py's "
            "neighbor rule, which owns its own length-scaled "
            "thresholds because no single whole-string Levenshtein "
            "cutoff works on real scope names: a shared 'projects:' "
            "prefix contributes zero distance (so short distinct tails "
            "collide) while namespace omission scores 9. Passing this "
            "flag changes nothing about which pairs are reported."
        ),
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help=(
            "Run the LLM-driven consolidation pass IN ADDITION to the "
            "structural passes. Asks the configured provider (default "
            "Ollama on localhost) to propose merges, contradiction "
            "resolutions, relative-date rewrites, and tier demotions on "
            "clusters of related memories. Dry-run by default; commits "
            "require --apply AND either --yes (batch accept) or an "
            "interactive terminal session (per-proposal prompt). The "
            "audit-transparency contract: Anthropic's Dreaming "
            "consolidates invisibly; bettermemory's --llm shows every "
            "proposed diff and refuses to commit without your accept."
        ),
    )
    parser.add_argument(
        "--llm-provider",
        type=str,
        default="ollama",
        choices=["ollama", "anthropic", "openai"],
        help=(
            "LLM provider to use with --llm. `ollama` (default) talks "
            "to a local Ollama instance at http://localhost:11434 — no "
            "network egress beyond localhost, no API key required. "
            "`anthropic` reads ANTHROPIC_API_KEY; `openai` reads "
            "OPENAI_API_KEY. Both require the corresponding SDK "
            "(`pip install anthropic` or `pip install openai`)."
        ),
    )
    parser.add_argument(
        "--llm-model",
        type=str,
        default=None,
        help=(
            "Override the provider's default model. Ollama default: "
            "`llama3.2:3b`. Anthropic default: `claude-haiku-4-5-20251001`. "
            "OpenAI default: `gpt-4o-mini`."
        ),
    )
    parser.add_argument(
        "--llm-url",
        type=str,
        default=None,
        help=(
            "Override the Ollama base URL. Default "
            "http://localhost:11434. Ignored by the Anthropic and "
            "OpenAI providers."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Batch-accept every --llm proposal without interactive "
            "prompts. Required for non-interactive --apply --llm runs "
            "(scripts, CI). Without --yes and without a TTY, --apply "
            "--llm refuses to commit anything."
        ),
    )
    parser.add_argument(
        "--acknowledge-debt",
        action="store_true",
        help=(
            "Walk the cold_endorsement_memories bucket (memories the "
            "ranker keeps surfacing without an explicit "
            "`memory_record_use(applied)` ever firing) and write one "
            "explicit `use(applied)` event per id. Retroactively clears "
            "the curation signal without touching bodies or scopes. "
            "Always commits — no --apply gate, because the events are "
            "purely additive and a misapplied acknowledgement can be "
            "reversed with a follow-up `corrected` event."
        ),
    )
    parser.add_argument(
        "--acknowledge-misses-before",
        type=str,
        default=None,
        metavar="ISO_TS",
        help=(
            "Write one additive `silent_miss_cutoff` event with "
            "`cutoff_ts=<ISO_TS>`. Subsequent `memory_health` / "
            "`memory_scope_overview` rollups drop any `turn_audited` / "
            "`search_miss` events earlier than the cutoff — the rollup "
            "always honors the latest cutoff seen. Use after a fix "
            "that invalidates a batch of historical misses (e.g. the "
            "v2.7.3 cwd-suppression change) so the rate metric reflects "
            "post-fix behavior. ISO_TS must carry an explicit UTC offset "
            "or trailing `Z` (e.g. `2026-05-25T05:25:35Z` or "
            "`2026-05-25T01:25:35-04:00`); naive local times are "
            "rejected to avoid silent off-by-zone cutoffs. Requires "
            "telemetry enabled — the cutoff is itself a telemetry event "
            "and a disabled recorder would silently no-op. Always "
            "commits — no --apply gate, because the event is purely "
            "additive and a misapplied cutoff can be superseded by a "
            "later one or ignored by manually pruning the cutoff event."
        ),
    )
    parser.add_argument(
        "--from-transcript",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Path to a transcript file (plain text, Markdown, or a "
            "Claude Code session JSONL — autodetected by extension). "
            "When set, the --llm pass adds a transcript_facts cluster "
            "that asks the LLM to propose new memories worth saving "
            "from the conversation — closing the writing-reflex gap "
            "where the model skips memory_write mid-task. Existing "
            "memories are passed in as the 'don't propose duplicates' "
            "context. Apply gate is shared with the other --llm "
            "proposal types: dry-run by default, --apply --yes for "
            "batch, --apply for interactive y/N. Requires --llm; "
            "without it the flag is a no-op."
        ),
    )
    return parser


def run(args: argparse.Namespace) -> None:
    """Dispatch handler for ``bettermemory consolidate``."""
    _cli_consolidate(
        apply=args.apply,
        json_out=args.json,
        window_days=args.window_days,
        cold_scope_days=args.cold_scope_days,
        dedup_threshold=args.dedup_threshold,
        typo_distance=args.typo_distance,
        llm=args.llm,
        llm_provider=args.llm_provider,
        llm_model=args.llm_model,
        llm_url=args.llm_url,
        yes=args.yes,
        from_transcript=args.from_transcript,
        acknowledge_debt=args.acknowledge_debt,
        acknowledge_misses_before=args.acknowledge_misses_before,
    )


def _cli_consolidate(
    *,
    apply: bool,
    json_out: bool,
    window_days: int,
    cold_scope_days: int,
    dedup_threshold: float | None,
    typo_distance: int,
    llm: bool = False,
    llm_provider: str = "ollama",
    llm_model: str | None = None,
    llm_url: str | None = None,
    yes: bool = False,
    from_transcript: str | None = None,
    acknowledge_debt: bool = False,
    acknowledge_misses_before: str | None = None,
) -> None:
    """`bettermemory consolidate` — offline curation pass.

    Runs four structural passes (dedup, demote-never-applied,
    cold-scope, scope-typo). Dry-run by default; `--apply` commits
    dedup tombstones and category demotions.

    With `--llm`, additionally runs an LLM-driven pass that proposes
    merges, contradiction resolutions, relative-date rewrites, and
    tier demotions across clusters of related memories. Commits
    require `--apply AND (--yes OR an interactive TTY)` — the
    audit-transparency contract refuses silent batch commits from
    untrusted reasoning.
    """
    from ..consolidate import consolidate, render_json, render_text

    ctx = cli_context()
    config = ctx.config
    store = ctx.store

    # Build a session id so tombstones produced by --apply carry a
    # caller-attributable record. Matches the SessionState pattern used
    # by the serve path; here we don't need the full state object, just
    # the id field for the tombstone frontmatter.
    from ..session import SessionState as _SessionState

    session_id = _SessionState().session_id

    report = consolidate(
        store,
        dedup_threshold=dedup_threshold,
        window_days=window_days,
        cold_scope_days=cold_scope_days,
        typo_distance=typo_distance,
        apply=apply,
        session_id=session_id,
    )
    sys.stdout.write(render_json(report) if json_out else render_text(report))

    if llm:
        _cli_consolidate_llm(
            store=store,
            dedup_threshold=dedup_threshold,
            apply=apply,
            yes=yes,
            json_out=json_out,
            session_id=session_id,
            provider_name=llm_provider,
            model=llm_model,
            url=llm_url,
            from_transcript=from_transcript,
            max_content_bytes=config.behavior.max_content_bytes,
            allowed_scopes=config.scopes.allowed,
        )

    if acknowledge_debt:
        _cli_consolidate_acknowledge_debt(
            store=store,
            config=config,
            session_id=session_id,
            json_out=json_out,
        )

    if acknowledge_misses_before is not None:
        _cli_consolidate_acknowledge_misses(
            store=store,
            config=config,
            session_id=session_id,
            cutoff_ts=acknowledge_misses_before,
            json_out=json_out,
        )


def _cli_consolidate_acknowledge_debt(
    *,
    store: Store,
    config: Config,
    session_id: str,
    json_out: bool,
) -> None:
    """Retroactively endorse memories in the ``cold_endorsement_memories`` bucket.

    Cold-endorsement memories = memories the ranker keeps surfacing
    (``retrieval_count >= endorsement_floor``) where every applied event
    came from the server's auto-fallback path (``auto=True``) rather
    than from a deliberate ``memory_record_use(applied)``. The
    ``health.cold_endorsement_memories`` rollup surfaces them; this
    pass clears them by writing one explicit ``use(applied)`` event
    per id — structurally identical to what an attentive model would
    have emitted on the next deliberate retrieval. No body or scope
    change; no tombstone; the original auto-applies stay in the log
    alongside the new explicit endorsements.

    Always commits regardless of ``--apply`` because the writes are
    additive: a mistaken acknowledgement can be reversed with a
    ``memory_record_use(outcome="corrected")`` follow-up, and no
    pre-existing event is overwritten. Mirrors the surface-area
    discipline of every other CLI write path (``reindex``, ``ingest``,
    ``--apply`` itself) by going through the shared
    :class:`Recorder` so file locking and rotation behave the same.

    Filter is re-derived inline because
    :class:`~bettermemory.health.ColdEndorsementMemories` caps its
    ``rows`` list at ``_COLD_ENDORSEMENT_CAP`` for inline display and
    we need every debt id, not just the top N. The four predicates
    match :func:`bettermemory.health.compute_health` exactly — if
    that canonical filter changes, this one must too. In particular
    the ``applied_count > 0`` gate (``health._is_weakly_endorsed``
    returns ``False`` at ``applied_count == 0``) is load-bearing: a
    pure dead-weight memory (retrieved over the floor but NEVER applied
    — zero auto AND zero explicit) belongs in the dead-weight removal
    bucket, NOT here. Omitting that gate would fabricate a
    ``use(applied)`` endorsement for it, bumping ``applied_count`` to 1
    and permanently shielding a never-applied memory from removal.
    """
    import json as _json

    from ..events import Recorder, iter_all_events
    from ..health import _COLD_ENDORSEMENT_MIN_RETRIEVALS
    from ..models import Category

    # Refuse to run with telemetry disabled — the Recorder's ``enabled``
    # flag turns ``record()`` into a hard no-op, so the explicit
    # use(applied) endorsements would silently disappear while the CLI
    # still printed "wrote N events" and exited 0. The endorsement IS a
    # telemetry event; with telemetry off there is nothing to write.
    # Mirrors the identical guard in ``_cli_consolidate_acknowledge_misses``.
    if not config.telemetry.enabled:
        sys.stderr.write(
            "acknowledge-debt: telemetry is disabled in the active "
            "config, so the explicit use(applied) endorsement events "
            "would be silently dropped. Enable telemetry "
            "(config.telemetry.enabled = true) before running this "
            "command.\n"
        )
        raise SystemExit(1)

    memories = store.load_all()
    events = list(iter_all_events(store.root))

    retrieval_counts: dict[str, int] = {m.id: 0 for m in memories}
    explicit_applied: dict[str, int] = {m.id: 0 for m in memories}
    # Total applied events of ANY kind (auto OR explicit). The
    # ``applied_count > 0`` gate below needs this — counting only
    # explicit applies (the prior behavior) cannot distinguish a
    # cold-endorsement memory (>=1 apply, all auto) from pure dead
    # weight (zero applies of any kind), and the latter must NOT be
    # endorsed here. Mirrors ``health._is_weakly_endorsed``.
    applied_total: dict[str, int] = {m.id: 0 for m in memories}
    for ev in events:
        kind = ev.get("kind")
        if kind == "search":
            for mid in (
                ev.get("returned") or ev.get("memory_ids") or ev.get("hit_ids") or []
            ):
                if mid in retrieval_counts:
                    retrieval_counts[mid] += 1
        elif kind == "use" and ev.get("outcome") == "applied":
            is_auto = ev.get("auto") is True
            for mid in ev.get("ids") or ev.get("memory_ids") or []:
                if mid in applied_total:
                    applied_total[mid] += 1
                if not is_auto and mid in explicit_applied:
                    explicit_applied[mid] += 1

    floor = _COLD_ENDORSEMENT_MIN_RETRIEVALS
    candidates = [
        m
        for m in memories
        if m.category != Category.AMBIENT
        and retrieval_counts.get(m.id, 0) >= floor
        and applied_total.get(m.id, 0) > 0
        and explicit_applied.get(m.id, 0) == 0
    ]

    if not candidates:
        if json_out:
            sys.stdout.write(
                _json.dumps(
                    {"acknowledged": 0, "floor": floor, "ids": []},
                    separators=(",", ":"),
                )
                + "\n"
            )
        else:
            sys.stdout.write(
                f"acknowledge-debt: no cold-endorsement memories "
                f"(floor: retrieval_count >= {floor} AND "
                f"applied_count > 0 AND explicit_applied_count == 0).\n"
            )
        return

    recorder = Recorder(
        root=store.root,
        session_id=session_id,
        enabled=config.telemetry.enabled,
        max_bytes=config.telemetry.max_bytes,
        log_queries_verbatim=config.telemetry.log_queries_verbatim,
    )

    acknowledged_ids: list[str] = []
    for m in candidates:
        recorder.record(
            "use",
            ids=[m.id],
            outcome="applied",
            auto=False,
            attribution="cli_acknowledge_debt",
            note="bettermemory consolidate --acknowledge-debt",
        )
        acknowledged_ids.append(m.id)

    if json_out:
        sys.stdout.write(
            _json.dumps(
                {
                    "acknowledged": len(acknowledged_ids),
                    "floor": floor,
                    "ids": acknowledged_ids,
                },
                separators=(",", ":"),
            )
            + "\n"
        )
        return

    sys.stdout.write(
        f"acknowledge-debt: wrote {len(acknowledged_ids)} explicit "
        f"`use(applied)` events for cold-endorsement memories "
        f"(floor: retrieval_count >= {floor}).\n"
    )
    display_cap = 20
    for mid in acknowledged_ids[:display_cap]:
        sys.stdout.write(f"  {mid}\n")
    if len(acknowledged_ids) > display_cap:
        sys.stdout.write(f"  ... and {len(acknowledged_ids) - display_cap} more\n")


def _cli_consolidate_acknowledge_misses(
    *,
    store: Store,
    config: Config,
    session_id: str,
    cutoff_ts: str,
    json_out: bool,
) -> None:
    """Write one additive `silent_miss_cutoff` event.

    The `memory_health` rollup honors the latest cutoff and drops any
    `turn_audited` / `search_miss` events with `ts < cutoff_ts` —
    invalidating a batch of historical misses after a fix lands
    (e.g. v2.7.3 cwd-suppression) so the miss-rate metric reflects
    post-fix behavior. Pure event-log write; no body or telemetry is
    mutated and no `.events.jsonl` line is removed. Mirrors the
    surface-area discipline of `--acknowledge-debt`: always commits,
    goes through the shared :class:`Recorder` for locking and rotation,
    and supports both text and JSON output. Validates the timestamp up
    front so a typo surfaces as an exit-1 error instead of silently
    writing a malformed event that the rollup will then ignore.

    The CLI rejects naive ISO timestamps and refuses to run with
    telemetry disabled, because both are silent-no-op footguns: a
    naive timestamp from a non-UTC user would be stamped UTC and
    produce an off-by-zone cutoff, and a disabled Recorder swallows
    every write so the user thinks the cutoff landed when nothing
    was written.
    """
    import json as _json
    from datetime import datetime, timedelta, timezone

    from ..events import Recorder, iter_all_events

    # Refuse to run with telemetry disabled — the Recorder's `enabled`
    # flag turns `record()` into a no-op, so the write would silently
    # disappear and exit 0. The cutoff is itself a telemetry event;
    # if telemetry is off there is nothing to acknowledge and no place
    # to write the marker.
    if not config.telemetry.enabled:
        sys.stderr.write(
            "acknowledge-misses-before: telemetry is disabled in the "
            "active config, so the cutoff event would be silently "
            "dropped. Enable telemetry (config.telemetry.enabled = "
            "true) before running this command.\n"
        )
        raise SystemExit(1)

    # Validate the cutoff up front. Accept both `Z` and explicit-offset
    # ISO forms (matching the Recorder's emission) — `_parse_ts` in
    # health.py does the same swap, but we re-implement here to keep
    # the CLI path from importing a private health helper.
    try:
        parsed = datetime.fromisoformat(cutoff_ts.replace("Z", "+00:00"))
    except ValueError:
        # Bare-date convenience hint: a tired oncall who types
        # `2026-05-25` (legitimate intent: midnight UTC of that day)
        # would otherwise just see "invalid ISO timestamp" and have to
        # guess the format. fromisoformat() *does* accept bare dates
        # since 3.11, so this branch only fires for genuinely malformed
        # input — but pointing out the midnight-UTC spelling is the
        # cheap-help.
        import re as _re

        bare_date_hint = ""
        if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", cutoff_ts):
            bare_date_hint = f" (or '{cutoff_ts}T00:00:00Z' if you meant midnight UTC)"
        sys.stderr.write(
            f"acknowledge-misses-before: invalid ISO timestamp "
            f"{cutoff_ts!r}. Expected e.g. '2026-05-25T05:25:35Z'"
            f"{bare_date_hint}.\n"
        )
        raise SystemExit(1) from None

    # Reject naive timestamps. A bare `2026-05-25T10:00:00` from a
    # non-UTC user produces a cutoff several hours off-by-zone with no
    # warning — the rollup compares aware datetimes, so the
    # discrepancy would only show up days later as a confusing
    # miss-rate skew. Forcing the user to spell out the offset (or
    # write `Z`) makes the assumption part of the input.
    if parsed.tzinfo is None:
        # If the user typed a bare date, the parse SUCCEEDED (3.11+
        # fromisoformat accepts it) but produced a naive midnight — so
        # the hint here is the same as the parse-error branch above:
        # spell out the offset.
        sys.stderr.write(
            f"acknowledge-misses-before: ISO timestamp {cutoff_ts!r} "
            f"is missing a UTC offset. Pass an explicit offset or "
            f"trailing `Z` (e.g. '{cutoff_ts}T00:00:00Z' for midnight "
            f"UTC, or '2026-05-25T01:25:35-04:00' for an explicit "
            f"offset) so the cutoff isn't silently interpreted as "
            f"your local zone.\n"
        )
        raise SystemExit(1)

    # Refuse far-future cutoffs. A typo like `2126-05-25T00:00:00Z`
    # parses and validates fine but writes a cutoff a century out;
    # subsequent `memory_health` runs report `audited_total=0,
    # miss_total=0` forever and the rollup looks "clean". A small
    # forward-grace is fine (the existing test exercises `now + 1min`
    # to clear a freshly-fired miss); a day is generous for legitimate
    # admin "drop everything up through tomorrow" intent. Beyond that
    # the input is almost certainly a typo or pasted-wrong-year.
    now_utc = datetime.now(timezone.utc)
    far_future_grace = timedelta(hours=24)
    if parsed > now_utc + far_future_grace:
        sys.stderr.write(
            f"acknowledge-misses-before: cutoff {cutoff_ts!r} is more "
            f"than 24 hours in the future (now is "
            f"{now_utc.isoformat().replace('+00:00', 'Z')}). This is "
            f"almost always a typo — a year-2126 cutoff would silently "
            f"hide every audited event in the log indefinitely. Pass a "
            f"timestamp at or before "
            f"{(now_utc + far_future_grace).isoformat().replace('+00:00', 'Z')}.\n"
        )
        raise SystemExit(1)

    # Normalize to UTC-Z so every cutoff event in the log uses the same
    # representation, regardless of which offset the caller passed. The
    # rollup compares aware datetimes, so this is a presentation detail
    # rather than a correctness one — but consistent formatting makes
    # the events easier to eyeball.
    canonical_cutoff = (
        parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    )

    recorder = Recorder(
        root=store.root,
        session_id=session_id,
        enabled=config.telemetry.enabled,
        max_bytes=config.telemetry.max_bytes,
        log_queries_verbatim=config.telemetry.log_queries_verbatim,
    )
    recorder.record(
        "silent_miss_cutoff",
        cutoff_ts=canonical_cutoff,
        attribution="cli_acknowledge_misses",
        note="bettermemory consolidate --acknowledge-misses-before",
    )

    # Defensive verification: `Recorder.record` swallows every
    # exception by design (a logging hiccup must never break a tool
    # call), but for an admin CLI op that means a chmod failure / I/O
    # error would still exit 0 with nothing on disk. Read back through
    # `iter_all_events` and confirm our event landed — scoped to this
    # session_id + canonical_cutoff so we don't false-positive on a
    # prior cutoff with the same timestamp.
    landed = any(
        ev.get("kind") == "silent_miss_cutoff"
        and ev.get("cutoff_ts") == canonical_cutoff
        and ev.get("session") == session_id
        for ev in iter_all_events(store.root)
    )
    if not landed:
        sys.stderr.write(
            "acknowledge-misses-before: recorder.record() returned "
            "but the silent_miss_cutoff event is not visible in the "
            "events log. Check filesystem permissions and disk space; "
            "no cutoff was applied.\n"
        )
        raise SystemExit(1)

    if json_out:
        sys.stdout.write(
            _json.dumps(
                {"silent_miss_cutoff": canonical_cutoff},
                separators=(",", ":"),
            )
            + "\n"
        )
        return

    sys.stdout.write(
        f"acknowledge-misses-before: wrote `silent_miss_cutoff` event "
        f"with cutoff_ts={canonical_cutoff}. Health rollups will now "
        f"drop any earlier `turn_audited` / `search_miss` events.\n"
    )


def _cli_consolidate_llm(
    *,
    store: Store,
    dedup_threshold: float | None,
    apply: bool,
    yes: bool,
    json_out: bool,
    session_id: str,
    provider_name: str,
    model: str | None,
    url: str | None,
    from_transcript: str | None = None,
    max_content_bytes: int | None = None,
    allowed_scopes: list[str] | None = None,
) -> None:
    """Run the --llm pass after the structural passes have rendered.

    Kept separate from `_cli_consolidate` so the structural-only path
    has zero LLM-related import cost on a clean run.
    """
    from .. import llm as _llm
    from ..consolidate import consolidate_llm, render_llm_json, render_llm_text
    from ..origin import capture as _capture_origin

    provider_kwargs: dict[str, Any] = {}
    if model is not None:
        provider_kwargs["model"] = model
    if url is not None and provider_name == "ollama":
        provider_kwargs["url"] = url
    provider = _llm.make_provider(provider_name, **provider_kwargs)

    # Interactive prompt: only when a TTY is attached AND --yes wasn't
    # passed. Non-TTY runs without --yes fall through to the in-module
    # refuse-to-commit branch (logged with a clear message).
    interactive_input: Any = (
        input if (apply and not yes and sys.stdin.isatty()) else None
    )

    report = consolidate_llm(
        store,
        provider,
        dedup_threshold=dedup_threshold,
        apply=apply,
        accept=yes,
        interactive_input=interactive_input,
        session_id=session_id,
        from_transcript=from_transcript,
        max_content_bytes=max_content_bytes,
        allowed_scopes=allowed_scopes,
        # Capture the caller's CWD context here (the CLI is the layer
        # that has it) so every propose_new write carries a real origin
        # instead of persisting origin=None and leaking across scopes /
        # worktrees. Mirrors the accept-proposal path's capture_origin().
        origin=_capture_origin(),
    )
    sys.stdout.write(render_llm_json(report) if json_out else render_llm_text(report))
