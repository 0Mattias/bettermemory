"""The capture (RS-F6, design 3.1, 8.1, 8.7, 8.9): every field `location`
reads, checked against git's own answers on fixtures; birth time where
the platform reports it and the path without it; a file provider's
evicted flag; the steady-state cost at a known checkout (E16); and the
modules the hook client imports.

Git runs with no user or system configuration (`neutral_git`, shared with
`test_membership_resolution`).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from bettermemory import checkout_token, location, project_file
from bettermemory.config import Config
from bettermemory.location import (
    BARE,
    FOLDER,
    INDETERMINATE,
    LINKED,
    OUTSIDE,
    PRIMARY,
    REPO,
    Capture,
    capture,
    remote_key,
    resolve_here,
    to_location,
)
from bettermemory.machine import open_machine
from bettermemory.store import Store

from .conftest import shielded_child_env
from .test_membership_resolution import (
    clone,
    git,
    make_repo,
    neutral_git,  # noqa: F401  (the autouse fixture, shared)
    unborn,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _real(path: str | Path) -> str:
    return os.path.realpath(path)


def _key_of(path: str | Path) -> location.Key:
    key = location.object_key(str(path))
    assert key is not None
    return key


def _fresh(path: Path, **kw: Any) -> Capture:
    return capture(
        str(path), write_token=kw.pop("write_token", True), use_cache=False, **kw
    )


# ---------------------------------------------------------------------------
# Every field against git's answers
# ---------------------------------------------------------------------------


@pytest.fixture
def known(tmp_path: Path) -> Path:
    """A primary checkout with three remotes (https, scp-like, a local
    path), branches loose and packed, a remote-tracking ref, a committed
    project file at its root and one below it."""
    other = make_repo(tmp_path / "other", commits=1)
    repo = make_repo(
        tmp_path / "work" / "proj",
        commits=2,
        remote="https://example.invalid/Team/Proj.git",
    )
    git(repo, "remote", "add", "upstream", "git@github.com:Acme/Lib.git")
    git(repo, "remote", "add", "local", str(other))
    project_file.write(repo, name="proj")
    (repo / "pkg").mkdir()
    project_file.write(repo / "pkg", name="pkg")
    git(repo, "add", "bettermemory.toml")
    git(repo, "commit", "-q", "-m", "project file")
    git(repo, "branch", "feature")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD~1")
    git(repo, "pack-refs", "--all")
    git(repo, "branch", "loose-after-pack")
    git(repo, "tag", "-a", "v1", "-m", "release")
    return repo


def test_capture_fields_match_git(known: Path, tmp_path: Path) -> None:
    """Design 3.1: the top level, the git and common directories and
    their objects, HEAD and its branch, the ref tips, every remote as a
    remote key (a local path as `local:<path>`), shallow, kind, the token
    written at first sight and present after, the init stamps, the
    project files from the cwd up, whether the root file is tracked, and
    the hostname: each as git (or the filesystem) gives it."""
    cwd = known / "pkg"
    first = _fresh(cwd)
    assert (first.state, first.kind) == (REPO, PRIMARY)
    assert first.cwd == _real(cwd)
    assert first.root == _real(git(cwd, "rev-parse", "--show-toplevel"))
    assert first.git_dir == _real(git(cwd, "rev-parse", "--absolute-git-dir"))
    assert first.common_dir == _real(
        (cwd / git(cwd, "rev-parse", "--git-common-dir")).resolve()
    )
    assert first.head == git(cwd, "rev-parse", "HEAD")
    assert first.branch == git(cwd, "symbolic-ref", "--short", "HEAD")
    tips = git(
        cwd, "for-each-ref", "--format=%(objectname)", "refs/heads", "refs/remotes"
    )
    assert set(first.ref_tips) == set(tips.split())
    urls = [
        line.split(" ", 1)[1]
        for line in git(
            cwd, "config", "--get-regexp", r"^remote\..*\.url$"
        ).splitlines()
    ]
    keys = [remote_key(u, first.root) for u in urls]
    assert None not in keys
    assert first.remote_keys == tuple(sorted(k for k in keys if k is not None))
    assert set(first.remote_keys) == {
        "example.invalid/team/proj",
        "github.com/acme/lib",
        "local:" + _real(tmp_path / "other"),
    }
    assert first.shallow is (git(cwd, "rev-parse", "--is-shallow-repository") == "true")
    assert first.object_key == _key_of(first.git_dir or "")
    assert first.common_object_key == _key_of(first.common_dir or "")
    assert first.root_object_key == _key_of(known)
    assert first.cwd_key == _key_of(cwd)

    token_file = Path(first.git_dir or "") / "info" / "bettermemory"
    assert first.token_status == "written" and first.token is None
    assert first.token_written == token_file.read_text().splitlines()[1]
    again = _fresh(cwd)
    assert (again.token_status, again.token, again.token_written) == (
        "present",
        first.token_written,
        None,
    )

    for name, ino, mtime, ctime, size in first.stamps:
        st = os.stat(Path(first.git_dir or "") / name)
        assert (ino, mtime, ctime, size) == (
            st.st_ino,
            st.st_mtime_ns,
            st.st_ctime_ns,
            st.st_size,
        )
    assert [s[0] for s in first.stamps] == ["description", "info/exclude"]

    found = [
        project_file.parse(cwd / "bettermemory.toml"),
        project_file.parse(known / "bettermemory.toml"),
    ]
    assert [f.id for f in first.files] == [
        f.id for f in found if isinstance(f, project_file.ProjectFile)
    ]
    assert [f.dir for f in first.files] == [_real(cwd), _real(known)]
    assert first.file_errors == ()
    assert first.file_tracked is bool(
        git(known, "ls-tree", "HEAD", "bettermemory.toml")
    )
    assert first.file_tracked is True
    assert first.manifest is None and first.born and not first.evicted
    assert first.hostname == socket.gethostname()

    assert Capture.from_token(json.loads(json.dumps(first.to_token()))) == first


def test_capture_of_a_commit_less_repository_keeps_its_content_witness(
    tmp_path: Path,
) -> None:
    """Design 2.2 (C): commit-less, HEAD names nothing and no ref has a
    tip; the branch HEAD names, and the folder's name with the paths and
    sizes of its working tree outside `.git` and the root's project file."""
    u = unborn(tmp_path / "draft", note="one\ntwo\n")
    (u / "sub").mkdir()
    (u / "sub" / "a.txt").write_text("abc")
    project_file.write(u, name="draft")
    cap = _fresh(u)
    assert (cap.head, cap.ref_tips, cap.born) == (None, (), False)
    assert cap.branch == git(u, "symbolic-ref", "--short", "HEAD")
    assert cap.file_tracked is None
    expected = sorted(
        (str(p.relative_to(u)).replace(os.sep, "/"), p.stat().st_size)
        for p in u.rglob("*")
        if p.is_file()
        and ".git" not in p.relative_to(u).parts
        and p.name != "bettermemory.toml"
    )
    assert cap.manifest == ("draft", tuple(expected))


def test_capture_of_a_linked_worktree_a_bare_repository_and_a_shallow_clone(
    tmp_path: Path,
) -> None:
    """Kind, git directory and common directory as git gives them for a
    linked worktree; a bare repository is its own root and gets no token
    (decision D11); a shallow clone says so."""
    p = make_repo(tmp_path / "main", commits=2)
    w = tmp_path / "wt" / "feat"
    git(p, "worktree", "add", "-q", "-b", "feat", w)
    cap = _fresh(w)
    assert cap.kind == LINKED
    assert cap.git_dir == _real(git(w, "rev-parse", "--absolute-git-dir"))
    assert cap.common_dir == _real(p / ".git")
    assert cap.root == _real(w)
    assert (Path(cap.git_dir or "") / "info" / "bettermemory").is_file()

    b = clone(p, tmp_path / "srv.git", "--bare")
    cap = _fresh(b)
    assert git(b, "rev-parse", "--is-bare-repository") == "true"
    assert (cap.kind, cap.root, cap.git_dir) == (BARE, _real(b), _real(b))
    assert cap.token_status == "skipped:bare"
    assert not (b / "info" / "bettermemory").exists()

    s = clone(p, tmp_path / "shallow", "--depth", "1")
    cap = _fresh(s)
    assert cap.shallow is True
    assert git(s, "rev-parse", "--is-shallow-repository") == "true"


def test_capture_outside_a_repository_folder_and_unreadable(tmp_path: Path) -> None:
    """Outside any repository: global, or a folder project where a project
    file is at the cwd or above (its directory the root); a cwd that is
    gone is INDETERMINATE and says so."""
    plain = tmp_path / "plain"
    plain.mkdir()
    cap = _fresh(plain)
    assert (cap.state, cap.kind, cap.root) == (OUTSIDE, None, None)
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    project_file.write(vault, name="vault")
    cap = _fresh(vault / "notes")
    assert (cap.state, cap.kind, cap.root) == (OUTSIDE, FOLDER, _real(vault))
    assert cap.object_key == _key_of(vault)
    assert cap.manifest is not None and cap.manifest[0] == "vault"
    gone = _fresh(tmp_path / "no-such-dir")
    assert gone.state == INDETERMINATE and gone.detail.startswith("cwd gone")


def test_location_carries_every_field_of_the_capture(known: Path) -> None:
    """`to_location`: the model's Location is the capture's fields for the
    machine and session given, with the object keys as the model's."""
    cap = _fresh(known)
    loc = to_location(cap, machine="M1", session="s1")
    assert (loc.machine, loc.session, loc.cwd, loc.state) == ("M1", "s1", cap.cwd, REPO)
    assert (loc.root, loc.kind, loc.git_dir, loc.head, loc.branch) == (
        cap.root,
        cap.kind,
        cap.git_dir,
        cap.head,
        cap.branch,
    )
    assert loc.object_key is not None and cap.object_key is not None
    assert (
        loc.object_key.device,
        loc.object_key.inode,
        loc.object_key.birth,
    ) == cap.object_key
    assert loc.remote_keys == frozenset(cap.remote_keys)
    assert [s.name for s in loc.stamps] == [s[0] for s in cap.stamps]
    assert [f.id for f in loc.files] == [f.id for f in cap.files]
    assert loc.root_file is not None and loc.root_file.id == cap.files[0].id


# ---------------------------------------------------------------------------
# Birth time and the evicted flag (design 8.7, D19)
# ---------------------------------------------------------------------------

_SEQUENCE_FIELDS = {
    "st_mode",
    "st_ino",
    "st_dev",
    "st_nlink",
    "st_uid",
    "st_gid",
    "st_size",
}


def _restat(st: os.stat_result, **extra: Any) -> os.stat_result:
    """`st` again, with the extra fields in `extra` replaced."""
    fields = {
        f: getattr(st, f)
        for f in dir(st)
        if f.startswith("st_") and f not in _SEQUENCE_FIELDS
    }
    fields.update({k: v for k, v in extra.items() if k in fields})
    return os.stat_result(tuple(st)[: os.stat_result.n_sequence_fields], fields)


def _stat_patched(monkeypatch: pytest.MonkeyPatch, target: Path, **extra: Any) -> None:
    """`os.stat` of `target` reports `extra` in place of its own fields."""
    real = os.stat
    wanted = _real(target)

    def stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        st = real(path, *args, **kwargs)
        if isinstance(path, (str, os.PathLike)) and _real(os.fspath(path)) == wanted:
            return _restat(st, **extra)
        return st

    monkeypatch.setattr(os, "stat", stat)
    location.forget()


def test_birth_time_is_read_on_this_platform(known: Path) -> None:
    """Design 8.7: the git directory's object carries its birth time where
    the platform reports one: st_birthtime on macOS and BSD, the creation
    time on Windows, statx (STATX_BTIME) on Linux where the filesystem
    keeps it."""
    if not location.REPORT_BIRTH:
        pytest.skip("reads the platform's birth time: BETTERMEMORY_TEST_NO_BIRTH is on")
    cap = _fresh(known)
    assert cap.object_key is not None
    if sys.platform.startswith("linux"):
        assert cap.object_key[2] == (
            None if (b := location._statx_birth(cap.git_dir or "")) is None else str(b)
        )
    else:
        assert cap.object_key[2] is not None
        st = os.stat(cap.git_dir or "")
        exact = getattr(st, "st_birthtime_ns", None)
        seconds = getattr(st, "st_birthtime", None)
        if exact is None and seconds is not None:
            exact = int(seconds * 1_000_000_000)
        if exact is not None:
            assert cap.object_key[2] == str(exact)


@pytest.mark.skipif(
    sys.platform == "win32", reason="NTFS reports a creation time in every stat"
)
def test_capture_without_birth_time(
    known: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Design 8.7, E15: a stat with no birth time (NFS, ext4 under an old
    kernel) gives the object as device and inode alone; the rest of the
    capture is unchanged."""
    gd = known / ".git"
    _stat_patched(monkeypatch, gd, st_birthtime=None, st_birthtime_ns=None)
    monkeypatch.setattr(location, "_statx_birth", lambda path: None)
    cap = _fresh(known)
    st = os.stat(gd)
    assert cap.object_key == (str(st.st_dev), str(st.st_ino), None)
    assert cap.head == git(known, "rev-parse", "HEAD")


_EVICTION = (
    ("st_flags", 0x40000000)  # SF_DATALESS
    if hasattr(os.stat(os.curdir), "st_flags")
    else ("st_file_attributes", 0x00400000)  # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
    if hasattr(os.stat(os.curdir), "st_file_attributes")
    else None
)


@pytest.mark.skipif(
    _EVICTION is None, reason="this platform's stat carries no provider flag"
)
@pytest.mark.parametrize("online", [False, True])
def test_capture_reads_the_evicted_flag(
    known: Path, monkeypatch: pytest.MonkeyPatch, online: bool
) -> None:
    """Design 3.1, D19: HEAD a file provider's placeholder (SF_DATALESS on
    macOS, the recall attributes on Windows). A fetch that brings it
    within the budget leaves an ordinary capture; one that does not
    leaves the objects stat gave, reads nothing else, names the file, and
    keeps no cache entry (the signature declines)."""
    assert _EVICTION is not None
    head = known / ".git" / "HEAD"
    _stat_patched(monkeypatch, head, **{_EVICTION[0]: _EVICTION[1]})
    asked: list[str] = []

    def fetch(path: str, budget: float) -> bool:
        asked.append(path)
        return online

    monkeypatch.setattr(location, "_fetch", fetch)
    cap = _fresh(known)
    assert asked == [_real(head)]
    if online:
        assert not cap.evicted and cap.head == git(known, "rev-parse", "HEAD")
    else:
        assert cap.evicted and cap.detail == _real(head)
        assert (cap.head, cap.token_status) == (None, "not_read:evicted")
        assert cap.object_key == _key_of(known / ".git")
        assert location.signature(str(known)) is None


def test_fetch_reads_a_file_within_its_budget(tmp_path: Path) -> None:
    """`_fetch` asks for the file by reading it on a worker thread and
    reports whether it arrived within the budget."""
    f = tmp_path / "f"
    f.write_text("x")
    assert location._fetch(str(f), location.FETCH_BUDGET) is True


# ---------------------------------------------------------------------------
# The steady-state cost (E16, design 8.9)
# ---------------------------------------------------------------------------


class _Counts:
    """What one call did: processes spawned, and the `os.stat` and
    `os.lstat` calls on paths at or below the directories it was measured
    within (`files`, the distinct paths among them), apart from the calls
    on any other path (`elsewhere`): the ancestors `os.path.realpath`
    walks and the walk up from the cwd, whose number grows with the depth
    of the temporary directory the test runs in."""

    def __init__(self) -> None:
        self.spawns = 0
        self.stats = 0
        self.lstats = 0
        self.elsewhere = 0
        self.files: set[str] = set()


@pytest.fixture
def counted(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Callable[[], object], Sequence[Path]], _Counts]:
    """Run a call with every process spawn (`subprocess.Popen`, which
    `subprocess.run` uses) counted, and every `os.stat` and `os.lstat`
    counted against the directories given (as given and resolved), the
    suite's own guard on captures (conftest) set aside so only the
    product's calls count. A call on a descriptor counts as within."""
    real_stat, real_lstat, real_popen = os.stat, os.lstat, subprocess.Popen

    def measure(fn: Callable[[], object], within: Sequence[Path]) -> _Counts:
        counts = _Counts()
        roots = {os.path.abspath(p) for p in within}
        roots |= {os.path.realpath(p) for p in within}

        def inside(path: Any) -> str | None:
            if isinstance(path, int):
                return f"<fd {path}>"
            try:
                here = os.path.abspath(os.fsdecode(path))
            except TypeError:
                return repr(path)
            if any(here == r or here.startswith(r + os.sep) for r in roots):
                return here
            return None

        def stat(path: Any, *a: Any, **k: Any) -> os.stat_result:
            here = inside(path)
            if here is None:
                counts.elsewhere += 1
            else:
                counts.stats += 1
                counts.files.add(here)
            return real_stat(path, *a, **k)

        def lstat(path: Any, *a: Any, **k: Any) -> os.stat_result:
            here = inside(path)
            if here is None:
                counts.elsewhere += 1
            else:
                counts.lstats += 1
                counts.files.add(here)
            return real_lstat(path, *a, **k)

        class Popen(real_popen):  # type: ignore[valid-type,misc]
            def __init__(self, *a: Any, **k: Any) -> None:
                counts.spawns += 1
                super().__init__(*a, **k)

        with monkeypatch.context() as m:
            for name in ("capture", "_capture_uncached"):
                fn_ = getattr(location, name)
                m.setattr(location, name, getattr(fn_, "__wrapped__", fn_))
            m.setattr(os, "stat", stat)
            m.setattr(os, "lstat", lstat)
            m.setattr(subprocess, "Popen", Popen)
            fn()
        return counts

    return measure


@pytest.fixture
def steady(
    tmp_path: Path, local_tmp_path: Path
) -> Iterator[tuple[Store, Config, Any, Path, list[Path]]]:
    """A checkout seen twice: the repository under `tmp_path`, the store
    and the machine's state directory on local disk (`local_tmp_path`)."""
    repo = make_repo(tmp_path / "Developer" / "game", commits=2)
    store = Store(local_tmp_path / "store")
    identity = open_machine(local_tmp_path / "state")
    config = Config()
    for _ in range(2):
        resolve_here(store, config, str(repo), identity=identity)
    yield (
        store,
        config,
        identity,
        repo,
        [repo, local_tmp_path / "store", local_tmp_path / "state"],
    )
    store.close()


def test_e16_a_known_checkout_spawns_no_git_and_stats_little(
    steady: tuple[Store, Config, Any, Path, list[Path]],
    counted: Callable[[Callable[[], object], Sequence[Path]], _Counts],
) -> None:
    """E16, design 8.9: a prompt at a known checkout, its capture cached
    against the file signature, spawns no git and stays within a bounded
    number of stat calls on the repository, the store and the state
    directory; after a commit (the signature changed, a fresh capture
    through the object reader) it still spawns nothing. The calls on the
    paths above them (the realpath walk and the walk up from the cwd)
    are reported, not bounded: they grow with the depth of the temporary
    directory. Measured on macOS (APFS), the same at any depth of the
    temporary directory: steady state 0 spawns, 12 os.stat and 0 os.lstat
    calls on 9 paths; after a commit 0 spawns, 47 os.stat and 11 os.lstat
    calls on 19 paths."""
    store, config, identity, repo, within = steady

    def prompt() -> object:
        return resolve_here(store, config, str(repo), identity=identity)

    def report(name: str, c: _Counts) -> None:
        print(
            f"{name}: spawns={c.spawns} stat={c.stats} lstat={c.lstats} "
            f"files={len(c.files)} elsewhere={c.elsewhere}"
        )

    warm = counted(prompt, within)
    report("steady", warm)
    assert warm.spawns == 0
    assert warm.stats <= 12 and warm.lstats == 0 and len(warm.files) <= 9

    git(repo, "commit", "-q", "--allow-empty", "-m", "work")
    moved = counted(prompt, within)
    report("after a commit", moved)
    assert moved.spawns == 0
    assert moved.stats <= 47 and moved.lstats <= 11 and len(moved.files) <= 19


# ---------------------------------------------------------------------------
# The hook client's imports (design 8.1)
# ---------------------------------------------------------------------------

_HEAVY = (
    "bettermemory.store",
    "bettermemory.membership",
    "bettermemory.config",
    "bettermemory.recall",
    "bettermemory.origin",
    "pydantic",
    "platformdirs",
    "sqlite3",
    "yaml",
)


def test_the_hook_client_path_imports_only_the_standard_library_and_its_readers(
    tmp_path: Path, local_tmp_path: Path
) -> None:
    """Design 8.1: the hook client, with everything its membership rounds
    use (the probe, the capture and its token, the answers to the daemon's
    questions, the machine identity), imports the standard library and
    bettermemory's stdlib-only readers, never the store, the model, the
    config or a third-party package."""
    repo = make_repo(tmp_path / "repo", commits=2)
    script = f"""
import json, sys
import bettermemory._daemon_client
from bettermemory import location, machine
cap = location.capture({str(repo)!r}, write_token=True)
location.probe({str(repo)!r})
json.dumps(cap.to_token())
location.answer({{"held": [cap.head], "views": [{{"path": {str(repo)!r}, "kind": "primary", "object": None, "token": None}}]}}, {{}}, cap)
location.identity_to_json(machine.open_machine())
print(json.dumps(sorted(sys.modules)))
"""
    env = shielded_child_env()
    env["HOME"] = str(tmp_path / "home")
    # The child's store, machine state and keys on local disk.
    for name, sub in (
        ("BETTERMEMORY_DIR", "dir"),
        ("BETTERMEMORY_STATE_DIR", "state"),
        ("BETTERMEMORY_KEYS_DIR", "keys"),
    ):
        env[name] = str(local_tmp_path / sub)
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    loaded = set(json.loads(done.stdout.splitlines()[-1]))
    assert not loaded & set(_HEAVY), sorted(loaded & set(_HEAVY))
    ours = {m for m in loaded if m == "bettermemory" or m.startswith("bettermemory.")}
    assert ours <= {
        "bettermemory",
        "bettermemory._caches",
        "bettermemory._daemon_client",
        "bettermemory._fsutil",
        "bettermemory._git",
        "bettermemory._location_probe",
        "bettermemory.checkout_token",
        "bettermemory.githead",
        "bettermemory.gitobjects",
        "bettermemory.location",
        "bettermemory.location_git",
        "bettermemory.machine",
        "bettermemory.project_file",
        "bettermemory.time_utils",
    }, sorted(ours)
    assert (repo / ".git" / "info" / "bettermemory").is_file()
    assert checkout_token.read(repo / ".git").state == "present"


def test_capture_constants_match_the_model_and_rc3() -> None:
    """The kinds and bounds the capture shares with the model, and the
    capture cache's lifetime with rc3's origin cache (design 3.1)."""
    from bettermemory import membership, origin

    assert (location.REPO, location.OUTSIDE, location.INDETERMINATE) == (
        membership.REPO,
        membership.OUTSIDE,
        membership.UNREADABLE,
    )
    assert (location.PRIMARY, location.LINKED, location.BARE, location.FOLDER) == (
        membership.PRIMARY,
        membership.LINKED,
        membership.BARE,
        membership.FOLDER,
    )
    assert location.WALK_LIMIT == membership.WALK_LIMIT
    assert location.BLOBS_PER_WITNESS == membership.BLOBS_PER_WITNESS
    assert location.CACHE_SECONDS == origin.ORIGIN_CACHE_SECONDS
