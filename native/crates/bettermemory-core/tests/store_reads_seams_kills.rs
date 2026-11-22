//! Cases a plausible wrong port of the plain index and record reads fails,
//! each over the fixture's store or a store built here: answers the
//! reference gives and a natural rewrite would not.

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::store::index_reads::{inbound_links, rows_updated_since, RankedQuery};
use bettermemory_core::store::record_reads as rr;
use bettermemory_core::unicode;
use common::jsonl;
use common::store_reads::{call, empty, full, ulid};
use rusqlite::Connection;
use serde_json::json;

fn store() -> Connection {
    full(&jsonl("store_reads_seams.jsonl")[0])
}

fn ids(ms: &[bettermemory_core::records::Memory]) -> Vec<String> {
    ms.iter()
        .map(|m| unicode::to_string(&m.fields.id).unwrap())
        .collect()
}

fn batches(conn: &Connection, text: &str, batch: i64) -> Vec<Vec<i64>> {
    let mut q = RankedQuery::new(conn, &unicode::cps(text)).unwrap();
    q.batches(batch).map(Result::unwrap).collect()
}

/// A store of `n` memories copied from the fixture's record 1, ids
/// `ulid(1000 + i)`.
fn many(n: i64) -> Connection {
    let conn = store();
    for i in 0..n {
        conn.execute(
            "INSERT INTO memories (id, created, updated, confidence, body, body_fts, scopes_text, \
             scopes_json, provenance, source) SELECT ?, created, updated, confidence, body, \
             body_fts, scopes_text, scopes_json, provenance, source FROM memories WHERE rowid = 1",
            [ulid(1000 + i as u64)],
        )
        .unwrap();
    }
    conn
}

#[test]
fn ranked_ties_break_by_row_and_batches_split_in_order() {
    let conn = store();
    // Rows 2, 40 and 41 hold the same text: equal scores, row order.
    assert_eq!(batches(&conn, "words", 2), vec![vec![2, 40], vec![41]]);
    // A repeated query word is asked once.
    assert_eq!(batches(&conn, "beta beta", 9), batches(&conn, "beta", 9));
    // Zero is no batch at all, not every row.
    assert!(batches(&conn, "alpha", 0).is_empty());
}

#[test]
fn ranked_size_errors_follow_cpython_3_13() {
    let conn = store();
    let mut q = RankedQuery::new(&conn, &unicode::cps("alpha")).unwrap();
    let first: Vec<_> = q.batches(-1).collect();
    assert_eq!(first.len(), 1);
    assert_eq!(first[0].as_ref().unwrap_err().class(), "ValueError");
    let at_bound: Vec<_> = q.batches(i64::from(u32::MAX)).collect();
    assert_eq!(at_bound.len(), 1);
    assert!(at_bound[0].is_ok());
    let past: Vec<_> = q.batches(i64::from(u32::MAX) + 1).collect();
    assert_eq!(past[0].as_ref().unwrap_err().class(), "OverflowError");
    // A blank text never reaches the size check.
    let mut blank = RankedQuery::new(&conn, &unicode::cps(" \u{3000}")).unwrap();
    assert_eq!(blank.batches(-1).count(), 0);
}

/// `conn.execute` steps once before `fetchmany` checks the size: a step
/// that fails raises SQLite's error, not the size's. A plain table named
/// `memories_fts` fails at the step (MATCH and bm25 out of context);
/// CPython 3.13 raises OperationalError there for a size of -1.
#[test]
fn ranked_first_step_fails_before_the_size_check() {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(
        "CREATE TABLE memories_fts (memories_fts TEXT); INSERT INTO memories_fts VALUES ('alpha');",
    )
    .unwrap();
    let mut q = RankedQuery::new(&conn, &unicode::cps("alpha")).unwrap();
    for size in [-1, 0, i64::from(u32::MAX) + 1, 5] {
        let got: Vec<_> = q.batches(size).collect();
        assert_eq!(got.len(), 1, "size {size}");
        assert_eq!(got[0].as_ref().unwrap_err().class(), "native:sqlite");
    }
}

#[test]
fn since_is_cut_in_utc_to_the_second() {
    let conn = store();
    let at = |t: &str| {
        rows_updated_since(&conn, &DateTime::from_isoformat(&unicode::cps(t)).unwrap()).unwrap()
    };
    // +02:00 is the same instant; a port that drops the offset reads 06:05.
    assert_eq!(
        at("2001-02-03T06:05:06+02:00"),
        at("2001-02-03T04:05:06+00:00")
    );
    // The fraction is cut, not rounded: .999999 keeps the second's rows.
    assert_eq!(at("2001-02-03T04:05:06.999999+00:00").len(), 9);
    // Equal stamps: the greater id first.
    assert_eq!(at("2001-02-04T00:00:00+00:00"), vec![5, 6, 4]);
    // The bound is inclusive: a stamp equal to the cut is kept.
    conn.execute(
        "UPDATE memories SET updated = '2001-02-03T04:05:06' WHERE rowid = 1",
        [],
    )
    .unwrap();
    assert!(at("2001-02-03T04:05:06+00:00").contains(&1));
    assert!(!at("2001-02-03T04:05:07+00:00").contains(&1));
    // A naive datetime is the machine's local time to the reference.
    let naive = DateTime::from_isoformat(&unicode::cps("2001-02-03T04:05:06")).unwrap();
    assert_eq!(
        rows_updated_since(&conn, &naive).unwrap_err().class(),
        "native:not_ported"
    );
}

#[test]
fn inbound_orders_by_source_stamp_then_id_and_needs_a_source_row() {
    let conn = store();
    let kinds = ["supersedes".to_owned(), "contradicts".to_owned()];
    let got = inbound_links(&conn, &[ulid(1)], &kinds).unwrap();
    let sources: Vec<&str> = got[&ulid(1)].iter().map(|e| e.1.as_str()).collect();
    // The +00:00 spelling sorts before ".500000+00:00" as text; the link
    // from ulid(50), which has no row, is dropped; ulid(12)'s two notes
    // give two entries.
    let want = [
        "01J\u{c9}\u{65e5}\u{672c}".to_owned(),
        ulid(3),
        ulid(12),
        ulid(12),
        ulid(4),
        ulid(6),
        ulid(5),
    ];
    assert_eq!(sources, want.iter().map(String::as_str).collect::<Vec<_>>());
    assert!(!sources.contains(&ulid(50).as_str()));
    assert_eq!(sources.iter().filter(|s| **s == ulid(12)).count(), 2);
    let pos = |id: &str| sources.iter().position(|s| *s == id).unwrap();
    assert!(
        pos(&ulid(4)) < pos(&ulid(6)),
        "equal stamps: the lesser id first"
    );
    // A target with no link of the types asked has no key.
    let none = inbound_links(&conn, &[ulid(2)], &["supersedes".to_owned()]).unwrap();
    assert!(none.is_empty());
}

#[test]
fn inbound_reads_targets_past_one_batch() {
    let conn = store();
    let mut targets: Vec<String> = (2000..2600).map(ulid).collect();
    targets.push(ulid(1));
    let got = inbound_links(&conn, &targets, &["supersedes".to_owned()]).unwrap();
    assert_eq!(got.len(), 1);
    assert!(got.contains_key(&ulid(1)));
}

#[test]
fn record_reads_keep_order_duplicates_and_first_sight() {
    let conn = store();
    let asked = [ulid(2), ulid(1), ulid(2)];
    assert_eq!(ids(&rr::get_many(&conn, &asked).unwrap()), asked.to_vec());
    assert_eq!(
        ids(&rr::light_by_ids(&conn, &asked).unwrap()),
        vec![ulid(2), ulid(1)]
    );
    assert_eq!(
        ids(&rr::light_many(&conn, &[2, 1, 2]).unwrap()),
        vec![ulid(2), ulid(1)]
    );
    // memory_ids follows the row, not the id.
    let all = rr::memory_ids(&conn).unwrap();
    assert_eq!(all[6], ulid(11));
    assert_eq!(all[7], "01J\u{c9}\u{65e5}\u{672c}");
}

#[test]
fn light_reads_drop_the_signed_origin() {
    let conn = store();
    let signed = rr::get_memory(&conn, &ulid(5)).unwrap();
    let light = &rr::light_many(&conn, &[5]).unwrap()[0];
    assert!(signed.fields.origin.is_some());
    assert!(light.fields.origin.is_none());
}

#[test]
fn light_many_reads_past_one_batch() {
    let conn = many(600);
    // Row 40 holds the id that is no ULID: it would raise.
    let rowids: Vec<i64> = (1..=700).rev().filter(|r| *r != 40).collect();
    let got = rr::light_many(&conn, &rowids).unwrap();
    assert_eq!(got.len(), 608);
    assert_eq!(unicode::to_string(&got[0].fields.id).unwrap(), ulid(1599));
}

#[test]
fn load_one_prefers_the_active_row_and_words_its_errors() {
    let conn = store();
    // ulid(11) has an active row and a tombstone.
    assert!(rr::load_one(&conn, &unicode::cps(&ulid(11))).is_ok());
    let out = call(&conn, "load_one", &[json!(ulid(9))]);
    assert_eq!(out["raised"], "TombstonedError");
    let out = call(&conn, "load_one", &[json!("01j00000000000000000000001")]);
    assert_eq!(out["text"], "\"invalid id: '01j00000000000000000000001'\"");
    // A KeyError's str() is the repr of its message.
    let out = call(&conn, "get_memory", &[json!("it's")]);
    assert_eq!(out["text"], "\"it's\"");
}

#[test]
fn filename_and_body_tell_absent_from_null() {
    let conn = store();
    assert_eq!(rr::filename_for(&conn, &ulid(2)).unwrap(), None);
    assert_eq!(
        rr::filename_for(&conn, &ulid(9)).unwrap_err().class(),
        "NotFoundError"
    );
    assert_eq!(rr::body_of(&conn, &ulid(9)).unwrap(), None);
    let e = empty();
    assert!(!rr::has_memories(&e).unwrap());
    assert_eq!(rr::count_memories(&e).unwrap(), 0);
}
