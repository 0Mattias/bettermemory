//! Hostile inputs to the queue: 10,000 queued rows over the stub and over
//! a real store, a scan of 2,000 memories that files a thousand pairs,
//! rows nested past `repr`'s depth, integers past the float range, texts
//! of a million code points and lone surrogates. None may panic, in debug
//! or release, and the queue's work grows with its rows as the
//! reference's loops do (tests/timing).
//!
//! The reference (CPython 3.13.13, Apple M-series, a loaded machine,
//! best of three): `upsert_scan` over the recording stub with 1,000 queued
//! rows found again and ten new ones takes 3.1 ms and with 10,000 rows
//! 34.8 ms; `scan_conflicts` over 200 memories that file 100 pairs takes
//! 0.03 s and over 2,000 that file 1,000 pairs 2.06 s, nearly all of it
//! the pairwise scan.

mod common;
mod timing;

use bettermemory_core::conflicts::{
    find_conflict_candidates, scan_conflicts, Batched, ConflictCandidate, ConflictQueue, RawRow,
};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::Memory;
use bettermemory_core::store::open::open_for_writing;
use bettermemory_core::unicode::cps;
use common::conflicts_queue::{dt, memory, Stub};
use common::stores::scratch;
use common::{opened, written};

const U: &str = "2026-01-01T00:00:00+00:00";

fn row(i: usize) -> RawRow {
    [
        ("id", PyValue::str(&format!("cf-{i:06}"))),
        ("a_id", PyValue::str(&format!("m{}", i % 500))),
        ("b_id", PyValue::str(&format!("m{}", (i + 1) % 500))),
        ("summary_a", PyValue::str("fa")),
        ("summary_b", PyValue::str("fb")),
        ("similarity", PyValue::Float(0.85)),
        ("method", PyValue::str("jaccard")),
        ("detector", PyValue::str("polarity")),
        ("created", PyValue::str("c0")),
        (
            "status",
            PyValue::str(["pending", "confirmed", "dismissed"][i % 3]),
        ),
        ("verdict_ts", PyValue::str("2026-01-02T00:00:00+00:00")),
    ]
    .into_iter()
    .map(|(k, v)| (PyValue::str(k), v))
    .collect()
}

fn fresh(i: usize) -> ConflictCandidate {
    ConflictCandidate {
        id: cps(&format!("cf-{i:06}")),
        a_id: cps(&format!("m{}", i % 500)),
        b_id: cps(&format!("m{}", (i + 1) % 500)),
        summary_a: cps("fa"),
        summary_b: cps("fb"),
        similarity: 0.85,
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

fn snapshot(n: usize) -> Vec<Memory> {
    (0..n)
        .map(|i| memory(cps(&format!("m{i}")), cps(&format!("body {i}")), U))
        .collect()
}

/// `n` queued rows (pending, confirmed and dismissed in turn) and the
/// fresh pairs of a scan over them: each row found again unchanged, and
/// ten new pairs. The stub's put scans its table, so the new pairs are
/// few and the claim is the queue's own work.
fn queue(n: usize) -> (Stub, Vec<ConflictCandidate>) {
    let s = Stub {
        table: (0..n).map(row).collect(),
        counts: vec![500],
        ..Stub::default()
    };
    let fresh = (0..n + 10).map(fresh).collect();
    (s, fresh)
}

#[test]
fn ten_thousand_queued_rows_grow_as_the_rows() {
    let ms = snapshot(499);
    let (mut s, f) = queue(10_000);
    let out = ConflictQueue::new(&mut s)
        .upsert_scan(&f, &ms, None, &mut |_| {})
        .expect("an upsert");
    // 499 memories against a count of 500: deferred, nothing dropped;
    // the pending rows refreshed as they were, so only the new are put.
    assert_eq!((out.added, out.refreshed), (10, 3_334));
    assert_eq!((out.dropped, out.gc_deferred), (0, 1));
    assert_eq!(s.calls.iter().filter(|c| c[0] == "put").count(), 10);
    let (small, small_fresh) = queue(1_000);
    let (large, large_fresh) = queue(10_000);
    timing::grows_within(
        "upsert_scan over 10,000 queued rows against 1,000",
        timing::TENFOLD,
        &mut || {
            let mut s = Stub {
                table: small.table.clone(),
                counts: vec![500],
                ..Stub::default()
            };
            timing::once(|| {
                ConflictQueue::new(&mut s).upsert_scan(&small_fresh, &ms, None, &mut |_| {})
            })
        },
        &mut || {
            let mut s = Stub {
                table: large.table.clone(),
                counts: vec![500],
                ..Stub::default()
            };
            timing::once(|| {
                ConflictQueue::new(&mut s).upsert_scan(&large_fresh, &ms, None, &mut |_| {})
            })
        },
    );
}

#[test]
fn ten_thousand_rows_on_a_real_store() {
    let dir = scratch("queue-robust");
    let c = dir.0.join("c");
    let file = c.join("memory.sqlite");
    opened::made("created", &c);
    let mut store =
        open_for_writing(&file, &c.join("keys"), &opened::engine_provider()).expect("open");
    let mut clock = written::clock(&[written::instant(U)]);
    // 10,000 rows written past the log, then folded in: none changes.
    let sql: String = (0..10_000)
        .map(|i| {
            format!(
                "INSERT INTO conflicts VALUES ('cf-{i:06}', 'm{}', 'm{}', 'sa', 'sb', 0.8, \
                 'jaccard', 'polarity', 'c0', 'pending', NULL, NULL, NULL, NULL);",
                i % 500,
                (i + 1) % 500
            )
        })
        .collect();
    store.connection().execute_batch(&sql).expect("rows");
    let seq = written::last_seq(&file);
    let ms = snapshot(500);
    let mut b = Batched::new(&mut store, &mut clock);
    let mut q = ConflictQueue::new(&mut b);
    assert_eq!(q.pending().expect("pending").len(), 10_000);
    let out = q
        .upsert_scan(&[], &ms, None, &mut |_| {})
        .expect("an upsert");
    assert_eq!((out.dropped, out.pending_rows_on_disk), (0, 10_000));
    assert_eq!(written::last_seq(&file), seq);
    assert!(b.committed[0].rows.is_empty());
}

/// `2n` memories: each pair a body and its negation, one pair per word.
fn negations(n: usize) -> Vec<Memory> {
    (0..2 * n)
        .map(|i| {
            let body = if i % 2 == 0 {
                format!("Use sudo for deploy w{}", i / 2)
            } else {
                format!("Do not use sudo for deploy w{}", i / 2)
            };
            memory(cps(&format!("m{i:05}")), cps(&body), U)
        })
        .collect()
}

#[test]
fn a_scan_of_two_thousand_memories() {
    let ms = negations(1_000);
    let now = dt("2026-02-03T04:05:06+00:00");
    let found = find_conflict_candidates(&ms, None, &now).expect("a scan");
    assert_eq!(found.len(), 1_000);
    let mut s = Stub {
        counts: vec![2_000],
        ..Stub::default()
    };
    let out = scan_conflicts(&mut s, &ms, None, None, &now, &mut |_| {}).expect("a scan");
    assert_eq!((out.added, out.pending_rows_on_disk), (1_000, 1_000));
    assert_eq!(s.table.len(), 1_000);
    // The pairwise scan is the reference's quadratic loop: 2,000 memories
    // against 200, with the room `TENFOLD` leaves a linear claim.
    let small = negations(100);
    timing::grows_within(
        "scan_conflicts over 2,000 memories against 200",
        timing::TENFOLD * 10,
        &mut || {
            let mut s = Stub {
                counts: vec![200],
                ..Stub::default()
            };
            timing::once(|| scan_conflicts(&mut s, &small, None, None, &now, &mut |_| {}))
        },
        &mut || {
            let mut s = Stub {
                counts: vec![2_000],
                ..Stub::default()
            };
            timing::once(|| scan_conflicts(&mut s, &ms, None, None, &now, &mut |_| {}))
        },
    );
}

fn nested(depth: usize) -> PyValue {
    let mut v = PyValue::None;
    for _ in 0..depth {
        v = PyValue::List(vec![v]);
    }
    v
}

fn with(i: usize, key: &str, value: PyValue) -> RawRow {
    let mut r = row(i);
    let key = PyValue::str(key);
    match r.iter_mut().find(|(k, _)| *k == key) {
        Some(slot) => slot.1 = value,
        None => r.push((key, value)),
    }
    r
}

#[test]
fn hostile_rows_answer_without_panic() {
    let big = PyValue::int_from_decimal(&format!("9{}", "9".repeat(400))).expect("an int");
    let long = PyValue::Str(vec![u32::from('x'); 1_000_000]);
    let surrogates = PyValue::Str(vec![0xD800; 1_000]);
    for (key, value, class) in [
        ("note", nested(100_000), Some("RecursionError")),
        ("similarity", big, Some("OverflowError")),
        ("similarity", nested(100_000), None),
        ("summary_a", long.clone(), None),
        ("id", surrogates.clone(), None),
        ("verdict_ts", long, None),
    ] {
        let mut s = Stub {
            table: vec![row(0), with(1, key, value)],
            counts: vec![500],
            ..Stub::default()
        };
        let got = ConflictQueue::new(&mut s).load();
        match class {
            Some(class) => assert_eq!(got.expect_err(key).class(), class, "{key}"),
            None => assert!(got.is_ok(), "{key}"),
        }
        let ms = snapshot(500);
        let _ = ConflictQueue::new(&mut s).upsert_scan(&[fresh(1)], &ms, None, &mut |_| {});
    }
    // A judged body of lone surrogates is UnicodeEncodeError, rolled back.
    let mut s = Stub {
        table: vec![with(1, "verdict_hash_a", PyValue::str("0123456789abcdef"))],
        counts: vec![2],
        ..Stub::default()
    };
    s.table[0][9].1 = PyValue::str("dismissed");
    let ms = vec![
        memory(cps("m1"), vec![0xDFFF; 1_000_000], U),
        memory(cps("m2"), cps("b"), U),
    ];
    let e = ConflictQueue::new(&mut s)
        .upsert_scan(&[fresh(1)], &ms, None, &mut |_| {})
        .expect_err("an encode error");
    assert_eq!(e.class(), "UnicodeEncodeError");
}
