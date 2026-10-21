"""Git objects read from a repository's files, with no git process.

Membership asks a repository's history three things (design 2.2): does it
hold the witness commits (H), which of its commits carry a witness's
fingerprint (Hf), and which file blobs a witness commit holds (Hb). Git
answers them at the cost of a process, and not at all for a repository its
ownership check refuses. The answers sit in files git writes, and design 3.1
reads them directly, as the prototype's ``proto/objects.py`` did on the
owner's packs (E17) and over one batch of witnesses with no spawn (E29,
design 8.9):

- loose objects, ``objects/xx/<rest>``: a zlib stream of ``<type> <size>``,
  a NUL byte and the content;
- pack indexes, ``objects/pack/*.idx``, version 2: a fanout table of 256
  counts, the sorted object names, a CRC per object, a 31-bit offset per
  object, and a table of 64-bit offsets for the entries whose high bit is
  set;
- packs, ``*.pack``: per object a type and size header and a zlib stream;
  a delta first names its base, as a distance back in the same pack
  (OFS_DELTA) or as an object name (REF_DELTA), and its stream holds the
  copy and insert instructions;
- ``objects/info/alternates``: further object directories, followed the way
  git follows them.

Reading runs nothing a repository configures, so git's ``safe.directory``
refusal has nothing to guard here (design 3.1, E20 f). Paths and names stay
bytes: no path is decoded. A file that is missing, cut short or damaged
reads as an absent object, never as an exception. A read inflates nothing
whose headers claim more than a cap (`ObjectReader.read`'s `max_size`), so a
hostile repository cannot make it inflate gigabytes. A file a provider has
evicted is not opened (design 3.1, D19): it is listed in
`ObjectReader.evicted` for the caller, which owns the fetch budget.

Where an absent answer may be wrong because the reader could not read
something git might (a pack index it cannot parse, an evicted file, an
object format it does not know), `ObjectReader.complete` is False and the
caller asks git.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import struct
import zlib
from collections.abc import Iterable, Iterator
from io import FileIO
from typing import NamedTuple

__all__ = [
    "MIN_ABBREV",
    "Commit",
    "ObjectReader",
    "TreeEntry",
    "commit_fingerprint",
]

# Object type codes of a pack entry header (git's ``enum object_type``).
_TYPE_NAMES = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
_LOOSE_TYPES = {name.encode(): name for name in _TYPE_NAMES.values()}
_OFS_DELTA = 6
_REF_DELTA = 7

# Pack index version 2: magic, version, then the fanout table.
_IDX_MAGIC = b"\xfftOc"
_IDX_VERSION = b"\x00\x00\x00\x02"
_IDX_NAMES_AT = 8 + 256 * 4
_LARGE_OFFSET = 0x80000000

# Bytes read at a pack entry's offset to parse its header: the type and size
# varint, then a distance varint (OFS_DELTA) or a base name of at most 32
# bytes (REF_DELTA).
_ENTRY_PEEK = 64
# Compressed bytes fed to zlib per read: small where only a header is
# wanted, so a large object is never inflated whole for its type and size;
# where the whole object is wanted, at most _BODY_CHUNK, and for a packed
# entry no more than its inflated size and a margin for zlib's own bytes.
_HEAD_CHUNK = 1024
_BODY_CHUNK = 1 << 16
_CHUNK_MARGIN = 64
# The most output asked of zlib in one call: a size header can claim more
# than any stream holds, and zlib takes a bounded length.
_PIECE = 1 << 24
# Inflated bytes that hold a loose header ``<type> <size>`` and its NUL.
_LOOSE_HEAD = 32
# Inflated bytes that hold a delta's two size varints.
_DELTA_HEAD = 20
# The default cap on a read: the largest size an object, or any entry of
# its delta chain, may claim before `ObjectReader.read` declines it. A
# defence against a hostile repository (a planted pack, a zlib bomb) whose
# headers claim gigabytes for one commit; membership reads commits and
# trees, never blob content.
_MAX_OBJECT_BYTES = 4 << 20
# The total a read may inflate across a delta chain (the base, every
# delta's instructions and every result), as a multiple of its cap: a chain
# of entries each under the cap still costs its whole length, and a hostile
# one is refused from its headers alone.
_WORK_FACTOR = 16
# Entries one read may follow down a delta chain. Git's writer caps a chain
# at 4095 deltas (``git repack --depth`` warns "forcing 4095"); a longer
# walk is a damaged pack or a cycle of REF_DELTA bases.
_MAX_CHAIN = 4096
# The deepest nesting at which git reads an alternates file, the
# repository's own being at 0 (``link_alt_odb_entries`` ignores a deeper one).
_ALT_DEPTH = 5

# A file whose content a provider fetches on first read: SF_DATALESS in
# ``st_flags`` on macOS, FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS in
# ``st_file_attributes`` on Windows.
_SF_DATALESS = 0x40000000
_RECALL_ON_DATA_ACCESS = 0x00400000

# Flags for every open: no wait on a FIFO, binary mode on Windows, no
# inheritance by child processes.
_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)

_HEX = re.compile(r"[0-9a-fA-F]+")
# The shortest abbreviated object name git accepts (``MINIMUM_ABBREV``).
MIN_ABBREV = 4
_HEX_BYTES = re.compile(rb"[0-9a-fA-F]+")
_MODE = re.compile(rb"[0-7]{1,6}")
_OCTAL_PAIR = re.compile(rb"[0-7]{2}")

# The escapes of git's ``unquote_c_style`` besides the octal form.
_C_ESCAPES = {
    ord("a"): 7,
    ord("b"): 8,
    ord("f"): 12,
    ord("n"): 10,
    ord("r"): 13,
    ord("t"): 9,
    ord("v"): 11,
    ord("\\"): ord("\\"),
    ord('"'): ord('"'),
}


class Commit(NamedTuple):
    """A commit's header fields and message. `tree` and `parents` are object
    names in lowercase hex; `parents` are the ``parent`` lines that follow the
    ``tree`` line, as git's ``parse_commit_buffer`` reads them. `author` and
    `committer` are the values of the first ``author`` and ``committer``
    lines, as stored (b"" where the line is absent); `message` is everything
    after the first blank line."""

    tree: str
    parents: tuple[str, ...]
    author: bytes
    committer: bytes
    message: bytes


class TreeEntry(NamedTuple):
    """One entry of a tree object: its mode (``0o40000`` for a subtree), its
    name as stored, and its object name in lowercase hex."""

    mode: int
    name: bytes
    sha: str


def commit_fingerprint(body: bytes) -> str | None:
    """Design 2.1's fingerprint of a commit object's content: sha256 over
    the header lines that start with ``author `` and ``committer `` (in
    their order, joined by a newline), a blank line and the message, as
    the first 32 hex digits. These are the prototype's bytes
    (``proto/registry.py``), so witnesses recorded by either agree. None
    unless exactly two such lines precede the first blank line."""
    head, _, message = body.partition(b"\n\n")
    kept = [
        line
        for line in head.split(b"\n")
        if line.startswith((b"author ", b"committer "))
    ]
    if len(kept) != 2:
        return None
    return hashlib.sha256(b"\n".join(kept) + b"\n\n" + message).hexdigest()[:32]


class ObjectReader:
    """Reads one repository's objects from its files (design 3.1).

    `git_dir` is the repository's common git directory: the ``.git``
    directory of a primary checkout, the repository itself when bare, and
    for a linked worktree the directory its ``commondir`` file names, which
    the caller resolves. Objects are read from its ``objects`` directory and
    the alternates that lists; its ``config`` gives the object format.

    The reader is meant to be short-lived: the pack indexes it loads stay
    in memory for its lifetime, and nothing is kept beyond it. Object names
    are hex strings of the repository's length in either case; answers name
    objects in lowercase.
    """

    def __init__(
        self, git_dir: str | bytes | os.PathLike[str] | os.PathLike[bytes]
    ) -> None:
        self._git_dir = os.fsencode(git_dir)
        self._loaded = False
        self._hash_len: int | None = None
        self._dirs: tuple[bytes, ...] = ()
        self._packs: list[_PackIndex] = []
        self._listed: set[bytes] = set()
        self._unreadable = False
        self._evicted: dict[bytes, None] = {}

    # -- the public questions ---------------------------------------------

    @property
    def object_dirs(self) -> tuple[bytes, ...]:
        """The object directories read: the repository's own, then its
        alternates in the order git links them. Empty when the repository
        has no ``objects`` directory."""
        self._load()
        return self._dirs

    @property
    def complete(self) -> bool:
        """Whether an absent answer is final as far as the files go. False
        when the object format is not one the reader knows, the
        ``objects`` directory is missing, a pack index beside its pack could
        not be read, or an evicted file was passed over; the caller then
        asks git. It reflects what the reader has opened so far."""
        self._load()
        return (
            self._hash_len is not None
            and bool(self._dirs)
            and not self._unreadable
            and not self._evicted
        )

    @property
    def evicted(self) -> tuple[bytes, ...]:
        """Files passed over because a provider has evicted their content
        (design 3.1, D19), in the order met."""
        return tuple(self._evicted)

    def has(self, sha: str) -> bool:
        """Whether the object named `sha` is in a pack index or a loose file
        of the object directories (git's existence check: the content is
        not read)."""
        return bool(self.has_many((sha,)))

    def has_many(self, shas: Iterable[str]) -> set[str]:
        """The names in `shas` the object directories hold, in lowercase.
        One pass over the loaded pack indexes answers the whole set; the
        rest are looked up as loose files (one directory listing per fanout
        directory where several share it), and what is still missing is
        looked up in pack indexes that appeared since the reader listed
        them, as git rereads its pack directory on a miss
        (``reprepare_packed_git``). Names that are not hex of the
        repository's length are left out."""
        want: dict[bytes, str] = {}
        for sha in shas:
            raw = self._raw(sha)
            if raw is not None:
                want[raw] = sha.lower()
        found: set[str] = set()
        if want:
            _take(self._packs, want, found)
        if want:
            self._take_loose(want, found)
        if want:
            _take(self._rescan(), want, found)
        return found

    @property
    def name_length(self) -> int | None:
        """The length in hex of a full object name in this repository (40
        for SHA-1, 64 for SHA-256); None when the object format is not
        one the reader knows."""
        self._load()
        return None if self._hash_len is None else 2 * self._hash_len

    def resolve(self, prefix: str) -> str | None:
        """The full name, in lowercase, of the one object whose name starts
        with `prefix` (git's abbreviated object names: at least
        `MIN_ABBREV` hex characters, at most a full name), searched in
        every pack index through its fanout and in the loose fanout
        directory of every object directory, alternates included, then in
        pack indexes that appeared since the reader listed them. None when
        no object matches or more than one does (git's "ambiguous"), and
        for a prefix that is not hex of a length git accepts. A full name
        answers as `has` does."""
        self._load()
        length = self.name_length
        if (
            length is None
            or not MIN_ABBREV <= len(prefix) <= length
            or not _HEX.fullmatch(prefix)
        ):
            return None
        want = prefix.lower()
        if len(want) == length:
            return want if self.has(want) else None
        found: set[bytes] = set()
        for pack in self._packs:
            found.update(pack.starting(want, limit=2))
            if len(found) > 1:
                return None
        fan, rest = want[:2].encode(), want[2:].encode()
        width = length - 2
        for objdir in self._dirs:
            try:
                names = os.listdir(os.path.join(objdir, fan))
            except OSError:
                continue
            for name in names:
                if (
                    len(name) == width
                    and name.startswith(rest)
                    and _HEX_BYTES.fullmatch(name)
                ):
                    found.add(bytes.fromhex((fan + name).decode("ascii")))
                    if len(found) > 1:
                        return None
        for pack in self._rescan():
            found.update(pack.starting(want, limit=2))
            if len(found) > 1:
                return None
        return found.pop().hex() if found else None

    def header(self, sha: str) -> tuple[str, int] | None:
        """The object's type and size from its headers alone: a loose
        object's first inflated bytes, a packed object's entry header, and
        for a delta the target size at the head of its instructions and the
        type of the base its chain ends at. None when the object is absent
        or its headers cannot be read. No size cap applies: no content is
        inflated."""
        found = self._object(sha, cap=None)
        return None if found is None else (found[0], found[1])

    def read(
        self, sha: str, *, max_size: int = _MAX_OBJECT_BYTES
    ) -> tuple[str, bytes] | None:
        """The object's type and content, with every delta applied. None
        when the object is absent, any file on the way is cut short or
        damaged, or a size its headers claim exceeds `max_size`: the
        object's own, or for a packed object that of any entry on its delta
        chain (every delta's instructions and target, and the base). For a
        packed object the sum of those sizes, the bytes the read would
        inflate and build, must also stay within `_WORK_FACTOR` times
        `max_size`. Both are checked from the headers before any content is
        inflated, and the walk stops at the first entry that breaks either.
        The cap is a defence against a hostile repository (a planted pack, a
        zlib bomb claiming gigabytes for one commit, a long chain of entries
        each under the cap); a caller that needs a larger object passes a
        larger `max_size`, which raises both limits."""
        found = self._object(sha, cap=max_size)
        return None if found is None else (found[0], found[2])

    def commit(self, sha: str, *, max_size: int = _MAX_OBJECT_BYTES) -> Commit | None:
        """The commit's header fields and message. None when the object is
        absent, not a commit, above `max_size` (as `read`), or has no
        well-formed ``tree`` line first or a malformed ``parent`` line
        after it (git's ``parse_commit_buffer`` rejects both)."""
        found = self.read(sha, max_size=max_size)
        if found is None or found[0] != "commit" or self._hash_len is None:
            return None
        return _parse_commit(found[1], self._hash_len)

    def tree(
        self, sha: str, *, max_size: int = _MAX_OBJECT_BYTES
    ) -> list[TreeEntry] | None:
        """The tree's entries in stored order. None when the object is
        absent, not a tree, above `max_size` (as `read`), or an entry is
        malformed (a mode that is not octal, an empty name, a name cut
        short)."""
        found = self.read(sha, max_size=max_size)
        if found is None or found[0] != "tree" or self._hash_len is None:
            return None
        return _parse_tree(found[1], self._hash_len)

    def fingerprint(self, sha: str, *, max_size: int = _MAX_OBJECT_BYTES) -> str | None:
        """`commit_fingerprint` of the commit `sha` (design 2.1). None when
        the object is absent, not a commit or above `max_size` (as `read`),
        or the fingerprint is undefined for it."""
        found = self.read(sha, max_size=max_size)
        if found is None or found[0] != "commit":
            return None
        return commit_fingerprint(found[1])

    # -- loading ------------------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._hash_len = self._object_format()
        self._dirs = self._object_dirs()
        if self._hash_len is not None:
            self._packs = self._list_new()

    def _object_format(self) -> int | None:
        """The length of an object name in bytes: 32 where the config sets
        ``extensions.objectFormat`` to sha256, 20 for sha1 or no setting or
        no config file (git's default), None for any other value or a
        config that was evicted."""
        path = os.path.join(self._git_dir, b"config")
        text = self._read_whole(path)
        if text is None:
            return None if path in self._evicted else 20
        return {b"sha1": 20, b"sha256": 32}.get(_config_object_format(text))

    def _object_dirs(self) -> tuple[bytes, ...]:
        main = os.path.join(self._git_dir, b"objects")
        if not os.path.isdir(main):
            return ()
        out = [main]
        self._link_alternates(main, 0, out, {_dir_key(main)})
        return tuple(out)

    def _link_alternates(
        self, objdir: bytes, depth: int, out: list[bytes], seen: set[bytes]
    ) -> None:
        """Appends the directories `objdir`'s ``info/alternates`` names, each
        followed at once by its own, as git's ``link_alt_odb_entries`` does:
        a relative entry is resolved against `objdir`'s real path, an entry
        that is not a directory or is already listed is passed over (the
        cycle guard), and a file deeper than git's nesting limit is not
        read."""
        if depth > _ALT_DEPTH:
            return
        text = self._read_whole(os.path.join(objdir, b"info", b"alternates"))
        if text is None:
            return
        try:
            base = os.path.realpath(objdir)
        except OSError:
            return
        for entry in _alternate_entries(text):
            path = os.path.normpath(
                entry if os.path.isabs(entry) else os.path.join(base, entry)
            )
            key = _dir_key(path)
            if key in seen or not os.path.isdir(path):
                continue
            seen.add(key)
            out.append(path)
            self._link_alternates(path, depth + 1, out, seen)

    def _list_new(self) -> list[_PackIndex]:
        """The pack indexes in the object directories not listed before,
        each read whole. One beside a missing pack is passed over, as git's
        ``add_packed_git`` passes over it; one that cannot be read or parsed
        leaves the reader incomplete."""
        hash_len = self._hash_len
        if hash_len is None:
            return []
        new: list[_PackIndex] = []
        for objdir in self._dirs:
            pack_dir = os.path.join(objdir, b"pack")
            try:
                names = sorted(os.listdir(pack_dir))
            except OSError:
                continue
            for name in names:
                if not name.endswith(b".idx"):
                    continue
                idx_path = os.path.join(pack_dir, name)
                if idx_path in self._listed:
                    continue
                self._listed.add(idx_path)
                pack_path = idx_path[: -len(b".idx")] + b".pack"
                if not os.path.isfile(pack_path):
                    continue
                data = self._read_whole(idx_path)
                index = (
                    None if data is None else _parse_index(data, pack_path, hash_len)
                )
                if index is None:
                    self._unreadable = True
                else:
                    new.append(index)
        return new

    def _rescan(self) -> list[_PackIndex]:
        new = self._list_new()
        self._packs.extend(new)
        return new

    # -- files ----------------------------------------------------------------

    def _open(self, path: bytes) -> FileIO | None:
        """The regular file at `path`, opened for unbuffered reading, or None.
        The open does not wait on a FIFO. An evicted file is not opened:
        opening it would start a fetch (design 3.1); it is recorded in
        `evicted` instead."""
        try:
            before = os.stat(path)
        except OSError:
            return None
        if not stat.S_ISREG(before.st_mode):
            return None
        if _is_evicted(before):
            self._evicted[path] = None
            return None
        try:
            fd = os.open(path, _OPEN_FLAGS)
        except OSError:
            return None
        try:
            regular = stat.S_ISREG(os.fstat(fd).st_mode)
        except OSError:
            regular = False
        if not regular:
            os.close(fd)
            return None
        return os.fdopen(fd, "rb", buffering=0)

    def _read_whole(self, path: bytes) -> bytes | None:
        fh = self._open(path)
        if fh is None:
            return None
        with fh:
            try:
                return fh.readall()
            except OSError:
                return None

    # -- objects ----------------------------------------------------------------

    def _raw(self, sha: str) -> bytes | None:
        self._load()
        hash_len = self._hash_len
        if hash_len is None or len(sha) != 2 * hash_len or not _HEX.fullmatch(sha):
            return None
        return bytes.fromhex(sha)

    def _places(self, raw: bytes) -> Iterator[tuple[_PackIndex, int] | bytes]:
        """Where the object named `raw` may be read, in git's order
        (``oid_object_info_extended``): the loaded pack indexes that list
        it, its loose path in each object directory, then pack indexes that
        appeared since the last listing, for an object another process has
        just packed and pruned."""
        yield from _listed_in(self._packs, raw)
        hexsha = raw.hex().encode()
        for objdir in self._dirs:
            yield os.path.join(objdir, hexsha[:2], hexsha[2:])
        yield from _listed_in(self._rescan(), raw)

    def _object(self, sha: str, *, cap: int | None) -> tuple[str, int, bytes] | None:
        """(type, size, content) of the object `sha`; with `cap` None only
        its headers are read and the content is b"", otherwise it is read
        whole under that cap."""
        raw = self._raw(sha)
        if raw is None:
            return None
        for place in self._places(raw):
            if isinstance(place, bytes):
                found = self._loose(place, cap=cap)
            else:
                found = self._packed(place[0], place[1], cap=cap)
            if found is not None:
                return found
        return None

    def _take_loose(self, want: dict[bytes, str], found: set[str]) -> None:
        """Moves the names in `want` that exist as loose files into
        `found`. A fanout directory several names share is listed once;
        a name alone in its directory is checked by its path."""
        groups: dict[bytes, list[bytes]] = {}
        for raw, sha in want.items():
            groups.setdefault(sha[:2].encode(), []).append(raw)
        for objdir in self._dirs:
            for prefix, raws in groups.items():
                live = [raw for raw in raws if raw in want]
                if not live:
                    continue
                fan = os.path.join(objdir, prefix)
                names: set[bytes] | None = None
                if len(live) > 1:
                    try:
                        names = set(os.listdir(fan))
                    except OSError:
                        continue
                for raw in live:
                    rest = want[raw][2:].encode()
                    present = (
                        rest in names
                        if names is not None
                        else os.path.isfile(os.path.join(fan, rest))
                    )
                    if present:
                        found.add(want.pop(raw))

    def _loose(self, path: bytes, *, cap: int | None) -> tuple[str, int, bytes] | None:
        """(type, size, content) of the loose object at `path`; with `cap`
        None only the header is inflated and the content is b"", otherwise
        the content is inflated only where the header claims at most
        `cap` bytes."""
        fh = self._open(path)
        if fh is None:
            return None
        with fh:
            try:
                stream = _Stream(fh, _HEAD_CHUNK if cap is None else _BODY_CHUNK)
                head = stream.read(_LOOSE_HEAD)
                parsed = _loose_header(head)
                if parsed is None:
                    return None
                kind, size, start = parsed
                if cap is None:
                    return kind, size, b""
                if size > cap:
                    return None
                body = head[start:]
                if len(body) > size:
                    return None
                rest = stream.read_to_end(size - len(body))
            except (OSError, zlib.error):
                return None
        if rest is None:
            return None
        return kind, size, body + rest

    def _packed(
        self, pack: _PackIndex, offset: int, *, cap: int | None
    ) -> tuple[str, int, bytes] | None:
        """(type, size, content) of the pack entry at `offset`. The delta
        chain is walked first by its entry headers alone (`_chain`); with
        `cap` None the type and size come from that walk and the content is
        b"". Otherwise the walk has checked every size on the chain against
        `cap` and their sum against the work limit, and the base is inflated
        and each delta inflated and applied in turn, from the base up, so a
        read holds no more than the current object, one delta and the
        result."""
        fh = self._open(pack.pack)
        if fh is None:
            return None
        with fh:
            try:
                walked = _chain(fh, pack, offset, cap=cap)
                if walked is None:
                    return None
                code, links = walked
                kind = _TYPE_NAMES[code]
                if cap is None:
                    top = links[0]
                    return kind, top.size if top.target is None else top.target, b""
                data = _inflate_at(fh, links[-1])
                for link in reversed(links[:-1]):
                    if data is None:
                        return None
                    delta = _inflate_at(fh, link)
                    data = None if delta is None else _apply_delta(data, delta)
            except (OSError, zlib.error):
                return None
        if data is None:
            return None
        return kind, len(data), data


class _PackIndex:
    """One pack index, version 2, held in memory, beside its pack."""

    __slots__ = (
        "_data",
        "_fanout",
        "_large_at",
        "_large_count",
        "_offsets_at",
        "hash_len",
        "pack",
    )

    def __init__(
        self,
        data: bytes,
        fanout: tuple[int, ...],
        pack: bytes,
        hash_len: int,
        offsets_at: int,
        large_at: int,
        large_count: int,
    ) -> None:
        self._data = data
        self._fanout = fanout
        self.pack = pack
        self.hash_len = hash_len
        self._offsets_at = offsets_at
        self._large_at = large_at
        self._large_count = large_count

    def find(self, raw: bytes) -> int | None:
        """The position of the object named `raw` in the sorted names, by
        the fanout table and a binary search; None when not listed."""
        first = raw[0]
        lo = self._fanout[first - 1] if first else 0
        hi = self._fanout[first]
        data, width = self._data, self.hash_len
        while lo < hi:
            mid = (lo + hi) >> 1
            start = _IDX_NAMES_AT + mid * width
            name = data[start : start + width]
            if name == raw:
                return mid
            if name < raw:
                lo = mid + 1
            else:
                hi = mid
        return None

    def starting(self, prefix: str, *, limit: int) -> list[bytes]:
        """Up to `limit` names listed here whose hex starts with `prefix`
        (lowercase hex, at least two characters), in order: the fanout
        bounds the first byte, a binary search finds the first name not
        below the prefix, and the names from there are read while they
        match."""
        first = int(prefix[:2], 16)
        lo = self._fanout[first - 1] if first else 0
        hi = self._fanout[first]
        low = bytes.fromhex(prefix if len(prefix) % 2 == 0 else prefix + "0")
        data, width = self._data, self.hash_len
        while lo < hi:
            mid = (lo + hi) >> 1
            start = _IDX_NAMES_AT + mid * width
            if data[start : start + width] < low:
                lo = mid + 1
            else:
                hi = mid
        out: list[bytes] = []
        end = self._fanout[first]
        position = lo
        while position < end and len(out) < limit:
            start = _IDX_NAMES_AT + position * width
            name = data[start : start + width]
            if not name.hex().startswith(prefix):
                break
            out.append(name)
            position += 1
        return out

    def offset(self, position: int) -> int | None:
        """The pack offset of the object at `position`: the 31-bit table, or
        the 64-bit table where the high bit is set. None for a 64-bit index
        past the table."""
        (value,) = struct.unpack_from(">I", self._data, self._offsets_at + 4 * position)
        if not value & _LARGE_OFFSET:
            return int(value)
        index = value & ~_LARGE_OFFSET
        if index >= self._large_count:
            return None
        (large,) = struct.unpack_from(">Q", self._data, self._large_at + 8 * index)
        return int(large)


def _parse_index(data: bytes, pack: bytes, hash_len: int) -> _PackIndex | None:
    """A version 2 pack index whose size is one git accepts
    (``check_packed_git_idx``: the fixed tables, both trailing checksums,
    and a whole number of 64-bit offsets, at most one per object but the
    first) and whose fanout never decreases. None otherwise."""
    if len(data) < _IDX_NAMES_AT or data[:4] != _IDX_MAGIC or data[4:8] != _IDX_VERSION:
        return None
    fanout = struct.unpack_from(">256I", data, 8)
    if any(a > b for a, b in zip(fanout, fanout[1:], strict=False)):
        return None
    count = fanout[255]
    offsets_at = _IDX_NAMES_AT + count * (hash_len + 4)
    large_at = offsets_at + 4 * count
    min_size = large_at + 2 * hash_len
    max_size = min_size + 8 * max(count - 1, 0)
    if not min_size <= len(data) <= max_size or (len(data) - min_size) % 8:
        return None
    large_count = (len(data) - min_size) // 8
    return _PackIndex(data, fanout, pack, hash_len, offsets_at, large_at, large_count)


def _listed_in(
    packs: Iterable[_PackIndex], raw: bytes
) -> Iterator[tuple[_PackIndex, int]]:
    for pack in packs:
        position = pack.find(raw)
        if position is not None:
            located = pack.offset(position)
            if located is not None:
                yield pack, located


def _take(packs: Iterable[_PackIndex], want: dict[bytes, str], found: set[str]) -> None:
    """Moves the names in `want` that `packs` list into `found`."""
    for pack in packs:
        hits = [raw for raw in want if pack.find(raw) is not None]
        for raw in hits:
            found.add(want.pop(raw))
        if not want:
            return


class _Link(NamedTuple):
    """One entry of a delta chain: where its zlib stream starts, its
    inflated size (a delta's instructions, or the base object), and for a
    delta the target size its instructions state, where read."""

    start: int
    size: int
    target: int | None


def _chain(
    fh: FileIO, pack: _PackIndex, offset: int, *, cap: int | None
) -> tuple[int, list[_Link]] | None:
    """The type code of the base the chain from `offset` ends at, and its
    entries from `offset` down to that base, read from entry headers and
    delta heads alone. An OFS_DELTA base is found by its distance back, a
    REF_DELTA base by its name in the same pack (git looks nowhere else,
    ``get_delta_base``). With `cap` None a delta's target size is read for
    the first entry only. With a cap it is read for every delta, and the
    walk stops with None at the first entry whose size or target exceeds
    the cap, or that brings the running total of sizes and targets past
    `_WORK_FACTOR` times the cap. None also for a damaged header, a base
    that is not there, a cycle or a chain longer than git writes."""
    links: list[_Link] = []
    seen: set[int] = set()
    spent = 0
    while True:
        if offset in seen or len(seen) >= _MAX_CHAIN:
            return None
        seen.add(offset)
        fh.seek(offset)
        entry = _entry_header(fh.read(_ENTRY_PEEK) or b"", pack.hash_len)
        if entry is None:
            return None
        code, size, base, length = entry
        start = offset + length
        target = None
        if base is not None and (cap is not None or not links):
            fh.seek(start)
            sizes = _delta_sizes(_Stream(fh, _HEAD_CHUNK).read(_DELTA_HEAD))
            if sizes is None:
                return None
            target = sizes[1]
        if cap is not None:
            spent += size + (target or 0)
            if size > cap or (target or 0) > cap or spent > _WORK_FACTOR * cap:
                return None
        links.append(_Link(start, size, target))
        if base is None:
            return code, links
        if isinstance(base, int):
            if not 0 < base < offset:
                return None
            offset -= base
        else:
            position = pack.find(base)
            located = None if position is None else pack.offset(position)
            if located is None:
                return None
            offset = located


def _inflate_at(fh: FileIO, link: _Link) -> bytes | None:
    """The whole inflated stream of one chain entry, of exactly its size."""
    fh.seek(link.start)
    chunk = min(link.size + _CHUNK_MARGIN, _BODY_CHUNK)
    return _Stream(fh, chunk).read_to_end(link.size)


class _Stream:
    """A zlib stream inflated on demand from a file, fed `chunk` compressed
    bytes at a time, so a header read stops after the first few chunks."""

    __slots__ = ("_chunk", "_fh", "_pending", "_z")

    def __init__(self, fh: FileIO, chunk: int) -> None:
        self._fh = fh
        self._chunk = chunk
        self._z = zlib.decompressobj()
        self._pending = b""

    def read(self, n: int) -> bytes:
        """Up to `n` inflated bytes: fewer only where the stream ends or the
        file does."""
        out = bytearray()
        while len(out) < n and not self._z.eof:
            fed = self._pending or self._fh.read(self._chunk) or b""
            piece = self._z.decompress(fed, min(n - len(out), _PIECE))
            self._pending = self._z.unconsumed_tail
            if not piece and len(self._pending) == len(fed):
                break
            out += piece
        return bytes(out)

    def read_to_end(self, n: int) -> bytes | None:
        """Exactly `n` inflated bytes that end the stream; None for a stream
        that is shorter, longer or cut off before its end (git's loose and
        packed readers reject all three)."""
        data = self.read(n)
        if len(data) != n or self.read(1) or not self._z.eof:
            return None
        return data


def _is_evicted(st: os.stat_result) -> bool:
    flags = getattr(st, "st_flags", 0) or 0
    attributes = getattr(st, "st_file_attributes", 0) or 0
    return bool(flags & _SF_DATALESS or attributes & _RECALL_ON_DATA_ACCESS)


def _dir_key(path: bytes) -> bytes:
    try:
        return os.path.normcase(os.path.realpath(path))
    except OSError:
        return os.path.normcase(path)


def _config_object_format(text: bytes) -> bytes:
    """The value of ``extensions.objectFormat`` in a git config file,
    lowercased, the last one set winning; b"sha1" when unset."""
    section = b""
    value = b"sha1"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith(b"["):
            close = line.find(b"]")
            if close < 0:
                continue
            section = line[1:close].strip().lower()
            line = line[close + 1 :].strip()
        if section != b"extensions" or not line or line[:1] in (b"#", b";"):
            continue
        key, equals, rest = line.partition(b"=")
        if equals and key.strip().lower() == b"objectformat":
            value = re.split(rb"[#;]", rest, maxsplit=1)[0].strip().strip(b'"').lower()
    return value


def _alternate_entries(text: bytes) -> list[bytes]:
    """The entries of an alternates file, parsed as git's
    ``parse_alt_odb_entry`` parses them: one per line, a line starting with
    ``#`` a comment, a line starting with a double quote C-quoted when the
    quoting is well formed (the entry ends at the closing quote and the byte
    after it is skipped), otherwise taken as written; empty entries are
    dropped."""
    out: list[bytes] = []
    i, n = 0, len(text)
    while i < n:
        quoted = _unquote_c(text, i) if text[i] == ord('"') else None
        if quoted is not None:
            entry, end = quoted
        else:
            end = text.find(b"\n", i)
            if end < 0:
                end = n
            entry = b"" if text[i] == ord("#") else text[i:end]
        if entry:
            out.append(entry)
        i = end + 1
    return out


def _unquote_c(text: bytes, start: int) -> tuple[bytes, int] | None:
    """The C-quoted string opening at `text[start]` and the index just past
    its closing quote, as git's ``unquote_c_style`` reads it; None when the
    quoting is malformed or never closes."""
    out = bytearray()
    i, n = start + 1, len(text)
    while i < n:
        c = text[i]
        i += 1
        if c == ord('"'):
            return bytes(out), i
        if c != ord("\\"):
            out.append(c)
            continue
        if i >= n:
            return None
        c = text[i]
        i += 1
        if c in _C_ESCAPES:
            out.append(_C_ESCAPES[c])
        elif ord("0") <= c <= ord("3") and _OCTAL_PAIR.fullmatch(text[i : i + 2]):
            out.append(int(text[i - 1 : i + 2], 8))
            i += 2
        else:
            return None
    return None


def _loose_header(head: bytes) -> tuple[str, int, int] | None:
    """(type, size, content start) from a loose object's first inflated
    bytes. The size must be canonical decimal, as git's
    ``parse_loose_header`` requires."""
    nul = head.find(b"\x00")
    if nul < 0:
        return None
    kind, space, digits = head[:nul].partition(b" ")
    name = _LOOSE_TYPES.get(kind)
    if name is None or not space or not digits.isdigit():
        return None
    if len(digits) > 1 and digits.startswith(b"0"):
        return None
    return name, int(digits), nul + 1


def _varint(data: bytes, i: int) -> tuple[int, int] | None:
    """A little-endian base-128 value at `data[i]` and the index after it."""
    value = shift = 0
    while i < len(data):
        c = data[i]
        i += 1
        value |= (c & 0x7F) << shift
        shift += 7
        if not c & 0x80:
            return value, i
    return None


def _entry_header(
    buf: bytes, hash_len: int
) -> tuple[int, int, int | bytes | None, int] | None:
    """(type code, size, base, header length) of a pack entry from its first
    bytes. The base is None for a whole object, the distance back for an
    OFS_DELTA, the base's name for a REF_DELTA."""
    if not buf:
        return None
    c = buf[0]
    code, size, shift, i = (c >> 4) & 7, c & 15, 4, 1
    while c & 0x80:
        if i >= len(buf):
            return None
        c = buf[i]
        i += 1
        size |= (c & 0x7F) << shift
        shift += 7
    if code in _TYPE_NAMES:
        return code, size, None, i
    if code == _OFS_DELTA:
        if i >= len(buf):
            return None
        c = buf[i]
        i += 1
        distance = c & 0x7F
        while c & 0x80:
            if i >= len(buf):
                return None
            c = buf[i]
            i += 1
            distance = ((distance + 1) << 7) | (c & 0x7F)
        return code, size, distance, i
    if code == _REF_DELTA and i + hash_len <= len(buf):
        return code, size, buf[i : i + hash_len], i + hash_len
    return None


def _delta_sizes(delta: bytes) -> tuple[int, int, int] | None:
    """(source size, target size, index of the first instruction) of a
    delta."""
    source = _varint(delta, 0)
    if source is None:
        return None
    target = _varint(delta, source[1])
    if target is None:
        return None
    return source[0], target[0], target[1]


def _apply_delta(base: bytes, delta: bytes) -> bytes | None:
    """`delta`'s instructions applied to `base`, checked as git's
    ``patch_delta`` checks them: the source size must be the base's, a copy
    must lie inside the base, opcode 0 is reserved, and the result must
    have the target size. None for any failure."""
    sizes = _delta_sizes(delta)
    if sizes is None or sizes[0] != len(base):
        return None
    _, target, i = sizes
    view = memoryview(base)
    out = bytearray()
    n = len(delta)
    while i < n:
        c = delta[i]
        i += 1
        if c & 0x80:
            offset = size = 0
            for bit in range(4):
                if c & (1 << bit):
                    if i >= n:
                        return None
                    offset |= delta[i] << (8 * bit)
                    i += 1
            for bit in range(3):
                if c & (0x10 << bit):
                    if i >= n:
                        return None
                    size |= delta[i] << (8 * bit)
                    i += 1
            size = size or 0x10000
            if offset + size > len(base):
                return None
            out += view[offset : offset + size]
        elif c:
            if i + c > n:
                return None
            out += delta[i : i + c]
            i += c
        else:
            return None
        if len(out) > target:
            return None
    return bytes(out) if len(out) == target else None


def _hex_name(value: bytes, hash_len: int) -> str | None:
    if len(value) != 2 * hash_len or not _HEX_BYTES.fullmatch(value):
        return None
    return value.decode("ascii").lower()


def _parse_commit(body: bytes, hash_len: int) -> Commit | None:
    head, _, message = body.partition(b"\n\n")
    lines = head.split(b"\n")
    if not lines[0].startswith(b"tree "):
        return None
    tree = _hex_name(lines[0][5:], hash_len)
    if tree is None:
        return None
    parents: list[str] = []
    i = 1
    while i < len(lines) and lines[i].startswith(b"parent "):
        parent = _hex_name(lines[i][7:], hash_len)
        if parent is None:
            return None
        parents.append(parent)
        i += 1
    author: bytes | None = None
    committer: bytes | None = None
    for line in lines[i:]:
        if author is None and line.startswith(b"author "):
            author = line[7:]
        elif committer is None and line.startswith(b"committer "):
            committer = line[10:]
    return Commit(tree, tuple(parents), author or b"", committer or b"", message)


def _parse_tree(body: bytes, hash_len: int) -> list[TreeEntry] | None:
    entries: list[TreeEntry] = []
    i, n = 0, len(body)
    while i < n:
        space = body.find(b" ", i)
        if space < 0:
            return None
        nul = body.find(b"\x00", space + 1)
        if nul < 0 or nul == space + 1 or nul + 1 + hash_len > n:
            return None
        mode = body[i:space]
        if not _MODE.fullmatch(mode):
            return None
        entries.append(
            TreeEntry(
                int(mode, 8),
                body[space + 1 : nul],
                body[nul + 1 : nul + 1 + hash_len].hex(),
            )
        )
        i = nul + 1 + hash_len
    return entries
