"""RS-P11 read from the live log: the recall's bookkeeping, three clauses.

THE PREDICTION (unit RS, memory 01M4PJJY6XDDAJNH7CEVDYFFV5, wording from
the 2026-09-26 draft 01M3FJ11EPZX0X8QJCGA2ES9SZ): "no id shown twice in a
segment; at least 0.95 of deliveries carry a prompt_id a Stop also
carries; at least 0.95 of Stops carry last_assistant_message." The
definitions below are the ones settled on 2026-10-17 after the second
read (memory 01M549H5NJ8S1RYVVG0402FEW1, sections Q3 and COUNTING
DEFECTS).

THE FORMULAS, per rule. Probe sessions (session id opening with
"switch-probe") are excluded throughout.

(1) repeat_shows / shows. A show is one id on a session_brief row's `ids`
or a prompt_recall row's `shown_ids`; a session_brief row's `omitted`
(the ids a brief withheld because its segment had shown them, recorded
since RS-F2) names no show and is not read. A show is keyed by
(session_id, agent, segment). The agent is empty for the session's own
shows, and a subagent's (ENG-150) for a brief printed into it: a
session_brief row that carries `agent_id` (a SessionStart whose payload
named the subagent), or one whose `event_id` a segment_opened row names
as its `brief_event_id` beside an `agent_id` (a compaction a later read
took back from the session for the subagent; that row is read wherever
it sits in the log, and its `segment`, the subagent's own, replaces the
brief row's). The clause is read over the session's keys, as the
prediction means it; a subagent's shows and repeats print on their own
line. A repeat show is one whose id was already shown under the same
key, by either surface, with the most recent earlier show within
`recall.STATE_IDLE_SECONDS` (24 hours) of it. A show across a longer gap
is counted on its own line, "re-shows past the idle window", and is not
a repeat: the daemon drops a session idle past that many seconds and
rebuilds it from the log's rows of the last 24 hours, so an id shown more
than a day before a resume is outside the rebuilt shown set and prints
again by the cooldown's own definition (RS-F2 declaration, memory
01M54ECJJ6XWS9KTVTC7PQJRQZ, section (b) item 4; pre-landing the cut moves
no figure, the longest gap between consecutive shows in a segment being
16.2 hours). The shown sets are built from every row up to the window's
end, so a repeat of a show before the window counts; shows, repeats and
re-shows are counted inside the window only. Repeats are split by the row
that repeated them: a brief (before RS-F2 a resumed session printed its
brief again in the segment it is in, since only compact and clear open a
segment) or a prompt recall. Target 0.

(2) with_prompt_id / deliveries, the clause's first half as the log
carries it. The second half is read directly where a turn_audited row
carries a prompt_id key: a delivery is carried when a Stop of its session
carries its prompt_id. Rows carry the key since RS-F3 (rs-v3,
`audit.turn_audited_fields`), and the direct line is the primary reading
for them; for rows written before it the line prints n/a, and the
labelled proxy is the transcript join: a Stop (a turn_audited row with
triggered_from stop_hook) writes probe_query as `events.redact_query` of
the raw human row its tail walk found (`hook.human_prompt`); that hash
finds the latest top-level human row of the same session at or before
the Stop in the transcripts under ~/.claude/projects (subagent files and
rows carrying isSidechain or agentId excluded), and that row's promptId
is what the Stop carries. A delivery is carried when its (session_id,
prompt_id) is among them. The proxy is printed over all deliveries and
over owner deliveries (probe_query.preview not opening with a harness
tag, the RS-P5 numerator's test). The former stop_carried line, which
joined deliveries to the settlement use rows, measured settlement (the
RS-P6 number) and is gone.

(3) replies / stops, both ways: with repeat Stops counted and without. A
Stop is a turn_audited row with triggered_from stop_hook whose
reply_source is not "interrupted" (from rs-v6 a Stop also audits the
turns the owner interrupted since the previous audit, RS-F7 part 6; those
rows are counted on their own line and are not Stops, while for clause
(2) they carry prompt ids as a Stop's row does); a repeat carries repeat
true; a reply is assistant_present true. That field is
true for a reply taken from the payload's last_assistant_message or, when
the payload has none, from the transcript tail, and the log does not say
which; the value reads at or above the clause.

THE SPLIT, by the rule the daemon served under. A log row of the recall's
own kinds (RULE_KINDS) carries its rule (rs-v1 at rc1, rs-v2 from the
RS-F1 landing, and so on) and splits by it. A row without one (a
turn_audited row written before RS-F7 part 6 gave the audit's rows the
key) takes the rule of the nearest earlier rule-carrying row by
timestamp (`rule_at`), and a row before the first such row reads as
UNLABELLED. The output prints each rule's first row.

Usage (store and transcripts opened read-only; counts only, no text):

    .venv/bin/python bench/recall/read_rs_p11.py --since 2026-10-12T04:30:46Z
    .venv/bin/python bench/recall/read_rs_p11.py --since 2026-10-12T04:30:46Z \
        --until 2026-10-26T00:00:00Z
"""

from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from bettermemory.events import redact_query  # noqa: E402
from bettermemory.hook import _SYNTHETIC_USER_PREFIXES, human_prompt  # noqa: E402
from bettermemory.recall import STATE_IDLE_SECONDS  # noqa: E402

PROBE_SESSION_PREFIX = "switch-probe"
#: The log kinds whose rows carry the rule the daemon served under when
#: they were written (read_rs_p5.RULE_KINDS, the same list).
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

Event = tuple[datetime, str, str, dict[str, Any]]
#: (session, Stop hash) -> the (timestamp, promptId) of each human row.
Turns = dict[tuple[str, str], list[tuple[datetime, str]]]


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


def _events(db: sqlite3.Connection, kinds: tuple[str, ...]) -> list[Event]:
    marks = ",".join("?" for _ in kinds)
    out: list[Event] = []
    rows = db.execute(
        f"select ts, session, kind, payload from log where kind in ({marks}) order by seq",
        kinds,
    )
    for ts, session, kind, payload in rows:
        stamp = _stamp(ts)
        if stamp is None:
            continue
        event = json.loads(payload)
        if not isinstance(event, dict):
            continue
        if _is_probe(session) or _is_probe(event.get("session_id")):
            continue
        out.append((stamp, session or "", kind, event))
    return out


def _is_stop_hook(event: dict[str, Any]) -> bool:
    return event.get("triggered_from") == "stop_hook"


def _is_interrupted_audit(event: dict[str, Any]) -> bool:
    return event.get("reply_source") == "interrupted"


def _is_stop(event: dict[str, Any]) -> bool:
    """A Stop's own row: written by the Stop hook for the turn it closed,
    not for a turn the owner interrupted."""
    return _is_stop_hook(event) and not _is_interrupted_audit(event)


def _probe_hash(event: dict[str, Any]) -> str | None:
    probe = event.get("probe_query")
    value = probe.get("hash") if isinstance(probe, dict) else None
    return value if isinstance(value, str) else None


def _is_owner_delivery(event: dict[str, Any]) -> bool:
    probe = event.get("probe_query")
    preview = probe.get("preview") if isinstance(probe, dict) else None
    return not (isinstance(preview, str) and preview.lstrip().startswith(PREVIEW_TAGS))


#: A brief's event id -> the (agent_id, segment) of the subagent it was
#: printed into, as a segment_opened row that took it back names them.
TakenBack = dict[str, tuple[str, int]]
#: The prefix of a subagent's counters (`shows`).
SUBAGENT = "subagent_"


def taken_back(db: sqlite3.Connection) -> TakenBack:
    """Every brief a later read took back from its session for a
    subagent (ENG-150): the `brief_event_id` of each segment_opened row
    that carries it beside an `agent_id` and an integer `segment`, mapped
    to that subagent and its segment."""
    out: TakenBack = {}
    for _stamp, _session, _kind, event in _events(db, ("segment_opened",)):
        agent = event.get("agent_id")
        brief = event.get("brief_event_id")
        segment = event.get("segment")
        if (
            isinstance(agent, str)
            and agent
            and isinstance(brief, str)
            and brief
            and isinstance(segment, int)
            and not isinstance(segment, bool)
        ):
            out[brief] = (agent, segment)
    return out


def show_scope(event: dict[str, Any], kind: str, taken: TakenBack) -> tuple[str, Any]:
    """The (agent, segment) a show row counts under: a subagent's for a
    brief printed into it (its own `agent_id`, or the take-back that
    names its `event_id`), the session's ("") otherwise."""
    if kind == "session_brief":
        event_id = event.get("event_id")
        if isinstance(event_id, str) and event_id in taken:
            return taken[event_id]
        agent = event.get("agent_id")
        if isinstance(agent, str) and agent:
            return agent, event.get("segment")
    return "", event.get("segment")


def shows(
    db: sqlite3.Connection, since: datetime, until: datetime, marks: RuleMarks
) -> dict[str, Counter[str]]:
    out: dict[str, Counter[str]] = defaultdict(Counter)
    taken = taken_back(db)
    #: (session_id, agent, segment) -> each shown id's most recent show.
    last_shown: dict[tuple[Any, str, Any], dict[str, datetime]] = defaultdict(dict)
    segments: dict[str, set[tuple[Any, str, Any]]] = defaultdict(set)
    for stamp, session, kind, event in _events(db, ("session_brief", "prompt_recall")):
        if stamp >= until:
            continue
        ids = event.get("ids") if kind == "session_brief" else event.get("shown_ids")
        agent, segment = show_scope(event, kind, taken)
        key = (event.get("session_id") or session, agent, segment)
        prefix = SUBAGENT if agent else ""
        inside = stamp >= since
        rule = rule_of(event, stamp, marks)
        repeated_by = (
            "repeat_shows_brief" if kind == "session_brief" else "repeat_shows_recall"
        )
        for memory_id in ids if isinstance(ids, list) else []:
            earlier = last_shown[key].get(memory_id)
            if inside:
                out[rule][prefix + "shows"] += 1
                segments[rule].add(key)
                if earlier is not None:
                    gap = (stamp - earlier).total_seconds()
                    if gap <= STATE_IDLE_SECONDS:
                        out[rule][prefix + "repeat_shows"] += 1
                        out[rule][prefix + repeated_by] += 1
                    else:
                        out[rule][prefix + "reshows_past_idle"] += 1
            last_shown[key][memory_id] = stamp
    for rule, keys in segments.items():
        out[rule]["segments"] = sum(1 for key in keys if not key[1])
        out[rule][SUBAGENT + "segments"] = sum(1 for key in keys if key[1])
    return out


def transcript_turns(transcripts: Path) -> Turns:
    """Every top-level human row keyed by (sessionId, Stop hash), the hash
    `hook.run_audit` writes as probe_query: `events.redact_query` of the
    text `hook.human_prompt` returns for the row."""
    out: Turns = defaultdict(list)
    for path in sorted(transcripts.glob("*/*.jsonl")):
        with path.open("rb") as handle:
            for raw in handle:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("type") != "user":
                    continue
                if row.get("isSidechain") or row.get("agentId"):
                    continue
                session = row.get("sessionId")
                if not isinstance(session, str) or _is_probe(session):
                    continue
                text = human_prompt(row)
                stamp = _stamp(row.get("timestamp"))
                prompt_id = row.get("promptId")
                if text is None or stamp is None:
                    continue
                if not isinstance(prompt_id, str) or not prompt_id:
                    continue
                out[(session, redact_query(text)["hash"])].append((stamp, prompt_id))
    for turns in out.values():
        turns.sort()
    return out


def carried_by_stops(
    db: sqlite3.Connection, until: datetime, turns: Turns
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """The (session_id, prompt_id) pairs the Stops carry: directly, on a
    turn_audited row with a prompt_id key; by proxy, through the
    transcript join the docstring describes."""
    direct: set[tuple[str, str]] = set()
    proxy: set[tuple[str, str]] = set()
    for stamp, session, _kind, event in _events(db, ("turn_audited",)):
        if stamp >= until or not _is_stop_hook(event):
            continue
        session_id = str(event.get("session_id") or session)
        prompt_id = event.get("prompt_id")
        if isinstance(prompt_id, str) and prompt_id:
            direct.add((session_id, prompt_id))
        probe_hash = _probe_hash(event)
        if probe_hash is None:
            continue
        before = [
            pid for ts, pid in turns.get((session_id, probe_hash), []) if ts <= stamp
        ]
        if before:
            proxy.add((session_id, before[-1]))
    return direct, proxy


def prompt_ids(
    db: sqlite3.Connection,
    since: datetime,
    until: datetime,
    direct: set[tuple[str, str]],
    proxy: set[tuple[str, str]],
    marks: RuleMarks,
) -> dict[str, Counter[str]]:
    out: dict[str, Counter[str]] = defaultdict(Counter)
    for stamp, session, _kind, event in _events(db, ("prompt_recall",)):
        if not (since <= stamp < until):
            continue
        counts = out[rule_of(event, stamp, marks)]
        counts["deliveries"] += 1
        owner = _is_owner_delivery(event)
        if owner:
            counts["owner_deliveries"] += 1
        prompt_id = event.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            continue
        counts["with_prompt_id"] += 1
        key = (str(event.get("session_id") or session), prompt_id)
        if key in direct:
            counts["direct_carried"] += 1
        if key in proxy:
            counts["proxy_carried"] += 1
            if owner:
                counts["owner_proxy_carried"] += 1
    return out


def stops(
    db: sqlite3.Connection, since: datetime, until: datetime, marks: RuleMarks
) -> dict[str, Counter[str]]:
    out: dict[str, Counter[str]] = defaultdict(Counter)
    for stamp, _session, _kind, event in _events(db, ("turn_audited",)):
        if not (since <= stamp < until) or not _is_stop_hook(event):
            continue
        counts = out[rule_of(event, stamp, marks)]
        if _is_interrupted_audit(event):
            counts["interrupted_audits"] += 1
            continue
        counts["stops"] += 1
        if "prompt_id" in event:
            counts["stops_with_prompt_id"] += 1
        repeat = bool(event.get("repeat"))
        reply = bool(event.get("assistant_present"))
        if repeat:
            counts["repeat_stops"] += 1
        else:
            counts["first_stops"] += 1
        if reply:
            counts["replies"] += 1
            if not repeat:
                counts["first_replies"] += 1
    return out


def _ratio(numerator: int, denominator: int) -> str:
    return f"{numerator / denominator:.3f}" if denominator else "n/a"


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
    shown = shows(db, since, until, marks)
    direct, proxy = carried_by_stops(
        db, until, transcript_turns(Path(args.transcripts).expanduser())
    )
    joined = prompt_ids(db, since, until, direct, proxy, marks)
    stopped = stops(db, since, until, marks)
    firsts = ", ".join(
        f"{rule} from {_iso(stamp)}" for rule, stamp in first_rows(marks)
    )
    print(
        f"RS-P11 window {_iso(since)} to {_iso(until)}; "
        f"rules from the log's rows: {firsts or 'none'}"
    )
    for rule in sorted(set(shown) | set(joined) | set(stopped)):
        s, j, t = shown[rule], joined[rule], stopped[rule]
        print(rule)
        print(
            f"  (1) repeat_shows {s['repeat_shows']} / shows {s['shows']} over "
            f"{s['segments']} segments = {_ratio(s['repeat_shows'], s['shows'])} "
            f"(repeated by a brief {s['repeat_shows_brief']}, by a prompt recall "
            f"{s['repeat_shows_recall']}; re-shows past the idle window "
            f"{s['reshows_past_idle']})"
        )
        print(
            f"      subagents (ENG-150), outside the clause: repeat_shows "
            f"{s[SUBAGENT + 'repeat_shows']} / shows {s[SUBAGENT + 'shows']} over "
            f"{s[SUBAGENT + 'segments']} segments (re-shows past the idle window "
            f"{s[SUBAGENT + 'reshows_past_idle']})"
        )
        print(
            f"  (2) with_prompt_id {j['with_prompt_id']} / deliveries {j['deliveries']} "
            f"= {_ratio(j['with_prompt_id'], j['deliveries'])}"
        )
        if t["stops_with_prompt_id"]:
            print(
                f"      direct (turn_audited prompt_id): {j['direct_carried']} / "
                f"deliveries {j['deliveries']} = "
                f"{_ratio(j['direct_carried'], j['deliveries'])} "
                f"({t['stops_with_prompt_id']} of {t['stops']} Stops carry the key)"
            )
        else:
            print(
                f"      direct (turn_audited prompt_id): n/a "
                f"(0 of {t['stops']} Stops carry the key)"
            )
        print(
            f"      proxy (transcript join): {j['proxy_carried']} / deliveries "
            f"{j['deliveries']} = {_ratio(j['proxy_carried'], j['deliveries'])}; "
            f"owner deliveries {j['owner_proxy_carried']} / {j['owner_deliveries']} "
            f"= {_ratio(j['owner_proxy_carried'], j['owner_deliveries'])}"
        )
        print(
            f"  (3) replies {t['replies']} / stops {t['stops']} = "
            f"{_ratio(t['replies'], t['stops'])} with repeat Stops; "
            f"{t['first_replies']} / {t['first_stops']} = "
            f"{_ratio(t['first_replies'], t['first_stops'])} without "
            f"(repeats {t['repeat_stops']}; interrupted turns audited by a Stop, "
            f"not counted as Stops: {t['interrupted_audits']})"
        )
    print(
        "(1) target 0; (2) and (3) target at least 0.95, read as the docstring says: "
        "the direct line reads turn_audited rows that carry prompt_id (rs-v3 on), "
        "the proxy is a transcript join, and assistant_present does not say whether "
        "the reply came from the Stop payload or the transcript tail"
    )


if __name__ == "__main__":
    main()
