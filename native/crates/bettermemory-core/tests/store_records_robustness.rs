//! Hostile arguments and hostile states for the record writers
//! (`put_memory`, `tombstone`, `delete_tombstone`): each a typed error
//! with nothing written (the log, the record tables and the marks as they
//! were), or written whole; never a panic; the native check ok after.

mod common;

use std::path::Path;

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::Memory;
use bettermemory_core::store::create;
use bettermemory_core::store::home::{Answer, Home};
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::writers::{Put, RecordWriteError, WriterNotPorted};
use common::created::{fixed, fixture};
use common::opened;
use common::stores::scratch;
use common::written::{self, instant, ulid};

fn fresh(root: &Path) -> WritableStore {
    let c = root.join("c");
    create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("open")
}

fn clock() -> create::Fixed {
    written::clock(&[instant("2001-02-03T11:00:00.000001Z")])
}

fn snapshot(store: &WritableStore) -> (i64, String) {
    let conn = store.connection();
    let n = conn
        .query_row("SELECT count(*) FROM log", [], |r| r.get(0))
        .expect("count");
    let mut text = String::new();
    for sql in [
        "SELECT id || ' ' || body || ' ' || provenance FROM memories ORDER BY id",
        "SELECT id FROM tombstones ORDER BY id",
        "SELECT record_id || ' ' || via FROM returned ORDER BY record_id",
    ] {
        let rows: Vec<String> = conn
            .prepare(sql)
            .and_then(|mut s| {
                s.query_map([], |r| r.get::<_, Option<String>>(0))?
                    .map(|v| v.map(Option::unwrap_or_default))
                    .collect::<rusqlite::Result<_>>()
            })
            .unwrap_or_else(|e| vec![format!("({e})")]);
        text.push_str(&rows.join(";"));
        text.push('|');
    }
    (n, text)
}

fn record(n: u64, body: &str) -> Memory {
    let p = written::memory_payload(&ulid(n, "01J"), body, &["global"], &[]);
    Memory::from_payload(p.get("memory").expect("memory")).expect("valid")
}

fn verified(root: &Path) {
    let (status, _) = written::native_verify(&root.join("c"), &common::stores::store_id());
    assert_eq!(status, "ok");
}

#[test]
fn hostile_arguments_are_typed_errors_with_nothing_written() {
    let dir = scratch("records-robust-args");
    let mut store = fresh(&dir.0);
    store
        .put_memory(&mut clock(), &record(1, "one"), Put::default())
        .expect("put");
    let before = snapshot(&store);
    // Tuples nested deeper than `repr` goes, built without recursion.
    let mut deep = PyValue::int(1);
    for _ in 0..12_000 {
        deep = PyValue::Tuple(vec![deep]);
    }
    let huge_text = PyValue::str(&"x".repeat(1 << 20));
    let tuple_list = PyValue::Tuple(vec![PyValue::str("local"), PyValue::List(vec![])]);
    let provenances = [
        PyValue::str(""),
        PyValue::str("\u{0}"),
        huge_text.clone(),
        PyValue::Float(f64::NAN),
        PyValue::int(-1),
        tuple_list,
        deep,
        PyValue::Dict(vec![]),
    ];
    let mut kinds = Vec::new();
    for p in &provenances {
        let put = Put {
            provenance: Some(p),
            ..Put::default()
        };
        let e = store
            .put_memory(&mut clock(), &record(2, "two"), put)
            .expect_err("refused");
        kinds.push(e.class());
        assert_eq!(snapshot(&store), before, "{}", e.class());
    }
    eprintln!("provenance refusals: {kinds:?}");
    let bad_home = Home {
        project: PyValue::int(1),
        checkout: huge_text.clone(),
        rule: PyValue::str("r"),
    };
    let bad_answer = Answer {
        probability: huge_text.clone(),
        source: PyValue::str("s"),
        answered_by: PyValue::str("a"),
    };
    for (home, standing) in [(Some(&bad_home), None), (None, Some(&bad_answer))] {
        let put = Put {
            home,
            standing,
            ..Put::default()
        };
        let e = store
            .put_memory(&mut clock(), &record(2, "two"), put)
            .expect_err("refused");
        assert_eq!(e.class(), "ValueError");
        assert_eq!(snapshot(&store), before);
    }
    let long_id = "0".repeat(1 << 20);
    for id in ["", "\u{0}", "01J\u{0}", "' OR 1=1 --", long_id.as_str()] {
        let e = store
            .tombstone(&mut clock(), id, "why", None, None)
            .expect_err("missing");
        assert_eq!(e.class(), "NotFoundError");
        let e = store
            .delete_tombstone(&mut clock(), id, None)
            .expect_err("missing");
        assert_eq!(e.class(), "NotFoundError");
        assert_eq!(snapshot(&store), before);
    }
    // An enormous body and reason are written whole.
    let big = "word ".repeat(400_000);
    store
        .put_memory(&mut clock(), &record(3, &big), Put::default())
        .expect("a big body");
    store
        .tombstone(&mut clock(), &ulid(3, "01J"), &big, Some(&big), None)
        .expect("a big reason");
    // A record whose target map names no listed path: record_payload's
    // ValueError, nothing written.
    let mut odd = written::memory_payload(&ulid(4, "01J"), "four", &["global"], &[]);
    if let PyValue::Dict(items) = &mut odd {
        for (k, v) in items.iter_mut() {
            if *k == PyValue::str("memory") {
                if let PyValue::Dict(m) = v {
                    m.push((
                        PyValue::str("verified_path_targets"),
                        written::payload("{\"not/listed.py\": null}"),
                    ));
                }
            }
        }
    }
    if let Ok(m) = Memory::from_payload(odd.get("memory").expect("memory")) {
        let at = snapshot(&store);
        match store.put_memory(&mut clock(), &m, Put::default()) {
            Ok(_) => {}
            Err(e) => {
                eprintln!("an unlisted target: {}", e.class());
                assert_eq!(snapshot(&store), at);
            }
        }
    }
    drop(store);
    verified(&dir.0);
}

#[test]
fn altered_tables_are_typed_errors_with_nothing_written() {
    let dir = scratch("records-robust-tables");
    let mut store = fresh(&dir.0);
    for n in 1..=4 {
        store
            .put_memory(&mut clock(), &record(n, "r"), Put::default())
            .expect("put");
    }
    let alter: [(&str, u64); 4] = [
        ("UPDATE memories SET links_json = '{bad' WHERE id = ?1", 1),
        (
            "UPDATE memories SET corroborations = 'many' WHERE id = ?1",
            2,
        ),
        ("UPDATE memories SET provenance = x'00' WHERE id = ?1", 3),
        (
            "UPDATE memories SET created = 'not a time' WHERE id = ?1",
            4,
        ),
    ];
    for (sql, n) in alter {
        let id = ulid(n, "01J");
        store
            .connection()
            .execute(sql, [&id])
            .expect("alter the table");
        let before = snapshot(&store);
        match store.tombstone(&mut clock(), &id, "why", None, None) {
            Ok(_) => eprintln!("{sql}: removed"),
            Err(e) => {
                eprintln!("{sql}: {}", e.class());
                assert_eq!(snapshot(&store), before, "{sql}");
            }
        }
    }
    // A blob label where a re-put keeps the stored one: not ported.
    let id = ulid(3, "01J");
    if store
        .connection()
        .query_row("SELECT 1 FROM memories WHERE id = ?1", [&id], |_| Ok(()))
        .is_ok()
    {
        let before = snapshot(&store);
        let e = store
            .put_memory(&mut clock(), &record(3, "again"), Put::default())
            .expect_err("a blob label");
        assert!(matches!(
            e,
            RecordWriteError::NotPorted(WriterNotPorted::Blob(_))
        ));
        assert_eq!(snapshot(&store), before);
    }
    // A table renamed away: a SQLite error, nothing written.
    store
        .connection()
        .execute_batch("ALTER TABLE tombstones RENAME TO gone")
        .expect("rename");
    let before = snapshot(&store);
    for e in [
        store
            .put_memory(&mut clock(), &record(9, "nine"), Put::default())
            .expect_err("no tombstones table"),
        store
            .delete_tombstone(&mut clock(), &ulid(9, "01J"), None)
            .expect_err("no tombstones table"),
    ] {
        assert_eq!(e.class(), "native:sqlite");
    }
    assert_eq!(snapshot(&store), before);
}

#[test]
fn a_hand_built_record_past_the_target_cap_is_the_reference_s_value_error() {
    let dir = scratch("records-robust-cap");
    let mut store = fresh(&dir.0);
    let before = snapshot(&store);
    let mut m = record(5, "five");
    let targets: Vec<_> = (0..65)
        .map(|i| (bettermemory_core::unicode::cps(&format!("p{i}")), None))
        .collect();
    m.fields.verified_absent_path_targets = Some(targets);
    let e = store
        .put_memory(&mut clock(), &m, Put::default())
        .expect_err("past the cap");
    assert_eq!(e.class(), "ValueError");
    // `models.validate_path_targets` (models.py:1173).
    assert_eq!(
        e.text().map(|t| bettermemory_core::json::utf8_text(&t)),
        Some("verified_absent_path_targets capped at 64 entries (got 65)".to_owned())
    );
    assert_eq!(snapshot(&store), before);
}
