"""Fixtures: the judge of one ``.git`` entry, what one folder gives git's
discovery, and the walk, as `gitfile_rule` reads them
(``gitfile_discovery.jsonl``, ``gitfile_discovery_python.jsonl``).

Unit 5 (``native/.../gitfile_rule/discovery.rs``) ports `gitfile_rule.Entry`,
`judge`, `Reading`, `reading_at`, `_entry_reading`, `discovery_reading`,
`discovery_refusal` and `toplevel_agrees`; this pins each to the
reference's own function on real trees.

Each case is a tree written in a fresh directory of the scratch world and
entered (``chdir``; its real path is the root), with the record store a
folder of the scratch world (`gitdir_pin.pins_dir` patched), the git on
PATH as data (``"gv"`` the release `_git_is_at_least` reads, default
2.54; `_reads_compat` True), ``GIT_COMMON_DIR`` unset unless the case
sets it (``"env"``, with ``<root>``; an empty one is set empty) and the
read budget raised so a slow disk cannot time a read out. Lines: a
header (``"cases"`` and ``"queries"``, the counts a reader must reach),
then one per case: the tree as `gen_follow` spells it (``"f"``,
``"d"``, ``"l"``, ``"p"`` FIFOs, ``"m"`` modes, ``"r"`` records) and
``"q"``: one answer a query ``op:args`` (split at ``|``):

- ``j:directory|dotgit|flags|of`` `judge(directory, dotgit, st)`: ``st``
  is ``os.lstat`` of ``of`` where given (an entry that changed after its
  ``lstat``), else of ``dotgit``; ``flags`` holds ``F`` for
  ``fetch=False`` and ``R`` for ``read=False``. The answer is null or
  ``{"g": git_dir, "why": refusal, "own": b, "prov": b}``;
- ``e:directory|dotgit`` `_entry_reading(directory, dotgit, lstat)`, ``r:directory``
  `reading_at`, ``d:start`` `discovery_reading`: null or ``{"why":
  refusal, "prov": b, "root": path}``;
- ``D:start`` `discovery_refusal`: a refusal or null;
- ``t:start|toplevel`` `toplevel_agrees`.

A refusal is ``{"why": text, "u": FormatUnknown}``. A path or text
answered is spelled with the case's directory as ``<scratch>``. An
exception is ``{"e": class}`` (``"n"`` the errno's name for an OSError).

A walk that finds no repository in its case goes on above the case's
directory to the file system's root. Its answer is None wherever no
folder above holds a ``.git`` or a ``HEAD`` entry (each `reading_at`
there states those two names and nothing else), so the generator checks
exactly that and refuses to run otherwise (`_clear_above`); the native
test on disk checks its own scratch directory the same way.

A query goes in the Python-gated file by its input: any query of a tree
holding a link loop or a record nested deeper than 500 (the JSON
decoder's recursion limit differs between 3.11 and 3.13); any query of a
tree with a link target or a ``commondir`` text that has an empty, ``.``
or ``..`` part after a name (`_walked_dotted`: `_git_common`'s strict
walk can meet such a part after a non-directory, where 3.11 and 3.13
differ; such parts before the first name, as in git's ``../..`` or a
relative link's ``../own/.git``, are met at the folder the text is read
from or one above it, so they climb through folders alone and are not
gated); and a ``j``, ``e`` or ``r`` query whose own paths have an empty,
``.`` or ``..`` part (a walk's start is resolved first, and a toplevel is
only stated, so ``d``, ``D`` and ``t`` are not gated by their paths).
Gates: POSIX only (links, modes, FIFOs). No FIFO is opened for reading
its bytes: one at ``.git`` or ``HEAD`` is only stated. Nothing depends on
root (`refuse_root`), the umask, the host's users, home, the
environment, case folding, ``listdir``'s order or the scratch path.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bettermemory import gitdir_pin
from bettermemory import gitfile_rule as g

from . import _sandbox
from ._common import jsonl
from .gen_follow import (
    DEEP,
    HEAD,
    V0,
    _call,
    _commondir_texts,
    _record,
    _refusal,
    _repo,
    _spell,
    _text,
)
from .gen_follow import Case as Recorded
from .gen_fs_realpath import (
    Tree,
    _b,
    check_length,
    check_parent_of_file,
    refuse_root,
    strict_gated,
)


class Case(Recorded):
    def __init__(
        self,
        queries: list[str],
        *,
        env: str | None = None,
        gv: tuple[int, int] = (2, 54),
        **kw: Any,
    ) -> None:
        super().__init__(queries, **kw)
        self.env = env
        self.gv = gv

    def record(self) -> dict[str, Any]:
        out = super().record()
        if self.env is not None:
            out["env"] = self.env
        if self.gv != (2, 54):
            out["gv"] = list(self.gv)
        return out


def _all(names: list[str]) -> list[str]:
    """`judge`, `_entry_reading` and `reading_at` of each folder's
    ``.git``."""
    out = []
    for name in names:
        out += [
            f"j:<root>/{name}|<root>/{name}/.git",
            f"e:<root>/{name}|<root>/{name}/.git",
            f"r:<root>/{name}",
        ]
    return out


def _own() -> Case:
    """A folder's own ``.git`` directory."""
    files: dict[str, Any] = _repo("plain/.git", V0)
    files |= {"plain/f": "", "plain/sub/deep/f": ""}
    # Below a followed repository: an entry git passes over, and one it
    # does not.
    files |= {"plain/skipme/.git/.keep": "", "plain/skipme/in/f": ""}
    files |= {"plain/inner/.git": "junk\n", "plain/inner/in/f": ""}
    # A git directory there whose HEAD is no ref: git passes over the
    # folder itself, as the walk reads it (fetched and read), to the
    # repository above.
    files |= _repo("plain/bad.git", V0) | {"plain/bad.git/HEAD": "garbage\n"}
    files |= _repo("noconfig/.git") | {"noconfig/f": ""}
    files |= _repo("bare/.git", V0 + "\tbare = true\n")
    files |= _repo("here/.git", V0 + "\tworktree = <root>/here\n")
    files |= _repo("else/.git", V0 + "\tworktree = <root>/plain\n")
    files |= _repo("v2/.git", "[core]\n\trepositoryformatversion = 2\n")
    files |= {"nohead/.git/objects/.keep": "", "nohead/.git/refs/.keep": ""}
    files |= _repo("badhead/.git", V0) | {"badhead/.git/HEAD": "garbage\n"}
    files |= {"noobj/.git/HEAD": HEAD, "noobj/.git/refs/.keep": ""}
    files |= {"fifohead/.git/objects/.keep": "", "fifohead/.git/refs/.keep": ""}
    files |= _repo("emptycd/.git", V0) | {"emptycd/.git/commondir": ""}
    files |= _repo("cdmid/.git", V0)
    files |= {"cdmid/.git/commondir": "<root>/nothing/deeper\n"}
    files |= _repo("cdlast/.git", V0) | {"cdlast/.git/commondir": "<root>/nothing\n"}
    files |= {
        "foreign/.git/HEAD": HEAD,
        "foreign/.git/commondir": "<root>/plain/.git\n",
        "foreign/f": "",
        "cdplain/.git/HEAD": HEAD,
        "cdplain/.git/commondir": "<root>/plain\n",
        "headonly/HEAD": HEAD,
        "none/f": "",
    }
    # A commondir naming a repository whose format is not known: the
    # format's refusal comes before the commondir's.
    files |= {"fv2/.git/HEAD": HEAD, "fv2/.git/commondir": "<root>/v2/.git\n"}
    files |= _repo("locked/.git", V0) | _repo("lockobj/.git", V0)
    names = [
        "plain",
        "noconfig",
        "bare",
        "here",
        "else",
        "v2",
        "nohead",
        "badhead",
        "noobj",
        "fifohead",
        "emptycd",
        "cdmid",
        "cdlast",
        "foreign",
        "fv2",
        "cdplain",
        "locked",
        "lockobj",
        "fifo",
    ]
    queries = _all(names) + [
        "j:<root>/plain|<root>/plain/.git|F",
        "j:<root>/plain|<root>/plain/.git|R",
        "j:<root>/plain|<root>/plain/.git|FR",
        "j:<root>/badhead|<root>/badhead/.git|R",
        "j:<root>/badhead|<root>/badhead/.git|F",
        "j:<root>/elsewhere|<root>/plain/.git",
        "j:<root>/lplain|<root>/lplain/.git",
        "j:plain|plain/.git",
        "e:plain|plain/.git",
        "r:plain",
        "r:<root>/lplain",
        "r:<root>/plain/sub",
        "r:<root>/headonly",
        "r:<root>/none",
        "r:<root>/nothing",
        "r:<root>/plain/f",
        "r:<root>/plain/.git",
        "r:<root>/bare/.git",
        "r:<root>/else/.git",
        "r:<root>/foreign/.git",
        "d:<root>/plain",
        "d:<root>/plain/sub/deep",
        "d:<root>/plain/sub/deep/f",
        "d:<root>/plain/sub/nothing/at/all",
        "d:<root>/plain/sub/deep/../..",
        "d:<root>/plain//sub/./deep/",
        "d:/<root>/plain/sub",
        "d:plain/sub",
        "d:<root>/lplain/sub",
        "d:<root>/plain/.git/objects",
        "d:<root>/plain/skipme/in",
        "d:<root>/plain/inner/in",
        "r:<root>/plain/bad.git",
        "d:<root>/plain/bad.git/objects",
        "D:<root>/plain/bad.git/objects",
        "t:<root>/plain/bad.git/objects|<root>/plain",
        "d:<root>/bare/.git/refs",
        "d:<root>/else/x/y",
        "d:<root>/v2",
        "d:<root>/fifohead/in",
        "d:<root>/foreign",
        "D:<root>/plain/sub/deep",
        "D:<root>/else",
        "D:<root>/v2",
        "D:<root>/fifohead",
        "D:<root>/bare/.git",
        "t:<root>/plain/sub|<root>/plain",
        "t:<root>/plain/sub|<root>/plain/",
        "t:<root>/plain/sub|<root>/plain/sub",
        "t:<root>/plain/sub|<root>/plain/sub/..",
        "t:/<root>/plain/sub|/<root>/plain",
        "t:<root>/plain/skipme/in|<root>/plain",
        "t:<root>/plain/inner/in|<root>/plain",
        "t:<root>/plain|<root>/lplain",
        "t:<root>/lplain/sub|<root>/plain",
        "t:plain/sub|plain",
        "t:<root>/plain|<root>/nothing",
        "t:<root>/plain|<root>/plain/f",
        "t:<root>/plain/.git/objects|<root>/plain",
        "t:<root>/else|<root>/else",
        "t:<root>/else|<root>/plain",
        "t:<root>/bare/.git|<root>/bare",
        "t:<root>/here/x|<root>/here",
    ]
    return Case(
        queries,
        files=files,
        links={"lplain": "<root>/plain"},
        fifos=("fifohead/.git/HEAD", "fifo/.git"),
        modes={"locked/.git": 0o000, "lockobj/.git/objects": 0o000},
    )


def _files() -> Case:
    """``.git`` files: worktrees, a submodule, git directories kept apart
    and their records, files that name nothing; and folders git reads as
    git directories themselves."""
    files: dict[str, Any] = _repo("main/.git", V0) | {"main/f": ""}
    files |= {
        "main/.git/worktrees/wt/HEAD": HEAD,
        "main/.git/worktrees/wt/commondir": "<root>/main/.git\n",
        "main/.git/worktrees/wt/gitdir": "<root>/wt/.git\n",
        "wt/.git": "gitdir: <root>/main/.git/worktrees/wt\n",
        "wt/f": "",
        "wt/deep/er/f": "",
        "copy/.git": "gitdir: <root>/main/.git/worktrees/wt\n",
        "copy/f": "",
        # As git writes a worktree by default: relative both ways.
        "main/.git/worktrees/wr/HEAD": HEAD,
        "main/.git/worktrees/wr/commondir": "../..\n",
        "main/.git/worktrees/wr/gitdir": "<root>/wr/.git\n",
        "wr/.git": "gitdir: ../main/.git/worktrees/wr\n",
        "wr/f": "",
    }
    files |= _repo("super/.git", V0) | {"super/f": ""}
    files |= _repo("super/.git/modules/sub", V0 + "\tworktree = ../../../sub\n")
    files |= {
        "super/sub/.git": "gitdir: ../.git/modules/sub\n",
        "super/sub/f": "",
        "stolen/.git": "gitdir: <root>/super/.git/modules/sub\n",
    }
    for name in ("sep.git", "sep2.git", "sep3.git"):
        files |= _repo(name, V0)
    files |= {
        "sepa/.git": "gitdir: <root>/sep.git\n",
        "sepa/f": "",
        "sepb/.git": "gitdir: <root>/sep.git\n",
        "sepc/.git": "gitdir: <root>/sep2.git\n",
        "sepc/in/f": "",
        "sepd/.git": "gitdir: <root>/sep3.git\n",
        "crlf/.git": "gitdir: <root>/sep2.git\r\n",
        "junk/.git": "not a gitdir line\n",
        "junk/in/f": "",
        "empty/.git": "",
        "missing/.git": "gitdir: <root>/nothing\n",
        "notgit/.git": "gitdir: <root>/main\n",
        "dies/.git": "gitdir: <root>/dying\n",
        "unread/.git": "gitdir: <root>/sep2.git\n",
        "big/.git": {"p": "gitdir: <root>/sep2.git", "x": " ", "n": (1 << 20) + 1},
        "nonutf/.git": {"hex": b"gitdir: <root>/sep2.git\xff\n".hex()},
        "dying/objects/.keep": "",
        "dying/refs/.keep": "",
        "dying/in/f": "",
        "forged/HEAD": HEAD,
        "forged/commondir": "<root>/main/.git\n",
        "fw/.git": "gitdir: <root>/forged\n",
        "thief/.git": "gitdir: <root>/main/.git\n",
        "nested.git/.git/.keep": "",
    }
    files |= _repo("bare.git", V0 + "\tbare = true\n")
    files |= _repo("nested.git", V0 + "\tbare = true\n")
    # A HEAD that is none unless it goes unread (`read=False`).
    files |= _repo("sepbad.git", V0) | {"sepbad.git/HEAD": "garbage\n"}
    files |= {"sbad/.git": "gitdir: <root>/sepbad.git\n"}
    # A git directory holding a `.git` file: the entry is read first.
    files |= _repo("both.git", V0) | {"both.git/.git": "junk\n"}
    files |= _repo("named.git", V0 + "\tworktree = <root>/main\n")
    names = [
        "wt",
        "copy",
        "wr",
        "super/sub",
        "stolen",
        "sepa",
        "sepb",
        "sepc",
        "sepd",
        "crlf",
        "junk",
        "empty",
        "missing",
        "notgit",
        "dies",
        "unread",
        "big",
        "nonutf",
        "fw",
        "thief",
        "lw",
        "nested.git",
        "sbad",
        "both.git",
    ]
    queries = _all(names) + [
        "j:<root>/wt|<root>/wt/.git|FR",
        "j:<root>/sbad|<root>/sbad/.git|R",
        "j:<root>/sbad|<root>/sbad/.git|F",
        "d:<root>/both.git/refs",
        "j:<root>/elsewhere|<root>/wt/.git",
        "j:wr|wr/.git",
        "r:wr",
        "r:<root>/bare.git",
        "r:<root>/forged",
        "r:<root>/dying",
        "r:<root>/named.git",
        "r:<root>/main",
        "r:<root>/main/.git",
        "r:<root>/main/.git/worktrees/wt",
        "r:<root>/main/.git/worktrees/wr",
        "r:<root>/super/.git/modules/sub",
        "r:<root>/sep.git",
        "r:<root>/sepbad.git",
        "d:<root>/sepbad.git/objects",
        "d:<root>/wt/deep/er",
        "d:<root>/wt/.git",
        "d:<root>/main/.git/worktrees/wt",
        "d:<root>/main/.git/objects",
        "d:<root>/super/sub",
        "d:<root>/super/.git/modules/sub/refs",
        "d:<root>/sepb",
        "d:<root>/sepc/in",
        "d:<root>/junk/in",
        "d:<root>/dying/in",
        "d:<root>/forged",
        "d:<root>/bare.git/objects",
        "d:<root>/named.git/refs",
        "D:<root>/wt/deep",
        "D:<root>/copy",
        "D:<root>/stolen",
        "D:<root>/sepb",
        "D:<root>/sepc",
        "D:<root>/junk/in",
        "D:<root>/dying/in",
        "D:<root>/forged",
        "D:<root>/bare.git",
        "t:<root>/wt/deep/er|<root>/wt",
        "t:<root>/wt/deep/er|<root>/lwt",
        "t:<root>/lwt/deep|<root>/wt",
        "t:<root>/wt/deep/er|<root>/main",
        "t:<root>/wt/deep/er|<root>/wt/deep",
        "t:<root>/wr|<root>/wr",
        "t:<root>/super/sub|<root>/super/sub",
        "t:<root>/super/sub|<root>/super",
        "t:<root>/sepa|<root>/sepa",
        "t:<root>/sepb|<root>/sepb",
        "t:<root>/sepc/in|<root>/sepc",
        "t:<root>/bare.git|<root>/bare.git",
        "t:<root>/main/.git/worktrees/wt|<root>/wt",
        "t:<root>/junk/in|<root>/junk",
    ]
    return Case(
        queries,
        files=files,
        links={"lwt": "<root>/wt", "lw/.git": "<root>/main/.git/worktrees/wt"},
        fifos=("dying/HEAD",),
        modes={"unread/.git": 0o000},
        records={
            "sep.git": _record("<root>/sep.git", "<root>/sepa"),
            "sep3.git": _record("<root>/sep3.git", "<root>/vanished"),
        },
    )


def _links() -> Case:
    """A link at ``.git``: to another working tree's own ``.git`` (a
    folder or a file), to a git directory that is no tree's own, to
    nothing, to a file, a FIFO or a folder that is no git directory; and
    an entry that changed after its ``lstat``."""
    files: dict[str, Any] = _repo("own/.git", V0) | {"own/f": ""}
    files |= _repo("alone/.git", V0) | _repo("sep.git", V0)
    # A git directory of a format git does not read, reached by a link:
    # the reason names its config as read through the link.
    files |= _repo("v2.git", "[core]\n\trepositoryformatversion = 2\n")
    files |= {"lv2/f": ""}
    files |= _repo("rl/real.git", V0)
    files |= {
        "gf/.git": "gitdir: <root>/sep.git\n",
        "gf/f": "",
        "gfalone/.git": "gitdir: <root>/sep.git\n",
        "gfrel/.git": "gitdir: ../sep.git\n",
        "junkfile": "junk\n",
        "plaindir/f": "",
        "dying/objects/.keep": "",
        "dying/refs/.keep": "",
        # Other trees' own `.git` entries: a FIFO, no git directory, one
        # git dies on.
        "ft/f": "",
        "emptygit/.git/.keep": "",
        "emptygit/f": "",
        "dieown/.git/objects/.keep": "",
        "dieown/.git/refs/.keep": "",
        "dieown/f": "",
        # A HEAD that is a link to no file, as git before 1.8 wrote it.
        "old.git/objects/.keep": "",
        "old.git/refs/.keep": "",
        "old.git/config": V0 + "\tbare = true\n",
        "oldt/.git/objects/.keep": "",
        "oldt/.git/refs/.keep": "",
        "oldt/f": "",
    }
    links = {
        "w2/.git": "<root>/own/.git",
        "w3/.git": "<root>/alone/.git",
        "sepl/.git": "<root>/sep.git",
        "lv2/.git": "<root>/v2.git",
        "lf/.git": "<root>/gf/.git",
        "lf2/.git": "<root>/gfalone/.git",
        "deep/lf3/.git": "<root>/gfrel/.git",
        "dangle/.git": "<root>/nothing",
        "ljunk/.git": "<root>/junkfile",
        "lfifo/.git": "<root>/afifo",
        "ldir/.git": "<root>/plaindir",
        "ldies/.git": "<root>/dying",
        "chain/.git": "<root>/w2/.git",
        "rl/.git": "real.git",
        "lown": "<root>/own",
        "lft/.git": "<root>/ft/.git",
        "lempty/.git": "<root>/emptygit/.git",
        "ldieown/.git": "<root>/dieown/.git",
        "old.git/HEAD": "refs/heads/main",
        "oldt/.git/HEAD": "refs/heads/main",
    }
    # A chain of 60 links to a working tree: more hops than the OS follows
    # (32 on macOS, 40 on Linux), which `realpath` resolves all the same.
    links["c0"] = "<root>/own"
    links |= {f"c{i}": f"<root>/c{i - 1}" for i in range(1, 60)}
    names = [
        "w2",
        "w3",
        "sepl",
        "lv2",
        "lf",
        "lf2",
        "deep/lf3",
        "ljunk",
        "lfifo",
        "ldir",
        "ldies",
        "chain",
        "rl",
        "lown",
        "own",
        "lft",
        "lempty",
        "ldieown",
        "oldt",
    ]
    queries = _all(names) + [
        "j:<root>/dangle|<root>/dangle/.git",
        "e:<root>/dangle|<root>/dangle/.git",
        # The owner's own folder, reached by a link, is the same folder.
        "j:<root>/lown|<root>/w2/.git",
        "j:<root>/own|<root>/w2/.git",
        # An entry that changed after its `lstat`: a link then, a folder
        # or a file now; a folder then, a file now; a file then, a folder
        # now; a FIFO then.
        "j:<root>/own|<root>/own/.git||<root>/w2/.git",
        "j:<root>/gf|<root>/gf/.git||<root>/w2/.git",
        "j:<root>/gf|<root>/gf/.git||<root>/own/.git",
        "j:<root>/own|<root>/own/.git||<root>/gf/.git",
        "j:<root>/own|<root>/own/.git||<root>/afifo",
        "j:<root>/gf|<root>/gf/.git||<root>/afifo",
        "j:<root>/own|<root>/nothing/.git||<root>/own/.git",
        "j:<root>/own|<root>/nothing/.git||<root>/gf/.git",
        "j:<root>/own|<root>/nothing/.git||<root>/w2/.git",
        "j:<root>/w2|<root>/w2/.git|R",
        "r:<root>/old.git",
        "d:<root>/old.git/objects",
        "r:<root>/oldt/.git",
        "d:<root>/c59",
        "r:<root>/c59",
        "r:<root>/c20",
        "t:<root>/c59|<root>/own",
        "t:<root>/own|<root>/c59",
        "t:<root>/own|<root>/c20",
        "d:<root>/w2",
        "d:<root>/lown",
        "d:<root>/sepl",
        "d:<root>/lv2",
        "d:<root>/lf",
        "d:<root>/ljunk",
        "d:<root>/ldies",
        "D:<root>/w2",
        "D:<root>/ljunk",
        "D:<root>/sepl",
        "D:<root>/lv2",
        "t:<root>/w2|<root>/own",
        "t:<root>/w2|<root>/w2",
        "t:<root>/sepl|<root>/sepl",
        "t:<root>/lown|<root>/own",
        "t:<root>/lown|<root>/lown",
        "t:<root>/rl|<root>/rl",
    ]
    return Case(
        queries,
        files=files,
        links=links,
        fifos=("afifo", "dying/HEAD", "ft/.git", "dieown/.git/HEAD"),
    )


def _given(env: str | None, gv: tuple[int, int]) -> Case:
    """One tree under another git and another ``GIT_COMMON_DIR``: a
    ``core.worktree`` holding a tab (2.45 keeps it, an earlier git reads
    a space), a git directory kept apart the variable makes a worktree's,
    and a link to one whose ``commondir`` is foreign."""
    files: dict[str, Any] = _repo("gd", V0)
    files |= _repo("tab/.git", V0 + "\tworktree = <root>/no\tdir\n")
    files |= {"wtx/.git": "gitdir: <root>/gd\n", "wtx/in/f": ""}
    # A link to a git directory whose `commondir` is foreign and whose
    # `gitdir` names the link: refused for the `commondir` unless the
    # variable makes it a worktree's, and then followed, since the test
    # of a followed entry's `commondir` is of a folder's own `.git` alone.
    files |= {
        "fgd/HEAD": HEAD,
        "fgd/commondir": "<root>/gd\n",
        "fgd/gitdir": "<root>/lwx/.git\n",
    }
    queries = [
        "j:<root>/tab|<root>/tab/.git",
        "r:<root>/tab",
        "r:<root>/tab/.git",
        "j:<root>/wtx|<root>/wtx/.git",
        "e:<root>/wtx|<root>/wtx/.git",
        "r:<root>/wtx",
        "d:<root>/wtx/in",
        "D:<root>/wtx/in",
        "t:<root>/wtx/in|<root>/wtx",
        "j:<root>/lwx|<root>/lwx/.git",
        "e:<root>/lwx|<root>/lwx/.git",
        "r:<root>/lwx",
        "d:<root>/lwx",
        "r:<root>/fgd",
    ]
    return Case(queries, files=files, links={"lwx/.git": "<root>/fgd"}, env=env, gv=gv)


def _nowhere() -> Case:
    """Walks that find no repository in the case and go on to the file
    system's root (`_clear_above`), and paths holding a NUL."""
    files: dict[str, Any] = {
        "plain/sub/f": "",
        "skip/.git/.keep": "",
        "skip/in/f": "",
        "headonly/HEAD": HEAD,
        "headonly/in/f": "",
    }
    files |= _repo("locked/.git", V0)
    queries = [
        "d:<root>/plain/sub",
        "d:<root>/plain/sub/f",
        "d:<root>/nothing/at/all",
        "d:<root>/plain/sub/..",
        "d:",
        "d:.",
        "d:plain",
        "d:<root>/skip/in",
        "d:<root>/headonly/in",
        "d:<root>/dangle",
        "d:<root>/fifo",
        "d:<root>/lplain/sub",
        "d:<root>/locked/.git/objects",
        "d:<root>/pl\x00ain",
        "D:<root>/plain/sub",
        "D:<root>/skip/in",
        "D:<root>/pl\x00ain",
        "t:<root>/plain/sub|<root>/plain",
        "t:<root>/plain|<root>",
        "t:<root>/skip/in|<root>/skip",
        "t:<root>/pl\x00ain|<root>/plain",
        "r:<root>/pl\x00ain",
        "r:<root>/skip",
        "r:<root>/dangle",
        "e:<root>/skip|<root>/skip/.git",
        "e:<root>/fifo|<root>/fifo/.git",
        "j:<root>/plain|<root>/pl\x00/.git||<root>/skip/.git",
        "j:<root>/plain|<root>/pl\x00/.git||<root>/plain/sub/f",
        "j:<root>/plain|<root>/pl\x00/.git||<root>/dangle/.git",
        "j:<root>/plain|<root>/pl\x00/.git||<root>/fifo/.git",
        "j:<root>/pl\x00|<root>/skip/.git",
    ]
    return Case(
        queries,
        files=files,
        links={"dangle/.git": "<root>/nothing", "lplain": "<root>/plain"},
        fifos=("fifo/.git",),
        modes={"locked/.git": 0o000},
    )


def _looped() -> Case:
    """Gated by input: link loops, and a record nested past the JSON
    decoder's limit."""
    files: dict[str, Any] = _repo("main/.git", V0) | _repo("sepd.git", V0)
    files |= {"deep/.git": "gitdir: <root>/sepd.git\n", "deep/in/f": ""}
    queries = [
        "j:<root>/loopy|<root>/loopy/.git",
        "e:<root>/loopy|<root>/loopy/.git",
        "r:<root>/loopy",
        "r:<root>/l1",
        "d:<root>/loopy",
        "d:<root>/l1/x",
        "t:<root>/l1|<root>/l1",
        "j:<root>/deep|<root>/deep/.git",
        "e:<root>/deep|<root>/deep/.git",
        "r:<root>/deep",
        "d:<root>/deep/in",
        "D:<root>/deep/in",
        "t:<root>/deep/in|<root>/deep",
        "r:<root>/main",
    ]
    return Case(
        queries,
        files=files,
        links={"l1": "l2", "l2": "l1", "loopy/.git": "<root>/loopy/.git"},
        loop=True,
        records={"sepd.git": {"nest": DEEP}},
    )


def _dotted() -> Case:
    """A ``commondir`` and a link target whose ``.`` and ``..`` parts come
    before any name (not gated: they climb from a folder), and, gated by
    input, ``j``, ``e`` and ``r`` paths with an empty, ``.`` or ``..``
    part."""
    files: dict[str, Any] = _repo("main/.git", V0) | {"main/f": ""}
    files |= _repo("own/.git", V0) | {"own/f": ""}
    files |= {
        "main/.git/worktrees/wd/HEAD": HEAD,
        "main/.git/worktrees/wd/commondir": "../../../.git\n",
        "main/.git/worktrees/wd/gitdir": "<root>/wd/.git\n",
        "wd/.git": "gitdir: <root>/main/.git/worktrees/wd\n",
        "wd/f": "",
        "main/.git/worktrees/ws/HEAD": HEAD,
        "main/.git/worktrees/ws/commondir": "./../..\n",
        "main/.git/worktrees/ws/gitdir": "<root>/ws/.git\n",
        "ws/.git": "gitdir: <root>/main/.git/worktrees/ws\n",
        "ws/f": "",
    }
    queries = _all(["wd", "ws", "rel"]) + [
        "r:<root>/main/.git/worktrees/wd",
        "r:<root>/main/.",
        "r:<root>//main",
        "r:<root>/main/",
        "r:<root>/own/../main",
        "r:",
        "r:.",
        "j:<root>/main/.|<root>/main/./.git",
        "j:<root>/wd|<root>/wd/../wd/.git",
        "e:./main|./main/.git",
        "d:<root>/wd",
        "d:<root>/rel",
        "t:<root>/wd|<root>/wd",
    ]
    return Case(queries, files=files, links={"rel/.git": "../own/.git"})


def _rooted() -> Case:
    """A repository at the case's own directory, the working directory:
    empty and relative paths, and a root that is the empty path."""
    files: dict[str, Any] = _repo(".git", V0) | {"f": "", "sub/deep/f": ""}
    queries = [
        "r:",
        "r:.",
        "r:sub",
        "r:.git",
        "r:<root>",
        "r:<root>/.git",
        "j:|.git",
        "j:.|./.git",
        "e:|.git",
        "d:",
        "d:.",
        "d:sub",
        "d:sub/deep",
        "d:.git",
        "d:.git/objects",
        "d:<root>",
        "D:",
        "D:sub",
        "t:|",
        "t:|.",
        "t:sub|",
        "t:sub|.",
        "t:sub|<root>",
        "t:sub/deep|sub/..",
        "t:sub/deep|sub",
        "t:.git/objects|.",
    ]
    return Case(queries, files=files)


def _trees() -> Iterator[Case]:
    yield _own()
    yield _files()
    yield _links()
    yield _given(None, (2, 41))
    yield _given("<root>/gd", (2, 54))
    yield _given("", (2, 54))
    yield _nowhere()
    yield _looped()
    yield _dotted()
    yield _rooted()


def _clear_above(real: str) -> None:
    """Refuse to run where a folder above the case's directory holds a
    ``.git`` or a ``HEAD`` entry: a walk that leaves the case would read
    it, and its answer would be the host's."""
    here = os.path.dirname(real)
    while True:
        for name in (".git", "HEAD"):
            if os.path.lexists(os.path.join(here, name)):
                raise RuntimeError(
                    f"{os.path.join(here, name)} exists above the scratch "
                    "directory: generate the discovery fixtures with a "
                    "temporary directory outside any repository"
                )
        parent = os.path.dirname(here)
        if parent == here:
            return
        here = parent


def _reading(found: g.Reading | None, real: str) -> Any:
    if found is None:
        return None
    return {
        "why": _refusal(found.why, real),
        "prov": found.provisional,
        "root": None if found.root is None else _spell(found.root, real),
    }


def _entry(seen: g.Entry | None, real: str) -> Any:
    if seen is None:
        return None
    return {
        "g": _spell(seen.git_dir, real),
        "why": _refusal(seen.why, real),
        "own": seen.own,
        "prov": seen.provisional,
    }


def _query(query: str, real: str, links: dict[str, str]) -> dict[str, Any]:
    op, _, raw = query.partition(":")
    args = [a.replace("<root>", real) for a in raw.split("|")]
    for a in (
        args[0],
        args[1] if len(args) > 1 else "",
        args[3] if len(args) > 3 else "",
    ):
        check_length(a, links, real)
        check_parent_of_file(a)
    out: dict[str, Any] = {"p": _b(query.encode("utf-8", "surrogateescape"))}
    if op == "j":
        flags = args[2] if len(args) > 2 else ""
        if flags.strip("FR"):
            raise ValueError(query)
        st = os.lstat(args[3] if len(args) > 3 else args[1])

        def judged() -> Any:
            seen = g.judge(
                args[0], args[1], st, fetch="F" not in flags, read="R" not in flags
            )
            return _entry(seen, real)

        out["v"] = _call(judged)
    elif op == "e":
        st = os.lstat(args[1])
        out["v"] = _call(lambda: _reading(g._entry_reading(args[0], args[1], st), real))
    elif op == "r":
        out["v"] = _call(lambda: _reading(g.reading_at(args[0]), real))
    elif op == "d":
        out["v"] = _call(lambda: _reading(g.discovery_reading(args[0]), real))
    elif op == "D":
        out["v"] = _call(lambda: _refusal(g.discovery_refusal(args[0]), real))
    elif op == "t":
        out["v"] = _call(lambda: g.toplevel_agrees(args[0], args[1]))
    else:
        raise ValueError(op)
    return out


def _answer(tree: Case, root: Path, pins: Path) -> list[dict[str, Any]]:
    os.chmod(root, 0o755)
    saved = os.getcwd()
    os.chdir(root)
    try:
        real = os.getcwd()
        _clear_above(real)
        base = Path(real)
        for d in tree.dirs:
            (base / d).mkdir(parents=True, exist_ok=True)
        for rel, value in tree.files.items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_text(value).replace(b"<root>", os.fsencode(real)))
            os.chmod(path, 0o644)
        for rel, target in tree.links.items():
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target.replace("<root>", real), base / rel)
        for rel in tree.fifos:
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            os.mkfifo(base / rel)  # type: ignore[attr-defined,unused-ignore]
            os.chmod(base / rel, 0o644)
        pins.mkdir(parents=True)
        for rel, value in tree.records.items():
            name = gitdir_pin._name(os.stat(base / rel))
            (pins / name).write_bytes(
                _text(value).replace(b"<root>", os.fsencode(real))
            )
        for rel in sorted(tree.modes, key=lambda r: -r.count("/")):
            os.chmod(base / rel, tree.modes[rel])

        def at_least(want: tuple[int, int]) -> bool:
            return tree.gv >= want

        try:
            with (
                patch.dict(os.environ),
                patch.object(g, "_git_is_at_least", at_least),
                patch.object(g, "_reads_compat", lambda: True),
                patch.object(g, "_RECORDED_BUDGET", 60.0),
                patch.object(gitdir_pin, "pins_dir", lambda: pins),
            ):
                os.environ.pop("GIT_COMMON_DIR", None)
                if tree.env is not None:
                    os.environ["GIT_COMMON_DIR"] = tree.env.replace("<root>", real)
                return [_query(q, real, tree.links) for q in tree.queries]
        finally:
            for rel in tree.modes:
                os.chmod(base / rel, 0o700)
    finally:
        os.chdir(saved)


def _dotted_text(text: str) -> bool:
    return strict_gated(text, Tree([text]))


def _walked_dotted(text: str) -> bool:
    """Whether a link target or ``commondir`` text has an empty, ``.`` or
    ``..`` part after a name, where `_git_common`'s strict walk can meet
    it after a non-directory: 3.13's walk raises ENOTDIR there and 3.11's
    asks the OS, which never sees the part (`strict_gated`). Such parts
    before the first name are met at the folder the text is read from (a
    link's own folder, a git directory) or one above it, so they climb
    through folders alone, as git's ``../..`` does. A text that is empty
    or ``.`` alone is gated, as `strict_gated` gates it."""
    if text in ("", "."):
        return True
    named = False
    for part in text.split("/"):
        if part in ("", ".", ".."):
            if named:
                return True
        else:
            named = True
    return False


def gated(query: str, tree: Case) -> bool:
    """Whether a query goes in the Python-gated file, by its input."""
    if tree.loop:
        return True
    if any(
        isinstance(v, dict) and v.get("nest", 0) > 500 for v in tree.records.values()
    ):
        return True
    if any(_walked_dotted(target) for target in tree.links.values()):
        return True
    if any(_walked_dotted(t) for t in _commondir_texts(tree) if t):
        return True
    op, _, raw = query.partition(":")
    if op not in "jer":
        return False
    return any(_dotted_text(path) for path in raw.split("|")[:2])


def _lines(want_gated: bool) -> list[dict[str, Any]]:
    refuse_root()
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "gitfile-discovery"
        for i, tree in enumerate(_trees()):
            queries = [q for q in tree.queries if gated(q, tree) == want_gated]
            if not queries:
                continue
            asked = Case(
                queries,
                files=tree.files,
                dirs=tree.dirs,
                links=tree.links,
                fifos=tree.fifos,
                modes=tree.modes,
                loop=tree.loop,
                records=tree.records,
                env=tree.env,
                gv=tree.gv,
            )
            root = base / str(i)
            root.mkdir(parents=True)
            answers = _answer(asked, root, world.root / f"pins-{i}-{int(want_gated)}")
            shutil.rmtree(root)
            lines.append({**asked.record(), "q": answers})
    return lines


def _file(name: str, lines: list[dict[str, Any]]) -> dict[str, bytes]:
    header = {
        "fixture": name,
        "cases": len(lines),
        "queries": sum(len(line["q"]) for line in lines),
    }
    return {f"{name}.jsonl": jsonl([header, *lines])}


def generate() -> dict[str, bytes]:
    return _file("gitfile_discovery", _lines(False))


def generate_python() -> dict[str, bytes]:
    return _file("gitfile_discovery_python", _lines(True))
