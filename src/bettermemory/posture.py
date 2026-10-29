"""`bettermemory security audit`: does this install match what SECURITY.md
assumes of it?

SECURITY.md rests the store's integrity on a few facts about the host that
nothing re-checks once the files exist: the store directory is ``0o700`` and
the store file ``0o600``; the key lives outside the store directory, owner-only,
and matches the store's recorded fingerprint; the daemon's state file, which
holds the bearer token, is owner-only in an owner-only directory; the daemon
listens on 127.0.0.1 only. `_fsutil.ensure_owner_only_dir` and `store._connect`
heal modes as they open, best-effort, and a sandboxed or network filesystem
can refuse the chmod. A store that sits in a git working tree or a synced
folder leaves the host with whatever the provider sees, and a store chosen
by a ``./.claude-memory`` in the working directory is whatever the repository
shipped. Each of those facts is one read of the filesystem, so the audit reads
them all and says which hold.

What the audit is not:

* **Not a fixer.** It never chmods, chowns, rekeys or moves anything. A
  finding carries the one-line fix for the user to run. A read-only audit
  that healed what it reported would answer "ok" the second time it ran and
  leave no record of what it found.
* **Not a store opener.** `Store.open`, even with ``allow_rekey=False``, goes
  through `store._connect`, which sets WAL mode and chmods the store file and
  its siblings to ``0o600``, and runs the additive membership schema step.
  The audit reads the two meta values it needs (the store id and the key
  fingerprint) through the read-only commands' open
  (`store_schema.connect_read_only`), which reads the file in place with
  ``mode=ro`` where that creates nothing, and a closed WAL-mode store from
  a private copy, never the live file with ``immutable=1``. The log itself
  is `bettermemory log verify`'s to check; the audit points at it.
* **Not a network client.** The daemon check reads the state file and the
  pid, nothing more: this build binds 127.0.0.1 and no other address
  (`daemon.serve`'s ``host``, `_daemon_client.HOST`), and the state file
  records the port and the pid but no address, so a recorded ``host``
  (should a later build write one) is checked and its absence reads as the
  fixed address.
* **Not a writer of anything else.** The config is read without the
  first-run write (`sandbox_settings.load_config_read_only`), so a fresh
  account does not get a default config.toml from an audit, and the
  managed file's pins apply whether or not the user's file exists. Claude Code's settings files
  are read, never written. ``git`` runs through `_git.run_git`, with its
  hooks and fsmonitor pinned off and optional locks disabled.

Severity. ``high`` is a finding under which a SECURITY.md promise does not
hold now: a secret readable or a file writable by another account, a key
that is missing or does not match the store, a key that travels with the
store, a store tracked by git. ``warn`` is exposure short of that: a
directory with group or other bits, a store in a synced folder or an
unignored working tree, a store chosen by the working directory, Claude
Code's sandbox enabled with a gap in it. ``info`` and ``ok`` report facts and
change no exit code, and ``skip`` names a check this platform cannot run
(file modes and owners on Windows). The exit code is 1 when any finding is
high, 2 when the worst is a warning and 0 otherwise.

The checks run in the order a reader fixes them: the store's selection and
the overrides in effect, file modes and owners, the key, where the store and
the key live, the daemon, the config's settings worth knowing, and Claude
Code's sandbox settings against what `bettermemory security sandbox` would
print for this host.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _daemon_client, claude_paths, githead, sandbox_settings
from ._git import run_git
from .config import (
    ENV_DIR_OVERRIDE,
    KEYS_DIR_ENV,
    PROJECT_DIR_NAME,
    STORE_FILENAME,
    Config,
)
from .log import STORE_ID_PATTERN, KeyRing

if TYPE_CHECKING:
    from .store_schema import StoreRefusedError

#: Every status a finding can carry, mildest first.
STATUSES: tuple[str, ...] = ("ok", "info", "skip", "warn", "high")
_EXIT_FOR: dict[str, int] = {"high": 1, "warn": 2}

#: The overrides an operator should know are in effect: each moves a
#: directory away from the platform default, and the hooks and the MCP
#: server see them only when the harness passes them on.
_OVERRIDES: tuple[str, ...] = (
    ENV_DIR_OVERRIDE,
    KEYS_DIR_ENV,
    _daemon_client.STATE_DIR_ENV,
)

#: An IPv4 address in one of the legacy spellings the C resolver
#: (``inet_aton``) still accepts: fewer than four parts (``127.1``), one
#: number (``2130706433``), octal or hex parts (``0177.0.0.1``,
#: ``0x7f.1``). Held to digits, hex digits, ``x`` and dots before it is
#: parsed, since ``inet_aton`` stops at the first space and would read
#: ``127.0.0.1 junk`` as an address.
_LEGACY_IPV4 = re.compile(r"[0-9][0-9a-fA-FxX.]*")

_geteuid = getattr(os, "geteuid", None)


@dataclass(frozen=True)
class Finding:
    """One check's result. `id` is stable for scripts (``mode.key``,
    ``key.match``); `summary` is the one line a person reads; `fix` the
    one-line remedy where there is one; `details` the values behind the
    summary, for ``--json``."""

    id: str
    status: str
    summary: str
    path: str | None = None
    fix: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "summary": self.summary,
            "path": self.path,
            "fix": self.fix,
            "details": self.details,
        }


@dataclass
class Audit:
    """Every finding for one store, and the verdict they add up to."""

    store: Path
    store_rule: str
    platform: str
    findings: list[Finding]

    @property
    def status(self) -> str:
        """``high`` when any finding is high, else ``warn`` when any is a
        warning, else ``ok``."""
        seen = {finding.status for finding in self.findings}
        for status in ("high", "warn"):
            if status in seen:
                return status
        return "ok"

    @property
    def exit_code(self) -> int:
        return _EXIT_FOR.get(self.status, 0)

    def counts(self) -> dict[str, int]:
        return {
            status: sum(1 for finding in self.findings if finding.status == status)
            for status in STATUSES
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "store": str(self.store),
            "store_rule": self.store_rule,
            "platform": self.platform,
            "counts": self.counts(),
            "findings": [finding.to_json() for finding in self.findings],
        }


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------


def audit(*, cwd: Path | None = None, platform: str | None = None) -> Audit:
    """Run every check against the store this process resolves from `cwd`
    (the working directory by default), as `platform` (``sys.platform`` by
    default) would see it. Reads only."""
    platform = sys.platform if platform is None else platform
    findings: list[Finding] = []
    try:
        config = sandbox_settings.load_config_read_only()
    except (ValueError, OSError) as exc:
        findings.append(
            Finding(
                "config.load",
                "warn",
                f"the config does not load: {exc}",
                fix=(
                    "fix the file the error names; the daemon and the CLI refuse "
                    "to start on it, and this audit read the defaults instead"
                ),
            )
        )
        config = Config()
    found = sandbox_settings.locations(config, cwd=cwd)
    by_role = {loc.role: loc for loc in found}
    store_dir = by_role["store"].path
    keys_dir = by_role["keys"].path
    state_dir = by_role["state"].path
    store_file = store_dir / STORE_FILENAME
    rule = by_role["store"].source

    findings.append(_selection(store_dir, rule))
    findings.extend(_overrides())

    meta, meta_error, refused = _meta_or_error(store_file)
    store_id = (meta or {}).get("store_id")
    # The meta is whatever the store file says, and a ring is built for a
    # well-formed id only (`log.KeyRing`): an id holding a path would have
    # the ring checks read, and glob, outside the keys directory. A
    # malformed id is the key check's one finding.
    keyring = (
        KeyRing(keys_dir, store_id)
        if store_id and STORE_ID_PATTERN.fullmatch(store_id)
        else None
    )

    findings.extend(
        _modes(
            platform=platform,
            store_dir=store_dir,
            store_file=store_file,
            keys_dir=keys_dir,
            keyring=keyring,
            state_dir=state_dir,
        )
    )
    findings.append(
        _key(store_file, keys_dir, meta, meta_error, keyring, refused=refused)
    )
    findings.append(_placement(store_dir, keys_dir))
    findings.extend(_where(store_dir, store_file, keys_dir))
    findings.extend(_daemon(state_dir, store_file))
    findings.extend(_settings(config))
    findings.extend(_claude(config, cwd=cwd, platform=platform))
    from .location_git import gitfile_posture  # ENG-158: the location family's

    findings.append(Finding(**gitfile_posture(None if cwd is None else str(cwd))))
    return Audit(
        store=store_file, store_rule=rule, platform=platform, findings=findings
    )


# ---------------------------------------------------------------------------
# The store's selection and the overrides
# ---------------------------------------------------------------------------


def _selection(store_dir: Path, rule: str) -> Finding:
    details = {"directory": str(store_dir), "rule": rule}
    if rule == f"./{PROJECT_DIR_NAME}":
        return Finding(
            "store.selection",
            "warn",
            f"the store was chosen by ./{PROJECT_DIR_NAME} in the working "
            f"directory: {store_dir}",
            path=str(store_dir),
            fix=(
                "name the store you mean with BETTERMEMORY_DIR or [storage] "
                "directory; otherwise a repository that ships a "
                f"{PROJECT_DIR_NAME} directory becomes the store the hooks and "
                "the MCP server serve from it"
            ),
            details=details,
        )
    return Finding(
        "store.selection",
        "ok",
        f"store {store_dir} ({rule})",
        path=str(store_dir),
        details=details,
    )


def _overrides() -> Iterator[Finding]:
    named = {name: os.environ[name] for name in _OVERRIDES if os.environ.get(name)}
    if not named:
        return
    listed = ", ".join(f"{name}={value}" for name, value in named.items())
    yield Finding(
        "env.overrides",
        "info",
        f"in effect: {listed}",
        fix=(
            "the hooks and the MCP server must see the same variables, or "
            "they serve another store with other keys"
        ),
        details=named,
    )


# ---------------------------------------------------------------------------
# File modes and owners
# ---------------------------------------------------------------------------


def _modes(
    *,
    platform: str,
    store_dir: Path,
    store_file: Path,
    keys_dir: Path,
    keyring: KeyRing | None,
    state_dir: Path,
) -> Iterator[Finding]:
    """Mode and owner of every file and directory SECURITY.md names, where
    it exists. A missing one is not a mode finding: the key's absence is
    the key check's, and the rest are created on first use."""
    if platform == "win32":
        yield Finding(
            "mode",
            "skip",
            "file modes and owners not checked on Windows: access there is "
            "the ACLs' and the account's",
        )
        return
    euid = _geteuid() if _geteuid is not None else None
    state_file = _daemon_client.state_file_for(state_dir, store_file)
    items: list[tuple[str, str, Path, bool, bool]] = [
        ("mode.store_dir", "store directory", store_dir, True, False),
        ("mode.store_file", "store file", store_file, False, True),
        ("mode.store_wal", "store WAL", _sibling(store_file, "-wal"), False, True),
        (
            "mode.store_shm",
            "store shared memory",
            _sibling(store_file, "-shm"),
            False,
            True,
        ),
        ("mode.keys_dir", "keys directory", keys_dir, True, False),
    ]
    if keyring is not None:
        items.append(("mode.key", "key file", keyring.current_path(), False, True))
        # `<store_id>.<fingerprint prefix>.key`; the current key, with no
        # middle part, is not among them.
        for retired in keyring.retired_paths():
            items.append(("mode.retired_key", "retired key", retired, False, True))
        items.append(
            ("mode.head", "head checkpoint", keyring.head_path(), False, False)
        )
        # Where this host's history of the store begins (`log.KeyRing`):
        # no secret, as the head is none, but another account that can
        # write it can move the anchor.
        items.append(
            ("mode.origin", "origin anchor", keyring.origin_path(), False, False)
        )
        # The store file the ring is bound to (`log.KeyRing.bind_store`),
        # written 0o600 as the head and the anchor are. No secret either,
        # but another account that can write it can name a file of its
        # own as the ring's store: the owner's open of the real store then
        # reads the binding as another file still holding the id, and
        # opens for reading only, or, pointed at nothing, lets a copy
        # claim the ring. Graded as the head and the anchor are.
        items.append(
            ("mode.binding", "key ring binding", keyring.binding_path(), False, False)
        )
    items.extend(
        [
            ("mode.state_dir", "state directory", state_dir, True, False),
            ("mode.state_file", "daemon state file", state_file, False, True),
            (
                "mode.daemon_log",
                "daemon log",
                _daemon_client.log_file_for(state_dir, store_file),
                False,
                False,
            ),
        ]
    )
    for finding_id, label, path, directory, secret in items:
        finding = mode_finding(
            finding_id, label, path, directory=directory, secret=secret, euid=euid
        )
        if finding is not None:
            yield finding


def _sibling(store_file: Path, suffix: str) -> Path:
    return store_file.with_name(store_file.name + suffix)


def mode_finding(
    finding_id: str,
    label: str,
    path: Path,
    *,
    directory: bool,
    secret: bool,
    euid: int | None,
) -> Finding | None:
    """The finding for one path, or None when it does not exist.

    The expected mode is ``0o700`` for a directory and ``0o600`` for a file,
    the modes the store, the key ring and the state file are created with.
    Graded by what an extra bit lets another account do: write (replace a
    key, a state file, a store page) is high everywhere; read is high on a
    secret (the store's pages, a key, the token) and a warning elsewhere
    (a directory listing, the head checkpoint, the origin anchor, the key
    ring binding, the daemon's log); any other group or other bit is a
    warning. An owner other than this account is high: that account can
    rewrite the file whatever its mode says."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return Finding(
            finding_id,
            "warn",
            f"{label} {path} cannot be read: {exc.strerror or exc}",
            path=str(path),
        )
    mode = stat.S_IMODE(st.st_mode)
    expected = 0o700 if directory else 0o600
    extra = mode & 0o077
    details: dict[str, Any] = {
        "mode": f"{mode:04o}",
        "expected": f"{expected:04o}",
        "uid": st.st_uid,
    }
    chmod = f"chmod {expected:o} {shlex.quote(str(path))}"
    if euid is not None and st.st_uid != euid:
        return Finding(
            finding_id,
            "high",
            f"{label} {path} is owned by uid {st.st_uid}, not this account "
            f"(uid {euid})",
            path=str(path),
            fix=(
                "find out why another account owns it, then chown it to this "
                "account or move the store"
            ),
            details=details,
        )
    if extra & 0o022:
        status, why = "high", "writable by group or others"
    elif secret and extra & 0o044:
        status, why = "high", "readable by group or others"
    elif extra:
        status, why = "warn", "has group or other bits"
    else:
        return Finding(
            finding_id,
            "ok",
            f"{label} {mode:04o}, owned by this account",
            path=str(path),
            details=details,
        )
    return Finding(
        finding_id,
        status,
        f"{label} {path} is {mode:04o}: {why}",
        path=str(path),
        fix=chmod,
        details=details,
    )


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------


def _presence(path: Path) -> tuple[bool, str | None]:
    """Whether `path` exists, from one ``os.stat``: ``(True, None)`` when it
    does, ``(False, None)`` when it does not, ``(False, why)`` when this
    account cannot tell. `Path.exists` and `Path.is_file` re-raise EACCES
    on Python 3.11 to 3.13 (and answer False on 3.14), so a store, keys or
    state directory this account cannot search, one another account owns
    among them, would end the audit in a traceback, or read as no store at
    all, exactly where it has a finding to make."""
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False, None
    except OSError as exc:
        return False, exc.strerror or str(exc)
    return True, None


def read_meta(store_file: Path) -> dict[str, str]:
    """The store's meta table, read without writing a byte to the file or
    beside it, through the read-only commands' open
    (`store_schema.connect_read_only`): ``mode=ro`` in place for a file in
    rollback-journal mode and for one whose ``-wal`` and ``-shm`` are both
    there (a running daemon, or one that crashed), so the read sees the
    WAL's pages; a closed WAL-mode store, and a ``-wal`` that arrived
    without its ``-shm``, from a private copy removed after the read. The
    live file is never read ``immutable=1``, which a daemon's checkpoint
    during the read could tear (review of ENG-104, M4). Raises
    ``sqlite3.Error`` on a file that is not a store, and `OSError` when the
    file cannot be read or copied. A lock held over the read is waited
    for as any CLI command waits (`store.CLI_BUSY_TIMEOUT_SECONDS`, whose
    evidence is stated there): the audit is one.

    The file is admitted first, as every opener admits it
    (`store_schema.admit_store`): a store this build did not write (a
    view or a trigger of its own choosing, the shape that made a
    repository's store hang this read for good) raises
    `store.StoreRefusedError` before meta is read, and meta is read under
    the admission deadline (`store_schema.read_store_meta`)."""
    from .store import CLI_BUSY_TIMEOUT_SECONDS, connect_read_only, read_store_meta

    resolved = store_file.resolve()
    conn, scratch = connect_read_only(resolved, timeout=CLI_BUSY_TIMEOUT_SECONDS)
    try:
        return read_store_meta(conn, resolved)
    finally:
        conn.close()
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def _meta_or_error(
    store_file: Path,
) -> tuple[dict[str, str] | None, str | None, StoreRefusedError | None]:
    """The store's meta, or why it could not be read, and the refusal
    every opener makes of the file when that is why (`read_meta`)."""
    from .store_schema import StoreRefusedError

    present, unreachable = _presence(store_file)
    if unreachable is not None:
        return None, f"{store_file} cannot be reached: {unreachable}", None
    if not present:
        return None, None, None
    try:
        return read_meta(store_file), None, None
    except StoreRefusedError as exc:
        cause = exc.__cause__
        if isinstance(cause, sqlite3.DatabaseError) and (
            getattr(cause, "sqlite_errorcode", 0) & 0xFF in (11, 26)
        ):
            # A schema SQLite cannot read (SQLITE_CORRUPT, SQLITE_NOTADB):
            # damaged or not a database, a meta that cannot be read, as
            # before the check (`admit_store`).
            return None, str(cause), None
        return None, str(exc), exc
    except (sqlite3.Error, OSError) as exc:
        return None, str(exc), None


def _key(
    store_file: Path,
    keys_dir: Path,
    meta: dict[str, str] | None,
    meta_error: str | None,
    keyring: KeyRing | None,
    *,
    refused: StoreRefusedError | None = None,
) -> Finding:
    """The key is present under the keys directory and its fingerprint is
    the one the store records, read as `Store.open` and doctor read it
    (`store_keys.key_state`) and said in the same words: ok only when
    present, high when unreadable, missing, foreign or bound elsewhere
    (a copy whose key ring belongs to its original, which every owner's
    open serves read-only). A store file every opener refuses
    (`refused`, `read_meta`) is this check's one finding instead: its meta
    was not read, so there is no key to look for."""
    present, unreachable = _presence(store_file)
    if not present and unreachable is None:
        return Finding(
            "key.match",
            "info",
            f"no store at {store_file} yet: nothing to check",
            path=str(store_file),
        )
    if refused is not None:
        from .store_schema import StoreVersionError

        later = isinstance(refused, StoreVersionError)
        return Finding(
            "key.match",
            "warn" if refused.timed_out or later else "high",
            f"{refused}: every opener (the daemon, a hook, the CLI) refuses it "
            "the same way, and none reads a value from it or writes to it",
            path=str(store_file),
            fix=(
                "upgrade bettermemory: a later build wrote this store, and this "
                "build reads no value from it, its key included"
                if later
                else "run the audit again once the disk answers; a store whose meta "
                "takes that long to read is not one this build wrote"
                if refused.timed_out
                else "move the file aside and find out what wrote it: a store this "
                "build writes holds only its own schema's tables, indexes and "
                "triggers, and one a repository shipped is not yours to serve"
            ),
            details={"refused": refused.why},
        )
    store_id = (meta or {}).get("store_id")
    if meta is not None and keyring is None and store_id:
        # `Store.open` refuses this store before it reads a key, so the
        # daemon, the hooks and the CLI serve nothing from it and nothing
        # rekeys it; the ring is not searched by the id.
        shown = store_id[:40]
        return Finding(
            "key.match",
            "high",
            f"the store's meta names a store id this build never writes "
            f"({shown!r}): every opener refuses this store, and the key ring is "
            "not searched by it",
            path=str(store_file),
            fix=(
                "find out what wrote this store file; a store this build creates "
                "carries 32 hex digits, and a repository-shipped one is not yours "
                "to serve"
            ),
            details={"store_id": shown, "store_id_valid": False},
        )
    if meta is None or keyring is None:
        why = meta_error or "the meta table names no store id"
        return Finding(
            "key.match",
            "warn",
            f"the store's meta cannot be read: {why}",
            path=str(store_file),
            fix="bettermemory log verify reads the whole store and names the problem",
        )
    from .store_keys import KEY_FOREIGN, KEY_PRESENT, key_state

    # The state `Store.open` and doctor read, in the same words: quiet
    # here, since the audit reports it as this finding.
    state = key_state(keyring, store_file, meta.get("key_fingerprint", ""), warn=False)
    retired = keyring.retired_paths()
    details: dict[str, Any] = {
        "store_id": keyring.store_id,
        "key_fingerprint": state.expected,
        "retired_keys": len(retired),
        **state.details(),
    }
    if state.state == KEY_FOREIGN:
        details["retired_match"] = (
            keyring.key_for_fingerprint(state.expected) is not None
        )
    if state.state != KEY_PRESENT:
        return Finding(
            "key.match",
            "high",
            state.summary,
            path=str(state.key_path),
            fix=state.remedy,
            details=details,
        )
    return Finding(
        "key.match",
        "ok",
        f"{state.summary}; {len(retired)} retired key(s) held",
        path=str(state.key_path),
        details=details,
    )


def _placement(store_dir: Path, keys_dir: Path) -> Finding:
    """The key lives apart from the store: tamper evidence is against a
    writer who reaches the store file but not the key (SECURITY.md)."""
    details = {"store_dir": str(store_dir), "keys_dir": str(keys_dir)}
    if keys_dir == store_dir or store_dir in keys_dir.parents:
        return Finding(
            "key.placement",
            "high",
            f"the keys directory {keys_dir} is inside the store directory: a "
            "copy, backup or sync of the store carries its key, and whoever "
            "holds both can sign rows",
            path=str(keys_dir),
            fix=(
                "move the keys out of the store directory and point "
                "BETTERMEMORY_KEYS_DIR at their new place, or unset it for the "
                "default"
            ),
            details=details,
        )
    if keys_dir in store_dir.parents:
        return Finding(
            "key.placement",
            "warn",
            f"the store directory {store_dir} is inside the keys directory: a "
            "copy of the keys directory carries the store with its key",
            path=str(store_dir),
            fix="keep the store and the keys in separate directories",
            details=details,
        )
    return Finding(
        "key.placement", "ok", "the keys live apart from the store", details=details
    )


# ---------------------------------------------------------------------------
# Where the store and the key live: git working trees, synced folders
# ---------------------------------------------------------------------------


def _where(store_dir: Path, store_file: Path, keys_dir: Path) -> Iterator[Finding]:
    # What makes the store tracked is the store's content, not whatever
    # else sits in its directory: a tracked README beside an ignored store
    # file is no store in the repository, a tracked backup bundle or a
    # copy of the store is (`git_finding`'s `store_name`).
    has_store = _presence(store_file)[0]
    store_probe = store_file if has_store else store_dir
    yield git_finding(
        "store.git",
        "store",
        store_dir,
        store_probe,
        ignored_ok=True,
        store_name=store_file.name,
    )
    yield git_finding(
        "keys.git", "keys directory", keys_dir, keys_dir, ignored_ok=False
    )
    store_sync = synced_root(store_dir)
    keys_sync = synced_root(keys_dir)
    if store_sync is None:
        yield Finding("store.synced", "ok", "the store is not in a known synced folder")
    else:
        yield Finding(
            "store.synced",
            "warn",
            f"the store is in a {store_sync} folder: it leaves this host with "
            "whatever the provider sees, and a sync client copying a live "
            "SQLite file can corrupt it",
            path=str(store_dir),
            fix="move the store out of the synced folder (BETTERMEMORY_DIR or [storage] directory)",
            details={"service": store_sync},
        )
    if keys_sync is None:
        yield Finding("keys.synced", "ok", "the keys are not in a known synced folder")
    else:
        both = store_sync is not None
        yield Finding(
            "keys.synced",
            "high" if both else "warn",
            f"the keys directory is in a {keys_sync} folder"
            + (
                ", with the store: the synced copy carries its own key, and whoever "
                "reaches it can sign rows"
                if both
                else ": the key leaves this host"
            ),
            path=str(keys_dir),
            fix="move the keys out of the synced folder (BETTERMEMORY_KEYS_DIR)",
            details={"service": keys_sync},
        )


#: How long each of `git_finding`'s three questions to git (the toplevel,
#: the tracked files, the ignore check) may take. Measured in this
#: project's repository (968 index entries) on the owner's Mac, 20 runs
#: each: 11 to 15 ms. Two seconds is over a hundred times that; a large
#: index, a cold cache or a network filesystem is not measured, and what
#: would set it is these three calls' time in the largest repository a
#: store is found in.
_GIT_TIMEOUT_SECONDS = 2.0


def _git_unanswered(
    finding_id: str,
    label: str,
    directory: Path,
    top: Path,
    question: str,
    run: subprocess.CompletedProcess[str] | None,
) -> Finding:
    """The skip `git_finding` returns when git leaves `question` unanswered
    for `directory` inside the working tree at `top`: None from the runner
    (it timed out or could not run, `_git.run_git` logged which), or an
    exit that is no answer."""
    exit_code = None if run is None else run.returncode
    why = (
        f"timed out after {_GIT_TIMEOUT_SECONDS:g} s or could not run"
        if run is None
        else f"exited {exit_code}"
    )
    verb = "tracks" if question == "ls-files" else "ignores"
    return Finding(
        finding_id,
        "skip",
        f"the {label} {directory} is inside the git working tree at {top}; "
        f"`git {question}` {why}, so whether git {verb} it is not checked",
        path=str(directory),
        fix="run `bettermemory security audit` again once git answers",
        details={"worktree": str(top), "git": question, "exit": exit_code},
    )


def _store_content(name: str, store: str, backups: str) -> bool:
    """Whether a tracked path (relative to the working tree, in POSIX form)
    is the store's content: the store file `store`, its ``-wal`` and
    ``-shm``, a copy named after it (restore's ``<store>.pre-restore-<stamp>``
    copies with their ``-wal`` and ``-shm``, the daemon's
    ``<store>.orphaned-<stamp>.sqlite`` saves), or anything under the store's
    `backups` directory, where `backup create` puts its bundles by default
    (a snapshot, a head, and with ``--include-key`` the signing key)."""
    return (
        name in (store, store + "-wal", store + "-shm")
        or name.startswith(store + ".")
        or name.startswith(backups + "/")
    )


def git_finding(
    finding_id: str,
    label: str,
    directory: Path,
    probe: Path,
    *,
    ignored_ok: bool,
    store_name: str | None = None,
) -> Finding:
    """Whether `directory` sits in a git working tree, and if so whether
    git tracks its content or ignores `probe` (the store file, or the
    directory). Git lists every tracked file under `directory`, as literal
    pathspecs. For the keys directory (`store_name` None), which holds
    nothing but the ring, every one is content. For the store, the content
    is the file named `store_name`, its copies and its backups
    (`_store_content`); any other tracked file (a README, a .gitignore) is
    named in ``details["other_tracked"]`` and changes nothing. Tracked
    content is high: the store, a copy, a bundle or the key goes wherever
    the repository goes. Not ignored is a warning: one ``git add -A`` away.
    Ignored is information for the store (``ignored_ok``) and a warning for
    the keys, which belong in no working tree at all.

    A question git leaves unanswered (it timed out, could not run, or
    failed: ``ls-files`` exits non-zero, ``check-ignore`` with neither 0,
    ignored, nor 1, not ignored) is a skip that names it: read as "not
    tracked" or "not ignored", it would call a tracked store a commit
    away (``check-ignore`` applies no ignore rule to a tracked file), and
    an ignored one the same."""
    if not os.path.isdir(directory):
        return Finding(finding_id, "ok", f"no {label} at {directory} yet")
    top_run = run_git(
        ["rev-parse", "--show-toplevel"], cwd=directory, timeout=_GIT_TIMEOUT_SECONDS
    )
    if top_run is None:
        return Finding(finding_id, "skip", f"git could not run; {label} not checked")
    top_text = top_run.stdout.strip()
    if top_run.returncode != 0 or not top_text:
        # A non-zero exit is also git refusing a repository it will not read
        # (another account's, `safe.directory` unset): the files then hold
        # one, and whether it tracks the directory is not known (ENG-175).
        held = githead.locate(directory) if top_run.returncode != 0 else None
        if held is None:
            return Finding(
                finding_id, "ok", f"the {label} is not in a git working tree"
            )
        lines = (top_run.stderr or "").strip().splitlines()
        reason = lines[0] if lines else f"exit {top_run.returncode}"
        return Finding(
            finding_id,
            "warn",
            f"the {label} is inside the git repository at {held.worktree_root}, "
            f"which git refused ({reason}): whether it tracks or ignores the "
            f"{label} is not known",
            path=str(directory),
            fix=(
                "check from the account that owns the repository, or add it to"
                " safe.directory only if you trust its configuration, then"
                " audit again"
            ),
            details={"worktree": str(held.worktree_root), "refused": True},
        )
    top = Path(top_text).resolve()
    details: dict[str, Any] = {"worktree": str(top)}
    try:
        relative_dir = directory.resolve().relative_to(top).as_posix()
        relative_probe = probe.resolve().relative_to(top).as_posix()
    except ValueError:
        return Finding(finding_id, "ok", f"the {label} is not in a git working tree")
    listed = run_git(
        ["--literal-pathspecs", "ls-files", "-z", "--", relative_dir],
        cwd=top,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if listed is None or listed.returncode != 0:
        return _git_unanswered(finding_id, label, directory, top, "ls-files", listed)
    tracked = [name for name in listed.stdout.split("\0") if name]
    if store_name is None:
        content, backups = tracked, ""
    else:
        prefix = "" if relative_dir == "." else relative_dir + "/"
        from .backup import BACKUPS_DIR_NAME

        backups = prefix + BACKUPS_DIR_NAME
        store = prefix + store_name
        content = [name for name in tracked if _store_content(name, store, backups)]
        other = [name for name in tracked if name not in content]
        if other:
            details["other_tracked"] = other[:20]
    if content:
        details["tracked"] = True
        details["tracked_files"] = content[:20]
        if store_name is None:
            remove = f"git rm -r --cached {shlex.quote(relative_dir)}"
        else:
            singles = [name for name in content if not name.startswith(backups + "/")]
            steps = []
            if singles:
                steps.append(
                    "git rm --cached -- " + " ".join(shlex.quote(n) for n in singles)
                )
            if len(singles) < len(content):
                steps.append(f"git rm -r --cached -- {shlex.quote(backups)}")
            remove = " and ".join(steps)
        return Finding(
            finding_id,
            "high",
            f"the {label} {directory} is tracked by the git repository at {top}",
            path=str(directory),
            fix=f"{remove}, ignore it, and treat what was pushed as disclosed",
            details=details,
        )
    ignored = run_git(
        ["check-ignore", "-q", "--", relative_probe],
        cwd=top,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if ignored is None or ignored.returncode not in (0, 1):
        return _git_unanswered(
            finding_id, label, directory, top, "check-ignore", ignored
        )
    details["tracked"] = False
    details["ignored"] = ignored.returncode == 0
    if details["ignored"]:
        return Finding(
            finding_id,
            "info" if ignored_ok else "warn",
            f"the {label} is inside the git working tree at {top}, ignored",
            path=str(directory),
            fix=None if ignored_ok else "keep the keys outside any working tree",
            details=details,
        )
    return Finding(
        finding_id,
        "warn",
        f"the {label} {directory} is inside the git working tree at {top} and "
        "not ignored: one `git add -A` commits it",
        path=str(directory),
        fix=f"add {relative_dir}/ to the repository's .gitignore, or move it",
        details=details,
    )


#: The extended attribute macOS's File Provider framework sets on the root
#: of every domain it syncs: iCloud Drive's, and with "Desktop & Documents
#: Folders" on, ``~/Desktop`` and ``~/Documents`` themselves, and those of
#: the clients built on it (Dropbox, OneDrive, Google Drive and others,
#: under ``~/Library/CloudStorage``). Its value names the domain
#: (``com.apple.CloudDocs.iCloudDriveFileProvider/<id>`` for iCloud). The
#: system refuses to let an account set it, so its presence is the
#: provider's word. Read at call time, so a test can name a settable one.
_FILE_PROVIDER_XATTR = "com.apple.file-provider-domain-id"
#: The most of an attribute's value read: a domain id is a reverse-DNS
#: name and an identifier, iCloud Drive's 80 bytes on the owner's
#: ``~/Documents``; another provider's is not measured, and one over the
#: cap is still a domain (the size query alone says the attribute is set).
_XATTR_VALUE_CAP = 1024

#: Windows cloud files (the Cloud Filter API OneDrive, iCloud for Windows
#: and Dropbox's current client sync through): a placeholder carries a
#: reparse tag of the ``IO_REPARSE_TAG_CLOUD`` family (``0x9000001A``,
#: with a variant in bits 12 to 15), and the provider's hydration state in
#: its attributes: recall on data access or on open, pinned, unpinned.
_IO_REPARSE_TAG_CLOUD = 0x9000001A
_IO_REPARSE_TAG_CLOUD_MASK = 0xFFFF0FFF
_CLOUD_FILE_ATTRIBUTES = (
    0x00040000  # FILE_ATTRIBUTE_RECALL_ON_OPEN
    | 0x00080000  # FILE_ATTRIBUTE_PINNED
    | 0x00100000  # FILE_ATTRIBUTE_UNPINNED
    | 0x00400000  # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)

#: The links iCloud Drive keeps to the folders "Desktop & Documents
#: Folders" syncs in place, under ``~/Library/Mobile Documents``.
_CLOUDDOCS_LINKED = ("Desktop", "Documents")


def synced_root(path: Path) -> str | None:
    """The sync service whose folder holds `path`, or None.

    The filesystem is asked first, on the path with its links resolved, so
    a store reached through a link into a synced folder is seen: on macOS
    the File Provider attribute on the path or an ancestor (iCloud Drive,
    with "Desktop & Documents Folders" on ``~/Documents`` and
    ``~/Desktop`` themselves, and the File Provider clients), and the
    links iCloud Drive keeps to the folders it syncs in place; on Windows
    a cloud files placeholder's reparse tag or attributes on the path or an
    ancestor. The folder names the clients use stay as the fallback, on
    the resolved path and then on the path as given: iCloud Drive's
    ``~/Library/Mobile Documents``, the File Provider clients'
    ``~/Library/CloudStorage/<provider>``, and the folders the older
    clients and the Linux ones name for the service. A sync client that
    marks nothing and keeps a folder under another name is still not
    seen."""
    resolved = _resolved(path)
    for candidate in (resolved, *resolved.parents):
        marked = _sync_marker(candidate)
        if marked is not None:
            return marked
    linked = _icloud_linked_root(resolved)
    if linked is not None:
        return linked
    named = _synced_by_name(resolved)
    if named is None and resolved != path:
        named = _synced_by_name(path)
    return named


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def _lstat(path: Path) -> Any:
    """`os.lstat`, or None where `path` cannot be read: a module function so
    the Windows attributes can be faked on any platform in a test."""
    try:
        return os.lstat(path)
    except (OSError, ValueError):
        return None


def _sync_marker(path: Path) -> str | None:
    """The service the filesystem marks `path` itself as synced by: the
    Windows cloud files tag or attributes, or the macOS File Provider
    attribute. None for a path that does not exist or carries neither."""
    st = _lstat(path)
    if st is None:
        return None
    if _cloud_files(st):
        return "Windows cloud files"
    domain = _file_provider_domain(path)
    if domain is None:
        return None
    if domain.startswith("com.apple.CloudDocs"):
        return "iCloud Drive"
    return f"File Provider ({domain.split('/', 1)[0][:80]})"


def _cloud_files(st: Any) -> bool:
    """Whether a stat result is a Windows cloud files placeholder: the
    ``IO_REPARSE_TAG_CLOUD`` family, or a recall, pinned or unpinned
    attribute. Both fields exist on Windows only, and read as 0 elsewhere."""
    tag = int(getattr(st, "st_reparse_tag", 0) or 0)
    attributes = int(getattr(st, "st_file_attributes", 0) or 0)
    if tag and tag & _IO_REPARSE_TAG_CLOUD_MASK == _IO_REPARSE_TAG_CLOUD:
        return True
    return bool(attributes & _CLOUD_FILE_ATTRIBUTES)


def _file_provider_domain(path: Path) -> str | None:
    """The value of `_FILE_PROVIDER_XATTR` on `path`, or None when it is
    absent or cannot be read, and always None off macOS, where no sync
    client sets it. Python's `os` has no extended attribute calls on
    macOS, so libc's ``getxattr`` is called directly."""
    if sys.platform != "darwin":
        return None
    import ctypes

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        getxattr = libc.getxattr
    except (OSError, AttributeError):
        return None
    getxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    getxattr.restype = ctypes.c_ssize_t
    name = _FILE_PROVIDER_XATTR.encode()
    target = os.fsencode(path)
    size = getxattr(target, name, None, 0, 0, 0)
    if size < 0:
        return None
    buffer = ctypes.create_string_buffer(max(1, min(size, _XATTR_VALUE_CAP)))
    read = getxattr(target, name, buffer, len(buffer), 0, 0)
    value = buffer.raw[:read].rstrip(b"\0") if read > 0 else b""
    return value.decode("utf-8", errors="replace") or "unnamed domain"


def _icloud_linked_root(path: Path) -> str | None:
    """The iCloud Drive service when `path` is in a folder iCloud
    Drive syncs in place: the target of its ``Desktop`` or
    ``Documents`` link under ``~/Library/Mobile Documents/com~apple~CloudDocs``.
    A second witness beside the File Provider attribute, for a folder whose
    attribute this account cannot read."""
    try:
        base = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    except (RuntimeError, OSError):
        return None
    for name in _CLOUDDOCS_LINKED:
        link = base / name
        try:
            if not link.is_symlink():
                continue
            root = Path(os.path.realpath(link))
        except (OSError, ValueError):
            continue
        if path == root or root in path.parents:
            return "iCloud Drive (Desktop & Documents)"
    return None


def _synced_by_name(path: Path) -> str | None:
    """The service a folder name on `path` belongs to, or None."""
    parts = [part.lower() for part in path.parts]
    for index in range(len(parts) - 1):
        if parts[index] == "library" and parts[index + 1] == "mobile documents":
            return "iCloud Drive"
        if parts[index] == "library" and parts[index + 1] == "cloudstorage":
            if index + 2 < len(path.parts):
                return f"cloud storage ({path.parts[index + 2]})"
            return "cloud storage"
    for part in parts:
        if part == "dropbox" or part.startswith("dropbox ("):
            return "Dropbox"
        if part == "onedrive" or part.startswith(("onedrive - ", "onedrive-")):
            return "OneDrive"
        if part in ("google drive", "googledrive", "my drive"):
            return "Google Drive"
        if part in ("icloud drive", "iclouddrive"):
            return "iCloud Drive"
        if part == "box sync":
            return "Box"
    return None


# ---------------------------------------------------------------------------
# The daemon
# ---------------------------------------------------------------------------


def _loopback_bind(host: str) -> bool:
    """Whether a recorded listening address is local-only: ``localhost``,
    or an address in the loopback block (``127.0.0.0/8``, ``::1``). Parsed
    as an address, so a name that merely starts with ``127.`` is not
    taken for one."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _daemon(state_dir: Path, store_file: Path) -> Iterator[Finding]:
    state_file = _daemon_client.state_file_for(state_dir, store_file)
    present, unreachable = _presence(state_file)
    if unreachable is not None:
        yield Finding(
            "daemon.bind",
            "warn",
            f"the daemon state file {state_file} cannot be reached: {unreachable}",
            path=str(state_file),
            fix=(
                "the state directory must be this account's own (owner-only, 0700); "
                "the hooks and the CLI read the daemon's port and token there"
            ),
        )
        return
    if not present:
        yield Finding(
            "daemon.bind",
            "info",
            "no daemon state file for this store: no daemon serves it now",
            path=str(state_file),
        )
        return
    state = _daemon_client.read_state_file(state_file)
    if state is None:
        yield Finding(
            "daemon.bind",
            "warn",
            f"the daemon state file {state_file} does not parse",
            path=str(state_file),
            fix="`bettermemory down`, then let the next hook or `bettermemory up` write a new one",
        )
        return
    recorded = state.get("host")
    host = recorded if isinstance(recorded, str) and recorded else _daemon_client.HOST
    details: dict[str, Any] = {
        "host": host,
        "host_recorded": isinstance(recorded, str) and bool(recorded),
        "port": state["port"],
        "pid": state["pid"],
        "version": state.get("version"),
    }
    if _loopback_bind(host):
        yield Finding(
            "daemon.bind",
            "ok",
            f"the daemon listens on {host}:{state['port']} only (pid {state['pid']})",
            path=str(state_file),
            details=details,
        )
    else:
        yield Finding(
            "daemon.bind",
            "high",
            f"the daemon is recorded as listening on {host}:{state['port']}: "
            "other hosts can reach its unauthenticated /health",
            path=str(state_file),
            fix="`bettermemory down`, then start it with this build, which binds 127.0.0.1",
            details=details,
        )
    if not _daemon_client.pid_alive(int(state["pid"])):
        yield Finding(
            "daemon.state",
            "info",
            f"the state file names pid {state['pid']}, which is not running: a "
            "stale file the next start replaces",
            path=str(state_file),
            details={"pid": state["pid"]},
        )


# ---------------------------------------------------------------------------
# The config's settings worth knowing
# ---------------------------------------------------------------------------


def _on(value: object) -> str:
    return "on" if value else "off"


def _settings(config: Config) -> Iterator[Finding]:
    """The switches that change what the store records or who may loosen
    it, as facts. The policy and security sections are read through
    getattr with their defaults, so this reads a build that predates them
    as the defaults it behaves as."""
    facts: dict[str, Any] = {
        "telemetry.enabled": config.telemetry.enabled,
        "capture.stop_checkpoint": config.capture.stop_checkpoint,
        "behavior.write_supersession": config.behavior.write_supersession,
        "membership.strict": config.membership.strict,
        "daemon.port": config.daemon.port,
    }
    cwd_store = getattr(config.storage, "cwd_store", None)
    if cwd_store is not None:
        facts["storage.cwd_store"] = cwd_store
    security = getattr(config, "security", None)
    facts["security.strict_provenance"] = bool(
        getattr(security, "strict_provenance", False)
    )
    yield Finding(
        "config.settings",
        "info",
        f"telemetry {_on(facts['telemetry.enabled'])}, capture checkpoint "
        f"{_on(facts['capture.stop_checkpoint'])}, write supersession "
        f"{_on(facts['behavior.write_supersession'])}, membership strict "
        f"{_on(facts['membership.strict'])}, strict provenance "
        f"{_on(facts['security.strict_provenance'])}, daemon port "
        f"{facts['daemon.port']}",
        path=None if config.config_path is None else str(config.config_path),
        details=facts,
    )
    policy = getattr(config, "policy", None)
    forbid_force = bool(getattr(policy, "forbid_force", False))
    forbid_acknowledge = sorted(getattr(policy, "forbid_acknowledge", None) or [])
    managed = sorted(getattr(config, "managed_keys", None) or [])
    if forbid_force or forbid_acknowledge:
        refused = (["force"] if forbid_force else []) + [
            f"acknowledge_{name}" for name in forbid_acknowledge
        ]
        summary = f"the tools refuse {', '.join(refused)} from agents"
    else:
        summary = "agents may pass force and every acknowledge flag ([policy] at its defaults)"
    if managed:
        summary += f"; {len(managed)} key(s) pinned by the managed config"
    yield Finding(
        "config.policy",
        "info",
        summary,
        details={
            "policy.forbid_force": forbid_force,
            "policy.forbid_acknowledge": forbid_acknowledge,
            "managed_keys": managed,
        },
    )


# ---------------------------------------------------------------------------
# Claude Code's sandbox settings
# ---------------------------------------------------------------------------


def read_settings(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """A Claude Code settings file as a dict, ``(None, None)`` when it is
    absent, ``(None, why)`` when it cannot be read or is not a JSON
    object. Read, never written, through Claude Code's one bounded reader
    (`claude_paths.read_settings_text`, at most
    `claude_paths.SETTINGS_CAP_BYTES`), which doctor reads the same
    files through."""
    try:
        text = claude_paths.read_settings_text(path)
    except FileNotFoundError:
        return None, None
    except UnicodeDecodeError as exc:
        return None, f"not JSON ({exc})"
    except (OSError, ValueError) as exc:
        return None, str(exc)
    try:
        data = json.loads(text)
    except ValueError as exc:
        return None, f"not JSON ({exc})"
    if not isinstance(data, dict):
        return None, "not a JSON object"
    return data, None


def _dig(document: dict[str, Any], *keys: str) -> Any:
    value: Any = document
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _strings(value: Any) -> list[str]:
    return (
        [item for item in value if isinstance(item, str)]
        if isinstance(value, list)
        else []
    )


def _settings_paths(platform: str) -> list[tuple[str, Path]]:
    """The settings files the audit reads, in the order Claude Code merges
    them: the managed file, then its drop-in directory's ``*.json`` files in
    name order (hidden ones skipped, as Claude Code skips them), then the
    user's file, in the directory ``CLAUDE_CONFIG_DIR`` names when it is
    set (`claude_paths.user_settings_path`). A project's own settings
    depend on where Claude Code starts, and are not read."""
    managed = sandbox_settings.claude_managed_settings_path(platform)
    paths: list[tuple[str, Path]] = [("managed", managed)]
    drop_ins = managed.parent / "managed-settings.d"
    try:
        paths.extend(
            ("managed", path)
            for path in sorted(drop_ins.glob("*.json"))
            if not path.name.startswith(".")
        )
    except OSError:
        pass
    paths.append(("user", claude_paths.user_settings_path()))
    return paths


def _expand(entry: str, home: Path | None) -> Path | None:
    """A settings path as an absolute path: ``~``/``~/x`` under the home
    directory, ``/x`` and ``//x`` absolute. A relative entry is relative to
    a settings file or a working directory and is not resolved."""
    if entry in ("~", "~/"):
        return home
    if entry.startswith("~/"):
        return None if home is None else home / entry[2:]
    if entry.startswith("/"):
        return Path("/" + entry.lstrip("/"))
    return None


def _spellings(path: Path) -> set[Path]:
    """`path` as written and as resolved: a settings entry names the path
    the user typed, the plan the real one (macOS's ``/tmp`` is
    ``/private/tmp``)."""
    found = {path}
    try:
        found.add(path.resolve())
    except OSError:
        pass
    return found


def _levels_above(base: Path | None, target: Path) -> int | None:
    """How many directories `base` sits above `target`: 0 for `target`
    itself, None when it is neither `target` nor a directory above it."""
    if base is None:
        return None
    levels = [
        len(target.parts) - len(candidate.parts)
        for candidate in _spellings(base)
        if candidate == target or candidate in target.parents
    ]
    return min(levels) if levels else None


def _covers(base: Path | None, target: Path) -> bool:
    return _levels_above(base, target) is not None


def _literal_prefix(entry: str) -> str:
    """`entry` up to the directory before its first wildcard, the directory
    a glob entry can match under; `entry` itself when it has none."""
    for index, char in enumerate(entry):
        if char in "*?[":
            return entry[: entry.rfind("/", 0, index) + 1]
    return entry


def _reopened(
    target: Path, deny_read: list[str], allow_read: list[str], home: Path | None
) -> list[str]:
    """The ``allowRead`` entries that open `target`, or part of it, to the
    sandboxed shell again. Where read rules overlap, the narrower path wins
    (Claude Code's sandboxing documentation): an entry inside `target`
    re-opens that part of it, and one between `target` and the narrowest
    ``denyRead`` entry above it re-opens all of it. An entry at that deny's
    own path counts too, since neither is narrower. A wildcard entry is
    read up to the directory before its first wildcard; a relative entry
    is not read. Empty when no ``denyRead`` entry covers `target`, which
    the deny check reports on its own."""
    nearest = min(
        (
            levels
            for levels in (_levels_above(_expand(e, home), target) for e in deny_read)
            if levels is not None
        ),
        default=None,
    )
    if nearest is None:
        return []
    found: list[str] = []
    for entry in allow_read:
        base = _expand(_literal_prefix(entry), home)
        if base is None:
            continue
        above = _levels_above(base, target)
        inside = any(_levels_above(target, real) for real in _spellings(base))
        if (above is not None and above <= nearest) or inside:
            found.append(entry)
    return found


def _rule_covers(rules: list[str], tool: str, target: Path, home: Path | None) -> bool:
    """Whether a deny rule for `tool` covers every file under `target`: the
    bare tool name, ``//**`` (the whole filesystem), or a pattern ending in
    ``/**`` whose base is `target` or a directory above it. Patterns with
    other wildcards and relative patterns are not interpreted: a relative
    ``**`` matches under the session's working directory only."""
    for rule in rules:
        if rule in (tool, f"{tool}(//**)"):
            return True
        if not (rule.startswith(f"{tool}(") and rule.endswith("/**)")):
            continue
        pattern = rule[len(tool) + 1 : -len("/**)")]
        if "*" in pattern:
            continue
        if pattern.startswith("~"):
            base = _expand(pattern, home)
        elif pattern.startswith("//"):
            base = Path("/" + pattern.lstrip("/"))
        else:
            continue
        if _covers(base, target):
            return True
    return False


def _reaches_loopback(host: str) -> bool:
    """Whether a host in ``sandbox.network.allowedDomains`` reaches this
    machine's loopback listeners, the daemon's among them, in any spelling
    a resolver reads: ``*`` (every host); ``localhost`` and ``*.localhost``,
    the names Claude Code's proxy lets resolve to loopback, with a root
    dot (``localhost.``) or without; and an address, parsed rather than
    matched as text, so every address in ``127.0.0.0/8``, ``::1``, an
    IPv4-mapped one (``::ffff:127.0.0.1``), the unspecified address
    (``0.0.0.0``, ``::``: a connection to it reaches the host's own
    listeners on macOS and Linux) and the legacy IPv4 spellings
    (``127.1``, ``2130706433``) all count."""
    name = host.strip().lower()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    name = name.rstrip(".")
    if name in ("*", "localhost", "*.localhost") or name.endswith(".localhost"):
        return True
    address: ipaddress.IPv4Address | ipaddress.IPv6Address
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        if not _LEGACY_IPV4.fullmatch(name):
            return False
        try:
            address = ipaddress.IPv4Address(socket.inet_aton(name))
        except (OSError, ValueError):
            return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified


def _loopback_entries(domains: list[str], ports: tuple[int, ...]) -> list[str]:
    """The ``allowedDomains`` entries that let sandboxed commands reach this
    machine's loopback on the daemon's port or on every port: a host that
    reaches loopback (`_reaches_loopback`) with no port, or with the
    daemon's. The daemon binds 127.0.0.1 alone (`_daemon_client.HOST`), so
    the spellings of that address, ``localhost``, ``*`` and the unspecified
    address reach the daemon itself; another address in ``127.0.0.0/8`` or
    ``::1`` reaches whatever else listens on the loopback, which the block
    keeps sandboxed commands off as well, and is flagged with them."""
    flagged: list[str] = []
    for entry in domains:
        host, port = entry.strip().lower(), None
        if host.startswith("["):
            closing = host.find("]")
            if closing != -1:
                rest = host[closing + 1 :]
                host = host[: closing + 1]
                if rest.startswith(":") and rest[1:].isdigit():
                    port = int(rest[1:])
        elif host.count(":") == 1:
            name, _, tail = host.partition(":")
            if tail.isdigit():
                host, port = name, int(tail)
        if _reaches_loopback(host) and (port is None or port in ports):
            flagged.append(entry)
    return flagged


@dataclass(frozen=True)
class _ClaudeSettings:
    """The Claude Code settings files the audit read, as (level, path,
    document) in merge order and highest precedence first, with the
    lookups it makes in them."""

    documents: list[tuple[str, Path, dict[str, Any]]]
    ranked: list[tuple[str, Path, dict[str, Any]]]

    def scalar(self, *keys: str) -> tuple[Any, str | None]:
        """Take a key path; returns the value of highest precedence set there and its level, or (None, None)."""
        for level, _, document in self.ranked:
            value = _dig(document, *keys)
            if value is not None:
                return value, level
        return None, None

    def locked(self, *keys: str) -> bool:
        """Take a key path; returns whether a managed file sets it to true."""
        return any(
            _dig(document, *keys) is True
            for level, _, document in self.documents
            if level == "managed"
        )

    def merged(self, *keys: str, managed_only: bool = False) -> list[str]:
        """Take a key path; returns the strings each file lists there, the managed files' alone when managed_only."""
        out: list[str] = []
        for level, _, document in self.documents:
            if level == "managed" or not managed_only:
                out.extend(_strings(_dig(document, *keys)))
        return out


def _read_claude_documents(
    platform: str,
) -> Generator[Finding, None, list[tuple[str, Path, dict[str, Any]]]]:
    """Read the managed and user settings files, yielding a warning for each unreadable one; returns the documents read, as (level, path, document)."""
    documents: list[tuple[str, Path, dict[str, Any]]] = []
    for level, path in _settings_paths(platform):
        document, error = read_settings(path)
        if error is not None:
            yield Finding(
                "claude.settings",
                "warn",
                f"the Claude Code settings file {path} cannot be read: {error}",
                path=str(path),
                fix="fix the file; Claude Code reports it as a settings error too",
            )
        elif document is not None:
            documents.append((level, path, document))
    return documents


def _sandbox_switch_gaps(
    settings: _ClaudeSettings,
) -> Generator[Finding, None, int]:
    """Check the sandbox's failIfUnavailable, allowUnsandboxedCommands and filesystem switches; yields a warning per gap and returns how many."""
    gaps = 0
    fail_if_unavailable, _ = settings.scalar("sandbox", "failIfUnavailable")
    if fail_if_unavailable is not True:
        gaps += 1
        yield Finding(
            "claude.sandbox.fail_if_unavailable",
            "warn",
            "sandbox.failIfUnavailable is not true: where the sandbox cannot "
            "start, commands run without it",
            fix='set "failIfUnavailable": true under "sandbox"',
        )
    unsandboxed, _ = settings.scalar("sandbox", "allowUnsandboxedCommands")
    if unsandboxed is not False:
        gaps += 1
        yield Finding(
            "claude.sandbox.unsandboxed_commands",
            "warn",
            "sandbox.allowUnsandboxedCommands is not false: a command the "
            "sandbox stops can be retried outside it",
            fix='set "allowUnsandboxedCommands": false under "sandbox"',
        )
    filesystem_off, _ = settings.scalar("sandbox", "filesystem", "disabled")
    if filesystem_off is True:
        gaps += 1
        yield Finding(
            "claude.sandbox.filesystem_disabled",
            "warn",
            "sandbox.filesystem.disabled is true: sandboxed commands run without "
            "filesystem isolation, so denyRead holds nothing back",
            fix='remove "disabled" from "sandbox.filesystem"',
        )
    return gaps


def _filesystem_gaps(
    settings: _ClaudeSettings, plan: sandbox_settings.SandboxPlan, home: Path | None
) -> Generator[Finding, None, int]:
    """Check denyRead, allowRead and the deny rules against the plan's denied paths; yields its findings and returns how many are gaps."""
    gaps = 0
    deny_read = settings.merged("sandbox", "filesystem", "denyRead")
    open_dirs = [
        loc
        for loc in plan.denied
        if not any(_covers(_expand(entry, home), loc.path) for entry in deny_read)
    ]
    if open_dirs:
        gaps += 1
        missing = [sandbox_settings.sandbox_path(loc.path, home) for loc in open_dirs]
        yield Finding(
            "claude.sandbox.deny_read",
            "warn",
            "sandbox.filesystem.denyRead leaves these readable to the sandboxed "
            f"shell: {', '.join(missing)}",
            fix="add them to denyRead; `bettermemory security sandbox` prints the list",
            details={"missing": missing},
        )
    allow_read = settings.merged(
        "sandbox",
        "filesystem",
        "allowRead",
        managed_only=settings.locked(
            "sandbox", "filesystem", "allowManagedReadPathsOnly"
        ),
    )
    reopened: dict[str, list[str]] = {}
    for loc in plan.denied:
        entries = _reopened(loc.path, deny_read, allow_read, home)
        if entries:
            reopened[sandbox_settings.sandbox_path(loc.path, home)] = entries
    if reopened:
        gaps += 1
        listed = "; ".join(
            f"{path} (by {', '.join(entries)})" for path, entries in reopened.items()
        )
        yield Finding(
            "claude.sandbox.allow_read",
            "warn",
            "sandbox.filesystem.allowRead opens these to the sandboxed shell "
            f"again, the narrower path winning over denyRead: {listed}",
            fix="drop those allowRead entries, or narrow them to paths outside these",
            details={"reopened": reopened},
        )
    rules = settings.merged("permissions", "deny")
    missing_rules = [
        f"{tool}({sandbox_settings.rule_path(loc.path, home)}/**)"
        for loc in plan.denied
        for tool in ("Read", "Edit")
        if not _rule_covers(rules, tool, loc.path, home)
    ]
    if missing_rules:
        gaps += 1
        yield Finding(
            "claude.sandbox.deny_rules",
            "warn",
            "permissions.deny lacks rules that keep Claude Code's own tools off "
            f"these: {', '.join(missing_rules)}",
            fix="add the rules; `bettermemory security sandbox` prints them",
            details={"missing": missing_rules},
        )
    project_missing = [
        rule
        for rule in sandbox_settings.PROJECT_RULES
        if rule not in rules and rule.replace("(", "(**/", 1) not in rules
    ]
    if project_missing:
        yield Finding(
            "claude.sandbox.project_rules",
            "info",
            "no deny rules for a project-local store: "
            f"{', '.join(project_missing)} would keep Claude Code's tools off one",
            details={"missing": project_missing},
        )
    return gaps


def _network_gaps(
    settings: _ClaudeSettings, plan: sandbox_settings.SandboxPlan, platform: str
) -> Generator[Finding, None, int]:
    """Check the allowed domains, local binding and excluded commands; yields its findings and returns how many are gaps."""
    gaps = 0
    domains = settings.merged(
        "sandbox",
        "network",
        "allowedDomains",
        managed_only=settings.locked("sandbox", "network", "allowManagedDomainsOnly"),
    )
    flagged = _loopback_entries(domains, plan.ports)
    if flagged:
        gaps += 1
        yield Finding(
            "claude.sandbox.network",
            "warn",
            "sandbox.network.allowedDomains lets sandboxed commands reach "
            "this machine's loopback, where the daemon listens on 127.0.0.1: "
            f"{', '.join(flagged)}",
            fix=(
                "name a port other than the daemon's "
                f"({', '.join(str(port) for port in plan.ports)}) on each, or drop them"
            ),
            details={"entries": flagged, "ports": list(plan.ports)},
        )
    local_binding, _ = settings.scalar("sandbox", "network", "allowLocalBinding")
    if local_binding is True and platform == "darwin":
        gaps += 1
        yield Finding(
            "claude.sandbox.local_binding",
            "warn",
            "sandbox.network.allowLocalBinding is true: sandboxed commands can "
            "connect to any port on localhost, the daemon's included",
            fix="leave it off",
        )
    elif local_binding is True:
        # Claude Code's sandboxing documentation: on Linux and WSL2 a
        # sandboxed command's localhost is its own, and the key has no
        # effect there. Native Windows, where the sandbox does not run,
        # returned a skip above and never reaches this.
        yield Finding(
            "claude.sandbox.local_binding",
            "info",
            "sandbox.network.allowLocalBinding is true, which has no effect on "
            "Linux: a sandboxed command's localhost is its own",
        )
    excluded = settings.merged("sandbox", "excludedCommands")
    if excluded:
        gaps += 1
        yield Finding(
            "claude.sandbox.excluded_commands",
            "warn",
            "sandbox.excludedCommands runs these as the user, outside the "
            f"sandbox: {', '.join(excluded)}",
            fix="remove them, or accept that each can reach the store, the key and the token",
            details={"commands": excluded},
        )
    return gaps


def _claude(config: Config, *, cwd: Path | None, platform: str) -> Iterator[Finding]:
    """Claude Code's sandbox settings in the managed and user files against
    the block `bettermemory security sandbox` prints for this host. Scalar
    keys take the managed value where one is set (managed settings sit
    above every other level), and among the managed files the last one
    merged: Claude Code merges managed-settings.json first and the drop-ins
    after it in name order, a later file's value replacing an earlier one's.
    List keys merge, as Claude Code merges them, except where a managed
    lock (``allowManagedReadPathsOnly``, ``allowManagedDomainsOnly``, on
    when any managed file sets it) leaves the managed entries alone.
    Beyond the keys the block sets, the audit reads the two that undo it:
    ``sandbox.filesystem.disabled``, and ``allowRead`` entries that open a
    denied directory again. No sandbox at all is information: the
    shell-holding agent is outside the guard by design (SECURITY.md), and
    docs/installation.md section 5 is opt-in. A sandbox that is on with a
    gap in it is a warning."""
    if platform == "win32":
        yield Finding(
            "claude.sandbox",
            "skip",
            "the Claude Code sandbox does not run on native Windows; not checked",
        )
        return
    plan = sandbox_settings.build_plan(
        config, fmt="managed", platform=platform, cwd=cwd
    )
    home = sandbox_settings.home_dir()
    documents = yield from _read_claude_documents(platform)
    read = [str(path) for _, path, _ in documents]
    # Highest precedence first: the managed files, the last merged first,
    # then the user's.
    ranked = [doc for doc in reversed(documents) if doc[0] == "managed"] + [
        doc for doc in documents if doc[0] == "user"
    ]
    settings = _ClaudeSettings(documents, ranked)

    enabled, enabled_level = settings.scalar("sandbox", "enabled")
    if enabled is not True:
        yield Finding(
            "claude.sandbox",
            "info",
            "Claude Code's sandbox is not enabled in the managed or user settings: "
            "an agent with a shell can read the store, the key and the daemon's "
            "token, as SECURITY.md describes",
            fix=(
                "`bettermemory security sandbox --format managed` prints the "
                "settings for this host"
            ),
            details={"read": read},
        )
        return
    gaps = 0
    gaps += yield from _sandbox_switch_gaps(settings)
    gaps += yield from _filesystem_gaps(settings, plan, home)
    gaps += yield from _network_gaps(settings, plan, platform)
    if enabled_level == "user":
        yield Finding(
            "claude.sandbox.scope",
            "info",
            "the sandbox is enabled in the user settings only: a project's "
            ".claude/settings.json can switch it off for its folder",
            fix="deliver it as managed settings (`bettermemory security sandbox --format managed`)",
        )
    if gaps == 0:
        yield Finding(
            "claude.sandbox",
            "ok",
            f"Claude Code's sandbox covers this host's paths ({enabled_level} settings)",
            details={"read": read},
        )


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def render(report: Audit) -> str:
    """The text form of an audit: the verdict, the store, one line per
    finding, and the fix under each warning and high finding. A summary
    can quote a path or an OS error's reason (a symbolic link's target),
    so it is printed through `history.printable`, as doctor prints its
    report: a line break or an escape in one cannot split or restyle a
    line."""
    from .history import printable

    counts = report.counts()
    lines = [
        f"bettermemory security audit: {report.status}",
        f"  store {printable(str(report.store))} ({report.store_rule})",
    ]
    width = max((len(finding.id) for finding in report.findings), default=0)
    for finding in report.findings:
        lines.append(
            f"  {finding.status:<4}  {finding.id:<{width}}  {printable(finding.summary)}"
        )
        if finding.fix and finding.status in ("warn", "high"):
            lines.append(f"  {'':<4}  {'':<{width}}  fix: {printable(finding.fix)}")
    lines.append(
        f"  {counts['high']} high, {counts['warn']} warning(s); "
        "`bettermemory log verify` checks the log itself"
    )
    return "\n".join(lines) + "\n"


__all__ = [
    "STATUSES",
    "Audit",
    "Finding",
    "audit",
    "git_finding",
    "mode_finding",
    "read_meta",
    "read_settings",
    "render",
    "synced_root",
]
