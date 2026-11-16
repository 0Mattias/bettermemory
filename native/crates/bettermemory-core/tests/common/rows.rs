//! What the row writers' tests share: `store_rows_written.jsonl`'s cases,
//! a step's call run natively as `gen_store_rows_written._run` runs it, and
//! a provider that replays the two clocks and the minted ids a call read.

use std::collections::VecDeque;
use std::io;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::log::Key;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::Episode;
use bettermemory_core::store::create::Provider;
use bettermemory_core::store::home::{self, Answer, Home};
use bettermemory_core::store::open::WritableStore;
use bettermemory_core::store::tables::{EpisodeArgs, Quarantine, Verification};
use bettermemory_core::store::write::WriteError;
use bettermemory_core::unicode;
use serde_json::Value;

use super::{jsonl, opened, written};

/// The header and the cases of `store_rows_written.jsonl`.
pub fn cases() -> (Value, Vec<Value>) {
    let mut lines = jsonl("store_rows_written.jsonl");
    let header = lines.remove(0);
    (header, lines)
}

/// The log clock's and the wall clock's readings and the minted ids, each
/// read once in order; a read past the end is an error.
pub struct Replay {
    pub log: VecDeque<DateTime>,
    pub wall: VecDeque<DateTime>,
    pub ids: VecDeque<String>,
    /// Each read in the order made: `log`, `wall` or `mint`.
    pub order: Vec<&'static str>,
}

impl Replay {
    pub fn of(step: &Value) -> Replay {
        let read = |k: &str| -> VecDeque<DateTime> {
            step[k]
                .as_array()
                .expect("clock")
                .iter()
                .map(|t| written::instant(t.as_str().expect("t")))
                .collect()
        };
        Replay {
            log: read("log_clock"),
            wall: read("wall_clock"),
            ids: step["minted"]
                .as_array()
                .expect("minted")
                .iter()
                .map(|t| t.as_str().expect("id").to_owned())
                .collect(),
            order: Vec::new(),
        }
    }

    pub fn drained(&self) -> bool {
        self.log.is_empty() && self.wall.is_empty() && self.ids.is_empty()
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
        self.order.push("log");
        self.log
            .pop_front()
            .ok_or_else(|| io::Error::other("the log clock was read more often than fixed"))
    }
    fn wall_now(&mut self) -> io::Result<DateTime> {
        self.order.push("wall");
        self.wall
            .pop_front()
            .ok_or_else(|| io::Error::other("the wall clock was read more often than fixed"))
    }
    fn engine_version(&self) -> String {
        opened::ENGINE.to_owned()
    }
    fn ulid(&mut self) -> io::Result<String> {
        self.order.push("mint");
        self.ids
            .pop_front()
            .ok_or_else(|| io::Error::other("an id was minted more often than fixed"))
    }
}

/// A fixture value as the reference's driver passes it: JSON, with
/// `{"$int": digits}` an int of any size.
pub fn py(v: &Value) -> PyValue {
    if let Some(digits) = v.get("$int").and_then(Value::as_str) {
        return PyValue::int_from_decimal(digits).expect("digits");
    }
    written::payload(&serde_json::to_string(v).expect("json"))
}

fn home_of(v: &Value) -> Option<Home> {
    let h = v.as_array()?;
    Some(Home {
        project: py(&h[0]),
        checkout: py(&h[1]),
        rule: py(&h[2]),
    })
}

fn time(v: &Value) -> Option<DateTime> {
    v.as_str()
        .map(|t| DateTime::from_isoformat(&unicode::cps(t)).expect("a time"))
}

fn s(v: &Value) -> Option<&str> {
    v.as_str()
}

/// A returned value as the fixture records it (JSON, or null).
pub fn value(v: &PyValue) -> Value {
    if matches!(v, PyValue::None) {
        return Value::Null;
    }
    let text = json::dumps(v, DumpOptions::SORTED_ASCII_COMPACT).expect("dumps");
    serde_json::from_str(&unicode::to_string(&text).expect("text")).expect("json")
}

/// `gen_store_rows_written._run`: the step's call; what it returned.
pub fn run(
    store: &mut WritableStore,
    step: &Value,
    p: &mut dyn Provider,
) -> Result<Value, WriteError> {
    let session = s(&step["session"]);
    let out = match step["call"].as_str().expect("call") {
        "memory_put" => {
            let payload = py(&step["payload"]);
            store.mutate("memory_put", &payload, Some("s-m"), p)?;
            Value::Null
        }
        "home" => {
            let h = home_of(&step["home"]);
            let id = s(&step["id"]).expect("id");
            let kind = s(&step["kind"]).expect("kind");
            let mut tx = store.begin(p)?;
            let row = home::home_row(&mut tx, id, kind, h.as_ref())?;
            if let Some(row) = &row {
                home::put_home(&mut tx, row, session)?;
            }
            tx.commit()?;
            row.map_or(Value::Null, |r| value(&r))
        }
        "put_standing" => {
            let a = step["answer"].as_array().expect("answer");
            let answer = Answer {
                probability: py(&a[0]),
                source: py(&a[1]),
                answered_by: py(&a[2]),
            };
            let (row, _) = store.put_standing(s(&step["id"]).expect("id"), &answer, session, p)?;
            value(&row)
        }
        "put_episode" => {
            let e = Episode::from_payload(&py(&step["episode"])).expect("an episode");
            let h = home_of(&step["home"]);
            let (e, _) = store.put_episode(&e, session, h.as_ref(), p)?;
            value(&e.dump())
        }
        "write_episode" => {
            let a = &step["args"];
            let args = EpisodeArgs {
                session_id: py(&a["session_id"]),
                body: py(&a["body"]),
                scopes: py(&a["scopes"]),
                takeaway: py(&a["takeaway"]),
                swarm_id: py(&a["swarm_id"]),
                origin: py(&a["origin"]),
            };
            let h = home_of(&step["home"]);
            let (e, _) = store.write_episode(&args, time(&step["now"]), h.as_ref(), p)?;
            value(&e.dump())
        }
        "write_floor" => {
            let h = home_of(&step["home"]);
            let (e, _) = store.write_floor(
                &py(&step["session_id"]),
                &py(&step["origin"]),
                time(&step["now"]),
                h.as_ref(),
                p,
            )?;
            value(&e.dump())
        }
        "delete_episode" => {
            store.delete_episode(s(&step["id"]).expect("id"), session, p)?;
            Value::Null
        }
        "put_verification" => {
            let a = &step["args"];
            let list = |k: &str| match py(&a[k]) {
                PyValue::List(ref v) => v.clone(),
                other => panic!("{k}: {other:?}"),
            };
            let targets = |k: &str| match py(&a[k]) {
                PyValue::Dict(ref v) => Some(v.clone()),
                PyValue::None => None,
                other => panic!("{k}: {other:?}"),
            };
            let v = Verification {
                memory_id: py(&a["memory_id"]),
                verified_at: time(&step["verified_at"]).expect("verified_at"),
                machine_id: py(&a["machine_id"]),
                verified_head: py(&a["verified_head"]),
                verified_paths: list("verified_paths"),
                verified_commits: list("verified_commits"),
                verified_versions: list("verified_versions"),
                verified_absent_paths: list("verified_absent_paths"),
                verified_path_targets: targets("verified_path_targets"),
                verified_absent_path_targets: targets("verified_absent_path_targets"),
            };
            store.put_verification(&v, session, p)?;
            Value::Null
        }
        "delete_verification" => {
            store.delete_verification(
                s(&step["memory_id"]).expect("id"),
                s(&step["machine_id"]),
                None,
                p,
            )?;
            Value::Null
        }
        "put_conflict" => {
            store.put_conflict(&py(&step["record"]), session, p)?;
            Value::Null
        }
        "delete_conflict" => {
            store.delete_conflict(s(&step["id"]).expect("id"), session, p)?;
            Value::Null
        }
        "put_import" => {
            store.put_import(
                &py(&step["source"]),
                &py(&step["content_hash"]),
                &py(&step["imported_at"]),
                &py(&step["memory_id"]),
                None,
                p,
            )?;
            Value::Null
        }
        "delete_import" => {
            store.delete_import(s(&step["source"]).expect("source"), None, p)?;
            Value::Null
        }
        "put_quarantine" => {
            let q = Quarantine {
                name: py(&step["name"]),
                reason: py(&step["reason"]),
                at: py(&step["at"]),
                detail: py(&step["detail"]),
                remote: py(&step["remote"]),
                size: py(&step["size"]),
                sha256: py(&step["sha256"]),
            };
            store.put_quarantine(&q, None, p)?;
            Value::Null
        }
        "delete_quarantine" => {
            store.delete_quarantine(s(&step["name"]).expect("name"), None, p)?;
            Value::Null
        }
        other => panic!("call {other} is not run here"),
    };
    Ok(out)
}

/// A refusal as `_refusal` records it: the class and text (sqlite3's text
/// is not recorded; a native-only class is `native:<class>`).
pub fn refusal(e: &WriteError) -> (String, Option<String>) {
    match e {
        WriteError::Refused { class, text } => ((*class).to_owned(), text.clone()),
        WriteError::Sqlite(err) => (sqlite_class(err).to_owned(), None),
        other => (format!("native:{}", other.class()), Some(other.to_string())),
    }
}

/// The `sqlite3` exception class a SQLite error raises as.
fn sqlite_class(e: &rusqlite::Error) -> &'static str {
    match e.sqlite_error_code() {
        Some(rusqlite::ErrorCode::ConstraintViolation) => "IntegrityError",
        _ => "OperationalError",
    }
}

/// A fixed clock (`written::clock`) that mints ids `01N` + a count, for
/// the tests that write without a fixture.
pub struct Counting {
    pub inner: bettermemory_core::store::create::Fixed,
    pub next: u64,
}

impl Counting {
    pub fn at(stamp: &str, first: u64) -> Counting {
        Counting {
            inner: written::clock(&[written::instant(stamp)]),
            next: first,
        }
    }
}

impl Provider for Counting {
    fn store_id(&mut self) -> io::Result<String> {
        self.inner.store_id()
    }
    fn key(&mut self) -> io::Result<Key> {
        self.inner.key()
    }
    fn membership_store_id(&mut self) -> io::Result<String> {
        self.inner.membership_store_id()
    }
    fn now(&mut self) -> io::Result<DateTime> {
        self.inner.now()
    }
    fn engine_version(&self) -> String {
        self.inner.engine_version()
    }
    fn ulid(&mut self) -> io::Result<String> {
        self.next += 1;
        Ok(written::ulid(self.next, "01N"))
    }
}
