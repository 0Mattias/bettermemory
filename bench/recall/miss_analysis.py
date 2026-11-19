"""Why a first ask missed: vocabulary, ranking, or something else.

The Stop audit's own miss rows cannot answer this. Its probe is a lexical
search, so every record it reports as missed shares words with the turn;
a record whose wording differs from the ask is invisible to it. This
program reads need from what the model went on to apply instead.

Two populations, both from a read-only copy of a store and the harness
transcripts (the log keeps a query only as a hash, a preview and a
length; the full text is the transcript's):

- ``search``: a record the model opened (``memory_show``) after a
  ``memory_search`` returned it. The ask counts as missed when the search
  just before it in the same session, at most a window earlier, did not
  return that record: the model had to ask again.
- ``hook``: a record the model opened in a turn after a search of that
  turn returned it, when the turn's prompt recall had not shown it.

Opening is the need signal because the log ties it to the session; the
subset later marked applied (by the model, the Stop hook or the command
line) is reported beside every figure.

Each miss is classed from the missed ask's text and the record as it is
in the copy, with the product's tokenizer and ranker over every record
(no admission, so a scope or membership filter shows as its own class):

- ``no_shared_term``: the ask and the record share no content token. A
  vocabulary gap when the two asks meant the same thing; a different
  topic otherwise. The token test cannot tell these apart, so the private
  rows carry both asks for a reader to judge.
- ``ranked_within_width``: the ranker puts the record inside the width
  the ask requested, so the ranking was not what withheld it (a scope or
  membership filter, a threshold, or a record edited since).
- ``outranked``: shared terms, ranked beyond the width.
- ``no_topic_words`` (hook only): the prompt has fewer than
  `MIN_TOPIC_TOKENS` content tokens ("yes, run it").
- ``record_newer_than_ask`` and ``record_gone``: not misses; counted apart.

Predictions, written before the first run (2026-11-16):

- P1  ``no_shared_term`` is under 25% of the classed search misses.
      MISSED if it is 25% or more.
- P2  ``no_shared_term`` is under 25% of the classed hook misses, and
      ``no_topic_words`` or ``outranked`` is the largest class.
      MISSED if either half fails.
- P3  at least 70% of records opened from a search were returned by
      the first ask. MISSED if under 70%.

The receipt holds aggregates only. The rows, which carry queries, prompts
and record ids, go to `--private-out`, which must lie outside the
repository.

    python bench/recall/miss_analysis.py --copy-dir <dir with memory.sqlite> \\
        --out bench/recall/results/miss-analysis-2026-11-16.json \\
        --private-out ~/.cache/bettermemory-v9/receipts/miss-analysis-2026-11-16
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "bench"))
sys.path.insert(0, str(_ROOT / "bench" / "panel"))

import store_side  # noqa: E402
import transcripts  # noqa: E402

#: Windows, in seconds, the search population is read at. The first is the
#: headline; the others show how much the reading depends on it.
WINDOWS = (600, 120, 1800)

#: A prompt with fewer content tokens than this names no topic to match
#: on. Three is the smallest count at which two terms can disagree with a
#: third; the receipt reports the token-count distribution beside it.
MIN_TOPIC_TOKENS = 3

#: A record opened this long after a search returned it is read as that
#: search's result; an explicit applied this long after the opening is read
#: as that opening's. Both are the Stop audit's lookback order of
#: magnitude; neither is tuned, and the search reading is also given at
#: three windows.
OPENED_WITHIN = 1800
APPLIED_WITHIN = 3600

RANK_BUCKETS = ((1, 3, "1-3"), (4, 10, "4-10"), (11, 50, "11-50"), (51, None, "51+"))


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _ids(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and isinstance(item.get("id"), str):
                out.append(item["id"])
    return out


def _text_of(content: Any) -> str | None:
    """A user entry's own words: a string, or the text blocks of a list
    that carries no tool result."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                return None
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts) if parts else None
    return None


def read_transcripts(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Search queries by the log's hash, and prompts by prompt id."""
    from bettermemory.events import redact_query

    queries: dict[str, str] = {}
    prompts: dict[str, str] = {}
    for path in sorted(root.rglob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            is_search = '"tool_use"' in line and "memory_search" in line
            is_prompt = '"promptId"' in line and '"type":"user"' in line
            if not (is_search or is_prompt):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            content = (entry.get("message") or {}).get("content")
            if entry.get("type") == "user":
                prompt_id = entry.get("promptId")
                if (
                    isinstance(prompt_id, str)
                    and prompt_id not in prompts
                    and not entry.get("isMeta")
                    and not entry.get("isSidechain")
                ):
                    text = _text_of(content)
                    if text:
                        prompts[prompt_id] = text
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if transcripts.tool_of(str(block.get("name") or "")) != "memory_search":
                    continue
                query = (block.get("input") or {}).get("query")
                if isinstance(query, str):
                    queries[redact_query(query)["hash"]] = query
    return queries, prompts


class Classifier:
    def __init__(self, memories: list[Any]) -> None:
        from bettermemory import search as _search

        self._search = _search
        self.memories = memories
        self.by_id = {m.id: m for m in memories}
        self._ranks: dict[str, dict[str, int]] = {}

    def content_tokens(self, text: str) -> set[str]:
        s = self._search
        return set(s._strip_stopwords(s._expand_kebab(s._lexicon.tokenize(text))))

    def record_tokens(self, record_id: str) -> set[str]:
        tokens = self._search._memory_tokens(self.by_id[record_id])
        return set(tokens.content) | set(tokens.scope_set)

    def rank_of(self, text: str, record_id: str) -> int | None:
        key = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
        ranks = self._ranks.get(key)
        if ranks is None:
            ranked = self._search.rank(self.memories, text)
            ranks = {rid: pos for pos, (rid, _matched) in enumerate(ranked, start=1)}
            self._ranks[key] = ranks
        return ranks.get(record_id)

    def classify(
        self, text: str, record_id: str, asked_at: datetime, width: int, *, hook: bool
    ) -> dict[str, Any]:
        memory = self.by_id.get(record_id)
        if memory is None:
            return {"class": "record_gone"}
        created = getattr(memory, "created", None)
        if isinstance(created, str):
            created = _ts(created)
        if isinstance(created, datetime) and created > asked_at:
            return {"class": "record_newer_than_ask"}
        ask = self.content_tokens(text)
        if hook and len(ask) < MIN_TOPIC_TOKENS:
            return {"class": "no_topic_words", "ask_tokens": len(ask)}
        shared = ask & self.record_tokens(record_id)
        rank = self.rank_of(text, record_id)
        out: dict[str, Any] = {
            "ask_tokens": len(ask),
            "shared_terms": len(shared),
            "rank": rank,
        }
        if not shared or rank is None:
            out["class"] = "no_shared_term"
        elif rank <= width:
            out["class"] = "ranked_within_width"
        else:
            out["class"] = "outranked"
        return out


def _bucket(rank: int | None) -> str:
    if rank is None:
        return "unranked"
    for low, high, name in RANK_BUCKETS:
        if rank >= low and (high is None or rank <= high):
            return name
    return "unranked"


def _applied_times(evs: list[dict[str, Any]]) -> dict[str, list[datetime]]:
    """When each record was explicitly marked applied, by whatever stamped
    it (the model, the Stop hook or the command line)."""
    out: dict[str, list[datetime]] = defaultdict(list)
    for ev in evs:
        if ev.get("kind") != "use" or ev.get("outcome") != "applied" or ev.get("auto"):
            continue
        at = _ts(ev.get("ts"))
        if at is None:
            continue
        for record_id in _ids(ev.get("ids")):
            out[record_id].append(at)
    return out


def _applied_after(
    applied: dict[str, list[datetime]], record_id: str, at: datetime
) -> bool:
    return any(
        0 <= (t - at).total_seconds() <= APPLIED_WITHIN
        for t in applied.get(record_id, ())
    )


def search_population(
    evs: list[dict[str, Any]],
    queries: dict[str, str],
    clf: Classifier,
    window: int,
    rows: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    searches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ev in evs:
        if ev.get("kind") == "search" and _ts(ev.get("ts")) is not None:
            searches[str(ev.get("session"))].append(ev)
    applied = _applied_times(evs)
    seen: set[tuple[str, str]] = set()
    counts: Counter[str] = Counter()
    classes: Counter[str] = Counter()
    classes_applied: Counter[str] = Counter()
    ranks: Counter[str] = Counter()
    for ev in evs:
        if ev.get("kind") != "show" or not isinstance(ev.get("id"), str):
            continue
        at = _ts(ev.get("ts"))
        if at is None:
            continue
        record_id = ev["id"]
        session = searches.get(str(ev.get("session")), [])
        hit_index = None
        for index in range(len(session) - 1, -1, -1):
            s_at = _ts(session[index].get("ts"))
            if s_at is None or s_at > at:
                continue
            if (at - s_at).total_seconds() > OPENED_WITHIN:
                break
            if record_id in _ids(session[index].get("returned")):
                hit_index = index
                break
        if hit_index is None:
            counts["opened_not_from_a_recent_search"] += 1
            continue
        hit = session[hit_index]
        key = (str(hit.get("ts")), record_id)
        if key in seen:
            continue
        seen.add(key)
        was_applied = _applied_after(applied, record_id, at)
        counts["opened_from_search"] += 1
        counts["opened_from_search_and_applied"] += was_applied
        prev = session[hit_index - 1] if hit_index > 0 else None
        hit_at = _ts(hit.get("ts"))
        prev_at = _ts(prev.get("ts")) if prev else None
        if (
            prev is None
            or prev_at is None
            or hit_at is None
            or (hit_at - prev_at).total_seconds() > window
            or record_id in _ids(prev.get("returned"))
        ):
            counts["first_ask_returned_it"] += 1
            counts["first_ask_returned_it_and_applied"] += was_applied
            continue
        counts["asked_again"] += 1
        logged = prev.get("query")
        digest = logged.get("hash") if isinstance(logged, dict) else None
        text = queries.get(str(digest))
        if text is None and isinstance(logged, str):
            text = logged
        if text is None:
            counts["asked_again_query_not_in_transcripts"] += 1
            continue
        width = prev.get("max_results")
        verdict = clf.classify(
            text, record_id, prev_at, width if isinstance(width, int) else 5, hook=False
        )
        classes[verdict["class"]] += 1
        if was_applied:
            classes_applied[verdict["class"]] += 1
        if verdict["class"] == "outranked":
            ranks[_bucket(verdict.get("rank"))] += 1
        if rows is not None:
            hit_logged = hit.get("query")
            hit_digest = (
                hit_logged.get("hash") if isinstance(hit_logged, dict) else None
            )
            rows.append(
                {
                    "population": "search",
                    "record": record_id,
                    "applied": was_applied,
                    "missed_ask": text,
                    "ask_that_returned_it": queries.get(str(hit_digest)),
                    "seconds_between": (hit_at - prev_at).total_seconds(),
                    **verdict,
                }
            )
    return {
        "window_seconds": window,
        "counts": dict(counts),
        "first_ask_rate": store_side._rate(
            counts["first_ask_returned_it"], counts["opened_from_search"]
        ),
        "first_ask_rate_applied": store_side._rate(
            counts["first_ask_returned_it_and_applied"],
            counts["opened_from_search_and_applied"],
        ),
        "classes": dict(classes.most_common()),
        "classes_applied": dict(classes_applied.most_common()),
        "outranked_by_rank": dict(ranks),
    }


def hook_population(
    evs: list[dict[str, Any]],
    prompts: dict[str, str],
    clf: Classifier,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    recalls: dict[str, list[dict[str, Any]]] = defaultdict(list)
    searches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    shows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ev in evs:
        kind = ev.get("kind")
        if kind == "prompt_recall" and isinstance(ev.get("session_id"), str):
            recalls[ev["session_id"]].append(ev)
        elif kind == "search" and isinstance(ev.get("harness_session"), str):
            searches[ev["harness_session"]].append(ev)
        elif kind == "show" and isinstance(ev.get("harness_session"), str):
            shows[ev["harness_session"]].append(ev)
    applied = _applied_times(evs)
    counts: Counter[str] = Counter()
    classes: Counter[str] = Counter()
    classes_applied: Counter[str] = Counter()
    ranks: Counter[str] = Counter()

    def inside(ev: dict[str, Any], start: datetime, end: datetime | None) -> bool:
        at = _ts(ev.get("ts"))
        return at is not None and at >= start and (end is None or at < end)

    for session_id, turns in recalls.items():
        for index, turn in enumerate(turns):
            start = _ts(turn.get("ts"))
            if start is None:
                continue
            end = _ts(turns[index + 1].get("ts")) if index + 1 < len(turns) else None
            counts["recall_turns"] += 1
            shown = set(_ids(turn.get("shown_ids")))
            returned: set[str] = set()
            for search in searches.get(session_id, []):
                if inside(search, start, end):
                    returned.update(_ids(search.get("returned")))
            opened: dict[str, None] = {}
            for show in shows.get(session_id, []):
                if inside(show, start, end) and isinstance(show.get("id"), str):
                    opened[show["id"]] = None
            counts["turns_with_a_record_opened"] += bool(opened)
            for record_id in opened:
                was_applied = _applied_after(applied, record_id, start)
                if record_id in shown:
                    counts["opened_and_the_recall_had_shown_it"] += 1
                    counts["opened_shown_and_applied"] += was_applied
                    continue
                if record_id not in returned:
                    counts["opened_from_elsewhere"] += 1
                    continue
                counts["opened_from_search_not_shown_by_the_recall"] += 1
                counts["opened_not_shown_and_applied"] += was_applied
                text = prompts.get(str(turn.get("prompt_id")))
                if text is None:
                    counts["not_shown_prompt_not_in_transcripts"] += 1
                    continue
                verdict = clf.classify(text, record_id, start, 5, hook=True)
                classes[verdict["class"]] += 1
                if was_applied:
                    classes_applied[verdict["class"]] += 1
                if verdict["class"] in ("outranked", "ranked_within_width"):
                    ranks[_bucket(verdict.get("rank"))] += 1
                rows.append(
                    {
                        "population": "hook",
                        "record": record_id,
                        "applied": was_applied,
                        "missed_ask": text[:600],
                        "shown": len(shown),
                        **verdict,
                    }
                )
    return {
        "counts": dict(counts),
        "classes": dict(classes.most_common()),
        "classes_applied": dict(classes_applied.most_common()),
        "shared_term_misses_by_rank": dict(ranks),
    }


def _share(classes: dict[str, int], name: str) -> dict[str, Any]:
    classed = sum(
        n
        for c, n in classes.items()
        if c not in ("record_gone", "record_newer_than_ask")
    )
    return store_side._rate(classes.get(name, 0), classed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--copy-dir", type=Path, required=True)
    parser.add_argument("--keys-dir", type=Path)
    parser.add_argument("--transcripts", type=Path, default=transcripts.default_root())
    parser.add_argument("--out", type=Path)
    parser.add_argument("--private-out", type=Path)
    args = parser.parse_args(argv)
    if args.private_out is not None:
        private = args.private_out.expanduser().resolve()
        if _ROOT in private.parents or private == _ROOT:
            parser.error("--private-out must lie outside the repository")

    from scratch_store import scratch_store

    keys = args.keys_dir or args.copy_dir.parent / "keys"
    store = scratch_store(args.copy_dir, keys)
    memories = list(store.load_all())
    conn = store_side.open_copy(args.copy_dir / "memory.sqlite")
    try:
        evs = store_side.events(conn)
    finally:
        conn.close()
    queries, prompts = read_transcripts(args.transcripts)
    clf = Classifier(memories)

    rows: list[dict[str, Any]] = []
    by_window = [
        search_population(
            evs, queries, clf, window, rows if window == WINDOWS[0] else None
        )
        for window in WINDOWS
    ]
    hook = hook_population(evs, prompts, clf, rows)
    head = by_window[0]
    audit = Counter(
        str(ev.get("verdict")) for ev in evs if ev.get("kind") == "turn_audited"
    )
    receipt = {
        "program": "bench/recall/miss_analysis.py",
        "program_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "commit": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip(),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "store": {
            "records": len(memories),
            "events": len(evs),
            "first_event": evs[0].get("ts") if evs else None,
            "last_event": evs[-1].get("ts") if evs else None,
        },
        "transcripts": {"search_queries": len(queries), "prompts": len(prompts)},
        "audit_rows": {
            "verdicts": dict(audit),
            "note": "the audit's probe is lexical, so its misses share words "
            "with the turn by construction and cannot show a vocabulary gap",
        },
        "search": {"headline": head, "other_windows": by_window[1:]},
        "hook": hook,
        "predictions": {
            "P1_no_shared_term_share_of_search_misses_under_0.25": _share(
                head["classes"], "no_shared_term"
            ),
            "P2_no_shared_term_share_of_hook_misses_under_0.25": _share(
                hook["classes"], "no_shared_term"
            ),
            "P3_first_ask_rate_at_least_0.70": head["first_ask_rate"],
        },
        "caveats": [
            "need is read from what the model opened, so a record no ask ever "
            "reached is not counted in either population",
            "no_shared_term holds vocabulary gaps and asks about another topic "
            "alike; the private rows carry both asks for a reader to separate them",
            "records are classed as they are in the copy, not as they were at the ask",
        ],
    }
    text = json.dumps(receipt, indent=2, sort_keys=False)
    print(text)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    if args.private_out is not None:
        private = args.private_out.expanduser()
        private.mkdir(parents=True, exist_ok=True)
        (private / "rows.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
