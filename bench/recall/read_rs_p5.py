"""RS-P5 read from the live log: the recall's fire rate on owner prompts.

THE PREDICTION (unit RS, memory 01M4PJJY6XDDAJNH7CEVDYFFV5, wording from
the 2026-09-26 draft 01M3FJ11EPZX0X8QJCGA2ES9SZ): "in the first two weeks
of daily use the recall fires on 0.30 to 0.50 of in-repository prompts",
counted from the owner's switch of his live install. The definitions
below are the ones settled on 2026-10-17 after the second read (memory
01M549H5NJ8S1RYVVG0402FEW1, sections Q1 and COUNTING DEFECTS).

THE FORMULA, per rule: value = matched_deliveries / owner_prompts. The
raw delivery count is printed beside the matched one, so nothing is
hidden by the join.

owner_prompts, the denominator: user rows of the top-level transcripts
under ~/.claude/projects (files under a subagents directory, and rows
carrying isSidechain or agentId, excluded) inside the window that pass
the gate the rc2 recall applies (`recall.prompt_recall`): not isMeta, not
isCompactSummary, `recall.clean_prompt` of the row's text non-empty, and
the cleaned text not `hook.is_synthetic_prompt`. The app wraps a
session's first prompt in a system reminder; `hook.human_prompt` rejects
such a row while the recall strips the reminder and probes the owner's
words, so the gate's own test is the one that says whether the recall
can fire. Two further exclusions, each counted in the output: rows
opening "[Request interrupted" (an interrupt the harness writes, never a
UserPromptSubmit), and rows of a session outside a repository (the
recall delivers nothing outside one, and the prediction counts
in-repository prompts). A session with hook rows in the log (rows whose
payload carries session_id) takes the hook's own verdict: it is in a
repository when any of its rows carries worktree_root. A session with no
hook rows is asked of git: `git rev-parse --show-toplevel` from the
nearest existing ancestor of the row's cwd, so a deleted agent worktree
still reads as its repository. The output says how many sessions were
decided each way. A session takes one verdict for all its rows, so a
session that changed directory has rows git would judge the other way;
the output counts those rows too (in the rc1 window one, the
scratch-workspace opener of a session that then moved into bettermemory,
which is why the denominator reads 167 where the per-row git test read
166).

owner_deliveries, raw: log rows of kind prompt_recall inside the window
(a row is written only when at least one pointer was delivered), harness
claude-code, probe sessions (session id opening with "switch-probe")
excluded, whose probe_query.preview does not open with a harness tag.
That is the gate's own test (`hook.is_synthetic_prompt`) applied to the
32-character preview the log keeps. Before RS-F1 the recall fired on task
notifications and agent messages too; those rows are excluded so both
sides of the landing count the same thing. After RS-F1 the gate writes no
such row.

matched_deliveries, the numerator: owner deliveries joined to a
denominator row of the same session, by the production hash
`events.redact_query(clean_prompt(text))["hash"]` of the row's text
against the delivery's probe_query hash, then by the delivery's
prompt_id against the row's promptId. The numerator is a subset of the
denominator by construction. Unmatched deliveries are printed as a
separate count with their seqs (pre-landing: seq 22448, whose probed
text is in no transcript row).

QUEUED COMMANDS, beside the denominator. What the owner types while a
turn runs, the harness holds for the next turn and writes to the
transcript as a row of type attachment carrying a queued_command
(`owner_prompt.queued_text`; an attachment flagged isMeta is the app's
own note and is not read). Such a row is no user row, so it is not in
the denominator the prediction was settled on; it is counted on its own
line per rule, through the same gate and the same in-repository test,
and a delivery no denominator row matches is joined to it by the same
hash. The output names how many of the unmatched deliveries a queued
command accounts for; the value line does not move.

THE SPLIT, by the rule the daemon served under. A log row of the recall's
own kinds (RULE_KINDS) carries its rule (rs-v1 at rc1, rs-v2 from the
RS-F1 landing, and so on) and splits by it. A row without one, a
transcript row or a log row written before its kind carried the key,
takes the rule of the nearest earlier rule-carrying row by timestamp
(`rule_at`), and a row before the first such row reads as
UNLABELLED. The output prints each rule's first row, so a reader can
see the span a daemon served before its first recall row (at rc2, 33
seconds and 2 user rows), which this labelling gives to the rule before.

Usage (store and transcripts opened read-only; counts only, no text):

    .venv/bin/python bench/recall/read_rs_p5.py --since 2026-10-12T04:30:46Z
    .venv/bin/python bench/recall/read_rs_p5.py --since 2026-10-12T04:30:46Z \
        --until 2026-10-26T00:00:00Z
"""

from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from bettermemory.events import redact_query  # noqa: E402
from bettermemory.hook import (  # noqa: E402
    _SYNTHETIC_USER_PREFIXES,
    _flatten_assistant_content,
    is_synthetic_prompt,
)
from bettermemory.owner_prompt import queued_text  # noqa: E402
from bettermemory.recall import clean_prompt  # noqa: E402

PROBE_SESSION_PREFIX = "switch-probe"
INTERRUPT_PREFIX = "[Request interrupted"
HARNESS = "claude-code"
#: The log kinds whose rows carry the rule the daemon served under when
#: they were written: the recall's own rows and its audit's. Other kinds
#: carry a ``rule`` key of their own meaning (checkout_registered names
#: the ladder rule that decided a home, recall_grade copies the graded
#: row's) and are not read.
RULE_KINDS = (
    "session_brief",
    "prompt_recall",
    "segment_opened",
    "turn_audited",
    "search_miss",
    "recall_held",
    "recall_suppressed",
)
#: The rule of a row before the first rule-carrying row.
UNLABELLED = "unlabelled"
#: `events.redact_query` keeps the first 32 characters of the probed text.
PREVIEW_CHARS = 32
#: The gate's tags as a preview can show them.
PREVIEW_TAGS = tuple(tag[:PREVIEW_CHARS] for tag in _SYNTHETIC_USER_PREFIXES)


def _stamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(stamp: datetime) -> str:
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _is_probe(session: Any) -> bool:
    return isinstance(session, str) and session.startswith(PROBE_SESSION_PREFIX)


#: (timestamp, rule) of every RULE_KINDS row carrying a rule, by timestamp.
RuleMarks = tuple[list[datetime], list[str]]


def rule_marks(db: sqlite3.Connection) -> RuleMarks:
    """Every RULE_KINDS row's (timestamp, rule), sorted by timestamp, as
    two parallel lists for `rule_at`."""
    marks: list[tuple[datetime, str]] = []
    placeholders = ",".join("?" for _ in RULE_KINDS)
    rows = db.execute(
        f"select ts, payload from log where kind in ({placeholders}) order by seq",
        RULE_KINDS,
    )
    for ts, payload in rows:
        if '"rule"' not in payload:
            continue
        stamp = _stamp(ts)
        try:
            event = json.loads(payload)
        except ValueError:
            continue
        rule = event.get("rule") if isinstance(event, dict) else None
        if stamp is not None and isinstance(rule, str) and rule:
            marks.append((stamp, rule))
    marks.sort()
    return [stamp for stamp, _ in marks], [rule for _, rule in marks]


def rule_at(stamp: datetime, marks: RuleMarks) -> str:
    """The rule of the nearest rule-carrying row at or before `stamp`;
    UNLABELLED before the first."""
    stamps, rules = marks
    index = bisect.bisect_right(stamps, stamp)
    return rules[index - 1] if index else UNLABELLED


def rule_of(event: dict[str, Any], stamp: datetime, marks: RuleMarks) -> str:
    """A log row's rule: its own when it carries one, else `rule_at`."""
    rule = event.get("rule")
    return rule if isinstance(rule, str) and rule else rule_at(stamp, marks)


def first_rows(marks: RuleMarks) -> list[tuple[str, datetime]]:
    """Each rule and the timestamp of its first row, in order of appearance."""
    out: list[tuple[str, datetime]] = []
    seen: set[str] = set()
    for stamp, rule in zip(*marks):
        if rule not in seen:
            seen.add(rule)
            out.append((rule, stamp))
    return out


@dataclass
class Prompts:
    """The denominator per rule, with the keys a delivery is joined by
    ((session, probe hash) and (session, promptId) of every denominator
    row), the queued commands' keys ((session, probe hash)) beside them,
    and the sessions decided each way."""

    counts: dict[str, Counter[str]] = field(
        default_factory=lambda: defaultdict(Counter)
    )
    hashes: set[tuple[str, str]] = field(default_factory=set)
    prompt_ids: set[tuple[str, str]] = field(default_factory=set)
    queued_hashes: set[tuple[str, str]] = field(default_factory=set)
    decided: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))


def hook_verdicts(db: sqlite3.Connection) -> dict[str, bool]:
    """The hook's own in-repository verdict per session: every log row
    whose payload carries session_id is a hook row, and the session is in
    a repository when any of its rows carries worktree_root."""
    verdicts: dict[str, bool] = {}
    for (payload,) in db.execute("select payload from log order by seq"):
        if '"session_id"' not in payload:
            continue
        try:
            event = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        session = event.get("session_id")
        if not isinstance(session, str) or not session:
            continue
        root = event.get("worktree_root")
        verdicts[session] = verdicts.get(session, False) or (
            isinstance(root, str) and bool(root)
        )
    return verdicts


def _git_in_repository(cwd: Any, cache: dict[str, bool]) -> bool:
    if not isinstance(cwd, str) or not cwd:
        return False
    if cwd not in cache:
        probe = Path(cwd)
        while not probe.is_dir() and probe != probe.parent:
            probe = probe.parent
        done = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
        cache[cwd] = done.returncode == 0
    return cache[cwd]


def _in_repository(
    session: str,
    cwd: Any,
    verdicts: dict[str, bool],
    decided: dict[str, set[str]],
    cache: dict[str, bool],
    counts: Counter[str],
) -> bool:
    """The hook's verdict where the session has hook rows, git's otherwise.
    Both are computed so the rows where they differ can be counted: a
    session takes one verdict for all its rows, and a session that changed
    directory has rows git judges the other way."""
    git = _git_in_repository(cwd, cache)
    if session not in verdicts:
        decided["git_in" if git else "git_out"].add(session)
        return git
    verdict = verdicts[session]
    decided["hook_in" if verdict else "hook_out"].add(session)
    if verdict != git:
        counts["hook_git_differ"] += 1
    return verdict


def _row_text(row: dict[str, Any]) -> str | None:
    """The row's text as the UserPromptSubmit payload carried it: a string
    content as is, a content list's text blocks joined (the extraction
    `hook.human_prompt` uses)."""
    message = row.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    return _flatten_assistant_content(content)


def count_prompts(
    transcripts: Path,
    since: datetime,
    until: datetime,
    marks: RuleMarks,
    verdicts: dict[str, bool],
) -> Prompts:
    out = Prompts()
    cache: dict[str, bool] = {}
    for path in sorted(transcripts.glob("*/*.jsonl")):
        with path.open("rb") as handle:
            for raw in handle:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                queued = row.get("type") == "attachment"
                if row.get("type") != "user" and not queued:
                    continue
                if row.get("isSidechain") or row.get("agentId"):
                    continue
                stamp = _stamp(row.get("timestamp"))
                if stamp is None or not (since <= stamp < until):
                    continue
                session = row.get("sessionId")
                if _is_probe(session):
                    continue
                if row.get("isMeta") or row.get("isCompactSummary"):
                    continue
                text = queued_text(row) if queued else _row_text(row)
                if text is None:
                    continue
                # The rc2 recall's gate, in its order.
                query = clean_prompt(text)
                if not query.strip() or is_synthetic_prompt(query):
                    continue
                counts = out.counts[rule_at(stamp, marks)]
                if queued:
                    counts["queued_rows"] += 1
                else:
                    counts["accepted_rows"] += 1
                if query.lstrip().startswith(INTERRUPT_PREFIX):
                    counts["interrupt_excluded"] += 1
                    continue
                session = session if isinstance(session, str) else ""
                if not _in_repository(
                    session, row.get("cwd"), verdicts, out.decided, cache, counts
                ):
                    counts[
                        "queued_outside_repository"
                        if queued
                        else "outside_repository_excluded"
                    ] += 1
                    continue
                if queued:
                    counts["queued_prompts"] += 1
                    out.queued_hashes.add((session, redact_query(query)["hash"]))
                    continue
                counts["owner_prompts"] += 1
                out.hashes.add((session, redact_query(query)["hash"]))
                prompt_id = row.get("promptId")
                if isinstance(prompt_id, str) and prompt_id:
                    out.prompt_ids.add((session, prompt_id))
    return out


def count_deliveries(
    db: sqlite3.Connection,
    since: datetime,
    until: datetime,
    prompts: Prompts,
    marks: RuleMarks,
) -> tuple[dict[str, Counter[str]], dict[str, list[int]]]:
    out: dict[str, Counter[str]] = defaultdict(Counter)
    unmatched: dict[str, list[int]] = defaultdict(list)
    rows = db.execute(
        "select seq, ts, session, payload from log "
        "where kind='prompt_recall' order by seq"
    )
    for seq, ts, session, payload in rows:
        stamp = _stamp(ts)
        if stamp is None or not (since <= stamp < until):
            continue
        event = json.loads(payload)
        rule = rule_of(event, stamp, marks)
        counts = out[rule]
        counts["rows"] += 1
        if _is_probe(session) or _is_probe(event.get("session_id")):
            counts["probe_excluded"] += 1
            continue
        if event.get("harness") != HARNESS:
            counts["other_harness_excluded"] += 1
            continue
        probe = event.get("probe_query")
        preview = probe.get("preview") if isinstance(probe, dict) else None
        if isinstance(preview, str) and preview.lstrip().startswith(PREVIEW_TAGS):
            counts["machine_prompt_excluded"] += 1
            continue
        counts["owner_deliveries"] += 1
        session_id = event.get("session_id") or session or ""
        probe_hash = probe.get("hash") if isinstance(probe, dict) else None
        if (session_id, probe_hash) in prompts.hashes or (
            session_id,
            event.get("prompt_id"),
        ) in prompts.prompt_ids:
            counts["matched_deliveries"] += 1
        else:
            unmatched[rule].append(int(seq))
            if (session_id, probe_hash) in prompts.queued_hashes:
                counts["matched_queued"] += 1
    return out, unmatched


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--since", required=True, help="ISO 8601 UTC, inclusive")
    parser.add_argument(
        "--until", default=None, help="ISO 8601 UTC, exclusive; default now"
    )
    parser.add_argument("--store", default="~/.bettermemory/memory.sqlite")
    parser.add_argument("--transcripts", default="~/.claude/projects")
    args = parser.parse_args()
    since = _stamp(args.since)
    until = _stamp(args.until) if args.until else datetime.now(timezone.utc)
    if since is None or until is None:
        parser.error("--since and --until take ISO 8601 timestamps")
    db = _ro(Path(args.store).expanduser())
    marks = rule_marks(db)
    prompts = count_prompts(
        Path(args.transcripts).expanduser(), since, until, marks, hook_verdicts(db)
    )
    deliveries, unmatched = count_deliveries(db, since, until, prompts, marks)
    firsts = ", ".join(
        f"{rule} from {_iso(stamp)}" for rule, stamp in first_rows(marks)
    )
    print(
        f"RS-P5 window {_iso(since)} to {_iso(until)}; "
        f"rules from the log's rows: {firsts or 'none'}"
    )
    for rule in sorted(set(deliveries) | set(prompts.counts)):
        d = deliveries[rule]
        p = prompts.counts[rule]
        matched = (
            d["matched_deliveries"] / p["owner_prompts"] if p["owner_prompts"] else None
        )
        raw = d["owner_deliveries"] / p["owner_prompts"] if p["owner_prompts"] else None
        seqs = ", ".join(str(seq) for seq in unmatched[rule]) or "none"
        print(rule)
        print(
            f"  owner_deliveries {d['owner_deliveries']} raw, "
            f"{d['matched_deliveries']} matched to a denominator row  "
            f"(prompt_recall rows {d['rows']}, excluded: probe sessions "
            f"{d['probe_excluded']}, other harness {d['other_harness_excluded']}, "
            f"machine prompts {d['machine_prompt_excluded']}; unmatched "
            f"{len(unmatched[rule])}: seq {seqs})"
        )
        print(
            f"  owner_prompts    {p['owner_prompts']}  (rows the gate accepts "
            f"{p['accepted_rows']}, excluded: interrupts {p['interrupt_excluded']}, "
            f"outside a repository {p['outside_repository_excluded']}; rows where "
            f"the hook's verdict differs from git's {p['hook_git_differ']})"
        )
        print(
            f"  value            {matched:.3f} matched ({raw:.3f} raw)"
            if matched is not None and raw is not None
            else "  value            n/a"
        )
        print(
            f"  queued_prompts   {p['queued_prompts']}  beside the denominator "
            f"(queued rows the gate accepts {p['queued_rows']}, outside a "
            f"repository {p['queued_outside_repository']}; unmatched deliveries "
            f"a queued command accounts for {d['matched_queued']})"
        )
    decided = prompts.decided
    print(
        f"sessions decided by the hook's rows "
        f"{len(decided['hook_in'] | decided['hook_out'])} "
        f"(in a repository {len(decided['hook_in'])}), by git "
        f"{len(decided['git_in'] | decided['git_out'])} "
        f"(in a repository {len(decided['git_in'])})"
    )
    print("value = matched_deliveries / owner_prompts; target 0.30 to 0.50")


if __name__ == "__main__":
    main()
