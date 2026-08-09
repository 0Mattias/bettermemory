"""Tests for `_fsutil` bounded-read helpers.

The fsync helpers are exercised indirectly by the durability suite. The
bounded-read helpers are new in 2.6.4 and are the single point of
enforcement for the byte-vs-char and unbounded-read defects the 2.6.x
audit cycle keeps surfacing — the tests pin the contract explicitly so
the next caller can't re-derive a broken cap.

The ``TestFlockWindows`` class exercises the ``_flock_windows`` branch
of ``flock_excl`` on POSIX hosts by injecting a fake ``msvcrt`` module
into ``sys.modules``. The Windows branch is ``# pragma: no cover`` on
the POSIX CI host and Windows CI is currently aspirational, so these
mocks are the only regression coverage we have for the retry, unlock-
symmetry, and env-var-timeout discipline of that path.

``TestFlockPosixWaitIsUnbounded`` pins the other half of that contract:
the POSIX branch blocks in ``fcntl.flock(fd, LOCK_EX)`` with no
deadline, and the timeout env-var the Windows branch reads does not
reach it. Both halves are stated in ``flock_excl``'s docstring, and the
asymmetry between them has already been mis-copied into a caller's
prose — commit 60b7553 replaced a sync-lock comment that granted the
POSIX wait the Windows 30s ceiling — so it is pinned mechanically here
rather than left to review.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import io
import os
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

import bettermemory
from bettermemory import _fsutil
from bettermemory._fsutil import (
    atomic_write_bytes,
    bounded_read,
    bounded_stream_read,
    bounded_tail_read,
)


class TestBoundedRead:
    def test_under_cap_returns_bytes(self, tmp_path: Path) -> None:
        path = tmp_path / "f.txt"
        path.write_bytes(b"hello")
        assert bounded_read(path, max_bytes=1024) == b"hello"

    def test_exactly_at_cap_succeeds(self, tmp_path: Path) -> None:
        path = tmp_path / "f.txt"
        path.write_bytes(b"x" * 100)
        assert bounded_read(path, max_bytes=100) == b"x" * 100

    def test_over_cap_raises_before_allocation(self, tmp_path: Path) -> None:
        path = tmp_path / "f.txt"
        path.write_bytes(b"x" * 1024)
        with pytest.raises(ValueError, match="exceeds cap"):
            bounded_read(path, max_bytes=512)

    def test_byte_not_char_cap(self, tmp_path: Path) -> None:
        """The classic 2.6.3 byte-vs-char trap: 4-byte UTF-8 codepoint
        repeated to fill the file. If the cap counted chars, a 2-byte
        cap would accept this. It must count bytes."""
        path = tmp_path / "f.txt"
        # 100 copies of a 4-byte codepoint (U+1F600 grinning face) = 400 bytes.
        path.write_text("\U0001f600" * 100, encoding="utf-8")
        assert path.stat().st_size == 400
        # Cap of 200 bytes must reject this even though it's only 100 chars.
        with pytest.raises(ValueError, match="exceeds cap"):
            bounded_read(path, max_bytes=200)
        # Cap of 400 bytes accepts it.
        assert bounded_read(path, max_bytes=400) == b"\xf0\x9f\x98\x80" * 100

    def test_missing_file_raises_filenotfounderror(self, tmp_path: Path) -> None:
        """A missing file must raise `FileNotFoundError` — the native
        subclass, NOT a flattened bare `OSError`. `Store.restore` and
        `Store.rename_scope` catch `FileNotFoundError` specifically to
        turn a vanished-file race into a clean `MemoryNotFoundError`;
        flattening the subclass (the 2.6.4 regression) silently turned
        those handlers into dead code."""
        with pytest.raises(FileNotFoundError):
            bounded_read(tmp_path / "nope", max_bytes=1024)

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty"
        path.write_bytes(b"")
        assert bounded_read(path, max_bytes=1024) == b""


class TestBoundedTailRead:
    def test_under_cap_returns_full_file(self, tmp_path: Path) -> None:
        path = tmp_path / "log.jsonl"
        path.write_bytes(b"line1\nline2\n")
        assert bounded_tail_read(path, max_bytes=1024) == b"line1\nline2\n"

    def test_over_cap_returns_tail(self, tmp_path: Path) -> None:
        path = tmp_path / "log.jsonl"
        path.write_bytes(b"line1\nline2\nline3\nline4\n")
        # 10-byte cap → seeks to size-10 = 14; reads from offset 14.
        # File bytes 14..24 = "line4\n" preceded by half of "line3\n".
        # Partial-line discard drops the half-line; tail starts after the
        # first newline.
        result = bounded_tail_read(path, max_bytes=10)
        # Result must begin after a newline (no partial line at the head).
        assert b"\n" not in result or result.startswith(b"line")
        assert result.endswith(b"line4\n")

    def test_partial_line_discard_at_seek_boundary(self, tmp_path: Path) -> None:
        """When the seek lands inside a line, the partial head is
        dropped before returning."""
        path = tmp_path / "log.jsonl"
        # 100-byte content, 10 lines of "0123456789\n" each (11 bytes).
        path.write_bytes(b"0123456789\n" * 10)
        # Cap of 25 bytes → seek lands at byte 85, mid-line. After
        # partial-line discard, result should start at the next newline.
        result = bounded_tail_read(path, max_bytes=25)
        # Every retained line is a full record.
        assert all(line == b"0123456789" for line in result.splitlines() if line)

    def test_unseekable_stream_falls_back_to_forward_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FIFO / pipe paths can't seek; the helper must read forward
        up to the cap rather than crashing."""
        path = tmp_path / "log.jsonl"
        path.write_bytes(b"line1\nline2\n")
        real_open = Path.open

        def patched_open(self: Path, *args, **kwargs):
            fh = real_open(self, *args, **kwargs)

            def fail_seek(*_a, **_kw):
                raise OSError("unseekable")

            fh.seek = fail_seek
            return fh

        monkeypatch.setattr(Path, "open", patched_open)
        result = bounded_tail_read(path, max_bytes=1024)
        # Fallback reads from current position (start), full content fits.
        assert result == b"line1\nline2\n"

    def test_no_newline_returns_chunk_unchanged(self, tmp_path: Path) -> None:
        """A single huge unbroken line has no partial-line discard
        applied — the caller has to handle it."""
        path = tmp_path / "blob"
        path.write_bytes(b"x" * 200)
        # Cap of 50 → seek to 150, read 50 bytes, no newline to anchor
        # the partial-line discard, return as-is.
        result = bounded_tail_read(path, max_bytes=50)
        assert result == b"x" * 50

    def test_byte_not_char_cap(self, tmp_path: Path) -> None:
        path = tmp_path / "log.jsonl"
        # Mix of single-byte and 4-byte codepoints to make sure the cap
        # operates on bytes regardless of UTF-8 width.
        body = ("a\U0001f600\n" * 50).encode("utf-8")  # 6 bytes * 50 = 300
        path.write_bytes(body)
        result = bounded_tail_read(path, max_bytes=60)
        # 60 bytes ≤ result ≤ 60 bytes (no partial-line discard adds bytes).
        assert len(result) <= 60


class TestBoundedStreamRead:
    def test_under_cap_returns_full(self) -> None:
        stream = io.BytesIO(b"hello world")
        assert bounded_stream_read(stream, max_bytes=1024) == b"hello world"

    def test_exactly_at_cap_succeeds(self) -> None:
        stream = io.BytesIO(b"x" * 100)
        assert bounded_stream_read(stream, max_bytes=100) == b"x" * 100

    def test_over_cap_raises(self) -> None:
        stream = io.BytesIO(b"x" * 200)
        with pytest.raises(ValueError, match="exceeds cap"):
            bounded_stream_read(stream, max_bytes=100)

    def test_empty_stream(self) -> None:
        stream = io.BytesIO(b"")
        assert bounded_stream_read(stream, max_bytes=1024) == b""

    def test_fifo_unseekable(self, tmp_path: Path) -> None:
        """Real FIFO smoke test — bounded_stream_read should work
        against an unseekable stream because it never seeks."""
        if not hasattr(os, "mkfifo"):  # pragma: no cover - non-unix
            pytest.skip("os.mkfifo not available")
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        # POSIX-only — hasattr(os, "mkfifo") above gates the call. The
        # `unused-ignore` code is stacked so mypy on POSIX (where os.fork
        # exists) doesn't flag the attr-defined ignore as unused; mypy on
        # Windows needs it because the symbol genuinely doesn't exist there.
        pid = os.fork()  # type: ignore[attr-defined, unused-ignore]
        if pid == 0:  # child
            try:
                with fifo.open("wb") as f:
                    f.write(b"hello from fifo")
            finally:
                os._exit(0)
        try:
            with fifo.open("rb") as f:
                result = bounded_stream_read(f, max_bytes=1024)
            assert result == b"hello from fifo"
        finally:
            os.waitpid(pid, 0)


# ---------------------------------------------------------------------------
# Windows flock branch — platform-mocked
# ---------------------------------------------------------------------------
#
# ``_flock_windows`` is the ``msvcrt.locking``-based fallback used when
# ``sys.platform == "win32"`` (see ``flock_excl``). It is gated by the
# `# pragma: no cover` marker because POSIX CI cannot exercise it, and
# Windows CI is currently aspirational. The tests below inject a fake
# ``msvcrt`` module into ``sys.modules`` so the branch runs end-to-end
# on macOS / Linux dev boxes: every call to ``msvcrt.locking`` is
# observable, the retry / backoff loop runs against real ``time.monotonic``
# (with ``time.sleep`` shimmed to a no-op for speed), and the env-var
# parsing is exercised by mutating ``BETTERMEMORY_FLOCK_TIMEOUT`` and
# observing the next acquisition's behaviour.
#
# These tests do NOT need ``@pytest.mark.skipif(win32)`` — the whole
# point of the fake-``msvcrt`` posture is that the branch is testable
# from any host, and a future Windows CI matrix run will exercise the
# real ``msvcrt`` end-to-end as a belt-and-suspenders complement.


class _FakeMsvcrt(types.ModuleType):
    """Minimal stand-in for the real Windows ``msvcrt`` module.

    Records every ``locking`` call as ``(fd, mode, nbytes)``. The
    ``fail_first`` counter controls how many initial ``LK_NBLCK``
    attempts raise ``OSError`` (the Windows "contention" signal) before
    the next call succeeds. ``LK_UNLCK`` calls always succeed and are
    recorded for the symmetry assertions.
    """

    # The exact integer values don't matter — production code references
    # them by attribute, not by value — but they must be distinct so the
    # call-log assertions can distinguish lock from unlock.
    LK_NBLCK = 1
    LK_UNLCK = 2

    def __init__(self, fail_first: int = 0, always_fail: bool = False) -> None:
        super().__init__("msvcrt")
        self.calls: list[tuple[int, int, int]] = []
        self._fail_first = fail_first
        self._always_fail = always_fail
        self._lock_attempts = 0

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        self.calls.append((fd, mode, nbytes))
        if mode == self.LK_NBLCK:
            self._lock_attempts += 1
            if self._always_fail or self._lock_attempts <= self._fail_first:
                # Real msvcrt raises OSError on contention with errno
                # EDEADLOCK/EACCES depending on the host. Production
                # catches bare ``OSError``, so the errno detail is not
                # load-bearing for this contract.
                raise OSError("simulated lock contention")
        # LK_UNLCK is always a no-op in the fake; the real msvcrt also
        # succeeds in the common path.

    @property
    def lock_attempts(self) -> int:
        return self._lock_attempts


@contextlib.contextmanager
def _drive(gen):
    """Run a bare generator through its single yield as a context manager.

    ``_flock_windows`` is intentionally a plain generator (so the
    POSIX branch in ``flock_excl`` can ``yield from`` it without an
    extra layer of indirection). Tests need the same shape — enter,
    body, exit — so we wrap it here rather than monkeying with
    ``flock_excl`` itself.
    """
    try:
        next(gen)
        yield
    finally:
        with contextlib.suppress(StopIteration):
            next(gen)


@pytest.fixture
def fake_msvcrt(monkeypatch: pytest.MonkeyPatch) -> _FakeMsvcrt:
    """Install a fresh ``_FakeMsvcrt`` in ``sys.modules`` and reset the
    one-shot ``_FLOCK_WARNED`` flag so warning emission is observable
    per test."""
    fake = _FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(_fsutil, "_FLOCK_WARNED", False)
    # ``time.sleep`` would otherwise burn real wall-clock seconds during
    # the retry-loop tests. Real ``time.monotonic`` is kept so the
    # deadline arithmetic in ``_flock_windows`` still behaves
    # realistically; the loop just doesn't actually sleep.
    monkeypatch.setattr("time.sleep", lambda _s: None)
    return fake


class TestFlockWindows:
    def test_acquires_on_first_attempt_and_releases_symmetrically(
        self, tmp_path: Path, fake_msvcrt: _FakeMsvcrt
    ) -> None:
        """Happy path: the lock succeeds on the first try, the body
        runs, and the release call mirrors the acquire — same fd, same
        byte count, but ``LK_UNLCK`` instead of ``LK_NBLCK``. This is
        the contract that keeps real Windows locks from leaking across
        ``with`` blocks."""
        lock_path = tmp_path / "thing.md.lock"

        with _drive(_fsutil._flock_windows(lock_path)):
            # Inside the body, exactly one call (the acquire) must have
            # been made. The release happens on exit below.
            assert fake_msvcrt.lock_attempts == 1
            assert len(fake_msvcrt.calls) == 1
            assert fake_msvcrt.calls[0][1] == _FakeMsvcrt.LK_NBLCK
            assert fake_msvcrt.calls[0][2] == 1

        # After the context exits there must be exactly one unlock call,
        # targeting the same fd and byte range as the acquire.
        assert len(fake_msvcrt.calls) == 2
        acquire_fd, acquire_mode, acquire_n = fake_msvcrt.calls[0]
        release_fd, release_mode, release_n = fake_msvcrt.calls[1]
        assert acquire_mode == _FakeMsvcrt.LK_NBLCK
        assert release_mode == _FakeMsvcrt.LK_UNLCK
        assert acquire_fd == release_fd, (
            "release must target the same fd as the acquire; a mismatch "
            "would orphan the lock or unlock a fd we don't hold"
        )
        assert acquire_n == release_n == 1, (
            "release byte-count must mirror the acquire — locking 1 byte "
            "but unlocking N would either leave bytes locked or attempt "
            "to unlock bytes we don't own"
        )

    def test_retries_with_backoff_until_acquired(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Under contention, the loop retries with capped exponential
        backoff until it succeeds (or hits the timeout). Inject six
        consecutive contention errors and assert: (1) the seventh
        attempt succeeds, (2) the sleep history shape matches the
        production discipline — one sleep per failure, monotonically
        non-decreasing, first sleep >= the documented 5ms initial,
        last sleep <= the documented 100ms cap.

        A regression that flattened ``backoff = 0.005`` to ``0``,
        removed the ``* 2`` doubling, or dropped the ``min(..., 0.1)``
        cap would only show up if the test observed the real sleep
        durations — counting attempts alone passes for the wrong
        reason. The list-appending sleep shim is the reusable pattern:
        ``sleeps: list[float] = []; monkeypatch.setattr(time, "sleep",
        sleeps.append)`` lets any future test verify timing without
        burning wall-clock seconds."""
        import time

        monkeypatch.setattr(_fsutil, "_FLOCK_WARNED", False)
        monkeypatch.setenv("BETTERMEMORY_FLOCK_TIMEOUT", "30")
        # Capture every backoff sleep so we can assert on the actual
        # durations rather than just call counts. Default sleep shim
        # from ``fake_msvcrt`` fixture is a no-op that throws the
        # durations away; this test owns its own shim instead.
        sleeps: list[float] = []
        monkeypatch.setattr(time, "sleep", sleeps.append)
        # Six contention failures gives us a meaningful sample of the
        # exponential ramp (0.005, 0.01, 0.02, 0.04, 0.08, then capped
        # at 0.1) without making the test fragile to small production
        # tweaks of the initial value.
        fake = _FakeMsvcrt(fail_first=6)
        monkeypatch.setitem(sys.modules, "msvcrt", fake)
        lock_path = tmp_path / "thing.md.lock"

        with _drive(_fsutil._flock_windows(lock_path)):
            assert fake.lock_attempts == 7, (
                f"expected 6 failures then 1 success = 7 attempts, "
                f"got {fake.lock_attempts}"
            )
            # Calls so far: 6 failed LK_NBLCK + 1 successful LK_NBLCK.
            # No LK_UNLCK yet — that fires on context exit.
            assert all(mode == _FakeMsvcrt.LK_NBLCK for _, mode, _ in fake.calls)
            assert len(fake.calls) == 7

        # Release call appended after the body exits.
        assert fake.calls[-1][1] == _FakeMsvcrt.LK_UNLCK

        # --- Backoff discipline assertions ----------------------------
        # One sleep per contention failure (the successful attempt does
        # not sleep — the loop breaks before the sleep call).
        assert len(sleeps) == 6, (
            f"expected one sleep per failed attempt = 6 sleeps, got "
            f"{len(sleeps)} ({sleeps}). A mismatch means the retry "
            f"loop either skipped a sleep or slept extra times."
        )
        # Monotonically non-decreasing: the backoff grows (then caps
        # but never shrinks). A regression that removed the ``* 2``
        # doubling would produce a flat list and trip this.
        assert all(a <= b for a, b in zip(sleeps, sleeps[1:])), (
            f"sleeps must be monotonically non-decreasing — production "
            f"doubles backoff each iteration and caps at 0.1; got {sleeps}"
        )
        # First sleep is the production initial backoff (5ms). A
        # regression flattening ``backoff = 0.005`` to ``0`` would
        # trip this — even though the attempt count would be unchanged.
        assert sleeps[0] >= 0.005, (
            f"first sleep must be >= production initial backoff (0.005s); "
            f"got {sleeps[0]}. A regression to ``backoff = 0`` would "
            f"surface here."
        )
        # Max sleep stays at the documented 100ms cap. A regression
        # removing ``min(backoff * 2, 0.1)`` would let the sixth sleep
        # grow to 0.005 * 2^5 = 0.16 and trip this.
        assert max(sleeps) <= 0.1, (
            f"max sleep must respect the documented 100ms cap; got "
            f"{max(sleeps)}. A regression removing ``min(..., 0.1)`` "
            f"would surface here as an unbounded ramp."
        )

    def test_timeout_raises_after_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``BETTERMEMORY_FLOCK_TIMEOUT`` is exhausted with the
        lock still contended, the loop raises ``TimeoutError`` rather
        than spinning forever. We set timeout to 0 so the very first
        deadline check (after attempt 1) trips, regardless of clock
        jitter on the host."""
        monkeypatch.setattr("time.sleep", lambda _s: None)
        monkeypatch.setattr(_fsutil, "_FLOCK_WARNED", False)
        # Always-failing fake — every LK_NBLCK call raises.
        fake = _FakeMsvcrt(always_fail=True)
        monkeypatch.setitem(sys.modules, "msvcrt", fake)
        monkeypatch.setenv("BETTERMEMORY_FLOCK_TIMEOUT", "0")
        lock_path = tmp_path / "thing.md.lock"

        with pytest.raises(TimeoutError, match="could not acquire"):
            with _drive(_fsutil._flock_windows(lock_path)):
                pytest.fail("body must not run when acquisition times out")

        # The acquire path tried at least once. No unlock call should
        # have fired — we never successfully acquired.
        assert fake.lock_attempts >= 1
        assert all(mode == _FakeMsvcrt.LK_NBLCK for _, mode, _ in fake.calls)

    def test_env_var_timeout_honors_valid_integer_string(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A valid numeric env-var value is parsed and used as the
        timeout ceiling. We verify this indirectly: a timeout of 0
        forces a TimeoutError after one failed attempt, but a timeout
        of (say) 5 lets the retry loop succeed when the fake yields
        on attempt 2. The contrast pins env-var influence on the next
        acquisition."""
        monkeypatch.setattr("time.sleep", lambda _s: None)
        monkeypatch.setattr(_fsutil, "_FLOCK_WARNED", False)
        fake = _FakeMsvcrt(fail_first=1)
        monkeypatch.setitem(sys.modules, "msvcrt", fake)
        monkeypatch.setenv("BETTERMEMORY_FLOCK_TIMEOUT", "5")
        lock_path = tmp_path / "thing.md.lock"

        with _drive(_fsutil._flock_windows(lock_path)):
            pass

        # One failed attempt + one success = 2 calls before exit; +1
        # unlock on exit = 3 total. Crucially the loop did NOT
        # short-circuit to TimeoutError — the env-var was honoured.
        assert fake.lock_attempts == 2
        assert len(fake.calls) == 3
        assert fake.calls[-1][1] == _FakeMsvcrt.LK_UNLCK

    def test_env_var_invalid_string_falls_back_to_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-numeric ``BETTERMEMORY_FLOCK_TIMEOUT`` must fall back
        to the documented 30s default — not propagate ``ValueError``,
        not silently degrade to ``timeout = 0`` (which would instantly
        time out under any real contention).

        Round-1 fortification proved only ``timeout > 0`` via
        ``fake.lock_attempts > 1``: with ``time.sleep`` no-op'd and
        real ``time.monotonic`` ticking on wall-clock, even a
        regression to ``timeout = 0.001`` (or any small nonzero) would
        burn through thousands of attempts before the deadline tripped
        and still pass. The docstring's claim of "distinguishing the
        real 30s default from an accidental zero" overpromised against
        what the count assertion actually enforced.

        This round patches ``time.monotonic`` to a deterministic
        per-call counter (``0, 1, 2, 3, ...``). With that counter,
        the deadline arithmetic in ``_flock_windows`` becomes exact:

        * First ``time.monotonic()`` call sets ``deadline = 0 + timeout``.
        * Each subsequent ``time.monotonic()`` (the post-attempt
          deadline check) returns the next integer.
        * The loop trips ``TimeoutError`` on the first attempt whose
          deadline check returns ``>= timeout``.

        So with ``timeout = 30`` we expect EXACTLY 30 attempts — the
        check at attempt 30 returns 30, satisfies ``30 >= 30``, and
        raises. A regression to ``timeout = 0.001`` would exit after
        attempt 1 (check returns 1, ``1 >= 0.001``, raise). A
        regression to ``timeout = 0`` would also give 1. The exact
        ``== 30`` assertion below pins the documented default rather
        than the weaker "is positive" property.

        ``time.sleep`` is still no-op'd because the deterministic
        counter makes real sleep arithmetic irrelevant (and slow)."""
        import time

        monkeypatch.setattr("time.sleep", lambda _s: None)
        monkeypatch.setattr(_fsutil, "_FLOCK_WARNED", False)
        # Deterministic monotonic counter: 0, 1, 2, 3, ... Each call
        # ticks the counter exactly one unit. The production deadline
        # arithmetic (``deadline = time.monotonic() + timeout`` then
        # ``time.monotonic() >= deadline`` per failed attempt) then
        # becomes a function of attempt count, not wall-clock.
        monotonic_counter = iter(range(0, 10_000))
        monkeypatch.setattr(time, "monotonic", lambda: next(monotonic_counter))
        # Always-failing fake forces the deadline arithmetic to be the
        # only thing that can end the loop. With a no-failure fake we
        # exit on attempt 1 regardless of what ``timeout`` got parsed
        # to — the original weakness.
        fake = _FakeMsvcrt(always_fail=True)
        monkeypatch.setitem(sys.modules, "msvcrt", fake)
        monkeypatch.setenv("BETTERMEMORY_FLOCK_TIMEOUT", "not-a-number")
        lock_path = tmp_path / "thing.md.lock"

        # If the helper propagated ValueError from float() this would
        # explode out of the context-manager entry. The fallback path
        # raises TimeoutError instead (eventually) — that's the
        # contract under always-fail.
        with pytest.raises(TimeoutError, match="could not acquire"):
            with _drive(_fsutil._flock_windows(lock_path)):
                pytest.fail("body must not run when acquisition times out")

        # Exact attempt count pins the documented 30s default. The
        # deterministic monotonic counter makes this precise: with
        # ``timeout = 30`` the loop's deadline is set from
        # ``time.monotonic() = 0`` (first call), each failed-attempt
        # check ticks the counter (``1, 2, ..., 30``), and the loop
        # raises when the check first returns ``>= 30`` — which happens
        # on attempt 30 (the 31st ``time.monotonic()`` call total).
        #
        # Regression matrix this catches:
        #   timeout = 30     → attempts == 30  (current, passes)
        #   timeout = 0      → attempts == 1   (instant trip)
        #   timeout = 0.001  → attempts == 1   (counter int=1 >= 0.001)
        #   timeout = 5      → attempts == 5   (wrong default value)
        #   timeout = 60     → attempts == 60  (wrong default value)
        assert fake.lock_attempts == 30, (
            f"expected EXACTLY 30 lock attempts under the documented "
            f"30s default ceiling with a deterministic monotonic "
            f"counter (one tick per call); got {fake.lock_attempts}. "
            f"A mismatch means the env-var fallback did not parse to "
            f"30.0 — could be 0 (instant trip → 1 attempt), a small "
            f"nonzero (still 1 attempt under integer-tick counter), "
            f"or a different default value entirely (5, 60, etc)."
        )

    def test_env_var_change_takes_effect_on_next_acquisition(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The env-var is re-read on every call to ``_flock_windows``
        (not cached at module load). Mutating it between two
        acquisitions on the same lockfile must change the second call's
        behaviour. This catches a subtle regression where someone
        caches the value at import time and breaks operator-runtime
        reconfiguration."""
        monkeypatch.setattr("time.sleep", lambda _s: None)
        monkeypatch.setattr(_fsutil, "_FLOCK_WARNED", False)
        # Always-failing fake — every LK_NBLCK call raises so the
        # timeout value is the only thing that controls success/failure.
        fake = _FakeMsvcrt(always_fail=True)
        monkeypatch.setitem(sys.modules, "msvcrt", fake)
        lock_path = tmp_path / "thing.md.lock"

        # First call: timeout 0 → TimeoutError after one attempt.
        monkeypatch.setenv("BETTERMEMORY_FLOCK_TIMEOUT", "0")
        with pytest.raises(TimeoutError):
            with _drive(_fsutil._flock_windows(lock_path)):
                pytest.fail("body must not run on first call")
        first_call_attempts = fake.lock_attempts
        assert first_call_attempts >= 1

        # Second call: same fake (still always failing), but now we
        # flip the env-var to a tiny-but-nonzero ceiling. If the
        # helper had cached the prior value of 0, this would still
        # raise on attempt 1; instead it must enter at least one retry
        # before raising — proving the env-var is re-read.
        monkeypatch.setenv("BETTERMEMORY_FLOCK_TIMEOUT", "0.05")
        with pytest.raises(TimeoutError):
            with _drive(_fsutil._flock_windows(lock_path)):
                pytest.fail("body must not run on second call either")

        # Second call did at least one additional attempt (more than
        # the first call's single attempt), proving the new timeout
        # was honoured rather than the prior cached 0.
        second_call_attempts = fake.lock_attempts - first_call_attempts
        assert second_call_attempts >= 2, (
            f"second call should have retried more than once under a "
            f"0.05s ceiling (with sleep no-op'd, retries are essentially "
            f"free until time.monotonic crosses the deadline); got "
            f"{second_call_attempts} attempts. If this is 1, the env-var "
            f"was cached from the first call rather than re-read."
        )


# ---------------------------------------------------------------------------
# POSIX flock branch — the wait with no deadline
# ---------------------------------------------------------------------------
#
# ``flock_excl``'s POSIX branch is a plain blocking ``fcntl.flock(fd,
# LOCK_EX)``. It carries no deadline, and ``BETTERMEMORY_FLOCK_TIMEOUT``
# — the knob the Windows branch reads — never reaches it. That
# asymmetry is easy to miss when reading the helper, because the only
# timeout figure in its docstring sits in the Windows paragraph, and a
# caller that carried that figure over to describe its own POSIX lock
# wait shipped a false 30s claim that commit 60b7553 had to correct.
#
# The tests below pin the halves that the docstring now states
# explicitly, so a future edit that softens either one turns red:
#   * behavioural — a real second interpreter holds the lock while this
#     process acquires with the env-var ceiling set to 0; the acquire
#     must block through it rather than raise.
#   * structural — the env-var is read in exactly one function, and the
#     POSIX branch names no non-blocking flock flag. Neither assertion
#     depends on wall-clock, so neither can rot the way a measured
#     figure would.


_POSIX_LOCK_HOLDER = """
import sys, time
from pathlib import Path
from bettermemory._fsutil import flock_excl

target, marker, hold = sys.argv[1], sys.argv[2], float(sys.argv[3])
with flock_excl(Path(target)):
    Path(marker).touch()
    time.sleep(hold)
"""


def _scoped_string_sites(
    source: str, filename: str, value: str
) -> list[tuple[int, str]]:
    """Return ``(lineno, dotted enclosing scope)`` for every string
    literal in ``source`` exactly equal to ``value``.

    Exact equality is what keeps prose out of the result. A docstring
    or an f-string fragment that merely mentions the name is a longer,
    unequal constant, so only genuine lookups survive the filter.
    """
    tree = ast.parse(source, filename)
    scoped = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    found: list[tuple[int, str]] = []

    def walk(node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, scoped):
                walk(child, (*scope, child.name))
                continue
            if isinstance(child, ast.Constant) and child.value == value:
                found.append((child.lineno, ".".join(scope)))
            walk(child, scope)

    walk(tree, ())
    return found


class TestFlockPosixWaitIsUnbounded:
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="the win32 branch routes to _flock_windows, which is bounded "
        "by BETTERMEMORY_FLOCK_TIMEOUT — the opposite of what this pins",
    )
    def test_acquire_blocks_past_the_flock_timeout_env_ceiling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The POSIX acquire waits for the holder, not for a deadline.

        A second interpreter takes the same lock and holds it. This
        process then acquires with ``BETTERMEMORY_FLOCK_TIMEOUT`` set
        to ``0`` — a ceiling so tight that any code path consulting it
        would give up on the first contended attempt. The acquire must
        instead block until the holder releases.

        Two failure modes this catches. If someone wires the env-var
        into the POSIX branch, the ``TimeoutError`` branch below fires.
        If the lock stops excluding at all, the acquire returns
        immediately and the elapsed-time assertion fires. Only the
        documented behaviour — block, then succeed — passes both.

        The elapsed assertion deliberately uses a floor far below the
        hold, not an equality: it asks whether the wait happened, which
        is the property under test, and stays true on a loaded CI box
        where the exact duration is not reproducible.
        """
        hold_seconds = 2.0
        min_blocked = 0.5
        target = tmp_path / "thing.md"
        marker = tmp_path / "held.marker"

        env = dict(os.environ)
        # Importable root of the package under test, whether the suite
        # runs against a source checkout or an installed copy.
        env["PYTHONPATH"] = str(Path(bettermemory.__file__).parent.parent)
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _POSIX_LOCK_HOLDER,
                str(target),
                str(marker),
                str(hold_seconds),
            ],
            env=env,
        )
        try:
            # Rendezvous on the marker file rather than on a sleep, so
            # the timer below starts from the holder's real acquisition.
            rendezvous_deadline = time.monotonic() + 30
            while not marker.exists():
                assert holder.poll() is None, (
                    f"holder process exited (rc={holder.returncode}) before it "
                    f"signalled acquisition — the child could not take the lock"
                )
                assert time.monotonic() < rendezvous_deadline, (
                    "holder never signalled acquisition within 30s"
                )
                time.sleep(0.01)

            monkeypatch.setenv("BETTERMEMORY_FLOCK_TIMEOUT", "0")
            start = time.monotonic()
            try:
                with _fsutil.flock_excl(target):
                    waited = time.monotonic() - start
            except TimeoutError as exc:  # pragma: no cover - regression path
                pytest.fail(
                    f"POSIX flock_excl raised TimeoutError after "
                    f"{time.monotonic() - start:.2f}s ({exc}). The POSIX "
                    f"branch must block in fcntl.flock(fd, LOCK_EX) with no "
                    f"deadline; BETTERMEMORY_FLOCK_TIMEOUT is documented as "
                    f"bounding the Windows branch only."
                )
        finally:
            holder.wait(timeout=30)

        assert waited >= min_blocked, (
            f"acquire returned after only {waited:.3f}s while another "
            f"process held the lock for {hold_seconds}s. Either the wait was "
            f"cut short by a deadline (the env-var was set to 0), or the "
            f"sidecar lock stopped excluding across processes."
        )

    def test_flock_timeout_env_is_read_in_one_function_only(self) -> None:
        """``BETTERMEMORY_FLOCK_TIMEOUT`` is looked up only inside
        ``_flock_windows``, which is what makes "it does not bound the
        POSIX path" true rather than incidental.

        Scanning the whole package, not just one module, is the point:
        the claim in ``flock_excl``'s docstring is about where the
        variable is honoured, and a lookup added in any other module
        would falsify it just as squarely as one added next door.
        """
        package_root = Path(bettermemory.__file__).parent
        modules = sorted(package_root.rglob("*.py"))
        assert modules, f"found no modules under {package_root}"

        sites: list[tuple[str, int, str]] = []
        for module in modules:
            for lineno, scope in _scoped_string_sites(
                module.read_text(encoding="utf-8"),
                str(module),
                "BETTERMEMORY_FLOCK_TIMEOUT",
            ):
                sites.append(
                    (module.relative_to(package_root).as_posix(), lineno, scope)
                )

        # Guard against the whole test passing for the wrong reason: if
        # the extractor stopped matching, `offenders` would be empty too.
        assert sites, (
            "no lookup of BETTERMEMORY_FLOCK_TIMEOUT found anywhere in the "
            "package — either the variable was renamed (update the docstring "
            "in _fsutil.flock_excl too) or this extractor stopped matching."
        )
        offenders = [site for site in sites if site[2] != "_flock_windows"]
        assert not offenders, (
            "BETTERMEMORY_FLOCK_TIMEOUT is read outside `_flock_windows`:\n  "
            + "\n  ".join(
                f"{path}:{lineno} in {scope or '<module>'}"
                for path, lineno, scope in offenders
            )
            + "\n\n`flock_excl`'s docstring tells callers this variable bounds "
            "the Windows branch and nothing else. If a POSIX wait is now "
            "bounded too, that paragraph is false and has to be rewritten "
            "before this exemption changes."
        )

    def test_posix_branch_takes_a_blocking_lock_with_no_deadline(self) -> None:
        """The POSIX branch names ``LOCK_EX`` and no non-blocking flag.

        ``LOCK_NB`` is the flag that would turn the acquire into a
        poll, which is the precondition for any retry-with-deadline
        loop — the shape the Windows helper uses. Its absence, together
        with the absence of a clock reference, is the structural form
        of "this wait has no deadline". Unlike a timing measurement,
        this assertion cannot go stale.
        """
        source = Path(_fsutil.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source, _fsutil.__file__)
        matches = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "flock_excl"
        ]
        assert len(matches) == 1, (
            f"expected exactly one `flock_excl` definition in "
            f"{_fsutil.__file__}, found {len(matches)}"
        )
        # `_flock_windows` is a sibling top-level function, so walking
        # `flock_excl` sees the POSIX branch and the win32 delegation
        # call — never the Windows helper's body.
        attrs = {
            node.attr
            for node in ast.walk(matches[0])
            if isinstance(node, ast.Attribute)
        }

        assert "LOCK_EX" in attrs, (
            "the POSIX branch no longer references fcntl.LOCK_EX — "
            "`flock_excl`'s docstring describes the acquire as a blocking "
            "LOCK_EX and would need rewriting."
        )
        assert "LOCK_NB" not in attrs, (
            "the POSIX branch now references a non-blocking lock flag. The "
            "docstring states the acquire blocks with no deadline; a "
            "poll-and-retry loop makes that false."
        )
        assert "monotonic" not in attrs, (
            "the POSIX branch now reads a clock, which is what a deadline "
            "check looks like. The docstring states this wait has no "
            "deadline — reconcile the two before relaxing this."
        )


# ---------------------------------------------------------------------------
# atomic_write_bytes — the bypass-callsite primitive
# ---------------------------------------------------------------------------
#
# Two bypass sites pre-3.1.0 called `path.write_text(...)` and could
# leave truncated files on power loss / process kill mid-write:
# `init.py`'s user MCP config writer (`~/.claude.json`, blast radius =
# every MCP server the user had registered) and `sync.py`'s `.gitignore`
# writer (truncated gitignore → next `sync push` commits event logs and
# lockfiles to the remote). Both now route through `atomic_write_bytes`.
# These tests pin the contract end-to-end: the bytes land, the rename
# is the durability boundary, the parent fsync fires, the mode is
# applied if requested, and a mid-write failure cleans up the tmp.


class TestAtomicWriteBytes:
    def test_happy_path_writes_bytes_at_target(self, tmp_path: Path) -> None:
        """The contents land at the target path and are byte-exact."""
        path = tmp_path / "f.txt"
        atomic_write_bytes(path, b"hello world")
        assert path.read_bytes() == b"hello world"

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        """A nested path under a non-existent parent directory works
        (parents=True, exist_ok=True). Bytes-level callers — init.py and
        sync.py — write under user-home paths that may not exist on a
        fresh install."""
        path = tmp_path / "a" / "b" / "c.txt"
        atomic_write_bytes(path, b"nested")
        assert path.read_bytes() == b"nested"

    def test_replaces_existing_file_atomically(self, tmp_path: Path) -> None:
        """When the target already exists, the new content replaces it
        atomically — no partial write window where the file is half the
        old content and half the new."""
        path = tmp_path / "f.txt"
        path.write_bytes(b"old content here")
        atomic_write_bytes(path, b"new")
        assert path.read_bytes() == b"new"

    def test_no_tmp_leftover_on_success(self, tmp_path: Path) -> None:
        """After a clean write, the parent directory contains only the
        target file — no orphan `<path>.<random>.tmp` siblings. A
        regression that skipped the rename (or unlinked the wrong path)
        would leave the tmp behind."""
        path = tmp_path / "f.txt"
        atomic_write_bytes(path, b"clean")
        # Only the target file should be present in the parent dir.
        entries = sorted(p.name for p in tmp_path.iterdir())
        assert entries == ["f.txt"], (
            f"expected only target file after clean write; "
            f"got {entries} (a .tmp leak would surface here)"
        )

    def test_mode_applied_when_provided(self, tmp_path: Path) -> None:
        """When `mode` is passed, the file lands with those permission
        bits. Skipped on Windows — POSIX mode bits don't apply there."""
        if sys.platform == "win32":
            pytest.skip("POSIX permission bits don't apply on Windows")
        path = tmp_path / "f.txt"
        atomic_write_bytes(path, b"private", mode=0o600)
        actual = path.stat().st_mode & 0o777
        assert actual == 0o600, (
            f"expected mode 0o600 from `mode=` arg; got {oct(actual)}"
        )

    def test_mode_before_rename_applied(self, tmp_path: Path) -> None:
        """`mode_before_rename` lands the same end-state bits as `mode`,
        but set via fchmod before the rename (see the ordering test for
        the privacy guarantee). Skipped on Windows — POSIX mode bits
        don't apply there."""
        if sys.platform == "win32":
            pytest.skip("POSIX permission bits don't apply on Windows")
        path = tmp_path / "f.txt"
        atomic_write_bytes(path, b"private", mode_before_rename=0o600)
        actual = path.stat().st_mode & 0o777
        assert actual == 0o600, (
            f"expected mode 0o600 from `mode_before_rename=` arg; got {oct(actual)}"
        )

    def test_mode_before_rename_fchmods_before_replace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The privacy guarantee `mode_before_rename` exists for: the
        fchmod on the tmp fd MUST precede the `os.replace` that brings the
        file to the visible path, so it's never world-readable at the
        target name even for an instant. A regression to chmod-after-
        rename would flip the order and reopen the window. Skipped on
        Windows where `os.fchmod` is a no-op."""
        if sys.platform == "win32":
            pytest.skip("os.fchmod is POSIX-only")
        order: list[str] = []
        real_fchmod = os.fchmod
        real_replace = os.replace

        def spy_fchmod(fd, m):
            order.append("fchmod")
            return real_fchmod(fd, m)

        def spy_replace(src, dst):
            order.append("replace")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "fchmod", spy_fchmod)
        monkeypatch.setattr(os, "replace", spy_replace)
        path = tmp_path / "f.txt"
        atomic_write_bytes(path, b"private", mode_before_rename=0o600)
        assert order == ["fchmod", "replace"], (
            f"expected fchmod before replace (no world-readable window); got {order}"
        )

    def test_mode_before_rename_fallback_chmods_when_fchmod_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The privacy guarantee must survive an `os.fchmod` that raises.
        Some sandbox filesystems reject fchmod, so the call is wrapped in
        `contextlib.suppress(OSError)` and a defensive post-rename
        `os.chmod` recovers the requested mode. This pins BOTH halves at
        once: with `os.fchmod` forced to raise, the write must (a) not
        propagate the error, and (b) still land the file at the requested
        bits via the fallback chmod — proving the suppression is real and
        the fallback is load-bearing. Uses 0o640 (not the 0o600 tmp
        default) so the assertion can only pass if the fallback actually
        ran. Skipped on Windows where `os.fchmod` is absent and POSIX
        bits don't apply."""
        if sys.platform == "win32":
            pytest.skip("os.fchmod is POSIX-only")

        def boom_fchmod(_fd: int, _mode: int) -> None:
            raise OSError("simulated sandbox fchmod rejection")

        monkeypatch.setattr(os, "fchmod", boom_fchmod)
        path = tmp_path / "f.txt"
        # Must not raise even though fchmod failed — the suppress() holds.
        atomic_write_bytes(path, b"private", mode_before_rename=0o640)
        assert path.read_bytes() == b"private", (
            "write must complete even when fchmod raises — the OSError is "
            "suppressed, not propagated"
        )
        actual = path.stat().st_mode & 0o777
        assert actual == 0o640, (
            f"expected the defensive post-rename chmod to recover mode 0o640 "
            f"after fchmod failed; got {oct(actual)}. The fallback "
            f"`os.chmod(path, mode_before_rename)` regressed — a sandbox-FS "
            f"caller would silently keep the 0o600 tmp default instead of the "
            f"requested mode."
        )
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "f.txt"]
        assert leftovers == [], f"unexpected leftover files: {leftovers}"

    def test_mode_and_mode_before_rename_are_mutually_exclusive(
        self, tmp_path: Path
    ) -> None:
        """Passing both `mode` and `mode_before_rename` is a caller bug —
        two disciplines for the same concern. The guard raises ValueError
        before any filesystem work, so no target or tmp file is created."""
        path = tmp_path / "f.txt"
        with pytest.raises(ValueError, match="mutually exclusive"):
            atomic_write_bytes(path, b"x", mode=0o644, mode_before_rename=0o600)
        assert list(tmp_path.iterdir()) == [], (
            "mutual-exclusion guard must fire before any filesystem work"
        )

    def test_no_explicit_chmod_when_mode_not_provided(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without `mode=`, the helper does NOT call `os.chmod` on the
        target — it inherits whatever the tmp's permission bits were
        (typically 0o600 from `tempfile.NamedTemporaryFile`'s defaults,
        but that's the tmpfile module's choice, not ours). Pinning the
        no-chmod contract matters for the canonical `_atomic_write_post`
        (migrated onto this helper in Q29): it passes `mode_before_rename`
        and relies on the absence of an unconditional chmod here, so a
        regression that always chmod'd would silently change the mode of
        every private memory write."""
        seen_chmods: list[tuple[str, int]] = []
        real_chmod = os.chmod

        def spy_chmod(p, m):
            seen_chmods.append((str(p), m))
            return real_chmod(p, m)

        monkeypatch.setattr(os, "chmod", spy_chmod)
        path = tmp_path / "f.txt"
        atomic_write_bytes(path, b"y")
        assert seen_chmods == [], (
            f"expected zero os.chmod calls when mode is not provided; "
            f"got {seen_chmods}. A regression that always chmods would "
            f"surface here."
        )

    def test_uses_os_replace_for_atomicity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rename hop MUST be `os.replace` (or equivalent atomic
        primitive). A regression to `shutil.copy` or a plain truncate-
        and-write would lose atomicity. We spy `os.replace` and assert
        it fires with a tmp source path and the target as the dest."""
        seen: list[tuple[str, str]] = []
        real_replace = os.replace

        def spy_replace(src, dst):
            seen.append((str(src), str(dst)))
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", spy_replace)
        path = tmp_path / "f.txt"
        atomic_write_bytes(path, b"atomic")
        assert len(seen) == 1, (
            f"expected exactly one os.replace call; got {len(seen)} "
            f"({seen}). The atomic rename is the durability boundary; "
            f"a non-replace path would silently bypass it."
        )
        src, dst = seen[0]
        assert dst == str(path)
        # The src must be a tmp sibling of the target — same parent dir
        # is required for `os.replace` atomicity (cross-FS rename is
        # not atomic).
        assert Path(src).parent == path.parent, (
            f"tmp source {src!r} must live in the target's parent dir "
            f"{str(path.parent)!r} for the rename to be atomic"
        )
        assert ".tmp" in src

    def test_fsync_dir_called_on_parent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`fsync_dir(parent)` runs after the rename so the new dirent
        is durable past a crash. The 2.6.x audit cycle keeps catching
        callers that skipped this — pin it explicitly."""
        seen: list[Path] = []

        def spy_fsync_dir(p: Path) -> None:
            seen.append(p)

        monkeypatch.setattr(_fsutil, "fsync_dir", spy_fsync_dir)
        path = tmp_path / "f.txt"
        atomic_write_bytes(path, b"durable")
        assert path.parent in seen, (
            f"expected fsync_dir({path.parent!r}); seen={seen}. "
            f"Without it, the new dirent is not durable past power loss."
        )

    def test_fsync_file_called_before_rename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`fsync_file(fd)` runs before `os.replace`. Without it, the
        bytes are still in the page cache when the rename returns — a
        crash leaves a renamed-but-empty file (the exact zero-byte-on-
        crash failure mode the discipline exists to prevent)."""
        call_order: list[str] = []
        real_fsync_file = _fsutil.fsync_file
        real_replace = os.replace

        def spy_fsync_file(fd: int) -> None:
            call_order.append("fsync_file")
            real_fsync_file(fd)

        def spy_replace(src, dst):
            call_order.append("replace")
            return real_replace(src, dst)

        monkeypatch.setattr(_fsutil, "fsync_file", spy_fsync_file)
        monkeypatch.setattr(os, "replace", spy_replace)
        path = tmp_path / "f.txt"
        atomic_write_bytes(path, b"ordered")
        assert "fsync_file" in call_order, "fsync_file was never called"
        assert "replace" in call_order, "os.replace was never called"
        assert call_order.index("fsync_file") < call_order.index("replace"), (
            f"fsync_file must run before os.replace; got {call_order!r}. "
            f"The opposite order means the bytes can be lost on crash "
            f"even though the dirent appears."
        )

    def test_tmp_cleaned_up_on_replace_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If `os.replace` raises after the tmp has been written, the
        orphan tmp file must be unlinked in the `finally` block — a
        crashed-write loop must not accumulate `<path>.<random>.tmp`
        siblings in the parent directory."""

        def boom(src, dst):
            raise OSError("simulated rename failure")

        monkeypatch.setattr(os, "replace", boom)
        path = tmp_path / "f.txt"
        with pytest.raises(OSError, match="simulated rename failure"):
            atomic_write_bytes(path, b"will not land")
        # Target was never created.
        assert not path.exists()
        # No tmp siblings left behind in the parent.
        leftovers = list(tmp_path.iterdir())
        assert leftovers == [], (
            f"expected zero leftover files after a rename failure; "
            f"got {[p.name for p in leftovers]}. The finally-block "
            f"cleanup of the tmp file regressed."
        )

    def test_tmp_cleaned_up_on_write_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If a step between tmp creation and rename raises (here:
        `os.fsync` simulating a disk error during durability flush),
        the tmp must still be unlinked — a crashed-write loop must not
        accumulate stranded tmps even on the failure path before the
        rename happens. We pick `os.fsync` rather than the tmp write
        itself because the helper imports `tempfile` lazily inside the
        function, and `NamedTemporaryFile` is harder to monkey-patch
        cleanly without going through `sys.modules['tempfile']`. The
        contract we're pinning is the same: a failure between tmp
        creation and the rename triggers tmp cleanup via the `finally`
        block."""

        # `fsync_file` swallows OSError, so patch the underlying
        # `os.fsync` to raise something that propagates — a bare
        # Exception subclass that isn't OSError.
        class _SimulatedDurabilityError(Exception):
            pass

        def boom_fsync(_fd: int) -> None:
            raise _SimulatedDurabilityError("simulated durability failure")

        monkeypatch.setattr(os, "fsync", boom_fsync)
        path = tmp_path / "f.txt"
        with pytest.raises(
            _SimulatedDurabilityError, match="simulated durability failure"
        ):
            atomic_write_bytes(path, b"never lands")
        assert not path.exists()
        leftovers = list(tmp_path.iterdir())
        assert leftovers == [], (
            f"expected zero leftover files after a pre-rename failure; "
            f"got {[p.name for p in leftovers]}. The tmp must be "
            f"unlinked on any failure path before rename."
        )

    def test_empty_payload_is_valid(self, tmp_path: Path) -> None:
        """Writing zero bytes is a valid use — empty config files,
        sentinel files, etc. The helper must not special-case empty
        and skip the rename."""
        path = tmp_path / "empty"
        atomic_write_bytes(path, b"")
        assert path.exists()
        assert path.read_bytes() == b""


# ---------------------------------------------------------------------------
# replace_atomic — bounded, Windows-only retry around `os.replace`.
#
# POSIX renames over an open destination happily; Windows raises
# PermissionError (ERROR_ACCESS_DENIED / ERROR_SHARING_VIOLATION) while a
# handle is open. That transient window turned a concurrent store mutation
# into a hard failure instead of the documented ConcurrentUpdateError, and
# flaked test_mark_verified_cas_threaded_one_winner on the windows-latest leg.
# ---------------------------------------------------------------------------


class TestReplaceAtomicRetry:
    def _force_win32(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Take the Windows branch regardless of the host platform."""
        # `_fsutil`'s `sys` / `time` globals ARE these module objects, so
        # patching here is what the code under test sees. Patch the modules
        # directly rather than reaching through `_fsutil.<mod>`, which mypy
        # rejects as a non-exported attribute.
        monkeypatch.setattr(sys, "platform", "win32")
        # Keep the suite fast: the real backoff sleeps up to ~150ms.
        monkeypatch.setattr(time, "sleep", lambda _s: None)

    def test_retries_then_succeeds_on_windows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transient PermissionError is retried, not surfaced."""
        self._force_win32(monkeypatch)
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.write_bytes(b"payload")

        calls: list[int] = []
        real_replace = os.replace

        def flaky(a: object, b: object) -> None:
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(5, "Access is denied")
            real_replace(a, b)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "replace", flaky)
        _fsutil.replace_atomic(src, dst)

        assert len(calls) == 3
        assert dst.read_bytes() == b"payload"
        assert not src.exists()

    def test_reraises_original_error_after_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A PERSISTENT failure still surfaces, with its real type and
        errno — the retry must not convert a genuine permission problem
        into a hang or a swallowed error."""
        self._force_win32(monkeypatch)
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.write_bytes(b"payload")

        calls: list[int] = []

        def always_denied(a: object, b: object) -> None:
            calls.append(1)
            raise PermissionError(5, "Access is denied")

        monkeypatch.setattr(os, "replace", always_denied)
        with pytest.raises(PermissionError) as excinfo:
            _fsutil.replace_atomic(src, dst)

        assert excinfo.value.errno == 5
        assert len(calls) == _fsutil._REPLACE_ATTEMPTS

    def test_does_not_retry_non_permission_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deliberately narrow: ENOSPC / EXDEV and friends are not
        transient. A blanket `except OSError` here would disguise a full
        disk as a slow rename."""
        self._force_win32(monkeypatch)
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.write_bytes(b"payload")

        calls: list[int] = []

        def no_space(a: object, b: object) -> None:
            calls.append(1)
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "replace", no_space)
        with pytest.raises(OSError) as excinfo:
            _fsutil.replace_atomic(src, dst)

        assert excinfo.value.errno == 28
        assert len(calls) == 1, "non-transient errors must not be retried"

    def test_posix_does_not_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On POSIX a PermissionError from os.replace is never the
        open-handle race — it means the directory genuinely is not
        writable. Retrying would delay a real diagnosis."""
        monkeypatch.setattr(sys, "platform", "linux")
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.write_bytes(b"payload")

        calls: list[int] = []

        def denied(a: object, b: object) -> None:
            calls.append(1)
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(os, "replace", denied)
        with pytest.raises(PermissionError):
            _fsutil.replace_atomic(src, dst)

        assert len(calls) == 1, "POSIX must call straight through"

    def test_atomic_write_bytes_routes_through_the_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pin the wiring: the store's write path must get the retry,
        not just direct replace_atomic callers."""
        self._force_win32(monkeypatch)
        target = tmp_path / "memory.md"
        target.write_bytes(b"old")

        calls: list[int] = []
        real_replace = os.replace

        def flaky(a: object, b: object) -> None:
            calls.append(1)
            if len(calls) < 2:
                raise PermissionError(32, "The process cannot access the file")
            real_replace(a, b)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "replace", flaky)
        _fsutil.atomic_write_bytes(target, b"new", mode=0o600)

        assert len(calls) == 2
        assert target.read_bytes() == b"new"
        # The orphan-tmp cleanup must not have fired on the successful path.
        assert not list(tmp_path.glob("*.tmp"))


# ---------------------------------------------------------------------------
# Structural guard — close the CLASS, not the instance.
#
# 3.25.1 added the Windows retry and wired up three rename sites
# (`atomic_write_bytes` plus the two in `events.py`). A FOURTH site was
# missed: `semantic.flush_persistent_cache` renamed the embedding-cache
# `.npz` into place with a bare `tmp.replace(dst)`. Nothing caught it,
# because a grep for `os.replace` cannot see a `Path.replace` — the
# rename primitive has four spellings and the earlier reasoning only
# considered one of them.
#
# So the guard below does NOT look for a string. It parses every module
# under the package with `ast` and flags every *call* that renames a
# file, in any spelling, anywhere except inside `replace_atomic` itself.
# Two prior guards in this repo rotted by being too literal (a substring
# grep counting the wrong thing; a structural check that could only see
# module constants and therefore missed a runtime-composed name), so
# `TestRenameSiteDetector` below separately pins that the *detector*
# still fires — a structural guard whose matcher silently stops matching
# passes vacuously, which is worse than no guard at all.
# ---------------------------------------------------------------------------

#: (module filename, enclosing function) pairs permitted to rename a file
#: directly. `replace_atomic` IS the retry, so it is the one exemption.
_RENAME_EXEMPTIONS = {("_fsutil.py", "replace_atomic")}

#: Module-qualified rename primitives, keyed by the canonical module name.
#: `shutil.move` belongs here and NOT because it is exotic: on a
#: same-filesystem move it degrades to `os.rename`, so it carries the
#: identical Windows open-destination exposure as `os.replace` while
#: being invisible to a guard that only knows the `os`/`pathlib`
#: spellings. `os.renames` is `os.rename` plus makedirs/removedirs — the
#: rename hop inside it is exactly the exposed one.
_MODULE_RENAME_FUNCS: dict[str, frozenset[str]] = {
    "os": frozenset({"replace", "rename", "renames"}),
    "shutil": frozenset({"move"}),
}

#: Bare-attribute (unknown-receiver) rename methods on `pathlib.Path`.
#: `move`/`move_into` are new in Python 3.14; they are listed
#: unconditionally because the detector parses SOURCE, not the running
#: interpreter — a 3.14-only spelling committed here must be caught even
#: when the guard runs on 3.11. Split by whether the name is shared with
#: a non-path type (see `_rename_call_kind` for the arity rule).
_PATH_RENAME_METHODS_UNIQUE = frozenset({"rename", "move_into"})
_PATH_RENAME_METHODS_SHARED = frozenset({"replace", "move"})

#: Keyword spelling of the single target argument on the shared-name
#: `Path` renames. `Path.replace(self, target)` and `Path.move(self,
#: target)` both accept it, so `p.replace(target=dst)` is a real rename
#: that a positional-only arity rule misses. Keying on the name adds no
#: false-positive surface for the three types the arity rule exists to
#: exclude: `str`, `bytes` and `datetime` all raise `TypeError` when
#: handed `target=`, asserted against the running interpreter in
#: `test_keyword_target_is_a_rename_and_the_excluded_types_reject_it`.
_PATH_TARGET_KEYWORD = "target"

#: Receiver names that make an attribute call an UNBOUND / explicit-receiver
#: one — `Path.replace(src, dst)`, `pathlib.Path.replace(src, dst)`. The
#: receiver type is spelled out there, so the arity rule does not need to
#: guess and must not be applied: before this was added, `Path.replace(a,
#: b)` evaded the guard while the identical `Path.rename(a, b)` was
#: caught, purely because `rename` sits in the UNIQUE table (no arity
#: check) and `replace` in the SHARED one.
#:
#: The two concrete subclasses are listed alongside `Path` because they
#: inherit all four rename methods and are directly instantiable, so
#: `PosixPath.replace(a, b)` is the same call written differently.
#: `PurePath` is deliberately absent — it carries none of them.
_PATH_CLASS_NAMES = frozenset({"Path", "PosixPath", "WindowsPath"})


def _receiver_name(node: ast.expr) -> str | None:
    """Terminal name of an attribute receiver — ``Path`` for both
    ``Path.replace`` and ``pathlib.Path.replace``. ``None`` when the
    receiver is not a plain name or attribute chain."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _rename_call_kind(
    call: ast.Call,
    module_aliases: dict[str, str],
    bare_names: dict[str, str],
) -> str | None:
    """Classify ``call`` as a file-rename primitive, or ``None``.

    Covers every stdlib spelling of "move a file over a destination":
    ``os.replace``, ``os.rename``, ``os.renames``, ``shutil.move``,
    ``Path.replace``, ``Path.rename``, ``Path.move``, ``Path.move_into``
    — including ``import os as _os`` / ``import shutil as sh`` aliases
    and ``from os import replace`` / ``from shutil import move``
    bare-name imports.

    **One import style is NOT covered, and is backstopped rather than
    closed.** ``from os import *`` followed by a bare ``replace(a, b)``
    evades this function: the bare-name table is built from the explicit
    names in an ``ImportFrom``, and a star import lists only ``"*"``, so
    nothing is registered. Closing it here would mean resolving the
    imported module's real exports at parse time, which this detector
    does not do. What keeps it from being exploitable today is a
    different tool: this repo sets no ``select`` under
    ``[tool.ruff.lint]``, so ruff's default rule set (pyflakes included)
    is active and reports ``F403`` on the star import itself plus
    ``F405`` on each name used through it, and ``ruff check .`` runs in
    the gate. That is ruff holding the line, not this detector — and it
    holds only as far as ruff does: an explicit ``# noqa: F403`` silences
    it (verified). The claim here is "defended in depth", not
    "impossible".

    **Why arity, and what it actually buys.** For the two names shared
    with non-path types (``replace``, ``move``) the receiver is usually
    unknown at parse time, so such a call is accepted at exactly one
    positional argument and no keywords. What that rules out with
    certainty is ``str.replace`` / ``bytes.replace``: those require at
    least TWO positional arguments (one is a ``TypeError``), and they
    are the overwhelmingly common ``.replace`` in this codebase — a
    guard that flagged them would be switched off within a day.

    Two shapes the positional rule alone let through are now closed
    rather than tolerated, because both are ordinary Python that a real
    rename could be written in:

    * ``p.replace(target=dst)`` / ``p.move(target=dst)`` — the keyword
      form of the same one-argument call. Admitted via
      ``_PATH_TARGET_KEYWORD``; ``str``, ``bytes`` and ``datetime`` all
      reject that keyword with ``TypeError``, so admitting it costs
      nothing against the types the arity rule exists to exclude.
    * ``Path.replace(a, b)`` — the unbound / explicit-receiver form.
      Admitted via ``_PATH_CLASS_NAMES``, which closes an asymmetry that
      had no justification: the identical ``Path.rename(a, b)`` was
      already caught, only because ``rename`` happens to live in the
      UNIQUE table where no arity check runs.

    It does NOT rule out ``datetime.replace``. Contrary to the note this
    docstring used to carry, ``datetime.replace`` is not keyword-only:
    ``dt.replace(2021)`` is legal and returns a datetime, so it would be
    reported as a rename. That is an accepted residual, and the
    asymmetry is deliberate — a false positive is a red build on a
    reviewable line, whereas a false negative is the silent class-left-
    open failure this guard exists to prevent. Nobody writes positional
    ``datetime.replace``; if someone does, the fix is to make it
    keyword, which is better code anyway.

    **Known bound: this matches Call nodes, so indirection evades it.**
    ``f = os.replace`` followed by ``f(a, b)``, ``getattr(os,
    "replace")(a, b)``, or a rename reached through a dispatch table are
    all invisible here. So is the rebound-class form ``P = Path`` then
    ``P.replace(a, b)``, which is the residual left by the
    explicit-receiver rule above — ``_PATH_CLASS_NAMES`` matches the name
    as written, not the object it resolves to. All of these are the same
    class: closing them needs dataflow analysis, which would add
    substantial false-positive surface to a guard whose entire value
    depends on staying enabled. They are left open knowingly, and what
    they have in common is the reason: the call site's own syntax does
    not say what is being called, and syntax is all an AST matcher has.
    Also invisible by construction — a rename performed inside a
    third-party callee, or shelled out to (``subprocess.run(["mv",
    ...])``).

    **What IS mitigated** is the pair of blind spots that syntax can
    reach: "a rename NAME nobody thought of" and "a call FORM nobody
    thought of". See
    ``TestRenameSiteDetector.test_covers_every_stdlib_rename_spelling``,
    which derives both the names and the ``Path`` call forms (bound
    positional, bound keyword, unbound) from the live stdlib rather than
    from these hand-written tables — so a name we forgot, a form we
    forgot, or one a future Python adds turns the guard red rather than
    passing vacuously.
    """
    func = call.func
    if isinstance(func, ast.Name) and func.id in bare_names:
        return f"{func.id}() imported bare from {bare_names[func.id]}"
    if not isinstance(func, ast.Attribute):
        return None
    if isinstance(func.value, ast.Name) and func.value.id in module_aliases:
        origin = module_aliases[func.value.id]
        if func.attr in _MODULE_RENAME_FUNCS[origin]:
            return f"{origin}.{func.attr}()"
    if func.attr in _PATH_RENAME_METHODS_UNIQUE:
        # No str/bytes/datetime method is named `rename` or `move_into`,
        # so any attribute call of that name is a path rename.
        return f"Path.{func.attr}()"
    if func.attr in _PATH_RENAME_METHODS_SHARED:
        if _receiver_name(func.value) in _PATH_CLASS_NAMES:
            # Explicit receiver: the type is written down, so the arity
            # rule has nothing to disambiguate and must not gate this.
            return f"Path.{func.attr}()"
        if len(call.args) == 1 and not call.keywords:
            return f"Path.{func.attr}()"
        if (
            not call.args
            and len(call.keywords) == 1
            and call.keywords[0].arg == _PATH_TARGET_KEYWORD
        ):
            return f"Path.{func.attr}()"
    return None


def _rename_sites(source: str, filename: str) -> list[tuple[int, str, str, str]]:
    """Return ``(lineno, kind, enclosing_scope, snippet)`` for every
    file-rename call in ``source``."""
    tree = ast.parse(source, filename)

    # local name -> canonical module name, for `import os` / `import
    # shutil as sh` / `import os.path` (which binds the top-level `os`).
    module_aliases: dict[str, str] = {}
    # local name -> canonical module name, for `from os import replace`.
    bare_names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname is None:
                    # `import os.path` binds `os`, not `os.path`.
                    bound = alias.name.split(".")[0]
                    if bound in _MODULE_RENAME_FUNCS:
                        module_aliases[bound] = bound
                elif alias.name in _MODULE_RENAME_FUNCS:
                    module_aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in _MODULE_RENAME_FUNCS:
            for alias in node.names:
                if alias.name in _MODULE_RENAME_FUNCS[node.module]:
                    bare_names[alias.asname or alias.name] = node.module

    found: list[tuple[int, str, str, str]] = []
    scoped = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

    def walk(node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, scoped):
                walk(child, (*scope, child.name))
                continue
            if isinstance(child, ast.Call):
                kind = _rename_call_kind(child, module_aliases, bare_names)
                if kind is not None:
                    found.append(
                        (child.lineno, kind, ".".join(scope), ast.unparse(child))
                    )
            walk(child, scope)

    walk(tree, ())
    return found


class TestEveryRenameRoutesThroughReplaceAtomic:
    def test_no_bare_rename_sites_under_src(self) -> None:
        """Every file rename in the package must go through
        `_fsutil.replace_atomic`, which carries the bounded Windows-only
        retry for the rename-over-open-destination race.

        A new rename site added ANYWHERE else turns this red. That is
        the entire point: the Windows failure mode is invisible on the
        POSIX dev box and on the POSIX CI legs, so nothing else in the
        suite would notice a fifth site being introduced.
        """
        package_root = Path(bettermemory.__file__).parent
        modules = sorted(package_root.rglob("*.py"))
        assert modules, f"found no modules under {package_root}"

        offenders: list[str] = []
        for module in modules:
            for lineno, kind, scope, snippet in _rename_sites(
                module.read_text(encoding="utf-8"), str(module)
            ):
                if (module.name, scope) in _RENAME_EXEMPTIONS:
                    continue
                rel = module.relative_to(package_root)
                offenders.append(
                    f"{rel}:{lineno} in {scope or '<module>'}(): {kind} -> {snippet}"
                )

        assert not offenders, (
            "these call sites rename a file without the Windows retry:\n  "
            + "\n  ".join(offenders)
            + "\n\nRoute them through `_fsutil.replace_atomic(src, dst)`. On "
            "Windows a rename over a destination another process still has "
            "open fails with PermissionError (ERROR_ACCESS_DENIED / "
            "ERROR_SHARING_VIOLATION); POSIX allows it, so this failure mode "
            "is invisible on the POSIX CI legs. Do NOT widen the retry to a "
            "blanket `except OSError` — that would mask ENOSPC."
        )

    def test_replace_atomic_is_exported(self) -> None:
        """`replace_atomic` is the designated helper for this entire
        class — `events.py` imports it directly and the module's own
        write helpers route through it — but it was missing from
        `_fsutil.__all__`, so the module's own declared public surface
        disagreed with the guard above, which requires every rename in
        the package to route through it. A helper you are REQUIRED to
        use must be exported.
        """
        assert "replace_atomic" in _fsutil.__all__, (
            "`replace_atomic` is absent from `_fsutil.__all__` despite "
            "being the mandatory rename helper for the whole package"
        )
        assert sorted(_fsutil.__all__) == list(_fsutil.__all__), (
            "`_fsutil.__all__` is no longer sorted"
        )

    def test_replace_atomic_is_the_only_exemption(self) -> None:
        """The exemption list must not quietly grow. Adding an entry is
        how this guard would be neutered, so the shape of the allowlist
        is itself pinned."""
        assert _RENAME_EXEMPTIONS == {("_fsutil.py", "replace_atomic")}


class TestRenameSiteDetector:
    """The detector must actually fire. A structural guard whose matcher
    stops matching passes vacuously — this repo has shipped exactly that
    failure twice (a substring grep counting the wrong thing; a guard
    that could only see module constants). These cases are the proof
    that `test_no_bare_rename_sites_under_src` can go red."""

    @pytest.mark.parametrize(
        ("source", "expected_kind"),
        [
            ("import os\ndef f(a, b):\n    os.replace(a, b)\n", "os.replace()"),
            ("import os\ndef f(a, b):\n    os.rename(a, b)\n", "os.rename()"),
            ("import os\ndef f(a, b):\n    os.renames(a, b)\n", "os.renames()"),
            ("def f(tmp, dst):\n    tmp.replace(dst)\n", "Path.replace()"),
            ("def f(tmp, dst):\n    tmp.rename(dst)\n", "Path.rename()"),
            # Python 3.14 pathlib additions. Parsed from source, so these
            # are detected on 3.11 too — the guard must not depend on the
            # spelling existing in the interpreter running the tests.
            ("def f(tmp, dst):\n    tmp.move(dst)\n", "Path.move()"),
            ("def f(tmp, d):\n    tmp.move_into(d)\n", "Path.move_into()"),
            # `shutil.move` degrades to `os.rename` on a same-filesystem
            # move, so it carries the identical Windows open-destination
            # exposure. An adversarial verifier planted exactly this call
            # in store.py and the pre-fix guard passed GREEN.
            (
                "import shutil\ndef f(a, b):\n    shutil.move(a, b)\n",
                "shutil.move()",
            ),
            (
                "import shutil as sh\ndef f(a, b):\n    sh.move(a, b)\n",
                "shutil.move()",
            ),
            (
                "from shutil import move\ndef f(a, b):\n    move(a, b)\n",
                "move() imported bare from shutil",
            ),
            (
                "from shutil import move as mv\ndef f(a, b):\n    mv(a, b)\n",
                "mv() imported bare from shutil",
            ),
            (
                "import os as _o\ndef f(a, b):\n    _o.replace(a, b)\n",
                "os.replace()",
            ),
            # `import os.path` binds the top-level `os` name, so the
            # module-qualified spelling is reachable without a bare
            # `import os` anywhere in the file.
            (
                "import os.path\ndef f(a, b):\n    os.replace(a, b)\n",
                "os.replace()",
            ),
            (
                "from os import replace\ndef f(a, b):\n    replace(a, b)\n",
                "replace() imported bare from os",
            ),
            (
                "from os import replace as mv\ndef f(a, b):\n    mv(a, b)\n",
                "mv() imported bare from os",
            ),
            (
                "from os import renames\ndef f(a, b):\n    renames(a, b)\n",
                "renames() imported bare from os",
            ),
            # Call FORMS the positional-arity rule alone let through.
            # Each matches a real `Path` signature (`replace(self,
            # target)`, `move(self, target)`), so each was a live way to
            # spell a rename that the guard reported as clean.
            ("def f(p, dst):\n    p.replace(target=dst)\n", "Path.replace()"),
            ("def f(p, dst):\n    p.move(target=dst)\n", "Path.move()"),
            ("def f(a, b):\n    Path.replace(a, b)\n", "Path.replace()"),
            # ...the unbound form also reached through the module, and
            # via a concrete subclass.
            ("def f(a, b):\n    pathlib.Path.move(a, b)\n", "Path.move()"),
            ("def f(a, b):\n    PosixPath.replace(a, b)\n", "Path.replace()"),
        ],
    )
    def test_detects_every_rename_spelling(
        self, source: str, expected_kind: str
    ) -> None:
        sites = _rename_sites(source, "<synthetic>")
        assert len(sites) == 1, f"expected one site, got {sites!r}"
        assert sites[0][1] == expected_kind
        assert sites[0][2] == "f", "enclosing scope must be reported"

    def test_covers_every_stdlib_rename_spelling(self) -> None:
        """The enumeration's real blind spot is "a spelling nobody
        thought of" — which is exactly how `shutil.move` slipped past
        the first version of this guard.

        So derive the expected coverage from the LIVE stdlib instead of
        from the detector's own hand-written list, and plant each
        derived spelling to confirm it is caught. A name we forgot, or
        one a future Python adds (`Path.move` / `Path.move_into` landed
        in 3.14 and are already covered), turns this red rather than
        passing vacuously.

        For the `Path` methods this now derives every CALL FORM the real
        signature admits — bound positional, bound keyword, unbound —
        not just the bound positional one. Deriving only that one form is
        why `p.replace(target=dst)` and `Path.replace(a, b)` could evade
        the detector while this test stayed green.

        Still not caught: a rename primitive whose NAME is outside the
        vocabulary below, and the indirection forms — both residuals are
        documented in `_rename_call_kind`.
        """
        vocab = {"rename", "renames", "replace", "move", "move_into"}
        uncovered: list[str] = []

        for modname, mod in (("os", os), ("shutil", shutil)):
            for name in sorted(vocab & set(dir(mod))):
                if not callable(getattr(mod, name)):
                    continue
                source = f"import {modname}\ndef f(a, b):\n    {modname}.{name}(a, b)\n"
                if not _rename_sites(source, "<derived>"):
                    uncovered.append(f"{modname}.{name}")

        for name in sorted(vocab & set(dir(Path))):
            # Every CALL FORM the real signature admits, derived from the
            # live signature rather than assumed. The bound-positional
            # form was all this test used to check, which is why the
            # keyword and unbound forms could evade the detector while
            # this test stayed green.
            param = list(inspect.signature(getattr(Path, name)).parameters)[1]
            forms = {
                "bound positional": f"def f(p, dst):\n    p.{name}(dst)\n",
                "bound keyword": f"def f(p, dst):\n    p.{name}({param}=dst)\n",
                "unbound": f"def f(a, b):\n    Path.{name}(a, b)\n",
            }
            for form, source in forms.items():
                if not _rename_sites(source, "<derived>"):
                    uncovered.append(f"Path.{name} ({form})")

        assert not uncovered, (
            "the rename detector does not cover these stdlib spellings: "
            f"{uncovered}. Each one moves a file over a destination and "
            "therefore carries the Windows open-destination exposure "
            "`replace_atomic` exists to absorb. Add them to "
            "`_MODULE_RENAME_FUNCS` / `_PATH_RENAME_METHODS_*`, or widen "
            "`_PATH_TARGET_KEYWORD` / `_PATH_CLASS_NAMES` if it is a call "
            "FORM rather than a name that is missing."
        )

    @pytest.mark.parametrize(
        "source",
        [
            # str.replace — two positional args. The overwhelmingly common
            # `.replace` in this codebase; a guard that flagged these would
            # be turned off within a day.
            "def f(s):\n    return s.replace('a', 'b')\n",
            "def f(s):\n    return s.replace('+00:00', 'Z')\n",
            # datetime.replace in its keyword form — the only form this
            # codebase uses. NOTE the positional form `dt.replace(2021)`
            # IS legal Python and WOULD be flagged; see the accepted-
            # residual note in `_rename_call_kind`.
            "def f(dt, tz):\n    return dt.replace(tzinfo=tz)\n",
            # str.replace with the optional count.
            "def f(s):\n    return s.replace('a', 'b', 1)\n",
        ],
    )
    def test_does_not_flag_string_or_datetime_replace(self, source: str) -> None:
        assert _rename_sites(source, "<synthetic>") == []

    def test_arity_rule_is_what_excludes_str_replace_not_a_type_guess(self) -> None:
        """Pin the documented basis of the arity rule against the real
        interpreter, so the docstring cannot drift back into the false
        premise it used to carry.

        The rule only claims to exclude `str`/`bytes`.replace, which
        genuinely REQUIRE two positional arguments. It does NOT claim
        `datetime.replace` is keyword-only — it isn't, and the accepted
        consequence is a false positive on the positional form.
        """
        import datetime

        with pytest.raises(TypeError):
            "abc".replace("a")  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            b"abc".replace(b"a")  # type: ignore[call-arg]

        # The premise the old docstring asserted, falsified: a single
        # positional argument is accepted by datetime.replace.
        assert datetime.datetime(2020, 1, 1).replace(2021).year == 2021
        assert _rename_sites(
            "def f(dt):\n    return dt.replace(2021)\n", "<synthetic>"
        ) == [(2, "Path.replace()", "f", "dt.replace(2021)")]

    def test_keyword_target_is_a_rename_and_the_excluded_types_reject_it(
        self,
    ) -> None:
        """Pin the premise behind `_PATH_TARGET_KEYWORD` against the real
        interpreter: `target=` is the genuine parameter name on the
        shared-name `Path` renames, and the three types the arity rule
        exists to exclude — `str`, `bytes`, `datetime` — all reject it.
        So admitting the keyword form costs no false positives against
        those. It says nothing about arbitrary third-party objects that
        might define `.replace(target=...)`; none exist in this package,
        and a false positive there would be a red build on a reviewable
        line rather than a silent miss.
        """
        import datetime

        assert list(inspect.signature(Path.replace).parameters)[1] == (
            _PATH_TARGET_KEYWORD
        )

        with pytest.raises(TypeError):
            "abc".replace(target="a")  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            b"abc".replace(target=b"a")  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            datetime.datetime(2020, 1, 1).replace(target=2021)  # type: ignore[call-arg]

        # Some other keyword is not a rename, so `dt.replace(year=...)`
        # style calls stay clean.
        assert _rename_sites("def f(dt):\n    dt.replace(year=2021)\n", "<x>") == []

    def test_unbound_coverage_is_symmetric_between_the_two_tables(self) -> None:
        """`Path.rename(a, b)` was caught and `Path.replace(a, b)` was
        not — not by design, but because `rename` sits in the UNIQUE
        table (no arity check) and `replace` in the SHARED one. Pin that
        the explicit-receiver form is now recognised for BOTH tables, so
        the asymmetry cannot silently come back.
        """
        for name in sorted(_PATH_RENAME_METHODS_UNIQUE | _PATH_RENAME_METHODS_SHARED):
            sites = _rename_sites(f"def f(a, b):\n    Path.{name}(a, b)\n", "<x>")
            assert [s[1] for s in sites] == [f"Path.{name}()"], (
                f"the unbound form `Path.{name}(a, b)` is not detected; the "
                "UNIQUE and SHARED tables have diverged in coverage again"
            )

    def test_star_import_residual_is_real_and_ruff_is_what_backstops_it(
        self, tmp_path: Path
    ) -> None:
        """The docstring claims two things about `from os import *`: that
        this detector misses it, and that ruff refuses it under this
        repo's config. Both are claims, so both get checked — the first
        so the docstring cannot outlive the hole it describes, the second
        so "backstopped by ruff" is not taken on faith.

        The probe is linted out-of-tree with `--config` pointing at the
        repo's own pyproject, so this exercises the real configuration
        without ever writing a file into the package.
        """
        star = "from os import *\n\n\ndef f(a, b):\n    replace(a, b)\n"
        assert _rename_sites(star, "<synthetic>") == [], (
            "the star-import form is now detected — good, but the "
            "`_rename_call_kind` docstring still documents it as an open "
            "residual backstopped by ruff. Update the docstring."
        )

        ruff = shutil.which("ruff")
        if ruff is None:  # pragma: no cover - ruff is a dev dependency
            pytest.skip("ruff not on PATH")

        repo_root = Path(__file__).resolve().parent.parent
        config = repo_root / "pyproject.toml"
        assert config.is_file(), "repo pyproject.toml not found next to tests/"
        probe = tmp_path / "star_import_probe.py"
        probe.write_text(star, encoding="utf-8")

        result = subprocess.run(
            [
                ruff,
                "check",
                "--config",
                str(config),
                "--output-format",
                "concise",
                str(probe),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert "F403" in result.stdout, (
            "ruff did not report F403 on a star import under this repo's "
            "config, so the star-import residual documented in "
            "`_rename_call_kind` is NOT backstopped and that docstring is "
            f"wrong. ruff said: {result.stdout!r} {result.stderr!r}"
        )

    def test_planting_each_spelling_in_a_real_module_goes_red(self) -> None:
        """The end-to-end proof: take a REAL package module, append one
        planted rename in each covered spelling, and confirm the guard's
        own offender filter reports it.

        This is the adversarial verifier's exact move. It planted
        `shutil.move(a, b)` in store.py and the pre-fix guard passed
        GREEN, because the detector only knew the `os`/`pathlib`
        spellings. The plants below are appended to store.py's genuine
        source, which currently contains no rename sites at all, so any
        site reported is unambiguously the plant.
        """
        package_root = Path(bettermemory.__file__).parent
        store_source = (package_root / "store.py").read_text(encoding="utf-8")
        assert _rename_sites(store_source, "store.py") == [], (
            "store.py already contains a rename site; this test's premise "
            "(a clean host module) no longer holds"
        )

        plants = {
            "shutil.move()": "import shutil\ndef _planted(a, b):\n    shutil.move(a, b)\n",
            "os.replace()": "import os\ndef _planted(a, b):\n    os.replace(a, b)\n",
            "os.rename()": "import os\ndef _planted(a, b):\n    os.rename(a, b)\n",
            "os.renames()": "import os\ndef _planted(a, b):\n    os.renames(a, b)\n",
            "Path.replace()": "def _planted(a, b):\n    a.replace(b)\n",
            "Path.rename()": "def _planted(a, b):\n    a.rename(b)\n",
            "Path.move()": "def _planted(a, b):\n    a.move(b)\n",
            "Path.move_into()": "def _planted(a, d):\n    a.move_into(d)\n",
            "move() imported bare from shutil": (
                "from shutil import move\ndef _planted(a, b):\n    move(a, b)\n"
            ),
        }
        # The three call FORMS that evaded the positional-arity rule.
        # Same adversarial move one level down: the NAME was known, the
        # spelling of the call was not. Keyed by form because several
        # share a reported kind.
        form_plants = {
            "Path.replace() via target= keyword": (
                "Path.replace()",
                "def _planted(a, b):\n    a.replace(target=b)\n",
            ),
            "Path.move() via target= keyword": (
                "Path.move()",
                "def _planted(a, b):\n    a.move(target=b)\n",
            ),
            "Path.replace() unbound": (
                "Path.replace()",
                "def _planted(a, b):\n    Path.replace(a, b)\n",
            ),
        }
        cases = [(k, k, v) for k, v in plants.items()]
        cases += [(label, kind, src) for label, (kind, src) in form_plants.items()]

        for label, expected_kind, snippet in cases:
            sites = _rename_sites(f"{store_source}\n\n{snippet}", "store.py")
            offenders = [
                s for s in sites if ("store.py", s[2]) not in _RENAME_EXEMPTIONS
            ]
            assert offenders, (
                f"planting {label} in store.py did NOT turn the "
                "rename guard red — this spelling evades the detector, "
                "exactly the hole `shutil.move` occupied"
            )
            assert [s[1] for s in offenders] == [expected_kind]
            assert [s[2] for s in offenders] == ["_planted"]

    def test_finds_sites_at_module_scope_and_in_nested_functions(self) -> None:
        """Scope tracking must not be fooled by nesting — an exemption
        keys on (file, function), so a rename hidden inside a nested
        helper must report the nested name, not the outer one."""
        source = (
            "import os\n"
            "os.replace('a', 'b')\n"
            "class C:\n"
            "    def outer(self):\n"
            "        def inner(a, b):\n"
            "            os.replace(a, b)\n"
        )
        sites = _rename_sites(source, "<synthetic>")
        assert [(s[0], s[2]) for s in sites] == [(2, ""), (6, "C.outer.inner")]

    def test_real_replace_atomic_is_seen_and_exempted(self) -> None:
        """Anchor the exemption against the real file: `replace_atomic`
        genuinely contains rename calls, and they are genuinely the ones
        the allowlist pardons. If `replace_atomic` were renamed or its
        `os.replace` calls refactored away, this fails loudly rather
        than leaving a stale exemption behind."""
        fsutil_path = Path(_fsutil.__file__)
        sites = _rename_sites(fsutil_path.read_text(encoding="utf-8"), "_fsutil.py")
        scopes = {scope for _, _, scope, _ in sites}
        assert scopes == {"replace_atomic"}, (
            f"_fsutil.py renames outside replace_atomic: {sites!r}"
        )
        assert len(sites) >= 1
