"""The `Store` methods behind search, and the document frequency memo.

The FTS candidate query (`query_candidates`), the link reads
(`links_for_many`, `inbound_links`), the declared scopes, the pending
conflict pairs and `contradicted_at`, and the memo behind
`Store.document_frequencies(..., memoised=True)`: `_DOC_FREQS`, keyed on
the log's last memory or home row (`_memory_head`), kept and bounded by
`_memoised_document_frequencies` and `_keep_doc_freqs` and registered with
`_caches`. The admission readers the membership guard pins stay in
`store.py` and import the admission query, the memo's type and its row
mask from here. It lives apart from `store.py` to keep each module under
the package's 2,000-line bound (ENG-96); it imports `store_schema` and
`store_rows`, and `store` only for type checking.

The index's own integrity (ENG-100) is here too: the tokenizer the stored
index text is spelled by, compared at every open with this build's
(`reconciles_tokenizer`, the one line `Store.open` carries), and the
re-spelling an owner's open runs when they differ (`rebuild_index`),
which `bettermemory index rebuild` runs on demand (`cli/index_cmd.py`),
and the checks `doctor` and `backup verify` run on the file's pages and
the index (`index_report`); and the ``PRAGMA optimize`` the daemon runs on
its schedule and at shutdown (`optimize`).
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import logging
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ParamSpec

from . import _caches
from .identity import Actor
from .log import INDEX_REBUILT, STORE_CREATED
from .search import fts_index_text, fts_match_query, tokenizer_fingerprint
from .store_schema import _PROVENANCE_BATCH
from .store_rows import _dt, _load_json

if TYPE_CHECKING:
    from .doctor import Check
    from .store import Store

log = logging.getLogger("bettermemory.store")


# Every memory row's admission inputs for the document frequencies: its
# scopes, the project its ``homes`` row names (null for a row with none)
# and its declared writer, in one query.
_ADMISSION_INPUTS = (
    "SELECT m.id AS id, m.scopes_json AS scopes_json, h.project AS home_project, "
    "m.actor_client AS actor_client, m.actor_model AS actor_model "
    "FROM memories m LEFT JOIN homes h ON h.record_id = m.id"
)


@dataclass
class _DocFreqInputs:
    """What `Store.document_frequencies(..., memoised=True)` keeps for one
    store while its memories stay as they were: the admission inputs of
    every memory row whose scopes parse, grouped by equal inputs, and each
    term's FTS hits. A row is a bit, its position in the scan; `groups`
    maps `(scopes, home project, actor)` to the mask of the rows that carry
    them (the project the row's ``homes`` row names, None for a row with
    none), and `terms` maps a term to the masks of the rows whose body
    matches it and whose body or scopes match it. `head` is the log's last
    memory or home row, `(seq, mac)`, when the inputs were read."""

    head: tuple[int, str]
    groups: tuple[tuple[list[str], str | None, Actor | None, int], ...]
    index: dict[str, int]
    terms: OrderedDict[str, tuple[int, int]]


# The document frequency memo of `Store.document_frequencies(...,
# memoised=True)`, the corpus statistics every search above the prefilter
# threshold prices its BM25 legs with: the per-prompt recall's probe,
# `memory_search` and the review sample. The uncached call reads every
# memory row and runs two FTS queries per term on every search, though the
# rows and the hits change only when a memory does. Every change to the
# memories table is a `memory_put` or `memory_delete` row appended to the
# log in the same transaction (`_mutate`; the fold applies the same rows),
# and each row's MAC covers the rows before it, so the log's last memory
# row names the table's state: the memo keeps a store's admission inputs
# and its terms' hits keyed on that row's sequence number and MAC, and a
# call whose key differs reads them again. A write, an update, a
# tombstone, a restore, a deletion and an import each append such a row; a
# telemetry row does not, so the searches' own events leave the memo warm.
# A record's home is a `home_put` row (beside its memory's, or alone from
# the migration and an import), so the key is the last row of either.
# What is not kept is the admission itself: `admit` is the search's
# predicate (`admission.Admission`), which follows the project table's
# merges and boundaries for the caller, so it runs on every call, once per
# distinct `(scopes, home project, actor)` instead of once per row; the
# counts are then those of the uncached call. A read inside an open
# transaction is served
# uncached, and a result is kept only when the key read after it is the
# key read before it, so a memory committed meanwhile by another
# connection is never kept under the older key. An edit to the memories
# table outside the store, with no log row, is not seen, as with the event
# window memo. Keyed by the store file and its id, so the daemon's worker
# connections share it; bounded to the most recently read stores, and to
# the most recently used terms per store (two masks of one bit per memory
# row each, about 170 bytes a term at 662 memories). The lock guards each
# look-up and each store, the reads run outside it. Registered with
# `_caches`, which empties it before each test.
DOC_FREQ_STORES = 8
DOC_FREQ_TERMS = 4096


def _row_mask(positions: Iterable[int], width: int) -> int:
    """The int whose bit `p` is set for each `p` in `positions`, each
    below `width`; built in a bytearray, linear in `width`."""
    bits = bytearray((width + 7) // 8)
    for position in positions:
        bits[position >> 3] |= 1 << (position & 7)
    return int.from_bytes(bits, "little")


_DOC_FREQS: OrderedDict[tuple[str, str], _DocFreqInputs] = OrderedDict()
_DOC_FREQS_LOCK = threading.Lock()


@_caches.register
def _clear_doc_freqs() -> None:
    with _DOC_FREQS_LOCK:
        _DOC_FREQS.clear()


# -- search seams ---------------------------------------------------------


def query_candidates(
    self: Store,
    text: str,
    *,
    scopes: Sequence[str] | None = None,
    client: str | None = None,
    model: str | None = None,
    max_results: int = 100,
) -> list[tuple[str, float]]:
    """``[(memory_id, bm25), ...]`` ascending, the v8 index query
    verbatim: the MATCH from ``fts_match_query``, scopes as an OR over
    the space-padded list, the actor filters as exact equality that an
    undeclared row never matches, and the cap applied in SQL."""
    if not text.strip():
        return []
    match_query = fts_match_query(text)
    if not match_query:
        return []
    sql = (
        "SELECT m.id, bm25(memories_fts) AS score "
        "FROM memories_fts "
        "JOIN memories m ON m.rowid = memories_fts.rowid "
        "WHERE memories_fts MATCH ? "
    )
    params: list[Any] = [match_query]
    if scopes:
        sql += "AND (" + " OR ".join(["m.scopes_text LIKE ?"] * len(scopes)) + ") "
        params.extend(f"% {s} %" for s in scopes)
    if client is not None:
        sql += "AND m.actor_client IS NOT NULL AND m.actor_client = ? "
        params.append(client)
    if model is not None:
        sql += "AND m.actor_model IS NOT NULL AND m.actor_model = ? "
        params.append(model)
    sql += "ORDER BY score ASC LIMIT ?"
    params.append(int(max_results))
    rows = self._conn.execute(sql, params).fetchall()
    return [(str(row["id"]), float(row["score"])) for row in rows]


def links_for_many(
    self: Store, memory_ids: Iterable[str]
) -> dict[
    str, tuple[list[tuple[str, str, str | None]], list[tuple[str, str, str | None]]]
]:
    """Outbound and inbound link rows per id, the v8 shape: outbound
    ``(type, target_id, note)``, inbound ``(type, source_id, note)``."""
    ids = list(dict.fromkeys(memory_ids))
    out: dict[
        str,
        tuple[list[tuple[str, str, str | None]], list[tuple[str, str, str | None]]],
    ] = {mid: ([], []) for mid in ids}
    if not ids:
        return out
    placeholders = ",".join("?" * len(ids))
    for row in self._conn.execute(
        "SELECT source_id, type, target_id, note FROM memory_links "
        f"WHERE source_id IN ({placeholders}) ORDER BY source_id, type, target_id",
        ids,
    ):
        out[row["source_id"]][0].append((row["type"], row["target_id"], row["note"]))
    for row in self._conn.execute(
        "SELECT target_id, type, source_id, note FROM memory_links "
        f"WHERE target_id IN ({placeholders}) ORDER BY target_id, type, source_id",
        ids,
    ):
        out[row["target_id"]][1].append((row["type"], row["source_id"], row["note"]))
    return out


def inbound_links(
    self: Store, target_ids: Iterable[str], types: Collection[str]
) -> dict[str, list[tuple[str, str, str]]]:
    """``{target: [(type, source_id, source updated)]}`` for the links of
    `types` into each of `target_ids`, oldest source first (id breaking
    a tie). A link's source is always an active memory: the cleanup
    trigger drops a removed one's rows. What `in_force` walks."""
    ids = list(dict.fromkeys(target_ids))
    kinds = sorted(set(types))
    out: dict[str, list[tuple[str, str, str]]] = {}
    if not ids or not kinds:
        return out
    marks = ",".join("?" * len(kinds))
    for start in range(0, len(ids), _PROVENANCE_BATCH):
        batch = ids[start : start + _PROVENANCE_BATCH]
        placeholders = ",".join("?" * len(batch))
        for row in self._conn.execute(
            "SELECT l.target_id AS target_id, l.type AS type, "
            "l.source_id AS source_id, m.updated AS updated "
            "FROM memory_links l JOIN memories m ON m.id = l.source_id "
            f"WHERE l.type IN ({marks}) AND l.target_id IN ({placeholders}) "
            "ORDER BY l.target_id, m.updated, l.source_id",
            (*kinds, *batch),
        ):
            out.setdefault(str(row["target_id"]), []).append(
                (str(row["type"]), str(row["source_id"]), str(row["updated"]))
            )
    return out


def _memory_head(self: Store) -> tuple[int, str]:
    """The log's last memory or home row, or `index_rebuilt` row, `(seq,
    mac)`: the key of the document frequency memo. A rebuild re-spells
    the index text the FTS hits are read from (`rebuild_index`), in this
    process or another, so its row moves the key too. `(0, "")` on a log
    with none. Each MAX is one probe of `log_by_kind`."""
    row = self._conn.execute(
        "SELECT seq, mac FROM log WHERE seq = (SELECT MAX(s) FROM ("
        "SELECT MAX(seq) AS s FROM log WHERE kind = 'memory_put' "
        "UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'memory_delete' "
        "UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'home_put' "
        "UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'home_delete' "
        f"UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = '{INDEX_REBUILT}'))"
    ).fetchone()
    if row is None:
        return 0, ""
    return int(row["seq"]), str(row["mac"])


def _memoised_document_frequencies(
    self: Store,
    unique: list[str],
    admit: Callable[[list[str], str | None, Actor | None], bool],
) -> tuple[int, dict[str, int], dict[str, int]] | None:
    """`document_frequencies` through the document frequency memo."""
    key = (str(self._path), self._store_id)
    head = self._memory_head()
    known: dict[str, tuple[int, int]] = {}
    with _DOC_FREQS_LOCK:
        entry = _DOC_FREQS.get(key)
        if entry is not None and entry.head != head:
            entry = None
        if entry is not None:
            _DOC_FREQS.move_to_end(key)
            for term in unique:
                masks = entry.terms.get(term)
                if masks is not None:
                    entry.terms.move_to_end(term)
                    known[term] = masks
    fresh = entry is None
    if entry is None:
        entry = self._read_doc_freq_inputs(head)
    # The predicate runs on every call, once per group: it is not a
    # function of the store alone.
    admitted = 0
    for scopes, home_project, actor, mask in entry.groups:
        if admit(list(scopes), home_project, actor):
            admitted |= mask
    found: dict[str, tuple[int, int]] = {}
    if admitted:
        found = {t: self._term_masks(t, entry) for t in unique if t not in known}
    if (fresh or found) and self._memory_head() == head:
        self._keep_doc_freqs(key, entry, found, fresh=fresh)
    if not admitted:
        return None
    body_df: dict[str, int] = {}
    scope_df: dict[str, int] = {}
    for term in unique:
        body_mask, any_mask = known.get(term) or found[term]
        body_count = (body_mask & admitted).bit_count()
        any_count = (any_mask & admitted).bit_count()
        if body_count:
            body_df[term] = body_count
        if any_count:
            scope_df[term] = any_count
    return admitted.bit_count(), body_df, scope_df


def _keep_doc_freqs(
    key: tuple[str, str],
    entry: _DocFreqInputs,
    found: dict[str, tuple[int, int]],
    *,
    fresh: bool,
) -> None:
    """Store `entry`, read at the store's current memory head, or add
    `found` to the entry already kept at that head."""
    with _DOC_FREQS_LOCK:
        current = _DOC_FREQS.get(key)
        if current is not entry:
            # A kept entry was replaced or dropped since this call read
            # it, or another connection kept one at this head first:
            # its masks index its own scan, so `found` is not merged.
            if not fresh or (current is not None and current.head == entry.head):
                return
            _DOC_FREQS[key] = entry
        entry.terms.update(found)
        for term in found:
            entry.terms.move_to_end(term)
        while len(entry.terms) > DOC_FREQ_TERMS:
            entry.terms.popitem(last=False)
        _DOC_FREQS.move_to_end(key)
        while len(_DOC_FREQS) > DOC_FREQ_STORES:
            _DOC_FREQS.popitem(last=False)


def declared_scopes(self: Store, record_ids: Sequence[str]) -> dict[str, list[str]]:
    """``{id: scopes}`` for the memories and episodes asked, as written;
    an id that is neither (a removed memory) is absent. What
    `membership.Membership.homes` reads a global record's named
    projects from."""
    wanted = list(dict.fromkeys(record_ids))
    out: dict[str, list[str]] = {}
    for table in ("memories", "episodes"):
        for start in range(0, len(wanted), _PROVENANCE_BATCH):
            batch = wanted[start : start + _PROVENANCE_BATCH]
            placeholders = ",".join("?" * len(batch))
            for row in self._conn.execute(
                f"SELECT id, scopes_json FROM {table} WHERE id IN ({placeholders})",
                batch,
            ):
                scopes = _load_json(row["scopes_json"], None)
                if isinstance(scopes, list):
                    out[str(row["id"])] = [str(scope) for scope in scopes]
    return out


def pending_conflict_pairs(self: Store) -> list[tuple[str, str, str]]:
    """``(a_id, b_id, pair id)`` for every conflict row whose status is
    ``pending``, whatever the detector: the recall surface's disputed
    mark, one query per brief and per prompt."""
    return [
        (str(row["a_id"]), str(row["b_id"]), str(row["id"]))
        for row in self._conn.execute(
            "SELECT a_id, b_id, id FROM conflicts WHERE status = 'pending' ORDER BY id"
        )
    ]


def contradicted_at(self: Store, memory_ids: Sequence[str]) -> dict[str, datetime]:
    """The newest ``use`` event with outcome ``contradicted`` naming
    each of `memory_ids`, by id; ids never contradicted are absent.
    Reads the ``use`` rows through the log's kind index, every
    session and the whole log, since a contradiction stays open until
    the record is updated or verified, however long ago it was
    recorded. The outcome is filtered in SQL, so the rows of every
    other outcome (nearly all of them) are never parsed here."""
    wanted = set(memory_ids)
    if not wanted:
        return {}
    out: dict[str, datetime] = {}
    for row in self._conn.execute(
        "SELECT ts, payload FROM log WHERE kind = 'use' "
        "AND json_valid(payload) "
        "AND json_extract(payload, '$.outcome') = 'contradicted' ORDER BY seq"
    ):
        try:
            fields = json.loads(row["payload"])
        except ValueError:
            continue
        if not isinstance(fields, dict) or fields.get("outcome") != "contradicted":
            continue
        ids = fields.get("ids") or fields.get("memory_ids")
        if not isinstance(ids, list):
            continue
        try:
            stamp = _dt(str(row["ts"]))
        except ValueError:
            continue
        for memory_id in ids:
            if isinstance(memory_id, str) and memory_id in wanted:
                previous = out.get(memory_id)
                if previous is None or stamp > previous:
                    out[memory_id] = stamp
    return out


# -- the tokenizer the index is spelled by (ENG-100) ----------------------
#
# Each memory row keeps its index text (`body_fts`, `scopes_fts`) as this
# build's tokenizer spells its body and scopes
# (`search_lexicon.fts_index_text`), and the FTS5 table indexes that text, so a
# query matches a memory only while the tokenizer that spells the query is the
# one that spelled the row. The signed log names that tokenizer
# (`search_lexicon.tokenizer_fingerprint`): the `store_created` row from
# `Store.create`, then the `index_rebuilt` row of each re-spelling
# (`stored_fingerprint`); meta keeps a copy. Every open compares it with this
# build's (`reconciles_tokenizer`). The owner's writing open re-spells a store
# whose fingerprint differs (`rebuild_index`), in one write
# transaction, as the owner's open runs a store's schema steps
# (`store_versions.check_versions`); every other open says so once and
# searches the index as it stands, so a memory spelled by the old
# tokenizer may not be found until the owner's next open.

#: meta's key for the tokenizer the stored index text is spelled by.
TOKENIZER_FINGERPRINT_KEY = "tokenizer_fingerprint"

#: Why an index was spelled again, as its `index_rebuilt` row says: an
#: owner's open found it spelled by another tokenizer than this build's,
#: or `bettermemory index rebuild` was run.
REBUILT_FOR_TOKENIZER = "tokenizer"
REBUILT_FOR_REPAIR = "repair"

_P = ParamSpec("_P")

# The stores this process has said it searches under another tokenizer's
# spelling, or failed to re-spell, with the two fingerprints: said once,
# not at every connection a reader or a daemon's worker opens.
_DRIFT_TOLD: set[tuple[str, ...]] = set()
_DRIFT_TOLD_LOCK = threading.Lock()


@_caches.register
def _clear_drift_told() -> None:
    with _DRIFT_TOLD_LOCK:
        _DRIFT_TOLD.clear()


@dataclass(frozen=True)
class IndexRebuild:
    """One re-spelling of a store's index (`rebuild_index`), as its
    `index_rebuilt` row records it: why, the tokenizer fingerprints meta
    named before (None when it named none) and after, how many memory rows
    were read and how many of them changed their index text, the row's
    seq, and how long the write transaction ran, in seconds. The
    fingerprint before is the one the log named (`stored_fingerprint`)."""

    reason: str
    old_fingerprint: str | None
    new_fingerprint: str
    memories: int
    respelled: int
    seq: int
    seconds: float


def stored_fingerprint(conn: sqlite3.Connection) -> str | None:
    """The tokenizer the store's index text is spelled by, as the signed
    log names it last: the newest `index_rebuilt` row's `new_fingerprint`,
    or the `store_created` row's `tokenizer_fingerprint`. Both rows are in
    the MAC'd chain, so an edit of meta, which nothing signs, moves neither
    the fold's comparison nor a re-spelling. meta's copy only for a log
    that names none; None when meta has none either. Two indexed reads
    (`log_by_kind`)."""
    named: tuple[int, str] | None = None
    for kind, key in (
        (INDEX_REBUILT, "new_fingerprint"),
        (STORE_CREATED, TOKENIZER_FINGERPRINT_KEY),
    ):
        row = conn.execute(
            "SELECT seq, payload FROM log WHERE kind = ? ORDER BY seq DESC LIMIT 1",
            (kind,),
        ).fetchone()
        if row is None:
            continue
        try:
            payload = _load_json(row[1], None)
        except ValueError:
            # Not JSON: `log verify` reads the row as tampered; the open
            # goes by the rows that name one.
            payload = None
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, str) and (named is None or int(row[0]) > named[0]):
            named = (int(row[0]), value)
    if named is not None:
        return named[1]
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (TOKENIZER_FINGERPRINT_KEY,)
    ).fetchone()
    return None if row is None else str(row[0])


def tokenizer_drift(conn: sqlite3.Connection) -> tuple[str | None, str] | None:
    """`(stored, live)` when the tokenizer the log names
    (`stored_fingerprint`) is not this build's
    (`search_lexicon.tokenizer_fingerprint`), None when they agree. Two indexed
    reads of the log; the live digest is computed once a process."""
    stored = stored_fingerprint(conn)
    live = tokenizer_fingerprint()
    return None if stored == live else (stored, live)


def cannot_write(store: Store) -> str | None:
    """Why this connection may not re-spell the index, or None when it
    may: it holds the key the log row is signed with, it was not opened
    read-only, and its versions let it write (``PRAGMA query_only`` off,
    `store_versions.check_versions`)."""
    if store._read_only:
        return "it was opened read-only"
    if not store.has_key:
        return "it was opened without the store's key"
    if store.conn.execute("PRAGMA query_only").fetchone()[0]:
        return "this build may only read the store"
    return None


def _first_told(*told: str) -> bool:
    with _DRIFT_TOLD_LOCK:
        if told in _DRIFT_TOLD:
            return False
        _DRIFT_TOLD.add(told)
        return True


#: What a drift warning names instead of the file the open read, while
#: set (`drift_named`).
_DRIFT_SUBJECT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_DRIFT_SUBJECT", default=None
)


@contextlib.contextmanager
def drift_named(subject: str) -> Iterator[None]:
    """While inside, an open that finds its index spelled by another
    tokenizer names `subject` instead of the file it opened, and says
    what holds for it: `backup verify` opens a private copy of a bundle's
    snapshot, whose path means nothing to its reader, and which no open
    of this build re-spells; a store restored from the bundle is
    re-spelled at its own owner's first open. Said once a process per
    subject and pair of fingerprints."""
    token = _DRIFT_SUBJECT.set(subject)
    try:
        yield
    finally:
        _DRIFT_SUBJECT.reset(token)


def _tell_drift(path: Path, stored: str | None, live: str, why: str) -> None:
    subject = _DRIFT_SUBJECT.get()
    if subject is not None:
        if _first_told(subject, stored or "", live):
            log.warning(
                "%s: its search index is spelled by tokenizer %s and this "
                "build's is %s; it is read as it stands, and a store restored "
                "from it is re-spelled at that store's first writing open by "
                "its owner",
                subject,
                (stored or "none")[:16],
                live[:16],
            )
        return
    if not _first_told(str(path), stored or "", live):
        return
    log.warning(
        "store %s: its search index is spelled by tokenizer %s and this "
        "build's is %s; this open does not re-spell it (%s), so a search may "
        "miss memories until the owner's next writing open does (the "
        "daemon's start, any writing command, or `bettermemory index rebuild`)",
        path,
        (stored or "none")[:16],
        live[:16],
        why,
    )


def settle_tokenizer(store: Store, *, owner: bool) -> IndexRebuild | None:
    """Compare the tokenizer `store`'s index is spelled by with this
    build's, and when they differ re-spell it at the owner's writing open
    (`owner`, the open's ``allow_rekey``, on a connection that may write:
    `cannot_write`), or else say so once (`_tell_drift`) and leave it.
    The rebuild never fails the open: whatever it raises (a busy or full
    store, or a row it cannot spell, such as scopes that are not text,
    which only an editor of the file writes), its transaction is rolled
    back (`Store._transaction`), the failure is said once a process,
    naming `bettermemory index rebuild`, and the store opens as it stands.
    Returns the rebuild, or None."""
    drift = tokenizer_drift(store.conn)
    if drift is None:
        return None
    stored, live = drift
    why = cannot_write(store) or (None if owner else "not the owner's writing open")
    if why is not None:
        _tell_drift(store.path, stored, live, why)
        return None
    try:
        return rebuild_index(store, reason=REBUILT_FOR_TOKENIZER)
    except Exception as exc:
        if not _first_told(str(store.path), stored or "", live, "failed"):
            return None
        log.warning(
            "store %s: its search index is spelled by tokenizer %s and this "
            "build's is %s, and re-spelling it failed (%s); a search may miss "
            "memories until a later writing open re-spells it, or "
            "`bettermemory index rebuild` does",
            store.path,
            (stored or "none")[:16],
            live[:16],
            exc,
        )
        return None


def reconciles_tokenizer(open_: Callable[_P, Store]) -> Callable[_P, Store]:
    """`Store.open`'s decorator: once the open has returned a store,
    whichever of its paths it took (a reader's, a reclaim, a rekey), its
    index's tokenizer is settled (`settle_tokenizer`) with the open's own
    ``allow_rekey`` as the owner's word. An exception the settling raises
    closes the store before it propagates."""

    # The owner's word is the caller's ``allow_rekey``, not whether the
    # open may write: an open `store_versions.check_versions` downgrades to a
    # reader sets ``PRAGMA query_only``, as it does whenever it downgrades,
    # and `cannot_write` reads that. A downgrade that left query_only off
    # would let this re-spell a store the open was told not to write.
    @functools.wraps(open_)
    def opened(*args: _P.args, **kwargs: _P.kwargs) -> Store:
        store = open_(*args, **kwargs)
        try:
            settle_tokenizer(store, owner=bool(kwargs.get("allow_rekey")))
        except BaseException:
            store.close()
            raise
        return store

    return opened


def rebuild_index(store: Store, *, reason: str) -> IndexRebuild | None:
    """Re-spell `store`'s index with this build's tokenizer, in one write
    transaction (`Store._transaction`, ``BEGIN IMMEDIATE``: the store's
    write lock, held to the commit): every memory row's `body_fts` and
    `scopes_fts` is set from its body and scopes as
    `search_lexicon.fts_index_text` spells them now (only the rows whose text
    changes are written), the FTS5 table is rebuilt from those columns
    (``'rebuild'``), meta's `tokenizer_fingerprint` names this build's
    tokenizer, and one `index_rebuilt` row records it, so `log verify` and `log
    export` see it. No record changes: the fold applies no `index_rebuilt` row.

    With `reason` `REBUILT_FOR_TOKENIZER`, the tokenizer the log names is
    read again under the lock and a store another opener re-spelled while
    this one waited is left alone (None). A busy, full or failing store raises
    `sqlite3.Error` with nothing written; the caller says what it means."""
    started = time.monotonic()
    with store._transaction() as tx:
        stored = stored_fingerprint(tx)
        live = tokenizer_fingerprint()
        if reason == REBUILT_FOR_TOKENIZER and stored == live:
            return None
        memories = 0
        changed: list[tuple[str, str, int]] = []
        for row in tx.execute(
            "SELECT rowid, body, scopes_json, body_fts, scopes_fts FROM memories"
        ):
            memories += 1
            body_fts = fts_index_text(str(row["body"]))
            scopes_fts = fts_index_text(" ".join(_load_json(row["scopes_json"], [])))
            if body_fts != row["body_fts"] or scopes_fts != row["scopes_fts"]:
                changed.append((body_fts, scopes_fts, int(row["rowid"])))
        tx.executemany(
            "UPDATE memories SET body_fts = ?, scopes_fts = ? WHERE rowid = ?",
            changed,
        )
        tx.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
        tx.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (TOKENIZER_FINGERPRINT_KEY, live),
        )
        appended = store._append(
            tx,
            INDEX_REBUILT,
            {
                "reason": reason,
                "old_fingerprint": stored,
                "new_fingerprint": live,
                "memories": memories,
                "respelled": len(changed),
            },
            session=None,
        )
    rebuilt = IndexRebuild(
        reason=reason,
        old_fingerprint=stored,
        new_fingerprint=live,
        memories=memories,
        respelled=len(changed),
        seq=appended.seq,
        seconds=time.monotonic() - started,
    )
    log.info(
        "store %s: search index re-spelled (%s): tokenizer %s to %s, %d of %d "
        "memories re-spelled in %.3f s",
        store.path,
        reason,
        (stored or "none")[:16],
        live[:16],
        rebuilt.respelled,
        rebuilt.memories,
        rebuilt.seconds,
    )
    return rebuilt


# -- the index's integrity (ENG-100) --------------------------------------
#
# Nothing read a store's pages or compared its FTS5 index with the rows it
# indexes, so a torn page, or an index that lost a memory's entries (a
# write that bypassed the triggers, a damaged shadow page), dropped
# records from search without a word. `doctor` (its `index` check) and
# `backup verify` (on the bundle's snapshot) now run both checks:
# SQLite's ``PRAGMA quick_check``, and FTS5's ``integrity-check`` in the
# form that compares an external-content table's index with its content
# (rank 1; the plain form checks the index's own structure only). That
# command is an INSERT, which every read-only connection refuses
# (``mode=ro``, ``immutable``, ``query_only``: probed), and `doctor` and
# `backup verify` read through one; so it runs on a copy in memory of the
# indexed columns and the index's four shadow tables, made with the
# store's own table definition (its ``sqlite_master`` text, so a store
# whose FTS5 table an older schema defined is checked as it was built),
# and writes nothing beside the store.

#: How many problems `PRAGMA quick_check` is asked to list at most.
QUICK_CHECK_MESSAGES = 20

#: The start of an FTS5 table named ``memories_fts`` as ``sqlite_master``
#: keeps its definition: what the integrity check's copy is made with, and
#: the only statement from the file it runs.
_FTS_DEFINITION = re.compile(
    r"\s*CREATE\s+VIRTUAL\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"[\"`\[]?memories_fts[\"`\]]?\s+USING\s+fts5\s*\(",
    re.I,
)

#: The shadow tables FTS5 keeps an external-content table's index in.
_FTS_SHADOWS = ("data", "idx", "docsize", "config")

#: The repairs the findings name.
REPAIR_PAGES = (
    "a page of the store file is damaged: restore the newest bundle that "
    "`bettermemory backup verify` reads ok with `bettermemory backup restore`, "
    "which keeps the damaged file aside; `bettermemory log verify` reads what "
    "the file still holds"
)
REPAIR_INDEX = (
    "run `bettermemory index rebuild`: it rebuilds the index from the "
    "memories, which a drift leaves as they are"
)
REPAIR_TOKENIZER = (
    "the owner's next writing open re-spells it (the daemon's next start, or "
    "any writing command); `bettermemory index rebuild` does it now"
)


@dataclass(frozen=True)
class IndexReport:
    """What the checks found on one store connection (`index_report`):
    `pages`, `PRAGMA quick_check`'s messages, empty when it reads ok;
    `fts`, what FTS5's integrity-check said, None when the index matches
    the rows; `tokenizer`, `(stored, live)` when meta names another
    tokenizer than this build's (`tokenizer_drift`); the memory rows
    copied; and how long the checks took, in seconds."""

    pages: tuple[str, ...]
    fts: str | None
    tokenizer: tuple[str | None, str] | None
    memories: int
    seconds: float


def quick_check(conn: sqlite3.Connection) -> tuple[str, ...]:
    """``PRAGMA quick_check``'s messages, at most `QUICK_CHECK_MESSAGES`,
    or none when the file reads ok: every page of every table and index
    read, without `integrity_check`'s comparison of each index with its
    table. A file too damaged to answer reads as the error SQLite gave."""
    try:
        rows = conn.execute(f"PRAGMA quick_check({QUICK_CHECK_MESSAGES})").fetchall()
    except sqlite3.DatabaseError as exc:
        return (str(exc),)
    messages = tuple(str(row[0]) for row in rows)
    return () if messages == ("ok",) else messages


def fts_integrity(conn: sqlite3.Connection) -> tuple[str | None, int]:
    """FTS5's integrity-check of `memories_fts` against the rows it
    indexes (``INSERT INTO memories_fts(memories_fts, rank) VALUES
    ('integrity-check', 1)``), run on a copy in memory of the memories'
    `rowid`, `body_fts` and `scopes_fts` and the four shadow tables, so it
    runs on a read-only connection and writes nothing beside the store.
    The copy's FTS5 table is made with the store's own definition, read
    from ``sqlite_master``: the tokenizer arguments it was built with are
    the ones its terms were split by, whatever this build's schema says,
    and `bettermemory index rebuild` keeps them too. A definition that is
    not an FTS5 table named ``memories_fts`` is not run. Returns what the
    check said (None when the index matches the rows, or why it could not
    be checked) and how many memory rows were copied."""
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("memories_fts",),
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        return f"the index could not be read: {exc}", 0
    definition = None if row is None or row[0] is None else str(row[0])
    if definition is None or _FTS_DEFINITION.match(definition) is None:
        return "the store has no FTS5 table memories_fts to check", 0
    scratch = sqlite3.connect(":memory:", isolation_level=None)
    copied = 0
    try:
        scratch.execute(
            "CREATE TABLE memories("
            "rowid INTEGER PRIMARY KEY, body_fts TEXT, scopes_fts TEXT)"
        )
        try:
            scratch.execute(definition)
        except sqlite3.Error as exc:
            return f"the index's definition could not be made again: {exc}", copied
        scratch.execute("BEGIN")
        try:
            for row in conn.execute("SELECT rowid, body_fts, scopes_fts FROM memories"):
                scratch.execute(
                    "INSERT INTO memories(rowid, body_fts, scopes_fts) VALUES (?, ?, ?)",
                    tuple(row),
                )
                copied += 1
            for name in _FTS_SHADOWS:
                table = f"memories_fts_{name}"
                columns = [
                    str(r[1]) for r in scratch.execute(f"PRAGMA table_info({table})")
                ]
                listed = ", ".join(columns)
                scratch.execute(f"DELETE FROM {table}")
                scratch.executemany(
                    f"INSERT INTO {table}({listed}) "
                    f"VALUES ({', '.join('?' * len(columns))})",
                    (tuple(r) for r in conn.execute(f"SELECT {listed} FROM {table}")),
                )
        except sqlite3.DatabaseError as exc:
            return f"the index could not be read: {exc}", copied
        scratch.execute("COMMIT")
        try:
            scratch.execute(
                "INSERT INTO memories_fts(memories_fts, rank) "
                "VALUES ('integrity-check', 1)"
            )
        except sqlite3.DatabaseError as exc:
            return str(exc), copied
        return None, copied
    finally:
        scratch.close()


def index_report(store: Store) -> IndexReport:
    """The file's pages (`quick_check`), the FTS5 index against the rows
    (`fts_integrity`) and the tokenizer the index is spelled by
    (`tokenizer_drift`), on `store`'s connection, which may be read-only."""
    started = time.monotonic()
    conn = store.conn
    pages = quick_check(conn)
    fts, memories = fts_integrity(conn)
    try:
        drift = tokenizer_drift(conn)
    except sqlite3.DatabaseError:
        drift = None
    return IndexReport(pages, fts, drift, memories, time.monotonic() - started)


def _more(items: Sequence[str]) -> str:
    return f" (and {len(items) - 1} more)" if len(items) > 1 else ""


def doctor_check(directory: Path | None, report: IndexReport | None = None) -> Check:
    """`doctor`'s `index` check: `report`, the `index_report` the store
    check ran on its own read-only open (`doctor.check_store`, so a closed
    store is copied once, not twice), or else one run on a read-only open
    of the store at `directory` made here, and each problem a finding
    naming its repair. A damaged page
    fails (records are at stake); an index that does not match its rows
    and an index spelled by another tokenizer warn (the records are whole,
    and one command or the next writing open repairs it). A store that
    does not open, or that sits in a backup bundle, is the `store`
    check's failure, and this one reads "not checked"."""
    from .backup import bundle_manifest_beside
    from .doctor import FAIL, OK, WARN, Check, Finding, _from_findings, _not_checked
    from .store import STORE_FILENAME, Store

    if directory is None:
        return _not_checked(
            "index", "the store's directory is not known", "fix the config first"
        )
    path = directory / STORE_FILENAME
    if not path.is_file():
        if path.exists():
            return _not_checked(
                "index", "the store does not open", "fix the store first"
            )
        return Check("index", OK, "no store yet; its index is written with it")
    if bundle_manifest_beside(path) is not None:
        return _not_checked(
            "index", "the store is a backup bundle's file", "copy it out first"
        )
    if report is None:
        try:
            store = Store.open(path, read_only=True)
        except (ValueError, sqlite3.Error, OSError):
            return _not_checked(
                "index", "the store does not open", "fix the store first"
            )
        with store:
            report = index_report(store)
    live = tokenizer_fingerprint()
    details: dict[str, Any] = {
        "path": str(path),
        "quick_check": list(report.pages) or "ok",
        "fts_integrity": report.fts or "ok",
        "tokenizer_fingerprint": live,
        "stored_tokenizer_fingerprint": (
            live if report.tokenizer is None else report.tokenizer[0]
        ),
        "memories": report.memories,
        "seconds": round(report.seconds, 6),
    }
    findings: list[Finding] = []
    if report.pages:
        findings.append(
            Finding(
                FAIL,
                f"{path} has a damaged page (SQLite's quick_check: "
                f"{report.pages[0]}{_more(report.pages)})",
                REPAIR_PAGES,
            )
        )
    if report.fts is not None:
        findings.append(
            Finding(
                WARN,
                f"{path}'s search index does not match its memories (FTS5 "
                f"integrity-check: {report.fts}): a search can miss a memory, or "
                "offer one for words it does not hold",
                REPAIR_INDEX,
            )
        )
    if report.tokenizer is not None:
        stored, _ = report.tokenizer
        findings.append(
            Finding(
                WARN,
                f"{path}'s search index is spelled by tokenizer "
                f"{(stored or 'none')[:16]} and this build's is {live[:16]}: a "
                "search misses the memories the old one spelled",
                REPAIR_TOKENIZER,
            )
        )
    return _from_findings(
        "index",
        findings,
        ok_summary=(
            f"{path}: every page reads (quick_check) and the search index matches "
            f"its {report.memories} memories (FTS5 integrity-check)"
        ),
        details=details,
    )


def index_problems(store: Store) -> list[dict[str, Any]]:
    """`backup verify`'s problems from `index_report` on the bundle's
    snapshot. A damaged page is ``snapshot_unreadable``, the problem the
    verification gives when its own reads meet one (verdict `mismatched`,
    so `backup restore` refuses the bundle), with ``check`` naming
    quick_check. An index that does not match its rows is
    ``index_drift``, which leaves the verdict as it is: the records are
    whole, and `bettermemory index rebuild` repairs the restored store.
    A snapshot spelled by an older tokenizer is no problem: the restored
    store's owner's open re-spells it."""
    report = index_report(store)
    problems: list[dict[str, Any]] = []
    if report.pages:
        problems.append(
            {
                "problem": "snapshot_unreadable",
                "check": "quick_check",
                "detail": "; ".join(report.pages[:3]) + _more(report.pages[2:]),
            }
        )
    if report.fts is not None:
        problems.append(
            {
                "problem": "index_drift",
                "detail": report.fts,
                "repair": "restore it, then run `bettermemory index rebuild`",
            }
        )
    return problems


# -- the query planner's statistics (ENG-100) -----------------------------

#: The bits ``PRAGMA optimize`` runs with: 0x02, analyse the tables that
#: may benefit, and 0x10000, look at every table, not only those this
#: connection queried. The daemon's own connection queries little (its
#: workers' connections do the reading), so without 0x10000 it would
#: analyse nothing. SQLite added the bit in 3.46.0; an older one ignores it.
OPTIMIZE_MASK = 0x10002


def optimize(store: Store, *, why: str) -> bool:
    """``PRAGMA optimize`` on `store`'s connection, for the daemon (`why`:
    ``schedule`` or ``shutdown``, for the log). It analyses only a table
    whose statistics are missing or behind its size, under the bounded
    `analysis_limit` SQLite sets for it, so it is cheap: 2.4 ms the first
    time on 5,000 memories, 5.5 us when nothing needs it (ENG-100). An
    analysis writes ``sqlite_stat1``, which needs the write lock: the
    connection's busy timeout is 0 for the call, so a store another
    connection is writing is skipped rather than waited on, and the next
    run tries again. Returns whether it ran; a store busy, read-only by
    its versions or closed is logged and skipped."""
    started = time.monotonic()
    try:
        wait = store.busy_timeout
        store.set_busy_timeout(0)
        try:
            store.conn.execute(f"PRAGMA optimize({OPTIMIZE_MASK})")
        finally:
            store.set_busy_timeout(wait)
    except (sqlite3.Error, ValueError) as exc:
        log.info("store %s: PRAGMA optimize (%s) skipped: %s", store.path, why, exc)
        return False
    log.debug(
        "store %s: PRAGMA optimize (%s) in %.6f s",
        store.path,
        why,
        time.monotonic() - started,
    )
    return True


def optimize_apart(store: Store, why: str) -> bool:
    """`optimize` on a connection of its own (`Store.reopen`, busy timeout
    0), opened, run and closed on the calling thread: for a caller that
    must not block the thread `store`'s connection serves. The daemon
    runs it in its event loop's executor, since the first run on a store
    with no statistics counts every table (measured on synthetic stores:
    1.8 ms at 5,000 memories, 399 ms at 50,000, 1.8 s at 250,000).
    `PRAGMA optimize` looks at every table on any connection (0x10000),
    so a fresh one analyses what the daemon's would. A store that does
    not open again is logged and skipped. Returns whether it ran."""
    try:
        other = store.reopen(busy_timeout=0)
    except (sqlite3.Error, ValueError, OSError) as exc:
        log.info("store %s: PRAGMA optimize (%s) skipped: %s", store.path, why, exc)
        return False
    try:
        return optimize(other, why=why)
    finally:
        other.close()
