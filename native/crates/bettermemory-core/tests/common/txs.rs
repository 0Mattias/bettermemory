//! What the transaction tests share: `store_tx.jsonl`'s cases, a
//! transaction's items run as `gen_store_tx._run_items` runs them, and a
//! provider that replays the two clocks a transaction read.

use std::collections::VecDeque;
use std::io;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::log::Key;
use bettermemory_core::store::create::Provider;
use bettermemory_core::store::tx::Transaction;
use bettermemory_core::store::write::WriteError;
use serde_json::Value;

use super::{jsonl, opened, written};

/// The header and the cases of `store_tx.jsonl`.
pub fn cases() -> (Value, Vec<Value>) {
    let mut lines = jsonl("store_tx.jsonl");
    let header = lines.remove(0);
    (header, lines)
}

/// The log clock's and the wall clock's readings, each read once in
/// order; a read past the end is an error, so a build that reads a clock
/// more often than the reference fails.
pub struct Replay {
    pub log: VecDeque<DateTime>,
    pub wall: VecDeque<DateTime>,
}

impl Replay {
    pub fn new(log: &[DateTime], wall: &[DateTime]) -> Replay {
        Replay {
            log: log.iter().copied().collect(),
            wall: wall.iter().copied().collect(),
        }
    }

    pub fn of(step: &Value) -> Replay {
        let read = |k: &str| -> Vec<DateTime> {
            step[k]
                .as_array()
                .expect("clock")
                .iter()
                .map(|t| written::instant(t.as_str().expect("t")))
                .collect()
        };
        Replay::new(&read("log_clock"), &read("wall_clock"))
    }

    /// Whether every reading was read.
    pub fn drained(&self) -> bool {
        self.log.is_empty() && self.wall.is_empty()
    }
}

impl Provider for Replay {
    fn store_id(&mut self) -> io::Result<String> {
        Err(io::Error::other("a transaction draws no store id"))
    }
    fn key(&mut self) -> io::Result<Key> {
        Err(io::Error::other("a transaction draws no key"))
    }
    fn membership_store_id(&mut self) -> io::Result<String> {
        Err(io::Error::other("a transaction draws no membership id"))
    }
    fn now(&mut self) -> io::Result<DateTime> {
        self.log
            .pop_front()
            .ok_or_else(|| io::Error::other("the log clock was read more often than fixed"))
    }
    fn wall_now(&mut self) -> io::Result<DateTime> {
        self.wall
            .pop_front()
            .ok_or_else(|| io::Error::other("the wall clock was read more often than fixed"))
    }
    fn engine_version(&self) -> String {
        opened::ENGINE.to_owned()
    }
}

/// A transaction item's failure: the store's error, or the failure a case
/// raises at a point it names (`gen_store_tx.Injected`, with its text).
#[derive(Debug)]
pub enum Failed {
    Write(WriteError),
    Injected(&'static str),
}

impl From<WriteError> for Failed {
    fn from(e: WriteError) -> Self {
        Failed::Write(e)
    }
}

impl Failed {
    /// The refusal as `gen_store_write._refusal` records it: the class and
    /// the text (None where the class's wording is not compared).
    pub fn refusal(&self) -> (String, Option<String>) {
        match self {
            Failed::Injected(text) => ("Injected".to_owned(), Some((*text).to_owned())),
            Failed::Write(WriteError::Refused { class, text }) => {
                ((*class).to_owned(), text.clone())
            }
            Failed::Write(other) => (format!("native:{}", other.class()), Some(other.to_string())),
        }
    }
}

/// Whether a native refusal is the recorded one: the class, and the text
/// where native words it (sqlite3's wording is not recorded).
pub fn same_refusal(got: &(String, Option<String>), want: &Value) -> bool {
    let text_same = match (&got.1, want["text"].as_str()) {
        (Some(t), Some(w)) => t == w,
        (None, None) | (Some(_), None) => true,
        (None, Some(_)) => false,
    };
    want["class"] == got.0.as_str() && text_same
}

/// `gen_store_tx._run_items`: each item in order; a scope's failure is
/// caught at the scope and its refusal pushed to `errors`.
pub fn run_items(
    tx: &mut Transaction<'_>,
    items: &[Value],
    errors: &mut Vec<(String, Option<String>)>,
) -> Result<(), Failed> {
    for item in items {
        let session = item.get("session").and_then(Value::as_str);
        if let Some(inner) = item.get("scope") {
            let inner = inner.as_array().expect("scope");
            let fail = item["fail"].as_bool().expect("fail");
            let out = tx.scope(|tx| {
                run_items(tx, inner, errors)?;
                if fail {
                    return Err(Failed::Injected("the scope's end"));
                }
                Ok(())
            });
            if let Err(e) = out {
                errors.push(e.refusal());
            }
        } else if item.get("fail").is_some() {
            return Err(Failed::Injected("here"));
        } else if let Some(id) = item.get("release") {
            tx.release_returned(id.as_str().expect("id"), session)?;
        } else if let Some(id) = item.get("name") {
            tx.name_returned(
                id.as_str().expect("id"),
                item["via"].as_str().expect("via"),
                session,
            );
        } else {
            let payload =
                written::payload(&serde_json::to_string(&item["payload"]).expect("payload text"));
            tx.mutate(item["op"].as_str().expect("op"), &payload, session)?;
        }
    }
    Ok(())
}
