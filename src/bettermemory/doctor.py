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
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import sysconfig
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from .config import Config, load_config
from .events import iter_all_events
from .init import KNOWN_CLIENTS, find_binary
from .store import Store


CheckStatus = Literal["ok", "warn", "fail"]


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
    probe = directory / ".doctor-probe"
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

    md_files = [
        p
        for p in directory.glob("*.md")
        if p.name != "README.md" and not p.name.startswith(".")
    ]
    parsed = len(memories)
    on_disk = len(md_files)
    if parsed == on_disk:
        return Diagnosis(
            name="memory_parse_health",
            status="ok",
            message=f"All {parsed} active memories parse cleanly.",
            details={"parsed": parsed, "files_on_disk": on_disk},
        )
    failed = on_disk - parsed
    return Diagnosis(
        name="memory_parse_health",
        status="warn",
        message=(
            f"{failed} of {on_disk} memory files in {directory} did not "
            f"parse (server skips them with a logged warning)."
        ),
        fix_hint=(
            "Run `bettermemory` directly to see the warnings, or check "
            "frontmatter of files that don't appear in `bettermemory health`."
        ),
        details={"parsed": parsed, "files_on_disk": on_disk, "failed": failed},
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
    log_path = directory / ".events.jsonl"
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
            fix_hint=f"`chmod u+w {log_path}`.",
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
        import sentence_transformers  # noqa: F401
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


def _check_mcp_client_configs() -> Diagnosis:
    """Scan known clients' MCP config files; report which ones reference
    bettermemory and whether the registered binary path matches the one
    we'd resolve from PATH right now.

    Mismatch is the most common "I reinstalled into a venv and now
    nothing works" failure mode — the client's config still points at
    the old binary path, which no longer exists.
    """
    resolved_binary = find_binary()
    findings: list[dict[str, Any]] = []
    has_mismatch = False

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
                if "bettermemory" not in command:
                    continue
                # Found a bettermemory entry. Check the binary path.
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
                    has_mismatch = True
                elif not matches:
                    has_mismatch = True

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

    if has_mismatch:
        return Diagnosis(
            name="mcp_client_configs",
            status="warn",
            message=(
                "At least one MCP client has a stale binary path or one "
                "that doesn't exist on disk."
            ),
            fix_hint=(
                "Re-run `bettermemory init --client X` to refresh the "
                "command path. The init patch is idempotent."
            ),
            details={"resolved_binary": resolved_binary, "findings": findings},
        )

    return Diagnosis(
        name="mcp_client_configs",
        status="ok",
        message=f"{len(findings)} client config(s) reference bettermemory; all paths match.",
        details={"resolved_binary": resolved_binary, "findings": findings},
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
                    header_section = b"".join(chunks).decode(
                        "utf-8", errors="replace"
                    )
                    header_ok = bool(
                        re.search(r"(?m)^Name:\s*\S", header_section)
                    )
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
            details={"scanned": scanned, "site_packages": [str(p) for p in site_packages]},
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
        return DoctorReport(checks=[c for c in checks if c is not None])

    storage_pair = _safe("storage_directory", lambda: _check_storage_directory(cfg))
    if storage_pair is None:
        checks.append(_safe("mcp_client_configs", _check_mcp_client_configs))
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
        checks.append(_safe("event_log", lambda: _check_event_log_writable(directory)))
        checks.append(
            _safe(
                "audit_turn_cadence",
                lambda: _check_audit_turn_cadence(directory),
            )
        )

    checks.append(_safe("embeddings_extra", lambda: _check_embeddings_extra(cfg)))
    checks.append(_safe("mcp_client_configs", _check_mcp_client_configs))
    checks.append(_safe("distinfo_metadata", _check_distinfo_metadata))

    # Filter out any None entries that snuck through (defensive).
    return DoctorReport(checks=[c for c in checks if c is not None])


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


_STATUS_GLYPH: dict[CheckStatus, str] = {"ok": "✓", "warn": "⚠", "fail": "✗"}


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


def render_json(report: DoctorReport) -> str:
    return (
        json.dumps(
            {
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
            },
            indent=2,
        )
        + "\n"
    )


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def cli_doctor(*, json_out: bool) -> int:
    """`bettermemory doctor` entry point. Returns the exit code:
    0 = ok, 1 = warn, 2 = fail. Tooling can branch on this without
    parsing output."""
    report = run_diagnostics()
    if json_out:
        sys.stdout.write(render_json(report))
    else:
        sys.stdout.write(render_text(report))
    return {"ok": 0, "warn": 1, "fail": 2}[report.overall]
