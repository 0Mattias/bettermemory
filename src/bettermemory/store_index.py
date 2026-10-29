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
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from . import _caches
from .identity import Actor
from .search import fts_match_query
from .store_schema import _PROVENANCE_BATCH
from .store_rows import _dt, _load_json

if TYPE_CHECKING:
    from .store import Store


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
    """The log's last memory or home row, `(seq, mac)`: the key of the
    document frequency memo. `(0, "")` on a log with none. Each MAX is
    one probe of `log_by_kind`."""
    row = self._conn.execute(
        "SELECT seq, mac FROM log WHERE seq = (SELECT MAX(s) FROM ("
        "SELECT MAX(seq) AS s FROM log WHERE kind = 'memory_put' "
        "UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'memory_delete' "
        "UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'home_put' "
        "UNION ALL SELECT MAX(seq) AS s FROM log WHERE kind = 'home_delete'))"
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
