"""Reciprocal-rank fusion of the bettermemory engine and a plain FTS5 index,
behind the same Add/Search interface, for measurement.

WHY. Offline, at a 90,000-character budget, a plain any-word FTS5 query
served slightly MORE labeled evidence than the engine on all three of
AML's datasets run here (LoCoMo 0.968 vs 0.947, BEAM-1M 0.421 vs 0.413,
LongMemEval-S dev 0.969 vs 0.962), while the engine answered better on two
of them: the engine orders the top well and the broad OR query reaches
stragglers further down. Fusing the two rankings served the most evidence
everywhere (0.983, 0.427, 0.969). Whether that turns into answers is what
this class exists to measure; nothing here is shipped until it does.

Both stores ingest the same rounds; a round is identified across them by
its normalized content, since each store mints its own ids. Fusion is
standard RRF (k = 60) over each ranking to `depth`, then the same
character budget the engine arm serves under.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from aml.fts import FtsService
from aml.service import MemoryService

_SPACE = re.compile(r"\s+")


def _key(content: str) -> str:
    return _SPACE.sub(" ", content).strip().lower()[:300]


class FusedService:
    def __init__(
        self,
        engine_root: Path,
        fts_root: Path,
        *,
        budget: int = 0,
        depth: int = 200,
        rrf_k: int = 60,
    ) -> None:
        self.engine = MemoryService(engine_root, serve=depth)
        self.fts = FtsService(fts_root, serve=depth)
        self.budget = budget
        self.depth = depth
        self.rrf_k = rrf_k
        self._session: dict[str, str] = {}

    def add(
        self,
        request_id: str,
        user_id: str,
        messages: list[dict[str, Any]],
        session_id: str = "",
    ) -> int:
        n = self.engine.add(request_id, user_id, messages, session_id)
        self.fts.add(request_id, user_id, messages, session_id)
        return n

    def search(self, user_id: str, query: str, top_k: int) -> list[dict[str, Any]]:
        eng = self.engine.search(user_id, query, self.depth)
        fts = self.fts.search(user_id, query, self.depth)
        eng_sess = self.engine.sessions_for(user_id, [h["id"] for h in eng])
        fts_sess = self.fts.sessions_for(user_id, [h["id"] for h in fts])
        score: dict[str, float] = {}
        keep: dict[str, dict[str, Any]] = {}
        for ranked, sessions in ((eng, eng_sess), (fts, fts_sess)):
            for rank, (hit, sess) in enumerate(zip(ranked, sessions)):
                key = _key(hit["content"])
                score[key] = score.get(key, 0.0) + 1.0 / (self.rrf_k + rank + 1)
                if key not in keep:
                    keep[key] = hit
                    self._session[hit["id"]] = sess
        order = sorted(score, key=lambda k: score[k], reverse=True)
        out: list[dict[str, Any]] = []
        used = 0
        for key in order:
            if len(out) >= top_k:
                break
            hit = dict(keep[key])
            hit["score"] = round(score[key], 6)
            n = len(hit["content"])
            if self.budget and out and used + n > self.budget:
                continue
            used += n
            out.append(hit)
        return out

    def sessions_for(self, user_id: str, ids: list[str]) -> list[str]:
        return [self._session.get(i, "") for i in ids]
