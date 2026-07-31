"""Episode storage — sibling to `store.Store` for journal-shaped entries.

Episodes are the run-state / iteration-takeaway primitive Memory's
durability gate (`durability.TRANSIENT_PHRASE_MARKERS`) explicitly
rejects. They give /loop iterations and subagents a home for "what we
tried", "what worked", "what the prior iteration concluded" — content
that's transient by design but needs to survive one context reset.

On-disk layout::

    <root>/episodes/<session_id>/<ulid>.md

The session-id-keyed directory is what makes `episode_handoff` cheap
(list one dir, read takeaways, return) and `prune_old_sessions` cheap
(stat session dirs, drop ones whose newest mtime is past the TTL).

Episode CONTENT is deliberately excluded from `memory_search`,
`memory_health`, `memory_list`, and `Store.load_all` — they live in a
sibling subtree, so the existing iteration helpers never see them. The
one exception is aggregate volume: `memory_health` reports
`episode_volume` (`EpisodeStore.volume`), a stat-only count of sessions,
episodes, bytes and past-TTL directories. No body, no takeaway, no
scopes — just how big the subtree has grown, because GC only fires on
write and a read-only loop would otherwise grow it silently.
"""

from __future__ import annotations

import shutil
import stat
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from . import _frontmatter as frontmatter
from ._fsutil import atomic_write_bytes, flock_excl, fsync_dir
from .models import (
    Episode,
    SCHEMA_VERSION,
    generate_ulid,
    utcnow,
)
from .origin import Origin
from .time_utils import ensure_utc, parse_event_ts


EPISODES_DIR = "episodes"

# Default TTL for an episode directory. Sessions whose newest episode
# is older than this get pruned on the next write. 30 days is the same
# window `compute_health` uses for `window_days`, so the curation
# horizon stays consistent across primitives.
DEFAULT_EPISODE_TTL_DAYS = 30


@dataclass(frozen=True)
class EpisodeVolume:
    """How big the episode subtree is — the journal-growth gauge.

    A volume reading, NOT a read of the journal: it costs one `iterdir`
    per session directory plus one `stat` per file and parses ZERO
    frontmatter. That is the whole point. `memory_health` attaches this
    so a curation pass can see the subtree growing without anyone paying
    `list_by_session`'s per-file `frontmatter.load`.

    `prunable_sessions` is the actionable field. Episode GC
    (`prune_old_sessions`) fires ONLY on `episode_write` and the
    `bettermemory episodes prune` CLI — so a read-only loop, one that
    calls `episode_handoff` / `episode_search` and never writes, never
    collects anything and the subtree grows unbounded with nothing
    reporting it. A non-zero count here is that missing report: "N
    session directories are already collectable; the next `episode_write`
    (or one CLI prune) takes them."

    It predicts `prune_old_sessions` rather than sharing its body — that
    method's two branches are wrapped in per-session flocks and rmtree
    recovery it would be wrong to run from a read-only gauge. The
    predicate itself IS shared, via `prunable_session_ids`, and
    `tests/test_episode_volume_rollup.py` pins the two against each
    other. One deliberate divergence: `prune_old_sessions(keep_session_id=…)`
    exempts the live session and the gauge has no way to know which one
    that is, so a currently-active-but-idle session can be counted
    prunable while the write path would spare it.
    """

    sessions: int
    episodes: int
    bytes: int
    prunable_sessions: int
    ttl_days: int

    def to_dict(self) -> dict[str, int]:
        return {
            "sessions": self.sessions,
            "episodes": self.episodes,
            "bytes": self.bytes,
            "prunable_sessions": self.prunable_sessions,
            "ttl_days": self.ttl_days,
        }


@dataclass
class EpisodeStore:
    """An episode store rooted at `<root>/episodes/`.

    `root` is the memory root (same one `Store` uses). The episode
    subdirectory is created lazily on first write — a fresh install
    that never touches episodes incurs no directory creation.
    """

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()
        # Don't create the directory eagerly. `Store.__post_init__` already
        # made `root` exist; the episodes subdir is created on first write
        # so a fresh install with no episodes doesn't leave an empty dir.

    @property
    def episodes_dir(self) -> Path:
        return self.root / EPISODES_DIR

    def _session_dir(self, session_id: str) -> Path:
        # Filesystem-safe: ULID / session-id are alphanumeric + underscore.
        # Reject anything else so a hostile session_id can't traverse out
        # of the episodes subtree.
        if not session_id or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for c in session_id
        ):
            raise ValueError(f"invalid session_id for episode storage: {session_id!r}")
        return self.episodes_dir / session_id

    # ---- write ------------------------------------------------------------

    def write(
        self,
        *,
        session_id: str,
        body: str,
        scopes: list[str] | None = None,
        takeaway: str | None = None,
        swarm_id: str | None = None,
        origin: Origin | None = None,
        now: datetime | None = None,
    ) -> Episode:
        """Append a new episode under `<root>/episodes/<session_id>/`.

        `swarm_id`, when set, tags this episode as part of a multi-agent
        swarm cohort (the coordinator's id) so `list_by_swarm` can fan
        in every sub-agent's episodes later. The episode still lives
        under the writer's own `session_id` directory; swarm_id is a
        cross-cutting label, not a routing key.
        """
        if not body or not body.strip():
            raise ValueError("episode body must be a non-empty string")
        created = now or utcnow()
        episode = Episode(
            id=generate_ulid(),
            session_id=session_id,
            created=created,
            body=body.strip() + "\n",
            scopes=list(scopes or []),
            takeaway=takeaway.strip() if takeaway else None,
            swarm_id=swarm_id,
            origin=origin,
        )
        self._persist_episode(episode)
        return episode

    def write_floor(
        self,
        *,
        session_id: str,
        origin: Origin | None = None,
        now: datetime | None = None,
    ) -> Episode:
        """Write a session-tag floor episode (E2 crash-recovery anchor).

        Floors are tiny journal-anchor episodes the `episode_handoff`
        handler writes at its own entry, BEFORE recording the handoff
        event, so a tick that crashes between handoff entry and
        `episode_write` still leaves the current session's worktree
        tag on disk. The next tick's handoff resolves this session's
        id via the event log, calls `list_by_session(sid)`, sees the
        floor episode, and the worktree filter on `_worktrees_equal_
        strict` matches the caller's worktree against the floor's
        captured `origin.worktree_root` — so the prior-session resolution
        adopts the crashed tick instead of silently walking past it
        to the tick-before-last.

        Floors carry empty body / empty takeaway / empty scopes and
        the `is_floor=True` marker. Reuses the same atomic write
        discipline (`_persist_episode`) as `write` — tmp+fchmod+fsync
        +rename+fsync_dir+per-session flock — so the durability and
        concurrency contracts are identical between the two write
        paths. The on-disk `body` is the literal floor marker text
        (still non-empty so `_persist_episode`'s file-shape invariants
        hold) but consumers branch on the `is_floor` flag, not on
        body inspection.

        Callers (handler layer) are expected to gate this call on a
        cheap `list_by_session` emptiness check first — a single
        floor per session is the goal, not one floor per handoff
        invocation. The handler also catches the case where a real
        takeaway has already been written into this session by a
        prior episode_write (then the floor is unnecessary).
        """
        created = now or utcnow()
        episode = Episode(
            id=generate_ulid(),
            session_id=session_id,
            created=created,
            # Non-empty body to satisfy the on-disk file-shape
            # invariant (`_write_path` writes the body verbatim into
            # the post content, downstream readers tolerate it). The
            # marker text is descriptive so a human walking the
            # journal manually can immediately distinguish a floor
            # from a real takeaway; consumers branching on the
            # `is_floor` flag never inspect this string.
            body="(session-tag floor — no takeaway recorded)\n",
            scopes=[],
            takeaway=None,
            origin=origin,
            is_floor=True,
        )
        self._persist_episode(episode)
        return episode

    def _persist_episode(self, episode: Episode) -> None:
        """Write `episode` to its on-disk path with full durability.

        Shared by `write` (real takeaway journal) and `write_floor`
        (session-tag anchor). Encapsulates the directory-creation
        ceremony, the per-session flock, the dir-fsync ceremony for
        first-create dirents, and delegates the per-file atomic
        rename to `_write_path`. Keeping a single implementation
        site for the disk discipline guarantees floor writes inherit
        every durability fix the real-write path has earned (audit-
        3 A3-04 / A3-05, the per-session flock against prune races,
        the first-create dir-fsync chain root → episodes_dir →
        session_dir, the lockfile-placement-in-episodes_dir
        invariant).
        """
        session_id = episode.session_id
        # Materialize the subdir on first write — the parent `episodes/`
        # gets the 0o700 treatment the tombstone directory does, since
        # episodes carry the same trust boundary as memories (origin
        # capture includes cwd, branch).
        #
        # Track whether the `episodes/` dirent was created by us. If
        # so, fsync the memory root (its parent) after the mkdir so the
        # new dirent survives a crash. POSIX requires an explicit
        # dir-fsync on the parent for a freshly-created subdir entry to
        # be durable; without it, a crash after the first-ever episode
        # write can resurrect a state where the file exists on disk but
        # `episodes/` is missing from `root`'s directory listing — the
        # file is orphan (no path traversal can reach it). Mirrors
        # `events.Recorder.record`'s `fsync_dir(self.root)` on first
        # write at `events.py:264`. Best-effort: `fsync_dir` no-ops on
        # Windows and swallows OSError on pseudo-filesystems.
        episodes_dir_was_created = not self.episodes_dir.exists()
        self.episodes_dir.mkdir(mode=0o700, exist_ok=True)
        if episodes_dir_was_created:
            fsync_dir(self.root)
        session_dir = self._session_dir(session_id)
        # Cross-process coordination with `prune_old_sessions`. Multi-MCP
        # racing: process A's prune (TTL eviction or maintenance call)
        # could `shutil.rmtree(session_dir)` while process B is mid-write
        # into the same dir — B's just-renamed `<ulid>.md` is wiped, or
        # B crashes on a vanished parent dir during mkdir/rename. Both
        # `write` (here) and `prune_old_sessions` take the same per-
        # session flock; the writer holds it across mkdir + rename + dir
        # fsync, the prune holds it across mtime recheck + rmtree, so
        # the session_dir lifecycle is serialised end-to-end on POSIX
        # and macOS. Windows uses `msvcrt.locking` via the same helper.
        #
        # The lockfile lives in `episodes_dir`, NOT inside `session_dir`,
        # so a peer prune's rmtree can't wipe the lock mid-acquisition.
        # The `.session-<id>` prefix keeps locks per-session (no global
        # bottleneck), and `iter_session_ids` / `prune_old_sessions`
        # both filter `is_dir()` so the lockfiles are invisible to
        # session enumeration.
        with flock_excl(self.episodes_dir / f".session-{session_id}"):
            # Track whether session_dir was created so we can fsync the
            # parent (episodes_dir) on first creation only. `_write_path`
            # below fsyncs `session_dir` itself for the rename, but the
            # NEW dirent for `session_dir` inside `episodes_dir` lives
            # in episodes_dir's own page-cache until a dir-fsync hits
            # disk. On crash after the first write into a fresh
            # session_id, the file + session_dir survive on disk but
            # are unreachable via path traversal (the dirent for
            # session_dir is missing from the recovered episodes_dir
            # listing). Mirrors the events.Recorder.record discipline
            # at `events.py:264` and stays inside the flock so the
            # metadata flush completes before any peer prune's
            # rmdir/rmtree can interleave. Subsequent writes to the
            # same session_dir don't need re-syncing — the dirent
            # already exists.
            session_dir_was_created = not session_dir.exists()
            session_dir.mkdir(mode=0o700, exist_ok=True)
            path = session_dir / f"{episode.id}.md"
            self._write_path(path, episode)
            if session_dir_was_created:
                fsync_dir(self.episodes_dir)

    def _write_path(self, path: Path, episode: Episode) -> None:
        post = frontmatter.Post(episode.body.strip() + "\n")
        meta: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "id": episode.id,
            "session_id": episode.session_id,
            "created": episode.created,
        }
        if episode.scopes:
            meta["scopes"] = list(episode.scopes)
        if episode.takeaway is not None:
            meta["takeaway"] = episode.takeaway
        if episode.origin is not None:
            origin_dict = episode.origin.model_dump(mode="json", exclude_none=True)
            if origin_dict:
                meta["origin"] = origin_dict
        # `is_floor` is opt-in additive — only emit when True. Legacy
        # episodes written before the field shipped have no `is_floor`
        # key in their frontmatter, and `_load_path` defaults to False
        # for the missing-key case (see below). Real-takeaway episodes
        # written post-E2 also omit the key (saves four bytes per file
        # × N episodes per worktree) and load identically. Floors are
        # the only writers that emit the key.
        if episode.is_floor:
            meta["is_floor"] = True
        # Swarm cohort link — opt-in additive, only emitted when set
        # (same pattern as `is_floor` / `takeaway` / `scopes`). A missing
        # key loads as None, so non-swarm episodes keep the exact
        # pre-field on-disk shape and no SCHEMA_VERSION bump is needed.
        if episode.swarm_id is not None:
            meta["swarm_id"] = episode.swarm_id
        post.metadata = meta
        # Atomic, durable, 0o600 write via the shared helper: tmp +
        # fchmod-before-rename + fsync + rename + fsync_dir(session_dir) +
        # orphan-tmp cleanup all live in `atomic_write_bytes`. Episode
        # bodies carry the same trust boundary as memories (origin capture
        # includes cwd + branch), so the 0o600-before-rename privacy
        # guarantee matters — see `_fsutil.atomic_write_bytes` for the
        # closed-window rationale. The caller (`_persist_episode`) owns the
        # episodes_dir/root dirent fsyncs that bracket this write.
        # Episodes are journal entries: written once and pruned wholesale,
        # never tombstoned / renamed / re-dumped, so they reserve no maintenance
        # headroom. Admit at the full read cap (`_MAX_FILE_BYTES`), not `dumps`'
        # reduced write-cap default — an episode only needs to be re-readable.
        # The 3.14.1 total-file cap silently tightened this ceiling from the read
        # cap to the write cap, so an episode body in the (write_cap, read_cap]
        # band that used to persist now raised at write time; restore the read
        # cap here (episodes.py was not in that release's diff — critic gap).
        atomic_write_bytes(
            path,
            frontmatter.dumps(post, max_file_bytes=frontmatter._MAX_FILE_BYTES).encode(
                "utf-8"
            ),
            mode_before_rename=0o600,
        )

    # ---- read -------------------------------------------------------------

    def _iter_session_paths(self, session_id: str) -> Iterator[Path]:
        session_dir = self._session_dir(session_id)
        if not session_dir.exists():
            return
        for entry in session_dir.iterdir():
            if entry.is_file() and not entry.is_symlink() and entry.suffix == ".md":
                yield entry

    def list_by_session(self, session_id: str) -> list[Episode]:
        """All episodes for one session, oldest first (ULIDs sort by creation)."""
        out: list[Episode] = []
        for path in self._iter_session_paths(session_id):
            try:
                out.append(self._load_path(path))
            except (ValueError, KeyError, OSError):
                continue
        out.sort(key=lambda e: e.created)
        return out

    def list_by_swarm(self, swarm_id: str) -> list[Episode]:
        """All episodes across every session tagged with `swarm_id`,
        oldest first — the multi-agent swarm fan-in.

        A coordinator fans out N sub-agents, each writing episodes under
        its own session directory but stamped with the coordinator's
        `swarm_id`. This walks every session directory and returns the
        cohort's episodes globally sorted, so the coordinator can gather
        what all its sub-agents concluded in one read.

        Cost is the same full-walk shape `episode_search` already uses
        when no single session is named — bounded by the prune TTL
        (`DEFAULT_EPISODE_TTL_DAYS`), so old swarms age out and the walk
        stays cheap. Floors (`is_floor`) are included here for parity
        with `list_by_session`; the `episode_search` summary surface
        filters them out. `swarm_id` is matched for equality only —
        never used as a path — so an empty result for an unknown id is
        the correct (and only) failure mode, not a raise.
        """
        out: list[Episode] = []
        for sid in self.iter_session_ids():
            try:
                episodes = self.list_by_session(sid)
            except ValueError:
                # Defensive: a directory name that fails session-id
                # validation is skipped rather than crashing the fan-in
                # (mirrors episode_search's per-session try/except).
                continue
            out.extend(ep for ep in episodes if ep.swarm_id == swarm_id)
        out.sort(key=lambda e: e.created)
        return out

    def iter_session_ids(self) -> Iterator[str]:
        """All session_ids that currently have an episode directory."""
        if not self.episodes_dir.exists():
            return
        for entry in self.episodes_dir.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                yield entry.name

    # ---- volume (stat-only; never parses an episode) ----------------------

    def _scan_sessions(self) -> Iterator[tuple[str, int, int, float | None]]:
        """Per session dir: `(session_id, episodes, bytes, newest_mtime)`.

        One `iterdir` + one `stat` per file, no `frontmatter.load`. The
        two tallies deliberately cover different file sets, each matching
        the consumer it feeds:

        - `episodes` / `bytes` count `.md` regular files only — the same
          set `_iter_session_paths` yields, i.e. what an episode IS.
        - `newest_mtime` spans EVERY regular file, because that is what
          `_newest_mtime_in_dir` does and `prune_old_sessions` keys its
          TTL cutoff on that value. Narrowing it to `.md` here would make
          the prunable count disagree with the GC it is predicting.

        `newest_mtime is None` means the directory holds no regular file
        at all — the empty-dir case `prune_old_sessions` also reclaims.
        """
        for session_id in self.iter_session_ids():
            session_dir = self.episodes_dir / session_id
            episodes = 0
            total_bytes = 0
            newest: float | None = None
            try:
                entries = list(session_dir.iterdir())
            except OSError:
                # Vanished or unreadable between the parent listing and
                # this one (a peer prune, a permissions change). Report it
                # as an empty session rather than crashing a read-only
                # gauge — same defensive posture `_newest_mtime_in_dir`
                # takes on OSError.
                yield session_id, 0, 0, None
                continue
            for entry in entries:
                # ONE `lstat` per entry, and it is exactly the predicate
                # `_newest_mtime_in_dir` spells as `is_file() and not
                # is_symlink()` — `S_ISREG` on an lstat result is false
                # for a symlink by definition, whatever it points at. The
                # single call is both cheaper (that form costs three
                # syscalls per file) and tighter: there is no window
                # between the two checks and the read for the entry to
                # change type underneath us.
                try:
                    stat_result = entry.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(stat_result.st_mode):
                    continue
                if newest is None or stat_result.st_mtime > newest:
                    newest = stat_result.st_mtime
                if entry.suffix == ".md":
                    episodes += 1
                    total_bytes += stat_result.st_size
            yield session_id, episodes, total_bytes, newest

    def prunable_session_ids(
        self,
        *,
        ttl_days: int = DEFAULT_EPISODE_TTL_DAYS,
        now: datetime | None = None,
    ) -> list[str]:
        """Session ids `prune_old_sessions` would collect right now.

        The shared statement of "is this session past the TTL?", called
        by `bettermemory episodes prune --dry-run`. Before this existed
        the CLI carried its own transcription of the predicate under a
        comment asking the next reader to keep the two aligned.

        `volume()` does NOT call this — it evaluates the same rule inline
        so the gauge costs one `_scan_sessions` pass rather than two. So
        the rule is written twice on purpose, and what keeps the copies
        honest is a test, not a call graph:
        `test_prunable_sessions_predicts_exactly_what_prune_collects`
        asserts the two agree over a fixture covering both collect
        branches, and a companion pins the `ttl_days <= 0` boundary for
        both. Change one copy without the other and that test fails.

        Mirrors `prune_old_sessions` exactly on its two collect branches —
        `ttl_days <= 0` is a no-op (never "delete everything"), a
        directory with no regular file is collectable, and otherwise the
        newest mtime must be strictly older than the cutoff. It does NOT
        model `keep_session_id`: the caller that knows which session is
        live can subtract it.
        """
        if ttl_days <= 0 or not self.episodes_dir.exists():
            return []
        cutoff_epoch = ((now or utcnow()) - timedelta(days=ttl_days)).timestamp()
        return [
            session_id
            for session_id, _episodes, _bytes, newest in self._scan_sessions()
            if newest is None or newest < cutoff_epoch
        ]

    def volume(
        self,
        *,
        ttl_days: int = DEFAULT_EPISODE_TTL_DAYS,
        now: datetime | None = None,
    ) -> EpisodeVolume:
        """Aggregate size of the subtree — see `EpisodeVolume`.

        Cost is one `iterdir` per session directory plus one `stat` per
        file: the same syscall shape `prune_old_sessions` already pays on
        every `episode_write`, and strictly cheaper than any `list_by_*`
        because nothing here parses frontmatter. Even so it is wired into
        exactly one caller — `health.report_for_directory`, which backs
        `memory_health` and `bettermemory health`, neither of which runs
        per turn. It must not acquire a per-turn caller; the point of the
        gauge is to report growth, not to add a walk to the surfaces that
        already have one.
        """
        if not self.episodes_dir.exists():
            return EpisodeVolume(
                sessions=0,
                episodes=0,
                bytes=0,
                prunable_sessions=0,
                ttl_days=ttl_days,
            )
        cutoff_epoch: float | None = None
        if ttl_days > 0:
            cutoff_epoch = ((now or utcnow()) - timedelta(days=ttl_days)).timestamp()
        sessions = 0
        episodes = 0
        total_bytes = 0
        prunable = 0
        # One pass, both answers: `_scan_sessions` already hands back the
        # newest mtime, so evaluating the `prunable_session_ids` predicate
        # inline costs nothing beyond a comparison. Calling that method
        # instead would double the syscalls for no correctness gain — the
        # predicate below is the same one, and the parity test pins them.
        for (
            _session_id,
            session_episodes,
            session_bytes,
            newest,
        ) in self._scan_sessions():
            sessions += 1
            episodes += session_episodes
            total_bytes += session_bytes
            if cutoff_epoch is not None and (newest is None or newest < cutoff_epoch):
                prunable += 1
        return EpisodeVolume(
            sessions=sessions,
            episodes=episodes,
            bytes=total_bytes,
            prunable_sessions=prunable,
            ttl_days=ttl_days,
        )

    def _load_path(self, path: Path) -> Episode:
        post = frontmatter.load(path)
        meta = post.metadata
        on_disk_version = meta.get("schema_version", 1)
        try:
            on_disk_int = int(on_disk_version)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{path}: schema_version is not an integer ({on_disk_version!r})"
            ) from exc
        if on_disk_int > SCHEMA_VERSION:
            raise ValueError(
                f"{path}: schema_version {on_disk_int} exceeds reader "
                f"max {SCHEMA_VERSION}; upgrade bettermemory"
            )
        origin_raw = meta.get("origin")
        origin_obj: Origin | None = None
        if isinstance(origin_raw, dict):
            origin_obj = Origin.model_validate(origin_raw)
        # Missing key → False (legacy default, real-takeaway episodes
        # never emit the key). Coerce defensively so a hand-edited
        # frontmatter that wrote `is_floor: "true"` (string) still
        # loads as the expected boolean rather than crashing the
        # model validator.
        is_floor_raw = meta.get("is_floor", False)
        is_floor = bool(is_floor_raw)
        # Missing key → None (legacy / non-swarm episodes never emit it).
        # Coerce to str so a hand-edited numeric frontmatter value still
        # loads as the expected type rather than tripping the validator.
        swarm_raw = meta.get("swarm_id")
        swarm_id = str(swarm_raw) if swarm_raw is not None else None
        # Missing key → [] (the writer only emits `scopes` when non-empty;
        # floors and legacy episodes carry none). Coerce defensively like
        # `is_floor` / `swarm_id` above: a scalar (`scopes: 5`, hand-edited
        # or a buggy client) made the previous bare `list(...)` raise
        # TypeError — NOT in the (ValueError, KeyError, OSError) skip set
        # `list_by_session` catches — so ONE malformed file crashed every
        # episode read surface (episode_handoff on the /loop iteration-entry
        # hot path, episode_search, episode_promote, list_by_swarm). A
        # non-list shape degrades to [] with body/takeaway preserved
        # (scopes are advisory tags, not identity; a bare str would
        # otherwise explode per-character through `list(...)`). List
        # elements are str()-coerced so a numeric tag (`scopes: [5]`)
        # loads instead of tripping the model validator and dropping the
        # row; an element whose string form fails scope validation still
        # raises ValueError, the designed skip-this-row signal.
        scopes_raw = meta.get("scopes")
        scopes = [str(s) for s in scopes_raw] if isinstance(scopes_raw, list) else []
        # Missing key → None (the writer only emits it when set). Same
        # str-coercion as `swarm_id`: a hand-edited scalar (`takeaway: 5`)
        # loads as its string form rather than tripping the validator and
        # silently dropping the whole row from reads.
        takeaway_raw = meta.get("takeaway")
        takeaway = str(takeaway_raw) if takeaway_raw is not None else None
        # Normalise `created` to tz-aware UTC at load, date-aware, mirroring
        # `store._as_dt`. The vendored YAML loader produces THREE distinct
        # shapes for a `created` value and an unguarded one fails the WHOLE
        # episode read (list_by_session / list_by_swarm and the `since`
        # filter in episode_search) instead of skipping one row:
        #   * a native datetime (unquoted ISO, may be naive when no offset
        #     was written) — ensure_utc stamps a naive value as UTC, passes
        #     an aware one through. `datetime` IS a subclass of `date`, so
        #     this branch MUST come first;
        #   * a bare YAML date scalar `created: 2026-05-31` parses as a
        #     `datetime.date` (NOT a datetime, NOT a str) — lift to UTC
        #     midnight, the natural choice;
        #   * a quoted string `created: "2026-05-31T12:00:00"` stays a `str`
        #     — parse via parse_event_ts, the canonical ISO parser.
        # An earlier hardening pass used bare `ensure_utc(meta["created"])`
        # here; ensure_utc is typed (datetime | None) -> datetime | None and
        # touches `.tzinfo`, so the date and str shapes raised AttributeError
        # — NOT in the (ValueError, KeyError, OSError) catch in
        # list_by_session — and a single hand-edited/legacy episode file
        # crashed the whole read surface (episode_search, episode_promote,
        # list_by_swarm, episode_handoff on the /loop hot path). A
        # missing/null/unparseable `created` raises ValueError, which the
        # read surface already treats as a skip-this-row signal (same as the
        # schema_version guard above).
        created_raw = meta.get("created")
        created: datetime
        if isinstance(created_raw, datetime):
            normalised = ensure_utc(created_raw)
            assert normalised is not None  # ensure_utc(datetime) is never None
            created = normalised
        elif isinstance(created_raw, date):
            # Bare YAML date — `datetime` already handled above, so this is
            # a pure date. UTC midnight is the natural lift.
            created = datetime(
                created_raw.year,
                created_raw.month,
                created_raw.day,
                tzinfo=timezone.utc,
            )
        elif isinstance(created_raw, str):
            parsed = parse_event_ts(created_raw)
            if parsed is None:
                raise ValueError(f"{path}: 'created' is not a parseable timestamp")
            created = parsed
        else:
            # None (missing/null) or any other unexpected type — skip the row.
            raise ValueError(f"{path}: 'created' is missing, null, or unparseable")
        return Episode(
            id=str(meta["id"]),
            session_id=str(meta["session_id"]),
            created=created,
            body=post.content,
            scopes=scopes,
            takeaway=takeaway,
            swarm_id=swarm_id,
            origin=origin_obj,
            is_floor=is_floor,
        )

    # ---- prune ------------------------------------------------------------

    def prune_old_sessions(
        self,
        *,
        ttl_days: int = DEFAULT_EPISODE_TTL_DAYS,
        keep_session_id: str | None = None,
        now: datetime | None = None,
    ) -> list[str]:
        """Drop session subdirectories whose newest episode is older
        than `ttl_days`. Returns the list of pruned session_ids.

        `keep_session_id`, when provided, is exempt from pruning even
        if its newest episode is past the TTL. Used by the write path
        to keep the active session's directory alive across a pause.

        Concurrency: serialised against `EpisodeStore.write` via a per-
        session flock (`episodes_dir / .session-<id>.lock`). The walk
        below stats mtimes unlocked (cheap), then takes the per-session
        flock and re-stats under the lock before `rmtree` — a writer
        racing in between the unlocked stat and the lock acquisition
        has already bumped mtime past cutoff, so the recheck skips the
        delete. Without this, a multi-MCP setup (two Claude Code
        sessions on the same store) could `shutil.rmtree` a session
        dir mid-write and silently lose B's just-rename'd episode.

        Sidecar-lockfile cleanup is deferred to AFTER each flock block
        releases (collected in `pruned_lock_files`, swept post-loop):
        an in-lock unlink is dead code on Windows because `msvcrt.
        locking` keeps the lockfile handle open for the `with` block's
        duration, so the unlink would raise and leak the sidecar — the
        exact class the 3.4.2 store.py fix (40e71e4) addressed. This
        mirrors `store.prune_tombstones`, which also defers all sidecar
        unlinks past lock release.
        """
        if ttl_days <= 0 or not self.episodes_dir.exists():
            return []
        cutoff = (now or utcnow()) - timedelta(days=ttl_days)
        cutoff_epoch = cutoff.timestamp()
        pruned: list[str] = []
        # Sidecar `.session-<id>.lock` paths to unlink AFTER their
        # `flock_excl(...)` block has exited and the OS lock handle is
        # closed. The unlink CANNOT happen inside the `with` block: on
        # Windows `msvcrt.locking` keeps the lockfile handle open for the
        # whole `with` duration (the fd is closed only in `_flock_windows`'s
        # outer `finally`, after the `yield` returns), so `lock_file.unlink`
        # against that still-open handle raises `OSError` — silently
        # swallowed, leaving the sidecar to leak. This is the exact root-
        # cause class the 3.4.2 store.py fix (40e71e4) addressed for
        # tombstone sidecars; `store.prune_tombstones` defers ALL sidecar
        # unlinks to after lock release for the same reason and we mirror it
        # here. (POSIX would tolerate an in-lock unlink because the held fd
        # keeps the inode alive, but the deferred sweep is correct on both.)
        pruned_lock_files: list[tuple[Path, Path]] = []
        for session_dir in self.episodes_dir.iterdir():
            if not session_dir.is_dir() or session_dir.is_symlink():
                continue
            session_name = session_dir.name
            if session_name == keep_session_id:
                continue
            newest_mtime = _newest_mtime_in_dir(session_dir)
            if newest_mtime is None:
                # Empty subdir — drop it under the per-session flock.
                # Without the lock there's a writer-race window: a
                # concurrent `episode_write` that holds the flock has
                # `mkdir(exist_ok=True)`'d the session_dir but not yet
                # rename'd its `<ulid>.md` into place (the tempfile is
                # still under construction). Our unlocked walk sees an
                # empty dir, decides to rmdir, and wins between the
                # writer's mkdir and its NamedTemporaryFile open —
                # the writer's next syscall raises FileNotFoundError
                # to the MCP caller. Mirror the past-cutoff branch's
                # discipline: take the per-session flock, recheck
                # emptiness under the lock, then rmdir.
                #
                # Same lockfile placement (`episodes_dir`, not
                # `session_dir`) and same persistence rationale as the
                # past-cutoff branch — see the long comment below.
                lock_anchor = self.episodes_dir / f".session-{session_name}"
                lock_file = lock_anchor.with_suffix(lock_anchor.suffix + ".lock")
                try:
                    with flock_excl(lock_anchor):
                        fresh_mtime = _newest_mtime_in_dir(session_dir)
                        if fresh_mtime is not None:
                            # Writer landed during our unlocked walk
                            # and our flock-acquire — the dir is no
                            # longer empty. Leave it alone; the next
                            # prune pass will reconsider via the
                            # past-cutoff branch.
                            continue
                        session_dir.rmdir()
                        # Durability gate (audit-3 A3-04): rmdir drops
                        # the dirent from `episodes_dir`, but the
                        # metadata change lives in the parent's
                        # page-cache until a dir-fsync hits disk. On
                        # crash, the kernel can present a recovered
                        # `episodes_dir` that still lists the deleted
                        # session_dir as a phantom entry — next list
                        # would attempt to iterate it and trip
                        # FileNotFoundError or, worse, the dir gets
                        # half-resurrected. Fsync inside the flock so
                        # the metadata flush completes before the lock
                        # releases and any peer can re-observe the dir.
                        fsync_dir(self.episodes_dir)
                    # Defer the sidecar-lockfile unlink to AFTER this
                    # `with` block closes the OS lock handle — an in-lock
                    # unlink is dead code on Windows (`msvcrt.locking`
                    # holds the handle open, so the unlink raises and is
                    # swallowed). See the `pruned_lock_files` comment at
                    # the top of this method and the past-cutoff branch
                    # below for the full lifecycle rationale (no live
                    # writer can exist on a session_dir we just rmdir'd).
                    pruned_lock_files.append((lock_file, session_dir))
                    pruned.append(session_name)
                except FileNotFoundError:
                    # Peer pruner won the race between our unlocked
                    # walk and our flock acquisition. Treat as success
                    # — the observable outcome (session_dir gone) is
                    # what the caller wanted. The orphan sweep at the
                    # end of this method reclaims any sidecar the peer
                    # left behind, so we don't need to enqueue one here.
                    pruned.append(session_name)
                except OSError:
                    # Either ENOTEMPTY (a writer slipped a file in
                    # between our locked recheck and rmdir — possible
                    # only via a non-flock-respecting peer, but cheap
                    # to handle) or another transient filesystem
                    # error. Skip this session; next prune pass will
                    # reconsider.
                    continue
                continue
            if newest_mtime < cutoff_epoch:
                # Take the per-session flock to serialise against a
                # concurrent writer. Recheck mtime inside the lock: a
                # writer that started between our unlocked stat above
                # and the flock acquisition will have rename'd a fresh
                # `<ulid>.md` into the dir by the time we own the lock,
                # so the recheck sees a fresh mtime and we skip the
                # delete. The lockfile lives in `episodes_dir` rather
                # than inside `session_dir` so the rmtree can't wipe
                # the lockfile mid-acquisition by a peer prune.
                #
                # Lockfile lifecycle on this branch (audit-3 carryover
                # A3-13 / E1): the sidecar lockfile is enqueued for
                # unlink AFTER this `with` block closes the OS lock
                # handle — NOT inside the flock. The in-lock unlink it
                # used to do was dead code on Windows: `msvcrt.locking`
                # keeps the lockfile handle open for the whole `with`
                # duration, so `lock_file.unlink` against the still-open
                # handle raised `OSError` and was silently swallowed,
                # leaking the sidecar (the precise root-cause class the
                # 3.4.2 store.py fix 40e71e4 addressed for tombstone
                # sidecars). The leak was masked only by the post-loop
                # orphan sweep below; deferring the unlink to after lock
                # release makes it actually run on BOTH platforms and
                # keeps us consistent with `store.prune_tombstones`,
                # which defers ALL sidecar unlinks the same way.
                #
                # Unlinking is safe (rather than leaving the lockfile in
                # place for flock-inode identity) because for a session
                # whose newest-mtime is past TTL by 30+ days, no live
                # writer can exist — every legitimate write refreshes the
                # session_dir's mtime past cutoff via `_write_path` +
                # rename. The only concurrent acquirers possible are peer
                # prunes, and two peer prunes converging on the same dead
                # session both reach the same observable outcome (session
                # gone, lockfile gone). Before E1 each fresh /loop tick
                # (new process => new session_id) left a 0-byte lockfile
                # that survived TTL prunes, and `iterdir()` over
                # `episodes_dir` slowed handoff latency materially at
                # N≈10⁵.
                lock_anchor = self.episodes_dir / f".session-{session_name}"
                lock_file = lock_anchor.with_suffix(lock_anchor.suffix + ".lock")
                try:
                    with flock_excl(lock_anchor):
                        fresh_mtime = _newest_mtime_in_dir(session_dir)
                        if fresh_mtime is None and session_dir.exists():
                            # The session_dir is now empty (writer
                            # deleted its own tmp on a failed rename).
                            # The empty-dir branch on the next prune
                            # pass will pick it up; don't delete here.
                            # Defense-in-depth: confirm session_dir
                            # still exists before continuing — if a
                            # peer prune already rmtree'd it we want
                            # to fall through to the orphan-lockfile
                            # cleanup below.
                            continue
                        if fresh_mtime is not None and fresh_mtime >= cutoff_epoch:
                            # Writer raced in and rendered the dir
                            # current again — leave it alone.
                            continue
                        if session_dir.exists():
                            shutil.rmtree(session_dir)
                            # Durability gate (audit-3 A3-04): rmtree
                            # drops the session_dir's dirent from
                            # `episodes_dir`, but the metadata change
                            # lives in the parent's page-cache until a
                            # dir-fsync hits disk. On crash, the kernel
                            # can present a recovered `episodes_dir`
                            # that still lists the deleted session as
                            # a phantom entry, with the inner files
                            # already wiped — readers attempting to
                            # iterate it would trip FileNotFoundError
                            # or surface stale episodes briefly. Fsync
                            # inside the flock so the metadata flush
                            # completes before the lock releases.
                            fsync_dir(self.episodes_dir)
                    # session_dir is gone (we just rmtree'd it, OR a peer
                    # prune wiped it during the unlocked-stat → flock-
                    # acquire window and we observed `fresh_mtime is
                    # None`). Either way, enqueue the sidecar lockfile
                    # for unlink now that the `with` block has closed the
                    # OS lock handle — see the lifecycle comment above
                    # for why the unlink must be deferred past lock
                    # release (dead code on Windows otherwise) and why
                    # it's safe on a past-TTL session.
                    pruned_lock_files.append((lock_file, session_dir))
                    pruned.append(session_name)
                except FileNotFoundError:
                    # Another prune in a peer process already rmtree'd
                    # this session between our unlocked stat and our
                    # flock acquisition. Treat as success — the
                    # observable outcome (session_dir gone) is what
                    # the caller wanted. The orphan sweep at the end of
                    # this method reclaims any sidecar the peer left
                    # behind, so we don't enqueue one here.
                    pruned.append(session_name)
                except OSError:
                    continue
        # Now that every per-session `flock_excl` block has exited and
        # the OS lock handle is closed, unlink the sidecar lockfiles we
        # enqueued for the sessions we pruned. This is the step the
        # in-lock unlink used to attempt but couldn't complete on
        # Windows (the held `msvcrt.locking` handle made the unlink
        # raise); running it here, post-release, deletes the sidecar on
        # both POSIX and Windows. Mirrors `store.prune_tombstones`'s
        # post-`_locked` sidecar sweep. `_unlink_session_lockfile` keeps
        # its `session_dir.exists()` guard so a sidecar whose session
        # somehow came back is left alone.
        for lock_file, session_dir in pruned_lock_files:
            _unlink_session_lockfile(self.episodes_dir, lock_file, session_dir)
        # Then sweep any orphan lockfiles whose corresponding session_dir
        # is already gone but that we did NOT enqueue above. Two sources
        # of orphans: (a) lockfiles written by pre-E1 versions of
        # bettermemory that ran TTL prunes on the same store, (b) an
        # unlikely peer-prune race that left a fresh inode at the path
        # between our unlink and lock-release. Cheap — one `iterdir()` +
        # per-file `is_file()` + a `stat()` on the corresponding
        # session_dir.
        self._cleanup_orphan_lockfiles()
        return pruned

    def _cleanup_orphan_lockfiles(self) -> None:
        """Remove `.session-<id>.lock` files whose session_dir is gone.

        Safe to call unconditionally at the end of `prune_old_sessions`:
        a session_dir's absence proves no live writer holds (or is
        waiting on) the lock. Peer prunes converging on the same dead
        session both end up with the session_dir gone before they would
        try to unlink the lockfile; the worst case is two prunes
        racing on the unlink itself, which is benign — `missing_ok=True`
        swallows the FileNotFoundError, and both prunes return success.

        Pre-E1 lockfile leaks (one orphan per fresh /loop tick) are
        mopped up on the first post-E1 prune call so the
        `iterdir(episodes_dir)` cost stops growing.
        """
        if not self.episodes_dir.exists():
            return
        try:
            entries = list(self.episodes_dir.iterdir())
        except OSError:
            return
        cleaned = False
        for entry in entries:
            # `.session-<id>.lock` is the only sidecar pattern produced
            # by `flock_excl` against an anchor like `.session-<id>`.
            if not entry.is_file() or entry.is_symlink():
                continue
            name = entry.name
            if not name.startswith(".session-") or not name.endswith(".lock"):
                continue
            session_name = name[len(".session-") : -len(".lock")]
            if not session_name:
                continue
            session_dir = self.episodes_dir / session_name
            if session_dir.exists():
                # Live session — leave its lockfile alone. The
                # session_dir's existence is the only signal that a
                # writer might be racing for this lock.
                continue
            try:
                entry.unlink()
                cleaned = True
            except FileNotFoundError:
                # Peer prune unlinked between our scan and our unlink
                # — same observable end-state, continue.
                continue
            except OSError:
                # Don't propagate — orphan cleanup is opportunistic.
                continue
        if cleaned:
            # Persist the unlinks so a crash can't resurrect the
            # dirents we just cleared.
            fsync_dir(self.episodes_dir)


def _newest_mtime_in_dir(dir_path: Path) -> float | None:
    """Largest mtime over the directory's regular files. None when empty."""
    newest: float | None = None
    try:
        for entry in dir_path.iterdir():
            if entry.is_file() and not entry.is_symlink():
                mtime = entry.stat().st_mtime
                if newest is None or mtime > newest:
                    newest = mtime
    except OSError:
        return None
    return newest


def _unlink_session_lockfile(
    episodes_dir: Path, lock_file: Path, session_dir: Path
) -> None:
    """Unlink a per-session sidecar lockfile and fsync the parent dir.

    Called from `prune_old_sessions`'s post-loop sweep — AFTER the
    per-session `flock_excl(...)` block has exited and the OS lock
    handle is closed. The unlink must NOT run inside the flock: on
    Windows `msvcrt.locking` keeps the lockfile handle open for the
    whole `with` duration, so unlinking the still-open file raises
    `OSError` and the sidecar leaks (the root-cause class the 3.4.2
    store.py fix 40e71e4 addressed). Deferring to post-release makes
    the unlink land on both POSIX and Windows, matching
    `store.prune_tombstones`'s post-`_locked` sidecar sweep.

    Defense-in-depth: verifies `session_dir` is gone before unlinking,
    so a refactor that calls this on a live session is a no-op rather
    than a silent correctness regression. The fsync persists the dirent
    removal so a crash can't resurrect an orphan lockfile that pre-
    E1's design intentionally left behind.

    See the lifecycle comment in `prune_old_sessions` past-cutoff
    branch for why unlinking is safe (no live writers possible on a
    past-TTL session).
    """
    if session_dir.exists():
        # Defensive guard — should never fire from the prune branches
        # because both call sites only reach this after rmtree/rmdir
        # or after observing session_dir is already gone. If a future
        # refactor breaks that invariant, refusing to unlink is the
        # safer failure mode than racing a live writer.
        return
    try:
        lock_file.unlink(missing_ok=True)
    except OSError:
        # Best-effort: a missing lockfile (already cleaned up by peer)
        # or a transient filesystem error doesn't justify failing the
        # prune. Next prune pass will retry the cleanup via the orphan
        # sweep.
        return
    fsync_dir(episodes_dir)


__all__ = [
    "DEFAULT_EPISODE_TTL_DAYS",
    "EPISODES_DIR",
    "EpisodeStore",
    "EpisodeVolume",
]
