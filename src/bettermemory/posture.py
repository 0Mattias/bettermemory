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
  fingerprint) through a SQLite URI of its own: ``immutable=1`` when no
  ``-wal`` file exists, since then the main file holds every committed page
  and nothing else is created; ``mode=ro`` when one exists, which a live
  daemon keeps, so the read sees the WAL's pages and creates nothing either
  (the ``-shm`` is there with it). The log itself is `bettermemory log
  verify`'s to check; the audit points at it.
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
import shlex
import sqlite3
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import _daemon_client, sandbox_settings
from ._fsutil import bounded_read
from ._git import run_git
from .config import (
    ENV_DIR_OVERRIDE,
    KEYS_DIR_ENV,
    PROJECT_DIR_NAME,
    STORE_FILENAME,
    Config,
)
from .log import KeyRing, KeyUnreadableError, fingerprint

#: Every status a finding can carry, mildest first.
STATUSES: tuple[str, ...] = ("ok", "info", "skip", "warn", "high")
_EXIT_FOR: dict[str, int] = {"high": 1, "warn": 2}

#: The cap on a Claude Code settings file the audit reads: far above any
#: real one, small enough that a hostile file cannot exhaust memory.
_SETTINGS_CAP_BYTES = 1 << 20

#: The overrides an operator should know are in effect: each moves a
#: directory away from the platform default, and the hooks and the MCP
#: server see them only when the harness passes them on.
_OVERRIDES: tuple[str, ...] = (
    ENV_DIR_OVERRIDE,
    KEYS_DIR_ENV,
    _daemon_client.STATE_DIR_ENV,
)

#: Hosts whose portless entry in ``sandbox.network.allowedDomains`` opens
#: the daemon's port to the sandboxed shell. ``*`` opens every host; a
#: connection to the unspecified address (``0.0.0.0``, ``[::]``) reaches
#: the host's own listeners on macOS and Linux.
_LOOPBACK_HOSTS = frozenset(
    {
        "localhost",
        "*.localhost",
        "127.0.0.1",
        "::1",
        "[::1]",
        "0.0.0.0",
        "[::]",
        "*",
    }
)

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

    meta, meta_error = _meta_or_error(store_file)
    store_id = (meta or {}).get("store_id")
    keyring = KeyRing(keys_dir, store_id) if store_id else None

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
    findings.append(_key(store_file, keys_dir, meta, meta_error, keyring))
    findings.append(_placement(store_dir, keys_dir))
    findings.extend(_where(store_dir, store_file, keys_dir))
    findings.extend(_daemon(state_dir, store_file))
    findings.extend(_settings(config))
    findings.extend(_claude(config, cwd=cwd, platform=platform))
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
        # middle part, does not match the pattern.
        for retired in sorted(keys_dir.glob(f"{keyring.store_id}.*.key")):
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
    (a directory listing, the head checkpoint, the origin anchor, the
    daemon's log); any other group or other bit is a warning. An owner
    other than this account is high: that account can rewrite the file
    whatever its mode says."""
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
    """The store's meta table, read without writing a byte.

    ``immutable=1`` when the store has no ``-wal`` file: every committed page
    is then in the main file, and an immutable open creates no ``-wal`` or
    ``-shm`` beside it (a plain read-only open of a WAL-mode file does, and
    leaves them). ``mode=ro`` when a ``-wal`` exists with its ``-shm``, as
    under a running daemon or after one crashed, so the read sees the pages
    still in the WAL. A ``-wal`` without its ``-shm`` (a copy that brought
    the one and not the other) is refused: a read-only open there creates
    the ``-shm``, and an immutable one would miss the WAL's pages, a rekey's
    new fingerprint among them. Raises ``sqlite3.Error`` on a file that is
    not a store, and on that refusal."""
    resolved = store_file.resolve()
    wal, shm = _sibling(resolved, "-wal"), _sibling(resolved, "-shm")
    if not os.path.exists(wal):
        query = "immutable=1"
    elif os.path.exists(shm):
        query = "mode=ro"
    else:
        raise sqlite3.OperationalError(
            f"{wal.name} is there without {shm.name}, and reading it read-only "
            f"would create {shm.name}"
        )
    conn = sqlite3.connect(f"{resolved.as_uri()}?{query}", uri=True, timeout=5.0)
    try:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
    finally:
        conn.close()
    return {str(row[0]): str(row[1]) for row in rows}


def _meta_or_error(store_file: Path) -> tuple[dict[str, str] | None, str | None]:
    present, unreachable = _presence(store_file)
    if unreachable is not None:
        return None, f"{store_file} cannot be reached: {unreachable}"
    if not present:
        return None, None
    try:
        return read_meta(store_file), None
    except sqlite3.Error as exc:
        return None, str(exc)


def _key(
    store_file: Path,
    keys_dir: Path,
    meta: dict[str, str] | None,
    meta_error: str | None,
    keyring: KeyRing | None,
) -> Finding:
    """The key is present under the keys directory and its fingerprint is
    the one the store records."""
    present, unreachable = _presence(store_file)
    if not present and unreachable is None:
        return Finding(
            "key.match",
            "info",
            f"no store at {store_file} yet: nothing to check",
            path=str(store_file),
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
    expected = meta.get("key_fingerprint", "")
    key_path = keyring.current_path()
    retired = sorted(keys_dir.glob(f"{keyring.store_id}.*.key"))
    details: dict[str, Any] = {
        "store_id": keyring.store_id,
        "key_fingerprint": expected,
        "key_path": str(key_path),
        "retired_keys": len(retired),
    }
    lost = (
        "the next owner open (the daemon, a hook, the CLI) writes a new key and "
        "rekeys the log, and the rows signed before it stay unverifiable for good"
    )
    unreadable: KeyUnreadableError | None = None
    try:
        key = keyring.load_current()
    except KeyUnreadableError as exc:
        # There and unreadable, a symbolic link to nothing among them: the
        # owner's opener fails on it rather than rekey (`log.load_current`).
        key, unreadable = None, exc
    key_present, key_unreachable = _presence(key_path)
    if key is None and (
        unreadable is not None or key_present or key_unreachable is not None
    ):
        if unreadable is not None:
            where = f"is there ({unreadable.reason})"
        elif key_present:
            where = "exists"
        else:
            where = f"cannot be looked for ({key_unreachable})"
        return Finding(
            "key.match",
            "high",
            f"the key at {key_path} {where} and this account cannot read it: "
            "the log cannot be verified here, and every owner open (the daemon, "
            "a hook, the CLI) fails until it can; nothing is rekeyed over it",
            path=str(key_path),
            fix=(
                "make it readable to this account again (owner-only, 0600), "
                "and the volume a link to it points into mounted"
            ),
            details=details,
        )
    if key is None:
        return Finding(
            "key.match",
            "high",
            f"no key for store {keyring.store_id} under {keys_dir}: the log "
            f"cannot be verified here, and {lost}",
            path=str(key_path),
            fix=(
                f"restore {key_path.name} into {keys_dir} from where it was kept, "
                "or point BETTERMEMORY_KEYS_DIR at the directory that holds it, "
                "before anything opens the store"
            ),
            details=details,
        )
    held = fingerprint(key)
    details["held_fingerprint"] = held
    if held != expected:
        details["retired_match"] = keyring.key_for_fingerprint(expected) is not None
        return Finding(
            "key.match",
            "high",
            f"the key at {key_path} (fingerprint {held[:16]}) is not the store's "
            f"(fingerprint {expected[:16]}): {lost}",
            path=str(key_path),
            fix=(
                "find the key whose fingerprint the store records and put it "
                "back; `bettermemory log verify` shows which segments verify"
            ),
            details=details,
        )
    return Finding(
        "key.match",
        "ok",
        f"key present and matches the store (fingerprint {held[:16]}); "
        f"{len(retired)} retired key(s) held",
        path=str(key_path),
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
    store_probe = store_file if _presence(store_file)[0] else store_dir
    yield git_finding("store.git", "store", store_dir, store_probe, ignored_ok=True)
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


def git_finding(
    finding_id: str, label: str, directory: Path, probe: Path, *, ignored_ok: bool
) -> Finding:
    """Whether `directory` sits in a git working tree, and if so whether
    git tracks it or ignores `probe` (the store file, or the directory).
    Tracked is high: the store or the key goes wherever the repository
    goes. Not ignored is a warning: one ``git add -A`` away. Ignored is
    information for the store (``ignored_ok``) and a warning for the keys,
    which belong in no working tree at all."""
    if not os.path.isdir(directory):
        return Finding(finding_id, "ok", f"no {label} at {directory} yet")
    top_run = run_git(["rev-parse", "--show-toplevel"], cwd=directory, timeout=2.0)
    if top_run is None:
        return Finding(finding_id, "skip", f"git could not run; {label} not checked")
    top_text = top_run.stdout.strip()
    if top_run.returncode != 0 or not top_text:
        return Finding(finding_id, "ok", f"the {label} is not in a git working tree")
    top = Path(top_text).resolve()
    details: dict[str, Any] = {"worktree": str(top)}
    try:
        relative_dir = directory.resolve().relative_to(top).as_posix()
        relative_probe = probe.resolve().relative_to(top).as_posix()
    except ValueError:
        return Finding(finding_id, "ok", f"the {label} is not in a git working tree")
    tracked = run_git(
        ["ls-files", "--error-unmatch", "--", relative_dir], cwd=top, timeout=2.0
    )
    if tracked is not None and tracked.returncode == 0:
        details["tracked"] = True
        return Finding(
            finding_id,
            "high",
            f"the {label} {directory} is tracked by the git repository at {top}",
            path=str(directory),
            fix=(
                f"git rm -r --cached {shlex.quote(relative_dir)}, ignore it, and "
                "treat what was pushed as disclosed"
            ),
            details=details,
        )
    ignored = run_git(
        ["check-ignore", "-q", "--", relative_probe], cwd=top, timeout=2.0
    )
    details["tracked"] = False
    details["ignored"] = ignored is not None and ignored.returncode == 0
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


def synced_root(path: Path) -> str | None:
    """The sync service whose folder holds `path`, by the path patterns the
    clients use, or None. iCloud Drive keeps ``~/Library/Mobile Documents``;
    macOS File Provider clients (Dropbox, OneDrive, Google Drive and others
    since macOS 12.3) keep ``~/Library/CloudStorage/<provider>``; the older
    clients and the Windows ones keep a folder named for the service. A
    synced folder under another name is not seen."""
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
    object. Read, never written."""
    try:
        raw = bounded_read(path, _SETTINGS_CAP_BYTES)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as exc:
        return None, str(exc)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
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
    user's file. A project's own settings depend on where Claude Code
    starts, and are not read."""
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
    paths.append(("user", sandbox_settings.claude_user_settings_path()))
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


def _loopback_entries(domains: list[str], ports: tuple[int, ...]) -> list[str]:
    """The ``allowedDomains`` entries that open the daemon's port: a
    loopback host with no port, or with the daemon's."""
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
        if host in _LOOPBACK_HOSTS and (port is None or port in ports):
            flagged.append(entry)
    return flagged


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
    read = [str(path) for _, path, _ in documents]
    # Highest precedence first: the managed files, the last merged first,
    # then the user's.
    ranked = [doc for doc in reversed(documents) if doc[0] == "managed"] + [
        doc for doc in documents if doc[0] == "user"
    ]

    def scalar(*keys: str) -> tuple[Any, str | None]:
        for level, _, document in ranked:
            value = _dig(document, *keys)
            if value is not None:
                return value, level
        return None, None

    def locked(*keys: str) -> bool:
        return any(
            _dig(document, *keys) is True
            for level, _, document in documents
            if level == "managed"
        )

    def merged(*keys: str, managed_only: bool = False) -> list[str]:
        out: list[str] = []
        for level, _, document in documents:
            if level == "managed" or not managed_only:
                out.extend(_strings(_dig(document, *keys)))
        return out

    enabled, enabled_level = scalar("sandbox", "enabled")
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
    fail_if_unavailable, _ = scalar("sandbox", "failIfUnavailable")
    if fail_if_unavailable is not True:
        gaps += 1
        yield Finding(
            "claude.sandbox.fail_if_unavailable",
            "warn",
            "sandbox.failIfUnavailable is not true: where the sandbox cannot "
            "start, commands run without it",
            fix='set "failIfUnavailable": true under "sandbox"',
        )
    unsandboxed, _ = scalar("sandbox", "allowUnsandboxedCommands")
    if unsandboxed is not False:
        gaps += 1
        yield Finding(
            "claude.sandbox.unsandboxed_commands",
            "warn",
            "sandbox.allowUnsandboxedCommands is not false: a command the "
            "sandbox stops can be retried outside it",
            fix='set "allowUnsandboxedCommands": false under "sandbox"',
        )
    filesystem_off, _ = scalar("sandbox", "filesystem", "disabled")
    if filesystem_off is True:
        gaps += 1
        yield Finding(
            "claude.sandbox.filesystem_disabled",
            "warn",
            "sandbox.filesystem.disabled is true: sandboxed commands run without "
            "filesystem isolation, so denyRead holds nothing back",
            fix='remove "disabled" from "sandbox.filesystem"',
        )
    deny_read = merged("sandbox", "filesystem", "denyRead")
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
    allow_read = merged(
        "sandbox",
        "filesystem",
        "allowRead",
        managed_only=locked("sandbox", "filesystem", "allowManagedReadPathsOnly"),
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
    rules = merged("permissions", "deny")
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
    domains = merged(
        "sandbox",
        "network",
        "allowedDomains",
        managed_only=locked("sandbox", "network", "allowManagedDomainsOnly"),
    )
    flagged = _loopback_entries(domains, plan.ports)
    if flagged:
        gaps += 1
        yield Finding(
            "claude.sandbox.network",
            "warn",
            "sandbox.network.allowedDomains opens the daemon's port: "
            f"{', '.join(flagged)}",
            fix=(
                "name a port other than the daemon's "
                f"({', '.join(str(port) for port in plan.ports)}) on each, or drop them"
            ),
            details={"entries": flagged, "ports": list(plan.ports)},
        )
    local_binding, _ = scalar("sandbox", "network", "allowLocalBinding")
    if local_binding is True:
        gaps += 1
        yield Finding(
            "claude.sandbox.local_binding",
            "warn",
            "sandbox.network.allowLocalBinding is true",
            fix="leave it off",
        )
    excluded = merged("sandbox", "excludedCommands")
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
    finding, and the fix under each warning and high finding."""
    counts = report.counts()
    lines = [
        f"bettermemory security audit: {report.status}",
        f"  store {report.store} ({report.store_rule})",
    ]
    width = max((len(finding.id) for finding in report.findings), default=0)
    for finding in report.findings:
        lines.append(f"  {finding.status:<4}  {finding.id:<{width}}  {finding.summary}")
        if finding.fix and finding.status in ("warn", "high"):
            lines.append(f"  {'':<4}  {'':<{width}}  fix: {finding.fix}")
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
