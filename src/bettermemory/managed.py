"""The organisation-managed config: settings the user's config.toml cannot loosen.

An organisation that deploys bettermemory needs some settings to hold
whatever the user's own file says: telemetry on (its `false` also drops
the audit trail), the escape hatches of the write gates taken away
(`[policy]`), the repository-supplied store refused (`[storage]
cwd_store = false`), the provenance of a foreign store read as
unaccounted. Before this module the user's file was the only source, so
anyone who could edit it, an unsandboxed agent shell included, could
flip every one of them.

The managed config has config.toml's own sections and keys. `load_config`
reads it after the user's file, and every key it sets is pinned: the
user's value for that key is ignored, with one logged notice per key
naming where the managed config is (`config._apply_managed`). It is the
organisation's to write, through MDM, Group Policy, a
configuration-management run or an installer, and it is never created
here.

Where it is read from is fixed per OS, and nothing in the environment
moves it:

- macOS: the file ``/Library/Application Support/bettermemory/managed.toml``
- Linux and every other POSIX system: the file
  ``/etc/bettermemory/managed.toml``. Cygwin's and MSYS2's Python read it
  too, and there ``/`` is a Windows folder (``C:\\cygwin64``,
  ``C:\\msys64``) that often inherits write access for every account from
  ``C:\\``: the file is refused, failing the load, when any folder from
  ``/`` down to it can be written by an account other than an
  administrator, or lies under a mount another account can add
  (`_refuse_planted`), the planting ProgramData would allow.
- Windows: no file. The managed TOML text is the ``ManagedConfig`` value
  of the registry key ``HKEY_LOCAL_MACHINE\\SOFTWARE\\Policies\\bettermemory``
  (`MANAGED_REGISTRY`), a REG_SZ, or a REG_MULTI_SZ whose strings are its
  lines. Only administrators can write under that key by default, and it
  is where Group Policy and Intune put policy, so a value there is the
  machine's administrator's. A file under ProgramData was the obvious
  alternative and is not one: a standard account can create folders there,
  so a file it planted would bind every other account on the machine
  (pinning ``[storage] directory`` to a folder it reads, for one). Nothing
  under ``%ProgramData%`` is read.

Every location needs an administrator to write, so a pin is the
organisation's. No environment variable may relocate, disable or loosen
it, which rules out the obvious helpers: `platformdirs.site_config_dir`
honours ``$XDG_CONFIG_DIRS`` on Linux, and that variable is the user's.

Two layers of seams, both module-level functions looked up at call time.
`managed_config_path` is the file's location, and the test suite's autouse
isolation (tests/conftest.py) points it at a scratch path on every
platform. `managed_config_source` takes that path whenever there is one
and turns to the registry only where it answers None, so the same fixture
keeps every test off the machine's registry as well as off its file, on
Windows too. `_platform` and `_winreg` stand for `sys.platform` and the
stdlib `winreg` module: a test that asks for the Windows source selects it
through the first and hands a fake registry in through the second, on any
host. A child process a test spawns reads the real location, which on a
developer machine or a CI runner holds nothing; giving the children a
redirect would be exactly the variable the rule forbids.

What the managed config is and is not: it binds the user's config, so an
agent that edits config.toml cannot loosen a pinned setting. It does not
bind a process running as the user. Such a process can export
``BETTERMEMORY_DIR``, build a `Config` in code, or open the store file
itself; the managed config is a policy, not a boundary against the
account.

Stdlib only: the hook client loads the config when ``BETTERMEMORY_DIR``
is unset (`_daemon_client.resolved_store_path`), and that path imports
neither the SDK nor pydantic (tests/test_hook_client.py).
"""

from __future__ import annotations

import os
import re
import stat
import sys
import tomllib
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, TypeAlias

#: The managed file's name inside the per-OS directory.
MANAGED_FILENAME = "managed.toml"

_MACOS_DIR = Path("/Library/Application Support/bettermemory")
_POSIX_DIR = Path("/etc/bettermemory")

#: The `sys.platform` prefixes of a Python built on a POSIX layer over
#: Windows: Cygwin's reports ``cygwin``, and MSYS2's runtime build
#: ``msys`` or ``cygwin`` depending on the build. A MinGW or native build
#: reports ``win32`` and reads the registry, not a file.
_POSIX_ON_WINDOWS = ("cygwin", "msys")

#: The registry key, under HKEY_LOCAL_MACHINE, that holds the managed
#: config on Windows, and the name of the value in it.
REGISTRY_KEY = "SOFTWARE\\Policies\\bettermemory"
REGISTRY_VALUE_NAME = "ManagedConfig"

#: The registry's value types by number (winnt.h), so a refusal names the
#: type an administrator picked in regedit or with ``reg add /t``.
_REGISTRY_TYPE_NAMES = {
    0: "REG_NONE",
    1: "REG_SZ",
    2: "REG_EXPAND_SZ",
    3: "REG_BINARY",
    4: "REG_DWORD",
    5: "REG_DWORD_BIG_ENDIAN",
    7: "REG_MULTI_SZ",
    11: "REG_QWORD",
}


@dataclass(frozen=True)
class RegistryValue:
    """A value under HKEY_LOCAL_MACHINE that holds managed TOML text.

    It stands where a file's path stands wherever the managed config is
    named: the loader's errors and notices, `Config.managed_path`, and
    `bettermemory config path|show|check`. Its `str` is the label they
    print, ``registry HKLM\\SOFTWARE\\Policies\\bettermemory\\ManagedConfig``
    for Windows' own."""

    key: str = REGISTRY_KEY
    name: str = REGISTRY_VALUE_NAME

    def __str__(self) -> str:
        return f"registry HKLM\\{self.key}\\{self.name}"


#: Where Windows keeps the managed config.
MANAGED_REGISTRY = RegistryValue()

#: Where a managed config is read from: a file, or a registry value.
ManagedSource: TypeAlias = Path | RegistryValue


class _Winreg(Protocol):
    """The part of the stdlib `winreg` module the reader uses: the module
    itself on Windows, a fake in the tests on any host."""

    @property
    def HKEY_LOCAL_MACHINE(self) -> int: ...
    @property
    def KEY_READ(self) -> int: ...
    @property
    def KEY_WOW64_64KEY(self) -> int: ...
    @property
    def REG_SZ(self) -> int: ...
    @property
    def REG_MULTI_SZ(self) -> int: ...

    def OpenKey(
        self, key: int, sub_key: str, reserved: int, access: int, /
    ) -> AbstractContextManager[Any]: ...

    def QueryValueEx(self, key: Any, name: str, /) -> tuple[Any, int]: ...


def _platform() -> str:
    """`sys.platform`, read at call time. The tests patch this function to
    select another OS's managed source on any host."""
    return sys.platform


def _winreg() -> _Winreg:
    """The stdlib `winreg` module, which only Windows has.

    Off Windows nothing selects the registry outside a test, and a test
    replaces this function with a fake module. Were the registry selected
    anyway, the OSError raised here fails the load closed, as any other
    failure to read the value does (`read_registry_text`)."""
    if sys.platform == "win32":
        import winreg

        return winreg
    raise OSError("the Windows registry exists only on Windows")


def system_path(platform: str) -> Path | None:
    """The managed file's location on `platform` (a `sys.platform`
    value), None on Windows, which reads no file: its managed config is
    the registry value `MANAGED_REGISTRY`.

    A pure function of the platform string, so the tests check every
    branch on any host. `managed_config_path` is the seam the suite
    patches; this is what it returns in production."""
    if platform == "win32":
        return None
    if platform == "darwin":
        return _MACOS_DIR / MANAGED_FILENAME
    return _POSIX_DIR / MANAGED_FILENAME


def managed_config_path() -> Path | None:
    """Where this machine's managed file lives, or None where no file is
    read (Windows, whose managed config is a registry value).

    The file seam: `managed_config_source` reads it through this function,
    looked up on the module at call time, so the test suite's autouse
    fixture can point it at a scratch path."""
    return system_path(_platform())


def managed_config_source() -> ManagedSource:
    """Where `load_config` and `bettermemory config` read this machine's
    managed config: the file `managed_config_path` names, or, where it
    names none, the registry value `MANAGED_REGISTRY`.

    The file seam decides first. That is how the suite's autouse fixture,
    which makes `managed_config_path` answer a scratch path on every
    platform, keeps a test that does not ask for the registry from
    reading the machine's, on Windows as everywhere else."""
    path = managed_config_path()
    return MANAGED_REGISTRY if path is None else path


def read_managed_source(source: ManagedSource) -> dict[str, dict[str, Any]] | None:
    """The managed config's sections from `source`, None when it holds
    none: `read_managed` for a file, `read_managed_registry` for a
    registry value. Both fail closed the same way."""
    if isinstance(source, RegistryValue):
        return read_managed_registry(source)
    return read_managed(source)


def read_managed(path: Path) -> dict[str, dict[str, Any]] | None:
    """The managed file's sections, or None when there is no file.

    Fails closed. A file that exists but cannot be read (its directory
    or the file not readable by this account, a directory where the file
    should be, a symlink whose target is gone), is not UTF-8 (a leading
    byte-order mark is allowed), is not TOML, or holds a top-level value
    that is not a section table, raises a ValueError naming the file:
    the same outcome a malformed config.toml has (the daemon does not
    start, the CLI reports the error). Skipping it would silently drop
    every pin the organisation set, which is the one outcome a managed
    file exists to prevent. Only a missing file means "no managed
    config". On Cygwin and MSYS a file under a folder another account can
    write is refused the same way (`_refuse_planted`).

    Keys are not checked here. `config._apply_managed` parses the
    sections with the loader's own coercions, so a mistyped value gets
    the same located error a config.toml value gets, naming this file.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        # A symlink whose target is gone answers FileNotFoundError too,
        # yet the path exists: an administrator linked it to a file that
        # configuration management keeps elsewhere, on a mount that is not
        # up yet or a checkout mid-update. Reading that as "no managed
        # config" would start without every pin, so it fails closed.
        if path.is_symlink():
            raise ValueError(
                f"managed config {path} is a symlink to a file that does not "
                "exist; bettermemory will not start without the settings it "
                "pins. Restore its target, or remove the link."
            ) from exc
        return None
    except OSError as exc:
        raise ValueError(
            f"managed config {path} exists but could not be read "
            f"({exc.strerror or exc}); bettermemory will not start without "
            "the settings it pins. Make the file readable by every account "
            "that runs bettermemory, or remove it."
        ) from exc
    _refuse_planted(path)
    # "utf-8-sig": a leading byte-order mark is dropped, not parsed.
    # Windows PowerShell 5.1, which MDM and configuration-management
    # scripts on Windows run under, writes one with `-Encoding UTF8`, and
    # tomllib refuses it as an invalid statement at line 1, column 1,
    # which would stop bettermemory over a file that reads as correct.
    # Without a mark the decoding is plain UTF-8.
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"malformed managed config in {path}: {exc}") from exc
    return _sections(parse_managed_text(text, path), path)


def _refuse_planted(path: Path) -> None:
    """On Cygwin and MSYS, refuse the managed file at `path` with a
    ValueError naming it, the folder that lets it be planted, why, and
    what would let it be read, when anything on the way from ``/`` down to
    it, or the file itself, another account could have put there
    (`_planting_reason`). Elsewhere, nothing.

    There ``/`` is a Windows folder, and a folder made under ``C:\\``
    inherits write access for every signed-in account by default, which
    MSYS2's ``C:\\msys64`` keeps: one account could plant
    ``/etc/bettermemory/managed.toml`` and bind every other account, the
    risk rc12 refused ProgramData for. And Cygwin makes ``/etc/fstab.d``
    writable by every account and mounts what ``/etc/fstab.d/<user>``
    names for that user, so one account can mount a folder it owns over
    ``/etc/bettermemory`` for another: the mounts are read from
    ``/proc/mounts``, and one that cannot be read is a refusal. The chain
    is read as written and as resolved, so a link into a writable folder
    is refused too. A refusal fails the load, as an unreadable managed
    file does: skipping the file would drop an organisation's pins
    without a word, and a file this account can write pins nothing
    against an agent running as it."""
    if not _platform().startswith(_POSIX_ON_WINDOWS):
        return
    try:
        mounts = _parse_mounts(_mounts_text())
    except OSError as exc:
        raise ValueError(
            f"managed config {path} is refused: {_MOUNTS.as_posix()} cannot be read "
            f"({exc.strerror or exc}), so a mount another account added on "
            f"the way to it cannot be ruled out; {_PLANTED} Remove the file, "
            "or use a native Windows Python, which reads the registry value."
        ) from exc
    for point in _planting_points(path):
        reason = _planting_reason(point, mounts)
        if reason is not None:
            why, fix = reason
            raise ValueError(
                f"managed config {path} is refused: {point} {why}, and {_PLANTED} {fix}"
            )


#: Where Cygwin and MSYS list their mounts, one per line as Linux does:
#: device, mount point (spaces written ``\\040``), type, options.
_MOUNTS = Path("/proc/mounts")

#: Why a planting refusal matters, said in each one.
_PLANTED = (
    "on Cygwin and MSYS / is a Windows folder where a file one account "
    "plants would bind every other account. bettermemory will not start "
    "without the settings it pins."
)


@dataclass(frozen=True)
class _Mount:
    """One line of ``/proc/mounts``: where it is mounted, and its options."""

    point: PurePosixPath
    options: frozenset[str]


def _mounts_text() -> str:
    """The text of ``/proc/mounts``, looked up at call time: the tests
    replace it."""
    return _MOUNTS.read_text(encoding="utf-8", errors="surrogateescape")


def _parse_mounts(text: str) -> list[_Mount]:
    """The mounts ``/proc/mounts`` lists, octal escapes decoded."""
    mounts: list[_Mount] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[1])
            options = frozenset(fields[3].split(","))
            mounts.append(_Mount(PurePosixPath(point), options))
    return mounts


def _covering(point: Path, mounts: Iterable[_Mount]) -> _Mount | None:
    """The mount `point` lies on among `mounts`: the deepest one mounted
    at `point` or at one of its parents, None when there is none."""
    here = PurePosixPath(point.as_posix())
    found: _Mount | None = None
    for mount in mounts:
        if (mount.point == here or mount.point in here.parents) and (
            found is None or len(mount.point.parts) > len(found.point.parts)
        ):
            found = mount
    return found


def _planting_reason(point: Path, mounts: list[_Mount]) -> tuple[str, str] | None:
    """Why another account could have put `point` there, and what would let
    the file be read, or None when nothing says so.

    In order: a user mount at `point` or above it (``user`` in its options,
    which Cygwin gives every mount that is not the system's), which any
    account can add for another through ``/etc/fstab.d``; a mount without
    access lists (``noacl``, MSYS2's default), where the mode is
    synthesised and nothing shows who can write; then the folder itself
    (`_writable_by_users`). An elevated process can write every folder,
    so a refusal for this account's own write access says so."""
    user_mount = _covering(point, [m for m in mounts if "user" in m.options])
    if user_mount is not None:
        return (
            f"is under the mount {user_mount.point}, which has the user option: "
            "a user mount, which any account can add for another through "
            "/etc/fstab.d",
            "Remove the mount's line under /etc/fstab.d, or remove the file.",
        )
    mount = _covering(point, mounts)
    if mount is not None and "noacl" in mount.options:
        return (
            f"is on the mount {mount.point}, which has the noacl option: no "
            "access lists, so nothing shows who can write it",
            "Mount it with acl, use a native Windows Python, which reads the "
            "registry value, or remove the file.",
        )
    why = _writable_by_users(point)
    if why is None:
        return None
    fix = (
        "Let only administrators write every folder from / down to the "
        "file, or remove the file."
    )
    if why == "is writable by this account":
        fix += (
            " An elevated process can write every folder: run bettermemory unelevated."
        )
    return why, fix


def _planting_points(path: Path) -> list[Path]:
    """Every folder from the root down to `path`, then `path` itself, as
    written and then as resolved, each once: whoever can write one of them
    can replace the file, or the folder on the way to it."""
    points: list[Path] = []
    for spelled in (path.absolute(), Path(os.path.realpath(path))):
        for point in (*reversed(spelled.parents), spelled):
            if point not in points:
                points.append(point)
    return points


def _writable_by_users(point: Path) -> str | None:
    """Why `point` can be written by an account other than an
    administrator, or None when nothing says so.

    Two readings, either one enough. Group or other write bits in its mode:
    Cygwin maps an access entry beyond the owner's, such as the
    Authenticated Users entry ``C:\\`` hands down, to the group bits. And
    this account's own write access (`os.access`), which Cygwin answers
    from the folder's access list: an account that can write it can plant
    the file. On a mount without access lists (``noacl``, which MSYS2
    uses by default) the mode is synthesised and every folder reads as
    this account's own and writable, so a file there is refused: nothing
    on that mount can show it is not plantable. A folder that cannot be
    checked is refused too."""
    try:
        mode = os.stat(point).st_mode
    except OSError as exc:
        return f"cannot be checked ({exc.strerror or exc})"
    if mode & 0o022:
        return f"is writable by group or others (mode {stat.S_IMODE(mode):04o})"
    if os.access(point, os.W_OK):
        return "is writable by this account"
    return None


def read_managed_registry(
    location: RegistryValue = MANAGED_REGISTRY,
) -> dict[str, dict[str, Any]] | None:
    """The sections of the managed TOML a registry value holds, or None
    when the key or the value is absent.

    The text goes through exactly the managed file's checks: the same
    TOML parse, the same refusal of a top-level value that is not a
    section table, the same ValueError naming the source, and the same
    leading byte-order mark dropped (`read_registry_text`), so one text
    pins the same settings, or fails the same way, from either place.
    Reading the value fails closed as reading the file does."""
    text = read_registry_text(location)
    if text is None:
        return None
    return _sections(parse_managed_text(text, location), location)


def read_registry_text(location: RegistryValue = MANAGED_REGISTRY) -> str | None:
    """The managed TOML text a registry value holds, None when the key or
    the value is absent, which is the only reading of "no managed config".

    Read through the registry's 64-bit view (``KEY_WOW64_64KEY``). WOW64
    lists ``SOFTWARE\\Policies`` among its shared keys today, so a 32-bit
    and a 64-bit process, writer or reader, already meet at one key; the
    flag keeps it so should a Windows release redirect the key under
    WOW6432Node, as Microsoft says its lists of redirected keys may change.
    A 32-bit Python then still reads what a 64-bit tool wrote.

    Fails closed, as an unreadable managed file does. A key this account
    may not open, a value of a type other than REG_SZ or REG_MULTI_SZ, or
    any other failure of the registry raises a ValueError naming the
    value: skipping it would start without every pin the organisation
    set. A REG_MULTI_SZ is its strings joined with newlines, one line
    each. A leading byte-order mark, which text pasted from a file saved
    by Windows PowerShell 5.1 can carry, is dropped, as the file's
    ``utf-8-sig`` decoding drops it.

    Text the file could not hold is refused as the file refuses bytes
    that are not UTF-8. The registry stores UTF-16 and hands over an
    unpaired surrogate as it is; TOML parses one inside a string or a
    comment without complaint, so a pinned ``[storage] directory`` would
    carry it to the store's open and stop there with a bare
    UnicodeEncodeError naming nothing. Refused here, it fails the load
    with the file's own error, naming the value."""
    try:
        reg = _winreg()
        with reg.OpenKey(
            reg.HKEY_LOCAL_MACHINE,
            location.key,
            0,
            reg.KEY_READ | reg.KEY_WOW64_64KEY,
        ) as handle:
            value, kind = reg.QueryValueEx(handle, location.name)
        text = _registry_text(reg, value, kind, location)
    except FileNotFoundError:
        # The key or the value is not there: no managed config, as a
        # missing file is none.
        return None
    except OSError as exc:
        raise ValueError(
            f"managed config {location} could not be read "
            f"({exc.strerror or exc}); bettermemory will not start without "
            "the settings it pins. Give every account that runs bettermemory "
            "read access to the key, or remove the value."
        ) from exc
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"malformed managed config in {location}: {exc}") from exc
    return text.removeprefix("\ufeff")


def _registry_text(reg: _Winreg, value: Any, kind: int, location: RegistryValue) -> str:
    """The text of one registry value, or a ValueError for a type that
    does not hold managed TOML. A REG_EXPAND_SZ is refused rather than
    expanded: expanding it substitutes environment variables, which are
    the user's to set."""
    if kind == reg.REG_SZ and isinstance(value, str):
        return value
    if (
        kind == reg.REG_MULTI_SZ
        and isinstance(value, list)
        and all(isinstance(line, str) for line in value)
    ):
        return "\n".join(value)
    name = _REGISTRY_TYPE_NAMES.get(kind, f"type {kind}")
    if kind in (reg.REG_SZ, reg.REG_MULTI_SZ):
        problem = f"is a {name} value whose data does not read as text"
    else:
        problem = (
            f"is a {name} value; the managed TOML is read only from a REG_SZ "
            "or REG_MULTI_SZ value"
        )
    raise ValueError(
        f"managed config {location} {problem}, and bettermemory will not "
        "start without the settings it pins. Write the text as a REG_SZ or "
        "REG_MULTI_SZ, or remove the value."
    )


def parse_managed_text(text: str, source: ManagedSource) -> dict[str, Any]:
    """Managed TOML text parsed, or a ValueError naming `source` when it
    is not TOML.

    The whole table, a top-level value that is not a section included:
    `bettermemory config check` reports such a line itself, and
    `read_managed` / `read_managed_registry` refuse it for the loader."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"malformed managed config in {source}: {exc}") from exc


def _sections(data: dict[str, Any], source: ManagedSource) -> dict[str, dict[str, Any]]:
    """The parsed managed config as section tables, refusing any top-level
    value that is not one: a pin written outside its section would
    otherwise be dropped without a word."""
    sections: dict[str, dict[str, Any]] = {}
    for name, value in data.items():
        if not isinstance(value, dict):
            raise ValueError(
                f"malformed managed config in {source}: `{name}` = {value!r} "
                f"must be a [{name}] section table"
            )
        sections[name] = value
    return sections


__all__ = [
    "MANAGED_FILENAME",
    "MANAGED_REGISTRY",
    "REGISTRY_KEY",
    "REGISTRY_VALUE_NAME",
    "ManagedSource",
    "RegistryValue",
    "managed_config_path",
    "managed_config_source",
    "parse_managed_text",
    "read_managed",
    "read_managed_registry",
    "read_managed_source",
    "read_registry_text",
    "system_path",
]
