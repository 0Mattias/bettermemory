//! Hostile inputs and states for chunked runs: typed errors or the
//! reference's answer, the chunks committed before a failure kept and
//! nothing else, never a panic. The callers take no nested value as an
//! argument (scopes are text, the age a number of microseconds, the time a
//! datetime), so the deep values come in through stored rows: a removed
//! record's `links_json` and an active record's `scopes_json` nested 9,998
//! and 100,000 deep, each run on a thread with a 512 KiB stack.

mod common;

use std::io;
use std::path::Path;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::log::Key;
use bettermemory_core::store::create::{self, Provider};
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::write::WriteError;
use bettermemory_core::unicode;
use common::bulk::put_records;
use common::created::{fixed, fixture};
use common::opened;
use common::stores::scratch;
use common::written::{self, instant, ulid};
use serde_json::json;

struct Steady {
    at: DateTime,
    mono: f64,
}

impl Provider for Steady {
    fn store_id(&mut self) -> io::Result<String> {
        Err(io::Error::other("none"))
    }
    fn key(&mut self) -> io::Result<Key> {
        Err(io::Error::other("none"))
    }
    fn membership_store_id(&mut self) -> io::Result<String> {
        Err(io::Error::other("none"))
    }
    fn now(&mut self) -> io::Result<DateTime> {
        Ok(self.at)
    }
    fn engine_version(&self) -> String {
        opened::ENGINE.to_owned()
    }
    fn monotonic(&mut self) -> io::Result<f64> {
        self.mono += 1.0 / 32.0;
        Ok(self.mono)
    }
    fn pause(&mut self, _seconds: f64) {}
}

fn steady() -> Steady {
    Steady {
        at: instant("2001-02-03T06:00:00.000001Z"),
        mono: 0.0,
    }
}

fn fresh(root: &Path, active: u64, removed: u64) -> WritableStore {
    let c = root.join("c");
    create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    let mut store = open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("open");
    let rec = |n: u64| {
        json!({
            "id": ulid(n, "01J"), "created": "2001-02-03T04:05:06.000000Z",
            "updated": "2001-02-03T04:05:06.000000Z", "scopes": ["project:a"],
            "confidence": "high", "source": "explicit-statement",
            "body": format!("record {n}"),
        })
    };
    let spec = json!({
        "active": (1..=active).map(rec).collect::<Vec<_>>(),
        "removed": (100..100 + removed)
            .map(|n| json!([rec(n), "2001-01-01T00:00:00+00:00"]))
            .collect::<Vec<_>>(),
    });
    put_records(&mut store, &spec);
    store
}

fn log_len(store: &WritableStore) -> i64 {
    store
        .connection()
        .query_row("SELECT count(*) FROM log", [], |r| r.get(0))
        .expect("count")
}

fn on_small_stack(f: impl FnOnce() + Send + 'static) {
    std::thread::Builder::new()
        .stack_size(512 * 1024)
        .spawn(f)
        .expect("thread")
        .join()
        .expect("no panic on a 512 KiB stack");
}

#[test]
fn deep_stored_values_are_typed_errors_on_a_small_stack() {
    for depth in [9_998usize, 100_000] {
        on_small_stack(move || {
            let dir = scratch("bulk-robust-deep");
            let mut store = fresh(&dir.0, 5, 2);
            let deep = format!("{}{}", "[".repeat(depth), "]".repeat(depth));
            // A removed record's links: read by the rename of removed ones.
            store
                .connection()
                .execute(
                    "UPDATE tombstones SET links_json = ? WHERE id = ?",
                    rusqlite::params![deep, ulid(100, "01J")],
                )
                .expect("links");
            let before = log_len(&store);
            let got = store.rename_scope(
                &mut steady(),
                &unicode::cps("project:a"),
                &unicode::cps("project:b"),
                true,
                None,
            );
            // The active chunks commit; the removed record raises or is
            // renamed with its links as read: either way no panic.
            let after = log_len(&store);
            assert!(
                after >= before + 5,
                "the active chunks: {after} after {before}"
            );
            if let Err(e) = &got {
                assert!(!e.class().is_empty());
            }
            // An active record's scopes: read by the listing, before any lock.
            store
                .connection()
                .execute(
                    "UPDATE memories SET scopes_json = ? WHERE id = ?",
                    rusqlite::params![deep, ulid(1, "01J")],
                )
                .expect("scopes");
            let before = log_len(&store);
            let got = store.rename_scope(
                &mut steady(),
                &unicode::cps("project:b"),
                &unicode::cps("project:c"),
                true,
                None,
            );
            assert!(got.is_err(), "a record whose scopes do not read");
            assert_eq!(
                log_len(&store),
                before,
                "the listing failed before any chunk"
            );
            // The prune reads every tombstone (the deep links among them
            // are not parsed): it prunes the two.
            let pruned = store
                .prune_tombstones(&mut steady(), 0, None, None)
                .expect("the prune")
                .value;
            assert_eq!(pruned.len(), 2);
        });
    }
}

#[test]
fn a_run_of_a_hundred_thousand_items() {
    let dir = scratch("bulk-robust-long");
    let mut store = fresh(&dir.0, 0, 0);
    let items: Vec<u32> = (0..100_000).collect();
    let mut seen = 0u32;
    let chunks = store
        .in_chunks(&mut steady(), None, &items, |tx, i: &u32| {
            assert_eq!(*i, seen);
            seen += 1;
            tx.connection()
                .query_row("SELECT 1", [], |_| Ok(()))
                .map_err(WriteError::from)
        })
        .expect("the run");
    assert_eq!(seen, 100_000);
    // Four items to a chunk under this clock; no chunk wrote a row.
    assert_eq!(chunks.len(), 25_000);
    assert!(chunks.iter().all(|c| c.rows.is_empty() && c.head.is_none()));
    // An item that fails part way: the chunks before stay, the rest is not tried.
    let mut tried = 0u32;
    let failed = store.in_chunks(&mut steady(), None, &items, |_tx, i: &u32| {
        tried += 1;
        if *i == 50_001 {
            return Err(WriteError::NotAnObject);
        }
        Ok(())
    });
    assert!(failed.is_err());
    assert_eq!(tried, 50_002);
    // Inside an open transaction: every item into it, no clock read.
    let mut p = steady();
    let mut tx = store.begin(&mut p).expect("begin");
    let mut count = 0;
    tx.in_chunks(&items, |_tx, _i: &u32| {
        count += 1;
        Ok::<(), WriteError>(())
    })
    .expect("joined");
    assert_eq!(count, 100_000);
    tx.commit().expect("commit");
}

#[test]
fn hostile_scopes_and_ages() {
    let dir = scratch("bulk-robust-scopes");
    let mut store = fresh(&dir.0, 6, 3);
    let long = "a".repeat(1 << 20);
    let mut hostile: Vec<Vec<u32>> = vec![
        Vec::new(),
        vec![0],
        vec![0xD800],
        vec![0xDCFF, 0x61],
        unicode::cps("project:a\n"),
        unicode::cps(&long),
        unicode::cps("PROJECT:A"),
        unicode::cps("project:a project:b"),
    ];
    hostile.push(unicode::cps("project:a"));
    for old in &hostile {
        for new in &hostile {
            let before = log_len(&store);
            match store.rename_scope(&mut steady(), old, new, true, Some("s")) {
                Ok(b) => {
                    // A rename that changed nothing wrote nothing.
                    if b.value.active.is_empty() && b.value.tombstoned.is_empty() {
                        assert_eq!(log_len(&store), before);
                    }
                }
                Err(e) => assert!(!e.class().is_empty()),
            }
        }
    }
    // Ages: none, negative, out of the calendar either way.
    for (older, now) in [
        (0i128, None),
        (-86_400_000_000i128, None),
        (i128::from(i64::MAX), None),
        (-i128::from(i64::MAX), Some(instant("2001-01-01T00:00:00Z"))),
        (
            0,
            Some(DateTime::from_isoformat(&unicode::cps("2001-01-01T00:00:00")).expect("naive")),
        ),
    ] {
        match store.prune_tombstones(&mut steady(), older, now, None) {
            Ok(_) => {}
            Err(e) => assert!(
                ["OverflowError", "TypeError"].contains(&e.class().as_str()),
                "{}",
                e.class()
            ),
        }
    }
    store.close().expect("close");
    let (status, _) = written::native_verify(&dir.0.join("c"), &common::stores::store_id());
    assert_eq!(status, "ok");
}

/// `put_plain` (the put `rename_scope` and the batch driver make) answers
/// as the writers' `put_memory` does for every state a rename can meet: the
/// same error, the same rows. The one difference is the reference's own: a
/// blob in a column `str()` reads (`provenance`) is spelled `b'...'` here,
/// where `put_memory` refuses it as not ported.
#[test]
fn put_plain_answers_as_put_memory() {
    use bettermemory_core::store::bulk::put_plain;
    use bettermemory_core::store::writers::Put;
    let rec = json!({
        "id": ulid(1, "01J"), "created": "2001-02-03T04:05:06.000000Z",
        "updated": "2001-02-03T04:05:06.000000Z", "scopes": ["project:b"],
        "confidence": "high", "source": "explicit-statement", "body": "record 1",
    });
    let memory = common::bulk::memory(&rec);
    let id = ulid(1, "01J");
    let states: Vec<(&str, String)> = vec![
        ("plain", String::new()),
        (
            "imported",
            "UPDATE memories SET provenance = 'imported'".to_owned(),
        ),
        (
            "empty label",
            "UPDATE memories SET provenance = ''".to_owned(),
        ),
        (
            "unknown label",
            "UPDATE memories SET provenance = 'x'".to_owned(),
        ),
        (
            "text filename",
            "UPDATE memories SET filename = 'n.md'".to_owned(),
        ),
        (
            "blob filename",
            "UPDATE memories SET filename = X'61'".to_owned(),
        ),
        (
            "integer filename",
            "UPDATE memories SET filename = 7".to_owned(),
        ),
        (
            "removed",
            format!(
                "INSERT INTO tombstones(id, created, updated, confidence, source, body, \
                 scopes_json, removed, removed_reason, provenance) SELECT id, created, \
                 updated, confidence, source, body, scopes_json, \
                 '2001-01-01T00:00:00.000000Z', 'twice', provenance FROM memories \
                 WHERE id = '{id}'"
            ),
        ),
        ("no row", "DELETE FROM memories".to_owned()),
        (
            "no filename column",
            "ALTER TABLE memories DROP COLUMN filename".to_owned(),
        ),
    ];
    for (label, sql) in states {
        let mut answers = Vec::new();
        for plain in [true, false] {
            let dir = scratch("bulk-plain");
            let mut store = fresh(&dir.0, 1, 0);
            if !sql.is_empty() {
                store.connection().execute_batch(&sql).expect("sql");
            }
            let at = [instant("2001-02-03T06:00:00.000001Z")];
            let mut p = written::clock(&at);
            let before = log_len(&store);
            let got = if plain {
                store
                    .batch(&mut p, None, |tx| put_plain(tx, &memory, Some("s1")))
                    .map(drop)
            } else {
                let put = Put {
                    session: Some("s1"),
                    ..Put::default()
                };
                store.put_memory(&mut p, &memory, put).map(drop)
            };
            answers.push((format!("{got:?}"), log_len(&store) - before));
        }
        assert_eq!(answers[0], answers[1], "{label}");
    }
    // The blob label: the reference's `str(bytes)`, which the apply refuses.
    let dir = scratch("bulk-plain");
    let mut store = fresh(&dir.0, 1, 0);
    store
        .connection()
        .execute_batch("UPDATE memories SET provenance = X'61'")
        .expect("sql");
    let mut p = written::clock(&[instant("2001-02-03T06:00:00.000001Z")]);
    let got = store.batch(&mut p, None, |tx| put_plain(tx, &memory, Some("s1")));
    let e = got.expect_err("refused");
    assert_eq!(e.class(), "ValueError");
    assert_eq!(
        e.text().and_then(|t| unicode::to_string(&t)).as_deref(),
        Some("unknown provenance \"b'a'\"")
    );
}

/// A label's bytes no codec reads, `X'ff'`: `str(bytes)` is `b'\xff'`, and
/// the apply's refusal quotes it with `repr` (the reference's text, read
/// from it at 5522b983; not in `store_bulk.jsonl`, whose host-fact check
/// takes its four backslashes in a row for a UNC path). Caught into
/// `failed` for an active record, raised for a removed one.
#[test]
fn an_undecodable_blob_label() {
    let want = "unknown provenance \"b'\\\\xff'\"";
    let dir = scratch("bulk-label");
    let mut store = fresh(&dir.0, 2, 0);
    store
        .connection()
        .execute_batch(&format!(
            "UPDATE memories SET provenance = X'ff' WHERE id = '{}'",
            ulid(1, "01J")
        ))
        .expect("sql");
    let got = store
        .rename_scope(
            &mut steady(),
            &unicode::cps("project:a"),
            &unicode::cps("project:b"),
            true,
            Some("s1"),
        )
        .expect("renamed");
    assert_eq!(got.value.active, vec![unicode::cps(&ulid(2, "01J"))]);
    assert_eq!(
        got.value.failed,
        vec![(unicode::cps(&ulid(1, "01J")), Some(unicode::cps(want)))]
    );
    let dir = scratch("bulk-label");
    let mut store = fresh(&dir.0, 0, 1);
    store
        .connection()
        .execute_batch("UPDATE tombstones SET provenance = X'ff'")
        .expect("sql");
    let before = log_len(&store);
    let e = store
        .rename_scope(
            &mut steady(),
            &unicode::cps("project:a"),
            &unicode::cps("project:b"),
            true,
            Some("s1"),
        )
        .expect_err("raised");
    assert_eq!(e.class(), "ValueError");
    assert_eq!(
        e.text().and_then(|t| unicode::to_string(&t)).as_deref(),
        Some(want)
    );
    assert_eq!(log_len(&store), before);
}
