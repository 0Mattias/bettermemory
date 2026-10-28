"""`bettermemory config path|show|check`: where the config lives, what is
in force, and what in it does not do what it says.

The loader is lenient by design. A section or key it does not read is
ignored, so ``[behaviour]`` (the British spelling) or ``enabeld = false``
under ``[telemetry]`` loads cleanly and changes nothing; a boolean it
cannot read (``strict_provenance = "ture"``, or ``1``) falls back to the
default without a word, by `_coerce_bool`'s rule that one bad knob must
not take the daemon down. For a ranking knob that is the right trade. For
a security switch it is not, and the person who wrote the line has no way
to learn that it is inert: the daemon starts, the hooks answer, and the
setting they meant to change is the setting they had. The organisation's
managed file (`managed.py`) adds a second source, and nothing showed which
of the two decided a value.

Three subcommands, each with ``--json``:

- ``path``: the config file, the managed file (none is read on Windows in
  this release, and the command says so rather than print a path nothing
  reads), the store directory with the rule that chose it, and the keys and
  state directories with what chose them.
- ``show``: every setting the loader reads, with its value in force and
  where it came from: ``default``, ``user`` (the user's config.toml) or
  ``managed``. A managed setting the user's file sets differently shows the
  managed value and names the user's, which is ignored; so does a user
  value the loader could not read, labelled ``default``.
- ``check``: the lines that do not do what they say, each an error, a
  warning or a note. Errors are what stop the config from loading (a file
  that is not TOML, a section that is not a table, a value the loader
  refuses, a `[policy] forbid_acknowledge` name it does not know) and what
  can never take effect as written (an `[scopes] allowed` entry outside
  the scope grammar, which no write can carry; a `[daemon] port` outside
  0..65535, which the daemon cannot bind). Warnings are lines the config
  loads past: an unknown section or key, with a did-you-mean from difflib;
  a key or section a release removed or renamed; a value that did not
  parse and fell back to the default; a value clamped where it is read; a
  `[storage] directory` under a system directory.
  Notes are user lines a managed pin overrides. Exit 0 when there is
  nothing above a note, 1 on any error, 2 on warnings only.

Read-only, all three. The config is loaded with ``create=False``
(`config.load_config`), so a missing config.toml reads as the defaults it
would be written with and is never created, and nothing here opens the
store, creates a directory or touches the keys. The loader's own notices
(a removed key, a pinned key, a fallback, a store under a system
directory) are held while the commands load the config, and each command
reports the ones that bear on it: `check` has a finding for every one,
`show` lists them, `path` keeps those about where the store is. None is
printed twice.

What `check` reads is the loader's own: the settings are found by walking
`config.Config`'s dataclasses (the walk `config._apply_managed` makes), the
removed keys come from the loader's registries, and every value is parsed
by `config._config_from_data` on its own, so a check and a load cannot
disagree about whether a value is valid. Only the silent fallbacks, which
the loader by design does not report, are recognised here: a boolean
setting whose value is not one of the spellings `_coerce_bool` reads, and
the two string settings that normalise (`search_mode`, `prompt_recall`).
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import json
import logging
import os
import sys
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, TypeGuard

from .. import config as _config
from .. import managed as _managed

#: The levels of a `check` finding, worst first. A note never moves the
#: exit code.
ERROR = "error"
WARNING = "warning"
NOTE = "note"
LEVELS: tuple[str, ...] = (ERROR, WARNING, NOTE)

#: `check`'s exit codes: nothing above a note, any error, warnings only.
#: `path` and `show` answer EXIT_OK, or EXIT_ERROR when the config does not
#: load.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_WARNING = 2

#: Where a value came from, as `show` labels it.
DEFAULT = "default"
USER = "user"
MANAGED = "managed"

#: Said where the managed file would be, on the platform where none is
#: read (`managed.managed_config_path` answers None there).
WINDOWS_MANAGED_NOTE = (
    "no managed file is read on Windows in this release: a standard account "
    "can create the folder under ProgramData, so a file there is not trusted "
    "until its owner is checked"
)

#: The legacy spellings the loader still reads, as (section, old) -> new
#: (`config._apply_legacy_endorsement_debt_alias`).
_ALIASES: dict[tuple[str, str], str] = {
    ("behavior", "endorsement_debt_ratio_threshold"): (
        "cold_endorsement_ratio_threshold"
    ),
}

#: The ports a socket can bind; anything else makes `daemon.bind` raise
#: OverflowError before the fallback to an ephemeral port can run.
_PORT_MAX = 65535
#: Below this a port is privileged on Linux by default: an unprivileged
#: daemon cannot bind it and falls back to an ephemeral port each start.
_FIRST_UNPRIVILEGED_PORT = 1024


# ---------------------------------------------------------------------------
# What the loader reads
# ---------------------------------------------------------------------------


def settings_catalogue() -> dict[str, tuple[str, ...]]:
    """Every section `Config` reads and the keys of each, in the
    dataclasses' own order: the walk `config._apply_managed` makes to find
    what a managed file can pin, so a section a later release adds is
    listed, shown and checked the moment it parses."""
    defaults = _config.Config()
    catalogue: dict[str, tuple[str, ...]] = {}
    for section_field in fields(defaults):
        section = getattr(defaults, section_field.name)
        if is_dataclass(section) and not isinstance(section, type):
            catalogue[section_field.name] = tuple(f.name for f in fields(section))
    return catalogue


def _removed_keys(section: str) -> Mapping[str, tuple[str, str]]:
    """The loader's registry of keys a release removed from `section`."""
    registries: dict[str, Mapping[str, tuple[str, str]]] = {
        "behavior": _config._REMOVED_BEHAVIOR_KEYS,
        "telemetry": _config._REMOVED_TELEMETRY_KEYS,
        "capture": _config._REMOVED_CAPTURE_KEYS,
    }
    return registries.get(section, {})


def _default(section: str, key: str) -> Any:
    return getattr(getattr(_config.Config(), section), key)


def raw_settings(data: Mapping[str, Any] | None) -> dict[tuple[str, str], Any]:
    """The settings one parsed file sets, as (section, key) -> the value as
    written, for the keys the loader reads. A legacy spelling counts as
    its successor, which wins when both are written, as in the loader."""
    if data is None:
        return {}
    catalogue = settings_catalogue()
    found: dict[tuple[str, str], Any] = {}
    for section, keys in catalogue.items():
        table = data.get(section)
        if not isinstance(table, dict):
            continue
        for (alias_section, old), new in _ALIASES.items():
            if alias_section == section and old in table and new not in table:
                found[(section, new)] = table[old]
        for key in keys:
            if key in table:
                found[(section, key)] = table[key]
    return found


@contextlib.contextmanager
def _held_notices() -> Iterator[list[str]]:
    """The warnings the config module logs inside the block, held rather
    than printed. The loader logs a removed key, a pinned key, a fallback
    or a store under a system directory once, on the log lane; these
    commands report them in their own output instead (`check` as findings,
    `show` and `path` as notices), so none is printed twice."""
    logger = logging.getLogger("bettermemory.config")
    held: list[str] = []

    class _Hold(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            held.append(record.getMessage())

    handler = _Hold(logging.WARNING)
    propagate = logger.propagate
    logger.addHandler(handler)
    logger.propagate = False
    try:
        yield held
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate


# ---------------------------------------------------------------------------
# The two files
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigFile:
    """One of the two files the config is built from.

    `role` is ``user`` (config.toml) or ``managed``. `path` is None only
    for the managed file on a platform where none is read, and `note` then
    says why. `data` is the parsed file, None when it is missing or does
    not parse, and `error` then says why it does not."""

    role: str
    path: Path | None
    exists: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    note: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "path": None if self.path is None else str(self.path),
            "exists": self.exists,
            "parses": self.data is not None,
            "error": self.error,
            "note": self.note,
        }


def _present(path: Path) -> bool:
    """Whether anything is at `path`, a dangling symlink included.

    Only FileNotFoundError reads as absent, as it does in both loaders. A
    path this account cannot look at (in a directory it may not search,
    or under a file where a directory belongs) is present: both loaders
    fail on it (`managed.read_managed` refuses it, and `load_config`
    raises on it), so the reports call it unreadable rather than "not
    created yet" or "none deployed", which is what `os.path.lexists`
    would make of it."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _read(role: str, path: Path) -> ConfigFile:
    """Read and parse one file without loading it. A missing file is not
    an error (the defaults apply, or nothing is pinned); a file that is
    there and does not parse is, with the reason the loader would give."""
    if not _present(path):
        return ConfigFile(role, path, exists=False)
    try:
        data = _config.read_config_file(path)
    except ValueError as exc:
        return ConfigFile(role, path, exists=True, error=str(exc))
    except OSError as exc:
        reason = exc.strerror or str(exc)
        if isinstance(exc, FileNotFoundError) and os.path.islink(path):
            reason = "it is a symlink to a file that does not exist"
        return ConfigFile(
            role, path, exists=True, error=f"{path} could not be read: {reason}"
        )
    return ConfigFile(role, path, exists=True, data=data)


def read_files() -> tuple[ConfigFile, ConfigFile]:
    """The user's config.toml and the managed file, each read through the
    module that owns its location, looked up at call time (the test suite
    points both at scratch paths)."""
    user = _read(USER, _config.default_config_path())
    managed_path = _managed.managed_config_path()
    if managed_path is None:
        managed = ConfigFile(MANAGED, None, exists=False, note=WINDOWS_MANAGED_NOTE)
    else:
        managed = _read(MANAGED, managed_path)
    return user, managed


def _load() -> _config.Config:
    """The effective config, loaded without its first-run write."""
    return _config.load_config(_config.default_config_path(), create=False)


# ---------------------------------------------------------------------------
# config path
# ---------------------------------------------------------------------------


def _store_entry(config: _config.Config | None, error: str | None) -> dict[str, Any]:
    """The store directory and the rule of `Config.resolved_directory`
    that chose it, read back by `sandbox_settings.store_rule`, the reader
    `security sandbox` and the posture audit use. When the config does not
    load, only ``BETTERMEMORY_DIR`` can still name the store, as it does
    for the hook client, which reads no config when the variable is set."""
    from ..sandbox_settings import store_rule

    entry: dict[str, Any] = {"path": None, "rule": None, "exists": False, "notes": []}
    if config is None:
        named = os.environ.get(_config.ENV_DIR_OVERRIDE)
        if not named:
            entry["notes"] = [f"not resolved: the config does not load ({error})"]
            return entry
        directory = Path(named).expanduser().resolve()
        entry.update(
            path=str(directory),
            rule=_config.ENV_DIR_OVERRIDE,
            exists=os.path.isdir(directory),
        )
        return entry
    with _held_notices() as notices:
        directory = config.resolved_directory()
    rule = store_rule(config, directory)
    if rule == "[storage] directory" and "storage.directory" in config.managed_keys:
        rule += " (managed)"
    notes = list(notices)
    if not config.storage.cwd_store and not os.environ.get(_config.ENV_DIR_OVERRIDE):
        with contextlib.suppress(OSError):
            here = (Path.cwd() / _config.PROJECT_DIR_NAME).resolve()
            if (
                os.path.isdir(here)
                and here != directory
                and not config.storage.directory
            ):
                pinned = (
                    " (managed)" if "storage.cwd_store" in config.managed_keys else ""
                )
                notes.append(f"{here} is not used: [storage] cwd_store = false{pinned}")
    entry.update(
        path=str(directory), rule=rule, exists=os.path.isdir(directory), notes=notes
    )
    return entry


def path_report() -> dict[str, Any]:
    """Every location the config and the environment resolve, and what
    chose each. Nothing is created; each entry says whether it exists."""
    from .. import _daemon_client
    from .. import store as _store

    user_path = _config.default_config_path()
    managed_path = _managed.managed_config_path()
    config: _config.Config | None = None
    error: str | None = None
    with _held_notices():
        try:
            config = _load()
        except (ValueError, OSError) as exc:
            error = str(exc)
    managed: dict[str, Any] = {
        "path": None if managed_path is None else str(managed_path),
        "exists": managed_path is not None and _present(managed_path),
        "read": managed_path is not None,
        "pinned": None if config is None else sorted(config.managed_keys),
        "note": WINDOWS_MANAGED_NOTE if managed_path is None else None,
    }
    keys_dir = _store.resolve_keys_dir().expanduser()
    state_dir = _daemon_client.resolve_state_dir().expanduser()
    return {
        "config": {"path": str(user_path), "exists": _present(user_path)},
        "managed": managed,
        "store": _store_entry(config, error),
        "keys": {
            "path": str(keys_dir),
            "source": (
                _config.KEYS_DIR_ENV
                if os.environ.get(_config.KEYS_DIR_ENV)
                else "user config directory"
            ),
            "exists": os.path.isdir(keys_dir),
        },
        "state": {
            "path": str(state_dir),
            "source": (
                _daemon_client.STATE_DIR_ENV
                if os.environ.get(_daemon_client.STATE_DIR_ENV)
                else "user state directory"
            ),
            "exists": os.path.isdir(state_dir),
        },
        "error": error,
    }


def render_path(report: Mapping[str, Any]) -> str:
    """The text form of `path_report`: one line per location."""
    lines: list[str] = []

    def row(label: str, value: str) -> None:
        lines.append(f"{label:<13} {value}")

    config = report["config"]
    row(
        "config file",
        config["path"]
        + ("" if config["exists"] else " (not created yet: the defaults apply)"),
    )
    managed = report["managed"]
    if managed["path"] is None:
        row("managed file", f"none ({managed['note']})")
    elif not managed["exists"]:
        row("managed file", f"{managed['path']} (none deployed: nothing is pinned)")
    else:
        pinned = managed["pinned"]
        count = "" if pinned is None else f", pins {_plural(len(pinned), 'setting')}"
        row("managed file", f"{managed['path']} (present{count})")
    store = report["store"]
    if store["path"] is None:
        row("store", "(not resolved)")
    else:
        missing = "" if store["exists"] else "; not created yet"
        row("store", f"{store['path']}  ({store['rule']}{missing})")
    for note in store["notes"]:
        lines.append(f"{'':<13}   {note}")
    for name in ("keys", "state"):
        entry = report[name]
        missing = "" if entry["exists"] else "; not created yet"
        row(name, f"{entry['path']}  ({entry['source']}{missing})")
    if report["error"] is not None:
        lines.append(f"the config does not load: {report['error']}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# config show
# ---------------------------------------------------------------------------


def show_report() -> dict[str, Any]:
    """The effective config, every setting labelled with its source.

    A user line the loader could not read, and so read as the default
    (`_fell_back`), is labelled ``default`` and its value named, as a
    pinned one is: labelled ``user``, ``strict_provenance = "ture"`` would
    show ``false # user``, which reads as the setting the line chose.

    Raises what `load_config` raises when the config does not load: there
    is no effective config to show then, and `check` says why."""
    user, managed = read_files()
    with _held_notices() as notices:
        config = _load()
    user_raw = raw_settings(user.data)
    managed_raw = raw_settings(managed.data)
    settings: dict[str, dict[str, Any]] = {}
    for section, keys in settings_catalogue().items():
        table: dict[str, Any] = {}
        for key in keys:
            value = getattr(getattr(config, section), key)
            entry: dict[str, Any] = {
                "value": list(value) if isinstance(value, list) else value
            }
            if f"{section}.{key}" in config.managed_keys:
                entry["source"] = MANAGED
                where = (section, key)
                if where in user_raw and user_raw[where] != managed_raw.get(where):
                    entry["user_value"] = user_raw[where]
            elif (section, key) in user_raw:
                written = user_raw[(section, key)]
                if _fell_back(section, key, written):
                    entry["source"] = DEFAULT
                    entry["user_value"] = written
                else:
                    entry["source"] = USER
            else:
                entry["source"] = DEFAULT
            table[key] = entry
        settings[section] = table
    return {
        "config": user.to_json(),
        "managed": managed.to_json(),
        "settings": settings,
        "notices": notices,
    }


def _shown(value: object) -> str:
    """A value as config.toml spells it (near enough): JSON's spelling of
    strings, booleans, numbers and lists is TOML's. A TOML date or time
    has no JSON form and is written as its own text."""
    if value is None:
        return "(not set)"
    return json.dumps(value, ensure_ascii=False, default=str)


def render_show(report: Mapping[str, Any]) -> str:
    """The text form of `show_report`: config.toml's own layout, with the
    source of each value after it."""
    lines = [f"# config file: {_file_line(report['config'])}"]
    lines.append(f"# managed file: {_file_line(report['managed'])}")
    rows: list[tuple[str, str]] = []
    for section, table in report["settings"].items():
        rows.append((f"[{section}]", ""))
        for key, entry in table.items():
            source = entry["source"]
            if "user_value" in entry:
                source += f" (your config's {_shown(entry['user_value'])} is ignored)"
            rows.append((f"{key} = {_shown(entry['value'])}", source))
    width = min(48, max(len(left) for left, source in rows if source))
    for left, source in rows:
        if not source:
            lines.extend(["", left])
        else:
            lines.append(f"{left:<{width}}  # {source}")
    if report["notices"]:
        lines.append("")
        lines.extend(f"# notice: {notice}" for notice in report["notices"])
    return "\n".join(lines) + "\n"


def _file_line(entry: Mapping[str, Any]) -> str:
    """One file as the reports name it: its path and, when it is not
    there, what that means for its role."""
    if entry["path"] is None:
        return f"none ({entry['note']})"
    if not entry["exists"]:
        if entry["role"] == USER:
            return f"{entry['path']} (not created yet: the defaults apply)"
        return f"{entry['path']} (none deployed: nothing is pinned)"
    return str(entry["path"])


# ---------------------------------------------------------------------------
# config check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One line of `check`'s report.

    `code` is stable for scripts: unreadable, not_a_table, unknown_section,
    unknown_key, removed, renamed, invalid_value, unknown_acknowledge_name,
    fallback, clamped, invalid_scope, port_out_of_range, privileged_port,
    system_directory, pinned, does_not_load. `file` is the role of the file the line is in
    (``user`` or ``managed``), None for the load as a whole. `message` is
    a sentence that names the section and key; `suggestion` the fix, when
    there is one to give."""

    level: str
    code: str
    file: str | None
    section: str | None
    key: str | None
    message: str
    suggestion: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "file": self.file,
            "section": self.section,
            "key": self.key,
            "message": self.message,
            "suggestion": self.suggestion,
        }


def _name(name: str) -> str:
    """A section or key name as the report prints it. A bare TOML key is
    letters, digits, ``_`` and ``-``; anything else (a quoted key) is
    printed quoted, escapes and all, so a key holding a control character
    cannot write to the terminal."""
    if name and all(ch.isascii() and (ch.isalnum() or ch in "_-") for ch in name):
        return name
    return json.dumps(name)


def _close(word: str, candidates: list[str], cutoff: float = 0.6) -> str | None:
    """The candidate `word` most likely misspells, compared without case,
    or None when none is close."""
    lowered = {candidate.lower(): candidate for candidate in candidates}
    match = difflib.get_close_matches(word.lower(), list(lowered), n=1, cutoff=cutoff)
    return lowered[match[0]] if match else None


def _unknown_section(role: str, name: str, catalogue: Mapping[str, Any]) -> Finding:
    guess = _close(name, list(catalogue))
    return Finding(
        WARNING,
        "unknown_section",
        role,
        name,
        None,
        f"[{_name(name)}] is not a section bettermemory reads; every line in it "
        "is ignored.",
        None if guess is None else f"did you mean [{guess}]?",
    )


def _guess_elsewhere(
    key: str, catalogue: Mapping[str, tuple[str, ...]], *, skip: str | None
) -> str | None:
    """``[section] key`` for a key that belongs to another section: the
    same name first, then a close one (a stricter cutoff, since a match in
    an unrelated section is a weaker guess)."""
    for section, keys in catalogue.items():
        if section != skip and key in keys:
            return f"[{section}] {key}"
    pairs = {
        k: f"[{section}] {k}"
        for section, keys in catalogue.items()
        if section != skip
        for k in keys
    }
    guess = _close(key, list(pairs), cutoff=0.8)
    return None if guess is None else pairs[guess]


def _unknown_key(
    role: str, section: str | None, key: str, catalogue: Mapping[str, tuple[str, ...]]
) -> Finding:
    if section is None:
        where = f"`{_name(key)}`, outside any section,"
        guess = _guess_elsewhere(key, catalogue, skip=None)
        suggestion = (
            None
            if guess is None
            else f"did you mean {guess}? Put the line under its section header."
        )
    else:
        where = f"[{section}] {_name(key)}"
        same = _close(key, list(catalogue[section]))
        if same is not None:
            suggestion = f"did you mean {same}?"
        else:
            guess = _guess_elsewhere(key, catalogue, skip=section)
            suggestion = (
                None
                if guess is None
                else f"did you mean {guess}? Move the line under that section."
            )
    return Finding(
        WARNING,
        "unknown_key",
        role,
        section,
        key,
        f"{where} is not a setting bettermemory reads; the line is ignored.",
        suggestion,
    )


def _not_a_table(role: str, name: str, value: object) -> Finding:
    """An error either way: the line never takes effect. In the user's file
    the three sections rc11 read as their defaults still load that way
    (`config._SCALAR_TOLERATED_SECTIONS`); anything else stops the load."""
    if role == USER and name in _config._SCALAR_TOLERATED_SECTIONS:
        outcome = "the line is ignored and the section's defaults are in force."
    else:
        outcome = "the config does not load."
    return Finding(
        ERROR,
        "not_a_table",
        role,
        name,
        None,
        f"`{_name(name)}` = {_shown(value)} must be a [{_name(name)}] section "
        f"table; {outcome}",
        f"write the settings under a [{_name(name)}] header, one per line",
    )


def _strip_location(message: str, path: Path | None) -> str:
    """The loader's located error without its file prefix: the report
    groups findings under their file already."""
    if path is not None:
        message = message.removeprefix(f"malformed config in {path}: ")
    return message


def _parse_one(section: str, key: str, value: object, path: Path) -> Any:
    """`value` as the loader reads `[section] key`, parsed on its own by
    the loader's own code, so the check and a load cannot disagree.
    Raises the loader's located ValueError when it refuses the value."""
    with _held_notices():
        parsed = _config._config_from_data({section: {key: value}}, path)
    return getattr(getattr(parsed, section), key)


def _is_bool_spelling(value: object) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        token = value.strip().lower()
        return token in _config._TRUE_STRINGS or token in _config._FALSE_STRINGS
    return False


def _fell_back(section: str, key: str, value: object) -> bool:
    """Whether the loader read `value` as the default because it could not
    read it at all, the one outcome it never reports."""
    if (section, key) == ("behavior", "search_mode"):
        return str(value).strip().lower() not in _config._SEARCH_MODES
    if (section, key) == ("behavior", "prompt_recall"):
        if isinstance(value, bool):
            return False
        return not (
            isinstance(value, str)
            and (
                value.strip().lower() in _config.PROMPT_RECALL_MODES
                or _is_bool_spelling(value)
            )
        )
    if isinstance(_default(section, key), bool):
        return not _is_bool_spelling(value)
    return False


def _scope_findings(role: str, key: str, entries: list[str]) -> list[Finding]:
    """`[scopes] allowed` and `typo_exceptions` entries outside the scope
    grammar (`models.validate_scope`). Every write validates its scopes
    against the grammar before the allowlist is consulted, so an allowed
    entry outside it admits nothing (an error), and a typo exception
    outside it can never match a flagged scope (a warning: it is inert)."""
    from ..models import validate_scope

    findings: list[Finding] = []
    for entry in entries:
        try:
            validate_scope(entry)
        except ValueError:
            pass
        else:
            continue
        candidate = entry.strip().lower()
        try:
            validate_scope(candidate)
        except ValueError:
            suggestion = None
        else:
            suggestion = f"did you mean {_shown(candidate)}?"
        if key == "allowed":
            level = ERROR
            effect = "no write can carry it, so the allowlist admits nothing by it"
        else:
            level = WARNING
            effect = "no scope can match it, so the entry is inert"
        findings.append(
            Finding(
                level,
                "invalid_scope",
                role,
                "scopes",
                key,
                f"[scopes] {key} entry {_shown(entry)} is not a scope (lowercase "
                f"letters and digits, joined by - or :); {effect}.",
                suggestion or "write the scope the way memory_write takes it",
            )
        )
    return findings


def _acknowledge_findings(role: str, names: list[str]) -> list[Finding]:
    """One error per `[policy] forbid_acknowledge` name `ACKNOWLEDGE_NAMES`
    does not know. The loader refuses the first and stops; this names
    every one."""
    known = list(_config.ACKNOWLEDGE_NAMES)
    findings: list[Finding] = []
    for name in names:
        if name in known:
            continue
        bare = name.removeprefix("acknowledge_")
        if bare != name and bare in known:
            suggestion = f'write "{bare}", without the acknowledge_ prefix'
        else:
            guess = _close(name, known)
            suggestion = (
                f'did you mean "{guess}"?'
                if guess is not None
                else "one of " + ", ".join(known)
            )
        findings.append(
            Finding(
                ERROR,
                "unknown_acknowledge_name",
                role,
                "policy",
                "forbid_acknowledge",
                f"[policy] forbid_acknowledge names {_shown(name)}, which is not "
                "an acknowledge flag of memory_write or memory_update; the "
                "config does not load.",
                suggestion,
            )
        )
    return findings


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _value_findings(
    role: str, path: Path, section: str, key: str, value: object
) -> list[Finding]:
    """What is wrong with one value the loader reads, if anything."""
    label = f"[{section}] {key}"
    if (section, key) == ("policy", "forbid_acknowledge") and _is_str_list(value):
        return _acknowledge_findings(role, value)
    try:
        parsed = _parse_one(section, key, value, path)
    except ValueError as exc:
        return [
            Finding(
                ERROR,
                "invalid_value",
                role,
                section,
                key,
                _strip_location(str(exc), path) + "; the config does not load.",
            )
        ]
    if _fell_back(section, key, value):
        return [
            Finding(
                WARNING,
                "fallback",
                role,
                section,
                key,
                f"{label} = {_shown(value)} is not a value it takes, so the "
                f"default, {_shown(parsed)}, is in force.",
                _fallback_hint(section, key),
            )
        ]
    findings: list[Finding] = []
    if (section, key) == ("behavior", "default_max_results"):
        from ..handlers.search import MAX_SEARCH_RESULTS, clamp_search_width

        clamped = clamp_search_width(parsed)
        if clamped != parsed:
            findings.append(
                Finding(
                    WARNING,
                    "clamped",
                    role,
                    section,
                    key,
                    f"{label} = {parsed} is outside 1..{MAX_SEARCH_RESULTS}; "
                    f"searches read it as {clamped}.",
                    f"write a value from 1 to {MAX_SEARCH_RESULTS}",
                )
            )
    elif (section, key) == ("capture", "checkpoint_tokens"):
        written = _config._coerce_int(value, parsed, label=label, config_path=path)
        if written < _config.MIN_CHECKPOINT_TOKENS:
            findings.append(
                Finding(
                    WARNING,
                    "clamped",
                    role,
                    section,
                    key,
                    f"{label} = {written} is below {_config.MIN_CHECKPOINT_TOKENS}; "
                    f"the loader raises it to {_config.MIN_CHECKPOINT_TOKENS}.",
                    f"write {_config.MIN_CHECKPOINT_TOKENS} or more",
                )
            )
    elif (section, key) == ("daemon", "port"):
        findings.extend(_port_findings(role, parsed))
    elif section == "scopes" and isinstance(parsed, list):
        findings.extend(_scope_findings(role, key, parsed))
    elif (section, key) == ("storage", "directory") and isinstance(parsed, str):
        findings.extend(_system_dir_findings(role, parsed))
    return findings


def _system_dir_findings(role: str, directory: str) -> list[Finding]:
    """`[storage] directory` under a system directory, by the loader's own
    test (`config._warn_on_system_dir`), which logs it only when the store
    is resolved: the line loads, and the warning reaches the daemon's log,
    not the person who wrote it."""
    try:
        resolved = Path(directory).expanduser().resolve()
    except (OSError, RuntimeError):
        return []
    with _held_notices() as held:
        _config._warn_on_system_dir("[storage] directory", resolved)
    if not held:
        return []
    return [
        Finding(
            WARNING,
            "system_directory",
            role,
            "storage",
            "directory",
            f"[storage] directory = {_shown(directory)} resolves to {resolved}, "
            "under a system directory; bettermemory will try to use it, but it "
            "is almost always a misconfiguration.",
            "point it at a directory your account owns",
        )
    ]


def _fallback_hint(section: str, key: str) -> str:
    if (section, key) == ("behavior", "search_mode"):
        return "one of " + ", ".join(_config._SEARCH_MODES)
    if (section, key) == ("behavior", "prompt_recall"):
        return "one of " + ", ".join(_config.PROMPT_RECALL_MODES)
    return "write true or false"


def _port_findings(role: str, port: int) -> list[Finding]:
    if not 0 <= port <= _PORT_MAX:
        return [
            Finding(
                ERROR,
                "port_out_of_range",
                role,
                "daemon",
                "port",
                f"[daemon] port = {port} is outside 0..{_PORT_MAX}; the daemon "
                "cannot bind it and does not start.",
                f"a port from 1024 to {_PORT_MAX}, or 0 for an ephemeral one",
            )
        ]
    if 0 < port < _FIRST_UNPRIVILEGED_PORT:
        return [
            Finding(
                WARNING,
                "privileged_port",
                role,
                "daemon",
                "port",
                f"[daemon] port = {port} is a privileged port; where an "
                "unprivileged process may not bind it (Linux, by default) the "
                "daemon binds an ephemeral port instead.",
                f"a port from 1024 to {_PORT_MAX}",
            )
        ]
    return []


def _section_findings(
    file: ConfigFile,
    path: Path,
    section: str,
    table: Mapping[str, Any],
    *,
    catalogue: Mapping[str, tuple[str, ...]],
    pinned: Mapping[tuple[str, str], Any],
) -> list[Finding]:
    """Every line of one known section: removed, renamed, unknown, pinned
    by the managed file (in the user's file), or a value to check."""
    findings: list[Finding] = []
    lines = dict(table)
    for (alias_section, old), new in _ALIASES.items():
        if alias_section != section or old not in lines:
            continue
        if new in lines:
            findings.append(
                Finding(
                    WARNING,
                    "renamed",
                    file.role,
                    section,
                    old,
                    f"[{section}] sets both {old} and {new}, the name it took in "
                    f"3.2.0; {new} is in force and {old} is ignored.",
                    f"delete {old}",
                )
            )
            del lines[old]
        else:
            findings.append(
                Finding(
                    WARNING,
                    "renamed",
                    file.role,
                    section,
                    old,
                    f"[{section}] {old} was renamed {new} in 3.2.0; it still "
                    "reads as the new name for now.",
                    f"rename it to {new}",
                )
            )
            lines[new] = lines.pop(old)
    removed = _removed_keys(section)
    for key, value in lines.items():
        if key in removed:
            removed_in, why = removed[key]
            findings.append(
                Finding(
                    WARNING,
                    "removed",
                    file.role,
                    section,
                    key,
                    f"[{section}] {key} was removed in bettermemory {removed_in}: "
                    f"{why}. The line is ignored.",
                    "delete the line",
                )
            )
        elif key not in catalogue[section]:
            findings.append(_unknown_key(file.role, section, key, catalogue))
        elif (section, key) in pinned:
            if value != pinned[(section, key)]:
                findings.append(
                    Finding(
                        NOTE,
                        "pinned",
                        file.role,
                        section,
                        key,
                        f"[{section}] {key} = {_shown(value)} is ignored: the "
                        f"managed file pins it to {_shown(pinned[(section, key)])}.",
                        "delete the line",
                    )
                )
        else:
            findings.extend(_value_findings(file.role, path, section, key, value))
    return findings


def _file_findings(
    file: ConfigFile, *, pinned: Mapping[tuple[str, str], Any]
) -> list[Finding]:
    """Every finding in one parsed file. The managed loader refuses any
    top-level value that is not a table (`managed.read_managed`), so in
    the managed file such a line is an error whatever its name, a removed
    section's included (it is refused before any section is read); in the
    user's file an unknown one is ignored like any unknown key."""
    if file.error is not None:
        return [Finding(ERROR, "unreadable", file.role, None, None, file.error)]
    if file.data is None or file.path is None:
        return []
    catalogue = settings_catalogue()
    removed_sections = _config._REMOVED_SECTIONS
    findings: list[Finding] = []
    for name, value in file.data.items():
        if file.role == MANAGED and not isinstance(value, dict):
            findings.append(_not_a_table(file.role, name, value))
        elif name in removed_sections:
            removed_in, why = removed_sections[name]
            findings.append(
                Finding(
                    WARNING,
                    "removed",
                    file.role,
                    name,
                    None,
                    f"[{name}] was removed in bettermemory {removed_in}: {why}. "
                    "Every line in it is ignored.",
                    "delete the section",
                )
            )
        elif name in catalogue and isinstance(value, dict):
            findings.extend(
                _section_findings(
                    file, file.path, name, value, catalogue=catalogue, pinned=pinned
                )
            )
        elif name in catalogue:
            findings.append(_not_a_table(file.role, name, value))
        elif isinstance(value, dict):
            findings.append(_unknown_section(file.role, name, catalogue))
        else:
            findings.append(_unknown_key(file.role, None, name, catalogue))
    return findings


def check_report() -> dict[str, Any]:
    """Every finding in the user's file and the managed file, the verdict
    of a whole load beside them, and the exit code.

    The whole load runs last, as `load_config(create=False)`. Each value
    has been parsed on its own above, so a load that fails has normally
    been explained already; when no error finding accounts for it (a
    failure the per-line checks do not model), its message is a finding of
    its own, so a config that does not load can never check clean."""
    user, managed = read_files()
    findings = _file_findings(user, pinned=raw_settings(managed.data))
    findings += _file_findings(managed, pinned={})
    load_error: str | None = None
    with _held_notices():
        try:
            _load()
        except (ValueError, OSError) as exc:
            load_error = str(exc)
        except Exception as exc:  # noqa: BLE001 - every load failure is a finding
            load_error = f"{type(exc).__name__}: {exc}"
    if load_error is not None and not any(f.level == ERROR for f in findings):
        findings.append(
            Finding(
                ERROR,
                "does_not_load",
                None,
                None,
                None,
                f"the config does not load: {load_error}",
            )
        )
    counts = {level: sum(1 for f in findings if f.level == level) for level in LEVELS}
    if counts[ERROR]:
        status, code = ERROR, EXIT_ERROR
    elif counts[WARNING]:
        status, code = WARNING, EXIT_WARNING
    else:
        status, code = "ok", EXIT_OK
    return {
        "status": status,
        "exit_code": code,
        "loads": load_error is None,
        "load_error": load_error,
        "files": [user.to_json(), managed.to_json()],
        "counts": counts,
        "findings": [f.to_json() for f in findings],
    }


def render_check(report: Mapping[str, Any]) -> str:
    """The text form of `check_report`: the findings grouped under the
    file they are in, then the tally."""
    lines: list[str] = []
    labels = {USER: "config file", MANAGED: "managed file"}
    for entry in report["files"]:
        lines.append(f"{labels[entry['role']]}: {_file_line(entry)}")
        own = [f for f in report["findings"] if f["file"] == entry["role"]]
        if not own:
            lines.append("  ok" if entry["parses"] else "  nothing to check")
        lines.extend(_finding_lines(own))
    loose = [f for f in report["findings"] if f["file"] is None]
    if loose:
        lines.append("the config as a whole")
        lines.extend(_finding_lines(loose))
    counts = report["counts"]
    tally = ", ".join(
        [
            _plural(counts[ERROR], "error"),
            _plural(counts[WARNING], "warning"),
            _plural(counts[NOTE], "note"),
        ]
    )
    lines.append("")
    lines.append(f"config check: {report['status']} ({tally})")
    return "\n".join(lines) + "\n"


def _finding_lines(findings: list[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    for finding in findings:
        lines.append(f"  {finding['level']:<8} {finding['message']}")
        if finding["suggestion"]:
            lines.append(f"  {'':<8} {finding['suggestion']}")
    return lines


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _terminal_safe(text: str) -> str:
    """`text` with every control, format or surrogate character but the
    newline and the tab written as an escape. The report prints what two
    files say, keys and values included, and neither should be able to
    move the cursor or reorder a line on the terminal it is read in."""
    out: list[str] = []
    for ch in text:
        if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf", "Cs"):
            out.append(ch)
        else:
            out.append(f"\\u{ord(ch):04x}")
    return "".join(out)


def _emit(document: Mapping[str, Any], text: str, *, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(
            json.dumps(document, indent=2, ensure_ascii=True, default=str) + "\n"
        )
    else:
        sys.stdout.write(_terminal_safe(text))


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register the ``config`` group and its three subcommands."""
    help_text = (
        "Where the config lives (`config path`), the settings in force and "
        "where each came from (`config show`), and the lines that do not do "
        "what they say (`config check`). Read-only: never creates the config "
        "file, the store or the keys."
    )
    parser = sub.add_parser("config", help=help_text, description=help_text)
    config_sub = parser.add_subparsers(dest="config_cmd")

    path_text = (
        "Print the config file, the organisation's managed file (none is read "
        "on Windows in this release), the store directory with the rule that "
        "chose it, and the keys and state directories. Exit 0, or 1 when the "
        "config does not load (the store is then resolved only from "
        "BETTERMEMORY_DIR)."
    )
    path = config_sub.add_parser("path", help=path_text, description=path_text)
    path.add_argument(
        "--json",
        action="store_true",
        help="Emit {config, managed, store, keys, state, error} as JSON.",
    )

    show_text = (
        "Print every setting in force, each labelled default, user (your "
        "config.toml) or managed (pinned by the managed file, which your "
        "config cannot change; a value of yours it overrides is named, as is "
        "one the loader could not read and took the default for). Exit 0, or "
        "1 when the config does not load (`config check` says why)."
    )
    show = config_sub.add_parser("show", help=show_text, description=show_text)
    show.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit {config, managed, settings: {section: {key: {value, source, "
            "user_value?}}}, notices} as JSON."
        ),
    )

    check_text = (
        "Check config.toml and the managed file: unknown sections and keys "
        "(with a did-you-mean), removed and renamed keys, values that do not "
        "load or fall back to the default, [scopes] allowed entries outside "
        "the scope grammar, [policy] forbid_acknowledge names, the [daemon] "
        "port range, and your lines a managed pin overrides. Exit 0 when "
        "nothing needs attention, 1 on any error (the config does not load, "
        "or a setting can never take effect as written), 2 on warnings only."
    )
    check = config_sub.add_parser("check", help=check_text, description=check_text)
    check.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit {status, exit_code, loads, load_error, files, counts, "
            "findings: [{level, code, file, section, key, message, "
            "suggestion}]} as JSON."
        ),
    )
    return parser


def _run_path(args: argparse.Namespace) -> None:
    report = path_report()
    _emit(report, render_path(report), as_json=args.json)
    raise SystemExit(EXIT_OK if report["error"] is None else EXIT_ERROR)


def _run_show(args: argparse.Namespace) -> None:
    try:
        report = show_report()
    except (ValueError, OSError) as exc:
        if args.json:
            _emit({"error": str(exc)}, "", as_json=True)
        else:
            sys.stderr.write(
                _terminal_safe(
                    f"bettermemory config show: the config does not load: {exc}\n"
                    "Run `bettermemory config check` for every problem in it.\n"
                )
            )
        raise SystemExit(EXIT_ERROR) from None
    _emit(report, render_show(report), as_json=args.json)
    raise SystemExit(EXIT_OK)


def _run_check(args: argparse.Namespace) -> None:
    report = check_report()
    _emit(report, render_check(report), as_json=args.json)
    raise SystemExit(report["exit_code"])


_RUNNERS: dict[str, Callable[[argparse.Namespace], None]] = {
    "path": _run_path,
    "show": _run_show,
    "check": _run_check,
}


def run(
    args: argparse.Namespace,
    *,
    sub_parser: argparse.ArgumentParser,
) -> None:
    """Dispatch handler for ``bettermemory config ...``."""
    if args.config_cmd is None:
        sub_parser.print_help()
        raise SystemExit(2)
    runner = _RUNNERS.get(args.config_cmd)
    if runner is None:
        sub_parser.error(f"unknown config subcommand: {args.config_cmd!r}")
    runner(args)


__all__ = [
    "ConfigFile",
    "Finding",
    "add_subparser",
    "check_report",
    "path_report",
    "raw_settings",
    "read_files",
    "render_check",
    "render_path",
    "render_show",
    "run",
    "settings_catalogue",
    "show_report",
]
