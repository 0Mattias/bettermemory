"""Fixture 9: the settings surface (``settings.json``, ungated).

What a config file may say and what the product makes of it:

- ``catalogue``: every key the loader reads, from the product's own
  catalogue (`config_cmd.settings_catalogue`), each with its section, its
  type (the dataclass field's annotation), its default (`config.Config()`)
  and whether the organisation's managed file pins it. The list is
  cross-checked against the loader: each key is set alone to a value other
  than its default and read back through `config._config_from_data`, and
  pinned alone through a managed file and read back through
  `config.load_config`; a key the loader does not read, or a read key the
  catalogue lacks, stops the generator.
- ``fixed``: the keys that are fixed values and no longer settings
  (`config.FIXED_SETTINGS`), with each value.
- ``coercion``: each kept key read alone from each of a fixed set of TOML
  values, through `config._config_from_data`: the value it loads as, or
  the refusal (the ValueError's text), and whether the loader logged a
  warning. ``on_bad_value`` sums it up: ``refuse`` when a value of the
  wrong shape (a table) stops the load, ``default`` when it reads as the
  default.
- ``environment``: the variables the product reads, each with what it sets,
  and ``config path --json`` with the three location variables set.
- ``cases``: a config.toml (and a managed file) → ``config check --json``
  and ``config show --json`` as the CLI prints them, with their exit codes,
  and the config findings of ``security audit`` (`posture._settings`).

Nothing here ranks, tokenizes or reads the Unicode database, so the file
is the same under every Python, SQLite and platform the suite runs on.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import sys
from collections.abc import Iterator
from dataclasses import fields
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bettermemory import _daemon_client, identity, managed, posture, sandbox_settings
from bettermemory import claude_paths
from bettermemory import config as bconfig
from bettermemory.cli import config_cmd

from ._common import json_doc
from ._sandbox import PLACEHOLDERS, Scrubber, World, world

#: The name every coercion probe locates its errors with.
_PROBE_FILE = Path("config.toml")

#: TOML type names for the dataclass fields' annotations.
_TYPES = {
    "bool": "boolean",
    "int": "integer",
    "str": "string",
    "str | None": "string, or absent",
    "list[str]": "array of strings",
}

#: A value other than the default for each kept key, to prove the loader
#: reads it (and a managed file pins it). A key the catalogue gains stops
#: the generator until it is added here.
_OTHER: dict[tuple[str, str], Any] = {
    ("storage", "directory"): "/srv/bettermemory",
    ("storage", "cwd_store"): False,
    ("behavior", "write_supersession"): False,
    ("behavior", "prompt_recall"): "everywhere",
    ("behavior", "session_brief"): False,
    ("behavior", "tombstone_retention_days"): 30,
    ("scopes", "allowed"): ["tools"],
    ("scopes", "typo_exceptions"): ["tools"],
    ("policy", "forbid_force"): True,
    ("policy", "forbid_acknowledge"): ["credential"],
    ("telemetry", "enabled"): False,
    ("daemon", "port"): 7400,
    ("capture", "stop_checkpoint"): True,
    ("capture", "checkpoint_tokens"): 6000,
    ("capture", "daily_cap"): 3,
    ("membership", "write_token"): False,
    ("membership", "strict"): True,
    ("security", "strict_provenance"): True,
}

#: The TOML values every kept key is read from, in order.
_PROBES: list[Any] = [
    True,
    False,
    "on",
    " Off ",
    "ture",
    "",
    0,
    1,
    499,
    2.5,
    "42",
    "x",
    "Everywhere",
    [],
    ["credential"],
    ["acknowledge_credential"],
    [1],
    {},
]

_SETUP = (
    "Every location variable points into <scratch> (_sandbox.world): "
    "BETTERMEMORY_DIR=<scratch>/mem, BETTERMEMORY_KEYS_DIR=<scratch>/keys, "
    "BETTERMEMORY_STATE_DIR=<scratch>/state, HOME=<scratch>/home. The user's "
    "config file is <scratch>/user/config.toml (config.default_config_path "
    "answers it) and the managed file <scratch>/managed/managed.toml "
    "(managed.managed_config_path answers it); a case that has no text for "
    "one has no such file. The CLI runs in process (cli.main with argv set)."
)


def _field_type(section: str, key: str) -> str:
    section_cls = type(getattr(bconfig.Config(), section))
    for f in fields(section_cls):
        if f.name == key:
            spelled = f.type if isinstance(f.type, str) else f.type.__name__
            return _TYPES[spelled]
    raise KeyError((section, key))


@contextlib.contextmanager
def _warnings() -> Iterator[list[str]]:
    """The config logger's warnings inside the block, held."""
    logger = logging.getLogger("bettermemory.config")
    held: list[str] = []

    class _Hold(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            held.append(record.getMessage())

    handler = _Hold(logging.WARNING)
    propagate, level = logger.propagate, logger.level
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.WARNING)
    saved = set(bconfig._DEPRECATED_KEY_WARNED_PATHS)
    bconfig._DEPRECATED_KEY_WARNED_PATHS.clear()
    try:
        yield held
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        logger.setLevel(level)
        bconfig._DEPRECATED_KEY_WARNED_PATHS.clear()
        bconfig._DEPRECATED_KEY_WARNED_PATHS.update(saved)


def _read_alone(section: str, key: str, value: Any) -> dict[str, Any]:
    with _warnings() as held:
        try:
            cfg = bconfig._config_from_data({section: {key: value}}, _PROBE_FILE)
        except ValueError as exc:
            return {"refused": str(exc), "warned": bool(held)}
    return {"value": getattr(getattr(cfg, section), key), "warned": bool(held)}


@contextlib.contextmanager
def _files(w: World, user: str | None, managed_text: str | None) -> Iterator[None]:
    user_path = w.root / "user" / "config.toml"
    managed_path = w.root / "managed" / "managed.toml"
    for path, text in ((user_path, user), (managed_path, managed_text)):
        path.parent.mkdir(exist_ok=True)
        path.unlink(missing_ok=True)
        if text is not None:
            path.write_bytes(text.encode("utf-8"))
    with (
        patch.object(bconfig, "default_config_path", lambda: user_path),
        patch.object(managed, "managed_config_path", lambda: managed_path),
    ):
        yield


def _cli(argv: list[str]) -> tuple[int, str, str]:
    """`bettermemory <argv>` in process: exit code, stdout, stderr."""
    from bettermemory.cli import main

    out, err = io.StringIO(), io.StringIO()
    code = 0
    with (
        patch.object(sys, "argv", ["bettermemory", *argv]),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ):
        try:
            main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def _catalogue(w: World) -> list[dict[str, Any]]:
    catalogue = config_cmd.settings_catalogue()
    listed = {(s, k) for s, keys in catalogue.items() for k in keys}
    if listed != set(_OTHER):
        raise AssertionError(f"catalogue drift: {sorted(listed ^ set(_OTHER))}")
    defaults = bconfig.Config()
    out = []
    for section, keys in catalogue.items():
        for key in keys:
            default = getattr(getattr(defaults, section), key)
            other = _OTHER[(section, key)]
            read = _read_alone(section, key, other)
            if read.get("value") != other:
                raise AssertionError(f"the loader does not read {section}.{key}")
            pin = f"[{section}]\n{key} = {bconfig.toml_spelling(other)}\n"
            with _files(w, None, pin), _warnings():
                cfg = bconfig.load_config(create=False)
            pinned = f"{section}.{key}" in cfg.managed_keys and (
                getattr(getattr(cfg, section), key) == other
            )
            out.append(
                {
                    "section": section,
                    "key": key,
                    "type": _field_type(section, key),
                    "default": default,
                    "managed_may_pin": pinned,
                }
            )
    # Every field of the sections that is neither listed nor fixed is a
    # setting the catalogue misses.
    for section in catalogue:
        for f in fields(type(getattr(defaults, section))):
            pair = (section, f.name)
            if pair not in listed and pair not in bconfig.FIXED_SETTINGS:
                raise AssertionError(f"field outside the catalogue: {pair}")
    return out


def _fixed() -> list[dict[str, Any]]:
    out = []
    for (section, key), value in bconfig.FIXED_SETTINGS.items():
        if getattr(getattr(bconfig.Config(), section), key) != value:
            raise AssertionError(f"fixed value is not the default: {section}.{key}")
        out.append(
            {
                "section": section,
                "key": key,
                "value": value,
                "limit": bconfig.FIXED_LIMITS.get((section, key)),
            }
        )
    return out


def _checked(w: World, section: str, key: str, probe: Any) -> list[list[str]]:
    """What `config check` reports for a user file holding only the line
    `key = probe` under `[section]`: [level, code] of each finding."""
    text = f"[{section}]\n{key} = {bconfig.toml_spelling(probe)}\n"
    with _files(w, text, None), _warnings():
        report = config_cmd.check_report()
    return [[f["level"], f["code"]] for f in report["findings"]]


def _coercion(w: World) -> list[dict[str, Any]]:
    out = []
    for section, keys in config_cmd.settings_catalogue().items():
        for key in keys:
            results = [
                {
                    **_read_alone(section, key, probe),
                    "check": _checked(w, section, key, probe),
                }
                for probe in _PROBES
            ]
            out.append(
                {
                    "section": section,
                    "key": key,
                    "on_bad_value": "refuse" if "refused" in results[-1] else "default",
                    "reads": results,
                }
            )
    return out


#: The variables the product reads, each with what it sets. The names are
#: the product's own constants.
_ENVIRONMENT: list[tuple[str, str]] = [
    (
        bconfig.ENV_DIR_OVERRIDE,
        "the store directory (resolution rule 1, before [storage] directory, "
        "./.claude-memory and the user default); the store file is "
        "<dir>/store/memory.sqlite",
    ),
    (
        bconfig.KEYS_DIR_ENV,
        "the key ring's directory (else the user config directory's keys)",
    ),
    (
        _daemon_client.STATE_DIR_ENV,
        "the daemon state directory: daemon-<digest>.json, its locks and log "
        "(else the user state directory)",
    ),
    (identity.ENV_CLIENT, "the client name a stdio server records as the caller"),
    (identity.ENV_CLIENT_VERSION, "the client version recorded with it"),
    (identity.ENV_MODEL, "the model name recorded with it"),
    (identity.ENV_WORKSPACE, "the caller's workspace (before MCP roots and the cwd)"),
    (
        identity.ENV_HARNESS_SESSION,
        "Claude Code's session id: the hooks' session; removed from a daemon's "
        "environment",
    ),
    (
        _daemon_client.ATTENDED_ENV,
        'Claude Code\'s attended flag; "1" when a person is at the session '
        "(the capture checkpoint asks only then); removed from a daemon's environment",
    ),
    (
        claude_paths.CLAUDE_CONFIG_DIR_ENV,
        "Claude Code's config directory (else ~/.claude)",
    ),
    (
        claude_paths.CUSTOM_OAUTH_URL_ENV,
        "when set, Claude Code's per-user files carry the -custom-oauth suffix",
    ),
]


def _case(
    w: World, scrub: Scrubber, name: str, user: str | None, managed_text: str | None
) -> dict[str, Any]:
    with _files(w, user, managed_text), _warnings():
        check_code, check_out, _ = _cli(["config", "check", "--json"])
        show_code, show_out, _ = _cli(["config", "show", "--json"])
        try:
            cfg = sandbox_settings.load_config_read_only()
            audit: Any = [f.to_json() for f in posture._settings(cfg)]
        except (ValueError, OSError) as exc:
            audit = {"does_not_load": str(exc)}
    return scrub(
        {
            "case": name,
            "config_toml": user,
            "managed_toml": managed_text,
            "check": {"exit_code": check_code, "json": json.loads(check_out)},
            "show": {"exit_code": show_code, "json": json.loads(show_out)},
            "audit_config_findings": audit,
        }
    )


_CASES: list[tuple[str, str | None, str | None]] = [
    ("no_files", None, None),
    (
        "clean",
        '[behavior]\nprompt_recall = "everywhere"\n\n[capture]\nstop_checkpoint = true\n'
        '\n[scopes]\nallowed = ["tools", "projects:zephyrcore"]\n',
        None,
    ),
    ("unknown_key", "[behavior]\nsesion_brief = false\n\n[behaviour]\nx = 1\n", None),
    ("fixed_key_at_fixed_value", '[behavior]\nsearch_mode = "hybrid"\n', None),
    ("fixed_key_away_from_fixed_value", '[behavior]\nsearch_mode = "bm25"\n', None),
    ("bad_type_refused", '[daemon]\nport = "seventy"\n', None),
    ("bad_type_falls_back", '[behavior]\nsession_brief = "ture"\n', None),
    ("prompt_recall_falls_back", '[behavior]\nprompt_recall = "sometimes"\n', None),
    ("checkpoint_tokens_clamped", "[capture]\ncheckpoint_tokens = 10\n", None),
    ("port_out_of_range", "[daemon]\nport = 70000\n", None),
    ("bad_acknowledge_name", '[policy]\nforbid_acknowledge = ["credentials"]\n', None),
    (
        "managed_pins_kept_key",
        "[telemetry]\nenabled = true\n",
        "[telemetry]\nenabled = false\n",
    ),
    (
        "managed_pins_fixed_key",
        None,
        "[behavior]\nmax_content_bytes = 10\ndefault_max_results = 8\n",
    ),
]


def generate() -> dict[str, bytes]:
    with world() as w:
        scrub = Scrubber(w.root, now=w.now)
        catalogue = _catalogue(w)
        cases = [_case(w, scrub, *case) for case in _CASES]
        coercion = _coercion(w)
        with _files(w, None, None), _warnings():
            path_code, path_out, _ = _cli(["config", "path", "--json"])
        path_report = scrub({"exit_code": path_code, "json": json.loads(path_out)})
    doc = {
        "fixture": "settings",
        "read": (
            "catalogue: every key the config file accepts, in the loader's "
            "order (section, key, type, default, managed_may_pin: a managed "
            "file's line for it is in force over the user's). fixed: the keys "
            "that are fixed values and no settings (config.FIXED_SETTINGS); "
            "limit names the write limits whose managed line was stricter. "
            "coercion: each kept key read alone from each value of probes "
            "(TOML values, in order): reads[i] is {value, warned} or {refused: "
            "the error text, warned}, the error locating the line in "
            "config.toml; warned says the loader logged a warning; check is "
            "[level, code] of each finding `config check` reports for a "
            "config.toml holding that one line (a finding can stand where "
            "nothing was logged, e.g. a float truncated to an integer). on_bad_value: refuse when a table stops the load, "
            "default when it reads as the default. environment: the variables "
            "read; config_path: `config path --json` with the location "
            "variables set. cases: config_toml / managed_toml (null: no "
            "file) -> check and show, each the exit code and the JSON the CLI "
            "printed, and audit_config_findings, the config findings of "
            "`security audit` (posture._settings over the config the audit "
            "loads). Check exits 0 (nothing above a note), 1 (an error), 2 "
            "(warnings only)."
        ),
        "setup": _SETUP,
        "placeholders": {"<scratch>": PLACEHOLDERS["<scratch>"]},
        "catalogue": catalogue,
        "fixed": _fixed(),
        "probes": _PROBES,
        "coercion": coercion,
        "environment": [{"name": n, "sets": s} for n, s in _ENVIRONMENT],
        "config_path": path_report,
        "cases": cases,
    }
    return {"settings.json": json_doc(doc)}
