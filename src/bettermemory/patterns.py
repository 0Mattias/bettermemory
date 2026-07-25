"""Cross-episode pattern candidates: themes recurring across sessions.

`episode_promote` consolidates ONE episode into a durable memory — a
judgment a session makes about its own journal entry. What no single
session can see is a theme recurring ACROSS sessions: four different
days each leaving an episode about the same flaky proxy is exactly the
kind of fact that should graduate to semantic memory, and exactly the
kind nobody promotes because each individual entry looks too minor.

This module is the mechanical half: tokenize every live episode,
find distinctive terms that recur across >= `min_sessions` DISTINCT
sessions, cluster terms that name the same episode group, and emit
ranked `PatternCandidate`s. The judgment half stays with the model —
the `episode_patterns` MCP tool lists candidates with per-episode
snippets, and the model either promotes one (authoring the synthesis
body itself; the write routes through the full memory_write gate
stack) or dismisses it.

Detection is deliberately conservative:

- floors and empty bodies are excluded (they're anchors, not content);
- a term must appear in >= `min_sessions` distinct sessions AND >= 3
  episodes — two mentions is coincidence, and one chatty session
  repeating itself is one data point, not a pattern;
- a term present in more than `_UBIQUITY_CEILING` of all episodes is
  ambient vocabulary ("bettermemory" in a bettermemory repo), not a
  pattern;
- candidates are capped and ranked by session spread, so the surface
  stays reviewable.

Dismissals persist in ``<root>/.episode_patterns.jsonl`` keyed by a
content-stable pattern id (hash of the member episode ids). A
dismissed pattern stays gone — until a NEW episode joins the cluster,
which changes the member set, which changes the id: fresh evidence
legitimately reopens the question. Rows whose members have all aged
out (episodes TTL at ~30 days) are GC'd opportunistically on read —
under the same exclusive flock the read's snapshot is taken beneath,
so a peer's concurrent `dismiss()` can't be rewritten away.

Promotion synergy: promoting a pattern whose fact is ALREADY stored
dedup-rejects through the normal write path — which now records a
corroboration on the existing memory. The recurrence signal lands
either way.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._fsutil import atomic_write_bytes, flock_excl
from .models import Episode, utcnow
from .search import _strip_stopwords, _tokenize_unstemmed

log = logging.getLogger("bettermemory.patterns")

PATTERNS_FILENAME = ".episode_patterns.jsonl"

# A term in more than this fraction of all live episodes is project
# vocabulary, not a recurring theme worth consolidating.
_UBIQUITY_CEILING = 0.6
# Two term-clusters whose episode-member sets overlap at least this much
# (Jaccard) describe the same pattern and merge.
_MEMBER_JACCARD_MERGE = 0.6
_MIN_TERM_LEN = 3
_MIN_EPISODES = 3
_MAX_SNIPPETS_PER_PATTERN = 8


def _pattern_id(member_ids: list[str]) -> str:
    joined = ":".join(sorted(member_ids))
    return "pat-" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


@dataclass
class PatternCandidate:
    """One recurring cross-session theme, awaiting promote-or-dismiss.

    `terms` are the shared distinctive tokens that bound the cluster —
    evidence pointers, not a synthesis (the model authors the actual
    memory body at promote time). `snippets` carry one line per member
    episode (takeaway when present, else the body's first line) so the
    surface is judgeable without N `episode_search` round-trips."""

    id: str
    terms: list[str]
    episode_ids: list[str]
    session_ids: list[str]
    snippets: list[dict[str, str]]
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "terms": self.terms,
            "episode_ids": self.episode_ids,
            "distinct_sessions": len(self.session_ids),
            "snippets": self.snippets,
            "score": round(self.score, 3),
        }


def _episode_snippet(ep: Episode) -> str:
    text = (ep.takeaway or "").strip()
    if not text:
        text = ep.body.strip().splitlines()[0] if ep.body.strip() else ""
    return text[:200]


def _distinctive_tokens(ep: Episode) -> set[str]:
    # Unstemmed on purpose: these tokens double as the SURFACED `terms`
    # on candidates, and a model reading "websocket, caddy" judges
    # faster than one reading stems ("websocke", "caddi"). Cross-episode
    # matching loses plural folding, which for recurring-theme detection
    # is an acceptable trade — themes recur in the same spelling far
    # more often than they alternate number.
    text = f"{ep.takeaway or ''} {ep.body}"
    tokens = _strip_stopwords(_tokenize_unstemmed(text))
    return {t for t in tokens if len(t) >= _MIN_TERM_LEN and not t.isdigit()}


def clusterable_episodes(episodes: list[Episode]) -> list[Episode]:
    """The subset of `episodes` detection can actually cluster.

    `find_episode_patterns` drops two shapes before it does anything
    else, and this is that filter: floors (the session-tag anchors
    `episode_handoff` writes — fixed marker body, `takeaway=None`) and
    episodes whose body is blank. The body test is deliberate rather
    than a "has no tokens" test: a blank-bodied episode can still carry
    a takeaway (`EpisodeStore.write` refuses an empty body, but a legacy
    or hand-edited file can hold one), and detection counts it as an
    anchor rather than content either way.

    Exported rather than inlined at that one call site because the
    `episode_patterns` handler reports the SIZE of this subset back to
    the caller as `episodes_clustered`. A number described as "what
    detection clustered over" and the detector's actual input have to be
    the same predicate, not two copies of it that can drift apart — the
    handler used to report the visible pool (larger by every floor and
    blank-bodied episode in it) under exactly that description.
    """
    return [ep for ep in episodes if not ep.is_floor and ep.body.strip()]


def find_episode_patterns(
    episodes: list[Episode],
    *,
    min_sessions: int = 3,
    max_patterns: int = 5,
) -> list[PatternCandidate]:
    """Pure detection over the given episodes. No queue I/O — the
    handler filters against persisted dismissals."""
    live = clusterable_episodes(episodes)
    if len(live) < _MIN_EPISODES:
        return []

    token_sets: dict[str, set[str]] = {}
    by_id: dict[str, Episode] = {}
    for ep in live:
        by_id[ep.id] = ep
        token_sets[ep.id] = _distinctive_tokens(ep)

    # term -> member episode ids; keep terms with enough session spread.
    members_by_term: dict[str, set[str]] = {}
    for eid, toks in token_sets.items():
        for tok in toks:
            members_by_term.setdefault(tok, set()).add(eid)

    def _collect(apply_ubiquity_ceiling: bool) -> list[tuple[str, set[str], int]]:
        ubiquity_cap = max(_MIN_EPISODES, int(len(live) * _UBIQUITY_CEILING))
        collected: list[tuple[str, set[str], int]] = []
        for term, member_ids in members_by_term.items():
            if len(member_ids) < _MIN_EPISODES:
                continue
            if apply_ubiquity_ceiling and len(member_ids) > ubiquity_cap:
                continue
            sessions = {by_id[eid].session_id for eid in member_ids}
            if len(sessions) < min_sessions:
                continue
            collected.append((term, member_ids, len(sessions)))
        return collected

    candidate_terms = _collect(apply_ubiquity_ceiling=True)
    if not candidate_terms:
        # The ceiling exists to keep project vocabulary ("bettermemory"
        # in a bettermemory repo) from fusing distinct topics into one
        # mega-pattern — which presupposes there ARE sub-clusters under
        # the ubiquitous terms. When the ceiling filters EVERYTHING out,
        # the journal is monothematic: every qualifying term spans most
        # of it because the whole journal keeps circling one theme.
        # That's the strongest possible pattern, not vocabulary — retry
        # without the ceiling rather than reporting silence.
        candidate_terms = _collect(apply_ubiquity_ceiling=False)

    # Deterministic order: widest session spread first, then most
    # member episodes, then lexical. The first unclaimed term seeds a
    # pattern; later terms whose member sets substantially overlap merge
    # into it rather than spawning near-duplicate patterns.
    candidate_terms.sort(key=lambda t: (-t[2], -len(t[1]), t[0]))
    patterns: list[tuple[list[str], set[str], int]] = []
    for term, member_ids, session_count in candidate_terms:
        merged = False
        for existing in patterns:
            _, existing_members, _ = existing
            inter = len(member_ids & existing_members)
            union = len(member_ids | existing_members)
            if union and inter / union >= _MEMBER_JACCARD_MERGE:
                existing[0].append(term)
                merged = True
                break
        if not merged:
            patterns.append(([term], set(member_ids), session_count))

    out: list[PatternCandidate] = []
    for terms, member_ids, session_count in patterns[: max_patterns * 2]:
        members = sorted(member_ids)
        sessions = sorted({by_id[eid].session_id for eid in members})
        eps = sorted((by_id[eid] for eid in members), key=lambda e: e.created)
        snippets = [
            {
                "episode_id": ep.id,
                "session_id": ep.session_id,
                "created": ep.created.isoformat(),
                "snippet": _episode_snippet(ep),
            }
            for ep in eps[:_MAX_SNIPPETS_PER_PATTERN]
        ]
        out.append(
            PatternCandidate(
                id=_pattern_id(members),
                terms=sorted(terms)[:6],
                episode_ids=members,
                session_ids=sessions,
                snippets=snippets,
                score=session_count + 0.1 * len(terms),
            )
        )
    out.sort(key=lambda p: (-p.score, p.id))
    return out[:max_patterns]


@dataclass
class PatternDismissals:
    """Persisted dismissals, keyed by content-stable pattern id."""

    root: Path
    _loaded: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()

    @property
    def path(self) -> Path:
        return self.root / PATTERNS_FILENAME

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(raw, dict) and "id" in raw:
                out.append(raw)
        return out

    def dismissed_ids(self, live_episode_ids: set[str]) -> set[str]:
        """Ids to filter from the candidate list. GCs rows whose member
        episodes have ALL aged out — the pattern can never recur with
        that exact member set, so the row is dead weight.

        LOCK THEN LOAD THEN REWRITE, in that order — the same shape
        `ConflictQueue.upsert_scan` uses, and for the same reason. This
        method both reads and (opportunistically) REWRITES the whole
        file, so the snapshot it rewrites from has to be taken under the
        lock that guards the rewrite. Loading first and locking only
        around the write left a window in which a peer process's
        `dismiss()` could append a row after our snapshot and before our
        lock: the rewrite then persisted the stale pre-lock view and the
        peer's dismissal vanished, silently resurfacing a pattern the
        user had just hidden.

        The lock is now taken on every call, not only when the GC has
        work. That is deliberate: deciding whether the GC fires requires
        the loaded rows, so any "peek first, lock only if needed" shape
        reintroduces exactly the load-outside-the-lock window this
        docstring exists to close.
        """
        with flock_excl(self.path):
            rows = self.load()
            kept = [
                r
                for r in rows
                if any(mid in live_episode_ids for mid in r.get("member_ids", []))
            ]
            if len(kept) != len(rows):
                self._write_all_locked(kept)
        return {str(r["id"]) for r in kept}

    def dismiss(self, pattern_id: str, member_ids: list[str]) -> None:
        with flock_excl(self.path):
            rows = self.load()
            if any(r.get("id") == pattern_id for r in rows):
                return
            rows.append(
                {
                    "id": pattern_id,
                    "member_ids": member_ids,
                    "dismissed_at": utcnow().isoformat(),
                }
            )
            self._write_all_locked(rows)

    def _write_all_locked(self, rows: list[dict[str, Any]]) -> None:
        body = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows)
        atomic_write_bytes(self.path, body.encode("utf-8"), mode_before_rename=0o600)


__all__ = [
    "PATTERNS_FILENAME",
    "PatternCandidate",
    "PatternDismissals",
    "clusterable_episodes",
    "find_episode_patterns",
]
