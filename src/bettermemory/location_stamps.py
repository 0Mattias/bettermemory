"""A git directory's init stamps (amendment A2, ENG-215): the name,
inode number, mtime, change time and size of the files ``git init``
writes and no git command rewrites (`_init_stamps`), and on Windows the
change time as NTFS keeps it (`basic_info_ns`); and whether the git
directory's file system keeps its times in FILETIME's 100 ns ticks
(`kept_in_ticks`, ENG-235).

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
import re
import sys
import threading
from collections.abc import Collection, Iterable, Sequence
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
#: Whether a stamp's change time is NTFS's ChangeTime (`_init_stamps`),
#: and every time a capture reads is kept in ticks (`kept_in_ticks`).
_WINDOWS = sys.platform == "win32"
#: FILETIME's tick, 100 ns: the grain NTFS and ReFS keep a file's times
#: in, and a view of them carries them in (`in_ticks`).
TICK_NS = 100
#: Nanoseconds in a second: a share that keeps times to the second.
SECOND_NS = 1_000_000_000
#: Where a Linux process reads its mounts (`_drvfs`), and whether it is
#: read: a module constant, so a test can stand another table in.
_MOUNTINFO = "/proc/self/mountinfo"
_LINUX = sys.platform.startswith("linux")
#: WSL2's drvfs in a mount's superblock options: a 9p mount of a Windows
#: drive names it as ``aname=drvfs;path=C:\;...`` (`_drvfs`).
_DRVFS_ANAME = re.compile(r"(?:^|[,;])aname=drvfs(?:[,;]|$)")


def in_ticks(times: Collection[int]) -> bool:
    """Whether `times` read as kept in FILETIME's 100 ns ticks: every one
    a whole tick, and not every one a whole second (a share that rounds
    them to the second keeps no ticks). None is not."""
    if not times or any(t % TICK_NS for t in times):
        return False
    return any(t % SECOND_NS for t in times)


def kept_in_ticks(times: Iterable[int], device: int | None = None) -> bool:
    """Whether a git directory's file system keeps its times in
    FILETIME's 100 ns ticks, as a capture reads it (ENG-235): on Windows
    always (NTFS, ReFS, FAT and SMB alike keep FILETIMEs); on Linux where
    `device`, given where the capture read no birth time, is a WSL drvfs
    mount (`_drvfs`), whatever its times; else where `times`, the mtimes
    and ctimes of the objects the capture read on the git directory's
    device, read as ticks (`in_ticks`). A file system keeping nanoseconds
    gives such times by chance, the rarer the more of them differ."""
    if _WINDOWS:
        return True
    if device is not None and _drvfs(device):
        return True
    return in_ticks(set(times))


def stat_each(paths: Iterable[str | None]) -> list[os.stat_result | None]:
    """Each of `paths` stat'ed, None where there is no path or it cannot
    be stat'ed."""
    out: list[os.stat_result | None] = []
    for path in paths:
        try:
            out.append(None if path is None else os.stat(path))
        except (OSError, ValueError):
            out.append(None)
    return out


def capture_ticks(
    git_st: os.stat_result | None,
    git_key: Sequence[object] | None,
    read: Iterable[os.stat_result | None],
    stamps: Iterable[Sequence[Any]],
) -> bool | None:
    """Whether a capture's git directory's file system keeps its times in
    FILETIME's ticks (`kept_in_ticks`, ENG-235), decided over the mtimes
    and ctimes of what the capture read on the git directory's device
    (`read`: the cwd, the common and the git directory, the root, HEAD
    and config) and of its init stamps: a checkout's one or two stamps
    alone fall on ticks by chance far more often
    (`membership_shared._ticked`). The mount table is read where the git
    directory's key (`git_key`) holds no birth time, as a view without it
    (WSL2's drvfs) does. None where the git directory was not read
    (`git_st`)."""
    if git_st is None:
        return None
    dev = git_st.st_dev
    times = [
        t
        for s in read
        if s is not None and s.st_dev == dev
        for t in (s.st_mtime_ns, s.st_ctime_ns)
    ]
    times += [int(t) for s in stamps for t in (s[2], s[3])]
    unborn = git_key is not None and git_key[2] is None
    return kept_in_ticks(times, dev if unborn else None)


def _device_name(device: int) -> str:
    """`device` as /proc/self/mountinfo names it, ``major:minor`` (none
    on Windows, which keeps no such numbers)."""
    if sys.platform == "win32":
        return ""
    return f"{os.major(device)}:{os.minor(device)}"


def _drvfs(device: int) -> bool:
    """Whether `device` is a WSL drvfs mount of a Windows drive, as
    /proc/self/mountinfo names it: a ``9p`` mount whose superblock options
    hold ``aname=drvfs`` (WSL2, and a container's bind mount of one), a
    ``virtiofs`` one whose tag names it drvfs (WSL2's opt-in virtiofs, as
    2.7 tags it, ``drvfsC0``; later releases tag it by a GUID, which the
    times decide), or a ``drvfs`` one (WSL1). False off Linux, or where
    the table cannot be read."""
    if not _LINUX:
        return False
    want = _device_name(device)
    try:
        with open(_MOUNTINFO, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                fields = line.split()
                if len(fields) < 7 or fields[2] != want or "-" not in fields[6:]:
                    continue
                tail = fields[fields.index("-", 6) + 1 :]
                fstype = tail[0] if tail else ""
                source = tail[1] if len(tail) > 1 else ""
                options = tail[2] if len(tail) > 2 else ""
                if (
                    fstype == "drvfs"
                    or (fstype == "9p" and _DRVFS_ANAME.search(options))
                    or (fstype == "virtiofs" and source.startswith("drvfs"))
                ):
                    return True
    except (OSError, ValueError):
        return False
    return False


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
