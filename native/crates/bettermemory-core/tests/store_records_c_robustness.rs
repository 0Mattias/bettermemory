//! Hostile arguments for `mark_verified` and `record_corroboration`: every
//! argument position given a value nested 9,998 and 100,000 deep, on a
//! 512 KiB thread (the debug build's frames are the largest), hostile ids
//! and wrong types. Each is a typed refusal with nothing written, or (where
//! the reference accepts it: an expected stamp is read only with
//! `check_expected`) written whole; never a panic or an overflow; the store
//! verifies ok after. And the head is checked before the transaction
//! starts, as the reference checks it (887-893): with the write lock held
//! elsewhere, a bad head is its ValueError and a good one meets the lock,
//! with no clock read.

mod common;

use std::path::Path;

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::Memory;
use bettermemory_core::store::create::{self, Provider};
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::writers::{Put, Verify};
use common::created::{fixed, fixture};
use common::opened;
use common::record_calls::Minting;
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

fn provider() -> Minting<create::Fixed> {
    Minting {
        inner: written::clock(&[instant("2001-02-03T11:00:00.000001Z")]),
        next: 0,
    }
}

fn log_len(store: &WritableStore) -> i64 {
    store
        .connection()
        .query_row("SELECT count(*) FROM log", [], |r| r.get(0))
        .expect("count")
}

fn deep(depth: usize, dict: bool) -> PyValue {
    let mut v = PyValue::int(1);
    for _ in 0..depth {
        v = if dict {
            PyValue::Dict(vec![(PyValue::str("k"), v)])
        } else {
            PyValue::List(vec![v])
        };
    }
    v
}

fn record(n: u64) -> Memory {
    let p = written::memory_payload(&ulid(n, "01K"), "a record", &["global"], &[]);
    Memory::from_payload(p.get("memory").expect("memory")).expect("valid")
}

/// `Verify` with `bad` in position `slot` (0 to 9) and `check_expected`.
fn verify_with(bad: &PyValue, slot: usize, check: bool) -> Verify<'_> {
    let at = |i: usize| (i == slot).then_some(bad);
    Verify {
        verified_paths: at(0),
        verified_commits: at(1),
        verified_versions: at(2),
        verified_absent_paths: at(3),
        verified_path_targets: at(4),
        verified_absent_path_targets: at(5),
        claims: at(6),
        verified_head: at(7),
        expected_last_verified_at: at(8),
        expected_updated: at(9),
        check_expected: check,
        session: Some("robust"),
    }
}

fn every_slot_deep() {
    let dir = scratch("records-c-robust-deep");
    let mut store = fresh(&dir.0);
    store
        .put_memory(&mut provider(), &record(1), Put::default())
        .expect("put");
    let (mut written_n, mut classes) = (0, Vec::new());
    for depth in [9_998, 100_000] {
        for dict in [false, true] {
            let bad = deep(depth, dict);
            for slot in 0..10 {
                for check in [false, true] {
                    let before = log_len(&store);
                    match store.mark_verified(
                        &mut provider(),
                        &ulid(1, "01K"),
                        verify_with(&bad, slot, check),
                    ) {
                        Ok(done) => {
                            assert!(log_len(&store) > before);
                            assert!(!done.committed.rows.is_empty());
                            written_n += 1;
                        }
                        Err(e) => {
                            assert_eq!(log_len(&store), before, "{}", e.class());
                            classes.push(e.class());
                        }
                    }
                }
            }
        }
    }
    classes.sort();
    classes.dedup();
    eprintln!("deep: {written_n} written; refusals {classes:?}");
    drop(store);
    let (status, _) = written::native_verify(&dir.0.join("c"), &common::stores::store_id());
    assert_eq!(status, "ok");
}

#[test]
fn every_argument_nested_deep_on_a_small_stack() {
    std::thread::Builder::new()
        .stack_size(512 * 1024)
        .spawn(every_slot_deep)
        .expect("thread")
        .join()
        .expect("no overflow, no panic");
}

#[test]
fn hostile_ids_and_wrong_types() {
    let dir = scratch("records-c-robust-ids");
    let mut store = fresh(&dir.0);
    store
        .put_memory(&mut provider(), &record(1), Put::default())
        .expect("put");
    let before = log_len(&store);
    let long = "0".repeat(1 << 20);
    for id in [
        "",
        "\u{0}",
        "01K\u{0}",
        "' OR 1=1 --",
        long.as_str(),
        "\u{1F600}",
    ] {
        let e = store
            .mark_verified(&mut provider(), id, Verify::default())
            .expect_err("an id that is not one");
        assert_eq!(e.class(), "MemoryNotFoundError");
        let e = store
            .record_corroboration(&mut provider(), id, Some("s"))
            .expect_err("an id that is not one");
        assert_eq!(e.class(), "MemoryNotFoundError");
    }
    let wrong = [
        PyValue::Bool(true),
        PyValue::Float(f64::NAN),
        PyValue::int(i128::MAX),
        PyValue::str("\u{0}"),
        PyValue::Tuple(vec![PyValue::List(vec![])]),
    ];
    let mut outcomes = Vec::new();
    for v in &wrong {
        for slot in 0..10 {
            match store.mark_verified(&mut provider(), &ulid(1, "01K"), verify_with(v, slot, true))
            {
                Ok(_) => outcomes.push("written".to_owned()),
                Err(e) => outcomes.push(e.class()),
            }
        }
    }
    let written_n = outcomes.iter().filter(|o| *o == "written").count();
    eprintln!("wrong types: {outcomes:?}");
    assert_eq!(log_len(&store), before + 2 * written_n as i64);
    // A session that is enormous, or holds the mark's own text.
    for s in [
        long.as_str(),
        "\"corroboration\":\"01K00000000000000000000001\"",
    ] {
        let done = store
            .record_corroboration(&mut provider(), &ulid(1, "01K"), Some(s))
            .expect("corroborated");
        assert!(done.value.is_some());
        let again = store
            .record_corroboration(&mut provider(), &ulid(1, "01K"), Some(s))
            .expect("checked");
        assert!(again.value.is_none());
    }
}

/// Counts the wall clock's reads.
struct Counting {
    inner: Minting<create::Fixed>,
    wall: usize,
}

impl Provider for Counting {
    fn store_id(&mut self) -> std::io::Result<String> {
        self.inner.store_id()
    }
    fn key(&mut self) -> std::io::Result<bettermemory_core::log::Key> {
        self.inner.key()
    }
    fn membership_store_id(&mut self) -> std::io::Result<String> {
        self.inner.membership_store_id()
    }
    fn now(&mut self) -> std::io::Result<bettermemory_core::datetime::DateTime> {
        self.inner.now()
    }
    fn wall_now(&mut self) -> std::io::Result<bettermemory_core::datetime::DateTime> {
        self.wall += 1;
        self.inner.wall_now()
    }
    fn engine_version(&self) -> String {
        self.inner.engine_version()
    }
    fn ulid(&mut self) -> std::io::Result<String> {
        self.inner.ulid()
    }
}

/// Another connection holds the write lock until every call has returned;
/// the writer waits no time.
#[test]
fn the_head_is_checked_before_the_transaction_starts() {
    let dir = scratch("records-c-robust-locked");
    let mut store = fresh(&dir.0);
    store
        .put_memory(&mut provider(), &record(1), Put::default())
        .expect("put");
    store
        .connection()
        .busy_timeout(std::time::Duration::ZERO)
        .expect("no wait");
    let holder = rusqlite::Connection::open(dir.0.join("c").join("memory.sqlite")).expect("open");
    holder
        .execute_batch("BEGIN IMMEDIATE")
        .expect("hold the lock");
    let bad = PyValue::str("not a hash");
    let good = PyValue::str(&"a".repeat(40));
    for (head, class) in [(&bad, "ValueError"), (&good, "native:sqlite")] {
        let mut p = Counting {
            inner: provider(),
            wall: 0,
        };
        let verify = Verify {
            verified_head: Some(head),
            ..Verify::default()
        };
        let e = store
            .mark_verified(&mut p, &ulid(1, "01K"), verify)
            .expect_err("refused");
        assert_eq!(e.class(), class, "{e}");
        assert_eq!(p.wall, 0);
    }
    let mut p = Counting {
        inner: provider(),
        wall: 0,
    };
    let e = store
        .record_corroboration(&mut p, &ulid(1, "01K"), Some("s"))
        .expect_err("locked");
    assert_eq!(e.class(), "native:sqlite");
    assert_eq!(p.wall, 0);
    holder.execute_batch("ROLLBACK").expect("release");
}
