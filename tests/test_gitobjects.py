"""Tests for gitobjects.py: git objects read from a repository's files.

Every test builds repositories with git under ``tmp_path`` and holds the
reader to git's own answers: `read` and `header` to ``git cat-file
--batch`` on every object, `commit` to ``git rev-list --parents`` and
``git log``, `tree` to ``git ls-tree``, `has_many` to ``git cat-file
--batch-check`` and ``git rev-list --objects``, and `fingerprint` to design
2.1's formula, computed here from git's bytes. Where the reader answers
"absent" for a damaged file, the test shows git refuses it too, or that the
content cannot be read from what is left.
"""

from __future__ import annotations

import collections
import hashlib
import os
import random
import shutil
import stat
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from bettermemory import gitobjects
from bettermemory.gitobjects import Commit, ObjectReader, TreeEntry

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_OFS_DELTA = 6
_REF_DELTA = 7
# Bound through getattr: the name is absent from the Windows stubs.
_MKFIFO = getattr(os, "mkfifo", None)

Objects = dict[str, tuple[str, bytes]]


@pytest.fixture(autouse=True)
def neutral_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A neutral git, the shape of experiments/lab.py: no user or system
    configuration, a fixed author, committer and date, no inherited
    repository variables, and automatic gc and maintenance off so no
    background repack changes the packs a test reads."""
    config = tmp_path / "gitconfig"
    config.write_text("[gc]\n\tauto = 0\n[maintenance]\n\tauto = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_INDEX_FILE",
        "GIT_CEILING_DIRECTORIES",
    ):
        monkeypatch.delenv(name, raising=False)
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Fixture")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "fixture@example.invalid")
        monkeypatch.setenv(f"GIT_{role}_DATE", "@1000000000 +0000")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=cwd, input=stdin, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout


def _git_ok(cwd: Path, *args: str) -> bool:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)
    return result.returncode == 0


def _sha(cwd: Path, *args: str, stdin: bytes | None = None) -> str:
    return _git(cwd, *args, stdin=stdin).decode().strip()


def _init(path: Path, *extra: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "--template=", "-b", "main", *extra)
    return path


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _sha(repo, "rev-parse", "HEAD")


def _blob(repo: Path, content: bytes) -> str:
    return _sha(repo, "hash-object", "-w", "--stdin", stdin=content)


def _history(repo: Path, *init_args: str) -> Path:
    """A repository with every object type and edits that give the packer
    delta candidates: a text file edited in every commit, a binary file, an
    empty file, a nested directory, a side branch merged back, and an
    annotated tag."""
    _init(repo, *init_args)
    rng = random.Random(7)
    lines = [f"line {i} {rng.random()}\n" for i in range(400)]
    (repo / "notes.txt").write_text("".join(lines))
    (repo / "data").mkdir()
    (repo / "data" / "blob.bin").write_bytes(rng.randbytes(3000))
    (repo / "data" / "empty").write_bytes(b"")
    _commit_all(repo, "first")
    for i in range(5):
        lines[i * 50] = f"edited {i}\n"
        lines.append(f"appended {i}\n")
        (repo / "notes.txt").write_text("".join(lines))
        _commit_all(repo, f"edit {i}\n\nA body line for edit {i}.")
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "data" / "side.txt").write_text("side\n" * 100)
    _commit_all(repo, "side")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    _git(repo, "tag", "-a", "-m", "a tag", "v1")
    return repo


def _common_dir(repo: Path) -> Path:
    out = _sha(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(out)


def _git_objects(repo: Path) -> Objects:
    """Every object git sees from `repo`, its alternates' included, by
    ``git cat-file --batch-all-objects --batch``: name to type and content."""
    out = _git(repo, "cat-file", "--batch-all-objects", "--batch")
    objects: Objects = {}
    i = 0
    while i < len(out):
        end = out.index(b"\n", i)
        sha, kind, size = out[i:end].decode().split()
        start = end + 1
        objects[sha] = (kind, out[start : start + int(size)])
        i = start + int(size) + 1
    return objects


def _ls_tree(repo: Path, tree: str) -> list[TreeEntry]:
    entries: list[TreeEntry] = []
    for item in _git(repo, "ls-tree", "-z", tree).split(b"\x00")[:-1]:
        meta, name = item.split(b"\t", 1)
        mode, _kind, sha = meta.split(b" ")
        entries.append(TreeEntry(int(mode, 8), name, sha.decode()))
    return entries


def _formula(body: bytes) -> str:
    """Design 2.1's fingerprint by its documented formula, from git's bytes:
    sha256 over the author line, a newline, the committer line, a blank line
    and the message, as the first 32 hex digits."""
    header, _, message = body.partition(b"\n\n")
    lines = header.split(b"\n")
    author = next(line for line in lines if line.startswith(b"author "))
    committer = next(line for line in lines if line.startswith(b"committer "))
    digest = hashlib.sha256(author + b"\n" + committer + b"\n\n" + message)
    return digest.hexdigest()[:32]


def _assert_agrees_with_git(repo: Path) -> Objects:
    """The reader on `repo`'s common directory gives git's answer for every
    object git sees, and is complete. Returns git's objects."""
    objects = _git_objects(repo)
    assert objects
    reader = ObjectReader(_common_dir(repo))
    for sha, (kind, body) in objects.items():
        assert reader.header(sha) == (kind, len(body)), sha
        assert reader.read(sha) == (kind, body), sha
    assert reader.has_many(objects) == set(objects)
    listed = _git(repo, "rev-list", "--all", "--objects").decode().splitlines()
    reachable = {line.split()[0] for line in listed}
    assert reader.has_many(reachable) == reachable
    log = _git(repo, "log", "--all", "--format=%H %T").decode().splitlines()
    trees = dict(line.split() for line in log)
    for line in _git(repo, "rev-list", "--all", "--parents").decode().splitlines():
        sha, *parents = line.split()
        commit = reader.commit(sha)
        assert commit is not None
        assert commit.tree == trees[sha]
        assert commit.parents == tuple(parents)
        assert reader.fingerprint(sha) == _formula(objects[sha][1])
    for sha, (kind, _) in objects.items():
        if kind == "tree":
            assert reader.tree(sha) == _ls_tree(repo, sha)
    assert reader.complete
    assert reader.evicted == ()
    return objects


def _loose_count(repo: Path) -> int:
    for line in _git(repo, "count-objects", "-v").decode().splitlines():
        key, _, value = line.partition(": ")
        if key == "count":
            return int(value)
    raise AssertionError("count-objects printed no count")


def _pack_entry_types(repo: Path) -> dict[str, collections.Counter[int]]:
    """Per pack of `repo`, how many entries carry each type code, read from
    the pack at the offsets ``git show-index`` lists (6 is OFS_DELTA, 7 is
    REF_DELTA)."""
    found: dict[str, collections.Counter[int]] = {}
    for idx in sorted((_common_dir(repo) / "objects" / "pack").glob("*.idx")):
        listing = _git(repo, "show-index", stdin=idx.read_bytes()).decode()
        counts: collections.Counter[int] = collections.Counter()
        with idx.with_suffix(".pack").open("rb") as fh:
            for line in listing.splitlines():
                fh.seek(int(line.split()[0]))
                counts[(fh.read(1)[0] >> 4) & 7] += 1
        found[idx.name] = counts
    return found


def _only_index(repo: Path) -> Path:
    (idx,) = (repo / ".git" / "objects" / "pack").glob("*.idx")
    return idx


def _overwrite(path: Path, data: bytes) -> None:
    """A new file holding `data` at `path`, in place of the read-only one
    git wrote. The old file is removed, never opened for writing: an SMB
    mount keeps the DOS read-only attribute whatever chmod asks, and
    refuses the open, while it removes the file. Windows removes a
    read-only file only once it is writable again."""
    if sys.platform == "win32":
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    path.unlink()
    path.write_bytes(data)


def _truncate(path: Path, size: int) -> None:
    _overwrite(path, path.read_bytes()[:size])


def _write_alternates(repo: Path, entries: list[bytes]) -> None:
    info = repo / ".git" / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "alternates").write_bytes(b"".join(entry + b"\n" for entry in entries))


def _c_quote(path: bytes) -> bytes:
    """`path` C-quoted with every separator as an octal escape, the form
    git's ``unquote_c_style`` reads."""
    escaped = path.replace(b"\\", b"\\\\").replace(b"/", b"\\057")
    return b'"' + escaped + b'"'


# ---------------------------------------------------------------------------
# Loose and packed objects against git
# ---------------------------------------------------------------------------


def test_loose_objects_agree_with_git(tmp_path: Path) -> None:
    repo = _history(tmp_path / "repo")
    assert not list((repo / ".git" / "objects" / "pack").glob("*.idx"))
    objects = _assert_agrees_with_git(repo)
    assert {kind for kind, _ in objects.values()} == {"commit", "tree", "blob", "tag"}


@pytest.mark.parametrize(
    ("pack_args", "delta_type"),
    [
        (("gc", "-q"), _OFS_DELTA),
        (("repack", "-a", "-d", "-f", "-q"), _OFS_DELTA),
        (
            ("-c", "repack.useDeltaBaseOffset=false", "repack", "-a", "-d", "-f", "-q"),
            _REF_DELTA,
        ),
    ],
    ids=["gc", "repack-ofs-delta", "repack-ref-delta"],
)
def test_packed_objects_agree_with_git(
    tmp_path: Path, pack_args: tuple[str, ...], delta_type: int
) -> None:
    repo = _history(tmp_path / "repo")
    _git(repo, *pack_args)
    assert _loose_count(repo) == 0
    assert any(counts[delta_type] for counts in _pack_entry_types(repo).values())
    _assert_agrees_with_git(repo)


def test_thin_fetched_pack_holds_both_delta_types(tmp_path: Path) -> None:
    """A fetch kept as a pack (``fetch.unpackLimit=1``) is a thin pack that
    ``index-pack --fix-thin`` completed: deltas between new objects are
    OFS_DELTA, deltas against the bases it appended are REF_DELTA, in one
    pack."""
    source = _history(tmp_path / "source")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--template=", "--no-local", str(source), str(clone))
    text = (source / "notes.txt").read_text()
    for i in range(3):
        text += f"later {i}\n"
        (source / "notes.txt").write_text(text)
        _commit_all(source, f"later {i}")
    _git(clone, "-c", "fetch.unpackLimit=1", "fetch", "-q", "origin")
    types = _pack_entry_types(clone)
    assert any(c[_OFS_DELTA] and c[_REF_DELTA] for c in types.values()), types
    _assert_agrees_with_git(clone)


def test_large_offset_table_is_read(tmp_path: Path) -> None:
    """``index-pack --index-version=2,<offset>`` moves every object past the
    offset into the 64-bit table, the way a pack over 2 GiB stores them."""
    repo = _history(tmp_path / "repo")
    _git(repo, "repack", "-a", "-d", "-q")
    idx = _only_index(repo)
    forced = tmp_path / "forced.idx"
    _git(
        repo,
        "index-pack",
        "--index-version=2,32",
        "-o",
        str(forced),
        str(idx.with_suffix(".pack")),
    )
    data = forced.read_bytes()
    (count,) = struct.unpack_from(">I", data, 8 + 255 * 4)
    offsets = struct.unpack_from(f">{count}I", data, 8 + 1024 + count * 24)
    assert sum(1 for value in offsets if value & 0x80000000) > 0
    _overwrite(idx, data)
    _assert_agrees_with_git(repo)


def test_sha256_repository_agrees_with_git(tmp_path: Path) -> None:
    probe = subprocess.run(
        ["git", "init", "-q", "--object-format=sha256", str(tmp_path / "probe")],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("this git has no sha256 object format")
    repo = _history(tmp_path / "repo", "--object-format=sha256")
    _assert_agrees_with_git(repo)
    _git(repo, "gc", "-q")
    _assert_agrees_with_git(repo)
    head = _sha(repo, "rev-parse", "HEAD")
    assert len(head) == 64
    assert not ObjectReader(repo / ".git").has(head[:40])


def test_tree_names_stay_bytes(tmp_path: Path) -> None:
    repo = _init(tmp_path / "repo")
    blob = _blob(repo, b"content\n")
    listing = (
        f"100644 blob {blob}\t".encode()
        + b"caf\xe9 latin-1\x00"
        + f"100755 blob {blob}\t".encode()
        + b"run\x00"
    )
    tree = _sha(repo, "mktree", "-z", stdin=listing)
    entries = ObjectReader(repo / ".git").tree(tree)
    assert entries == _ls_tree(repo, tree)
    assert entries is not None
    assert [entry.name for entry in entries] == [b"caf\xe9 latin-1", b"run"]
    assert [entry.mode for entry in entries] == [0o100644, 0o100755]


# ---------------------------------------------------------------------------
# Header-only reads
# ---------------------------------------------------------------------------


def test_header_of_a_large_blob_reads_only_its_head(tmp_path: Path) -> None:
    """A blob whose compressed stream spans many chunks: its header reads
    from the first bytes of the loose file and of the pack, and still
    reads with everything after the first 4 KiB cut off, where the content
    no longer can."""
    repo = _init(tmp_path / "repo")
    content = random.Random(11).randbytes(1 << 20)
    (repo / "big.bin").write_bytes(content)
    _commit_all(repo, "big")
    sha = _sha(repo, "rev-parse", "HEAD:big.bin")
    loose = repo / ".git" / "objects" / sha[:2] / sha[2:]
    assert loose.stat().st_size > gitobjects._BODY_CHUNK
    assert ObjectReader(repo / ".git").header(sha) == ("blob", len(content))

    pack = _git(repo, "pack-objects", "-q", "--stdout", stdin=f"{sha}\n".encode())
    other = _init(tmp_path / "other")
    _git(other, "index-pack", "--stdin", stdin=pack)
    reader = ObjectReader(other / ".git")
    assert reader.read(sha) == ("blob", content)
    (packfile,) = (other / ".git" / "objects" / "pack").glob("*.pack")
    _truncate(packfile, 4096)
    reader = ObjectReader(other / ".git")
    assert reader.header(sha) == ("blob", len(content))
    assert reader.read(sha) is None

    _truncate(loose, 4096)
    reader = ObjectReader(repo / ".git")
    assert reader.header(sha) == ("blob", len(content))
    assert reader.read(sha) is None
    assert reader.has(sha)


def test_header_of_a_delta_gives_its_target_size(tmp_path: Path) -> None:
    repo = _init(tmp_path / "repo")
    rng = random.Random(13)
    first = rng.randbytes(1 << 18)
    second = first[:1000] + b"inserted" + first[1000:] + rng.randbytes(5000)
    (repo / "big.bin").write_bytes(first)
    _commit_all(repo, "first")
    (repo / "big.bin").write_bytes(second)
    _commit_all(repo, "second")
    _git(repo, "repack", "-a", "-d", "-f", "-q")
    old = _sha(repo, "rev-parse", "HEAD~1:big.bin")
    new = _sha(repo, "rev-parse", "HEAD:big.bin")
    verify = _git(repo, "verify-pack", "-v", str(_only_index(repo))).decode()
    based = {
        f[0] for f in (line.split() for line in verify.splitlines()) if len(f) == 7
    }
    assert based & {old, new}
    reader = ObjectReader(repo / ".git")
    assert reader.header(old) == ("blob", len(first))
    assert reader.header(new) == ("blob", len(second))
    assert reader.read(old) == ("blob", first)
    assert reader.read(new) == ("blob", second)


# ---------------------------------------------------------------------------
# The size cap on reads
# ---------------------------------------------------------------------------


def test_read_declines_a_blob_claiming_more_than_the_cap(tmp_path: Path) -> None:
    """A blob one byte above the default cap, of repeated bytes so its
    stream is small (the shape of a zlib bomb), loose and then packed:
    `read` declines it in both forms while `header` and `has` still answer,
    and a caller passing a larger cap reads it."""
    cap = gitobjects._MAX_OBJECT_BYTES
    content = b"a" * (cap + 1)
    repo = _init(tmp_path / "repo")
    sha = _blob(repo, content)
    pack = _git(repo, "pack-objects", "-q", "--stdout", stdin=f"{sha}\n".encode())
    other = _init(tmp_path / "other")
    _git(other, "index-pack", "--stdin", stdin=pack)
    assert list((other / ".git" / "objects" / "pack").glob("*.pack"))
    for git_dir in (repo / ".git", other / ".git"):
        reader = ObjectReader(git_dir)
        assert reader.read(sha) is None
        assert reader.header(sha) == ("blob", cap + 1)
        assert reader.has(sha)
        assert reader.read(sha, max_size=cap + 1) == ("blob", content)


@pytest.mark.parametrize("packed", [False, True], ids=["loose", "packed"])
def test_commit_and_tree_below_the_cap_read(tmp_path: Path, packed: bool) -> None:
    """Commits and trees read under the default cap, and under a cap equal
    to their size; one byte less declines them through `read`, `commit`,
    `tree` and `fingerprint` alike."""
    repo = _history(tmp_path / "repo")
    if packed:
        _git(repo, "gc", "-q")
    head = _sha(repo, "rev-parse", "HEAD")
    tree = _sha(repo, "rev-parse", "HEAD^{tree}")
    raw_commit = _git(repo, "cat-file", "commit", head)
    raw_tree = _git(repo, "cat-file", "tree", tree)
    reader = ObjectReader(repo / ".git")
    assert reader.commit(head) is not None
    assert reader.fingerprint(head) == _formula(raw_commit)
    assert reader.tree(tree) == _ls_tree(repo, tree)
    assert reader.read(head, max_size=len(raw_commit)) == ("commit", raw_commit)
    assert reader.read(tree, max_size=len(raw_tree)) == ("tree", raw_tree)
    below = len(raw_commit) - 1
    assert reader.read(head, max_size=below) is None
    assert reader.commit(head, max_size=below) is None
    assert reader.fingerprint(head, max_size=below) is None
    assert reader.tree(tree, max_size=len(raw_tree) - 1) is None
    assert reader.header(head) == ("commit", len(raw_commit))


def test_delta_whose_base_exceeds_the_cap_reads_none(tmp_path: Path) -> None:
    """A file cut to its first half: the packer stores the shorter version
    as a delta against the longer. Under a cap between the two sizes the
    delta itself fits, its base does not, and `read` declines it; `header`
    still gives its type and target size."""
    repo = _init(tmp_path / "repo")
    large = random.Random(17).randbytes(200 << 10)
    small = large[: 100 << 10]
    (repo / "file.bin").write_bytes(large)
    _commit_all(repo, "large")
    (repo / "file.bin").write_bytes(small)
    _commit_all(repo, "small")
    _git(repo, "repack", "-a", "-d", "-f", "-q")
    large_sha = _sha(repo, "rev-parse", "HEAD~1:file.bin")
    small_sha = _sha(repo, "rev-parse", "HEAD:file.bin")
    verify = _git(repo, "verify-pack", "-v", str(_only_index(repo))).decode()
    bases = {
        f[0]: f[6]
        for f in (line.split() for line in verify.splitlines())
        if len(f) == 7
    }
    assert bases.get(small_sha) == large_sha
    cap = 150 << 10
    reader = ObjectReader(repo / ".git")
    assert reader.read(small_sha, max_size=cap) is None
    assert reader.read(large_sha, max_size=cap) is None
    assert reader.header(small_sha) == ("blob", len(small))
    assert reader.header(large_sha) == ("blob", len(large))
    assert reader.read(small_sha) == ("blob", small)
    assert reader.read(small_sha, max_size=len(large)) == ("blob", small)


def _pack_varint(value: int) -> bytes:
    """A delta size: little-endian base 128."""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if not value:
            out.append(byte)
            return bytes(out)
        out.append(byte | 0x80)


def _pack_entry_header(code: int, size: int) -> bytes:
    """A pack entry's type and size header, as git's pack writer encodes it."""
    out = bytearray()
    byte = (code << 4) | (size & 0x0F)
    size >>= 4
    while size:
        out.append(byte | 0x80)
        byte = size & 0x7F
        size >>= 7
    out.append(byte)
    return bytes(out)


def _pack_distance(distance: int) -> bytes:
    """An OFS_DELTA distance, as git's pack writer encodes it."""
    out = bytearray([distance & 0x7F])
    distance >>= 7
    while distance:
        distance -= 1
        out.insert(0, 0x80 | (distance & 0x7F))
        distance >>= 7
    return bytes(out)


def test_chain_past_the_work_limit_reads_none(tmp_path: Path) -> None:
    """A pack written here and indexed by git: a 64-byte blob and a chain
    of 40 OFS_DELTA entries, each changing one byte of the one before.
    Under a cap of 64 bytes every entry fits, but the deepest object costs
    the base, every delta's instructions and every result, past
    `_WORK_FACTOR` times the cap, so `read` declines it; a shallow object
    of the same chain reads, and so does the deepest under a larger cap."""
    size = 64
    versions = [b"a" * size]
    deltas: list[bytes] = []
    for k in range(1, 41):
        before = versions[-1]
        versions.append(before[: k - 1] + b"b" + before[k:])
        copy_head = bytes([0x91, 0, k - 1]) if k > 1 else b""
        copy_tail = bytes([0x91, k, size - k])
        deltas.append(
            _pack_varint(size) + _pack_varint(size) + copy_head + b"\x01b" + copy_tail
        )
    pack = bytearray(b"PACK" + struct.pack(">II", 2, len(versions)))
    offsets = [len(pack)]
    pack += _pack_entry_header(3, size) + zlib.compress(versions[0])
    for delta in deltas:
        offsets.append(len(pack))
        distance = _pack_distance(offsets[-1] - offsets[-2])
        pack += _pack_entry_header(_OFS_DELTA, len(delta)) + distance
        pack += zlib.compress(delta)
    pack += hashlib.sha1(bytes(pack)).digest()
    repo = _init(tmp_path / "repo")
    _git(repo, "index-pack", "--stdin", stdin=bytes(pack))
    names = [hashlib.sha1(b"blob 64\x00" + v).hexdigest() for v in versions]
    assert _git(repo, "cat-file", "blob", names[-1]) == versions[-1]

    def work(depth: int) -> int:
        return size + sum(len(delta) + size for delta in deltas[:depth])

    limit = gitobjects._WORK_FACTOR * size
    assert max(len(delta) for delta in deltas) < size
    assert work(40) > limit
    assert work(5) <= limit
    reader = ObjectReader(repo / ".git")
    assert reader.read(names[-1], max_size=size) is None
    assert reader.header(names[-1]) == ("blob", size)
    assert reader.read(names[5], max_size=size) == ("blob", versions[5])
    assert reader.read(names[-1], max_size=work(40)) == ("blob", versions[-1])
    assert reader.read(names[-1]) == ("blob", versions[-1])


# ---------------------------------------------------------------------------
# Alternates
# ---------------------------------------------------------------------------


def test_alternates_chain_of_two_levels(tmp_path: Path) -> None:
    """C borrows from B by a relative, C-quoted entry beside a comment, an
    empty line and a missing directory; B borrows from A by an absolute
    path. The reader sees every object git sees from C, and a cycle back
    from A to C changes nothing."""
    a = _history(tmp_path / "a")
    _git(a, "gc", "-q")
    a_loose = _blob(a, b"only in a, loose\n")
    a_head = _sha(a, "rev-parse", "HEAD")

    b = _init(tmp_path / "b")
    a_objects = a / ".git" / "objects"
    _write_alternates(b, [os.fsencode(a_objects)])
    b_commit = _sha(b, "commit-tree", f"{a_head}^{{tree}}", "-p", a_head, "-m", "in b")
    _git(b, "update-ref", "refs/heads/main", b_commit)
    b_blob = _blob(b, b"only in b\n")

    c = _init(tmp_path / "c")
    b_objects = b / ".git" / "objects"
    c_objects = c / ".git" / "objects"
    relative = os.fsencode(os.path.relpath(b_objects, c_objects))
    missing = os.fsencode(tmp_path / "missing" / "objects")
    _write_alternates(c, [b"# a comment", b"", missing, _c_quote(relative)])
    c_commit = _sha(
        c, "commit-tree", f"{b_commit}^{{tree}}", "-p", b_commit, "-m", "in c"
    )
    _git(c, "update-ref", "refs/heads/main", c_commit)
    c_blob = _blob(c, b"only in c\n")

    reader = ObjectReader(c / ".git")
    real = [os.path.realpath(d) for d in reader.object_dirs]
    expected = [
        os.path.realpath(os.fsencode(d)) for d in (c_objects, b_objects, a_objects)
    ]
    assert real == expected
    assert reader.has_many([a_loose, a_head, b_blob, c_blob, c_commit]) == {
        a_loose,
        a_head,
        b_blob,
        c_blob,
        c_commit,
    }
    objects = _assert_agrees_with_git(c)
    assert {a_loose, b_blob, c_blob} <= set(objects)

    _write_alternates(a, [os.fsencode(c_objects)])
    reader = ObjectReader(c / ".git")
    assert [os.path.realpath(d) for d in reader.object_dirs] == expected
    from_a = ObjectReader(a / ".git")
    assert len(from_a.object_dirs) == 3
    assert from_a.has(c_blob) and _git_ok(a, "cat-file", "-e", c_blob)
    _assert_agrees_with_git(c)


def test_alternates_nesting_stops_where_gits_does(tmp_path: Path) -> None:
    repos = [_init(tmp_path / f"r{i}") for i in range(8)]
    blobs = [_blob(repo, f"level {i}\n".encode()) for i, repo in enumerate(repos)]
    for outer, inner in zip(repos, repos[1:], strict=False):
        _write_alternates(outer, [os.fsencode(inner / ".git" / "objects")])
    seen_by_git = [_git_ok(repos[0], "cat-file", "-e", sha) for sha in blobs]
    assert not all(seen_by_git)
    reader = ObjectReader(repos[0] / ".git")
    assert [reader.has(sha) for sha in blobs] == seen_by_git
    assert len(reader.object_dirs) == seen_by_git.count(True)


@pytest.mark.parametrize(
    ("text", "entries"),
    [
        (b"/a\n/b\n", [b"/a", b"/b"]),
        (b"# comment\n\n/a", [b"/a"]),
        (b'"/x\\tq\\"\\\\\\101"\n', [b'/x\tq"\\A']),
        (b'"ab"Xcd\n', [b"ab", b"cd"]),
        (b'"never closed\n', [b'"never closed']),
        (b'"bad \\q escape"\n', [b'"bad \\q escape"']),
        (b'"bad \\19 octal"\n', [b'"bad \\19 octal"']),
        (b'"ends in \\', [b'"ends in \\']),
        (b"  /spaced \n", [b"  /spaced "]),
    ],
)
def test_alternates_entries_parse_as_git_parses_them(
    text: bytes, entries: list[bytes]
) -> None:
    assert gitobjects._alternate_entries(text) == entries


# ---------------------------------------------------------------------------
# Absent, damaged and unreadable
# ---------------------------------------------------------------------------


def test_missing_object_and_malformed_names_read_as_absent(tmp_path: Path) -> None:
    repo = _history(tmp_path / "repo")
    head = _sha(repo, "rev-parse", "HEAD")
    blob = _sha(repo, "rev-parse", "HEAD:notes.txt")
    reader = ObjectReader(repo / ".git")
    missing = hashlib.sha1(b"never stored").hexdigest()
    assert not _git_ok(repo, "cat-file", "-e", missing)
    names = [
        missing,
        "",
        "xyz",
        head[:-1],
        head + "0",
        head[:38] + "zz",
        " " + head[1:],
    ]
    for name in names:
        assert not reader.has(name)
        assert reader.header(name) is None
        assert reader.read(name) is None
        assert reader.commit(name) is None
        assert reader.tree(name) is None
        assert reader.fingerprint(name) is None
    assert reader.has(head.upper())
    assert reader.has_many([head.upper(), head]) == {head}
    assert reader.commit(blob) is None
    assert reader.tree(blob) is None
    assert reader.fingerprint(blob) is None
    assert reader.complete


@pytest.mark.parametrize("keep", ["nothing", "magic", "fanout", "half", "all-but-one"])
def test_truncated_pack_index_reads_as_absent(tmp_path: Path, keep: str) -> None:
    repo = _history(tmp_path / "repo")
    _git(repo, "gc", "-q")
    loose = _blob(repo, b"loose after gc\n")
    head = _sha(repo, "rev-parse", "HEAD")
    idx = _only_index(repo)
    size = idx.stat().st_size
    cut = {"nothing": 0, "magic": 6, "fanout": 1040, "half": size // 2}
    _truncate(idx, cut.get(keep, size - 1))
    assert not _git_ok(repo, "cat-file", "-e", head)
    reader = ObjectReader(repo / ".git")
    assert not reader.has(head)
    assert reader.header(head) is None
    assert reader.read(head) is None
    assert reader.commit(head) is None
    assert reader.has(loose)
    assert reader.read(loose) == ("blob", b"loose after gc\n")
    assert not reader.complete


def test_wrong_index_version_reads_as_absent(tmp_path: Path) -> None:
    repo = _history(tmp_path / "repo")
    _git(repo, "gc", "-q")
    head = _sha(repo, "rev-parse", "HEAD")
    idx = _only_index(repo)
    data = idx.read_bytes()
    _overwrite(idx, data[:4] + b"\x00\x00\x00\x03" + data[8:])
    reader = ObjectReader(repo / ".git")
    assert not reader.has(head)
    assert not reader.complete


def test_index_beside_a_missing_pack_is_passed_over(tmp_path: Path) -> None:
    """Git lists a pack only where its ``.pack`` exists (``add_packed_git``);
    the reader passes over the index the same way and stays complete."""
    repo = _history(tmp_path / "repo")
    _git(repo, "gc", "-q")
    head = _sha(repo, "rev-parse", "HEAD")
    idx = _only_index(repo)
    idx.with_suffix(".pack").rename(tmp_path / "moved.pack")
    assert not _git_ok(repo, "cat-file", "-e", head)
    reader = ObjectReader(repo / ".git")
    assert not reader.has(head)
    assert reader.complete


def test_damaged_pack_reads_as_absent_never_raises(tmp_path: Path) -> None:
    repo = _history(tmp_path / "repo")
    _git(repo, "repack", "-a", "-d", "-q")
    objects = _git_objects(repo)
    pack = _only_index(repo).with_suffix(".pack")
    data = bytearray(pack.read_bytes())
    middle = len(data) // 2
    data[middle : middle + 64] = b"\xff" * 64
    _overwrite(pack, bytes(data))
    reader = ObjectReader(repo / ".git")
    results = {sha: reader.read(sha) for sha in objects}
    headers = {sha: reader.header(sha) for sha in objects}
    assert all(results[sha] in (objects[sha], None) for sha in objects)
    assert any(found is None for found in results.values())
    assert all(
        headers[sha] in ((kind, len(body)), None)
        for sha, (kind, body) in objects.items()
    )

    _truncate(pack, middle)
    reader = ObjectReader(repo / ".git")
    results = {sha: reader.read(sha) for sha in objects}
    assert all(results[sha] in (objects[sha], None) for sha in objects)
    assert any(found is None for found in results.values())


@pytest.mark.parametrize(
    ("stored", "header"),
    [
        (b"", None),
        (b"not a zlib stream", None),
        (zlib.compress(b"blob 3\x00abc")[:-4], ("blob", 3)),
        (zlib.compress(b"blob 3\x00abc")[:9], None),
        (zlib.compress(b"blob 3\x00abcd"), ("blob", 3)),
        (zlib.compress(b"blob 4\x00abc"), ("blob", 4)),
        (zlib.compress(b"blob 03\x00abc"), None),
        (zlib.compress(b"blob x\x00abc"), None),
        (zlib.compress(b"blob\x00abc"), None),
        (zlib.compress(b"thing 3\x00abc"), None),
        (zlib.compress(b"blob " + b"9" * 40 + b"\x00abc"), None),
    ],
    ids=[
        "empty",
        "not-zlib",
        "no-checksum",
        "cut-short",
        "longer-than-declared",
        "shorter-than-declared",
        "size-not-canonical",
        "size-not-a-number",
        "no-size",
        "unknown-type",
        "header-too-long",
    ],
)
def test_damaged_loose_object_reads_as_absent(
    tmp_path: Path, stored: bytes, header: tuple[str, int] | None
) -> None:
    repo = _init(tmp_path / "repo")
    sha = _blob(repo, b"abc")
    _overwrite(repo / ".git" / "objects" / sha[:2] / sha[2:], stored)
    reader = ObjectReader(repo / ".git")
    assert reader.has(sha)
    assert reader.header(sha) == header
    assert reader.read(sha) is None


@pytest.mark.skipif(_MKFIFO is None, reason="FIFOs are POSIX")
def test_fifo_in_place_of_an_object_is_not_waited_on(local_tmp_path: Path) -> None:
    """The repository on local disk: the reader's answer to a FIFO is the
    point, and an SMB mount makes none (EOPNOTSUPP)."""
    repo = _init(local_tmp_path / "repo")
    sha = _blob(repo, b"abc")
    path = repo / ".git" / "objects" / sha[:2] / sha[2:]
    path.unlink()
    assert _MKFIFO is not None
    _MKFIFO(path)
    reader = ObjectReader(repo / ".git")
    assert reader.read(sha) is None
    assert reader.header(sha) is None


def test_unknown_object_format_leaves_the_reader_incomplete(tmp_path: Path) -> None:
    repo = _history(tmp_path / "repo")
    head = _sha(repo, "rev-parse", "HEAD")
    config = repo / ".git" / "config"
    config.write_bytes(config.read_bytes() + b"[extensions]\n\tobjectFormat = sha512\n")
    reader = ObjectReader(repo / ".git")
    assert not reader.has(head)
    assert reader.read(head) is None
    assert not reader.complete


@pytest.mark.parametrize(
    ("text", "value"),
    [
        (b"", b"sha1"),
        (b"[core]\n\tbare = false\n", b"sha1"),
        (b"[extensions]\n\tobjectformat = sha256\n", b"sha256"),
        (b'[Extensions]\n\tObjectFormat = "SHA256" ; comment\n', b"sha256"),
        (b"[extensions] objectFormat = sha256\n", b"sha256"),
        (b"[core]\n\tobjectformat = sha256\n", b"sha1"),
        (b"[extensions]\n# objectformat = sha256\n", b"sha1"),
        (b"[extensions\n\tobjectformat = sha256\n", b"sha1"),
        (b"[extensions]\n\tobjectformat = sha256\n\tobjectformat = sha1\n", b"sha1"),
    ],
)
def test_object_format_read_from_config(text: bytes, value: bytes) -> None:
    assert gitobjects._config_object_format(text) == value


def test_directory_without_objects_is_incomplete(tmp_path: Path) -> None:
    reader = ObjectReader(tmp_path / "not-a-repository")
    name = hashlib.sha1(b"anything").hexdigest()
    assert reader.object_dirs == ()
    assert reader.has_many([name]) == set()
    assert reader.read(name) is None
    assert not reader.complete


@pytest.mark.skipif(
    not (
        hasattr(os.stat_result, "st_flags")
        or hasattr(os.stat_result, "st_file_attributes")
    ),
    reason="no eviction flag in this platform's stat",
)
def test_evicted_index_is_listed_not_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pack index a provider has evicted (SF_DATALESS on macOS, recall on
    data access on Windows) is not opened (design 3.1): the reader lists it
    and is incomplete, so the caller can fetch it within its budget."""
    repo = _history(tmp_path / "repo")
    _git(repo, "gc", "-q")
    head = _sha(repo, "rev-parse", "HEAD")
    target = os.fsencode(_only_index(repo))
    real_stat = os.stat

    def evicted_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        result = real_stat(path, *args, **kwargs)  # type: ignore[arg-type]
        if isinstance(path, (str, bytes)) and os.fsencode(path) == target:
            flags = {
                name: value
                for name, value in (
                    ("st_flags", 0x40000000),
                    ("st_file_attributes", 0x00400000),
                )
                if hasattr(os.stat_result, name)
            }
            return os.stat_result(tuple(result), flags)
        return result

    monkeypatch.setattr(os, "stat", evicted_stat)
    reader = ObjectReader(repo / ".git")
    assert not reader.has(head)
    assert reader.evicted == (target,)
    assert not reader.complete


# ---------------------------------------------------------------------------
# The batch and the pack directory reread
# ---------------------------------------------------------------------------


def test_has_many_answers_a_mixed_set(tmp_path: Path) -> None:
    """Packed, loose (many sharing a fanout directory), borrowed through an
    alternate, missing, malformed and upper-case names in one batch: the
    reader returns the set ``git cat-file --batch-check`` finds."""
    base = _history(tmp_path / "base")
    _git(base, "gc", "-q")
    repo = _init(tmp_path / "repo")
    _write_alternates(repo, [os.fsencode(base / ".git" / "objects")])
    packed = list(_git_objects(base))
    sources = tmp_path / "sources"
    sources.mkdir()
    paths = []
    for i in range(300):
        path = sources / f"{i}.txt"
        path.write_bytes(f"loose {i}\n".encode())
        paths.append(str(path))
    # The paths on stdin: 300 of them overrun a Windows command line.
    loose = (
        _git(
            repo,
            "hash-object",
            "-w",
            "--stdin-paths",
            stdin="\n".join(paths).encode() + b"\n",
        )
        .decode()
        .split()
    )
    missing = [hashlib.sha1(f"missing {i}".encode()).hexdigest() for i in range(300)]
    malformed = ["", "xyz", loose[0][:-1], loose[0] + "0", "g" * 40]
    upper = [name.upper() for name in loose[:5] + packed[:5]]
    query = packed + loose + missing + malformed + upper
    out = _git(
        repo,
        "cat-file",
        "--batch-check=%(objectname)",
        stdin="\n".join(query).encode() + b"\n",
    )
    by_git = {
        line for line in out.decode().splitlines() if not line.endswith(" missing")
    }
    reader = ObjectReader(repo / ".git")
    assert reader.has_many(query) == by_git
    assert by_git == set(packed) | set(loose)
    assert reader.has_many([]) == set()


def test_objects_packed_after_loading_are_found(tmp_path: Path) -> None:
    """Git rereads its pack directory on a miss (``reprepare_packed_git``)
    for an object another process has just packed and pruned; so does the
    reader, for `read` and for `has_many`."""
    repo = _history(tmp_path / "repo")
    _git(repo, "gc", "-q")
    head = _sha(repo, "rev-parse", "HEAD")
    readers = [ObjectReader(repo / ".git"), ObjectReader(repo / ".git")]
    assert all(reader.has(head) for reader in readers)
    (repo / "later.txt").write_text("later\n")
    later = _commit_all(repo, "later")
    _git(repo, "repack", "-d", "-q")
    assert _loose_count(repo) == 0
    found = readers[0].read(later)
    assert found is not None and found[0] == "commit"
    assert readers[1].has_many([later, head]) == {later, head}
    assert all(reader.complete for reader in readers)


# ---------------------------------------------------------------------------
# Commits and fingerprints
# ---------------------------------------------------------------------------


def test_fingerprint_follows_the_documented_formula(tmp_path: Path) -> None:
    """The fingerprint of design 2.1 for commits git wrote, a merge, and a
    commit written by hand with an encoding header, a continued header,
    author bytes outside UTF-8 and a Latin-1 message; `commit` returns the
    fields as stored."""
    repo = _history(tmp_path / "repo")
    head = _sha(repo, "rev-parse", "HEAD")
    tree = _sha(repo, "rev-parse", "HEAD^{tree}")
    author = b"Fixture \xe9 <fixture@example.invalid> 1000000000 +0000"
    committer = b"Fixture <fixture@example.invalid> 1000000000 +0000"
    body = (
        f"tree {tree}\nparent {head}\n".encode()
        + b"author "
        + author
        + b"\ncommitter "
        + committer
        + b"\nencoding ISO-8859-1\nx-extra first\n second line\n"
        + b"\nLatin-1 message \xe9\n"
    )
    crafted = _sha(repo, "hash-object", "-t", "commit", "-w", "--stdin", stdin=body)
    empty_message = _sha(
        repo,
        "hash-object",
        "-t",
        "commit",
        "-w",
        "--stdin",
        stdin=f"tree {tree}\n".encode()
        + b"author "
        + author
        + b"\ncommitter "
        + committer
        + b"\n\n",
    )
    reader = ObjectReader(repo / ".git")
    commits = _git(repo, "rev-list", "--all").decode().split() + [
        crafted,
        empty_message,
    ]
    assert len(commits) > 8
    for sha in commits:
        raw = _git(repo, "cat-file", "commit", sha)
        assert reader.fingerprint(sha) == _formula(raw)
        assert reader.fingerprint(sha) == gitobjects.commit_fingerprint(raw)
    merge = reader.commit(head)
    assert merge is not None and len(merge.parents) == 2
    assert reader.commit(crafted) == Commit(
        tree, (head,), author, committer, b"Latin-1 message \xe9\n"
    )
    found = reader.commit(empty_message)
    assert found is not None and found.message == b"" and found.parents == ()


def test_malformed_commits_and_trees(tmp_path: Path) -> None:
    """Objects git only writes with ``--literally``: `read` returns their
    bytes as git does, `commit` and `tree` decline what git's parsers
    reject, and the fingerprint follows its formula wherever exactly one
    author and one committer line exist."""
    repo = _init(tmp_path / "repo")
    tree = _sha(repo, "mktree", stdin=b"")
    people = b"author A <a@example.invalid> 1 +0000\ncommitter C <c@example.invalid> 1 +0000\n"

    def literal(kind: str, content: bytes) -> str:
        return _sha(
            repo,
            "hash-object",
            "-t",
            kind,
            "--literally",
            "-w",
            "--stdin",
            stdin=content,
        )

    no_tree = literal("commit", people + b"\nmessage\n")
    bad_parent = literal(
        "commit", f"tree {tree}\nparent xyz\n".encode() + people + b"\nm\n"
    )
    bad_tree_line = literal("commit", b"tree xyz\n" + people + b"\nm\n")
    two_authors = literal(
        "commit",
        f"tree {tree}\n".encode() + b"author B <b> 1 +0000\n" + people + b"\nm\n",
    )
    no_author = literal("commit", f"tree {tree}\n\nm\n".encode())
    reader = ObjectReader(repo / ".git")
    for sha in (no_tree, bad_parent, bad_tree_line, two_authors, no_author):
        assert reader.read(sha) == ("commit", _git(repo, "cat-file", "commit", sha))
    assert reader.commit(no_tree) is None
    assert reader.commit(bad_parent) is None
    assert reader.commit(bad_tree_line) is None
    assert reader.fingerprint(no_tree) == _formula(people + b"\nmessage\n")
    assert reader.fingerprint(two_authors) is None
    found = reader.commit(two_authors)
    assert found is not None and found.author == b"B <b> 1 +0000"
    assert reader.commit(no_author) == Commit(tree, (), b"", b"", b"m\n")
    assert reader.fingerprint(no_author) is None

    blob = _blob(repo, b"x")
    raw_blob = bytes.fromhex(blob)
    for body in (
        b"garbage",
        b"100644 \x00" + raw_blob,
        b"10x644 name\x00" + raw_blob,
        b"100644 name\x00" + raw_blob[:5],
        b"100644 name" + raw_blob,
    ):
        sha = literal("tree", body)
        assert reader.read(sha) == ("tree", body)
        assert reader.tree(sha) is None
    assert reader.tree(tree) == []


# ---------------------------------------------------------------------------
# Delta instructions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("delta", "result"),
    [
        (b"\x06\x09\x91\x02\x03\x06XYZXYZ", b"cdeXYZXYZ"),
        (b"\x06\x06\x80", None),
        (b"\x05\x09\x91\x02\x03\x06XYZXYZ", None),
        (b"\x06\x09\x91\x02\x03\x00", None),
        (b"\x06\x09\x91\x05\x03", None),
        (b"\x06\x09\x06XYZ", None),
        (b"\x06\x02\x03XYZ", None),
        (b"\x06\x09\x02XY", None),
        (b"\x06\x09\x91\x02", None),
        (b"\x06\x09\xb1\x02\x03", None),
        (b"", None),
        (b"\x06", None),
    ],
    ids=[
        "copy-then-insert",
        "copy-of-0x10000-past-base",
        "source-size-differs",
        "opcode-zero",
        "copy-past-base",
        "insert-past-end",
        "longer-than-target",
        "shorter-than-target",
        "copy-size-cut-off",
        "copy-size-byte-missing",
        "empty",
        "target-size-missing",
    ],
)
def test_delta_instructions_checked_as_git_checks_them(
    delta: bytes, result: bytes | None
) -> None:
    assert gitobjects._apply_delta(b"abcdef", delta) == result


# ---------------------------------------------------------------------------
# Abbreviated names (`ObjectReader.resolve`, decision B19)
# ---------------------------------------------------------------------------


def _blob_name(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def _twin_contents() -> tuple[bytes, bytes]:
    """Two blob contents whose SHA-1 names share their first four hex
    characters, the first pair met in a fixed sequence."""
    seen: dict[str, bytes] = {}
    i = 0
    while True:
        content = f"twin {i}\n".encode()
        prefix = _blob_name(content)[:4]
        if prefix in seen:
            return seen[prefix], content
        seen[prefix] = content
        i += 1


def _git_names(repo: Path, names: list[str]) -> list[str]:
    """git's answer for each of `names` by ``git cat-file --batch-check``:
    the full name, or ``missing`` or ``ambiguous``."""
    out = _git(
        repo, "cat-file", "--batch-check", stdin=("\n".join(names) + "\n").encode()
    )
    answers = []
    for line in out.decode().splitlines():
        first, kind = line.split()[:2]
        answers.append(kind if kind in ("missing", "ambiguous") else first)
    assert len(answers) == len(names)
    return answers


def _shortest_unique(sha: str, names: Objects) -> str:
    others = [n for n in names if n != sha]
    for length in range(gitobjects.MIN_ABBREV, len(sha) + 1):
        if not any(n.startswith(sha[:length]) for n in others):
            return sha[:length]
    return sha


def test_resolve_names_the_one_loose_object_a_prefix_starts(tmp_path: Path) -> None:
    repo = _init(tmp_path / "repo")
    blob = _blob(repo, b"a loose blob\n")
    assert _loose_count(repo) == 1
    reader = ObjectReader(repo / ".git")
    names = [blob[:4], blob[:7], blob[:7].upper(), blob[:39], blob]
    assert _git_names(repo, names) == [blob] * len(names)
    assert [reader.resolve(n) for n in names] == [blob] * len(names)
    assert reader.complete


def test_resolve_names_packed_objects_as_git_does(tmp_path: Path) -> None:
    repo = _history(tmp_path / "repo")
    _git(repo, "gc", "-q")
    assert _loose_count(repo) == 0
    objects = _git_objects(repo)
    shortest = [_shortest_unique(sha, objects) for sha in objects]
    assert _git_names(repo, shortest) == list(objects)
    reader = ObjectReader(repo / ".git")
    assert [reader.resolve(p) for p in shortest] == list(objects)
    assert [reader.resolve(sha[:12]) for sha in objects] == list(objects)


def test_resolve_of_an_absent_or_malformed_prefix_is_none(tmp_path: Path) -> None:
    repo = _history(tmp_path / "repo")
    _git(repo, "gc", "-q")
    loose = _blob(repo, b"loose beside the pack\n")
    objects = _git_objects(repo)
    absent = next(
        p
        for p in (f"{i:04x}" for i in range(16**4))
        if not any(o.startswith(p) for o in objects)
    )
    assert _git_names(repo, [absent, absent + "abc"]) == ["missing", "missing"]
    reader = ObjectReader(repo / ".git")
    names = [
        absent,
        absent + "abc",
        "",
        "abc",
        loose[:3],
        "zzzz",
        loose + "0",
        " " + loose[:6],
        hashlib.sha1(b"never stored").hexdigest(),
    ]
    assert [reader.resolve(n) for n in names] == [None] * len(names)
    assert reader.resolve(loose[:6]) == loose
    assert reader.complete


def test_resolve_of_a_prefix_two_objects_share_is_none(tmp_path: Path) -> None:
    """Two blobs whose names share their first four hex characters, one
    packed and one loose, then both loose: the shared prefix names no
    object (git: ambiguous), one character more names each."""
    first, second = _twin_contents()
    repo = _init(tmp_path / "repo")
    (repo / "first.txt").write_bytes(first)
    _commit_all(repo, "first")
    _git(repo, "gc", "-q")
    assert _loose_count(repo) == 0
    packed, loose = _blob_name(first), _blob(repo, second)
    shared = os.path.commonprefix([packed, loose])
    assert len(shared) >= gitobjects.MIN_ABBREV
    longer = [packed[: len(shared) + 1], loose[: len(shared) + 1]]
    assert _git_names(repo, [shared, *longer]) == ["ambiguous", packed, loose]
    reader = ObjectReader(repo / ".git")
    assert reader.resolve(shared) is None
    assert [reader.resolve(p) for p in longer] == [packed, loose]
    both = _init(tmp_path / "both-loose")
    assert [_blob(both, first), _blob(both, second)] == [packed, loose]
    assert ObjectReader(both / ".git").resolve(shared) is None
