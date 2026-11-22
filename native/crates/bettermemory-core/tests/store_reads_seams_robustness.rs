//! The plain index and record reads over hostile input: very long texts
//! and ids, integer bounds, many rows and many ids. No call panics, in
//! debug or release, and the reads grow linearly: ten times the rows or
//! ids cost under `timing::TENFOLD` times the time.

mod common;
mod timing;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::store::index_reads::{inbound_links, rows_updated_since, RankedQuery};
use bettermemory_core::store::record_reads as rr;
use bettermemory_core::unicode;
use common::store_reads::{bulk_body, empty, ulid};
use rusqlite::Connection;

/// `n` memories by the bulk rule, each linking to the next.
fn store(n: u64) -> Connection {
    let conn = empty();
    let tx = conn.unchecked_transaction().unwrap();
    for i in 1..=n {
        let body = bulk_body(i);
        tx.execute(
            "INSERT INTO memories (rowid, id, created, updated, confidence, body, body_fts, \
             scopes_text, scopes_json, provenance, source) VALUES (?, ?, \
             '2001-02-03T04:05:06+00:00', ?, 'high', ?, ?, 'global', '[\"global\"]', 'local', \
             'explicit-statement')",
            rusqlite::params![
                i as i64,
                ulid(i),
                format!("2001-02-03T04:05:{:02}+00:00", i % 60),
                body,
                bettermemory_core::lexicon::fts_index_text(&unicode::cps(&body)),
            ],
        )
        .unwrap();
        tx.execute(
            "INSERT INTO memory_links VALUES (?, 'supersedes', ?, NULL)",
            [ulid(i), ulid(i + 1)],
        )
        .unwrap();
    }
    tx.commit().unwrap();
    conn
}

#[test]
fn hostile_texts_and_bounds_do_not_panic() {
    let conn = store(50);
    let long: Vec<u32> = unicode::cps(&"alpha-beta_gamma.delta ".repeat(20_000));
    let mut q = RankedQuery::new(&conn, &long).unwrap();
    assert_eq!(q.batches(i64::MAX).count(), 1);
    assert_eq!(q.batches(i64::MIN).count(), 1);
    let quotes: Vec<u32> = unicode::cps(&"\"'()*:^-+ NEAR AND OR NOT ".repeat(5_000));
    let mut q = RankedQuery::new(&conn, &quotes).unwrap();
    for b in q.batches(1) {
        b.unwrap();
    }
    let surrogates = vec![0xD800, 0xDFFF, 0x10FFFF, 0];
    let mut q = RankedQuery::new(&conn, &surrogates).unwrap();
    let _ = q.batches(3).count();
    let huge_id = "Z".repeat(1 << 20);
    assert!(rr::get_memory(&conn, &huge_id).is_err());
    assert!(rr::load_one(&conn, &surrogates).is_err());
    assert!(rr::load_one(&conn, &unicode::cps(&huge_id)).is_err());
    assert!(rr::light_many(&conn, &[i64::MIN, i64::MAX, 0, -1])
        .unwrap()
        .is_empty());
    for text in [
        "0001-01-01T00:00:00+23:59",
        "9999-12-31T23:59:59.999999-23:59",
    ] {
        let dt = DateTime::from_isoformat(&unicode::cps(text)).unwrap();
        assert_eq!(
            rows_updated_since(&conn, &dt).unwrap_err().class(),
            "OverflowError"
        );
    }
    let kinds: Vec<String> = (0..2_000).map(|i| format!("k{i}")).collect();
    let targets: Vec<String> = (0..5_000).map(ulid).collect();
    assert!(inbound_links(&conn, &targets, &kinds).unwrap().is_empty());
}

#[test]
fn reads_grow_linearly() {
    let small = store(400);
    let large = store(4_000);
    let run = |conn: &Connection, n: u64| {
        let mut q = RankedQuery::new(conn, &unicode::cps("alpha filler")).unwrap();
        assert_eq!(q.batches(7).flat_map(Result::unwrap).count() as u64, n);
        let rowids: Vec<i64> = (1..=n as i64).collect();
        assert_eq!(rr::light_many(conn, &rowids).unwrap().len() as u64, n);
        let ids: Vec<String> = (1..=n).map(ulid).collect();
        assert_eq!(rr::light_by_ids(conn, &ids).unwrap().len() as u64, n);
        let got = inbound_links(conn, &ids, &["supersedes".to_owned()]).unwrap();
        assert_eq!(got.len() as u64, n - 1);
        let since = DateTime::from_isoformat(&unicode::cps("2001-02-03T04:05:00+00:00")).unwrap();
        assert_eq!(rows_updated_since(conn, &since).unwrap().len() as u64, n);
    };
    timing::grows_within(
        "reads: 400 and 4,000 rows",
        timing::TENFOLD,
        &mut || timing::once(|| run(&small, 400)),
        &mut || timing::once(|| run(&large, 4_000)),
    );
}
