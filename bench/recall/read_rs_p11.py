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
or a prompt_recall row's `shown_ids`; a repeat show is one whose id was
already shown under the same (session_id, segment). The shown sets are
built from every row up to the window's end, so a repeat of a show before
the window counts; shows and repeats are counted inside the window only.
Repeats are split by the row that repeated them: a brief (a resumed
session prints its brief again in the segment it is in, since only
compact and clear open a segment) or a prompt recall. Target 0.

(2) with_prompt_id / deliveries, the clause's first half as the log
carries it. The second half is read directly where a turn_audited row
carries a prompt_id key: a delivery is carried when a Stop of its session
carries its prompt_id. No row carries the key yet (`audit.
turn_audited_fields` takes none; a later unit adds it), so that line
prints n/a until rows written after that unit exist, and it is the
primary reading for those rows. For rows written before it, the labelled
proxy is the transcript join: a Stop (a turn_audited row with
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
Stop is a turn_audited row with triggered_from stop_hook; a repeat
carries repeat true; a reply is assistant_present true. That field is
true for a reply taken from the payload's last_assistant_message or, when
the payload has none, from the transcript tail, and the log does not say
which; the value reads at or above the clause.

THE SPLIT. prompt_recall and session_brief rows carry their rule (rs-v1
at rc1, rs-v2 from the RS-F1 landing) and split by it. turn_audited rows
and transcript rows carry none and split at the landing instant, the
moment the rc2 daemon began serving: 2026-10-17T05:50:18Z, the default of
--landing. The first log row whose rule is rs-v2, the landing row the
RS-F1 record names, is at 2026-10-17T05:50:51.886601Z; 2 user rows and no
delivery or Stop fall between the two instants.

Usage (store and transcripts opened read-only; counts only, no text):

    .venv/bin/python bench/recall/read_rs_p11.py --since 2026-10-12T04:30:46Z
    .venv/bin/python bench/recall/read_rs_p11.py --since 2026-10-12T04:30:46Z \
        --until 2026-10-26T00:00:00Z
"""

from __future__ import annotations

import argparse
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

PROBE_SESSION_PREFIX = "switch-probe"
#: The instant the rc2 daemon began serving (install done 05:50:14Z).
LANDING_DEFAULT = "2026-10-17T05:50:18Z"
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


def _rule_at(stamp: datetime, landing: datetime) -> str:
    return "rs-v2" if stamp >= landing else "rs-v1"


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


def _is_stop(event: dict[str, Any]) -> bool:
    return event.get("triggered_from") == "stop_hook"


def _probe_hash(event: dict[str, Any]) -> str | None:
    probe = event.get("probe_query")
    value = probe.get("hash") if isinstance(probe, dict) else None
    return value if isinstance(value, str) else None


def _is_owner_delivery(event: dict[str, Any]) -> bool:
    probe = event.get("probe_query")
    preview = probe.get("preview") if isinstance(probe, dict) else None
    return not (isinstance(preview, str) and preview.lstrip().startswith(PREVIEW_TAGS))


def shows(
    db: sqlite3.Connection, since: datetime, until: datetime
) -> dict[str, Counter[str]]:
    out: dict[str, Counter[str]] = defaultdict(Counter)
    seen: dict[tuple[Any, Any], set[str]] = defaultdict(set)
    segments: dict[str, set[tuple[Any, Any]]] = defaultdict(set)
    for stamp, session, kind, event in _events(db, ("session_brief", "prompt_recall")):
        if stamp >= until:
            continue
        ids = event.get("ids") if kind == "session_brief" else event.get("shown_ids")
        key = (event.get("session_id") or session, event.get("segment"))
        inside = stamp >= since
        # Both kinds carry their rule; the split needs no instant here.
        rule = str(event.get("rule"))
        repeated_by = (
            "repeat_shows_brief" if kind == "session_brief" else "repeat_shows_recall"
        )
        for memory_id in ids if isinstance(ids, list) else []:
            if inside:
                out[rule]["shows"] += 1
                segments[rule].add(key)
                if memory_id in seen[key]:
                    out[rule]["repeat_shows"] += 1
                    out[rule][repeated_by] += 1
            seen[key].add(memory_id)
    for rule, keys in segments.items():
        out[rule]["segments"] = len(keys)
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
        if stamp >= until or not _is_stop(event):
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
) -> dict[str, Counter[str]]:
    out: dict[str, Counter[str]] = defaultdict(Counter)
    for stamp, session, _kind, event in _events(db, ("prompt_recall",)):
        if not (since <= stamp < until):
            continue
        counts = out[str(event.get("rule"))]
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
    db: sqlite3.Connection, since: datetime, until: datetime, landing: datetime
) -> dict[str, Counter[str]]:
    out: dict[str, Counter[str]] = defaultdict(Counter)
    for stamp, _session, _kind, event in _events(db, ("turn_audited",)):
        if not (since <= stamp < until) or not _is_stop(event):
            continue
        counts = out[_rule_at(stamp, landing)]
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
    parser.add_argument(
        "--landing",
        default=LANDING_DEFAULT,
        help="ISO 8601 UTC; rule-less rows at or after it read as rs-v2",
    )
    parser.add_argument("--store", default="~/.bettermemory/memory.sqlite")
    parser.add_argument("--transcripts", default="~/.claude/projects")
    args = parser.parse_args()
    since = _stamp(args.since)
    until = _stamp(args.until) if args.until else datetime.now(timezone.utc)
    landing = _stamp(args.landing)
    if since is None or until is None or landing is None:
        parser.error("--since, --until and --landing take ISO 8601 timestamps")
    db = _ro(Path(args.store).expanduser())
    shown = shows(db, since, until)
    direct, proxy = carried_by_stops(
        db, until, transcript_turns(Path(args.transcripts).expanduser())
    )
    joined = prompt_ids(db, since, until, direct, proxy)
    stopped = stops(db, since, until, landing)
    print(
        f"RS-P11 window {_iso(since)} to {_iso(until)}; "
        f"landing {_iso(landing)} (rc2 daemon up)"
    )
    for rule in sorted(set(shown) | set(joined) | set(stopped)):
        s, j, t = shown[rule], joined[rule], stopped[rule]
        print(rule)
        print(
            f"  (1) repeat_shows {s['repeat_shows']} / shows {s['shows']} over "
            f"{s['segments']} segments = {_ratio(s['repeat_shows'], s['shows'])} "
            f"(repeated by a brief {s['repeat_shows_brief']}, by a prompt recall "
            f"{s['repeat_shows_recall']})"
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
            f"(repeats {t['repeat_stops']})"
        )
    print(
        "(1) target 0; (2) and (3) target at least 0.95, read as the docstring says: "
        "the direct line waits for turn_audited to carry prompt_id, the proxy is a "
        "transcript join, and assistant_present does not say whether the reply came "
        "from the Stop payload or the transcript tail"
    )


if __name__ == "__main__":
    main()
