"""The row half of a real _DriftPass: `lookup`, `_key`, `unstored`, `store`
and the per-hit memo they read and merge into.

Every effect the pass reaches is scripted: the location and history reads
(as gen_response_history scripts them), the admission, the head's
signature, the three file signatures and `files_held`, the git call
tallies, the monotonic clock and `resolve_hit_drift` itself. The anchors,
the claims and the commit paths are the reference's own.

A case is a script over one memo (`_DRIFT_MEMO_CAP` patched to ``cap``)
and the passes it opens. The scripted state ``st`` answers each effect;
a step changes it, opens a pass, looks a row up, stores, or observes:

- ``["open"]`` / ``["open", cwd, at_home]``: a new pass;
- ``["set", name, value]``: a scripted answer changes;
- ``["lookup", opts]``: one row (``opts`` holds the row's fields, the
  arguments and ``res``, what the scripted resolution does);
- ``["store"]``;
- ``["obs"]``: the pass's kept maps, the memo's keys in order, and the
  size of the pass's walked map.

Signatures are tokens: ``("A", n)`` for the repository's attribute files,
``("C", n)`` for a chain, ``("W", n)`` for the walk files, ``("S", t)``
for the head's signature (``"?"`` is an undetermined one, equal to none).
"""

from __future__ import annotations

import types
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from unittest.mock import patch

from bettermemory import _response, git_read_budget, githead
from bettermemory.origin_history import HistoryUnread
from bettermemory.verify import ResolvedCommitDrift

from ._common import jsonl

ROOT = PurePosixPath("/r")
SINCE = "2024-01-02T03:04:05+00:00"
OUTSIDE = ("outside", True)

_STATE: dict[str, Any] = {
    "files": "H",
    "git": "H",
    "moved": False,
    "head": "H",
    "dates": [2, 3],
    "sig": "s1",
    "attrs": 1,
    "chains": {},
    "walk": 1,
    "held": True,
    "deny": [],
    "now": 100.0,
    "raise": "",
    "error": "RuntimeError",
}


def _value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, HistoryUnread):
        return value.value
    return [value.count, value.basis]


def _drift(spec: Any) -> Any:
    if spec is None:
        return None
    if isinstance(spec, str):
        return HistoryUnread(spec)
    return ResolvedCommitDrift(count=spec)


def _key(key: tuple[Any, ...]) -> list[Any]:
    attributes = key[7]
    return [
        key[0],
        key[1],
        key[2],
        list(key[3]),
        list(key[4]),
        key[5].isoformat(),
        key[6],
        None if attributes is None else [attributes[0][1], attributes[1][1]],
        key[8][1],
    ]


def _record(opts: dict[str, Any]) -> Any:
    return types.SimpleNamespace(
        id=opts.get("id", "m1"),
        origin=types.SimpleNamespace(repo="r") if opts.get("repo", True) else None,
        body=opts.get("body", ""),
        verified_paths=opts.get("paths", ["src/a.py"]),
        verified_absent_paths=[],
        verified_path_targets=None,
        verified_absent_path_targets=None,
        claims=opts.get("claims", []),
        verified_head=opts.get("vh"),
    )


def _case(
    label: str,
    steps: list[Any],
    *,
    cap: int = 5000,
    cwd: str = "/r/s",
    at_home: bool = False,
    **state: Any,
) -> dict[str, Any]:
    st: dict[str, Any] = {**_STATE, **state}
    initial = dict(st)
    calls: list[Any] = []
    results: list[Any] = []
    counts = {"failed": 0, "timed_out": 0}
    res: dict[str, Any] = {}
    reads = 0

    def effect(name: str, *args: Any) -> None:
        root = git_read_budget._ROOT.get()
        place = "o" if root == OUTSIDE else [root[0], root[1]] if root else None
        active = git_read_budget._ACTIVE.get() is not None
        calls.append([name, place, active, *args])
        if st["raise"] == name:
            st["raise"] = ""
            if st["error"] == "ReadDeadline":
                raise git_read_budget.ReadDeadline
            raise RuntimeError(name)

    def pair(head: str | None) -> Any:
        return None if head is None else (ROOT, head)

    def files(path: Any) -> Any:
        effect("files", str(path))
        return pair(st["files"])

    def git(path: Any) -> Any:
        effect("git", str(path))
        return pair(st["git"])

    def moved() -> bool:
        effect("moved")
        return bool(st["moved"])

    def head(path: Any) -> Any:
        effect("head", str(path))
        return st["head"]

    def dates(path: Any, *, located: Any) -> Any:
        nonlocal reads
        effect("dates", str(path), None if located is None else located[1])
        reads += 1
        answer = st["dates"]
        if answer is None:
            return None
        if isinstance(answer, str):
            return HistoryUnread(answer)
        return [datetime(2020, 1, d) for d in answer]

    def note(why: HistoryUnread) -> None:
        effect("note", why.value)

    def signature(path: Any, *, owner: bool) -> Any:
        effect("sig", str(path), owner)
        return object() if st["sig"] == "?" else ("S", st["sig"])

    def hold(directories: Any, name: str, value: int) -> None:
        if directories is not None:
            directories.setdefault(name, (len(name), value))

    def attributes(path: Any, root: Any, *, directories: Any = None) -> Any:
        effect("attrs", str(path), str(root))
        token = st["attrs"]
        if token is not None:
            hold(directories, f"{root}/.git/info", token)
        return None if token is None else ("A", token)

    def chain(root: Any, specs: Any, *, directories: Any = None) -> Any:
        effect("chain", str(root), list(specs))
        token = st["chains"].get("|".join(specs), 1)
        if token is not None:
            hold(directories, f"{root}/{specs[0]}", token)
        return None if token is None else ("C", token)

    def walk(root: Any, *, directories: Any = None) -> Any:
        effect("walk", str(root))
        token = st["walk"]
        if token is not None:
            hold(directories, str(root), token)
        return None if token is None else ("W", token)

    def held(
        path: Any, root: Any, repo: Any, chains: Any, walk_files: Any, dirs: Any
    ) -> bool:
        effect(
            "held",
            str(path),
            str(root),
            None if repo is None else repo[1],
            [[list(s), None if c is None else c[1]] for s, c in chains.items()],
            None if walk_files is None else walk_files[1],
            [[k, *v] for k, v in sorted(dirs.items())],
        )
        return bool(st["held"])

    def resolve(
        *,
        cwd: Any,
        toplevel: Any,
        head: Any,
        timestamps: Any,
        anchors: Any,
        claims: Any,
        since: datetime,
        verified_head: Any,
        walked: Any,
    ) -> Any:
        calls.append(
            [
                "resolve",
                str(cwd),
                None if toplevel is None else str(toplevel),
                head,
                list(anchors),
                [c.rel_path for c in claims],
                since.isoformat(),
                verified_head,
                len(walked),
            ]
        )
        if res.get("ts"):
            timestamps()
        if res.get("walk"):
            walked[(str(toplevel), verified_head or "", head or "")] = True
        counts["failed"] += res.get("fail", 0) + res.get("tout", 0)
        counts["timed_out"] += res.get("tout", 0)
        if res.get("raise") == "ReadDeadline":
            raise git_read_budget.ReadDeadline
        if res.get("raise"):
            raise RuntimeError("resolve")
        return _drift(res.get("v", 1))

    class Admission:
        def drift_counts(self, record_id: str) -> bool:
            # The crate's DriftScope cannot fail and sees no budget scope.
            calls.append(["admit", record_id])
            return record_id not in st["deny"]

    clock = types.SimpleNamespace(monotonic=lambda: st["now"])
    outer = git_read_budget._ROOT.set(OUTSIDE)
    active = git_read_budget._ACTIVE.set(None)
    owner = git_read_budget._OWNER.set(None)
    p: Any = None
    try:
        with (
            patch.multiple(
                _response,
                toplevel_and_head_from_files=files,
                repo_toplevel_and_head=git,
                located_head=head,
                commit_author_timestamps=dates,
                note_unread=note,
                attribute_files_signature=attributes,
                gitattributes_signature=chain,
                walk_files_signature=walk,
                files_held=held,
                resolve_hit_drift=resolve,
                failed_git_calls=lambda: counts["failed"],
                timed_out_calls=lambda: counts["timed_out"],
                time=clock,
                _DRIFT_MEMO_CAP=cap,
            ),
            patch.object(githead, "discovery_moved", moved),
            patch.object(githead, "signature", signature),
        ):
            _response._clear_drift_memo()
            for step in steps:
                kind = step[0]
                try:
                    if kind == "open":
                        where = step[1] if len(step) > 1 else cwd
                        home = step[2] if len(step) > 2 else at_home
                        p = _response._DriftPass(
                            PurePosixPath(where),  # type: ignore[arg-type]
                            "",
                            Admission(),  # type: ignore[arg-type]
                            at_home=home,
                        )
                        value: Any = None
                    elif kind == "set":
                        st[step[1]] = step[2]
                        value = None
                    elif kind == "lookup":
                        opts = step[1]
                        res.clear()
                        res.update(opts.get("res", {}))
                        since = opts.get("since", SINCE)
                        anchors = opts.get("anchors")
                        found, drift = p.lookup(
                            _record(opts),
                            None if since is None else datetime.fromisoformat(since),
                            resolve=opts.get("resolve", True),
                            anchor_paths=None if anchors is None else tuple(anchors),
                        )
                        value = [found, _value(drift)]
                    elif kind == "store":
                        value = p.store()
                    elif kind == "obs":
                        value = [
                            [[_key(k), _value(v)] for k, v in p._resolved.items()],
                            [[_key(k), _value(v)] for k, v in p._timed_out.items()],
                            [_key(k) for k in _response._DRIFT_MEMO],
                            [_key(k) for k in _response._DRIFT_TIMED_OUT],
                            len(p._walked),
                        ]
                    else:
                        raise ValueError(kind)
                    answer = ["return", value]
                except (git_read_budget.ReadDeadline, RuntimeError) as exc:
                    answer = ["raise", type(exc).__name__]
                assert git_read_budget._ROOT.get() == OUTSIDE
                assert git_read_budget._ACTIVE.get() is None
                unstored = p.unstored if p is not None else 0
                results.append([answer, len(calls), unstored])
            _response._clear_drift_memo()
    finally:
        git_read_budget._OWNER.reset(owner)
        git_read_budget._ACTIVE.reset(active)
        git_read_budget._ROOT.reset(outer)
    return dict(
        label=label,
        cap=cap,
        cwd=cwd,
        at_home=at_home,
        state=initial,
        steps=steps,
        calls=calls,
        results=results,
    )


def _lk(**opts: Any) -> list[Any]:
    return ["lookup", opts]


OPEN = ["open"]
OBS = ["obs"]
STORE = ["store"]
GOVERNED = ["src/a.py"]


def _early() -> list[dict[str, Any]]:
    return [
        _case(
            "early answers",
            [
                OPEN,
                _lk(since=None),
                _lk(repo=False),
                _lk(id="denied"),
                _lk(paths=[]),
                _lk(paths=[], claims=["", "  "]),
                _lk(anchors=[], claims=[]),
                _lk(anchors=[], claims=["src/b.py"]),
                _lk(paths=[], anchors=["x/y.py"]),
                OBS,
            ],
            deny=["denied"],
        ),
        _case(
            "at home without a remote",
            [OPEN, _lk(repo=False), OBS, STORE, ["open"], _lk(repo=False)],
            at_home=True,
        ),
        _case(
            "anchors from the body and the paths",
            [
                OPEN,
                _lk(body="see src/x.py:3 and docs/y.md", paths=["src/a.py"]),
                _lk(body="see src/x.py:3", paths=[]),
                _lk(body="", paths=["/abs/p.py", "src/a.py", "src/a.py"]),
                OBS,
            ],
        ),
    ]


def _resolution() -> list[dict[str, Any]]:
    return [
        _case(
            "resolve false",
            [
                OPEN,
                _lk(resolve=False),
                _lk(),
                _lk(resolve=False),
                OBS,
                STORE,
                OPEN,
                _lk(resolve=False),
                _lk(resolve=False, vh="V"),
            ],
        ),
        _case(
            "resolve false before git keyed the rows",
            [OPEN, _lk(resolve=False), _lk(), _lk(resolve=False), OBS],
            files=None,
        ),
        _case(
            "memo and pass maps",
            [
                OPEN,
                _lk(res={"v": 4}),
                _lk(res={"v": 9}),
                _lk(res={"v": None}, vh="V"),
                _lk(res={"v": "timeout", "tout": 1}, vh="W"),
                _lk(res={"v": 9}, vh="W"),
                OBS,
                STORE,
                OBS,
                OPEN,
                _lk(res={"v": 9}),
                _lk(res={"v": 9}, vh="V"),
                _lk(res={"v": 9}, vh="W"),
                OBS,
                STORE,
            ],
        ),
        _case(
            "call budget and deadlines",
            [
                OPEN,
                _lk(res={"v": "call_budget"}),
                _lk(res={"raise": "ReadDeadline"}),
                _lk(res={"raise": "RuntimeError"}),
                _lk(res={"v": 2}),
                OBS,
            ],
        ),
        _case(
            "deadline in the key's git",
            [
                ["set", "raise", "git"],
                ["set", "error", "ReadDeadline"],
                OPEN,
                _lk(),
                _lk(),
                OBS,
            ],
            files=None,
        ),
        _case(
            "failures and timeouts",
            [
                OPEN,
                _lk(res={"fail": 1, "v": 1}),
                _lk(res={"tout": 2, "v": "timeout"}, vh="A"),
                _lk(res={"fail": 1, "tout": 1, "v": 1}, vh="B"),
                _lk(res={"v": 3}, vh="B"),
                _lk(res={"v": 5}),
                OBS,
                STORE,
                OBS,
                OPEN,
                ["set", "now", 159.5],
                _lk(res={"v": 7}, vh="A"),
                ["set", "now", 160.0],
                _lk(res={"v": 7}, vh="A"),
                OBS,
                ["set", "now", 99.0],
                _lk(res={"v": 8, "tout": 1}, vh="A"),
                OBS,
            ],
        ),
        _case(
            "timestamps and the walked map",
            [
                OPEN,
                _lk(res={"ts": True, "walk": True, "v": 1}, vh="A"),
                _lk(res={"ts": True, "walk": True, "v": 1}, vh="B"),
                _lk(res={"ts": True, "v": 1}, vh="C"),
                OBS,
            ],
            dates="timeout",
        ),
    ]


def _keys() -> list[dict[str, Any]]:
    return [
        _case(
            "a recall keeps the since the memo stored",
            [
                OPEN,
                _lk(),
                STORE,
                _lk(since="2024-01-02T04:04:05+01:00"),
                OBS,
                OPEN,
                _lk(since="2024-01-02T05:04:05+02:00"),
                OBS,
            ],
        ),
        _case(
            "each key part",
            [
                OPEN,
                _lk(),
                _lk(anchors=["src/b.py"]),
                _lk(claims=["  "]),
                _lk(since="2024-01-02T03:04:05"),
                _lk(since="2024-01-02T04:04:05+01:00"),
                _lk(since="2024-01-02T03:04:06+00:00"),
                _lk(vh="V"),
                _lk(vh=""),
                _lk(claims=GOVERNED),
                _lk(claims=["src/b.py"]),
                _lk(claims=["src/b.py", "src/a.py"]),
                OBS,
                STORE,
                ["open", "/r/t"],
                _lk(),
                ["set", "files", "G"],
                ["open"],
                _lk(),
                ["set", "files", "H"],
                ["set", "walk", 2],
                ["open"],
                _lk(),
                ["set", "attrs", 2],
                _lk(claims=GOVERNED),
                ["open"],
                _lk(claims=GOVERNED),
                OBS,
            ],
        ),
        _case(
            "governed claims read the attribute files",
            [
                OPEN,
                _lk(claims=GOVERNED),
                _lk(claims=GOVERNED, vh="V"),
                _lk(claims=["src/b.py"]),
                _lk(claims=["src/c.py"]),
                _lk(claims=["src/c.py"]),
                _lk(claims=["src/a.py::X = [1]"]),
                _lk(claims=["!src/gone.py"]),
                OBS,
                STORE,
            ],
            chains={"src/c.py": None},
        ),
        _case(
            "repository attribute files unreadable",
            [
                OPEN,
                _lk(claims=GOVERNED),
                _lk(claims=GOVERNED),
                _lk(),
                OBS,
                STORE,
            ],
            attrs=None,
        ),
        _case(
            "walk files unreadable",
            [OPEN, _lk(), _lk(), _lk(claims=GOVERNED), OBS, STORE],
            walk=None,
        ),
        _case(
            "keyed by git",
            [OPEN, _lk(), _lk(), OBS, STORE, OPEN, _lk()],
            files=None,
        ),
        _case(
            "git's head moved under discovery",
            [OPEN, _lk(), _lk(), OBS],
            files=None,
            moved=True,
        ),
        _case(
            "git's root reads another head",
            [OPEN, _lk(), _lk(), OBS],
            files=None,
            head="J",
        ),
        _case(
            "no repository",
            [OPEN, _lk(), _lk(), OBS, STORE],
            files=None,
            git=None,
        ),
        _case(
            "eviction at the cap",
            [
                OPEN,
                _lk(vh="A"),
                _lk(vh="B"),
                STORE,
                OPEN,
                _lk(vh="A"),
                _lk(vh="C", res={"tout": 1}),
                _lk(vh="D", res={"tout": 1}),
                _lk(vh="E", res={"tout": 1}),
                STORE,
                OBS,
                OPEN,
                _lk(vh="B"),
                _lk(vh="C"),
                OBS,
            ],
            cap=2,
        ),
    ]


def _stores() -> list[dict[str, Any]]:
    cases = [
        _case(
            "store holds twice",
            [
                OPEN,
                STORE,
                _lk(vh="A"),
                STORE,
                STORE,
                _lk(vh="B", claims=GOVERNED),
                STORE,
                OBS,
            ],
        ),
    ]
    owned = [
        ("files moved", "files", "G"),
        ("files gone", "files", None),
        ("signature moved", "sig", "s2"),
        ("signature undetermined", "sig", "?"),
        ("files not held", "held", False),
    ]
    for label, name, value in owned:
        cases.append(
            _case(
                f"store fails: {label}",
                [
                    OPEN,
                    _lk(vh="A", claims=GOVERNED),
                    ["set", name, value],
                    STORE,
                    OBS,
                    ["set", name, _STATE[name]],
                    STORE,
                    _lk(vh="B"),
                    STORE,
                    OBS,
                    OPEN,
                    _lk(vh="A"),
                    STORE,
                ],
            )
        )
    for label, name, value in (
        ("git's head moved", "head", "J"),
        ("git's signature moved", "sig", "s2"),
        ("git's files not held", "held", False),
    ):
        cases.append(
            _case(
                f"store fails: {label}",
                [
                    OPEN,
                    _lk(vh="A"),
                    ["set", name, value],
                    STORE,
                    _lk(vh="B"),
                    STORE,
                ],
                files=None,
            )
        )
    # Only the location reads may meet the deadline: the signatures and
    # files_held run no git.
    for name, error in (
        ("files", "RuntimeError"),
        ("files", "ReadDeadline"),
        ("sig", "RuntimeError"),
        ("held", "RuntimeError"),
    ):
        cases.append(
            _case(
                f"store raises in {name}, {error}",
                [
                    OPEN,
                    _lk(vh="A"),
                    ["set", "raise", name],
                    STORE,
                    STORE,
                    OBS,
                ],
                error=error,
            )
        )
    for name in ("sig", "attrs", "chain", "walk"):
        cases.append(
            _case(
                f"lookup raises in {name}",
                [
                    OPEN,
                    ["set", "raise", name],
                    _lk(claims=GOVERNED),
                    _lk(claims=GOVERNED),
                    OBS,
                    STORE,
                ],
            )
        )
    cases.append(
        _case(
            "signature undetermined from the start",
            [OPEN, _lk(), STORE, _lk(vh="B"), STORE],
            sig="?",
        )
    )
    return cases


def generate() -> dict[str, bytes]:
    cases = [*_early(), *_resolution(), *_keys(), *_stores()]
    return {"response_drift.jsonl": jsonl([{"count": len(cases)}, *cases])}
