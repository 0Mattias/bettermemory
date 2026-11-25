//! The upkeep under contention and hostile logs: two owners opening one
//! store at once end with one `rekey` row and one anchor; a writer that
//! holds the lock past the busy timeout leaves the tail open, logged, and
//! the connection out of any transaction; a log of 20,000 `rekey` rows
//! settles in time linear in the rows; a rekey payload or an anchor nested
//! 100,000 and 1,000,000 deep raises RecursionError, no panic; a tail
//! named by a megabyte of text is closed with its name whole in the row
//! and cut to 16 code points in the warning. Also the faults no fixture
//! can stage: a clock or a checkpoint-warning callback that raises (logged
//! when it is an OSError, else raised with its class), a rekey row that
//! stops reading between the first read and the locked one, and a
//! refusal held back for a RecursionError that another writer took away.

#![cfg(unix)]

mod common;
mod timing;

use std::fs;
use std::path::Path;
use std::time::Duration;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::log::Key;
use bettermemory_core::recall_prompt::PyException;
use bettermemory_core::store::create::Provider;
use bettermemory_core::store::exception::{ExceptionFamily, StoreException};
use bettermemory_core::store::open::{
    open_for_writing_mut, open_for_writing_mut_with, Boundary, NotPorted, OpenError, UpkeepWarning,
};
use bettermemory_core::store::write::WriteError;
use common::opened;
use common::stores::{scratch, store_id};
use common::written::{clock, instant};
use rusqlite::Connection;
use serde_json::{json, Value};

/// The intact base's current key's fingerprint.
const OWN: &str = "630dcd2966c4336691125448bbb25b4ff412a49c732db2c8abc1b8581bd710dd";
const MAC8: &str = "27c628f1c20a3f30af2bdd631e3f6de27b0603c00e950a11c704336524f6a760";

fn provider() -> bettermemory_core::store::create::Fixed {
    clock(&[
        instant("2001-02-03T04:05:06.123456Z"),
        instant("2001-02-03T04:05:07.123456Z"),
    ])
}

fn rekey(conn: &Connection, seq: i64, payload: &str) {
    conn.execute(
        "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) VALUES \
         (?, '2001-02-03T04:05:06.000000Z', NULL, 'rekey', ?, ?, ?)",
        rusqlite::params![seq, payload, MAC8, "c".repeat(64)],
    )
    .expect("rekey row");
}

fn names(new: &str) -> String {
    json!({"new_fingerprint": new, "old_fingerprint": "e".repeat(64)}).to_string()
}

/// The intact base, bound, with `recipe` applied.
fn store(dir: &Path, recipe: &[Value]) -> std::path::PathBuf {
    let c = dir.join("c");
    opened::made("intact", &c);
    let mut all = recipe.to_vec();
    all.push(json!({"bind": "memory.sqlite"}));
    opened::setup(&all, &c, &store_id());
    c
}

fn open(c: &Path) -> Result<bettermemory_core::store::open::WritableStore, OpenError> {
    open_for_writing_mut(&c.join("memory.sqlite"), &c.join("keys"), &mut provider())
}

fn rekey_rows(c: &Path) -> i64 {
    Connection::open(c.join("memory.sqlite"))
        .expect("open")
        .query_row("SELECT count(*) FROM log WHERE kind = 'rekey'", [], |r| {
            r.get(0)
        })
        .expect("count")
}

#[test]
fn two_owners_at_once_append_one_rekey_row_and_write_one_anchor() {
    for round in 0..8 {
        let dir = scratch("upkeep-two");
        let c = store(&dir.0, &[json!({"unlink": ".origin"})]);
        rekey(
            &Connection::open(c.join("memory.sqlite")).expect("open"),
            9,
            &names(&"f".repeat(64)),
        );
        let logs: Vec<Vec<UpkeepWarning>> = std::thread::scope(|scope| {
            let workers: Vec<_> = (0..2)
                .map(|_| {
                    let c = &c;
                    scope.spawn(move || {
                        let store = open(c).expect("opened");
                        assert!(store.connection().is_autocommit());
                        let log = store.upkeep().to_vec();
                        store.close().expect("close");
                        log
                    })
                })
                .collect();
            workers
                .into_iter()
                .map(|w| w.join().expect("worker"))
                .collect()
        });
        assert_eq!(
            rekey_rows(&c),
            2,
            "round {round}: the forged row and one more"
        );
        let closed = logs
            .iter()
            .flatten()
            .filter(|w| matches!(w, UpkeepWarning::TailClosed { .. }))
            .count();
        assert_eq!(closed, 1, "round {round}: {logs:?}");
        let origin = fs::read_to_string(c.join("keys").join(format!("{}.origin", store_id())))
            .expect("anchor");
        assert!(origin.starts_with("{\"seq\": 1, "), "{origin}");
        assert!(origin.contains(OWN), "{origin}");
    }
}

#[test]
fn a_writer_past_the_busy_timeout_leaves_the_tail_logged_and_no_transaction() {
    let dir = scratch("upkeep-busy");
    let c = store(
        &dir.0,
        &[json!({"sql": "UPDATE meta SET value = '9.0.0' WHERE key = 'engine_version';"})],
    );
    rekey(
        &Connection::open(c.join("memory.sqlite")).expect("open"),
        9,
        &names(&"f".repeat(64)),
    );
    let holder = Connection::open(c.join("memory.sqlite")).expect("holder");
    holder.execute_batch("BEGIN IMMEDIATE").expect("lock");
    let started = std::time::Instant::now();
    let store = open_for_writing_mut_with(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &mut provider(),
        &mut |b| {
            if b == Boundary::TailClosed && !holder.is_autocommit() {
                holder.execute_batch("ROLLBACK").expect("release");
            }
        },
    )
    .expect("the open goes on");
    assert!(started.elapsed() >= Duration::from_secs(4), "no busy wait");
    assert!(store.connection().is_autocommit());
    match store.upkeep() {
        [UpkeepWarning::TailNotClosed { cause, .. }] => {
            assert_eq!(cause.class, "OperationalError");
            assert_eq!(cause.sqlite, Some("SQLITE_BUSY"));
        }
        other => panic!("{other:?}"),
    }
    store.close().expect("close");
    assert_eq!(rekey_rows(&c), 1);
    // The next open closes it.
    let store = open(&c).expect("again");
    assert!(matches!(
        store.upkeep(),
        [UpkeepWarning::TailClosed { seq: 10, .. }]
    ));
    store.close().expect("close");
}

/// A store with `rows` rekey rows (each naming this key, so no tail) and
/// no anchor: `_settle_origin` reads every one.
fn many(dir: &Path, rows: i64) -> std::path::PathBuf {
    let c = store(dir, &[json!({"unlink": ".origin"})]);
    let conn = Connection::open(c.join("memory.sqlite")).expect("open");
    conn.execute_batch("BEGIN").expect("begin");
    let payload = json!({"new_fingerprint": OWN, "old_fingerprint": OWN}).to_string();
    for k in 0..rows {
        rekey(&conn, 9 + k, &payload);
    }
    conn.execute_batch("COMMIT").expect("commit");
    c
}

#[test]
fn twenty_thousand_rekey_rows_settle_in_linear_time() {
    let dir = scratch("upkeep-many");
    let small = many(&dir.0.join("s"), 2_000);
    let large = many(&dir.0.join("l"), 20_000);
    let timed = |template: &Path| {
        let run = scratch("upkeep-many-run");
        let c = run.0.join("c");
        fs::create_dir_all(c.join("keys")).expect("dir");
        for (from, to) in [(template.to_path_buf(), c.clone())] {
            for entry in fs::read_dir(&from).expect("dir").flatten() {
                if entry.file_type().expect("type").is_file() {
                    fs::copy(entry.path(), to.join(entry.file_name())).expect("copy");
                }
            }
            for entry in fs::read_dir(from.join("keys")).expect("keys").flatten() {
                fs::copy(entry.path(), to.join("keys").join(entry.file_name())).expect("copy");
            }
        }
        // The binding names the template: rebind to the copy.
        let binding = c.join("keys").join(format!("{}.path", store_id()));
        let text = fs::read_to_string(&binding).expect("binding");
        let from = fs::canonicalize(template).expect("t").display().to_string();
        let into = fs::canonicalize(&c).expect("c").display().to_string();
        fs::write(&binding, text.replace(&from, &into)).expect("rebind");
        let took = timing::wall(|| open(&c).expect("open").close().expect("close"));
        assert!(
            fs::read_to_string(c.join("keys").join(format!("{}.origin", store_id())))
                .is_ok_and(|t| t.starts_with("{\"seq\": 1, "))
        );
        took
    };
    timing::grows_within(
        "an open settling the anchor over 2,000 then 20,000 rekey rows",
        timing::TENFOLD,
        &mut || timed(&small),
        &mut || timed(&large),
    );
}

fn nested(deep: usize) -> String {
    format!("{}{}", "[".repeat(deep), "]".repeat(deep))
}

#[test]
fn nested_payloads_and_anchors_raise_recursion_and_never_panic() {
    for deep in [100_000, 1_000_000] {
        // The last rekey row: `_close_foreign_tail`'s first read.
        let dir = scratch("upkeep-nested-tail");
        let c = store(&dir.0, &[]);
        rekey(
            &Connection::open(c.join("memory.sqlite")).expect("o"),
            9,
            &nested(deep),
        );
        match open(&c) {
            Err(OpenError::Recursion { text, .. }) => {
                assert!(
                    text.starts_with("maximum recursion depth exceeded"),
                    "{text}"
                )
            }
            other => panic!("{deep}: {:?}", other.map(|_| ())),
        }
        // An earlier rekey row, with no anchor: `_settle_origin`'s segments.
        let dir = scratch("upkeep-nested-segments");
        let c = store(&dir.0, &[json!({"unlink": ".origin"})]);
        let conn = Connection::open(c.join("memory.sqlite")).expect("o");
        rekey(&conn, 9, &nested(deep));
        rekey(
            &conn,
            10,
            &json!({"new_fingerprint": OWN, "old_fingerprint": OWN}).to_string(),
        );
        drop(conn);
        assert!(matches!(open(&c), Err(OpenError::Recursion { .. })));
        // The anchor itself.
        let dir = scratch("upkeep-nested-origin");
        let c = store(&dir.0, &[]);
        fs::write(
            c.join("keys").join(format!("{}.origin", store_id())),
            format!("{{\"seq\": {}}}", nested(deep)),
        )
        .expect("anchor");
        assert!(matches!(open(&c), Err(OpenError::Recursion { .. })));
    }
}

#[test]
fn a_megabyte_name_closes_whole_and_logs_cut() {
    let dir = scratch("upkeep-wide");
    let c = store(&dir.0, &[]);
    let name = "\u{1f600}".repeat(250_000);
    rekey(
        &Connection::open(c.join("memory.sqlite")).expect("o"),
        9,
        &names(&name),
    );
    let store = open(&c).expect("open");
    match store.upkeep() {
        [UpkeepWarning::TailClosed { seq: 10, .. }] => {}
        other => panic!("{other:?}"),
    }
    let args = store.upkeep()[0].args();
    assert_eq!(
        args[1],
        bettermemory_core::store::open::UpkeepArg::Text(vec![0x1f600; 16])
    );
    store.close().expect("close");
    let payload: String = Connection::open(c.join("memory.sqlite"))
        .expect("o")
        .query_row("SELECT payload FROM log WHERE seq = 10", [], |r| r.get(0))
        .expect("row");
    assert!(payload.contains(&name));
}

/// A provider whose log clock or checkpoint-warning callback raises
/// `raised` (`clock`: the first log clock reading; else the warning).
struct Raising {
    inner: bettermemory_core::store::create::Fixed,
    raised: Option<StoreException>,
    clock: bool,
    warned: usize,
}

impl Provider for Raising {
    fn store_id(&mut self) -> std::io::Result<String> {
        self.inner.store_id()
    }
    fn key(&mut self) -> std::io::Result<Key> {
        self.inner.key()
    }
    fn membership_store_id(&mut self) -> std::io::Result<String> {
        self.inner.membership_store_id()
    }
    fn now(&mut self) -> std::io::Result<DateTime> {
        self.inner.now()
    }
    fn engine_version(&self) -> String {
        self.inner.engine_version()
    }
    fn log_now(&mut self) -> Result<DateTime, WriteError> {
        match self.raised.take_if(|_| self.clock) {
            Some(raised) => Err(WriteError::Callback(raised)),
            None => self.inner.log_now(),
        }
    }
    fn checkpoint_warning(
        &mut self,
        _store: &Path,
        _error: &StoreException,
    ) -> Result<(), StoreException> {
        self.warned += 1;
        match self.raised.take_if(|_| !self.clock) {
            Some(raised) => Err(raised),
            None => Ok(()),
        }
    }
}

fn raised(class: &str, family: ExceptionFamily) -> StoreException {
    StoreException::new(
        PyException {
            class: class.to_owned(),
            message: bettermemory_core::unicode::cps("raised by the test"),
        },
        family,
    )
}

fn foreign_tail(dir: &Path) -> std::path::PathBuf {
    let c = store(dir, &[]);
    rekey(
        &Connection::open(c.join("memory.sqlite")).expect("open"),
        9,
        &names(&"f".repeat(64)),
    );
    c
}

/// The head's lock a directory: the head move after the tail's commit
/// fails, and its warning goes to the provider's callback.
fn head_unlockable(c: &Path) {
    let lock = c.join("keys").join(format!("{}.head.lock", store_id()));
    let _ = fs::remove_file(&lock);
    fs::create_dir(&lock).expect("a directory as the head's lock");
}

fn last_seq(c: &Path) -> i64 {
    Connection::open(c.join("memory.sqlite"))
        .expect("open")
        .query_row("SELECT max(seq) FROM log", [], |r| r.get(0))
        .expect("max")
}

/// What the clock and the checkpoint callback raise: an OSError (or the
/// clock that cannot be read) is logged and the open goes on; another
/// class raises out of the open with its own class, the row rolled back
/// (the clock) or committed (the callback, after the commit).
#[test]
fn a_clock_or_callback_exception_is_logged_or_raised_by_its_family() {
    // The clock that cannot be read: OSError, logged, nothing appended.
    let dir = scratch("upkeep-no-clock");
    let c = foreign_tail(&dir.0);
    let store = open_for_writing_mut(&c.join("memory.sqlite"), &c.join("keys"), &mut clock(&[]))
        .expect("the open goes on");
    match store.upkeep() {
        [UpkeepWarning::TailNotClosed { cause, .. }] => assert_eq!(cause.class, "OSError"),
        other => panic!("{other:?}"),
    }
    store.close().expect("close");
    assert_eq!(last_seq(&c), 9);
    // The clock raises ValueError: out of the open, nothing appended.
    let dir = scratch("upkeep-clock-raises");
    let c = foreign_tail(&dir.0);
    let mut raising = Raising {
        inner: provider(),
        raised: Some(raised("ValueError", ExceptionFamily::Other)),
        clock: true,
        warned: 0,
    };
    let got = open_for_writing_mut(&c.join("memory.sqlite"), &c.join("keys"), &mut raising);
    assert_eq!(got.expect_err("raised").class(), "ValueError");
    assert_eq!(last_seq(&c), 9);
    // The head's warning callback raises PermissionError: logged, the
    // rekey row committed.
    let dir = scratch("upkeep-warn-os");
    let c = foreign_tail(&dir.0);
    head_unlockable(&c);
    let mut raising = Raising {
        inner: provider(),
        raised: Some(raised("PermissionError", ExceptionFamily::Os)),
        clock: false,
        warned: 0,
    };
    let store = open_for_writing_mut(&c.join("memory.sqlite"), &c.join("keys"), &mut raising)
        .expect("the open goes on");
    match store.upkeep() {
        [UpkeepWarning::TailNotClosed { cause, .. }] => {
            assert_eq!(cause.class, "PermissionError")
        }
        other => panic!("{other:?}"),
    }
    store.close().expect("close");
    assert_eq!(raising.warned, 1);
    assert_eq!(last_seq(&c), 10);
    // The same callback raising ValueError: out of the open with that
    // class, the rekey row committed.
    let dir = scratch("upkeep-warn-value");
    let c = foreign_tail(&dir.0);
    head_unlockable(&c);
    let mut raising = Raising {
        inner: provider(),
        raised: Some(raised("ValueError", ExceptionFamily::Other)),
        clock: false,
        warned: 0,
    };
    let got = open_for_writing_mut(&c.join("memory.sqlite"), &c.join("keys"), &mut raising);
    assert_eq!(got.expect_err("raised").class(), "ValueError");
    assert_eq!(last_seq(&c), 10);
}

/// The name read again under the lock does not read: another writer
/// appended a rekey row whose `ts` is not UTF-8 after the first read.
/// `_close_foreign_tail` logs the OperationalError with the first name.
#[test]
fn a_rekey_row_that_does_not_read_under_the_lock_is_logged() {
    let dir = scratch("upkeep-reread");
    let c = foreign_tail(&dir.0);
    let file = c.join("memory.sqlite");
    let store = open_for_writing_mut_with(&file, &c.join("keys"), &mut provider(), &mut |b| {
        if b == Boundary::TailNamed {
            let other = Connection::open(&file).expect("another writer");
            rekey(&other, 10, &names(&"a".repeat(64)));
            other
                .execute_batch("UPDATE log SET ts = CAST(x'ff' AS TEXT) WHERE seq = 10;")
                .expect("not UTF-8");
        }
    })
    .expect("the open goes on");
    match store.upkeep() {
        [UpkeepWarning::TailNotClosed { named, cause, .. }] => {
            assert_eq!(cause.class, "OperationalError");
            assert_eq!(cause.sqlite, None);
            assert_eq!(*named, bettermemory_core::unicode::cps(&"f".repeat(64)));
        }
        other => panic!("{other:?}"),
    }
    store.close().expect("close");
    assert_eq!(last_seq(&c), 10);
}

/// A refusal of a step after the upkeep (an index another tokenizer
/// spelled) is held back while the decision reads the upkeep as raising
/// first (a nested anchor), and still refuses when the upkeep does not
/// raise after all: here another writer put a readable anchor back once
/// the ring was bound.
#[test]
fn a_held_back_refusal_still_refuses_when_the_upkeep_does_not_raise() {
    let dir = scratch("upkeep-later");
    let c = store(
        &dir.0,
        &[json!({"sql": format!(
            "UPDATE log SET payload = json_set(payload, '$.tokenizer_fingerprint', '{}') \
             WHERE seq = 1;",
            "0".repeat(64)
        )})],
    );
    let anchor = c.join("keys").join(format!("{}.origin", store_id()));
    let readable = fs::read(&anchor).expect("the anchor");
    fs::write(&anchor, format!("{{\"seq\": {}}}", nested(100_000))).expect("nested");
    let got = open_for_writing_mut_with(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &mut provider(),
        &mut |b| {
            if b == Boundary::Bound {
                fs::write(&anchor, &readable).expect("put back");
            }
        },
    );
    assert!(
        matches!(
            got,
            Err(OpenError::NotPorted(NotPorted::TokenizerDrift { .. }))
        ),
        "{:?}",
        got.map(|_| ())
    );
}
