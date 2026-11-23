//! The record and checkpoint clocks are on opposite sides of COMMIT.
//! A failure from the checkpoint warning itself reenters record_event's
//! enclosing catch, including the rare case of a second durable row.

mod common;

use std::{io, path::Path};

use bettermemory_core::datetime::DateTime;
use bettermemory_core::log::Key;
use bettermemory_core::recall_prompt::PyException;
use bettermemory_core::store::create::Provider;
use bettermemory_core::store::exception::{ExceptionFamily, StoreException};
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::record_event::{
    record_event, EventCall, EventError, EventLogger, EventTransaction, Logged,
};
use bettermemory_core::store::write::WriteError;
use bettermemory_core::unicode::cps;
use common::{opened, stores::scratch, written};
use rusqlite::Connection;
use serde_json::Value;

fn raised(class: &str, family: ExceptionFamily, code: Option<i64>) -> StoreException {
    let mut error = StoreException::new(
        PyException {
            class: class.into(),
            message: vec![0x3c0, 0xdcff],
        },
        family,
    );
    error.sqlite_code = code;
    error
}

fn snapshot(file: &Path) -> (i64, i64) {
    let conn = Connection::open(file).unwrap();
    let seq = conn
        .query_row("SELECT max(seq) FROM log", [], |r| r.get(0))
        .unwrap();
    let name = format!("{}.head", common::stores::store_id());
    let path = file.parent().unwrap().join("keys").join(name);
    let head: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
    (seq, head["seq"].as_i64().unwrap())
}

struct Clock {
    ticks: usize,
    fail_at: usize,
    failure: StoreException,
    handler: Option<StoreException>,
    pauses: usize,
    shared: Vec<(i64, i64)>,
}

impl Clock {
    fn new(fail_at: usize, failure: StoreException) -> Self {
        Self {
            ticks: 0,
            fail_at,
            failure,
            handler: None,
            pauses: 0,
            shared: Vec::new(),
        }
    }
}

impl Provider for Clock {
    fn store_id(&mut self) -> io::Result<String> {
        unreachable!()
    }
    fn membership_store_id(&mut self) -> io::Result<String> {
        unreachable!()
    }
    fn key(&mut self) -> io::Result<Key> {
        unreachable!()
    }
    fn now(&mut self) -> io::Result<DateTime> {
        unreachable!()
    }
    fn engine_version(&self) -> String {
        opened::ENGINE.to_owned()
    }
    fn log_now(&mut self) -> Result<DateTime, WriteError> {
        self.ticks += 1;
        if self.ticks == self.fail_at {
            return Err(WriteError::Callback(self.failure.clone()));
        }
        Ok(written::instant("2002-03-04T05:06:07.123456Z"))
    }
    fn monotonic(&mut self) -> io::Result<f64> {
        Ok(0.0)
    }
    fn pause(&mut self, seconds: f64) {
        assert_eq!(seconds, 0.05);
        self.pauses += 1;
    }
    fn checkpoint_warning(
        &mut self,
        file: &Path,
        error: &StoreException,
    ) -> Result<(), StoreException> {
        assert_eq!(error, &self.failure);
        self.shared.push(snapshot(file));
        self.handler.take().map_or(Ok(()), Err)
    }
}

#[derive(Default)]
struct Logger {
    entries: Vec<Logged>,
    cause: Vec<StoreException>,
    on_head: Option<StoreException>,
    on_other: Option<StoreException>,
}

impl EventLogger for Logger {
    fn emit(&mut self, _: &[u32], _: Logged) -> Result<(), PyException> {
        unreachable!()
    }
    fn emit_typed(
        &mut self,
        kind: &[u32],
        entry: Logged,
        cause: Option<&WriteError>,
    ) -> Result<(), StoreException> {
        assert_eq!(kind, cps("probe"));
        let error = match &entry {
            Logged::HeadCheckpoint { path, .. } => {
                assert_eq!(snapshot(path), (2, 1));
                assert!(cause.is_none());
                self.on_head.take()
            }
            _ => {
                if let Some(WriteError::Callback(e)) = cause {
                    self.cause.push(e.clone());
                }
                self.on_other.take()
            }
        };
        self.entries.push(entry);
        error.map_or(Ok(()), Err)
    }
}

fn fresh(dir: &Path) -> WritableStore {
    opened::made("created", dir);
    open_for_writing(
        &dir.join("memory.sqlite"),
        &dir.join("keys"),
        &opened::engine_provider(),
    )
    .unwrap()
}

fn call(
    target: EventTransaction<'_, '_>,
    logger: &mut Logger,
    best_effort: bool,
    deadline: f64,
) -> Result<Option<i64>, EventError> {
    let call = EventCall {
        kind: &cps("probe"),
        session: None,
        best_effort,
        attempts: 2,
        deadline: Some(deadline),
    };
    record_event(Ok(target), &call, &[], None, &mut |_| Vec::new(), logger)
        .map(|row| row.map(|r| r.seq))
}

#[test]
fn the_checkpoint_warning_can_retry_a_durable_write_only_for_a_coded_busy_handler() {
    use ExceptionFamily::*;
    for (family, code, deadline, retry) in [
        (Operational, Some(5), 0.2, true),
        (Operational, Some(262), 0.2, true),
        (Operational, Some(5), 0.05, false),
        (Operational, None, 0.2, false),
        (Sqlite, Some(5), 0.2, false),
    ] {
        let dir = scratch("checkpoint-retry");
        let mut store = fresh(&dir.0);
        let os = raised("ClockOSError", Os, None);
        let handler = raised("LoggerSubclass", family, code);
        let mut clock = Clock::new(2, os.clone());
        let mut logger = Logger {
            on_head: Some(handler.clone()),
            ..Logger::default()
        };
        let seq = call(
            EventTransaction::Store(&mut store, &mut clock),
            &mut logger,
            true,
            deadline,
        )
        .unwrap();
        assert_eq!(seq, retry.then_some(3));
        assert_eq!(clock.pauses, usize::from(retry));
        assert_eq!(logger.cause.as_slice(), std::slice::from_ref(&handler));
        assert_eq!(
            logger.entries[0],
            Logged::HeadCheckpoint {
                path: store.path().to_owned(),
                error: os
            }
        );
        let reason = handler.exception.message;
        assert_eq!(
            logger.entries[1],
            if retry {
                Logged::Retry {
                    reason,
                    next: 2,
                    tries: 2,
                }
            } else {
                Logged::NotRecorded(reason)
            }
        );
        assert_eq!(snapshot(store.path()), if retry { (3, 3) } else { (2, 1) });
        assert!(store.connection().is_autocommit());
        let ms: i64 = store
            .connection()
            .query_row("PRAGMA busy_timeout", [], |r| r.get(0))
            .unwrap();
        assert_eq!(ms, 5000);
    }
}

#[test]
fn exceptions_from_both_clocks_and_the_terminal_handler_keep_their_type_and_codepoints() {
    use ExceptionFamily::*;
    for tick in [1, 2] {
        let dir = scratch("checkpoint-clocks");
        let mut store = fresh(&dir.0);
        let fail = raised("TypeError", Other, None);
        let mut clock = Clock::new(tick, fail.clone());
        let mut log = Logger::default();
        let result = call(
            EventTransaction::Store(&mut store, &mut clock),
            &mut log,
            true,
            1.0,
        );
        assert!(matches!(result, Err(EventError::Write(WriteError::Callback(e))) if e == fail));
        assert_eq!(snapshot(store.path()), (tick as i64, 1));
        assert!(log.entries.is_empty());
    }
    for fail_at_head in [true, false] {
        let dir = scratch("checkpoint-logger");
        let mut store = fresh(&dir.0);
        let os = raised("ClockOSError", Os, None);
        let mut clock = Clock::new(2, os);
        let original = raised("Unhandled", Other, None);
        let caught = raised("RuntimeSubclass", Runtime, None);
        let mut log = Logger {
            on_head: Some(if fail_at_head {
                original.clone()
            } else {
                caught.clone()
            }),
            on_other: Some(original.clone()),
            ..Logger::default()
        };
        let result = call(
            EventTransaction::Store(&mut store, &mut clock),
            &mut log,
            true,
            1.0,
        );
        match result {
            Err(EventError::Write(WriteError::Callback(e))) if fail_at_head => {
                assert_eq!(e, original)
            }
            Err(EventError::Log(e)) if !fail_at_head => {
                assert_eq!(e, original);
                assert_eq!(log.cause, [caught]);
            }
            other => panic!("{other:?}"),
        }
        assert_eq!(snapshot(store.path()), (2, 1));
    }
}

#[test]
fn enclosing_transaction_owns_the_checkpoint_and_its_handler_exception() {
    let dir = scratch("checkpoint-joined");
    let mut store = fresh(&dir.0);
    let mut clock = Clock::new(2, raised("OSError", ExceptionFamily::Os, None));
    let fail = raised("OperationalError", ExceptionFamily::Operational, Some(5));
    clock.handler = Some(fail.clone());
    let mut tx = store.begin(&mut clock).unwrap();
    let mut log = Logger::default();
    assert_eq!(
        call(EventTransaction::Joined(&mut tx), &mut log, true, 1.0).unwrap(),
        Some(2)
    );
    assert!(log.entries.is_empty());
    assert!(matches!(tx.commit(), Err(WriteError::Callback(e)) if e == fail));
    assert_eq!(clock.shared, [(2, 1)]);
    assert_eq!(clock.pauses, 0);
    assert_eq!(store.head().unwrap().0, 2);
    assert!(store.connection().is_autocommit());
}

#[cfg(unix)]
#[test]
fn filesystem_warnings_keep_the_failing_syscall_filenames_and_durable_row() {
    use bettermemory_core::{pyvalue::repr_str, unicode::to_string};
    for replacing in [false, true] {
        let dir = scratch("checkpoint-filesystem");
        let mut store = fresh(&dir.0);
        let base = format!("{}.head", store.store_id());
        let path = store.keys_dir().join(if replacing {
            base.clone()
        } else {
            format!("{base}.lock")
        });
        if path.exists() {
            std::fs::rename(&path, path.with_extension("previous")).unwrap();
        }
        std::fs::create_dir(&path).unwrap();
        let mut clock = Clock::new(0, raised("unused", ExceptionFamily::Other, None));
        let mut logged = Vec::new();
        let call = EventCall {
            kind: &cps("probe"),
            session: None,
            best_effort: false,
            attempts: 1,
            deadline: None,
        };
        let row = record_event(
            Ok(EventTransaction::Store(&mut store, &mut clock)),
            &call,
            &[],
            None,
            &mut |_| Vec::new(),
            &mut logged,
        )
        .unwrap()
        .unwrap();
        assert_eq!(row.seq, 2);
        let [Logged::HeadCheckpoint {
            path: store_path,
            error,
        }] = logged.as_slice()
        else {
            panic!("{logged:?}");
        };
        assert_eq!(store_path, store.path());
        assert_eq!(error.exception.class, "IsADirectoryError");
        assert_eq!(error.errno, Some(21));
        let source = error.filename.as_deref().unwrap();
        let quoted = |p: &Path| to_string(&repr_str(&cps(p.to_str().unwrap()))).unwrap();
        let mut expected = format!("[Errno 21] Is a directory: {}", quoted(source));
        if replacing {
            assert_eq!(source.parent(), path.parent());
            assert!(source
                .file_name()
                .unwrap()
                .to_str()
                .unwrap()
                .starts_with(&format!("{base}.")));
            assert_eq!(source.extension().unwrap(), "tmp");
            assert!(!source.exists());
            assert_eq!(error.filename2.as_deref(), Some(path.as_path()));
            expected.push_str(&format!(" -> {}", quoted(&path)));
        } else {
            assert_eq!(source, path);
            assert_eq!(error.filename2, None);
        }
        assert_eq!(error.exception.message, cps(&expected));
        let conn = Connection::open(store.path()).unwrap();
        let max: i64 = conn
            .query_row("SELECT max(seq) FROM log", [], |r| r.get(0))
            .unwrap();
        assert_eq!(max, row.seq);
    }
}

#[test]
fn malformed_head_raises_after_commit_and_bypasses_the_os_warning() {
    for (raw, class, message) in [
        (
            r#"{"seq":1e999,"mac":"x"}"#.to_owned(),
            "OverflowError",
            "cannot convert float infinity to integer",
        ),
        (
            "[".repeat(15000),
            "RecursionError",
            "maximum recursion depth exceeded while decoding a JSON array from a unicode string",
        ),
        (
            r#"{"x":"#.repeat(15000),
            "RecursionError",
            "maximum recursion depth exceeded while decoding a JSON object from a unicode string",
        ),
    ] {
        let dir = scratch("checkpoint-invalid");
        let mut store = fresh(&dir.0);
        let head = store.keys_dir().join(format!("{}.head", store.store_id()));
        std::fs::write(&head, &raw).unwrap();
        let mut clock = Clock::new(0, raised("unused", ExceptionFamily::Other, None));
        let mut logs = Logger::default();
        let result = call(
            EventTransaction::Store(&mut store, &mut clock),
            &mut logs,
            false,
            1.0,
        );
        assert!(
            matches!(result, Err(EventError::Write(WriteError::Refused { class: actual, text: Some(ref text) })) if actual == class && text == message)
        );
        assert_eq!(std::fs::read_to_string(head).unwrap(), raw);
        assert_eq!(store.head().unwrap().0, 2);
        assert!(store.connection().is_autocommit());
        assert!(logs.entries.is_empty());
    }
}

/// A normal Provider::now, using the default typed log clock. SQLite gets
/// its row stamp before the checkpoint clock returns the actual OS error.
struct OsClock(Option<io::Error>, bool);

impl Provider for OsClock {
    fn store_id(&mut self) -> io::Result<String> {
        unreachable!()
    }
    fn membership_store_id(&mut self) -> io::Result<String> {
        unreachable!()
    }
    fn key(&mut self) -> io::Result<Key> {
        unreachable!()
    }
    fn engine_version(&self) -> String {
        opened::ENGINE.to_owned()
    }
    fn monotonic(&mut self) -> io::Result<f64> {
        Ok(0.0)
    }
    fn now(&mut self) -> io::Result<DateTime> {
        if self.1 {
            return Err(self.0.take().unwrap());
        }
        self.1 = true;
        Ok(written::instant("2002-03-04T05:06:07.123456Z"))
    }
}

#[test]
fn a_native_clock_oserror_reaches_the_checkpoint_handler_with_its_python_subclass() {
    let cases = vec![
        (
            io::Error::new(io::ErrorKind::BrokenPipe, "provider pipe"),
            "BrokenPipeError",
        ),
        (io::Error::other("provider error"), "OSError"),
    ];
    // POSIX errno values measured against OSError(errno, strerror(errno)).
    // EAGAIN and EWOULDBLOCK share their value on both supported Unix targets.
    #[cfg(any(target_os = "macos", target_os = "linux"))]
    let cases = {
        let mut cases = cases;
        let varying = if cfg!(target_os = "macos") {
            [35, 37, 36, 58, 53, 61, 54, 60]
        } else {
            [11, 114, 115, 108, 103, 111, 104, 110]
        };
        let names = [
            "BlockingIOError",
            "BlockingIOError",
            "BlockingIOError",
            "BrokenPipeError",
            "ConnectionAbortedError",
            "ConnectionRefusedError",
            "ConnectionResetError",
            "TimeoutError",
        ];
        let common = [
            (1, "PermissionError"),
            (2, "FileNotFoundError"),
            (3, "ProcessLookupError"),
            (4, "InterruptedError"),
            (5, "OSError"),
            (10, "ChildProcessError"),
            (13, "PermissionError"),
            (17, "FileExistsError"),
            (20, "NotADirectoryError"),
            (21, "IsADirectoryError"),
            (32, "BrokenPipeError"),
        ];
        cases.extend(
            common
                .into_iter()
                .chain(varying.into_iter().zip(names))
                .map(|(code, class)| (io::Error::from_raw_os_error(code), class)),
        );
        cases
    };
    for (error, class) in cases {
        let dir = scratch("checkpoint-errno");
        let mut store = fresh(&dir.0);
        let code = error.raw_os_error();
        let message = error.to_string();
        let mut clock = OsClock(Some(error), false);
        let mut log = Logger::default();
        assert_eq!(
            call(
                EventTransaction::Store(&mut store, &mut clock),
                &mut log,
                false,
                1.0
            )
            .unwrap(),
            Some(2)
        );
        let [Logged::HeadCheckpoint { error, .. }] = log.entries.as_slice() else {
            panic!("{:?}", log.entries)
        };
        assert_eq!(error.errno, code);
        assert_eq!(error.exception.class, class);
        assert_eq!(error.family, ExceptionFamily::Os);
        let expected = match code {
            Some(n) => format!(
                "[Errno {n}] {}",
                message.strip_suffix(&format!(" (os error {n})")).unwrap()
            ),
            None => message,
        };
        assert_eq!(error.exception.message, cps(&expected));
        assert_eq!(snapshot(store.path()), (2, 1));
    }
}
