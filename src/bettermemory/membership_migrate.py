"""The migration of design 8.6: the membership tables built from the
store's own records, with no manual step (RS-F6, decisions D4 and D5).

Every memory (active or removed) and every episode without a home row gets
exactly one, written through ``Store.put_membership("home", ...)``: no
memory, tombstone or episode row is rewritten and the origin columns stay
as they are (design 1, 8.5). What decides a home is read from the records
themselves: their origin columns, the commits their verifications
recorded (``verified_head``, the root's own HEAD; ``verified_commits``,
what a record cites; full ids or abbreviated ones, which a repository
resolves to the one object they name, decision B19), and the log's history
of their roots.

Each distinct recorded root (``origin.worktree_root``) is probed once and
read-only: `location.capture` with the token write off and no cache.

- live: the path is a repository's top level, or a folder with a project
  file. The ladder resolves it (`Membership.resolve`, design 3.2), which
  registers the checkout on this machine with its object, its token if it
  has one, HEAD, remotes and view; the commits its records recorded that
  it holds become witnesses of its project.
- otherwise (gone, replaced by something else, or unreachable), the first
  rule that answers: a prior view of a live checkout when the log shows
  its records re-homed there (``rehome``); a checkout of the one live
  project holding its records' verified heads, remotes compatible
  (``lineage``); of the one holding their cited commits under the same
  folder name (``lineage_cited+same_name``); of the one carrying its remote
  when no commit was recorded (``remote``); else a project of its own with
  no live checkout (``none``, or ``ambiguous`` when several live projects
  hold its verified heads), whose verified heads and remotes that no live
  project holds become its evidence. Such a project whose recorded commits
  a live checkout partly holds becomes one durable ``legacy`` question
  (line L6) in the live checkout holding the most of them (D5), so it is
  asked where it can be answered.

A record with no root follows its remote: the one live project carrying
its key, else the one project carrying it with no live checkout (decided
in this run or an earlier one), else the first minted of several carriers
(``remote_ambiguous``), and only when no project carries the key a project
minted for it, named after the repository, so no key is carried by two
projects the migration minted; a remote-less one whose cwd lies inside a recorded root is homed to that root's checkout
(D4, ``cwd_in_root``); one whose cwd is in a live repository or folder
project takes that checkout's resolution (``cwd``); the rest were written
outside any repository and any project file and are ``global``.

Nothing is written into any repository: the capture writes no token, and
a checkout gets its token at its next sighting, in its caller's own
namespace (design 1.1). The run is one store transaction ending in one
``membership_migrated`` row with its counts and its per-root decisions;
a dry run is the same transaction rolled back. A later run homes only the
records without a home (those an earlier build wrote meanwhile, design
8.12) by the same rules: a root an earlier run decided that is not live
now keeps that decision, a live root is resolved again.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from . import location
from . import membership as mb
from .models import utcnow
from .store import Store
from .time_utils import isoformat_utc

if TYPE_CHECKING:
    from .config import Config
    from .machine import MachineIdentity

# What a probe found at a recorded root.
LIVE = "live"
GONE = "gone"
REPLACED = "replaced"
UNREACHABLE = "unreachable"

#: Every home rule the migration writes starts with this.
RULE_PREFIX = "migration:"
#: The reason of the migration's ``legacy`` questions (design 8.6, D5).
LEGACY_REASON = mb.LEGACY_NO_LIVE_CHECKOUT
#: The membership row the run ends with (design 8.10).
MIGRATED = "membership_migrated"

_RECORD_TABLES = (
    ("memories", "memory"),
    ("tombstones", "memory"),
    ("episodes", "episode"),
)
_RECORD_KINDS = (
    "memory_put",
    "memory_delete",
    "tombstone_put",
    "tombstone_delete",
    "episode_put",
    "episode_delete",
)
# A recorded commit: a full id, or an abbreviated one as a memory's
# ``verified_commits`` often carry it (decision B19), of git's shortest
# abbreviation and up.
_SHA = re.compile(r"[0-9a-f]{4,64}")
# A full object id (SHA-1 or SHA-256): what a witness row carries.
_FULL_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RootDecision:
    """One recorded root: what the probe found there, the project and
    checkout its records were homed to, the rule, and the records homed.
    ``recorded`` is the number of commits its records recorded and
    ``held`` what each live checkout holds of them (gone roots)."""

    path: str
    state: str
    project: str
    project_name: str
    checkout: str | None
    rule: str
    memories: int
    episodes: int
    tombstones: int
    recorded: int = 0
    held: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RootlessHome:
    """A record with no recorded root homed by its cwd: inside a recorded
    root (``cwd_in_root``, D4) or in a live checkout found there now
    (``cwd``). ``root`` is that root or checkout path. A record homed by a
    remote key several projects carry (``remote_ambiguous``) is listed
    too: ``root`` names the key and ``carriers`` the projects carrying
    it, the one it was homed in first."""

    record: str
    kind: str
    cwd: str
    root: str
    project: str
    checkout: str | None
    rule: str
    carriers: tuple[str, ...] = ()


@dataclass(frozen=True)
class LegacyQuestion:
    """A ``legacy`` question the run opened (line L6)."""

    proposal: str
    checkout: str
    at: str
    legacy_project: str
    root: str
    line: str


@dataclass(frozen=True)
class MigrationTotals:
    """The rows the run wrote (table rows added, log rows appended) and
    the record rows it rewrote, which is 0 (M1)."""

    projects: int = 0
    checkouts: int = 0
    views: int = 0
    memory_homes: int = 0
    tombstone_homes: int = 0
    episode_homes: int = 0
    witnesses: int = 0
    proposals: int = 0
    log_rows: int = 0
    memories_rewritten: int = 0
    tombstones_rewritten: int = 0
    episodes_rewritten: int = 0

    def to_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class MigrationReport:
    """What a run did, or with ``dry_run`` what it would do: ``kept`` says
    whether the transaction was committed or rolled back. ``remint`` names
    the checkouts whose token the ladder found superseded by history at the
    probe (design 3.3, ``token_superseded``): the run writes no token, so
    each is listed, here and in the ``membership_migrated`` row, as due a
    token of its own at its next sighting."""

    dry_run: bool
    kept: str
    machine: str
    roots: tuple[RootDecision, ...] = ()
    rootless: tuple[RootlessHome, ...] = ()
    proposals: tuple[LegacyQuestion, ...] = ()
    totals: MigrationTotals = field(default_factory=MigrationTotals)
    homes_by_rule: Mapping[str, int] = field(default_factory=dict)
    remint: tuple[str, ...] = ()

    @property
    def homed(self) -> int:
        t = self.totals
        return t.memory_homes + t.tombstone_homes + t.episode_homes

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "kept": self.kept,
            "machine": self.machine,
            "roots": [dataclasses.asdict(r) for r in self.roots],
            "rootless": [dataclasses.asdict(r) for r in self.rootless],
            "proposals": [dataclasses.asdict(p) for p in self.proposals],
            "totals": self.totals.to_dict(),
            "homes_by_rule": dict(self.homes_by_rule),
            "remint": list(self.remint),
        }

    def lines(self) -> list[str]:
        """The report as `bettermemory membership migrate` prints it."""
        head = "bettermemory membership migrate"
        out = [
            f"{head} (dry run: {self.kept})" if self.dry_run else f"{head}: {self.kept}"
        ]
        out.append(f"roots ({len(self.roots)}):")
        for r in self.roots:
            where = f"checkout {r.checkout}" if r.checkout else "no checkout"
            held = (
                "; held of its "
                + str(r.recorded)
                + " recorded commits: "
                + ", ".join(f"{k} {n}" for k, n in sorted(r.held.items()))
                if r.held
                else ""
            )
            out.append(
                f'  {r.path}: {r.state}, project "{r.project_name}" ({r.project}), '
                f"{where}, rule {r.rule}; {r.memories} memories, {r.episodes} "
                f"episodes, {r.tombstones} removed memories homed{held}"
            )
        out.append(f"legacy questions opened ({len(self.proposals)}):")
        for p in self.proposals:
            out.append(f"  {p.proposal} in checkout {p.checkout} at {p.at}: {p.line}")
        out.append(
            "records with no root homed by their cwd, or by a remote several "
            f"projects carry ({len(self.rootless)}):"
        )
        for x in self.rootless:
            where = f"checkout {x.checkout}" if x.checkout else "no checkout"
            carried = f"; carried by {', '.join(x.carriers)}" if x.carriers else ""
            out.append(
                f"  {x.record} ({x.kind}) at {x.cwd}: in {x.root}, project "
                f"{x.project}, {where}, rule {x.rule}{carried}"
            )
        t = self.totals
        out.append(
            f"totals: {t.projects} projects, {t.checkouts} checkouts, {t.views} views, "
            f"{t.memory_homes} memory homes, {t.tombstone_homes} removed-memory homes, "
            f"{t.episode_homes} episode homes, {t.witnesses} witnesses, "
            f"{t.proposals} questions, {t.log_rows} log rows"
        )
        out.append(
            f"rows rewritten: {t.memories_rewritten} memories, "
            f"{t.tombstones_rewritten} removed memories, {t.episodes_rewritten} episodes"
        )
        out.append("homes by rule:")
        for rule, n in sorted(self.homes_by_rule.items()):
            out.append(f"  {rule}: {n}")
        if self.remint:
            out.append(f"tokens due at the next sighting ({len(self.remint)}):")
        for k in self.remint:
            out.append(
                f"  checkout {k}: carries the token of another line (history "
                "disagrees); the migration writes no token"
            )
        return out


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


class _RolledBack(Exception):
    """Raised inside a dry run's transaction to roll it back."""


def unhomed(store: Store) -> bool:
    """Whether any memory, removed memory or episode has no home row: each
    record table read against the homes index, stopping at the first
    record without one."""
    conn = store.conn
    for table, _kind in _RECORD_TABLES:
        found = conn.execute(
            f"SELECT 1 FROM {table} t WHERE NOT EXISTS "
            "(SELECT 1 FROM homes h WHERE h.record_id = t.id) LIMIT 1"
        ).fetchone()
        if found is not None:
            return True
    return False


def migrate(
    store: Store,
    identity: MachineIdentity,
    *,
    dry_run: bool = False,
    now: Callable[[], datetime] = utcnow,
    write_token: bool = True,
    strict: bool = False,
) -> MigrationReport:
    """Home every record without a home (design 8.6), in one transaction,
    ending in one ``membership_migrated`` row. With `dry_run` the
    transaction is rolled back after the report is made, so the store is
    as it was and the report shows what a run writes. `write_token` and
    `strict` are the ``[membership]`` switches: whether the next sighting
    writes a checkout token (a probe never does), and the A3 switch the
    ladder resolves live roots under. Idempotent: a store with every
    record homed is not written and the report counts 0."""
    if dry_run and store.in_batch:
        raise RuntimeError("a dry run needs a transaction of its own")
    if not unhomed(store):
        return MigrationReport(
            dry_run=dry_run, kept="nothing to home", machine=identity.id
        )
    run = _Run(store, identity, now=now, write_token=write_token, strict=strict)
    if store.in_batch:
        return run.run(dry_run=False)
    if not dry_run:
        with store.batch():
            return run.run(dry_run=False)
    made: list[MigrationReport] = []
    try:
        with store.batch():
            made.append(run.run(dry_run=True))
            raise _RolledBack
    except _RolledBack:
        pass
    return made[0]


def run_command(*, dry_run: bool) -> None:
    """`bettermemory membership migrate [--dry-run]`: the run over this
    process's store and machine, its report printed. The store is opened
    directly, not through the CLI's context, so a dry run is never
    preceded by the run at open. Exits 2 when there is no store, and when
    the store file is inside a backup bundle (`backup.bundle_refusal`):
    the run writes, and would change the bundle's snapshot."""
    import sys

    from .backup import bundle_refusal
    from .config import STORE_FILENAME, load_config

    config = load_config()
    path = config.resolved_directory() / STORE_FILENAME
    if not path.is_file():
        sys.stderr.write(f"bettermemory: no bettermemory 9 store at {path}\n")
        raise SystemExit(2)
    refusal = bundle_refusal(path)
    if refusal is not None:
        sys.stderr.write(f"bettermemory: {refusal}\n")
        raise SystemExit(2)
    # The migration writes, from the owner's process: a store whose key
    # moved is rekeyed here as `open_or_create` would.
    store = Store.open(path, allow_rekey=True)
    try:
        report = migrate(
            store,
            location.process_machine(),
            dry_run=dry_run,
            write_token=config.membership.write_token,
            strict=config.membership.strict,
        )
    finally:
        store.close()
    for line in report.lines():
        print(line)


def settle_at_open(store: Store, identity: MachineIdentity) -> list[str]:
    """The settle pass at an open (design 8.6): every open legacy question
    whose evidence decides nothing is closed as separate
    (`Membership.settle_legacy_questions`), so a store a candidate before
    9.0.0rc6 opened such questions in asks no one. Returns the ids
    closed; nothing when no such question is open, which is one read."""
    if not store.membership_rows("proposals", where={"state": mb.OPEN}):
        return []
    model = mb.Membership.open(store, identity)
    return [pr.id for pr in model.settle_legacy_questions()]


def ensure_migrated(
    store: Store, identity: MachineIdentity, *, config: Config
) -> MigrationReport | None:
    """The run-at-open hook (design 8.6): migrate when any record has no
    home (a first open of an upgraded store, or a re-upgrade after a
    rollback, design 8.12), else nothing. Returns the report of a run
    that homed records, None otherwise."""
    if not unhomed(store):
        return None
    report = migrate(
        store,
        identity,
        write_token=config.membership.write_token,
        strict=config.membership.strict,
    )
    return report if report.homed else None


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _norm(path: str) -> str:
    stripped = path.rstrip("/\\")
    return stripped or path


def _inside(path: str, root: str) -> bool:
    path, root = _norm(path), _norm(root)
    return path == root or any(path.startswith(root + sep) for sep in ("/", os.sep))


def _basename(path: str) -> str:
    return os.path.basename(_norm(path).replace("\\", "/")) or path


def _origin(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _text(origin: Mapping[str, Any] | None, key: str) -> str | None:
    value = origin.get(key) if origin is not None else None
    return value if isinstance(value, str) and value.strip() else None


def _id_order(membership_id: str) -> tuple[int, str]:
    """Membership ids in the order they were minted (P9 before P10)."""
    return len(membership_id), membership_id


def _repository_name(key: str) -> str:
    """The repository's name in a remote key: its last path segment,
    without a ``.git`` suffix (``github.com/acme/app`` gives ``app``)."""
    path = key[len("local:") :] if key.startswith("local:") else key
    last = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if last.endswith(".git"):
        last = last[: -len(".git")]
    return last or key


def _shas(values: Iterable[Any]) -> set[str]:
    """The commit ids among `values`, in lowercase, full or abbreviated."""
    return {
        v.strip().lower()
        for v in values
        if isinstance(v, str) and _SHA.fullmatch(v.strip().lower())
    }


def _json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str) or not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return value if isinstance(value, list) else []


@dataclass
class _Record:
    id: str
    kind: str
    table: str
    origin: dict[str, Any] | None
    first: str
    last: str
    strong: set[str] = field(default_factory=set)
    weak: set[str] = field(default_factory=set)


@dataclass
class _Decision:
    path: str
    state: str
    project: str
    checkout: str | None
    rule: str
    recorded: int = 0
    held: dict[str, int] = field(default_factory=dict)
    #: Whether the overlap is worth a question (`membership.legacy_asks`).
    asks: bool = False


@dataclass
class _Live:
    checkout: str
    path: str
    history: mb.HistoryReader | None


@dataclass
class _Probe:
    state: str
    capture: location.Capture | None = None


class _Run:
    """One migration over one store, inside the caller's transaction."""

    def __init__(
        self,
        store: Store,
        identity: MachineIdentity,
        *,
        now: Callable[[], datetime],
        write_token: bool,
        strict: bool,
    ) -> None:
        self.store = store
        self.identity = identity
        self.machine = identity.id
        self.now = now
        self.write_token = write_token
        self.strict = strict
        self.model = mb.Membership(store, identity.id, now=now)
        self.stamp = isoformat_utc(now().astimezone(timezone.utc))
        self.decided: dict[str, _Decision] = {}
        self.live: dict[str, _Live] = {}
        self.live_complete = False
        self.remint: list[str] = []
        self.rootless: list[RootlessHome] = []
        self.questions: list[LegacyQuestion] = []
        self.homes_by_rule: Counter[str] = Counter()
        self.homed: Counter[str] = Counter()
        self.per_root: dict[str, Counter[str]] = defaultdict(Counter)
        self.resolved_cwd: dict[str, mb.Resolution | None] = {}

    # -- rows --------------------------------------------------------------

    def _put(self, kind: str, row: Mapping[str, Any]) -> None:
        self.store.put_membership(kind, row)

    def _event(self, kind: str, /, **payload: Any) -> None:
        self.store.record_membership(kind, payload)

    def _count(self, table: str) -> int:
        row = self.store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0])

    def _last_seq(self) -> int:
        row = self.store.conn.execute("SELECT MAX(seq) FROM log").fetchone()
        return int(row[0] or 0)

    def _digest(self, table: str) -> str:
        h = hashlib.sha256()
        for row in self.store.conn.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            h.update(repr(tuple(row)).encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()

    def _name(self, project: str) -> str:
        if project == mb.GLOBAL:
            return mb.GLOBAL
        try:
            return self.model.project(self.model.canonical(project) or project).name
        except KeyError:
            return project

    def _canon(self, project: str) -> str:
        return self.model.canonical(project) or project

    # -- reading the records -----------------------------------------------

    def _unhomed(self) -> list[_Record]:
        out: list[_Record] = []
        conn = self.store.conn
        for table, kind in _RECORD_TABLES:
            if kind == "memory":
                columns = "id, origin_json, created, updated, verified_head, verified_commits_json"
            else:
                columns = "id, origin_json, created, created, NULL, NULL"
            for row in conn.execute(
                f"SELECT {columns} FROM {table} t WHERE NOT EXISTS "
                "(SELECT 1 FROM homes h WHERE h.record_id = t.id) ORDER BY rowid"
            ):
                rec = _Record(
                    id=str(row[0]),
                    kind=kind,
                    table=table,
                    origin=_origin(row[1]),
                    first=str(row[2]),
                    last=str(row[3]),
                )
                rec.strong |= _shas([row[4]])
                rec.weak |= _shas(_json_list(row[5]))
                out.append(rec)
        return out

    def _evidence(self, records: Sequence[_Record]) -> None:
        """The commits each memory's verifications recorded beyond its own
        row: the verifications table and the log's ``verify`` rows."""
        mine = {r.id: r for r in records if r.kind == "memory"}
        if not mine:
            return
        conn = self.store.conn
        for memory_id, head, commits in conn.execute(
            "SELECT memory_id, verified_head, verified_commits_json FROM verifications"
        ):
            rec = mine.get(str(memory_id))
            if rec is not None:
                rec.strong |= _shas([head])
                rec.weak |= _shas(_json_list(commits))
        for memory_id, head, commits in conn.execute(
            "SELECT json_extract(payload, '$.id'), json_extract(payload, "
            "'$.verified_head'), json_extract(payload, '$.verified_commits') "
            "FROM log WHERE kind = 'verify'"
        ):
            rec = mine.get(str(memory_id))
            if rec is not None:
                rec.strong |= _shas([head])
                rec.weak |= _shas(_json_list(commits))

    def _moves(self) -> Counter[tuple[str, str]]:
        """Recorded re-homes: each memory's root as its ``memory_put`` rows
        carried it, one count per change from one root to another."""
        roots: dict[str, list[str | None]] = defaultdict(list)
        for memory_id, root in self.store.conn.execute(
            "SELECT json_extract(payload, '$.memory.id'), json_extract(payload, "
            "'$.memory.origin.worktree_root') FROM log WHERE kind = 'memory_put' "
            "ORDER BY seq"
        ):
            if isinstance(memory_id, str):
                roots[memory_id].append(
                    _norm(root) if isinstance(root, str) and root else None
                )
        moves: Counter[tuple[str, str]] = Counter()
        for seq in roots.values():
            for a, b in zip(seq, seq[1:]):
                if a and b and a != b:
                    moves[(a, b)] += 1
        return moves

    def _prior(self) -> dict[str, dict[str, Any]]:
        """The decisions earlier runs on this machine recorded per root."""
        out: dict[str, dict[str, Any]] = {}
        for row in self.store.membership_log([MIGRATED]):
            try:
                payload = json.loads(row.payload)
            except ValueError:
                continue
            if not isinstance(payload, dict) or payload.get("machine") != self.machine:
                continue
            for d in payload.get("roots") or ():
                if isinstance(d, dict) and isinstance(d.get("path"), str):
                    out[_norm(d["path"])] = d
        return out

    def _known_view(self, path: str) -> mb.Checkout | None:
        """The newest checkout seen at `path` on this machine."""
        best: tuple[int, str] | None = None
        for v in self.store.membership_rows(
            "checkout_views", where={"machine": self.machine, "path": _norm(path)}
        ):
            key = (int(v["seen_order"]), str(v["checkout"]))
            if best is None or key > best:
                k = self.model.checkout(str(v["checkout"]))
                if k.kind != mb.UNRESOLVED:
                    best = key
        return None if best is None else self.model.checkout(best[1])

    # -- probing -----------------------------------------------------------

    def _probe(self, path: str, *, at_root: bool) -> _Probe:
        """What `path` holds now, read without writing: LIVE when it is a
        repository (with `at_root`, one whose top level it is) or a folder
        project; GONE when nothing is there; UNREACHABLE when the OS could
        not say or HEAD is not on this disk; REPLACED otherwise."""
        try:
            os.stat(path)
        except FileNotFoundError:
            return _Probe(GONE)
        except NotADirectoryError:
            return _Probe(GONE)
        except (OSError, ValueError):
            return _Probe(UNREACHABLE)
        cap = location.capture(
            path, write_token=False, use_cache=False, budget=location.FETCH_BUDGET
        )
        if cap.state == location.INDETERMINATE or cap.evicted:
            return _Probe(UNREACHABLE, cap)
        if cap.state == location.REPO or cap.kind == location.FOLDER:
            if cap.root is None:
                return _Probe(REPLACED, cap)
            if not at_root or os.path.realpath(path) == cap.root:
                return _Probe(LIVE, cap)
        return _Probe(REPLACED, cap)

    def _location(self, cap: location.Capture) -> mb.Location:
        history: mb.HistoryReader | None = None
        if cap.state == location.REPO and cap.common_dir:
            history = location.RepositoryHistory(cap.common_dir)
        loc = location.to_location(cap, machine=self.machine, history=history)
        if loc.token_status == "skipped:policy" and self.write_token:
            # The probe wrote nothing; the next sighting writes the token.
            loc = dataclasses.replace(loc, token_status="n/a")
        return loc

    def _resolve(self, cap: location.Capture) -> mb.Resolution | None:
        res = self.model.resolve(self._location(cap), strict=self.strict)
        if res.checkout is None or res.project is None:
            return None
        if res.remint_token:
            self.remint.append(res.checkout)
        return res

    def _live_set(self) -> dict[str, _Live]:
        """The live checkouts of this machine whose history can be asked:
        the live roots resolved here, and the other checkouts with a live
        view now (a later run)."""
        if self.live_complete:
            return self.live
        self.live_complete = True
        for k in self.model.checkouts():
            if (
                k.id in self.live
                or k.machine != self.machine
                or k.kind not in (mb.PRIMARY, mb.LINKED)
                or k.legacy
            ):
                continue
            path = self.model.live_view(k)
            if path is None:
                continue
            probe = self._probe(path, at_root=True)
            if probe.state != LIVE or probe.capture is None:
                continue
            common = probe.capture.common_dir
            history = location.RepositoryHistory(common) if common else None
            self.live[k.id] = _Live(k.id, path, history)
        return self.live

    def _live_project(self, live: _Live) -> str:
        return self._canon(self.model.checkout(live.checkout).project)

    def _remotes(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for row in self.store.membership_rows("project_remotes"):
            out[self._canon(str(row["project"]))].add(str(row["remote_key"]))
        return out

    # -- homes ---------------------------------------------------------------

    def _home(
        self, rec: _Record, project: str, checkout: str | None, rule: str
    ) -> None:
        full = RULE_PREFIX + rule
        self._put(
            "home",
            {
                "record_id": rec.id,
                "kind": rec.kind,
                "project": project,
                "checkout": checkout,
                "rule": full,
                "decided": self.stamp,
            },
        )
        self.homes_by_rule[full] += 1
        self.homed[rec.table] += 1

    # -- the steps -----------------------------------------------------------

    def run(self, *, dry_run: bool) -> MigrationReport:
        records = self._unhomed()
        if not records:
            return MigrationReport(
                dry_run=dry_run, kept="nothing to home", machine=self.machine
            )
        before = {table: self._digest(table) for table, _k in _RECORD_TABLES}
        tables = (
            "projects",
            "checkouts",
            "checkout_views",
            "project_witnesses",
            "proposals",
        )
        counts = {t: self._count(t) for t in tables}
        seq = self._last_seq()
        self.model.record_machine(self.identity)
        self._evidence(records)
        by_root: dict[str, list[_Record]] = defaultdict(list)
        rootless: list[_Record] = []
        for rec in records:
            root = _text(rec.origin, "worktree_root")
            if root is not None:
                by_root[_norm(root)].append(rec)
            else:
                rootless.append(rec)
        order = sorted(by_root, key=lambda r: (-len(by_root[r]), r))
        probes = {r: self._probe(r, at_root=True) for r in order}
        for r in order:
            if probes[r].state == LIVE:
                self._live_root(r, by_root[r], probes[r])
        prior = self._prior()
        moves = self._moves()
        for r in order:
            if r not in self.decided:
                self._gone_root(r, by_root[r], probes[r].state, prior, moves)
        for r in order:
            d = self.decided[r]
            for rec in by_root[r]:
                self._home(rec, d.project, d.checkout, d.rule)
                self.per_root[r][rec.table] += 1
        for rec in rootless:
            self._rootless(rec, prior)
        self._legacy_questions(by_root)
        totals = self._totals(before, counts, seq)
        roots = tuple(self._root_report(r) for r in order)
        self._event(
            MIGRATED,
            machine=self.machine,
            dry_run=dry_run,
            counts={**totals.to_dict(), "log_rows": totals.log_rows + 1},
            roots=[
                {
                    "path": d.path,
                    "state": d.state,
                    "project": d.project,
                    "checkout": d.checkout,
                    "rule": d.rule,
                }
                for d in (self.decided[r] for r in order)
            ],
            rootless=len(self.rootless),
            proposals=[q.proposal for q in self.questions],
            remint=list(dict.fromkeys(self.remint)),
        )
        totals = dataclasses.replace(totals, log_rows=self._last_seq() - seq)
        return MigrationReport(
            dry_run=dry_run,
            kept=(
                "one transaction, rolled back; nothing was kept"
                if dry_run
                else "committed in one transaction"
            ),
            machine=self.machine,
            roots=roots,
            rootless=tuple(self.rootless),
            proposals=tuple(self.questions),
            totals=totals,
            homes_by_rule=dict(sorted(self.homes_by_rule.items())),
            remint=tuple(dict.fromkeys(self.remint)),
        )

    def _live_root(self, root: str, recs: Sequence[_Record], probe: _Probe) -> None:
        """A live root: the ladder decides its checkout; the commits its
        records recorded that it holds become witnesses of its project."""
        cap = probe.capture
        res = self._resolve(cap) if cap is not None else None
        if cap is None or res is None or res.checkout is None or res.project is None:
            probe.state = UNREACHABLE
            return
        history = (
            location.RepositoryHistory(cap.common_dir)
            if cap.state == location.REPO and cap.common_dir
            else None
        )
        self.decided[root] = _Decision(
            root, LIVE, res.project, res.checkout, "live:" + res.rule
        )
        if cap.state == location.REPO:
            self.live[res.checkout] = _Live(res.checkout, cap.root or root, history)
        recorded: set[str] = set()
        for rec in recs:
            recorded |= rec.strong | rec.weak
        self.decided[root].recorded = len(recorded)
        if history is None or not recorded:
            return
        # An abbreviated id is held as the one object it names, and the
        # witness carries that object's full id (B19).
        held = sorted(set(history.expand(sorted(recorded)).values()))
        project = self._canon(res.project)
        for sha in held:
            self.model.add_witness(
                project, sha, source="migration:recorded_commit", history=history
            )

    def _gone_root(
        self,
        root: str,
        recs: Sequence[_Record],
        state: str,
        prior: Mapping[str, Mapping[str, Any]],
        moves: Counter[tuple[str, str]],
    ) -> None:
        first = min((r.first for r in recs), default=self.stamp)
        last = max((r.last for r in recs), default=self.stamp)
        earlier = prior.get(root)
        if earlier is not None and isinstance(earlier.get("project"), str):
            # Decided by an earlier run: the same checkout and rule; a root
            # that was live then is a recorded view of its checkout now.
            rule = "view" if earlier.get("state") == LIVE else str(earlier.get("rule"))
            checkout = earlier.get("checkout")
            self.decided[root] = _Decision(
                root,
                state,
                self._canon(str(earlier["project"])),
                checkout if isinstance(checkout, str) else None,
                rule,
            )
            return
        seen = self._known_view(root)
        if seen is not None:
            self.decided[root] = _Decision(
                root, state, self._canon(seen.project), seen.id, "view"
            )
            return
        live = self._live_set()
        live_paths = {_norm(x.path): x.checkout for x in live.values()}
        targets: dict[str, int] = defaultdict(int)
        for (a, b), n in moves.items():
            if a != root:
                continue
            d = self.decided.get(b)
            if d is not None and d.state == LIVE and d.checkout is not None:
                targets[d.checkout] += n
            elif b in live_paths:
                targets[live_paths[b]] += n
        if len(targets) == 1:
            ((kid, moved),) = targets.items()
            self._prior_view(kid, root, first, last, moved)
            self.decided[root] = _Decision(
                root,
                state,
                self._canon(self.model.checkout(kid).project),
                kid,
                "rehome",
            )
            return
        strong: set[str] = set()
        weak: set[str] = set()
        for rec in recs:
            strong |= rec.strong
            weak |= rec.weak
        weak -= strong
        keys = {
            k
            for k in (
                location.remote_key(_text(rec.origin, "repo"), root) for rec in recs
            )
            if k
        }
        remotes = self._remotes()

        def holders(commits: set[str]) -> dict[str, dict[str, set[str]]]:
            """{project: {live checkout: the commits it holds}}."""
            out: dict[str, dict[str, set[str]]] = defaultdict(dict)
            if not commits:
                return out
            for x in live.values():
                if x.history is None:
                    continue
                got = x.history.holds(sorted(commits))
                if got:
                    out[self._live_project(x)][x.checkout] = set(got)
            return out

        def compatible(found: Mapping[str, Any]) -> list[str]:
            return sorted(
                p for p in found if not keys or not remotes.get(p) or keys & remotes[p]
            )

        by_strong, by_weak = holders(strong), holders(weak)
        comp_s, comp_w = compatible(by_strong), compatible(by_weak)
        same_name = {
            self._live_project(x)
            for x in live.values()
            if _basename(x.path) == _basename(root)
        }
        held: dict[str, int] = defaultdict(int)
        for found in (by_strong, by_weak):
            for per in found.values():
                for kid, got in per.items():
                    held[kid] += len(got)
        chosen: str | None = None
        rule = "none"
        if len(comp_s) == 1:
            chosen, rule = comp_s[0], "lineage"
        elif not comp_s and len(comp_w) == 1 and comp_w[0] in same_name:
            chosen, rule = comp_w[0], "lineage_cited+same_name"
        elif not strong and not weak and keys:
            carriers = sorted(
                {self._live_project(x) for x in live.values()}
                & {p for p, ks in remotes.items() if ks & keys}
            )
            if len(carriers) == 1:
                chosen, rule = carriers[0], "remote"
        asks = False
        if chosen is None:
            rule = "ambiguous" if len(comp_s) > 1 else "none"
            # Whether the overlap decides anything (`membership.legacy_asks`):
            # more than one compatible holder of the records' own commits,
            # or compatible holders by citation under the same name. Cited
            # only under another name, or held through a remote the holder
            # does not carry, is no question: the project stands apart.
            asks = mb.legacy_asks(
                rule=rule,
                same_name=bool(same_name & set(comp_w)),
                compatible=bool(comp_s or comp_w),
            )
            chosen = self._mint(
                _basename(root),
                reason="migration_no_live_checkout",
                provisional="ambiguous_lineage" if rule == "ambiguous" else None,
            )
            held_anywhere: set[str] = set()
            for found in (by_strong, by_weak):
                for per in found.values():
                    for got in per.values():
                        held_anywhere |= got
            # A witness row carries a full id only (B19): an abbreviation
            # no live repository resolves names no object to record.
            for sha in sorted(strong - held_anywhere):
                if _FULL_SHA.fullmatch(sha):
                    self.model.add_witness(
                        chosen, sha, source="migration:verified_head"
                    )
            carried = {k for ks in remotes.values() for k in ks}
            for key in sorted(keys - carried):
                self._put("project_remote", {"project": chosen, "remote_key": key})
        kid = self._legacy_checkout(chosen, root, rule, keys, first, last)
        self.decided[root] = _Decision(
            root,
            state,
            chosen,
            kid,
            rule,
            recorded=len(strong | weak),
            held=dict(held) if rule in ("none", "ambiguous") else {},
            asks=asks,
        )

    def _mint(self, name: str, *, reason: str, provisional: str | None) -> str:
        pid = self.store.next_membership_id("projects", "P")
        self._put(
            "project",
            {
                "id": pid,
                "name": name,
                "merged_into": None,
                "provisional": provisional,
                "parent": None,
                "tenant": None,
                "folder": 0,
                "boundary": 0,
                "minted": self.stamp,
            },
        )
        self._event(
            "project_minted",
            project=pid,
            name=name,
            reason=reason,
            provisional=provisional,
            parent=None,
            tenant=None,
        )
        return pid

    def _legacy_checkout(
        self,
        project: str,
        path: str,
        rule: str,
        remotes: Iterable[str],
        first: str,
        last: str,
    ) -> str:
        """A checkout no capture saw: the root's records were written in it,
        its object is unknown, its one view is the recorded root."""
        kid = self.store.next_membership_id("checkouts", "K")
        self._put(
            "checkout",
            {
                "id": kid,
                "project": project,
                "machine": self.machine,
                "kind": mb.PRIMARY,
                "object_device": None,
                "object_inode": None,
                "object_birth": None,
                "common_device": None,
                "common_inode": None,
                "common_birth": None,
                "git_dir": None,
                "token": None,
                "token_status": "n/a",
                "stamps_json": [],
                "last_head": None,
                "branch": None,
                "remotes_json": sorted(set(remotes)),
                "manifest_json": None,
                "legacy": 1,
                "retired": 0,
                "registered": self.stamp,
                "unresolved_json": None,
            },
        )
        self._put(
            "checkout_view",
            {
                "checkout": kid,
                "machine": self.machine,
                "path": path,
                "first_seen": first,
                "last_seen": last,
                "seen_order": self.store.next_membership_number(
                    "checkout_views", "seen_order"
                ),
            },
        )
        self._event(
            "checkout_registered",
            checkout=kid,
            project=project,
            path=path,
            machine=self.machine,
            checkout_kind=mb.PRIMARY,
            rule=RULE_PREFIX + rule,
            token="n/a",
            legacy=True,
        )
        return kid

    def _prior_view(
        self, checkout: str, path: str, first: str, last: str, moved: int
    ) -> None:
        """`path` as a view of `checkout` older than every view it has."""
        k = self.model.checkout(checkout)
        if any(v.machine == self.machine and _norm(v.path) == path for v in k.views):
            return
        order = min((v.order for v in k.views), default=1) - 1
        self._put(
            "checkout_view",
            {
                "checkout": checkout,
                "machine": self.machine,
                "path": path,
                "first_seen": first,
                "last_seen": last,
                "seen_order": order,
            },
        )
        self._event(
            "checkout_view_added",
            checkout=checkout,
            path=path,
            machine=self.machine,
            evidence="recorded re-home in the log",
            moved_records=moved,
        )

    def _legacy_questions(self, by_root: Mapping[str, Sequence[_Record]]) -> None:
        """One ``legacy`` question per project with no live checkout whose
        recorded commits a live checkout partly holds: in the checkout
        holding the most (D5, line L6). A later run adding records to a
        project whose question is open restates its counts."""
        open_by_project: dict[str, mb.Proposal] = {}
        for pr in self.model.pending():
            legacy = pr.evidence.get("unresolved_project")
            if (
                pr.kind == mb.LEGACY
                and pr.reason == LEGACY_REASON
                and isinstance(legacy, str)
            ):
                open_by_project[legacy] = pr
        for root, d in self.decided.items():
            if d.rule not in ("none", "ambiguous") or root not in by_root:
                continue
            memories, episodes = self._record_counts(d.project)
            known = open_by_project.get(d.project)
            if known is not None:
                self._restate(known, memories, episodes)
                continue
            if not d.held or d.checkout is None or not d.asks:
                continue
            at, n_held = sorted(d.held.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            k = self.model.checkout(at)
            live_project = self._canon(k.project)
            where = self.live[at].path if at in self.live else self._view_path(k)
            name, n_memories, n_episodes, n_recorded = (
                _basename(root),
                memories,
                episodes,
                d.recorded,
            )

            def line(q: str, _root: str = root, _project: str = live_project) -> str:
                return mb.line_legacy(
                    name,
                    n_memories,
                    n_episodes,
                    _root,
                    n_held,
                    self._name(_project),
                    _project,
                    recorded=n_recorded,
                    question=q,
                )

            # `unresolved_project` and `unresolved_checkout` are the keys a
            # `legacy` question is answered by (`Membership._answer_join`,
            # `_answer_new`): join merges the records' project into this
            # checkout's, new keeps it a project of its own.
            evidence = {
                "unresolved_project": d.project,
                "unresolved_checkout": d.checkout,
                "path": root,
                "memories": memories,
                "episodes": episodes,
                "held": n_held,
                "recorded": d.recorded,
                "rule": d.rule,
            }
            pr = self.model.open_proposal(
                checkout=at,
                kind=mb.LEGACY,
                candidates=[live_project],
                evidence=evidence,
                reason=LEGACY_REASON,
                line=line,
                distinct="unresolved_project",
            )
            open_by_project[d.project] = pr
            self.questions.append(
                LegacyQuestion(pr.id, at, where, d.project, root, pr.line)
            )

    def _view_path(self, k: mb.Checkout) -> str:
        newest = k.newest_view
        return newest.path if newest is not None else k.id

    def _record_counts(self, project: str) -> tuple[int, int]:
        rows = self.store.membership_rows("homes", where={"project": project})
        memories = sum(1 for r in rows if r["kind"] == "memory")
        return memories, len(rows) - memories

    def _restate(self, pr: mb.Proposal, memories: int, episodes: int) -> None:
        ev = dict(pr.evidence)
        if (ev.get("memories"), ev.get("episodes")) == (memories, episodes):
            return
        ev["memories"], ev["episodes"] = memories, episodes
        live_project = pr.candidates[0] if pr.candidates else ""
        line = mb.line_legacy(
            _basename(str(ev.get("path") or "")),
            memories,
            episodes,
            str(ev.get("path") or ""),
            int(ev.get("held") or 0),
            self._name(live_project),
            live_project,
            recorded=int(ev.get("recorded") or 0),
            question=pr.id,
        )
        self.model.restate_proposal(pr.id, line=line, evidence=ev)

    def _rootless(self, rec: _Record, prior: Mapping[str, Mapping[str, Any]]) -> None:
        """A record with no recorded root: its remote, its cwd inside a
        recorded root (D4), a live checkout at its cwd, else global."""
        origin = rec.origin
        if origin is None:
            self._home(rec, mb.GLOBAL, None, "global:no_origin")
            return
        cwd = _text(origin, "cwd")
        repo = _text(origin, "repo")
        if repo is not None:
            key = location.remote_key(repo, cwd)
            if key is not None:
                self._by_remote(rec, key)
                return
        if cwd is not None:
            inside = self._recorded_root_of(cwd, prior)
            if inside is not None:
                root, project, checkout = inside
                self._home(rec, project, checkout, "cwd_in_root")
                self.rootless.append(
                    RootlessHome(
                        rec.id,
                        rec.kind,
                        cwd,
                        root,
                        project,
                        checkout,
                        RULE_PREFIX + "cwd_in_root",
                    )
                )
                return
            res = self._resolved_at(cwd)
            if res is not None and res.project is not None:
                rule = "cwd:" + res.rule
                k = self.model.checkout(res.checkout) if res.checkout else None
                where = self._view_path(k) if k is not None else cwd
                self._home(rec, res.project, res.checkout, rule)
                self.rootless.append(
                    RootlessHome(
                        rec.id,
                        rec.kind,
                        cwd,
                        where,
                        res.project,
                        res.checkout,
                        RULE_PREFIX + rule,
                    )
                )
                return
        self._home(rec, mb.GLOBAL, None, "global:outside_repository")

    def _by_remote(self, rec: _Record, key: str) -> None:
        """A record with no recorded root and a remote: homed in the one
        project carrying the remote key, a live one first
        (``remote_no_root``), else one with no live checkout, decided in
        this run or an earlier one (``remote_no_live_checkout``). With
        several carriers nothing is minted: the record goes to the one
        minted first (``remote_ambiguous``), and the report names them all.
        Only when no project carries the key is a project minted for it,
        named after the repository and carrying the key, so no key is
        carried by two projects the migration minted. A project an earlier
        run minted under the name ``remote:<key>`` is found by that name as
        well as by its key."""
        carriers = sorted(
            {p for p, keys in self._remotes().items() if key in keys}, key=_id_order
        )
        live = {self._live_project(x) for x in self._live_set().values()}
        live_carriers = [p for p in carriers if p in live]
        if len(live_carriers) == 1:
            self._home(rec, live_carriers[0], None, "remote_no_root")
            return
        if len(carriers) == 1:
            self._home(rec, carriers[0], None, "remote_no_live_checkout")
            return
        if carriers:
            self._home(rec, carriers[0], None, "remote_ambiguous")
            self.rootless.append(
                RootlessHome(
                    rec.id,
                    rec.kind,
                    _text(rec.origin, "cwd") or "",
                    f"remote {key}",
                    carriers[0],
                    None,
                    RULE_PREFIX + "remote_ambiguous",
                    tuple(carriers),
                )
            )
            return
        named = sorted(
            (p.id for p in self.model.projects() if p.name == f"remote:{key}"),
            key=_id_order,
        )
        if named:
            pid = self._canon(named[0])
        else:
            pid = self._mint(
                _repository_name(key),
                reason="migration_remote_no_checkout",
                provisional=None,
            )
        self._put("project_remote", {"project": pid, "remote_key": key})
        self._home(rec, pid, None, "remote_no_live_checkout")

    def _recorded_root_of(
        self, cwd: str, prior: Mapping[str, Mapping[str, Any]]
    ) -> tuple[str, str, str | None] | None:
        """The recorded root `cwd` lies in, longest first: a root decided
        here or by an earlier run, or a view of a checkout of this machine;
        with its project and checkout."""
        found: list[tuple[str, str, str | None]] = []
        for root, d in self.decided.items():
            if _inside(cwd, root):
                found.append((root, d.project, d.checkout))
        for root, earlier in prior.items():
            project = earlier.get("project")
            checkout = earlier.get("checkout")
            if (
                _inside(cwd, root)
                and isinstance(project, str)
                and root not in self.decided
            ):
                found.append(
                    (root, project, checkout if isinstance(checkout, str) else None)
                )
        if not found:
            for v in self.store.membership_rows(
                "checkout_views", where={"machine": self.machine}
            ):
                path = str(v["path"])
                if _inside(cwd, path):
                    k = self.model.checkout(str(v["checkout"]))
                    if k.kind != mb.UNRESOLVED:
                        found.append((path, k.project, k.id))
        if not found:
            return None
        root, project, checkout = max(found, key=lambda t: len(_norm(t[0])))
        return root, self._canon(project), checkout

    def _resolved_at(self, cwd: str) -> mb.Resolution | None:
        key = _norm(cwd)
        if key not in self.resolved_cwd:
            probe = self._probe(cwd, at_root=False)
            res = None
            if probe.state == LIVE and probe.capture is not None:
                res = self._resolve(probe.capture)
            self.resolved_cwd[key] = res
        return self.resolved_cwd[key]

    # -- the report ----------------------------------------------------------

    def _totals(
        self, before: Mapping[str, str], counts: Mapping[str, int], seq: int
    ) -> MigrationTotals:
        rewritten: dict[str, int] = {}
        changed_ids: dict[str, set[str]] = defaultdict(set)
        for kind, record_id in self.store.conn.execute(
            "SELECT kind, COALESCE(json_extract(payload, '$.memory.id'), "
            "json_extract(payload, '$.tombstone.id'), json_extract(payload, "
            "'$.episode.id'), json_extract(payload, '$.id')) FROM log WHERE seq > ? "
            f"AND kind IN ({','.join('?' * len(_RECORD_KINDS))})",
            (seq, *_RECORD_KINDS),
        ):
            table = {"memory": "memories", "tombstone": "tombstones"}.get(
                str(kind).rsplit("_", 1)[0], "episodes"
            )
            changed_ids[table].add(str(record_id))
        for table, _k in _RECORD_TABLES:
            same = self._digest(table) == before[table]
            rewritten[table] = 0 if same else max(1, len(changed_ids[table]))
        return MigrationTotals(
            projects=self._count("projects") - counts["projects"],
            checkouts=self._count("checkouts") - counts["checkouts"],
            views=self._count("checkout_views") - counts["checkout_views"],
            memory_homes=self.homed["memories"],
            tombstone_homes=self.homed["tombstones"],
            episode_homes=self.homed["episodes"],
            witnesses=self._count("project_witnesses") - counts["project_witnesses"],
            proposals=self._count("proposals") - counts["proposals"],
            log_rows=self._last_seq() - seq,
            memories_rewritten=rewritten["memories"],
            tombstones_rewritten=rewritten["tombstones"],
            episodes_rewritten=rewritten["episodes"],
        )

    def _root_report(self, root: str) -> RootDecision:
        d = self.decided[root]
        n = self.per_root[root]
        return RootDecision(
            path=root,
            state=d.state,
            project=self._canon(d.project),
            project_name=self._name(d.project),
            checkout=d.checkout,
            rule=RULE_PREFIX + d.rule,
            memories=n["memories"],
            episodes=n["episodes"],
            tombstones=n["tombstones"],
            recorded=d.recorded,
            held=dict(d.held),
        )


__all__ = [
    "GONE",
    "LEGACY_REASON",
    "settle_at_open",
    "LIVE",
    "MIGRATED",
    "REPLACED",
    "RULE_PREFIX",
    "UNREACHABLE",
    "LegacyQuestion",
    "MigrationReport",
    "MigrationTotals",
    "RootDecision",
    "RootlessHome",
    "ensure_migrated",
    "migrate",
    "run_command",
    "unhomed",
]
