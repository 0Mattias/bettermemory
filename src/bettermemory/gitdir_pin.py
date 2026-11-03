"""The working tree recorded for a git directory kept apart (ENG-206).

A ``.git`` file is followed where git's own pointers say the folder is
the working tree of the git directory it names
(`gitfile_rule.gitfile_refusal`, ENG-158). git writes no such pointer
for a git directory kept apart with ``--separate-git-dir`` (``git
init``, ``git clone``): its config names no ``core.worktree`` and
nothing in it names the folder, so a ``.git`` file anywhere that names
one reads to git as that checkout's own. bettermemory keeps the pointer
git does not. The first capture that follows such a file records the
folder it followed it from (`put`), and a ``.git`` entry in another
folder naming the same git directory is not followed while the recorded
folder's still names it; a recorded folder whose ``.git`` entry is gone
or names another git directory now (the tree moved with a plain ``mv``,
or was removed) gives way to the next folder followed, as a worktree's
pointer back that names nothing any more does.

The records sit in the state directory under ``gitdir-pins/`` and never
in the repository: design 1.1 keeps the checkout token the one file
bettermemory writes there, and a record there would be copied with the
git directory and be absent where the token cannot be written
(``membership.write_token = false``, a read-only or another user's git
directory). A record is a JSON object naming the git directory and the
folder as the capture resolved them, in a file named for the git
directory's filesystem object (device and inode), so a lookup reads one
file whatever path names the git directory (a symlink, another case on a
case-insensitive volume) and stats nothing else; the git directory it
names must still be that object, so a record left by a git directory
whose inode number was reused reads as none. A device number that
changes with a remount finds no record, and the next capture makes a
first record, which the resolution checks against the store's views
(`location._earlier_tree`). A first record is created exclusively (a
hard link, the checkout token's method), so of two captures making one
at once the first stands.

This module reads and writes records and decides nothing: which record
refuses a follow is `gitfile_rule`'s rule, which membership, `githead`
and the origin family read (ENG-193). Only membership's capture writes
a record (`location.record_working_tree`); the others read them.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from ._fsutil import atomic_write_bytes, ensure_owner_only_dir, fsync_file

#: The records' folder inside the state directory.
PINS_DIR = "gitdir-pins"
#: The format each record carries in its ``format`` field.
PIN_FORMAT = 1
# A record holds two paths and a few dozen bytes beside them; a file past
# this is not one of ours, and is not read.
_MAX_BYTES = 1 << 16
_SUFFIX = ".json"


@dataclass(frozen=True)
class Pin:
    """One record: the git directory and the folder recorded as its
    working tree, and the file the record sits in."""

    file: Path
    git_dir: str
    tree: str


def pins_dir() -> Path:
    """``<state>/gitdir-pins``, under the state directory the daemon and
    the hook client resolve alike."""
    from . import _daemon_client

    return _daemon_client.resolve_state_dir() / PINS_DIR


def recorded(git_dir: str) -> Pin | None:
    """The record for the git directory `git_dir`, or None: nothing
    recorded, `git_dir` gone, the record unreadable, or a record whose git
    directory is no longer `git_dir`'s object."""
    try:
        here = os.stat(git_dir)
        pin = _read(pins_dir() / _name(here))
        if pin is None or not os.path.samestat(os.stat(pin.git_dir), here):
            return None
    except (OSError, ValueError):
        return None
    return pin


def put(git_dir: str, tree: str, previous: Pin | None = None) -> bool:
    """Record the folder `tree` as the working tree of `git_dir`: over
    `previous` (the record `recorded` found, which no longer names a
    tree), else as a first record, created only where no record is, so a
    record another capture made meanwhile stands (a file there that is
    not a record is replaced). Returns whether this call wrote it; where the state directory refuses the write nothing is
    recorded, and the next capture that follows the git directory tries
    again."""
    body = {"format": PIN_FORMAT, "git_dir": git_dir, "tree": tree}
    data = (json.dumps(body, ensure_ascii=True) + "\n").encode("ascii")
    try:
        root = pins_dir()
        ensure_owner_only_dir(root, parents=True)
        if previous is not None:
            atomic_write_bytes(previous.file, data, mode_before_rename=0o600)
            return True
        target = root / _name(os.stat(git_dir))
        if _create(target, data):
            return True
        if _read(target) is not None:
            return False
        # A file there that is not a record (cut short by a crash, or
        # another program's): nothing stands, so it is replaced.
        atomic_write_bytes(target, data, mode_before_rename=0o600)
        return True
    except (OSError, ValueError):
        return False


def writable() -> bool:
    """Whether a record could be written: the records' folder, or the
    nearest folder above it that exists, takes writes from this account."""
    try:
        path = pins_dir()
    except (OSError, ValueError):
        return False
    for candidate in (path, *path.parents):
        if candidate.exists():
            return os.access(candidate, os.W_OK | os.X_OK)
    return False


def _create(target: Path, data: bytes) -> bool:
    """Put `data` at `target` only where nothing is: written whole to a
    temporary file beside it, then hard-linked to its name, which never
    replaces one. Where the filesystem refuses hard links, an exclusive
    create of the name itself."""
    temp = target.with_name(f".{target.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY, 0o600)
    try:
        try:
            os.write(fd, data)
            fsync_file(fd)
        finally:
            os.close(fd)
        try:
            os.link(temp, target)
        except FileExistsError:
            return False
        except OSError:
            return _create_in_place(target, data)
        return True
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temp)


def _create_in_place(target: Path, data: bytes) -> bool:
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(fd, data)
        fsync_file(fd)
    finally:
        os.close(fd)
    return True


_BINARY = getattr(os, "O_BINARY", 0)


def _name(st: os.stat_result) -> str:
    """A record's file name: a digest of the git directory's device and
    inode."""
    digest = hashlib.sha256(f"{st.st_dev}:{st.st_ino}".encode("ascii")).hexdigest()
    return digest[:32] + _SUFFIX


def _read(path: Path) -> Pin | None:
    """The record in `path`, or None when it is not one: absent,
    unreadable, past `_MAX_BYTES`, not JSON, another format, or a path
    that is not absolute."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(_MAX_BYTES + 1)
    except OSError:
        return None
    if len(data) > _MAX_BYTES:
        return None
    try:
        body = json.loads(data)
    except ValueError:
        return None
    if not isinstance(body, dict) or body.get("format") != PIN_FORMAT:
        return None
    git_dir, tree = body.get("git_dir"), body.get("tree")
    if not isinstance(git_dir, str) or not isinstance(tree, str):
        return None
    if not (os.path.isabs(git_dir) and os.path.isabs(tree)):
        return None
    return Pin(path, git_dir, tree)
