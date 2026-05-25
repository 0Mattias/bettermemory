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
        """A non-numeric ``BETTERMEMORY_FLOCK_TIMEOUT`` must NOT raise
        ``ValueError`` out of the lock acquire path — the helper
        catches the parse error and falls back to the 30s default.

        The earlier version of this test combined garbage env-var with
        a no-failure fake, which proved only that ``float()`` didn't
        explode — the retry loop was never entered, so even a
        regression that fell back to ``timeout = 0`` (a real bug:
        lock acquire would instantly time out under any contention)
        would have passed. This stronger version uses an always-failing
        fake so the retry loop MUST run, and asserts (1) the loop
        does eventually raise ``TimeoutError`` (proving 30s is finite,
        not infinite), and (2) the loop made more than one attempt
        before raising (proving ``timeout > 0`` — a regression to
        ``timeout = 0`` would bail after attempt 1 with
        ``call_count == 1``)."""
        monkeypatch.setattr("time.sleep", lambda _s: None)
        monkeypatch.setattr(_fsutil, "_FLOCK_WARNED", False)
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

        # call_count > 1 is the load-bearing assertion: it proves the
        # retry loop ran at least one full lap (attempt → sleep →
        # attempt). A regression that fell back to ``timeout = 0``
        # would exit on the first deadline check after attempt 1, giving
        # ``call_count == 1`` and tripping this — distinguishing the
        # real 30s default from an accidental zero.
        assert fake.lock_attempts > 1, (
            f"retry loop must have iterated more than once under the "
            f"30s default ceiling; got {fake.lock_attempts} attempt(s). "
            f"If this is 1, the env-var fallback set ``timeout = 0`` — "
            f"a real bug that this test now catches."
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
