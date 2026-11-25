//! `WritableStore::reopen` held to what a plausible wrong reopen gets
//! wrong: claiming the key ring (a binding written), running the owner's
//! upkeep (a foreign tail closed, an anchor written, a restore's row
//! appended, the digest touched), binding a key the ring withholds,
//! keeping a reader's key that `_open_reader` would not, saying a drift
//! or a later writer twice in a process (or not again for another
//! minimum), taking the default busy timeout for this connection's, a
//! close that leaves the WAL behind, and the owner's refusal of a
//! ring bound elsewhere, which keeps a [`WritableStore`] from ever being
//! bound elsewhere (the reference's `_bound_elsewhere` branch of
//! `Store.reopen` is unreachable here).

#![cfg(unix)]

mod common;

use std::fs;
use std::path::{Path, PathBuf};

use bettermemory_core::store::key_read::KeyLog;
use bettermemory_core::store::key_state::KeyStatus;
use bettermemory_core::store::open::{
    open_for_writing, DigestOutcome, NotPorted, OpenError, Reopened, WritableStore, TOKENIZER_DRIFT,
};
use common::opened;
use common::stores::{scratch, store_id, Scratch};
use rusqlite::Connection;
use serde_json::{json, Value};

/// The `intact` base bound to its file, opened by its owner, then `edits`.
fn owner(name: &str, edits: &[Value]) -> (Scratch, PathBuf, WritableStore) {
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
    opened::setup(edits, &c, &store_id());
    (dir, c, store)
}

fn reopen(store: &WritableStore, logged: &mut Vec<KeyLog>) -> Reopened {
    store
        .reopen(&opened::engine_provider(), None, logged)
        .expect("reopened")
}

fn key_file(c: &Path, suffix: &str) -> PathBuf {
    c.join("keys").join(format!("{}{suffix}", store_id()))
}

fn last_seq(c: &Path) -> i64 {
    let conn = Connection::open(c.join("memory.sqlite")).expect("open");
    conn.query_row("SELECT max(seq) FROM log", [], |r| r.get(0))
        .expect("max")
}

#[test]
fn a_reopen_binds_no_ring() {
    let (_dir, c, store) = owner("reopen-unbound", &[json!({"unlink": ".path"})]);
    let mut logged = Vec::new();
    let other = reopen(&store, &mut logged);
    assert!(other.has_key(), "a present key binds the reopen");
    assert!(
        !key_file(&c, ".path").exists(),
        "the reopen wrote a binding"
    );
    other.close().expect("close");
    store.close().expect("close");
}

#[test]
fn a_reopen_runs_no_upkeep() {
    let mac8: String = {
        let dir = scratch("reopen-mac");
        let c = dir.0.join("c");
        opened::made("intact", &c);
        Connection::open(c.join("memory.sqlite"))
            .expect("open")
            .query_row("SELECT mac FROM log WHERE seq = 8", [], |r| r.get(0))
            .expect("mac")
    };
    let tail = format!(
        "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) VALUES (9, \
         '2001-02-03T04:05:06.000000Z', NULL, 'rekey', '{{\"new_fingerprint\":\"{}\",\
         \"old_fingerprint\":\"{}\"}}', '{mac8}', '{}');",
        "f".repeat(64),
        "e".repeat(64),
        "c".repeat(64)
    );
    let record = format!(
        "{{\"format\": \"bettermemory.backup_restore\", \"restores\": [{{\"kind\": \
         \"store_restored\", \"row\": null, \"restored_at\": \"2000-01-01T00:00:00.000000Z\", \
         \"bundle\": \"/b/1\", \"store_id\": \"{}\", \"head_before\": null, \"head_after\": \
         {{\"seq\": 9, \"mac\": \"{}\"}}, \"log_end_before\": null}}]}}\n",
        store_id(),
        "c".repeat(64)
    );
    let (_dir, c, store) = owner(
        "reopen-no-upkeep",
        &[
            json!({ "sql": tail }),
            json!({"unlink": ".origin"}),
            json!({"write": ".backup_restore.json", "hex": common::hex(record.as_bytes())}),
            json!({"sql": "DELETE FROM digest_state;"}),
        ],
    );
    let before = opened::state(&c, &c, &store_id(), &[]);
    let mut logged = Vec::new();
    let other = reopen(&store, &mut logged);
    let Reopened::Writer(writer) = &other else {
        panic!("a present key opens a writer: {other:?}")
    };
    assert_eq!(writer.digest(), &DigestOutcome::Untouched);
    assert!(writer.upkeep().is_empty() && !writer.bound());
    assert_eq!(last_seq(&c), 9, "a row was appended");
    assert!(!key_file(&c, ".origin").exists(), "an anchor was written");
    assert_eq!(
        fs::read_to_string(key_file(&c, ".backup_restore.json")).expect("record"),
        record
    );
    let after = opened::state(&c, &c, &store_id(), &[]);
    assert!(opened::differing(&before, &after).is_empty());
    assert!(logged.is_empty(), "{logged:?}");
    other.close().expect("close");
    store.close().expect("close");
}

#[test]
fn a_withheld_ring_opens_a_reader_with_a_checking_key() {
    let (_dir, _c, store) = owner(
        "reopen-elsewhere",
        &[
            json!({"copy_store": "other/memory.sqlite"}),
            json!({"bind": "other/memory.sqlite"}),
        ],
    );
    let mut logged = Vec::new();
    let other = reopen(&store, &mut logged);
    let Reopened::Reader(reader) = &other else {
        panic!("the key was bound for writing: {other:?}")
    };
    assert_eq!(reader.key_state(), KeyStatus::BoundElsewhere);
    assert!(!other.has_key() && other.can_verify());
    assert!(reader.keys().bound_elsewhere().is_some());
    assert_eq!(logged.len(), 1, "{logged:?}");
    other.close().expect("close");
    store.close().expect("close");
}

#[test]
fn a_reader_keeps_no_key_on_its_file_alone() {
    // Another key, no anchor, and a log it signs no segment of: none kept.
    let (_dir, _c, store) = owner(
        "reopen-foreign",
        &[
            json!({"write": ".key", "hex": "ee".repeat(32)}),
            json!({"unlink": ".origin"}),
        ],
    );
    let mut logged = Vec::new();
    let other = reopen(&store, &mut logged);
    assert!(!other.has_key());
    assert!(
        !other.can_verify(),
        "a read key kept for a key that signs nothing"
    );
    other.close().expect("close");
    // With the anchor there, the same key is kept for checking.
    let (_dir, _c, kept) = owner(
        "reopen-foreign-anchored",
        &[json!({"write": ".key", "hex": "ee".repeat(32)})],
    );
    let other = reopen(&kept, &mut logged);
    assert!(!other.has_key() && other.can_verify());
    other.close().expect("close");
    kept.close().expect("close");
    store.close().expect("close");
}

fn drift_sql() -> Value {
    json!({"sql": format!(
        "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) VALUES (9, \
         '2001-02-03T04:05:06.000000Z', NULL, 'index_rebuilt', \
         '{{\"new_fingerprint\":\"{}\"}}', 'p', '{}');",
        "5".repeat(64),
        "d".repeat(64)
    )})
}

#[test]
fn a_drift_and_a_later_writer_are_said_once_a_process_per_store() {
    let later = json!({"sql":
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('min_writer_revision', '99');"});
    let (_dir, _c, store) = owner("reopen-told", &[drift_sql(), later.clone()]);
    let mut first = Vec::new();
    let one = reopen(&store, &mut first);
    let formats: Vec<&str> = first.iter().map(|l| l.format).collect();
    assert_eq!(formats, ["%s", TOKENIZER_DRIFT], "{first:?}");
    one.close().expect("close");
    let mut again = Vec::new();
    reopen(&store, &mut again).close().expect("close");
    assert!(again.is_empty(), "said twice: {again:?}");
    // Another store's path is told on its own.
    let (_dir, _c, other_store) = owner("reopen-told-other", &[drift_sql(), later]);
    let mut other = Vec::new();
    reopen(&other_store, &mut other).close().expect("close");
    assert_eq!(other.len(), 2, "{other:?}");
    other_store.close().expect("close");
    store.close().expect("close");
}

#[test]
fn the_owner_refuses_a_ring_bound_elsewhere_so_no_reopen_inherits_one() {
    let dir = scratch("reopen-owner-elsewhere");
    let c = dir.0.join("c");
    opened::made("intact", &c);
    opened::setup(
        &[
            json!({"copy_store": "other/memory.sqlite"}),
            json!({"bind": "other/memory.sqlite"}),
        ],
        &c,
        &store_id(),
    );
    let got = open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    );
    assert!(
        matches!(
            got,
            Err(OpenError::NotPorted(NotPorted::BoundElsewhere { .. }))
        ),
        "{:?}",
        got.map(|_| ())
    );
}

#[test]
fn a_reopen_of_a_file_gone_is_not_found_and_a_bad_timeout_fails_it() {
    let (_dir, c, store) = owner("reopen-gone", &[]);
    let mut logged = Vec::new();
    match store.reopen(&opened::engine_provider(), Some(f64::NAN), &mut logged) {
        Err(e) => {
            assert_eq!(e.class(), "ValueError");
            assert_eq!(
                e.to_string(),
                "a busy timeout is a finite number of seconds, got nan"
            );
        }
        Ok(_) => panic!("a NaN timeout set"),
    }
    fs::rename(c.join("memory.sqlite"), c.join("moved.sqlite")).expect("move");
    match store.reopen(&opened::engine_provider(), None, &mut logged) {
        Err(OpenError::NotFound(path)) => assert!(path.ends_with("memory.sqlite")),
        other => panic!("{:?}", other.map(|_| ())),
    }
    store.close().expect("close");
}

#[test]
fn a_reopen_without_a_timeout_takes_this_connections() {
    let (_dir, _c, store) = owner("reopen-timeout", &[]);
    let mut logged = Vec::new();
    let first = store
        .reopen(&opened::engine_provider(), Some(1.5), &mut logged)
        .expect("reopened");
    let Reopened::Writer(writer) = &first else {
        panic!("a present key opens a writer: {first:?}")
    };
    let second = reopen(writer, &mut logged);
    assert_eq!(second.busy_timeout(), 1.5);
    let millis: i64 = second
        .connection()
        .query_row("PRAGMA busy_timeout", [], |r| r.get(0))
        .expect("pragma");
    assert_eq!(millis, 1500);
    second.close().expect("close");
    first.close().expect("close");
    store.close().expect("close");
}

#[test]
fn a_later_writer_is_said_again_for_another_minimum() {
    let at = |n: i64| {
        json!({"sql": format!(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('min_writer_revision', '{n}');"
        )})
    };
    let (_dir, c, store) = owner("reopen-told-minimum", &[at(99)]);
    let mut first = Vec::new();
    reopen(&store, &mut first).close().expect("close");
    assert_eq!(first.len(), 1, "{first:?}");
    opened::setup(&[at(100)], &c, &store_id());
    let mut again = Vec::new();
    reopen(&store, &mut again).close().expect("close");
    assert_eq!(again.len(), 1, "not said for the new minimum: {again:?}");
    assert_ne!(first[0].args, again[0].args);
    store.close().expect("close");
}

/// `_DRIFT_TOLD` holds the stored fingerprint: a later `index_rebuilt`
/// row naming another tokenizer is said again for the same store, then
/// not a third time.
#[test]
fn a_drift_is_said_again_for_another_stored_tokenizer() {
    let (_dir, c, store) = owner("reopen-told-stored", &[drift_sql()]);
    let mut first = Vec::new();
    reopen(&store, &mut first).close().expect("close");
    assert_eq!(first.len(), 1, "{first:?}");
    opened::setup(
        &[json!({"sql": format!(
            "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) VALUES (10, \
             '2001-02-03T04:05:06.000000Z', NULL, 'index_rebuilt', \
             '{{\"new_fingerprint\":\"{}\"}}', '{}', '{}');",
            "6".repeat(64),
            "d".repeat(64),
            "e".repeat(64)
        )})],
        &c,
        &store_id(),
    );
    let mut again = Vec::new();
    reopen(&store, &mut again).close().expect("close");
    assert_eq!(
        again.len(),
        1,
        "not said for the new stored tokenizer: {again:?}"
    );
    assert_eq!(again[0].format, TOKENIZER_DRIFT);
    assert_ne!(first[0].args, again[0].args);
    let mut third = Vec::new();
    reopen(&store, &mut third).close().expect("close");
    assert!(third.is_empty(), "said twice: {third:?}");
    store.close().expect("close");
}

/// The reopen's connection checkpoints at its close as any writing
/// connection does: the last of the two to close leaves no `-wal`.
#[test]
fn the_last_connection_to_close_checkpoints_the_wal() {
    let (_dir, c, store) = owner("reopen-wal", &[]);
    let mut logged = Vec::new();
    let other = reopen(&store, &mut logged);
    store.close().expect("close");
    other.close().expect("close");
    assert!(
        !c.join("memory.sqlite-wal").exists(),
        "the reopen's close left the WAL"
    );
}
