//! Hostile arguments for `put_tombstone`, `restore` and `restore_trimmed`:
//! every argument position given a value nested 9,998 and 100,000 deep, on
//! a 512 KiB thread (the debug build's frames are the largest), hostile
//! ids and wrong types. Each is a typed refusal with nothing written, or
//! written whole where the reference accepts it; never a panic or an
//! overflow; the store verifies ok after. Stored rows nested as deep
//! (a tombstone's links, a linking record's links) are refused the same
//! way, nothing written. And what the reference checks before its transaction is checked
//! before `start`: with the write lock held elsewhere, a bad label, a bad
//! link or an id that is not a ULID is its own refusal, and a good call
//! meets the lock, with no clock read.

mod common;

use std::path::Path;

use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::{Memory, Tombstone};
use bettermemory_core::store::create::{self, Provider};
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::writers::{Put, PutTombstone, Relink, Trim};
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

fn dead(n: u64) -> Tombstone {
    let text = format!(
        "{{\"id\": \"{}\", \"created\": \"2001-02-03T04:05:06Z\", \
         \"updated\": \"2001-02-03T04:05:06Z\", \"scopes\": [\"global\"], \
         \"confidence\": \"high\", \"source\": \"explicit-statement\", \
         \"body\": \"removed\", \"claims\": [\"a\"], \
         \"removed\": \"2001-02-04T00:00:00Z\", \"removed_reason\": \"outdated\"}}",
        ulid(n, "01K")
    );
    Tombstone::from_payload(&written::payload(&text)).expect("valid")
}

fn put_tombstone_with(bad: &PyValue, slot: usize) -> PutTombstone<'_> {
    let at = |i: usize| (i == slot).then_some(bad);
    PutTombstone {
        provenance: at(0),
        links: at(1),
        corroborations: at(2),
        session: Some("robust"),
        ..PutTombstone::default()
    }
}

fn trim_with(bad: &PyValue, slot: usize) -> Trim<'_> {
    let at = |i: usize| (i == slot).then_some(bad);
    Trim {
        drop_claims: at(0),
        drop_verified_paths: at(1),
        clear_verification: at(2),
        drop_verified_head: at(3),
        session: Some("robust"),
        relink: Relink::default(),
    }
}

fn outcome<T>(
    store: &WritableStore,
    before: i64,
    got: Result<T, bettermemory_core::store::writers::RecordWriteError>,
    classes: &mut Vec<String>,
) -> bool {
    match got {
        Ok(_) => {
            assert!(log_len(store) > before);
            true
        }
        Err(e) => {
            assert_eq!(log_len(store), before, "{}", e.class());
            classes.push(e.class());
            false
        }
    }
}

fn every_slot_deep() {
    let dir = scratch("records-d-robust-deep");
    let mut store = fresh(&dir.0);
    let (mut written_n, mut classes) = (0, Vec::new());
    for depth in [9_998, 100_000] {
        for dict in [false, true] {
            let bad = deep(depth, dict);
            for slot in 0..3 {
                let before = log_len(&store);
                let got =
                    store.put_tombstone(&mut provider(), &dead(1), put_tombstone_with(&bad, slot));
                written_n += usize::from(outcome(&store, before, got, &mut classes));
            }
            for slot in 0..4 {
                // A fresh tombstone each time: a trim that writes restores it.
                store
                    .put_tombstone(&mut provider(), &dead(2), PutTombstone::default())
                    .ok();
                let before = log_len(&store);
                let got =
                    store.restore_trimmed(&mut provider(), &ulid(2, "01K"), trim_with(&bad, slot));
                if outcome(&store, before, got, &mut classes) {
                    written_n += 1;
                    store
                        .tombstone(&mut provider(), &ulid(2, "01K"), "again", None, None)
                        .expect("tombstoned again");
                }
            }
        }
    }
    drop(store);
    let (status, _) = written::native_verify(&dir.0.join("c"), &common::stores::store_id());
    assert_eq!(status, "ok");
    // Stored rows as deep, by hand (the check then reads the store as
    // edited outside the log): a tombstone's links, and a linking record's.
    let c = dir.0.join("c");
    let mut store = open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("reopen");
    let deep_text = format!("{}1{}", "[".repeat(100_000), "]".repeat(100_000));
    store
        .put_tombstone(&mut provider(), &dead(3), PutTombstone::default())
        .expect("tombstone 3");
    store
        .connection()
        .execute(
            "UPDATE tombstones SET links_json = ? WHERE id = ?",
            [deep_text.as_str(), ulid(3, "01K").as_str()],
        )
        .expect("deep links");
    let before = log_len(&store);
    let got = store.restore(&mut provider(), &ulid(3, "01K"), None, Relink::default());
    assert!(!outcome(&store, before, got, &mut classes));
    store
        .put_memory(&mut provider(), &record(4), Put::default())
        .expect("put 4");
    store
        .put_tombstone(&mut provider(), &dead(5), PutTombstone::default())
        .expect("tombstone 5");
    let linking = format!("[{{\"target_id\": \"{}\"}}, {deep_text}]", ulid(5, "01K"));
    store
        .connection()
        .execute(
            "UPDATE memories SET links_json = ? WHERE id = ?",
            [linking.as_str(), ulid(4, "01K").as_str()],
        )
        .expect("deep linking row");
    let before = log_len(&store);
    let got = store.restore(&mut provider(), &ulid(5, "01K"), None, Relink::default());
    assert!(!outcome(&store, before, got, &mut classes));
    classes.sort();
    classes.dedup();
    eprintln!("deep: {written_n} written; refusals {classes:?}");
    assert!(
        classes.contains(&"RecursionError".to_owned()),
        "{classes:?}"
    );
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
    let dir = scratch("records-d-robust-ids");
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
        "%",
        "_",
        long.as_str(),
        "\u{1F600}",
    ] {
        let e = store
            .restore(&mut provider(), id, Some("s"), Relink::default())
            .expect_err("no tombstone");
        assert_eq!(e.class(), "NotFoundError");
        let e = store
            .restore_trimmed(&mut provider(), id, Trim::default())
            .expect_err("not a ULID");
        assert_eq!(e.class(), "MemoryNotFoundError");
    }
    assert_eq!(log_len(&store), before);
    store
        .put_tombstone(&mut provider(), &dead(2), PutTombstone::default())
        .expect("tombstone 2");
    let wrong = [
        PyValue::Bool(true),
        PyValue::Float(f64::NAN),
        PyValue::int(i128::MAX),
        PyValue::str("\u{0}"),
        PyValue::Tuple(vec![PyValue::List(vec![])]),
    ];
    let mut outcomes = Vec::new();
    for v in &wrong {
        for slot in 0..3 {
            let before = log_len(&store);
            match store.put_tombstone(&mut provider(), &dead(2), put_tombstone_with(v, slot)) {
                Ok(_) => {
                    assert_eq!(log_len(&store), before + 1);
                    outcomes.push("written".to_owned());
                }
                Err(e) => {
                    assert_eq!(log_len(&store), before);
                    outcomes.push(e.class());
                }
            }
        }
        for slot in 0..4 {
            let before = log_len(&store);
            match store.restore_trimmed(&mut provider(), &ulid(2, "01K"), trim_with(v, slot)) {
                Ok(_) => {
                    outcomes.push("written".to_owned());
                    store
                        .tombstone(&mut provider(), &ulid(2, "01K"), "again", None, None)
                        .expect("again");
                }
                Err(e) => {
                    assert_eq!(log_len(&store), before);
                    outcomes.push(e.class());
                }
            }
        }
    }
    eprintln!("wrong types: {outcomes:?}");
    // An active id is refused by both.
    let e = store
        .put_tombstone(&mut provider(), &dead(1), PutTombstone::default())
        .expect_err("active");
    assert_eq!(e.class(), "ValueError");
    let e = store
        .restore_trimmed(&mut provider(), &ulid(1, "01K"), Trim::default())
        .expect_err("active");
    assert_eq!(e.class(), "NotTombstonedError");
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

fn counting() -> Counting {
    Counting {
        inner: provider(),
        wall: 0,
    }
}

/// Another connection holds the write lock until every call has returned;
/// the writer waits no time.
#[test]
fn the_checks_before_the_transaction_come_before_the_lock() {
    let dir = scratch("records-d-robust-locked");
    let mut store = fresh(&dir.0);
    store
        .put_tombstone(&mut provider(), &dead(1), PutTombstone::default())
        .expect("tombstone");
    store
        .connection()
        .busy_timeout(std::time::Duration::ZERO)
        .expect("no wait");
    let holder = rusqlite::Connection::open(dir.0.join("c").join("memory.sqlite")).expect("open");
    holder
        .execute_batch("BEGIN IMMEDIATE")
        .expect("hold the lock");
    let bogus = PyValue::str("bogus");
    let bad_links = PyValue::List(vec![PyValue::int(5)]);
    for (args, class) in [
        (
            PutTombstone {
                provenance: Some(&bogus),
                ..PutTombstone::default()
            },
            "ValueError",
        ),
        (
            PutTombstone {
                links: Some(&bad_links),
                ..PutTombstone::default()
            },
            "ValidationError",
        ),
        (PutTombstone::default(), "native:sqlite"),
    ] {
        let mut p = counting();
        let e = store
            .put_tombstone(&mut p, &dead(2), args)
            .expect_err("refused");
        assert_eq!(e.class(), class, "{e}");
        assert_eq!(p.wall, 0);
    }
    for (id, class) in [
        ("not-a-ulid", "MemoryNotFoundError"),
        (ulid(1, "01K").as_str(), "native:sqlite"),
    ] {
        let mut p = counting();
        let e = store
            .restore_trimmed(&mut p, id, Trim::default())
            .expect_err("refused");
        assert_eq!(e.class(), class, "{e}");
        assert_eq!(p.wall, 0);
    }
    let mut p = counting();
    let e = store
        .restore(&mut p, &ulid(1, "01K"), None, Relink::default())
        .expect_err("locked");
    assert_eq!(e.class(), "native:sqlite");
    assert_eq!(p.wall, 0);
    holder.execute_batch("ROLLBACK").expect("release");
}

/// A linking record whose tombstone row points at a payload nested past
/// the parser's limit: `_unknown_record_fields` raises RecursionError out
/// of the relink, so the restore refuses with that class and writes
/// nothing (on a 512 KiB thread).
#[test]
fn a_deep_payload_behind_a_linking_record_refuses_the_restore() {
    std::thread::Builder::new()
        .stack_size(512 * 1024)
        .spawn(|| {
            let dir = scratch("records-d-robust-unknown");
            let mut store = fresh(&dir.0);
            let linking = written::memory_payload(&ulid(2, "01K"), "linking", &["global"], &[]);
            let mut linking = linking.get("memory").expect("memory").clone();
            if let PyValue::Dict(items) = &mut linking {
                for (k, v) in items.iter_mut() {
                    if *k == PyValue::str("links") {
                        *v = written::payload(&format!(
                            "[{{\"type\": \"extends\", \"target_id\": \"{}\"}}]",
                            ulid(1, "01K")
                        ));
                    }
                }
            }
            let linking = Memory::from_payload(&linking).expect("valid");
            store
                .put_memory(&mut provider(), &linking, Put::default())
                .expect("put 2");
            store
                .put_tombstone(&mut provider(), &dead(1), PutTombstone::default())
                .expect("tombstone 1");
            store
                .put_tombstone(&mut provider(), &dead(3), PutTombstone::default())
                .expect("tombstone 3");
            let deep = format!("{{\"tombstone\": {}1{}}}", "[".repeat(100_000), "]".repeat(100_000));
            let conn = store.connection();
            conn.execute(
                "UPDATE log SET payload = ? WHERE mac = (SELECT log_mac FROM tombstones WHERE id = ?)",
                [deep.as_str(), ulid(3, "01K").as_str()],
            )
            .expect("deep payload");
            conn.execute(
                "UPDATE tombstones SET id = ? WHERE id = ?",
                [ulid(2, "01K").as_str(), ulid(3, "01K").as_str()],
            )
            .expect("moved");
            let before = log_len(&store);
            let e = store
                .restore(&mut provider(), &ulid(1, "01K"), None, Relink::default())
                .expect_err("refused");
            assert_eq!(e.class(), "RecursionError");
            assert_eq!(log_len(&store), before);
        })
        .expect("thread")
        .join()
        .expect("no overflow, no panic");
}

/// Every file of the store's directory and its keys, by name.
fn files_of(c: &Path) -> Vec<(String, Vec<u8>)> {
    let mut out = Vec::new();
    for dir in [c.to_path_buf(), c.join("keys")] {
        for entry in std::fs::read_dir(&dir).expect("dir").flatten() {
            if entry.file_type().expect("type").is_file() {
                let name = entry.path().display().to_string();
                out.push((name, std::fs::read(entry.path()).expect("read")));
            }
        }
    }
    out.sort();
    out
}

/// The relink's RecursionError at the parser's limit: a payload behind a
/// linking record nested 9,998 deep is read (the restore writes), 9,999
/// and 100,000 deep refuse, and after a refusal every file of the store
/// is byte for byte what it was before the call.
#[test]
fn the_relink_recursion_limit_and_a_refusal_leaving_every_byte() {
    std::thread::Builder::new()
        .stack_size(512 * 1024)
        .spawn(|| {
            for (depth, refused) in [(9_998usize, false), (9_999, true), (100_000, true)] {
                let dir = scratch("records-d-robust-limit");
                let mut store = fresh(&dir.0);
                let linking = written::memory_payload(&ulid(2, "01K"), "linking", &["global"], &[]);
                let mut linking = linking.get("memory").expect("memory").clone();
                if let PyValue::Dict(items) = &mut linking {
                    for (k, v) in items.iter_mut() {
                        if *k == PyValue::str("links") {
                            *v = written::payload(&format!(
                                "[{{\"type\": \"extends\", \"target_id\": \"{}\"}}]",
                                ulid(1, "01K")
                            ));
                        }
                    }
                }
                let linking = Memory::from_payload(&linking).expect("valid");
                store
                    .put_memory(&mut provider(), &linking, Put::default())
                    .expect("put 2");
                store
                    .put_tombstone(&mut provider(), &dead(1), PutTombstone::default())
                    .expect("tombstone 1");
                store
                    .put_tombstone(&mut provider(), &dead(3), PutTombstone::default())
                    .expect("tombstone 3");
                // `depth` levels in all: the object, then lists.
                let deep = format!(
                    "{{\"tombstone\": {}1{}}}",
                    "[".repeat(depth - 1),
                    "]".repeat(depth - 1)
                );
                let conn = store.connection();
                conn.execute(
                    "UPDATE log SET payload = ? WHERE mac = \
                     (SELECT log_mac FROM tombstones WHERE id = ?)",
                    [deep.as_str(), ulid(3, "01K").as_str()],
                )
                .expect("deep payload");
                conn.execute(
                    "UPDATE tombstones SET id = ? WHERE id = ?",
                    [ulid(2, "01K").as_str(), ulid(3, "01K").as_str()],
                )
                .expect("moved");
                conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE)")
                    .expect("checkpoint");
                let before = files_of(&dir.0.join("c"));
                let got = store.restore(&mut provider(), &ulid(1, "01K"), None, Relink::default());
                match (got, refused) {
                    (Err(e), true) => {
                        assert_eq!(e.class(), "RecursionError", "{depth}");
                        assert!(
                            files_of(&dir.0.join("c")) == before,
                            "{depth}: a byte changed"
                        );
                    }
                    // The linking record carries its tombstone row too: the
                    // put refuses it as removed, after the payload was read.
                    (Err(e), false) => assert_eq!(e.class(), "TombstonedError", "{depth}"),
                    (Ok(_), r) => panic!("{depth}: restored, refusal expected {r}"),
                }
            }
        })
        .expect("thread")
        .join()
        .expect("no overflow, no panic");
}

/// The relink reads provenance on the transaction's own connection: a row
/// the open transaction wrote, uncommitted, is seen with its label.
#[test]
fn a_conn_source_sees_its_own_transactions_rows() {
    use bettermemory_core::store::provenance::{ConnSource, Provenance};
    let dir = scratch("records-d-robust-connsource");
    let mut store = fresh(&dir.0);
    let c = dir.0.join("c");
    let ring =
        bettermemory_core::store::keys::load_key_ring(&c.join("keys"), &common::stores::store_id())
            .expect("ring");
    let mut p = provider();
    let mut tx = store.begin(&mut p).expect("begin");
    let id = ulid(7, "01K");
    let ask = |tx: &bettermemory_core::store::tx::Transaction<'_>| {
        let source = ConnSource(tx.connection());
        let reader = Provenance::new(&source, &ring, true, false).expect("reader");
        reader.provenance_for(&[id.as_str()]).expect("labels")
    };
    assert!(ask(&tx).is_empty(), "nothing before the write");
    let m = written::memory_payload(&id, "uncommitted", &["global"], &[]);
    let payload = PyValue::Dict(vec![
        (
            PyValue::str("memory"),
            m.get("memory").expect("memory").clone(),
        ),
        (PyValue::str("provenance"), PyValue::str("imported")),
        (PyValue::str("filename"), PyValue::None),
    ]);
    tx.mutate("memory_put", &payload, None).expect("put");
    assert_eq!(ask(&tx), vec![(id.clone(), "imported".to_owned())]);
    drop(tx);
    let read = bettermemory_core::store::StoreFile::open(&c.join("memory.sqlite")).expect("read");
    let after = Provenance::new(&read, &ring, true, false).expect("reader");
    assert!(
        after
            .provenance_for(&[id.as_str()])
            .expect("labels")
            .is_empty(),
        "rolled back"
    );
}
