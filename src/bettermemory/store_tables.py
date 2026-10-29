"""The `Store` methods for verifications, conflicts, imports and quarantine.

This host's verification stamps (`put_verification`, `verifications_for`,
`delete_verification`), the conflicts queue, the import ledger and the
quarantined files: a put, a list and a delete each, every change a
mutation row through the store's transaction seam (`_transaction`,
`_mutate`). Each is a function of `self: Store` that `Store` binds by name.
It lives apart from `store.py` to keep each module under the package's
2,000-line bound (ENG-96); it imports `store_schema` and `store_rows`, and
`store` only for type checking.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .store_schema import (
    _CONFLICT_COLUMNS,
    _IMPORT_COLUMNS,
    _PROVENANCE_BATCH,
    _QUARANTINE_COLUMNS,
    NotFoundError,
)
from .store_rows import _iso, _verification_row

if TYPE_CHECKING:
    from .store import Store


# -- verifications ------------------------------------------------------


def put_verification(
    self: Store,
    memory_id: str,
    *,
    verified_at: datetime,
    machine_id: str | None = None,
    verified_head: str | None = None,
    verified_paths: Sequence[str] = (),
    verified_commits: Sequence[str] = (),
    verified_versions: Sequence[str] = (),
    verified_absent_paths: Sequence[str] = (),
    session: str | None = None,
) -> None:
    """This host's stamp on a record (``machine_id`` None until the
    per-machine identity of phase 3 fills it). One row per memory and
    machine; a later stamp replaces the earlier."""
    payload = {
        "memory_id": memory_id,
        "machine_id": machine_id,
        "verified_at": _iso(verified_at),
        "verified_head": verified_head,
        "verified_paths": list(verified_paths),
        "verified_commits": list(verified_commits),
        "verified_versions": list(verified_versions),
        "verified_absent_paths": list(verified_absent_paths),
    }
    with self._transaction() as tx:
        self._mutate(tx, "verification_put", payload, session=session)


def verifications_for(
    self: Store, memory_ids: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    ids = list(dict.fromkeys(memory_ids))
    for start in range(0, len(ids), _PROVENANCE_BATCH):
        batch = ids[start : start + _PROVENANCE_BATCH]
        placeholders = ",".join("?" * len(batch))
        for row in self._conn.execute(
            "SELECT * FROM verifications "
            f"WHERE memory_id IN ({placeholders}) ORDER BY memory_id, machine_id",
            batch,
        ):
            out.setdefault(str(row["memory_id"]), []).append(_verification_row(row))
    return out


def delete_verification(
    self: Store,
    memory_id: str,
    *,
    machine_id: str | None = None,
    session: str | None = None,
) -> None:
    with self._transaction() as tx:
        found = tx.execute(
            "SELECT 1 FROM verifications WHERE memory_id = ? "
            "AND COALESCE(machine_id, '') = COALESCE(?, '')",
            (memory_id, machine_id),
        ).fetchone()
        if found is None:
            raise NotFoundError((memory_id, machine_id))
        self._mutate(
            tx,
            "verification_delete",
            {"memory_id": memory_id, "machine_id": machine_id},
            session=session,
        )


# -- conflicts, imports, quarantine ------------------------------------


def put_conflict(
    self: Store, record: Mapping[str, Any], *, session: str | None = None
) -> None:
    conflict = {c: record.get(c) for c in _CONFLICT_COLUMNS}
    for required in ("id", "a_id", "b_id"):
        if not conflict[required]:
            raise ValueError(f"a conflict needs {required}")
    with self._transaction() as tx:
        self._mutate(tx, "conflict_put", {"conflict": conflict}, session=session)


def list_conflicts(self: Store) -> list[dict[str, Any]]:
    return [
        {c: row[c] for c in _CONFLICT_COLUMNS}
        for row in self._conn.execute(
            f"SELECT {', '.join(_CONFLICT_COLUMNS)} FROM conflicts ORDER BY created, id"
        )
    ]


def delete_conflict(
    self: Store, conflict_id: str, *, session: str | None = None
) -> None:
    with self._transaction() as tx:
        self._require_row(tx, "conflicts", "id", conflict_id)
        self._mutate(tx, "conflict_delete", {"id": conflict_id}, session=session)


def put_import(
    self: Store,
    source: str,
    *,
    content_hash: str,
    imported_at: str,
    memory_id: str | None = None,
    session: str | None = None,
) -> None:
    payload = {
        "source": source,
        "content_hash": content_hash,
        "imported_at": imported_at,
        "memory_id": memory_id,
    }
    with self._transaction() as tx:
        self._mutate(tx, "import_put", payload, session=session)


def list_imports(self: Store) -> list[dict[str, Any]]:
    return [
        {c: row[c] for c in _IMPORT_COLUMNS}
        for row in self._conn.execute(
            f"SELECT {', '.join(_IMPORT_COLUMNS)} FROM imports ORDER BY source"
        )
    ]


def delete_import(self: Store, source: str, *, session: str | None = None) -> None:
    with self._transaction() as tx:
        self._require_row(tx, "imports", "source", source)
        self._mutate(tx, "import_delete", {"source": source}, session=session)


def put_quarantine(
    self: Store,
    name: str,
    *,
    reason: str,
    at: str,
    detail: str | None = None,
    remote: str | None = None,
    size: int | None = None,
    sha256: str | None = None,
    session: str | None = None,
) -> None:
    payload = {
        "name": name,
        "reason": reason,
        "detail": detail,
        "remote": remote,
        "at": at,
        "size": size,
        "sha256": sha256,
    }
    with self._transaction() as tx:
        self._mutate(tx, "quarantine_put", payload, session=session)


def list_quarantine(self: Store) -> list[dict[str, Any]]:
    return [
        {c: row[c] for c in _QUARANTINE_COLUMNS}
        for row in self._conn.execute(
            f"SELECT {', '.join(_QUARANTINE_COLUMNS)} FROM quarantine ORDER BY name"
        )
    ]


def delete_quarantine(self: Store, name: str, *, session: str | None = None) -> None:
    with self._transaction() as tx:
        self._require_row(tx, "quarantine", "name", name)
        self._mutate(tx, "quarantine_delete", {"name": name}, session=session)
