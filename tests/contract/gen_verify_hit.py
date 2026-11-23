"""The real per-hit resolver over scripted Git reads and one walked map per pass.

Only simple relative cwd and toplevel spellings enter Path, so all hosts emit
the same file. The supplied anchors are already projected by the caller.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bettermemory import git_read_budget, origin_history, verify
from bettermemory.origin_history import HistoryUnread, ReachableWalk, Walked

from ._common import jsonl
from .gen_verify_count import _Calls, _patched, _script
from .gen_verify_weak import A, B, D, L30, LIT, P, _claims

SINCE = "2024-02-03T11:12:13+00:00"
EQUAL = "2024-02-03T06:12:13-05:00"
LATER = "2024-02-03T11:12:13.000001+00:00"
LOWER = (
    "resolve_repo_pathspecs",
    "commit_author_sha_pairs_touching_pathspecs",
    "paths_in_history",
    "too_large",
    "commit_patch_stream",
    "local_patch_window",
    "literal_floor",
    "spelled",
)


def _observe(step: dict[str, Any], saved: Walked) -> dict[str, Any]:
    calls = _Calls(step.get("cut"))
    lower = step.get("git", {})
    cwd = Path(step.get("cwd", "subdir"))
    top = None if step.get("top") is None else Path(step["top"])

    def walk(
        cwd: Path, anchor: str, *, toplevel: Path | None, head: str, walked: Walked
    ) -> ReachableWalk | None:
        assert walked is saved
        key = (str(cwd), head, anchor)
        calls.append(
            [
                "walk",
                str(cwd),
                anchor,
                str(toplevel) if toplevel is not None else None,
                head,
                len(walked),
                key in walked,
            ]
        )
        if step.get("reuse") and key in walked:
            kept = walked[key]
        elif step.get("walk") is not None:
            commits, touched, listed, timed_out = step["walk"]
            kept = ReachableWalk(
                anchor,
                head,
                tuple(commits),
                {p: frozenset(v) for p, v in touched},
                listed,
                timed_out,
            )
        elif step.get("why") is not None:
            kept = HistoryUnread(step["why"])
        else:
            kept = False
        walked[key] = kept
        if "nested_note" in step:
            origin_history.note_unwalked(HistoryUnread(step["nested_note"]))
        if isinstance(kept, HistoryUnread):
            origin_history.note_unwalked(kept)
        return kept if isinstance(kept, ReachableWalk) else None

    def stamps() -> list[datetime] | HistoryUnread | None:
        calls.append(["dates"])
        raw = step.get("dates")
        if isinstance(raw, str):
            return HistoryUnread(raw)
        return None if raw is None else [datetime.fromisoformat(x) for x in raw]

    def traced(fn: Callable[..., Any]) -> Callable[..., Any]:
        def invoke(cwd: Path, *args: Any, **kwargs: Any) -> Any:
            before = len(calls)
            try:
                return fn(cwd, *args, **kwargs)
            finally:
                if len(calls) > before:
                    calls[before].insert(1, str(cwd))

        return invoke

    since = datetime.fromisoformat(step.get("since", SINCE))
    with _script(lower, calls), patch.object(verify, "commits_since_anchor", walk):
        effects = {name: traced(getattr(verify, name)) for name in LOWER}
        with _patched(verify, effects):
            try:
                result = verify.resolve_hit_drift(
                    cwd=cwd,
                    toplevel=top,
                    head=step.get("head"),
                    timestamps=stamps,
                    anchors=tuple(step.get("anchors", ["m.py"])),
                    claims=_claims(step.get("claims", [])),
                    since=since,
                    verified_head=step.get("anchor_head"),
                    walked=saved,
                )
            except (TypeError, git_read_budget.ReadDeadline) as exc:
                value: Any = ["raise", type(exc).__name__]
            else:
                if isinstance(result, HistoryUnread):
                    value = ["unread", result.value]
                elif result is None:
                    value = None
                else:
                    value = [
                        result.count,
                        result.basis,
                        result.claims_checked,
                        list(result.claims_drifted),
                    ]
    return {
        "calls": list(calls),
        "result": value,
        "walked": [list(key) for key in saved],
        "unwalked": origin_history.unwalked(),
    }


def _pass(steps: list[dict[str, Any]]) -> dict[str, Any]:
    saved: Walked = {}
    with patch.object(origin_history, "_UNWALKED", origin_history._Unwalked()):
        expected = [_observe(step, saved) for step in steps]
    return {"steps": steps, "expected": expected}


def generate() -> dict[str, bytes]:
    heads: dict[str, Any] = {"head": D, "anchor_head": A}
    walked: list[Any] = [[A, B], [["m.py", [B]]], True, False]
    reachable = {**heads, "walk": walked, "top": "repo", "git": {"hist": True}}
    dated = {"dates": [EQUAL, LATER], "git": {"ts": [EQUAL, LATER]}}
    governed = {
        **dated,
        "claims": [L30, P],
        "git": {"pairs": [[[LATER, B]]] * 4, "stream": LIT, "hist": True},
    }
    quiescent = {"dates": [EQUAL], "git": {"rt": "repo", "hist": True}}
    cases = [
        _pass(
            [
                {**reachable, "nested_note": "timeout"},
                {**heads, **dated},
            ]
        ),
        _pass(
            [
                reachable,
                {**heads, "reuse": True, "top": "repo"},
                {**reachable, "cwd": "other"},
            ]
        ),
        _pass([{**heads, "why": "timeout"}, {**heads, "reuse": True}, dated]),
        _pass([{**heads, "why": "error"}, {**heads, **dated}]),
        _pass(
            [
                {**heads, "head": "", "anchor_head": "", **dated},
                {"head": "", **dated},
                {"anchor_head": "", **dated},
            ]
        ),
        _pass([{**heads, "walk": [[], [], True, False], "git": {"hist": True}}]),
        _pass(
            [
                {**reachable, "anchors": []},
                {**reachable, "git": {"rp": [], "hist": False}},
            ]
        ),
        _pass([{**dated, "git": {"ts": [EQUAL]}}, {**dated, "git": {"ts": []}}]),
        _pass(
            [
                quiescent,
                {**quiescent, "git": {"hist": False}},
                {**quiescent, "anchors": []},
            ]
        ),
        _pass(
            [{}, {"dates": [], "since": "2024-02-03"}, {**dated, "since": "2024-02-03"}]
        ),
        _pass(
            [
                governed,
                {**reachable, "claims": [L30], "git": {"stream": LIT, "hist": True}},
            ]
        ),
        _pass([{**dated, "claims": [["path", "a\nb", "", ""]]}]),
    ]
    for reason in HistoryUnread:
        cases.append(
            _pass(
                [
                    {"dates": reason.value, "git": {"rt": "repo"}},
                    {"dates": reason.value, "git": {"rp": []}},
                ]
            )
        )
    for prototype in (
        reachable,
        governed,
        quiescent,
        {**heads, **dated},
        {**heads, "why": "error", "git": {"rt": "repo"}},
    ):
        observed = _pass([prototype])["expected"][0]
        for cut in range(1, len(observed["calls"]) + 1):
            cases.append(_pass([{**prototype, "cut": cut}]))
    return {"verify_hit.jsonl": jsonl([{"passes": len(cases)}, *cases])}
