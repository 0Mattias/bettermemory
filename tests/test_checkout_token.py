"""Tests for checkout_token.py: the one file bettermemory writes into a
repository, `info/bettermemory` in the git directory (design 1.1, decisions
D10 and D11).

Every repository is built with git under ``tmp_path`` with a neutral git
configuration (an empty global file, no system file, a fixed identity), the
shape of the round 2 experiments' lab. Tests that name a design section 7
row carry it in their name (I16, I19, I22, I23, I34, X7, X8, X9); the git
checks of E19 a (status, clone, maintenance) close the file.
"""

from __future__ import annotations

import errno
import multiprocessing
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from bettermemory import checkout_token
from bettermemory.checkout_token import (
    CROCKFORD,
    TOKEN_HEADER,
    TOKEN_LENGTH,
    TokenRead,
    TokenWrite,
    mint,
    random_crockford,
    read,
    remint,
    render,
    token_path,
    write,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_POSIX_PERMISSIONS = pytest.mark.skipif(
    sys.platform == "win32" or getattr(os, "geteuid", lambda: -1)() == 0,
    reason="POSIX permission bits, not enforced for root",
)
_WRITERS = 8


@pytest.fixture(autouse=True)
def neutral_git(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No user or system git configuration, no inherited repository
    variables, a fixed identity: the lab's environment."""
    empty = tmp_path_factory.mktemp("gitconfig") / "empty"
    empty.write_text("")
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Fixture")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "fixture@example.invalid")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repo(path: Path, *, commits: int = 1) -> Path:
    """A repository at `path` with `commits` commits; returns its git
    directory."""
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    for i in range(commits):
        (path / f"file{i}.txt").write_text(f"{path.name} {i}\n")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", f"rev {i}")
    return path / ".git"


def _info_listing(git_dir: Path) -> list[str]:
    return sorted(p.name for p in (git_dir / "info").iterdir())


def _plant(git_dir: Path, body: bytes) -> Path:
    path = token_path(git_dir)
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(body)
    return path


def _refuse_links(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(src: Any, dst: Any) -> None:
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(os, "link", refused)


# ---------------------------------------------------------------------------
# The format
# ---------------------------------------------------------------------------


def test_token_file_is_a_header_line_and_26_crockford_characters(
    tmp_path: Path,
) -> None:
    git_dir = _repo(tmp_path / "proj")
    result = write(git_dir, policy=True, bare=False)
    assert result.status == "written" and result.ok
    assert result.token is not None
    body = token_path(git_dir).read_bytes()
    assert body == f"{TOKEN_HEADER}\n{result.token}\n".encode("ascii")
    header, value = body.decode("ascii").splitlines()
    assert header == "bettermemory checkout token v1"
    assert len(value) == TOKEN_LENGTH == 26
    assert set(value) <= set(CROCKFORD)
    assert not set(CROCKFORD) & set("ILOU")
    # Only git's own file and the token: no temporary file is left.
    assert _info_listing(git_dir) == ["bettermemory", "exclude"]


def test_minted_values_are_random_crockford() -> None:
    values = {mint() for _ in range(200)}
    assert len(values) == 200
    for value in values:
        assert len(value) == 26 and set(value) <= set(CROCKFORD)
    assert len(random_crockford(12)) == 12
    # 130 bits: every character position takes every symbol over enough draws.
    firsts = {mint()[0] for _ in range(2000)}
    assert firsts == set(CROCKFORD)


@pytest.mark.parametrize(
    "body",
    [
        f"{TOKEN_HEADER}\r\n0XNK4RY1ZCMBN31P1J2EN2STQP\r\n".encode("ascii"),
        f"{TOKEN_HEADER}\n0XNK4RY1ZCMBN31P1J2EN2STQP".encode("ascii"),
    ],
    ids=["crlf", "no-final-line-end"],
)
def test_line_end_variants_still_read(tmp_path: Path, body: bytes) -> None:
    git_dir = _repo(tmp_path / "proj")
    _plant(git_dir, body)
    assert read(git_dir) == TokenRead("present", token="0XNK4RY1ZCMBN31P1J2EN2STQP")


def test_render_and_parse_round_trip() -> None:
    value = mint()
    assert checkout_token.parse(render(value).decode("ascii")) == value


# ---------------------------------------------------------------------------
# Write, read, keep, remint
# ---------------------------------------------------------------------------


def test_write_then_read(tmp_path: Path) -> None:
    git_dir = _repo(tmp_path / "proj")
    assert read(git_dir) == TokenRead("absent")
    written = write(git_dir, policy=True, bare=False)
    assert read(git_dir) == TokenRead("present", token=written.token)


def test_read_without_a_git_directory_is_absent(tmp_path: Path) -> None:
    assert read(tmp_path / "nothing-here") == TokenRead("absent")


def test_an_existing_token_is_kept(tmp_path: Path) -> None:
    git_dir = _repo(tmp_path / "proj")
    first = write(git_dir, policy=True, bare=False)
    before = token_path(git_dir).read_bytes()
    second = write(git_dir, policy=True, bare=False)
    assert second == TokenWrite("exists", token=first.token)
    assert token_path(git_dir).read_bytes() == before


def test_a_planted_token_is_kept_and_reported(tmp_path: Path) -> None:
    git_dir = _repo(tmp_path / "proj")
    planted = mint()
    _plant(git_dir, render(planted))
    assert write(git_dir, policy=True, bare=False) == TokenWrite(
        "exists", token=planted
    )


def test_remint_replaces_the_token(tmp_path: Path) -> None:
    git_dir = _repo(tmp_path / "proj")
    first = write(git_dir, policy=True, bare=False)
    again = remint(git_dir, policy=True, bare=False)
    assert again.status == "written"
    assert again.token not in (None, first.token)
    assert read(git_dir) == TokenRead("present", token=again.token)
    assert _info_listing(git_dir) == ["bettermemory", "exclude"]


def test_remint_replaces_a_malformed_file(tmp_path: Path) -> None:
    git_dir = _repo(tmp_path / "proj")
    _plant(git_dir, b"garbage\n")
    result = remint(git_dir, policy=True, bare=False)
    assert result.status == "written"
    assert read(git_dir).token == result.token


def test_remint_keeps_the_same_skips(tmp_path: Path) -> None:
    git_dir = _repo(tmp_path / "proj")
    write(git_dir, policy=True, bare=False)
    before = token_path(git_dir).read_bytes()
    assert remint(git_dir, policy=False, bare=False) == TokenWrite("skipped:policy")
    assert remint(git_dir, policy=True, bare=True) == TokenWrite("skipped:bare")
    assert token_path(git_dir).read_bytes() == before


def _leftovers(git_dir: Path) -> tuple[Path, Path, Path]:
    """A temporary file a killed writer left two hours ago, a concurrent
    writer's fresh one, and an old file of another name in `info/`."""
    info = git_dir / "info"
    old = info / "bettermemory.tmp-OLDLEFTOVER0"
    fresh = info / "bettermemory.tmp-FRESHWRITER0"
    other = info / "other.tmp-OLD"
    for path in (old, fresh, other):
        path.write_bytes(b"x")
    two_hours_ago = time.time() - 2 * 3600
    for path in (old, other, info / "exclude"):
        os.utime(path, (two_hours_ago, two_hours_ago))
    return old, fresh, other


@pytest.mark.parametrize("call", [write, remint], ids=["write", "remint"])
def test_a_killed_writers_old_temporary_file_is_swept(
    tmp_path: Path, call: Any
) -> None:
    git_dir = _repo(tmp_path / "proj")
    old, fresh, other = _leftovers(git_dir)
    result = call(git_dir, policy=True, bare=False)
    assert result.status == "written"
    assert not old.exists()
    assert fresh.exists() and other.exists()
    assert read(git_dir).token == result.token
    assert _info_listing(git_dir) == sorted(
        ["bettermemory", "bettermemory.tmp-FRESHWRITER0", "exclude", "other.tmp-OLD"]
    )


def test_the_sweep_ignores_os_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_dir = _repo(tmp_path / "proj")
    old, _fresh, _other = _leftovers(git_dir)
    real_unlink = os.unlink

    def refuse_the_leftover(path: Any) -> None:
        if Path(path) == old:
            raise PermissionError(errno.EACCES, "Permission denied")
        real_unlink(path)

    monkeypatch.setattr(os, "unlink", refuse_the_leftover)
    assert write(git_dir, policy=True, bare=False).status == "written"
    assert old.exists()


def test_nothing_is_swept_when_nothing_is_written(tmp_path: Path) -> None:
    git_dir = _repo(tmp_path / "proj")
    old, _fresh, _other = _leftovers(git_dir)
    assert write(git_dir, policy=False, bare=False).status == "skipped:policy"
    assert old.exists()


# ---------------------------------------------------------------------------
# Concurrent writers (E28 c)
# ---------------------------------------------------------------------------


def _race(git_dir: Path) -> list[TokenWrite]:
    results: list[TokenWrite | None] = [None] * _WRITERS
    start = threading.Barrier(_WRITERS)

    def run(i: int) -> None:
        start.wait()
        results[i] = write(git_dir, policy=True, bare=False)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(_WRITERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(result is not None for result in results)
    return [result for result in results if result is not None]


def _assert_one_token(
    git_dir: Path, results: list[tuple[str, str | None, str]]
) -> None:
    """One writer wrote, every other found its token; a failure names each
    writer's status and detail."""
    statuses = [status for status, _, _ in results]
    tokens = {token for _, token, _ in results}
    said = [(status, detail) for status, _, detail in results]
    assert statuses.count("written") == 1, said
    assert statuses.count("exists") == len(results) - 1, said
    assert len(tokens) == 1 and None not in tokens
    assert read(git_dir).token in tokens
    assert _info_listing(git_dir) == ["bettermemory", "exclude"]


def test_i34_concurrent_writers_in_threads_leave_one_token(tmp_path: Path) -> None:
    git_dir = _repo(tmp_path / "proj")
    results = _race(git_dir)
    _assert_one_token(git_dir, [(r.status, r.token, r.detail) for r in results])


def _write_in_child(git_dir: str, start: Any, out: Any) -> None:
    start.wait()
    result = write(git_dir, policy=True, bare=False)
    out.put((result.status, result.token, result.detail))


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(), reason="no fork here"
)
@pytest.mark.filterwarnings(
    "ignore:.*use of fork\\(\\) may lead to deadlocks:DeprecationWarning"
)
def test_i34_concurrent_writers_in_processes_leave_one_token(tmp_path: Path) -> None:
    git_dir = _repo(tmp_path / "proj")
    context: Any = multiprocessing.get_context("fork")
    start = context.Barrier(_WRITERS)
    out = context.Queue()
    children = [
        context.Process(target=_write_in_child, args=(str(git_dir), start, out))
        for _ in range(_WRITERS)
    ]
    for child in children:
        child.start()
    results = [out.get(timeout=60) for _ in children]
    for child in children:
        child.join(timeout=60)
        assert child.exitcode == 0
    _assert_one_token(git_dir, results)


def test_x9_links_refused_falls_back_to_an_exclusive_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_dir = _repo(tmp_path / "proj")
    _refuse_links(monkeypatch)
    result = write(git_dir, policy=True, bare=False)
    assert result.status == "written"
    assert token_path(git_dir).read_bytes() == render(result.token or "")
    assert _info_listing(git_dir) == ["bettermemory", "exclude"]
    assert write(git_dir, policy=True, bare=False) == TokenWrite(
        "exists", token=result.token
    )


def test_x9_concurrent_writers_without_links_leave_one_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_dir = _repo(tmp_path / "proj")
    _refuse_links(monkeypatch)
    results = _race(git_dir)
    _assert_one_token(git_dir, [(r.status, r.token, r.detail) for r in results])


def test_x9_exclusive_create_losing_the_race_reports_the_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another writer creates the token between this writer's refused link
    and its exclusive create: the create fails and the winner's token is
    reported."""
    git_dir = _repo(tmp_path / "proj")
    winner = mint()

    def refused_after_another_writer(src: Any, dst: Any) -> None:
        Path(dst).write_bytes(render(winner))
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(os, "link", refused_after_another_writer)
    assert write(git_dir, policy=True, bare=False) == TokenWrite("exists", token=winner)
    assert _info_listing(git_dir) == ["bettermemory", "exclude"]


def test_a_link_losing_the_race_reports_the_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_dir = _repo(tmp_path / "proj")
    winner = mint()
    real_link = os.link

    def link_after_another_writer(src: Any, dst: Any) -> None:
        Path(dst).write_bytes(render(winner))
        real_link(src, dst)

    monkeypatch.setattr(os, "link", link_after_another_writer)
    assert write(git_dir, policy=True, bare=False) == TokenWrite("exists", token=winner)
    assert _info_listing(git_dir) == ["bettermemory", "exclude"]


def test_a_winner_that_vanishes_is_a_failure_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_dir = _repo(tmp_path / "proj")

    def exists_then_gone(src: Any, dst: Any) -> None:
        raise FileExistsError(errno.EEXIST, "File exists")

    monkeypatch.setattr(os, "link", exists_then_gone)
    result = write(git_dir, policy=True, bare=False)
    assert result.status == "failed:io" and not result.ok
    assert result.detail == "the token another writer placed was removed"
    assert not token_path(git_dir).exists()


class _Clock:
    """`time.monotonic` and `time.sleep` for one writer: a pause moves the
    clock on by its length and is kept, so a test reads every pause the
    writer took without waiting for it."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.pauses: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds


def _losing(monkeypatch: pytest.MonkeyPatch, seen: list[TokenWrite | None]) -> _Clock:
    """A writer that loses the race: its link finds the name taken, its
    first look (before it writes) finds nothing, and each read after the
    race finds the next of `seen`, the last one from then on. Returns the
    writer's clock."""
    clock = _Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    looks = iter([None, *seen])
    last = seen[-1]

    def existing(path: Path) -> TokenWrite | None:
        return next(looks, last)

    def taken(src: Any, dst: Any) -> None:
        raise FileExistsError(errno.EEXIST, "File exists")

    monkeypatch.setattr(checkout_token, "_existing", existing)
    monkeypatch.setattr(os, "link", taken)
    return clock


@pytest.mark.parametrize(
    "unfinished",
    [
        None,
        TokenWrite("malformed", detail="empty"),
        TokenWrite("malformed", detail="cut short"),
    ],
    ids=["no-file-yet", "empty", "cut-short"],
)
def test_i34_a_loser_reads_the_winners_token_once_it_shows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unfinished: TokenWrite | None
) -> None:
    """I34 on an SMB mount (cache=strict, actimeo=1): the loser's link is
    refused because the winner's name exists, and its reads still find no
    file there, or one empty or cut short. It reads again in short pauses
    until the winner's token shows, and reports it."""
    git_dir = _repo(tmp_path / "proj")
    winner = mint()
    clock = _losing(
        monkeypatch, [unfinished, unfinished, unfinished, TokenWrite("exists", winner)]
    )
    assert write(git_dir, policy=True, bare=False) == TokenWrite("exists", winner)
    assert clock.pauses == [0.01, 0.02, 0.04]
    assert _info_listing(git_dir) == ["exclude"]


def test_i34_a_loser_gives_up_once_the_bound_is_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing shows within the bound: the loser reports the winner's token
    removed, having paused no longer than the bound in all, the pauses
    growing to their longest."""
    git_dir = _repo(tmp_path / "proj")
    clock = _losing(monkeypatch, [None])
    result = write(git_dir, policy=True, bare=False)
    assert result == TokenWrite(
        "failed:io", detail="the token another writer placed was removed"
    )
    assert sum(clock.pauses) == pytest.approx(checkout_token._RACE_READ_SECONDS)
    assert max(clock.pauses) == checkout_token._RACE_PAUSE_MAX_SECONDS


@pytest.mark.parametrize("ids", [1, 3], ids=["once", "three-times"])
def test_i34_a_loser_whose_read_is_refused_reads_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ids: int
) -> None:
    """The loser's read of the winner's file refused by the filesystem for
    the moment (a cifs client answering EINVAL just after the race): read
    again in the same pauses until it answers, then the token reported."""
    git_dir = _repo(tmp_path / "proj")
    winner = mint()
    refused = TokenWrite(
        "failed:io",
        detail="the existing token cannot be read: read: Invalid argument (EINVAL)",
    )
    clock = _losing(monkeypatch, [*([refused] * ids), TokenWrite("exists", winner)])
    assert write(git_dir, policy=True, bare=False) == TokenWrite("exists", winner)
    assert len(clock.pauses) == ids


@pytest.mark.skipif(not hasattr(os, "O_NONBLOCK"), reason="no non-blocking open here")
def test_a_non_blocking_open_refused_with_einval_is_made_again_plainly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filesystem client that refuses the non-blocking open with EINVAL:
    once a stat says a regular file is there, the open is made again
    without the flag and the token is read."""
    git_dir = _repo(tmp_path / "proj")
    written = write(git_dir, policy=True, bare=False)
    real_open = os.open
    opens: list[int] = []

    nonblock = getattr(os, "O_NONBLOCK", 0)

    def refusing(path: Any, flags: int, *args: Any, **kw: Any) -> int:
        opens.append(flags)
        if flags & nonblock:
            raise OSError(errno.EINVAL, "Invalid argument")
        return real_open(path, flags, *args, **kw)

    monkeypatch.setattr(os, "open", refusing)
    assert read(git_dir) == TokenRead("present", token=written.token)
    assert len(opens) == 2 and not opens[1] & nonblock


def test_a_read_refused_with_einval_is_made_again_with_the_flag_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filesystem client that refuses a read on the non-blocking
    descriptor with EINVAL: the flag is cleared on that descriptor and
    the read made again."""
    git_dir = _repo(tmp_path / "proj")
    written = write(git_dir, policy=True, bare=False)
    real_read = os.read
    reads = {"n": 0}

    def refusing(fd: int, n: int) -> bytes:
        reads["n"] += 1
        if reads["n"] == 1:
            raise OSError(errno.EINVAL, "Invalid argument")
        return real_read(fd, n)

    monkeypatch.setattr(os, "read", refusing)
    assert read(git_dir) == TokenRead("present", token=written.token)
    assert reads["n"] >= 2


def test_an_open_refused_for_good_names_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused twice, the detail says which call failed."""
    git_dir = _repo(tmp_path / "proj")
    write(git_dir, policy=True, bare=False)

    def refusing(path: Any, flags: int, *args: Any, **kw: Any) -> int:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "open", refusing)
    found = read(git_dir)
    assert found.state == "unreadable" and found.detail.startswith("open: ")


def test_i34_a_loser_finding_another_file_reports_it_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file at the token's name that is no token, and none a writer is
    making: reported as it is, without a pause."""
    git_dir = _repo(tmp_path / "proj")
    other = TokenWrite("malformed", detail=f"not a {TOKEN_HEADER} file")
    clock = _losing(monkeypatch, [other])
    assert write(git_dir, policy=True, bare=False) == other
    assert clock.pauses == []


def test_an_exclusive_create_cut_short_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Links refused, and the disk fills while the token's bytes go out:
    the half-made file is removed and the failure carries the OS text."""
    git_dir = _repo(tmp_path / "proj")
    _refuse_links(monkeypatch)
    real_write = os.write
    calls = {"n": 0}

    def write_then_disk_full(fd: int, data: Any) -> int:
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", write_then_disk_full)
    result = write(git_dir, policy=True, bare=False)
    assert result.status == "failed:io"
    assert "ENOSPC" in result.detail
    assert _info_listing(git_dir) == ["exclude"]


# ---------------------------------------------------------------------------
# Reading a file a writer has not finished, and malformed files
# ---------------------------------------------------------------------------


def test_reader_reads_an_empty_file_again_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer using the exclusive create has made the file and not yet
    written it: the reader pauses, reads again, and finds the token."""
    git_dir = _repo(tmp_path / "proj")
    path = _plant(git_dir, b"")
    value = mint()
    pauses: list[float] = []

    def writer_finishes(seconds: float) -> None:
        pauses.append(seconds)
        path.write_bytes(render(value))

    monkeypatch.setattr(time, "sleep", writer_finishes)
    assert read(git_dir) == TokenRead("present", token=value)
    assert len(pauses) == 1


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (b"", "empty"),
        (f"{TOKEN_HEADER}\n".encode("ascii"), "cut short"),
        (f"{TOKEN_HEADER}\n0XNK4RY".encode("ascii"), "cut short"),
        (b"bettermemory che", "cut short"),
    ],
    ids=["empty", "header-only", "token-partial", "header-partial"],
)
def test_reader_gives_up_after_one_more_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes, detail: str
) -> None:
    git_dir = _repo(tmp_path / "proj")
    _plant(git_dir, body)
    pauses: list[float] = []
    monkeypatch.setattr(time, "sleep", pauses.append)
    assert read(git_dir) == TokenRead("malformed", detail=detail)
    assert len(pauses) == 1
    # write never replaces it; it reports what is there.
    assert write(git_dir, policy=True, bare=False) == TokenWrite(
        "malformed", detail=detail
    )
    assert token_path(git_dir).read_bytes() == body


@pytest.mark.parametrize(
    "body",
    [
        b"hello\n",
        b"bettermemory checkout token v2\n0XNK4RY1ZCMBN31P1J2EN2STQP\n",
        f"{TOKEN_HEADER}\n0xnk4ry1zcmbn31p1j2en2stqp\n".encode("ascii"),
        f"{TOKEN_HEADER}\n0XNK4RY1ZCMBN31P1J2EN2STQ\n".encode("ascii"),
        f"{TOKEN_HEADER}\n0XNK4RY1ZCMBN31P1J2EN2STQPP\n".encode("ascii"),
        f"{TOKEN_HEADER}\nIXNK4RY1ZCMBN31P1J2EN2STQP\n".encode("ascii"),
        f"{TOKEN_HEADER}\n0XNK4RY1ZCMBN31P1J2EN2STQP\nmore\n".encode("ascii"),
        f" {TOKEN_HEADER}\n0XNK4RY1ZCMBN31P1J2EN2STQP\n".encode("ascii"),
        "bettermemory checkout token v1\n0XNK4RY1ZCMBN31P1J2EN2STQÅ\n".encode(),
        b"x" * 5000,
    ],
    ids=[
        "other-text",
        "other-version",
        "lower-case",
        "short",
        "long",
        "non-crockford",
        "extra-line",
        "leading-space",
        "non-ascii",
        "oversized",
    ],
)
def test_malformed_files_are_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    git_dir = _repo(tmp_path / "proj")
    _plant(git_dir, body)
    pauses: list[float] = []
    monkeypatch.setattr(time, "sleep", pauses.append)
    found = read(git_dir)
    assert found.state == "malformed" and found.token is None and found.detail
    assert pauses == []
    result = write(git_dir, policy=True, bare=False)
    assert result.status == "malformed" and not result.ok
    assert token_path(git_dir).read_bytes() == body


def test_a_directory_at_the_token_name_is_not_a_token(tmp_path: Path) -> None:
    git_dir = _repo(tmp_path / "proj")
    token_path(git_dir).mkdir()
    assert read(git_dir).state in ("malformed", "unreadable")
    assert not write(git_dir, policy=True, bare=False).ok
    assert token_path(git_dir).is_dir()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here")
def test_a_fifo_at_the_token_name_does_not_block_the_reader(
    local_tmp_path: Path,
) -> None:
    """The repository on local disk: the reader's answer to a FIFO is the
    point, and an SMB mount makes none (EOPNOTSUPP)."""
    git_dir = _repo(local_tmp_path / "proj")
    make_fifo = getattr(os, "mkfifo")
    make_fifo(token_path(git_dir))
    assert read(git_dir) == TokenRead("malformed", detail="not a regular file")


@_POSIX_PERMISSIONS
def test_an_unreadable_token_is_reported(local_tmp_path: Path) -> None:
    """The repository on local disk: the reader's answer to a refused read
    is the point, and an SMB mount without the unix extensions ignores chmod."""
    git_dir = _repo(local_tmp_path / "proj")
    path = _plant(git_dir, render(mint()))
    path.chmod(0)
    try:
        found = read(git_dir)
        result = write(git_dir, policy=True, bare=False)
    finally:
        path.chmod(0o644)
    assert found.state == "unreadable" and "EACCES" in found.detail
    assert result.status == "failed:io" and "EACCES" in result.detail


# ---------------------------------------------------------------------------
# Where nothing is written (design 1.1, 8.7)
# ---------------------------------------------------------------------------


def test_i23_bare_repository_is_never_written(tmp_path: Path) -> None:
    source = _repo(tmp_path / "source").parent
    bare = tmp_path / "served.git"
    # A cifs mount whose inode numbers the client makes up (noserverino)
    # makes git refuse a hard-linked local clone ("hardlink different from
    # source"), and a clone's objects are not what this test reads.
    _git(tmp_path, "clone", "-q", "--bare", "--no-hardlinks", str(source), str(bare))
    before = sorted(p.name for p in (bare / "info").iterdir())
    assert write(bare, policy=True, bare=True) == TokenWrite("skipped:bare")
    assert sorted(p.name for p in (bare / "info").iterdir()) == before
    assert read(bare) == TokenRead("absent")


def test_x7_policy_off_writes_nothing(tmp_path: Path) -> None:
    git_dir = _repo(tmp_path / "proj")
    assert write(git_dir, policy=False, bare=False) == TokenWrite("skipped:policy")
    assert _info_listing(git_dir) == ["exclude"]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no uid to compare")
def test_i19_another_owners_git_directory_is_never_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_dir = _repo(tmp_path / "proj")
    owner = git_dir.stat().st_uid
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    assert write(git_dir, policy=True, bare=False) == TokenWrite(
        "skipped:foreign_owner"
    )
    assert remint(git_dir, policy=True, bare=False) == TokenWrite(
        "skipped:foreign_owner"
    )
    assert _info_listing(git_dir) == ["exclude"]


@_POSIX_PERMISSIONS
def test_x8_read_only_info_records_failed_readonly(tmp_path: Path) -> None:
    git_dir = _repo(tmp_path / "proj")
    info = git_dir / "info"
    info.chmod(0o555)
    try:
        result = write(git_dir, policy=True, bare=False)
        reminted = remint(git_dir, policy=True, bare=False)
    finally:
        info.chmod(0o755)
    assert result.status == "failed:readonly" and not result.ok
    assert "EACCES" in result.detail and "Permission denied" in result.detail
    assert reminted.status == "failed:readonly"
    assert _info_listing(git_dir) == ["exclude"]


@_POSIX_PERMISSIONS
def test_x8_read_only_git_directory_without_info_records_failed_readonly(
    tmp_path: Path,
) -> None:
    git_dir = _repo(tmp_path / "proj")
    shutil.rmtree(git_dir / "info")
    git_dir.chmod(0o555)
    try:
        result = write(git_dir, policy=True, bare=False)
    finally:
        git_dir.chmod(0o755)
    assert result.status == "failed:readonly"
    assert not (git_dir / "info").exists()


def test_a_missing_git_directory_fails_without_raising(tmp_path: Path) -> None:
    missing = tmp_path / "gone" / ".git"
    result = write(missing, policy=True, bare=False)
    assert result.status == "failed:io" and "ENOENT" in result.detail
    assert remint(missing, policy=True, bare=False).status == "failed:io"
    assert not missing.exists()


# ---------------------------------------------------------------------------
# Linked worktrees and separate git directories (I16, I22)
# ---------------------------------------------------------------------------


def test_i16_linked_worktree_token_sits_in_its_own_git_directory(
    tmp_path: Path,
) -> None:
    primary = _repo(tmp_path / "main").parent
    worktree = tmp_path / "feat"
    _git(primary, "worktree", "add", "-q", "-b", "feat", str(worktree))
    wt_git_dir = Path(_git(worktree, "rev-parse", "--absolute-git-dir"))
    assert wt_git_dir.parent.name == "worktrees"
    assert wt_git_dir.parent.parent.resolve() == (primary / ".git").resolve()

    mine = write(wt_git_dir, policy=True, bare=False)
    assert mine.status == "written"
    assert (wt_git_dir / "info" / "bettermemory").is_file()
    assert read(primary / ".git") == TokenRead("absent")
    assert _git(worktree, "status", "--porcelain", "--untracked-files=all") == ""

    theirs = write(primary / ".git", policy=True, bare=False)
    assert theirs.token != mine.token
    assert read(wt_git_dir).token == mine.token


def test_i22_separate_git_directory_holds_the_token(tmp_path: Path) -> None:
    store = tmp_path / "store" / "proj.git"
    store.parent.mkdir()
    work = tmp_path / "work" / "proj"
    work.parent.mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", f"--separate-git-dir={store}", str(work))
    (work / "a.txt").write_text("a\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "one")

    result = write(store, policy=True, bare=False)
    assert result.status == "written"
    assert read(store).token == result.token
    assert sorted(p.name for p in work.iterdir()) == [".git", "a.txt"]
    assert (work / ".git").is_file()
    assert _git(work, "status", "--porcelain", "--untracked-files=all") == ""


# ---------------------------------------------------------------------------
# git leaves the token alone (E19 a)
# ---------------------------------------------------------------------------


def test_git_status_stays_clean_after_the_write(tmp_path: Path) -> None:
    git_dir = _repo(tmp_path / "proj")
    write(git_dir, policy=True, bare=False)
    status = _git(
        git_dir.parent, "status", "--porcelain", "--untracked-files=all", "--ignored"
    )
    assert status == ""


@pytest.mark.parametrize("how", ["path", "file-url"])
def test_git_clone_carries_no_token(tmp_path: Path, how: str) -> None:
    git_dir = _repo(tmp_path / "proj")
    write(git_dir, policy=True, bare=False)
    clone = tmp_path / "clone"
    source = str(git_dir.parent) if how == "path" else git_dir.parent.as_uri()
    _git(tmp_path, "clone", "-q", "--no-hardlinks", source, str(clone))
    assert (clone / "file0.txt").is_file()
    assert read(clone / ".git") == TokenRead("absent")


def test_git_maintenance_leaves_the_token_as_it_was(tmp_path: Path) -> None:
    git_dir = _repo(tmp_path / "proj", commits=3)
    write(git_dir, policy=True, bare=False)
    path = token_path(git_dir)
    before = path.read_bytes()
    mode_before = stat.S_IMODE(path.stat().st_mode)
    worktree = tmp_path / "wt"
    _git(git_dir.parent, "worktree", "add", "-q", "-b", "wt", str(worktree))
    shutil.rmtree(worktree)
    _git(git_dir.parent, "worktree", "prune")
    _git(git_dir.parent, "gc", "-q", "--prune=now", "--aggressive")
    _git(git_dir.parent, "repack", "-a", "-d", "-q")
    _git(git_dir.parent, "update-server-info")
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == mode_before
