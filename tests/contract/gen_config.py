"""Fixture 9: the settings surface (``settings.json`` ungated; ``settings_python.json``,
``settings_paths.json`` and ``settings_paths_python.json`` gated, see
``generate_python`` and the sets above ``generate``).

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
- ``config_paths``: ``config path --json`` under other environments (the
  store variable empty, relative, under ``~``, with ``..``; a project
  store; ``cwd_store`` off; a managed directory; a config that does not
  load; lapsed managed pins; the spellings ``str(Path(...))`` rewrites;
  relative directories; a NUL in ``[storage] directory``).
- ``cases``: a config.toml (and a managed file) → ``config check --json``
  and ``config show --json`` as the CLI prints them (the text itself and
  the exit code, or the exception that escaped the command), and the
  config findings of ``security audit`` (`posture._settings`). The loader's
  one-shot notices are cleared before each of the three, so each answers
  as a CLI process of its own does.

The native port (unit N5a) added ``text``, ``config_paths`` and every case
after ``managed_pins_fixed_key``: managed pins the user also sets, keys in
the wrong section or outside any, TOML errors in either file, bytes that
are not UTF-8, a byte-order mark, CRLF and a lone CR, empty files,
duplicate keys and sections, dates and times where values belong, NaN,
infinite, float and huge ports, an integer past ``int``'s 4300 digits, deep
nesting, removed keys and sections, scalar sections, the managed file's
non-table lines, fixed settings in every spelling, and the boolean,
switch, scope and acknowledge spellings. The answers recorded before it
are unchanged.

Nothing here ranks, tokenizes or reads the Unicode database, so the file
is the same under every Python, SQLite and platform the suite runs on.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import posixpath
import sys
from collections.abc import Iterator
from dataclasses import fields
from pathlib import Path, PurePosixPath, PureWindowsPath
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
def _files(
    w: World, user: str | bytes | None, managed_text: str | bytes | None
) -> Iterator[None]:
    user_path = w.root / "user" / "config.toml"
    managed_path = w.root / "managed" / "managed.toml"
    for path, text in ((user_path, user), (managed_path, managed_text)):
        path.parent.mkdir(exist_ok=True)
        path.unlink(missing_ok=True)
        if text is not None:
            path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
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


def _run(scrub: Scrubber, argv: list[str]) -> dict[str, Any]:
    """`_cli`, recorded: the exit code, the JSON printed and the text
    itself (scrubbed), or the exception that escaped the command (the CLI
    prints its traceback and exits 1)."""
    try:
        code, out, _ = _cli(argv)
    except Exception as exc:  # noqa: BLE001 - what escapes is the answer
        return {"raised": type(exc).__name__, "message": scrub(str(exc))}
    json.loads(out)  # the command printed one JSON document (bare NaN/Infinity allowed)
    return {"exit_code": code, "text": scrub(out)}


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


def _source_field(name: str, text: str | bytes | None) -> dict[str, Any]:
    """A file's text under `name`, or its bytes in hex under `name_hex`
    when they are not UTF-8."""
    if isinstance(text, bytes):
        return {f"{name}_hex": text.hex()}
    return {name: text}


def _case(
    w: World,
    scrub: Scrubber,
    name: str,
    user: str | bytes | None,
    managed_text: str | bytes | None,
) -> dict[str, Any]:
    # Each command as its own CLI process prints it: the loader's one-shot
    # notices (`_DEPRECATED_KEY_WARNED_PATHS`) are cleared before each of
    # check, show and the audit, so a notice check's load logged is logged
    # again under show.
    with _files(w, user, managed_text), _warnings():
        check = _run(scrub, ["config", "check", "--json"])
        bconfig._DEPRECATED_KEY_WARNED_PATHS.clear()
        show = _run(scrub, ["config", "show", "--json"])
        bconfig._DEPRECATED_KEY_WARNED_PATHS.clear()
        try:
            cfg = sandbox_settings.load_config_read_only()
            audit: Any = [f.to_json() for f in posture._settings(cfg)]
        except (ValueError, OSError) as exc:
            audit = {"does_not_load": str(exc)}
        except Exception as exc:  # noqa: BLE001 - what escapes is the answer
            audit = {"raised": type(exc).__name__, "message": str(exc)}
    # The inputs are recorded as written: the scrubber rewrites backslashes,
    # so it sees only what the commands printed.
    return {
        "case": name,
        **_source_field("config_toml", user),
        **_source_field("managed_toml", managed_text),
        **scrub(
            {
                "check": check,
                "show": show,
                "audit_config_findings": audit,
            }
        ),
    }


_CASES: list[tuple[str, str | bytes | None, str | bytes | None]] = [
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
    # Added with the native port (unit N5a): every case below is new.
    (
        "managed_pins_what_the_user_sets",
        "[policy]\nforbid_force = 1\n[daemon]\nport = 7397.0\n[security]\n"
        'strict_provenance = "true"\n[behavior]\nprompt_recall = true\n',
        "[policy]\nforbid_force = true\n[daemon]\nport = 7400\n[security]\n"
        'strict_provenance = true\n[behavior]\nprompt_recall = "off"\n',
    ),
    (
        "managed_pin_the_user_restates",
        "[telemetry]\nenabled = false\n[scopes]\nallowed = []\n",
        '[telemetry]\nenabled = false\n[scopes]\nallowed = ["tools"]\n',
    ),
    ("key_in_the_wrong_section", "[behavior]\nport = 7400\nenabeld = 1\n", None),
    ("key_outside_any_section", "enabled = false\nzzz = 1\n[storage]\n", None),
    # Names and values that print with a backslash escape are kept out:
    # the escape, escaped again in `text`, reads as a host path to
    # test_no_golden_file_carries_a_host_fact.
    (
        "quoted_unknown_names",
        '["sec tion"]\n[telemetry]\n"en abled" = 1\n"a.b" = 2\n',
        None,
    ),
    ("syntax_error_user", "[behavior\nsession_brief = false\n", None),
    ("syntax_error_managed", None, "[telemetry]\nenabled = \n"),
    ("syntax_error_both", "a = 1\nb = [1,\n", "x = 'unterminated\n"),
    ("not_utf8_user", b"[telemetry]\nenabled = \xff\n", None),
    ("not_utf8_managed", None, b"\xef\xbb\xbf[telemetry]\r\nenabled = \xe2\x82\n"),
    ("bom", "\ufeff[telemetry]\nenabled = false\n", "\ufeff[daemon]\nport = 7400\n"),
    (
        "crlf",
        '[telemetry]\r\nenabled = false\r\n[scopes]\r\nallowed = [\r\n  "tools",\r\n]\r\n',
        None,
    ),
    ("lone_cr", "[telemetry]\renabled = false\n", None),
    ("empty_files", "", ""),
    ("duplicate_key", "[telemetry]\nenabled = true\nenabled = false\n", None),
    ("duplicate_section", "[telemetry]\nenabled = true\n[telemetry]\n", None),
    (
        "dates_where_values_belong",
        "[daemon]\nport = 1979-05-27\n",
        None,
    ),
    (
        "date_values_fall_back",
        "[telemetry]\nenabled = 1979-05-27T07:32:00Z\n[membership]\n"
        "strict = 07:32:00.5\n[security]\nstrict_provenance = 1979-05-27T07:32:00-07:00\n"
        "[behavior]\nprompt_recall = 1979-05-27\n",
        None,
    ),
    ("time_as_a_path", "[storage]\ndirectory = 07:32:00\n", None),
    ("nan_port", "[daemon]\nport = nan\n", None),
    ("inf_port", "[daemon]\nport = inf\n", None),
    ("float_port", "[daemon]\nport = 7400.5\n[capture]\ndaily_cap = -1e3\n", None),
    ("big_port", "[daemon]\nport = 0x" + "f" * 40 + "\n", None),
    ("int_past_the_digit_limit", "[daemon]\nport = " + "1" * 4301 + "\n", None),
    ("int_past_the_digit_limit_managed", None, "[daemon]\nport = " + "1" * 4301 + "\n"),
    ("deep_nesting", "a = " + "[" * 600 + "]" * 600 + "\n", None),
    ("privileged_and_zero_port", "[daemon]\nport = 80\n", "[capture]\ndaily_cap = 0\n"),
    ("port_zero", "[daemon]\nport = 0\n", None),
    (
        "string_numbers",
        '[daemon]\nport = " 7400 "\n[capture]\ndaily_cap = "1_0"\n',
        None,
    ),
    ("unicode_digits", '[daemon]\nport = "\uff17\uff14\uff10\uff10"\n', None),
    (
        "bad_scopes",
        '[scopes]\nallowed = ["Projects:Foo", "ok", "a b", "x-"]\n'
        'typo_exceptions = ["X", " y "]\n',
        None,
    ),
    ("scope_list_of_ints", "[scopes]\nallowed = [1, 2]\n", None),
    ("scope_bare_string", '[scopes]\nallowed = "tools"\n', None),
    (
        "acknowledge_names",
        '[policy]\nforbid_acknowledge = ["acknowledge_credential", "transiant", "zzz"]\n',
        None,
    ),
    (
        "acknowledge_known",
        '[policy]\nforbid_acknowledge = ["credential", "truncation"]\n'
        "forbid_force = true\n",
        None,
    ),
    (
        "switch_spellings",
        '[policy]\nforbid_force = " YES "\n[storage]\ncwd_store = "no"\n',
        None,
    ),
    ("switch_refused", '[storage]\ncwd_store = "nope"\n', None),
    ("directory_set", '[storage]\ndirectory = "~/store"\n', None),
    (
        "removed_keys_and_sections",
        "[behavior]\ncorroboration_boost = true\nrescue_expansion = 1\n[telemetry]\n"
        "max_bytes = 10\n[capture]\nenabled = true\n[consolidate]\nx = 1\ny = 2\n"
        "[proposals]\n",
        None,
    ),
    ("removed_section_as_scalar", "consolidate = 1\nproposals = [1]\n", None),
    (
        "removed_keys_managed",
        None,
        "[behavior]\nstanding_tier = true\n[consolidate]\nx = 1\n",
    ),
    ("scalar_sections_tolerated", "daemon = 5\ncapture = []\nmembership = {}\n", None),
    ("scalar_section_refused", "security = true\n", None),
    ("scalar_section_inline_table", "telemetry = {enabled = false}\n", None),
    ("managed_scalar", None, "x = 1\n[telemetry]\nenabled = false\n"),
    ("managed_scalar_date", None, "when = 1979-05-27T07:32:00+01:30\n"),
    ("managed_scalar_list", None, "arr = [1, 'a', 2.5]\n"),
    ("managed_array_of_tables", None, "[[telemetry]]\nenabled = false\n"),
    (
        "fixed_keys_every_spelling",
        '[behavior]\nsearch_mode = " BM25 "\nconversational = "no"\n'
        'recency_boost_half_life_days = "30"\ncold_endorsement_ratio_threshold = 0\n'
        "verification_stale_days = 30.0\nmax_scopes_per_write = true\n"
        'heavily_used_min_applied = "3"\n',
        None,
    ),
    (
        "managed_fixed_limits",
        None,
        "[behavior]\nmax_takeaway_bytes = 100\nmax_scopes_per_write = 0\n"
        'min_content_tokens = 5\nmax_content_bytes = "1000000"\nsearch_mode = "bm25"\n',
    ),
    ("prompt_recall_spellings", '[behavior]\nprompt_recall = " Everywhere "\n', None),
    ("prompt_recall_legacy_false", "[behavior]\nprompt_recall = false\n", None),
    ("prompt_recall_number", "[behavior]\nprompt_recall = 1\n", None),
    ("checkpoint_tokens_float", "[capture]\ncheckpoint_tokens = 499.9\n", None),
    ("checkpoint_tokens_negative", "[capture]\ncheckpoint_tokens = -5\n", None),
    (
        "bool_spellings",
        '[telemetry]\nenabled = "Off"\n[membership]\nstrict = "1"\n',
        None,
    ),
    ("bool_as_int", "[telemetry]\nenabled = 0\n[capture]\nstop_checkpoint = 1\n", None),
    ("int_as_bool", "[capture]\ndaily_cap = true\n", None),
    ("inline_table_value", "[daemon]\nport = {a = 1}\n", None),
    (
        "non_ascii",
        '[scopes]\nallowed = ["café", "日本"]\n[storage]\ndirectory = "~/é"\n',
        None,
    ),
    (
        "control_in_string",
        '[storage]\ndirectory = "a\\u0007b"\n[scopes]\nallowed = ["[2J"]\n',
        None,
    ),
    # A NUL in the one path-valued key: `Path.resolve()` raises ValueError
    # ("lstat: embedded null character in path"). A location variable cannot
    # hold one: `os.environ` refuses it, as execve would.
    ("nul_in_directory", '[storage]\ndirectory = "\\u0000"\n', None),
    ("nul_in_directory_managed", None, '[storage]\ndirectory = "a\\u0000b"\n'),
    ("nul_after_tilde", '[storage]\ndirectory = "~/\\u0000"\n', None),
    # `config show --json` prints a non-finite float bare (not JSON).
    (
        "non_finite_floats",
        "[behavior]\nwrite_supersession = inf\n[telemetry]\nenabled = -inf\n"
        "[membership]\nstrict = nan\n",
        None,
    ),
    # `~name` for a user the system does not know: `Path.expanduser()`
    # raises RuntimeError, which check's `_system_dir_findings` catches.
    ("tilde_user_directory", '[storage]\ndirectory = "~nosuchuser9/y"\n', None),
    ("tilde_user_directory_managed", None, '[storage]\ndirectory = "~nosuchuser9"\n'),
    (
        "everything_at_once",
        '[behaviour]\n[telemetry]\nenabled = "ture"\n[daemon]\nport = 80\n'
        "[capture]\ncheckpoint_tokens = 1\n",
        "[telemetry]\nenabled = true\n[security]\nstrict_provenance = true\n",
    ),
]


#: `config path --json` under other environments: (name, the location
#: variables changed from the world's, config.toml, the managed file,
#: directories made under <scratch>). `<scratch>` in a value or a file
#: stands for the run's directory; files name it in literal strings, which
#: hold a Windows path as written.
_PATH_CASES: list[tuple[str, dict[str, str], str | None, str | None, list[str]]] = [
    ("dir_variable_empty", {"BETTERMEMORY_DIR": ""}, None, None, []),
    ("dir_variable_relative", {"BETTERMEMORY_DIR": "relative/dir"}, None, None, []),
    ("dir_variable_tilde", {"BETTERMEMORY_DIR": "~/x"}, None, None, []),
    (
        "dir_variable_dotted",
        {"BETTERMEMORY_DIR": "<scratch>/mem/../keys"},
        None,
        None,
        [],
    ),
    (
        "directory_key",
        {"BETTERMEMORY_DIR": ""},
        "[storage]\ndirectory = '~/store'\n",
        None,
        [],
    ),
    ("project_store", {"BETTERMEMORY_DIR": ""}, None, None, ["alpha/.claude-memory"]),
    (
        "project_store_off",
        {"BETTERMEMORY_DIR": ""},
        "[storage]\ncwd_store = false\n",
        None,
        ["alpha/.claude-memory"],
    ),
    (
        "project_store_off_managed",
        {"BETTERMEMORY_DIR": ""},
        None,
        "[storage]\ncwd_store = false\n",
        ["alpha/.claude-memory"],
    ),
    (
        "directory_managed",
        {"BETTERMEMORY_DIR": ""},
        "[storage]\ndirectory = '~/mine'\n",
        "[storage]\ndirectory = '<scratch>/pinned'\n",
        ["pinned"],
    ),
    ("does_not_load", {"BETTERMEMORY_DIR": ""}, "[daemon]\nport = 'x'\n", None, []),
    ("does_not_load_dir_set", {}, "[daemon]\nport = 'x'\n", None, []),
    # Spellings `str(Path(...))` rewrites: `.` segments, repeated and
    # trailing separators go, `..` stays.
    *[
        (f"{name}_{label}", {variable: value}, None, None, [])
        for name, variable in (
            ("dir_variable", "BETTERMEMORY_DIR"),
            ("keys_variable", "BETTERMEMORY_KEYS_DIR"),
            ("state_variable", "BETTERMEMORY_STATE_DIR"),
        )
        for label, value in (
            ("dot_segments", "<scratch>/a/./b/"),
            ("dot_relative", "./x"),
            ("double_slash", "<scratch>//double"),
            ("dot_dot_kept", "<scratch>/a/../keys"),
            ("parent", ".."),
            ("here", "."),
        )
    ],
    *[
        (f"directory_key_{label}", {"BETTERMEMORY_DIR": ""}, text, None, [])
        for label, text in (
            ("dot_segments", "[storage]\ndirectory = '<scratch>/a/./b/'\n"),
            ("dot_relative", "[storage]\ndirectory = './x'\n"),
            ("double_slash", "[storage]\ndirectory = '<scratch>//double'\n"),
            ("parent", "[storage]\ndirectory = '..'\n"),
            ("relative", "[storage]\ndirectory = 'sub'\n"),
        )
    ],
    (
        "keys_variable_relative_made",
        {"BETTERMEMORY_KEYS_DIR": "sub"},
        None,
        None,
        ["alpha/sub"],
    ),
    (
        "dir_variable_relative_made",
        {"BETTERMEMORY_DIR": "sub"},
        None,
        None,
        ["alpha/sub"],
    ),
    # `~name` for no such user, and the file system's root (`/..` is the
    # root too). A case that resolves above <scratch> (`../..` from
    # <scratch>/alpha) is left out: the reference prints the host's own
    # directory there, which `_path_case` refuses. The keys and state
    # variables are not pointed at the root: the suite's guard refuses a
    # keys or state directory outside the test's scratch directory.
    *[
        (f"{name}_{label}", {variable: value}, None, None, [])
        for name, variable in (
            ("dir_variable", "BETTERMEMORY_DIR"),
            ("keys_variable", "BETTERMEMORY_KEYS_DIR"),
            ("state_variable", "BETTERMEMORY_STATE_DIR"),
        )
        for label, value in (
            ("tilde_user", "~nosuchuser9"),
            ("tilde_user_path", "~nosuchuser9/y"),
            ("root", "/"),
            ("root_two_slashes", "//"),
            ("root_three_slashes", "///"),
            ("root_parent", "/.."),
        )
        if name == "dir_variable" or not label.startswith("root")
    ],
    *[
        (f"directory_{where}_{label}", {"BETTERMEMORY_DIR": ""}, *files, [])
        for label, line in (
            ("tilde_user", "[storage]\ndirectory = '~nosuchuser9'\n"),
            ("tilde_user_path", "[storage]\ndirectory = '~nosuchuser9/y'\n"),
            ("root", "[storage]\ndirectory = '/'\n"),
            ("root_two_slashes", "[storage]\ndirectory = '//'\n"),
            ("root_parent", "[storage]\ndirectory = '/..'\n"),
        )
        for where, files in (("key", (line, None)), ("managed", (None, line)))
    ],
    (
        "directory_key_nul",
        {"BETTERMEMORY_DIR": ""},
        '[storage]\ndirectory = "\\u0000"\n',
        None,
        [],
    ),
    (
        "directory_managed_nul",
        {"BETTERMEMORY_DIR": ""},
        None,
        '[storage]\ndirectory = "x\\u0000"\n',
        [],
    ),
    (
        "managed_lapsed_pins",
        {},
        None,
        "[behavior]\nsearch_mode = 'bm25'\nmax_content_bytes = 5\n[telemetry]\n"
        "enabled = true\n",
        [],
    ),
]


def _within_scratch(name: str, result: dict[str, Any]) -> None:
    """Refuse a case whose output names a place outside the run's directory
    other than the file system's root: an absolute path must be under
    <scratch> or spell the root, and a relative one must stay under
    <scratch> read from <scratch>/alpha. What lies above <scratch> is the
    host's, never a fixture's."""
    if "text" not in result:
        return
    doc = json.loads(result["text"])
    for entry in doc.values():
        path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(path, str):
            continue
        if path.startswith("/"):
            ok = posixpath.normpath(path) in ("/", "//")
        else:
            ok = posixpath.normpath(f"<scratch>/alpha/{path}").startswith("<scratch>")
        if not ok:
            raise AssertionError(f"{name}: {path!r} is outside <scratch>")


def _pure_paths() -> list[list[str]]:
    """Texts spelled by `PurePosixPath` and `PureWindowsPath`: every text of
    up to two segments after zero to three slashes, and 400 longer ones from
    a fixed seed. Inputs use `/` only (both flavours read it as a
    separator); the Windows spelling is written with `/` for its `\\`,
    which it never holds otherwise."""
    import random

    segments = ["", ".", "..", "a", "C:", "c:x", "?", "UNC", "lpt9", "~"]
    texts: list[str] = []
    for lead in range(4):
        texts.append("/" * lead)
        for one in segments:
            texts.append("/" * lead + one)
            for two in segments:
                texts.append("/" * lead + one + "/" + two)
    rng = random.Random(9)
    for _ in range(400):
        parts = [rng.choice(segments) for _ in range(rng.randint(3, 5))]
        texts.append("/" * rng.randint(0, 4) + "/".join(parts))
    seen: dict[str, None] = dict.fromkeys(texts)
    return [
        [t, str(PurePosixPath(t)), str(PureWindowsPath(t)).replace("\\", "/")]
        for t in seen
    ]


def _path_case(
    w: World,
    scrub: Scrubber,
    name: str,
    env: dict[str, str],
    user: str | None,
    managed_text: str | None,
    made: list[str],
) -> dict[str, Any]:
    root = str(w.root)

    def here(text: str | None) -> str | None:
        return None if text is None else text.replace("<scratch>", root)

    saved = {k: os.environ.get(k) for k in env}
    made_paths = [w.root / rel for rel in made]
    try:
        for key, value in env.items():
            os.environ[key] = value.replace("<scratch>", root)
        for path in made_paths:
            path.mkdir(parents=True, exist_ok=True)
        with _files(w, here(user), here(managed_text)), _warnings():
            result = _run(scrub, ["config", "path", "--json"])
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        for path in reversed(made_paths):
            path.rmdir()
    _within_scratch(name, result)
    return {
        "case": name,
        "env": env,
        "config_toml": user,
        "managed_toml": managed_text,
        "mkdir": made,
        **result,
    }


#: Where each case of `_CASES` and `_PATH_CASES` is recorded, by how far
#: its bytes can be trusted to hold (anything not named is in
#: ``settings.json``, ungated).
#:
#: ``settings_python.json`` (Python-gated): the text holds the
#: interpreter's own wording (a `tomllib` or codec message, `int()`'s
#: digit limit, a float conversion's error, `repr`, a RecursionError, bare
#: NaN and Infinity from `json.dumps`).
_PYTHON_WORDED = frozenset(
    {
        "syntax_error_user",
        "syntax_error_managed",
        "syntax_error_both",
        "not_utf8_user",
        "not_utf8_managed",
        "lone_cr",
        "duplicate_key",
        "duplicate_section",
        "nan_port",
        "inf_port",
        "int_past_the_digit_limit",
        "int_past_the_digit_limit_managed",
        "deep_nesting",
        "managed_scalar_date",
        "managed_scalar_list",
        "managed_array_of_tables",
        "scalar_sections_tolerated",
        "fixed_keys_every_spelling",
        "managed_fixed_limits",
        "non_finite_floats",
    }
)
#: ``settings_paths.json`` (POSIX only): `[storage] directory` is expanded
#: and resolved through the OS's path rules. Every `_PATH_CASES` case is
#: too.
_PATH_WORDED = frozenset(
    {
        "directory_set",
        "non_ascii",
        "control_in_string",
        "tilde_user_directory",
        "tilde_user_directory_managed",
    }
)
#: ``settings_paths_python.json`` (POSIX only and Python-gated): a NUL in a
#: path, whose ValueError the release words (3.11 says "embedded null
#: byte").
_PATH_AND_PYTHON = frozenset(
    {
        "nul_in_directory",
        "nul_in_directory_managed",
        "nul_after_tilde",
        "directory_key_nul",
        "directory_managed_nul",
    }
)

_READ_CASES = (
    "cases: config_toml / managed_toml (null: no file; *_hex: the bytes, "
    "when they are not UTF-8) -> check and show, each the exit code and the "
    "text the CLI printed (one JSON document, where a non-finite float "
    "prints bare as NaN or Infinity), or raised: the exception that escaped "
    "the command; each command runs with the loader's one-shot notices "
    "cleared, as a CLI process of its own would; and audit_config_findings, "
    "the config findings of `security audit` (posture._settings over the "
    "config the audit loads). Check exits 0 (nothing above a note), 1 (an "
    "error), 2 (warnings only)."
)
_READ_PATHS = (
    "config_paths: `config path --json` under other environments (env: the "
    "location variables changed, <scratch> the run's directory; mkdir: "
    "directories made under it; text: what the CLI printed; a case whose "
    "output names a place above <scratch> is refused, so none is recorded)."
)


def _cases(
    names: frozenset[str] | None,
) -> list[tuple[str, str | bytes | None, str | bytes | None]]:
    """The `_CASES` in ``names``, or (None) those in no other file."""
    other = _PYTHON_WORDED | _PATH_WORDED | _PATH_AND_PYTHON
    return [
        c for c in _CASES if (c[0] in names if names is not None else c[0] not in other)
    ]


def _part(
    fixture: str, read: str, cases: Any, path_cases: Any, extra: dict[str, Any]
) -> bytes:
    with world() as w:
        scrub = Scrubber(w.root, now=w.now)
        doc: dict[str, Any] = {
            "fixture": fixture,
            "read": read,
            "setup": _SETUP,
            "placeholders": {"<scratch>": PLACEHOLDERS["<scratch>"]},
            **extra,
        }
        if path_cases is not None:
            doc["config_paths"] = [_path_case(w, scrub, *c) for c in path_cases]
        doc["cases"] = [_case(w, scrub, *c) for c in cases]
    return json_doc(doc)


def generate() -> dict[str, bytes]:
    """``settings.json``, ungated: what reads the same under every Python
    the suite runs and on every platform (the loader's own words; paths
    only under the run's directory)."""
    with world() as w:
        scrub = Scrubber(w.root, now=w.now)
        catalogue = _catalogue(w)
        cases = [_case(w, scrub, *case) for case in _cases(None)]
        coercion = _coercion(w)
        with _files(w, None, None), _warnings():
            path_code, path_out, _ = _cli(["config", "path", "--json"])
        json.loads(path_out)
        path_report = scrub({"exit_code": path_code, "text": path_out})
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
            "nothing was logged, e.g. a float truncated to an integer). "
            "on_bad_value: refuse when a table stops the load, default when it "
            "reads as the default. environment: the variables read; "
            "config_path: `config path --json` with the location variables "
            "set. " + _READ_CASES + " The cases whose text is the "
            "interpreter's are in settings_python.json; those that expand or "
            "resolve a path are in settings_paths.json and "
            "settings_paths_python.json."
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


def generate_python() -> dict[str, bytes]:
    """``settings_python.json``, Python-gated: the cases worded by the
    interpreter, and `pure_paths` (pathlib's parsing differs by release:
    3.11 spells 307 of the 800 texts otherwise)."""
    read = (
        _READ_CASES + " pure_paths: [text, str(PurePosixPath(text)), "
        "str(PureWindowsPath(text)) with each backslash written as /]."
    )
    data = _part(
        "settings_python",
        read,
        _cases(_PYTHON_WORDED),
        None,
        {"pure_paths": _pure_paths()},
    )
    return {"settings_python.json": data}


def generate_paths() -> dict[str, bytes]:
    """``settings_paths.json``, POSIX only: `config path` under other
    environments and the cases whose directory is expanded and resolved."""
    path_cases = [c for c in _PATH_CASES if c[0] not in _PATH_AND_PYTHON]
    data = _part(
        "settings_paths",
        _READ_PATHS + " " + _READ_CASES,
        _cases(_PATH_WORDED),
        path_cases,
        {},
    )
    return {"settings_paths.json": data}


def generate_paths_python() -> dict[str, bytes]:
    """``settings_paths_python.json``, POSIX only and Python-gated: a NUL in
    a path."""
    path_cases = [c for c in _PATH_CASES if c[0] in _PATH_AND_PYTHON]
    data = _part(
        "settings_paths_python",
        _READ_PATHS + " " + _READ_CASES,
        _cases(_PATH_AND_PYTHON),
        path_cases,
        {},
    )
    return {"settings_paths_python.json": data}
