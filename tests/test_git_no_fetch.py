"""No git the package runs fetches (ENG-218).

A repository's own config can make it a partial clone whose promisor
remote is an ``ext::`` transport, and allow that transport
(``protocol.ext.allow=always``); git then runs the remote's command to
fetch an object a read needs and the clone lacks. The history reader asks
git (``cat-file``) for a commit whenever its own object reader is
incomplete, which a garbage pack index is enough for, so a planted
repository ran a command of its choosing in the daemon or a hook client.
`_git.run_git` now sets ``GIT_NO_LAZY_FETCH=1`` and an empty
``GIT_ALLOW_PROTOCOL`` on every call; each alone stops the fetch, the
first on git 2.44 and later, the second on any git since 2.6.1, earlier
than partial clones (2.17). The ``ext::`` tests need a shell script, so a
blobless clone of an ordinary ``file://`` remote shows the same on every
platform, Windows included.

The one diff reader, `origin.commit_patch_stream`, had git start a git in
a submodule whenever ``diff.submodule=diff`` was set, and that git runs
the submodule's own ``diff.external``; `_git.PINS` keeps a gitlink's
change to git's default ``-/+Subproject commit`` lines.

Where that stream fails on a blob a partial clone lacks, claim drift asks
which of the window's blobs are local (`origin_patch.local_patch_window`,
ENG-220): its ``log --raw`` reads trees alone, which a treeless clone
lacks, and its ``cat-file --batch-check`` would fetch each blob it is
asked about, so a treeless and the blobless clone show that neither
fetches.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _git, gitsystem, location, location_git, origin_patch
from bettermemory.location_git import RepositoryHistory
from bettermemory.origin_patch import PatchWindow, local_patch_window

_POSIX = os.name == "posix"


def _plain_git(repo: Path, *args: str, stdin: bytes | None = None) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], input=stdin, capture_output=True, check=True
    )
    return done.stdout.decode().strip()


def _git_version() -> tuple[int, ...]:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout
    digits = out.split()[2].split(".")[:2]
    return tuple(int(d) for d in digits if d.isdigit())


class Hostile:
    """A repository of six commits on one line whose third commit's object
    is gone, configured as a partial clone of an ``ext::`` remote whose
    command touches `marker`."""

    def __init__(self, base: Path) -> None:
        self.repo = base / "repo"
        self.repo.mkdir()
        self.marker = base / "MARKER"
        script = base / "fetch.sh"
        script.write_text(f"#!/bin/sh\ntouch '{self.marker}'\nexit 1\n")
        script.chmod(0o755)
        _plain_git(self.repo, "init", "-q")
        tree = _plain_git(
            self.repo, "hash-object", "-w", "-t", "tree", "--stdin", stdin=b""
        )
        self.ids: list[str] = []
        for i in range(1, 7):
            parent = f"parent {self.ids[-1]}\n" if self.ids else ""
            body = (
                f"tree {tree}\n{parent}author A <a> {1_600_000_000 + i} +0000\n"
                f"committer C <c> {1_600_000_000 + i} +0000\n\nc{i}\n"
            ).encode()
            self.ids.append(
                _plain_git(
                    self.repo,
                    "hash-object",
                    "-w",
                    "-t",
                    "commit",
                    "--stdin",
                    stdin=body,
                )
            )
        for key, value in (
            ("core.repositoryformatversion", "1"),
            ("extensions.partialClone", "origin"),
            ("remote.origin.url", f"ext::{script}"),
            ("remote.origin.promisor", "true"),
            ("protocol.ext.allow", "always"),
        ):
            _plain_git(self.repo, "config", key, value)
        self.missing = self.ids[2]
        objects = self.repo / ".git" / "objects"
        (objects / self.missing[:2] / self.missing[2:]).unlink()
        self.common = str(self.repo / ".git")

    def leave_the_reader_incomplete(self) -> None:
        pack = self.repo / ".git" / "objects" / "pack"
        pack.mkdir(parents=True, exist_ok=True)
        name = "pack-" + "ab" * 20
        (pack / f"{name}.pack").write_bytes(b"PACK garbage")
        (pack / f"{name}.idx").write_bytes(b"garbage index")

    def ask(self) -> None:
        _git.run_git(
            ["cat-file", "commit", self.missing], git_dir=self.common, timeout=10.0
        )


@pytest.fixture
def hostile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Hostile:
    if not _POSIX:
        pytest.skip("the ext:: command is a POSIX shell script")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    for name in ("GIT_NO_LAZY_FETCH", "GIT_ALLOW_PROTOCOL"):
        monkeypatch.delenv(name, raising=False)
    return Hostile(tmp_path)


def test_every_call_fetches_nothing_whatever_the_callers_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_NO_LAZY_FETCH", "0")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "ext:file:https")
    for name in _git.SCRUBBED_ENV:
        monkeypatch.setenv(name, os.devnull)
    env = _git._environment()
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    assert env["GIT_ALLOW_PROTOCOL"] == ""
    for name in (
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_SHALLOW_FILE",
    ):
        assert name in _git.SCRUBBED_ENV
        assert name not in env


def test_a_callers_own_variables_cannot_loosen_the_pins_or_the_scrub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_git`'s `env` is set beneath the scrub and the pins (ENG-203's
    review, L1): a caller handing in a transport, a lazy fetch or another
    repository gets none of them, and its own variables still reach git.
    Its `unset` (ENG-207) removes a variable of the process beneath them
    as well, and cannot remove a pin."""
    extra = {
        "GIT_NO_LAZY_FETCH": "0",
        "GIT_ALLOW_PROTOCOL": "ext",
        "GIT_OPTIONAL_LOCKS": "1",
        "GIT_GRAFT_FILE": os.devnull,
        **{name: os.devnull for name in _git.SCRUBBED_ENV},
    }
    env = _git._environment(extra)
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    assert env["GIT_ALLOW_PROTOCOL"] == ""
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_GRAFT_FILE"] == os.devnull
    assert not set(_git.SCRUBBED_ENV) & set(env)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    pinned = ("GIT_NO_LAZY_FETCH", "GIT_ALLOW_PROTOCOL", "GIT_OPTIONAL_LOCKS")
    env = _git._environment(unset=(*pinned, "GIT_CONFIG_SYSTEM"))
    assert [env[name] for name in pinned] == ["1", "", "0"]
    assert "GIT_CONFIG_SYSTEM" not in env
    assert "GIT_CONFIG_SYSTEM" in _git._environment()


def test_the_system_config_questions_run_at_the_root_under_the_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-207: `gitsystem` asks the git on PATH which system config
    files it reads, with the runner's pins, scrub and no-fetch variables,
    at the root, so the config of the repository a process stands in
    never reaches git; the caller's GIT_CONFIG_SYSTEM is unset for ``git
    var`` and the system and global files are the null device for the
    listing."""
    _plain_git(tmp_path, "init", "-q")
    calls: list[tuple[list[str], object, dict[str, str]]] = []
    real = subprocess.run

    def run(command: list[str], **kw: object) -> object:
        env = kw.get("env")
        calls.append(
            (list(command), kw.get("cwd"), dict(env) if isinstance(env, dict) else {})
        )
        return real(command, **kw)  # type: ignore[call-overload]

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(gitsystem, "ASK_SECONDS", 30.0)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "mine"))
    monkeypatch.chdir(tmp_path)
    answer = gitsystem._ask("key", 0.0)
    assert answer.ok and answer.sources.system != str(tmp_path / "mine")
    assert [c[0][1 + len(_git.PINS) :] for c in calls] == [
        list(gitsystem._VAR),
        list(gitsystem._LIST),
    ]
    for command, cwd, env in calls:
        assert command[: 1 + len(_git.PINS)] == ["git", *_git.PINS]
        assert cwd == os.path.abspath(os.sep)
        assert (env["GIT_NO_LAZY_FETCH"], env["GIT_ALLOW_PROTOCOL"]) == ("1", "")
        assert not set(_git.SCRUBBED_ENV) & set(env)
    assert "GIT_CONFIG_SYSTEM" not in calls[0][2]
    assert (
        calls[1][2]["GIT_CONFIG_SYSTEM"]
        == calls[1][2]["GIT_CONFIG_GLOBAL"]
        == os.devnull
    )


def test_the_hostile_clone_runs_its_command_for_a_git_that_may_fetch(
    hostile: Hostile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: with neither pin, git runs the remote's command, so
    the tests below fail for the right reason when a pin goes."""
    monkeypatch.setattr(_git, "NO_FETCH", {})
    hostile.ask()
    assert hostile.marker.exists()


def test_a_partial_clones_promisor_command_never_runs(hostile: Hostile) -> None:
    hostile.ask()
    assert not hostile.marker.exists()


def test_no_transport_is_allowed_where_git_ignores_the_lazy_fetch_pin(
    hostile: Hostile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git before 2.44 ignores ``GIT_NO_LAZY_FETCH``; the empty
    ``GIT_ALLOW_PROTOCOL`` alone still stops the command."""
    monkeypatch.setattr(_git, "NO_FETCH", {"GIT_ALLOW_PROTOCOL": ""})
    hostile.ask()
    assert not hostile.marker.exists()


def test_the_lazy_fetch_pin_alone_stops_the_fetch(
    hostile: Hostile, monkeypatch: pytest.MonkeyPatch
) -> None:
    if _git_version() < (2, 44):
        pytest.skip("GIT_NO_LAZY_FETCH is read from git 2.44")
    monkeypatch.setattr(_git, "NO_FETCH", {"GIT_NO_LAZY_FETCH": "1"})
    hostile.ask()
    assert not hostile.marker.exists()


def test_the_history_reader_asks_git_without_running_the_remote(
    hostile: Hostile,
) -> None:
    hostile.leave_the_reader_incomplete()
    history = RepositoryHistory(hostile.common)
    assert not history.reader.complete
    assert history.parents(hostile.ids[3]) == (hostile.missing,)
    assert tuple(history.parents(hostile.missing)) == ()
    assert not hostile.marker.exists()


class PlantedSubmodule:
    """A repository whose README.md was a gitlink for two commits, its
    submodule's git directory planted absorbed at ``.git/modules/m`` with a
    ``diff.external`` that touches `marker` (the review's probe e218_b2)."""

    def __init__(self, base: Path) -> None:
        self.repo = base / "repo"
        self.repo.mkdir()
        self.marker = base / "MARKER"
        script = base / "diff.sh"
        script.write_text(f"#!/bin/sh\ntouch '{self.marker}'\nexit 0\n")
        script.chmod(0o755)
        _plain_git(self.repo, "init", "-q")
        mod = self.repo / ".git" / "modules" / "m"
        _plain_git(base, "init", "-q", "--bare", str(mod))
        empty = _plain_git(mod, "hash-object", "-w", "-t", "tree", "--stdin", stdin=b"")
        blob = _plain_git(mod, "hash-object", "-w", "--stdin", stdin=b"s\n")
        tree = _plain_git(mod, "mktree", stdin=f"100644 blob {blob}\tx.txt\n".encode())
        s1 = self._commit(mod, empty, [], 1_600_000_000)
        s2 = self._commit(mod, tree, [s1], 1_600_000_100)
        _plain_git(mod, "config", "diff.external", str(script))
        readme = _plain_git(self.repo, "hash-object", "-w", "--stdin", stdin=b"hi\n")
        modules = _plain_git(
            self.repo,
            "hash-object",
            "-w",
            "--stdin",
            stdin=b'[submodule "m"]\n\tpath = README.md\n\turl = ./m\n',
        )
        trees = [
            f"100644 blob {readme}\tREADME.md\n",
            f"100644 blob {modules}\t.gitmodules\n160000 commit {s1}\tREADME.md\n",
            f"100644 blob {modules}\t.gitmodules\n160000 commit {s2}\tREADME.md\n",
            f"100644 blob {modules}\t.gitmodules\n100644 blob {readme}\tREADME.md\n",
        ]
        parents: list[str] = []
        self.commits: list[str] = []
        for i, entries in enumerate(trees):
            made = _plain_git(self.repo, "mktree", stdin=entries.encode())
            sha = self._commit(self.repo, made, parents, 1_700_000_000 + i)
            self.commits.append(sha)
            parents = [sha]
        _plain_git(self.repo, "update-ref", "HEAD", self.commits[-1])
        _plain_git(self.repo, "reset", "-q", "--hard", "HEAD")
        # The gitlink's second change: s1 to s2.
        self.bump = self.commits[2]
        self.s2 = s2

    @staticmethod
    def _commit(repo: Path, tree: str, parents: list[str], when: int) -> str:
        args = ["commit-tree", tree, "-m", "c"]
        for parent in parents:
            args += ["-p", parent]
        stamp = f"@{when} +0000"
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "A",
            "GIT_AUTHOR_EMAIL": "a@a",
            "GIT_COMMITTER_NAME": "C",
            "GIT_COMMITTER_EMAIL": "c@c",
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_DATE": stamp,
        }
        done = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, check=True, env=env
        )
        return done.stdout.decode().strip()

    def patch(self) -> str | None:
        from bettermemory import origin
        from bettermemory.verify import _literal_pathspecs

        return origin.commit_patch_stream(
            self.repo, [self.bump], _literal_pathspecs(["README.md"])
        )


@pytest.fixture
def submodule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PlantedSubmodule:
    if not _POSIX:
        pytest.skip("the diff program is a POSIX shell script")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    return PlantedSubmodule(tmp_path)


@pytest.mark.parametrize("where", ["repository", "global"])
def test_a_patch_never_starts_a_git_in_a_planted_submodule(
    submodule: PlantedSubmodule,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    """``diff.submodule=diff``, in the repository's config or the user's
    own, has git diff a gitlink's change with a git started in the
    submodule, which runs the submodule's ``diff.external``. The control
    shows plain git does; the package's patch stream does not."""
    if where == "repository":
        _plain_git(submodule.repo, "config", "diff.submodule", "diff")
    else:
        config = tmp_path / "global.cfg"
        config.write_text("[diff]\n\tsubmodule = diff\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    _plain_git(submodule.repo, "log", "-p", "-1", submodule.bump)
    assert submodule.marker.exists(), "the control: plain git runs the program"
    submodule.marker.unlink()
    stream = submodule.patch()
    assert stream is not None
    assert f"+Subproject commit {submodule.s2}" in stream
    assert not submodule.marker.exists()


class Blobless:
    """A blobless clone, through ``file://``, of a repository whose a.txt
    changed in each of three commits: the clone holds every commit and only
    the blobs its checkout needed, so the first a.txt is one a read must
    fetch, from an ordinary remote that needs no program of the test's."""

    def __init__(self, base: Path) -> None:
        source = base / "source"
        source.mkdir()
        _plain_git(source, "init", "-q")
        _plain_git(source, "config", "uploadpack.allowFilter", "true")
        for i in range(3):
            (source / "a.txt").write_bytes(f"v{i}\n".encode())
            _plain_git(source, "add", "a.txt")
            _plain_git(
                source,
                "-c",
                "user.name=A",
                "-c",
                "user.email=a@a",
                "commit",
                "-q",
                "-m",
                f"c{i}",
            )
        self.first = _plain_git(source, "rev-parse", "HEAD~2:a.txt")
        self.repo = base / "clone"
        _plain_git(
            base, "clone", "-q", "--filter=blob:none", source.as_uri(), str(self.repo)
        )

    def read_first(self) -> str | None:
        done = _git.run_git(
            ["cat-file", "blob", self.first], cwd=self.repo, timeout=10.0
        )
        return done.stdout if done is not None and done.returncode == 0 else None


@pytest.fixture
def blobless(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Blobless:
    config = tmp_path / "global.cfg"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    for name in ("GIT_NO_LAZY_FETCH", "GIT_ALLOW_PROTOCOL"):
        monkeypatch.delenv(name, raising=False)
    return Blobless(tmp_path)


def test_a_blobless_clone_fetches_the_blob_for_a_git_that_may(
    blobless: Blobless, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: with neither setting, git fetches the blob."""
    monkeypatch.setattr(_git, "NO_FETCH", {})
    assert blobless.read_first() == "v0\n"


@pytest.mark.parametrize(
    "kept",
    [None, {"GIT_ALLOW_PROTOCOL": ""}, {"GIT_NO_LAZY_FETCH": "1"}],
    ids=["both", "GIT_ALLOW_PROTOCOL", "GIT_NO_LAZY_FETCH"],
)
def test_a_blob_a_partial_clone_lacks_reads_as_absent_on_every_platform(
    blobless: Blobless, monkeypatch: pytest.MonkeyPatch, kept: dict[str, str] | None
) -> None:
    """Each setting alone holds, on Windows too, where the review could not
    show that Git for Windows reads an empty ``GIT_ALLOW_PROTOCOL`` from
    its environment as set."""
    if kept is not None:
        if "GIT_NO_LAZY_FETCH" in kept and _git_version() < (2, 44):
            pytest.skip("GIT_NO_LAZY_FETCH is read from git 2.44")
        monkeypatch.setattr(_git, "NO_FETCH", kept)
    assert blobless.read_first() is None


def test_a_clients_tie_answer_runs_git_under_the_pins_the_daemons_call_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-216: a hook client asked for git's reading of tied witnesses
    (`location.answer`, ``independent``) runs the daemon's own call
    (`RepositoryHistory.independent`) through `run_git`: both no-fetch
    pins and the scrub, no replace refs, no commit-graph, no grafts file,
    and its one-second bound. A blobless clone shows it on every platform,
    Windows included."""
    config = tmp_path / "global.cfg"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_NO_LAZY_FETCH", "0")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "ext:file")
    clone = Blobless(tmp_path).repo
    head = _plain_git(clone, "rev-parse", "HEAD")
    root = _plain_git(clone, "rev-list", "--max-parents=0", "HEAD")
    calls: list[tuple[list[str], dict[str, str], float]] = []
    real = subprocess.run

    def spy(command: list[str], *args: Any, **kwargs: Any) -> Any:
        calls.append((list(command), dict(kwargs["env"]), kwargs["timeout"]))
        return real(command, *args, **kwargs)

    cap = location.capture(str(clone), write_token=False, use_cache=False)
    monkeypatch.setattr(subprocess, "run", spy)

    answers = location.answer({"independent": [root, head]}, {}, cap)

    assert answers["independent"] == [{"of": sorted([root, head]), "top": [head]}]
    (command, env, timeout), *_ = [c for c in calls if "merge-base" in c[0]]
    assert command[command.index("merge-base") :] == [
        "merge-base",
        "--independent",
        *sorted([root, head]),
    ]
    assert "--no-replace-objects" in command
    assert "core.commitGraph=false" in command
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    assert env["GIT_ALLOW_PROTOCOL"] == ""
    assert env["GIT_GRAFT_FILE"] == os.devnull
    assert not set(_git.SCRUBBED_ENV) & set(env)
    assert timeout == location_git.INDEPENDENT_SECONDS


def _split(blobless: Blobless) -> PatchWindow | None:
    commits = _plain_git(blobless.repo, "log", "--format=%H").split()
    return local_patch_window(blobless.repo, commits, ["a.txt"])


def test_the_local_patch_split_fetches_the_blobs_for_a_git_that_may(
    blobless: Blobless, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for claim drift's split in a partial clone (ENG-220):
    with neither setting, its ``cat-file --batch-check`` fetches every blob
    the window's diffs read, so none is missing and the split declines;
    a read with the settings back then finds the first blob local."""
    with monkeypatch.context() as unpinned:
        unpinned.setattr(_git, "NO_FETCH", {})
        assert _split(blobless) is None
    assert blobless.read_first() == "v0\n"


@pytest.mark.parametrize(
    "kept",
    [None, {"GIT_ALLOW_PROTOCOL": ""}, {"GIT_NO_LAZY_FETCH": "1"}],
    ids=["both", "GIT_ALLOW_PROTOCOL", "GIT_NO_LAZY_FETCH"],
)
def test_the_local_patch_split_reads_a_missing_blob_as_missing(
    blobless: Blobless, monkeypatch: pytest.MonkeyPatch, kept: dict[str, str] | None
) -> None:
    """The split's ``log --raw`` and ``cat-file --batch-check`` fetch
    nothing, and the first blob is still absent. Where git reads
    ``GIT_NO_LAZY_FETCH``, the check names each blob the clone lacks as
    missing: every commit reads one, so each is counted and none streamed.
    Under the empty ``GIT_ALLOW_PROTOCOL`` alone, the setting an older git
    is held by, git tries the fetch, is refused the transport and ends
    the check, and the split declines: the per-file count, as before."""
    if kept is not None:
        if "GIT_NO_LAZY_FETCH" in kept and _git_version() < (2, 44):
            pytest.skip("GIT_NO_LAZY_FETCH is read from git 2.44")
        monkeypatch.setattr(_git, "NO_FETCH", kept)
    window = _split(blobless)
    if "GIT_NO_LAZY_FETCH" in _git.NO_FETCH and _git_version() >= (2, 44):
        assert window is not None
        assert (window.stream, len(window.counted)) == ("", 3)
    else:
        assert window is None
    assert blobless.read_first() is None


class Treeless:
    """The blobless clone's source cloned treeless (``--filter=tree:0``):
    the clone holds every commit and only its checkout's trees and blobs,
    so the tree of HEAD's parent, which the split's ``log --raw`` diffs
    against, is one a read must fetch."""

    def __init__(self, base: Path) -> None:
        source = base / "source"
        self.parent_tree = _plain_git(source, "rev-parse", "HEAD~1^{tree}")
        self.commits = _plain_git(source, "log", "--format=%H").split()
        self.repo = base / "treeless"
        _plain_git(
            base, "clone", "-q", "--filter=tree:0", source.as_uri(), str(self.repo)
        )

    def holds_parent_tree(self) -> bool:
        done = _git.run_git(
            ["cat-file", "-e", self.parent_tree], cwd=self.repo, timeout=10.0
        )
        assert done is not None
        return done.returncode == 0

    def listing(self) -> object:
        return origin_patch._changes(self.repo, self.commits, ["a.txt"])


@pytest.fixture
def treeless(blobless: Blobless, tmp_path: Path) -> Treeless:
    return Treeless(tmp_path)


def test_the_split_listing_fetches_a_treeless_clones_trees_for_a_git_that_may(
    treeless: Treeless, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the split's ``log --raw`` (ENG-220's review): in a
    treeless clone it diffs trees the clone lacks, and with neither setting
    git fetches them and lists the changes."""
    assert not treeless.holds_parent_tree(), "premise: the clone lacks it"
    with monkeypatch.context() as unpinned:
        unpinned.setattr(_git, "NO_FETCH", {})
        assert treeless.listing() is not None
    assert treeless.holds_parent_tree()


@pytest.mark.parametrize(
    "kept",
    [None, {"GIT_ALLOW_PROTOCOL": ""}, {"GIT_NO_LAZY_FETCH": "1"}],
    ids=["both", "GIT_ALLOW_PROTOCOL", "GIT_NO_LAZY_FETCH"],
)
def test_the_split_listing_fetches_no_tree_a_treeless_clone_lacks(
    treeless: Treeless, monkeypatch: pytest.MonkeyPatch, kept: dict[str, str] | None
) -> None:
    """Under either setting the listing fails on the tree the clone lacks,
    which the split reads as no answer (the per-file count), and the tree
    is still absent."""
    if kept is not None:
        if "GIT_NO_LAZY_FETCH" in kept and _git_version() < (2, 44):
            pytest.skip("GIT_NO_LAZY_FETCH is read from git 2.44")
        monkeypatch.setattr(_git, "NO_FETCH", kept)
    assert treeless.listing() is None
    assert not treeless.holds_parent_tree()
