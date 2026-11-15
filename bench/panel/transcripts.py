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
