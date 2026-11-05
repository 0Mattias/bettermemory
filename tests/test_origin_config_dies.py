"""The origin answers a repository's root and HEAD from its files only
where git answers too (ENG-207's round 7).

`origin.toplevel_and_head_from_files` stands in for ``git rev-parse
--show-toplevel HEAD`` with no process, and declined only where the
repository's config could move the working tree. git also dies reading
that config, on a value of a variable its default config reads (a
``user.name``, ``core.excludesFile`` or ``include.path`` with no value, a
``core.filemode`` that is no boolean) and on an include it cannot read,
and the files answered a root and a head there. The config is now read
with git's own parser (`gitconfig.git_entries`, through
`origin_gitfiles._config_entries`), and a file git may die reading
(`origin_gitfiles._rev_parse_dies`) declines, so git answers, as it
does: one process, and none where the config is one a checkout carries.

Each case is git's own answer first: a corpus of variables and values
against ``git rev-parse``, includes, ``config.worktree``, and the
settings checkouts commonly carry, which keep their answer from the
files.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _caches, githead, location_git, origin

from .test_location_git_include_conditions import _git, _repo
from .test_membership_security import neutral_git

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


@pytest.fixture(autouse=True)
def _neutral(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system git config, and a fixed author and committer in
    the environment (`neutral_git`), so `_repo`'s commit needs no identity
    of the machine's: a runner with none made it die ("Author identity
    unknown"). A ``user.name`` a case writes in the repository's config
    still reaches ``git rev-parse``, which reads it whatever names the
    commit's author."""
    neutral_git(tmp_path, monkeypatch)


#: Variables of git's default config, of the repository settings and of
#: other sections, each written with every one of `VALUES`.
KEYS = """
core.filemode core.bare core.logallrefupdates core.ignorecase core.precomposeunicode
core.symlinks core.repositoryformatversion core.hookspath core.excludesfile
core.attributesfile core.editor core.pager core.sshcommand core.autocrlf core.safecrlf
core.eol core.untrackedcache core.fsmonitor core.splitindex core.sparsecheckout
core.sparsecheckoutcone core.trustctime core.checkstat core.quotepath core.ignorestat
core.abbrev core.whitespace core.compression core.loosecompression core.bigfilethreshold
core.packedgitlimit core.deltabasecachelimit core.commentchar core.askpass core.notesref
core.preloadindex core.fsync core.fsyncmethod core.createobject core.protecthfs
core.protectntfs core.warnambiguousrefs core.hidedotfiles core.checkroundtripencoding
core.multipackindex core.commitgraph core.longpaths core.fscache core.disambiguate
core.maxtreedepth core.bogus user.name user.email user.useconfigonly user.signingkey
author.name committer.email i18n.commitencoding i18n.logoutputencoding
branch.autosetupmerge branch.autosetuprebase branch.main.remote push.default
push.autosetupremote mailmap.file attr.tree advice.detachedhead advice.bogus
color.advice color.ui color.pager pager.color pack.packsizelimit pack.compression
pack.threads sparse.expectfilesoutsideofpatterns gc.auto init.defaultbranch
extensions.partialclone extensions.preciousobjects extensions.noop
extensions.objectformat extensions.refstorage extensions.bogus index.version
feature.manyfiles feature.experimental fetch.prune log.follow pull.rebase commit.gpgsign
credential.helper submodule.recurse remote.origin.promisor
remote.origin.partialclonefilter
""".split()
#: The values each of `KEYS` is written with: none (a key with no ``=``),
#: words git refuses as booleans or takes, empty, negative and spaced.
VALUES = [None, "maybe", "", "-1", "always", "x y", "true"]


def _text(name: str, value: str | None) -> str:
    """The config lines that set `name` (``section.key`` or
    ``section.sub.key``) to `value`."""
    section, _, key = name.rpartition(".")
    head, _, sub = section.partition(".")
    header = f'[{head} "{sub}"]' if sub else f"[{head}]"
    return f"{header}\n\t{key}\n" if value is None else f"{header}\n\t{key} = {value}\n"


def _answers(repo: Path) -> tuple[bool, bool]:
    """Whether git answers ``rev-parse --show-toplevel HEAD`` in `repo`,
    and whether the files answer for it (caches cleared)."""
    done = _git(repo, "rev-parse", "--show-toplevel", "HEAD", check=False)
    _caches.clear_all()
    return done.returncode == 0, origin.toplevel_and_head_from_files(repo) is not None


def test_the_files_answer_only_where_git_rev_parse_does(tmp_path: Path) -> None:
    """Each of `KEYS` with each of `VALUES` in the repository's config:
    where ``git rev-parse --show-toplevel HEAD`` dies the files decline,
    and they answer for most of what git reads on past. The files
    answered on every one of them that does not move the working tree."""
    repo = _repo(tmp_path / "repo")
    config = repo / ".git" / "config"
    base = config.read_text(encoding="utf-8")
    wrong: list[tuple[str, str | None]] = []
    died = answered = 0
    for name in KEYS:
        for value in VALUES:
            config.write_text(base + _text(name, value), encoding="utf-8")
            git_answers, files_answer = _answers(repo)
            died += not git_answers
            answered += files_answer
            if files_answer and not git_answers:
                wrong.append((name, value))
    config.write_text(base, encoding="utf-8")
    assert wrong == []
    assert died > 100, "premise: git dies on many"
    assert answered > len(KEYS) * len(VALUES) // 2, "the files answer most of the rest"


#: Includes a repository's config may hold, and what git does: dies on
#: one with no value, an empty one, one of a folder and one whose file it
#: dies on; reads on past one of a missing file.
INCLUDES = {
    "no_value": "[include]\n\tpath\n",
    "empty": '[include]\n\tpath = ""\n',
    "folder": "[include]\n\tpath = .\n",
    "dying_file": "[include]\n\tpath = dying.inc\n",
    "missing": "[include]\n\tpath = missing.inc\n",
    "includeif_dying": '[includeIf "gitdir:/"]\n\tpath = dying.inc\n',
}


@pytest.mark.parametrize("case", list(INCLUDES))
def test_an_include_declines_where_git_may_die_on_it(tmp_path: Path, case: str) -> None:
    """The reviewer's cases: an include with no value, an empty one, one
    of a folder, and one of a file holding a valueless ``user.name``, which
    git dies on; one of a missing file, which git reads on past. The files
    decline at every include, since its file is not read here: git
    answers, one process."""
    repo = _repo(tmp_path / "repo")
    (repo / ".git" / "dying.inc").write_text("[user]\n\tname\n", encoding="utf-8")
    with (repo / ".git" / "config").open("a", encoding="utf-8") as fh:
        fh.write(INCLUDES[case])
    git_answers, files_answer = _answers(repo)
    assert git_answers is (case == "missing"), "premise: git's answer"
    assert not files_answer


def test_a_worktree_config_git_dies_on_declines(tmp_path: Path) -> None:
    """``config.worktree``, which git reads after the common config under
    ``extensions.worktreeConfig``, is read so too: a ``user.name`` with no
    value there makes git die, and the files decline."""
    repo = _repo(tmp_path / "repo")
    _git(repo, "config", "extensions.worktreeConfig", "true")
    (repo / ".git" / "config.worktree").write_text("[user]\n\tname\n", encoding="utf-8")
    assert _answers(repo) == (False, False)
    (repo / ".git" / "config.worktree").write_text(
        "[user]\n\tname = x\n", encoding="utf-8"
    )
    assert _answers(repo) == (True, True)


#: Settings checkouts commonly carry in their own config, which git reads
#: on past and the files answer for, with no process.
COMMON = [
    "[user]\n\tname = A Person\n\temail = a@example.invalid\n\tsigningkey = ABC\n",
    "[core]\n\tautocrlf = input\n\tsafecrlf = warn\n\thooksPath = .husky\n",
    "[core]\n\tsparseCheckout = true\n\tuntrackedCache = true\n\tfsmonitor = true\n",
    "[core]\n\tsshCommand = ssh -i key\n\teditor = vim\n\texcludesFile = ~/.ignore\n",
    '[remote "origin"]\n\tpromisor = true\n\tpartialclonefilter = blob:none\n'
    "[extensions]\n\tpartialClone = origin\n",
    '[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n\trebase = true\n',
    "[pull]\n\trebase = true\n[push]\n\tautoSetupRemote = true\n[commit]\n\tgpgsign = true\n",
    '[filter "lfs"]\n\tclean = git-lfs clean -- %f\n\trequired = true\n',
    "[core]\n\tfilemode = false\n\tlogallrefupdates = always\n\tignorecase = false\n",
]


@pytest.mark.parametrize("text", COMMON)
def test_a_checkout_s_common_settings_keep_the_files_answer(
    tmp_path: Path, text: str
) -> None:
    """What checkouts commonly set in their own config (an identity, line
    ends, hooks, a sparse or blobless clone, a branch, LFS) is read by git
    without dying, and the files answer for it with no process."""
    repo = _repo(tmp_path / "repo")
    with (repo / ".git" / "config").open("a", encoding="utf-8") as fh:
        fh.write(text)
    assert _answers(repo) == (True, True)


def _machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The global config git and the readers read (``GIT_CONFIG_GLOBAL``),
    empty, and no system config."""
    path = tmp_path / "machine-global.gitconfig"
    path.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return path


def test_the_files_answer_only_where_git_does_on_the_machine_s_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-207's round 8: git dies on the machine's config as on the
    repository's (``git_default_config`` reads every scope), and the files
    answered there. Each of `KEYS` with each of `VALUES` in the global
    config: where ``git rev-parse --show-toplevel HEAD`` dies the files
    decline (`origin_gitfiles._machine_config_dies`), and they answer for
    most of what git reads on past."""
    machine = _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path / "repo")
    wrong: list[tuple[str, str | None]] = []
    died = answered = 0
    for name in KEYS:
        for value in VALUES:
            machine.write_text(_text(name, value), encoding="utf-8")
            git_answers, files_answer = _answers(repo)
            died += not git_answers
            answered += files_answer
            if files_answer and not git_answers:
                wrong.append((name, value))
    assert wrong == []
    assert died > 100, "premise: git dies on many"
    assert answered > len(KEYS) * len(VALUES) // 2, "the files answer most of the rest"


#: Includes the machine's config may hold, with git's answer: it dies on
#: one with no value, on a bad line, and on one whose file holds a value it
#: refuses, under a condition that holds too; it reads past a missing one.
MACHINE_INCLUDES = {
    "no_value": ("[include]\n\tpath\n", False),
    "bad_line": ("[include\n", False),
    "missing": ("[include]\n\tpath = {dir}/missing.inc\n", True),
    "dying_file": ("[include]\n\tpath = {dir}/dying.inc\n", False),
    "onbranch_dying": (
        '[includeIf "onbranch:main"]\n\tpath = {dir}/dying.inc\n',
        False,
    ),
}


@pytest.mark.parametrize("case", list(MACHINE_INCLUDES))
def test_a_machine_s_include_git_dies_on_declines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """The machine's includes are read where they stand (the origin's
    capture reads them): one git dies reading, or whose file holds a value
    it refuses, declines, under a condition the read does not evaluate too
    (``onbranch:``); one of a missing file keeps the files' answer."""
    machine = _machine(tmp_path, monkeypatch)
    repo = _repo(tmp_path / "repo")
    (tmp_path / "dying.inc").write_text("[core]\n\texcludesfile\n", encoding="utf-8")
    text, git_reads = MACHINE_INCLUDES[case]
    machine.write_text(text.format(dir=tmp_path.as_posix()), encoding="utf-8")
    assert _answers(repo) == (git_reads, git_reads)


#: What a machine's config commonly holds: an identity, an editor, line
#: ends, a pager, push and pull and branch defaults, aliases, colours, a
#: credential helper, LFS, Git for Windows' system settings, and includes.
MACHINE_COMMON = """[user]
\tname = A Person
\temail = a@example.invalid
[core]
\teditor = vim
\texcludesFile = ~/.gitignore_global
\tautocrlf = input
\tpager = less -FRX
\tfscache = true
\tlongpaths = true
\tsymlinks = false
[push]
\tdefault = simple
\tautoSetupRemote = true
[pull]
\trebase = true
[branch]
\tautoSetupMerge = simple
\tautoSetupRebase = always
[init]
\tdefaultBranch = main
[alias]
\tst = status -sb
[color]
\tui = auto
[credential]
\thelper = osxkeychain
[filter "lfs"]
\tclean = git-lfs clean -- %f
\trequired = true
[include]
\tpath = {dir}/missing.inc
[includeIf "gitdir:{dir}/"]
\tpath = {dir}/work.inc
"""


def test_a_machine_s_common_settings_keep_the_files_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a machine's config commonly sets is read by git without
    dying, and the files answer for it with no process: ``push.default``
    and the ``branch.autoSetup*`` words are read as git reads them, where
    any value of them declined, and Git for Windows' own booleans too."""
    machine = _machine(tmp_path, monkeypatch)
    (tmp_path / "work.inc").write_text("[user]\n\temail = w@example.invalid\n")
    machine.write_text(MACHINE_COMMON.format(dir=tmp_path.as_posix()), encoding="utf-8")
    repo = _repo(tmp_path / "repo")
    assert _answers(repo) == (True, True)


#: The words ``push.default`` and the ``branch.autoSetup*`` settings take,
#: as written, and spellings git refuses.
WORDS = [
    *(("push.default", w) for w in ("nothing", "matching", "simple", "upstream")),
    *(("push.default", w) for w in ("tracking", "current", "Simple", "true", None)),
    *(("branch.autosetuprebase", w) for w in ("never", "local", "remote", "always")),
    *(("branch.autosetuprebase", w) for w in ("Always", "true", None)),
    *(("branch.autosetupmerge", w) for w in ("always", "inherit", "simple", "true")),
    *(("branch.autosetupmerge", w) for w in ("ALWAYS", "maybe", None)),
]


@pytest.mark.parametrize(("name", "value"), WORDS)
def test_a_word_is_read_as_git_reads_it(
    tmp_path: Path, name: str, value: str | None
) -> None:
    """Each word git takes, and each spelling it dies on, in the
    repository's config: the files answer exactly where git does."""
    repo = _repo(tmp_path / "repo")
    with (repo / ".git" / "config").open("a", encoding="utf-8") as fh:
        fh.write(_text(name, value))
    git_answers, files_answer = _answers(repo)
    assert files_answer == git_answers


def test_the_machine_s_config_costs_the_files_no_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The machine's config is judged from the reads the origin's capture
    keeps against its stamps (`location_git.machine_config_reads`): after
    a capture, the files' answer opens and states no file of it."""
    machine = _machine(tmp_path, monkeypatch)
    machine.write_text("[user]\n\tname = A Person\n", encoding="utf-8")
    repo = _repo(tmp_path / "repo")
    _caches.clear_all()
    assert origin.capture(repo).repo is not None
    touched: list[str] = []

    def spy(real: Any) -> Any:
        def counted(path: Any, *args: Any, **kwargs: Any) -> Any:
            if os.path.basename(os.fspath(path)) == machine.name:
                touched.append(real.__name__)
            return real(path, *args, **kwargs)

        return counted

    with monkeypatch.context() as patch:
        for name in ("stat", "lstat", "open"):
            patch.setattr(os, name, spy(getattr(os, name)))
        assert origin.toplevel_and_head_from_files(repo) is not None
    assert touched == []


def test_a_cold_process_states_the_machine_s_config_once_for_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENG-207's review of round 8: in a process that has captured no
    origin, the files' answer reads the machine's config, each file opened
    and stated, and the origin's signature then stated each again (9
    stats became 14). The signature that follows within
    `location_git.FRESH_SECONDS` takes that read's stamps, so the two
    state each file once; the signature after it, and one past the
    window, state the files as before."""
    machine = _machine(tmp_path, monkeypatch)
    extra = tmp_path / "machine-extra.gitconfig"
    extra.write_text("[core]\n\tautocrlf = input\n", encoding="utf-8")
    machine.write_text(
        f"[user]\n\tname = A Person\n[include]\n\tpath = {extra.as_posix()}\n",
        encoding="utf-8",
    )
    repo = _repo(tmp_path / "repo")
    found = githead.find_gitdir(repo)
    assert found is not None
    git_dir = str(found.gitdir)
    names = {machine.name, extra.name}
    touched: list[str] = []

    def spy(real: Any) -> Any:
        def counted(path: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(path, (str, os.PathLike)) and (
                os.path.basename(os.fspath(path)) in names
            ):
                touched.append(os.path.basename(os.fspath(path)))
            return real(path, *args, **kwargs)

        return counted

    def stated(call: Any) -> list[str]:
        touched.clear()
        with monkeypatch.context() as patch:
            for name in ("stat", "lstat"):
                patch.setattr(os, name, spy(getattr(os, name)))
            call()
        return sorted(touched)

    _caches.clear_all()
    files = stated(lambda: origin.toplevel_and_head_from_files(repo))
    assert set(files) == names, "premise: the files read the machine's config"
    assert stated(lambda: location_git.machine_config_stamps(git_dir)) == []
    later = stated(lambda: location_git.machine_config_stamps(git_dir))
    assert set(later) == names
    _caches.clear_all()
    stated(lambda: origin.toplevel_and_head_from_files(repo))
    monkeypatch.setattr(location_git, "FRESH_SECONDS", 0.0)
    assert stated(lambda: location_git.machine_config_stamps(git_dir)) == later
