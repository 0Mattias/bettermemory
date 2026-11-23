"""The location and lazy history reads of a real _DriftPass.

Only its six external effects are scripted. Paths use both pathlib flavours
on every host. Code-point arrays retain all spellings, including surrogates.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, cast
from unittest.mock import Mock, patch

from bettermemory import _response, git_read_budget, githead
from bettermemory.admission import DriftScope
from bettermemory.origin_history import HistoryUnread

from ._common import jsonl

_STEPS = [
    "owned",
    "key-off",
    "applies",
    "located",
    "key-off",
    "key-on",
    "timestamps",
    "history-a",
    "history-b",
    "applies",
    "timestamps",
    "timestamps",
    "key-on",
    "applies",
]


def _codes(s: str) -> list[int]:
    return list(map(ord, s))


def _pair(pair: tuple[PurePath, str] | None) -> Any:
    return None if pair is None else [_codes(str(pair[0])), pair[1]]


def _value(value: Any) -> Any:
    if isinstance(value, HistoryUnread):
        return ["unread", value.value]
    if isinstance(value, tuple):
        return _pair(value)
    if isinstance(value, list):
        return [d.day for d in value]
    return value


def _case(
    label: str,
    *,
    path: PurePath,
    root: PurePath,
    files: str | None = None,
    git: str | None = "A",
    moved: bool = False,
    head: str | None = "A",
    dates: tuple[Any, ...] = ([2, 3],),
    at_home: bool = False,
    stop: str = "",
    error: str = "",
    steps: list[str] = _STEPS,
) -> dict[str, Any]:
    calls: list[Any] = []
    results: list[Any] = []
    raised = False
    reads = 0

    def effect(name: str, *args: Any) -> None:
        nonlocal raised
        scope = git_read_budget._ROOT.get()
        assert scope is not None
        calls.append(
            [
                name,
                [_codes(scope[0]), scope[1]],
                git_read_budget._ACTIVE.get() is not None,
                *args,
            ]
        )
        if name == stop and not raised:
            raised = True
            if error == "ReadDeadline":
                raise git_read_budget.ReadDeadline
            raise RuntimeError("history fixture")

    def initial(cwd: PurePath) -> Any:
        effect("files", _codes(str(cwd)))
        return None if files is None else (root, files)

    def locate(cwd: PurePath) -> Any:
        effect("git", _codes(str(cwd)))
        return None if git is None else (root, git)

    def discovery() -> bool:
        effect("moved")
        return moved

    def current(p: PurePath) -> str | None:
        effect("head", _codes(str(p)))
        return head

    def history(cwd: PurePath, *, located: Any) -> Any:
        nonlocal reads
        effect("dates", _codes(str(cwd)), _pair(located))
        answer = dates[min(reads, len(dates) - 1)]
        reads += 1
        if answer is None:
            return None
        if isinstance(answer, str):
            return HistoryUnread(answer)
        return [datetime(2020, 1, d, tzinfo=timezone.utc) for d in answer]

    def note(why: HistoryUnread) -> None:
        effect("note", why.value)

    outer = git_read_budget._ROOT.set(("outside", True))
    active = git_read_budget._ACTIVE.set(None)
    owner = git_read_budget._OWNER.set(None)
    opened = ""
    try:
        with (
            patch.multiple(
                _response,
                toplevel_and_head_from_files=initial,
                repo_toplevel_and_head=locate,
                located_head=current,
                commit_author_timestamps=history,
                note_unread=note,
            ),
            patch.object(githead, "discovery_moved", discovery),
        ):
            try:
                p = _response._DriftPass(
                    cast(Path, path), "", cast(DriftScope, Mock()), at_home=at_home
                )
            except (git_read_budget.ReadDeadline, RuntimeError) as exc:
                opened = type(exc).__name__
            else:
                for step in steps:
                    try:
                        if step == "owned":
                            value: Any = p._owned
                        elif step == "located":
                            value = p.located()
                        elif step in ("key-off", "key-on"):
                            value = p._keyed_pair(step == "key-on")
                        elif step == "timestamps":
                            value = p.timestamps()
                        elif step == "applies":
                            value = p.history_applies()
                        elif step in ("history-a", "history-b"):
                            value = p._history_at(step[-1].upper())
                        else:
                            raise ValueError(step)
                        answer = ["return", _value(value)]
                    except (git_read_budget.ReadDeadline, RuntimeError) as exc:
                        answer = ["raise", type(exc).__name__]
                    assert git_read_budget._ROOT.get() == ("outside", True)
                    assert git_read_budget._ACTIVE.get() is None
                    results.append([answer, len(calls)])
    finally:
        git_read_budget._OWNER.reset(owner)
        git_read_budget._ACTIVE.reset(active)
        git_read_budget._ROOT.reset(outer)
    return dict(
        label=label,
        cwd=_codes(str(path)),
        root=_codes(str(root)),
        files=files,
        git=git,
        moved=moved,
        head=head,
        dates=dates,
        at_home=at_home,
        stop=stop,
        error=error,
        steps=steps,
        opened=opened,
        calls=calls,
        results=results,
    )


def generate() -> dict[str, bytes]:
    posix = PurePosixPath("//repo/under/../sub")
    root = PurePosixPath("//repo")
    cases = [
        _case("settled", path=posix, root=root, files="A"),
        _case("empty head", path=posix, root=root, files="", head="", dates=([],)),
        _case("fallback key", path=posix, root=root),
        _case("moved", path=posix, root=root, moved=True),
        _case("different head", path=posix, root=root, head="B"),
        _case("absent head", path=posix, root=root, head=None),
        _case(
            "retries absent dates",
            path=posix,
            root=root,
            git=None,
            dates=(None, None, []),
        ),
        _case("absent dates at a root", path=posix, root=root, dates=(None, [2])),
        _case(
            "off-POSIX spelling",
            path=PureWindowsPath("C:/é/sub/../tail"),
            root=PureWindowsPath("C:/é"),
            at_home=True,
        ),
        _case(
            "surrogate in a path",
            path=PurePosixPath("a/\udcff"),
            root=PurePosixPath("a/\ud800"),
        ),
        _case(
            "drive relative",
            path=PureWindowsPath("D:folder"),
            root=PureWindowsPath("D:"),
            files="A",
        ),
    ]
    for why in HistoryUnread:
        cases.append(_case(why.value, path=posix, root=root, dates=(why.value,)))
    for where in ("files", "git", "moved", "head", "dates", "note"):
        for error in ("ReadDeadline", "RuntimeError"):
            for first in ("located", "key-on", "timestamps", "applies"):
                cases.append(
                    _case(
                        f"{where} {error} starting {first}",
                        path=posix,
                        root=root,
                        stop=where,
                        error=error,
                        dates=("timeout",),
                        steps=[first, *_STEPS],
                    )
                )
    return {"response_history.jsonl": jsonl([{"count": len(cases)}, *cases])}
