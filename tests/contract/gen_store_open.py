"""Fixture: what the owner's writing open of an existing store leaves, or
how it refuses (``store_open.jsonl``).

The native build opens stores for writing too (``native/``,
`store::open`) and is held to this file. Each case is a starting store
(a base of ``store_bases.json``, rebuilt from its SQL, or a store
`Store.create` makes under ``store_create.json``'s fixed values) and a
recipe of edits, then ``Store.open(..., allow_rekey=True)`` and ``close``
as the hooks, the daemon and the CLI open (`open_or_create`). Recorded
for each: the state before and after (every table's rows, digest tables
included, as a count and a sha256 of a canonical text, meta and
``digest_state`` row by row, ``sqlite_master``, the pragmas a file
keeps, the store directory's and keys directory's names, the key ring's
texts with the scratch directory as ``<scratch>``, and modes), or the
refusal (class and text). Where the native unit refuses a state it does
not port (``native`` names it), the reference's after-state is recorded
only as which parts it changed: those writes carry fresh keys, clocks and
random draws.

The tables are compared as one sha256 over each table's name, row count
and sha256 of its canonical text (``tables``), with ``log`` and the
digest's two record tables also on their own, and ``meta`` and
``digest_state`` row by row. The canonical text of a table: its rows (with ``rowid`` first where the
table has one) ordered by every column; each cell ``N`` (NULL), ``i``
and the decimal, ``r`` and the float's repr, ``t``, the UTF-8 length, a
colon and the text, ``b`` and the hex; cells joined by U+001F, rows by
U+001E. FTS5's own tables (the virtual table and its shadows) are left
out: no writing open writes them, and their bytes are SQLite's
(``store_create`` holds them).

Gates. Unicode: the bytes depend on the tokenizer's fingerprint (a store
whose index another tokenizer spelled is re-spelled at this open) and the
digest's (its rows are the tokenizer's tokens and claim sizes). POSIX
only: the binding's text is a resolved path, the modes are read, and
the key ring's binding is written under a POSIX ``flock``; on Windows
the reference's paths and locks differ. Not on the SQLite version: no
FTS5 table is compared, the schema texts are the reference's own
strings, and ``schema_version`` counts the open's DDL, which both builds
issue alike. Not on the Python version: every refusal text compared is
the product's own wording (`repr` of ASCII text only), and no case
reaches a standard-library message.
"""

from __future__ import annotations

import gc
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import bettermemory
from bettermemory.store import Store

from . import gen_store_create
from .gen_log import STORE_ID
from .gen_store_files import apply as apply_base_ops
from .gen_store_files import load_bases, rebuild

#: The engine version the open runs as: newer than the bases' 9.0.0rc19,
#: so `_record_engine` names it in meta.
ENGINE = "9.0.0"

PRAGMAS = (
    "journal_mode",
    "page_size",
    "auto_vacuum",
    "encoding",
    "user_version",
    "application_id",
    "schema_version",
)

_HEAD_TEXT = '{{"mac": "{mac}", "seq": {seq}, "ts": "2001-02-03T04:05:06Z"}}'


#: A store id that is not the fixture's (`gen_log.STORE_ID`).
OTHER_ID = "fedcba9876543210fedcba9876543210"


_SPELLINGS = {
    "trailing": "other/memory.sqlite/",
    "doubled": "other//memory.sqlite",
    "dot": "other/./memory.sqlite",
    "dotdot": "other/../other/memory.sqlite",
}


def _spelled_cases(case: Any) -> list[dict[str, Any]]:
    """A binding to the other file spelled as pathlib reads it, not as
    resolved: onto a copy of this store, a live copy, and another store."""
    copy = {"copy_store": "other/memory.sqlite"}
    live = {
        "live_wal": "INSERT INTO meta(key, value) VALUES ('fixture_wal', 'w');",
        "at": "other/memory.sqlite",
    }
    other = {
        "sql_at": "other/memory.sqlite",
        "sql": f"UPDATE meta SET value = '{OTHER_ID}' WHERE key = 'store_id';",
    }
    out = []
    for name, spelled in _SPELLINGS.items():
        bind = {"bind_raw": spelled}
        out += [
            case(
                f"spelled_{name}_copy",
                "intact",
                [copy, bind],
                "not_ported:bound_elsewhere",
            ),
            case(
                f"spelled_{name}_live",
                "intact",
                [copy, live, bind],
                "not_ported:bound_elsewhere",
            ),
            case(f"spelled_{name}_other", "intact", [copy, other, bind]),
        ]
    out.append(
        case("spelled_self_trailing", "intact", [{"bind_raw": "memory.sqlite//"}])
    )
    out.append(case("spelled_self_dot", "intact", [{"bind_raw": "./memory.sqlite/."}]))
    return out


def _digits_to(zero: str) -> str:
    """An UPDATE of ``digest_state`` that spells each ASCII digit of its
    value in the decimal digits starting at ``zero`` (no WHERE)."""
    value = "value"
    for d in range(10):
        value = f"replace({value}, '{d}', '{chr(ord(zero) + d)}')"
    return f"UPDATE digest_state SET value = {value}"


def _hex(text: str) -> str:
    return text.encode("utf-8").hex()


def _row_mac(base: dict[str, Any], seq: int) -> str:
    for statement in base["statements"]:
        prefix = 'INSERT INTO "log"("seq", '
        if statement.startswith(prefix) and f"VALUES ({seq}, " in statement:
            return statement.rsplit("'", 2)[-2]
    raise AssertionError(f"no log row {seq}")


def cases(bases: dict[str, Any]) -> list[dict[str, Any]]:
    """Each case: its name, base, recipe, how many opens, and what the
    native unit does (``same``: opens and leaves what the reference
    leaves; ``refused``: refuses as the reference refuses;
    ``not_ported:<state>``: refuses with nothing written, or, beside a
    hot journal, with only its rollback (``rolled_back``); ``raises``:
    raises as the reference raises, after the same writes)."""
    intact = bases["intact"]
    mac2 = _row_mac(intact, 2)
    mac8 = _row_mac(intact, 8)
    bind = {"bind": "memory.sqlite"}

    def case(
        name: str,
        base: str,
        recipe: list[dict[str, Any]],
        native: str = "same",
        **kw: Any,
    ) -> dict[str, Any]:
        return {"case": name, "base": base, "recipe": recipe, "native": native, **kw}

    sql = lambda text: {"sql": text}  # noqa: E731
    bind_sql = " WHERE key = 'seq';"
    return [
        case("created", "created", []),
        case("intact", "intact", []),
        case("intact_bound", "intact", [bind]),
        case("intact_twice", "intact", [], opens=2),
        case("fts", "fts", [bind]),
        case("rekeyed", "rekeyed", [bind]),
        case("restored", "restored", [bind]),
        case("accepted", "accepted", [bind]),
        case(
            "rollback_journal", "intact", [sql("PRAGMA journal_mode = DELETE;"), bind]
        ),
        case(
            "live_wal",
            "intact",
            [
                bind,
                {
                    "live_wal": "INSERT INTO meta(key, value) "
                    "VALUES ('fixture_wal', 'only in the wal');"
                },
            ],
        ),
        case("head_missing", "intact", [{"unlink": ".head"}, bind]),
        case(
            "head_stale",
            "intact",
            [{"write": ".head", "hex": _hex(_HEAD_TEXT.format(mac=mac2, seq=2))}, bind],
        ),
        case(
            "head_ahead",
            "intact",
            [
                {"write": ".head", "hex": _hex(_HEAD_TEXT.format(mac=mac8, seq=99))},
                bind,
            ],
        ),
        case(
            "head_ahead_unbound",
            "intact",
            [{"write": ".head", "hex": _hex(_HEAD_TEXT.format(mac=mac8, seq=99))}],
            "not_ported:unclaimed",
        ),
        case(
            "head_junk", "intact", [{"write": ".head", "hex": _hex("not json")}, bind]
        ),
        case("head_junk_unbound", "intact", [{"write": ".head", "hex": _hex("{")}]),
        case(
            "origin_missing",
            "intact",
            [{"unlink": ".origin"}, bind],
        ),
        case(
            "origin_unnamed",
            "intact",
            [
                {
                    "write": ".origin",
                    "hex": _hex(json.dumps({"mac": _row_mac(intact, 1), "seq": 1})),
                },
                bind,
            ],
        ),
        case("binding_junk", "intact", [{"write": ".path", "hex": _hex("junk")}]),
        case(
            "binding_relative",
            "intact",
            [{"write": ".path", "hex": _hex('{"path": "x"}')}],
        ),
        case("binding_gone", "intact", [{"bind": "gone/memory.sqlite"}]),
        case(
            "binding_elsewhere",
            "intact",
            [{"copy_store": "other/memory.sqlite"}, {"bind": "other/memory.sqlite"}],
            "not_ported:bound_elsewhere",
        ),
        # `_store_id_at` on the bound file: another store, a live copy, a
        # file that is no store, an empty file, a directory, a store that
        # cannot be read, and this file under other spellings.
        case(
            "bound_other_store",
            "intact",
            [
                {"copy_store": "other/memory.sqlite"},
                {
                    "sql_at": "other/memory.sqlite",
                    "sql": f"UPDATE meta SET value = '{OTHER_ID}' "
                    "WHERE key = 'store_id';",
                },
                {"bind": "other/memory.sqlite"},
            ],
        ),
        case(
            "bound_copy_live",
            "intact",
            [
                {"copy_store": "other/memory.sqlite"},
                {
                    "live_wal": "INSERT INTO meta(key, value) "
                    "VALUES ('fixture_wal', 'only in the wal');",
                    "at": "other/memory.sqlite",
                },
                {"bind": "other/memory.sqlite"},
            ],
            "not_ported:bound_elsewhere",
        ),
        case(
            "bound_not_a_store",
            "intact",
            [
                {"file": "other/memory.sqlite", "hex": _hex("not a store " * 400)},
                {"bind": "other/memory.sqlite"},
            ],
        ),
        case(
            "bound_empty_file",
            "intact",
            [
                {"file": "other/memory.sqlite", "hex": ""},
                {"bind": "other/memory.sqlite"},
            ],
        ),
        case(
            "bound_directory",
            "intact",
            [{"mkdir": "other/memory.sqlite"}, {"bind": "other/memory.sqlite"}],
        ),
        case(
            "bound_unreadable",
            "intact",
            [
                {"copy_store": "other/memory.sqlite"},
                {"chmod": "other/memory.sqlite", "mode": "0000"},
                {"bind": "other/memory.sqlite"},
            ],
            "not_ported:bound_elsewhere",
        ),
        case(
            "bound_symlink",
            "intact",
            [
                {"symlink": "link.sqlite", "to": "memory.sqlite"},
                {"bind_raw": "link.sqlite"},
            ],
        ),
        *_spelled_cases(case),
        case("bound_dotdot", "intact", [{"bind_raw": "keys/../memory.sqlite"}]),
        case("bound_trailing_separator", "intact", [{"bind_raw": "memory.sqlite/"}]),
        case(
            "key_missing",
            "intact",
            [{"unlink": ".key"}, bind],
            "not_ported:key_missing",
        ),
        case(
            "key_foreign",
            "intact",
            [{"write": ".key", "hex": "ee" * 32}, bind],
            "not_ported:key_foreign",
        ),
        case(
            "rotation_uncommitted",
            "intact",
            [{"write": ".key.pending", "hex": "dd" * 32}, bind],
        ),
        case(
            "rotation_pending",
            "intact",
            [{"write": ".key.pending", "hex": intact["keys"][".key"]}, bind],
            "not_ported:rotation_pending",
        ),
        case(
            "restore_record_settled",
            "intact",
            [{"write": ".backup_restore.json", "hex": _hex('{"restores": []}')}, bind],
        ),
        case(
            "restore_record_pending",
            "intact",
            [
                {
                    "write": ".backup_restore.json",
                    "hex": _hex(
                        json.dumps(
                            {
                                "restores": [
                                    {
                                        "kind": "store_restored",
                                        "row": None,
                                        "store_id": STORE_ID,
                                        "restored_at": "2001-02-03T05:06:07.000000Z",
                                        "bundle": "/fixture/bundles/20010203T040506Z",
                                    }
                                ]
                            }
                        )
                    ),
                },
                bind,
            ],
            "not_ported:restore_record",
        ),
        case(
            "tokenizer_drift",
            "intact",
            [
                sql(
                    "UPDATE log SET payload = json_set(payload, "
                    "'$.tokenizer_fingerprint', '" + "0" * 64 + "') WHERE seq = 1;"
                ),
                bind,
            ],
            "not_ported:tokenizer_drift",
        ),
        case(
            "foreign_tail",
            "intact",
            [
                sql(
                    "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) "
                    "VALUES (9, '2001-02-03T04:05:06.000000Z', NULL, 'rekey', "
                    '\'{"new_fingerprint":"'
                    + "f" * 64
                    + '","old_fingerprint":"'
                    + "e" * 64
                    + "\"}', '"
                    + mac8
                    + "', '"
                    + "c" * 64
                    + "');"
                ),
                bind,
            ],
            "not_ported:foreign_tail",
        ),
        case(
            "older_revision",
            "intact",
            [sql("UPDATE meta SET value = '5' WHERE key = 'schema_revision';"), bind],
            "not_ported:schema_steps",
        ),
        case(
            "later_writer",
            "intact",
            [
                sql("UPDATE meta SET value = '7' WHERE key = 'min_writer_revision';"),
                bind,
            ],
            "not_ported:later_writer",
        ),
        case(
            "membership_index_missing",
            "intact",
            [sql("DROP INDEX log_corroborations;"), bind],
            "not_ported:membership_step",
        ),
        case(
            "membership_id_missing",
            "intact",
            [sql("DELETE FROM meta WHERE key = 'membership_store_id';"), bind],
            "not_ported:membership_step",
        ),
        case(
            "later_reader",
            "intact",
            [
                sql("UPDATE meta SET value = '7' WHERE key = 'min_reader_revision';"),
                bind,
            ],
            "refused",
        ),
        case(
            "bad_store_id",
            "intact",
            [sql("UPDATE meta SET value = 'NOT-AN-ID' WHERE key = 'store_id';"), bind],
            "refused",
        ),
        case(
            "no_fingerprint",
            "intact",
            [sql("DELETE FROM meta WHERE key = 'key_fingerprint';"), bind],
            "refused",
        ),
        case(
            "schema_ahead",
            "intact",
            [sql("UPDATE meta SET value = '2' WHERE key = 'schema_version';"), bind],
            "refused",
        ),
        case("missing_store", "intact", [{"remove_store": True}], "refused"),
        case(
            "engine_newer",
            "intact",
            [
                sql("UPDATE meta SET value = '99.0.0' WHERE key = 'engine_version';"),
                bind,
            ],
        ),
        case(
            "engine_junk",
            "intact",
            [sql("UPDATE meta SET value = 'junk' WHERE key = 'engine_version';"), bind],
        ),
        case(
            "digest_other_fingerprint",
            "rekeyed",
            [
                sql(
                    "UPDATE digest_state SET value = '"
                    + "0" * 64
                    + "' WHERE key = 'fingerprint';"
                ),
                bind,
            ],
        ),
        case(
            "digest_missing",
            "fts",
            [
                sql(
                    "DROP TABLE digest_records; DROP TABLE digest_tokens; "
                    "DROP TABLE digest_state;"
                ),
                bind,
            ],
        ),
        case(
            "digest_unfinished",
            "intact",
            [
                sql(
                    "DELETE FROM digest_state WHERE key IN ('seq', 'mac'); "
                    "INSERT INTO digest_state(key, value) VALUES ('build', 'deadbeef');"
                ),
                bind,
            ],
        ),
        # `digest_state.seq` as `str.isdigit` and `int()` read it.
        case(
            "digest_seq_arabic_indic",
            "intact",
            [sql(_digits_to("\u0660") + bind_sql), bind],
        ),
        case(
            "digest_seq_superscript",
            "intact",
            [sql("UPDATE digest_state SET value = '\u00b2' WHERE key = 'seq';"), bind],
        ),
        case(
            "digest_seq_spaced",
            "intact",
            [
                sql("UPDATE digest_state SET value = ' ' || value WHERE key = 'seq';"),
                bind,
            ],
        ),
        case(
            "digest_seq_overflow",
            "intact",
            [
                sql(
                    "UPDATE digest_state SET value = '9223372036854775808' "
                    "WHERE key = 'seq';"
                ),
                bind,
            ],
            "raises",
        ),
        # A refusal with a writer's sidecars beside the file: a live WAL is
        # left as it is; a hot journal is rolled back, as any connection
        # that can write rolls it back at its first read.
        case(
            "not_ported_live_wal",
            "intact",
            [
                {"write": ".key", "hex": "ee" * 32},
                bind,
                {
                    "live_wal": "INSERT INTO meta(key, value) "
                    "VALUES ('fixture_wal', 'only in the wal');"
                },
            ],
            "not_ported:key_foreign",
        ),
        case(
            "not_ported_hot_journal",
            "intact",
            [
                {"write": ".key", "hex": "ee" * 32},
                bind,
                sql("PRAGMA journal_mode = DELETE;"),
                {
                    "hot_journal": "UPDATE meta SET value = 'in the journal' "
                    "WHERE key = 'engine_version'"
                },
            ],
            "not_ported:key_foreign",
        ),
        case(
            "digest_moved_log",
            "rekeyed",
            [sql("UPDATE digest_state SET value = 'x' WHERE key = 'mac';"), bind],
        ),
    ]


# ---------------------------------------------------------------------------
# Setting a case up
# ---------------------------------------------------------------------------


def _bind_text(target: Path) -> str:
    return json.dumps({"path": str(target.resolve())})


def _live_wal(store: Path, sql: str) -> None:
    """Leave `sql` committed only in a ``-wal`` beside its ``-shm``, as a
    writer that died with the store open leaves it."""
    keep = store.parent / ".live"
    keep.mkdir()
    conn = sqlite3.connect(store, isolation_level=None)
    try:
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.executescript(sql)
        for suffix in ("", "-wal", "-shm"):
            shutil.copyfile(f"{store}{suffix}", keep / f"f{suffix}")
    finally:
        conn.close()
    for suffix in ("", "-wal", "-shm"):
        os.replace(keep / f"f{suffix}", f"{store}{suffix}")
    keep.rmdir()


def _hot_journal(store: Path, sql: str) -> None:
    """Leave `sql` uncommitted with its rollback journal beside the file,
    as a writer in rollback-journal mode that died mid-transaction leaves
    it (a hot journal)."""
    keep = store.parent / ".hot"
    keep.mkdir()
    journal = Path(f"{store}-journal")
    conn = sqlite3.connect(store, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(sql)
        shutil.copyfile(store, keep / "f")
        shutil.copyfile(journal, keep / "f-journal")
        conn.execute("ROLLBACK")
    finally:
        conn.close()
    os.replace(keep / "f", store)
    os.replace(keep / "f-journal", journal)
    keep.rmdir()


def setup(recipe: list[dict[str, Any]], case: Path) -> None:
    """Apply a case's recipe: `gen_store_files.apply`'s ops, and this
    fixture's own (a binding to a file under the case, resolved or as
    spelled, a store copy and SQL run on it, a live WAL, a hot journal, a
    file, a directory, a symbolic link, a mode, no store)."""
    keys = case / "keys"
    for op in recipe:
        if "bind" in op:
            (keys / f"{STORE_ID}.path").write_text(
                _bind_text(case / op["bind"]), encoding="utf-8"
            )
        elif "bind_raw" in op:
            (keys / f"{STORE_ID}.path").write_text(
                json.dumps({"path": f"{case}/{op['bind_raw']}"}), encoding="utf-8"
            )
        elif "copy_store" in op:
            target = case / op["copy_store"]
            target.parent.mkdir(parents=True)
            shutil.copyfile(case / "memory.sqlite", target)
        elif "sql_at" in op:
            conn = sqlite3.connect(case / op["sql_at"], isolation_level=None)
            try:
                conn.executescript(op["sql"])
            finally:
                conn.close()
        elif "file" in op:
            target = case / op["file"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bytes.fromhex(op["hex"]))
        elif "mkdir" in op:
            (case / op["mkdir"]).mkdir(parents=True)
        elif "symlink" in op:
            (case / op["symlink"]).symlink_to(op["to"])
        elif "chmod" in op:
            os.chmod(case / op["chmod"], int(op["mode"], 8))
        elif "hot_journal" in op:
            _hot_journal(case / "memory.sqlite", op["hot_journal"])
        elif "live_wal" in op:
            _live_wal(case / op.get("at", "memory.sqlite"), op["live_wal"])
        elif "remove_store" in op:
            (case / "memory.sqlite").unlink()
        else:
            apply_base_ops([op], case)


@contextmanager
def _engine() -> Iterator[None]:
    with patch.object(bettermemory, "__version__", ENGINE):
        yield


def _made(base: str, bases: dict[str, Any], case: Path) -> None:
    """The case's starting store: a base rebuilt, or (``created``) the
    store `Store.create` makes under ``store_create.json``'s fixed values."""
    if base == "created":
        case.mkdir()
        with gen_store_create._fixed():
            Store.create(case, keys_dir=case / "keys").close()
        return
    rebuild(bases[base], case)


# ---------------------------------------------------------------------------
# Reading a state
# ---------------------------------------------------------------------------


def _cell(value: Any) -> str:
    if value is None:
        return "N"
    if isinstance(value, int):
        return f"i{value}"
    if isinstance(value, float):
        return f"r{value!r}"
    if isinstance(value, str):
        return f"t{len(value.encode('utf-8'))}:{value}"
    return f"b{bytes(value).hex()}"


def _canonical(rows: list[tuple[Any, ...]]) -> str:
    return "\x1e".join("\x1f".join(_cell(v) for v in row) for row in rows)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scrub(text: str, root: Path) -> str:
    for spelling in sorted({str(root.resolve()), str(root)}, key=len, reverse=True):
        text = text.replace(spelling, "<scratch>")
    return text


def _mode(path: Path) -> str:
    return f"{stat.S_IMODE(os.lstat(path).st_mode):04o}"


def state(case: Path, root: Path) -> dict[str, Any]:
    """The compared parts of a case's store and keys directory."""
    out: dict[str, Any] = {
        "dir": sorted(p.name for p in case.iterdir() if p.name != "keys"),
        "keys": [],
    }
    keys = case / "keys"
    if keys.is_dir():
        for p in sorted(keys.iterdir()):
            item: dict[str, Any] = {
                "name": p.name.replace(STORE_ID, "<id>"),
                "mode": _mode(p),
            }
            if p.suffix in (".head", ".origin", ".path") or p.name.endswith(".json"):
                item["text"] = _scrub(
                    p.read_text(encoding="utf-8", errors="replace"), root
                )
            elif p.name.endswith((".key", ".pending")):
                item["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
            else:
                item["size"] = p.stat().st_size
            out["keys"].append(item)
    store = case / "memory.sqlite"
    if not store.is_file():
        return out
    out["mode"] = _mode(store)
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        try:
            master = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            out["unreadable"] = type(exc).__name__
            return out
        out["master"] = _sha(_canonical(master))
        out["pragmas"] = {p: conn.execute(f"PRAGMA {p}").fetchone()[0] for p in PRAGMAS}
        virtual = [
            n for k, n, _t, s in master if k == "table" and "VIRTUAL" in (s or "")
        ]
        shadows = tuple(f"{n}_" for n in virtual)
        lines: list[str] = []
        for kind, name, _t, sql in master:
            if (
                kind != "table"
                or name in virtual
                or name.startswith(("sqlite_", *shadows))
            ):
                continue
            width = len(conn.execute(f'PRAGMA table_info("{name}")').fetchall())
            rowid = "WITHOUT ROWID" not in (sql or "").upper()
            select = "rowid, *" if rowid else "*"
            order = ", ".join(str(i + 1) for i in range(width + (1 if rowid else 0)))
            rows = conn.execute(
                f'SELECT {select} FROM "{name}" ORDER BY {order}'
            ).fetchall()
            digest = _sha(_canonical(rows))
            lines.append(f"{name}\t{len(rows)}\t{digest}\n")
            if name in ("meta", "digest_state"):
                out[name] = [[_cell(v) for v in row] for row in rows]
            elif name in ("log", "digest_records", "digest_tokens"):
                out[name] = {"count": len(rows), "sha256": digest}
        out["tables"] = _sha("".join(lines))
    finally:
        conn.close()
    return out


def _changed(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Which parts a not-ported state's reference open changed."""
    parts: list[str] = []
    for key in ("dir", "mode", "master", "pragmas"):
        if before.get(key) != after.get(key):
            parts.append(key)
    names = {k["name"]: k for k in before.get("keys", [])}
    for item in after.get("keys", []):
        if names.get(item["name"]) != item:
            parts.append(f"keys/{item['name']}")
    for name in set(names) - {k["name"] for k in after.get("keys", [])}:
        parts.append(f"keys/{name} (removed)")
    for name in (
        "tables",
        "meta",
        "digest_state",
        "log",
        "digest_records",
        "digest_tokens",
    ):
        if before.get(name) != after.get(name):
            parts.append(name)
    return sorted(parts)


def _refusal(exc: BaseException, root: Path) -> dict[str, Any]:
    return {"class": type(exc).__name__, "text": _scrub(str(exc), root)}


def _close_left(root: Path) -> None:
    """Close the store a raise out of `Store.open` left open under
    ``root``, as the native build's open closes its connection when it
    returns the error. The raise leaves the `Store` to whatever still
    holds the traceback's frames (the collector, or a test runner's
    report), and its connection's close is what checkpoints the WAL and
    removes the ``-wal`` and ``-shm``."""
    gc.collect()
    roots = {root, root.resolve()}
    for obj in gc.get_objects():
        if isinstance(obj, Store) and roots & set(Path(obj._path).parents):
            obj.close()


def run_case(c: dict[str, Any], bases: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        case = root / "c"
        _made(c["base"], bases, case)
        setup(c["recipe"], case)
        before = state(case, root)
        rolled_back = None
        if any("hot_journal" in op for op in c["recipe"]):
            # What any connection that can write leaves at its first read.
            copy = root / "r"
            shutil.copytree(case, copy, symlinks=True)
            conn = sqlite3.connect(copy / "memory.sqlite")
            try:
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            finally:
                conn.close()
            rolled_back = state(copy, root)
        outcome: dict[str, Any] = {"opened": True}
        for _ in range(int(c.get("opens", 1))):
            try:
                with _engine():
                    store = Store.open(
                        case / "memory.sqlite", keys_dir=case / "keys", allow_rekey=True
                    )
                outcome["synchronous"] = store.conn.execute(
                    "PRAGMA synchronous"
                ).fetchone()[0]
                store.close()
            except Exception as exc:  # noqa: BLE001 - the refusal is the record
                outcome = {"opened": False, "refusal": _refusal(exc, root)}
                break
        if not outcome["opened"]:
            _close_left(root)
        after = state(case, root)
    record: dict[str, Any] = {
        **c,
        "before": {k: before.get(k) for k in ("master", "tables")},
        "outcome": outcome,
    }
    if rolled_back is not None:
        record["rolled_back"] = rolled_back
    if c["native"] == "raises":
        record["after"] = after
    elif c["native"].startswith("not_ported") or not outcome["opened"]:
        record["reference_changed"] = _changed(before, after)
    else:
        record["after"] = after
    return record


def generate() -> dict[str, bytes]:
    bases = load_bases()
    lines = [
        {
            "fixture": "store_open",
            "engine_version": ENGINE,
            "store_id": STORE_ID,
            "pragmas": list(PRAGMAS),
        }
    ]
    lines += [run_case(c, bases) for c in cases(bases)]
    text = "".join(
        json.dumps(line, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        + "\n"
        for line in lines
    )
    return {"store_open.jsonl": text.encode("ascii")}
