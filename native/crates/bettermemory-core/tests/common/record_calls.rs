//! What the record writer tests share: `store_records.jsonl`'s cases and
//! one writer call run natively as `gen_store_records._call` runs it.

use bettermemory_core::json;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::Memory;
use bettermemory_core::store::create::Provider;
use bettermemory_core::store::home::{Answer, Home};
use bettermemory_core::store::open::WritableStore;
use bettermemory_core::store::writers::{Put, RecordWriteError, Update, WriteArgs};
use serde_json::Value;

use super::{jsonl, written};

/// The header and the cases of `store_records.jsonl`.
pub fn cases() -> (Value, Vec<Value>) {
    let mut lines = jsonl("store_records.jsonl");
    let header = lines.remove(0);
    (header, lines)
}

/// A JSON value as the reference's `json.loads` reads it.
pub fn py(value: &Value) -> PyValue {
    written::payload(&serde_json::to_string(value).expect("text"))
}

/// The refusal as `gen_store_write._refusal` records it.
pub fn refusal(e: &RecordWriteError) -> Value {
    serde_json::json!({
        "class": e.class(),
        "text": e.text().map(|t| json::utf8_text(&t)),
    })
}

/// One call: the returned value's canonical payload text (None for
/// `delete_tombstone`), or the refusal. Only the transaction's reads and
/// writes happen inside; the caller compares.
pub fn call(
    store: &mut WritableStore,
    provider: &mut dyn Provider,
    call: &Value,
) -> Result<Option<String>, RecordWriteError> {
    let session = call["session"].as_str();
    match call["call"].as_str().expect("call") {
        "put" => {
            let memory = Memory::from_payload(&py(&call["memory"])).expect("a valid record");
            let provenance = call.get("provenance").map(py);
            let home = call.get("home").map(|_| fixed_home());
            let standing = call.get("standing").map(|_| fixed_answer());
            let put = Put {
                provenance: provenance.as_ref(),
                filename: call.get("filename").map(|f| f.as_str().expect("text")),
                session,
                home: home.as_ref(),
                standing: standing.as_ref(),
            };
            let done = store.put_memory(provider, &memory, put)?;
            Ok(Some(done.value.payload_text().expect("payload")))
        }
        "tombstone" => {
            let now = call["now"].as_str().map(written::instant);
            let done = store.tombstone(
                provider,
                call["id"].as_str().expect("id"),
                call["reason"].as_str().expect("reason"),
                session,
                now,
            )?;
            Ok(Some(done.value.payload_text().expect("payload")))
        }
        "delete_tombstone" => {
            store.delete_tombstone(provider, call["id"].as_str().expect("id"), session)?;
            Ok(None)
        }
        other => panic!("an unknown call {other}"),
    }
}

/// The fixture's `home`: `RecordHome("global", None, "fallback")`.
pub fn fixed_home() -> Home {
    Home {
        project: PyValue::str("global"),
        checkout: PyValue::None,
        rule: PyValue::str("fallback"),
    }
}

/// The fixture's `standing`: `StandingAnswer(0.5, "explicit", "test")`.
pub fn fixed_answer() -> Answer {
    Answer {
        probability: PyValue::Float(0.5),
        source: PyValue::str("explicit"),
        answered_by: PyValue::str("test"),
    }
}

/// The header and the cases of `store_records_b.jsonl`.
pub fn cases_b() -> (Value, Vec<Value>) {
    let mut lines = jsonl("store_records_b.jsonl");
    let header = lines.remove(0);
    (header, lines)
}

fn answer_of(v: &Value) -> Option<Answer> {
    let a = v.as_array()?;
    Some(Answer {
        probability: py(&a[0]),
        source: py(&a[1]),
        answered_by: py(&a[2]),
    })
}

fn home_of(v: &Value) -> Option<Home> {
    let h = v.as_array()?;
    Some(Home {
        project: py(&h[0]),
        checkout: py(&h[1]),
        rule: py(&h[2]),
    })
}

/// One call of `store_records_b.jsonl` (`gen_store_records_b._run`).
pub fn call_b(
    store: &mut WritableStore,
    provider: &mut dyn Provider,
    call: &Value,
) -> Result<Option<String>, RecordWriteError> {
    let session = call["session"].as_str();
    let done = match call["call"].as_str().expect("call") {
        "put" => {
            let memory = Memory::from_payload(&py(&call["memory"])).expect("a valid record");
            let put = Put {
                session,
                ..Put::default()
            };
            store.put_memory(provider, &memory, put)?
        }
        "tombstone" => {
            store.tombstone(
                provider,
                call["id"].as_str().expect("id"),
                "removed",
                session,
                None,
            )?;
            return Ok(None);
        }
        "write" => {
            let a = &call["args"];
            let get = |k: &str| a.get(k).map(py);
            let (content, scopes) = (py(&a["content"]), py(&a["scopes"]));
            let (confidence, source, origin) = (get("confidence"), get("source"), get("origin"));
            let (category, claims, links, actor) =
                (get("category"), get("claims"), get("links"), get("actor"));
            let home = home_of(&call["home"]);
            let standing = answer_of(&call["standing"]);
            let args = WriteArgs {
                content: &content,
                scopes: &scopes,
                confidence: confidence.as_ref(),
                source: source.as_ref(),
                origin: origin.as_ref(),
                category: category.as_ref(),
                claims: claims.as_ref(),
                links: links.as_ref(),
                actor: actor.as_ref(),
                session,
                home: home.as_ref(),
                standing: standing.as_ref(),
            };
            store.write(provider, args)?
        }
        "update" => {
            let memory = Memory::from_payload(&py(&call["memory"])).expect("a valid record");
            let standing = answer_of(&call["standing"]);
            let update = Update {
                force: call["force"].as_bool().expect("force"),
                preserve_verification: call["preserve_verification"].as_bool().expect("p"),
                session,
                standing: standing.as_ref(),
            };
            store.update(provider, &memory, update)?
        }
        other => panic!("an unknown call {other}"),
    };
    Ok(Some(done.value.payload_text().expect("payload")))
}

/// A provider whose ids are `01K` numbered from `next`, everything else
/// `inner`'s: for tests that write several records through `write`.
pub struct Minting<P> {
    pub inner: P,
    pub next: u64,
}

impl<P: Provider> Provider for Minting<P> {
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
        self.inner.wall_now()
    }
    fn engine_version(&self) -> String {
        self.inner.engine_version()
    }
    fn ulid(&mut self) -> std::io::Result<String> {
        self.next += 1;
        Ok(written::ulid(self.next, "01K"))
    }
}
