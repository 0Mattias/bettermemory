//! Cases a plausible wrong port of the native membership writes,
//! `membership_log` and `reissue_membership_store_id` fails: a legacy row
//! written for a surrogate key, `[]` and `{}` swapped for a `_json`
//! column, the native put appended before the legacy one, `_clear_native`
//! run after a native put, a log read kind by kind (rows out of seq
//! order), a checkout compared outside SQLite, and the reissue's token
//! drawn inside the transaction (a store whose transaction cannot begin
//! still drew it in the reference), the surrogate walk's frame budget
//! moved, `require_native_writer` checked after the legacy put, the log's
//! kinds taken from the wrong slice of the mutation tables or bound with
//! no membership kind, a log read past the row a caller stopped at, and a
//! reissue run without its savepoint. POSIX only, as the store fixture
//! is.
//! Each answer was read from the reference (`store_membership.py`,
//! `store_native.py`) under CPython 3.13.13.

#![cfg(unix)]

mod common;

use std::cell::Cell;
use std::io;
use std::ops::ControlFlow;
use std::path::Path;
use std::rc::Rc;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::log::Key;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::store::create::{self, Provider};
use bettermemory_core::store::membership as m;
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::rows::{membership_spec, MUTATION_TABLES};
use bettermemory_core::store::write::WriteError;
use bettermemory_core::unicode;
use common::created::{fixed, fixture};
use common::opened;
use common::rows::Counting;
use common::stores::scratch;

const STAMP: &str = "2001-02-03T06:00:00.000001Z";

fn fresh(name: &str) -> (common::stores::Scratch, WritableStore) {
    let dir = scratch(name);
    let c = dir.0.join("c");
    create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    let store = open(&c);
    (dir, store)
}

fn open(c: &Path) -> WritableStore {
    open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("open")
}

fn s(t: &str) -> PyValue {
    PyValue::str(t)
}

/// `"r\udc80"`.
fn sur() -> PyValue {
    PyValue::Str(vec![0x72, 0xDC80])
}

fn cps(t: &str) -> PyStr {
    unicode::cps(t)
}

fn machine(id: PyValue, facts: PyValue) -> Vec<(PyValue, PyValue)> {
    vec![
        (s("id"), id),
        (s("facts_json"), facts),
        (s("predecessor"), PyValue::None),
        (s("minted"), s("t")),
    ]
}

fn kinds(store: &WritableStore) -> Vec<String> {
    let conn = store.connection();
    let mut st = conn
        .prepare("SELECT kind FROM log ORDER BY seq")
        .expect("log");
    let kinds = st.query_map([], |r| r.get(0)).expect("rows");
    kinds.collect::<rusqlite::Result<_>>().expect("kinds")
}

fn texts(store: &WritableStore, sql: &str) -> Vec<String> {
    let conn = store.connection();
    let mut st = conn.prepare(sql).expect("prepare");
    let rows = st.query_map([], |r| r.get(0)).expect("rows");
    rows.collect::<rusqlite::Result<_>>().expect("texts")
}

fn tail(store: &WritableStore, n: usize) -> Vec<String> {
    let all = kinds(store);
    all[all.len() - n..].to_vec()
}

#[test]
fn a_surrogate_key_writes_the_native_row_alone() {
    let (_d, mut store) = fresh("memb-native-kill-key");
    let mut p = Counting::at(STAMP, 0);
    let before = kinds(&store).len();
    let row = vec![(s("project"), s("P1")), (s("remote_key"), sur())];
    let (written, _) = store
        .put_membership(&mut p, &cps("project_remote"), &row, None)
        .expect("put");
    assert_eq!(written.kind, "native_member_put");
    assert_eq!(kinds(&store).len(), before + 1);
    assert!(texts(&store, "SELECT project FROM project_remotes").is_empty());
    assert_eq!(
        texts(&store, "SELECT key_text FROM native_membership_rows"),
        [r#"["P1","r\udc80"]"#]
    );
    assert_eq!(
        texts(&store, "SELECT row_text FROM native_membership_rows"),
        [r#"{"project":"P1","remote_key":"r\udc80"}"#]
    );
}

#[test]
fn a_json_column_blanks_a_list_to_brackets_and_anything_else_to_braces() {
    let spec = membership_spec(&cps("machine")).expect("spec");
    let cases = [
        (PyValue::List(vec![sur()]), "[]"),
        (PyValue::Tuple(vec![sur()]), "[]"),
        (PyValue::Dict(vec![(s("os"), sur())]), "{}"),
        (PyValue::Dict(vec![(sur(), s("x"))]), "{}"),
        (sur(), "{}"),
    ];
    for (facts, want) in cases {
        let row = PyValue::Dict(machine(s("M1"), facts.clone()));
        let legacy = m::legacy_row(spec, &row)
            .expect("within the budget")
            .expect("a legacy row");
        let blank = legacy.get("facts_json").expect("facts");
        let shown = match blank {
            PyValue::List(v) if v.is_empty() => "[]",
            PyValue::Dict(v) if v.is_empty() => "{}",
            other => panic!("{other:?}"),
        };
        assert_eq!(shown, want, "{facts:?}");
    }
    let (_d, mut store) = fresh("memb-native-kill-json");
    let mut p = Counting::at(STAMP, 0);
    for (id, facts) in [
        ("M1", PyValue::List(vec![sur()])),
        ("M2", PyValue::Dict(vec![(s("os"), sur())])),
    ] {
        store
            .put_membership(&mut p, &cps("machine"), &machine(s(id), facts), None)
            .expect("put");
    }
    assert_eq!(
        texts(&store, "SELECT facts_json FROM machines ORDER BY id"),
        ["[]", "{}"]
    );
}

#[test]
fn a_name_line_or_reason_keeps_its_text_escaped_and_another_column_is_none() {
    let spec = membership_spec(&cps("proposal")).expect("spec");
    let mut row = vec![
        (s("id"), s("Q1")),
        (s("checkout"), sur()),
        (s("kind"), s("join")),
        (s("candidates_json"), PyValue::List(vec![])),
        (s("evidence_json"), PyValue::Dict(vec![])),
        (s("reason"), sur()),
        (s("line"), PyValue::Str(vec![0xE9, 0xD800, 0xDC00])),
        (s("state"), s("open")),
        (s("answer"), PyValue::None),
        (s("answered_by"), PyValue::None),
        (s("signature_json"), PyValue::None),
        (s("opened"), s("t")),
    ];
    let legacy = m::legacy_row(spec, &PyValue::Dict(row.clone()))
        .expect("within the budget")
        .expect("legacy");
    assert_eq!(legacy.get("checkout"), Some(&PyValue::None));
    assert_eq!(legacy.get("reason"), Some(&s("r\\udc80")));
    // A pair of halves stays two escapes: `utf8_text` joins nothing.
    assert_eq!(legacy.get("line"), Some(&s("\u{e9}\\ud800\\udc00")));
    assert_eq!(legacy.get("id"), Some(&s("Q1")));
    // The row keeps its order.
    let PyValue::Dict(items) = &legacy else {
        panic!("{legacy:?}")
    };
    let order: Vec<_> = items.iter().map(|(k, _)| k.clone()).collect();
    let given: Vec<_> = row.iter().map(|(k, _)| k.clone()).collect();
    assert_eq!(order, given);
    // A key column holding one: no legacy row.
    row[0].1 = sur();
    assert_eq!(
        m::legacy_row(spec, &PyValue::Dict(row)).expect("walk"),
        None
    );
}

#[test]
fn the_legacy_put_comes_before_the_native_one_and_the_native_is_answered() {
    let (_d, mut store) = fresh("memb-native-kill-order");
    let mut p = Counting::at(STAMP, 0);
    let row = machine(s("M1"), PyValue::Dict(vec![(s("os"), sur())]));
    let (written, _) = store
        .put_membership(&mut p, &cps("machine"), &row, Some("s1"))
        .expect("put");
    assert_eq!(tail(&store, 2), ["machine_put", "native_member_put"]);
    assert_eq!(written.kind, "native_member_put");
    assert_eq!(written.session.as_deref(), Some("s1"));
    assert_eq!(
        texts(&store, "SELECT row_text FROM native_membership_rows"),
        [r#"{"facts_json":{"os":"r\udc80"},"id":"M1","minted":"t","predecessor":null}"#]
    );
}

#[test]
fn a_native_put_over_its_own_twin_clears_nothing() {
    let (_d, mut store) = fresh("memb-native-kill-clear");
    let mut p = Counting::at(STAMP, 0);
    let row = machine(s("M1"), PyValue::List(vec![sur()]));
    store
        .put_membership(&mut p, &cps("machine"), &row, None)
        .expect("put");
    let before = kinds(&store).len();
    store
        .put_membership(&mut p, &cps("machine"), &row, None)
        .expect("again");
    assert_eq!(kinds(&store).len(), before + 2);
    assert_eq!(tail(&store, 2), ["machine_put", "native_member_put"]);
    assert_eq!(
        texts(&store, "SELECT key_text FROM native_membership_rows").len(),
        1
    );
    // A plain put of the same key does clear it.
    store
        .put_membership(&mut p, &cps("machine"), &machine(s("M1"), s("x")), None)
        .expect("plain");
    assert_eq!(tail(&store, 2), ["machine_put", "native_member_delete"]);
    assert!(texts(&store, "SELECT key_text FROM native_membership_rows").is_empty());
}

#[test]
fn a_surrogate_delete_logs_the_native_delete_alone() {
    let (_d, mut store) = fresh("memb-native-kill-delete");
    let mut p = Counting::at(STAMP, 0);
    let key = vec![(s("project"), s("P1")), (s("remote_key"), sur())];
    store
        .put_membership(&mut p, &cps("project_remote"), &key, None)
        .expect("put");
    let before = kinds(&store).len();
    let (written, _) = store
        .delete_membership(&mut p, &cps("project_remote"), &key, None)
        .expect("delete");
    assert_eq!(written.kind, "native_member_delete");
    assert_eq!(kinds(&store).len(), before + 1);
    assert_eq!(
        written.payload,
        r#"{"key_text":"[\"P1\",\"r\\udc80\"]","member_kind":"project_remote"}"#
    );
}

#[test]
fn a_store_held_to_older_writers_refuses_before_anything_is_written() {
    let (_d, mut store) = fresh("memb-native-kill-revision");
    let mut p = Counting::at(STAMP, 0);
    store
        .connection()
        .execute(
            "UPDATE meta SET value = '4' WHERE key = 'min_writer_revision'",
            [],
        )
        .expect("meta");
    let before = kinds(&store).len();
    let row = machine(s("M1"), PyValue::List(vec![sur()]));
    let e = store
        .put_membership(&mut p, &cps("machine"), &row, None)
        .expect_err("refused");
    assert!(
        matches!(
            &e,
            WriteError::Refused {
                class: "StoreVersionError",
                ..
            }
        ),
        "{e:?}"
    );
    assert_eq!(kinds(&store).len(), before);
    assert!(texts(&store, "SELECT id FROM machines").is_empty());
}

fn record(store: &mut WritableStore, p: &mut Counting, kind: &str, checkout: PyValue) {
    store
        .record_membership(p, &cps(kind), &[(s("checkout"), checkout)], None)
        .expect("record");
}

#[test]
fn the_log_is_read_in_seq_order_whatever_order_kinds_come_in() {
    let (_d, mut store) = fresh("memb-native-kill-log-order");
    let mut p = Counting::at(STAMP, 0);
    store
        .put_membership(&mut p, &cps("machine"), &machine(s("M1"), s("x")), None)
        .expect("put");
    record(&mut store, &mut p, "checkout_registered", s("C1"));
    store
        .put_membership(&mut p, &cps("machine"), &machine(s("M2"), s("x")), None)
        .expect("put");
    let kinds_given = [
        s("machine_put"),
        s("checkout_registered"),
        s("machine_put"),
        PyValue::Tuple(vec![s("machine_put")]),
    ];
    let rows = m::membership_log(store.connection(), Some(&kinds_given), 0, None).expect("log");
    let got: Vec<_> = rows.iter().map(|r| (r.seq, r.kind.as_str())).collect();
    assert_eq!(
        got,
        [
            (2, "machine_put"),
            (3, "checkout_registered"),
            (4, "machine_put")
        ]
    );
    // An unhashable kind is TypeError, a later one too.
    let e = m::membership_log(
        store.connection(),
        Some(&[
            s("machine_put"),
            PyValue::Tuple(vec![PyValue::List(vec![])]),
        ]),
        0,
        None,
    )
    .expect_err("unhashable");
    assert!(
        matches!(&e, WriteError::Refused { class: "TypeError", text: Some(t) } if t == "unhashable type: 'list'"),
        "{e:?}"
    );
}

#[test]
fn the_checkout_is_compared_by_sqlite() {
    let (_d, mut store) = fresh("memb-native-kill-log-checkout");
    let mut p = Counting::at(STAMP, 0);
    record(
        &mut store,
        &mut p,
        "checkout_registered",
        PyValue::Bool(true),
    );
    record(&mut store, &mut p, "checkout_moved", PyValue::int(1));
    record(&mut store, &mut p, "checkout_retired", s("1"));
    record(
        &mut store,
        &mut p,
        "checkout_rekeyed",
        PyValue::List(vec![s("a")]),
    );
    let seqs = |checkout: PyValue| -> Vec<i64> {
        m::membership_log(store.connection(), None, 0, Some(&checkout))
            .expect("log")
            .iter()
            .map(|r| r.seq)
            .collect()
    };
    // JSON true reads as the integer 1; a REAL 1.0 equals it; text does
    // not.
    assert_eq!(seqs(PyValue::Bool(true)), [2, 3]);
    assert_eq!(seqs(PyValue::Float(1.0)), [2, 3]);
    assert_eq!(seqs(s("1")), [4]);
    assert_eq!(seqs(s("true")), Vec::<i64>::new());
    // An array reads back as its JSON text.
    assert_eq!(seqs(s(r#"["a"]"#)), [5]);
    // A text holding a surrogate cannot be bound (sqlite3's
    // UnicodeEncodeError, read under 3.13.13).
    let e = m::membership_log(
        store.connection(),
        None,
        0,
        Some(&PyValue::Str(vec![0x43, 0xDC80, 0xDC81, 0x44])),
    )
    .expect_err("unencodable");
    assert!(
        matches!(&e, WriteError::Refused { class: "UnicodeEncodeError", text: Some(t) }
            if t == "'utf-8' codec can't encode characters in position 1-2: surrogates not allowed"),
        "{e:?}"
    );
    let e = m::membership_log(store.connection(), None, 0, Some(&sur())).expect_err("one");
    assert!(
        matches!(&e, WriteError::Refused { class: "UnicodeEncodeError", text: Some(t) }
            if t == "'utf-8' codec can't encode character '\\udc80' in position 1: surrogates not allowed"),
        "{e:?}"
    );
}

/// `Counting`, with the reissue's token and the order of the draws.
struct Drawn {
    inner: Counting,
    tokens: Vec<String>,
    order: Vec<&'static str>,
}

impl Provider for Drawn {
    fn store_id(&mut self) -> io::Result<String> {
        self.inner.store_id()
    }
    fn key(&mut self) -> io::Result<Key> {
        self.inner.key()
    }
    fn membership_store_id(&mut self) -> io::Result<String> {
        self.order.push("token");
        Ok(self.tokens.remove(0))
    }
    fn now(&mut self) -> io::Result<DateTime> {
        self.order.push("log");
        self.inner.now()
    }
    fn engine_version(&self) -> String {
        self.inner.engine_version()
    }
    fn ulid(&mut self) -> io::Result<String> {
        self.inner.ulid()
    }
}

fn drawn(tokens: &[&str]) -> Drawn {
    Drawn {
        inner: Counting::at(STAMP, 0),
        tokens: tokens.iter().map(|t| (*t).to_owned()).collect(),
        order: Vec::new(),
    }
}

fn store_id(store: &WritableStore) -> Vec<String> {
    texts(
        store,
        "SELECT value FROM meta WHERE key = 'membership_store_id'",
    )
}

#[test]
fn the_reissue_draws_its_token_before_the_transaction_begins() {
    let (_d, mut store) = fresh("memb-native-kill-reissue");
    let mut p = drawn(&["a".repeat(32).as_str(), "b".repeat(32).as_str()]);
    let old = store_id(&store);
    let ((was, now), _) = store
        .reissue_membership_store_id(&mut p, "R1", "import", Some("s1"))
        .expect("reissue");
    assert_eq!([was], old.as_slice());
    assert_eq!(now, "a".repeat(32));
    // The token before any clock read of the transaction.
    assert_eq!(p.order[0], "token");
    assert!(p.order.len() > 1 && p.order[1..].iter().all(|o| *o == "log"));
    assert_eq!(tail(&store, 1), ["store_forked"]);
    assert_eq!(store_id(&store), std::slice::from_ref(&now));
    // A store whose transaction cannot begin (held to a later writer):
    // the token is drawn all the same, nothing written.
    store
        .connection()
        .execute(
            "UPDATE meta SET value = '99' WHERE key = 'min_writer_revision'",
            [],
        )
        .expect("meta");
    let before = kinds(&store).len();
    let e = store
        .reissue_membership_store_id(&mut p, "R2", "s", None)
        .expect_err("a later writer");
    assert!(matches!(e, WriteError::LaterWriter { .. }), "{e:?}");
    assert!(p.tokens.is_empty(), "the token was not drawn");
    assert_eq!(kinds(&store).len(), before);
    assert_eq!(store_id(&store), [now]);
}

#[test]
fn a_reissue_without_an_id_leaves_meta_and_the_log_as_they_were() {
    let (_d, mut store) = fresh("memb-native-kill-reissue-none");
    let mut p = drawn(&["c".repeat(32).as_str()]);
    store
        .connection()
        .execute("DELETE FROM meta WHERE key = 'membership_store_id'", [])
        .expect("meta");
    let before = kinds(&store).len();
    let e = store
        .reissue_membership_store_id(&mut p, "R1", "s", None)
        .expect_err("no id");
    assert!(
        matches!(
            &e,
            WriteError::Refused {
                class: "RuntimeError",
                ..
            }
        ),
        "{e:?}"
    );
    assert!(p.tokens.is_empty());
    assert_eq!(kinds(&store).len(), before);
    assert!(store_id(&store).is_empty());
    // Inside a batch, the token the caller drew; the batch's other write
    // stays when the reissue's own failure is caught.
    let mut q = Counting::at(STAMP, 0);
    let path = store.path().to_owned();
    let (caught, _) = store
        .batch(&mut q, None, |tx| {
            tx.put_membership(&cps("machine"), &machine(s("M1"), s("x")), None)?;
            let caught = tx
                .reissue_membership_store_id(&path, "d", "R2", "s", None)
                .is_err();
            Ok::<_, WriteError>(caught)
        })
        .expect("batch");
    assert!(caught);
    assert_eq!(tail(&store, 1), ["machine_put"]);
}

fn nested(n: usize, leaf: PyValue) -> PyValue {
    let mut v = leaf;
    for _ in 0..n {
        v = PyValue::List(vec![v]);
    }
    v
}

fn is_recursion(e: &WriteError) -> bool {
    matches!(
        e,
        WriteError::Refused {
            class: "RecursionError",
            ..
        }
    )
}

#[test]
fn the_surrogate_walk_raises_recursion_error_where_the_reference_does() {
    // The deepest `facts_json` each put answers for, read under CPython
    // 3.13.13 with `put_membership` called from a module's top level; one
    // level deeper raises RecursionError and writes nothing. Where an int
    // is innermost (the int, the dict's value) the answer is a process
    // that has checked an int against `Mapping` before; a fresh one's
    // first check costs more frames (494 lists around an int; see the
    // README's depth budget).
    let (_d, mut store) = fresh("memb-native-kill-depth");
    let mut p = Counting::at(STAMP, 0);
    type Make = fn(usize) -> PyValue;
    let cases: [(&str, Make, usize); 5] = [
        ("a str", |n| nested(n, s("x")), 497),
        ("an int", |n| nested(n, PyValue::int(5)), 497),
        (
            "an empty list",
            |n| nested(n - 1, PyValue::List(vec![])),
            498,
        ),
        ("a surrogate", |n| nested(n, sur()), 497),
        (
            "a dict's key",
            |n| nested(n - 1, PyValue::Dict(vec![(s("k"), PyValue::int(1))])),
            497,
        ),
    ];
    for (what, make, deepest) in cases {
        for (n, answers) in [(deepest, true), (deepest + 1, false)] {
            let row = machine(s(&format!("M{n}")), make(n));
            let before = kinds(&store).len();
            let got = store.put_membership(&mut p, &cps("machine"), &row, None);
            match got {
                Ok(_) => assert!(answers, "{what} {n}"),
                Err(e) => {
                    assert!(!answers && is_recursion(&e), "{what} {n}: {e:?}");
                    assert_eq!(kinds(&store).len(), before, "{what} {n}");
                }
            }
        }
    }
    // A surrogate in an earlier column ends the put's own walk; `legacy_row`
    // walks the later column one frame shallower.
    for (n, answers) in [(497, true), (498, false)] {
        let row = vec![
            (s("id"), s(&format!("L{n}"))),
            (s("predecessor"), sur()),
            (s("facts_json"), nested(n, s("x"))),
            (s("minted"), s("t")),
        ];
        let got = store.put_membership(&mut p, &cps("machine"), &row, None);
        match got {
            Ok((written, _)) => {
                assert!(answers, "{n}");
                assert_eq!(written.kind, "native_member_put");
            }
            Err(e) => assert!(!answers && is_recursion(&e), "{n}: {e:?}"),
        }
    }
    // A delete's key holds text alone: no depth to reach.
    let key = vec![(s("id"), sur())];
    let (written, _) = store
        .delete_membership(&mut p, &cps("machine"), &key, None)
        .expect("delete");
    assert_eq!(written.kind, "native_member_delete");
}

#[test]
fn a_store_held_to_older_writers_refuses_before_the_legacy_put() {
    let (_d, mut store) = fresh("memb-native-kill-revision-legacy");
    store
        .connection()
        .execute(
            "UPDATE meta SET value = '4' WHERE key = 'min_writer_revision'",
            [],
        )
        .expect("meta");
    let before = kinds(&store).len();
    // Its legacy projection exists: StoreVersionError before the legacy
    // put reads the log's clock.
    let mut p = drawn(&[]);
    let row = machine(s("M001"), PyValue::Dict(vec![(s("os"), sur())]));
    let e = store
        .put_membership(&mut p, &cps("machine"), &row, None)
        .expect_err("refused");
    assert!(
        matches!(
            &e,
            WriteError::Refused {
                class: "StoreVersionError",
                ..
            }
        ),
        "{e:?}"
    );
    assert!(!p.order.contains(&"log"), "{:?}", p.order);
    // Its legacy projection breaks NOT NULL (`minted` None):
    // StoreVersionError all the same, not IntegrityError.
    let mut row = machine(s("M002"), s("x"));
    row[3].1 = sur();
    let e = store
        .put_membership(&mut p, &cps("machine"), &row, None)
        .expect_err("refused");
    assert!(
        matches!(
            &e,
            WriteError::Refused {
                class: "StoreVersionError",
                ..
            }
        ),
        "{e:?}"
    );
    assert_eq!(kinds(&store).len(), before);
    assert!(texts(&store, "SELECT id FROM machines").is_empty());
}

#[test]
fn the_log_holds_no_row_of_a_record_or_quarantine_kind() {
    let (_d, store) = fresh("memb-native-kill-log-quarantine");
    let conn = store.connection();
    let mut others = Vec::new();
    for table in &MUTATION_TABLES[..7] {
        for op in ["put", "delete"] {
            let kind = format!("{table}_{op}");
            conn.execute(
                "INSERT INTO log(ts, session, kind, payload, prev_mac, mac) \
                 VALUES ('t', NULL, ?, '{}', 'p', 'm')",
                [&kind],
            )
            .expect("insert");
            others.push(s(&kind));
        }
    }
    assert!(others.contains(&s("quarantine_put")));
    // Read under 3.13.13: no row, with `kinds` None and with them named.
    assert!(m::membership_log(conn, None, 0, None)
        .expect("log")
        .is_empty());
    assert!(m::membership_log(conn, Some(&others), 0, None)
        .expect("named")
        .is_empty());
}

#[test]
fn no_membership_kind_reads_nothing_and_binds_no_checkout() {
    let (_d, mut store) = fresh("memb-native-kill-log-none");
    let mut p = Counting::at(STAMP, 0);
    record(&mut store, &mut p, "checkout_moved", s("C1"));
    let conn = store.connection();
    let list = PyValue::List(vec![s("C1")]);
    let unencodable = PyValue::Str(vec![0x43, 0xDC80]);
    // Read under 3.13.13: the generator returns before binding anything.
    assert!(m::membership_log(conn, Some(&[]), 0, Some(&list))
        .expect("no kind")
        .is_empty());
    assert!(
        m::membership_log(conn, Some(&[s("nope")]), 0, Some(&unencodable))
            .expect("no membership kind")
            .is_empty()
    );
    // With a membership kind the same checkouts are bound, and refused.
    let kinds = [s("checkout_moved")];
    let e = m::membership_log(conn, Some(&kinds), 0, Some(&list)).expect_err("a list");
    assert!(
        matches!(
            &e,
            WriteError::Refused {
                class: "ProgrammingError",
                ..
            }
        ),
        "{e:?}"
    );
    let e = m::membership_log(conn, Some(&kinds), 0, Some(&unencodable)).expect_err("unencodable");
    assert!(
        matches!(
            &e,
            WriteError::Refused {
                class: "UnicodeEncodeError",
                ..
            }
        ),
        "{e:?}"
    );
}

#[test]
fn a_log_reader_that_stops_early_never_reads_a_later_row() {
    let (_d, mut store) = fresh("memb-native-kill-log-lazy");
    let mut p = Counting::at(STAMP, 0);
    store
        .record_membership(
            &mut p,
            &cps("project_minted"),
            &[(s("project"), s("P1"))],
            None,
        )
        .expect("record");
    // A later row whose payload is not UTF-8.
    store
        .connection()
        .execute(
            "INSERT INTO log(ts, session, kind, payload, prev_mac, mac) \
             VALUES ('t', NULL, 'project_minted', CAST(x'7bff7d' AS TEXT), 'p', 'm')",
            [],
        )
        .expect("insert");
    let conn = store.connection();
    let kinds = [s("project_minted")];
    // Read under 3.13.13: the first `next` yields seq 2; only a read on
    // to the later row raises OperationalError.
    let first = m::membership_log_each(conn, Some(&kinds), 0, None, |row| {
        ControlFlow::Break((row.seq, row.kind))
    })
    .expect("the first row");
    assert_eq!(first, ControlFlow::Break((2, "project_minted".to_owned())));
    let mut seen = Vec::new();
    let e = m::membership_log_each(conn, Some(&kinds), 0, None, |row| {
        seen.push(row.seq);
        ControlFlow::<()>::Continue(())
    })
    .expect_err("the later row");
    assert_eq!(seen, [2]);
    let operational = |e: &WriteError| {
        matches!(
            e,
            WriteError::Refused {
                class: "OperationalError",
                ..
            }
        )
    };
    assert!(operational(&e), "{e:?}");
    let e = m::membership_log(conn, Some(&kinds), 0, None).expect_err("a full read");
    assert!(operational(&e), "{e:?}");
}

/// `Counting`, whose next clock read fails once `stop` is set.
struct Stopping {
    inner: Counting,
    stop: Rc<Cell<bool>>,
}

impl Provider for Stopping {
    fn store_id(&mut self) -> io::Result<String> {
        self.inner.store_id()
    }
    fn key(&mut self) -> io::Result<Key> {
        self.inner.key()
    }
    fn membership_store_id(&mut self) -> io::Result<String> {
        self.inner.membership_store_id()
    }
    fn now(&mut self) -> io::Result<DateTime> {
        if self.stop.replace(false) {
            return Err(io::Error::other("the clock stopped"));
        }
        self.inner.now()
    }
    fn engine_version(&self) -> String {
        self.inner.engine_version()
    }
    fn ulid(&mut self) -> io::Result<String> {
        self.inner.ulid()
    }
}

#[test]
fn a_reissue_whose_store_forked_row_fails_takes_back_its_meta_update() {
    // Read under 3.13.13 with the log's clock failing at the
    // `store_forked` append inside a batch that catches it: meta keeps the
    // old id (the nested transaction's savepoint), the batch's put stays.
    let (_d, mut store) = fresh("memb-native-kill-reissue-savepoint");
    let old = store_id(&store);
    let stop = Rc::new(Cell::new(false));
    let mut p = Stopping {
        inner: Counting::at(STAMP, 0),
        stop: Rc::clone(&stop),
    };
    let path = store.path().to_owned();
    let (inside, _) = store
        .batch(&mut p, None, |tx| {
            tx.put_membership(&cps("machine"), &machine(s("M1"), s("x")), None)?;
            stop.set(true);
            let e = tx
                .reissue_membership_store_id(&path, &"f".repeat(32), "R1", "s", None)
                .expect_err("the clock stopped");
            assert!(matches!(e, WriteError::Clock(_)), "{e:?}");
            let inside: String = tx
                .connection()
                .query_row(
                    "SELECT value FROM meta WHERE key = 'membership_store_id'",
                    [],
                    |r| r.get(0),
                )
                .expect("meta");
            Ok::<_, WriteError>(inside)
        })
        .expect("batch");
    assert_eq!([inside], old.as_slice());
    assert_eq!(store_id(&store), old);
    assert_eq!(tail(&store, 1), ["machine_put"]);
}
