"""The hook client's namespace probe (design 8.1), apart from the capture.

Every hook posts where its caller stands before anything else: the cwd
resolved, its device and inode, and the host's name (`probe`), which the
daemon compares with its own reading of the same path. That is one stat
and a ``realpath``, and where the daemon reads the same object it is all
the client sends. `location`, which captures the rest, imports the readers
it captures with (`checkout_token`, `githead`, `gitobjects`,
`project_file`) and a hook that imported it for the probe paid for all of
them on every post. So the probe and the little it reads live here, on
the standard library's ``errno``, ``os`` and ``socket`` alone, and
`location` imports them from here: `location.probe` is this module's
function. `_daemon_client` imports this module on every hook and
`location` only in the witness exchange (design 8.7);
``tests/test_hook_client.py`` pins the modules a hook loads.
"""

from __future__ import annotations

import errno
import os
import socket
from typing import Any

__all__ = ["INDETERMINATE", "TOKEN_VERSION", "probe", "view_path"]

#: The state of a location nothing could be read for; `location`'s
#: vocabulary (``tests/test_membership_capture.py`` pins it to the model's).
INDETERMINATE = "indeterminate"
#: The location token's format version.
TOKEN_VERSION = 1

_GONE_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.ENAMETOOLONG})


def _path_state(path: str) -> tuple[str, os.stat_result | None, str]:
    """('present', stat, '') or ('gone' | 'unreachable', None, detail):
    'gone' when the OS answered that nothing is there, 'unreachable' when
    it could not say (permission, a dropped mount)."""
    try:
        st = os.stat(path)
    except ValueError as exc:
        return "gone", None, str(exc)
    except OSError as exc:
        reason = os.strerror(exc.errno) if exc.errno else str(exc)
        if exc.errno in _GONE_ERRNOS:
            return "gone", None, reason
        return "unreachable", None, reason
    return "present", st, ""


#: The path module `view_path` reads with: a test swaps in a Windows one
#: (``ntpath`` with its junctions) to read Windows spellings on any host.
_VIEW_PATHS: Any = os.path


def view_path(path: str) -> str:
    """`path` as a view records it (ENG-152, ENG-155): absolute, each
    symbolic link and junction on it resolved as the capture resolves a
    cwd (``realpath``), without a trailing separator (the model's
    `_norm`). The one spelling in which a declaration's folder, a
    question's folder and the key of a write made where nothing could be
    read (decision B6) meet the views the capture recorded, so a folder
    reached through a symlinked cwd or a Windows junction names its view.
    Where the OS cannot resolve the path it is made absolute and
    normalised alone. Reads the links on the path (an ``lstat`` per
    part), never what is in it."""
    paths = _VIEW_PATHS
    try:
        real = str(paths.realpath(path))
    except (OSError, ValueError):
        try:
            real = str(paths.normpath(paths.abspath(path)))
        except (OSError, ValueError):
            real = path
    stripped = real.rstrip("/\\")
    return stripped or real


def _hostname() -> str | None:
    try:
        return socket.gethostname() or None
    except OSError:
        return None


def probe(cwd: str) -> dict[str, Any]:
    """The hook client's first word on where it stands (design 8.1): the
    cwd resolved, its device and inode, and the host's name, which the
    daemon compares with its own reading of the same path. A stat and a
    ``realpath``; nothing else is read."""
    out: dict[str, Any] = {"v": TOKEN_VERSION, "probe": True, "cwd": cwd}
    state, st, why = _path_state(cwd)
    if state != "present" or st is None:
        # Keyed as a view records it, so the write it homes (B6) settles
        # when the checkout there is resolved (ENG-155).
        out["cwd"] = view_path(cwd)
        out["state"] = INDETERMINATE
        out["detail"] = f"cwd {state}: {why}" if why else f"cwd {state}"
        return out
    try:
        real = os.path.realpath(cwd)
    except (OSError, ValueError):
        real = cwd
    out["cwd"] = real
    # Device and inode: the daemon reads the path at the same moment, so a
    # reused inode number cannot stand between the two readings, and no
    # birth time is read (statx would load ctypes into every hook).
    out["cwd_object"] = [str(st.st_dev), str(st.st_ino), None]
    out["hostname"] = _hostname()
    return out
