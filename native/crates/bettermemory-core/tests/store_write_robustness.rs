//! Hostile inputs and hostile states for `WritableStore::mutate`: each a
//! typed error and the store as it was (the log's length, the native
//! check ok), or, where the reference writes, a write that verifies; never
//! a panic.

mod common;

use std::path::Path;

use bettermemory_core::store::create;
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::write::{HeadMove, WriteError, WriteNotPorted};
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

fn rows(store: &WritableStore) -> i64 {
    store
        .connection()
        .query_row("SELECT count(*) FROM log", [], |r| r.get(0))
        .expect("count")
}

fn clock() -> bettermemory_core::store::create::Fixed {
    written::clock(&[instant("2001-02-03T11:00:00.000001Z")])
}

#[test]
fn hostile_inputs_are_typed_errors_with_nothing_written() {
    let dir = scratch("write-robust");
    let mut store = fresh(&dir.0);
    let before = rows(&store);
    let cases: Vec<(&str, &str, &str)> = vec![
        ("memory_put", "[1, 2]", "not_an_object"),
        ("memory_put", "\"text\"", "not_an_object"),
        ("memory_put", "{}", "refused"),
        (
            "memory_put",
            "{\"memory\": null, \"provenance\": \"local\"}",
            "refused",
        ),
        (
            "memory_put",
            "{\"memory\": {\"id\": 5}, \"provenance\": \"local\"}",
            "refused",
        ),
        ("home_put", "{\"home\": {}}", "not_ported"),
        ("native_member_put", "{}", "not_ported"),
        ("", "{}", "not_ported"),
        ("memory_put\u{0}", "{}", "not_ported"),
        ("rekey", "{}", "not_ported"),
    ];
    for (kind, text, class) in cases {
        let got = store.mutate(kind, &written::payload(text), None, &mut clock());
        match got {
            Err(e) => assert_eq!(e.class(), class, "{kind} {text}: {e}"),
            Ok(w) => panic!("{kind} {text} wrote {:?}", w.row.seq),
        }
        assert_eq!(rows(&store), before, "{kind} {text} wrote");
    }
    // `str(value)` of what is not text, as the reference's `str()` takes
    // it: written, not refused (`{"id": null}` removes the id "None").
    store
        .mutate(
            "memory_delete",
            &written::payload("{\"id\": null}"),
            None,
            &mut clock(),
        )
        .expect("str(None)");
    let before = before + 1;
    // An enormous body: the reference writes it; so does native.
    let big = "word ".repeat(400_000);
    let p = written::memory_payload(&ulid(1, "01J"), &big, &["global"], &[]);
    store
        .mutate("memory_put", &p, None, &mut clock())
        .expect("a big record");
    // A surrogate in an id the reference binds before writing.
    let p = written::payload("{\"id\": \"\\udcff\"}");
    let e = store
        .mutate("memory_delete", &p, None, &mut clock())
        .expect_err("surrogate");
    assert_eq!(e.class(), "refused");
    // A clock that does not read: rolled back.
    let mut broken = written::clock(&[]);
    let p = written::memory_payload(&ulid(2, "01J"), "b", &["global"], &[]);
    assert!(matches!(
        store.mutate("memory_put", &p, None, &mut broken),
        Err(WriteError::Clock(_))
    ));
    assert_eq!(rows(&store), before + 1);
    let c = dir.0.join("c");
    store.close().expect("close");
    let (status, n) = written::native_verify(&c, &common::stores::store_id());
    assert_eq!((status, n), ("ok", 1));
}

#[test]
fn a_schema_altered_after_the_open_refuses_with_nothing_written() {
    let dir = scratch("write-schema");
    let mut store = fresh(&dir.0);
    let file = dir.0.join("c").join("memory.sqlite");
    let other = rusqlite::Connection::open(&file).expect("other");
    other
        .execute_batch("DROP TABLE memory_links")
        .expect("drop");
    drop(other);
    let before = rows(&store);
    let p = written::memory_payload(&ulid(1, "01J"), "body", &["global"], &[]);
    let e = store
        .mutate("memory_put", &p, None, &mut clock())
        .expect_err("refused");
    assert!(
        matches!(e, WriteError::Refused { .. } | WriteError::Sqlite(_)),
        "{e:?}"
    );
    assert_eq!(rows(&store), before);
    let n: i64 = store
        .connection()
        .query_row("SELECT count(*) FROM memories", [], |r| r.get(0))
        .expect("count");
    assert_eq!(n, 0);
}

#[test]
fn a_write_while_another_holds_the_lock_waits_or_fails_typed() {
    let dir = scratch("write-busy");
    let mut store = fresh(&dir.0);
    let file = dir.0.join("c").join("memory.sqlite");
    let other = rusqlite::Connection::open(&file).expect("other");
    other.execute_batch("BEGIN IMMEDIATE").expect("lock");
    store
        .connection()
        .busy_timeout(std::time::Duration::from_millis(50))
        .expect("timeout");
    let p = written::memory_payload(&ulid(1, "01J"), "body", &["global"], &[]);
    let e = store
        .mutate("memory_put", &p, None, &mut clock())
        .expect_err("busy");
    assert!(matches!(e, WriteError::Sqlite(_)), "{e:?}");
    other.execute_batch("ROLLBACK").expect("unlock");
    store
        .mutate("memory_put", &p, None, &mut clock())
        .expect("then it writes");
}

#[test]
fn the_kind_refusal_names_the_kind() {
    let dir = scratch("write-kind");
    let mut store = fresh(&dir.0);
    match store.mutate("standing_put", &written::payload("{}"), None, &mut clock()) {
        Err(WriteError::NotPorted(WriteNotPorted::Kind(k))) => assert_eq!(k, "standing_put"),
        other => panic!("{other:?}"),
    }
}

/// The head cannot move (a keys directory that refuses writes): the
/// commit stands and the head is left behind, as `_move_head` logs an
/// OSError and the call succeeds; the chain verifies ok.
#[cfg(unix)]
#[test]
fn a_head_that_cannot_move_leaves_the_commit() {
    use std::os::unix::fs::PermissionsExt;
    let dir = scratch("write-head");
    let mut store = fresh(&dir.0);
    let keys = dir.0.join("c").join("keys");
    std::fs::set_permissions(&keys, std::fs::Permissions::from_mode(0o500)).expect("chmod");
    let p = written::memory_payload(&ulid(1, "01J"), "body", &["global"], &[]);
    let w = store.mutate("memory_put", &p, None, &mut clock());
    std::fs::set_permissions(&keys, std::fs::Permissions::from_mode(0o700)).expect("chmod back");
    let w = w.expect("committed");
    assert!(matches!(w.head, HeadMove::Failed(_)), "{:?}", w.head);
    store.close().expect("close");
    let (status, n) = written::native_verify(&dir.0.join("c"), &common::stores::store_id());
    assert_eq!((status, n), ("ok", 1));
}

/// Throughput: 5,000 `memory_put`s, one transaction each, the product's
/// durability settings (WAL, `synchronous = FULL`, the head moved and
/// synced after each commit). `native/tools/bench_write.py` writes the
/// same payloads with the reference at the same level. Run with
/// `cargo test --release --test store_write_robustness -- --ignored`.
#[test]
#[ignore = "a measurement"]
fn bench_five_thousand_puts() {
    let dir = scratch("write-bench");
    let mut store = fresh(&dir.0);
    let payloads: Vec<_> = (0..5000u64)
        .map(|k| {
            written::memory_payload(
                &ulid(k, "01J"),
                &format!("benchmark record {k} about sqlite docker and staging"),
                &["global", "bench"],
                &[],
            )
        })
        .collect();
    let mut clock = clock();
    use bettermemory_core::store::write::WriteBoundary;
    let mut spent = [0f64; 6];
    let start = std::time::Instant::now();
    for p in &payloads {
        let mut last = std::time::Instant::now();
        let mut hook = |b: WriteBoundary| {
            if let Some(i) = WriteBoundary::ALL.iter().position(|x| *x == b) {
                spent[i] += last.elapsed().as_secs_f64();
            }
            last = std::time::Instant::now();
        };
        store
            .mutate_with("memory_put", p, Some("bench"), &mut clock, &mut hook)
            .expect("write");
    }
    let secs = start.elapsed().as_secs_f64();
    eprintln!(
        "native: 5000 puts in {secs:.3} s, {:.0} per second",
        5000.0 / secs
    );
    for (b, t) in WriteBoundary::ALL.iter().zip(spent) {
        eprintln!("  up to {b:?}: {t:.3} s");
    }
}

fn synchronous(conn: &rusqlite::Connection) -> i64 {
    conn.query_row("PRAGMA synchronous", [], |r| r.get(0))
        .expect("synchronous")
}

/// The writing connection syncs at every COMMIT (`synchronous = FULL`,
/// 2), read back on the connection that commits: create's, before it
/// closes; open's, after the open and again after a write committed and
/// its head moved (ENG-328).
#[test]
fn the_writing_connection_is_synchronous_full() {
    let dir = scratch("write-sync");
    let c = dir.0.join("c");
    let made = create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    assert_eq!(made.synchronous, 2, "create's connection");
    let mut store = open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("open");
    assert_eq!(synchronous(store.connection()), 2, "open's connection");
    let journal: String = store
        .connection()
        .query_row("PRAGMA journal_mode", [], |r| r.get(0))
        .expect("journal_mode");
    assert_eq!(journal, "wal");
    let p = written::memory_payload(&ulid(1, "01J"), "body", &["global"], &[]);
    let w = store
        .mutate("memory_put", &p, None, &mut clock())
        .expect("committed");
    assert!(matches!(w.head, HeadMove::Moved), "{:?}", w.head);
    assert_eq!(synchronous(store.connection()), 2, "after a commit");
    store.close().expect("close");
}

/// A reader's connection keeps SQLite's default: the setting is the
/// writing connection's alone.
#[test]
fn a_readers_connection_keeps_the_default() {
    let dir = scratch("read-sync");
    let c = dir.0.join("c");
    create::create(&c, &c.join("keys"), &mut fixed(&fixture())).expect("create");
    let file = c.join("memory.sqlite");
    let plain = rusqlite::Connection::open_in_memory().expect("memory");
    let default = synchronous(&plain);
    let reader = bettermemory_core::store::StoreFile::open(&file).expect("open");
    assert_eq!(synchronous(reader.connection()), default);
}
