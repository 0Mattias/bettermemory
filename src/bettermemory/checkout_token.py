"""The checkout token: `info/bettermemory` inside a checkout's git directory.

Design 1.1, decisions D10 and D11. The token is the one file bettermemory
writes into a repository on its own. It holds two lines, the header
`bettermemory checkout token v1` and 26 random Crockford base-32 characters
(130 bits), and sits in the git directory's `info/` folder:
`.git/info/bettermemory`, a linked worktree's
`.git/worktrees/<name>/info/bettermemory`, or the same place under a
separate git directory or GIT_DIR. git keeps `info/` local: `git status`
never shows the file, `git clone` never copies it, and git's maintenance
leaves it as it was (E19 a). Whatever copies a git directory copies the
token, which makes it a witness of moves and copies (design 2.2, witness T).

`write` puts a fresh token in place only where none is. The bytes go to a
temporary file in the same `info/` folder, are flushed to disk, and the
file is hard-linked to the token's name (CreateHardLinkW on Windows); a
link never replaces an existing name, so of two concurrent writers exactly
one succeeds and both read the same token afterwards (E28 c). The loser
reads the winner's file again for a bounded time while it finds it missing
or unfinished, as an SMB client can show it just after the race (I34).
Where the filesystem refuses hard links (some SMB servers, FAT) the token is
made by an exclusive create instead, and `read` reads a file it finds empty
or cut short once more, since such a writer may be between its create and
its write (design 8.7). `remint` replaces the token, for a checkout that leaves
the line it was copied from (`bettermemory project new`, or history
superseding the token, E21 g). Before either makes its temporary file it
removes the temporary files older than an hour that a writer killed
mid-write left in `info/`, and nothing else.

Nothing is written into a bare repository, into a git directory another OS
user owns, or anywhere while the policy switch `membership.write_token` is
off; a read-only directory records its failure with the OS error text
(design 1.1, 8.7). The caller passes the policy and whether the repository
is bare: this module runs no git and reads no repository configuration. It
imports nothing beyond the standard library and `_fsutil`, since the hook
client writes the token in its own namespace (design 8.1).
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import secrets
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ._fsutil import fsync_dir, fsync_file, replace_atomic

#: The token's file name inside the git directory's `info/` folder.
TOKEN_NAME = "bettermemory"
#: The first line of every token file (format version 1, decision D10).
TOKEN_HEADER = "bettermemory checkout token v1"
#: Characters in a token: 26 Crockford base-32 characters carry 130 bits.
TOKEN_LENGTH = 26
#: Crockford's base-32 alphabet (no I, L, O or U), upper case.
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_TOKEN_RE = re.compile(f"[{CROCKFORD}]{{{TOKEN_LENGTH}}}")
# A well-formed file is a few dozen bytes; a file longer than this is not
# a token file and is not read further.
_MAX_BYTES = 4096
# The pause before a file found empty or cut short is read again: the
# prototype's interval (proto/capture.py `read_token`).
_REREAD_DELAY_SECONDS = 0.01
# How long a writer that lost the race reads the winner's token again while
# it finds no file there, or one empty or cut short (design 8.7, I34): past
# the second for which a Linux SMB client answers a lookup that found
# nothing from its cache (cifs keeps a negative dentry that long), and the
# longest pause between two reads.
_RACE_READ_SECONDS = 1.5
_RACE_PAUSE_MAX_SECONDS = 0.2
# What `_malformed_detail` says of a file a writer has not finished.
_EMPTY = "empty"
_CUT_SHORT = "cut short"
# Characters of randomness in a temporary file's name.
_TEMP_SUFFIX_LENGTH = 12
# Every temporary file starts with this name in `info/`.
_TEMP_PREFIX = f"{TOKEN_NAME}.tmp-"
# A temporary file last modified longer ago than this was left by a writer
# killed mid-write; a live writer's is far younger.
_STALE_TEMP_SECONDS = 3600.0
_FILE_MODE = 0o644
_READONLY_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})
# O_NONBLOCK keeps a FIFO planted at the token's name from blocking the
# reader (it is then reported as not a regular file); O_BINARY keeps
# Windows from translating line ends.
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_OPEN_READ = os.O_RDONLY | _NONBLOCK | getattr(os, "O_BINARY", 0)
# The errors a non-blocking open or read is made again after, plainly: a
# network filesystem's client refusing the flag (`_read_text`).
_RETRY_ERRNOS = frozenset({errno.EINVAL, errno.EIO})
_OPEN_CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)

ReadState = Literal["present", "absent", "malformed", "unreadable"]
WriteStatus = Literal[
    "written",
    "exists",
    "malformed",
    "skipped:bare",
    "skipped:foreign_owner",
    "skipped:policy",
    "failed:readonly",
    "failed:io",
]


@dataclass(frozen=True)
class TokenRead:
    """What `read` found at `info/bettermemory`.

    `state` is "present" (`token` holds the value), "absent" (no file, or
    no git directory), "malformed" (a file that is not a version 1 token,
    `detail` says how) or "unreadable" (the OS refused the read, `detail`
    holds its error text).
    """

    state: ReadState
    token: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class TokenWrite:
    """The outcome of `write` or `remint`, for the caller to log and to
    name in the session's line (design 5.4, L1 and L2).

    - "written": this call put `token` in place.
    - "exists": a token was already there (or another writer placed one
      first); `token` is its value. `write` never replaces it.
    - "malformed": a file is there that is not a token; it is left as it is.
    - "skipped:policy", "skipped:bare", "skipped:foreign_owner": nothing was
      attempted (design 1.1).
    - "failed:readonly": the OS refused the write (EACCES, EPERM, EROFS).
    - "failed:io": any other OS error, or an existing token that could not
      be read.

    For the failures `detail` holds the OS error text; for "malformed",
    what is wrong with the file.
    """

    status: WriteStatus
    token: str | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        """True when the git directory now carries a token."""
        return self.status in ("written", "exists")


def random_crockford(length: int) -> str:
    """`length` random Crockford base-32 characters, 5 bits each, from
    `secrets`."""
    value = secrets.randbits(5 * length)
    return "".join(CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(length)))


def mint() -> str:
    """A fresh token value: 26 Crockford base-32 characters (130 bits)."""
    return random_crockford(TOKEN_LENGTH)


def valid(token: object) -> bool:
    """Whether `token` is a token's value as `mint` makes one: what a
    client's answer of the token it re-minted must hold (ENG-153)."""
    return isinstance(token, str) and _TOKEN_RE.fullmatch(token) is not None


def token_path(git_dir: str | os.PathLike[str]) -> Path:
    """`<git_dir>/info/bettermemory`."""
    return Path(git_dir) / "info" / TOKEN_NAME


def render(token: str) -> bytes:
    """The bytes of a token file holding `token`."""
    return f"{TOKEN_HEADER}\n{token}\n".encode("ascii")


def parse(text: str) -> str | None:
    """The token a file's text holds, or None when the text is not a
    version 1 token file: exactly the header line and the token line, with
    or without a final line end (a CR before each line end is accepted)."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    if len(lines) != 2:
        return None
    header, value = (line.removesuffix("\r") for line in lines)
    if header != TOKEN_HEADER or _TOKEN_RE.fullmatch(value) is None:
        return None
    return value


def read(git_dir: str | os.PathLike[str]) -> TokenRead:
    """Read the token of the checkout whose git directory is `git_dir`.

    A file found empty, or holding only the start of a token file, is read
    once more after a short pause: a writer that had to use the exclusive
    create may be between creating the file and writing it (design 8.7).
    If the second read still finds no token the file is reported as
    malformed. Nothing here raises for the file's content or for an OS
    error.
    """
    return _read_at(token_path(git_dir))


def write(git_dir: str | os.PathLike[str], *, policy: bool, bare: bool) -> TokenWrite:
    """Give the checkout whose git directory is `git_dir` a token, unless it
    has one (decision D11: at a checkout's first sight).

    `policy` is `membership.write_token`; `bare` says whether `git_dir` is a
    bare repository's, which only the caller's capture can tell without
    reading the repository's configuration. Skipped with policy off, in a
    bare repository and in a git directory another OS user owns. An existing
    token, or an existing file that is not one, is never replaced.

    The token is written to a temporary file in `info/`, flushed to disk
    and hard-linked to its name, so two concurrent writers leave one token
    and both report its value (E28 c); where the filesystem refuses the
    link, an exclusive create takes its place (design 8.7). Never raises
    for an OS error: the status says what happened.
    """
    skipped = _guard(git_dir, policy=policy, bare=bare)
    if skipped is not None:
        return skipped
    path = token_path(git_dir)
    present = _existing(path)
    if present is not None:
        return present
    value = mint()
    body = render(value)
    try:
        temp = _write_temp(_info_dir(git_dir), body)
    except OSError as exc:
        return _failed(exc)
    try:
        return _place(temp, path, value, body)
    finally:
        _unlink_quietly(temp)


def remint(git_dir: str | os.PathLike[str], *, policy: bool, bare: bool) -> TokenWrite:
    """Replace the checkout's token with a fresh one: the checkout leaves
    the line it was copied from (`bettermemory project new`, or history
    superseding the token, design 3.3 and E21 g). Whatever is at the
    token's name, a token or a malformed file, is replaced by a rename, so
    a reader sees the old file or the new one, never a partial one.

    The same skips as `write` apply. Two concurrent calls each report the
    value they wrote and the later rename stands, so callers make the call
    inside the resolution they serialize per location (E28).
    """
    skipped = _guard(git_dir, policy=policy, bare=bare)
    if skipped is not None:
        return skipped
    value = mint()
    try:
        info = _info_dir(git_dir)
        temp = _write_temp(info, render(value))
    except OSError as exc:
        return _failed(exc)
    try:
        replace_atomic(temp, info / TOKEN_NAME)
    except OSError as exc:
        return _failed(exc)
    finally:
        _unlink_quietly(temp)
    fsync_dir(info)
    return TokenWrite("written", token=value)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _guard(
    git_dir: str | os.PathLike[str], *, policy: bool, bare: bool
) -> TokenWrite | None:
    """The cases where nothing is attempted (design 1.1, 8.7): the policy
    switch off, a bare repository, a git directory another OS user owns.
    The owner is compared with the effective uid where the platform has
    uids; Windows has no such check."""
    if not policy:
        return TokenWrite("skipped:policy")
    if bare:
        return TokenWrite("skipped:bare")
    if sys.platform != "win32":
        try:
            owner = os.stat(git_dir).st_uid
        except OSError as exc:
            return _failed(exc)
        if owner != os.geteuid():
            return TokenWrite("skipped:foreign_owner")
    return None


def _read_at(path: Path) -> TokenRead:
    text = _read_text(path)
    if isinstance(text, str) and parse(text) is None and _cut_short(text):
        time.sleep(_REREAD_DELAY_SECONDS)
        text = _read_text(path)
    if not isinstance(text, str):
        return text
    value = parse(text)
    if value is None:
        return TokenRead("malformed", detail=_malformed_detail(text))
    return TokenRead("present", token=value)


def _read_text(path: Path) -> str | TokenRead:
    """The file's text, or the TokenRead that ends the read (absent,
    unreadable, or malformed for what is not a small ASCII regular file).

    The open is non-blocking, so a FIFO planted at the name does not hold
    the reader. A network filesystem's client can refuse the non-blocking
    open, or a read on such a descriptor, with ``EINVAL`` (seen on a cifs
    mount just after another writer placed the file): the open is then
    made again plainly once a stat says a regular file is there, and the
    read again with the flag cleared on the same descriptor. The detail
    of an unreadable file names the call that failed."""
    try:
        fd = os.open(path, _OPEN_READ)
    except (FileNotFoundError, NotADirectoryError):
        return TokenRead("absent")
    except OSError as exc:
        if exc.errno not in _RETRY_ERRNOS:
            return TokenRead("unreadable", detail=f"open: {_os_text(exc)}")
        plain = _open_plain(path)
        if not isinstance(plain, int):
            return plain
        fd = plain
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return TokenRead("malformed", detail="not a regular file")
        data = _read_all(fd)
    except OSError as exc:
        return TokenRead("unreadable", detail=f"read: {_os_text(exc)}")
    finally:
        os.close(fd)
    if len(data) > _MAX_BYTES:
        return TokenRead("malformed", detail="larger than a token file")
    try:
        return data.decode("ascii")
    except UnicodeDecodeError:
        return TokenRead("malformed", detail="not ASCII text")


def _open_plain(path: Path) -> int | TokenRead:
    """A blocking open of `path` for the retry of a non-blocking one the
    filesystem refused: only once a stat says a regular file is there, so
    the open cannot wait on a FIFO."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return TokenRead("malformed", detail="not a regular file")
        return os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except (FileNotFoundError, NotADirectoryError):
        return TokenRead("absent")
    except OSError as exc:
        return TokenRead("unreadable", detail=f"open: {_os_text(exc)}")


def _read_all(fd: int) -> bytes:
    """Up to a byte more than a token file may hold, from `fd`. A read the
    filesystem refuses with ``EINVAL`` on the non-blocking descriptor is
    made once more with the flag cleared (`fcntl`, where there is one)."""
    data = b""
    while len(data) <= _MAX_BYTES:
        try:
            chunk = os.read(fd, _MAX_BYTES + 1 - len(data))
        except OSError as exc:
            if exc.errno not in _RETRY_ERRNOS or not _clear_nonblock(fd):
                raise
            chunk = os.read(fd, _MAX_BYTES + 1 - len(data))
        if not chunk:
            break
        data += chunk
    return data


def _clear_nonblock(fd: int) -> bool:
    """Clear ``O_NONBLOCK`` on `fd`; False where the platform has no
    ``fcntl`` or the flag could not be changed."""
    if sys.platform == "win32":
        return False
    import fcntl

    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~_NONBLOCK)
    except OSError:
        return False
    return True


def _cut_short(text: str) -> bool:
    """Whether `text` is the start of a token file that a writer has not
    finished: empty, part of the header line, or the header line and fewer
    than 26 token characters."""
    head = TOKEN_HEADER + "\n"
    if len(text) <= len(head):
        return head.startswith(text)
    rest = text[len(head) :]
    return (
        text.startswith(head)
        and len(rest) < TOKEN_LENGTH
        and all(char in CROCKFORD for char in rest)
    )


def _malformed_detail(text: str) -> str:
    if not text:
        return _EMPTY
    if _cut_short(text):
        return _CUT_SHORT
    return f"not a {TOKEN_HEADER} file"


def _existing(path: Path) -> TokenWrite | None:
    """What `write` reports for a file already at the token's name; None
    when there is none."""
    found = _read_at(path)
    if found.state == "absent":
        return None
    if found.state == "present":
        return TokenWrite("exists", token=found.token)
    if found.state == "malformed":
        return TokenWrite("malformed", detail=found.detail)
    return TokenWrite(
        "failed:io", detail=f"the existing token cannot be read: {found.detail}"
    )


def _info_dir(git_dir: str | os.PathLike[str]) -> Path:
    """`<git_dir>/info`, made when missing (a linked worktree's git
    directory has none until something needs it). The git directory itself
    is never made: its absence fails here."""
    info = Path(git_dir) / "info"
    with contextlib.suppress(FileExistsError):
        os.mkdir(info)
    return info


def _create(path: Path, body: bytes) -> None:
    """Create `path` exclusively, write `body` and flush it to disk. A file
    this call created and could not finish is removed."""
    fd = os.open(path, _OPEN_CREATE, _FILE_MODE)
    try:
        try:
            view = memoryview(body)
            while view:
                view = view[os.write(fd, view) :]
            fsync_file(fd)
        finally:
            os.close(fd)
    except OSError:
        _unlink_quietly(path)
        raise


def _write_temp(info: Path, body: bytes) -> Path:
    _sweep(info)
    temp = info / f"{_TEMP_PREFIX}{random_crockford(_TEMP_SUFFIX_LENGTH)}"
    _create(temp, body)
    return temp


def _sweep(info: Path) -> None:
    """Remove the temporary files writers killed between creating and
    removing them left in `info/`: names starting with `bettermemory.tmp-`
    last modified more than an hour ago. A concurrent writer's temporary
    file is younger and stays; nothing else in `info/` is touched, and
    every OS error here is ignored."""
    cutoff = time.time() - _STALE_TEMP_SECONDS
    with contextlib.suppress(OSError), os.scandir(info) as entries:
        for entry in entries:
            if not entry.name.startswith(_TEMP_PREFIX):
                continue
            with contextlib.suppress(OSError):
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.unlink(entry.path)


def _place(temp: Path, path: Path, value: str, body: bytes) -> TokenWrite:
    """Link the finished temporary file to the token's name; where links
    are refused, create the name exclusively. Losing the race to another
    writer reports that writer's token."""
    try:
        os.link(temp, path)
    except FileExistsError:
        return _after_race(path)
    except OSError:
        # Links refused here (some SMB servers, FAT): an exclusive create,
        # which a reader may catch before its bytes arrive (see `read`).
        try:
            _create(path, body)
        except FileExistsError:
            return _after_race(path)
        except OSError as exc:
            return _failed(exc)
    fsync_dir(path.parent)
    return TokenWrite("written", token=value)


def _after_race(path: Path) -> TokenWrite:
    """What a writer that lost the race to `path` reports: the winner's
    token. The loser's link or exclusive create was refused because the
    name exists, yet its read can still find no file there (an SMB client
    answers from the lookup it made just before) or one empty or cut short
    (the winner's bytes not yet in), or a read the filesystem refuses for
    the moment. Each is read again, in pauses growing from
    `_REREAD_DELAY_SECONDS`, for up to `_RACE_READ_SECONDS` (design 8.7,
    I34); then what was last found is reported, the token removed when
    nothing is there."""
    deadline = time.monotonic() + _RACE_READ_SECONDS
    pause = _REREAD_DELAY_SECONDS
    while True:
        found = _existing(path)
        unfinished = (
            found is None
            or found.status == "failed:io"
            or (found.status == "malformed" and found.detail in (_EMPTY, _CUT_SHORT))
        )
        left = deadline - time.monotonic()
        if not unfinished or left <= 0:
            break
        time.sleep(min(pause, left))
        pause = min(2 * pause, _RACE_PAUSE_MAX_SECONDS)
    if found is None:
        return TokenWrite(
            "failed:io", detail="the token another writer placed was removed"
        )
    return found


def _failed(exc: OSError) -> TokenWrite:
    status: WriteStatus = (
        "failed:readonly" if exc.errno in _READONLY_ERRNOS else "failed:io"
    )
    return TokenWrite(status, detail=_os_text(exc))


def _os_text(exc: OSError) -> str:
    """The OS error text with its errno name, without the path."""
    text = exc.strerror or str(exc)
    name = errno.errorcode.get(exc.errno) if exc.errno is not None else None
    return f"{text} ({name})" if name else text


def _unlink_quietly(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)
