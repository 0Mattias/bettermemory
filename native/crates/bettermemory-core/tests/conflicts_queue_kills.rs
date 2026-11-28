//! Cases a plausible wrong queue fails, each named for its mutant: the
//! deletes after the puts, the listed dict compared instead of `to_dict`,
//! a NaN similarity taken as unchanged, a pending row's detector
//! overwritten, a dismissed pair resurrected on `updated` when a hash is
//! stored, collection on a partial snapshot, `before_stamp` run before the
//! pending re-check, a `from_dict` refusal failing the load, the listing's
//! own error skipped, a batch that keeps rows after a raise, a repeated
//! memory counted twice, the warning's count read once, the status
//! checked inside the batch, rounding and -0.0 taken as changes, and the
//! pending count raising. Over real stores, on every platform: a repeated
//! memory read as its first, a SQLite failure named by a coarse class, a
//! queue call inside an open transaction keeping its writes after a raise
//! (no savepoint of its own), the listing out of `created, id` order, a
//! joined delete without `_require_row`, a BLOB similarity past ASCII
//! read as text, and a failing write or a transaction SQLite ended named
//! by a coarse class.

mod common;

use std::collections::HashMap;
use std::fs::OpenOptions;
use std::io::{Seek, SeekFrom, Write};
use std::path::PathBuf;

use bettermemory_core::conflicts::{
    conflicts_pending_count, split_judgeable, Batched, ConflictCandidate, ConflictQueue,
    ConflictWrites, QueueError, RawRow, Upserted,
};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::records::Memory;
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::tables::list_conflicts;
use bettermemory_core::store::tx::Transaction;
use bettermemory_core::store::SqlValue;
use bettermemory_core::unicode::cps;
use common::conflicts_queue::{memory, Stub};
use common::stores::{scratch, Scratch};
use common::{opened, written};
use serde_json::{json, Value};

const VERDICT: &str = "2026-01-10T00:00:00+00:00";
const EARLIER: &str = "2026-01-09T00:00:00+00:00";
const LATER: &str = "2026-01-11T00:00:00+00:00";

fn row(id: &str, a: &str, b: &str, extra: &[(&str, PyValue)]) -> RawRow {
    let mut out: RawRow = [
        ("id", PyValue::str(id)),
        ("a_id", PyValue::str(a)),
        ("b_id", PyValue::str(b)),
        ("summary_a", PyValue::str("sa")),
        ("summary_b", PyValue::str("sb")),
        ("similarity", PyValue::Float(0.8)),
        ("method", PyValue::str("jaccard")),
        ("detector", PyValue::str("polarity")),
        ("created", PyValue::str("c0")),
        ("status", PyValue::str("pending")),
    ]
    .into_iter()
    .map(|(k, v)| (PyValue::str(k), v))
    .collect();
    for (k, v) in extra {
        let key = PyValue::str(k);
        match out.iter_mut().find(|(ok, _)| *ok == key) {
            Some(slot) => slot.1 = v.clone(),
            None => out.push((key, v.clone())),
        }
    }
    out
}

fn fresh(id: &str, a: &str, b: &str) -> ConflictCandidate {
    ConflictCandidate {
        id: cps(id),
        a_id: cps(a),
        b_id: cps(b),
        summary_a: cps("fa"),
        summary_b: cps("fb"),
        similarity: 0.9,
        method: cps("jaccard"),
        detector: cps("numeric"),
        created: cps("c1"),
        status: cps("pending"),
        verdict_ts: None,
        note: None,
        verdict_hash_a: None,
        verdict_hash_b: None,
    }
}

fn mem(id: &str, updated: &str) -> Memory {
    memory(cps(id), cps(&format!("body of {id}")), updated)
}

fn stub(rows: Vec<RawRow>, count: i64) -> Stub {
    Stub {
        table: rows,
        counts: vec![count],
        ..Stub::default()
    }
}

fn ops(stub: &Stub) -> Vec<String> {
    stub.calls
        .iter()
        .map(|c| c[0].as_str().expect("op").to_owned())
        .collect()
}

fn upsert(s: &mut Stub, fresh: &[ConflictCandidate], ms: &[Memory]) -> (Upserted, Vec<String>) {
    let mut warns = Vec::new();
    let out = ConflictQueue::new(s)
        .upsert_scan(fresh, ms, None, &mut |w| warns.push(w.to_owned()))
        .expect("an upsert");
    (out, warns)
}

fn field<'r>(r: &'r RawRow, key: &str) -> &'r PyValue {
    let key = PyValue::str(key);
    &r.iter().find(|(k, _)| *k == key).expect("a field").1
}

fn dismissed(hashes: bool) -> RawRow {
    let mut extra = vec![
        ("status", PyValue::str("dismissed")),
        ("verdict_ts", PyValue::str(VERDICT)),
    ];
    if hashes {
        let h = |id: &str| {
            bettermemory_core::conflicts::body_hash(&cps(&format!("body of {id}"))).expect("a hash")
        };
        extra.push(("verdict_hash_a", PyValue::str(&h("a"))));
        extra.push(("verdict_hash_b", PyValue::str(&h("b"))));
    }
    row("cf-1", "a", "b", &extra)
}

#[test]
fn deletes_come_before_puts() {
    let mut s = stub(
        vec![row("cf-3", "a", "z", &[]), row("cf-1", "a", "b", &[])],
        2,
    );
    let (out, _) = upsert(
        &mut s,
        &[fresh("cf-4", "a", "b"), fresh("cf-1", "a", "b")],
        &[mem("a", EARLIER), mem("b", EARLIER)],
    );
    assert_eq!((out.added, out.refreshed, out.dropped), (1, 1, 1));
    assert_eq!(
        ops(&s),
        ["batch", "list", "count", "delete", "put", "put", "commit"]
    );
    assert_eq!(s.calls[3][1], json!("cf-3"));
    // The puts in the kept rows' order: the loaded row first.
    assert_eq!(s.calls[4][1]["c"][0], json!("cf-1"));
    assert_eq!(s.calls[5][1]["c"][0], json!("cf-4"));
}

#[test]
fn an_unchanged_row_is_not_put_whatever_its_listed_form() {
    // Listed in reversed key order, and with every column the table holds
    // (the unset ones None): `to_dict` on both sides decides.
    let mut reversed = row("cf-1", "a", "b", &[]);
    reversed.reverse();
    let mut full = row("cf-2", "a", "b", &[]);
    for k in ["verdict_ts", "note", "verdict_hash_a", "verdict_hash_b"] {
        full.push((PyValue::str(k), PyValue::None));
    }
    let mut s = stub(vec![reversed, full], 2);
    let (out, _) = upsert(&mut s, &[], &[mem("a", EARLIER), mem("b", EARLIER)]);
    assert_eq!(out.pending_rows_on_disk, 2);
    assert_eq!(ops(&s), ["batch", "list", "count", "commit"]);
}

#[test]
fn a_nan_similarity_is_put_on_every_scan() {
    let mut s = stub(
        vec![row(
            "cf-1",
            "a",
            "b",
            &[("similarity", PyValue::Float(f64::NAN))],
        )],
        2,
    );
    let ms = [mem("a", EARLIER), mem("b", EARLIER)];
    for _ in 0..2 {
        s.calls.clear();
        upsert(&mut s, &[], &ms);
        assert_eq!(ops(&s), ["batch", "list", "count", "put", "commit"]);
    }
}

#[test]
fn equal_at_four_places_and_minus_zero_are_unchanged() {
    for (stored, found) in [(0.8123, 0.81234), (-0.0, 0.0)] {
        let extra = [
            ("summary_a", PyValue::str("fa")),
            ("summary_b", PyValue::str("fb")),
            ("similarity", PyValue::Float(stored)),
        ];
        let mut s = stub(vec![row("cf-1", "a", "b", &extra)], 2);
        let mut f = fresh("cf-1", "a", "b");
        f.similarity = found;
        let (out, _) = upsert(&mut s, &[f], &[mem("a", EARLIER), mem("b", EARLIER)]);
        assert_eq!(out.refreshed, 1);
        assert_eq!(ops(&s), ["batch", "list", "count", "commit"], "{stored}");
    }
}

#[test]
fn a_pending_rows_detector_is_kept_unless_empty() {
    let ms = [mem("a", EARLIER), mem("b", EARLIER)];
    for (stored, want) in [("polarity", "polarity"), ("", "numeric")] {
        let mut s = stub(
            vec![row("cf-1", "a", "b", &[("detector", PyValue::str(stored))])],
            2,
        );
        upsert(&mut s, &[fresh("cf-1", "a", "b")], &ms);
        assert_eq!(*field(&s.table[0], "detector"), PyValue::str(want));
        assert_eq!(*field(&s.table[0], "summary_a"), PyValue::str("fa"));
    }
}

#[test]
fn a_stored_hash_outranks_updated() {
    // Hashes stored and equal: a later `updated` changes nothing.
    let mut s = stub(vec![dismissed(true)], 2);
    let (out, _) = upsert(
        &mut s,
        &[fresh("cf-1", "a", "b")],
        &[mem("a", LATER), mem("b", LATER)],
    );
    assert_eq!((out.resurrected, out.pending_rows_on_disk), (0, 0));
    assert_eq!(*field(&s.table[0], "status"), PyValue::str("dismissed"));
    // No hashes: the later `updated` resurrects, the detector kept.
    let mut s = stub(vec![dismissed(false)], 2);
    let (out, _) = upsert(
        &mut s,
        &[fresh("cf-1", "a", "b")],
        &[mem("a", LATER), mem("b", EARLIER)],
    );
    assert_eq!(out.resurrected, 1);
    let r = &s.table[0];
    assert_eq!(*field(r, "status"), PyValue::str("pending"));
    assert_eq!(*field(r, "detector"), PyValue::str("polarity"));
    assert_eq!(*field(r, "created"), PyValue::str("c1"));
    assert!(r.iter().all(|(k, _)| *k != PyValue::str("verdict_ts")));
}

#[test]
fn a_partial_snapshot_collects_nothing() {
    let mut s = stub(vec![row("cf-1", "a", "z", &[])], 3);
    s.counts = vec![3, 9];
    let (out, warns) = upsert(&mut s, &[], &[mem("a", EARLIER), mem("b", EARLIER)]);
    assert_eq!((out.dropped, out.gc_deferred), (0, 1));
    assert_eq!(ops(&s), ["batch", "list", "count", "count", "commit"]);
    assert_eq!(
        warns,
        ["conflict-queue GC deferred: the caller's snapshot has 2 memories but the store holds 9"]
    );
}

#[test]
fn a_repeated_memory_counts_once() {
    let mut s = stub(vec![row("cf-1", "a", "z", &[])], 3);
    let (out, _) = upsert(
        &mut s,
        &[],
        &[mem("a", EARLIER), mem("b", EARLIER), mem("a", LATER)],
    );
    assert_eq!(out.gc_deferred, 1);
}

fn resolve(s: &mut Stub, id: &str, status: &str) -> Result<Option<ConflictCandidate>, QueueError> {
    let now = common::conflicts_queue::dt("2026-02-03T04:05:06.789012+00:00");
    let stamp = |tx: &mut Stub| -> Result<(), QueueError> {
        tx.calls.push(json!(["stamp"]));
        Ok(())
    };
    ConflictQueue::new(s).resolve(
        &cps(id),
        &cps(status),
        None,
        &HashMap::new(),
        Some(stamp),
        None,
        &now,
    )
}

#[test]
fn before_stamp_runs_after_the_pending_recheck() {
    let mut s = stub(
        vec![row(
            "cf-1",
            "a",
            "b",
            &[("status", PyValue::str("confirmed"))],
        )],
        0,
    );
    assert!(resolve(&mut s, "cf-1", "dismissed")
        .expect("a resolve")
        .is_none());
    assert_eq!(ops(&s), ["batch", "list", "commit"]);
    let mut s = stub(vec![row("cf-1", "a", "b", &[])], 0);
    let hit = resolve(&mut s, "cf-1", "dismissed")
        .expect("a resolve")
        .expect("a row");
    assert_eq!(
        hit.verdict_ts,
        Some(cps("2026-02-03T04:05:06.789012+00:00"))
    );
    assert_eq!(ops(&s), ["batch", "list", "stamp", "put", "commit"]);
}

#[test]
fn the_status_is_refused_before_the_batch() {
    let mut s = stub(vec![row("cf-1", "a", "b", &[])], 0);
    let e = resolve(&mut s, "cf-1", "pending").expect_err("refused");
    assert_eq!(e.class(), "ValueError");
    assert_eq!(
        e.text(),
        Some(cps(
            "verdict status must be 'confirmed' or 'dismissed', got 'pending'"
        ))
    );
    assert!(s.calls.is_empty());
}

#[test]
fn a_refused_row_does_not_fail_the_load() {
    let mut missing = row("cf-3", "a", "b", &[]);
    missing.remove(0);
    let mut s = stub(
        vec![
            row("cf-1", "a", "b", &[]),
            row("cf-2", "a", "b", &[("similarity", PyValue::str("x"))]),
            missing,
            row(
                "cf-4",
                "a",
                "b",
                &[("similarity", PyValue::List(Vec::new()))],
            ),
            row("cf-5", "a", "b", &[]),
        ],
        0,
    );
    let ids: Vec<PyStr> = ConflictQueue::new(&mut s)
        .load()
        .expect("a load")
        .into_iter()
        .map(|c| c.id)
        .collect();
    assert_eq!(ids, [cps("cf-1"), cps("cf-5")]);
}

#[test]
fn what_from_dict_or_the_listing_raises_otherwise_goes_on() {
    let big = PyValue::int_from_decimal(&format!("1{}", "0".repeat(400))).expect("an int");
    let mut s = stub(vec![row("cf-1", "a", "b", &[("similarity", big)])], 0);
    let e = ConflictQueue::new(&mut s).load().expect_err("an overflow");
    assert_eq!(e.class(), "OverflowError");
    let mut s = stub(vec![row("cf-1", "a", "b", &[])], 0);
    s.fail = Some(("list".to_owned(), 1, "KeyError".to_owned()));
    let e = ConflictQueue::new(&mut s)
        .load()
        .expect_err("the listing's raise");
    assert_eq!(e.class(), "KeyError");
    // The pending count answers 0 for it, and warns.
    s.n.clear();
    let mut warns = Vec::new();
    assert_eq!(
        conflicts_pending_count(&mut s, "p", &mut |w| warns.push(w.to_owned())),
        0
    );
    assert_eq!(warns, ["conflict queue unreadable in p"]);
}

#[test]
fn a_raise_rolls_the_stub_batch_back() {
    let mut s = stub(vec![row("cf-1", "a", "z", &[])], 2);
    s.fail = Some(("put".to_owned(), 2, "RuntimeError".to_owned()));
    let before = s.table.clone();
    let e = ConflictQueue::new(&mut s)
        .upsert_scan(
            &[fresh("cf-2", "a", "b"), fresh("cf-3", "a", "b")],
            &[mem("a", EARLIER), mem("b", EARLIER)],
            None,
            &mut |_| {},
        )
        .expect_err("the second put raises");
    assert_eq!(e.class(), "RuntimeError");
    assert_eq!(s.table, before);
    assert_eq!(s.calls.last(), Some(&json!(["rollback", "RuntimeError"])));
}

#[test]
fn a_raise_rolls_the_store_batch_back() {
    let dir = scratch("queue-kills");
    let c = dir.0.join("c");
    let file = c.join("memory.sqlite");
    opened::made("created", &c);
    let mut store =
        open_for_writing(&file, &c.join("keys"), &opened::engine_provider()).expect("open");
    let mut clock = written::clock(&[written::instant("2026-01-01T00:00:00Z")]);
    let seq = written::last_seq(&file);
    let ms = [mem("a", EARLIER), mem("b", EARLIER)];
    {
        let mut b = Batched::new(&mut store, &mut clock);
        // The first put is made, the second refused (a falsy id): nothing
        // stays, in the table or the log.
        let e = ConflictQueue::new(&mut b)
            .upsert_scan(
                &[fresh("cf-1", "a", "b"), fresh("", "a", "b")],
                &ms,
                None,
                &mut |_| {},
            )
            .expect_err("refused");
        assert_eq!(e.text(), Some(cps("a conflict needs id")));
        assert!(b.committed.is_empty());
    }
    assert_eq!(written::last_seq(&file), seq);
    assert!(list_conflicts(store.connection())
        .expect("a list")
        .is_empty());
    // The same queue then writes.
    let mut b = Batched::new(&mut store, &mut clock);
    let out = ConflictQueue::new(&mut b)
        .upsert_scan(&[fresh("cf-1", "a", "b")], &ms, None, &mut |_| {})
        .expect("an upsert");
    assert_eq!(out.added, 1);
    assert_eq!(b.committed.len(), 1);
    assert_eq!(b.committed[0].rows.len(), 1);
    assert_eq!(written::last_seq(&file), seq + 1);
}

#[test]
fn is_active_short_circuits_left_to_right() {
    let rows = vec![
        fresh("p1", "a", "b"),
        fresh("p2", "x", "b"),
        fresh("p3", "a", "y"),
    ];
    let mut asked = Vec::new();
    let (judgeable, omitted) = split_judgeable(rows, |id: &[u32]| -> Result<bool, ()> {
        asked.push(String::from_utf8(id.iter().map(|&c| c as u8).collect()).expect("ascii"));
        Ok(id == cps("a").as_slice() || id == cps("b").as_slice())
    })
    .expect("a split");
    assert_eq!(judgeable.len(), 1);
    assert_eq!(omitted, 2);
    assert_eq!(asked, ["a", "b", "x", "a", "y"]);
}

#[test]
fn a_repeated_memory_is_read_as_its_last() {
    // The hashes are those of "body of a" and "body of b"; the snapshot's
    // last memory of `a` holds another body, so the pair is resurrected.
    let edited = memory(cps("a"), cps("an edited body"), EARLIER);
    let mut s = stub(vec![dismissed(true)], 2);
    let (out, _) = upsert(
        &mut s,
        &[fresh("cf-1", "a", "b")],
        &[mem("a", EARLIER), mem("b", EARLIER), edited.clone()],
    );
    assert_eq!((out.resurrected, out.pending_rows_on_disk), (1, 1));
    assert_eq!(*field(&s.table[0], "status"), PyValue::str("pending"));
    // The same memories the other way about: the last is unchanged.
    let mut s = stub(vec![dismissed(true)], 2);
    let (out, _) = upsert(
        &mut s,
        &[fresh("cf-1", "a", "b")],
        &[edited, mem("b", EARLIER), mem("a", EARLIER)],
    );
    assert_eq!(out.resurrected, 0);
    assert_eq!(ops(&s), ["batch", "list", "count", "commit"]);
}

/// A store made as the fixtures make it, opened for writing.
fn real_store(tag: &str) -> (Scratch, PathBuf, WritableStore) {
    let dir = scratch(tag);
    let c = dir.0.join("c");
    let file = c.join("memory.sqlite");
    opened::made("created", &c);
    let store = open_for_writing(&file, &c.join("keys"), &opened::engine_provider()).expect("open");
    (dir, file, store)
}

fn one_clock() -> impl bettermemory_core::store::create::Provider {
    written::clock(&[written::instant("2026-01-01T00:00:00Z")])
}

/// A conflicts row written past the log: `similarity` and `id` as SQL
/// spells them.
fn insert(store: &WritableStore, id: &str, a: &str, similarity: &str, created: &str) {
    store
        .connection()
        .execute_batch(&format!(
            "INSERT INTO conflicts VALUES ({id}, '{a}', 'z', 'x', 'y', {similarity}, 'm', \
             'd', '{created}', 'pending', NULL, NULL, NULL, NULL)"
        ))
        .expect("an insert");
}

fn listed_ids(store: &WritableStore) -> Vec<SqlValue> {
    list_conflicts(store.connection())
        .expect("a list")
        .into_iter()
        .map(|r| r[0].clone())
        .collect()
}

fn text(t: &str) -> SqlValue {
    SqlValue::Text(t.to_owned())
}

/// The kinds of the log rows after `seq`.
fn kinds_after(file: &std::path::Path, seq: i64) -> Vec<String> {
    match written::log_rows(file, seq) {
        Value::Array(rows) => rows
            .iter()
            .map(|r| r[3].as_str().expect("a kind").to_owned())
            .collect(),
        other => panic!("log rows {other}"),
    }
}

#[test]
fn a_failing_count_is_sqlites_operational_error() {
    let (_dir, file, mut store) = real_store("queue-count");
    store
        .connection()
        .execute_batch("ALTER TABLE memories RENAME TO memories_gone")
        .expect("a rename");
    let seq = written::last_seq(&file);
    let mut clock = one_clock();
    {
        let mut b = Batched::new(&mut store, &mut clock);
        // The listing reads; `count_memories` finds no table (sqlite3's
        // OperationalError, `no such table: memories`), and the batch is
        // rolled back.
        let e = ConflictQueue::new(&mut b)
            .upsert_scan(
                &[fresh("cf-1", "a", "b")],
                &[mem("a", EARLIER), mem("b", EARLIER)],
                None,
                &mut |_| {},
            )
            .expect_err("no memories table");
        assert_eq!(e.class(), "OperationalError");
        assert!(b.committed.is_empty());
    }
    assert_eq!(written::last_seq(&file), seq);
    assert!(listed_ids(&store).is_empty());
    store
        .connection()
        .execute_batch("ALTER TABLE memories_gone RENAME TO memories")
        .expect("a rename back");
}

#[test]
fn a_later_writers_minimum_is_sqlites_operational_error() {
    let (_dir, file, mut store) = real_store("queue-later");
    store
        .connection()
        .execute_batch(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_writer_revision', '999')",
        )
        .expect("a minimum");
    let seq = written::last_seq(&file);
    let mut clock = one_clock();
    let mut b = Batched::new(&mut store, &mut clock);
    // `require_current_writer` at the batch's BEGIN raises the reference's
    // own sqlite3.OperationalError; nothing is written.
    let e = ConflictQueue::new(&mut b)
        .upsert_scan(&[fresh("cf-1", "a", "b")], &[], None, &mut |_| {})
        .expect_err("a later writer");
    assert_eq!(e.class(), "OperationalError");
    assert!(b.committed.is_empty());
    assert_eq!(written::last_seq(&file), seq);
}

#[test]
fn an_unreadable_writers_minimum_is_store_version_error() {
    // `require_current_writer` reads meta's min_writer_revision as an int:
    // a value that is not one raises store_versions.StoreVersionError, not
    // a native class; nothing is written.
    let (_dir, file, mut store) = real_store("queue-abc");
    store
        .connection()
        .execute_batch(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('min_writer_revision', 'abc')",
        )
        .expect("a minimum");
    let seq = written::last_seq(&file);
    let mut clock = one_clock();
    let mut b = Batched::new(&mut store, &mut clock);
    let e = ConflictQueue::new(&mut b)
        .upsert_scan(&[fresh("cf-1", "a", "b")], &[], None, &mut |_| {})
        .expect_err("an unreadable minimum");
    assert_eq!(e.class(), "StoreVersionError");
    assert!(b.committed.is_empty());
    assert_eq!(written::last_seq(&file), seq);
}

#[test]
fn a_corrupt_conflicts_page_is_sqlites_database_error() {
    let (dir, file, store) = real_store("queue-corrupt");
    let conn = store.connection();
    let root: i64 = conn
        .query_row(
            "SELECT rootpage FROM sqlite_master WHERE name = 'conflicts'",
            [],
            |r| r.get(0),
        )
        .expect("the root page");
    let size: i64 = conn
        .query_row("PRAGMA page_size", [], |r| r.get(0))
        .expect("the page size");
    store.close().expect("a close");
    // The root page's type byte made one SQLite has no b-tree page of.
    let mut f = OpenOptions::new()
        .write(true)
        .open(&file)
        .expect("the file");
    f.seek(SeekFrom::Start(
        u64::try_from((root - 1) * size).expect("an offset"),
    ))
    .expect("a seek");
    f.write_all(&[0x07]).expect("a write");
    drop(f);
    let keys = dir.0.join("c").join("keys");
    let mut store = open_for_writing(&file, &keys, &opened::engine_provider()).expect("reopen");
    // sqlite3's DatabaseError (SQLITE_CORRUPT), not an OperationalError.
    let e = ConflictQueue::new(&mut store)
        .load()
        .expect_err("malformed");
    assert_eq!(e.class(), "DatabaseError");
    let mut clock = one_clock();
    let mut b = Batched::new(&mut store, &mut clock);
    let e = ConflictQueue::new(&mut b)
        .upsert_scan(&[], &[], None, &mut |_| {})
        .expect_err("malformed");
    assert_eq!(e.class(), "DatabaseError");
    let mut warns = Vec::new();
    assert_eq!(
        conflicts_pending_count(&mut store, "p", &mut |w| warns.push(w.to_owned())),
        0
    );
    assert_eq!(warns, ["conflict queue unreadable in p"]);
}

#[test]
fn a_queue_call_inside_an_open_transaction_takes_back_only_its_own_writes() {
    let (_dir, file, mut store) = real_store("queue-nested");
    let seq = written::last_seq(&file);
    let ms = [mem("a", EARLIER), mem("b", EARLIER)];
    let now = common::conflicts_queue::dt("2026-02-03T04:05:06.789012+00:00");
    let mut clock = one_clock();
    let (_, committed) = store
        .batch(&mut clock, None, |tx| -> Result<(), QueueError> {
            let out = ConflictQueue::new(&mut *tx).upsert_scan(
                &[fresh("cf-1", "a", "b")],
                &ms,
                None,
                &mut |_| {},
            )?;
            assert_eq!(out.added, 1);
            // `cf-3` is put, then the empty id refused: this call's own
            // savepoint takes `cf-3` back, `cf-1` stays.
            let e = ConflictQueue::new(&mut *tx)
                .upsert_scan(
                    &[fresh("cf-3", "a", "b"), fresh("", "a", "b")],
                    &ms,
                    None,
                    &mut |_| {},
                )
                .expect_err("refused");
            assert_eq!(e.text(), Some(cps("a conflict needs id")));
            assert_eq!(tx.rows().len(), 1);
            // `before_stamp` puts, then raises: the put goes with the resolve.
            let stamp = |t: &mut Transaction<'_>| -> Result<(), QueueError> {
                ConflictWrites::put_conflict(t, &fresh("cf-s", "x", "y").to_dict(), Some("s"))?;
                Err(QueueError::Raised("RuntimeError".to_owned()))
            };
            let e = ConflictQueue::new(&mut *tx)
                .resolve(
                    &cps("cf-1"),
                    &cps("dismissed"),
                    None,
                    &HashMap::new(),
                    Some(stamp),
                    None,
                    &now,
                )
                .expect_err("the stamp raises");
            assert_eq!(e.class(), "RuntimeError");
            assert_eq!(tx.rows().len(), 1);
            Ok(())
        })
        .expect("the caller's batch commits");
    assert_eq!(committed.rows.len(), 1);
    assert_eq!(kinds_after(&file, seq), ["conflict_put"]);
    assert_eq!(listed_ids(&store), [text("cf-1")]);
    let rows = ConflictQueue::new(&mut store).load().expect("a load");
    assert_eq!(rows[0].status, cps("pending"));
}

#[test]
fn a_joined_put_that_fails_takes_back_its_log_row() {
    // A NaN similarity binds as NULL: the log row is appended, then the
    // table's NOT NULL refuses the row (IntegrityError). A caller that
    // catches it goes on with neither.
    let (_dir, file, mut store) = real_store("queue-joined-put");
    let seq = written::last_seq(&file);
    let mut clock = one_clock();
    let (_, committed) = store
        .batch(&mut clock, None, |tx| -> Result<(), QueueError> {
            let mut nan = fresh("cf-n", "a", "b");
            nan.similarity = f64::NAN;
            let e = tx.put_conflict(&nan.to_dict(), None).expect_err("NOT NULL");
            assert_eq!(QueueError::from(e).class(), "IntegrityError");
            assert!(tx.rows().is_empty());
            let e = tx.delete_conflict("cf-x", None).expect_err("no such row");
            assert_eq!(QueueError::from(e).class(), "NotFoundError");
            tx.put_conflict(&fresh("cf-1", "a", "b").to_dict(), None)?;
            Ok(())
        })
        .expect("the caller's batch commits");
    assert_eq!(committed.rows.len(), 1);
    assert_eq!(kinds_after(&file, seq), ["conflict_put"]);
    assert_eq!(listed_ids(&store), [text("cf-1")]);
}

#[test]
fn rows_are_listed_by_created_then_id() {
    let (_dir, _file, mut store) = real_store("queue-order");
    // Written out of order: neither the rowid's order nor the id's.
    for (id, created) in [
        ("'cf-3'", "2026-01-03"),
        ("'cf-2'", "2026-01-01"),
        ("'cf-9'", "2026-01-02"),
        ("'cf-1'", "2026-01-02"),
    ] {
        insert(&store, id, "a", "0.5", created);
    }
    let want = ["cf-2", "cf-1", "cf-9", "cf-3"];
    assert_eq!(listed_ids(&store), want.map(text));
    let loaded: Vec<PyStr> = ConflictQueue::new(&mut store)
        .load()
        .expect("a load")
        .into_iter()
        .map(|c| c.id)
        .collect();
    assert_eq!(loaded, want.map(cps));
}

#[test]
fn a_collection_whose_delete_finds_no_row_is_rolled_back() {
    let (_dir, file, mut store) = real_store("queue-gone");
    // Both rows name `z`, outside the snapshot. The BLOB id loads as its
    // repr, which names no row: `_require_row`'s NotFoundError after the
    // first delete, and the batch rolled back.
    insert(&store, "'cf-a'", "a", "0.5", "2026-01-01");
    insert(&store, "CAST('cf-b' AS BLOB)", "a", "0.5", "2026-01-02");
    let seq = written::last_seq(&file);
    let mut clock = one_clock();
    {
        let mut b = Batched::new(&mut store, &mut clock);
        let e = ConflictQueue::new(&mut b)
            .upsert_scan(
                &[fresh("cf-1", "a", "b")],
                &[mem("a", EARLIER), mem("b", EARLIER)],
                None,
                &mut |_| {},
            )
            .expect_err("no row");
        assert_eq!(e.class(), "NotFoundError");
        assert_eq!(e.text(), Some(cps("\"b'cf-b'\"")));
        assert!(b.committed.is_empty());
    }
    assert_eq!(written::last_seq(&file), seq);
    assert_eq!(
        listed_ids(&store),
        [text("cf-a"), SqlValue::Blob(b"cf-b".to_vec())]
    );
}

#[test]
fn a_blob_similarity_past_ascii_is_refused() {
    let (_dir, _file, mut store) = real_store("queue-blob");
    // `float(b"\xa00.5")` is ValueError (bytes are not read as text, so
    // the NBSP is no whitespace): the row is skipped. ASCII bytes read as
    // their text.
    insert(&store, "'cf-nb'", "a", "X'a0302e35'", "2026-01-01");
    insert(&store, "'cf-ok'", "a", "X'302e35'", "2026-01-02");
    let rows = ConflictQueue::new(&mut store).load().expect("a load");
    let ids: Vec<PyStr> = rows.iter().map(|c| c.id.clone()).collect();
    assert_eq!(ids, [cps("cf-ok")]);
    assert_eq!(rows[0].similarity.to_bits(), 0.5f64.to_bits());
}

#[test]
fn a_failing_write_is_named_by_its_result_code() {
    // A trigger that fails the conflicts row (the apply) or its log row
    // (the append) with a code outside the constraint family: sqlite3
    // raises DataError for SQLITE_TOOBIG and IntegrityError for
    // SQLITE_MISMATCH. The batch is rolled back.
    for table in ["conflicts", "log"] {
        for (body, class) in [
            ("SELECT zeroblob(2000000000);", "DataError"),
            ("INSERT INTO zz_t VALUES ('abc');", "IntegrityError"),
        ] {
            let (_dir, file, mut store) = real_store("queue-code");
            let setup = format!(
                "CREATE TABLE zz_t (k INTEGER PRIMARY KEY); \
                 CREATE TRIGGER zz BEFORE INSERT ON {table} BEGIN {body} END"
            );
            store.connection().execute_batch(&setup).expect("a trigger");
            let seq = written::last_seq(&file);
            let mut clock = one_clock();
            {
                let mut b = Batched::new(&mut store, &mut clock);
                let e = ConflictQueue::new(&mut b)
                    .file_pair(&fresh("cf-1", "a", "b"), None)
                    .expect_err("the trigger fails the put");
                assert_eq!(e.class(), class, "{setup}");
                assert!(b.committed.is_empty());
            }
            assert_eq!(written::last_seq(&file), seq);
            assert!(listed_ids(&store).is_empty());
        }
    }
}

#[test]
fn a_transaction_sqlite_ended_is_operational_error() {
    // RAISE(ROLLBACK) ends the caller's whole transaction: the put fails
    // (IntegrityError), the next queue call finds the transaction gone and
    // so does the caller's commit (`_require_open`'s OperationalError).
    let (_dir, file, mut store) = real_store("queue-ended");
    store
        .connection()
        .execute_batch(
            "CREATE TRIGGER zz BEFORE INSERT ON conflicts \
             BEGIN SELECT RAISE(ROLLBACK, 'zz'); END",
        )
        .expect("a trigger");
    let seq = written::last_seq(&file);
    let mut clock = one_clock();
    let e = store
        .batch(&mut clock, None, |tx| -> Result<(), QueueError> {
            let e = ConflictQueue::new(&mut *tx)
                .file_pair(&fresh("cf-1", "a", "b"), None)
                .expect_err("the trigger");
            assert_eq!(e.class(), "IntegrityError");
            let e = ConflictQueue::new(&mut *tx)
                .file_pair(&fresh("cf-1", "a", "b"), None)
                .expect_err("the transaction is gone");
            assert_eq!(e.class(), "OperationalError");
            Ok(())
        })
        .expect_err("the commit finds it gone");
    assert_eq!(e.class(), "OperationalError");
    assert_eq!(written::last_seq(&file), seq);
    assert!(listed_ids(&store).is_empty());
}
