"""The organisation-managed config: settings the user's config.toml cannot loosen.

An organisation that deploys bettermemory needs some settings to hold
whatever the user's own file says: telemetry on (its `false` also drops
the audit trail), the escape hatches of the write gates taken away
(`[policy]`), the repository-supplied store refused (`[storage]
cwd_store = false`), the provenance of a foreign store read as
unaccounted. Before this module the user's file was the only source, so
anyone who could edit it, an unsandboxed agent shell included, could
flip every one of them.

The managed file has config.toml's own sections and keys. `load_config`
reads it after the user's file, and every key it sets is pinned: the
user's value for that key is ignored, with one logged notice per key
naming this file (`config._apply_managed`). The file is the
organisation's to write, through MDM, a configuration-management run or
an installer, and it is never created here.

Its location is fixed per OS and nothing in the environment moves it:

- macOS: ``/Library/Application Support/bettermemory/managed.toml``
- Linux and every other POSIX system: ``/etc/bettermemory/managed.toml``
- Windows: ``<ProgramData>\\bettermemory\\managed.toml``, the ProgramData
  folder read from the shell's known-folder API, not from the
  ``%ProgramData%`` variable a user can set.

No environment variable may relocate, disable or loosen it, which rules
out the obvious helpers: `platformdirs.site_config_dir` honours
``$XDG_CONFIG_DIRS`` on Linux, and that variable is the user's. The one
seam is `managed_config_path`, a module-level function the test suite's
autouse isolation (tests/conftest.py) points at a scratch path, so no
test reads the machine's real file. A child process a test spawns reads
the real location, which on a developer machine or a CI runner holds
nothing; giving the children a redirect would be exactly the variable
the rule forbids.

What the file is and is not: it binds the user's config, so an agent
that edits config.toml cannot loosen a pinned setting. It does not bind
a process running as the user. Such a process can export
``BETTERMEMORY_DIR``, build a `Config` in code, or open the store file
itself; the managed file is a policy, not a boundary against the account.

Stdlib only: the hook client loads the config when ``BETTERMEMORY_DIR``
is unset (`_daemon_client.resolved_store_path`), and that path imports
neither the SDK nor pydantic (tests/test_hook_client.py).
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Any

#: The managed file's name inside the per-OS directory.
MANAGED_FILENAME = "managed.toml"

_MACOS_DIR = Path("/Library/Application Support/bettermemory")
_POSIX_DIR = Path("/etc/bettermemory")
#: `CSIDL_COMMON_APPDATA`, the shell's id for the ProgramData folder.
_CSIDL_COMMON_APPDATA = 0x23
#: Where ProgramData lives on every Windows install that has not moved
#: it, used only when the shell call fails.
_WINDOWS_PROGRAM_DATA_FALLBACK = "C:\\ProgramData"


def _windows_program_data() -> Path:
    """The ProgramData folder, from the shell rather than the environment.

    ``%ProgramData%`` is an ordinary variable of the user's process, so
    reading it would let the user point the managed file at one of their
    own. `SHGetFolderPathW` answers from the system's known-folder
    registration instead (the call `platformdirs` makes for its
    ``CSIDL_COMMON_APPDATA``). When the call fails the folder's standard
    location is used: a fixed path, still not one the user chooses.
    """
    if sys.platform == "win32":
        import ctypes

        buf = ctypes.create_unicode_buffer(260)
        try:
            status = ctypes.windll.shell32.SHGetFolderPathW(
                None, _CSIDL_COMMON_APPDATA, None, 0, buf
            )
        except (AttributeError, OSError):
            status = -1
        if status == 0 and buf.value:
            return Path(buf.value)
    return Path(_WINDOWS_PROGRAM_DATA_FALLBACK)


def system_path(platform: str) -> Path:
    """The managed file's location on `platform` (a `sys.platform` value).

    A pure function of the platform string, so the tests check every
    branch on any host. `managed_config_path` is the seam the suite
    patches; this is what it returns in production.
    """
    if platform == "darwin":
        return _MACOS_DIR / MANAGED_FILENAME
    if platform == "win32":
        return _windows_program_data() / "bettermemory" / MANAGED_FILENAME
    return _POSIX_DIR / MANAGED_FILENAME


def managed_config_path() -> Path:
    """Where this machine's managed file lives. `load_config` reads it
    through this function, looked up on the module at call time, so the
    test suite's autouse fixture can point it at a scratch path."""
    return system_path(sys.platform)


def read_managed(path: Path) -> dict[str, dict[str, Any]] | None:
    """The managed file's sections, or None when there is no file.

    Fails closed. A file that exists but cannot be read (its directory
    or the file not readable by this account, a directory where the file
    should be), is not UTF-8, is not TOML, or holds a top-level value
    that is not a section table, raises a ValueError naming the file:
    the same outcome a malformed config.toml has (the daemon does not
    start, the CLI reports the error). Skipping it would silently drop
    every pin the organisation set, which is the one outcome a managed
    file exists to prevent. Only a missing file means "no managed
    config".

    Keys are not checked here. `config._apply_managed` parses the
    sections with the loader's own coercions, so a mistyped value gets
    the same located error a config.toml value gets, naming this file.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(
            f"managed config {path} exists but could not be read "
            f"({exc.strerror or exc}); bettermemory will not start without "
            "the settings it pins. Make the file readable by every account "
            "that runs bettermemory, or remove it."
        ) from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"malformed managed config in {path}: {exc}") from exc
    sections: dict[str, dict[str, Any]] = {}
    for name, value in data.items():
        if not isinstance(value, dict):
            raise ValueError(
                f"malformed managed config in {path}: `{name}` = {value!r} "
                f"must be a [{name}] section table"
            )
        sections[name] = value
    return sections


__all__ = [
    "MANAGED_FILENAME",
    "managed_config_path",
    "read_managed",
    "system_path",
]
