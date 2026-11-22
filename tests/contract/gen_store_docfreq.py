"""Fixture: the document frequency memo (``store_docfreq.jsonl``).

The native build reads it in
``native/crates/bettermemory-core/src/store/docfreq.rs``. This pins it to
`store_index`'s `_memoised_document_frequencies`, `_current_inputs`,
`_patched_masks`, `_admitted_mask`, `_keep_doc_freqs` and `_row_mask`,
with `Store.ranking_statistics` around them and the grouping of
`Store._read_doc_freq_inputs`, over the reference's own memo
(`_DOC_FREQS`, cleared at each case's start).

The store's side is a stand-in (`_Fake`): an in-memory SQLite database
with the five tables the memo's reads name (``memories``, ``homes``,
``digest_records``, ``log`` and an FTS5 ``memories_fts``), driven by the
reference's own `_memory_head`, `_advanced_inputs`,
`Store._read_doc_freq_inputs` and `Store._term_masks`, and its digest
answers (`digest_admission_rows`, `digest_mark`) read from
``digest_records`` while the case's ``digest`` flag holds. The native
test's double (``tests/common/docfreq_source.rs``) builds the same tables
and runs the same statements; the counts recorded are the memo's.

A case is a list of operations, each a list:

- ``["put", s, n, body, scopes, home, client, model]``: record ``r<n>``
  of store ``s`` written: its ``memories`` row (an upsert keeps the
  row number), its FTS row (body, and the scopes joined by spaces), its
  ``homes`` row (removed for a null home), its digest row (length the
  body's words) and a ``memory_put`` log row. ``scopes`` is a list
  (stored as ``json.dumps``) or a text stored as it is.
- ``["bulk", s, first, count, body, scopes]``: ``put`` of records
  ``first`` .. ``first + count - 1``, no home, no actor.
- ``["home", s, n, project]``, ``["unhome", s, n]``: a ``homes`` row put
  or removed, with its ``home_put`` or ``home_delete`` log row.
- ``["del", s, n]``: the record removed (row, FTS row, digest row marked
  removed) with a ``memory_delete`` log row.
- ``["log", s, kind]``: a log row of that kind alone.
- ``["remac", s, seq]``: the MAC of log row ``seq`` changed.
- ``["digest", s, on]``: the digest answers or not.
- ``["tx", s, on]``: a transaction open or not.
- ``["clear"]``: `_clear_doc_freqs`.
- ``["q", s, terms, admit]`` and ``["qn", s, prefix, count, admit]``
  (terms ``prefix`` and four digits, 0 up): `ranking_statistics`.
- ``["qr", s, terms, admit, op]``: the same, with ``op`` done when the
  first term's masks are read (another connection writes meanwhile).

An admit spec is ``["all"]``, ``["none"]``, ``["scope", x]`` (x among the
scopes), ``["home", p]`` (p the home; null for none) or ``["client", c]``.
A store ``s`` is the path ``/stores/s<s>`` and the id ``id<s>``; a log
row's MAC is ``m<seq>``.

Each query answers ``r``: null, ``"uncached"``, an error class, or
``[size, body_df, scope_df, average]`` (dfs as pairs in term order, the
average's ``repr``); ``t``, the store's side as the memo called it
(``head``, ``read``, ``adv:ok`` or ``adv:none``, ``tm:<term>``,
``fts:<expr>:<low>``) with each admit call (``a:`` and its JSON), or
past 60 events their count and SHA-256; and ``k``, the memo's stores
afterwards (store numbers, least recent first) with ``m``, the terms
the queried store's entry keeps (or their count and SHA-256 past 20).
Nothing here depends on the platform or the Python release.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any
from unittest import mock

from bettermemory import store_index
from bettermemory.store import Store

from ._common import jsonl

SCHEMA = (
    "CREATE TABLE memories(id TEXT PRIMARY KEY, scopes_json TEXT, "
    "actor_client TEXT, actor_model TEXT)",
    "CREATE TABLE homes(record_id TEXT PRIMARY KEY, project TEXT)",
    "CREATE TABLE digest_records(rid INTEGER PRIMARY KEY, id TEXT, scopes TEXT, "
    "client TEXT, model TEXT, dl INTEGER, removed INTEGER)",
    "CREATE TABLE log(seq INTEGER PRIMARY KEY, kind TEXT, payload TEXT, mac TEXT)",
    "CREATE VIRTUAL TABLE memories_fts USING fts5(body_fts, scopes_fts)",
)


def _short(events: list[str], bound: int) -> Any:
    if len(events) <= bound:
        return events
    text = json.dumps(events, separators=(",", ":"))
    return {"n": len(events), "sha": hashlib.sha256(text.encode()).hexdigest()}


class _Conn:
    """The connection, with the patch's statement traced."""

    def __init__(self, conn: sqlite3.Connection, fake: _Fake) -> None:
        self._c = conn
        self._f = fake

    def execute(self, sql: str, params: Any = ()) -> Any:
        if "rowid >= ?" in sql:
            self._f.trace.append(f"fts:{params[0]}:{params[1]}")
        return self._c.execute(sql, params)

    @property
    def in_transaction(self) -> bool:
        return self._f.tx


class _Fake:
    _memoised_document_frequencies = store_index._memoised_document_frequencies
    _keep_doc_freqs = staticmethod(store_index._keep_doc_freqs)
    ranking_statistics = Store.ranking_statistics

    def __init__(self, number: int) -> None:
        raw = sqlite3.connect(":memory:")
        raw.row_factory = sqlite3.Row
        for statement in SCHEMA:
            raw.execute(statement)
        self.raw = raw
        self._conn = _Conn(raw, self)
        self._path = f"/stores/s{number}"
        self._store_id = f"id{number}"
        self._tx_depth = 0
        self.tx = False
        self.digest = True
        self.trace: list[str] = []
        self.hook: list[Any] | None = None
        self.case: _Case | None = None

    def _memory_head(self) -> tuple[int, str]:
        self.trace.append("head")
        return store_index._memory_head(self)  # type: ignore[arg-type]

    def _read_doc_freq_inputs(self, head: tuple[int, str]) -> Any:
        self.trace.append("read")
        return Store._read_doc_freq_inputs(self, head)  # type: ignore[arg-type]

    def _term_masks(self, term: str, inputs: Any) -> tuple[int, int]:
        self.trace.append(f"tm:{term}")
        if self.hook is not None and self.case is not None:
            hook, self.hook = self.hook, None
            self.case.apply(hook)
        return Store._term_masks(self, term, inputs)  # type: ignore[arg-type]

    def document_frequencies(self, *args: Any, **kwargs: Any) -> Any:
        # The uncached call, marked: `ranking_statistics` adds a None.
        return ("uncached",)

    def digest_mark(self, build: bool = False) -> str | None:
        return "d" if self.digest else None

    def digest_admission_rows(self) -> list[Any] | None:
        if not self.digest:
            return None
        return [
            (int(r[0]), str(r[1]), r[2], r[3], r[4], int(r[5]))
            for r in self.raw.execute(
                "SELECT d.rid, d.scopes, h.project, d.client, d.model, d.dl "
                "FROM digest_records d LEFT JOIN homes h ON h.record_id = d.id "
                "WHERE d.removed = 0 ORDER BY d.rid"
            )
        ]

    def log(self, kind: str, payload: dict[str, Any]) -> None:
        seq = int(
            self.raw.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM log").fetchone()[0]
        )
        self.raw.execute(
            "INSERT INTO log(seq, kind, payload, mac) VALUES (?, ?, ?, ?)",
            (seq, kind, json.dumps(payload), f"m{seq}"),
        )


def _admit(spec: list[Any], calls: list[str]) -> Any:
    def admit(scopes: list[Any], home: str | None, actor: Any) -> bool:
        client = None if actor is None else actor.client
        model = None if actor is None else actor.model
        calls.append(
            "a:" + json.dumps([scopes, home, client, model], separators=(",", ":"))
        )
        kind = spec[0]
        if kind == "all":
            return True
        if kind == "none":
            return False
        if kind == "scope":
            return spec[1] in scopes
        if kind == "home":
            return home == spec[1]
        return client == spec[1]

    return admit


class _Case:
    def __init__(self) -> None:
        self.stores: dict[int, _Fake] = {}

    def store(self, number: int) -> _Fake:
        if number not in self.stores:
            fake = _Fake(number)
            fake.case = self
            self.stores[number] = fake
        return self.stores[number]

    def put(
        self, s: int, n: int, body: str, scopes: Any, home: Any, client: Any, model: Any
    ) -> None:
        f = self.store(s)
        rid_text = f"r{n}"
        text = json.dumps(scopes) if isinstance(scopes, list) else scopes
        words = (
            " ".join(x for x in scopes if isinstance(x, str))
            if isinstance(scopes, list)
            else scopes
        )
        c = f.raw
        row = c.execute(
            "SELECT rowid FROM memories WHERE id = ?", (rid_text,)
        ).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO memories(id, scopes_json, actor_client, actor_model) "
                "VALUES (?, ?, ?, ?)",
                (rid_text, text, client, model),
            )
            rid = int(
                c.execute(
                    "SELECT rowid FROM memories WHERE id = ?", (rid_text,)
                ).fetchone()[0]
            )
        else:
            rid = int(row[0])
            c.execute(
                "UPDATE memories SET scopes_json = ?, actor_client = ?, actor_model = ? "
                "WHERE id = ?",
                (text, client, model, rid_text),
            )
        c.execute("DELETE FROM memories_fts WHERE rowid = ?", (rid,))
        c.execute(
            "INSERT INTO memories_fts(rowid, body_fts, scopes_fts) VALUES (?, ?, ?)",
            (rid, body, words),
        )
        if home is None:
            c.execute("DELETE FROM homes WHERE record_id = ?", (rid_text,))
        else:
            c.execute(
                "INSERT OR REPLACE INTO homes(record_id, project) VALUES (?, ?)",
                (rid_text, home),
            )
        c.execute(
            "INSERT OR REPLACE INTO digest_records(rid, id, scopes, client, model, dl, removed) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (rid, rid_text, text, client, model, len(body.split())),
        )
        f.log("memory_put", {"memory": {"id": rid_text}})

    def apply(self, op: list[Any]) -> None:
        kind = op[0]
        if kind == "clear":
            store_index._clear_doc_freqs()
            return
        f = self.store(op[1])
        c = f.raw
        if kind == "put":
            self.put(*op[1:])
        elif kind == "bulk":
            _, s, first, count, body, scopes = op
            for n in range(first, first + count):
                self.put(s, n, body, scopes, None, None, None)
        elif kind == "home":
            c.execute(
                "INSERT OR REPLACE INTO homes(record_id, project) VALUES (?, ?)",
                (f"r{op[2]}", op[3]),
            )
            f.log("home_put", {"home": {"record_id": f"r{op[2]}"}})
        elif kind == "unhome":
            c.execute("DELETE FROM homes WHERE record_id = ?", (f"r{op[2]}",))
            f.log("home_delete", {"record_id": f"r{op[2]}"})
        elif kind == "del":
            rid = int(
                c.execute(
                    "SELECT rowid FROM memories WHERE id = ?", (f"r{op[2]}",)
                ).fetchone()[0]
            )
            c.execute("DELETE FROM memories WHERE rowid = ?", (rid,))
            c.execute("DELETE FROM memories_fts WHERE rowid = ?", (rid,))
            c.execute("UPDATE digest_records SET removed = 1 WHERE rid = ?", (rid,))
            f.log("memory_delete", {"id": f"r{op[2]}"})
        elif kind == "log":
            f.log(op[2], {})
        elif kind == "remac":
            c.execute("UPDATE log SET mac = 'x' WHERE seq = ?", (op[2],))
        elif kind == "digest":
            f.digest = bool(op[2])
        elif kind == "tx":
            f.tx = bool(op[2])
        else:
            raise ValueError(kind)

    def query(self, op: list[Any]) -> dict[str, Any]:
        kind = op[0]
        f = self.store(op[1])
        if kind == "qn":
            terms = [f"{op[2]}{i:04d}" for i in range(op[3])]
            spec = op[4]
        else:
            terms, spec = op[2], op[3]
        if kind == "qr":
            f.hook = op[4]
        f.trace = []
        real_advance = store_index._advanced_inputs

        def advance(store: Any, entry: Any, head: Any) -> Any:
            out = real_advance(store, entry, head)
            f.trace.append("adv:none" if out is None else "adv:ok")
            return out

        try:
            with mock.patch.object(store_index, "_advanced_inputs", advance):
                fake: Any = f
                got: Any = fake.ranking_statistics(terms, admit=_admit(spec, f.trace))
        except Exception as exc:  # the class is the answer
            # json.loads raises JSONDecodeError, a ValueError.
            answer: Any = (
                "ValueError" if isinstance(exc, ValueError) else type(exc).__name__
            )
        else:
            if got is None or got[0] == "uncached":
                answer = None if got is None else "uncached"
            else:
                size, body_df, scope_df, average = got
                answer = [
                    size,
                    [[k, v] for k, v in body_df.items()],
                    [[k, v] for k, v in scope_df.items()],
                    None if average is None else repr(average),
                ]
        f.hook = None
        kept = [int(k[0].rsplit("s", 1)[1]) for k in store_index._DOC_FREQS]
        entry = store_index._DOC_FREQS.get((f._path, f._store_id))
        terms_kept = [] if entry is None else list(entry.terms)
        return {
            "r": answer,
            "t": _short(f.trace, 60),
            "k": kept,
            "m": _short(terms_kept, 20),
        }


def _scope_rows(count: int) -> list[list[Any]]:
    return [
        [
            "put",
            0,
            n,
            f"alpha body{n % 3} common",
            ["proj", f"s{n % 2}"],
            None,
            None,
            None,
        ]
        for n in range(count)
    ]


def _cases() -> list[tuple[str, list[list[Any]]]]:
    q = ["q", 0, ["alpha", "body0", "common", "proj", "zeta"], ["all"]]
    cases: list[tuple[str, list[list[Any]]]] = []
    cases.append(
        (
            "basic",
            [
                *_scope_rows(4),
                q,
                q,
                ["q", 0, ["alpha", "s1"], ["scope", "s1"]],
                ["q", 0, ["alpha"], ["none"]],
                ["q", 0, ["new"], ["none"]],
                ["q", 0, ["", "alpha", "alpha", "s0", ""], ["all"]],
                ["q", 0, ["", ""], ["all"]],
                ["q", 0, ['say "hi"', "alpha"], ["all"]],
            ],
        )
    )
    cases.append(
        (
            "groups",
            [
                ["put", 0, 1, "one two", ["a"], "p", "cli", None],
                ["put", 0, 2, "one three", ["a"], "p", "cli", None],
                ["put", 0, 3, "two", ["a"], None, None, "mod"],
                ["put", 0, 4, "two four", ["a", "b"], "q", None, None],
                ["put", 0, 5, "one", ["a"], "p", "cli", "mod"],
                ["q", 0, ["one", "two", "b"], ["all"]],
                ["q", 0, ["one", "two", "b"], ["home", "p"]],
                ["q", 0, ["one", "two", "b"], ["home", None]],
                ["q", 0, ["one", "two", "b"], ["client", "cli"]],
            ],
        )
    )
    cases.append(
        (
            "writes",
            [
                *_scope_rows(5),
                q,
                ["put", 0, 9, "alpha fresh", ["proj"], None, None, None],
                q,
                ["put", 0, 1, "zeta now", ["other"], "home1", None, None],
                q,
                ["q", 0, ["alpha", "zeta"], ["scope", "other"]],
                ["home", 0, 2, "hp"],
                ["q", 0, ["alpha", "zeta"], ["home", "hp"]],
                ["unhome", 0, 2],
                ["q", 0, ["alpha", "zeta"], ["home", "hp"]],
                ["log", 0, "telemetry"],
                q,
                ["put", 0, 3, "body0 again", ["proj", "s1"], None, None, None],
                ["put", 0, 3, "zeta last", ["proj", "s1"], None, None, None],
                q,
                ["del", 0, 4],
                q,
                ["log", 0, "index_rebuilt"],
                q,
            ],
        )
    )
    cases.append(
        (
            "patch_scopes_only",
            [
                *_scope_rows(3),
                ["q", 0, ["s1", "alpha"], ["all"]],
                ["put", 0, 7, "nothing here", ["s1"], None, None, None],
                ["q", 0, ["s1", "alpha"], ["all"]],
                ["put", 0, 8, "s1 in body", ["x"], None, None, None],
                ["q", 0, ["s1", "alpha"], ["all"]],
            ],
        )
    )
    cases.append(
        (
            "patch_window",
            [
                ["bulk", 0, 0, 70, "wide word", ["w"]],
                ["q", 0, ["wide", "word"], ["all"]],
                ["put", 0, 100, "wide new", ["w"], None, None, None],
                ["q", 0, ["wide", "word"], ["all"]],
                ["put", 0, 3, "wide again", ["w"], None, None, None],
                ["q", 0, ["wide", "word"], ["all"]],
                ["put", 0, 60, "word only", ["w"], None, None, None],
                ["q", 0, ["wide", "word"], ["all"]],
            ],
        )
    )
    cases.append(
        (
            "patch_rows",
            [
                *_scope_rows(3),
                ["q", 0, ["alpha", "common"], ["all"]],
                ["bulk", 0, 10, 8, "alpha", ["proj"]],
                ["q", 0, ["alpha"], ["all"]],
                ["bulk", 0, 20, 9, "alpha", ["proj"]],
                ["q", 0, ["alpha", "common"], ["all"]],
            ],
        )
    )
    cases.append(
        (
            "advance_bound",
            [
                *_scope_rows(2),
                ["q", 0, ["alpha"], ["all"]],
                ["bulk", 0, 10, 256, "alpha", ["proj"]],
                ["q", 0, ["alpha"], ["all"]],
                ["bulk", 0, 300, 257, "alpha", ["proj"]],
                ["q", 0, ["alpha"], ["all"]],
            ],
        )
    )
    cases.append(
        (
            "no_digest",
            [
                ["digest", 0, False],
                *_scope_rows(3),
                q,
                ["put", 0, 9, "alpha", ["proj"], None, None, None],
                q,
                ["digest", 0, True],
                q,
                ["digest", 0, False],
                ["put", 0, 10, "alpha", ["proj"], None, None, None],
                q,
            ],
        )
    )
    cases.append(
        (
            "other_history",
            [
                *_scope_rows(3),
                q,
                ["remac", 0, 3],
                ["put", 0, 9, "alpha", ["proj"], None, None, None],
                q,
            ],
        )
    )
    cases.append(
        (
            "scopes_texts",
            [
                ["put", 0, 1, "alpha", ["a"], None, None, None],
                ["put", 0, 2, "alpha", "{}", None, None, None],
                ["put", 0, 3, "alpha", "null", None, None, None],
                ["put", 0, 4, "alpha", [1, None, "a"], None, None, None],
                ["put", 0, 5, "alpha", "[]", None, None, None],
                ["put", 0, 6, "alpha", ' ["a"] ', None, None, None],
                ["q", 0, ["alpha", "a"], ["all"]],
                ["q", 0, ["alpha", "a"], ["scope", "a"]],
                ["put", 0, 2, "alpha", ["a"], None, None, None],
                ["q", 0, ["alpha"], ["all"]],
                ["put", 0, 1, "alpha", "{}", None, None, None],
                ["q", 0, ["alpha"], ["all"]],
                ["put", 0, 7, "alpha", "[1,", None, None, None],
                ["q", 0, ["alpha"], ["all"]],
                ["clear"],
                ["q", 0, ["alpha"], ["all"]],
            ],
        )
    )
    cases.append(
        (
            "transaction",
            [
                *_scope_rows(2),
                ["tx", 0, True],
                q,
                ["q", 0, [""], ["all"]],
                ["tx", 0, False],
                q,
            ],
        )
    )
    cases.append(
        (
            "race",
            [
                *_scope_rows(3),
                [
                    "qr",
                    0,
                    ["alpha", "common"],
                    ["all"],
                    ["put", 0, 8, "alpha", ["proj"], None, None, None],
                ],
                q,
                [
                    "qr",
                    0,
                    ["new1"],
                    ["all"],
                    ["put", 0, 9, "alpha", ["proj"], None, None, None],
                ],
                ["q", 0, ["new1", "alpha"], ["all"]],
            ],
        )
    )
    stores: list[list[Any]] = []
    for s in range(9):
        stores.append(["put", s, 1, "alpha", ["a"], None, None, None])
        stores.append(["q", s, ["alpha"], ["all"]])
    stores += [
        ["q", 0, ["alpha"], ["all"]],
        ["q", 8, ["alpha"], ["all"]],
        ["q", 2, ["alpha"], ["all"]],
    ]
    cases.append(("stores", stores))
    cases.append(
        (
            "terms",
            [
                ["put", 0, 1, "t0000 t4096 t2000", ["a"], None, None, None],
                ["qn", 0, "t", 4096, ["all"]],
                ["q", 0, ["t0000"], ["all"]],
                ["qn", 0, "t", 4097, ["all"]],
                ["q", 0, ["t4096", "t0001"], ["all"]],
                ["q", 0, ["t0000"], ["all"]],
                ["put", 0, 2, "t0000", ["a"], None, None, None],
                ["q", 0, ["t0000", "t2000"], ["all"]],
            ],
        )
    )
    return cases


def _run(ops: list[list[Any]]) -> list[Any]:
    store_index._clear_doc_freqs()
    case = _Case()
    out = []
    for op in ops:
        if op[0] in ("q", "qn", "qr"):
            out.append(case.query(op))
        else:
            case.apply(op)
    store_index._clear_doc_freqs()
    return out


def generate() -> dict[str, bytes]:
    records: list[Any] = [
        {
            "fixture": "store_docfreq",
            "schema": list(SCHEMA),
            "bounds": [store_index.DOC_FREQ_STORES, store_index.DOC_FREQ_TERMS],
            "cases": len(_cases()),
        }
    ]
    for name, ops in _cases():
        records.append({"case": name, "ops": ops, "out": _run(ops)})
    return {"store_docfreq.jsonl": jsonl(records)}
