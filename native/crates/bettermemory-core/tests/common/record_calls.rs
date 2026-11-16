//! What the record writer tests share: `store_records.jsonl`'s cases and
//! one writer call run natively as `gen_store_records._call` runs it.

use bettermemory_core::json;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::Memory;
use bettermemory_core::store::create::Provider;
use bettermemory_core::store::open::WritableStore;
use bettermemory_core::store::writers::{Put, RecordWriteError};
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
            let home = call.get("home").map(py);
            let standing = call.get("standing").map(py);
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
