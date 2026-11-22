"""Fixtures: the uncached capture of `location`
(``location_capture.jsonl``, ``location_capture_platform.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/location/capture.rs``.

Every call `_capture_uncached` makes outside the unit is served by a
scripted world, in both languages: the path's state, ``realpath``,
`object_key`, the host name, discovery, `kept_apart`, `_real_git`, the
stats, eviction and fetch, the token's read and write, the head, the ref
tips, the remotes, the manifest, the init stamps, `capture_ticks`, the
clocks, `project_file.find` and `above_start`, and the object reader. A
stat result is a label. Each row records the calls in order (the log)
and the capture's fields, so the order of the steps is pinned with the
answer. No row depends on the Python release or the host, except the
``mode_t`` rows of ``location_capture_platform``: `stat.S_ISDIR` refuses
a mode wider than the C ``mode_t``, 16 bits on macOS and Windows, 32 on
Linux.

A world is a dict; a key left out takes its default (`_DEFAULT`).
"""

from __future__ import annotations

import posixpath
import types
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

from bettermemory import location, location_git

from ._common import jsonl

_DEFAULT: dict[str, Any] = {
    "path_state": ["present", "cwd", ""],
    "realpath": "/w/r",
    "keys": {},
    "host": "h",
    "discover": None,
    "kept": False,
    "real_git": None,
    "stat_each": ["c", "g", "t"],
    "stats": {},
    "evicted": [],
    "fetched": [],
    "token": ["absent", None],
    "write": ["written", "TOK"],
    "head": [None, None],
    "tips": [],
    "remotes": {},
    "manifest": None,
    "stamps": [],
    "ticks": None,
    "files": [],
    "clock": [[100.0, 1000.0]],
    "above_start": None,
    "walk": {},
    "commit": None,
    "tree": None,
}


class _St:
    def __init__(self, name: str) -> None:
        self.name = name


class _Dies(Exception):
    pass


class _World:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.s = {**_DEFAULT, **spec}
        self.log: list[str] = []
        self.clocks = list(self.s["clock"])
        self._wall = 0.0

    def note(self, *parts: Any) -> None:
        self.log.append(" ".join("-" if p is None else str(p) for p in parts))

    # location's own names
    def path_state(self, cwd: str) -> Any:
        self.note("path_state", cwd)
        state, st, why = self.s["path_state"]
        return state, None if st is None else _St(st), why

    def view_path(self, cwd: str) -> str:
        self.note("view_path", cwd)
        return "v:" + cwd

    def realpath(self, cwd: str) -> str:
        self.note("realpath", cwd)
        r = self.s["realpath"]
        if isinstance(r, dict):
            raise (ValueError if r.get("value") else OSError)(r["raise"])
        return str(r)

    def object_key(self, path: str, st: _St | None = None) -> Any:
        self.note("object_key", path, None if st is None else st.name)
        k = self.s["keys"].get(path)
        return None if k is None else tuple(k)

    def hostname(self) -> Any:
        self.note("hostname")
        return self.s["host"]

    def _git(self, g: dict[str, Any]) -> location_git._Git:
        return location_git._Git(
            g["dotgit"], g["git_dir"], g["common_dir"], g["root"], g["kind"]
        )

    def discover(self, real: str) -> Any:
        self.note("discover", real)
        d = self.s["discover"]
        if d is None:
            return None
        if "raise" in d:
            cls = {
                "os": OSError,
                "dies": location_git.DiscoveryDies,
                "format": location_git.GitDies,
            }[d["raise"]]
            raise cls(d["text"])
        return self._git(d)

    def kept_apart(self, git: Any) -> bool:
        self.note("kept_apart", git.git_dir)
        return bool(self.s["kept"])

    def real_git(self, git: Any) -> Any:
        self.note("real_git", git.git_dir)
        r = self.s["real_git"]
        return git if r is None else self._git(r)

    def stat_each(self, paths: Any) -> Any:
        self.note("stat_each", *paths)
        return tuple(None if n is None else _St(n) for n in self.s["stat_each"])

    def stat(self, path: str) -> _St:
        self.note("stat", path)
        n = self.s["stats"].get(path)
        if n is None:
            raise OSError("no")
        return _St(n)

    def isfile(self, path: str) -> bool:
        self.note("isfile", path)
        return path in self.s["files"]

    def evicted(self, st: _St) -> bool:
        self.note("evicted", st.name)
        return st.name in self.s["evicted"]

    def fetch(self, path: str, budget: float) -> bool:
        self.note("fetch", path)
        return path in self.s["fetched"]

    def token_path(self, git_dir: str) -> str:
        self.note("token_path", git_dir)
        return git_dir + "/token"

    def token_read(self, git_dir: str) -> Any:
        self.note("token_read", git_dir)
        state, token = self.s["token"]
        return types.SimpleNamespace(state=state, token=token)

    def token_write(self, git_dir: str, *, policy: bool, bare: bool) -> Any:
        self.note("token_write", git_dir, policy, bare)
        status, token = self.s["write"]
        return types.SimpleNamespace(status=status, token=token)

    def resolve_head(self, git: Any) -> Any:
        self.note("resolve_head", git.git_dir)
        return tuple(self.s["head"])

    def ref_tips(self, git: Any) -> Any:
        self.note("ref_tips", git.common_dir)
        return tuple(self.s["tips"])

    def read_remotes(self, common: str, git_dir: str, key: Any) -> Any:
        self.note("read_remotes", common, git_dir)
        key("u")
        r = self.s["remotes"]
        return location_git.RemoteKeys(
            keys=tuple(r.get("keys", ())),
            aliases=tuple(r.get("aliases", ())),
            files=(),
            sources=tuple(r.get("sources", ())),
            declined=r.get("declined"),
        )

    def remote_key(self, url: str, base: str | None = None) -> str:
        self.note("remote_key", url, base)
        return "rk"

    def manifest(self, root: str) -> Any:
        self.note("manifest", root)
        m = self.s["manifest"]
        return None if m is None else (m[0], tuple((p, n) for p, n in m[1]))

    def init_stamps(self, git_dir: str, *, linked: bool) -> Any:
        self.note("init_stamps", git_dir, linked)
        return tuple(tuple(s) for s in self.s["stamps"])

    def capture_ticks(self, own: Any, key: Any, read: Any, stamps: Any) -> Any:
        names = ",".join("-" if r is None else r.name for r in read)
        self.note(
            "capture_ticks", None if own is None else own.name, key, names, len(stamps)
        )
        return self.s["ticks"]

    def clock(self) -> float:
        pair = self.clocks[0] if len(self.clocks) == 1 else self.clocks.pop(0)
        self.note("clock", pair[0], pair[1])
        self._wall = pair[1]
        return float(pair[0])

    def wall(self) -> float:
        return float(self._wall)

    # project_file
    def above_start(self, cwd: str, root: str) -> Any:
        self.note("above_start", cwd, root)
        a = self.s["above_start"]
        return None if a is None else a

    def find(self, cwd: str, root: str | None, *, above: Any = None) -> Any:
        self.note("find", cwd, root, above)
        w = self.s["walk"]
        made = [
            types.SimpleNamespace(
                id=f[0],
                name=f[1],
                directory=f[2],
                path=f[2] + "/bettermemory.toml",
                boundary=f[3],
            )
            for f in w.get("files", [])
        ]
        pick = {f.directory: f for f in made}
        return types.SimpleNamespace(
            files=tuple(made),
            errors=tuple(types.SimpleNamespace(line=e) for e in w.get("errors", [])),
            above="above:" + cwd if above is None else above,
            folder=pick.get(w.get("folder")),
            root=pick.get(w.get("root")),
        )

    # gitobjects
    def reader(self, common: str) -> Any:
        world = self

        class _Reader:
            def commit(self, sha: str) -> Any:
                world.note("commit", common, sha)
                t = world.s["commit"]
                return None if t is None else types.SimpleNamespace(tree=t)

            def tree(self, sha: str) -> Any:
                world.note("tree", common, sha)
                t = world.s["tree"]
                if t is None:
                    return None
                return tuple(
                    types.SimpleNamespace(name=n.encode(), mode=m) for n, m in t
                )

        return _Reader()


@contextmanager
def _patched(obj: Any, **values: Any) -> Iterator[None]:
    old = {k: getattr(obj, k) for k in values}
    try:
        for k, v in values.items():
            setattr(obj, k, v)
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


@contextmanager
def _world(w: _World, cap: int | None = None) -> Iterator[None]:
    fake_os = types.SimpleNamespace(
        stat=w.stat,
        path=types.SimpleNamespace(
            join=posixpath.join, isfile=w.isfile, realpath=w.realpath
        ),
    )
    with ExitStack() as stack:
        stack.enter_context(
            _patched(
                location,
                _path_state=w.path_state,
                view_path=w.view_path,
                object_key=w.object_key,
                _hostname=w.hostname,
                _discover=w.discover,
                kept_apart=w.kept_apart,
                _real_git=w.real_git,
                stat_each=w.stat_each,
                _evicted=w.evicted,
                _fetch=w.fetch,
                _resolve_head=w.resolve_head,
                _ref_tips=w.ref_tips,
                read_remotes=w.read_remotes,
                remote_key=w.remote_key,
                _manifest=w.manifest,
                _init_stamps=w.init_stamps,
                capture_ticks=w.capture_ticks,
                os=fake_os,
                time=types.SimpleNamespace(monotonic=w.clock, time=w.wall),
                checkout_token=types.SimpleNamespace(
                    read=w.token_read, write=w.token_write, token_path=w.token_path
                ),
                project_file=types.SimpleNamespace(
                    above_start=w.above_start, find=w.find
                ),
                gitobjects=types.SimpleNamespace(ObjectReader=w.reader),
                CACHE_CAP=location.CACHE_CAP if cap is None else cap,
            )
        )
        location_git._ABOVE.clear()
        try:
            yield
        finally:
            location_git._ABOVE.clear()


def _key(k: Any) -> Any:
    return None if k is None else list(k)


def _capture(c: Any) -> dict[str, Any]:
    return {
        "cwd": c.cwd,
        "state": c.state,
        "root": c.root,
        "kind": c.kind,
        "git_dir": c.git_dir,
        "common_dir": c.common_dir,
        "object_key": _key(c.object_key),
        "common_object_key": _key(c.common_object_key),
        "root_object_key": _key(c.root_object_key),
        "cwd_key": _key(c.cwd_key),
        "head": c.head,
        "branch": c.branch,
        "ref_tips": list(c.ref_tips),
        "remote_keys": list(c.remote_keys),
        "alias_keys": list(c.alias_keys),
        "config_sources": list(c.config_sources),
        "shallow": c.shallow,
        "token": c.token,
        "token_written": c.token_written,
        "token_status": c.token_status,
        "stamps": [list(s) for s in c.stamps],
        "ticks": c.ticks,
        "files": [
            [f.id, f.name, f.dir, f.path, f.boundary, _key(f.key)] for f in c.files
        ],
        "file_errors": list(c.file_errors),
        "file_tracked": c.file_tracked,
        "evicted": c.evicted,
        "detail": c.detail,
        "manifest": None
        if c.manifest is None
        else [c.manifest[0], [list(e) for e in c.manifest[1]]],
        "hostname": c.hostname,
        "kept_apart": c.kept_apart,
    }


def _run(spec: dict[str, Any], **opts: Any) -> dict[str, Any]:
    w = _World(spec)
    write_token = opts.get("write_token", True)
    manifest = opts.get("manifest", True)
    with _world(w):
        try:
            out: Any = _capture(
                location._capture_uncached(
                    "/w/c", write_token=write_token, budget=1.0, manifest=manifest
                )
            )
        except OverflowError as exc:
            out = {"raise": f"OverflowError: {exc}"}
    return {
        "kind": "capture",
        "world": spec,
        "write_token": write_token,
        "manifest": manifest,
        "log": w.log,
        "out": out,
    }


_GIT: dict[str, Any] = {
    "dotgit": "/w/r/.git",
    "git_dir": "/w/r/.git",
    "common_dir": "/w/r/.git",
    "root": "/w/r",
    "kind": "primary",
}
_REAL: dict[str, Any] = {
    **_GIT,
    "git_dir": "/R/g",
    "common_dir": "/R/c",
    "root": "/R/t",
}
_REPO: dict[str, Any] = {"discover": _GIT, "real_git": _REAL}
_KEYS: dict[str, Any] = {
    "/w/r": ["1", "2", "3"],
    "/R/c": ["1", "4", None],
    "/R/g": ["1", "5", "6"],
    "/R/t": ["1", "7", "8"],
    "/R/t/sub": ["1", "9", None],
}
_FULL: dict[str, Any] = {
    **_REPO,
    "keys": _KEYS,
    "stats": {"/R/g/HEAD": "head", "/R/c/config": "config"},
    "head": ["abc", "main"],
    "tips": ["t1", "t2"],
    "remotes": {"keys": ["k1"], "aliases": ["a1"], "sources": ["s1"]},
    "stamps": [["HEAD", 1, 2, 3, 4]],
    "ticks": True,
    "files": ["/R/c/shallow"],
    "walk": {
        "files": [["bmp_1", "p", "/R/t", False], ["bmp_2", "q", "/R/t/sub", True]],
        "errors": ["bad line"],
        "root": "/R/t",
    },
    "above_start": "/R",
    "commit": "tree1",
    "tree": [["README", 0o100644], ["bettermemory.toml", 0o100644]],
}


def _capture_rows() -> Iterator[dict[str, Any]]:
    # The cwd is not there, or cannot be resolved.
    yield _run({"path_state": ["gone", None, "No such file"]})
    yield _run({"path_state": ["unreachable", None, ""]})
    yield _run({"path_state": ["present", None, ""]})
    yield _run({"realpath": {"raise": "loop"}})
    yield _run({"realpath": {"raise": "embedded null byte", "value": True}})
    yield _run({"host": None, "path_state": ["gone", None, "x"]})
    # Discovery raises.
    for kind in ("os", "dies", "format"):
        yield _run({"discover": {"raise": kind, "text": "why"}, "keys": _KEYS})
    # Outside a repository: no file, a folder project, with and without
    # the manifest.
    yield _run({"keys": _KEYS})
    folder: dict[str, Any] = {
        "keys": _KEYS,
        "walk": {
            "files": [["bmp_1", "p", "/w/r", False]],
            "folder": "/w/r",
            "errors": ["e1", "e2"],
        },
        "manifest": ["r", [["a", 1], ["b", 22]]],
    }
    yield _run(folder)
    yield _run(folder, manifest=False)
    yield _run({**folder, "keys": {}})
    # A repository, every field.
    yield _run(_FULL)
    yield _run(_FULL, write_token=False)
    yield _run({**_FULL, "token": ["present", "OLD"]})
    for state in ("malformed", "unreadable"):
        yield _run({**_FULL, "token": [state, None]})
    for status, token in (
        ("exists", "OLD"),
        ("malformed", None),
        ("skipped:bare", None),
        ("skipped:foreign_owner", None),
        ("failed:readonly", None),
        ("failed:io", None),
    ):
        yield _run({**_FULL, "write": [status, token]})
    yield _run({**_FULL, "real_git": {**_REAL, "kind": "bare", "root": None}})
    yield _run({**_FULL, "real_git": {**_REAL, "kind": "linked"}})
    yield _run({**_FULL, "real_git": {**_REAL, "root": ""}})
    yield _run({**_FULL, "kept": True})
    yield _run({**_FULL, "stat_each": [None, None, None]})
    yield _run({**_FULL, "stat_each": ["c", None, "t"]})
    yield _run({**_FULL, "stats": {}})
    yield _run({**_FULL, "head": [None, None]})
    yield _run(
        {**_FULL, "head": [None, None], "tips": [], "manifest": ["t", [["x", 3]]]}
    )
    yield _run(
        {**_FULL, "head": [None, None], "tips": [], "manifest": ["t", []]},
        manifest=False,
    )
    yield _run(
        {
            **_FULL,
            "head": [None, None],
            "tips": [],
            "real_git": {**_REAL, "root": ""},
            "manifest": ["t", []],
        }
    )
    yield _run({**_FULL, "walk": {**_FULL["walk"], "root": None}})
    yield _run({**_FULL, "commit": None})
    yield _run({**_FULL, "tree": None})
    yield _run({**_FULL, "tree": []})
    yield _run({**_FULL, "tree": [["bettermemory.toml", 0o40000]]})
    yield _run(
        {
            **_FULL,
            "tree": [["bettermemory.toml", 0o40755], ["bettermemory.toml", 0o120000]],
        }
    )
    yield _run(
        {
            **_FULL,
            "tree": [["Bettermemory.toml", 0o100644], ["bettermemory.toml/", 0o100644]],
        }
    )
    yield _run({**_FULL, "remotes": {"keys": ["k"], "declined": "include"}})
    yield _run({**_FULL, "remotes": {"declined": ""}})
    yield _run({**_FULL, "ticks": None, "files": []})
    # Evicted HEAD or config: fetched, or not and the token read or not.
    ev: dict[str, Any] = {**_FULL, "evicted": ["head"]}
    yield _run({**ev, "fetched": ["/R/g/HEAD"]})
    yield _run(ev)
    yield _run(
        {
            **ev,
            "stats": {**ev["stats"], "/R/g/token": "tok"},
            "token": ["present", "OLD"],
        }
    )
    yield _run(
        {**ev, "stats": {**ev["stats"], "/R/g/token": "tok"}, "token": ["present", ""]}
    )
    yield _run(
        {**ev, "stats": {**ev["stats"], "/R/g/token": "tok"}, "token": ["absent", None]}
    )
    yield _run(
        {
            **ev,
            "stats": {**ev["stats"], "/R/g/token": "tok"},
            "evicted": ["head", "tok"],
        }
    )
    yield _run({**_FULL, "evicted": ["config"]})
    yield _run({**_FULL, "evicted": ["config"], "stats": {"/R/c/config": "config"}})


def _walk_rows() -> Iterator[dict[str, Any]]:
    """`_walk_files` called in turn on one cache: hits, stale entries on
    either clock, clocks gone backwards, eviction past the cap, a start
    set again keeping its place."""
    seqs: list[tuple[int, list[tuple[str, str | None, str | None, list[float]]]]] = [
        (4, [("/a/x", "/a", "/s1", [10, 10]), ("/a/y", "/a", "/s1", [20, 20])]),
        (4, [("/a/x", "/a", "/s1", [10, 10]), ("/a/y", "/a", "/s1", [610, 20])]),
        (4, [("/a/x", "/a", "/s1", [10, 10]), ("/a/y", "/a", "/s1", [20, 610])]),
        (4, [("/a/x", "/a", "/s1", [10, 10]), ("/a/y", "/a", "/s1", [609.9, 609.9])]),
        (4, [("/a/x", "/a", "/s1", [10, 10]), ("/a/y", "/a", "/s1", [5, 10])]),
        (4, [("/a/x", "/a", "/s1", [10, 10]), ("/a/y", "/a", "/s1", [10, 9])]),
        (4, [("/a/x", "/a", None, [10, 10]), ("/a/y", "/a", None, [10, 10])]),
        (4, [("/a/x", None, "/s1", [10, 10]), ("/a/y", None, "/s1", [10, 10])]),
        (
            2,
            [
                ("/a", "/a", "/s1", [1, 1]),
                ("/b", "/b", "/s2", [2, 2]),
                ("/a", "/a", "/s1", [700, 700]),
                ("/c", "/c", "/s3", [701, 701]),
                ("/a", "/a", "/s1", [702, 702]),
                ("/b", "/b", "/s2", [703, 703]),
            ],
        ),
        (
            1,
            [
                ("/a", "/a", "/s1", [1, 1]),
                ("/b", "/b", "/s2", [2, 2]),
                ("/a", "/a", "/s1", [3, 3]),
            ],
        ),
    ]
    for cap, calls in seqs:
        w = _World({"keys": {"/a": ["1", "1", None]}})
        out = []
        with _world(w, cap):
            for cwd, root, start, clock in calls:
                w.s["above_start"] = start
                w.s["walk"] = {
                    "files": [["bmp_9", "n", "/a", False], ["bmp_8", "m", "/z", True]],
                    "errors": ["e"],
                }
                w.clocks = [clock]
                walk, files, errors = location._walk_files(cwd, root)
                out.append(
                    {
                        "above": walk.above,
                        "files": [
                            [f.id, f.name, f.dir, f.path, f.boundary, _key(f.key)]
                            for f in files
                        ],
                        "errors": list(errors),
                        "cache": list(location_git._ABOVE),
                    }
                )
        yield {
            "kind": "walk",
            "cap": cap,
            "calls": [list(c) for c in calls],
            "log": w.log,
            "out": out,
        }


def _token_rows() -> Iterator[dict[str, Any]]:
    for state, token in (
        ("present", "T"),
        ("present", None),
        ("absent", None),
        ("malformed", None),
        ("unreadable", None),
        ("odd", "T"),
    ):
        for write_token in (True, False):
            for status, wtoken in (
                ("written", "W"),
                ("exists", "E"),
                ("malformed", None),
                ("failed:io", None),
                ("written", None),
            ):
                w = _World({"token": [state, token], "write": [status, wtoken]})
                git = location_git._Git(
                    None, "/g", "/g", None, "bare" if write_token else "primary"
                )
                with _world(w):
                    got = location._token_fields(git, write_token=write_token)
                yield {
                    "kind": "token",
                    "read": [state, token],
                    "write": [status, wtoken],
                    "write_token": write_token,
                    "bare": git.kind == "bare",
                    "log": w.log,
                    "out": list(got),
                }


def _file(name: str, rows: list[dict[str, Any]]) -> dict[str, bytes]:
    return {f"{name}.jsonl": jsonl([{"fixture": name, "cases": len(rows)}, *rows])}


def generate() -> dict[str, bytes]:
    rows = [*_capture_rows(), *_walk_rows(), *_token_rows()]
    return _file("location_capture", rows)


def generate_platform() -> dict[str, bytes]:
    """Modes wider than 16 bits at the project file's name: macOS's and
    Windows' ``mode_t`` refuse them, Linux's takes them."""
    rows = []
    for mode in (0o177777, 0o200000, 0o240000, 0o777777):
        rows.append(_run({**_FULL, "tree": [["bettermemory.toml", mode]]}))
        rows.append(
            _run({**_FULL, "tree": [["other", mode], ["bettermemory.toml", 0o100644]]})
        )
    return _file("location_capture_platform", rows)
