"""Fixture: the stamped reads a kept answer about a checkout's remotes is
reused by (``location_stamped.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/gitconfig/stamped.rs``; this pins it
to `location_git`'s `machine_config_stamps` (with `reuse`),
`machine_config_reads`, `repository_config_stamps`, `_stamped_read` and its
two caches, and `config_unchanged` over a capture `_rewrites` takes.

Each case is a tree (``"f"`` files, ``"d"`` folders, ``"p"`` FIFOs, ``"m"``
modes; every other file 644, every folder 755) and ``"k"`` its inputs, as
in ``location_remotes.jsonl`` (``cd``, ``gd``, ``env``, ``so`` with
``own`` and ``system``, ``v``), then ``"s"`` a sequence of steps, each
answered in ``"r"``:

- ``["stamps", reuse, now]``: `machine_config_stamps`, ``now`` the
  monotonic clock; answers ``nm`` the files named, ``fl`` each one's
  files with stamps, ``eq`` whether all the stamps equal the last
  ``stamps`` step's (null for the first);
- ``["reads", now]``: `machine_config_reads`; ``fl`` each read's files;
- ``["repo"]``: `repository_config_stamps`; ``fl`` and ``eq`` as above;
- ``["capture"]``: `_rewrites`, kept for ``["unchanged"]``:
  `config_unchanged` of it (``u``);
- ``["change", kind, path, data]``: a change on disk (below);
- ``["clear"]``: `_clear_stamped_reads`.

After each step but a change, ``kp`` lists the kept reads, oldest first,
as ``[path, git dir, home, same]`` (``same``: the very read kept after the
last such step), and ``fr`` the fresh marks, ``[path, now]``. A ``kp`` of
more than one entry, and any other list of more than eight, is recorded
as ``{"n": length, "h": hash}``: FNV-1a 64 of its compact JSON
(`_digest`). ``["gitdir", rel]`` sets the git directory the steps after
it read for; ``["read", path]`` is `_stamped_read` of one file (``fl``).

A stamp is recorded as ``[mode, size]`` (null missing, ``"u"`` unstated),
never a time or an inode number. Every change is seen whatever the file
system's clock: a write sets the file's modification time to a fixed
time far from now (2001, a second apart for each change), a replace
renames a new file over the old while both exist (a new inode), and the
others change the mode, the size or the kind. A mode change, the one kind
a pair of which can leave nothing but the change time moved, is applied
again until that time has moved (`_until_ctime_moves`). A folder an entry is
added to gets the same fixed time.

POSIX only (`generate.POSIX_ONLY`): modes, FIFOs, the host's ``os.path``.
Not run as root (`gen_fs_realpath.refuse_root`).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import _git as git_runner
from bettermemory import gitconfig, gitfile_rule, gitsystem, location_git

from . import _sandbox
from ._common import jsonl
from .gen_fs_realpath import refuse_root
from .gen_gitconfig_file import _b
from .gen_location_remotes import _stamp

#: The time a change sets (2001), a second more for each change.
CHANGED_AT = 1_000_000_000
REMOTE = b'[remote "origin"]\n\turl = https://example.invalid/a\n'
HEAD = {"repo/.git/HEAD": b"ref: refs/heads/main\n"}


class Case:
    def __init__(
        self,
        files: dict[str, bytes],
        steps: list[list[Any]],
        *,
        dirs: tuple[str, ...] = (),
        fifos: tuple[str, ...] = (),
        modes: dict[str, int] | None = None,
        env: dict[str, str | None] | None = None,
        own: tuple[str, ...] = (),
        many: int = 0,
        system: str | None = None,
        git_dir: str = "repo/.git",
        version: tuple[int, int] | None = (2, 54),
    ) -> None:
        self.files, self.steps, self.dirs, self.fifos = files, steps, dirs, fifos
        self.modes = dict(modes or {})
        given: dict[str, str | None] = {"HOME": "@/home", "GIT_CONFIG_NOSYSTEM": "1"}
        given.update(env or {})
        self.ask: dict[str, Any] = {
            "cd": "repo/.git",
            "gd": git_dir,
            "env": {k: v for k, v in given.items() if v is not None},
            "so": {"own": list(own), "system": system},
        }
        if many:
            # ``many``: the build's own files go on with ``@/e/0`` to
            # ``@/e/<many - 1>`` (none there).
            self.ask["so"]["many"] = many
        self.ask |= {
            "v": None if version is None else list(version),
        }

    def record(self) -> dict[str, Any]:
        out: dict[str, Any] = {"f": {k: _b(v) for k, v in self.files.items()}}
        if self.dirs:
            out["d"] = list(self.dirs)
        if self.fifos:
            out["p"] = list(self.fifos)
        if self.modes:
            out["m"] = {k: f"{v:03o}" for k, v in self.modes.items()}
        out["k"] = self.ask
        out["s"] = [
            [_b(x) if isinstance(x, bytes) else x for x in s] for s in self.steps
        ]
        return out


def _build(case: Case, root: Path) -> None:
    for rel in case.dirs:
        (root / rel).mkdir(parents=True, exist_ok=True)
    for rel, data in case.files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
        os.chmod(root / rel, 0o644)
    for rel in case.fifos:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(root / rel)  # type: ignore[attr-defined,unused-ignore]
        os.chmod(root / rel, 0o644)
    for path in sorted(root.rglob("*"), key=lambda p: -len(p.parts)):
        if path.is_dir() and not path.is_symlink():
            os.chmod(path, 0o755)
    for rel, mode in case.modes.items():
        os.chmod(root / rel, mode)


def _digest(value: Any, most: int = 8) -> Any:
    """A list longer than `most` as its length and the FNV-1a 64 hash of
    its compact JSON (ASCII: every text in it is `_b`'s), so a sequence
    that fills the cache stays small; anything else as it is."""
    if not isinstance(value, list) or len(value) <= most:
        return value
    text = json.dumps(value, separators=(",", ":")).encode("ascii")
    h = 0xCBF29CE484222325
    for byte in text:
        h = ((h ^ byte) * 0x100000001B3) % (1 << 64)
    return {"n": len(value), "h": f"{h:016x}"}


def _until_ctime_moves(path: Path, apply: Callable[[], None]) -> None:
    """Apply a change whose only mark may be the change time (a mode set
    and set back leaves size, mode and mtime as they were), again until
    `st_ctime_ns` has moved from before it. Linux stamps the change time
    from a coarse clock (a 4 or 10 ms tick), so a file created and changed
    within one tick would keep its change time and the stamp would come out
    equal; APFS's nanosecond times never show that. No fixed sleep: the
    loop ends as soon as the clock has ticked, and fails loudly past five
    seconds."""
    before = os.stat(path).st_ctime_ns
    started = time.perf_counter()
    apply()
    while os.stat(path).st_ctime_ns == before:
        if time.perf_counter() - started > 5:
            raise RuntimeError(f"the change time of {path.name} did not move")
        time.sleep(0.002)
        apply()


def _change(root: Path, kind: str, rel: str, data: bytes | None, n: int) -> None:
    path = root / rel
    at = CHANGED_AT + n
    if kind == "write":
        assert data is not None
        path.write_bytes(data)
        os.utime(path, (at, at))
    elif kind == "mtime":
        os.utime(path, (at, at))
    elif kind == "rename":
        assert data is not None
        new = path.with_name(path.name + ".new")
        new.write_bytes(data)
        os.chmod(new, 0o644)
        os.utime(new, (at, at))
        os.rename(new, path)
    elif kind == "mode":
        mode = int(data or b"600", 8)
        _until_ctime_moves(path, lambda: os.chmod(path, mode))
    elif kind == "remove":
        path.unlink()
    elif kind == "create":
        assert data is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.chmod(path, 0o644)
        os.utime(path, (at, at))
        os.utime(path.parent, (at, at))
    elif kind == "dir":
        path.unlink()
        path.mkdir()
        os.chmod(path, 0o755)
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path)  # type: ignore[attr-defined,unused-ignore]
        os.chmod(path, 0o644)
    else:
        raise ValueError(kind)


def _answer(case: Case, root: Path) -> list[Any]:
    root = Path(os.path.realpath(root))
    _build(case, root)
    prefix = str(root)

    def at(value: str | None) -> str | None:
        if value is not None and value.startswith("@/"):
            return prefix + value[1:]
        return value

    def label(text: str | None) -> Any:
        return (
            None if text is None else _b(os.fsencode(text.replace(prefix, "<scratch>")))
        )

    def files(found: Any) -> list[Any]:
        return [[label(p), _stamp(s)] for p, s in found]

    so = case.ask["so"]
    own = [*so["own"], *(f"@/e/{i}" for i in range(so.get("many", 0)))]
    sources = gitsystem.Sources(
        own=tuple(at(p) or "" for p in own),
        system=at(so["system"]),
        hasconfig=True,
        legacy=True,
        resets=True,
        version=(2, 54, 0),
    )
    env = {k: at(v) or "" for k, v in case.ask["env"].items()}
    release = case.ask["v"]
    common = prefix + "/" + case.ask["cd"]
    git_dir = prefix + "/" + case.ask["gd"]
    clock = [0.0]

    def at_least(want: tuple[int, int]) -> bool | None:
        return None if release is None else tuple(release) >= want

    def run_git(*_a: Any, **_k: Any) -> Any:
        return subprocess.CompletedProcess([], 1, b"", b"")

    out: list[Any] = []
    last: dict[str, Any] = {}
    seen: dict[Any, Any] = {}
    captured: list[Any] = []
    changes = 0
    saved = os.getcwd()
    os.chdir(root)
    try:
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(gitsystem, "sources", lambda **_k: sources),
            mock.patch.object(location_git, "_sources_once", lambda: lambda: sources),
            mock.patch.object(git_runner, "run_git", run_git),
            mock.patch.object(gitfile_rule, "_git_is_at_least", at_least),
            mock.patch.object(time, "monotonic", lambda: clock[0]),
        ):
            gitconfig._clear_format_memo()
            location_git._clear_stamped_reads()
            for step in case.steps:
                kind = step[0]
                got: dict[str, Any] = {}
                if kind == "env":
                    if step[2] is None:
                        os.environ.pop(step[1], None)
                    else:
                        os.environ[step[1]] = at(step[2]) or ""
                    out.append({})
                    continue
                if kind == "gitdir":
                    git_dir = prefix + "/" + step[1]
                    out.append({})
                    continue
                if kind == "change":
                    _change(
                        root,
                        step[1],
                        step[2],
                        step[3] if len(step) > 3 else None,
                        changes,
                    )
                    changes += 1
                    out.append({})
                    continue
                if kind == "stamps":
                    clock[0] = step[2]
                    found = location_git.machine_config_stamps(git_dir, reuse=step[1])
                    got["nm"] = [label(p) for p in found[0]]  # type: ignore[attr-defined]
                    got["fl"] = [files(f) for f in found[1:]]
                    got["eq"] = (
                        None if "stamps" not in last else last["stamps"] == found
                    )
                    last["stamps"] = found
                elif kind == "reads":
                    clock[0] = step[1]
                    reads = location_git.machine_config_reads(git_dir)
                    got["fl"] = [files(r.files or ()) for r in reads]
                elif kind == "repo":
                    found = location_git.repository_config_stamps(common, git_dir)
                    got["fl"] = [files(f) for f in found]
                    got["eq"] = None if "repo" not in last else last["repo"] == found
                    last["repo"] = found
                elif kind == "capture":
                    captured[:] = [location_git._rewrites(common, git_dir)]
                    got["fl"] = files(captured[0].files)
                    got["nm"] = [label(p) for p in captured[0].named]
                elif kind == "unchanged":
                    cap = mock.Mock(
                        config_files=tuple(captured[0].files),
                        config_sources=tuple(captured[0].named),
                    )
                    got["u"] = location_git.config_unchanged(cap)
                elif kind == "read":
                    read = location_git._stamped_read(at(step[1]) or "", git_dir)
                    got["fl"] = files(read.files or ())
                elif kind == "clear":
                    location_git._clear_stamped_reads()
                else:
                    raise ValueError(kind)
                kept = []
                # The reads themselves are held, so an id is never reused.
                now_seen: dict[Any, Any] = {}
                for key, read in location_git._STAMPED_READS.items():
                    now_seen[key] = read
                    kept.append(
                        [
                            label(key[0]),
                            label(key[1]),
                            label(key[2]),
                            seen.get(key) is read,
                        ]
                    )
                seen = now_seen
                got["kp"] = kept
                got["fr"] = [
                    [label(k[0]), v] for k, v in location_git._FRESH_READS.items()
                ]
                out.append(
                    {k: _digest(v, 1 if k == "kp" else 8) for k, v in got.items()}
                )
    finally:
        os.chdir(saved)
        location_git._clear_stamped_reads()
        for path in root.rglob("*"):
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
    return out


def _cond(condition: str, path: str) -> bytes:
    return f'[includeIf "{condition}"]\n\tpath = {path}\n'.encode()


def _cases() -> Iterator[Case]:
    rule = b'[url "https://m.invalid/"]\n\tinsteadOf = https://example.invalid/\n'
    rule2 = b'[url "https://n.invalid/"]\n\tinsteadOf = https://example.invalid/\n'
    home = {
        "home/.gitconfig": rule + b"[include]\n\tpath = inc\n",
        "home/inc": b"[x]\n\ty = 1\n",
    }
    repo = {**HEAD, "repo/.git/config": REMOTE, **home}
    g = "home/.gitconfig"
    # machine_config_stamps, without and with reuse: no change, then each
    # kind of change to the global file, and to the file it includes.
    changes: list[list[Any]] = [
        [],
        [["change", "write", g, rule2 + b"[include]\n\tpath = inc\n"]],
        [["change", "write", g, rule + b"[include]\n\tpath = inc\n\n"]],
        [["change", "mtime", g]],
        [["change", "rename", g, rule + b"[include]\n\tpath = inc\n"]],
        [["change", "mode", g, b"600"]],
        [["change", "remove", g]],
        [["change", "dir", g]],
        [["change", "fifo", g]],
        [["change", "write", "home/inc", b"[x]\n\ty = 2\n"]],
        [["change", "remove", "home/inc"]],
        [["change", "write", "repo/.git/config", REMOTE + b"\n"]],
        [["change", "write", "repo/.git/HEAD", b"ref: refs/heads/side\n"]],
    ]
    # With no read taken just before, `reuse` has no mark to take: both
    # ways for no change and a write, without it for the rest.
    for i, change in enumerate(changes):
        for reuse in (False, True) if i < 2 else (False,):
            yield Case(
                repo, [["stamps", reuse, 10.0], *change, ["stamps", reuse, 10.5]]
            )
    # A file created where none was: the XDG file, git's first global one.
    yield Case(
        repo,
        [
            ["stamps", False, 1.0],
            ["change", "create", "home/.config/git/config", rule],
            ["stamps", False, 2.0],
        ],
    )
    # reads, then stamps within the window, at its end, before it, and a
    # second stamps after the mark is spent; a change inside the window
    # unseen with reuse, seen without.
    for now in (0.5, 0.999, 1.0, -0.1):
        yield Case(repo, [["reads", 0.0], ["stamps", True, now], ["stamps", True, now]])
    yield Case(repo, [["reads", 0.0], ["stamps", False, 0.5]])
    for reuse in (True, False):
        yield Case(
            repo,
            [
                ["reads", 5.0],
                ["change", "write", g, rule2],
                ["stamps", reuse, 5.2],
                ["stamps", reuse, 5.3],
            ],
        )
    yield Case(repo, [["reads", 1.0], ["reads", 2.0], ["stamps", True, 2.1]])
    yield Case(repo, [["stamps", False, 1.0], ["reads", 2.0], ["stamps", True, 2.1]])
    yield Case(repo, [["reads", 1.0], ["clear"], ["stamps", True, 1.1]])
    # Two git directories: kept apart.
    yield Case(
        {**repo, "repo/.git/worktrees/w/HEAD": b"ref: refs/heads/w\n"},
        [["stamps", False, 1.0], ["stamps", False, 1.0]],
        git_dir="repo/.git/worktrees/w",
    )
    # A system file and the build's own, besides the global ones.
    yield Case(
        {**repo, "etc/own": rule, "etc/sys": rule2},
        [
            ["stamps", False, 1.0],
            ["change", "write", "etc/sys", rule],
            ["stamps", False, 2.0],
        ],
        env={"GIT_CONFIG_NOSYSTEM": None},
        own=("@/etc/own",),
        system="@/etc/sys",
    )
    # An include under onbranch: is read whatever HEAD names, and HEAD
    # moving changes no stamp; a gitdir: include is matched.
    onb = {
        **repo,
        "home/.gitconfig": _cond("onbranch:main", "inc")
        + _cond("gitdir:**/repo/.git", "gd"),
    }
    yield Case(
        {**onb, "home/gd": b"[g]\n\tk = 1\n"},
        [
            ["stamps", False, 1.0],
            ["change", "write", "repo/.git/HEAD", b"ref: refs/heads/other\n"],
            ["stamps", False, 2.0],
            ["change", "write", "home/gd", b"[g]\n\tk = 2\n"],
            ["stamps", False, 3.0],
        ],
    )
    # repository_config_stamps: the common config, config.worktree where
    # git reads it, an include of it; changes to each.
    wt = b"[core]\n\trepositoryformatversion = 1\n[extensions]\n\tworktreeConfig = true\n"
    own = {
        **HEAD,
        **home,
        "repo/.git/config": wt + REMOTE + b"[include]\n\tpath = rinc\n",
        "repo/.git/rinc": b"[r]\n\tk = 1\n",
        "repo/.git/config.worktree": b"[w]\n\tk = 1\n",
    }
    for change in (
        [],
        [["change", "write", "repo/.git/rinc", b"[r]\n\tk = 2\n"]],
        [["change", "write", "repo/.git/config.worktree", b"[w]\n\tk = 22\n"]],
        [
            [
                "change",
                "write",
                "repo/.git/config",
                REMOTE + b"[include]\n\tpath = rinc\n",
            ]
        ],
        [["change", "write", "home/.gitconfig", rule2]],
    ):
        yield Case(own, [["repo"], *change, ["repo"]])
    yield Case(own, [["repo"]], version=(2, 41))
    # config_unchanged over a capture: no change, the remote's config, a
    # machine file, an include, a legacy file added, HEAD moved under an
    # onbranch: include the capture asked and one no condition asks; the
    # files git names changing.
    for change in (
        [],
        [["change", "write", "repo/.git/config", REMOTE + b"\n"]],
        [["change", "write", g, rule2 + b"[include]\n\tpath = inc\n"]],
        [["change", "write", "home/inc", b"[x]\n\ty = 3\n"]],
        [
            [
                "change",
                "create",
                "repo/.git/remotes/up",
                b"URL: https://example.invalid/u\n",
            ]
        ],
        [["change", "write", "repo/.git/HEAD", b"ref: refs/heads/side\n"]],
        [["change", "create", "home/.config/git/config", rule]],
    ):
        yield Case(
            {**repo, "repo/.git/remotes/.keep": b""},
            [["capture"], *change, ["unchanged"]],
        )
    br = {
        **repo,
        "repo/.git/config": REMOTE + _cond("onbranch:main", "binc"),
        "repo/.git/binc": b"[b]\n\tk = 1\n",
    }
    yield Case(
        br,
        [
            ["capture"],
            ["change", "write", "repo/.git/HEAD", b"ref: refs/heads/side\n"],
            ["unchanged"],
        ],
    )
    yield Case(
        {**repo, "etc/glob": rule},
        [["capture"], ["unchanged"]],
        env={"GIT_CONFIG_GLOBAL": "@/etc/glob"},
    )
    # The files git names changing after the capture: a capture that named
    # some, and one that named none (its sources are not compared).
    yield Case(repo, [["capture"], ["env", "XDG_CONFIG_HOME", "@/xdg"], ["unchanged"]])
    yield Case(
        repo,
        [["capture"], ["env", "GIT_CONFIG_GLOBAL", None], ["unchanged"]],
        env={"GIT_CONFIG_GLOBAL": ""},
    )
    # The fresh window's edges, with a change of size inside it: reused
    # before its end, read again at it and before its start.
    for now in (0.999, 1.0, -0.1):
        yield Case(
            repo,
            [
                ["reads", 0.0],
                ["change", "write", g, rule2 + b"\n"],
                ["stamps", True, now],
            ],
        )
    # A format git dies reading: config.worktree not stamped.
    yield Case(
        {
            **own,
            "repo/.git/config": b"[core]\n\trepositoryformatversion = 2\n" + REMOTE,
        },
        [["repo"]],
    )
    # A relative global file, stated from the working directory.
    yield Case(
        {**repo, "rel/glob": rule},
        [
            ["stamps", False, 1.0],
            ["change", "write", "rel/glob", rule2],
            ["stamps", False, 2.0],
            ["capture"],
            ["unchanged"],
        ],
        env={"GIT_CONFIG_GLOBAL": "rel/glob"},
    )
    # A FIFO and an unreadable file as the global one.
    yield Case(
        repo,
        [["stamps", False, 1.0], ["stamps", False, 2.0]],
        fifos=("etc/x",),
        env={"GIT_CONFIG_GLOBAL": "@/etc/x"},
    )
    yield Case(repo, [["stamps", False, 1.0], ["stamps", False, 2.0]], modes={g: 0o000})

    # A capture that named no machine file still states its files: the
    # repository's config gaining a remote, an include changed, removed.
    bare = {
        **HEAD,
        "repo/.git/config": REMOTE + b"[include]\n\tpath = rinc\n",
        "repo/.git/rinc": b"[r]\n\tk = 1\n",
    }
    more = REMOTE + b"[include]\n\tpath = rinc\n" + b'[remote "up"]\n\turl = u\n'
    for change in (
        ["change", "write", "repo/.git/config", more],
        ["change", "write", "repo/.git/rinc", b"[r]\n\tk = 22\n"],
        ["change", "remove", "repo/.git/rinc"],
    ):
        yield Case(
            bare, [["capture"], change, ["unchanged"]], env={"GIT_CONFIG_GLOBAL": ""}
        )
    # Past the bound: 260 of the build's own files (none there). A read
    # evicted comes back last; one evicted while its fresh mark stays,
    # then reuse asked; read again later, its mark taken anew.
    many = 260
    yield Case(
        repo,
        [["stamps", False, 1.0], ["stamps", False, 2.0]],
        env={"GIT_CONFIG_NOSYSTEM": None},
        many=many,
    )
    evict: list[list[Any]] = [
        ["reads", 0.0],
        ["env", "GIT_CONFIG_NOSYSTEM", None],
        ["env", "GIT_CONFIG_GLOBAL", ""],
        ["stamps", False, 0.2],
        ["env", "GIT_CONFIG_NOSYSTEM", "1"],
        ["env", "GIT_CONFIG_GLOBAL", None],
    ]
    yield Case(repo, [*evict, ["stamps", True, 0.5]], many=many)
    yield Case(repo, [*evict, ["reads", 5.0], ["stamps", True, 5.5]], many=many)
    # One file under three spellings: kept apart.
    yield Case(
        {**repo, "etc/g": rule},
        [
            ["stamps", False, 1.0],
            ["env", "GIT_CONFIG_GLOBAL", "@/etc//g"],
            ["stamps", False, 1.0],
            ["env", "GIT_CONFIG_GLOBAL", "@/etc/x/../g"],
            ["stamps", False, 1.0],
        ],
        dirs=("etc/x",),
        env={"GIT_CONFIG_GLOBAL": "@/etc/./g"},
    )
    # A mode changed and changed back: only the change time moved. The
    # mtime of a file no one may read or write.
    yield Case(
        repo,
        [
            ["stamps", False, 1.0],
            ["change", "mode", g, b"000"],
            ["change", "mode", g, b"644"],
            ["stamps", False, 2.0],
        ],
    )
    yield Case(
        repo,
        [
            ["stamps", False, 1.0],
            ["change", "mode", g, b"000"],
            ["change", "mtime", g],
            ["stamps", False, 2.0],
        ],
    )
    # One file read directly, for two git directories in turn, and the
    # machine's stamps alternating between them.
    gd = {
        **repo,
        "etc/g": rule + _cond("gitdir:**/w/", "gw"),
        "etc/gw": b"[w]\n\tk = 1\n",
    }
    yield Case(
        gd,
        [
            ["read", "@/etc/g"],
            ["gitdir", "repo/.git/worktrees/w"],
            ["read", "@/etc/g"],
            ["stamps", False, 1.0],
            ["gitdir", "repo/.git"],
            ["stamps", False, 1.0],
            ["change", "write", "etc/gw", b"[w]\n\tk = 2\n"],
            ["gitdir", "repo/.git/worktrees/w"],
            ["read", "@/etc/g"],
            ["gitdir", "repo/.git"],
            ["read", "@/etc/g"],
        ],
        dirs=("repo/.git/worktrees/w",),
        env={"GIT_CONFIG_GLOBAL": "@/etc/g"},
    )


def _file() -> bytes:
    refuse_root()
    cases = list(_cases())
    lines: list[dict[str, Any]] = []
    with _sandbox.world() as world:
        base = world.root / "location-stamped"
        for i, case in enumerate(cases):
            root = base / str(i)
            root.mkdir(parents=True)
            os.chmod(root, 0o755)
            answer = _answer(case, root)
            shutil.rmtree(root)
            lines.append({**case.record(), "r": answer})
    header = {"fixture": "location_stamped", "cases": len(cases)}
    return jsonl([header, *lines])


def generate() -> dict[str, bytes]:
    return {"location_stamped.jsonl": _file()}
