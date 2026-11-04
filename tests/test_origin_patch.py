"""Claim drift in a partial clone narrows over the commits whose blobs are
local (ENG-220).

No git the package runs fetches (ENG-218), so in a blobless clone a blob
the clone lacks reads as absent, and one ``git log -p`` over a window failed
whole on the first: the governed half fell back to the claimed file's
per-file count and named no claim. `origin_patch.local_patch_window` now
streams the commits whose blobs are local and counts the rest per file, and
the count is never below a full clone's.

Each clone is a blobless ``file://`` clone of a scratch repository, so the
tests run on every platform. The test's own plain git fetches the blobs a
case needs local, as a user's checkout or diff would.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bettermemory import _git, origin, origin_patch
from bettermemory import verify as verify_mod
from bettermemory.claims import COMMIT_MARK, load_claims, parse_claim
from bettermemory.origin_patch import PatchWindow, local_patch_window
from bettermemory.verify import (
    BASIS_AUTHOR_DATE,
    BASIS_REACHABILITY,
    ResolvedCommitDrift,
    resolve_commit_drift,
)

from .conftest import set_git_discovery_ceiling

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git_version() -> tuple[int, ...]:
    if shutil.which("git") is None:
        return ()
    out = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout
    digits = out.split()[2].split(".")[:2]
    return tuple(int(d) for d in digits if d.isdigit())


#: A git before 2.44 ignores ``GIT_NO_LAZY_FETCH``: held by the empty
#: ``GIT_ALLOW_PROTOCOL`` alone, its blob check ends at the first blob the
#: clone lacks, and the window keeps the per-file count.
needs_lazy_fetch_pin = pytest.mark.skipif(
    _git_version() < (2, 44), reason="GIT_NO_LAZY_FETCH is read from git 2.44"
)

_T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
_VERIFIED = _T0 + timedelta(hours=1)

_FOO = "src/mod.py::foo"
_GONE = "src/gone.py"


def _module(foo: str, bar: int) -> str:
    return f"def foo({foo}):\n    return 0\n\n\ndef bar():\n    return {bar}\n"


def _gone(n: int) -> str:
    return f"SETTING_NUMBER_ONE = 'a value long enough {n}'\n"


#: The commits of a history, each the files it writes (None deletes one).
History = list[dict[str, str | None]]

#: The symbol window: the first commit changes foo's def line, the other
#: three bar's body.
_SYMBOL: History = [
    {"src/mod.py": _module("", 0)},
    {"src/mod.py": _module("flag=False", 0)},
    {"src/mod.py": _module("flag=False", 1)},
    {"src/mod.py": _module("flag=False", 2)},
    {"src/mod.py": _module("flag=False", 3)},
]
#: The path window: gone.py is edited twice and then deleted.
_PATH: History = [
    {"src/gone.py": _gone(0), "src/keep.py": "x = 0\n"},
    {"src/gone.py": _gone(1)},
    {"src/gone.py": _gone(2)},
    {"src/gone.py": None},
]


@pytest.fixture(autouse=True)
def neutral_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system git configuration, no inherited repository
    variables, and no no-fetch setting of the shell's: the test's own git
    fetches from the source as a user's would."""
    config = tmp_path / "gitconfig"
    config.write_text("[gc]\n\tauto = 0\n[maintenance]\n\tauto = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in (*_git.SCRUBBED_ENV, *_git.NO_FETCH):
        monkeypatch.delenv(name, raising=False)
    set_git_discovery_ceiling(tmp_path, monkeypatch)


def _plain(repo: Path, *args: str, when: datetime | None = None) -> str:
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="Fixture",
        GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_NAME="Fixture",
        GIT_COMMITTER_EMAIL="fixture@example.invalid",
    )
    if when is not None:
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    done = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, env=env
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


class Partial:
    """A source repository whose first commit is verified and whose later
    commits are the window, and a blobless ``file://`` clone of it.

    `commits` are the window's commits, oldest first, and `blobs[i]` the
    blob of `path` the i-th commit of the history left (the first commit
    is 0), where it left one."""

    def __init__(self, base: Path, history: History, path: str):
        self.source = base / "source"
        self.source.mkdir()
        _plain(self.source, "init", "-q")
        _plain(self.source, "config", "uploadpack.allowFilter", "true")
        shas: list[str] = []
        self.blobs: list[str | None] = []
        for i, files in enumerate(history):
            when = _T0 if i == 0 else _VERIFIED + timedelta(hours=i)
            for rel, text in files.items():
                target = self.source / rel
                if text is None:
                    target.unlink()
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(text.encode())
            _plain(self.source, "add", "-A")
            _plain(self.source, "commit", "-q", "-m", f"c{i}", when=when)
            shas.append(_plain(self.source, "rev-parse", "HEAD"))
            listed = _plain(self.source, "ls-tree", "HEAD", "--", path)
            self.blobs.append(listed.split()[2] if listed else None)
        self.anchor = shas[0]
        self.commits = shas[1:]
        self.repo = Path(os.path.realpath(base)) / "clone"

    def clone(self, *, blobless: bool = True) -> Path:
        args = ["clone", "-q"]
        if blobless:
            args.append("--filter=blob:none")
        _plain(self.source.parent, *args, self.source.as_uri(), str(self.repo))
        return self.repo

    def fetch(self, *versions: int) -> None:
        """Make local the blobs the given commits of the history left."""
        for i in versions:
            blob = self.blobs[i]
            assert blob is not None
            _plain(self.repo, "cat-file", "blob", blob)

    def held(self, blob: str | None) -> bool:
        """Whether the clone holds `blob`, asked with the no-fetch pins."""
        assert blob is not None
        done = _git.run_git(["cat-file", "-e", blob], cwd=self.repo, timeout=10.0)
        assert done is not None
        return done.returncode == 0

    def resolve(self, claims: list[str], basis: str) -> ResolvedCommitDrift:
        walk = None
        if basis == BASIS_REACHABILITY:
            walk = origin.commits_since_anchor(
                self.repo, self.anchor, toplevel=self.repo
            )
            assert walk is not None and len(walk.commits) == len(self.commits)
        resolved = resolve_commit_drift(
            cwd=self.repo,
            since=_VERIFIED,
            unfiltered=len(self.commits),
            anchors=(),
            claims=load_claims(claims),
            toplevel=self.repo,
            walk=walk,
        )
        assert resolved is not None
        assert resolved.basis == basis
        return resolved


_BASES = [BASIS_REACHABILITY, BASIS_AUTHOR_DATE]


@pytest.mark.parametrize("basis", _BASES)
def test_a_full_clone_narrows_to_the_claim(tmp_path: Path, basis: str) -> None:
    """The control: one commit changed foo's def line, so one escalates."""
    partial = Partial(tmp_path, _SYMBOL, "src/mod.py")
    partial.clone(blobless=False)
    resolved = partial.resolve([_FOO], basis)
    assert (resolved.count, resolved.claims_drifted) == (1, (_FOO,))


@pytest.mark.parametrize("basis", _BASES)
def test_a_blobless_window_whose_blobs_are_all_local_narrows_as_a_full_clone(
    tmp_path: Path, basis: str
) -> None:
    partial = Partial(tmp_path, _SYMBOL, "src/mod.py")
    partial.clone()
    partial.fetch(0, 1, 2, 3)
    resolved = partial.resolve([_FOO], basis)
    assert (resolved.count, resolved.claims_drifted) == (1, (_FOO,))


@needs_lazy_fetch_pin
@pytest.mark.parametrize("basis", _BASES)
def test_a_window_with_blobs_missing_narrows_over_the_local_commits(
    tmp_path: Path, basis: str
) -> None:
    """The blobs of the first three versions are local, so the first two
    window commits are streamed: the first changed foo's def line and
    escalates, the second only bar's body and does not. The third and
    fourth read the third commit's blob, which the clone lacks: each is
    counted, as the per-file count counts it. 3, where the per-file count
    is 4 and a full clone's 1; and foo is named."""
    partial = Partial(tmp_path, _SYMBOL, "src/mod.py")
    partial.clone()
    partial.fetch(0, 1, 2)
    assert not partial.held(partial.blobs[3]), "premise: the clone lacks it"
    resolved = partial.resolve([_FOO], basis)
    assert (resolved.count, resolved.claims_drifted) == (3, (_FOO,))
    assert not partial.held(partial.blobs[3]), "no call fetched it"


@pytest.mark.parametrize("basis", _BASES)
def test_a_window_with_no_local_commit_counts_every_commit_per_file(
    tmp_path: Path, basis: str
) -> None:
    """A fresh blobless clone holds only the checkout's blobs, so every
    window commit reads one it lacks: the per-file count, as in
    9.0.0rc18, and nothing is fetched."""
    partial = Partial(tmp_path, _SYMBOL, "src/mod.py")
    partial.clone()
    resolved = partial.resolve([_FOO], basis)
    assert (resolved.count, resolved.claims_drifted) == (4, ())
    assert not any(partial.held(blob) for blob in partial.blobs[:4])


@pytest.mark.parametrize("basis", _BASES)
@pytest.mark.parametrize(
    "blobless",
    [pytest.param(True, marks=needs_lazy_fetch_pin), False],
    ids=["blobless", "full"],
)
def test_a_deletion_in_a_counted_commit_still_counts_the_local_lines(
    tmp_path: Path, basis: str, blobless: bool
) -> None:
    """The path claim fires on its file's deletion and implicates every
    commit carrying lines for the file. The deletion reads a blob the
    clone lacks, so it is counted, not streamed; the trees show the
    deletion, so the claim still fires and the streamed edit is counted
    too: 3, as in a full clone, never 2."""
    partial = Partial(tmp_path, _PATH, "src/gone.py")
    partial.clone(blobless=blobless)
    if blobless:
        partial.fetch(0, 1)
        assert not partial.held(partial.blobs[2]), "premise: the clone lacks it"
    resolved = partial.resolve([_GONE], basis)
    assert (resolved.count, resolved.claims_drifted) == (3, (_GONE,))


def test_a_window_whose_blobs_are_all_local_is_not_split(tmp_path: Path) -> None:
    """Every blob local: the one-call stream did not fail for want of a
    blob, so the split does not answer for it, and the caller keeps the
    per-file count."""
    partial = Partial(tmp_path, _SYMBOL, "src/mod.py")
    repo = partial.clone(blobless=False)
    assert local_patch_window(repo, partial.commits, ["src/mod.py"]) is None


def test_the_whole_window_rule_reads_the_local_commits_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A claim fired over the local commits that implicates none of them
    (a deletion with no lines) counts the whole window, the counted
    commits included, as it would over the whole window."""
    stream = (
        f"{COMMIT_MARK}{'a' * 40}\n"
        "diff --git a/src/x.py b/src/x.py\n"
        "deleted file mode 100644\n"
        "--- a/src/x.py\n"
        "+++ /dev/null\n"
        f"{COMMIT_MARK}{'b' * 40}\n"
    )
    window = PatchWindow(
        stream=stream,
        counted=frozenset({"c" * 40}),
        touched=frozenset({"src/x.py"}),
        deleted=frozenset(),
    )
    monkeypatch.setattr(verify_mod, "commit_patch_stream", lambda *a, **k: None)
    monkeypatch.setattr(verify_mod, "local_patch_window", lambda *a, **k: window)
    shas = ["a" * 40, "b" * 40, "c" * 40]
    drifted, implicated = verify_mod._weak_tier_evaluation(
        tmp_path, shas, ["src/x.py"], load_claims(["src/x.py"]), None
    )
    assert drifted == ["src/x.py"]
    assert implicated == set(shas)


def test_a_gitlink_and_a_quoted_path_in_the_raw_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gitlink's object is a commit of the submodule, never a blob of the
    repository, so it is not asked for; a path git still quotes is read as
    the path it names."""
    sha = "d" * 40
    blob = "e" * 40
    module = "f" * 40
    raw = (
        f"{COMMIT_MARK}{sha}\n\n"
        f":160000 160000 {module} {'1' * 40} M\tvendor/lib\n"
        f':000000 100644 {"0" * 40} {blob} A\t"src/we\\"ird.py"\n'
    )
    monkeypatch.setattr(origin, "_git", lambda *a, **k: raw)
    changes = origin_patch._changes(tmp_path, [sha], ["src"])
    assert changes == {
        sha: [
            origin_patch.Change("vendor/lib", "M", ()),
            origin_patch.Change('src/we"ird.py', "A", (blob,)),
        ]
    }


def test_the_claim_the_tests_name_is_a_governed_symbol_claim() -> None:
    claim = parse_claim(_FOO)
    assert (claim.kind, claim.rel_path) == ("symbol", "src/mod.py")
