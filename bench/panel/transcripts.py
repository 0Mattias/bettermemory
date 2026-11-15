"""What bettermemory adds to a Claude Code session, read from the local
transcripts (``~/.claude/projects/**/*.jsonl``).

Each transcript line is one JSON entry. Three kinds are read:

- an ``assistant`` entry's ``tool_use`` blocks, for the calls made to
  bettermemory's tools (named ``mcp__bettermemory__<tool>``, or
  ``mcp__plugin_bettermemory_bettermemory__<tool>`` through the plugin);
- a ``user`` entry's ``tool_result`` blocks, for the characters each call
  returned into the context, and for every tool's results in the same
  sessions, which gives bettermemory's share;
- an ``attachment`` entry of type ``hook_success``, which Claude Code
  writes for each hook it ran: the command, the text it injected
  (``content``) and the wall time it measured (``durationMs``, from the
  spawn of the hook's shell to its exit).

Everything is counted in characters of the text Claude Code put in the
context; nothing here is a token count. A forked or resumed session copies
earlier lines into a new file, so calls are counted once by their
``tool_use`` id and hook runs once by their entry's ``uuid``.

Only aggregates leave this module: counts, sums and quantiles keyed by
tool name, hook word and status. No prompt, result text, path or id.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

#: The prefixes Claude Code gives bettermemory's tools: the server added by
#: ``bettermemory init`` and the one the plugin adds.
TOOL_PREFIXES = ("mcp__bettermemory__", "mcp__plugin_bettermemory_bettermemory__")

#: The hook words of ``bettermemory hook <word>``.
_HOOK_WORD = re.compile(r"\bbettermemory\s+hook\s+([a-z][a-z-]*)")

#: A hook the harness ran through uvx (the plugin's path) rather than an
#: installed ``bettermemory``.
_UVX = re.compile(r"\buvx\b")

#: Lines worth decoding: everything else in a transcript (requests, title
#: and state rows) carries no call, result or hook run.
_MARKERS = ('"tool_use"', '"tool_result"', '"hook_success"')

#: Status words a write or update response leads with, read from the
#: response's JSON; a response that is not JSON is counted as "text".
_STATUS = re.compile(r'"status"\s*:\s*"([a-z_]+)"')


def tool_of(name: str) -> str | None:
    """The bettermemory tool a ``tool_use`` name calls, or None."""
    for prefix in TOOL_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix) :]
    return None


def hook_of(command: str) -> tuple[str, str] | None:
    """``(word, launcher)`` for a bettermemory hook command, or None:
    the hook word and whether it ran through ``uvx`` or an installed
    ``bettermemory``."""
    match = _HOOK_WORD.search(command)
    if match is None:
        return None
    return match.group(1), "uvx" if _UVX.search(command) else "installed"


#: The hook word each event runs, for a run whose command Claude Code
#: recorded only as a placeholder ("command hook 1", the desktop app).
_WORD_OF_EVENT = {
    "UserPromptSubmit": "prompt",
    "SessionStart": "session-start",
    "Stop": "stop",
}


def injected_text(output: str) -> str:
    """The text a hook's output puts in the context: the output itself, or
    the ``additionalContext`` of a ``hookSpecificOutput`` answer."""
    if output.startswith("{") and "hookSpecificOutput" in output[:200]:
        try:
            parsed = json.loads(output)
        except ValueError:
            return output
        specific = (
            parsed.get("hookSpecificOutput") if isinstance(parsed, dict) else None
        )
        context = (
            specific.get("additionalContext") if isinstance(specific, dict) else None
        )
        return context if isinstance(context, str) else ""
    return output


def bettermemory_hook(command: str, event: str, text: str) -> tuple[str, str] | None:
    """``(word, launcher)`` for a run of a bettermemory hook, or None. The
    command names it when Claude Code recorded the command; otherwise its
    injected text does, which every bettermemory hook opens with its name
    (``launcher`` is then ``unknown``). A placeholder run that injected
    nothing cannot be told apart and is not counted as bettermemory's."""
    named = hook_of(command)
    if named is not None:
        return named
    if text.startswith("bettermemory"):
        word = _WORD_OF_EVENT.get(event.split(":")[0])
        if word is not None:
            return word, "unknown"
    return None


def result_chars(content: Any) -> int:
    """The characters a tool result put in the context: its text, or the
    text blocks of a block list. An image block counts nothing here."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return 0


def result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text") or ""
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def status_of(text: str) -> str:
    """The status a write-shaped response names first, or ``text``."""
    match = _STATUS.search(text[:4000])
    return match.group(1) if match else "text"


def quantiles(values: Iterable[float]) -> dict[str, Any]:
    """n, total, mean, p50, p90 and max, nearest rank; Nones when empty."""
    data = sorted(values)
    n = len(data)
    if n == 0:
        return {"n": 0, "total": 0, "mean": None, "p50": None, "p90": None, "max": None}

    def rank(q: float) -> float:
        return data[max(0, math.ceil(q * n) - 1)]

    total = sum(data)
    return {
        "n": n,
        "total": round(total, 1),
        "mean": round(total / n, 1),
        "p50": rank(0.5),
        "p90": rank(0.9),
        "max": data[-1],
    }


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class _Tally:
    files_read: int = 0
    files_skipped_by_mtime: int = 0
    lines_bad: int = 0
    # tool_use id -> (tool, session); a result is counted once its use is seen.
    uses: dict[str, tuple[str, str]] = field(default_factory=dict)
    results_seen: set[str] = field(default_factory=set)
    hook_runs_seen: set[str] = field(default_factory=set)
    tool_chars: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    tool_status: dict[str, Counter[str]] = field(
        default_factory=lambda: defaultdict(Counter)
    )
    write_chars: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(int))
    )
    hook_chars: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    hook_ms: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    hook_launchers: Counter[str] = field(default_factory=Counter)
    other_hook_ms: dict[str, list[float]] = field(
        default_factory=lambda: defaultdict(list)
    )
    # Per session: all tool-result characters, bettermemory's, and its hooks'.
    session_all: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    session_bm_tools: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    session_bm_hooks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    session_bm_calls: Counter[str] = field(default_factory=Counter)
    pending_results: dict[str, tuple[int, str, str]] = field(default_factory=dict)


def _files(root: Path, since: datetime | None) -> Iterator[tuple[Path, bool]]:
    """Every transcript under `root`, and whether it can hold an entry at
    or after `since` (a file last modified before it cannot)."""
    floor = since.timestamp() if since is not None else None
    for path in sorted(root.rglob("*.jsonl")):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        yield path, floor is None or mtime >= floor


def _in_window(
    entry: dict[str, Any], since: datetime | None, until: datetime | None
) -> bool:
    if since is None and until is None:
        return True
    ts = _parse_ts(entry.get("timestamp"))
    if ts is None:
        return False
    return (since is None or ts >= since) and (until is None or ts < until)


def _read_assistant(tally: _Tally, entry: dict[str, Any], session: str) -> None:
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        use_id = block.get("id")
        if not isinstance(use_id, str):
            continue
        tool = tool_of(str(block.get("name") or ""))
        tally.uses.setdefault(use_id, (tool or "", session))
        pending = tally.pending_results.pop(use_id, None)
        if pending is not None:
            _count_result(tally, use_id, *pending)


def _count_result(
    tally: _Tally, use_id: str, chars: int, status: str, session: str
) -> None:
    if use_id in tally.results_seen:
        return
    tally.results_seen.add(use_id)
    tool, use_session = tally.uses[use_id]
    session = use_session or session
    tally.session_all[session] += chars
    if not tool:
        return
    tally.tool_chars[tool].append(chars)
    tally.session_bm_tools[session] += chars
    tally.session_bm_calls[session] += 1
    if tool in ("memory_write", "memory_update"):
        tally.tool_status[tool][status] += 1
        tally.write_chars[tool][status] += chars


def _read_user(tally: _Tally, entry: dict[str, Any], session: str) -> None:
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        use_id = block.get("tool_use_id")
        if not isinstance(use_id, str) or use_id in tally.results_seen:
            continue
        body = block.get("content")
        chars = result_chars(body)
        status = status_of(result_text(body))
        if use_id in tally.uses:
            _count_result(tally, use_id, chars, status, session)
        else:
            # The use sits in an earlier file of a resumed session, or later
            # in this one; held until it is seen.
            tally.pending_results.setdefault(use_id, (chars, status, session))


def _read_hook(tally: _Tally, entry: dict[str, Any], session: str) -> None:
    attachment = entry.get("attachment") or {}
    if attachment.get("type") != "hook_success":
        return
    run_id = entry.get("uuid")
    if isinstance(run_id, str):
        if run_id in tally.hook_runs_seen:
            return
        tally.hook_runs_seen.add(run_id)
    command = str(attachment.get("command") or "")
    event = str(attachment.get("hookName") or attachment.get("hookEvent") or "")
    took = attachment.get("durationMs")
    content = attachment.get("content")
    # `content` is what Claude Code took from the run; a Stop hook's plain
    # stdout is not put in the context, and is not in `content`.
    text = injected_text(content if isinstance(content, str) else "")
    hook = bettermemory_hook(command, event, text)
    if hook is None:
        # Another program's hook, or a placeholder run that put nothing in
        # the context (bettermemory's Stop hook among them, unattributable).
        if isinstance(took, (int, float)):
            which = "no output" if not text else "other output"
            tally.other_hook_ms[f"{event.split(':')[0]} ({which})"].append(float(took))
        return
    word, launcher = hook
    tally.hook_launchers[launcher] += 1
    key = f"{word} ({event})" if event else word
    injected = len(text)
    tally.hook_chars[key].append(injected)
    tally.session_bm_hooks[session] += injected
    if isinstance(took, (int, float)):
        tally.hook_ms[key].append(float(took))


def read(
    root: Path, *, since: datetime | None = None, until: datetime | None = None
) -> dict[str, Any]:
    """The aggregate of every transcript under `root` in the window."""
    tally = _Tally()
    for path, may_hold in _files(root, since):
        if not may_hold:
            tally.files_skipped_by_mtime += 1
            continue
        tally.files_read += 1
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if not any(marker in line for marker in _MARKERS):
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    tally.lines_bad += 1
                    continue
                if not isinstance(entry, dict) or not _in_window(entry, since, until):
                    continue
                session = str(entry.get("sessionId") or path.stem)
                kind = entry.get("type")
                if kind == "assistant":
                    _read_assistant(tally, entry, session)
                elif kind == "user":
                    _read_user(tally, entry, session)
                elif kind == "attachment":
                    _read_hook(tally, entry, session)
    return _summary(tally, since, until)


def _summary(
    tally: _Tally, since: datetime | None, until: datetime | None
) -> dict[str, Any]:
    tools = {
        tool: quantiles(chars)
        for tool, chars in sorted(tally.tool_chars.items(), key=lambda kv: -sum(kv[1]))
    }
    writes: dict[str, Any] = {}
    for tool, statuses in sorted(tally.tool_status.items()):
        calls = sum(statuses.values())
        committed = statuses.get("committed", 0)
        chars = sum(tally.write_chars[tool].values())
        writes[tool] = {
            "calls": calls,
            "committed": committed,
            "calls_per_committed": round(calls / committed, 3) if committed else None,
            "chars_per_committed": round(chars / committed, 1) if committed else None,
            "statuses": dict(statuses.most_common()),
        }
    hooks = {
        key: {
            "injected_chars": quantiles(chars),
            "wall_ms": quantiles(tally.hook_ms[key]),
        }
        for key, chars in sorted(tally.hook_chars.items())
    }
    sessions = [
        s
        for s in tally.session_all
        if tally.session_bm_calls[s] or tally.session_bm_hooks[s]
    ]
    sessions += [s for s in tally.session_bm_hooks if s not in tally.session_all]
    sessions = sorted(set(sessions))
    all_chars = sum(tally.session_all.get(s, 0) for s in sessions)
    bm_tools = sum(tally.session_bm_tools.get(s, 0) for s in sessions)
    bm_hooks = sum(tally.session_bm_hooks.get(s, 0) for s in sessions)
    added = all_chars + bm_hooks
    return {
        "unit": "characters of context text; no figure here is a token count",
        "window": {
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
        },
        "files": {
            "read": tally.files_read,
            "skipped_older_than_window": tally.files_skipped_by_mtime,
            "bad_lines": tally.lines_bad,
        },
        "tool_responses": tools,
        "writes": writes,
        "hooks": hooks,
        "hook_launchers": dict(tally.hook_launchers),
        "other_hooks_wall_ms": {
            event: quantiles(ms) for event, ms in sorted(tally.other_hook_ms.items())
        },
        "sessions": {
            "with_bettermemory": len(sessions),
            "tool_calls_per_session": quantiles(
                tally.session_bm_calls.get(s, 0) for s in sessions
            ),
            "tool_result_chars_all_tools": all_chars,
            "bettermemory_tool_result_chars": bm_tools,
            "bettermemory_hook_chars": bm_hooks,
            "bettermemory_share": round((bm_tools + bm_hooks) / added, 4)
            if added
            else None,
            "bettermemory_chars_per_session": quantiles(
                tally.session_bm_tools.get(s, 0) + tally.session_bm_hooks.get(s, 0)
                for s in sessions
            ),
        },
    }


def default_root() -> Path:
    return Path(os.path.expanduser("~/.claude/projects"))


# ---------------------------------------------------------------------------
# Provider-reported usage per completed workflow
# ---------------------------------------------------------------------------

#: The usage fields an assistant message carries, as the provider reported
#: them, and the names the receipt gives them.
USAGE_FIELDS = (
    ("input_tokens", "input"),
    ("cache_read_input_tokens", "cache_read"),
    ("cache_creation_input_tokens", "cache_creation"),
    ("output_tokens", "output"),
)

#: The workflow kinds.
WORKFLOWS = ("write_committed", "search_then_show", "hook_pointer_opened")

USAGE_NOTE = (
    "Provider-reported usage (the `usage` of each assistant message, one "
    "figure per message id: the largest of each field over the entries that "
    "repeat it), summed over the assistant turns that carried one completed "
    "workflow, then p50 and p90 across workflows of a kind. A turn carries a "
    "workflow when it holds one of its tool calls: write_committed, the "
    "memory_write calls of a session from the first after the previous "
    "commit through the call whose response says committed; search_then_show, "
    "one memory_search and the memory_show calls of ids its response named, "
    "before the next memory_search, counted only with at least one show; "
    "hook_pointer_opened, the memory_show calls of ids a bettermemory hook "
    "injected, counted per injection with at least one show (the hook itself "
    "has no assistant turn). A show is credited to the newest search or "
    "injection that named its id. Sessions are read by lineage (a resumed or "
    "forked session joined to the one whose lines it repeats), in timestamp "
    "order. Included: each such turn's whole request, the full input context "
    "it was sent with (input, cache read, cache creation) and everything it "
    "generated (output, thinking and any other tool calls in the same turn). "
    "Not included: the turn after the last tool result, which reads it; "
    "turns between the workflow's calls that made no workflow call; hook "
    "injections' cost, which lands in the next turn's input. A turn that "
    "carries calls of two workflows is counted in both (`shared_turns` per "
    "kind), so figures of different kinds must not be added; "
    "`distinct_turns` sums each turn once. Usage cannot be attributed to one "
    "tool response: a figure is what the turns cost, not what bettermemory "
    "added to them."
)


def usage_of(message: dict[str, Any]) -> dict[str, int] | None:
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    out: dict[str, int] = {}
    for key, name in USAGE_FIELDS:
        value = usage.get(key)
        out[name] = int(value) if isinstance(value, (int, float)) else 0
    return out


@dataclass
class _Flow:
    kind: str
    messages: list[str] = field(default_factory=list)
    calls: int = 0

    def add(self, message: str) -> None:
        self.calls += 1
        if message not in self.messages:
            self.messages.append(message)


@dataclass
class _Entry:
    """One line the usage reading needs, reduced to what it reads."""

    order: tuple[str, int, int]
    session: str
    kind: str  # "turn", "result" or "hook"
    message_id: str = ""
    # turn: (use id, tool, shown id); result: (use id, status, text).
    items: list[tuple[str, str, str]] = field(default_factory=list)
    text: str = ""


@dataclass
class _Lineage:
    chain: _Flow | None = None
    # (sequence, text, flow) for searches and hook injections not yet closed.
    sources: list[tuple[int, str, _Flow]] = field(default_factory=list)
    seq: int = 0


@dataclass
class _UsageTally:
    usage: dict[str, dict[str, int]] = field(default_factory=dict)
    uses: dict[str, tuple[str, str]] = field(default_factory=dict)
    results_seen: set[str] = field(default_factory=set)
    hook_runs_seen: set[str] = field(default_factory=set)
    done: list[_Flow] = field(default_factory=list)
    open_at_end: Counter[str] = field(default_factory=Counter)
    searches_without_show: int = 0
    injections_without_show: int = 0


class _Sessions:
    """Union-find over session ids: two sessions that carry the same
    message, call or hook run are one lineage."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.first: dict[str, str] = {}

    def root(self, session: str) -> str:
        self.parent.setdefault(session, session)
        while self.parent[session] != session:
            self.parent[session] = self.parent[self.parent[session]]
            session = self.parent[session]
        return session

    def seen(self, key: str, session: str) -> None:
        other = self.first.setdefault(key, session)
        a, b = self.root(other), self.root(session)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def _collect_turn(
    tally: _UsageTally, entry: dict[str, Any], order: tuple[str, int, int], session: str
) -> _Entry | None:
    message = entry.get("message") or {}
    message_id = message.get("id")
    if not isinstance(message_id, str):
        return None
    usage = usage_of(message)
    if usage is not None:
        known = tally.usage.setdefault(message_id, usage)
        for name, value in usage.items():
            known[name] = max(known[name], value)
    items: list[tuple[str, str, str]] = []
    content = message.get("content")
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        use_id = block.get("id")
        tool = tool_of(str(block.get("name") or ""))
        if not isinstance(use_id, str) or tool is None:
            continue
        shown = (block.get("input") or {}).get("id") if tool == "memory_show" else ""
        items.append((use_id, tool, shown if isinstance(shown, str) else ""))
    return _Entry(order, session, "turn", message_id, items)


def _collect_result(
    entry: dict[str, Any], order: tuple[str, int, int], session: str
) -> _Entry | None:
    content = (entry.get("message") or {}).get("content")
    items: list[tuple[str, str, str]] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        use_id = block.get("tool_use_id")
        if not isinstance(use_id, str):
            continue
        text = result_text(block.get("content"))
        items.append((use_id, status_of(text), text))
    return _Entry(order, session, "result", items=items) if items else None


def _collect_hook(
    entry: dict[str, Any], order: tuple[str, int, int], session: str
) -> _Entry | None:
    attachment = entry.get("attachment") or {}
    if attachment.get("type") != "hook_success":
        return None
    content = attachment.get("content")
    text = injected_text(content if isinstance(content, str) else "")
    event = str(attachment.get("hookName") or attachment.get("hookEvent") or "")
    command = str(attachment.get("command") or "")
    if not text or bettermemory_hook(command, event, text) is None:
        return None
    run_id = entry.get("uuid")
    return _Entry(order, session, "hook", str(run_id or ""), text=text)


def _close_source(tally: _UsageTally, flow: _Flow) -> None:
    if flow.calls > (1 if flow.kind == "search_then_show" else 0):
        tally.done.append(flow)
    elif flow.kind == "search_then_show":
        tally.searches_without_show += 1
    else:
        tally.injections_without_show += 1


def _play_turn(tally: _UsageTally, state: _Lineage, entry: _Entry) -> None:
    fresh = [item for item in entry.items if item[0] not in tally.uses]
    for use_id, tool, _ in fresh:
        tally.uses[use_id] = (tool, entry.message_id)
    # Shows first: a show in the same message as a new search opens the
    # previous search's hit, which the new search must not close away.
    for _, tool, shown in fresh:
        if tool != "memory_show" or len(shown) < 6:
            continue
        for _, text, flow in sorted(state.sources, key=lambda s: -s[0]):
            if shown in text:
                flow.add(entry.message_id)
                break
    for _, tool, _ in fresh:
        if tool == "memory_write":
            if state.chain is None:
                state.chain = _Flow("write_committed")
            state.chain.add(entry.message_id)
    if any(tool == "memory_search" for _, tool, _ in fresh):
        for _, _, flow in state.sources:
            if flow.kind == "search_then_show":
                _close_source(tally, flow)
        state.sources = [s for s in state.sources if s[2].kind != "search_then_show"]


def _play_result(tally: _UsageTally, state: _Lineage, entry: _Entry) -> None:
    for use_id, status, text in entry.items:
        if use_id in tally.results_seen or use_id not in tally.uses:
            continue
        tally.results_seen.add(use_id)
        tool, message_id = tally.uses[use_id]
        if tool == "memory_write" and state.chain is not None:
            if status == "committed":
                tally.done.append(state.chain)
                state.chain = None
        elif tool == "memory_search":
            flow = _Flow("search_then_show")
            flow.add(message_id)
            state.seq += 1
            state.sources.append((state.seq, text, flow))


def _play_hook(tally: _UsageTally, state: _Lineage, entry: _Entry) -> None:
    if entry.message_id:
        if entry.message_id in tally.hook_runs_seen:
            return
        tally.hook_runs_seen.add(entry.message_id)
    state.seq += 1
    state.sources.append((state.seq, entry.text, _Flow("hook_pointer_opened")))


def _flow_usage(tally: _UsageTally, messages: Iterable[str]) -> dict[str, int]:
    total = {name: 0 for _, name in USAGE_FIELDS}
    for message_id in messages:
        for name, value in (tally.usage.get(message_id) or {}).items():
            total[name] += value
    return total


def _collect(
    root: Path, tally: _UsageTally, since: datetime | None, until: datetime | None
) -> tuple[list[_Entry], _Sessions]:
    entries: list[_Entry] = []
    sessions = _Sessions()
    markers = (*_MARKERS, '"usage"')
    for file_index, (path, may_hold) in enumerate(_files(root, since)):
        if not may_hold:
            continue
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line_index, line in enumerate(handle):
                if not any(marker in line for marker in markers):
                    continue
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(raw, dict) or not _in_window(raw, since, until):
                    continue
                session = str(raw.get("sessionId") or path.stem)
                sessions.root(session)
                order = (str(raw.get("timestamp") or ""), file_index, line_index)
                kind = raw.get("type")
                found: _Entry | None = None
                if kind == "assistant":
                    found = _collect_turn(tally, raw, order, session)
                    if found is not None:
                        sessions.seen("m:" + found.message_id, session)
                elif kind == "user":
                    found = _collect_result(raw, order, session)
                    for use_id, _, _ in found.items if found else []:
                        sessions.seen("u:" + use_id, session)
                elif kind == "attachment":
                    found = _collect_hook(raw, order, session)
                    if found is not None and found.message_id:
                        sessions.seen("h:" + found.message_id, session)
                if found is not None:
                    entries.append(found)
    return entries, sessions


def workflow_usage(
    root: Path, *, since: datetime | None = None, until: datetime | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Provider-reported usage per completed workflow (`USAGE_NOTE`): the
    aggregate for the receipt and one row per workflow (kind, turns, calls,
    usage; no id or text) for the private directory."""
    tally = _UsageTally()
    entries, sessions = _collect(root, tally, since, until)
    lineages: dict[str, list[_Entry]] = defaultdict(list)
    for entry in entries:
        lineages[sessions.root(entry.session)].append(entry)
    for lineage in lineages.values():
        state = _Lineage()
        for entry in sorted(lineage, key=lambda e: e.order):
            if entry.kind == "turn":
                _play_turn(tally, state, entry)
            elif entry.kind == "result":
                _play_result(tally, state, entry)
            else:
                _play_hook(tally, state, entry)
        if state.chain is not None:
            tally.open_at_end["write_committed"] += 1
        for _, _, flow in state.sources:
            _close_source(tally, flow)
    carriers: Counter[str] = Counter()
    for flow in tally.done:
        carriers.update(set(flow.messages))
    rows = [
        {
            "kind": flow.kind,
            "turns": len(flow.messages),
            "calls": flow.calls,
            "turns_with_usage": sum(1 for m in flow.messages if m in tally.usage),
            "shared_turns": sum(1 for m in flow.messages if carriers[m] > 1),
            **_flow_usage(tally, flow.messages),
        }
        for flow in tally.done
    ]
    kinds: dict[str, Any] = {}
    for kind in WORKFLOWS:
        mine = [row for row in rows if row["kind"] == kind]
        block: dict[str, Any] = {
            "workflows": len(mine),
            "turns": quantiles(row["turns"] for row in mine),
            "calls": quantiles(row["calls"] for row in mine),
            "shared_turns": sum(row["shared_turns"] for row in mine),
            "turns_missing_usage": sum(
                row["turns"] - row["turns_with_usage"] for row in mine
            ),
        }
        for _, name in USAGE_FIELDS:
            q = quantiles(row[name] for row in mine)
            block[name] = {k: q[k] for k in ("n", "total", "p50", "p90")}
        kinds[kind] = block
    aggregate = {
        "unit": "provider-reported tokens",
        "note": USAGE_NOTE,
        "kinds": kinds,
        "distinct_turns": {
            "turns": len(carriers),
            **_flow_usage(tally, carriers),
        },
        "lineages": len(lineages),
        "incomplete": {
            "write_chains_without_commit": tally.open_at_end["write_committed"],
            "searches_without_show": tally.searches_without_show,
            "injections_without_show": tally.injections_without_show,
        },
    }
    return aggregate, rows
