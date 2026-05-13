"""Filesystem durability helpers — fsync a file and its parent directory.

The store's write path is `write to .tmp → rename to target`. POSIX
guarantees the rename itself is atomic, but the rename only updates the
directory entry; the page cache holding the file's bytes (and the
directory's own metadata) can still be lost on power loss between the
rename returning and the kernel flushing dirty pages. Without an explicit
fsync we can end up with a zero-byte file at the target path after an
ungraceful shutdown — the directory entry says the file exists, the data
backing it never reached disk.

Two helpers, narrow on purpose:

* `fsync_file(fd)` — flush a file's data to disk. Cheap. The caller
  passes the open file descriptor (typically `f.fileno()` after
  `f.flush()` and before `close`).
* `fsync_dir(path)` — flush a directory's metadata so a rename inside
  it is durable. POSIX-only; on Windows you can't `open()` a directory
  for fsync and the OS handles rename durability differently anyway,
  so we no-op there.

Both helpers swallow `OSError` and return. fsync legitimately fails on
some pseudo-filesystems (`/proc`, certain tmpfs/overlayfs configs, sandbox
mounts) where the operation isn't supported — that's not a corruption
risk, and propagating the error would break the write surface for
test/CI/container environments without buying any durability they
actually have. The contract is "best-effort durability on filesystems
that support it", not "raise on filesystems that don't".
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path


def fsync_file(fd: int) -> None:
    """fsync a file descriptor. Best-effort — swallows OSError.

    Call after `flush()` and before `close()` on the writer. The kernel
    is free to ignore the fsync on filesystems that don't support it
    (proc, some tmpfs); we treat that as "as durable as this filesystem
    gets" rather than an error.
    """
    with contextlib.suppress(OSError):
        os.fsync(fd)


def fsync_dir(path: Path) -> None:
    """fsync a directory so a rename inside it is durable.

    POSIX requires `fsync` on the parent directory after a rename to
    guarantee the new directory entry survives a crash. On Windows you
    cannot open a directory with `os.open` for fsync, and NTFS handles
    rename ordering through its journal rather than user-issued fsync,
    so the helper is a no-op there.

    The `O_RDONLY` open is the cross-Unix portable form. Some
    filesystems (FAT, overlayfs in unprivileged user namespaces) reject
    directory fsync with EINVAL — we suppress and move on; the
    write/rename has already happened.
    """
    if sys.platform == "win32":  # pragma: no cover - non-unix
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


__all__ = ["fsync_file", "fsync_dir"]
