"""The `Store` methods of the membership tables (design 8.5).

The rows the membership model (`membership.py` and its family) reads and
writes: a put and a delete per table (`put_membership`,
`delete_membership`), each a mutation row of the table's own kind, the
control rows of design 8.10 (`record_membership`, `membership_log`), the
batched reads (`membership_rows`), the id counters, `wrote_record` and the
store id reissue of design 8.7. Nothing here decides membership. Each is
a function of `self: Store` that `Store` binds by name;
`membership_store_id` stays a property in `store.py`. It lives apart from
`store.py` to keep each module under the package's 2,000-line bound
(ENG-96); it imports `store_schema` and `store_rows`, and `store` only
for type checking.
"""

from __future__ import annotations

import secrets
from collections.abc import Collection, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .log import LogRow, canonical_payload
from .store_schema import (
    MEMBERSHIP_EVENT_KINDS,
    MEMBERSHIP_MUTATION_KINDS,
    MEMBERSHIP_STORE_ID,
    _MEMBERSHIP_BY_TABLE,
    _PROVENANCE_BATCH,
)
from .store_rows import (
    _load_json,
    _membership_column,
    _membership_key,
    _membership_spec,
    _membership_values,
    _sort_rows,
)

if TYPE_CHECKING:
    from .store import Store


def put_membership(
    self: Store, kind: str, row: Mapping[str, Any], *, session: str | None = None
) -> LogRow:
    """Insert or replace one row of the membership table whose mutation
    kind prefix is `kind` (``home``, ``checkout``, ...): every column
    given, a ``_json`` column as its JSON value. The row is checked
    before it is logged, so a malformed one raises and writes
    nothing."""
    spec = _membership_spec(kind)
    payload = {spec.kind: dict(row)}
    _membership_values(spec, payload[spec.kind])
    with self._transaction() as tx:
        return self._mutate(tx, f"{spec.kind}_put", payload, session=session)


def delete_membership(
    self: Store, kind: str, key: Mapping[str, Any], *, session: str | None = None
) -> LogRow:
    """Delete the membership row `key` names (its key columns)."""
    spec = _membership_spec(kind)
    payload = {column: key[column] for column in spec.key}
    _membership_key(spec, payload)
    with self._transaction() as tx:
        return self._mutate(tx, f"{spec.kind}_delete", payload, session=session)


def record_membership(
    self: Store, kind: str, payload: Mapping[str, Any], *, session: str | None = None
) -> LogRow:
    """One membership row of design 8.10 (`MEMBERSHIP_EVENT_KINDS`),
    written whether or not telemetry is on. Inside a `batch` it commits
    with the batch."""
    if kind not in MEMBERSHIP_EVENT_KINDS:
        raise ValueError(f"{kind!r} is not a membership row kind")
    with self._transaction() as tx:
        return self._append(tx, kind, dict(payload), session=session)


def membership_rows(
    self: Store,
    table: str,
    *,
    where: Mapping[str, Any] | None = None,
    column_in: tuple[str, Collection[Any]] | None = None,
    order_by: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Rows of a membership table as dicts, a ``_json`` column parsed
    back to its value: those whose columns equal `where` (None matches
    null) and whose `column_in` column holds one of its values, ordered
    by `order_by` (a leading ``-`` sorts that column descending), else
    by the key. Column names are checked against the table."""
    spec = _MEMBERSHIP_BY_TABLE.get(table)
    if spec is None:
        raise ValueError(f"{table!r} is not a membership table")
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (where or {}).items():
        _membership_column(spec, column)
        if value is None:
            clauses.append(f"{column} IS NULL")
        else:
            clauses.append(f"{column} = ?")
            params.append(value)
    order: list[str] = []
    for name in order_by or spec.key:
        column = name.lstrip("-")
        _membership_column(spec, column)
        order.append(f"{column} DESC" if name.startswith("-") else column)
    select = f"SELECT {', '.join(spec.columns)} FROM {table}"
    batches: list[list[Any]] = [[]]
    if column_in is not None:
        column, values = column_in
        _membership_column(spec, column)
        wanted = list(dict.fromkeys(values))
        if not wanted:
            return []
        batches = [
            wanted[start : start + _PROVENANCE_BATCH]
            for start in range(0, len(wanted), _PROVENANCE_BATCH)
        ]
    out: list[dict[str, Any]] = []
    for batch in batches:
        batch_clauses = list(clauses)
        if column_in is not None:
            batch_clauses.append(f"{column_in[0]} IN ({','.join('?' * len(batch))})")
        sql = select
        if batch_clauses:
            sql += " WHERE " + " AND ".join(batch_clauses)
        sql += " ORDER BY " + ", ".join(order)
        for raw in self._conn.execute(sql, (*params, *batch)):
            out.append(
                {
                    column: (
                        _load_json(raw[column], None)
                        if column.endswith("_json")
                        else raw[column]
                    )
                    for column in spec.columns
                }
            )
    if len(batches) > 1:
        _sort_rows(out, list(order_by or spec.key))
    return out


def next_membership_id(self: Store, table: str, prefix: str) -> str:
    """The next local id of a membership table: `prefix` and one more
    than the highest number its ids carry, at least three digits
    (``P001``). Read inside the caller's transaction, so two writers
    never mint one id."""
    spec = _MEMBERSHIP_BY_TABLE.get(table)
    if spec is None or spec.key != ("id",):
        raise ValueError(f"{table!r} has no minted id")
    row = self._conn.execute(
        f"SELECT MAX(CAST(SUBSTR(id, ?) AS INTEGER)) FROM {table} WHERE id LIKE ?",
        (len(prefix) + 1, f"{prefix}%"),
    ).fetchone()
    number = int(row[0] or 0) + 1
    return f"{prefix}{number:03d}"


def next_membership_number(self: Store, table: str, column: str) -> int:
    """One more than the highest value of an integer column of a
    membership table (a witness's arrival, a view's sighting order)."""
    spec = _MEMBERSHIP_BY_TABLE.get(table)
    if spec is None:
        raise ValueError(f"{table!r} is not a membership table")
    _membership_column(spec, column)
    row = self._conn.execute(f"SELECT MAX({column}) FROM {table}").fetchone()
    return int(row[0] or 0) + 1


def membership_log(
    self: Store, kinds: Collection[str] | None = None, *, since_seq: int = 0
) -> Iterator[LogRow]:
    """The membership rows after `since_seq`, in log order: the
    mutation rows of the membership tables and the rows of design
    8.10, or only those of `kinds`, read through the log's kind
    index."""
    allowed = MEMBERSHIP_MUTATION_KINDS | MEMBERSHIP_EVENT_KINDS
    names = sorted(allowed if kinds is None else set(kinds) & allowed)
    if not names:
        return
    placeholders = ",".join("?" * len(names))
    for raw in self._conn.execute(
        "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log "
        f"WHERE kind IN ({placeholders}) AND seq > ? ORDER BY seq",
        (*names, int(since_seq)),
    ):
        yield LogRow(
            int(raw[0]),
            str(raw[1]),
            None if raw[2] is None else str(raw[2]),
            str(raw[3]),
            str(raw[4]),
            str(raw[5]),
            str(raw[6]),
        )


def wrote_record(self: Store, record_id: str) -> bool:
    """Whether this store's own log ever put the record `record_id`: a
    memory, tombstone or episode row naming it, or its home."""
    if (
        self._conn.execute(
            "SELECT 1 FROM homes WHERE record_id = ?", (record_id,)
        ).fetchone()
        is not None
    ):
        return True
    needle = canonical_payload({"id": record_id})[1:-1]
    return (
        self._conn.execute(
            "SELECT 1 FROM log WHERE kind IN "
            "('memory_put', 'tombstone_put', 'episode_put') "
            "AND instr(payload, ?) > 0 LIMIT 1",
            (needle,),
        ).fetchone()
        is not None
    )


def reissue_membership_store_id(
    self: Store, *, record_id: str, source: str, session: str | None = None
) -> tuple[str, str]:
    """A new store id after a fork (design 8.7): the meta value
    replaced and a ``store_forked`` row naming the old and new ids,
    the record that showed the fork and where it came from, in one
    transaction, the old id read under its write lock. Returns
    ``(old, new)``."""
    new = secrets.token_hex(16)
    with self._transaction() as tx:
        old = self.membership_store_id
        tx.execute(
            "UPDATE meta SET value = ? WHERE key = ?", (new, MEMBERSHIP_STORE_ID)
        )
        self._append(
            tx,
            "store_forked",
            {
                "old_store": old,
                "new_store": new,
                "record": record_id,
                "source": source,
            },
            session=session,
        )
    return old, new
