"""The machine and the store id (design 2.1 and 8.7, decision D16).

A machine is one OS user on one host, VM or container. Its id is random,
minted in the state directory, and kept beside three facts: the
platform's hardware or machine id (IOPlatformUUID on macOS,
``/etc/machine-id`` on Linux, MachineGuid on Windows), the hostname, and
the id file's own filesystem object. `open_machine` compares the facts at
every open. A different hardware id, or, with none on either side, a
different hostname and a different id file object, means the state
directory was carried to another machine (a migrated or restored laptop,
a cloned VM): a new id is minted naming its predecessor, and
`membership.Membership.open` writes the ``machine_cloned`` row before any
resolution. A fact that changed while the machine stayed (a renamed host
whose hardware id agrees) keeps the id and is recorded as
``machine_facts_updated`` (E26 a, b).

A block-level clone with every fact equal keeps one id; each store
decides only for its own disk. At sync and import a machine is keyed by
(machine id, store id), and an import bringing a record that carries
this store's own store id, which this store never wrote, is a fork: the
store takes a new store id (`check_import_fork`, ``store_forked``,
E26 e).

Standard library only. The hardware id is read from files on Linux,
from the registry on Windows and through ``gethostuuid`` on macOS, so no
process is spawned; where ``gethostuuid`` is unavailable ``ioreg`` runs
once per process and its answer is kept.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _caches
from ._fsutil import atomic_write_bytes, ensure_owner_only_dir
from .time_utils import isoformat_utc

if TYPE_CHECKING:
    from .store import Store

#: The id file and the facts file, in the state directory.
MACHINE_ID_FILE = "machine-id"
MACHINE_FACTS_FILE = "machine.json"
_ID_HEADER = "bettermemory machine id v1"
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ID_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_LINUX_MACHINE_ID = ("/etc/machine-id", "/var/lib/dbus/machine-id")


@dataclass(frozen=True)
class MachineFacts:
    """What a machine id is minted with and compared against at every
    open: the hardware or machine id (None where the platform gives
    none), the hostname, and the id file's object as (device, inode,
    birth time in ns or None)."""

    hardware: str | None
    hostname: str | None
    id_file: tuple[str, str, str | None] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "hardware": self.hardware,
            "hostname": self.hostname,
            "id_file": None if self.id_file is None else list(self.id_file),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> MachineFacts | None:
        if not isinstance(raw, dict):
            return None
        hardware = raw.get("hardware")
        hostname = raw.get("hostname")
        id_file = raw.get("id_file")
        obj: tuple[str, str, str | None] | None = None
        if isinstance(id_file, list) and len(id_file) == 3:
            device, inode, birth = id_file
            obj = (str(device), str(inode), None if birth is None else str(birth))
        return cls(
            hardware=hardware if isinstance(hardware, str) else None,
            hostname=hostname if isinstance(hostname, str) else None,
            id_file=obj,
        )


@dataclass(frozen=True)
class MachineIdentity:
    """This machine as `open_machine` found it. ``minted`` is True when the
    id was minted by this open: the state directory had none, or the
    facts showed another machine (``cloned``), in which case
    ``predecessor`` names the id it replaces. ``facts_updated`` is True
    when the id stayed and a fact changed; ``recorded`` holds the facts
    the id was kept with before this open."""

    id: str
    facts: MachineFacts
    predecessor: str | None
    minted: bool
    cloned: bool
    facts_updated: bool
    recorded: MachineFacts | None


def new_machine_id() -> str:
    """26 random Crockford base-32 characters (130 bits)."""
    value = int.from_bytes(secrets.token_bytes(17), "big")
    return "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in range(26))


def is_clone(recorded: MachineFacts, seen: MachineFacts) -> bool:
    """Whether the facts seen at this open belong to another machine than
    the recorded ones (E26): the hardware id decides where both sides
    have one; without it on either side, a different hostname and a
    different id file object together."""
    if recorded.hardware and seen.hardware:
        return recorded.hardware != seen.hardware
    return recorded.hostname != seen.hostname and recorded.id_file != seen.id_file


# -- the facts ---------------------------------------------------------------

_HARDWARE_LOCK = threading.Lock()
_HARDWARE: dict[str, str | None] = {}


@_caches.register
def _clear_hardware() -> None:
    with _HARDWARE_LOCK:
        _HARDWARE.clear()


def _darwin_hardware() -> str | None:
    """IOPlatformUUID through ``gethostuuid(3)``, no process spawned; where
    the call is unavailable, ``ioreg`` once."""
    if sys.platform != "darwin":
        return None
    import ctypes

    try:
        libc = ctypes.CDLL(None)
        buffer = (ctypes.c_ubyte * 16)()
        wait = (ctypes.c_long * 2)(0, 0)
        if libc.gethostuuid(buffer, wait) == 0:
            return str(uuid.UUID(bytes=bytes(buffer))).upper()
    except (OSError, AttributeError, ValueError):
        pass
    try:
        out = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r'"IOPlatformUUID" = "([^"]+)"', out)
    return found.group(1) if found else None


def _linux_hardware() -> str | None:
    for name in _LINUX_MACHINE_ID:
        try:
            value = Path(name).read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if value:
            return value
    return None


def _windows_hardware() -> str | None:
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "MachineGuid")
    except OSError:
        return None
    return str(value) if value else None


def read_hardware_id() -> str | None:
    """The platform's hardware or machine id, read at most once per
    process: IOPlatformUUID on macOS, ``/etc/machine-id`` (or the D-Bus
    copy) on Linux, MachineGuid on Windows; None elsewhere or when the
    platform gives none (a container without a machine id)."""
    with _HARDWARE_LOCK:
        if "value" in _HARDWARE:
            return _HARDWARE["value"]
    if sys.platform == "darwin":
        value = _darwin_hardware()
    elif sys.platform == "win32":
        value = _windows_hardware()
    elif sys.platform.startswith("linux"):
        value = _linux_hardware()
    else:
        value = None
    with _HARDWARE_LOCK:
        _HARDWARE["value"] = value
    return value


def current_hostname() -> str | None:
    try:
        name = socket.gethostname()
    except OSError:
        return None
    return name or None


def _file_object(path: Path) -> tuple[str, str, str | None] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    birth = getattr(st, "st_birthtime_ns", None)
    if birth is None:
        seconds = getattr(st, "st_birthtime", None)
        birth = None if seconds is None else int(seconds * 1_000_000_000)
    return (str(st.st_dev), str(st.st_ino), None if birth is None else str(birth))


# -- the files -----------------------------------------------------------------


def _read_id(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if len(lines) < 2 or lines[0].strip() != _ID_HEADER:
        return None
    value = lines[1].strip()
    return value if _ID_RE.fullmatch(value) else None


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _write_record(
    path: Path, machine_id: str, facts: MachineFacts, predecessor: str | None
) -> None:
    body = {
        "id": machine_id,
        "facts": facts.to_dict(),
        "predecessor": predecessor,
        "recorded": isoformat_utc(datetime.now(timezone.utc)),
    }
    atomic_write_bytes(
        path,
        (json.dumps(body, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        mode_before_rename=0o600,
    )


def open_machine(
    state_dir: Path | None = None,
    *,
    hardware: Callable[[], str | None] | None = None,
    hostname: Callable[[], str | None] | None = None,
) -> MachineIdentity:
    """This machine's id, as of this open of the state directory: the id
    kept there when its facts match this host's (`is_clone`), else a new
    id naming the old one as its predecessor; a first open mints one. A
    changed fact that is no clone is written back. `hardware` and
    `hostname` read the two facts (the platform's own by default)."""
    if state_dir is None:
        from ._daemon_client import resolve_state_dir

        state_dir = resolve_state_dir()
    ensure_owner_only_dir(state_dir, parents=True)
    id_path = state_dir / MACHINE_ID_FILE
    facts_path = state_dir / MACHINE_FACTS_FILE
    seen_hardware = (hardware or read_hardware_id)()
    seen_hostname = (hostname or current_hostname)()

    current = _read_id(id_path)
    record = _read_record(facts_path)
    recorded = MachineFacts.from_dict(record.get("facts")) if record else None
    kept_predecessor = record.get("predecessor") if record else None
    if (
        current is not None
        and record is not None
        and record.get("id") == current
        and recorded is not None
    ):
        seen = MachineFacts(seen_hardware, seen_hostname, _file_object(id_path))
        if not is_clone(recorded, seen):
            updated = seen != recorded
            if updated:
                _write_record(
                    facts_path,
                    current,
                    seen,
                    kept_predecessor if isinstance(kept_predecessor, str) else None,
                )
            return MachineIdentity(
                id=current,
                facts=seen,
                predecessor=(
                    kept_predecessor if isinstance(kept_predecessor, str) else None
                ),
                minted=False,
                cloned=False,
                facts_updated=updated,
                recorded=recorded,
            )

    minted = new_machine_id()
    atomic_write_bytes(
        id_path,
        f"{_ID_HEADER}\n{minted}\n".encode("ascii"),
        mode_before_rename=0o600,
    )
    facts = MachineFacts(seen_hardware, seen_hostname, _file_object(id_path))
    _write_record(facts_path, minted, facts, current)
    return MachineIdentity(
        id=minted,
        facts=facts,
        predecessor=current,
        minted=True,
        cloned=current is not None,
        facts_updated=False,
        recorded=recorded,
    )


# -- the store id ------------------------------------------------------------------


def check_import_fork(
    store: Store, *, record_store_id: str | None, record_id: str, source: str
) -> tuple[str, str] | None:
    """The fork rule of design 8.7 (E26 e), run for each record an import
    or a sync brings: a record that carries this store's own store id and
    that this store never wrote shows the store was copied whole and both
    copies wrote. This store then takes a new store id with a
    ``store_forked`` row; returns ``(old, new)``, or None when it is no
    fork (another store's record, or this store's own export brought
    back)."""
    if record_store_id is None or record_store_id != store.membership_store_id:
        return None
    if store.wrote_record(record_id):
        return None
    return store.reissue_membership_store_id(record_id=record_id, source=source)


__all__ = [
    "MACHINE_FACTS_FILE",
    "MACHINE_ID_FILE",
    "MachineFacts",
    "MachineIdentity",
    "check_import_fork",
    "current_hostname",
    "is_clone",
    "new_machine_id",
    "open_machine",
    "read_hardware_id",
]
