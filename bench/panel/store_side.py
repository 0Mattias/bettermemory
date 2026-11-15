"""What a store's own log says about its use, read from a copy.

The copy is taken through a read-only connection (`snapshot`): SQLite's
backup API reads the live file under a shared lock and writes nothing to
it, and every read here is of the copy. Events come from the product's
own row reader (`store_events.iter_events`) and the helped rate from its
own eval (`eval.compute_eval`), so the panel and ``bettermemory eval``
read one definition.

Four readings:

- ``eval``: the helped rate (Y1), the endorsement and silent-miss rates,
  the write review's chains (calls and characters per committed write,
  Y6) and the recall surfaces, as ``bettermemory eval`` computes them.
- ``verdicts`` (Y5): each served ``memory_show`` whose staleness verdict
  asked for a check, and what came of it within `CHECK_WINDOW`: a
  ``memory_verify`` of the record (the check found it held), a committed
  ``memory_update`` of it (the check found drift and fixed it), a
  ``memory_remove``, or nothing. Precision is updates over updates and
  verifies. Records shown as fresh are read the same way for contrast.
- ``pointers``: each settled pointer of the session brief and the prompt
  recall by its rank in the delivery, applied or unused.
- ``records``: per active record, how often it was retrieved (search
  hits, brief and recall pointers), opened, applied and ignored over the
  whole log, by age and standing. The aggregate goes in the receipt; the
  rows, which name records, are returned apart (`record_rows`) for a
  private file only.
"""

from __future__ import annotations

import json
import sqlite3
import types
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bettermemory import eval as _eval
from bettermemory import standing as _standing
from bettermemory import store_events

#: How long after a flagged show a verify, update or remove of the record
#: is read as that show's check.
CHECK_WINDOW = timedelta(hours=2)

#: The staleness verdicts that ask the model to check a record.
FLAGGED = frozenset({"spot_check_required", "spot_check_recommended", "stale"})

#: Age buckets, in days since a record was created.
AGE_BUCKETS = ((0, 7, "0-7d"), (7, 30, "7-30d"), (30, 90, "30-90d"), (90, None, "90d+"))

#: The pointer ranks reported one by one; deeper ranks are pooled.
RANKS = 8


def snapshot(live: Path, dest: Path) -> Path:
    """Copy `live` to `dest` through a read-only connection, and return
    `dest`. A `dest` that exists is refused, so no copy is overwritten."""
    if dest.exists():
        raise FileExistsError(f"{dest} exists; the snapshot never overwrites")
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return dest


def open_copy(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The log's telemetry rows in the product's event shape, seq order."""
    return list(store_events.iter_events(types.SimpleNamespace(_conn=conn)))  # type: ignore[arg-type]


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _in(ev: dict[str, Any], since: datetime | None, until: datetime | None) -> bool:
    ts = _ts(ev.get("ts"))
    if ts is None:
        return False
    return (since is None or ts >= since) and (until is None or ts < until)


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": round(numerator / denominator, 4) if denominator else None,
    }


# -- eval ----------------------------------------------------------------

_REVIEW_KEYS = (
    "chains",
    "calls_per_committed_write",
    "response_chars_p50",
    "response_chars_n",
    "committed_chains",
    "refused_chains",
    "unanswered_chains",
    "unanswered_share",
    "actions",
    "update_suggested",
)


def eval_section(
    evs: list[dict[str, Any]],
    *,
    since: datetime | None,
    until: datetime | None,
    now: datetime,
) -> dict[str, Any]:
    report = _eval.compute_eval([], evs, now=now, since=since, until=until).to_dict()
    surfaces = report.get("surfaces") or {}
    return {
        "memory_helped_rate": report["memory_helped_rate"],
        "endorsement_rate": report["endorsement_rate"],
        "silent_miss_rate": report["silent_miss_rate"],
        "counts": report["counts"],
        "events_in_window": report["events_in_window"],
        "review": {k: (report.get("review") or {}).get(k) for k in _REVIEW_KEYS},
        "surfaces": {
            name: surfaces.get(name)
            for name in ("brief", "recall", "settlements", "segments")
        },
    }


# -- verdicts ------------------------------------------------------------


def _committed_update(ev: dict[str, Any]) -> bool:
    return ev.get("status") in (None, "committed")


def verdict_section(
    evs: list[dict[str, Any]], *, since: datetime | None, until: datetime | None
) -> dict[str, Any]:
    """Y5: what the check a flagged show asked for found."""
    # Per record, the times of the actions that settle a check.
    actions: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for ev in evs:
        kind = ev.get("kind")
        mid = ev.get("id")
        ts = _ts(ev.get("ts"))
        if not isinstance(mid, str) or ts is None:
            continue
        if kind == "verify":
            actions[mid].append((ts, "verified"))
        elif kind == "update" and _committed_update(ev):
            actions[mid].append((ts, "updated"))
        elif kind == "remove":
            actions[mid].append((ts, "removed"))
    for rows in actions.values():
        rows.sort()

    def outcome(mid: str, shown: datetime) -> str:
        for ts, what in actions.get(mid, ()):
            if ts < shown:
                continue
            if ts - shown > CHECK_WINDOW:
                break
            return what
        return "unchecked"

    tallies: dict[str, Counter[str]] = {"flagged": Counter(), "fresh": Counter()}
    by_verdict: dict[str, Counter[str]] = defaultdict(Counter)
    drift_reported = Counter()
    shows = 0
    for ev in evs:
        if ev.get("kind") != "show" or not _in(ev, since, until):
            continue
        mid = ev.get("id")
        ts = _ts(ev.get("ts"))
        if not isinstance(mid, str) or ts is None:
            continue
        shows += 1
        verdict = str(ev.get("staleness_verdict") or "none")
        what = outcome(mid, ts)
        group = "flagged" if verdict in FLAGGED else "fresh"
        tallies[group][what] += 1
        by_verdict[verdict][what] += 1
        if group == "flagged":
            missing = ev.get("path_drift_missing")
            drift_reported[
                "path_missing"
                if isinstance(missing, int) and missing > 0
                else "no_path_missing"
            ] += 1

    def block(counter: Counter[str]) -> dict[str, Any]:
        total = sum(counter.values())
        return {
            "shows": total,
            "outcomes": dict(counter.most_common()),
            "verified_share": _rate(counter["verified"], total),
            "updated_share": _rate(counter["updated"], total),
        }

    flagged, fresh = block(tallies["flagged"]), block(tallies["fresh"])
    flagged_updated = flagged["updated_share"]["rate"]
    fresh_updated = fresh["updated_share"]["rate"]
    return {
        "check_window_hours": CHECK_WINDOW.total_seconds() / 3600,
        "shows": shows,
        "flagged_share": _rate(sum(tallies["flagged"].values()), shows),
        "flagged": flagged,
        "fresh": fresh,
        # Above 1 when a flagged record is corrected more often than a fresh
        # one: the flag carries news of drift. At or below 1 it does not.
        "update_lift_flagged_over_fresh": round(flagged_updated / fresh_updated, 3)
        if flagged_updated is not None and fresh_updated
        else None,
        "flagged_path_drift_reported": dict(drift_reported),
        "by_verdict": {v: dict(c.most_common()) for v, c in sorted(by_verdict.items())},
        "note": (
            "a verify after a show is read as a check that found the record "
            "held; a committed update follows a show for any edit, drift or "
            "not, so the update share is read against the fresh shows' (the "
            "lift), never alone; a check made without a tool call is not seen"
        ),
    }


# -- pointers by rank ----------------------------------------------------


def _rank_key(rank: int) -> str:
    return str(rank) if rank <= RANKS else f">{RANKS}"


def pointer_section(
    evs: list[dict[str, Any]], *, since: datetime | None, until: datetime | None
) -> dict[str, Any]:
    """Settled pointers of the brief and the recall by their rank."""
    delivered: dict[str, tuple[str, list[str]]] = {}
    for ev in evs:
        kind = ev.get("kind")
        event_id = ev.get("event_id")
        if not isinstance(event_id, str):
            continue
        if kind == "session_brief" and isinstance(ev.get("ids"), list):
            delivered[event_id] = (
                "brief",
                [i for i in ev["ids"] if isinstance(i, str)],
            )
        elif kind == "prompt_recall" and isinstance(ev.get("shown_ids"), list):
            delivered[event_id] = (
                "recall",
                [i for i in ev["shown_ids"] if isinstance(i, str)],
            )
    table: dict[str, dict[str, Counter[str]]] = {
        "brief": defaultdict(Counter),
        "recall": defaultdict(Counter),
    }
    unmatched = 0
    for ev in evs:
        if (
            ev.get("kind") != "use"
            or not ev.get("surface")
            or not _in(ev, since, until)
        ):
            continue
        shown = delivered.get(str(ev.get("shown_event_id")))
        if shown is None:
            unmatched += 1
            continue
        surface, ids = shown
        for mid in ev.get("ids") or []:
            if mid in ids:
                rank = ids.index(mid) + 1
                table[surface][_rank_key(rank)][str(ev.get("outcome"))] += 1
            else:
                unmatched += 1
    out: dict[str, Any] = {"unmatched_settlements": unmatched}
    for surface, ranks in table.items():
        rows = {}
        for rank in sorted(ranks, key=lambda r: (len(r), r)):
            counter = ranks[rank]
            total = sum(counter.values())
            rows[rank] = {
                "settled": total,
                "outcomes": dict(counter.most_common()),
                "applied": _rate(counter["applied"], total),
            }
        out[surface] = rows
    return out


# -- records -------------------------------------------------------------


def _bucket(age_days: float) -> str:
    for low, high, name in AGE_BUCKETS:
        if age_days >= low and (high is None or age_days < high):
            return name
    return AGE_BUCKETS[-1][2]


def record_rows(
    conn: sqlite3.Connection, evs: Iterable[dict[str, Any]], *, now: datetime
) -> list[dict[str, Any]]:
    """One row per active record: its age, standing and lifetime use.
    The rows name records: they go to a private file, never the receipt."""
    rows: dict[str, dict[str, Any]] = {}
    probability = dict(conn.execute("SELECT record_id, probability FROM standing"))
    for mid, created, category, length in conn.execute(
        "SELECT id, created, category, length(body) FROM memories"
    ):
        made = _ts(created) or now
        rows[mid] = {
            "id": mid,
            "category": category,
            "chars": length,
            "age_days": round((now - made).total_seconds() / 86400, 1),
            "standing": _standing.is_standing(category, probability.get(mid)),
            "searched": 0,
            "briefed": 0,
            "recalled": 0,
            "opened": 0,
            "applied": 0,
            "applied_explicit": 0,
            "unused": 0,
            "ignored": 0,
            "contradicted": 0,
            "last_retrieved": None,
            "last_applied": None,
        }

    def touch(mid: Any, field: str, ts: Any, last: str | None = None) -> None:
        row = rows.get(mid) if isinstance(mid, str) else None
        if row is None:
            return
        row[field] += 1
        if last is not None and isinstance(ts, str):
            if row[last] is None or ts > row[last]:
                row[last] = ts

    for ev in evs:
        kind = ev.get("kind")
        ts = ev.get("ts")
        if kind in ("search", "list"):
            for mid in ev.get("returned") or []:
                touch(mid, "searched", ts, "last_retrieved")
        elif kind == "session_brief":
            for mid in ev.get("ids") or []:
                touch(mid, "briefed", ts, "last_retrieved")
        elif kind == "prompt_recall":
            for mid in ev.get("shown_ids") or []:
                touch(mid, "recalled", ts, "last_retrieved")
        elif kind == "show":
            touch(ev.get("id"), "opened", ts, "last_retrieved")
        elif kind == "use":
            outcome = ev.get("outcome")
            for mid in set(i for i in ev.get("ids") or [] if isinstance(i, str)):
                if outcome == "applied":
                    touch(mid, "applied", ts, "last_applied")
                    if ev.get("auto") is not True and ev.get("attribution") != "auto":
                        touch(mid, "applied_explicit", ts)
                elif outcome in ("unused", "ignored", "contradicted"):
                    touch(mid, outcome, ts)
    return sorted(rows.values(), key=lambda r: r["id"])


def record_section(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The rows' aggregate by age and standing: no record is named."""
    groups: dict[tuple[str, bool], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(_bucket(row["age_days"]), row["standing"])].append(row)
    out: dict[str, Any] = {"records": len(rows), "groups": []}
    order = [name for _, _, name in AGE_BUCKETS]
    for (bucket, standing), members in sorted(
        groups.items(), key=lambda kv: (order.index(kv[0][0]), not kv[0][1])
    ):
        n = len(members)
        retrieved = [
            m
            for m in members
            if m["searched"] + m["briefed"] + m["recalled"] + m["opened"]
        ]
        out["groups"].append(
            {
                "age": bucket,
                "standing": standing,
                "records": n,
                "chars_total": sum(m["chars"] or 0 for m in members),
                "never_retrieved": _rate(n - len(retrieved), n),
                "never_opened": _rate(sum(1 for m in members if not m["opened"]), n),
                "never_applied_explicitly": _rate(
                    sum(1 for m in members if not m["applied_explicit"]), n
                ),
                "uses": {
                    key: sum(m[key] for m in members)
                    for key in (
                        "searched",
                        "briefed",
                        "recalled",
                        "opened",
                        "applied",
                        "applied_explicit",
                        "unused",
                        "ignored",
                        "contradicted",
                    )
                },
            }
        )
    standing_rows = [r for r in rows if r["standing"]]
    out["standing"] = {
        "records": len(standing_rows),
        "chars_total": sum(r["chars"] or 0 for r in standing_rows),
        "never_applied_explicitly": _rate(
            sum(1 for r in standing_rows if not r["applied_explicit"]),
            len(standing_rows),
        ),
    }
    return out


def read(
    conn: sqlite3.Connection,
    *,
    since: datetime | None,
    until: datetime | None,
    now: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The store's sections for the receipt, and the private record rows."""
    evs = events(conn)
    rows = record_rows(conn, evs, now=now)
    sections = {
        "log_rows": conn.execute("SELECT count(*) FROM log").fetchone()[0],
        "eval": eval_section(evs, since=since, until=until, now=now),
        "verdicts": verdict_section(evs, since=since, until=until),
        "pointers": pointer_section(evs, since=since, until=until),
        "records": record_section(rows),
    }
    return sections, rows


def dumps_rows(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, indent=1, sort_keys=True) + "\n"
