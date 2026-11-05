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

import shutil
from pathlib import Path

import pytest

from bettermemory import _caches, origin

from .test_location_git_include_conditions import _git, _repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

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
