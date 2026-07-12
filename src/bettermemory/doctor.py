"""`bettermemory doctor` — diagnose install state.

A series of independent checks that each return a `Diagnosis`. The
output is the list of diagnoses plus an aggregate verdict
(`ok` | `warn` | `fail`). A `fail` diagnosis means something is
actively broken — fix it. A `warn` means something is suspicious or
might bite later. An `ok` means the check passed.

Each check wraps its body in a `try/except` so a single broken probe
can't take down the whole report — the failure surfaces as a
diagnosis with status `fail`, not an unhandled exception.

The motivating failure modes (from the README troubleshooting
section):
- `bettermemory` not on PATH for the spawned client process
- `BETTERMEMORY_DIR` mis-set, storage directory not writable
- `mcpServers.memory.command` in a client config points at a stale
  binary path (e.g. user reinstalled `bettermemory` into a different
  venv and the registered path no longer exists)
- `semantic_dedup = true` in config but the `embeddings` extra not
  installed
- frontmatter parse errors on memory bodies that have been hand-edited
  into invalid YAML

`--fix` applies the SAFE subset of the remediations the hints describe
— store/event-log permissions, index rebuild, stale-lockfile removal,
the sync repo's `.gitignore` refresh — by calling the same underlying
functions the hints point at (never by re-parsing hint strings, which
would reopen the quoting class the hint hardening closed), re-runs each
affected check, and reports before/after. Plain `doctor` remains the
dry run; destructive remediations (untracking, history rewrites, MCP
client config edits, anything that could delete possibly-unique user
content, anything on another host) stay hints forever. Every applied
fix lands one `doctor_fix` event in the store's event log.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import sysconfig
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from .config import Config, load_config
from .events import EVENT_LOG_FILENAME, iter_all_events
from .init import KNOWN_CLIENTS, command_launches_bettermemory, find_binary
from .store import Store, count_active_memory_files, count_unparseable_memory_files


CheckStatus = Literal["ok", "warn", "fail"]


# Sentinel filename the storage-directory probe writes and unlinks. Lifted
# to a module-level constant so `sync.py`'s gitignore writer can import the
# name rather than hardcode a sibling literal (which would drift silently
# if the probe ever moved). See `_check_storage_directory` below.
DOCTOR_PROBE_FILENAME = ".doctor-probe"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class Diagnosis:
    """One check's verdict.

    `details` is a free-form dict for machine-readable JSON output;
    text rendering ignores it. `fix_hint` is the actionable
    one-sentence "do this to fix it" prompt — set when status != "ok".
    """

    name: str
    status: CheckStatus
    message: str
    fix_hint: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class DoctorReport:
    checks: list[Diagnosis]

    @property
    def overall(self) -> CheckStatus:
        if any(c.status == "fail" for c in self.checks):
            return "fail"
        if any(c.status == "warn" for c in self.checks):
            return "warn"
        return "ok"


@dataclass
class FixResult:
    """One attempted `--fix` remediation's outcome.

    `applied` means a mutation actually happened (a chmod landed, files
    were removed, the index was rebuilt) — a fixer that raised, or found
    nothing matching its guard at fix time, reports `applied=False`.
    `after_status` is the re-run of the SAME check immediately after the
    attempt; "fixed" in the rendered output means `applied and
    after_status == "ok"`, never "we ran something".
    """

    check: str
    action: str
    applied: bool
    before_status: CheckStatus
    after_status: CheckStatus
    message: str
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_python_version() -> Diagnosis:
    minor = sys.version_info[:2]
    pretty = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    if minor < (3, 11):
        return Diagnosis(
            name="python_version",
            status="fail",
            message=f"Python {pretty} is below the minimum supported version (3.11).",
            fix_hint="Reinstall bettermemory under a Python ≥ 3.11 interpreter.",
            details={"version": pretty, "minimum": "3.11"},
        )
    return Diagnosis(
        name="python_version",
        status="ok",
        message=f"Python {pretty} (≥ 3.11).",
        details={"version": pretty},
    )


def _check_binary_on_path() -> Diagnosis:
    binary = shutil.which("bettermemory")
    if binary:
        return Diagnosis(
            name="binary_on_path",
            status="ok",
            message=f"bettermemory found on $PATH at {binary}.",
            details={"path": binary},
        )
    fallback = find_binary()
    # Keep the fix_hint generic. Pre-Round-3 doctor substituted the
    # resolved invocation path ("Use the absolute path in MCP client
    # configs: /some/path") into the hint to save the user a `which`
    # lookup. The footgun: on a machine with parallel installs (pipx +
    # `uv tool install` + a venv shim), the path we'd substitute is
    # the binary that satisfied the current `bettermemory doctor`
    # invocation, which is not necessarily the one the user wants
    # pinned into their MCP client config. Pasting a stale shim path
    # there silently breaks future upgrades. The generic hint sends the
    # user through `bettermemory init` (which handles the rewrite
    # transactionally) or `which bettermemory` (which they run from
    # the shell they intend the binary to come from). The resolved
    # path is still in `details` for tooling that wants it.
    resolved_path: str | None = None
    if Path(fallback).is_absolute() and Path(fallback).exists():
        resolved_path = fallback
    elif sys.argv and sys.argv[0]:
        argv0 = Path(sys.argv[0])
        if argv0.is_absolute() and argv0.exists() and "bettermemory" in argv0.name:
            resolved_path = str(argv0.resolve())

    hint = (
        "Use the absolute path to the `bettermemory` binary in MCP "
        "client configs (find it with `which bettermemory` from a "
        "shell that has it on PATH, or run "
        "`bettermemory init --client X` which does this automatically)."
    )
    return Diagnosis(
        name="binary_on_path",
        status="warn",
        message=(
            "`bettermemory` not on $PATH for this shell. MCP clients "
            "spawn the server in a fresh process and won't find it "
            "either unless their PATH is set up at GUI-launch time "
            "(macOS Finder/Launchpad inherits a minimal PATH)."
        ),
        fix_hint=hint,
        details={"resolved_binary": fallback, "resolved_path": resolved_path},
    )


def _check_config_loadable() -> tuple[Diagnosis, Config | None]:
    try:
        cfg = load_config()
    except Exception as exc:  # noqa: BLE001
        return (
            Diagnosis(
                name="config_loadable",
                status="fail",
                message=f"Failed to load config: {exc.__class__.__name__}: {exc}.",
                fix_hint=(
                    "The config file is the platform-standard config dir "
                    "(see README §Config). Inspect or delete it; defaults "
                    "are used when the file is missing."
                ),
            ),
            None,
        )
    return (
        Diagnosis(
            name="config_loadable",
            status="ok",
            message="Config loads cleanly.",
        ),
        cfg,
    )


def _check_storage_directory(cfg: Config) -> tuple[Diagnosis, Path | None]:
    try:
        directory = cfg.resolved_directory()
    except Exception as exc:  # noqa: BLE001
        return (
            Diagnosis(
                name="storage_directory",
                status="fail",
                message=f"Could not resolve storage directory: {exc}.",
                fix_hint="Check `BETTERMEMORY_DIR` and `[storage] directory` in config.",
            ),
            None,
        )

    info: dict[str, Any] = {"directory": str(directory)}

    if not directory.exists():
        # Created on first write, so missing is not a failure — just
        # an info point. We probe writability via the parent.
        parent = directory.parent
        if parent.exists() and os.access(parent, os.W_OK):
            return (
                Diagnosis(
                    name="storage_directory",
                    status="ok",
                    message=f"Storage at {directory} (will be created on first write).",
                    details=info,
                ),
                directory,
            )
        return (
            Diagnosis(
                name="storage_directory",
                status="fail",
                message=(
                    f"Storage at {directory} doesn't exist and the parent "
                    f"({parent}) is not writable."
                ),
                fix_hint=(
                    "Create the parent directory or set `BETTERMEMORY_DIR` "
                    "to a writable path."
                ),
                details=info,
            ),
            directory,
        )

    if not directory.is_dir():
        return (
            Diagnosis(
                name="storage_directory",
                status="fail",
                message=f"{directory} exists but is not a directory.",
                fix_hint="Remove the file at that path or set BETTERMEMORY_DIR.",
                details=info,
            ),
            directory,
        )

    if not os.access(directory, os.W_OK):
        return (
            Diagnosis(
                name="storage_directory",
                status="fail",
                message=f"Storage directory {directory} is not writable.",
                fix_hint="`chmod u+w` the directory or set BETTERMEMORY_DIR to a writable path.",
                details=info,
            ),
            directory,
        )

    # Probe write a sentinel file. Cheap insurance against weird FS
    # situations (read-only mounts, NFS quirks) that pass os.access but
    # fail on actual write.
    #
    # The cleanup is in `finally` with `missing_ok=True` so an ENOSPC
    # mid-write (which can leave a zero-byte sentinel before raising)
    # doesn't strand `.doctor-probe` in the user's store directory.
    # Without the finally arm a subsequent `doctor` run would still
    # report `fail`, but the user would also be wondering where that
    # stray file came from.
    probe = directory / DOCTOR_PROBE_FILENAME
    try:
        try:
            probe.write_text("ok", encoding="utf-8")
        finally:
            probe.unlink(missing_ok=True)
    except OSError as exc:
        return (
            Diagnosis(
                name="storage_directory",
                status="fail",
                message=f"Probe write to {directory} failed: {exc}.",
                fix_hint="Check filesystem permissions and disk space.",
                details=info,
            ),
            directory,
        )

    return (
        Diagnosis(
            name="storage_directory",
            status="ok",
            message=f"Storage at {directory} (writable).",
            details=info,
        ),
        directory,
    )


def _check_memory_parse_health(directory: Path) -> Diagnosis:
    """Try to load every active memory; surface parse failures.

    Store.load_all already skips malformed files with a logged warning;
    we run it here under a count-mode harness so doctor can report
    "everything parses" or "N files failed to parse".
    """
    # Don't construct a Store against a non-existent path: Store.__post_init__
    # would mkdir it (+ a .tombstones/ subdir), a write side effect from a
    # read-only probe. Mirrors the sibling checks' `if not directory.exists()`
    # guard. (The live caller already gates on directory.exists(); this keeps
    # the helper safe in isolation / if that gate is ever reordered.)
    if not directory.exists():
        return Diagnosis(
            name="memory_parse_health",
            status="ok",
            message="Storage dir does not exist yet — nothing to parse.",
        )
    try:
        store = Store(directory)
        memories = store.load_all()
    except Exception as exc:  # noqa: BLE001
        return Diagnosis(
            name="memory_parse_health",
            status="fail",
            message=f"Could not list memories: {exc}.",
            fix_hint=f"Inspect {directory} for corrupt files.",
        )

    # Count with the store's own enumeration: `count_active_memory_files`
    # is the `_iter_active_paths` filter (regular file, not a symlink,
    # `.md` suffix) as a bare count. The store makes no exception for
    # README.md or dot-prefixed names, so neither can this check — a
    # hand-rolled filter that skipped them made this check disagree with
    # index_health, which counts via the same store helpers (it deferred
    # unparseable READMEs here while this check reported "all clean").
    # Symlink exclusion is part of the same contract: `_iter_active_paths`
    # rejects symlinks BEFORE parsing, so counting one as "failed to
    # parse" would point the user at frontmatter that was never read.
    parsed = len(memories)
    on_disk = count_active_memory_files(directory)
    if parsed == on_disk:
        return Diagnosis(
            name="memory_parse_health",
            status="ok",
            message=f"All {parsed} active memories parse cleanly.",
            details={"parsed": parsed, "files_on_disk": on_disk},
        )
    # The count delta can't distinguish malformed frontmatter from an
    # intentionally-skipped file (a schema_version newer than this install,
    # e.g. after a `sync pull` from a machine on a newer bettermemory). Don't
    # assert "did not parse" or point only at frontmatter — and don't claim a
    # "logged warning" the skip path doesn't actually emit.
    skipped = on_disk - parsed
    return Diagnosis(
        name="memory_parse_health",
        status="warn",
        message=(
            f"{skipped} of {on_disk} memory files in {directory} were skipped "
            f"by the loader (malformed frontmatter, or a schema_version newer "
            f"than this install)."
        ),
        fix_hint=(
            "Check the frontmatter of files missing from `bettermemory "
            "health`; if you recently downgraded bettermemory, upgrade back "
            "to read memories written under the newer version."
        ),
        details={"parsed": parsed, "files_on_disk": on_disk, "skipped": skipped},
    )


def _probe_index_integrity(index_file: Path) -> str | None:
    """`PRAGMA quick_check` over the index database; None when it
    reports `ok`, else a one-line description of the damage.

    `index.status()` is meta-only BY DESIGN — it runs on every Store
    construction, so it reads the meta table and a stat and never
    touches the FTS shadow/data pages; a torn interior page passes it
    with clean counts. Doctor runs on demand and can afford the full
    page walk. The connection is read-only (URI `mode=ro`) so the probe
    can neither create a missing file nor mutate an existing one, and
    the broad except mirrors `status()`'s never-raises tolerance:
    severe corruption makes the PRAGMA itself raise (`database disk
    image is malformed`) rather than return finding rows, and either
    shape IS the finding.
    """
    # Lazy for the same no-sqlite3-interpreter reason as
    # `_check_index_health`'s `index` import.
    import sqlite3

    try:
        # `as_uri()` percent-encodes the path (spaces, `?`, `#`), so
        # `?mode=ro` is the only query parameter SQLite parses.
        conn = sqlite3.connect(
            index_file.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0
        )
        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return f"{exc.__class__.__name__}: {exc}"
    # Milder damage comes back as finding rows (possibly multi-line,
    # e.g. "Tree 6 page 6: free space corruption"); flatten and cap so
    # the diagnosis stays one line.
    findings = [" ".join(str(row[0]).split()) for row in rows]
    if findings == ["ok"]:
        return None
    if len(findings) > 3:
        findings = findings[:3] + [f"… +{len(findings) - 3} more"]
    return "; ".join(findings) or "quick_check returned no rows"


def _check_index_health(directory: Path) -> Diagnosis:
    """Probe the FTS5 index: `index.status()` (never raises) for the
    meta-level states, `PRAGMA quick_check` for the page-level
    corruption those meta reads can't see (see
    `_probe_index_integrity`), and compare `indexed_count` against the
    on-disk file count.

    A status()-visible unhealthy index (corrupt meta, missing,
    rebuild-pending) never breaks correctness —
    `_load_search_candidates` routes every `memory_search` to a full
    `load_all` — but the degradation to a linear scan is silent, and a
    count divergence additionally means stale filename lookups and link
    annotations. Page-level corruption is worse: that routing keys off
    the same meta-only `status()`, so nothing falls back and the first
    read to touch a damaged page raises — doctor is the surface that
    has to catch it. Every unhealthy state shares the one repair:
    `bettermemory reindex`. The count comparison
    reuses the S4 divergence machinery
    (`store.count_active_memory_files` +
    `store.count_unparseable_memory_files`) so doctor and the startup
    warning cannot disagree about what "in sync" means — in particular
    both subtract the unparseable files a rebuild can never index, so
    this check never prescribes a reindex that cannot clear it (those
    files are memory_parse_health's finding, with the accurate
    fix-the-file hint).
    """
    # Lazy import mirrors every other `index` consumer (store,
    # _handlers, the reindex CLI): an interpreter built without sqlite3
    # then fails THIS check via `_safe`, not `import bettermemory.doctor`.
    from . import index

    if not directory.exists():
        return Diagnosis(
            name="index_health",
            status="ok",
            message="Storage dir does not exist yet — no index to check.",
        )
    status = index.status(directory)
    try:
        disk_count = count_active_memory_files(directory)
    except OSError as exc:
        return Diagnosis(
            name="index_health",
            status="fail",
            message=f"Could not count memory files: {exc}.",
            fix_hint=f"Check permissions on {directory}.",
        )
    details: dict[str, Any] = dict(status)
    details["disk_count"] = disk_count
    fix = "Run `bettermemory reindex` to rebuild the index from canonical disk state."

    if not status.get("exists"):
        if disk_count == 0:
            return Diagnosis(
                name="index_health",
                status="ok",
                message="No index yet — created on the first memory write.",
                details=details,
            )
        return Diagnosis(
            name="index_health",
            status="warn",
            message=(
                f"No index file but {disk_count} memory file(s) on disk "
                f"(typical after a sync pull); memory_search falls back "
                f"to a linear scan."
            ),
            fix_hint=fix,
            details=details,
        )
    if status.get("corrupt"):
        return Diagnosis(
            name="index_health",
            status="warn",
            message=(
                f"Index at {status.get('path')} is corrupt or unreadable "
                f"({status.get('error')}); memory_search falls back to a "
                f"linear scan."
            ),
            fix_hint=fix,
            details=details,
        )
    if status.get("needs_rebuild"):
        return Diagnosis(
            name="index_health",
            status="warn",
            message=(
                "Index is rebuild-pending after a schema upgrade; "
                "memory_search bypasses it (linear scan) until it is "
                "rebuilt."
            ),
            fix_hint=fix,
            details=details,
        )
    # Everything above came from meta reads alone — a torn interior
    # page passes those gates with clean counts, and the runtime never
    # notices until a query lands on the damaged pages (an FTS MATCH,
    # the next rebuild's table sweep) and raises. Walk the pages for
    # real before certifying anything healthy.
    integrity_error = _probe_index_integrity(index.index_path(directory))
    if integrity_error is not None:
        details["quick_check"] = integrity_error
        return Diagnosis(
            name="index_health",
            status="warn",
            message=(
                f"Index at {status.get('path')} fails PRAGMA quick_check "
                f"({integrity_error}) — page-level corruption the "
                f"meta-only runtime checks cannot see."
            ),
            fix_hint=fix,
            details=details,
        )
    details["quick_check"] = "ok"
    indexed_count = int(status.get("indexed_count", 0) or 0)
    if indexed_count == disk_count:
        return Diagnosis(
            name="index_health",
            status="ok",
            message=(
                f"Index healthy: {indexed_count} memories indexed "
                f"(matches disk; PRAGMA quick_check passed)."
            ),
            details=details,
        )
    # Raw counts diverged — refine with the parse walk (parse_health
    # already walks every file, so no new cost class for doctor).
    # `disk - unparseable` is the highest count a rebuild can reach.
    try:
        unparseable_count = count_unparseable_memory_files(directory)
    except OSError as exc:
        return Diagnosis(
            name="index_health",
            status="fail",
            message=f"Could not count memory files: {exc}.",
            fix_hint=f"Check permissions on {directory}.",
        )
    details["unparseable_count"] = unparseable_count
    indexable_count = disk_count - unparseable_count
    if indexed_count == indexable_count:
        # As synced as a rebuild can make it. The unparseable files are
        # a real problem, but they're memory_parse_health's finding —
        # warning here would prescribe a reindex that can never clear.
        return Diagnosis(
            name="index_health",
            status="ok",
            message=(
                f"Index healthy: {indexed_count} memories indexed — matches "
                f"every parseable file on disk ({unparseable_count} "
                f"unparseable file(s) excluded; see memory_parse_health). "
                f"PRAGMA quick_check passed."
            ),
            details=details,
        )
    unparseable_note = (
        f" {unparseable_count} of the disk files are unparseable and will "
        f"never index; expect index={indexable_count} after the rebuild."
        if unparseable_count
        else ""
    )
    return Diagnosis(
        name="index_health",
        status="warn",
        message=(
            f"Index out of sync with disk (index={indexed_count}, "
            f"disk={disk_count}) — a memory file was likely "
            f"added/edited outside the Store API.{unparseable_note}"
        ),
        fix_hint=fix,
        details=details,
    )


def _check_event_log_writable(directory: Path) -> Diagnosis:
    """The event log writer creates the file lazily; we probe-append
    to confirm we'd be allowed to."""
    if not directory.exists():
        return Diagnosis(
            name="event_log",
            status="ok",
            message="Event log not yet created (storage dir is brand new).",
        )
    log_path = directory / EVENT_LOG_FILENAME
    if not log_path.exists():
        # Probe writability of the directory itself; the log file will
        # be created on first server start.
        if os.access(directory, os.W_OK):
            return Diagnosis(
                name="event_log",
                status="ok",
                message="Event log not yet created (will appear on first server start).",
            )
        return Diagnosis(
            name="event_log",
            status="fail",
            message=f"Event log at {log_path} cannot be created (directory not writable).",
            fix_hint="Fix the storage directory permissions.",
        )

    if not os.access(log_path, os.W_OK):
        return Diagnosis(
            name="event_log",
            status="fail",
            message=f"Event log at {log_path} is not writable.",
            # shlex.quote: a raw interpolation shell-splits on a
            # space-bearing storage path (the macOS `Application
            # Support` neighbourhood) and can chmod an innocent sibling
            # on a glob-bearing one — the same executes-verbatim
            # contract `_quoted_literal_pathspecs` holds for the
            # pathspec hints.
            fix_hint=f"`chmod u+w {shlex.quote(str(log_path))}`.",
        )
    size = log_path.stat().st_size
    return Diagnosis(
        name="event_log",
        status="ok",
        message=f"Event log writable ({size} bytes).",
        details={"path": str(log_path), "bytes": size},
    )


def _check_audit_turn_cadence(directory: Path) -> Diagnosis:
    """Detect a silently no-opping Stop hook.

    The plugin ships `hooks/hooks.json` declaring a Stop binding that
    invokes `bettermemory audit-turn`. When that hook fires, the
    server records a `turn_audited` event per turn. Without the hook
    (or with a hook wired to a stale binary path, a sandbox that
    blocks `~/.local/bin`, a settings.json typo, etc.) the server
    still records other events from in-session tool calls but
    `turn_audited` is silent.

    Heuristic: over the last 7 days, if the event log shows at least
    two distinct sessions with zero `turn_audited` events, warn. Soft
    warning, not a fatal error — a user who deliberately doesn't run
    the hook (CI run, a one-off bulk-ingest session, a probe via the
    programmatic client) has the same shape and we don't want to
    spam them. The ≥2-session floor (Round-3 fix-up) kills the
    false-positive that fired for low-cadence users: a once-a-week
    Claude Code user had exactly one session in the 7-day window
    every check, so the old `n_sessions > 0` predicate fired the
    warning even with a perfectly-working hook (the next session
    hadn't happened yet, so there was no "Stop event that should
    have produced turn_audited but didn't"). Requiring two sessions
    means we've seen at least one session END (the Stop hook's
    trigger) without the corresponding turn_audited row, which is
    the real signal.
    """
    if not directory.exists():
        return Diagnosis(
            name="audit_turn_cadence",
            status="ok",
            message="Event log not yet created — skipping audit-turn check.",
        )
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    sessions: set[str] = set()
    turn_audited = 0
    total_events = 0
    try:
        for event in iter_all_events(directory):
            ts_raw = event.get("ts")
            if not isinstance(ts_raw, str):
                continue
            try:
                # `_utcnow_iso` writes `…Z`; fromisoformat handles `+00:00`
                # but the trailing-Z form needs a tiny normalization.
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
            total_events += 1
            session = event.get("session")
            if isinstance(session, str) and session:
                sessions.add(session)
            if event.get("kind") == "turn_audited":
                turn_audited += 1
    except OSError as exc:
        return Diagnosis(
            name="audit_turn_cadence",
            status="warn",
            message=f"Could not read event log to check audit cadence: {exc}.",
        )

    n_sessions = len(sessions)
    info: dict[str, Any] = {
        "window_days": 7,
        "sessions": n_sessions,
        "turn_audited_events": turn_audited,
        "total_events": total_events,
    }

    if total_events == 0:
        return Diagnosis(
            name="audit_turn_cadence",
            status="ok",
            message="No events in the last 7 days — nothing to check.",
            details=info,
        )
    if turn_audited == 0 and n_sessions >= 2:
        # Don't pretend we know the exact expected count — the cadence
        # depends on how often the user invokes Claude Code. "At least
        # N" is a useful order-of-magnitude where N is the session
        # count (a turn produces one Stop event, but a session
        # produces many turns — N is a lower bound).
        return Diagnosis(
            name="audit_turn_cadence",
            status="warn",
            message=(
                f"Your Stop hook may be silently no-opping — expected at "
                f"least {n_sessions} audit-turn events given "
                f"{n_sessions} session(s) in the last 7 days, found 0."
            ),
            fix_hint=(
                "Check `~/.claude/settings.json` (or your hooks config) "
                "for a Stop binding to `bettermemory audit-turn`. The "
                "plugin's `hooks/hooks.json` does this automatically "
                "when the plugin is installed; manual setups need to "
                "wire it themselves."
            ),
            details=info,
        )
    if turn_audited == 0 and n_sessions == 1:
        # Exactly one session in the window: either the user is a
        # low-cadence Claude Code user (weekly-or-less) and the next
        # session simply hasn't happened yet, or this IS the broken
        # hook but we haven't seen enough sessions to be sure. Either
        # way, an "ok" verdict with the count surfaced is the right
        # response — the user can run `doctor` after their next
        # session to get a definitive signal.
        return Diagnosis(
            name="audit_turn_cadence",
            status="ok",
            message=(
                "Only 1 session in the last 7 days — not enough cadence "
                "data to verify the audit-turn hook fires. Re-run "
                "`bettermemory doctor` after at least one more session."
            ),
            details=info,
        )
    return Diagnosis(
        name="audit_turn_cadence",
        status="ok",
        message=(
            f"{turn_audited} `turn_audited` event(s) across {n_sessions} "
            f"session(s) in the last 7 days."
        ),
        details=info,
    )


def _check_auto_memory_stranded(directory: Path, cwd: Path | None = None) -> Diagnosis:
    """Detect Claude Code auto-memory files for this cwd that never
    made it into the store.

    Claude Code's filesystem auto-memory
    (``~/.claude/projects/<sanitized-cwd>/memory/``) accumulates facts
    bettermemory retrieval never sees — exactly the fragmentation the
    instructions block warns against. ``bettermemory ingest`` imports
    them, but discovery of *whether stranded files exist* was manual.

    A source counts as stranded only when ``compute_ingest_plan``
    classifies it as a fresh ``write`` AND ingest's provenance watermark
    has no matching record for its current content. The watermark is the
    load-bearing half: ingest never mutates the source files, so a bare
    file count would warn forever after a successful import, and the
    dedup classification alone is not durable either — a routine
    ``memory_update`` that substantively rewrites an imported memory (a
    curated rewrite) drops the body-Jaccard similarity back under the
    duplicate threshold, at which point the UNTOUCHED source re-classifies
    as a fresh write. Keying on the recorded content hash instead means an
    unchanged source stays "ingested" no matter how far its memory has
    since drifted; only genuinely-new or genuinely-changed-since-import
    files remain stranded (for which ``ingest`` is the honest fix, not a
    stale-body resurrection).
    """
    from .ingest import (
        compute_ingest_plan,
        discover_default_source_root,
        load_ingest_watermark,
        source_is_ingested,
    )

    source_root = discover_default_source_root(cwd)
    if source_root is None:
        return Diagnosis(
            name="auto_memory_stranded",
            status="ok",
            message="No Claude Code auto-memory directory for this cwd.",
            details={"source_root": None},
        )
    # Mirror `_check_memory_parse_health`'s guard: never construct a
    # Store against a missing path from a read-only probe (its
    # __post_init__ mkdirs).
    if not directory.exists():
        return Diagnosis(
            name="auto_memory_stranded",
            status="ok",
            message="Storage dir does not exist yet — nothing to compare.",
            details={"source_root": str(source_root)},
        )
    try:
        store = Store(directory)
        plan = compute_ingest_plan(
            source_root,
            existing_memories=store.load_all(),
            existing_tombstones=store.load_tombstones(),
        )
    except FileNotFoundError:
        # Discovery raced a deletion of the source dir; nothing stranded.
        return Diagnosis(
            name="auto_memory_stranded",
            status="ok",
            message="No Claude Code auto-memory directory for this cwd.",
            details={"source_root": None},
        )
    # A plan `write` row means "not a current duplicate/tombstone" — but
    # that classification drifts as imported memories are curated, so it
    # cannot stand alone. Suppress any write whose source content still
    # matches the ingest watermark: those bytes were imported and haven't
    # changed, so the file is not stranded no matter how far its memory
    # has drifted. Only genuinely-new or genuinely-changed sources survive.
    watermark = load_ingest_watermark(directory)
    stranded_rows = [
        row
        for row in plan.rows
        if row.action == "write" and not source_is_ingested(row.source_path, watermark)
    ]
    would_write = len(stranded_rows)
    details = {
        "source_root": str(source_root),
        "summary": plan.summary,
        "stranded": would_write,
    }
    if would_write == 0:
        return Diagnosis(
            name="auto_memory_stranded",
            status="ok",
            message=(
                f"Auto-memory directory present ({source_root}); nothing un-ingested."
            ),
            details=details,
        )
    plural = "" if would_write == 1 else "s"
    verb = "has" if would_write == 1 else "have"
    return Diagnosis(
        name="auto_memory_stranded",
        status="warn",
        message=(
            f"{would_write} Claude Code auto-memory file{plural} under "
            f"{source_root} {verb} not been ingested (new, or changed since "
            "the last import) — facts stored there are invisible to "
            "bettermemory retrieval."
        ),
        fix_hint=(
            "Run `bettermemory ingest --dry-run` to preview the import, "
            "then `bettermemory ingest` to commit it."
        ),
        details=details,
    )


def _quoted_literal_pathspecs(paths: list[str]) -> str:
    """Render tracked paths as shell-quoted ``:(literal)`` pathspecs
    for a copy-pasteable ``git rm --cached`` remediation hint.

    Joining the raw paths hands the user's shell and git's pathspec
    engine exactly the hazards the index scan itself guards against
    (verified on git 2.50.1): a leading-``:`` path parses as the
    pathspec-magic marker and ``git rm`` aborts rc=128 without
    untracking anything, an embedded space shell-splits into two bogus
    pathspecs, and a glob metacharacter silently untracks a DIFFERENT
    tracked path (``st[a]re/…`` matches the innocent sibling
    ``stare/…`` and exits 0) — a command that fails or damages the
    wrong entries, handed to someone remediating a secret leak.
    ``shlex.quote`` on the composite ``:(literal)<path>`` string closes
    the one hole a fixed ``'…'`` wrap left open: a path with an
    embedded ``'`` terminated the quoted span early and left the
    command quote-imbalanced, so pasting it into a POSIX shell died on
    a syntax error ("unexpected EOF while looking for matching `'`")
    without untracking anything. Quote-free paths keep the exact
    single-quoted form the wrap produced — the ``(`` in the magic
    prefix is never in ``shlex.quote``'s safe set, so the surrounding
    quotes are always emitted — and only a quote-bearing path switches
    to the ``'"'"'`` escape idiom.
    """
    return " ".join(shlex.quote(f":(literal){path}") for path in paths)


def _pattern_matches_tracked_path(path: str, pattern: str) -> bool:
    """Does one ``sync._GITIGNORE_LINES`` pattern ignore this tracked
    path, by gitignore's rules?

    For the shape the structural guard in test_sync.py pins the list to
    (positive, slash-free lines), a gitignore pattern matches a file or
    directory NAME at any depth, and a matching DIRECTORY name ignores
    everything beneath it — so the pattern matches ``path`` iff ANY
    ``/``-separated component fnmatches it. That per-component walk is
    complete as well as correct for this pattern shape: with no
    ``/``-anchored or ``!``-negated lines possible, there is nothing
    else gitignore semantics could consult. Fnmatching the WHOLE path
    instead (the pre-fix spelling at both call sites) diverged in both
    directions: ``*`` crossed ``/`` (``.embeddings.*.npz`` matched the
    legitimately tracked ``.embeddings.cache/model.npz``, which git
    does NOT ignore, so the caller handed its owner the untrack +
    history-rewrite + secret-rotation hint — a destructive false
    positive), and paths UNDER an ignored directory matched nothing
    (``snapshots.tmp/file.md`` — a real leak git ignores wholesale —
    was silently missed). The parity test in test_doctor.py holds this
    helper against ``git check-ignore`` as the oracle for both
    directions. Case handling stays ``fnmatch.fnmatch``'s platform
    default, exactly as both call sites always had it (git's own ignore
    matching honours ``core.ignorecase``, so a tracked path whose CASE
    alone diverges from a pattern on a case-insensitive filesystem
    keeps its established verdict rather than silently changing here).
    """
    return any(
        fnmatch.fnmatch(component, pattern) for component in PurePosixPath(path).parts
    )


def _check_sync_tracked_ignored(directory: Path) -> Diagnosis:
    """Detect transient sidecar files a store sync repo still TRACKS.

    ``sync.init()`` refreshes the store's ``.gitignore`` on every run,
    but gitignore only stops FUTURE staging — a repo initialised before
    a pattern joined ``sync._GITIGNORE_LINES`` keeps the matching files
    tracked, so every later ``sync push`` (``git add -A``) commits and
    pushes their current contents to the remote. For the plaintext
    payloads in that list (the raw-capture proposals queue, the ingest
    watermark, the consolidate clock, orphaned ``*.tmp`` atomic-write
    bodies) that is an ongoing leak, not a one-off: the gitignore
    refresh cannot untrack a file, so only ``git rm --cached`` stops
    it, and anything already pushed sits in remote history until a
    rewrite. This check is the migration surface the ``sync.init()``
    comment points at; a non-repo store (or a repo with zero tracked
    matches) passes.
    """
    # Lazy import — `sync` imports `DOCTOR_PROBE_FILENAME` from this
    # module at import time, so a top-level `from .sync import …` here
    # would be circular. Same pattern as `_check_index_health`'s `index`
    # import. `_GITIGNORE_LINES` stays the single canonical pattern
    # list; duplicating it here would let the two drift.
    from .sync import _GITIGNORE_LINES, _is_repo, _run_git

    if not directory.exists():
        return Diagnosis(
            name="sync_tracked_ignored",
            status="ok",
            message="Storage dir does not exist yet — no sync repo to check.",
        )
    # `_is_repo` is top-of-worktree-only (a store nested inside some
    # parent repo is not a sync repo — that nested shape is
    # `store_nested_in_parent_repo`'s finding, not this check's) and
    # returns False when git itself is missing — both degrade to
    # "nothing to check", matching the sync wrapper's own notion of an
    # initialised store.
    if not _is_repo(directory):
        return Diagnosis(
            name="sync_tracked_ignored",
            status="ok",
            message="Store is not a git sync repo — nothing to check.",
        )
    listing = _run_git(directory, ["ls-files", "-z"], check=False)
    if listing.returncode != 0:
        return Diagnosis(
            name="sync_tracked_ignored",
            status="warn",
            message=(
                "Could not list git-tracked files in the store repo: "
                f"{listing.stderr.strip() or listing.stdout.strip() or 'unknown error'}."
            ),
            fix_hint=f"Run `git ls-files` in {directory} to investigate.",
        )
    # `_GITIGNORE_LINES` is comment lines + positive glob/literal
    # patterns (no `!` negations, no `/`-anchored paths — the structural
    # guard in test_sync.py keeps it that way).
    patterns = [
        line for line in _GITIGNORE_LINES if line and not line.lstrip().startswith("#")
    ]
    tracked: list[str] = []
    for path in listing.stdout.split("\0"):
        if not path:
            continue
        # A slash-free gitignore pattern matches a NAME at any depth —
        # per path component, never whole-path fnmatch (see
        # `_pattern_matches_tracked_path` for why that distinction is
        # load-bearing in both directions).
        if any(_pattern_matches_tracked_path(path, pattern) for pattern in patterns):
            tracked.append(path)
    if not tracked:
        return Diagnosis(
            name="sync_tracked_ignored",
            status="ok",
            message=(
                "No transient sidecar files are git-tracked in the store sync repo."
            ),
            details={"patterns_checked": len(patterns)},
        )
    shown = ", ".join(tracked[:3])
    if len(tracked) > 3:
        shown += f", … (+{len(tracked) - 3} more)"
    plural = "" if len(tracked) == 1 else "s"
    verb = "is" if len(tracked) == 1 else "are"
    cmd_paths = _quoted_literal_pathspecs(tracked[:5])
    more_note = (
        f" (+{len(tracked) - 5} more — full list in `bettermemory doctor --json`)"
        if len(tracked) > 5
        else ""
    )
    return Diagnosis(
        name="sync_tracked_ignored",
        status="fail",
        message=(
            f"{len(tracked)} transient sidecar file{plural} ({shown}) {verb} "
            f"git-TRACKED in the store sync repo at {directory}. The "
            f".gitignore refresh only stops future staging — every "
            f"`sync push` keeps committing and pushing these files (which "
            f"can carry plaintext captures) until they are untracked."
        ),
        fix_hint=(
            f"From {directory} run `git rm --cached {cmd_paths}`{more_note} "
            "and commit, so the next `sync push` stops shipping them. If "
            "the repo was ever pushed, the contents are already in remote "
            "history: rewrite it with git-filter-repo (or BFG) and "
            "force-push, then rotate any secrets those files may have "
            "captured. On a multi-host store, untrack on EVERY host before "
            "its next `sync pull` — pulling another host's untrack commit "
            "deletes your tracked working copies of these paths from disk "
            "(copy them aside first if in doubt)."
        ),
        details={"tracked_ignored": tracked},
    )


def _enclosing_worktree_levels(
    start_dir: Path, store_root: Path, *, seen: set[Path]
) -> list[tuple[Path, str]]:
    """Walk upward from ``start_dir`` toward the filesystem root and
    collect EVERY enclosing git worktree that path-contains
    ``store_root``, innermost first, as ``(toplevel, prefix)`` pairs.

    One ``rev-parse --show-toplevel`` probe discovers the innermost
    repo at or above the probe directory; the walk then restarts from
    that toplevel's parent directory, so a doubly-nested chain (store
    inside repo A inside grandparent B) yields BOTH A and B — git never
    auto-untracks, so the stale-index sidecar leak the innermost scan
    catches exists at every additional nesting level. Termination is
    bounded by path depth: every accepted toplevel must be a strict
    ancestor of ``store_root`` (there are finitely many), ``seen``
    rejects re-discovering a toplevel (GIT_DIR / GIT_WORK_TREE
    overrides can make a probe answer somewhere it did not come from),
    and the walk stops cleanly at the filesystem root, on the first
    probe that finds no repo, or on an answer that does not
    path-contain the store.
    """
    # Same lazy-import rationale as `_check_sync_tracked_ignored`: a
    # top-level `from .sync import …` here would be circular.
    from .sync import SyncError, _run_git

    levels: list[tuple[Path, str]] = []
    probe_dir = start_dir
    while True:
        try:
            probe = _run_git(probe_dir, ["rev-parse", "--show-toplevel"], check=False)
        except (SyncError, OSError):
            # git itself missing, or the probe directory vanished /
            # unreadable mid-walk — no further enclosing repo to scan.
            break
        if probe.returncode != 0:
            break
        top = Path(probe.stdout.strip()).resolve()
        if top == store_root or top in seen:
            break
        try:
            prefix = store_root.relative_to(top).as_posix()
        except ValueError:
            break
        seen.add(top)
        levels.append((top, prefix))
        if top.parent == top:
            # Filesystem root: nowhere further up to probe from.
            break
        probe_dir = top.parent
    return levels


def _scan_parent_index_for_sidecars(
    directory: Path,
    *,
    levels: list[tuple[Path, str, str]],
    clean_message: str,
) -> Diagnosis:
    """Shared tail of ``_check_store_nested_in_parent_repo``: for each
    enclosing-repo level ``(parent_top, prefix, leak_route)`` —
    innermost first, non-empty — list what the repo at ``parent_top``
    tracks under the store's ``prefix`` and match each row's
    STORE-relative remainder per path component
    (``_pattern_matches_tracked_path``) against
    ``sync._GITIGNORE_LINES``. The prefix is passed with git's
    ``:(literal)`` pathspec magic: a raw pathspec hands glob
    metacharacters and the leading-``:`` magic marker in a legal store
    path straight to git's pathspec engine, which can silently list
    nothing (a false negative in the exact leak class this check
    closes — a store under ``:dir`` parses as magic-prefixed) or match
    entries OUTSIDE the store (a trailing-``*`` segment fnmatches
    across ``/`` into sibling directories). Zero matching hits across
    every level returns the caller's ``clean_message`` ok — the WARN
    keys strictly on actually-tracked sidecar paths, never on mere
    nesting, because every caller shape (a plain nested subdir, and a
    store-as-own-repo inside a monorepo / home repo, at any nesting
    depth) is a legitimate setup when the enclosing indexes are clean.
    Hits aggregate into ONE warn: a single offending parent keeps the
    established single-repo message shape, several offending parents
    are each named with their tracked paths. Each level's
    ``leak_route`` is the one warn sentence explaining WHY that parent
    still ships the files: for the plain-nested shape no bettermemory
    ``.gitignore`` applies in the parent; for the combined shape (and
    every outer level) the parent tracked the paths before the nesting
    below it arose and git does not auto-untrack.
    """
    # Same lazy-import rationale as `_check_sync_tracked_ignored`:
    # `sync` imports `DOCTOR_PROBE_FILENAME` from this module at import
    # time, so a top-level `from .sync import …` would be circular, and
    # `_GITIGNORE_LINES` must stay the single canonical pattern list.
    from .sync import _GITIGNORE_LINES, _run_git

    # Same pattern handling as `_check_sync_tracked_ignored`: comment
    # lines out, positive slash-free glob/literal patterns in, matched
    # per path component — but the ls-files rows here are
    # TOPLEVEL-relative (e.g. `memory-store/.write_proposals.jsonl`),
    # so each row is re-framed to its store-relative remainder before
    # matching (see the loop below for why the prefix components must
    # stay out of the match).
    patterns = [
        line for line in _GITIGNORE_LINES if line and not line.lstrip().startswith("#")
    ]
    hits: list[tuple[Path, str, str, list[str]]] = []
    first_error: Diagnosis | None = None
    for parent_top, prefix, leak_route in levels:
        listing = _run_git(
            parent_top,
            ["ls-files", "-z", "--", f":(literal){prefix}"],
            check=False,
        )
        if listing.returncode != 0:
            if first_error is None:
                first_error = Diagnosis(
                    name="store_nested_in_parent_repo",
                    status="warn",
                    message=(
                        f"Store is nested inside the git repo at {parent_top}, but "
                        "listing what that repo tracks under the store failed: "
                        f"{listing.stderr.strip() or listing.stdout.strip() or 'unknown error'}."
                    ),
                    fix_hint=(
                        f"Run `git ls-files -- "
                        f"{shlex.quote(f':(literal){prefix}')}` from "
                        f"{parent_top} to investigate."
                    ),
                    details={
                        "parent_toplevel": str(parent_top),
                        "store_prefix": prefix,
                    },
                )
            continue
        tracked: list[str] = []
        for path in listing.stdout.split("\0"):
            if not path:
                continue
            # The patterns' authoritative frame is the STORE root, but
            # ls-files rows are TOPLEVEL-relative — matching the row
            # verbatim let the store PREFIX components trip a pattern:
            # a store directory literally named `state.tmp` (or any
            # intermediate component between the parent toplevel and
            # the store) made every tracked file beneath it —
            # legitimate memory bodies included — report as a transient
            # sidecar, with an untrack + history-rewrite hint that
            # never converges (the parent re-tracks the memories on its
            # next `git add -A`). Match only the store-relative
            # remainder — the same frame the store's own .gitignore
            # (and the check-ignore parity oracle in test_doctor.py)
            # speaks.
            if not path.startswith(prefix + "/"):
                # Inside this pathspec the only row NOT under
                # `prefix + "/"` is one EQUAL to the prefix: the
                # store-absorbed-as-submodule gitlink shape (a single
                # mode-160000 index entry for the store itself, no
                # files beneath). That entry IS the store, not a
                # sidecar under it — skip.
                continue
            if any(
                _pattern_matches_tracked_path(path[len(prefix) + 1 :], pattern)
                for pattern in patterns
            ):
                tracked.append(path)
        if tracked:
            hits.append((parent_top, prefix, leak_route, tracked))
    # Only noted when the walk actually found more than one enclosing
    # repo, so single-nesting diagnoses keep their established shape.
    scanned_note: dict[str, Any] = (
        {"scanned_parent_toplevels": [str(top) for top, _, _ in levels]}
        if len(levels) > 1
        else {}
    )
    if not hits:
        if first_error is not None:
            return first_error
        parent_top, prefix, _ = levels[0]
        return Diagnosis(
            name="store_nested_in_parent_repo",
            status="ok",
            message=clean_message,
            details={
                "parent_toplevel": str(parent_top),
                "store_prefix": prefix,
                "patterns_checked": len(patterns),
                **scanned_note,
            },
        )
    if len(hits) == 1:
        parent_top, prefix, leak_route, tracked = hits[0]
        shown = ", ".join(tracked[:3])
        if len(tracked) > 3:
            shown += f", … (+{len(tracked) - 3} more)"
        plural = "" if len(tracked) == 1 else "s"
        verb = "is" if len(tracked) == 1 else "are"
        cmd_paths = _quoted_literal_pathspecs(tracked[:5])
        more_note = (
            f" (+{len(tracked) - 5} more — full list in `bettermemory doctor --json`)"
            if len(tracked) > 5
            else ""
        )
        return Diagnosis(
            name="store_nested_in_parent_repo",
            status="warn",
            message=(
                f"{len(tracked)} transient sidecar file{plural} ({shown}) under "
                f"the store at {directory} {verb} git-TRACKED by the PARENT "
                f"repo at {parent_top}. {leak_route} the parent's own "
                f"`git add -A` / commit / push flows keep shipping these files "
                f"(which can carry plaintext captures) to wherever that repo "
                f"pushes."
            ),
            fix_hint=(
                f"In {parent_top}/.gitignore ignore the store's transient "
                f"sidecars (the patterns `bettermemory sync init` writes to a "
                f"store .gitignore, scoped under `{prefix}/`), then from "
                f"{parent_top} run `git rm --cached {cmd_paths}`{more_note} and "
                "commit so the parent stops tracking them. If the parent repo "
                "was ever pushed, the contents are already in remote history: "
                "rewrite it with git-filter-repo (or BFG) and force-push, then "
                "rotate any secrets those files may have captured."
            ),
            details={
                "parent_toplevel": str(parent_top),
                "store_prefix": prefix,
                "tracked_sidecars": tracked,
                **scanned_note,
            },
        )
    # Two or more enclosing repos track sidecars — ONE aggregated WARN
    # naming each offending parent toplevel with its tracked paths.
    total = sum(len(tracked) for _, _, _, tracked in hits)
    per_repo_msgs: list[str] = []
    per_repo_cmds: list[str] = []
    for parent_top, _prefix, _leak_route, tracked in hits:
        shown = ", ".join(tracked[:3])
        if len(tracked) > 3:
            shown += f", … (+{len(tracked) - 3} more)"
        per_repo_msgs.append(f"{parent_top} tracks {shown}")
        cmd_paths = _quoted_literal_pathspecs(tracked[:5])
        more_note = (
            f" (+{len(tracked) - 5} more — full list in `bettermemory doctor --json`)"
            if len(tracked) > 5
            else ""
        )
        per_repo_cmds.append(
            f"from {parent_top} run `git rm --cached {cmd_paths}`{more_note}"
        )
    return Diagnosis(
        name="store_nested_in_parent_repo",
        status="warn",
        message=(
            f"{total} transient sidecar files under the store at {directory} "
            f"are git-TRACKED by {len(hits)} enclosing PARENT repos: "
            + "; ".join(per_repo_msgs)
            + ". Git does not auto-untrack at any nesting level, so each "
            "parent's own `git add -A` / commit / push flows keep shipping "
            "these files (which can carry plaintext captures) to wherever "
            "that repo pushes."
        ),
        fix_hint=(
            "In each parent repo's .gitignore ignore the store's transient "
            "sidecars (the patterns `bettermemory sync init` writes to a "
            "store .gitignore, scoped under the store's path within that "
            "repo), then untrack them: "
            + "; ".join(per_repo_cmds)
            + "; commit in each parent so they stop tracking the files. If "
            "a parent repo was ever pushed, the contents are already in "
            "remote history: rewrite it with git-filter-repo (or BFG) and "
            "force-push, then rotate any secrets those files may have "
            "captured."
        ),
        details={
            "parent_toplevels": [str(top) for top, _, _, _ in hits],
            "tracked_by_parent": {str(top): tracked for top, _, _, tracked in hits},
            **scanned_note,
        },
    )


def _check_store_nested_in_parent_repo(directory: Path) -> Diagnosis:
    """Detect a store directory nested INSIDE a foreign parent git repo.

    ``sync._is_repo`` is deliberately top-of-worktree-only, so a store
    that merely sits inside some parent repo's worktree (e.g. a home
    directory managed as a dotfiles repo) is "not a sync repo" to the
    sync wrapper — and therefore invisible to ``sync_tracked_ignored``,
    which owns the store-IS-the-repo case. But the PARENT repo's own
    ``git add -A`` / commit / push flows can track and ship the exact
    same plaintext sidecars (the raw-capture proposals queue, the
    ingest watermark, the consolidate clock, orphaned ``*.tmp``
    atomic-write bodies) with no bettermemory-written ``.gitignore`` in
    the way — the one sidecar-leak route neither sync nor the sibling
    check can see. This check names the parent worktree's toplevel and
    lists any paths under the store that the parent actually TRACKS
    matching ``sync._GITIGNORE_LINES``. It never fails: a nested store
    inside a local-only parent repo is a legitimate setup, so a tracked
    sidecar is a WARN with the parent-side remediation, and a clean
    nested store is ok with the nesting noted.

    The COMBINED shape is covered too: a store that began as a plain
    subdirectory of such a parent — whose index tracked the store's
    sidecars — and only LATER ran ``bettermemory sync init``, becoming
    its own worktree toplevel. ``rev-parse --show-toplevel`` from
    inside the store then answers with the store itself, so the upward
    probe restarts from the store's parent DIRECTORY; the parent's
    stale index entries survive the nested ``git init`` (git does not
    auto-untrack), so the parent keeps shipping those blobs while
    ``sync_tracked_ignored`` — which reads only the STORE repo's index
    — sees nothing. Because a store-as-own-repo nested inside a
    monorepo / home repo with ZERO tracked sidecars is a perfectly
    normal sync setup, that branch WARNs strictly on actually-tracked
    matching paths in the PARENT index — mere nesting never alarms.

    Neither probe stops at the innermost enclosing repo: the same
    stale-index mechanism exists at EVERY additional nesting level
    (store inside repo A inside grandparent B whose index tracked
    pre-init paths under the store — B keeps shipping them while both
    the store repo and A look clean). The upward walk continues past
    each discovered toplevel toward the filesystem root, scans every
    enclosing repo's index, and aggregates per-repo hits into the one
    WARN.

    A FAILED initial probe does not stand down either: git discovery
    aborts rc=128 at a broken ``.git`` gitfile in the store (a
    dangling ``gitdir:`` target after the linked worktree's main repo
    was deleted or moved, or garbage content from a backup tool that
    restored the store without the admin dir) WITHOUT continuing
    upward, so "probe failed" does not mean "no enclosing repo".
    ``sync_tracked_ignored`` stands down on the same broken probe
    (``_is_repo`` is False — correct, the store is not a working sync
    repo), while a healthy enclosing parent still tracks the store's
    sidecars and its ``git add -A`` still stages NEW ones (git only
    treats the subdirectory as a foreign-repo boundary when its
    ``.git`` VALIDATES — a broken one is traversed like any plain
    directory). The probe-failure branch therefore restarts discovery
    from the store's parent directory — which never enters the store's
    broken ``.git`` — and only returns the quiet
    "not inside any git worktree" ok when that walk finds nothing.
    """
    # Same lazy-import rationale as `_check_sync_tracked_ignored` (and
    # the `_scan_parent_index_for_sidecars` helper): a top-level `sync`
    # import would be circular.
    from .sync import SyncError, _run_git

    if not directory.exists():
        return Diagnosis(
            name="store_nested_in_parent_repo",
            status="ok",
            message="Storage dir does not exist yet — no nesting to check.",
        )
    try:
        toplevel_probe = _run_git(
            directory, ["rev-parse", "--show-toplevel"], check=False
        )
    except SyncError:
        # git itself is missing — degrade to "nothing to check", the
        # same SyncError-to-False shape as `sync._is_repo`: without git
        # there is no parent repo committing anything.
        toplevel_probe = None
    not_inside = Diagnosis(
        name="store_nested_in_parent_repo",
        status="ok",
        message="Store is not inside any git worktree — nothing to check.",
    )
    if toplevel_probe is None:
        return not_inside
    store_root = directory.resolve()
    # Every level past the innermost shares one leak route: an outer
    # repo's index holds paths under the store from before the repo
    # nesting below it arose, and git does not auto-untrack them.
    outer_leak_route = (
        "The PARENT repo's index still tracks these paths from before "
        "the repo nesting below it arose — git does not auto-untrack — so"
    )
    if toplevel_probe.returncode != 0:
        # The probe failing from INSIDE the store proves nothing about
        # enclosing repos: discovery aborts at a broken `.git` gitfile
        # without ever probing upward (the docstring's dangling-worktree
        # / partial-restore shapes), so standing down here left a
        # healthy tracking parent invisible while its `git add -A` kept
        # staging NEW plaintext sidecars under the store. Restart
        # discovery from the store's parent DIRECTORY, exactly like the
        # own-toplevel branch below — the walk re-probes from above the
        # store, so it is safe for BOTH failure meanings: a store that
        # truly sits under no repo (a clean rc=128 at a plain
        # directory) finds nothing and keeps the quiet ok.
        if store_root.parent == store_root:
            # Filesystem root: no parent directory to probe from.
            return not_inside
        found = _enclosing_worktree_levels(store_root.parent, store_root, seen=set())
        if not found:
            return not_inside
        broken_probe_route = (
            "Git discovery from inside the store fails (typically a "
            "broken .git entry — e.g. a dangling worktree gitdir) "
            "without ever reaching that parent, whose index still "
            "tracks these paths, so"
        )
        levels = [
            (top, prefix, broken_probe_route if depth == 0 else outer_leak_route)
            for depth, (top, prefix) in enumerate(found)
        ]
        return _scan_parent_index_for_sidecars(
            directory,
            levels=levels,
            clean_message=(
                f"Store's own git discovery fails (broken .git entry?) "
                f"but it is nested inside the git repo at {found[0][0]}; "
                f"that parent repo tracks no transient sidecar files "
                f"under the store."
            ),
        )
    parent_top = Path(toplevel_probe.stdout.strip()).resolve()
    if parent_top == store_root:
        # The store is the top of its own worktree, so sidecars tracked
        # in the STORE repo are `sync_tracked_ignored`'s finding — but
        # ENCLOSING repos can still exist (the combined shape in the
        # docstring), and `rev-parse` from inside the store can only
        # ever answer with the store itself. Restart the probe from the
        # store's parent directory; anything short of a resolving,
        # store-containing enclosing worktree stands down exactly as
        # before the upward probe existed.
        stand_down = Diagnosis(
            name="store_nested_in_parent_repo",
            status="ok",
            message=(
                "Store is the top of its own git worktree — tracked "
                "sidecars there are sync_tracked_ignored's finding."
            ),
        )
        if store_root.parent == store_root:
            # Filesystem root: no parent directory to probe from.
            return stand_down
        found = _enclosing_worktree_levels(store_root.parent, store_root, seen=set())
        if not found:
            return stand_down
        own_top_route = (
            "The store is now the top of its own git worktree, but "
            "the PARENT repo's index still tracks these paths from "
            "before the store became its own repo — git does not "
            "auto-untrack — so"
        )
        levels = [
            (top, prefix, own_top_route if depth == 0 else outer_leak_route)
            for depth, (top, prefix) in enumerate(found)
        ]
        return _scan_parent_index_for_sidecars(
            directory,
            levels=levels,
            clean_message=(
                f"Store is the top of its own git worktree (tracked "
                f"sidecars there are sync_tracked_ignored's finding), "
                f"nested inside the git repo at {found[0][0]}; that "
                f"parent repo tracks no transient sidecar files under "
                f"the store."
            ),
        )
    try:
        prefix = store_root.relative_to(parent_top).as_posix()
    except ValueError:
        # `--show-toplevel` answered from somewhere the resolved store
        # path is not actually under (GIT_DIR / GIT_WORK_TREE overrides
        # in the environment); there is no path-nesting to report.
        return Diagnosis(
            name="store_nested_in_parent_repo",
            status="ok",
            message=(
                "Store is not path-nested under any git worktree — nothing to check."
            ),
        )
    levels = [
        (
            parent_top,
            prefix,
            "The store only sits inside that repo's worktree — "
            "bettermemory's sync .gitignore does not apply there — so",
        )
    ]
    if parent_top.parent != parent_top:
        levels.extend(
            (top, top_prefix, outer_leak_route)
            for top, top_prefix in _enclosing_worktree_levels(
                parent_top.parent, store_root, seen={parent_top}
            )
        )
    return _scan_parent_index_for_sidecars(
        directory,
        levels=levels,
        clean_message=(
            f"Store is nested inside the git repo at {parent_top} (not "
            "itself a sync repo); that parent repo tracks no transient "
            "sidecar files under the store."
        ),
    )


def _check_embeddings_extra(cfg: Config) -> Diagnosis:
    """If `behavior.semantic_dedup = true`, the embeddings extra has to
    be installed or write-time dedup silently falls back to Jaccard.
    The fallback is logged as a WARNING but a doctor pass surfaces the
    problem proactively."""
    if not cfg.behavior.semantic_dedup:
        return Diagnosis(
            name="embeddings_extra",
            status="ok",
            message="semantic_dedup disabled (no extras needed).",
        )
    try:
        import sentence_transformers  # noqa: F401  # pyright: ignore[reportMissingImports]
    except ImportError:
        return Diagnosis(
            name="embeddings_extra",
            status="fail",
            message=(
                "`semantic_dedup = true` in config but the `embeddings` "
                "extra is not installed; write-time dedup will fall back "
                "to Jaccard with a logged WARNING."
            ),
            fix_hint=(
                "Install the extra: `uv pip install -e .[embeddings]`, or "
                "set `semantic_dedup = false` in config.toml."
            ),
        )
    return Diagnosis(
        name="embeddings_extra",
        status="ok",
        message="semantic_dedup enabled and `sentence_transformers` importable.",
    )


def _binary_dist_version(binary: str) -> str | None:
    """Best-effort ``<binary> --version`` probe. Returns the trailing
    version token ("bettermemory 3.13.0" -> "3.13.0") or None when the
    binary can't be executed, times out, exits non-zero, or prints
    nothing. Only ever invoked on paths the user themselves registered
    as a bettermemory binary in an MCP client config (plus our own
    PATH-resolved binary), so executing it is the same trust decision
    the client makes on every spawn.
    """
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip() or proc.stderr.strip()
    if not out:
        return None
    return out.split()[-1] or None


def _check_mcp_client_configs() -> Diagnosis:
    """Scan known clients' MCP config files; report which ones reference
    bettermemory and whether the registered binary path matches the one
    we'd resolve from PATH right now.

    Mismatch is the most common "I reinstalled into a venv and now
    nothing works" failure mode — the client's config still points at
    the old binary path, which no longer exists. A path that exists but
    differs from the PATH resolution is only stale when it actually runs
    a DIFFERENT version — a dev-venv binary serving the same release as
    the uv-tool install is a deliberate topology, not drift, so that
    case degrades to ok instead of warning forever.
    """
    resolved_binary = find_binary()
    # Whether find_binary() pinned a real on-disk absolute path. When it
    # couldn't (PATH miss -> bare "bettermemory" fallback), we have no
    # canonical reference to judge staleness against, so a config holding a
    # correct absolute console-script path must NOT be flagged as "stale" —
    # the companion binary_on_path check already tells the user PATH is broken.
    resolved_is_real = (
        Path(resolved_binary).is_absolute() and Path(resolved_binary).exists()
    )
    findings: list[dict[str, Any]] = []
    stale_rows: list[str] = []
    stale_clients: list[str] = []
    same_version_clients: list[str] = []
    # The resolved binary's version is probed at most once, and only
    # when some config actually needs the comparison.
    resolved_version_memo: list[str | None] = []

    def _resolved_version() -> str | None:
        if not resolved_version_memo:
            resolved_version_memo.append(_binary_dist_version(resolved_binary))
        return resolved_version_memo[0]

    for client_name, getter in KNOWN_CLIENTS.items():
        for path in getter().paths:
            if not path.exists():
                continue
            try:
                text = path.read_text(encoding="utf-8")
                data = json.loads(text) if text.strip() else {}
            except (OSError, json.JSONDecodeError) as exc:
                findings.append(
                    {
                        "client": client_name,
                        "config_path": str(path),
                        "status": "unreadable",
                        "error": str(exc),
                    }
                )
                continue
            if not isinstance(data, dict):
                continue
            mcp = data.get("mcpServers")
            if not isinstance(mcp, dict):
                continue
            for entry_name, entry in mcp.items():
                if not isinstance(entry, dict):
                    continue
                command = entry.get("command")
                if not isinstance(command, str):
                    continue
                # The `uvx`/`uv bettermemory` runner shape (the plugin's
                # .mcp.json) names "uvx" as the command with "bettermemory" in
                # args, so the substring filter below misses it and doctor used
                # to report a healthy managed install as absent. uvx resolves
                # the binary dynamically — there is no static path to validate,
                # and the byte-path checks below assume `command` IS the binary
                # — so recognize it via the SHARED init recognizer (one
                # definition, so init and doctor cannot drift on which runner
                # shapes count as ours: bare, version-pinned
                # `bettermemory@latest` / `bettermemory==X`, `--from`,
                # `uv tool run`, and the Windows `uvx.exe` spelling) and
                # record it healthy.
                args = entry.get("args")
                if (
                    Path(command).stem.lower() in {"uvx", "uv"}
                    and isinstance(args, list)
                    and command_launches_bettermemory(command, args, resolved_binary)
                ):
                    findings.append(
                        {
                            "client": client_name,
                            "config_path": str(path),
                            "entry_name": entry_name,
                            "command": command,
                            "binary_exists": True,
                            "matches_resolved_binary": True,
                            "runner": Path(command).name,
                        }
                    )
                    continue
                # Doctor deliberately matches ANY bettermemory-pathed command
                # (broader than init's exact-launch gate) so it can diagnose
                # stale / different-install / version-mismatch entries too.
                if "bettermemory" not in command:
                    continue
                # Found a direct-command bettermemory entry. Check the binary path.
                exists_on_disk = Path(command).is_absolute() and Path(command).exists()
                matches = command == resolved_binary or (
                    not Path(command).is_absolute() and command == "bettermemory"
                )
                # Symlink-aware fallback: a `~/.local/bin/bettermemory`
                # symlink in the config and a
                # `~/.local/share/uv/tools/bettermemory/bin/bettermemory`
                # canonical install (the standard `uv tool install`
                # layout) compare unequal as strings but resolve to the
                # same inode. Only follow up the realpath when the
                # cheap string check missed AND the file actually
                # exists, so we don't burn syscalls on every healthy
                # match or every genuinely-broken stale path.
                if not matches and exists_on_disk:
                    try:
                        same_target = (
                            Path(command).resolve() == Path(resolved_binary).resolve()
                        )
                        if same_target:
                            matches = True
                    except OSError:
                        # `resolve()` can raise on broken symlinks etc.;
                        # leave `matches=False` and let the existing
                        # has_mismatch branch fire.
                        pass
                findings.append(
                    {
                        "client": client_name,
                        "config_path": str(path),
                        "entry_name": entry_name,
                        "command": command,
                        "binary_exists": exists_on_disk,
                        "matches_resolved_binary": matches,
                    }
                )
                if Path(command).is_absolute() and not exists_on_disk:
                    stale_rows.append(
                        f"{client_name} ({path}): {command} no longer exists"
                    )
                    stale_clients.append(client_name)
                elif not matches and resolved_is_real:
                    # Only call it a stale path when we have a real canonical
                    # binary to compare against; otherwise this is a PATH
                    # problem (binary_on_path covers it), not a stale config.
                    configured_version = (
                        _binary_dist_version(command) if exists_on_disk else None
                    )
                    resolved_version = (
                        _resolved_version() if configured_version else None
                    )
                    if (
                        configured_version is not None
                        and configured_version == resolved_version
                    ):
                        # Different install, same release — deliberate
                        # multi-install topology, not staleness.
                        findings[-1]["same_version"] = configured_version
                        same_version_clients.append(client_name)
                    else:
                        findings[-1]["version_mismatch"] = {
                            "configured": configured_version,
                            "resolved": resolved_version,
                        }
                        stale_rows.append(
                            f"{client_name} ({path}): {command} runs "
                            f"{configured_version or 'an unknown version'} but PATH "
                            f"resolves {resolved_version or 'an unknown version'} "
                            f"at {resolved_binary}"
                        )
                        stale_clients.append(client_name)

    if not findings:
        return Diagnosis(
            name="mcp_client_configs",
            status="warn",
            message=(
                "No MCP client config references bettermemory yet. The "
                "server is installed but no client knows about it."
            ),
            fix_hint=(
                "Run `bettermemory init --client claude-code` (or "
                "claude-desktop / cursor / continue) to register."
            ),
            details={"resolved_binary": resolved_binary, "findings": findings},
        )

    if stale_rows:
        distinct_stale = sorted(set(stale_clients))
        return Diagnosis(
            name="mcp_client_configs",
            status="warn",
            message="Stale MCP client binary path: " + "; ".join(stale_rows),
            fix_hint=(
                "Re-run "
                + " / ".join(
                    f"`bettermemory init --client {name}`" for name in distinct_stale
                )
                + " to refresh the command path. The init patch is idempotent."
            ),
            details={"resolved_binary": resolved_binary, "findings": findings},
        )

    if same_version_clients:
        distinct_same = sorted(set(same_version_clients))
        return Diagnosis(
            name="mcp_client_configs",
            status="ok",
            message=(
                f"{len(findings)} client config(s) reference bettermemory; "
                f"all paths match or run the same version as the resolved "
                f"binary (different install, same version: {', '.join(distinct_same)})."
            ),
            details={"resolved_binary": resolved_binary, "findings": findings},
        )

    return Diagnosis(
        name="mcp_client_configs",
        status="ok",
        message=f"{len(findings)} client config(s) reference bettermemory; all paths match.",
        details={"resolved_binary": resolved_binary, "findings": findings},
    )


def _check_stale_config_lockfiles() -> Diagnosis:
    """Detect the `<config>.lock` REGULAR FILE bettermemory 3.15.0 left next
    to client configs.

    Claude Code locks `~/.claude.json` with a proper-lockfile mkdir-style
    DIRECTORY lock at exactly that name, so the leftover file wedges the
    client's config persistence (its lock mkdir sees EEXIST; its stale-lock
    rmdir dies ENOTDIR) until the file is deleted. Doctor stays read-only:
    report and point at the fix — a re-run of `bettermemory init` heals it
    automatically. A DIRECTORY at that path is the client's own (live or
    stale) lock and is not ours to judge; a NON-empty regular file may be
    some other tool's lock with content; only the 0-byte regular-file
    artifact 3.15.0 actually produced is flagged.
    """
    stale: list[str] = []
    clients: list[str] = []
    for client_name, getter in KNOWN_CLIENTS.items():
        for path in getter().paths:
            lock = path.with_suffix(path.suffix + ".lock")
            try:
                if (
                    lock.is_file()
                    and not lock.is_symlink()
                    and lock.stat().st_size == 0
                ):
                    stale.append(str(lock))
                    clients.append(client_name)
            except OSError:
                continue
    if not stale:
        return Diagnosis(
            name="stale_config_lockfiles",
            status="ok",
            message="No stale bettermemory lockfiles next to client configs.",
        )
    distinct = sorted(set(clients))
    return Diagnosis(
        name="stale_config_lockfiles",
        status="warn",
        message=(
            "Stale bettermemory 3.15.0 lockfile(s) next to client config(s): "
            + ", ".join(stale)
            + ". A regular file at `<config>.lock` wedges Claude Code's own "
            "config lock (its stale-lock cleanup fails ENOTDIR), so the "
            "client cannot persist settings until it is removed."
        ),
        fix_hint=(
            "Re-run "
            + " / ".join(f"`bettermemory init --client {n}`" for n in distinct)
            + " (heals it automatically), or delete the file(s) by hand."
        ),
        details={"stale_lockfiles": stale},
    )


# Pattern for the iCloud-style duplicate that the canonical METADATA
# file commonly gets renamed to: "METADATA 2", "METADATA 3", …, or
# "METADATA copy", "METADATA copy 2". The detector treats either as a
# hint that the original was clobbered by a sync conflict.
_METADATA_DUP_RE = re.compile(r"^METADATA(?: \d+| copy(?: \d+)?)$")


def _discover_site_packages() -> list[Path]:
    """Return the active interpreter's site-packages directories.

    Wrapped as a module-level helper so tests can monkeypatch a tmp
    path in without having to fake `sysconfig`/`site` themselves.
    Returns absolute, deduplicated paths that exist on disk; an empty
    list if none can be located (which the caller treats as "nothing
    to check").
    """
    candidates: list[str] = []
    purelib = sysconfig.get_paths().get("purelib")
    if purelib:
        candidates.append(purelib)
    platlib = sysconfig.get_paths().get("platlib")
    if platlib:
        candidates.append(platlib)
    # `site.getsitepackages()` isn't available in every embedded
    # interpreter (e.g. some virtualenv-in-virtualenv chains); guard it.
    try:
        import site

        candidates.extend(site.getsitepackages())
    except Exception:  # noqa: BLE001
        pass

    # User-site (`pip install --user`) is a legitimate install path
    # whose dist-info dirs can also trip the `-32000` failure mode.
    # Modern venvs default `ENABLE_USER_SITE` to False, in which case
    # this is a no-op. Guard the call the same way as `getsitepackages`
    # for platforms where it would fail.
    try:
        import site

        if getattr(site, "ENABLE_USER_SITE", False):
            user_site = site.getusersitepackages()
            if user_site:
                candidates.append(user_site)
    except Exception:  # noqa: BLE001
        pass

    seen: set[str] = set()
    out: list[Path] = []
    for c in candidates:
        p = Path(c)
        try:
            resolved = str(p.resolve())
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if p.is_dir():
            out.append(p)
    return out


def _check_distinfo_metadata(site_packages: list[Path] | None = None) -> Diagnosis:
    """Detect `*.dist-info/` dirs missing the canonical `METADATA` file.

    iCloud Drive on macOS occasionally duplicates files inside a
    dependency's dist-info directory — the canonical `METADATA` file
    ends up only as `METADATA 2` (or `METADATA copy`, etc.) and the
    canonical name disappears. When that happens,
    `importlib.metadata.version("<pkg>")` returns None for that
    distribution. Inside the `mcp` library this trips a pydantic
    validation that crashes the MCP server with an opaque `-32000`
    reconnect failure — the user sees the server disconnect mid-
    session and nothing in the logs points at the root cause.
    The detector is a no-op fixer: it surfaces affected dist-info dirs
    so the user can rename the duplicate or re-install the package.
    """
    if site_packages is None:
        site_packages = _discover_site_packages()

    if not site_packages:
        return Diagnosis(
            name="distinfo_metadata",
            status="ok",
            message="No site-packages directories located — skipping dist-info check.",
        )

    broken: list[dict[str, Any]] = []
    scanned = 0
    for sp in site_packages:
        try:
            entries = list(sp.glob("*.dist-info"))
        except OSError:
            continue
        for dist_info in entries:
            if not dist_info.is_dir():
                continue
            scanned += 1
            metadata_path = dist_info / "METADATA"
            # `importlib.metadata.version()` returns None for an empty
            # METADATA file the same way it does for a missing one — a
            # zero-byte file (FS-interrupted write, manual edit, sync
            # glitch) trips the same `-32000` crash downstream, so we
            # treat empty as broken too. Use `stat()` rather than reading
            # the file to keep the scan O(dirs), not O(bytes).
            try:
                is_nonempty = (
                    metadata_path.is_file() and metadata_path.stat().st_size > 0
                )
            except OSError:
                is_nonempty = False
            if is_nonempty:
                # Whitespace-only METADATA (e.g. `"   \n  \n"` from a
                # partial sync or manual edit) passes both `is_file()`
                # and `stat().st_size > 0`, but `importlib.metadata.
                # version()` still returns None because the canonical
                # `Name:` header is absent. Require the RFC-822-ish
                # `Name: <value>` header to be present in the header
                # section — that's literally what the loader parses to
                # answer `version()`.
                #
                # The header section is RFC-822-shaped and terminates
                # at the first blank line. PEP 643 / Core Metadata
                # doesn't fix the order of fields, so `Name:` can sit
                # arbitrarily deep behind `Metadata-Version:`,
                # `License-Expression:`, multiple `Project-URL:` rows,
                # or in-header `Description:` text emitted by some
                # packaging tools. Read in chunks until we hit a blank
                # line OR a defensive 16 KiB cap — well past any real
                # wheel's header but bounded against a pathological
                # multi-MiB file. Wrap the read in try/except so a
                # race-condition mid-walk doesn't crash doctor.
                try:
                    chunks: list[bytes] = []
                    total = 0
                    cap = 16 * 1024
                    with metadata_path.open("rb") as fh:
                        while total < cap:
                            chunk = fh.read(1024)
                            if not chunk:
                                break
                            chunks.append(chunk)
                            total += len(chunk)
                            # `\n\n` (LF) or `\r\n\r\n` (CRLF) marks
                            # the end of the RFC-822 header section.
                            # Check the rolling buffer so a terminator
                            # split across two chunks still triggers.
                            joined = b"".join(chunks)
                            if b"\n\n" in joined or b"\r\n\r\n" in joined:
                                break
                    header_section = b"".join(chunks).decode("utf-8", errors="replace")
                    header_ok = bool(re.search(r"(?m)^Name:\s*\S", header_section))
                except OSError:
                    header_ok = False
                if header_ok:
                    continue
            # Missing or empty canonical METADATA. Scan for the iCloud-
            # style duplicate so we can hint at the likely cause.
            duplicates: list[str] = []
            try:
                for child in dist_info.iterdir():
                    if child.is_file() and _METADATA_DUP_RE.match(child.name):
                        duplicates.append(child.name)
            except OSError:
                pass
            broken.append(
                {
                    "dist_info": str(dist_info),
                    "duplicates": sorted(duplicates),
                }
            )

    if not broken:
        return Diagnosis(
            name="distinfo_metadata",
            status="ok",
            message=f"All {scanned} dist-info dir(s) have a canonical METADATA file.",
            details={
                "scanned": scanned,
                "site_packages": [str(p) for p in site_packages],
            },
        )

    names = ", ".join(Path(b["dist_info"]).name for b in broken[:3])
    if len(broken) > 3:
        names += f", … (+{len(broken) - 3} more)"
    any_dup = any(b["duplicates"] for b in broken)
    cause_hint = (
        " A duplicate like `METADATA 2` is present, which is the iCloud "
        "Drive sync-conflict signature on macOS."
        if any_dup
        else ""
    )
    return Diagnosis(
        name="distinfo_metadata",
        status="warn",
        message=(
            f"{len(broken)} dist-info dir(s) missing canonical METADATA "
            f"({names}). `importlib.metadata.version()` returns None for "
            f"these packages, which can crash the MCP server with an "
            f"opaque `-32000` disconnect.{cause_hint}"
        ),
        fix_hint=(
            "Re-install the affected package(s) with `uv pip install "
            "--force-reinstall <pkg>` (or `pip install --force-reinstall "
            "<pkg>`). Renaming `METADATA 2` → `METADATA` works if it's "
            "the only canonical-shaped file in the dir, but re-installing "
            "is the safer fix."
        ),
        details={"scanned": scanned, "broken": broken},
    )


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


def run_diagnostics() -> DoctorReport:
    """Run every check and return the aggregate report. Defensive against
    individual check failures: an unexpected exception inside a check is
    wrapped into a `fail` diagnosis with the exception class name."""
    checks: list[Diagnosis] = []

    def _safe(name: str, fn: Any) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            checks.append(
                Diagnosis(
                    name=name,
                    status="fail",
                    message=f"check raised {exc.__class__.__name__}: {exc}",
                    fix_hint="This is a bettermemory bug; please file an issue.",
                )
            )
            return None

    checks.append(_safe("python_version", _check_python_version))
    checks.append(_safe("binary_on_path", _check_binary_on_path))

    cfg_pair = _safe("config_loadable", _check_config_loadable)
    if cfg_pair is None:
        return DoctorReport(checks=[c for c in checks if c is not None])

    cfg_diag, cfg = cfg_pair
    checks.append(cfg_diag)

    if cfg is None:
        # Config load failed; we can still run the binary/client checks
        # but storage probes are not meaningful.
        checks.append(_safe("mcp_client_configs", _check_mcp_client_configs))
        checks.append(_safe("stale_config_lockfiles", _check_stale_config_lockfiles))
        return DoctorReport(checks=[c for c in checks if c is not None])

    storage_pair = _safe("storage_directory", lambda: _check_storage_directory(cfg))
    if storage_pair is None:
        checks.append(_safe("mcp_client_configs", _check_mcp_client_configs))
        checks.append(_safe("stale_config_lockfiles", _check_stale_config_lockfiles))
        return DoctorReport(checks=[c for c in checks if c is not None])
    storage_diag, directory = storage_pair
    checks.append(storage_diag)

    if directory is not None and directory.exists():
        checks.append(
            _safe(
                "memory_parse_health",
                lambda: _check_memory_parse_health(directory),
            )
        )
        # After memory_parse_health deliberately: that check constructs
        # a Store, whose __post_init__ auto-rebuilds a rebuild-pending
        # index — a needs_rebuild that still shows here means the
        # auto-heal itself failed and `bettermemory reindex` is
        # genuinely needed.
        checks.append(
            _safe(
                "index_health",
                lambda: _check_index_health(directory),
            )
        )
        checks.append(_safe("event_log", lambda: _check_event_log_writable(directory)))
        checks.append(
            _safe(
                "audit_turn_cadence",
                lambda: _check_audit_turn_cadence(directory),
            )
        )
        checks.append(
            _safe(
                "auto_memory_stranded",
                lambda: _check_auto_memory_stranded(directory),
            )
        )
        checks.append(
            _safe(
                "sync_tracked_ignored",
                lambda: _check_sync_tracked_ignored(directory),
            )
        )
        checks.append(
            _safe(
                "store_nested_in_parent_repo",
                lambda: _check_store_nested_in_parent_repo(directory),
            )
        )

    checks.append(_safe("embeddings_extra", lambda: _check_embeddings_extra(cfg)))
    checks.append(_safe("mcp_client_configs", _check_mcp_client_configs))
    checks.append(_safe("stale_config_lockfiles", _check_stale_config_lockfiles))
    checks.append(_safe("distinfo_metadata", _check_distinfo_metadata))

    # Filter out any None entries that snuck through (defensive).
    return DoctorReport(checks=[c for c in checks if c is not None])


# ---------------------------------------------------------------------------
# Fixes (`doctor --fix`)
#
# Fixer contract: each fixer re-probes GROUND TRUTH under its own guard
# (never parses the diagnosis message — messages are for humans), mutates
# only through the same underlying functions the fix hints point at, and
# re-runs its own check so "fixed" is a verified claim, not an attempted
# one. A fixer returns None when the red diagnosis is not its
# auto-fixable branch (e.g. storage_directory failing because the path is
# a FILE) — the finding then stays manual with its hint. AUTO-fixable is
# deliberately narrow: idempotent + reversible + target-regenerable.
# Anything touching git history (untracking, filter-repo, secret
# rotation), anything that could delete possibly-unique user content,
# MCP client config edits, and anything on another host stay hints
# forever.
# ---------------------------------------------------------------------------


def _fix_storage_directory(
    *, cfg: Config | None, directory: Path | None, diagnosis: Diagnosis
) -> FixResult | None:
    """chmod 0700 an existing-but-unwritable store directory.

    Only the not-writable branch is auto-fixable: missing-parent,
    path-is-a-file, and probe-write failures (ENOSPC, read-only mounts)
    all need decisions or resources a chmod cannot supply. 0700 rather
    than a minimal `u+w` is deliberate — the store carries private user
    data, so the heal converges on the private posture the event-log
    writer already enforces for its own file; the prior mode is recorded
    in the result (and the event log) so the change is reversible from
    the audit trail.
    """
    if cfg is None or directory is None:
        return None
    if not directory.exists() or not directory.is_dir():
        return None
    if os.access(directory, os.W_OK):
        # Already writable (or running as root, where os.access is blind
        # to modes) — whatever made the check red, it isn't the
        # chmod-able branch.
        return None
    old_mode = stat.S_IMODE(directory.stat().st_mode)
    try:
        directory.chmod(0o700)
    except OSError as exc:
        return FixResult(
            check="storage_directory",
            action="chmod_storage_dir",
            applied=False,
            before_status=diagnosis.status,
            after_status=diagnosis.status,
            message=f"chmod 0700 on {directory} failed",
            error=f"{exc.__class__.__name__}: {exc}",
            details={"path": str(directory), "old_mode": oct(old_mode)},
        )
    after, _redirectory = _check_storage_directory(cfg)
    return FixResult(
        check="storage_directory",
        action="chmod_storage_dir",
        applied=True,
        before_status=diagnosis.status,
        after_status=after.status,
        message=f"chmod {oct(old_mode)} → 0o700 on {directory}",
        details={
            "path": str(directory),
            "old_mode": oct(old_mode),
            "new_mode": "0o700",
        },
    )


def _fix_event_log(
    *, cfg: Config | None, directory: Path | None, diagnosis: Diagnosis
) -> FixResult | None:
    """chmod 0600 an existing-but-unwritable event log file.

    The cannot-be-created branch (directory not writable) is the
    storage_directory fixer's cause, not this one's — when that fix
    lands first, the final full re-run reports this check healed too.
    0600 matches the mode the Recorder itself sets on first write.
    """
    if directory is None or not directory.exists():
        return None
    log_path = directory / EVENT_LOG_FILENAME
    if not log_path.exists():
        return None
    if os.access(log_path, os.W_OK):
        return None
    old_mode = stat.S_IMODE(log_path.stat().st_mode)
    try:
        log_path.chmod(0o600)
    except OSError as exc:
        return FixResult(
            check="event_log",
            action="chmod_event_log",
            applied=False,
            before_status=diagnosis.status,
            after_status=diagnosis.status,
            message=f"chmod 0600 on {log_path} failed",
            error=f"{exc.__class__.__name__}: {exc}",
            details={"path": str(log_path), "old_mode": oct(old_mode)},
        )
    after = _check_event_log_writable(directory)
    return FixResult(
        check="event_log",
        action="chmod_event_log",
        applied=True,
        before_status=diagnosis.status,
        after_status=after.status,
        message=f"chmod {oct(old_mode)} → 0o600 on {log_path}",
        details={
            "path": str(log_path),
            "old_mode": oct(old_mode),
            "new_mode": "0o600",
        },
    )


def _fix_index_health(
    *, cfg: Config | None, directory: Path | None, diagnosis: Diagnosis
) -> FixResult | None:
    """Rebuild the FTS5 index through `index.rebuild` — the exact
    function `bettermemory reindex` runs. Every red index_health state
    (missing-with-files, corrupt meta, torn pages, a rebuild-pending
    that survived Store's auto-heal, count divergence) shares this one
    repair, and the index is derived state — the .md files stay
    canonical throughout, so the rebuild is regenerable-by-definition
    safe. `rebuild` itself is the documented recovery primitive and
    tolerates ANY prior on-disk index state.
    """
    if directory is None or not directory.exists():
        return None
    # Lazy for the same no-sqlite3-interpreter reason as
    # `_check_index_health`'s `index` import.
    import sqlite3

    from . import index

    try:
        store = Store(directory)
        count = index.rebuild(directory, store.iter_active())
    except (OSError, sqlite3.Error) as exc:
        # The same failure surface `reindex` routes through
        # parser.error: read-only dir, ENOSPC, a SQLite I/O error.
        return FixResult(
            check="index_health",
            action="rebuild_index",
            applied=False,
            before_status=diagnosis.status,
            after_status=diagnosis.status,
            message="index rebuild failed",
            error=f"{exc.__class__.__name__}: {exc}",
            details={"path": str(index.index_path(directory))},
        )
    after = _check_index_health(directory)
    return FixResult(
        check="index_health",
        action="rebuild_index",
        applied=True,
        before_status=diagnosis.status,
        after_status=after.status,
        message=f"rebuilt the search index from disk ({count} memories indexed)",
        details={"indexed": count, "path": str(index.index_path(directory))},
    )


def _fix_sync_gitignore(
    *, cfg: Config | None, directory: Path | None, diagnosis: Diagnosis
) -> FixResult | None:
    """Refresh the store sync repo's `.gitignore` to the canonical
    pattern list — `sync.init()`'s own idempotent, atomic refresh,
    applied without the user having to remember that init is also the
    refresher.

    This is a PARTIAL fix by design: gitignore cannot untrack, so the
    check stays red until the user runs the `git rm --cached`
    remediation from the hint — but without the refresh, that manual
    step silently un-does itself on a stale-gitignore store: `sync
    push` does NOT refresh the gitignore, so its next `git add -A`
    would re-stage the just-untracked file. The untrack itself — and
    the history rewrite / secret rotation behind it — stays manual
    forever.
    """
    if directory is None or not directory.exists():
        return None
    # Same lazy-import rationale as `_check_sync_tracked_ignored`: a
    # top-level `from .sync import …` here would be circular.
    from ._fsutil import atomic_write_bytes
    from .sync import _GITIGNORE_LINES, _is_repo

    if not _is_repo(directory):
        return None
    gitignore = directory / ".gitignore"
    desired = "\n".join(_GITIGNORE_LINES) + "\n"
    try:
        current = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    except OSError:
        # Unreadable counts as stale — the write below either heals it
        # or reports the failure honestly.
        current = ""
    if current == desired:
        # Gitignore already canonical — the remaining remediation is
        # the manual untrack; nothing auto-applicable here.
        return None
    try:
        atomic_write_bytes(gitignore, desired.encode("utf-8"))
    except OSError as exc:
        return FixResult(
            check="sync_tracked_ignored",
            action="refresh_gitignore",
            applied=False,
            before_status=diagnosis.status,
            after_status=diagnosis.status,
            message=f"refreshing {gitignore} failed",
            error=f"{exc.__class__.__name__}: {exc}",
            details={"gitignore": str(gitignore)},
        )
    after = _check_sync_tracked_ignored(directory)
    return FixResult(
        check="sync_tracked_ignored",
        action="refresh_gitignore",
        applied=True,
        before_status=diagnosis.status,
        after_status=after.status,
        message=(
            ".gitignore refreshed to the canonical pattern list (stops "
            "future staging); the already-tracked files still need the "
            "manual `git rm --cached` remediation in the hint"
        ),
        details={"gitignore": str(gitignore)},
    )


def _fix_stale_config_lockfiles(
    *, cfg: Config | None, directory: Path | None, diagnosis: Diagnosis
) -> FixResult | None:
    """Remove the 0-byte `<config>.lock` artifacts through
    `init._heal_stale_sidecar_lockfile` — the same heal `bettermemory
    init` applies. The heal's own guard re-checks the artifact shape at
    unlink time (regular file, not a symlink, exactly 0 bytes), so a
    client's live mkdir-style DIRECTORY lock, or a non-empty file some
    other tool owns, is never touched even if the path changed shape
    between the diagnosis and the fix.
    """
    from .init import _heal_stale_sidecar_lockfile

    removed: list[str] = []
    for _client_name, getter in KNOWN_CLIENTS.items():
        for path in getter().paths:
            healed = _heal_stale_sidecar_lockfile(path)
            if healed is not None:
                removed.append(str(healed))
    after = _check_stale_config_lockfiles()
    if not removed:
        # The heal is best-effort (it swallows unlink errors, mirroring
        # init's never-fail contract) — the re-run above is what tells
        # the truth about whether the artifact is actually gone.
        return FixResult(
            check="stale_config_lockfiles",
            action="remove_stale_lockfiles",
            applied=False,
            before_status=diagnosis.status,
            after_status=after.status,
            message=(
                "no 0-byte lockfile artifact matched at fix time "
                "(vanished, changed shape, or unlink was denied)"
            ),
        )
    plural = "" if len(removed) == 1 else "s"
    return FixResult(
        check="stale_config_lockfiles",
        action="remove_stale_lockfiles",
        applied=True,
        before_status=diagnosis.status,
        after_status=after.status,
        message=(
            f"removed {len(removed)} stale 3.15.0 lockfile{plural}: "
            + ", ".join(removed)
        ),
        details={"removed": removed},
    )


# Check name → fixer. Every red check WITHOUT an entry here is
# manual-only by construction — the safe default for any new check. Keys
# are pinned against the check inventory by the registry parity test in
# test_doctor.py; a fourth kind of entry (a fixer for a check that
# cannot exist) would be dead code the pin catches.
_FIXERS: dict[str, Any] = {
    "storage_directory": _fix_storage_directory,
    "index_health": _fix_index_health,
    "event_log": _fix_event_log,
    "sync_tracked_ignored": _fix_sync_gitignore,
    "stale_config_lockfiles": _fix_stale_config_lockfiles,
}


def _fix_context() -> tuple[Config | None, Path | None]:
    """The (config, storage directory) pair the fixers need, derived
    with the same tolerance `run_diagnostics` applies: a config that
    fails to load (config_loadable's fail) leaves only the
    directory-independent fixers reachable; an unresolvable directory
    (storage_directory's first fail branch) the same."""
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001 — mirrors _check_config_loadable
        return None, None
    try:
        directory = cfg.resolved_directory()
    except Exception:  # noqa: BLE001 — mirrors _check_storage_directory
        return cfg, None
    return cfg, directory


def _record_fix_events(directory: Path | None, applied: list[FixResult]) -> None:
    """One `doctor_fix` event per applied fix — the same observability
    bar as every other mutating surface. Best-effort like the Recorder
    itself: a logging hiccup must never fail a fix that already landed.
    Skipped when no store directory exists to host the log (then the
    CLI/JSON output is the only record, which is still honest)."""
    if not applied or directory is None or not directory.exists():
        return
    try:
        from .events import Recorder
        from .session import SessionState

        recorder = Recorder(root=directory, session_id=SessionState().session_id)
        for f in applied:
            recorder.record(
                "doctor_fix",
                check=f.check,
                action=f.action,
                before_status=f.before_status,
                after_status=f.after_status,
                detail=f.details,
            )
    except Exception:  # noqa: BLE001 — audit trail is best-effort
        pass


def run_fixes(
    report: DoctorReport, *, cfg: Config | None, directory: Path | None
) -> list[FixResult]:
    """Apply the auto-fixable subset of `report`'s red findings, in
    report order (so the storage fix lands before checks that depend on
    a writable store re-run). Returns one FixResult per ATTEMPTED fix;
    red checks with no registered fixer — or whose fixer's guard says
    the red state isn't its branch — contribute nothing and stay
    manual. A fixer that raises is wrapped into a failed FixResult, the
    same never-take-down-the-report tolerance `_safe` gives checks."""
    fixes: list[FixResult] = []
    for diag in report.checks:
        if diag.status == "ok":
            continue
        fixer = _FIXERS.get(diag.name)
        if fixer is None:
            continue
        try:
            result = fixer(cfg=cfg, directory=directory, diagnosis=diag)
        except Exception as exc:  # noqa: BLE001 — mirror _safe
            result = FixResult(
                check=diag.name,
                action=f"fix_{diag.name}",
                applied=False,
                before_status=diag.status,
                after_status=diag.status,
                message=f"fixer raised {exc.__class__.__name__}: {exc}",
                error=f"{exc.__class__.__name__}: {exc}",
            )
        if result is not None:
            fixes.append(result)
    _record_fix_events(directory, [f for f in fixes if f.applied])
    return fixes


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


_STATUS_GLYPH: dict[CheckStatus, str] = {"ok": "✓", "warn": "⚠", "fail": "✗"}


# Exit-code mapping for `cli_doctor`. Lifted to a module-level constant
# (mirroring `_STATUS_GLYPH`'s placement) so the closed-Literal-keyed dict
# can be pinned in tests against `CheckStatus`. The inline dict it replaces
# carried the same KeyError hazard: adding a fourth `CheckStatus` literal
# without updating the mapping would crash `bettermemory doctor` on the
# first diagnosis that surfaced the new status — exit codes are user-visible
# in shell pipelines, so the failure mode is worth pinning.
_EXIT_CODE_BY_STATUS: dict[CheckStatus, int] = {"ok": 0, "warn": 1, "fail": 2}


def render_text(report: DoctorReport) -> str:
    overall = report.overall
    glyph = _STATUS_GLYPH[overall]
    overall_label = {
        "ok": "all checks passed",
        "warn": "passed with warnings",
        "fail": "one or more checks failed",
    }[overall]
    out: list[str] = [f"{glyph}  bettermemory doctor — {overall_label}", ""]
    for check in report.checks:
        out.append(f"{_STATUS_GLYPH[check.status]} {check.name}: {check.message}")
        if check.fix_hint:
            out.append(f"    fix: {check.fix_hint}")
    out.append("")
    return "\n".join(out)


def render_fixes_text(fixes: list[FixResult], pre: DoctorReport) -> str:
    """The `--fix` tail of the text report. `pre` is the PRE-fix report
    — the "before" half of the before/after story (the caller renders
    the post-fix check list above this section). Says so explicitly
    when there was nothing to fix, per the no-op contract."""
    out: list[str] = ["--fix:"]
    red = [c.name for c in pre.checks if c.status != "ok"]
    if not red:
        out.append("  all checks passed — nothing to fix.")
    elif not fixes:
        out.append(
            "  no auto-fixable findings — every finding is manual-only; "
            "see the fix hints above."
        )
    else:
        for f in fixes:
            if f.applied and f.after_status == "ok":
                out.append(
                    f"  ✓ {f.check}: fixed (was {f.before_status}) — {f.message}"
                )
            elif f.applied:
                out.append(
                    f"  ⚠ {f.check}: applied (still {f.after_status}) — {f.message}"
                )
            else:
                out.append(f"  ✗ {f.check}: not applied — {f.error or f.message}")
        manual = [n for n in red if n not in {f.check for f in fixes}]
        if manual:
            out.append(
                "  manual-only finding(s), see hints above: " + ", ".join(manual)
            )
    out.append("")
    return "\n".join(out)


def render_json(report: DoctorReport, fixes: list[FixResult] | None = None) -> str:
    """`fixes is None` means "not a --fix run" — the payload keeps its
    pre---fix shape exactly (no `fixes` key), so existing `doctor
    --json` consumers see zero drift. A --fix run always carries the
    `fixes` array, empty included, so its consumers can branch on
    presence rather than sniffing."""
    payload: dict[str, Any] = {
        "overall": report.overall,
        "checks": [
            {
                "name": c.name,
                "status": c.status,
                "message": c.message,
                "fix_hint": c.fix_hint,
                "details": c.details,
            }
            for c in report.checks
        ],
    }
    if fixes is not None:
        payload["fixes"] = [
            {
                "check": f.check,
                "action": f.action,
                "applied": f.applied,
                "before_status": f.before_status,
                "after_status": f.after_status,
                "message": f.message,
                "error": f.error,
                "details": f.details,
            }
            for f in fixes
        ]
        payload["fixes_applied"] = sum(1 for f in fixes if f.applied)
    return json.dumps(payload, indent=2) + "\n"


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def cli_doctor(*, json_out: bool, fix: bool = False) -> int:
    """`bettermemory doctor` entry point. Returns the exit code:
    0 = ok, 1 = warn, 2 = fail. Tooling can branch on this without
    parsing output. With `fix=True` the safe remediations are applied
    first and the code reflects the POST-fix state — `doctor --fix &&
    …` means "healthy, possibly after healing", the same contract
    scripts already rely on."""
    report = run_diagnostics()
    if fix:
        cfg, directory = _fix_context()
        fixes = run_fixes(report, cfg=cfg, directory=directory)
        # Full re-run rather than patching the per-fix re-runs into the
        # pre report: a fix can heal a NEIGHBOUR check's cause (the
        # storage chmod unblocks event-log creation), and only a fresh
        # pass reports that honestly. Skipped when nothing mutated —
        # the pre report is still current.
        post = run_diagnostics() if any(f.applied for f in fixes) else report
        if json_out:
            sys.stdout.write(render_json(post, fixes=fixes))
        else:
            sys.stdout.write(render_text(post))
            sys.stdout.write(render_fixes_text(fixes, report))
        return _EXIT_CODE_BY_STATUS[post.overall]
    if json_out:
        sys.stdout.write(render_json(report))
    else:
        sys.stdout.write(render_text(report))
    return _EXIT_CODE_BY_STATUS[report.overall]
