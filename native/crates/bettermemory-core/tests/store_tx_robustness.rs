//! Hostile inputs and hostile states for a [`Transaction`]: each a typed
//! error and nothing written (the log, the record tables and the marks as
//! they were, the native check ok), or, where only a scope fails, the rest
//! of the transaction written; never a panic.

mod common;

use std::path::Path;

use bettermemory_core::store::create;
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::write::{WriteError, WriteNotPorted};
use common::created::{fixed, fixture};
use common::opened;
use common::stores::scratch;
use common::txs::Replay;
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

fn clock() -> create::Fixed {
    written::clock(&[instant("2001-02-03T11:00:00.000001Z")])
}

/// The log's length, and the record tables and marks as one text.
fn snapshot(store: &WritableStore) -> (i64, String) {
    let conn = store.connection();
    let n = conn
        .query_row("SELECT count(*) FROM log", [], |r| r.get(0))
        .expect("count");
    let mut text = String::new();
    for sql in [
        "SELECT id || ' ' || body FROM memories ORDER BY id",
        "SELECT source_id || ' ' || target_id FROM memory_links ORDER BY 1",
        "SELECT id FROM tombstones ORDER BY id",
        "SELECT record_id || ' ' || via FROM returned ORDER BY record_id",
    ] {
        let rows: Vec<String> = conn
            .prepare(sql)
            .and_then(|mut s| {
                s.query_map([], |r| r.get::<_, String>(0))?
                    .collect::<rusqlite::Result<_>>()
            })
            .unwrap_or_else(|e| vec![format!("({e})")]);
        text.push_str(&rows.join(";"));
        text.push('|');
    }
    (n, text)
}

fn put(n: u64, sup: &[u64]) -> bettermemory_core::pyvalue::PyValue {
    let sup: Vec<String> = sup.iter().map(|t| ulid(*t, "01J")).collect();
    let refs: Vec<&str> = sup.iter().map(String::as_str).collect();
    written::memory_payload(&ulid(n, "01J"), &format!("record {n}"), &["global"], &refs)
}

fn delete(n: u64) -> bettermemory_core::pyvalue::PyValue {
    written::payload(&format!("{{\"id\": \"{}\"}}", ulid(n, "01J")))
}

/// Two records, the second superseding the first: removing the second
/// marks the first.
fn superseded(store: &mut WritableStore) {
    let mut c = clock();
    let mut tx = store.begin(&mut c).expect("begin");
    tx.mutate("memory_put", &put(1, &[]), None).expect("put");
    tx.mutate("memory_put", &put(2, &[1]), None).expect("put");
    tx.commit().expect("commit");
}

fn verified(root: &Path) {
    let (status, _) = written::native_verify(&root.join("c"), &common::stores::store_id());
    assert_eq!(status, "ok");
}

#[test]
fn hostile_payloads_in_nested_scopes_roll_back_alone() {
    let dir = scratch("tx-robust-scopes");
    let mut store = fresh(&dir.0);
    superseded(&mut store);
    let deep = format!("{}1{}", "[".repeat(5000), "]".repeat(5000));
    let huge = format!(
        "{{\"memory\": {{\"id\": \"{}\", \"body\": \"{}\"}}, \"provenance\": \"local\"}}",
        ulid(9, "01J"),
        "x".repeat(1 << 20)
    );
    let hostile: Vec<(&str, String)> = vec![
        ("memory_put", "[1, 2]".into()),
        ("memory_put", "{}".into()),
        (
            "memory_put",
            "{\"memory\": {\"id\": 5}, \"provenance\": \"local\"}".into(),
        ),
        ("memory_put", format!("{{\"memory\": {deep}}}")),
        ("memory_put", huge),
        ("memory_delete", "{\"id\": \"\\udcff\"}".into()),
        ("tombstone_put", "{\"tombstone\": {}, \"links\": 5}".into()),
        (
            "returned_put",
            "{\"returned\": {\"record_id\": \"x\"}}".into(),
        ),
        ("home_put", "{}".into()),
    ];
    for (kind, text) in &hostile {
        let before = snapshot(&store);
        let mut c = clock();
        let mut tx = store.begin(&mut c).expect("begin");
        // The scope removes the superseding record (a mark due), then
        // meets the hostile payload: all of it rolls back.
        let failed = tx.scope(|tx| {
            tx.mutate("memory_delete", &delete(2), None)?;
            // Read as the reference's `json.loads` reads it (the test's own
            // tagged-value decoding is recursive and is left out).
            let payload = bettermemory_core::json::loads(&bettermemory_core::unicode::cps(text))
                .expect("a JSON text");
            tx.mutate(kind, &payload, None)
        });
        let e = failed.expect_err("a hostile payload is refused");
        let committed = tx.commit();
        let after = snapshot(&store);
        match e {
            WriteError::NotPorted(_) => {
                // The reference would have written: the whole transaction
                // is refused.
                let ce = committed.expect_err("a not-ported write refuses the commit");
                assert_eq!(ce.class(), "not_ported", "{kind}");
                assert_eq!(after, before, "{kind}: a refused transaction wrote");
            }
            other => {
                assert!(
                    matches!(other.class(), "refused" | "not_an_object"),
                    "{kind}: {other}"
                );
                let c = committed.expect("the outer transaction goes on");
                assert!(c.rows.is_empty(), "{kind}: the failed scope's rows kept");
                assert_eq!(after, before, "{kind}: the scope's writes kept");
            }
        }
    }
    // A scope that fails after a nested scope succeeded takes both back;
    // what came before it in the outer stays, and its mark is due.
    let mut c = clock();
    let mut tx = store.begin(&mut c).expect("begin");
    tx.mutate("memory_put", &put(3, &[]), None).expect("put");
    let failed: Result<(), WriteError> = tx.scope(|tx| {
        tx.scope(|tx| tx.mutate("memory_delete", &delete(2), None).map(drop))?;
        tx.mutate("memory_put", &put(4, &[1]), None)?;
        Err(WriteError::NotAnObject)
    });
    assert!(failed.is_err());
    let c = tx.commit().expect("commit");
    let kinds: Vec<&str> = c.rows.iter().map(|r| r.kind.as_str()).collect();
    assert_eq!(kinds, ["memory_put"], "only the outer put is kept");
    store.close().expect("close");
    verified(&dir.0);
}

#[test]
fn a_transaction_not_committed_writes_nothing() {
    let dir = scratch("tx-robust-drop");
    let mut store = fresh(&dir.0);
    superseded(&mut store);
    let before = snapshot(&store);
    // Dropped without a commit.
    {
        let mut c = clock();
        let mut tx = store.begin(&mut c).expect("begin");
        tx.mutate("memory_delete", &delete(2), None)
            .expect("delete");
        tx.mutate("memory_put", &put(5, &[]), None).expect("put");
        assert!(tx.release_returned(&ulid(1, "01J"), None).is_ok());
    }
    assert_eq!(snapshot(&store), before, "a dropped transaction wrote");
    // A failure in the outermost scope: the caller drops it.
    {
        let mut c = clock();
        let mut tx = store.begin(&mut c).expect("begin");
        tx.mutate("memory_put", &put(6, &[]), None).expect("put");
        let e = tx.mutate("memory_put", &written::payload("{}"), None);
        assert!(e.is_err());
    }
    assert_eq!(snapshot(&store), before);
    // Forgotten (no destructor runs): the connection holds a transaction
    // no call owns, which the next begin rolls back first.
    {
        let mut c = clock();
        let mut tx = store.begin(&mut c).expect("begin");
        tx.mutate("memory_put", &put(7, &[]), None).expect("put");
        std::mem::forget(tx);
    }
    assert!(!store.connection().is_autocommit());
    let mut c = clock();
    let tx = store
        .begin(&mut c)
        .expect("a begin after a forgotten transaction");
    let committed = tx.commit().expect("an empty commit");
    assert!(committed.rows.is_empty() && committed.head.is_none());
    assert_eq!(
        snapshot(&store),
        before,
        "the forgotten transaction's row kept"
    );
    store.close().expect("close");
    verified(&dir.0);
}

#[test]
fn a_second_writer_waits_and_is_refused_typed() {
    let dir = scratch("tx-robust-second");
    let mut store = fresh(&dir.0);
    let c = dir.0.join("c");
    let mut other = open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("a second open");
    other
        .connection()
        .busy_timeout(std::time::Duration::from_millis(50))
        .expect("busy");
    let mut clock_a = clock();
    let mut tx = store.begin(&mut clock_a).expect("begin");
    tx.mutate("memory_put", &put(1, &[]), None).expect("put");
    let mut clock_b = clock();
    match other.begin(&mut clock_b) {
        Err(WriteError::Sqlite(_)) => {}
        got => panic!("a second writer's begin: {got:?}"),
    }
    tx.commit().expect("the first commits");
    let mut tx = other.begin(&mut clock_b).expect("the second begins after");
    tx.mutate("memory_put", &put(2, &[]), None).expect("put");
    tx.commit().expect("the second commits");
    store.close().expect("close");
    other.close().expect("close");
    verified(&dir.0);
}

#[test]
fn a_returned_table_missing_or_misshapen() {
    let dir = scratch("tx-robust-table");
    let mut store = fresh(&dir.0);
    superseded(&mut store);
    let before = snapshot(&store);
    // Wrong columns: the settle's read or the mark's insert fails, typed,
    // and the transaction is rolled back.
    store
        .connection()
        .execute_batch(
            "DROP TABLE returned; CREATE TABLE returned(record_id TEXT PRIMARY KEY, other TEXT)",
        )
        .expect("misshape");
    let before_shape = snapshot(&store);
    let mut c = clock();
    let mut tx = store.begin(&mut c).expect("begin");
    tx.mutate("memory_delete", &delete(2), None)
        .expect("delete");
    let e = tx.commit().expect_err("a mark into a misshapen table");
    assert!(matches!(e.class(), "sqlite" | "refused"), "{e}");
    assert_eq!(snapshot(&store), before_shape);
    let mut c = clock();
    let mut tx = store.begin(&mut c).expect("begin");
    let e = tx
        .release_returned(&ulid(1, "01J"), None)
        .expect_err("a release reads the columns");
    assert_eq!(e.class(), "sqlite");
    drop(tx);
    assert_eq!(snapshot(&store), before_shape);
    // Missing: no mark is written, and a release finds none.
    store
        .connection()
        .execute_batch("DROP TABLE returned")
        .expect("drop");
    let mut c = clock();
    let mut tx = store.begin(&mut c).expect("begin");
    assert!(!tx.release_returned(&ulid(1, "01J"), None).expect("release"));
    tx.mutate("memory_delete", &delete(2), None)
        .expect("delete");
    let committed = tx.commit().expect("commit");
    let kinds: Vec<&str> = committed.rows.iter().map(|r| r.kind.as_str()).collect();
    assert_eq!(kinds, ["memory_delete"]);
    assert_ne!(snapshot(&store).0, before.0);
    store.close().expect("close");
}

#[test]
fn a_transaction_sqlite_ended_is_refused_in_the_reference_words() {
    let dir = scratch("tx-robust-ended");
    let mut store = fresh(&dir.0);
    superseded(&mut store);
    let before = snapshot(&store);
    let path = store.path().display().to_string();
    let mut c = clock();
    let mut tx = store.begin(&mut c).expect("begin");
    tx.mutate("memory_delete", &delete(2), None)
        .expect("delete");
    // As an error inside it would: the transaction ends under the writer.
    tx.connection().execute_batch("ROLLBACK").expect("end it");
    let scoped: Result<(), WriteError> = tx.scope(|_| Ok(()));
    assert_eq!(
        scoped.expect_err("a scope in an ended transaction").class(),
        "rolled_back"
    );
    let e = tx.commit().expect_err("an ended transaction");
    assert_eq!(
        e.to_string(),
        format!(
            "the write transaction on store {path} was rolled back by an error inside it; \
             nothing it wrote was kept"
        )
    );
    assert_eq!(snapshot(&store), before);
    store.close().expect("close");
    verified(&dir.0);
}

#[test]
fn a_clock_that_fails_or_a_rekey_leaves_nothing() {
    let dir = scratch("tx-robust-clock");
    let mut store = fresh(&dir.0);
    superseded(&mut store);
    let before = snapshot(&store);
    // The wall clock has no reading when the mark is stamped.
    let t = instant("2001-02-03T11:00:00.000001Z");
    let mut clocks = Replay::new(&[t, t, t], &[]);
    let mut tx = store.begin(&mut clocks).expect("begin");
    tx.mutate("memory_delete", &delete(2), None)
        .expect("delete");
    let e = tx.commit().expect_err("no wall clock");
    assert_eq!(e.class(), "clock");
    assert_eq!(snapshot(&store), before);
    // A native-path origin inside a scope refuses the whole transaction.
    let mut c = clock();
    let mut tx = store.begin(&mut c).expect("begin");
    tx.mutate("memory_put", &put(8, &[]), None).expect("put");
    let origin = written::payload(&format!(
        "{{\"memory\": {{\"id\": \"{}\", \"created\": \"2001-02-03T04:05:06.000000Z\", \
         \"updated\": \"2001-02-03T04:05:06.000000Z\", \"scopes\": [\"global\"], \
         \"confidence\": \"high\", \"source\": \"explicit-statement\", \"body\": \"b\", \
         \"origin\": {{\"cwd\": \"/w/\\udcff\"}}}}, \"provenance\": \"local\"}}",
        ulid(9, "01J")
    ));
    let e = tx
        .scope(|tx| tx.mutate("memory_put", &origin, None))
        .expect_err("a native origin");
    assert!(matches!(
        e,
        WriteError::NotPorted(WriteNotPorted::NativeOrigin)
    ));
    let e = tx.commit().expect_err("refused at the commit");
    assert_eq!(e.class(), "not_ported");
    assert_eq!(snapshot(&store), before);
    store.close().expect("close");
    verified(&dir.0);
}
