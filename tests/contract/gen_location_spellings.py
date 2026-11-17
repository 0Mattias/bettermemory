"""Golden cases for which URLs a checkout's remotes are keyed by (POSIX).

`location_git._spellings` with `_best_rule` and `_undecided_matters`, the
rest of `_rewrites` after it, and `read_remotes` over the trees of
`gen_location_remotes` (its `Tree`, read the same way): each remote URL
as written and as ``url.<base>.insteadOf`` rewrites it, whether the rule
is the machine's, the keys and the alias keys, and why the capture
declines. ``so`` also names what the git on PATH does (``legacy``,
``resets``, ``version``). The key is `location.remote_key` with a local
URL keyed as its text: resolving its path is the caller's (`_local`).

Its own file, beside ``location_remotes.jsonl``: that one pins the reads
`_spellings` is handed, this one what comes of them, so neither record
holds the other's fields.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from typing import Any

from . import _sandbox
from ._common import jsonl
from .gen_fs_realpath import refuse_root
from .gen_location_remotes import Tree, _answer, _cond

S: dict[str, Any] = {"legacy": True, "resets": True, "version": [2, 54, 0]}
HEAD = {"repo/.git/HEAD": b"ref: refs/heads/main\n"}
U = b"https://example.invalid/o/r"


def _remote(name: bytes, *urls: bytes, key: bytes = b"url") -> bytes:
    lines = b"".join(b"\t" + key + b" = " + u + b"\n" for u in urls)
    return b'[remote "' + name + b'"]\n' + lines


def _rule(base: bytes, *prefixes: bytes | None, key: bytes = b"insteadOf") -> bytes:
    lines = b"".join(
        b"\t" + key + (b"\n" if p is None else b" = " + p + b"\n") for p in prefixes
    )
    return b'[url "' + base + b'"]\n' + lines


def t(
    own: bytes,
    *,
    home: bytes | None = None,
    spell: dict[str, Any] | None = None,
    **kw: Any,
) -> Tree:
    files: dict[str, Any] = {**HEAD, "repo/.git/config": own}
    if home is not None:
        files["home/.gitconfig"] = home
    files.update(kw.pop("files", {}))
    return Tree(files, spell={**S, **(spell or {})}, **kw)


def _trees() -> Iterator[Tree]:
    origin = _remote(b"origin", U)
    # Remotes: one, several URLs, two remotes, a pushurl beside a url, a
    # URL in two remotes, a remote with no URL.
    yield t(origin)
    yield t(_remote(b"origin", U, b"git@example.invalid:o/s.git"))
    yield t(origin + _remote(b"up", b"https://example.invalid/u/p"))
    yield t(
        _remote(b"origin", b"https://example.invalid/push", key=b"pushurl") + origin
    )
    yield t(origin + _remote(b"twin", U))
    yield t(origin + b'[remote "bare"]\n\tfetch = +refs/heads/*:refs/remotes/bare/*\n')
    # insteadOf: the own config's, the machine's (an alias), the system
    # file's; pushInsteadOf alone; several bases (the longest prefix
    # wins); equal prefixes under two bases (the first met) and in one;
    # an empty prefix, an empty base.
    yield t(origin + _rule(b"https://mirror.invalid/", b"https://example.invalid/"))
    yield t(origin, home=_rule(b"https://mirror.invalid/", b"https://example.invalid/"))
    yield t(
        origin,
        env={"GIT_CONFIG_NOSYSTEM": None, "GIT_CONFIG_SYSTEM": "@/etc/sys"},
        files={
            "etc/sys": _rule(b"https://sys.invalid/", b"https://example.invalid/o/")
        },
    )
    yield t(
        origin
        + _rule(
            b"https://push.invalid/", b"https://example.invalid/", key=b"pushInsteadOf"
        )
    )
    yield t(
        origin
        + _rule(b"https://short.invalid/", b"https://example.invalid/")
        + _rule(b"https://long.invalid/", b"https://example.invalid/o/")
    )
    yield t(
        origin
        + _rule(b"https://long.invalid/", b"https://example.invalid/o/")
        + _rule(b"https://short.invalid/", b"https://example.invalid/")
    )
    yield t(
        origin,
        home=_rule(b"https://b.invalid/", b"https://example.invalid/"),
        files={
            "repo/.git/config": origin
            + _rule(b"https://a.invalid/", b"https://example.invalid/")
        },
    )
    yield t(
        origin
        + _rule(b"https://a.invalid/", b"https://example.invalid/")
        + _rule(b"https://b.invalid/", b"https://example.invalid/")
    )
    yield t(
        origin
        + _rule(
            b"https://a.invalid/",
            b"https://example.invalid/",
            b"https://example.invalid/",
        )
    )
    yield t(
        origin
        + _rule(b"https://a.invalid/", b"https://other.invalid/")
        + _rule(b"https://b.invalid/", b"https://example.invalid/")
        + _rule(b"https://a.invalid/", b"https://example.invalid/")
    )
    yield t(origin + _rule(b"https://any.invalid/", b""))
    yield t(origin + _rule(b"", b"https://example.invalid/"))
    yield t(
        origin, home=_rule(b"https://example.invalid/", b"https://example.invalid/")
    )
    # A machine rule rewriting to another key, and that key fetched too
    # (no alias then); a URL that keys as its rewrite does.
    alias = _rule(b"https://example.invalid/x/", b"https://example.invalid/o/")
    yield t(origin, home=alias)
    yield t(origin + _remote(b"x", b"git@example.invalid:o/r"), home=alias)
    yield t(
        origin, home=_rule(b"https://EXAMPLE.invalid/o/", b"https://example.invalid/o/")
    )
    # A URL set then emptied, under each `resets` (a url or an insteadOf
    # with no value never gets here: git's reader of remotes dies on it).
    reset = _remote(b"origin", b"https://example.invalid/a", b'""', U)
    for resets in (True, False, None):
        yield t(reset, spell={"resets": resets})
    # Legacy files: a remote with no URL in the config, the same remote
    # in both, legacy read or not or not known; a branches file with no
    # branch against init.defaultBranch and the test variable; an empty
    # URL there with resets not known.
    lg = {"repo/.git/remotes/lr": b"URL: https://example.invalid/l/r\n"}
    yield t(b"", files=lg)
    yield t(
        origin,
        files={"repo/.git/remotes/origin": b"URL: https://example.invalid/l/o\n"},
    )
    yield t(b'[remote "lr"]\n\turl = ""\n', files=lg)
    for legacy in (False, None):
        yield t(b"", files=lg, spell={"legacy": legacy})
    yield t(origin, files=lg, spell={"legacy": None})
    br = {"repo/.git/branches/b": b"https://example.invalid/b/r\n"}
    yield t(b"", files=br)
    yield t(b"[init]\n\tdefaultBranch = trunk\n", files=br)
    yield t(b"[init]\n\tdefaultBranch = bad..name\n", files=br)
    yield t(b"", files=br, env={"GIT_TEST_DEFAULT_INITIAL_BRANCH_NAME": "dev"})
    yield t(
        b"",
        files={"repo/.git/remotes/e": b"URL:\nURL: https://example.invalid/e\n"},
        spell={"resets": None},
    )
    yield t(
        b"",
        files={"repo/.git/remotes/e": b"URL:\nURL: https://example.invalid/e\n"},
        spell={"resets": False},
    )
    # Blank space a git before 2.45 reads as a space, in a URL and in a
    # prefix, under each version.
    for version in ([2, 44, 9], [2, 45, 0], None):
        yield t(
            _remote(b"origin", b"https://example.invalid/a\tb"),
            spell={"version": version},
        )
    yield t(
        origin + _rule(b"https://m.invalid/", b"https://example.invalid/\tx"),
        spell={"version": [2, 44, 0]},
    )
    yield t(_remote(b"/x", b"https://example.invalid/a\tb"), spell={"version": None})

    # Includes under an unsettled condition (HEAD unread, git not
    # answering): each branch of _undecided_matters.
    def unsettled(x: bytes, own: bytes = origin, **kw: Any) -> Tree:
        return t(
            own + _cond("onbranch:main", "x"),
            files={"repo/.git/x": x, **kw.pop("files", {})},
            modes={"repo/.git/HEAD": 0o000},
            asked="?",
            **kw,
        )

    yield unsettled(_remote(b"u", b"https://example.invalid/u"))
    yield unsettled(_remote(b"/u", b"https://example.invalid/u"))
    yield unsettled(_remote(b"u", b"https://example.invalid/u", key=b"pushurl"))
    yield unsettled(_rule(b"https://m.invalid/", b"https://example.invalid/"))
    yield unsettled(_rule(b"https://m.invalid/", b"https://other.invalid/"))
    yield unsettled(
        _rule(b"https://m.invalid/", b"https://example.invalid/", key=b"pushInsteadOf")
    )
    tied = (
        origin
        + _rule(b"https://a.invalid/", b"https://example.invalid/")
        + _rule(b"https://b.invalid/", b"https://example.invalid/")
    )
    yield unsettled(_rule(b"https://b.invalid/", b"https://other.invalid/"), own=tied)
    yield unsettled(_rule(b"https://a.invalid/", b"https://other.invalid/"), own=tied)
    yield unsettled(_rule(b"https://c.invalid/", b"https://other.invalid/"), own=tied)
    yield unsettled(b"[init]\n\tdefaultBranch = x\n")
    yield unsettled(b"[init]\n\tdefaultBranch = x\n", own=b"", files=br)
    yield unsettled(b"[x]\n\ty = 1\n")
    yield t(
        origin + b'[includeIf "onbranch:main"]\n\tpath\n',
        modes={"repo/.git/HEAD": 0o000},
        asked="?",
    )
    yield t(
        b'[includeIf "onbranch:main"]\n\tpath\n',
        files=lg,
        modes={"repo/.git/HEAD": 0o000},
        asked="?",
    )
    # Names that need care; bytes a URL decodes with a replacement; URL
    # forms keyed alike and apart; a blank URL.
    yield t(
        _remote(b"a.b", U)
        + _remote(b"", b"https://example.invalid/e")
        + _remote(b"\xc3\xa9", b"https://example.invalid/u")
    )
    yield t(_remote(b"\xff", b"https://example.invalid/\xff\xed\xa0\x80/r"))
    yield t(
        _remote(
            b"o",
            b"https://Example.Invalid/O/R.git",
            b"git@example.invalid:o/r",
            b"ssh://git@example.invalid/o/r/",
            b"https://example.invalid/o/r.git.git",
        )
    )
    yield t(_remote(b"o", b"/srv/repo", b"./rel", b"../up", b"C:/x", b"host:path"))
    yield t(_remote(b"o", b'" "', U))

    # A remote the machine's config names is not keyed; a test branch
    # name git refuses where the config's would do; the machine's read
    # met before the own one.
    yield t(origin, home=_remote(b"m", b"https://example.invalid/m"))
    yield t(
        b"[init]\n\tdefaultBranch = trunk\n",
        files=br,
        env={"GIT_TEST_DEFAULT_INITIAL_BRANCH_NAME": "bad..name"},
    )
    yield t(
        origin + b'[includeIf "onbranch:main"]\n\tpath\n',
        home=_cond("onbranch:main", "../repo/.git/x"),
        files={"repo/.git/x": _remote(b"u", b"https://example.invalid/u")},
        modes={"repo/.git/HEAD": 0o000},
        asked="?",
    )

    # More of the blank-space decline in an insteadOf prefix, and the
    # versions that keep it.
    tab = origin + _rule(b"https://m.invalid/", b"https://example.invalid/\tx")
    for version in (None, [2, 45, 0]):
        yield t(tab, spell={"version": version})
    yield t(
        origin,
        home=_rule(b"https://m.invalid/", b"https://ex\tq"),
        spell={"version": None},
    )
    # Tied bases: the winner the machine's and the include naming the
    # own one; three bases tied; a tie on a prefix that rewrites no URL.
    yield unsettled(
        _rule(b"https://b.invalid/", b"https://other.invalid/"),
        own=origin + _rule(b"https://b.invalid/", b"https://example.invalid/"),
        home=_rule(b"https://a.invalid/", b"https://example.invalid/"),
    )
    three = (
        origin
        + _rule(b"https://a.invalid/", b"https://example.invalid/")
        + _rule(b"https://b.invalid/", b"https://example.invalid/")
        + _rule(b"https://c.invalid/", b"https://example.invalid/")
    )
    yield unsettled(_rule(b"https://c.invalid/", b"https://other.invalid/"), own=three)
    yield unsettled(
        _rule(b"https://b.invalid/", b"https://other.invalid/"),
        own=origin
        + _rule(b"https://a.invalid/", b"https://example.invalid/o/")
        + _rule(b"https://b.invalid/", b"https://example.invalid/"),
    )
    yield unsettled(
        _rule(b"https://b.invalid/", b"https://other.invalid/"),
        own=origin
        + _rule(b"https://a.invalid/", b"https://example.invalid/")
        + _rule(b"https://b.invalid/", b"https://none.invalid/"),
    )
    # Legacy files git dies on, beside a remote it reads: a refspec it
    # refuses, a branch name it refuses.
    dies = {
        "repo/.git/remotes/d": b"URL: https://example.invalid/d\nPull: a:b:c\n",
        "repo/.git/branches/e": b"https://example.invalid/e#bad..name\n",
    }
    yield t(b"", files={**dies, **lg})
    yield t(b'[remote "d"]\n\turl = ""\n', files={**dies, **lg})


def _file() -> bytes:
    refuse_root()
    cases = list(_trees())
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "location-spellings"
        for i, case in enumerate(cases):
            root = base / str(i)
            root.mkdir(parents=True)
            os.chmod(root, 0o755)
            answer = _answer(case, root, spell=True)
            shutil.rmtree(root)
            lines.append({**case.record(), "r": answer})
    header = {"fixture": "location_spellings", "cases": len(cases)}
    return jsonl([header, *lines])


def generate() -> dict[str, bytes]:
    return {"location_spellings.jsonl": _file()}
