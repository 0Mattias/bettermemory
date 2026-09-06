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
fix lands one `doctor_fix` event in the store's event log — unless
`[telemetry] enabled = false`, which turns the event log off everywhere
(doctor included); the CLI/JSON output is then the only record.
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
from typing import Any, Literal, cast

from . import search
from .config import Config, TelemetryConfig, _coerce_search_mode, load_config
from .durability import sentence_around
from .eval import is_admin_recorded_event
from .events import EVENT_LOG_FILENAME, iter_all_events
from .health import is_hook_telemetry_event
from .init import KNOWN_CLIENTS, command_launches_bettermemory, find_binary
from .models import Memory, looks_truncated
from .store import (
    Store,
    count_active_memory_files,
    count_unparseable_memory_files,
    scan_active_memory_ids,
)

# Redundant alias on purpose, and not a typo to tidy away. Under
# `strict = true` mypy applies `no_implicit_reexport`, so a name merely
# imported here is not readable as `doctor._has_confirmed_index_gap` from
# outside — and the identity leg's regression test has to read it, because
# it spies on the call to prove the reconciliation RAN rather than trusting
# the verdict. The `X as X` form is mypy's documented way to say "this
# module deliberately re-exports this". Collapsing it back to a plain
# import fails the type-check leg, not the tests.
from .store import _has_confirmed_index_gap as _has_confirmed_index_gap


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

    # Group/other bits on the store root are a real disclosure, not a
    # style nit: a memory's FILENAME carries the first ~43 chars of its
    # summary, so a 0o755 root hands every local account the gist of the
    # whole store from `ls` alone — the 0o600 on the bodies never comes
    # into it.
    #
    # This ATTEMPTS the tighten before judging, and warns only if it did
    # not take. The alternative — report any 0o755 root as `warn` — would
    # have flipped `doctor` from exit 0 to exit 1 for every store created
    # before the root got its explicit mode, on the first run after
    # upgrading, breaking the "exits 0 when it's wired correctly" contract
    # that CI gates rely on. Since the heal is what `Store.__post_init__`
    # does on open anyway, and the same helper backs both, the only
    # condition that survives to `warn` here is the one worth a human's
    # attention: a filesystem that refuses the chmod (sandbox, some
    # network mounts). `--fix` retries it and reports the failure loudly.
    #
    # Yes, a check mutating is unusual. It is bounded by the same
    # best-effort/POSIX-only contract as the Store heal, and this function
    # already performs a probe WRITE below, so it was never a pure read.
    #
    # POSIX-only. Windows has no meaningful mode bits, and `stat()` there
    # synthesises a mode that would trip this unconditionally.
    if sys.platform != "win32":
        from .store import _tighten_dir_mode

        _tighten_dir_mode(directory)
        try:
            mode = stat.S_IMODE(directory.stat().st_mode)
        except OSError:
            mode = 0
        if mode & 0o077:
            info["mode"] = oct(mode)
            return (
                Diagnosis(
                    name="storage_directory",
                    status="warn",
                    message=(
                        f"Storage at {directory} is {oct(mode)} — readable "
                        f"beyond its owner, and it could not be tightened. "
                        f"Memory filenames embed the first ~43 characters "
                        f"of each summary."
                    ),
                    fix_hint=f"`chmod 700 {directory}` (or run `doctor --fix`).",
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


class _MemoryLoad:
    """One `Store(directory).load_all()`, shared by every check that
    needs the parsed memories.

    Three checks read the same list — parse health counts it, body
    completeness reads the bodies, index health compares them against
    the index rows — and each used to pay its own walk. Cost is the
    smaller half of the reason to share: three independent samples of a
    directory another agent may be writing to can disagree with each
    other, and two checks reporting on two different snapshots is how a
    report contradicts itself.

    Each caller keeps its own degraded answer, so the failure is handed
    back rather than raised: parse health owns the "cannot read the
    store" verdict and the others must not report the same breakage a
    second time in their own voice. A failed load is remembered as a
    failure — retrying it once per check would be three chances to get
    three different stories.

    Constructing one per call site is the default (`load=None` on each
    check) and stays correct: the sharing is an optimisation the caller
    opts into, never a precondition. The `--fix` path relies on that —
    it re-runs index health after a rebuild and must see the store as
    it is now, not as the pre-fix report sampled it.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._loaded = False
        self._memories: list[Memory] | None = None
        self._error: Exception | None = None

    def get(self) -> tuple[list[Memory] | None, Exception | None]:
        """`(memories, None)` on success, `(None, exc)` on failure."""
        if not self._loaded:
            self._loaded = True
            try:
                self._memories = Store(self._directory).load_all()
            except Exception as exc:  # noqa: BLE001
                self._error = exc
        return self._memories, self._error


# How many skipped filenames the one-line text report spells out. The
# full list is always in `details["skipped_files"]` for `--json`; this
# only bounds the terminal line, where a store that dropped its whole
# root (a bad `sync pull`) would otherwise print hundreds of names and
# push every other check off the screen. Five is enough to recognise a
# pattern — one file, one directory, one migration — and the count in
# front of them is the quantity.
_PARSE_SKIP_NAMES_SHOWN = 5


def _skipped_memory_filenames(directory: Path) -> tuple[int, list[str]]:
    """One walk of the store root: `(files walked, names the loader skips)`.

    The naming counterpart to `count_unparseable_memory_files`, which
    answers the same question as a bare integer. Neither the file filter
    nor the skip width is restated here — the filenames come from
    `store.active_memory_filenames` (the `_iter_active_paths` rule) and
    the verdict from the store's own `_parse_memory_file` under its own
    `PARSE_SKIP_EXCEPTIONS`, so "named here" is "skipped there" by
    construction rather than by two definitions agreeing today. Reaching
    for the private parser is the same boundary crossing `_handlers.py`
    (`store._load_path`) and `cli/export.py` (`store._iter_tombstone_paths`)
    make, for the same reason.

    Both halves come from ONE snapshot, which is what makes them
    subtractable: the caller can report walked / skipped / parsed without
    a concurrent `memory_write` landing between two walks and inventing a
    file that failed to parse. Pays a full parse of the store, so callers
    reach for it only once a cheaper signal says something is already
    wrong. Returns the names sorted, for a stable report.

    A file deleted mid-walk (a `memory_remove` racing this) fails its
    parse with `FileNotFoundError` and is named — the same
    indistinguishable-from-malformed case `load_all` swallows. It costs a
    transient wrong name in a report, not a wrong verdict about a file
    that is still there.
    """
    from .store import (
        PARSE_SKIP_EXCEPTIONS,
        _parse_memory_file,
        active_memory_filenames,
    )

    names = sorted(active_memory_filenames(directory))
    skipped: list[str] = []
    for name in names:
        try:
            _parse_memory_file(directory / name)
        except PARSE_SKIP_EXCEPTIONS:
            skipped.append(name)
    return len(names), skipped


def _check_memory_parse_health(
    directory: Path, load: _MemoryLoad | None = None
) -> Diagnosis:
    """Try to load every active memory; surface the files it could not.

    Two stages on purpose. The cheap one is a count comparison against
    the shared `_MemoryLoad` — no second parse on the healthy path, which
    is nearly every run. Only when that disagrees does the check pay
    `_skipped_memory_filenames` for a walk that can say WHICH files, and
    the confirming walk is then the authority for everything reported:
    `bettermemory export --strict` and `index_health` both send the user
    here to identify the files, and a count cannot identify anything.

    The confirming walk also decides whether there is anything to report
    at all. `load_all` and the file count are two walks of a directory a
    live server may be writing to, so a `memory_write` landing between
    them shows up as a delta with no unparseable file behind it — a
    warning about corruption nobody can find. The re-read either names a
    file or the check goes green.
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
    memories, load_error = (load or _MemoryLoad(directory)).get()
    if memories is None:
        return Diagnosis(
            name="memory_parse_health",
            status="fail",
            message=f"Could not list memories: {load_error}.",
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
    # The counts disagree. Re-read the store naming names, and report
    # that walk's numbers rather than the delta's: one snapshot keeps
    # `parsed + skipped == files_on_disk` true in the report, and
    # `skipped` equal to the `count_unparseable_memory_files` index_health
    # subtracts, by construction instead of by coincidence.
    walked, skipped_files = _skipped_memory_filenames(directory)
    if not skipped_files:
        return Diagnosis(
            name="memory_parse_health",
            status="ok",
            message=(
                f"All {walked} active memories parse cleanly. A first count "
                f"disagreed with the load by {on_disk - parsed}, which a "
                f"re-read did not reproduce — a memory written between "
                f"doctor's two walks, not a file the loader refused."
            ),
            details={"parsed": walked, "files_on_disk": walked, "skipped": 0},
        )
    # The re-read can't distinguish malformed frontmatter from an
    # intentionally-skipped file (a schema_version newer than this install,
    # e.g. after a `sync pull` from a machine on a newer bettermemory). Don't
    # assert "did not parse" or point only at frontmatter — and don't claim a
    # "logged warning" the skip path doesn't actually emit. Naming the files
    # is what lets the user tell the two apart by looking.
    skipped = len(skipped_files)
    shown = skipped_files[:_PARSE_SKIP_NAMES_SHOWN]
    listed = ", ".join(shown)
    if skipped > len(shown):
        listed += f", and {skipped - len(shown)} more"
    return Diagnosis(
        name="memory_parse_health",
        status="warn",
        message=(
            f"{skipped} of {walked} memory files in {directory} were skipped "
            f"by the loader (malformed frontmatter, or a schema_version newer "
            f"than this install): {listed}."
        ),
        fix_hint=(
            "Check the frontmatter of the files named above; if you recently "
            "downgraded bettermemory, upgrade back to read memories written "
            "under the newer version."
        ),
        details={
            "parsed": walked - skipped,
            "files_on_disk": walked,
            "skipped": skipped,
            "skipped_files": skipped_files,
        },
    )


def _check_memory_body_completeness(
    directory: Path, load: _MemoryLoad | None = None
) -> Diagnosis:
    """Report active memories whose body reads as cut off mid-sentence.

    The gap this closes: `memory_parse_health` above answers "does the
    frontmatter parse", which is a true statement about file structure
    and says nothing about whether the CONTENT is whole. A body that
    arrived truncated from the caller — an interrupted tool call, an LLM
    that hit its output cap mid-argument, a copy-paste that lost its tail
    — round-trips byte-exactly through the store and is reported healthy
    by every check here. One memory in the maintainer's store sat cut off
    at "The whole security/red-team stack (Hak5" for ten days with a
    clean bill of health, and the lost tail held a correction another
    memory pointed at. There was no surface that would have said so.

    `warn`, never `fail`, and it names the ids rather than prescribing a
    repair: `models.looks_truncated` is a heuristic with a real if small
    false-positive rate (its own docstring carries the measurement), and
    the one thing doctor must not do is tell an operator that a
    legitimately-worded memory is damaged. A hit is a prompt to look, not
    a verdict.
    """
    if not directory.exists():
        return Diagnosis(
            name="memory_body_completeness",
            status="ok",
            message="Storage dir does not exist yet — nothing to check.",
        )
    memories, load_error = (load or _MemoryLoad(directory)).get()
    if memories is None:
        # Deliberately not a `fail`: `memory_parse_health` runs first and
        # owns the "cannot read the store" verdict. Reporting the same
        # breakage twice, in two voices, sends the operator looking for
        # two problems.
        return Diagnosis(
            name="memory_body_completeness",
            status="ok",
            message=f"Skipped — could not list memories ({load_error}).",
        )

    suspect = [m.id for m in memories if looks_truncated(m.body)]
    if not suspect:
        return Diagnosis(
            name="memory_body_completeness",
            status="ok",
            message=f"All {len(memories)} active memory bodies end intact.",
            details={"checked": len(memories), "suspect": 0},
        )
    shown = suspect[:10]
    more = len(suspect) - len(shown)
    return Diagnosis(
        name="memory_body_completeness",
        status="warn",
        message=(
            f"{len(suspect)} of {len(memories)} active memories end "
            f"mid-sentence, which is what a body truncated in transit looks "
            f"like: {', '.join(shown)}"
            f"{f' (+{more} more)' if more else ''}."
        ),
        fix_hint=(
            "Run `memory_show` on each and read the last line. A body that "
            "stops mid-word lost its tail on the way in and the store has no "
            "older copy — re-derive the content and `memory_update`. A body "
            "that legitimately ends on a list item or a bare identifier is a "
            "false positive; nothing needs doing."
        ),
        details={
            "checked": len(memories),
            "suspect": len(suspect),
            "suspect_ids": suspect,
        },
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


def _index_content_rows(index_file: Path) -> dict[str, tuple[str, str]]:
    """`{id: (scopes_json, body)}` for every row in the index.

    The two columns a hand-edit can change without changing anything a
    count or an id set can see. `index._upsert_memory` writes
    `json.dumps(memory.scopes)` into `scopes_json` and `memory.body`
    verbatim into `body`, so an unmodified store compares byte-equal on
    both — measured on the maintainer's live store, 239 memories, zero
    mismatches on either column.

    Read-only (URI `mode=ro`) for the same reason as
    `_probe_index_integrity`: a diagnostic must not be able to create
    the file it is diagnosing, nor write to it. Errors propagate; the
    caller decides how to degrade.
    """
    import sqlite3

    conn = sqlite3.connect(
        index_file.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0
    )
    try:
        return {
            str(row[0]): (str(row[1]), str(row[2]))
            for row in conn.execute("SELECT id, scopes_json, body FROM memories")
        }
    finally:
        conn.close()


def _reconcile_index_against_disk(
    directory: Path, *, details: dict[str, Any], load: _MemoryLoad
) -> str | None:
    """Answer whether the index still DESCRIBES the store, not merely
    whether it holds the same number of rows. `None` when it does, else
    a one-line description of the divergence.

    Two shapes survive an equal count, and both are reachable through
    the workflow this project advertises as its differentiator — one
    file per memory, grep-able and hand-editable (`docs/internals.md`):

    - **Identity.** Remove one memory and add another out of band and
      the count is unchanged while the id sets are not. Resolved with
      `store._has_confirmed_index_gap` rather than a set difference
      against `index.indexed_ids`: every Store mutator lands the `.md`
      and commits the row as two steps inside one `_locked()` block, so
      a raw diff taken against a store a fleet is writing to reports a
      hole that closes a millisecond later. That helper re-resolves each
      candidate under the writer's own file lock, which synchronises
      with the writer instead of guessing how long it will take.
    - **Content.** Same id, same file, edited body or scopes. No id-set
      comparison can see it; the index keeps serving the pre-edit text
      to FTS and the pre-edit scopes to the scope rollup.

    Both legs record whether they RAN in `details`, because "reconciled
    and clean" and "could not reconcile" are different claims and this
    check's whole history is of the second being reported as the first.
    A leg that could not run is a divergence this cannot rule out, so it
    reads as a finding rather than as a pass.

    Both legs run even when the first has already found something: one
    reindex repairs both, and an operator reading the report deserves
    the whole list rather than the first item on it.
    """
    from . import index

    problems: list[str] = []

    try:
        disk_paths, _ = scan_active_memory_ids(directory)
        identity_gap = _has_confirmed_index_gap(directory, disk_paths)
    except Exception as exc:  # noqa: BLE001
        details["identity_reconciled"] = False
        problems.append(
            f"could not reconcile index ids against disk "
            f"({exc.__class__.__name__}: {exc})"
        )
    else:
        details["identity_reconciled"] = True
        if identity_gap:
            problems.append(
                "the indexed ids no longer match the ids on disk (a memory "
                "with no row, or a row naming an id that is not on disk)"
            )

    memories, load_error = load.get()
    if memories is None:
        details["content_reconciled"] = False
        problems.append(
            f"could not read the memory files to compare against the index "
            f"rows ({load_error})"
        )
        return "; ".join(problems) or None
    try:
        rows = _index_content_rows(index.index_path(directory))
    except Exception as exc:  # noqa: BLE001
        details["content_reconciled"] = False
        problems.append(
            f"could not read the index rows to compare against disk "
            f"({exc.__class__.__name__}: {exc})"
        )
        return "; ".join(problems) or None

    details["content_reconciled"] = True
    # Both bodies go through `_frontmatter.normalise_body` before the
    # comparison, because the two sides sit on opposite banks of it.
    # `_index_upsert_quietly` indexes the in-memory `Memory`, while the
    # `.md` reaches disk through `dumps`, which strips CR-before-newline
    # (`_frontmatter.py`, the CRLF note above the `normalise_body` call).
    # A body written as `alpha\r\nbeta` is therefore stored with its CRs
    # in the index and without them on disk, and `load_all` returns the
    # disk form — so a byte comparison reports drift on a store where
    # nothing drifted. Normalising both sides asks the question this leg
    # means to ask: does the index still hold the same TEXT, as every
    # reader of either side would see it.
    from ._frontmatter import normalise_body

    drifted = sorted(
        m.id
        for m in memories
        if m.id in rows
        and (json.dumps(m.scopes), normalise_body(m.body))
        != (rows[m.id][0], normalise_body(rows[m.id][1]))
    )
    details["content_drift_count"] = len(drifted)
    if drifted:
        shown = drifted[:10]
        more = len(drifted) - len(shown)
        details["content_drift_ids"] = shown
        problems.append(
            f"{len(drifted)} memory file(s) carry a body or scope list the "
            f"index row no longer matches: {', '.join(shown)}"
            f"{f' (+{more} more)' if more else ''}"
        )
    return "; ".join(problems) or None


def _check_index_health(directory: Path, load: _MemoryLoad | None = None) -> Diagnosis:
    """Probe the FTS5 index: `index.status()` (never raises) for the
    meta-level states, `PRAGMA quick_check` for the page-level
    corruption those meta reads can't see (see
    `_probe_index_integrity`), and compare `indexed_count` against the
    on-disk file count.

    A status()-visible unhealthy index (corrupt meta, missing,
    rebuild-pending) never breaks correctness —
    `_handlers.load_search_candidates` routes every `memory_search` to a
    full `load_all` — but the degradation to a linear scan is silent, and a
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

    The count comparison is a TRIGGER, never the verdict. Twice before
    (CHANGELOG "bettermemory doctor checks FTS index health", and again
    for the torn interior page) this check certified an index that no
    longer described the store, because equal counts returned `ok`
    directly. Nothing certifies now without
    `_reconcile_index_against_disk` having compared the ids and the
    content behind those counts — see
    `docs/incidents/2026-07-31-index-health-certified-a-stale-index.md`
    for the third occurrence and why the message shape changed with it.
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
    # The counts still line up per `status()`, but a count is not a
    # description. Reconcile the ids and the content behind it before
    # any `ok` leaves this function.
    reconcile = _MemoryLoad(directory) if load is None else load
    indexed_count = int(status.get("indexed_count", 0) or 0)
    if indexed_count == disk_count:
        divergence = _reconcile_index_against_disk(
            directory, details=details, load=reconcile
        )
        if divergence is not None:
            return Diagnosis(
                name="index_health",
                status="warn",
                message=(
                    f"Index no longer describes the store even though the "
                    f"row count matches disk ({indexed_count} rows, "
                    f"{disk_count} file(s)): {divergence}."
                ),
                fix_hint=fix,
                details=details,
            )
        return Diagnosis(
            name="index_health",
            status="ok",
            message=(
                f"Index healthy: {indexed_count} rows; row count matches "
                f"disk; PRAGMA quick_check passed; every id and every "
                f"body/scope list reconciled against disk."
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
        # As synced as a rebuild can make it — by count. This branch
        # certifies too, so it owes the same reconciliation as the
        # equal-count branch above; the unparseable files are excluded
        # from both legs because `scan_active_memory_ids` skips exactly
        # what `iter_active` skips.
        divergence = _reconcile_index_against_disk(
            directory, details=details, load=reconcile
        )
        if divergence is not None:
            return Diagnosis(
                name="index_health",
                status="warn",
                message=(
                    f"Index no longer describes the store even though the "
                    f"row count matches every parseable file on disk "
                    f"({indexed_count} rows, {unparseable_count} unparseable "
                    f"file(s) excluded): {divergence}."
                ),
                fix_hint=fix,
                details=details,
            )
        # The unparseable files are a real problem, but they're
        # memory_parse_health's finding — warning here would prescribe a
        # reindex that can never clear.
        return Diagnosis(
            name="index_health",
            status="ok",
            message=(
                f"Index healthy: {indexed_count} rows; row count matches "
                f"every parseable file on disk ({unparseable_count} "
                f"unparseable file(s) excluded; see memory_parse_health); "
                f"PRAGMA quick_check passed; every id and every body/scope "
                f"list reconciled against disk."
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


def _event_log_files(directory: Path) -> list[Path]:
    """Existing ACTIVE event-log files: the sharded `.events.NN.jsonl`
    segments (since v3.24.0) plus a legacy pre-sharding `.events.jsonl`
    if present. Rotated `.events-*.jsonl.gz` archives are excluded —
    they are immutable and read-only by contract, so neither the
    writability probe nor the 0600 healer applies to them. The
    `.events.*.jsonl` glob matches the sharded names but not the legacy
    `.events.jsonl` (no segment between the dots), which is appended
    explicitly."""
    files = sorted(directory.glob(".events.*.jsonl"))
    legacy = directory / EVENT_LOG_FILENAME
    if legacy.exists():
        files.append(legacy)
    return files


def _probe_event_log_segment(log_path: Path) -> tuple[str, str, int]:
    """Probe ONE active event-log segment. Returns
    `(outcome, detail, size)` where outcome is "ok" (appendable,
    `size` bytes), "vanished" (gone mid-run — not a finding),
    "not_writable" or "append_failed" (`detail` is the human sentence
    naming the cause).

    The existence gate runs FIRST and that ordering is load-bearing:
    `os.access` answers False for a path that is not there, so probing
    permission first misfiled a segment that vanished between
    `_event_log_files`' glob and this call as "not writable" — a red
    verdict naming a file that no longer exists, and a dead
    "vanished"/not-yet-created shape for the COMMON vanish timing (the
    FileNotFoundError branch below only catches the far narrower window
    between this gate and the open)."""
    if not os.path.lexists(log_path):
        # Gone between the glob and this probe (concurrent tidy-up,
        # rotation, the user deleting the log). `lexists`, not
        # `exists`: a DANGLING symlink at the log path is an entry that
        # really is there and really is not appendable on our terms, so
        # it must stay a finding rather than be excused as a vanish.
        return "vanished", "", 0
    if not os.access(log_path, os.W_OK):
        return "not_writable", f"{log_path} is not writable", 0
    # os.access is only the cheap pre-guard — on Windows it consults
    # nothing but FILE_ATTRIBUTE_READONLY (the exact bit `--fix`'s
    # chmod(0o600) clears), so an ACL-denied log passes it while every
    # real append still fails; on POSIX a directory squatting at the
    # path passes W_OK too. Probe-append for real, per this check's
    # own contract: a zero-byte write through an append-mode fd
    # exercises the exact permission the Recorder's append needs
    # without mutating the log. O_CREAT is deliberately absent —
    # `open("ab")` implies it, so a log that vanished between the
    # exists() gate above and this probe would be CONJURED back as an
    # empty file, a mutation this read-only diagnostic must never
    # make.
    try:
        fd = os.open(str(log_path), os.O_WRONLY | os.O_APPEND)
        try:
            os.write(fd, b"")
        finally:
            os.close(fd)
        size = log_path.stat().st_size
    except FileNotFoundError:
        # The segment legitimately vanished mid-run (concurrent
        # tidy-up, rotation) — nothing to fix, and nothing to report.
        return "vanished", "", 0
    except OSError as exc:
        return (
            "append_failed",
            (
                f"{log_path} passed the permission-bit check but a real "
                f"append failed ({exc.__class__.__name__}: {exc})"
            ),
            0,
        )
    return "ok", "", size


def _unwritable_segment_hint(log_path: Path, *, append_failed: bool) -> str:
    """The remediation prompt for one unappendable segment."""
    if append_failed:
        return (
            "Inspect what blocks appends despite writable permission "
            "bits — an ACL, a read-only mount, or a non-file "
            "squatting at the log path; a plain chmod cannot fix "
            "this class."
        )
    if log_path.is_symlink():
        # A symlinked log gets a NON-executable steer, never a
        # pasteable command: chmod follows symlinks, so the verbatim
        # `chmod u+w <log_path>` hint would have the user mutate the
        # TARGET's permissions by hand — the exact victim mutation
        # `_fix_event_log` declines for the same shape.
        return (
            "The event log is a symlink — inspect its target before "
            "changing permissions; a permission change through the "
            "link lands on the target file, which may not be ours."
        )
    # shlex.quote: a raw interpolation shell-splits on a
    # space-bearing storage path (the macOS `Application
    # Support` neighbourhood) and can chmod an innocent sibling
    # on a glob-bearing one — the same executes-verbatim
    # contract `_quoted_literal_pathspecs` holds for the
    # pathspec hints.
    return f"`chmod u+w {shlex.quote(str(log_path))}`."


def _check_event_log_writable(directory: Path) -> Diagnosis:
    """The event log writer creates the file lazily; we probe-append
    to confirm we'd be allowed to.

    The active log is SHARDED (v3.24.0), so "the event log" is a SET of
    `.events.NN.jsonl` segments plus any legacy `.events.jsonl`. EVERY
    segment is probed. Probing only the lexicographically first one
    (the pre-fix shape) returned a green `event_log` verdict covering a
    surface it never looked at: a mispermissioned `.events.07.jsonl`
    was invisible, and `_fix_event_log`'s heal-every-segment loop was
    unreachable because the check that gates it could not go red for
    any N>0. The reported byte count is likewise the whole log's, not
    the first shard's."""
    if not directory.exists():
        return Diagnosis(
            name="event_log",
            status="ok",
            message="Event log not yet created (storage dir is brand new).",
        )
    log_files = _event_log_files(directory)
    if not log_files:
        # Nothing on disk yet: probe writability of the directory
        # itself; the first segment will be created on first server
        # start, which needs exactly that directory write permission.
        log_path = directory / EVENT_LOG_FILENAME
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

    unwritable: list[str] = []
    failures: list[str] = []
    hints: list[str] = []
    probed_paths: list[str] = []
    total_bytes = 0
    for log_path in log_files:
        outcome, detail, size = _probe_event_log_segment(log_path)
        if outcome == "vanished":
            continue
        probed_paths.append(str(log_path))
        if outcome != "ok":
            unwritable.append(str(log_path))
            failures.append(detail)
            hints.append(
                _unwritable_segment_hint(
                    log_path, append_failed=outcome == "append_failed"
                )
            )
            continue
        total_bytes += size

    if unwritable:
        if len(unwritable) == 1:
            message = f"Event log at {failures[0]}."
            hint = hints[0]
        else:
            message = (
                f"{len(unwritable)} of {len(probed_paths)} event-log segments "
                f"are not appendable: " + "; ".join(failures) + "."
            )
            # Dedupe while preserving order: the per-path chmod hints
            # are distinct, the shape-level steers repeat.
            hint = " ".join(dict.fromkeys(hints))
        return Diagnosis(
            name="event_log",
            status="fail",
            message=message,
            fix_hint=hint,
            details={
                "unwritable": unwritable,
                "probed": len(probed_paths),
                "bytes": total_bytes,
            },
        )
    if not probed_paths:
        # Every segment vanished between the glob and its probe — the
        # same not-yet-created shape the empty-directory branch reports.
        return Diagnosis(
            name="event_log",
            status="ok",
            message="Event log not yet created (will appear on first server start).",
        )
    return Diagnosis(
        name="event_log",
        status="ok",
        message=(
            f"Event log writable ({len(probed_paths)} segment(s), {total_bytes} bytes)."
        ),
        details={
            "paths": probed_paths,
            "probed": len(probed_paths),
            "bytes": total_bytes,
        },
    )


# Admin-recorded events are excluded from the cadence census by calling
# `eval.is_admin_recorded_event` — never by re-testing one of its axes
# here. An event is admin-recorded when it comes from an admin/CLI
# surface (`doctor --fix`'s audit trail, `consolidate`'s bulk markers
# and acknowledgements) rather than from an MCP server session serving
# a client.
#
# INVARIANT: such an event is recorded outside any client session,
# under a fresh throwaway session id, so a "session" observed only
# through them never had a Stop hook that could produce
# `turn_audited`. Counting one corrupts the ≥2-sessions denominator:
# `doctor --fix` on a store with one real session would manufacture the
# second "session" whose missing `turn_audited` its own post-fix re-run
# then warns about, flipping a fully-healed run's exit code to 1.
#
# WHY THE PREDICATE AND NOT A CONSTANT: the classification has two
# independent axes — the kind roster (`ADMIN_RECORDED_EVENT_KINDS`) and
# the `cli_*` attribution prefix, which is what separates
# `consolidate --acknowledge-debt`'s `kind="use"` rows from the
# same-shaped rows a real client session emits. This module used to
# test the kind axis alone, so those rows kept publishing a phantom
# session here even after eval's own tally stopped counting them. The
# predicate keeps both axes in one place and a new axis reaches this
# census without a second edit.
#
# `tests/test_eval.py::TestAdminRecordedParity` enforces the routing:
# it AST-scans `src/` and fails any module other than eval.py that
# names either axis constant, so re-testing one axis by hand here
# breaks CI rather than silently disagreeing with eval about which
# sessions ever existed.


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
    two distinct sessions with zero HOOK-SOURCED `turn_audited` events,
    warn. The hook/mcp split rides the shared coverage predicate
    (`health.is_hook_telemetry_event`): an in-process
    `memory_audit_turn` stamps `triggered_from="mcp_tool"`, and
    counting it as hook evidence was the conflation this census kept
    after every other surface dropped it — an MCP-only store read as
    "hook is wired". Such a store still warns (the hook genuinely is
    not wired), but the message says which kind of store it is instead
    of claiming silence. Soft
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

    Admin/CLI events (everything `eval.is_admin_recorded_event`
    accepts) are excluded from the census entirely: they're recorded
    outside any client session and can never produce `turn_audited`,
    so counting their sessions (or events) would corrupt the
    denominator this heuristic rests on.
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
    hook_audited = 0
    total_events = 0
    try:
        for event in iter_all_events(directory):
            if is_admin_recorded_event(event):
                # Not client-session activity — see the comment above
                # this function. Both exclusion axes come from the one
                # shared predicate; testing either by hand here is what
                # let acknowledge-debt's rows through before.
                continue
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
                if is_hook_telemetry_event(event):
                    hook_audited += 1
    except OSError as exc:
        return Diagnosis(
            name="audit_turn_cadence",
            status="warn",
            message=f"Could not read event log to check audit cadence: {exc}.",
        )

    n_sessions = len(sessions)
    mcp_audited = turn_audited - hook_audited
    info: dict[str, Any] = {
        "window_days": 7,
        "sessions": n_sessions,
        "turn_audited_events": turn_audited,
        "hook_turn_audited": hook_audited,
        "mcp_turn_audited": mcp_audited,
        "total_events": total_events,
    }

    if total_events == 0:
        return Diagnosis(
            name="audit_turn_cadence",
            status="ok",
            message="No events in the last 7 days — nothing to check.",
            details=info,
        )
    if hook_audited == 0 and n_sessions >= 2:
        # Don't pretend we know the exact expected count — the cadence
        # depends on how often the user invokes Claude Code. "At least
        # N" is a useful order-of-magnitude where N is the session
        # count (a turn produces one Stop event, but a session
        # produces many turns — N is a lower bound). Two distinct
        # stores land here and the message tells them apart: a silent
        # one (nothing audits at all) and an MCP-audited one (the
        # model calls `memory_audit_turn` in-process — real telemetry,
        # but not the automatic end-of-turn lane).
        if mcp_audited:
            message = (
                f"No Stop-hook audit event in the last 7 days across "
                f"{n_sessions} session(s) — the hook is not wired. The "
                f"{mcp_audited} `turn_audited` event(s) present came "
                f"from in-process `memory_audit_turn` calls: an "
                f"MCP-audited store, not a hook-wired one. The "
                f"telemetry is real, but the automatic end-of-turn "
                f"audit lane is missing."
            )
        else:
            message = (
                f"Your Stop hook may be silently no-opping — expected "
                f"at least {n_sessions} hook-sourced audit-turn events "
                f"given {n_sessions} session(s) in the last 7 days, "
                f"found 0."
            )
        return Diagnosis(
            name="audit_turn_cadence",
            status="warn",
            message=message,
            fix_hint=(
                "Check `~/.claude/settings.json` (or your hooks config) "
                "for a Stop binding to `bettermemory audit-turn`. The "
                "plugin's `hooks/hooks.json` does this automatically "
                "when the plugin is installed; manual setups need to "
                "wire it themselves."
            ),
            details=info,
        )
    if hook_audited == 0 and n_sessions == 1:
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
    suffix = (
        f" ({mcp_audited} more from in-process `memory_audit_turn` calls.)"
        if mcp_audited
        else ""
    )
    return Diagnosis(
        name="audit_turn_cadence",
        status="ok",
        message=(
            f"{hook_audited} Stop-hook `turn_audited` event(s) across "
            f"{n_sessions} session(s) in the last 7 days.{suffix}"
        ),
        details=info,
    )


# The substring that identifies OUR SessionStart binding inside a hook
# command line. Matching on the subcommand rather than on the full
# command string is deliberate: the plugin ships `uvx bettermemory
# session-start || true`, a `uv tool install` user writes
# `bettermemory session-start`, and a venv user writes an absolute path
# — all three are correctly wired, and pinning any one spelling would
# warn at the other two.
_SESSION_START_HOOK_MARKER = "bettermemory session-start"

# Ceiling on how many `hooks.json` MATCHES the plugin-directory walk
# collects, and so on how many files this check then opens and parses.
# It bounds the parsing, not the traversal: `rglob` yields lazily, so
# breaking at the cap only stops a walk that has already found that many
# manifests — under the cap (a handful on a real install) every installed
# plugin's directory is still visited in full. That split is the intended
# one. Visiting a directory is readdir/stat with no file opens and stays
# in the low milliseconds, whereas reading and JSON-parsing every match
# pays per file for however many manifests a plugin ships (test fixtures,
# vendored sub-plugins). Hitting the cap can make this check publish a
# false "not wired" warn — a binding past the cap is simply never read —
# so the cap is a real accuracy trade, not a free one. It is an
# acceptable trade because the settings-file candidates above are
# collected FIRST and are never truncated, so a hand-wired user is never
# missed; only a plugin binding sitting behind 200 other manifests is,
# and a real install carries a handful. Raise the cap rather than
# rationalise the warn if that ever stops being true.
_PLUGIN_HOOK_SCAN_CAP = 200


def _installed_plugin_roots(
    plugins_root: Path, *, cwd: Path, disabled: frozenset[str] = frozenset()
) -> list[Path]:
    """Directories of the plugins INSTALLED here, scoped to `cwd`, not turned off.

    `~/.claude/plugins` is not one tree, it is two with opposite
    meanings, and only one of them is evidence of wiring:

    * `cache/<marketplace>/<plugin>/<version>/` — what an install
      actually puts on disk. `installed_plugins.json` records one entry
      per install with the `installPath` pointing here.
    * `marketplaces/<marketplace>/` — the marketplace's own git
      CHECKOUT, recorded in `known_marketplaces.json` under
      `installLocation`. It ships the CATALOGUE: a source copy of every
      plugin the marketplace offers, installed or not, each with its
      `hooks/hooks.json`. Adding a marketplace clones all of them.

    A catalogue copy declares hooks that Claude Code will never run,
    because the plugin behind it was never installed. Counting one as
    live wiring is a false green, and with the "one runnable binding
    anywhere wins" rule in `_check_session_start_hook_wired` it is a
    verdict-flipping one: a catalogue entry can fill the live slot and
    demote a user's genuinely-stale settings binding from `warn` to a
    detail on an `ok`. On the machine this was found on, all five
    `hooks.json` files under `~/.claude/plugins` were catalogue copies
    of plugins that were not installed.

    An install is also not necessarily scoped HERE. Each record carries
    a `scope`, and a `"local"` one names the `projectPath` it was
    installed for; Claude Code runs that plugin's hooks in that project
    only. Counting one from another project is the same false green as
    counting the catalogue, and a sharper one — the plugin really is
    installed, so nothing about the directory looks wrong. Hence `cwd`:
    the project being judged, against which project-scoped records are
    filtered by `_install_is_for_another_project`.

    Nor is an install necessarily switched ON. Installation and
    enablement are two separate records: `installed_plugins.json` says
    what is on disk, and the settings files say what runs, under an
    `enabledPlugins` map keyed by the same `plugin@marketplace` string.
    A disabled plugin stays installed — `claude plugin disable` is a
    different command from `uninstall`, and `--all` puts every install
    into that state at once — and Claude Code reads its
    `hooks/hooks.json` off disk only to decline registering it. That is
    the catalogue's false green again, on a directory with nothing wrong
    with it at all. Hence `disabled`: the keys `_disabled_plugin_keys`
    reads out of those settings files.

    `disabled` is narrower than "not enabled", deliberately: only an
    EXPLICIT `false` drops a record. An absent key normally means
    enabled (the plugin's own `defaultEnabled` decides, and it defaults
    to true), a value in some other shape is not this function's to
    adjudicate, and an administrator's managed-policy disable lives in a
    file this never reads — every one of those keeps the record, the
    same asymmetry `_install_is_for_another_project` documents. So the
    check can still certify a hook that will not run; what it can no
    longer do is certify one the user themselves switched off.

    So the manifest is the authority. When it parses and names installs,
    those paths are the whole answer — a plugin the user uninstalled
    leaves its cache directory behind, and that is not wiring either.
    Only when the manifest is missing, unreadable, or shaped in a way
    this doesn't recognise does it fall back to the `cache/` tree, which
    is where the recorded `installPath`s point on a normal install: a
    coarser answer than the manifest, but still one that excludes the
    catalogue, and still one that protects a plugin user from a false
    "not wired" warn.

    That last clause is why the manifest arm has to tell two empty
    answers apart, and the file carries a `"version"` (a `2` on the
    machine this was found on) precisely because its inner shape is
    Claude Code's to change:

    * we UNDERSTOOD the manifest and it yields nothing — `plugins` is
      empty, or every record parsed and none survived the filters. That
      is an answer, and the catalogue is not a second opinion on it.
    * we understood NOTHING in a non-empty `plugins` — every record was
      an unrecognised shape. That is the "shaped in a way this doesn't
      recognise" case, and the fallback exists for exactly it.

    Partial understanding counts as understanding: if some records parse
    and others don't, the ones that parsed are the answer. Falling back
    on a manifest we half-read would re-admit the uninstalled and the
    out-of-project alongside the drift we couldn't follow.

    Returns only paths that are directories, and never raises — a
    damaged plugin tree is not this check's problem to report.
    """
    manifest = plugins_root / "installed_plugins.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("plugins"), dict):
        by_plugin: dict[Any, Any] = data["plugins"]
        roots: list[Path] = []
        understood = 0
        # Everything is defensively type-checked: this is a FOREIGN file
        # whose shape is Claude Code's to change, and a check that raises
        # on someone else's unexpected JSON is worse than no check.
        for key, installs in by_plugin.items():
            if not isinstance(installs, list):
                continue
            switched_off = isinstance(key, str) and key in disabled
            for install in installs:
                if not isinstance(install, dict):
                    continue
                install_path = install.get("installPath")
                if not (isinstance(install_path, str) and install_path):
                    continue
                # Counted BEFORE the scope and enablement filters, not
                # after: a record we read and deliberately dropped is
                # still a record we understood. Counting only survivors
                # would send a manifest holding nothing but other
                # projects' or switched-off installs into the `cache/`
                # fallback, whose rglob would sweep those very installs
                # straight back in.
                understood += 1
                if switched_off or _install_is_for_another_project(install, cwd):
                    continue
                roots.append(Path(install_path))
        if understood or not by_plugin:
            return [p for p in roots if _is_dir_quiet(p)]
    cache = plugins_root / "cache"
    return [cache] if _is_dir_quiet(cache) else []


def _disabled_plugin_keys(settings_paths: list[Path]) -> frozenset[str]:
    """The `plugin@marketplace` keys a settings file switches OFF.

    Claude Code keeps enablement in an `enabledPlugins` object inside the
    same settings files this check already reads for hand-wired bindings,
    keyed exactly as `installed_plugins.json` keys its per-plugin lists.

    `settings_paths` is read in the order given, and a later verdict
    replaces an earlier one, because that order is ascending precedence:
    user scope before this project's, each `.local` override after the
    file it overrides. That ordering is what makes the result per-`cwd`
    rather than global — a project-scope `false` switches a plugin off
    here while leaving it on everywhere else.

    A literal `false` is the only verdict read. `true`, the list form,
    an unrecognised shape, an absent key, a file that will not parse or
    is not an object at all — every one of those leaves the plugin out of
    the returned set, since the failure to prefer here is the false "not
    wired" warn at a user whose plugin is working.
    """
    verdicts: dict[str, bool] = {}
    for path in settings_paths:
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text) if text.strip() else {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        entries = data.get("enabledPlugins")
        if not isinstance(entries, dict):
            continue
        for key, value in entries.items():
            if isinstance(key, str):
                verdicts[key] = value is False
    return frozenset(key for key, off in verdicts.items() if off)


def _install_is_for_another_project(install: dict[Any, Any], cwd: Path) -> bool:
    """Is this install record scoped to a project other than `cwd`?

    An `installed_plugins.json` record carries `"scope": "local"` plus
    the `projectPath` it was installed for when the user installed the
    plugin into one project rather than for the whole user. Claude Code
    enables it there and nowhere else, so its `hooks/hooks.json` is not
    evidence of a live SessionStart binding in any other project — and
    since one runnable binding anywhere wins the live slot in
    `_check_session_start_hook_wired`, letting one through turns a
    genuinely-stale settings binding into a footnote on an `ok`.

    Every uncertainty answers False, i.e. KEEP the record: no `scope`
    (user-scope, the common case), a `scope` this doesn't know, a
    missing or unusable `projectPath`, or a path pair that will not
    compare. The failure to prefer on this side is the false "not wired"
    warn at a plugin user, not the false ok — the same asymmetry that
    puts a `cache/` fallback behind the manifest above.
    """
    if install.get("scope") != "local":
        return False
    project_path = install.get("projectPath")
    if not isinstance(project_path, str) or not project_path:
        return False
    project = _resolved_quiet(Path(project_path))
    here = _resolved_quiet(cwd)
    if project is None or here is None:
        return False
    # An install scoped to a parent covers its subdirectories: doctor run
    # from `<project>/src` is still doctor run inside that project.
    return here != project and project not in here.parents


def _resolved_quiet(path: Path) -> Path | None:
    """`path` made absolute and symlink-free, or None if it cannot be.

    Resolved rather than compared as text because the two sides are
    written by different hands: `projectPath` was recorded by Claude
    Code when the plugin was installed, `cwd` is whatever the shell
    handed this process. A symlinked home, a `/tmp` that is really
    `/private/tmp`, or a relative `cwd` would otherwise make two
    spellings of one directory look like two projects — and reading the
    user's own project as someone else's is how the filter above would
    start causing the false warn it exists to prevent.
    """
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _is_dir_quiet(path: Path) -> bool:
    """`path.is_dir()`, with an OSError reading as "no"."""
    try:
        return path.is_dir()
    except OSError:
        return False


def _session_start_hook_config_candidates(cwd: Path | None = None) -> list[Path]:
    """Existing files that could carry a SessionStart hook binding.

    Two families, both of which a correctly-wired user may use, and
    neither of which is authoritative on its own:

    * Claude Code settings files (user- and project-scope, plus the
      `.local` overrides) — where a manual wiring lands.
    * `hooks.json` manifests inside plugins INSTALLED for `cwd` and not
      switched off there — where a *plugin* install's hooks live. This is
      the load-bearing half: a plugin user never edits settings.json, so
      scanning settings alone would warn at exactly the users who did
      the recommended thing.

    The second family is deliberately not "every `hooks.json` under
    `~/.claude/plugins`". That sweeps in the marketplace catalogue, whose
    manifests belong to plugins that were never installed and whose hooks
    therefore never run, it sweeps in plugins installed into somebody
    else's project, whose hooks do not run here, and it sweeps in plugins
    the user disabled, whose manifests Claude Code reads and then
    declines to register; `_installed_plugin_roots` documents all three
    splits and why counting any of them as wiring flips this check's
    verdict. `cwd` decides the second and third, which is why it is
    passed on rather than only used for the project-scope settings files
    above.

    The first family therefore feeds the second: the settings files are
    where `enabledPlugins` lives, so they are collected first and handed
    to `_disabled_plugin_keys` before the plugin walk begins.

    Only paths that exist are returned, which doubles as the "is this
    even a Claude Code install?" probe — an empty list means we have
    nothing to judge and the check stays silent rather than guessing.
    """
    home = Path.home()
    base = cwd if cwd is not None else Path.cwd()
    candidates = [
        home / ".claude" / "settings.json",
        home / ".claude" / "settings.local.json",
        base / ".claude" / "settings.json",
        base / ".claude" / "settings.local.json",
    ]
    found = [p for p in candidates if p.is_file()]

    plugins_root = home / ".claude" / "plugins"
    if plugins_root.is_dir():
        # `found` holds the settings files and nothing else at this point
        # — the plugin manifests are appended below — so it is exactly the
        # set Claude Code merges `enabledPlugins` across.
        disabled = _disabled_plugin_keys(found)
        # The cap counts matches across ALL installed plugins, not per
        # plugin: it bounds how many files this check opens and parses,
        # and that budget is one budget.
        matches = 0
        try:
            for root in _installed_plugin_roots(
                plugins_root, cwd=base, disabled=disabled
            ):
                for path in root.rglob("hooks.json"):
                    if matches >= _PLUGIN_HOOK_SCAN_CAP:
                        break
                    matches += 1
                    if path.is_file():
                        found.append(path)
                if matches >= _PLUGIN_HOOK_SCAN_CAP:
                    break
        except OSError:
            # An unreadable plugins tree is not this check's problem to
            # report — the settings-file arm still has something to say.
            pass
    return found


def _session_start_hook_commands(data: Any) -> list[str]:
    """EVERY bound SessionStart command string, in file order.

    One structural reader for both file families because the shape is
    identical — Claude Code's settings files and a plugin's `hooks.json`
    both nest `{"hooks": {"<Event>": [{"hooks": [{"type": "command",
    "command": ...}]}]}}`. Everything is defensively type-checked: these
    are FOREIGN files, frequently hand-edited, and a check that raises on
    someone else's unexpected shape is worse than no check.

    Returns commands rather than a bool because "a hook is bound" and
    "that hook can run" are different questions, and only the string can
    answer the second — see `_session_start_hook_broken_path`.

    ALL of them, not the first, and that is the load-bearing part. Claude
    Code runs every matching SessionStart binding it finds, and one FILE
    can hold several: `hooks.SessionStart` is a list of matcher groups
    and each group's `hooks` is a list of commands, so a user who
    hand-wired an absolute path and later installed the plugin's binding
    into the same settings.json has two. Stopping at the first meant a
    stale binding sitting above a runnable one hid it completely, and
    the caller — which scans across files precisely so one good binding
    anywhere wins — never got to see the good one.
    """
    found: list[str] = []
    if not isinstance(data, dict):
        return found
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return found
    entries = hooks.get("SessionStart")
    if not isinstance(entries, list):
        return found
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("hooks")
        if not isinstance(inner, list):
            continue
        for hook in inner:
            if not isinstance(hook, dict):
                continue
            command = hook.get("command")
            if isinstance(command, str) and _SESSION_START_HOOK_MARKER in command:
                found.append(command)
    return found


def _session_start_hook_command(data: Any) -> str | None:
    """The FIRST bound SessionStart command, or None when none is bound.

    Kept as the one-binding convenience over
    `_session_start_hook_commands`. Callers that judge runnability must
    use the plural form — the first binding is not necessarily the one
    that works.
    """
    commands = _session_start_hook_commands(data)
    return commands[0] if commands else None


def _declares_session_start_hook(data: Any) -> bool:
    """Back-compat predicate over `_session_start_hook_commands`."""
    return bool(_session_start_hook_commands(data))


def _session_start_hook_broken_path(
    command: str, *, windows: bool | None = None
) -> str | None:
    """The explicit binary path this command names, when it cannot run.

    ``None`` means "no complaint" — either the command names the binary by
    an explicit path that exists and is executable, or the form is one this
    cannot judge.

    **Judges exactly one shape on purpose: an absolute or relative PATH.**
    A hook whose command is `uvx bettermemory session-start` names a
    launcher that fetches the tool on demand, so `bettermemory` not being
    on `$PATH` proves nothing; `env VAR=1 …`, `sh -c "…"` and
    `${CLAUDE_PLUGIN_ROOT}/…` are all likewise unjudgeable from a string.
    Returning ``None`` for every one of those is the whole design: this
    check's output is a green light on a hook that RECORDS NOTHING, so a
    false alarm here is expensive and a missed alarm merely restores
    today's behaviour.

    The shape it does judge is the one that actually rots, and it is
    always a HAND-WRITTEN one: nothing this project ships writes a hook
    at all. `bettermemory init` patches `mcpServers` and stops there, and
    the plugin's `hooks/hooks.json` binds `uvx bettermemory session-start
    || true` — the launcher form above, deliberately unjudged. A pinned
    path gets into a config when someone runs `uv tool install` and wires
    the answer `command -v bettermemory` gave (this check's own `fix_hint`
    offers exactly that), or pastes a venv path. It then goes stale the
    way the MCP client's binary path does when an environment is rebuilt
    or moved, and it goes stale QUIETLY: the documented bindings all end
    in `|| true` and a hand-written one usually copies that, so the
    missing binary exits 0 and nothing anywhere reports it.

    Locates the executable as the token BEFORE `session-start` rather than
    the first token of the string: `cd /x && …`, `env …` and `uvx …` all
    put something else first, and reading that as the binary is how a
    check invents failures that aren't there.
    """
    # `windows` is a test seam, and an explicit one on purpose. Simulating
    # the platform by monkeypatching `os.name` looks tidier and is a trap:
    # on Python <= 3.11 `pathlib.Path.__new__` dispatches on `os.name`, so
    # a patched `os.name` makes `Path(...)` build a `WindowsPath` on Linux
    # and raise `NotImplementedError` — including inside pytest's own
    # failure formatting, which turns a test failure into an
    # INTERNALERROR. A parameter mutates nothing and behaves the same on
    # every interpreter.
    is_windows = os.name == "nt" if windows is None else windows

    # `posix=False` on Windows because POSIX tokenizing treats `\` as an
    # ESCAPE character: `C:\Users\me\bin\bettermemory` comes back as
    # `C:Usersmebinbettermemory`, which no longer contains a separator, so
    # the path check below silently declines to judge a path that is
    # perfectly judgeable — and the check then reports a stale hook as
    # wired, which is the defect this function exists to close. Non-posix
    # mode keeps backslashes but also keeps the quotes around a quoted
    # token, hence the strip.
    try:
        tokens = shlex.split(command, posix=not is_windows)
    except ValueError:
        # Unbalanced quotes in a foreign, hand-edited file. Not our
        # business to adjudicate.
        return None
    try:
        index = tokens.index("session-start")
    except ValueError:
        return None
    if index == 0:
        return None
    candidate = tokens[index - 1]
    if (
        is_windows
        and len(candidate) >= 2
        and candidate[0] == candidate[-1]
        and candidate[0] in "\"'"
    ):
        candidate = candidate[1:-1]
    # Only an explicit path is judgeable. A bare name may be resolved by a
    # launcher that precedes it, or fetched on demand. Separators come from
    # `is_windows` rather than `os.sep` so the seam covers this too.
    separators = ("\\", "/") if is_windows else ("/",)
    if not any(sep in candidate for sep in separators):
        return None
    # An unexpanded placeholder or variable is a template, not a path.
    if "$" in candidate or "%" in candidate:
        return None
    resolved = Path(candidate).expanduser()
    # `os.X_OK` is meaningful only on POSIX. On Windows `os.access` reports
    # X_OK for any existing file, so this reduces to an existence check
    # there — which is the part that actually rots, and the part the
    # missing-binary case turns on. The exec bit is simply not a state a
    # Windows install can be in, which is why the not-executable test is
    # POSIX-marked and the missing-path one is not.
    if resolved.is_file() and os.access(resolved, os.X_OK):
        return None
    return candidate


def _check_session_start_hook_wired(
    directory: Path,
    config_paths: list[Path] | None = None,
) -> Diagnosis:
    """Is the SessionStart hook that injects the memory hint actually wired?

    CONFIG-SHAPED, NOT TELEMETRY-SHAPED — and that is forced, not a
    preference. `_check_audit_turn_cadence` above can infer a broken Stop
    hook from missing `turn_audited` rows because that hook RECORDS. The
    SessionStart hook deliberately records nothing (a row from it would
    become the anchor `hook._latest_in_process_session` attributes the
    next turn audit against, and would publish a session id no
    `turn_audited` could ever accompany), so it leaves no footprint to
    count. The only observable is the configuration itself.

    Two gates keep this quiet for people it has nothing to offer:

    * an empty store — the hook prints nothing on one, so wiring it
      changes nothing;
    * no discoverable hook config at all — the user isn't running Claude
      Code, or hasn't configured it, and either way we have no evidence.

    EVERY binding in every READABLE candidate is judged, and one
    runnable binding anywhere wins over any number of unrunnable ones.
    Neither the candidate list nor the order within a file is a
    precedence order — Claude Code merges the SessionStart bindings it
    finds across settings files and plugin manifests and runs all of
    them (which is why `_session_start_hook_config_candidates` collects
    both families instead of stopping at the first hit, and why
    `_session_start_hook_commands` returns a LIST instead of the first
    match). So a user who hand-wired an absolute path years ago and has
    since installed the plugin carries two bindings — possibly both in
    one settings.json — and the hint DOES reach the model; a scan that
    stopped at the first match, at either level, would report the whole
    hook broken because one of its two spellings rotted. The stale one
    still gets named — it is dead weight and its `|| true` hides it —
    but as a detail on an `ok`, not as a warning about a hook that works.

    "Readable" is the honest limit and the messages below say so: a
    candidate that would not parse was never judged, so nothing here
    claims anything about what it holds.

    `config_paths` is injectable for tests only; production passes None
    and takes `_session_start_hook_config_candidates()`.
    """
    try:
        active = count_active_memory_files(directory) if directory.exists() else 0
    except OSError as exc:
        return Diagnosis(
            name="session_start_hook",
            status="ok",
            message=f"Could not read the store to check the hint hook: {exc}.",
        )
    if active == 0:
        return Diagnosis(
            name="session_start_hook",
            status="ok",
            message=(
                "Store is empty — the session-start hint would print "
                "nothing, so the hook is not needed yet."
            ),
            details={"active_memories": 0},
        )

    paths = (
        config_paths
        if config_paths is not None
        else _session_start_hook_config_candidates()
    )
    unreadable: list[str] = []
    readable = 0
    live: tuple[Path, str] | None = None
    stale: list[dict[str, str]] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text) if text.strip() else {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            unreadable.append(f"{path}: {exc}")
            continue
        readable += 1
        for command in _session_start_hook_commands(data):
            broken = _session_start_hook_broken_path(command)
            if broken is None:
                # First runnable-or-unjudgeable binding wins the
                # `wired_in` slot, but the scan continues — through the
                # rest of THIS file as well as the remaining candidates.
                # Every one of them still holds a binding Claude Code
                # will run, so a stale sibling is worth naming, and a
                # runnable sibling below a stale one must not be missed.
                if live is None:
                    live = (path, command)
                continue
            stale.append(
                {
                    "wired_in": str(path),
                    "command": command,
                    "unrunnable_path": broken,
                }
            )

    if live is not None:
        path, command = live
        details: dict[str, Any] = {
            "wired_in": str(path),
            "command": command,
            "scanned": len(paths),
            "active_memories": active,
        }
        message = (
            f"SessionStart hint hook is wired ({path}) — new sessions open "
            f"with the per-scope counts already in context."
        )
        if stale:
            details["stale_bindings"] = stale
            message += (
                f" {len(stale)} other binding(s) name a binary that cannot "
                f"run (first: {stale[0]['unrunnable_path']} in "
                f"{stale[0]['wired_in']}); the hint still reaches the model "
                f"through the one above, so this is dead config rather than "
                f"a broken hook."
            )
        return Diagnosis(
            name="session_start_hook",
            status="ok",
            message=message,
            details=details,
        )
    if stale:
        first = stale[0]
        # Counts the BINDINGS actually judged and the files they were
        # actually read from. `len(paths)` would be neither: it includes
        # candidates that failed to parse, and a claim about a file we
        # could not read is a claim we have no standing to make — the
        # unreadable ones are named separately instead.
        scope = (
            f"All {len(stale)} binding(s) found across the {readable} "
            f"readable config file(s) name a binary that cannot run"
        )
        if unreadable:
            scope += f" ({len(unreadable)} further file(s) could not be read)"
        return Diagnosis(
            name="session_start_hook",
            status="warn",
            message=(
                f"SessionStart hint hook is wired ({first['wired_in']}) but "
                f"the binary it names does not exist or is not executable: "
                f"{first['unrunnable_path']}. {scope}. "
                f"The documented bindings all end in "
                f"`|| true` and a hand-written one usually copies that, so "
                f"the failure exits 0 — the hook is configured, contributes "
                f"nothing, and says nothing."
            ),
            fix_hint=(
                "Point the hook at a binary that exists — "
                "`bettermemory init --client claude-code` refreshes "
                "the MCP command path but NOT hook commands, so this "
                "one is edited by hand. `command -v bettermemory` "
                "gives a current path; `uvx bettermemory "
                "session-start || true` avoids pinning one at all."
            ),
            # `first` is spread flat AND repeated inside `stale_bindings`:
            # the flat `wired_in` / `command` / `unrunnable_path` keys are
            # the shape consumers already read off this warn, and the list
            # is what a second stale binding would otherwise have nowhere
            # to appear.
            details={
                **first,
                "stale_bindings": stale,
                "scanned": len(paths),
                "readable": readable,
                "unreadable": unreadable,
                "active_memories": active,
            },
        )

    info: dict[str, Any] = {
        "scanned": len(paths),
        "readable": readable,
        "unreadable": unreadable,
        "active_memories": active,
    }
    if readable == 0:
        # Either there is no hook config to read, or every candidate is a
        # foreign file we couldn't parse. Both are "no evidence", and a
        # check that warns on absent evidence would fire at every Claude
        # Desktop / Cursor / Continue user forever.
        return Diagnosis(
            name="session_start_hook",
            status="ok",
            message=(
                "No readable Claude Code hook config found — skipping the "
                "session-start hint check."
            ),
            details=info,
        )
    return Diagnosis(
        name="session_start_hook",
        status="warn",
        message=(
            f"{active} memories are stored but no SessionStart hook runs "
            f"`bettermemory session-start`, so every new session opens "
            f"blind to them unless the model chooses to call "
            f"`memory_scope_overview`."
        ),
        fix_hint=(
            "Install the plugin (`/plugin install bettermemory@bettermemory`), "
            "which ships the binding in `hooks/hooks.json`; or add a "
            "SessionStart hook running `uvx bettermemory session-start "
            "|| true` to `~/.claude/settings.json`."
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


def _check_sync_quarantine(directory: Path) -> Diagnosis:
    """Are pulled files being held out of the store by the admission chain?

    `sync pull` quarantines a file that fails the size cap, the parser,
    the id-alias check or the credential gate (`quarantine.py`): the
    file stays on disk under git and every active walk skips it. A
    non-empty quarantine is a finding the user should look at, either
    because the remote carried something hostile or because a
    legitimate memory was refused (a credential-shaped example string, a
    hand-edit with a YAML typo) and waits for a fix upstream or a release
    here. An unreadable sidecar is its own warning: `load_quarantine`
    reads it as empty, which admits every file it named.
    """
    from .quarantine import load_quarantine, quarantine_path, sidecar_unreadable

    if not directory.exists():
        return Diagnosis(
            name="sync_quarantine",
            status="ok",
            message="Storage dir does not exist yet — nothing quarantined.",
        )
    problem = sidecar_unreadable(directory)
    if problem is not None:
        sidecar = quarantine_path(directory)
        return Diagnosis(
            name="sync_quarantine",
            status="warn",
            message=(
                f"The quarantine sidecar at {sidecar} is unreadable ({problem}); "
                "it reads as empty, so every file it named is being served."
            ),
            fix_hint=(
                f"Restore {sidecar.name} from a backup or delete it, then run "
                "`bettermemory sync pull`: admission judges every pulled file "
                "again and rewrites the sidecar."
            ),
            details={"sidecar": str(sidecar), "error": problem},
        )
    entries = sorted(
        load_quarantine(directory).values(), key=lambda e: (e.pulled_at, e.filename)
    )
    if not entries:
        return Diagnosis(
            name="sync_quarantine",
            status="ok",
            message="No pulled files are quarantined.",
            details={"count": 0},
        )
    first = entries[0]
    noun = "file is" if len(entries) == 1 else "files are"
    detail = f": {first.detail}" if first.detail else ""
    return Diagnosis(
        name="sync_quarantine",
        status="warn",
        message=(
            f"{len(entries)} pulled {noun} quarantined and excluded from the "
            f"store. First: {first.filename} ({first.reason}{detail})."
        ),
        fix_hint=(
            "`bettermemory sync quarantine` lists them. Fix a file on the host "
            "that wrote it and pull again, or `bettermemory sync quarantine "
            "--release NAME` after correcting it here; `--force` admits a "
            "credential refusal as it is."
        ),
        details={
            "count": len(entries),
            "files": [
                {
                    "file": e.filename,
                    "reason": e.reason,
                    "detail": e.detail,
                    "remote": e.remote,
                    "pulled_at": e.pulled_at,
                }
                for e in entries
            ],
        },
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


#: Memories sampled per scope by the retrieval-discrimination probe. The
#: probe runs one search per sampled memory per query shape, so cost is
#: `2 * _DISCRIMINATION_SAMPLE` searches against a scope-sized pool — the
#: reason this is a fixed sample and not a full sweep. Raising it buys
#: precision at O(sample x pool).
_DISCRIMINATION_SAMPLE = 20

#: Scopes smaller than this are skipped: with a handful of memories almost
#: any query retrieves almost everything, so a ratio computed there says
#: more about the pool size than about discrimination.
_DISCRIMINATION_MIN_POOL = 15

#: Topical-query recall@1 at or below this warns. Calibrated against real
#: scopes rather than picked from theory: heterogeneous scopes cleared it
#: comfortably and a highly coherent one fell well under, so the threshold
#: sits in the gap between the two populations. The rates behind that
#: calibration were never captured as a committed artifact, so they are
#: deliberately not quoted here — bench/retrieval is where a reproducible
#: figure would live, and the per-scope rates this check reports are the
#: ones an operator can actually check. It is a floor for "this scope has
#: a retrieval problem you cannot see", not a quality target.
_DISCRIMINATION_WARN_AT = 0.55

#: Terms per probe query. A memory needs twice this many scorable terms to
#: be sampled, so that its rare slice and its topical slice cannot overlap
#: — an overlap would blur the very contrast the probe measures.
_DISCRIMINATION_QUERY_TERMS = 6


def _discrimination_probe(
    memories: list[Any], scope: str, cfg: Config
) -> tuple[int, float, float] | None:
    """`(sampled, rare_recall_at_1, topical_recall_at_1)` for one scope.

    Each sampled memory is queried for TWICE, using terms taken from its
    own body, and we record whether it comes back ranked first:

    * **rare** — its highest-IDF body terms. The most favourable query a
      lexical ranker can be handed, so this arm is a *control*: when it
      is high the ranker, the index and the fusion are all working, and a
      low topical arm cannot be blamed on them.
    * **topical** — its lowest-IDF body terms, i.e. the vocabulary the
      scope shares. This approximates how a natural-language question
      actually addresses a store ("how do I cut a release"), which
      carries a topic and almost never carries a document's rare tokens.

    The GAP between the two arms is the measurement. It isolates
    query-document vocabulary mismatch from every other cause of a bad
    result, because both arms run against the same pool, ranker, mode and
    corpus statistics — only the query's term rarity differs.

    Both arms rank with the CALLER'S configured ranker — `cfg.behavior`'s
    `search_mode`, `recency_boost_half_life_days`, `conversational` and
    `rescue_expansion` — never `search.search`'s defaults. The probe's
    question is whether PRODUCTION retrieval can find this store, and
    before `cfg` was threaded through it quietly answered for
    hybrid-at-30-days regardless of what `memory_search` actually runs:
    a `[behavior] search_mode = "keyword"` store was measured on a
    ranker it never executes, and keyword mode has no IDF weighting —
    the exact axis this probe's rare/topical contrast is built on. The
    mode passes through `_coerce_search_mode` first because the loader
    only normalises config FILES; a programmatically built
    `BehaviorConfig` reaches consumers unnormalised (that function's
    scope note), and a diagnostic must degrade to the loader's own loud
    "hybrid" fallback rather than die on `search.search`'s runtime mode
    guard.

    Returns None when the scope holds fewer than
    `_DISCRIMINATION_MIN_POOL` memories, and also when no sampled memory
    cleared the term guard below — in both cases there is nothing to
    report rather than a zero to misread.

    A memory is only sampled if it has at least
    `2 * _DISCRIMINATION_QUERY_TERMS` scorable terms, so its rare slice
    and topical slice are disjoint; both arms skip the same memories, so
    the returned count describes each of them.

    **What this deliberately does NOT measure.** A high topical arm does
    not mean retrieval is good — real queries are not bags of a
    document's own words, and this probe never sees a paraphrase, a
    synonym or a question the store has no wording for. It is a floor:
    a low score proves a problem, a high score proves only that this
    particular failure is absent. It is not `memory_helped_rate` and must
    never be reported as a helpfulness or quality number.

    The sample is the first `_DISCRIMINATION_SAMPLE` memories by id.
    ULIDs sort by creation time, so this is the OLDEST slice. Chosen so
    the SAMPLE is reproducible where a random one would not be, and
    disclosed because it is a real bias: a scope whose recent memories are
    worded differently from its old ones is measured on the old ones.

    Reproducible sample is not a reproducible number, and the difference
    matters when reading a report. `search.search` applies recency decay
    against a `now` it defaults to the current time, so among memories a
    topical query cannot separate — the exact case this arm provokes —
    which one lands first can change between runs, moving a scope by a
    sample-quantised step (1/`counted`) and occasionally across the warn
    threshold. Read the arms as coarse levels, not as a trend line, and
    do not diff two runs for a small delta.
    """
    pool = [m for m in memories if scope in m.scopes]
    if len(pool) < _DISCRIMINATION_MIN_POOL:
        return None
    body_idf = search.compute_idf(pool)[0]
    sample = sorted(pool, key=lambda m: m.id)[:_DISCRIMINATION_SAMPLE]
    # One clock for every search in this probe. The arms are only
    # comparable if the recency component is identical across them —
    # letting each call default `now` to its own utcnow() would let the
    # two arms be scored against slightly different decay.
    now = datetime.now(timezone.utc)
    # The configured mode, normalised exactly the way `load_config` would
    # have — see the docstring for why the loader can't be trusted to
    # have run already. The cast is the same `SearchMode` narrowing
    # `handlers.search` performs after its own guard.
    mode = cast(
        search.SearchMode,
        _coerce_search_mode(cfg.behavior.search_mode, config_path=None),
    )

    counted = 0
    rare_first = 0
    topical_first = 0
    for mem in sample:
        # `search.tokenize` and not a `.split()`: the IDF map is keyed by
        # the same normalised, stemmed token stream the ranker indexes, so
        # a raw split silently matches only the words whose surface form
        # already equals their stem — an arbitrary subset, which would
        # bias every query this probe builds.
        scored = {
            term: body_idf[term]
            for term in set(search.tokenize(mem.body))
            if term in body_idf
        }
        # Too few scorable terms to build two meaningfully different
        # queries from — skipping keeps both arms over the same memories,
        # so `counted` describes each of them.
        if len(scored) < _DISCRIMINATION_QUERY_TERMS * 2:
            continue
        counted += 1
        ordered = sorted(scored, key=lambda t: scored[t], reverse=True)
        for terms, is_rare in (
            (ordered[:_DISCRIMINATION_QUERY_TERMS], True),
            (ordered[-_DISCRIMINATION_QUERY_TERMS:], False),
        ):
            hits = search.search(
                pool,
                " ".join(terms),
                scopes=[scope],
                max_results=1,
                now=now,
                mode=mode,
                half_life_days=cfg.behavior.recency_boost_half_life_days,
                conversational=cfg.behavior.conversational,
                rescue_expansion=cfg.behavior.rescue_expansion,
            )
            if hits and hits[0].id == mem.id:
                if is_rare:
                    rare_first += 1
                else:
                    topical_first += 1
    if not counted:
        return None
    return counted, rare_first / counted, topical_first / counted


#: Backticked spans that could be a symbol. Bounded so a backticked
#: paragraph can't become a token.
_ANCHOR_BACKTICKED = re.compile(r"`([^`\n]{2,80})`")
#: A fenced block's contents — commands and config are claims too, and a
#: memory whose evidence is a command line carries no backticked symbol.
_ANCHOR_FENCE = re.compile(r"```[a-zA-Z0-9]*\n(.*?)```", re.DOTALL)
#: Identifier-ish words inside a fence.
_ANCHOR_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]{3,}")
#: Anchors this method can actually read. Everything else — a directory,
#: a venv, a binary, a path outside the repo — is EXEMPT, not a finding.
_ANCHOR_TEXT_SUFFIXES = frozenset(
    {".py", ".md", ".toml", ".json", ".yml", ".yaml", ".cfg", ".ini", ".txt"}
)


def _anchor_tokens(body: str) -> set[str]:
    """Distinctive strings a file supporting this body would contain.

    Deliberately narrow on ONE axis and wide on another, and both
    directions were measured rather than guessed.

    Narrow: a token must look like an identifier, not like English. A
    lowercase word with no underscore is prose that happened to be
    backticked (`my_env`, `node_modules` in a memory about macOS hiding
    dotfiles) and matching on it produces findings about nothing.

    Wide: fenced blocks count. Restricting to backticked spans made the
    check fire on a memory whose load-bearing claim is a shell command
    chain mirrored in `ci.yml` — its anchor was doing real work, and the
    extractor simply could not see the evidence. Reading fences removed
    that false positive without costing a true one.
    """
    out: set[str] = set()

    def _keep(raw: str) -> None:
        leaf = raw.strip().rstrip("()").split(".")[-1]
        if len(leaf) < 4:
            return
        if "_" in leaf or "-" in leaf or (leaf[:1].isupper() and leaf.lower() != leaf):
            out.add(leaf)

    for span in _ANCHOR_BACKTICKED.findall(body):
        token = span.strip().rstrip("()")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:[._][A-Za-z0-9_]+)*", token):
            _keep(token)
    for block in _ANCHOR_FENCE.findall(body):
        for word in _ANCHOR_WORD.findall(block):
            _keep(word)
    return out


# Vocabulary for "this symbol is GONE", which is a different claim from
# the value-change vocabulary in `supersession.CHANGE_CUES` and needs its
# own list. Deliberately excludes "dropped": measured against the store,
# it is the one word here that routinely describes DATA being discarded
# rather than code being removed ("a re-ranker rescuing the dropped
# co-evidence"), and it produced the only false exemption in the set.
_ABSENCE_CUE_RE = re.compile(
    r"\b(?:removed|deleted|purged|stripped|retired|gone|absent|neither"
    r"|no longer|never existed|does ?n[o']t exist|nothing left)\b",
    re.IGNORECASE,
)

# How close a cue must sit to the symbol it retires. Sharing a sentence
# is not enough — a long sentence can mention a live symbol at one end
# and discard something unrelated at the other, which is exactly how the
# false exemption arose. Measured: the two genuine absence records in the
# store bind at 60, the live-symbol false positive needs 110, and the
# verdict is unchanged from 60 through 120, so this sits in a gap rather
# than on a knife edge. A semicolon between cue and symbol disqualifies
# the pairing whatever the distance: `_SENTENCE_END_RE` does not treat
# one as a boundary, so "the `old_helper` shim was removed;
# `detect_supersession` is live" would otherwise retire a live symbol
# sitting well inside the window. Pinned by
# `tests/test_doctor.py::test_attestation_anchors_still_requires_the_live_half_of_a_mixed_body`.
_ABSENCE_CUE_WINDOW = 60


def _absence_claimed(body: str, token: str) -> bool:
    """True when EVERY mention of `token` in `body` sits beside an
    absence cue — the body names the symbol in order to say it is gone.

    All-occurrences rather than any: a body that retires a symbol in one
    breath and describes it live in another is still making a live
    claim, and the live half is what an attestation has to watch.
    """
    hits = [m.start() for m in re.finditer(re.escape(token), body)]
    if not hits:
        return False
    for hit in hits:
        sentence = sentence_around(body, hit)
        offset = body.find(sentence)
        if offset < 0:
            return False
        relative = hit - offset
        if not any(
            abs(cue.start() - relative) <= _ABSENCE_CUE_WINDOW
            and ";"
            not in sentence[min(cue.end(), relative) : max(cue.start(), relative)]
            for cue in _ABSENCE_CUE_RE.finditer(sentence)
        ):
            return False
    return True


def _readable_anchor(raw: str, root: Path) -> Path | None:
    """The attested path as an in-`root`, text-like FILE, or None.

    None means "this method cannot judge it", never "it is wrong". The
    store legitimately attests directories, virtualenvs and absolute
    paths on other machines; treating any of those as a finding is how a
    diagnostic earns a reputation for noise and stops being read.
    """
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            return None
    except (OSError, ValueError):  # pragma: no cover - defensive
        return None
    if not resolved.is_file() or resolved.suffix not in _ANCHOR_TEXT_SUFFIXES:
        return None
    return resolved


def _check_attestation_anchors(directory: Path, cwd: Path) -> Diagnosis:
    """Do a memory's attested paths carry the claims they attest?

    `memory_verify(id, verified_paths=[...])` records the files someone
    read to confirm a memory. Everything downstream trusts that list:
    `path_drift` watches those paths, and `commit_drift` counts commits
    against them to decide whether a calendar-fresh memory has gone
    stale. All of it assumes the list points at the right files.

    `path_drift` only catches an anchor whose file is MISSING. An anchor
    that EXISTS but is irrelevant is invisible to every current signal —
    and it is the worse failure, because the memory then reads green
    forever while its real ground truth moves unwatched. The live
    instance that motivated this: a memory attesting a 3,166-line
    `eval.py` for a claim about symbols that had never been under `src/`
    at all, its drift detector pointed at an unrelated file for months.

    The test is deliberately weak and one-directional: extract the
    body's distinctive symbols, and require at least ONE attested file
    to contain at least ONE of them. That cannot prove an anchor
    supports a claim — only that it mentions the vocabulary. It is a
    smoke alarm, not a proof, and it is tuned so that everything it
    cannot judge is silent:

    * no attestation at all -> nothing to check
    * no identifier-shaped tokens in the body -> exempt, because
      preferences, directives and decisions legitimately have no code
      anchor and flagging them would drown the real findings
    * no attested path this can read -> exempt (see `_readable_anchor`)
    * a memory recorded in ANOTHER worktree -> skipped before its anchors
      are resolved at all. `_readable_anchor` drops an absolute path
      outside this root on its own, but a RELATIVE one it joins to the
      root of the worktree this process happens to be in — so a memory
      written in repo B attesting `pyproject.toml` would otherwise be
      judged against repo A's file and reported as a mis-anchor the user
      cannot fix. `worktrees_match` draws the boundary, which keeps a
      linked worktree of the same checkout on the "here" side; the one
      case it still lets through is a recorded root that is positively
      GONE, where there is no other worktree left to resolve against
      either.

    Measured on a 189-memory store: 63 unanchored, 65 exempt for tokens,
    25 exempt for unreadable anchors, 36 checked, 1 finding — which was
    a genuine mis-anchor. Reported, never auto-fixed: only a reader can
    say which file actually backs a claim.
    """
    from .origin import _git_worktree_root, worktrees_match

    raw_root = _git_worktree_root(cwd)
    root = Path(raw_root).resolve() if raw_root else None
    if root is None:
        return Diagnosis(
            name="attestation_anchors",
            status="ok",
            message="not inside a git worktree; attested paths can't be resolved.",
        )
    try:
        memories = Store(directory).load_all()
    except Exception as exc:  # pragma: no cover - defensive
        return Diagnosis(
            name="attestation_anchors",
            status="ok",
            message=f"could not load memories to check attestations ({exc}).",
        )

    checked = 0
    offenders: list[dict[str, Any]] = []
    for memory in memories:
        anchors = list(getattr(memory, "verified_paths", None) or [])
        if not anchors:
            continue
        # Before any anchor is resolved: a relative anchor is joined to
        # `root`, which is THIS process's worktree, so judging a memory
        # recorded elsewhere reads someone else's file and calls the
        # attestation wrong. `verify._check_anchored_attestations` anchors
        # the same lists to each memory's OWN `origin.worktree_root`; this
        # check has one root to offer, so the honest move is to decline.
        # `worktrees_match` rather than an equality test so the linked-
        # worktree and dead-worktree relaxations stay defined in one place.
        if not worktrees_match(
            memory.origin.worktree_root if memory.origin else None, raw_root
        ):
            continue
        tokens = _anchor_tokens(memory.body)
        if not tokens:
            continue
        # A symbol the body names in order to say it is GONE cannot be in
        # any live file, so requiring one to carry it inverts the test:
        # the memory is punished for being accurate about a removal. Drop
        # those, and if nothing live is left the check has nothing to
        # judge and stays silent, like the no-identifiers case above.
        tokens = {t for t in tokens if not _absence_claimed(memory.body, t)}
        if not tokens:
            continue
        readable = [p for raw in anchors if (p := _readable_anchor(raw, root))]
        if not readable:
            continue
        checked += 1
        if any(
            token in text
            for path in readable
            if (text := _read_text_or_none(path)) is not None
            for token in tokens
        ):
            continue
        offenders.append(
            {
                "id": memory.id,
                "symbols": sorted(tokens)[:8],
                "attested": [str(p.relative_to(root)) for p in readable],
            }
        )

    details = {"checked": checked, "findings": offenders}
    if not offenders:
        return Diagnosis(
            name="attestation_anchors",
            status="ok",
            message=(
                f"{checked} attested memory/-ies checked; each names at least "
                "one symbol its attested files carry. This is a smoke alarm, "
                "not proof the anchors support the claims."
            ),
            details=details,
        )
    return Diagnosis(
        name="attestation_anchors",
        status="warn",
        message=(
            f"{len(offenders)} of {checked} attested memory/-ies name symbols "
            "that appear in NONE of their attested files. Those attestations "
            "may be watching the wrong ground truth — path_drift stays green "
            "on a file that exists, and commit_drift counts commits against "
            f"it. First: {offenders[0]['id']} -> {offenders[0]['attested']}."
        ),
        fix_hint=(
            "Re-read each flagged memory and call memory_verify(id, "
            "verified_paths=[...]) with the files you ACTUALLY read to "
            "confirm it — never the files the body happens to mention, and "
            "never the previous attestation copied forward. If the claim is "
            "genuinely about something outside this repo (a venv, another "
            "machine), the anchor list should say so rather than naming an "
            "in-repo file that carries none of it."
        ),
        details=details,
    )


def _read_text_or_none(path: Path) -> str | None:
    """File contents, or None when unreadable — an unreadable anchor is
    not evidence of a bad anchor."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - defensive
        return None


def _check_memory_provenance(directory: Path) -> Diagnosis:
    """Did every memory enter the store through a path that records?

    Reads the index's provenance census (schema v7; `provenance.py`
    carries the derivation). `unaccounted` is the finding: the event log
    covers the memory's creation window and nothing wrote or pulled it,
    the shape of a file placed by hand. `unclassified` rows are a
    rebuild away from a label and get the reindex hint instead. A store
    with no index has nothing to classify and reads ok, with the census
    reported as null so the two cannot be confused.

    The remedy is deliberate. Nothing here relabels a record: a memory
    you recognise is re-admitted through the store (remove, then
    restore, which stamps `local` and records the restore), and the
    rest are removed. `memory_verify` is not the accept path, because a
    verify vouches for the body's truth, not for how the file arrived.
    """
    from . import index as _index

    counts = _index.provenance_counts(directory)
    if counts is None:
        return Diagnosis(
            name="memory_provenance",
            status="ok",
            message=(
                "No index to read provenance from; a store that has never "
                "been indexed has nothing to classify."
            ),
            details={"counts": None},
        )
    total = sum(counts.values())
    unaccounted = counts.get("unaccounted", 0)
    unclassified = counts.get("unclassified", 0)
    if unaccounted:
        rows = _index.provenance_rows(directory, label="unaccounted") or []
        first = rows[0] if rows else None
        noun = "memory" if unaccounted == 1 else "memories"
        return Diagnosis(
            name="memory_provenance",
            status="warn",
            message=(
                f"{unaccounted} of {total} indexed {noun} entered the store "
                "outside every recorded path: the event log covers the "
                "creation and nothing wrote or pulled it. "
                f"First: {first}."
            ),
            fix_hint=(
                "memory_show each one. To keep a record you recognise, "
                "memory_remove(id, reason=...) then memory_restore(id): the "
                "restore re-admits it through the store and it reads local "
                "from then on. Remove the rest. `memory_health` lists them "
                "under `provenance.unaccounted`."
            ),
            details={"counts": counts, "unaccounted": rows, "first": first},
        )
    if unclassified:
        noun = "row" if unclassified == 1 else "rows"
        return Diagnosis(
            name="memory_provenance",
            status="warn",
            message=(
                f"{unclassified} of {total} indexed {noun} carry no provenance "
                "label yet."
            ),
            fix_hint=(
                "Run `bettermemory reindex`: the rebuild classifies every row "
                "from the event log and the sync repo."
            ),
            details={"counts": counts},
        )
    census = ", ".join(f"{label} {n}" for label, n in sorted(counts.items()))
    return Diagnosis(
        name="memory_provenance",
        status="ok",
        message=(
            f"All {total} indexed memories carry a provenance label "
            f"({census or 'empty index'})."
        ),
        details={"counts": counts},
    )


def _check_memory_content_evidence(directory: Path) -> Diagnosis:
    """Did any memory file change without a store write behind it?

    Every store write records the SHA-256 of the bytes it put on disk
    beside the index row (schema v9), and a rebuild carries a recorded
    hash forward for a non-pulled file whose bytes no longer match it,
    so the evidence survives `bettermemory reindex`. This check computes
    each active file's hash and names the rows where the two disagree:
    a hand edit, a script writing into the directory, a pull done
    outside `bettermemory sync`. Detect-only and single-machine (the
    threat model in SECURITY.md): a writer who also rewrites or deletes
    the index defeats it. A row with no recorded hash is unanchored, a
    rebuild away from one, and is counted rather than judged.

    The remedy is the same asymmetry as `memory_provenance`'s: nothing
    here restamps a file. A change the owner recognises is re-anchored
    by the store's own paths — `memory_verify` (the stamp rewrites the
    file) or `memory_update` — and the rest are removed.
    """
    from . import index as _index
    from .store import PARSE_SKIP_EXCEPTIONS, Store

    if not directory.exists():
        return Diagnosis(
            name="memory_content_evidence",
            status="ok",
            message="Storage dir does not exist yet — nothing to compare.",
        )
    recorded = _index.content_hashes(directory)
    if recorded is None:
        return Diagnosis(
            name="memory_content_evidence",
            status="ok",
            message=(
                "No index to read content hashes from; a store that has never "
                "been indexed records no evidence."
            ),
            details={"changed": None},
        )
    changed: list[dict[str, str | None]] = []
    unanchored = 0
    unreadable = 0
    checked = 0
    try:
        for path, memory in Store(directory).iter_active():
            checked += 1
            expected, label = recorded.get(memory.id, (None, None))
            if expected is None:
                unanchored += 1
                continue
            try:
                current = _index.file_sha256(path)
            except OSError:
                unreadable += 1
                continue
            if current != expected:
                changed.append(
                    {"id": memory.id, "filename": path.name, "provenance": label}
                )
    except PARSE_SKIP_EXCEPTIONS:
        pass
    details: dict[str, Any] = {
        "checked": checked,
        "changed": changed,
        "unanchored": unanchored,
        "unreadable": unreadable,
    }
    if changed:
        noun = "file" if len(changed) == 1 else "files"
        return Diagnosis(
            name="memory_content_evidence",
            status="warn",
            message=(
                f"{len(changed)} of {checked} memory {noun} changed since the "
                "store last wrote, verified or restored them, with no store "
                "write behind the change. "
                f"First: {changed[0]['id']} ({changed[0]['filename']})."
            ),
            fix_hint=(
                "memory_show each one. A change you made yourself is "
                "re-anchored by memory_verify (the stamp rewrites the file) or "
                "memory_update; remove the rest. `bettermemory reindex` does "
                "not clear this — the recorded hash is carried across rebuilds."
            ),
            details=details,
        )
    if unanchored:
        noun = "row carries" if unanchored == 1 else "rows carry"
        return Diagnosis(
            name="memory_content_evidence",
            status="ok",
            message=(
                f"No file changed outside the store; {unanchored} of {checked} "
                f"{noun} no recorded hash yet."
            ),
            fix_hint="Run `bettermemory reindex` to anchor every row.",
            details=details,
        )
    return Diagnosis(
        name="memory_content_evidence",
        status="ok",
        message=f"All {checked} memory files match the bytes the store last wrote.",
        details=details,
    )


def _check_retrieval_discrimination(directory: Path, cfg: Config) -> Diagnosis:
    """Can this store still be found by the questions a model asks?

    Every other check here asks whether memory is stored, parsed, synced
    or fresh. None of them ask whether it can be RETRIEVED, and a store
    can pass all of them while the ranker cannot surface the right memory
    for a plainly-worded question — the failure is silent from both ends,
    because the caller gets five confident-looking hits and never learns
    the one it needed ranked sixth.

    This probes that directly (see `_discrimination_probe`) and warns on
    the topical arm, per scope. The probe ranks with this store's
    configured `[behavior]` retrieval knobs — search mode, recency
    half-life, conversational, rescue expansion — so the verdict is
    about the retrieval `memory_search` actually runs; `cfg` sat unread
    here after the 4.0.0 embedding removal deleted its only use (the
    ad56c07 semantic-lane branch), quietly measuring hybrid-at-30-days
    for every store. A low topical arm beside a high rare arm
    means query-document vocabulary mismatch: lexical retrieval needs the
    query to share rare terms with the target, and inside a topically
    coherent scope the shared vocabulary carries almost no information —
    the scope's own subject words appear in nearly every member, so their
    IDF is near zero. That narrowing is CORRECT (`CorpusStats` prices IDF
    over the collection the caller can actually retrieve, deliberately;
    see c58c836), which is why this is a ceiling to be reported rather
    than a bug to be tuned away: it gets lower as a scope gets more
    coherent, and coherent scopes are what good scope hygiene produces.

    Reported, never auto-fixed. The ceiling it measures is structural to
    lexical ranking, and by project direction the ranker stays code —
    so the reading is the TARGET for retrieval work, not a prompt to
    install anything. The lever available today is the query itself:
    rare terms the memory actually contains single it out where the
    scope's shared vocabulary cannot.
    """
    try:
        memories = Store(directory).load_all()
    except Exception as exc:  # pragma: no cover - defensive
        return Diagnosis(
            name="retrieval_discrimination",
            status="ok",
            message=f"could not load memories to probe retrieval ({exc}).",
        )

    scopes = sorted({s for m in memories for s in m.scopes})
    measured: list[tuple[str, int, float, float]] = []
    for scope in scopes:
        probed = _discrimination_probe(memories, scope, cfg)
        if probed is not None:
            measured.append((scope, *probed))
    if not measured:
        return Diagnosis(
            name="retrieval_discrimination",
            status="ok",
            message=(
                f"no scope holds {_DISCRIMINATION_MIN_POOL}+ memories yet; "
                "too small to measure retrieval discrimination meaningfully."
            ),
        )

    details = {
        "sample_per_scope": _DISCRIMINATION_SAMPLE,
        "warn_at_topical_recall": _DISCRIMINATION_WARN_AT,
        "scopes": [
            {
                "scope": scope,
                "sampled": sampled,
                "rare_term_recall_at_1": round(rare, 3),
                "topical_recall_at_1": round(topical, 3),
                "gap": round(rare - topical, 3),
            }
            for scope, sampled, rare, topical in measured
        ],
    }
    degraded = [
        (scope, rare, topical)
        for scope, _n, rare, topical in measured
        if topical <= _DISCRIMINATION_WARN_AT
    ]
    if not degraded:
        worst = min(measured, key=lambda row: row[3])
        return Diagnosis(
            name="retrieval_discrimination",
            status="ok",
            message=(
                "topical-query retrieval holds in every measured scope "
                f"(worst: {worst[0]} at {worst[3]:.0%} recall@1). Note this "
                "is a floor, not a quality score — see the probe docstring."
            ),
            details=details,
        )
    worst_scope, worst_rare, worst_topical = min(degraded, key=lambda row: row[2])
    return Diagnosis(
        name="retrieval_discrimination",
        status="warn",
        message=(
            f"{len(degraded)} scope(s) retrieve poorly from topically-worded "
            f"queries; worst is {worst_scope} at {worst_topical:.0%} recall@1 "
            f"against {worst_rare:.0%} for rare-term queries. The ranker is "
            "working — the shared vocabulary in a coherent scope carries too "
            "little signal for a lexical query to single a memory out."
        ),
        fix_hint=(
            "Structural to lexical ranking inside a coherent scope — the "
            "scope's own subject words carry near-zero IDF. Query with the "
            "memory's rare terms (identifiers, filenames, error strings) "
            "or split very broad scopes. This ceiling is the standing "
            "target of retrieval work; nothing here needs installing."
        ),
        details=details,
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
            # UnicodeDecodeError sits beside the other two: one client
            # config with a non-UTF8 byte is that FILE's finding, not a
            # doctor crash — without it, `_safe` converted the escape
            # into a whole-check "file an issue" fail.
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
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
                # the BETTERMEMORY binary dynamically — there is no static
                # server path to validate, and the byte-path checks below
                # assume `command` IS the binary — so recognize it via the
                # SHARED init recognizer (one definition, so init and doctor
                # cannot drift on which runner shapes count as ours: bare,
                # version-pinned `bettermemory@latest` / `bettermemory==X`,
                # `--from`, `uv tool run`, and the Windows `uvx.exe`
                # spelling). The RUNNER path is a different matter: an
                # ABSOLUTE `command` — the shape GUI-launched clients
                # realistically pin, since they inherit the minimal PATH the
                # binary_on_path story describes — IS statically checkable
                # and rots exactly like a pinned server binary when uv is
                # moved or reinstalled, so judge its existence and report it
                # stale like any other dead path. The bare `"uvx"` name stays
                # unjudged, mirroring `_session_start_hook_broken_path`'s
                # absolute-path-only rule, and `binary_exists` records the
                # measured answer, never a literal.
                args = entry.get("args")
                if (
                    Path(command).stem.lower() in {"uvx", "uv"}
                    and isinstance(args, list)
                    and command_launches_bettermemory(command, args, resolved_binary)
                ):
                    runner_exists = (
                        Path(command).exists() if Path(command).is_absolute() else True
                    )
                    findings.append(
                        {
                            "client": client_name,
                            "config_path": str(path),
                            "entry_name": entry_name,
                            "command": command,
                            "binary_exists": runner_exists,
                            "matches_resolved_binary": runner_exists,
                            "runner": Path(command).name,
                        }
                    )
                    if not runner_exists:
                        stale_rows.append(
                            f"{client_name} ({path}): {command} no longer exists"
                        )
                        stale_clients.append(client_name)
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
                    joined = b"".join(chunks)
                    # Cut at the terminator before searching: the chunk
                    # that contains the blank line also carries the
                    # first body bytes (or, capped with no terminator,
                    # the buffer is body-heavy), and a `Name:` line in
                    # the BODY — a long_description echoing metadata —
                    # must not green-light a header that lacks the
                    # field. Earliest of the two spellings wins; pure
                    # CRLF text never contains bare `\n\n`.
                    cut = min(
                        (
                            idx
                            for idx in (
                                joined.find(b"\n\n"),
                                joined.find(b"\r\n\r\n"),
                            )
                            if idx != -1
                        ),
                        default=-1,
                    )
                    if cut != -1:
                        joined = joined[:cut]
                    header_section = joined.decode("utf-8", errors="replace")
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
        # One load for the three checks that need the parsed memories.
        # Constructed here rather than inside them so all three report on
        # the SAME snapshot of a directory other agents may be writing to.
        memory_load = _MemoryLoad(directory)
        checks.append(
            _safe(
                "memory_parse_health",
                lambda: _check_memory_parse_health(directory, memory_load),
            )
        )
        # Reads the bodies `memory_parse_health` only counted: parsing
        # cleanly and being complete are different claims, and until this
        # check existed the report made the first and was read as the
        # second.
        checks.append(
            _safe(
                "memory_body_completeness",
                lambda: _check_memory_body_completeness(directory, memory_load),
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
                lambda: _check_index_health(directory, memory_load),
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
                "session_start_hook",
                lambda: _check_session_start_hook_wired(directory),
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
                "sync_quarantine",
                lambda: _check_sync_quarantine(directory),
            )
        )
        checks.append(
            _safe(
                "store_nested_in_parent_repo",
                lambda: _check_store_nested_in_parent_repo(directory),
            )
        )
        checks.append(
            _safe(
                "retrieval_discrimination",
                lambda: _check_retrieval_discrimination(directory, cfg),
            )
        )
        checks.append(
            _safe(
                "attestation_anchors",
                lambda: _check_attestation_anchors(directory, Path.cwd()),
            )
        )
        checks.append(
            _safe(
                "memory_provenance",
                lambda: _check_memory_provenance(directory),
            )
        )
        checks.append(
            _safe(
                "memory_content_evidence",
                lambda: _check_memory_content_evidence(directory),
            )
        )

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
    """chmod 0700 a store directory that is unwritable OR over-permissive.

    Two fixable branches, both converging on the same 0700:

    * not writable by its owner — the historical case;
    * writable but carrying group/other bits (0o755 from the default
      umask). This one is the reason the writability test is no longer a
      bare early-return: such a directory passes `os.access(W_OK)`
      cleanly, so the chmod below used to be unreachable for it.

    Everything else stays a hint: missing-parent, path-is-a-file, and
    probe-write failures (ENOSPC, read-only mounts) all need decisions or
    resources a chmod cannot supply. 0700 rather than a minimal `u+w` is
    deliberate — the store carries private user data, so the heal
    converges on the private posture the event-log writer already
    enforces for its own file; the prior mode is recorded in the result
    (and the event log) so the change is reversible from the audit trail.
    """
    if cfg is None or directory is None:
        return None
    if not directory.exists() or not directory.is_dir():
        return None
    old_mode = stat.S_IMODE(directory.stat().st_mode)
    over_permissive = sys.platform != "win32" and bool(old_mode & 0o077)
    if os.access(directory, os.W_OK) and not over_permissive:
        # Already writable (or running as root, where os.access is blind
        # to modes) AND not leaking to group/other — whatever made the
        # check red, it isn't the chmod-able branch.
        #
        # The `over_permissive` half is why this is not a bare
        # `os.access` early-return: a 0o755 store is perfectly writable
        # by its owner, so the plain writability test returned None here
        # and the chmod below was unreachable for the one case it most
        # needed to handle.
        return None
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
    """chmod 0600 every existing-but-unwritable event-log segment.

    The active log is sharded (v3.24.0), so more than one segment can
    end up mispermissioned; heal them all in one pass. A chmod that
    fails does NOT abort the pass and does NOT erase the segments
    already healed: the loop continues and the FixResult reports the
    real split — `applied` is True whenever at least one chmod landed,
    `details["healed"]`/`details["failed"]` name which, and `error`
    carries every failure. Reporting `applied=False` after mutating
    earlier segments (the pre-fix shape) told the caller nothing had
    changed while the store was in fact partially mutated — the same
    "attempted is not applied" honesty 3.22.1 established.

    The cannot-be-created branch (directory not writable) is the
    storage_directory fixer's cause, not this one's — when that fix
    lands first, the final full re-run reports this check healed too.
    0600 matches the mode the Recorder itself sets on first write. A
    symlinked segment is DECLINED, never chmod'd through (chmod follows
    symlinks, so 'fixing' it would mutate whatever the link points at —
    the same refuse-on-symlink standard `_check_stale_config_lockfiles`
    and `init._heal_stale_sidecar_lockfile` hold).

    A segment that VANISHES between the glob and its stat/chmod is
    skipped, not raised: `doctor --fix` runs every fixer in one pass, so
    an uncaught FileNotFoundError from a concurrently-rotated segment
    took the whole run down — including the fixes that had nothing to
    do with the event log.
    """
    if directory is None or not directory.exists():
        return None
    healed: list[tuple[Path, int]] = []
    # Failed segments carry their pre-fix mode ALREADY RENDERED, so the
    # unstattable arm below can say "unknown" instead of inventing a
    # numeric mode it never read.
    failed: list[tuple[Path, str]] = []
    errors: list[str] = []
    for log_path in _event_log_files(directory):
        if log_path.is_symlink() or os.access(log_path, os.W_OK):
            continue  # decline symlinks; skip already-writable segments
        try:
            old_mode = stat.S_IMODE(log_path.stat().st_mode)
        except FileNotFoundError:
            # The segment vanished between `_event_log_files`' glob and
            # this stat (concurrent rotation, a tidy-up, the user
            # deleting the log). Nothing left to heal and nothing to
            # report — the same not-a-finding shape
            # `_probe_event_log_segment` gives a vanished segment. This
            # guard is why the stat is INSIDE a try: uncaught, it
            # aborted the whole `doctor --fix` run with a traceback,
            # taking every not-yet-run fixer down with it.
            continue
        except OSError as exc:
            # Unstattable for any other reason (a denied parent
            # directory). Report rather than swallow: the chmod below
            # could not have worked either, and silence would leave the
            # user with a red check and no explanation.
            failed.append((log_path, "unknown"))
            errors.append(f"{log_path}: {exc.__class__.__name__}: {exc}")
            continue
        try:
            log_path.chmod(0o600)
        except OSError as exc:
            failed.append((log_path, oct(old_mode)))
            errors.append(f"{log_path}: {exc.__class__.__name__}: {exc}")
            continue
        healed.append((log_path, old_mode))
    if not healed and not failed:
        return None
    healed_paths = [str(p) for p, _ in healed]
    failed_paths = [str(p) for p, _ in failed]
    old_modes = {str(p): oct(m) for p, m in healed}
    old_modes.update({str(p): m for p, m in failed})
    # Re-run the check whichever way the pass went: after_status must
    # describe the store as it now IS, and a partial heal leaves it red.
    after = _check_event_log_writable(directory)
    if healed and failed:
        message = (
            f"chmod → 0o600 on {len(healed)} of {len(healed) + len(failed)} "
            f"event-log segment(s): healed "
            + ", ".join(healed_paths)
            + "; failed "
            + ", ".join(failed_paths)
        )
    elif healed:
        message = f"chmod → 0o600 on {len(healed)} event-log segment(s): " + ", ".join(
            healed_paths
        )
    else:
        message = "chmod 0600 on " + ", ".join(failed_paths) + " failed"
    return FixResult(
        check="event_log",
        action="chmod_event_log",
        applied=bool(healed),
        before_status=diagnosis.status,
        after_status=after.status,
        message=message,
        error="; ".join(errors) if errors else None,
        details={
            "healed": healed_paths,
            "failed": failed_paths,
            "old_modes": old_modes,
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
    """Reconcile the store sync repo's `.gitignore` with the canonical
    pattern list through `sync._reconcile_gitignore` — the SAME writer
    `sync push` runs — applied without the user having to remember that
    a sync is also what refreshes it.

    ONE WRITER, ONE POLICY. This fixer used to rewrite the file
    WHOLESALE (`"\\n".join(_GITIGNORE_LINES)`) whenever the on-disk text
    differed from the canonical block, so `doctor --fix` silently
    deleted every line the user had added to their own store's
    `.gitignore` — a store's gitignore is a file users legitimately
    edit (machine-local exclusions live there too). `sync` had already
    been made deliberately APPEND-ONLY for exactly that reason, which
    left two writers on one file with OPPOSITE policies: whichever ran
    last decided whether the user's lines survived. Delegating here
    rather than re-implementing the policy is the point — a second
    implementation is how the two drifted apart in the first place.

    This is a PARTIAL fix by design: gitignore cannot untrack, so the
    check stays red until the user runs the `git rm --cached`
    remediation from the hint. `sync push` now reconciles the gitignore
    on its own push path, so the manual untrack does stick afterwards —
    but the untrack itself, and the history rewrite / secret rotation
    behind it, stays manual forever.

    Nothing missing (including the already-canonical shape, and the
    UNREADABLE file `_reconcile_gitignore` declines to clobber) means
    nothing auto-applicable: the fixer returns None and the finding
    stays purely manual.
    """
    if directory is None or not directory.exists():
        return None
    # Same lazy-import rationale as `_check_sync_tracked_ignored`: a
    # top-level `from .sync import …` here would be circular (sync
    # imports `DOCTOR_PROBE_FILENAME` from this module).
    from .sync import _is_repo, _reconcile_gitignore

    if not _is_repo(directory):
        return None
    gitignore = directory / ".gitignore"
    try:
        outcome = _reconcile_gitignore(directory)
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
    # `_reconcile_gitignore` returns a `_GitignoreReconcile`, not the bare
    # `list[str]` it once did, and it no longer RAISES on a write failure
    # — it stands down and reports, so the `except OSError` above no
    # longer sees that case. The two stand-down halves are not equivalent
    # to this fixer, so branch on `failed_stage` rather than on `error`:
    #
    # * WRITE — we knew exactly what to append and could not (ENOSPC,
    #   read-only mount, a directory at the path). The user asked --fix
    #   to do a thing, it was attempted, it did not happen. Report an
    #   honest not-applied FixResult carrying the reason.
    # * READ — we never learned what the file contains, so we cannot
    #   enumerate what an overwrite would destroy. Declining is the
    #   CORRECT outcome rather than a failed job: the finding stays
    #   manual with its hint, the bytes are untouched, and sync logs
    #   why. Manufacturing a FixResult here would report a refusal we
    #   are right to make as a failure.
    #
    # Pinned by `test_fix_sync_gitignore_reports_write_failure` and
    # `test_fix_sync_gitignore_leaves_an_unreadable_gitignore_alone`.
    if outcome.failed_stage == "write":
        return FixResult(
            check="sync_tracked_ignored",
            action="refresh_gitignore",
            applied=False,
            before_status=diagnosis.status,
            after_status=diagnosis.status,
            message=f"refreshing {gitignore} failed",
            error=outcome.error,
            details={"gitignore": str(gitignore)},
        )
    added = outcome.added
    if not added:
        # Either every canonical pattern is already covered, or the read
        # half stood down. The remaining remediation is the manual
        # untrack; nothing auto-applicable.
        return None
    after = _check_sync_tracked_ignored(directory)
    plural = "" if len(added) == 1 else "s"
    return FixResult(
        check="sync_tracked_ignored",
        action="refresh_gitignore",
        applied=True,
        before_status=diagnosis.status,
        after_status=after.status,
        message=(
            f".gitignore reconciled with the canonical pattern list — "
            f"added {len(added)} missing pattern{plural} (stops future "
            f"staging); the already-tracked files still need the manual "
            f"`git rm --cached` remediation in the hint"
        ),
        details={"gitignore": str(gitignore), "added": added},
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


def _record_fix_events(
    cfg: Config | None, directory: Path | None, applied: list[FixResult]
) -> None:
    """One `doctor_fix` event per applied fix — the same observability
    bar as every other mutating surface, under the same telemetry
    settings: `[telemetry] enabled = false` disables the event log for
    doctor exactly like it does for the server, so an opted-out user
    gets NO event (the CLI/JSON output is then the only record, which
    is still honest). Best-effort like the Recorder itself: a logging
    hiccup must never fail a fix that already landed. Skipped when no
    store directory exists to host the log. A None `cfg` (programmatic
    callers only — `_fix_context` never pairs a real directory with a
    None config) falls back to the default telemetry posture, matching
    the Recorder's own defaults."""
    if not applied or directory is None or not directory.exists():
        return
    try:
        from .events import Recorder
        from .session import SessionState

        telemetry = cfg.telemetry if cfg is not None else TelemetryConfig()
        recorder = Recorder(
            root=directory,
            session_id=SessionState().session_id,
            enabled=telemetry.enabled,
            max_bytes=telemetry.max_bytes,
            log_queries_verbatim=telemetry.log_queries_verbatim,
        )
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
    _record_fix_events(cfg, directory, [f for f in fixes if f.applied])
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


def render_fixes_text(
    fixes: list[FixResult], pre: DoctorReport, post: DoctorReport
) -> str:
    """The `--fix` tail of the text report. `pre` is the PRE-fix report
    — the "before" half of the before/after story — and `post` is the
    re-run whose check list the caller renders above this section (the
    same report as `pre` when nothing was applied). The manual-only
    list is computed against POST state, not just "had no FixResult":
    a fix can heal a NEIGHBOUR check's cause (the storage chmod
    unblocks event-log creation), and a pre-fix red the post re-run
    shows green has no hint left to point at — listing it as
    manual-only right under its own green check line would be a
    self-contradiction. Says so explicitly when there was nothing to
    fix, per the no-op contract."""
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
        pre_status = {c.name: c.status for c in pre.checks}
        post_status = {c.name: c.status for c in post.checks}
        attempted = {f.check for f in fixes}
        rest = [n for n in red if n not in attempted]
        # A pre-fix red with no FixResult of its own that the post
        # re-run shows green was healed by another fix; a name the
        # post report dropped entirely stays manual (conservative).
        healed = [n for n in rest if post_status.get(n) == "ok"]
        manual = [n for n in rest if post_status.get(n) != "ok"]
        for name in healed:
            out.append(
                f"  ✓ {name}: healed by another fix above (was {pre_status[name]})"
            )
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
        # pass reports that honestly. Gated on ATTEMPTED (fixes
        # non-empty), not applied: the vanished-artifact race yields an
        # applied=False fix whose red cause is already gone, and an
        # any(applied) gate would freeze the stale pre report — exit 1
        # on a healthy machine, with a payload contradicting the fix's
        # own after_status="ok". Attempted-but-genuinely-failed shapes
        # re-report still-red (same exit as pre; one extra diagnostics
        # pass is the honest price). Nothing attempted skips the
        # re-run — the pre report is still current.
        post = run_diagnostics() if fixes else report
        if json_out:
            sys.stdout.write(render_json(post, fixes=fixes))
        else:
            sys.stdout.write(render_text(post))
            sys.stdout.write(render_fixes_text(fixes, report, post))
        return _EXIT_CODE_BY_STATUS[post.overall]
    if json_out:
        sys.stdout.write(render_json(report))
    else:
        sys.stdout.write(render_text(report))
    return _EXIT_CODE_BY_STATUS[report.overall]
