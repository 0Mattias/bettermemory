//! Native event writes against `store_record_event_posix.jsonl`: every case
//! replayed from the store `store_create.json` pins, opened for writing
//! natively, then each step as the generator ran it: `record_event` (a
//! second connection holding `BEGIN IMMEDIATE` for a real SQLITE_BUSY,
//! `PRAGMA query_only` for SQLITE_READONLY, the monotonic readings replayed
//! exactly, the pauses counted and the blocker committed at the pause the
//! step names), `record_event` inside a transaction, `import_event`,
//! `imported_event_keys`, the importer's re-run, `record_migration` and
//! `record_capture`. Each step's outcome, log levels, clock readings,
//! pauses, rows appended (every column) and busy timeout after equal the
//! reference's. POSIX only, as the fixture is;
//! `store_record_event_not_posix.rs` records the gap elsewhere.

#![cfg(unix)]

mod common;

use std::collections::{BTreeSet, VecDeque};
use std::io;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::Path;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::event_hook::BusyTimeout;
use bettermemory_core::json::{canonical_payload, loads};
use bettermemory_core::log::{Key, LogRow};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::recall_prompt::PyException;
use bettermemory_core::store::create::Provider;
use bettermemory_core::store::exception::StoreException;
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::record_event::{
    import_event, imported_event_keys, record_capture, record_event, record_migration,
    CaptureGuard, EventCall, EventError, EventLogger, EventTransaction, Logged,
};
use bettermemory_core::store::rows::event_import_payload;
use bettermemory_core::store::tx::Transaction;
use bettermemory_core::store::write::WriteError;
use bettermemory_core::unicode::{cps, to_string};
use common::opened;
use common::parity::Ledger;
use common::stores::scratch;
use common::written;
use rusqlite::Connection;
use serde_json::{json, Value};

/// The step's clocks replayed, the pauses counted, the blocker released.
#[derive(Default)]
struct Clocks {
    log: VecDeque<DateTime>,
    mono: VecDeque<f64>,
    pauses: usize,
    release: Option<usize>,
    blocker: Option<Connection>,
    interrupt_stamp: bool,
    clock_failure: Option<String>,
    head_failure: Option<String>,
    reads: usize,
}

impl Provider for Clocks {
    fn store_id(&mut self) -> io::Result<String> {
        Err(io::Error::other("no store id"))
    }
    fn key(&mut self) -> io::Result<Key> {
        Err(io::Error::other("no key"))
    }
    fn membership_store_id(&mut self) -> io::Result<String> {
        Err(io::Error::other("no membership id"))
    }
    fn now(&mut self) -> io::Result<DateTime> {
        assert!(!self.interrupt_stamp, "interrupted at event timestamp");
        self.reads += 1;
        if self.reads == 2 {
            if let Some(text) = self.head_failure.take() {
                return Err(io::Error::other(text));
            }
        }
        if let Some(text) = &self.clock_failure {
            return Err(io::Error::other(text.clone()));
        }
        self.log
            .pop_front()
            .ok_or_else(|| io::Error::other("the log clock was read more often than fixed"))
    }
    fn wall_now(&mut self) -> io::Result<DateTime> {
        Err(io::Error::other("the wall clock was read"))
    }
    fn engine_version(&self) -> String {
        opened::ENGINE.to_owned()
    }
    fn monotonic(&mut self) -> io::Result<f64> {
        self.mono
            .pop_front()
            .ok_or_else(|| io::Error::other("the monotonic clock was read more often"))
    }
    fn pause(&mut self, seconds: f64) {
        assert_eq!(seconds, 0.05);
        self.pauses += 1;
        if self.release == Some(self.pauses) {
            if let Some(b) = &self.blocker {
                b.execute_batch("COMMIT").expect("commit the blocker");
            }
        }
    }
}

fn items(v: &Value) -> Vec<(PyValue, PyValue)> {
    match &loads(&cps(&v.to_string())).expect("json") {
        PyValue::Dict(items) => items.clone(),
        other => panic!("not an object: {other:?}"),
    }
}

fn refuse_guard(_: &Transaction<'_>) -> Result<(), PyException> {
    Err(PyException {
        class: "Guarded".to_owned(),
        message: cps("refused"),
    })
}

fn pass_guard(tx: &Transaction<'_>) -> Result<(), PyException> {
    assert!(!tx.connection().is_autocommit());
    Ok(())
}

fn no_spans(_: &[u32]) -> Vec<(usize, usize, Vec<u32>)> {
    Vec::new()
}

fn try_event(
    store: &mut WritableStore,
    clocks: &mut dyn Provider,
    best_effort: bool,
) -> (Result<Option<LogRow>, EventError>, Vec<Logged>) {
    let call = EventCall {
        kind: &cps("search"),
        session: None,
        best_effort,
        attempts: 2,
        deadline: None,
    };
    let mut log = Vec::new();
    let result = record_event(
        Ok(EventTransaction::Store(store, clocks)),
        &call,
        &[],
        None,
        &mut no_spans,
        &mut log,
    );
    (result, log)
}

#[test]
fn writer_rechecks_meta_before_signing_and_propagates_the_reference_error() {
    let dir = scratch("event-write-revision");
    let case = dir.0.join("a\"b");
    opened::made("created", &case);
    let file = case.join("memory.sqlite");
    let mut clocks = opened::engine_provider();
    clocks
        .clock
        .push(written::instant("2025-03-04T00:00:00+00:00"));
    let mut store = open_for_writing(&file, &case.join("keys"), &clocks).unwrap();
    let log_size = |c: &Connection| -> i64 {
        c.query_row("SELECT count(*) FROM log", [], |r| r.get(0))
            .unwrap()
    };
    store
        .connection()
        .execute_batch(
            "CREATE TEMP TABLE meta(key, value); INSERT INTO temp.meta SELECT * FROM main.meta;",
        )
        .unwrap();
    let set_value = |conn: &Connection, sql: &str| {
        conn.execute_batch(
            "PRAGMA query_only = OFF; DELETE FROM temp.meta WHERE key = 'min_writer_revision'",
        )
        .unwrap();
        if !sql.is_empty() {
            conn.execute_batch(&format!(
                "INSERT INTO temp.meta VALUES ('min_writer_revision', {sql})"
            ))
            .unwrap();
        }
    };
    for value in ["", "0", "'000006'", "6"] {
        set_value(store.connection(), value);
        let tx = store
            .begin(&mut clocks)
            .expect("missing or current revision");
        tx.commit().unwrap();
    }

    for (sql, raw) in [
        ("''", ""),
        ("'x'", "x"),
        ("'+6'", "+6"),
        ("'-1'", "-1"),
        ("'٣'", "٣"),
        ("'1000000000'", "1000000000"),
        ("NULL", "None"),
        ("2.5", "2.5"),
        ("X'36'", "b'6'"),
    ] {
        set_value(store.connection(), sql);
        for best_effort in [true, false] {
            let initial = log_size(store.connection());
            let (result, log) = try_event(&mut store, &mut clocks, best_effort);
            let Err(EventError::Write(WriteError::Version(e))) = result else {
                panic!("expected StoreVersionError for {sql}")
            };
            assert_eq!(e.key, Some("min_writer_revision"));
            assert_eq!(e.needed, None);
            let expected = format!(
                "{} carries a min_writer_revision that is not a whole number ('{raw}'); the store \
                 is not opened: set meta's min_writer_revision back to a whole number, or restore \
                 the store from a backup taken before the edit",
                file.display()
            );
            assert_eq!(e.message, expected);
            assert!(log.is_empty());
            assert!(store.connection().is_autocommit());
            assert_eq!(log_size(store.connection()), initial);
            let query_only: i64 = store
                .connection()
                .query_row("PRAGMA query_only", [], |r| r.get(0))
                .unwrap();
            assert_eq!(query_only, 0);
        }
    }
    for best_effort in [true, false] {
        set_value(store.connection(), "'000000999'");
        let initial = log_size(store.connection());
        let (result, log) = try_event(&mut store, &mut clocks, best_effort);
        let quoted = serde_json::to_string(file.to_str().unwrap()).unwrap();
        let expected = format!(
            "attempt to write a readonly database: {quoted} now needs a writer at \
            schema revision 999; this build writes 6. Upgrade bettermemory and reopen the store."
        );
        if best_effort {
            assert!(result.unwrap().is_none());
            assert_eq!(log, [Logged::NotRecorded(cps(&expected))]);
        } else {
            let Err(EventError::Write(error @ WriteError::LaterWriter { .. })) = result else {
                panic!("expected later writer error")
            };
            assert_eq!(error.to_string(), expected);
            assert!(bettermemory_core::store::record_event::sqlite_code(&error).is_none());
            assert!(log.is_empty());
        }
        assert!(store.connection().is_autocommit());
        assert_eq!(log_size(store.connection()), initial);
        let query_only: i64 = store
            .connection()
            .query_row("PRAGMA query_only", [], |r| r.get(0))
            .unwrap();
        assert_eq!(query_only, 1);
    }
    set_value(store.connection(), "CAST(X'c3a9ff00ff' AS TEXT)");
    let decode = "Could not decode to UTF-8 column 'value' with text '���'";
    let (result, log) = try_event(&mut store, &mut clocks, true);
    assert!(result.unwrap().is_none());
    assert_eq!(log, [Logged::NotRecorded(cps(decode))]);
    let (strict, log) = try_event(&mut store, &mut clocks, false);
    let Err(EventError::Write(WriteError::Refused { class, text })) = strict else {
        panic!("expected an uncoded OperationalError");
    };
    assert_eq!(class, "OperationalError");
    assert_eq!(text.as_deref(), Some(decode));
    assert!(log.is_empty());
    assert!(store.connection().is_autocommit());

    store
        .connection()
        .execute_batch("DROP TABLE temp.meta; ALTER TABLE main.meta RENAME TO moved_meta;")
        .unwrap();
    let err = match store.begin(&mut clocks) {
        Err(e) => e,
        Ok(_) => panic!("expected SQLite schema error"),
    };
    assert_eq!(
        bettermemory_core::store::record_event::sqlite_code(&err),
        Some(1)
    );
    assert_eq!(err.to_string(), "no such table: meta");
    assert!(matches!(err, WriteError::Sqlite(_)));
    let (result, log) = try_event(&mut store, &mut clocks, true);
    assert!(result.unwrap().is_none());
    assert_eq!(log, [Logged::NotRecorded(cps("no such table: meta"))]);
    assert!(store.connection().is_autocommit());
}

#[test]
fn the_writing_opens_digest_reports_the_same_writer_check() {
    use bettermemory_core::store::open::{open_for_writing_with, Boundary, DigestOutcome};
    let dir = scratch("digest-writer-revision");
    let case = dir.0.join("c");
    opened::made("created", &case);
    let file = case.join("memory.sqlite");
    let store = open_for_writing_with(
        &file, &case.join("keys"), &opened::engine_provider(), &mut |stage| {
            if stage == Boundary::Bound {
                Connection::open(&file).unwrap().execute_batch(
                    "DELETE FROM digest_state; UPDATE meta SET value = 'bad' WHERE key = 'min_writer_revision';"
                ).unwrap();
            }
        }
    ).unwrap();
    let DigestOutcome::Left(message) = store.digest() else {
        panic!("digest should decline")
    };
    assert!(message.starts_with(&format!("{} carries a min_writer_revision", file.display())));
    assert!(message.contains("not a whole number ('bad')"));
    assert!(store.connection().is_autocommit());
}

#[test]
fn an_interrupted_callback_restores_the_wait_and_rolls_back_its_scope() {
    let dir = scratch("interrupted-event");
    let case = dir.0.join("c");
    opened::made("created", &case);
    let mut provider = opened::engine_provider();
    let mut store = open_for_writing(&case.join("memory.sqlite"), &case.join("keys"), &provider)
        .expect("open store");
    let mut clocks = Clocks {
        mono: [0.0].into(),
        interrupt_stamp: true,
        ..Clocks::default()
    };
    let call = EventCall {
        kind: &cps("search"),
        session: None,
        best_effort: true,
        attempts: 1,
        deadline: Some(0.002),
    };
    let held = store.busy_timeout();
    assert!(catch_unwind(AssertUnwindSafe(|| record_event(
        Ok(EventTransaction::Store(&mut store, &mut clocks)),
        &call,
        &[],
        None,
        &mut no_spans,
        &mut Vec::new(),
    )))
    .is_err());
    assert!(store.connection().is_autocommit());
    assert_eq!(store.busy_timeout(), held);
    let ms: i64 = store
        .connection()
        .query_row("PRAGMA busy_timeout", [], |r| r.get(0))
        .unwrap();
    assert_eq!(ms, 5000);

    let mut tx = store
        .begin(&mut provider)
        .expect("begin enclosing transaction");
    let mut guard = |t: &Transaction<'_>| -> Result<(), PyException> {
        t.connection()
            .execute_batch("CREATE TEMP TABLE capture_guard(value)")
            .unwrap();
        panic!("interrupted in capture guard");
    };
    assert!(catch_unwind(AssertUnwindSafe(|| record_capture(
        EventTransaction::Joined(&mut tx),
        &cps("capture_undo"),
        &[],
        None,
        None,
        Some(&mut guard),
    )))
    .is_err());
    let remaining: i64 = tx
        .connection()
        .query_row(
            "SELECT count(*) FROM sqlite_temp_master WHERE name = 'capture_guard'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(remaining, 0);
    tx.commit()
        .expect("the enclosing transaction still commits");
}

fn outcome(r: Result<Option<LogRow>, EventError>) -> Value {
    match r {
        Ok(row) => json!({"seq": row.map(|r| r.seq)}),
        Err(EventError::Value(t)) => json!({"raised": "ValueError", "text": to_string(&t)}),
        Err(
            EventError::Guard(error)
            | EventError::Log(StoreException {
                exception: error, ..
            })
            | EventError::Write(WriteError::Callback(StoreException {
                exception: error, ..
            })),
        ) => match to_string(&error.message) {
            Some(text) => json!({"raised": error.class, "text": text}),
            None => json!({"raised": error.class, "text_cps": error.message}),
        },
        Err(EventError::Write(WriteError::Sqlite(_))) => {
            json!({"raised": "OperationalError", "text": null})
        }
        Err(EventError::Write(WriteError::Refused { class, text })) => {
            json!({"raised": class, "text":text})
        }
        Err(EventError::Write(e)) => json!({"raised": e.class(), "text": e.to_string()}),
    }
}

fn levels(log: &[Logged]) -> Vec<&'static str> {
    log.iter()
        .map(|l| match l {
            Logged::Retry { .. } => "INFO",
            Logged::NotRecorded(_) | Logged::HeadCheckpoint { .. } => "WARNING",
        })
        .collect()
}

fn column(step: &Value, field: &str) -> Option<Vec<u32>> {
    if let Some(points) = step.get(format!("{field}_codes")).and_then(Value::as_array) {
        Some(
            points
                .iter()
                .map(|cp| u32::try_from(cp.as_u64().expect("code point")).unwrap())
                .collect(),
        )
    } else {
        step[field].as_str().map(cps)
    }
}

struct StepLogger<'a> {
    entries: &'a mut Vec<Logged>,
    fail: Option<Vec<u32>>,
    observed: Vec<(Vec<u32>, Logged)>,
    observe: bool,
}

impl EventLogger for StepLogger<'_> {
    fn emit(&mut self, kind: &[u32], entry: Logged) -> Result<(), PyException> {
        if self.observe {
            self.observed.push((kind.to_vec(), entry.clone()));
        }
        self.entries.push(entry);
        if let Some(message) = &self.fail {
            return Err(PyException {
                class: "LogFailure".to_owned(),
                message: message.clone(),
            });
        }
        Ok(())
    }
}

fn event(
    target: Result<EventTransaction<'_, '_>, &[u32]>,
    step: &Value,
    log: &mut Vec<Logged>,
) -> Value {
    let kind = column(step, "kind").expect("kind");
    let session = column(step, "session");
    let call = EventCall {
        kind: &kind,
        session: session.as_deref(),
        best_effort: step
            .get("best_effort")
            .and_then(Value::as_bool)
            .unwrap_or(true),
        attempts: step.get("attempts").and_then(Value::as_i64).unwrap_or(1),
        deadline: step.get("deadline").and_then(Value::as_f64),
    };
    let actor = step.get("actor").map(items);
    let mut logger = StepLogger {
        entries: log,
        fail: column(step, "log_failure"),
        observed: Vec::new(),
        observe: step["check_reason"] == true,
    };
    let result = record_event(
        target,
        &call,
        &items(&step["fields"]),
        actor.as_deref(),
        &mut no_spans,
        &mut logger,
    );
    let mut out = outcome(result);
    if logger.observe {
        let messages: Vec<_> = logger
            .observed
            .into_iter()
            .map(|(kind, entry)| {
                let reason = match entry {
                    Logged::Retry { reason, .. } => reason,
                    Logged::HeadCheckpoint { error, .. } => error.exception.message,
                    Logged::NotRecorded(reason) => reason,
                };
                json!([kind, reason])
            })
            .collect();
        out["logged_arguments"] = json!(messages);
    }
    out
}

fn key_values(keys: &BTreeSet<(String, Option<String>, String, String)>) -> BTreeSet<String> {
    keys.iter()
        .map(|(t, s, k, p)| json!([t, s, k, p]).to_string())
        .collect()
}

fn busy_ms(store: &WritableStore) -> i64 {
    store
        .connection()
        .query_row("PRAGMA busy_timeout", [], |r| r.get(0))
        .expect("busy timeout")
}

fn run_step(
    store: &mut WritableStore,
    file: &Path,
    clocks: &mut Clocks,
    step: &Value,
) -> (Value, Vec<Logged>) {
    let mut log = Vec::new();
    let mut out = json!({});
    match step["op"].as_str().expect("op") {
        "event" => {
            if step["busy"].as_bool() == Some(true) {
                let b = Connection::open(file).expect("blocker");
                b.execute_batch("BEGIN IMMEDIATE").expect("hold the lock");
                clocks.blocker = Some(b);
            }
            clocks.release = step
                .get("release")
                .and_then(Value::as_u64)
                .map(|n| n as usize);
            let held = store.busy_timeout().expect("held");
            if let Some(held) = step.get("held").and_then(Value::as_f64) {
                store.set_busy_timeout(held).expect("set timeout");
            }
            if step["zero"].as_bool() == Some(true) {
                store.set_busy_timeout(0.0).expect("zero");
            }
            if step["sample_wait"].as_bool() == Some(true) {
                store
                    .connection()
                    .execute_batch(
                        "CREATE TEMP TABLE wait_samples(ms INTEGER);
                    CREATE TEMP TRIGGER sample_wait BEFORE INSERT ON log BEGIN
                    INSERT INTO wait_samples SELECT timeout FROM pragma_busy_timeout; END;",
                    )
                    .unwrap();
            }
            if step["readonly"].as_bool() == Some(true) {
                store
                    .connection()
                    .execute_batch("PRAGMA query_only = 1")
                    .expect("ro");
            }
            let result = if step["keyless"].as_bool() == Some(true) {
                event(Err(&cps("the key state refused the write")), step, &mut log)
            } else {
                event(Ok(EventTransaction::Store(store, clocks)), step, &mut log)
            };
            out["outcome"] = result;
            out["held_after"] = json!(store.busy_timeout());
            out["busy_ms"] = json!(busy_ms(store));
            out["pauses"] = json!(clocks.pauses);
            if step["sample_wait"].as_bool() == Some(true) {
                let ms: Option<i64> = store
                    .connection()
                    .query_row("SELECT max(ms) FROM wait_samples", [], |row| row.get(0))
                    .unwrap();
                out["in_write_ms"] = json!(ms);
            }
            if let Some(b) = clocks.blocker.take() {
                if !b.is_autocommit() {
                    b.execute_batch("ROLLBACK").expect("rollback");
                }
            }
            store
                .connection()
                .execute_batch("PRAGMA query_only = 0")
                .expect("rw");
            if step["sample_wait"].as_bool() == Some(true) {
                store
                    .connection()
                    .execute_batch("DROP TRIGGER sample_wait; DROP TABLE wait_samples;")
                    .unwrap();
            }
            store.set_busy_timeout(held).expect("restore");
        }
        "in_tx" => {
            let mut tx = store.begin(clocks).expect("begin");
            let mut outs = Vec::new();
            for e in step["events"].as_array().expect("events") {
                outs.push(event(Ok(EventTransaction::Joined(&mut tx)), e, &mut log));
            }
            tx.commit().expect("commit");
            out["outcomes"] = json!(outs);
        }
        "import" => {
            let from = cps(step["imported_from"].as_str().expect("from"));
            let mut raw = items(&step["event"]);
            if let Some(session) = column(step, "event_session") {
                raw.push((PyValue::str("session"), PyValue::Str(session)));
            }
            let r = import_event(
                EventTransaction::Store(store, clocks),
                &raw,
                &from,
                &mut no_spans,
            );
            out["outcome"] = outcome(r.map(Some));
        }
        "keys" => {
            let from = cps(step["imported_from"].as_str().expect("from"));
            let got = imported_event_keys(store.connection(), &from).expect("keys");
            out["keys"] = json!(key_values(&got).into_iter().collect::<Vec<_>>());
        }
        "rerun" => {
            let held =
                key_values(&imported_event_keys(store.connection(), &cps("v8")).expect("keys"));
            let mut found = Vec::new();
            for e in step["events"].as_array().expect("events") {
                let (ts, session, kind, payload) =
                    event_import_payload(&items(e), &cps("v8"), &mut no_spans).expect("payload");
                let text = canonical_payload(&PyValue::Dict(payload)).expect("canonical");
                let s = |t: Option<Vec<u32>>| t.map(|t| to_string(&t).expect("text"));
                let key = json!([s(ts), s(session), to_string(&kind), text]).to_string();
                found.push(held.contains(&key));
            }
            out["found"] = json!(found);
        }
        "edit_token" => {
            let text = canonical_payload(&PyValue::Dict(vec![
                (PyValue::str("imported_from"), PyValue::str("v8")),
                (PyValue::str("note"), PyValue::str("a <|im_start|> b")),
            ]))
            .expect("canonical");
            store
                .connection()
                .execute("UPDATE log SET payload = ? WHERE kind = 'recall'", [text])
                .expect("edit");
        }
        "batch_writes" => {
            let mut tx = store.begin(clocks).expect("begin");
            let migration = record_migration(
                EventTransaction::Joined(&mut tx),
                &PyValue::str("v8"),
                &[],
                None,
            )
            .map(Some)
            .map_err(EventError::Write);
            let imported = import_event(
                EventTransaction::Joined(&mut tx),
                &items(&json!({"kind":"recall"})),
                &cps("v8"),
                &mut no_spans,
            )
            .map(Some);
            let captured = record_capture(
                EventTransaction::Joined(&mut tx),
                &cps("capture_undo"),
                &[],
                None,
                None,
                None,
            )
            .map(Some);
            if !step["rollback"].as_bool().expect("rollback") {
                tx.commit().expect("commit");
            }
            out["outcomes"] = json!([outcome(migration), outcome(imported), outcome(captured)]);
        }
        "migration" => {
            let r = record_migration(
                EventTransaction::Store(store, clocks),
                &PyValue::str(step["source"].as_str().expect("source")),
                &items(&step["counts"]),
                column(step, "session").as_deref(),
            );
            out["outcome"] = outcome(r.map(Some).map_err(EventError::Write));
        }
        "capture" => {
            let guard_name = step.get("guard").and_then(Value::as_str);
            let mut capture_refusal = |tx: &Transaction<'_>| -> Result<(), PyException> {
                tx.connection()
                    .execute_batch("CREATE TEMP TABLE capture_guard_probe(value)")
                    .unwrap();
                Err(PyException {
                    class: guard_name.expect("capture exception").to_owned(),
                    message: column(step, "guard_text").expect("capture guard message"),
                })
            };
            let mut refuse = refuse_guard;
            let mut pass = pass_guard;
            let guard: Option<&mut CaptureGuard<'_>> = match guard_name {
                Some("BatchAlreadyOpenError" | "BatchClosedError") => Some(&mut capture_refusal),
                Some("refuse") => Some(&mut refuse),
                Some("pass") => Some(&mut pass),
                _ => None,
            };
            let r = record_capture(
                EventTransaction::Store(store, clocks),
                &column(step, "kind").expect("kind"),
                &items(&step["payload"]),
                column(step, "session").as_deref(),
                column(step, "ts").as_deref(),
                guard,
            );
            out["outcome"] = outcome(r.map(Some));
            if matches!(
                guard_name,
                Some("BatchAlreadyOpenError" | "BatchClosedError")
            ) {
                let count: i64 = store
                    .connection()
                    .query_row(
                        "SELECT count(*) FROM sqlite_temp_master WHERE name='capture_guard_probe'",
                        [],
                        |r| r.get(0),
                    )
                    .unwrap();
                out["guard_effect_after"] = json!(count);
            }
        }
        other => panic!("op {other}"),
    }
    (out, log)
}

fn run(case: &Value, header: &Value) -> Result<usize, String> {
    let name = case["case"].as_str().expect("name");
    let dir = scratch("recev");
    let c = dir.0.join("c");
    let file = c.join("memory.sqlite");
    opened::made("created", &c);
    let provider = opened::provider(header);
    let mut store = open_for_writing(&file, &c.join("keys"), &provider)
        .map_err(|e| format!("{name}: the open refused: {e}"))?;
    let mut checked = 0;
    for (i, rec) in case["steps"].as_array().expect("steps").iter().enumerate() {
        let step = &rec["step"];
        let label = format!("{name} step {i} {step}");
        let read = |k: &str| {
            rec.get(k)
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default()
        };
        let mut clocks = Clocks {
            head_failure: step["head_failure"].as_str().map(str::to_owned),
            clock_failure: step["clock_failure"].as_str().map(str::to_owned),
            log: read("log_clock")
                .iter()
                .map(|t| written::instant(t.as_str().expect("t")))
                .collect(),
            mono: read("mono")
                .iter()
                .map(|t| t.as_f64().expect("f"))
                .collect(),
            ..Clocks::default()
        };
        let before = written::last_seq(&file);
        let (mut got, log) = run_step(&mut store, &file, &mut clocks, step);
        if step.get("head_failure").is_some() {
            let bytes =
                std::fs::read(store.keys_dir().join(format!("{}.head", store.store_id()))).unwrap();
            got["checkpoint_seq"] = serde_json::from_slice::<Value>(&bytes).unwrap()["seq"].clone();
        }
        let mut bad = Vec::new();
        for key in [
            "outcome",
            "outcomes",
            "pauses",
            "busy_ms",
            "in_write_ms",
            "checkpoint_seq",
            "found",
        ] {
            if rec.get(key) != got.get(key) {
                bad.push(format!(
                    "{key}: want {:?} got {:?}",
                    rec.get(key),
                    got.get(key)
                ));
            }
        }
        if got.get("held_after").and_then(Value::as_f64)
            != rec.get("held_after").and_then(Value::as_f64)
        {
            bad.push(format!(
                "held_after: want {} got {}",
                rec["held_after"], got["held_after"]
            ));
        }
        if let Some(want) = rec.get("keys") {
            let want: BTreeSet<String> = want
                .as_array()
                .expect("keys")
                .iter()
                .map(Value::to_string)
                .collect();
            let got: BTreeSet<String> = got["keys"]
                .as_array()
                .expect("keys")
                .iter()
                .map(|v| v.as_str().expect("s").to_owned())
                .collect();
            if want != got {
                bad.push(format!("keys: want {want:?} got {got:?}"));
            }
        }
        let retries: Vec<_> = log
            .iter()
            .filter_map(|line| match line {
                Logged::Retry { next, tries, .. } => Some([*next, *tries]),
                Logged::NotRecorded(_) | Logged::HeadCheckpoint { .. } => None,
            })
            .collect();
        if json!(retries) != rec["retries"] {
            bad.push(format!("retries: want {} got {retries:?}", rec["retries"]));
        }
        if json!(levels(&log)) != rec["levels"] {
            bad.push(format!(
                "levels: want {} got {:?}",
                rec["levels"],
                levels(&log)
            ));
        }
        if !clocks.log.is_empty() || !clocks.mono.is_empty() {
            bad.push("a clock read less often than the reference".to_owned());
        }
        let rows = written::log_rows(&file, before);
        if rows != rec["rows"] {
            bad.push(format!("rows: want {} got {rows}", rec["rows"]));
        }
        if !bad.is_empty() {
            return Err(format!("{label}: {}", bad.join("; ")));
        }
        checked += 1;
    }
    Ok(checked)
}

#[test]
fn every_event_write_answers_as_the_reference() {
    let mut lines = common::jsonl("store_record_event_posix.jsonl").into_iter();
    let header = lines.next().expect("header");
    let cases: Vec<Value> = lines.collect();
    let mut failures = Vec::new();
    let mut steps = 0;
    for case in &cases {
        match run(case, &header) {
            Ok(n) => steps += n,
            Err(e) => failures.push(e),
        }
    }
    assert!(steps > 0 || !failures.is_empty(), "no step checked");
    common::assert_count(
        "store_record_event_posix.jsonl",
        cases.len(),
        header["count"].as_u64().expect("case count") as usize,
    );
    eprintln!(
        "store_record_event_posix.jsonl: {} cases, {steps} steps checked",
        cases.len()
    );
    Ledger::new(
        "every_event_write_answers_as_the_reference",
        "store_record_event_posix.jsonl",
    )
    .settle(cases.len(), &failures);
}
