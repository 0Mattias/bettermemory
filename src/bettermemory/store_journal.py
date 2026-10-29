"""The `Store` methods of the episode journal.

The episode puts and reads (`put_episode`, `write_episode`, `write_floor`,
`episodes_by_session`, `episodes_by_swarm` ...), the TTL prune
(`prunable_episode_sessions`, the predicate the prune and the CLI's dry
run share, and `prune_episode_sessions`) and the health report's gauge
(`episode_volume`). Each is a function of `self: Store` that `Store` binds
by name. A test that patches `_row_to_episode` or `utcnow` for these
readers patches this module; `store_records.utcnow` does not reach them.
It lives apart from `store.py` to keep each module under the package's
2,000-line bound (ENG-96); it imports `store_schema` and `store_rows`,
and `store` only for type checking.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .models import Episode, generate_ulid, utcnow
from .origin import Origin
from .store_schema import (
    DEFAULT_EPISODE_TTL_DAYS,
    EpisodeVolume,
    NotFoundError,
    RecordHome,
)
from .store_rows import _iso, _row_to_episode

if TYPE_CHECKING:
    from .store import Store


# -- episodes -----------------------------------------------------------


def put_episode(
    self: Store,
    episode: Episode,
    *,
    session: str | None = None,
    home: RecordHome | None = None,
) -> Episode:
    """Insert or replace the episode; ``home`` writes its ``homes`` row
    in the same transaction, as ``put_memory`` does."""
    payload = {"episode": episode.model_dump(mode="json")}
    with self._transaction() as tx:
        home_row = self._home_row(episode.id, "episode", home)
        self._mutate(tx, "episode_put", payload, session=session)
        if home_row is not None:
            self._mutate(tx, "home_put", {"home": home_row}, session=session)
    return episode


def get_episode(self: Store, episode_id: str) -> Episode:
    row = self._conn.execute(
        "SELECT * FROM episodes WHERE id = ?", (episode_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(episode_id)
    return _row_to_episode(row)


def iter_episodes(self: Store, *, session_id: str | None = None) -> Iterator[Episode]:
    if session_id is None:
        cursor = self._conn.execute("SELECT * FROM episodes ORDER BY rowid")
    else:
        cursor = self._conn.execute(
            "SELECT * FROM episodes WHERE session_id = ? ORDER BY rowid",
            (session_id,),
        )
    for row in cursor:
        yield _row_to_episode(row)


def count_episodes(self: Store) -> int:
    return int(self._conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])


def delete_episode(self: Store, episode_id: str, *, session: str | None = None) -> None:
    with self._transaction() as tx:
        self._require_row(tx, "episodes", "id", episode_id)
        self._mutate(tx, "episode_delete", {"id": episode_id}, session=session)


# -- the journal ------------------------------------------------------------


def write_episode(
    self: Store,
    *,
    session_id: str,
    body: str,
    scopes: list[str] | None = None,
    takeaway: str | None = None,
    swarm_id: str | None = None,
    origin: Origin | None = None,
    now: datetime | None = None,
    home: RecordHome | None = None,
) -> Episode:
    """Append one journal entry for `session_id`, with its home when
    `home` is given (`put_episode`)."""
    if not body or not body.strip():
        raise ValueError("episode body must be a non-empty string")
    episode = Episode(
        id=generate_ulid(),
        session_id=session_id,
        created=now or utcnow(),
        body=body.strip() + "\n",
        scopes=list(scopes or []),
        takeaway=takeaway.strip() if takeaway else None,
        swarm_id=swarm_id,
        origin=origin,
    )
    return self.put_episode(episode, session=session_id, home=home)


def write_floor(
    self: Store,
    *,
    session_id: str,
    origin: Origin | None = None,
    now: datetime | None = None,
    home: RecordHome | None = None,
) -> Episode:
    """The session-tag floor the handoff writes at its entry, so a
    session that crashes before its first takeaway still marks its
    worktree; homed like any episode when `home` is given."""
    episode = Episode(
        id=generate_ulid(),
        session_id=session_id,
        created=now or utcnow(),
        body="(session-tag floor, no takeaway recorded)\n",
        scopes=[],
        takeaway=None,
        origin=origin,
        is_floor=True,
    )
    return self.put_episode(episode, session=session_id, home=home)


def episodes_by_session(self: Store, session_id: str) -> list[Episode]:
    out = list(self.iter_episodes(session_id=session_id))
    out.sort(key=lambda e: e.created)
    return out


def episodes_by_swarm(self: Store, swarm_id: str) -> list[Episode]:
    out = [
        _row_to_episode(row)
        for row in self._conn.execute(
            "SELECT * FROM episodes WHERE swarm_id = ? ORDER BY created, rowid",
            (swarm_id,),
        )
    ]
    return out


def episode_session_ids(self: Store) -> list[str]:
    """Every session with at least one episode, oldest first."""
    return [
        str(row["session_id"])
        for row in self._conn.execute(
            "SELECT session_id, MIN(rowid) AS first FROM episodes "
            "GROUP BY session_id ORDER BY first"
        )
    ]


def prunable_episode_sessions(
    self: Store,
    *,
    ttl_days: int = DEFAULT_EPISODE_TTL_DAYS,
    now: datetime | None = None,
) -> list[str]:
    """Sessions whose newest episode is older than the TTL."""
    if ttl_days <= 0:
        return []
    cutoff = _iso((now or utcnow()) - timedelta(days=ttl_days))
    return [
        str(row["session_id"])
        for row in self._conn.execute(
            "SELECT session_id, MAX(created) AS newest FROM episodes "
            "GROUP BY session_id HAVING newest < ? ORDER BY newest",
            (cutoff,),
        )
    ]


def prune_episode_sessions(
    self: Store,
    *,
    ttl_days: int = DEFAULT_EPISODE_TTL_DAYS,
    keep_session_id: str | None = None,
    now: datetime | None = None,
    session: str | None = None,
) -> list[str]:
    """Delete every episode of the sessions past the TTL, except the
    session named by `keep_session_id`. Returns the pruned session
    ids. The sessions are pruned in chunks (`_in_chunks`), each one's
    newest episode read again under its chunk's write lock, so a
    session that gained an episode meanwhile is kept whole. With no
    session past the TTL it takes no lock.

    A failure part way raises, and the ids are not returned: the chunks
    committed before it stay committed (those sessions are gone), the
    failing chunk is rolled back, and the rest are still there.
    Running it again prunes what is left."""
    if ttl_days <= 0:
        return []
    cutoff = _iso((now or utcnow()) - timedelta(days=ttl_days))
    candidates = [
        sid
        for sid in self.prunable_episode_sessions(ttl_days=ttl_days, now=now)
        if sid != keep_session_id
    ]
    pruned: list[str] = []

    def prune(sid: str) -> None:
        newest = self._conn.execute(
            "SELECT MAX(created) FROM episodes WHERE session_id = ?", (sid,)
        ).fetchone()[0]
        if newest is None or str(newest) >= cutoff:
            return
        episode_ids = [
            str(row[0])
            for row in self._conn.execute(
                "SELECT id FROM episodes WHERE session_id = ? ORDER BY rowid",
                (sid,),
            )
        ]
        with self._transaction() as tx:
            for episode_id in episode_ids:
                self._mutate(tx, "episode_delete", {"id": episode_id}, session=session)
        pruned.append(sid)

    self._in_chunks(candidates, prune)
    return pruned


def episode_volume(
    self: Store,
    *,
    ttl_days: int = DEFAULT_EPISODE_TTL_DAYS,
    now: datetime | None = None,
) -> EpisodeVolume:
    row = self._conn.execute(
        "SELECT COUNT(DISTINCT session_id) AS sessions, COUNT(*) AS episodes, "
        "COALESCE(SUM(LENGTH(CAST(body AS BLOB)) + "
        "LENGTH(CAST(COALESCE(takeaway, '') AS BLOB))), 0) AS bytes "
        "FROM episodes"
    ).fetchone()
    return EpisodeVolume(
        sessions=int(row["sessions"]),
        episodes=int(row["episodes"]),
        bytes=int(row["bytes"]),
        prunable_sessions=len(
            self.prunable_episode_sessions(ttl_days=ttl_days, now=now)
        ),
        ttl_days=ttl_days,
    )
