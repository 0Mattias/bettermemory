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

__all__ = [
    "INDETERMINATE",
    "TOKEN_VERSION",
    "path_gone",
    "path_missing",
    "probe",
    "view_path",
]

#: The state of a location nothing could be read for; `location`'s
#: vocabulary (``tests/test_membership_capture.py`` pins it to the model's).
INDETERMINATE = "indeterminate"
#: The location token's format version.
TOKEN_VERSION = 1

# The errnos in which the OS ANSWERED "is there anything at this path?"
# with "no". Each is a property of the path itself failing to resolve to
# an object, independent of who is asking and of whether any device is
# reachable, which a share that fails does not give and an unmount does
# give (the mount point's folder is empty):
#
#   ENOENT        a component of the path does not exist
#   ENOTDIR       a component that would have to be a directory is not
#                 one, so nothing can live below it
#   ELOOP         the path cycles through symlinks and resolves to
#                 nothing
#   ENAMETOOLONG  this system cannot name an object at that path at
#                 all, the shape a store synced from a longer-path OS
#                 arrives in
#
# The complement is deliberately NOT enumerated: EACCES/EPERM on a
# parent, the unreachable-device family (ENOTCONN, EHOSTDOWN, ETIMEDOUT,
# ESTALE, EIO, ENODEV, ...), and any errno a future platform invents all
# fall through to "cannot tell", so a new error class holds an isolation
# boundary rather than opening it.
GONE_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.ENAMETOOLONG})

# On Windows an error is told by its own code alone, never by the errno
# or the exception class CPython folds it into: a share or drive that
# does not answer (ERROR_BAD_NETPATH 53, ERROR_BAD_NET_NAME 67,
# ERROR_INVALID_DRIVE 15) is ENOENT there, and so a FileNotFoundError, as
# a path that names nothing is (ENG-217). Gone are the codes of the path
# itself: ERROR_FILE_NOT_FOUND (2), ERROR_PATH_NOT_FOUND (3),
# ERROR_INVALID_NAME (123, "that syntax cannot name anything"),
# ERROR_FILENAME_EXCED_RANGE (206, ENAMETOOLONG), ERROR_DIRECTORY (267, a
# file where a folder is named: ENOTDIR) and the reparse-point cycle
# ERROR_CANT_RESOLVE_FILENAME (1921, ELOOP). ERROR_NOT_READY (21), a
# removable or disconnected volume, is deliberately absent, though
# `pathlib` reads it as not existing: that is the unmounted-volume
# fail-open this reading exists to close.
GONE_WINERRORS = frozenset({2, 3, 123, 206, 267, 1921})


def path_gone(exc: BaseException) -> bool:
    """Whether `exc`, raised by a read of a path, is the OS's answer that
    nothing is there: the one reading of that question for membership,
    location and origin, so no two of them tell a share that does not
    answer apart differently (ENG-217). A ValueError is a path the OS
    cannot be asked about (an embedded NUL), where nothing can be. An
    error with a Windows code is told by that code alone (`GONE_WINERRORS`);
    any other OSError by its class or errno (`GONE_ERRNOS`). Any other
    exception cannot tell."""
    if isinstance(exc, ValueError):
        return True
    if not isinstance(exc, OSError):
        return False
    winerror: object = getattr(exc, "winerror", None)
    if isinstance(winerror, int):
        return winerror in GONE_WINERRORS
    return (
        isinstance(exc, (FileNotFoundError, NotADirectoryError))
        or exc.errno in GONE_ERRNOS
    )


def path_missing(exc: BaseException) -> bool:
    """Whether `exc` is a FileNotFoundError or NotADirectoryError that is
    the OS's answer that nothing is there (`path_gone`): the narrower
    absence a reader takes as "no such file" (ENOENT or ENOTDIR, as git
    takes them), which on Windows excludes a share or drive that does not
    answer, raised as the same FileNotFoundError."""
    return isinstance(exc, (FileNotFoundError, NotADirectoryError)) and path_gone(exc)


def _path_state(path: str) -> tuple[str, os.stat_result | None, str]:
    """('present', stat, '') or ('gone' | 'unreachable', None, detail):
    'gone' when the OS answered that nothing is there (`path_gone`),
    'unreachable' when it could not say (permission, a dropped mount, a
    share that does not answer)."""
    try:
        st = os.stat(path)
    except ValueError as exc:
        return "gone", None, str(exc)
    except OSError as exc:
        reason = os.strerror(exc.errno) if exc.errno else str(exc)
        if path_gone(exc):
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
