"""Fixture: the config files a checkout reads for its remotes, and their
reads (``location_remotes.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/gitconfig/remotes.rs``; this pins it
to `location_git._rewrites` up to its call of `_spellings` (`_env_true`,
`_system_configs`, `_global_configs`, `_machine_configs`,
`_repository_paths` with `gitfile_rule.reads_worktree_config`,
`_read_configs`, `_failed`, `_head_branch`, `_asked_branch` and the
passes): `_spellings` is stood in for and its arguments recorded.

Each case is a tree as data under a fresh directory of the scratch world
(``"f"`` files, ``"d"`` folders, ``"l"`` links, ``"m"`` modes; every
other file is 644 and every folder 755), with ``"k"`` the inputs, never
the host's: ``cd`` and ``gd`` the common and git directories, ``env``
the environment (HOME, XDG_CONFIG_HOME, GIT_CONFIG_NOSYSTEM,
GIT_CONFIG_SYSTEM, GIT_CONFIG_GLOBAL; a value starting ``@/`` is under
the case's directory), ``so`` what the git on PATH says it reads (``own``,
``system``, ``prefix``, ``hasconfig``), ``v`` the release it names (null:
none) and ``ab`` what ``git symbolic-ref -q HEAD`` answers where the
files do not settle the branch (a branch, null for none, ``"?"`` for no
answer in time). ``"r"``: ``fl`` the files read with their stamps
(``[mode, size]``, null missing, ``"u"`` unstated), ``nm`` the machine's
config files named, and ``o``: ``{"d": why}``, ``"none"`` (no remote), or
the reads `_spellings` is given (``m`` the machine's, ``w`` the
repository's own, each with ``v``, ``ud`` and ``md``; ``lg`` the legacy
names). Texts name the case's directory ``<scratch>``.

POSIX only (`generate.POSIX_ONLY`): modes, links and the host's
``os.path``. Not run as root (`gen_fs_realpath.refuse_root`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import _git as git_runner
from bettermemory import gitconfig, gitfile_rule, gitsystem, location, location_git

from . import _sandbox
from ._common import jsonl
from .gen_fs_realpath import refuse_root
from .gen_gitconfig_file import _b, _content, _expand

REMOTE = b'[remote "origin"]\n\turl = https://example.invalid/a\n'
PLAIN = b"[core]\n\tbare = false\n"


class Tree:
    def __init__(
        self,
        files: dict[str, Any],
        *,
        dirs: tuple[str, ...] = (),
        links: dict[str, str] | None = None,
        fifos: tuple[str, ...] = (),
        modes: dict[str, int] | None = None,
        common: str = "repo/.git",
        git_dir: str | None = None,
        env: dict[str, str | None] | None = None,
        own: tuple[str, ...] = (),
        system: str | None = None,
        prefix: str | None = None,
        hasconfig: bool | None = True,
        version: tuple[int, int] | None = (2, 54),
        asked: str | None = None,
        spell: dict[str, Any] | None = None,
    ) -> None:
        self.files = files
        self.dirs = dirs
        self.links = links or {}
        self.fifos = fifos
        self.explicit = dict(modes or {})
        given_env: dict[str, str | None] = {
            "HOME": "@/home",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        given_env.update(env or {})
        self.ask: dict[str, Any] = {
            "cd": common,
            "gd": git_dir or common,
            "env": {k: v for k, v in given_env.items() if v is not None},
            "so": {
                "own": list(own),
                "system": system,
                "prefix": prefix,
                "hasconfig": hasconfig,
            },
            "v": None if version is None else list(version),
            "ab": asked,
        }
        if spell is not None:
            # What unit B reads of the git on PATH (`location_spellings`).
            self.ask["so"].update(spell)

    def record(self) -> dict[str, Any]:
        out: dict[str, Any] = {"f": {k: _content(v) for k, v in self.files.items()}}
        if self.dirs:
            out["d"] = list(self.dirs)
        if self.links:
            out["l"] = dict(self.links)
        if self.fifos:
            out["p"] = list(self.fifos)
        if self.explicit:
            out["m"] = {k: f"{v:03o}" for k, v in self.explicit.items()}
        out["k"] = self.ask
        return out


def _stamp(stamp: gitconfig.Stamp | None) -> Any:
    if stamp is None:
        return None
    if stamp == gitconfig._UNSTATED:
        return "u"
    return [f"{stamp[4]:o}", stamp[2]]


def _values(names: dict[Any, Any]) -> list[Any]:
    return [
        [
            section,
            None if sub is None else _b(gitconfig.subsection_bytes(sub)),
            key,
            [None if v is None else _b(v) for v in found],
        ]
        for (section, sub, key), found in names.items()
    ]


def _answer(case: Tree, root: Path, spell: bool = False) -> dict[str, Any]:
    root = Path(os.path.realpath(root))
    made: dict[str, int] = {}

    def parents(rel: str) -> None:
        parts = rel.split("/")[:-1]
        for i in range(len(parts)):
            made.setdefault("/".join(parts[: i + 1]), 0o755)

    for rel in case.dirs:
        (root / rel).mkdir(parents=True, exist_ok=True)
        parents(rel)
        made[rel] = 0o755
    for rel, data in case.files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_expand(data))
        parents(rel)
        made[rel] = 0o644
    for rel in case.fifos:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(path)  # type: ignore[attr-defined,unused-ignore]
        parents(rel)
        made[rel] = 0o644
    for rel, target in case.links.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, path)
        parents(rel)
    made.update(case.explicit)
    by_depth = sorted(made, key=lambda rel: rel.count("/"))
    for rel in reversed(by_depth):
        os.chmod(root / rel, made[rel])
    prefix = str(root)

    def at(value: str | None) -> str | None:
        if value is not None and value.startswith("@/"):
            return prefix + value[1:]
        return value

    def label(text: str) -> Any:
        return _b(os.fsencode(text.replace(prefix, "<scratch>")))

    so = case.ask["so"]
    sources = gitsystem.Sources(
        own=tuple(at(p) or "" for p in so["own"]),
        system=at(so["system"]),
        prefix=at(so["prefix"]),
        hasconfig=so["hasconfig"],
        legacy=so.get("legacy"),
        resets=so.get("resets"),
        version=None if so.get("version") is None else tuple(so["version"]),
    )
    env = {k: at(v) or "" for k, v in case.ask["env"].items()}
    release = case.ask["v"]
    asked = case.ask["ab"]

    def run_git(*_a: Any, **_k: Any) -> Any:
        if asked == "?":
            return None
        if asked is None:
            return subprocess.CompletedProcess([], 1, b"", b"")
        return subprocess.CompletedProcess(
            [], 0, b"refs/heads/" + asked.encode() + b"\n", b""
        )

    seen: dict[str, Any] = {}

    def spellings(reads: Any, legacy: Any, *_a: Any) -> list[Any]:
        seen["reads"] = reads
        seen["legacy"] = legacy
        return []

    def at_least(want: tuple[int, int]) -> bool | None:
        return None if release is None else tuple(release) >= want

    saved = os.getcwd()
    os.chdir(root)
    try:
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(location_git, "_sources_once", lambda: lambda: sources),
            mock.patch.object(git_runner, "run_git", run_git),
            mock.patch.object(
                location_git,
                "_spellings",
                location_git._spellings if spell else spellings,
            ),
            mock.patch.object(gitfile_rule, "_git_is_at_least", at_least),
            # A local URL keys as its text: resolving its path is the
            # caller's file system, not this unit's.
            mock.patch.object(location, "_local", lambda path, _base: path),
        ):
            gitconfig._clear_format_memo()
            found = location_git._rewrites(
                prefix + "/" + case.ask["cd"], prefix + "/" + case.ask["gd"]
            )
            if spell:
                with mock.patch.object(location_git, "_rewrites", lambda *_a: found):
                    keyed = location_git.read_remotes(
                        "", "", lambda u: location.remote_key(u, None)
                    )
    finally:
        os.chdir(saved)
        for rel in by_depth:
            try:
                os.chmod(root / rel, 0o700)
            except OSError:
                pass
    out: dict[str, Any] = {
        "fl": [[label(p), _stamp(s)] for p, s in found.files],
        "nm": [label(p) for p in found.named],
    }
    if spell:

        def text(value: str) -> Any:
            return _b(value.encode("utf-8"))

        out["sp"] = [
            [text(u), None if r is None else text(r), m] for u, r, m in found.spellings
        ]
        out["ky"] = [text(k) for k in keyed.keys]
        out["al"] = [text(k) for k in keyed.aliases]
        out["dc"] = None if keyed.declined is None else label(keyed.declined)
        return out
    if found.declined is not None:
        out["o"] = {"d": label(found.declined)}
    elif "reads" not in seen:
        out["o"] = "none"
    else:

        def shown(read: gitconfig.ConfigValues) -> dict[str, Any]:
            got: dict[str, Any] = {"v": _values(read)}
            if read.undecided:
                got["ud"] = _values(read.undecided)
            if read.maybe_dies is not None:
                got["md"] = label(read.maybe_dies)
            return got

        reads = seen["reads"]
        out["o"] = {
            "m": [shown(r) for r, mine in reads if not mine],
            "w": [shown(r) for r, mine in reads if mine],
            "lg": [_b(os.fsencode(n)) for n in seen["legacy"]],
        }
    return out


def _cond(condition: str, path: str) -> bytes:
    return f'[includeIf "{condition}"]\n\tpath = {path}\n'.encode()


def _trees() -> Iterator[Tree]:
    head = {"repo/.git/HEAD": b"ref: refs/heads/main\n"}
    repo = {**head, "repo/.git/config": PLAIN + REMOTE}
    # No remote; a remote; a legacy name alone.
    yield Tree({**head, "repo/.git/config": PLAIN})
    yield Tree(repo)
    yield Tree({**head, "repo/.git/config": PLAIN, "repo/.git/remotes/o": b"URL: u\n"})
    # The machine's files: the system's (GIT_CONFIG_NOSYSTEM values, the
    # build's own files, GIT_CONFIG_SYSTEM named or empty), the global
    # ones (XDG_CONFIG_HOME set or empty, ~/.config/git/config,
    # ~/.gitconfig, GIT_CONFIG_GLOBAL named or empty, HOME unset).
    machine = {
        "etc/own": b'[url "x"]\n\tinsteadOf = own\n',
        "etc/system": b'[url "x"]\n\tinsteadOf = sys\n',
        "etc/named": b'[url "x"]\n\tinsteadOf = named\n',
        "home/.gitconfig": b'[url "x"]\n\tinsteadOf = home\n',
        "home/.config/git/config": b'[url "x"]\n\tinsteadOf = dotconfig\n',
        "xdg/git/config": b'[url "x"]\n\tinsteadOf = xdg\n',
    }
    for nosystem in ("1", "0", "false", "yes", "junk", "", None):
        yield Tree(
            {**repo, **machine},
            env={"GIT_CONFIG_NOSYSTEM": nosystem},
            own=("@/etc/own",),
            system="@/etc/system",
        )
    for named in ("@/etc/named", "", "@/etc/missing"):
        yield Tree(
            {**repo, **machine},
            env={"GIT_CONFIG_NOSYSTEM": None, "GIT_CONFIG_SYSTEM": named},
            system="@/etc/system",
        )
    for xdg in ("@/xdg", ""):
        yield Tree({**repo, **machine}, env={"XDG_CONFIG_HOME": xdg})
    for glob in ("@/etc/named", ""):
        yield Tree({**repo, **machine}, env={"GIT_CONFIG_GLOBAL": glob})
    yield Tree({**repo, **machine}, env={"HOME": None})
    # Unreadable and odd machine files: a global one skipped, a system one
    # and a folder dying.
    yield Tree({**repo, **machine}, modes={"home/.gitconfig": 0o000})
    yield Tree(
        {**repo, **machine},
        env={"GIT_CONFIG_NOSYSTEM": None},
        system="@/etc/system",
        modes={"etc/system": 0o000},
    )
    yield Tree(repo, dirs=("home/.gitconfig",))
    yield Tree({**repo, "home/.gitconfig": b"[bad\n"})
    # Relative machine paths, opened from the working directory (the case
    # root): each of XDG_CONFIG_HOME, GIT_CONFIG_SYSTEM, GIT_CONFIG_GLOBAL
    # and HOME with a readable file, a refused line, no file and a remote.
    texts = (
        b'[url "x"]\n\tinsteadOf = r\n',
        b"[bad\n",
        None,
        b'[remote "m"]\n\turl = https://example.invalid/m\n',
    )
    relative: tuple[tuple[dict[str, str | None], str], ...] = (
        ({"XDG_CONFIG_HOME": "rx"}, "rx/git/config"),
        ({"GIT_CONFIG_NOSYSTEM": None, "GIT_CONFIG_SYSTEM": "rel/sys"}, "rel/sys"),
        ({"GIT_CONFIG_GLOBAL": "rel/glob"}, "rel/glob"),
        ({"HOME": "rh"}, "rh/.gitconfig"),
    )
    for relative_env, rel in relative:
        for text in texts:
            yield Tree(
                {**repo, **({} if text is None else {rel: text})}, env=relative_env
            )
    # An include in a relative file, taken from beside it.
    yield Tree(
        {
            **repo,
            "rx/git/config": b"[include]\n\tpath = inc\n",
            "rx/git/inc": b'[remote "n"]\n\turl = https://example.invalid/n\n',
        },
        env={"XDG_CONFIG_HOME": "rx"},
    )
    # A FIFO and a file over the bound, as the machine's and the own.
    big = [(b"#" * 1023 + b"\n", 2049)]
    yield Tree(repo, fifos=("home/.gitconfig",))
    yield Tree({**repo, "home/.gitconfig": big})
    yield Tree(head, fifos=("repo/.git/config",))
    yield Tree({**head, "repo/.git/config": big})
    wt = b"[core]\n\trepositoryformatversion = 1\n[extensions]\n\tworktreeConfig = true\n"
    yield Tree(
        {**head, "repo/.git/config": wt + REMOTE}, fifos=("repo/.git/config.worktree",)
    )
    yield Tree(
        {**head, "repo/.git/config": wt + REMOTE, "repo/.git/config.worktree": big}
    )
    # config.worktree: extensions.worktreeConfig under each version.
    for version, release in (
        (b"1", (2, 54)),
        (b"0", (2, 54)),
        (None, (2, 54)),
        (b"2", (2, 54)),
        (b"-2", (2, 41)),
        (b"-2", (2, 42)),
        (b"-2", None),
    ):
        core = b"[core]\n" + (
            b"\trepositoryformatversion = " + version + b"\n" if version else b""
        )
        yield Tree(
            {
                **head,
                "repo/.git/config": core
                + b"[extensions]\n\tworktreeConfig = true\n"
                + REMOTE,
                "repo/.git/config.worktree": b'[remote "wt"]\n\turl = w\n',
            },
            version=release,
        )
    # A linked worktree: its own HEAD and config.worktree.
    linked = {
        "repo/.git/config": b"[core]\n\trepositoryformatversion = 1\n"
        b"[extensions]\n\tworktreeConfig = true\n" + REMOTE,
        "repo/.git/HEAD": b"ref: refs/heads/main\n",
        "repo/.git/worktrees/w/HEAD": b"ref: refs/heads/side\n",
        "repo/.git/worktrees/w/config.worktree": b'[remote "wt"]\n\turl = w\n'
        + _cond("onbranch:side", "../../side"),
        "repo/.git/side": b"[s]\n\tk = 1\n",
    }
    yield Tree(linked, git_dir="repo/.git/worktrees/w")
    # A per-worktree ref under the linked git directory; that HEAD
    # unreadable, git asked, the reftable lists of two folders.
    yield Tree(
        {
            **linked,
            "repo/.git/worktrees/w/HEAD": b"ref: refs/worktree/b\n",
            "repo/.git/worktrees/w/refs/worktree/b": b"ref: refs/heads/side\n",
        },
        git_dir="repo/.git/worktrees/w",
    )
    yield Tree(
        linked,
        git_dir="repo/.git/worktrees/w",
        modes={"repo/.git/worktrees/w/HEAD": 0o000},
        asked="side",
    )
    # Includes and each condition family; HEAD on a branch, detached,
    # unborn, through a symbolic ref, unreadable, past the chain's depth.
    inc = {"repo/.git/inc": b'[remote "inc"]\n\turl = i\n'}
    yield Tree(
        {**repo, **inc, "repo/.git/config": REMOTE + b"[include]\n\tpath = inc\n"}
    )
    for text in (
        b"ref: refs/heads/main\n",
        b"ref: refs/heads/other\n",
        b"0123456789abcdef0123456789abcdef01234567\n",
        b"ref: refs/tags/main\n",
        b"ref: refs/heads/sym\n",
        b"garbage\n",
    ):
        yield Tree(
            {
                **inc,
                "repo/.git/HEAD": text,
                "repo/.git/refs/heads/sym": b"ref: refs/heads/main\n",
                "repo/.git/config": REMOTE + _cond("onbranch:main", "inc"),
            },
            asked="main",
        )
    # A ref git cannot read; a detached HEAD against a pattern matching
    # any name.
    yield Tree(
        {
            **inc,
            "repo/.git/HEAD": b"ref: refs/heads/main\n",
            "repo/.git/refs/heads/main": b"x",
            "repo/.git/config": REMOTE + _cond("onbranch:main", "inc"),
        },
        modes={"repo/.git/refs/heads/main": 0o000},
        asked="main",
    )
    yield Tree(
        {
            **inc,
            "repo/.git/HEAD": b"0123456789abcdef0123456789abcdef01234567\n",
            "repo/.git/config": REMOTE + _cond("onbranch:*", "inc"),
        },
    )
    chain = {
        f"repo/.git/refs/heads/c{i}": f"ref: refs/heads/c{i + 1}\n".encode()
        for i in range(6)
    }
    yield Tree(
        {
            **inc,
            **chain,
            "repo/.git/HEAD": b"ref: refs/heads/c0\n",
            "repo/.git/config": REMOTE + _cond("onbranch:c*", "inc"),
        },
        asked=None,
    )
    for asked in ("main", None, "?"):
        yield Tree(
            {
                **inc,
                "repo/.git/HEAD": b"x",
                "repo/.git/config": REMOTE + _cond("onbranch:main", "inc"),
            },
            modes={"repo/.git/HEAD": 0o000},
            asked=asked,
        )
    # The branch asked once, in the machine's read; then the own read.
    yield Tree(
        {
            **repo,
            **inc,
            "home/.gitconfig": _cond("onbranch:main", "../repo/.git/inc"),
        },
    )
    yield Tree(
        {
            **repo,
            **inc,
            "repo/.git/config": REMOTE + _cond("gitdir:**/repo/.git", "inc"),
        }
    )
    yield Tree(
        {**repo, **inc, "repo/.git/config": REMOTE + _cond("gitdir:**/other/", "inc")}
    )
    # %(prefix)/: a directory given, written as is, not known.
    for given in ("@/etc", "%(prefix)", None):
        yield Tree(
            {
                **repo,
                "etc/inc": b"[p]\n\tk = 1\n",
                "repo/.git/config": REMOTE + b"[include]\n\tpath = %(prefix)/inc\n",
            },
            prefix=given,
        )
    # hasconfig: the collecting pass and the stop rule's every branch.
    has = _cond("hasconfig:remote.*.url:https://example.invalid/**", "inc")
    yield Tree({**repo, **inc, "repo/.git/config": REMOTE + has})
    yield Tree(
        {**repo, "repo/.git/inc": b"[i]\n\tk = 1\n", "repo/.git/config": REMOTE + has}
    )
    yield Tree({**repo, **inc, "repo/.git/config": REMOTE + has}, hasconfig=False)
    # A remote URL only in an include a hasconfig condition holds; a
    # matched include git's reader of remotes dies on, which the
    # collecting read does not check: found only once the URLs settle.
    yield Tree({**head, **inc, "repo/.git/config": has})
    yield Tree(
        {
            **repo,
            "repo/.git/inc": b'[branch ""]\n\tx = 1\n',
            "repo/.git/config": REMOTE + has,
        }
    )
    yield Tree({**repo, **inc, "repo/.git/config": REMOTE + has}, hasconfig=None)
    yield Tree(
        {
            **repo,
            "repo/.git/inc": b'[remote "f"]\n\turl = f\n',
            "repo/.git/config": REMOTE + has,
        }
    )
    yield Tree(
        {
            **repo,
            **inc,
            "repo/.git/config": REMOTE
            + _cond("onbranch:main", "inc")
            + _cond("hasconfig:remote.*.url:https://example.invalid/**", "x"),
            "repo/.git/x": b"[x]\n\ty = 1\n",
        },
        modes={"repo/.git/HEAD": 0o000},
        asked="?",
    )
    yield Tree(
        {
            **repo,
            "home/.gitconfig": _cond(
                "hasconfig:remote.*.url:https://example.invalid/**", "../repo/.git/x"
            ),
            "repo/.git/x": b"[x]\n\ty = 1\n",
        }
    )
    # An unsettled onbranch include setting only a pushurl goes on; its
    # twin setting a url stops. More shapes of the unsettled stop: a
    # match past the wildmatch depth, an unsettled include nested in a
    # held one.
    for key in (b"pushurl", b"url"):
        yield Tree(
            {
                **repo,
                "repo/.git/inc": b"[i]\n\tk = 1\n",
                "repo/.git/x": b'[remote "o"]\n\t' + key + b" = p\n",
                "repo/.git/config": REMOTE + has + _cond("onbranch:main", "x"),
            },
            modes={"repo/.git/HEAD": 0o000},
            asked="?",
        )
    unsettled = {
        **repo,
        "repo/.git/inc": b"[i]\n\tk = 1\n",
        "repo/.git/x": b'[remote "u"]\n\turl = p\n',
    }
    long = "a" * 200
    yield Tree(
        {
            **unsettled,
            "repo/.git/HEAD": f"ref: refs/heads/{long}\n".encode(),
            "repo/.git/config": REMOTE
            + has
            + _cond("onbranch:" + "*a" * 160 + "b", "x"),
        }
    )
    yield Tree(
        {
            **unsettled,
            "repo/.git/y": _cond("onbranch:main", "x"),
            "repo/.git/config": REMOTE + has + b"[include]\n\tpath = y\n",
        },
        modes={"repo/.git/HEAD": 0o000},
        asked="?",
    )
    # A hasconfig collection after a machine read that dies, and one not
    # read whole: no collecting pass.
    hasinc = {**repo, **inc, "repo/.git/config": REMOTE + has}
    yield Tree({**hasinc, "home/.gitconfig": b"[bad\n"})
    yield Tree(hasinc, fifos=("home/.gitconfig",))
    # A failed read, an incomplete one, and maybe_dies alone (no stop).
    yield Tree({**repo, "repo/.git/config": REMOTE + b"[bad\n"})
    yield Tree(
        {**repo, "repo/.git/config": REMOTE + b'[includeIf "onbranch:main"]\n\tpath\n'},
        modes={"repo/.git/HEAD": 0o000},
        asked="?",
    )
    yield Tree(
        {
            **head,
            "repo/.git/config": b"[core]\n\trepositoryformatversion = 9\n" + REMOTE,
        }
    )


def _file() -> bytes:
    refuse_root()
    cases = list(_trees())
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "location-remotes"
        for i, case in enumerate(cases):
            root = base / str(i)
            root.mkdir(parents=True)
            os.chmod(root, 0o755)
            answer = _answer(case, root)
            shutil.rmtree(root)
            lines.append({**case.record(), "r": answer})
    header = {"fixture": "location_remotes", "cases": len(cases)}
    return jsonl([header, *lines])


def generate() -> dict[str, bytes]:
    return {"location_remotes.jsonl": _file()}
