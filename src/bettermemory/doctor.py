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
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .config import Config, load_config
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
    return Diagnosis(
        name="binary_on_path",
        status="warn",
        message=(
            "`bettermemory` not on $PATH for this shell. MCP clients "
            "spawn the server in a fresh process and won't find it "
            "either unless their PATH is set up at GUI-launch time "
            "(macOS Finder/Launchpad inherits a minimal PATH)."
        ),
        fix_hint=(
            f"Use the absolute path in MCP client configs: {fallback}. "
            "`bettermemory init --client X` does this automatically."
        ),
        details={"resolved_binary": fallback},
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
    probe = directory / ".doctor-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
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

    checks.append(_safe("embeddings_extra", lambda: _check_embeddings_extra(cfg)))
    checks.append(_safe("mcp_client_configs", _check_mcp_client_configs))

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
