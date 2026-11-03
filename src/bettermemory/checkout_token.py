"""The checkout token: `info/bettermemory` inside a checkout's git directory.

Design 1.1, decisions D10 and D11. The token is the one file bettermemory
writes into a repository on its own. It holds two lines, the header
`bettermemory checkout token v1` and 26 Crockford base-32 characters (130
bits; random as `write` mints them, half derived in a re-mint's, below),
and sits in the git directory's `info/` folder:
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
superseding the token, E21 g). Once either has made its temporary file
it removes the temporary files a writer killed mid-write left in `info/`,
those last modified more than an hour before its own (the filesystem's
clock, not this machine's), and nothing else.

A re-mint the store asks for names the token it replaces (`successor`):
half its characters are random and half are derived from them, the token
replaced and the device and inode number of the git directory it is
replaced in (a `Lineage`). The file keeps the version 1 form, so every
reader reads it as any token. Where the store misses the re-mint (a crash
between the write and the commit, an exchange that ends after the client
wrote it), the token found at the checkout's object is one it never
recorded, and `succeeds` tells the checkout's own re-mint from any other
token without birth time (ENG-205). Such a re-mint leaves a token its caller
did not read in place (for a declaration, only another re-mint of the
same lineage) and reports it, under a claim (`_claim`) that orders it
with any other such re-mint of the git directory, so two re-mints of one
token leave one.

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
import hashlib
import itertools
import os
import re
import secrets
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple, TypeGuard

from ._fsutil import fsync_dir, fsync_file, replace_atomic, unlink_retrying

#: The token's file name inside the git directory's `info/` folder.
TOKEN_NAME = "bettermemory"
#: The first line of every token file (format version 1, decision D10).
TOKEN_HEADER = "bettermemory checkout token v1"
#: Characters in a token: 26 Crockford base-32 characters carry 130 bits.
TOKEN_LENGTH = 26
#: Crockford's base-32 alphabet (no I, L, O or U), upper case.
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_TOKEN_RE = re.compile(f"[{CROCKFORD}]{{{TOKEN_LENGTH}}}")
# Characters of a re-mint's token drawn at random (`successor`); the other
# 13 are derived from them and the lineage it replaces. 13 characters of 5
# bits are 65 bits each way: two re-mints of one token in one git directory
# draw one value with probability 2**-65, and a token not minted from that
# lineage (`mint`, a re-mint in another git directory, one planted without
# knowledge of the recorded token) passes `succeeds` with probability
# 2**-65. The link has no key (see `succeeds` for what that leaves).
_NONCE_LENGTH = 13
# BLAKE2b's personalization (16 bytes at most): only this link hashes with
# it, so no digest of the same text made for another use can stand for one.
_LINK_PERSON = b"bm-token-remint"
# A lineage's device and inode number as an object key records them: the
# decimal text of `st_dev` and `st_ino` (`location_git.object_key`; a
# Windows volume serial number is one too). Anything else names no lineage.
_DECIMAL = re.compile("-?[0-9]+")
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
# The claim a re-mint naming its lineage holds while it reads the token and
# renames over it (`_claim`). It holds its holder's nonce (as many random
# characters as a temporary file's name, 60 bits), so a holder removes only
# its own claim and a waiter knows one claim from the next. Under the
# temporary prefix, so `_sweep` removes one a killed re-mint left, an hour
# on, where no re-mint broke it.
_CLAIM_NAME = f"{_TEMP_PREFIX}claim"
# What a waiter breaking a stale claim creates first, exclusively, named
# after the nonce it found and a generation (`_break`): of the waiters that
# found one claim stale, the one that creates it breaks the claim and the
# rest do not.
_BREAK_PREFIX = f"{_TEMP_PREFIX}break-"
# How much older than a waiter's own fresh file (`_fs_now`) a break mark is
# when the breaker that made it was killed holding it: the next generation
# is then taken. A breaker holds its mark for one exclusive create, one read
# of the claim and two unlinks: 0.14 ms median, 0.24 ms at the 99th
# percentile and 5.6 ms at most over 2,000 breaks on APFS (macOS); FAT
# keeps modification times to 2 s, so two of its steps and a margin.
_STALE_BREAK_SECONDS = 5.0
# How long a re-mint waits for another's claim. A holder keeps it for one
# read of a file of a few dozen bytes and one rename, milliseconds even on
# a network filesystem; the bound is the race read's, past the second an
# SMB client answers a lookup from its cache (I34). A claim holding one
# nonce through the whole wait was left by a re-mint killed holding it.
_CLAIM_WAIT_SECONDS = _RACE_READ_SECONDS
# A temporary file last modified longer ago than this, by the filesystem's
# clock (`_fs_now`), was left by a writer killed mid-write; a live writer's
# is far younger.
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
    "skipped:contended",
    "failed:claim_blocked",
    "failed:readonly",
    "failed:io",
]
#: The status of a re-mint whose claim other re-mints held through the
#: whole wait (`remint`): nothing was written, and a checkout recorded as
#: superseded stays so, to be asked again at its next sighting (ENG-205).
CONTENDED: WriteStatus = "skipped:contended"
#: The status of a re-mint that found something other than a regular file
#: at the claim's name (a directory, a link): no re-mint can take the claim
#: until it is removed, so nothing is written, at once (ENG-205).
CLAIM_BLOCKED: WriteStatus = "failed:claim_blocked"


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
      first); `token` is its value. `write` never replaces it; `remint`
      naming a lineage reports so a token other than the one it replaces
      (with `only_siblings`, another re-mint of that lineage).
    - "malformed": a file is there that is not a token; it is left as it is.
    - "skipped:policy", "skipped:bare", "skipped:foreign_owner": nothing was
      attempted (design 1.1).
    - "skipped:contended" (`CONTENDED`): a re-mint's claim stayed other
      re-mints' through the wait; nothing was written.
    - "failed:claim_blocked" (`CLAIM_BLOCKED`): no regular file is at the
      claim's name; nothing was written, and nothing was waited for.
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


def valid(token: object) -> TypeGuard[str]:
    """Whether `token` is a token's value as `mint` makes one: what a
    client's answer of the token it re-minted must hold (ENG-153), and a
    lineage's token (ENG-205)."""
    return isinstance(token, str) and _TOKEN_RE.fullmatch(token) is not None


class Lineage(NamedTuple):
    """What a re-mint replaces (ENG-205): the checkout's token, and the
    device and inode number of its git directory as the store records them
    (`membership_records.ObjectKey`, a capture's object key), as text. A
    re-minted token names it (`successor`) and is read against it
    (`succeeds`)."""

    token: str
    device: str
    inode: str

    @classmethod
    def read(cls, value: object) -> Lineage | None:
        """The lineage a question names (``[token, device, inode]``, as
        `location_witness.AnsweredToken` asks for a re-mint), or None for
        anything else."""
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            return None
        return lineage(value[0], value[1:])


def lineage(token: object, key: object) -> Lineage | None:
    """The lineage of `token` in the git directory whose object key is
    `key` (a ``(device, inode, birth)`` sequence; birth is not read), or
    None where either is missing or malformed: a device or inode number
    that is not the decimal text of an integer among them."""
    if not valid(token) or not isinstance(key, (list, tuple)) or len(key) < 2:
        return None
    device, inode = key[0], key[1]
    if not all(isinstance(x, str) and _DECIMAL.fullmatch(x) for x in (device, inode)):
        return None
    return Lineage(token, device, inode)


def successor(replaced: Lineage) -> str:
    """A fresh token that names the lineage it replaces (ENG-205): 13
    random characters, then 13 derived from them and `replaced`
    (`_link`). To every reader it is a token as `mint` makes one, 26
    Crockford base-32 characters."""
    nonce = random_crockford(_NONCE_LENGTH)
    return nonce + _link(replaced, nonce)


def succeeds(token: str | None, replaced: Lineage | None) -> bool:
    """Whether `token` is a re-mint of `replaced` (`successor`): the
    checkout's own token replaced in its own git directory, which the store
    may never have recorded (ENG-205). False for the token replaced itself,
    and, but with probability 2**-65 each, for a token `mint` made, a
    re-mint of the same token in another git directory (a copy that
    carried it) and a token planted without knowledge of the recorded one.

    The link has no key: whoever knows the recorded token and can stat the
    git directory can make a token that passes. For a superseded token such
    a token decides at once, where planting the token itself leaves the
    object to the last HEAD (ENG-193). A key the hook client also held
    would close nothing: whoever can write the git directory runs code as
    its owner (git runs its hooks and configured commands), so can read
    such a key."""
    if replaced is None or not valid(token) or token == replaced.token:
        return False
    return token[_NONCE_LENGTH:] == _link(replaced, token[:_NONCE_LENGTH])


def _link(replaced: Lineage, nonce: str) -> str:
    """The characters of a re-mint's token derived from its random ones and
    the lineage it replaces: BLAKE2b of the four, each prefixed by its
    length so no two lineages hash one text, read as Crockford base-32."""
    fields = (replaced.token, replaced.device, replaced.inode, nonce)
    text = b"".join(b"%d:%s," % (len(x), x) for x in (f.encode() for f in fields))
    digest = hashlib.blake2b(text, digest_size=16, person=_LINK_PERSON).digest()
    value = int.from_bytes(digest, "big")
    width = TOKEN_LENGTH - _NONCE_LENGTH
    return "".join(CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(width)))


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


def remint(
    git_dir: str | os.PathLike[str],
    *,
    policy: bool,
    bare: bool,
    replacing: Lineage | None = None,
    only_siblings: bool = False,
) -> TokenWrite:
    """Replace the checkout's token with a fresh one: the checkout leaves
    the line it was copied from (`bettermemory project new`, or history
    superseding the token, design 3.3 and E21 g). Whatever is at the
    token's name, a token or a malformed file, is replaced by a rename, so
    a reader sees the old file or the new one, never a partial one. The
    fresh token names `replacing`, the lineage the store records for the
    checkout (`successor`), so a re-mint the store misses is still known
    for the checkout's (ENG-205); with none it is random (`mint`).

    A re-mint naming its lineage replaces the token it names, no token, or
    a file that is no token, and leaves any other token in place, reported
    as ``exists``: its caller read the file moments ago (a hook client's
    capture, a resolution's re-read under its lock), so another token is
    another session's re-mint since, of this release (`succeeds`) or of
    one before 9.0.0rc18, which names nothing. A declaration, which acts on
    the store's record and has not read the file, passes `only_siblings`:
    it leaves only another re-mint of the lineage (`succeeds`) and replaces
    any other token, one copied in from another checkout, which the
    checkout must not keep. The read and rename are made under a claim
    (`_claim`), so of two such re-mints of one token exactly one writes and
    the other reports its token (ENG-205); where other re-mints hold the
    claim through the wait it writes nothing (``skipped:contended``).
    Without a lineage, two concurrent calls each report the value they
    wrote and the later rename stands.

    The same skips as `write` apply. Callers make the call inside the
    resolution they serialize per location (E28).
    """
    skipped = _guard(git_dir, policy=policy, bare=bare)
    if skipped is not None:
        return skipped
    value = mint() if replacing is None else successor(replacing)
    try:
        info = _info_dir(git_dir)
        temp = _write_temp(info, render(value))
    except OSError as exc:
        return _failed(exc)
    try:
        if replacing is None:
            replace_atomic(temp, info / TOKEN_NAME)
        else:
            other = _replace_named(temp, info, replacing, only_siblings)
            if other is not None:
                return other
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


def _replace_named(
    temp: Path, info: Path, replacing: Lineage, only_siblings: bool
) -> TokenWrite | None:
    """Rename `temp` over the token in `info/` unless the file holds another
    token than `replacing`'s, or with `only_siblings` another re-mint of
    `replacing` (`succeeds`), under the re-mint claim: None once renamed,
    else the token found (``exists``), or what kept the claim from this
    re-mint (``skipped:contended``, ``failed:claim_blocked``)."""
    path = info / TOKEN_NAME
    got = _claim(info, temp)
    nonce = got if isinstance(got, str) else None
    try:
        found = _read_at(path)
        if found.state == "present" and (
            succeeds(found.token, replacing)
            if only_siblings
            else found.token != replacing.token
        ):
            return TokenWrite("exists", token=found.token)
        if not isinstance(got, str):
            return got
        replace_atomic(temp, path)
        return None
    finally:
        if nonce is not None:
            _release(info / _CLAIM_NAME, nonce)


def _claim(info: Path, temp: Path) -> str | TokenWrite:
    """Take the re-mint claim in `info/`: an exclusive create (the
    discipline `write` falls back to) of a file holding a fresh nonce,
    which is returned, for `_release`; else what kept it: other re-mints
    holding it through `_CLAIM_WAIT_SECONDS` (``skipped:contended``), or,
    at once, something other than a regular file at its name, which no
    re-mint can take or break (``failed:claim_blocked``). A claim holding
    one nonce through the whole wait was left by a re-mint killed holding
    it (or stalled that long): it is broken (`_break`, judged by the clock
    of `temp`'s filesystem) and the claim taken once more, by this waiter
    or another, never by two."""
    claim = info / _CLAIM_NAME
    nonce = random_crockford(_TEMP_SUFFIX_LENGTH)
    own = info / f"{_TEMP_PREFIX}{nonce}"
    fd = os.open(own, _OPEN_CREATE, _FILE_MODE)
    try:
        os.write(fd, nonce.encode("ascii"))
    finally:
        os.close(fd)
    try:
        return _wait_for(info, claim, nonce, own, temp)
    finally:
        _unlink_quietly(own)


def _wait_for(
    info: Path, claim: Path, nonce: str, own: Path, temp: Path
) -> str | TokenWrite:
    """`_claim`'s wait: `_take` until the claim is this re-mint's, the
    claim's name is blocked, or the wait is out."""
    deadline = time.monotonic() + _CLAIM_WAIT_SECONDS
    pause = _REREAD_DELAY_SECONDS
    first: str | None = None
    contended = TokenWrite(CONTENDED, detail="other re-mints held the claim")
    while not _take(claim, nonce, own):
        if _blocked(claim):
            return TokenWrite(CLAIM_BLOCKED, detail="no regular file at the claim")
        held = _holder(claim)
        left = deadline - time.monotonic()
        if left <= 0:
            if held is not None and held == first:
                _break(info, held, _fs_now(temp))
            return nonce if _take(claim, nonce, own) else contended
        first = held if first is None else first
        time.sleep(min(pause, left))
        pause = min(2 * pause, _RACE_PAUSE_MAX_SECONDS)
    return nonce


def _take(claim: Path, nonce: str, own: Path) -> bool:
    """Make `claim`, holding `nonce`; False where one is there. `own`, a
    file already holding `nonce`, is linked to the claim's name, so the
    claim appears whole: a waiter never reads another's new claim empty,
    which is how a claim a holder was killed making reads, and breaks it.
    Where links are refused (some SMB servers, FAT) an exclusive create
    and a write, which a reader may find empty between the two; a claim
    this call created and could not finish is removed."""
    try:
        os.link(own, claim)
        return True
    except FileExistsError:
        return False
    except OSError:
        pass
    try:
        fd = os.open(claim, _OPEN_CREATE, _FILE_MODE)
    except FileExistsError:
        return False
    try:
        try:
            os.write(fd, nonce.encode("ascii"))
        finally:
            os.close(fd)
    except OSError:
        _unlink_quietly(claim)
        raise
    return True


def _blocked(claim: Path) -> bool:
    """Whether something other than a regular file is at the claim's name
    (a directory, a link, a FIFO): `_take` can never create it, nor
    `_unlink_quietly` or `_sweep` remove a directory."""
    try:
        return not stat.S_ISREG(os.lstat(claim).st_mode)
    except OSError:
        return False


def _holder(claim: Path) -> str | None:
    """What `claim` holds (its holder's nonce; empty where a holder was
    killed making it, or, where links are refused, between its create and
    its write), or None where there is none. A claim that is no small ASCII
    regular file is named by what is wrong with it, which stays the same
    while it stays."""
    text = _read_text(claim)
    if isinstance(text, str):
        return text
    return None if text.state == "absent" else f"{text.state}: {text.detail}"


def _break(info: Path, held: str, now: float | None) -> None:
    """Remove the claim a waiter found holding `held` through its whole
    wait. The waiter first creates a break mark exclusively,
    `_BREAK_PREFIX`, a digest of `held` and a generation: where another
    waiter did, that one breaks it. A mark more than
    `_STALE_BREAK_SECONDS` older than `now` (the filesystem's clock,
    `_fs_now`; None where it could not be read: no mark is) was left by a
    breaker killed holding it, and the waiter tries the next generation,
    so the claim is not kept until `_sweep` removes both an hour on. The
    claim is removed only while it still holds `held`, so a claim another
    waiter took since is left; then this mark and the stale ones before
    it go, as `held` never comes back. Only a holder stalled past the
    wait, or a breaker stalled past `_STALE_BREAK_SECONDS`, between the
    read and the removal while another takes the claim, loses that
    one's."""
    marks = _break_marks(info, held, now)
    if marks is None:
        return
    claim = info / _CLAIM_NAME
    try:
        if _holder(claim) == held:
            _unlink_quietly(claim, lambda: _holder(claim) == held)
    finally:
        for mark in marks:
            _unlink_quietly(mark)


def _break_marks(info: Path, held: str, now: float | None) -> list[Path] | None:
    """`_break`'s marks for `held`: the stale ones it passed and, last, the
    one it created; None where a live breaker holds one."""
    digest = hashlib.blake2b(held.encode("utf-8", "replace"), digest_size=8)
    marks: list[Path] = []
    for generation in itertools.count():
        mark = info / f"{_BREAK_PREFIX}{digest.hexdigest()}-{generation}"
        marks.append(mark)
        try:
            os.close(os.open(mark, _OPEN_CREATE, _FILE_MODE))
        except FileExistsError:
            try:
                age = None if now is None else now - os.stat(mark).st_mtime
            except OSError:
                return None
            if age is None or age <= _STALE_BREAK_SECONDS:
                return None
            continue
        break
    return marks


def _fs_now(path: Path) -> float | None:
    """The filesystem's clock: the modification time of `path`, a file this
    writer made, set to now there (`utime` with no times takes the
    server's clock on NFS; an SMB client may send its own), so on NFS a
    file's age is read in one clock and a machine whose clock runs ahead
    or behind the server's reads it right. None where it cannot be set or
    read."""
    try:
        os.utime(path)
        return os.stat(path).st_mtime
    except OSError:
        return None


def _release(claim: Path, nonce: str) -> None:
    """Remove `claim` if it still holds `nonce`: a claim a waiter broke
    (this holder stalled past the wait) and another took is left to its
    holder."""
    if _holder(claim) == nonce:
        _unlink_quietly(claim, lambda: _holder(claim) == nonce)


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
    temp = info / f"{_TEMP_PREFIX}{random_crockford(_TEMP_SUFFIX_LENGTH)}"
    _create(temp, body)
    _sweep(info, _fs_now(temp))
    return temp


def _sweep(info: Path, now: float | None) -> None:
    """Remove the temporary files writers killed between creating and
    removing them left in `info/`: names starting with `bettermemory.tmp-`
    last modified more than an hour before `now`, the filesystem's clock
    (`_fs_now`: a temporary file just made; None, where it could not be
    read, removes nothing). A concurrent writer's temporary file is younger
    and stays, whatever this machine's clock says; nothing else in `info/`
    is touched, and every OS error here is ignored."""
    if now is None:
        return
    cutoff = now - _STALE_TEMP_SECONDS
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


def _unlink_quietly(path: Path, still: Callable[[], bool] | None = None) -> None:
    """Remove `path` where it can be removed. On Windows a waiter reading
    the claim holds it open for a moment, and the claim and its holder's
    own link to it are one file, so either removal can meet that reader:
    it is tried again (`unlink_retrying`), while `still` holds."""
    with contextlib.suppress(OSError):
        unlink_retrying(path, still)
