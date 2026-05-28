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
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import types
from pathlib import Path

import pytest

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
        no-chmod contract leaves room for the future helper-migrate of
        the canonical `_atomic_write_post` (queue #29) which would need
        an explicit-mode story it can re-derive without surprise from
        an unconditional chmod here."""
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
