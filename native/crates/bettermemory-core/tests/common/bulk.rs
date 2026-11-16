//! What the chunked-run tests share: `store_bulk.jsonl`'s cases, their
//! setup steps and calls run natively as `gen_store_bulk` runs them, and a
//! provider that replays every clock a call read and records the order of
//! reads, pauses and chunk boundaries.

use std::cell::RefCell;
use std::collections::VecDeque;
use std::io;
use std::path::PathBuf;
use std::rc::Rc;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::log::Key;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::records::Memory;
use bettermemory_core::store::bulk::{put_plain, Renamed};
use bettermemory_core::store::create::Provider;
use bettermemory_core::store::open::WritableStore;
use bettermemory_core::store::tx::Transaction;
use bettermemory_core::store::write::{WriteBoundary, WriteError};
use bettermemory_core::store::writers::{Put, RecordWriteError};
use bettermemory_core::unicode;
use serde_json::{json, Value};

use super::{jsonl, opened, written};

/// The header and the cases of `store_bulk.jsonl`.
pub fn cases() -> (Value, Vec<Value>) {
    let mut lines = jsonl("store_bulk.jsonl");
    let header = lines.remove(0);
    (header, lines)
}

/// The order of a call's reads, pauses and chunk boundaries, shared by the
/// provider and the boundary hook.
pub type Order = Rc<RefCell<Vec<String>>>;

/// Every clock a call read, replayed once each in order (a read past the
/// end is an error); `order` records each read as `gen_store_bulk` names it.
pub struct Replay {
    pub log: VecDeque<DateTime>,
    pub wall: VecDeque<DateTime>,
    pub ids: VecDeque<String>,
    pub mono: VecDeque<f64>,
    pub order: Order,
    /// The store's file and the lock (`write` or `read`) another
    /// connection takes at the first pause (`gen_store_bulk._take`).
    pub lock: Option<(PathBuf, String)>,
    /// That connection, once it holds its lock.
    pub held: Option<rusqlite::Connection>,
}

impl Replay {
    pub fn of(step: &Value) -> Replay {
        let strings = |k: &str| -> Vec<String> {
            step[k]
                .as_array()
                .expect("list")
                .iter()
                .map(|t| t.as_str().expect("text").to_owned())
                .collect()
        };
        Replay {
            log: strings("log_clock")
                .iter()
                .map(|t| written::instant(t))
                .collect(),
            wall: strings("wall_clock")
                .iter()
                .map(|t| written::instant(t))
                .collect(),
            ids: strings("minted").into(),
            mono: strings("monotonic")
                .iter()
                .map(|t| t.parse::<f64>().expect("a float"))
                .collect(),
            order: Rc::new(RefCell::new(Vec::new())),
            lock: None,
            held: None,
        }
    }

    /// A provider that repeats one stamp on both clocks and records
    /// nothing: the setup's.
    pub fn setup(at: DateTime) -> Replay {
        Replay {
            log: std::iter::repeat_n(at, 100_000).collect(),
            wall: std::iter::repeat_n(at, 100_000).collect(),
            ids: VecDeque::new(),
            mono: VecDeque::new(),
            order: Rc::new(RefCell::new(Vec::new())),
            lock: None,
            held: None,
        }
    }

    pub fn drained(&self) -> bool {
        self.log.is_empty() && self.wall.is_empty() && self.ids.is_empty() && self.mono.is_empty()
    }

    fn note(&self, what: &str) {
        self.order.borrow_mut().push(what.to_owned());
    }
}

impl Provider for Replay {
    fn store_id(&mut self) -> io::Result<String> {
        Err(io::Error::other("a writer draws no store id"))
    }
    fn key(&mut self) -> io::Result<Key> {
        Err(io::Error::other("a writer draws no key"))
    }
    fn membership_store_id(&mut self) -> io::Result<String> {
        Err(io::Error::other("a writer draws no membership id"))
    }
    fn now(&mut self) -> io::Result<DateTime> {
        self.note("log");
        self.log
            .pop_front()
            .ok_or_else(|| io::Error::other("the log clock was read more often than fixed"))
    }
    fn wall_now(&mut self) -> io::Result<DateTime> {
        self.note("wall");
        self.wall
            .pop_front()
            .ok_or_else(|| io::Error::other("the wall clock was read more often than fixed"))
    }
    fn engine_version(&self) -> String {
        opened::ENGINE.to_owned()
    }
    fn ulid(&mut self) -> io::Result<String> {
        self.note("mint");
        self.ids
            .pop_front()
            .ok_or_else(|| io::Error::other("an id was minted more often than fixed"))
    }
    fn monotonic(&mut self) -> io::Result<f64> {
        self.note("mono");
        self.mono
            .pop_front()
            .ok_or_else(|| io::Error::other("the monotonic clock was read more often than fixed"))
    }
    fn pause(&mut self, seconds: f64) {
        self.note(&format!("sleep {seconds}"));
        if let (Some((file, lock)), None) = (&self.lock, &self.held) {
            let other = rusqlite::Connection::open(file).expect("a second connection");
            if lock == "write" {
                other
                    .execute_batch("BEGIN IMMEDIATE")
                    .expect("the write lock");
            } else {
                other.execute_batch("BEGIN").expect("begin");
                other
                    .query_row("SELECT count(*) FROM log", [], |r| r.get::<_, i64>(0))
                    .expect("a read lock");
            }
            self.held = Some(other);
        }
    }
}

/// A hook that records each chunk's boundaries into `order`.
pub fn boundaries(order: &Order) -> impl FnMut(WriteBoundary) + '_ {
    move |b| match b {
        WriteBoundary::Begun => order.borrow_mut().push("begin".into()),
        WriteBoundary::Committed => order.borrow_mut().push("commit".into()),
        _ => {}
    }
}

pub fn memory(record: &Value) -> Memory {
    let text = serde_json::to_string(record).expect("record");
    Memory::from_payload(&written::payload(&text)).expect("a valid record")
}

/// A setup step (`gen_store_bulk._setup`).
pub fn setup(step: &Value, store: &mut WritableStore, at: DateTime) {
    let mut p = Replay::setup(at);
    match step["edit"].as_str().expect("edit") {
        "active" => {
            for r in step["records"].as_array().expect("records") {
                let put = Put {
                    session: Some("setup"),
                    ..Put::default()
                };
                store
                    .put_memory(&mut p, &memory(r), put)
                    .expect("setup put");
            }
        }
        "removed" => {
            for pair in step["records"].as_array().expect("records") {
                let r = &pair[0];
                let put = Put {
                    session: Some("setup"),
                    ..Put::default()
                };
                store
                    .put_memory(&mut p, &memory(r), put)
                    .expect("setup put");
                let removed = written::instant(pair[1].as_str().expect("at"));
                store
                    .tombstone(
                        &mut p,
                        r["id"].as_str().expect("id"),
                        "old",
                        Some("setup"),
                        Some(removed),
                    )
                    .expect("setup tombstone");
            }
        }
        "sql" => {
            store
                .connection()
                .execute_batch(step["sql"].as_str().expect("sql"))
                .expect("setup sql");
        }
        other => panic!("an unknown setup {other}"),
    }
}

fn texts(v: &[PyStr]) -> Value {
    Value::Array(
        v.iter()
            .map(|s| json!(unicode::to_string(s).expect("text")))
            .collect(),
    )
}

/// `rename_scope`'s value as the fixture records it (`_value`).
pub fn renamed(r: &Renamed) -> Value {
    let mut out = json!({"active": texts(&r.active), "tombstoned": texts(&r.tombstoned)});
    if !r.failed.is_empty() {
        out["failed"] = Value::Array(
            r.failed
                .iter()
                .map(|(id, reason)| {
                    let reason = reason
                        .as_ref()
                        .map(|t| unicode::to_string(t).expect("text"))
                        .unwrap_or_default();
                    json!({
                        "id": unicode::to_string(id).expect("id"),
                        "reason": reason.lines().next().unwrap_or_default(),
                    })
                })
                .collect(),
        );
    }
    out
}

pub fn ids(v: &[PyStr]) -> Value {
    texts(v)
}

/// A call's failure: the store's, or the driver's own (`Injected`).
#[derive(Debug)]
pub enum Failed {
    Write(RecordWriteError),
    Injected,
}

impl From<RecordWriteError> for Failed {
    fn from(e: RecordWriteError) -> Self {
        Failed::Write(e)
    }
}

impl From<bettermemory_core::store::write::WriteError> for Failed {
    fn from(e: bettermemory_core::store::write::WriteError) -> Self {
        Failed::Write(RecordWriteError::Write(e))
    }
}

impl Failed {
    /// `_refusal`: the class and the text (None where not reproduced).
    pub fn refusal(&self) -> Value {
        match self {
            Failed::Injected => json!({"class": "Injected", "text": "here"}),
            // A busy database: sqlite3's OperationalError, whose wording is
            // the interpreter's (recorded as None).
            Failed::Write(RecordWriteError::Write(WriteError::Sqlite(
                rusqlite::Error::SqliteFailure(f, _),
            ))) if f.code == rusqlite::ErrorCode::DatabaseBusy => {
                json!({"class": "OperationalError", "text": null})
            }
            Failed::Write(e) => json!({
                "class": e.class(),
                "text": e.text().map(|t| unicode::to_string(&t).expect("text")),
            }),
        }
    }
}

/// `gen_store_bulk._batch_items` inside `tx`.
pub fn batch_items(
    tx: &mut Transaction<'_>,
    items: &[Value],
    session: Option<&str>,
) -> Result<(), Failed> {
    for item in items {
        if let Some(r) = item.get("put") {
            put_plain(tx, &memory(r), session)?;
        } else if item.get("fail").is_some() {
            return Err(Failed::Injected);
        } else {
            let inner = item["batch"].as_array().expect("batch");
            let out = tx.scope(|tx| batch_items(tx, inner, session));
            if out.is_err() && !item["catch"].as_bool().expect("catch") {
                return out;
            }
        }
    }
    Ok(())
}

/// The refusal matches the recorded one: the class and the text.
pub fn same_refusal(got: &Value, want: &Value) -> bool {
    got["class"] == want["class"] && got["text"] == want["text"]
}

/// Records put, and records put then removed at their time, as the cross
/// and kill tests set a store up: `{"active": [record], "removed":
/// [[record, time]]}`, under one fixed clock.
pub fn put_records(store: &mut WritableStore, spec: &Value) {
    let mut p = written::clock(&[written::instant("2001-02-03T04:00:00.500000Z")]);
    let put = Put {
        session: Some("setup"),
        ..Put::default()
    };
    for r in spec["active"].as_array().expect("active") {
        store.put_memory(&mut p, &memory(r), put).expect("put");
    }
    for pair in spec["removed"].as_array().expect("removed") {
        let r = &pair[0];
        store.put_memory(&mut p, &memory(r), put).expect("put");
        let at =
            DateTime::from_isoformat(&unicode::cps(pair[1].as_str().expect("at"))).expect("a time");
        store
            .tombstone(
                &mut p,
                r["id"].as_str().expect("id"),
                "old",
                Some("setup"),
                Some(at),
            )
            .expect("tombstone");
    }
}
