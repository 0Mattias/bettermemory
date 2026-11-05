"""A git directory's init stamps (amendment A2, ENG-215): the name,
inode number, mtime, change time and size of the files ``git init``
writes and no git command rewrites (`_init_stamps`), and on Windows the
change time as NTFS keeps it (`basic_info_ns`).

A capture stamps its git directory with them, and the model compares a
record's stamps with the ones read at a sighting, and with another
machine's record of the same files (`membership_shared`). These readers
live apart from `location_git`, which re-exports `_init_stamps`, to keep
each module under the package's 2,000-line bound (ENG-96). The module
imports the standard library alone (ctypes on Windows, inside a
function), as the hook client loads it with `location_git`.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any

#: The files `_init_stamps` stamps: `description` and `info/exclude`,
#: which ``git init`` (and a clone) writes from its template, and in a
#: linked worktree's git directory, which holds neither, `commondir`,
#: which ``git worktree add`` writes there (`_STAMPED_LINKED`, ENG-215).
#: No git command rewrites any of them; ``git worktree move`` and
#: ``repair`` rewrite `gitdir`.
_STAMPED = ("description", "info/exclude")
_STAMPED_LINKED = ("commondir",)
#: FILETIME's epoch, 1601-01-01 UTC, in its 100 ns ticks before the Unix
#: epoch (`filetime_ns`).
_FILETIME_UNIX = 116_444_736_000_000_000
_BASIC_INFO_LOCK = threading.Lock()
_BASIC_INFO: list[Any] = []
#: Whether a stamp's change time is NTFS's ChangeTime (`_init_stamps`).
_WINDOWS = sys.platform == "win32"


def _init_stamps(
    git_dir: str, *, linked: bool = False
) -> tuple[tuple[str, int, int, int, int], ...]:
    """Stamps of the files ``git init`` writes and no git command
    rewrites (`_STAMPED`, and for a `linked` worktree's git directory
    `_STAMPED_LINKED`), compared by equality only (amendment A2):
    ``(name, inode, mtime, ctime, size)``, in ns. On Windows the change
    time is NTFS's ChangeTime (`basic_info_ns`), which a metadata change
    moves as one moves a ctime elsewhere, where ``st_ctime`` holds the
    creation time: a file whose ChangeTime cannot be read is not stamped,
    so the ctime field holds a change time or nothing (every reader of a
    stamp takes five integers). Another machine's record of the same
    files compares the times (`membership_shared`, ENG-215)."""
    out: list[tuple[str, int, int, int, int]] = []
    for name in _STAMPED_LINKED if linked else _STAMPED:
        path = os.path.join(git_dir, *name.split("/"))
        try:
            st = os.stat(path)
        except OSError:
            continue
        changed = st.st_ctime_ns
        if _WINDOWS:
            info = basic_info_ns(path)
            if info is None:
                continue
            changed = info[3]
        out.append((name, st.st_ino, st.st_mtime_ns, changed, st.st_size))
    return tuple(out)


def filetime_ns(ticks: int) -> int:
    """A FILETIME (100 ns ticks since 1601-01-01 UTC) as ns since the Unix
    epoch, as ``os.stat`` reports Windows times."""
    return (ticks - _FILETIME_UNIX) * 100


def basic_info_ns(path: str) -> tuple[int, int, int, int] | None:
    """The four times of `path`'s FILE_BASIC_INFO, read through
    GetFileInformationByHandleEx on a handle opened for its attributes
    alone (a directory too, every share mode): creation, last access,
    last write and change, in ns since the Unix epoch (`filetime_ns`), or
    None off Windows or where any call fails."""
    if sys.platform != "win32":
        return None
    calls = _basic_info_calls()
    if calls is None:
        return None
    create, query, close, info_type = calls
    import ctypes

    handle = create(path, 0x80, 0x7, None, 3, 0x02000000, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        return None
    info = info_type()
    try:
        if not query(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
            return None
    finally:
        close(handle)
    return (
        filetime_ns(info.CreationTime),
        filetime_ns(info.LastAccessTime),
        filetime_ns(info.LastWriteTime),
        filetime_ns(info.ChangeTime),
    )


def _basic_info_calls() -> tuple[Any, Any, Any, Any] | None:
    """kernel32's CreateFileW, GetFileInformationByHandleEx and
    CloseHandle, typed once, and the FILE_BASIC_INFO structure; None where
    they cannot be loaded."""
    with _BASIC_INFO_LOCK:
        if not _BASIC_INFO:
            _BASIC_INFO.append(_load_basic_info())
        got: tuple[Any, Any, Any, Any] | None = _BASIC_INFO[0]
        return got


def _load_basic_info() -> tuple[Any, Any, Any, Any] | None:
    import ctypes
    from ctypes import wintypes

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_int64),
            ("LastAccessTime", ctypes.c_int64),
            ("LastWriteTime", ctypes.c_int64),
            ("ChangeTime", ctypes.c_int64),
            ("FileAttributes", wintypes.DWORD),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined, unused-ignore]
        create = kernel32.CreateFileW
        query = kernel32.GetFileInformationByHandleEx
        close = kernel32.CloseHandle
    except (OSError, AttributeError):
        return None
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    query.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    query.restype = wintypes.BOOL
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    return create, query, close, FileBasicInfo
