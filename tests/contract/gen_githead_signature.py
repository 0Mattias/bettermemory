"""Fixtures: `githead.entry_above` and the change signature
(``githead_signature.jsonl``).

Unit githead-2 (``native/.../githead/signature.rs``) ports `entry_above`,
`_stops_at`, `_would_be_git_directory`, `_ref_stamps` and `signature`;
this pins `entry_above` and `signature` to the reference's own functions on
real trees, laid and entered as `gen_githead_discovery` lays them, with the
same stand-in for ``os`` (`_Os`: the case's environment, user and owners).

Each case is a tree in `gen_githead_discovery`'s spelling and ``"q"``, one
answer per query ``op:args`` (split at ``|``), asked in order:

- ``E:start`` `entry_above`: a bool;
- ``G:start|flags`` `signature` (``n``: ``owner=False``):
  ``{"g": gitdir | null | "u", "h": HEAD's bytes, "c": config,
  "hf": HEAD's stamp (past 256 bytes ``{"n", "sha256"}``), "r": [ref stamps] | null, "pk": packed-refs,
  "env": [GIT_DIR, GIT_WORK_TREE, GIT_CEILING_DIRECTORIES], "eq": i}``
  where ``"u"`` is the undetermined marker and ``i`` the first signature
  query of the case equal to this one;
- ``W:rel`` rewrites the file ``rel`` as git does, through a new file and
  a rename, with the same bytes (no answer, null).

A stamp is null or ``{"t": st_mode >> 12, "eq": i}`` with ``"s"``, the
size, for a regular file alone (a folder's size follows the file system),
``i`` the first stamp of the case equal to it: no time or inode is in a
row. A path is spelled with the case's directory as ``<scratch>``.
Gates: POSIX only (links, modes, owners). A lowercase ``head`` is left to
the native tests: whether ``HEAD`` finds it follows the file system's case
folding (macOS's APFS folds, Linux's ext4 does not). The device boundary
and a FIFO ref are the native tests' too.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bettermemory import gitdir_pin, githead
from bettermemory import gitfile_rule as g

from . import _sandbox
from ._common import jsonl
from .gen_discovery import _clear_above
from .gen_follow import HEAD, V0, _repo, _spell, _text
from .gen_fs_realpath import _b, refuse_root
from .gen_githead_discovery import Case, _Os

SHA = "1" * 40
SHA2 = "2" * 40


def _stamp(got: Any, stamps: list[Any]) -> Any:
    if got is None:
        return None
    stamps.append(got)
    shown: dict[str, Any] = {"t": got[4] >> 12, "eq": stamps.index(got)}
    if stat.S_ISREG(got[4]):
        shown["s"] = got[2]
    return shown


def _head(data: bytes | None) -> Any:
    """HEAD's bytes, or past 256 of them their count and SHA-256."""
    if data is None or len(data) <= 256:
        return None if data is None else _b(data)
    return {"n": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _signature(sig: Any, real: str, stamps: list[Any], sigs: list[Any]) -> Any:
    sigs.append(sig)
    first = sig[0]
    if isinstance(first, githead._Undetermined):
        g_out: Any = "u"
    elif first is None:
        g_out = None
    else:
        g_out = _spell(first, real)
    refs = sig[4]
    return {
        "g": g_out,
        "h": _head(sig[1]),
        "c": _stamp(sig[2], stamps),
        "hf": _stamp(sig[3], stamps),
        "r": None if refs is None else [_stamp(r, stamps) for r in refs],
        "pk": _stamp(sig[5], stamps),
        "env": [None if v is None else _spell(v, real) for v in sig[6:]],
        "eq": next(i for i, s in enumerate(sigs) if s == sig),
    }


def _rewrite(path: Path) -> None:
    data = path.read_bytes()
    temporary = path.with_name(path.name + ".lock")
    temporary.write_bytes(data)
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def _query(query: str, real: str, stamps: list[Any], sigs: list[Any]) -> dict[str, Any]:
    op, _, raw = query.partition(":")
    args = [a.replace("<root>", real) for a in raw.split("|")]
    flags = args[1] if len(args) > 1 else ""
    out: dict[str, Any] = {"p": _b(query.encode())}
    if op == "E":
        out["v"] = githead.entry_above(Path(args[0]))
    elif op == "G":
        sig = githead.signature(Path(args[0]), owner="n" not in flags)
        out["v"] = _signature(sig, real, stamps, sigs)
    elif op == "W":
        _rewrite(Path(real) / raw)
        out["v"] = None
    else:
        raise ValueError(op)
    return out


def _answer(case: Case, root: Path, pins: Path) -> list[dict[str, Any]]:
    os.chmod(root, 0o755)
    saved = os.getcwd()
    os.chdir(root)
    try:
        real = os.getcwd()
        _clear_above(real)
        base = Path(real)
        for d in case.dirs:
            (base / d).mkdir(parents=True, exist_ok=True)
        for rel, value in case.files.items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_text(value).replace(b"<root>", os.fsencode(real)))
            os.chmod(path, 0o644)
        for rel, target in case.links.items():
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target.replace("<root>", real), base / rel)
        pins.mkdir(parents=True)
        for rel in sorted(case.modes, key=lambda r: -r.count("/")):
            os.chmod(base / rel, case.modes[rel])
        me = os.geteuid()  # type: ignore[attr-defined,unused-ignore]
        fake = _Os(case, real, me)
        stamps: list[Any] = []
        sigs: list[Any] = []
        try:
            with (
                patch.dict(os.environ),
                patch.object(githead, "os", fake),
                patch.object(g, "_git_is_at_least", lambda want: (2, 54) >= want),
                patch.object(g, "_reads_compat", lambda: True),
                patch.object(g, "_RECORDED_BUDGET", 60.0),
                patch.object(gitdir_pin, "pins_dir", lambda: pins),
            ):
                os.environ.pop("GIT_COMMON_DIR", None)
                return [_query(q, real, stamps, sigs) for q in case.queries]
        finally:
            for rel in case.modes:
                os.chmod(base / rel, 0o700)
    finally:
        os.chdir(saved)


# --- the trees ---------------------------------------------------------------


def _sig(start: str = "<root>/r") -> list[str]:
    return [f"G:{start}", f"G:{start}|n"]


def _branch(refs: dict[str, Any], head: str = HEAD) -> dict[str, Any]:
    files = {**_repo("r/.git", V0), "r/.git/HEAD": head, "r/in/f": ""}
    files |= {f"r/.git/{name}": text for name, text in refs.items()}
    return files


def _cases() -> Iterator[Case]:
    # Outside any repository, and a start that is missing or a file.
    yield Case(
        [*_sig("<root>/plain"), *_sig("<root>/nothere"), *_sig("<root>/plain/f")],
        files={"plain/f": ""},
    )
    # Detached.
    yield Case(_sig() + _sig("<root>/r/in"), files=_branch({}, f"{SHA}\n"))
    # On a loose branch, rewritten in turn.
    loose = _branch({"refs/heads/main": f"{SHA}\n"})
    yield Case(
        [
            *_sig(),
            "G:<root>/r",
            "W:r/.git/HEAD",
            "G:<root>/r",
            "W:r/.git/refs/heads/main",
            "G:<root>/r",
            "W:r/.git/config",
            "G:<root>/r",
            "E:<root>/r",
            "E:<root>/r/in",
        ],
        files=loose,
    )
    # Packed only, and packed beside a loose file.
    packed = _branch(
        {"packed-refs": f"# pack-refs with: peeled sorted\n{SHA} refs/heads/main\n"}
    )
    yield Case(
        [
            "G:<root>/r",
            "G:<root>/r",
            "W:r/.git/packed-refs",
            "G:<root>/r",
            "G:<root>/r|n",
        ],
        files=packed,
    )
    yield Case(
        ["G:<root>/r", "W:r/.git/packed-refs", "G:<root>/r"],
        files={**packed, "r/.git/refs/heads/main": f"{SHA2}\n"},
    )
    # The ref a directory, with and without packed-refs.
    yield Case(
        _sig(),
        files=_branch({"refs/heads/main/x": f"{SHA}\n"}),
    )
    yield Case(
        _sig(),
        files=_branch(
            {"refs/heads/main/x": f"{SHA}\n", "packed-refs": f"{SHA} refs/heads/main\n"}
        ),
    )
    # Symbolic chains: to the depth git reads, and past it.
    for depth in (1, 2, 3, 4, 5):
        refs: dict[str, Any] = {}
        for i in range(depth - 1):
            refs[f"refs/heads/b{i}"] = f"ref: refs/heads/b{i + 1}\n"
        refs[f"refs/heads/b{depth - 1}"] = f"{SHA}\n"
        yield Case(
            ["G:<root>/r", "W:r/.git/refs/heads/b0", "G:<root>/r"],
            files=_branch(refs, "ref: refs/heads/b0\n"),
        )
    # A chain to a missing ref, and to a ref of a bad shape.
    yield Case(
        _sig(),
        files=_branch({"refs/heads/main": "ref: refs/heads/gone\n"}),
    )
    for text in ("ref: refs/heads/.bad\n", "not a hash\n", "", "ref: HEAD\n"):
        yield Case(_sig(), files=_branch({"refs/heads/main": text}))
    # Sizes of a ref and of HEAD at and past the limit.
    limit = 1 << 16
    for n in (limit, limit + 1):
        body = {"p": SHA + "\n", "x": " ", "n": n - 41}
        yield Case(["G:<root>/r"], files=_branch({"refs/heads/main": body}))
        yield Case(["G:<root>/r"], files=_branch({}, body))  # type: ignore[arg-type]
    # Reftable's HEAD, and other HEADs `_parse` refuses.
    for head in (
        "ref: refs/heads/.invalid\n",
        "ref: refs/x\n",
        "ref:refs/heads/m\n",
        "",
    ):
        yield Case(_sig(), files=_branch({}, head))
    # HEAD a link (git's oldest format), a ref a link.
    yield Case(
        _sig(),
        files={k: v for k, v in _branch({}).items() if k != "r/.git/HEAD"}
        | {"r/.git/refs/heads/main": f"{SHA}\n"},
        links={"r/.git/HEAD": "refs/heads/main"},
    )
    yield Case(
        _sig(),
        files=_branch({"refs/heads/real": f"{SHA}\n"}),
        links={"r/.git/refs/heads/main": "real"},
    )
    # A ref link that dangles, and one to a directory: stamped unfollowed.
    for target in ("gone", "dir"):
        yield Case(
            _sig(),
            files=_branch({"refs/heads/dir/x": f"{SHA}\n"}),
            links={"r/.git/refs/heads/main": target},
        )
    # config and packed-refs links to regular files: stamped followed.
    yield Case(
        _sig(),
        files={
            k: v
            for k, v in _branch(
                {
                    "refs/heads/main": f"{SHA}\n",
                    "config.real": V0,
                    "packed.real": f"{SHA} refs/heads/main\n",
                }
            ).items()
            if k != "r/.git/config"
        },
        links={"r/.git/config": "config.real", "r/.git/packed-refs": "packed.real"},
    )
    # No config file.
    yield Case(
        _sig(),
        files={
            k: v
            for k, v in _branch({"refs/heads/main": f"{SHA}\n"}).items()
            if k != "r/.git/config"
        },
    )
    # A linked worktree on a per-worktree ref, and on a shared one.
    wt = {
        **_branch({"refs/heads/main": f"{SHA}\n", "refs/worktree/x": f"{SHA}\n"}),
        "r/.git/worktrees/wt/HEAD": "ref: refs/worktree/x\n",
        "r/.git/worktrees/wt/commondir": "../..\n",
        "r/.git/worktrees/wt/gitdir": "<root>/wt/.git\n",
        "r/.git/worktrees/wt/refs/worktree/x": f"{SHA}\n",
        "wt/.git": "gitdir: <root>/r/.git/worktrees/wt\n",
        "wt/in/f": "",
    }
    yield Case([*_sig("<root>/wt/in"), "E:<root>/wt/in"], files=wt)
    yield Case(
        _sig("<root>/wt"),
        files={**wt, "r/.git/worktrees/wt/HEAD": HEAD},
    )
    # The worktree's HEAD detached, of another size than the main HEAD.
    yield Case(
        _sig("<root>/wt"),
        files={**wt, "r/.git/worktrees/wt/HEAD": f"{SHA}\n"},
    )
    # A `.git` file to a git directory kept apart; one membership refuses
    # (it names another checkout's git directory).
    yield Case(
        [*_sig("<root>/s/in"), "E:<root>/s/in"],
        files={
            **_repo("sep", V0),
            "sep/refs/heads/main": f"{SHA}\n",
            "s/.git": "gitdir: <root>/sep\n",
            "s/in/f": "",
        },
    )
    yield Case(
        [*_sig("<root>/o/in"), "E:<root>/o/in"],
        files={**_branch({}), "o/.git": "gitdir: <root>/r/.git\n", "o/in/f": ""},
    )
    # Owners: another's repository is undetermined with `owner`, read
    # without it.
    own = _branch({"refs/heads/main": f"{SHA}\n"})
    for euid, owned in (("same", ("r/.git",)), ("other", ()), ("same", ())):
        yield Case(_sig("<root>/r/in"), files=own, euid=euid, owned=owned)
    # The environment: kept in every slot, and a variable that moves
    # discovery.
    for env in (
        {"GIT_DIR": "<root>/r/.git"},
        {"GIT_WORK_TREE": "<root>/r"},
        {"GIT_CEILING_DIRECTORIES": "<root>/r"},
        {"GIT_CEILING_DIRECTORIES": "<root>"},
        {"GIT_COMMON_DIR": "<root>/r/.git"},
        {"GIT_DISCOVERY_ACROSS_FILESYSTEM": "1"},
    ):
        yield Case([*_sig("<root>/r/in"), "E:<root>/r/in"], files=own, env=env)
    # Inside the git directory, and a bare repository.
    yield Case(
        [*_sig("<root>/r/.git"), *_sig("<root>/r/.git/refs"), *_sig("<root>/bare")],
        files={**own, **_repo("bare", V0)},
    )

    # entry_above over `_stops_at`'s cases.
    e = [
        "E:<root>/headonly/in",
        "E:<root>/headdir/in",
        "E:<root>/emptygit/in",
        "E:<root>/notgit/in",
        "E:<root>/badhead/in",
        "E:<root>/nohead/in",
        "E:<root>/noobjects/in",
        "E:<root>/norefs/in",
        "E:<root>/gitfile/in",
        "E:<root>/anyfile/in",
        "E:<root>/dirgit/in",
        "E:<root>/linkgit/in",
        "E:<root>/linkhead/in",
        "E:<root>/badlinkhead/in",
        "E:<root>/bare/in",
        "E:<root>/commondir/in",
        "E:<root>/badcommondir/in",
        "E:<root>/missingcommondir/in",
        "E:<root>/hiddenobj/in",
        "E:<root>/locked/in",
        "E:<root>/lockedhead/in",
        "E:<root>/emptycd/in",
        "E:<root>/linkgitfile/in",
        "E:<root>/linkgitfile",
        "E:<root>/danglingobj/in",
        "E:<root>/dirrefs/in",
        "E:<root>/dirgit/in/f",
        "E:<root>/dirgit/in",
        "E:<root>/plain",
        "E:<root>/plain/f",
        "E:<root>/nothere",
        "E:<root>/nothere/x",
        "E:plain",
        "E:",
        "E:<root>/linkstart",
        *_sig("<root>/headonly/in"),
        *_sig("<root>/notgit/in"),
        *_sig("<root>/anyfile/in"),
        *_sig("<root>/locked/in"),
    ]
    world: dict[str, Any] = {"plain/f": ""}
    for name in (
        "headonly",
        "headdir",
        "emptygit",
        "notgit",
        "badhead",
        "nohead",
        "noobjects",
        "norefs",
        "gitfile",
        "anyfile",
        "dirgit",
        "linkgit",
        "linkhead",
        "badlinkhead",
        "bare",
        "commondir",
        "badcommondir",
        "missingcommondir",
        "hiddenobj",
        "locked",
        "lockedhead",
        "emptycd",
        "linkgitfile",
        "danglingobj",
        "dirrefs",
    ):
        world[f"{name}/in/f"] = ""
    world |= {
        "headonly/HEAD": HEAD,
        "headdir/HEAD/x": "",
        "emptygit/.git/.keep": "",
        **_repo("notgit/.git"),
        "notgit/.git/HEAD": "nonsense\n",
        **_repo("badhead/.git"),
        "badhead/.git/HEAD": "ref: HEAD\n",
        "nohead/.git/objects/.keep": "",
        "nohead/.git/refs/.keep": "",
        "noobjects/.git/HEAD": HEAD,
        "noobjects/.git/refs/.keep": "",
        "norefs/.git/HEAD": HEAD,
        "norefs/.git/objects/.keep": "",
        "gitfile/.git": "gitdir: <root>/nothere\n",
        "anyfile/.git": "anything\n",
        **_repo("dirgit/.git"),
        **_repo("target"),
        **_repo("linkhead/.git"),
        **_repo("badlinkhead/.git"),
        **_repo("bare"),
        **_repo("cd/main"),
        "commondir/.git/HEAD": HEAD,
        "commondir/.git/commondir": "<root>/cd/main\n",
        "badcommondir/.git/HEAD": HEAD,
        "badcommondir/.git/commondir": "\n",
        "missingcommondir/.git/HEAD": HEAD,
        "missingcommondir/.git/commondir": "<root>/cd/gone\n",
        **_repo("hiddenobj/.git"),
        **_repo("locked/.git"),
        **_repo("lockedhead/.git"),
        **_repo("emptycd/.git"),
        "emptycd/.git/commondir": "",
        "gitfile-target": "gitdir: /nonexistent\n",
        **_repo("danglingobj/.git"),
        **_repo("dirrefs/.git"),
        "realrefs/.keep": "",
    }
    del world["linkhead/.git/HEAD"], world["badlinkhead/.git/HEAD"]
    del world["danglingobj/.git/objects/.keep"], world["dirrefs/.git/refs/.keep"]
    yield Case(
        e,
        files=world,
        links={
            "linkgit/.git": "<root>/target",
            "linkhead/.git/HEAD": "refs/heads/main",
            "badlinkhead/.git/HEAD": "elsewhere",
            "linkstart": "<root>/dirgit/in",
            "linkgitfile/.git": "<root>/gitfile-target",
            "danglingobj/.git/objects": "<root>/gone",
            "dirrefs/.git/refs": "<root>/realrefs",
        },
        modes={
            "hiddenobj/.git/objects": 0o000,
            "locked/.git": 0o600,
            "lockedhead/.git/HEAD": 0o000,
        },
    )
    # A ceiling between the start and the repository.
    for ceiling in (
        "<root>/r",
        "<root>/r/in",
        "<root>",
        "<root>/r:<root>/nothere",
        ":<root>/r",
    ):
        yield Case(
            ["E:<root>/r/in/deep", "E:<root>/r", *_sig("<root>/r/in/deep")],
            files={**own, "r/in/deep/f": ""},
            env={"GIT_CEILING_DIRECTORIES": ceiling},
        )


def _lines() -> list[dict[str, Any]]:
    refuse_root()
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "githead-signature"
        for i, case in enumerate(_cases()):
            root = base / str(i)
            root.mkdir(parents=True)
            answers = _answer(case, root, world.root / f"pins-{i}")
            for rel in case.modes:
                os.chmod(root / rel, 0o700)
            shutil.rmtree(root)
            lines.append({**case.record(), "q": answers})
    return lines


def generate() -> dict[str, bytes]:
    lines = _lines()
    header = {
        "fixture": "githead_signature",
        "cases": len(lines),
        "queries": sum(len(line["q"]) for line in lines),
    }
    return {"githead_signature.jsonl": jsonl([header, *lines])}
