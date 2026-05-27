"""Filesystem helpers — fsync durability + bounded reads.

Two concerns live here, both narrow:

**Durability (`fsync_file`, `fsync_dir`, `atomic_write_bytes`).** The
store's write path is `write to .tmp → rename to target`. POSIX
guarantees the rename itself is atomic, but the rename only updates
the directory entry; the page cache holding the file's bytes (and the
directory's own metadata) can still be lost on power loss between the
rename returning and the kernel flushing dirty pages. Without an
explicit fsync we can end up with a zero-byte file at the target path
after an ungraceful shutdown — the directory entry says the file
exists, the data backing it never reached disk.

* `fsync_file(fd)` — flush a file's data to disk. Cheap. The caller
  passes the open file descriptor (typically `f.fileno()` after
  `f.flush()` and before `close`).
* `fsync_dir(path)` — flush a directory's metadata so a rename inside
  it is durable. POSIX-only; on Windows you can't `open()` a directory
  for fsync and the OS handles rename durability differently anyway,
  so we no-op there.
* `atomic_write_bytes(path, data, *, mode=None)` — the full
  tmp+fsync+rename+fsync_dir discipline packaged as a one-call helper
  for small files. Two callsites (the user MCP config in `init.py`
  and the sync `.gitignore` in `sync.py`) used `path.write_text(...)`
  pre-3.1.0 and could leave truncated files on power loss; both now
  route through this helper. The canonical `_atomic_write_post`
  (frontmatter Post writer) and `episodes._write_path` keep their
  bespoke implementations for now — queue item #29 lifts them onto
  this primitive in a separate refactor so the well-tested paths
  stay untouched here.

Both fsync helpers swallow `OSError` and return. fsync legitimately
fails on some pseudo-filesystems (`/proc`, certain tmpfs/overlayfs
configs, sandbox mounts) where the operation isn't supported — that's
not a corruption risk, and propagating the error would break the write
surface for test/CI/container environments without buying any
durability they actually have.

**Bounded reads (`bounded_read`, `bounded_tail_read`,
`bounded_stream_read`).** Single point of enforcement for resource
caps on input we don't trust to be small. The 2.6.2 and 2.6.3 releases
fixed three separate unbounded-read defects (consolidate transcript,
hook transcript, llm.py contradiction clustering byte-vs-char trap);
the underlying class is one we keep producing because each call site
re-derives its own `.read(N)` discipline. Centralising it here means
the *next* time someone adds a "read this user-controlled file"
helper, the cap honours bytes (not characters), the error path is
named (`ValueError`, not OOM), and the byte-vs-char trap is impossible
because the helpers open in binary mode.

* `bounded_read(path, max_bytes)` — read a file's full contents in
  binary mode; raise `ValueError` if the file exceeds the cap. Stat-
  checks first so an over-cap file is rejected before the allocation.
  Use for files where over-cap is a malformed-input signal (config,
  frontmatter, JSON payloads).
* `bounded_tail_read(path, max_bytes)` — read up to the trailing
  `max_bytes` of a file; truncate silently and discard the first
  partial line. Falls back to a bounded forward read on unseekable
  streams (FIFOs from `mkfifo`-based fixtures, named pipes). Use for
  append-only logs where the head is uninteresting (transcripts,
  event-log tails).
* `bounded_stream_read(stream, max_bytes)` — read up to `max_bytes`
  from an open binary stream and raise `ValueError` if the stream
  carries more. Detects over-cap by attempting one extra byte after
  the limit. Use for stdin and other unseekable streams the caller
  has already opened.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Generator
from pathlib import Path
from typing import BinaryIO


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


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Atomic, durable write of ``data`` to ``path``.

    Write-to-tmp (same parent dir, so it's on the same filesystem and
    ``os.replace`` is atomic), fsync the file, rename into place, fsync
    the parent directory. The rename is POSIX-atomic for the directory
    entry, but without the file-fsync we can end up with a renamed-but-
    empty file after power loss (the dirent exists, the bytes never
    reached disk); without the dir-fsync the rename itself isn't durable
    past a crash. Both fsyncs are best-effort — see :func:`fsync_file`
    and :func:`fsync_dir` for the platform/filesystem caveats.

    ``mode`` is applied via ``os.chmod`` AFTER the rename if provided.
    This helper is the bypass-callsite primitive (small JSON/config
    files written outside the Store's frontmatter discipline) where
    no-one is racing readers in the rename-to-chmod window — the
    fchmod-before-rename ceremony that ``_atomic_write_post`` and
    ``episodes._write_path`` perform is privacy-critical for memory
    bodies and warrants its own bespoke shape. For the MCP-config and
    ``.gitignore`` callsites that don't carry private bytes,
    chmod-after-rename is the simpler and equally-correct choice.

    The tmp file is created via ``tempfile.NamedTemporaryFile(dir=
    path.parent, delete=False)`` so it lives on the same filesystem as
    the target (required for ``os.replace`` atomicity) and the rename
    can move it without copying. A per-process random suffix means two
    writers racing on the same target serialise on the rename rather
    than on the tmp name itself. On any failure between tmp creation
    and successful rename, the tmp file is unlinked in a ``finally``
    block so a crashed write doesn't accumulate orphan
    ``<path>.<random>.tmp`` files in the parent directory.

    The parent directory is created (``parents=True, exist_ok=True``)
    if missing — bytes-level callers (``init.py``'s ``patch_client_config``,
    ``sync.py``'s ``init``) often write under user-home paths that may
    not exist yet on a fresh install.
    """
    import tempfile

    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    # `delete=False` is required: we rename the tmp to `path` before the
    # context manager closes it; the default `delete=True` would then
    # try to unlink the (now-renamed) original tmp path on close and
    # raise. `prefix=path.name + "."` keeps the tmp visibly associated
    # with the target file in `ls` output, which helps post-crash
    # inspection.
    tmp_file = tempfile.NamedTemporaryFile(
        dir=str(parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(tmp_file.name)
    renamed = False
    try:
        with tmp_file as f:
            f.write(data)
            f.flush()
            fsync_file(f.fileno())
        os.replace(tmp_path, path)
        renamed = True
        if mode is not None:
            # chmod-after-rename: see the docstring rationale. Best-
            # effort — Windows has no POSIX mode bits and some sandbox
            # filesystems reject chmod; that's not a corruption risk.
            with contextlib.suppress(OSError):
                os.chmod(path, mode)
        fsync_dir(parent)
    finally:
        # If we never renamed (write or rename raised), the tmp is an
        # orphan; clean it up so the parent directory doesn't accumulate
        # `<path>.<random>.tmp` files on every failure. Suppressed
        # because the more important error already on the way up the
        # stack shouldn't be masked by a unlink failure.
        if not renamed:
            with contextlib.suppress(OSError):
                tmp_path.unlink()


def bounded_read(path: Path, max_bytes: int) -> bytes:
    """Read a file's full contents in binary mode, capped at ``max_bytes``.

    Raises ``ValueError`` if the file's size exceeds the cap. The stat
    check happens before the allocation so an oversized file is
    rejected without ever loading it. Returns bytes; callers decode
    explicitly (``.decode("utf-8")`` for user-controlled text,
    ``json.loads`` for structured input).

    A stat/open failure propagates as its native ``OSError`` subclass
    (``FileNotFoundError``, ``PermissionError``, …) unchanged — NOT
    re-wrapped into a bare ``OSError``. Callers special-case the
    subclass: ``Store.restore`` / ``Store.rename_scope`` catch
    ``FileNotFoundError`` to turn a vanished-file race into a clean
    ``MemoryNotFoundError``; flattening the subclass would silently
    make those handlers dead code.

    Use for inputs where over-cap is a malformed-input signal —
    frontmatter, config files, JSON payloads. For append-only logs
    where truncation is the right behaviour, use
    :func:`bounded_tail_read` instead.
    """
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"{path}: file size {size} exceeds cap {max_bytes} bytes")
    with path.open("rb") as fh:
        return fh.read(max_bytes)


def bounded_tail_read(path: Path, max_bytes: int) -> bytes:
    """Read up to the trailing ``max_bytes`` of a file.

    Truncates silently — the use case is "I want the tail of an
    append-only log; the head is uninteresting." Returns bytes.
    Discards the first partial line in the chunk: when the seek lands
    mid-line, the bytes before the first newline are dropped so the
    caller never sees half a record. If no newline exists in the read
    window the whole chunk is returned (a single huge unbroken line
    will not have a partial-line discard applied — that's the caller's
    responsibility to handle).

    Falls back to a bounded forward read when the open file can't be
    seeked; the partial-line discard does not apply in that case
    because the read starts at the head. NOTE: the fallback only
    helps once the file is open — opening a FIFO with no writer
    blocks indefinitely, so a caller passing a possibly-non-regular
    path must guard with ``Path.is_file()`` first (see
    ``hook._extract_last_exchange`` and ``consolidate._load_transcript``).
    """
    with path.open("rb") as fh:
        try:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            offset = max(0, size - max_bytes)
            fh.seek(offset)
        except OSError:
            offset = 0
        chunk = fh.read(max_bytes)
    if offset > 0:
        newline = chunk.find(b"\n")
        if newline != -1:
            chunk = chunk[newline + 1 :]
    return chunk


# Module-level guard so the "Windows lock fell back to in-process only"
# warning is emitted at most once per process. Pre-2.7 the Windows
# branch was a silent no-op; that's a real cross-process correctness
# gap (two MCP processes pointed at the same memory directory could
# race writes), so any fallback path is at least visible in the
# logs now.
_FLOCK_WARNED = False


@contextlib.contextmanager
def flock_excl(path: Path) -> Generator[None, None, None]:
    """Cross-process exclusive lock on a sidecar lockfile next to ``path``.

    POSIX path: ``fcntl.flock(fd, LOCK_EX)`` on ``<path>.lock``. The
    lockfile is created (or opened) at ``path.with_suffix(suffix +
    ".lock")`` and held under ``LOCK_EX`` for the duration of the
    ``with`` block. The lockfile is NOT unlinked on release — see the
    2.6.3 audit note: ``flock`` identity is per-inode; unlinking on
    release lets a third opener race in between the holder's
    ``os.open`` and a fresh ``O_CREAT``, ending up with two holders on
    different inodes that both believe they own the lock. Persisting
    the 0-byte lockfile keeps every ``os.open(lock_path, O_CREAT)``
    on the same inode so the flock actually serialises.

    Windows path (audit H3): ``msvcrt.locking(fd, LK_NBLCK, 1)`` on
    the same sidecar lockfile, with a retry-with-exponential-backoff
    loop because ``LK_NBLCK`` is non-blocking and raises ``OSError``
    on contention. Pre-2.7 this branch was a silent ``yield`` no-op
    — two MCP processes on Windows pointed at the same memory
    directory could race writes and corrupt files with no warning.
    ``msvcrt.locking`` is the closest Windows analog: it's a
    cross-process byte-range advisory lock on the file, and locking
    a single byte (offset 0, length 1) gives whole-file mutual
    exclusion in practice for our usage. The non-blocking variant
    plus a retry loop avoids the dead-process-holds-the-lock failure
    mode that the blocking variant exhibits. Default timeout is
    30 seconds (overridable via ``BETTERMEMORY_FLOCK_TIMEOUT``);
    backoff caps at 100ms per sleep.

    If the Windows branch can't load ``msvcrt`` (extremely unusual —
    it ships with CPython) or the lockfile can't be created at all,
    the helper falls back to an in-process-only yield and emits a
    one-shot ``logger.warning`` so the regression is visible in
    operator logs. Pre-2.7 the no-op was permanent and silent.

    The lockfile is created with ``0o600`` mode (POSIX) or default
    Windows mode bits (Windows ignores POSIX bits anyway) so the
    cross-host ``sync push`` posture doesn't leak it as world-readable
    on POSIX hosts.

    This is the SINGLE definition. ``store.py``, ``events.py``, and
    ``sync.py`` all alias to this so a future fix to the locking
    discipline lands in one place and not three — see the 2.6.3
    pattern-generalization audit note.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")

    if sys.platform == "win32":  # pragma: no cover - non-unix in CI
        yield from _flock_windows(lock_path)
        return

    import fcntl

    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _flock_windows(
    lock_path: Path,
) -> Generator[None, None, None]:  # pragma: no cover - non-unix in CI
    """Windows-only exclusive lock helper for ``flock_excl``.

    Yields once the lock is held; releases on context exit. Splits out
    so the POSIX branch in ``flock_excl`` reads naturally and so the
    Windows-specific imports (``msvcrt``) don't pollute module-load
    on POSIX hosts.

    The retry loop spaces attempts with capped exponential backoff so
    short-lived contention doesn't burn CPU and long-held locks don't
    hammer the lockfile. The timeout is intentionally generous (30s
    default) — bettermemory writes are interactive in nature; if
    nothing has progressed in 30 seconds the right behaviour is to
    surface an error to the caller rather than spin indefinitely.
    """
    import logging
    import time

    global _FLOCK_WARNED

    try:
        import msvcrt
    except ImportError:
        if not _FLOCK_WARNED:
            _FLOCK_WARNED = True
            logging.getLogger("bettermemory._fsutil").warning(
                "flock_excl: msvcrt unavailable on this Windows interpreter; "
                "cross-process locking is disabled. Concurrent writers may "
                "corrupt files. Falling back to in-process-only yield."
            )
        yield
        return

    timeout_str = os.environ.get("BETTERMEMORY_FLOCK_TIMEOUT", "30")
    try:
        timeout = float(timeout_str)
    except ValueError:
        timeout = 30.0

    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    except OSError:
        if not _FLOCK_WARNED:
            _FLOCK_WARNED = True
            logging.getLogger("bettermemory._fsutil").warning(
                "flock_excl: cannot open lockfile %s on Windows; "
                "cross-process locking is disabled. Concurrent writers may "
                "corrupt files.",
                lock_path,
            )
        yield
        return

    try:
        deadline = time.monotonic() + timeout
        backoff = 0.005  # 5ms initial
        acquired = False
        while True:
            try:
                # LK_NBLCK: non-blocking exclusive lock on 1 byte at
                # the current file position. Raises OSError on
                # contention. The byte-range is the conventional
                # whole-file proxy for advisory locks on Windows.
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined,unused-ignore]
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"flock_excl: could not acquire {lock_path} within "
                        f"{timeout:.1f}s (set BETTERMEMORY_FLOCK_TIMEOUT to "
                        f"raise the ceiling)"
                    )
                time.sleep(backoff)
                # Cap at 100ms — keeps the retry interval bounded so a
                # long-held lock doesn't spin out to multi-second sleeps
                # that hide a lock that JUST released.
                backoff = min(backoff * 2, 0.1)
        try:
            yield
        finally:
            if acquired:
                try:
                    # Release the same byte we locked. Errors here
                    # would orphan the lock; suppress and log via the
                    # close path so the process can continue.
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined,unused-ignore]
                except OSError:
                    pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def bounded_stream_read(stream: BinaryIO, max_bytes: int) -> bytes:
    """Read up to ``max_bytes`` from an open binary stream.

    Raises ``ValueError`` if the stream carries more than the cap.
    Detection works by reading ``max_bytes + 1``: if the read returns
    exactly that many bytes, the stream has more available and the
    input is over-cap. The +1 byte is discarded on the error path.

    Use for unseekable streams the caller has opened — typically
    ``sys.stdin.buffer`` in CLI entry points where a pipe writer
    upstream might misbehave or stream gigabytes by accident.
    """
    raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"stream input exceeds cap {max_bytes} bytes")
    return raw


__all__ = [
    "atomic_write_bytes",
    "bounded_read",
    "bounded_stream_read",
    "bounded_tail_read",
    "flock_excl",
    "fsync_dir",
    "fsync_file",
]
