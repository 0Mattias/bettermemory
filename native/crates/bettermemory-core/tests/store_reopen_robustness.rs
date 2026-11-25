//! `WritableStore::reopen` under load and at its bounds: eight threads,
//! each writing through its own reopened connection while the owner
//! writes (every row verifies under the store's key, the chain links, and
//! the head names the last row); a reopen while another connection holds
//! the write lock (it opens: nothing it does takes the lock); busy timeouts
//! of 0 and of 1e9 seconds (SQLite's millisecond count past 32 bits, read
//! as 0 by the reference's SQLite 3.50.4); and the time a reopen takes,
//! the same for a log ten times as long (it reads no log row it need not).

#![cfg(unix)]

mod common;
mod timing;

use std::path::Path;

use bettermemory_core::log::replaced::append_history_replaced;
use bettermemory_core::log::{row_verifies, LogRow};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::open::{open_for_writing, Reopened, WritableStore};
use bettermemory_core::unicode;
use common::opened;
use common::stores::{scratch, store_id, Scratch};
use common::written::{clock, instant};
use rusqlite::Connection;
use serde_json::json;

fn owner(name: &str) -> (Scratch, std::path::PathBuf, WritableStore) {
    let dir = scratch(name);
    let c = dir.0.join("c");
    opened::made("intact", &c);
    opened::setup(&[json!({"bind": "memory.sqlite"})], &c, &store_id());
    let store = open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("the owner's open");
    (dir, c, store)
}

fn entry(n: usize) -> PyValue {
    PyValue::Dict(vec![
        (PyValue::str("kind"), PyValue::str("store_restored")),
        (PyValue::str("bundle"), PyValue::str(&format!("/b/{n}"))),
    ])
}

fn write(store: &mut WritableStore, n: usize) {
    let mut provider = clock(&[instant("2003-04-05T06:07:08.123456Z")]);
    append_history_replaced(
        store,
        &mut provider,
        &unicode::cps("store_restored"),
        &entry(n),
    )
    .expect("appended")
    .expect("a row");
}

fn rows(c: &Path) -> Vec<LogRow> {
    let conn = Connection::open(c.join("memory.sqlite")).expect("open");
    let mut statement = conn
        .prepare("SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log ORDER BY seq")
        .expect("prepare");
    statement
        .query_map([], |r| {
            Ok(LogRow {
                seq: r.get(0)?,
                ts: r.get(1)?,
                session: r.get(2)?,
                kind: r.get(3)?,
                payload: r.get(4)?,
                prev_mac: r.get(5)?,
                mac: r.get(6)?,
            })
        })
        .expect("rows")
        .collect::<Result<_, _>>()
        .expect("rows")
}

#[test]
fn eight_reopened_writers_and_the_owner_keep_one_chain_and_one_head() {
    const EACH: usize = 6;
    let (_dir, c, mut store) = owner("reopen-threads");
    let before = rows(&c).len();
    let key = std::fs::read(c.join("keys").join(format!("{}.key", store_id()))).expect("key");
    let mut reopened = Vec::new();
    for _ in 0..8 {
        let mut logged = Vec::new();
        match store
            .reopen(&opened::engine_provider(), Some(30.0), &mut logged)
            .expect("reopened")
        {
            Reopened::Writer(w) => reopened.push(w),
            Reopened::Reader(r) => panic!("a reader: {r:?}"),
        }
    }
    std::thread::scope(|scope| {
        for (t, mut other) in reopened.into_iter().enumerate() {
            scope.spawn(move || {
                for i in 0..EACH {
                    write(&mut other, 100 * (t + 1) + i);
                }
                other.close().expect("close");
            });
        }
        for i in 0..EACH {
            write(&mut store, i);
        }
    });
    let all = rows(&c);
    assert_eq!(all.len(), before + 9 * EACH);
    for pair in all.windows(2) {
        assert_eq!(pair[1].seq, pair[0].seq + 1);
        assert_eq!(pair[1].prev_mac, pair[0].mac, "seq {}", pair[1].seq);
    }
    for row in &all[before..] {
        assert!(row_verifies(&key, row), "seq {} does not verify", row.seq);
    }
    let head: serde_json::Value = serde_json::from_str(
        &std::fs::read_to_string(c.join("keys").join(format!("{}.head", store_id())))
            .expect("head"),
    )
    .expect("head JSON");
    let last = all.last().expect("rows");
    assert_eq!(head["seq"], json!(last.seq));
    assert_eq!(head["mac"], json!(last.mac));
    store.close().expect("close");
}

#[test]
fn a_reopen_under_a_writer_holding_the_lock_opens() {
    let (_dir, c, store) = owner("reopen-locked");
    let holder = Connection::open(c.join("memory.sqlite")).expect("holder");
    holder.execute_batch("BEGIN IMMEDIATE").expect("lock");
    let mut logged = Vec::new();
    let started = std::time::Instant::now();
    let other = store
        .reopen(&opened::engine_provider(), Some(0.0), &mut logged)
        .expect("reopened");
    assert!(started.elapsed() < std::time::Duration::from_secs(4));
    assert!(other.has_key());
    // Its own write waits no time and fails busy, as its timeout says.
    let Reopened::Writer(mut writer) = other else {
        panic!("a reader")
    };
    let mut provider = clock(&[instant("2003-04-05T06:07:08.123456Z")]);
    assert!(append_history_replaced(
        &mut writer,
        &mut provider,
        &unicode::cps("store_restored"),
        &entry(1)
    )
    .is_err());
    holder.execute_batch("ROLLBACK").expect("release");
    writer.close().expect("close");
    store.close().expect("close");
}

#[test]
fn busy_timeouts_at_the_bounds() {
    let (_dir, _c, store) = owner("reopen-bounds");
    for (seconds, sqlite) in [
        (0.0, 0),
        (1e9, 0),
        (2_147_483.647, 2_147_483_647),
        (1e-9, 0),
    ] {
        let mut logged = Vec::new();
        let other = store
            .reopen(&opened::engine_provider(), Some(seconds), &mut logged)
            .expect("reopened");
        assert_eq!(other.busy_timeout(), seconds);
        let set: i64 = other
            .connection()
            .query_row("PRAGMA busy_timeout", [], |r| r.get(0))
            .expect("pragma");
        assert_eq!(set, sqlite, "{seconds}");
        other.close().expect("close");
    }
    let mut logged = Vec::new();
    for bad in [f64::INFINITY, -0.001, f64::NEG_INFINITY] {
        let got = store.reopen(&opened::engine_provider(), Some(bad), &mut logged);
        assert_eq!(got.map(|_| ()).expect_err("refused").class(), "ValueError");
    }
    // `int(1e306 * 1000)` is a float infinity's: OverflowError.
    let got = store.reopen(&opened::engine_provider(), Some(1e306), &mut logged);
    assert_eq!(
        got.map(|_| ()).expect_err("refused").class(),
        "OverflowError"
    );
    store.close().expect("close");
}

#[test]
fn a_reopen_reads_no_more_of_a_longer_log() {
    let sized = |n: usize| {
        let (dir, c, mut store) = owner(&format!("reopen-time-{n}"));
        for i in 0..n {
            write(&mut store, i);
        }
        drop(c);
        (dir, store)
    };
    let (_small_dir, small) = sized(20);
    let (_large_dir, large) = sized(200);
    // The reopen reads the store's file: its wait counts (`timing::wall`).
    let run = |store: &WritableStore| {
        timing::wall(|| {
            let mut logged = Vec::new();
            store
                .reopen(&opened::engine_provider(), None, &mut logged)
                .expect("reopened")
                .close()
                .expect("close");
        })
    };
    timing::grows_within("reopen", 3, &mut || run(&small), &mut || run(&large));
    small.close().expect("close");
    large.close().expect("close");
}
