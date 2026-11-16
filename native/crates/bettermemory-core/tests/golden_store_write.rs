//! Native writes against `store_write.jsonl`: every sequence replayed
//! from the store `store_create.json` pins, its key ring edited as the
//! recipe says, opened for writing natively, then each step:
//!
//! - an edit, made as the generator made it;
//! - `same`: `WritableStore::mutate` with the step's clock; written or
//!   refused as the reference (its class and text), and after it the rows
//!   it appended (every column, byte for byte), the whole state's digest
//!   (every table, meta, the digest's tables, the key ring's texts and
//!   modes, the directory listings), the head's text and the FTS5 index
//!   as queries see it all equal the reference's;
//! - `not_ported:<class>`: refused as that class with nothing written
//!   (the state as before the step), where the reference wrote or refused.
//!   `not_ported:returned` is ported since (`store::tx`): written, with as
//!   many rows as the reference wrote; `golden_store_tx.rs` compares such
//!   writes byte for byte.
//!
//! POSIX only, as the fixture is (modes, a resolved binding path);
//! `store_write_not_posix.rs` records the gap elsewhere.

#![cfg(unix)]

mod common;

use bettermemory_core::store::fold::{MEMORY_FIELDS, TOMBSTONE_FIELDS};
use bettermemory_core::store::open::open_for_writing;
use bettermemory_core::store::write::WriteError;
use common::opened::{self, differing, state};
use common::parity::Ledger;
use common::stores::scratch;
use common::written::{self, instant};
use serde_json::{json, Value};

fn run(seq: &Value, header: &Value) -> Result<usize, String> {
    let name = seq["sequence"].as_str().expect("name");
    let store_id = header["store_id"].as_str().expect("id");
    let pragmas = header["pragmas"].as_array().expect("pragmas");
    let dir = scratch("write");
    let root = dir.0.clone();
    let c = root.join("c");
    let file = c.join("memory.sqlite");
    opened::made("created", &c);
    written::recipe(
        seq["recipe"].as_array().expect("recipe"),
        &c.join("keys"),
        &file,
        store_id,
    );
    let provider = opened::provider(header);
    let mut store = open_for_writing(&file, &c.join("keys"), &provider)
        .map_err(|e| format!("{name}: the open refused: {e}"))?;
    let opened_state = state(&c, &root, store_id, pragmas);
    let parts = differing(&seq["opened"], &opened_state);
    if !parts.is_empty() {
        return Err(format!("{name}: the opened store differs in {parts:?}"));
    }
    let mut checked = 0;
    for step in seq["steps"].as_array().expect("steps") {
        if let Some(edit) = step.get("edit") {
            written::edit(edit, &store, &c, store_id);
            continue;
        }
        let label = format!("{name} step {}", step["step"]);
        let stamps: Vec<_> = step["clock"]
            .as_array()
            .expect("clock")
            .iter()
            .map(|t| instant(t.as_str().expect("t")))
            .collect();
        let mut clock = written::clock(&stamps);
        let kind = step["kind"].as_str().expect("kind");
        let payload = written::payload(step["payload"].as_str().expect("payload"));
        let session = step["session"].as_str();
        let before_seq = written::last_seq(&file);
        let before = state(&c, &root, store_id, pragmas);
        let outcome = store.mutate(kind, &payload, session, &mut clock);
        let native = step["native"].as_str().expect("native");
        if native == "not_ported:returned" {
            // Ported since: the write and its `returned` rows. This file
            // left the reference's mark stamp to the real wall clock and
            // recorded only how many rows it wrote; `store_tx.jsonl` holds
            // such writes byte for byte. Here: written, as many rows, the
            // mutation's then `returned_*` ones.
            let wrote = step["reference_wrote"].as_i64().expect("reference_wrote");
            match &outcome {
                Ok(w) if w.returned.iter().all(|r| r.kind.starts_with("returned_")) => {}
                other => return Err(format!("{label}: expected a write, got {other:?}")),
            }
            let got = written::last_seq(&file) - before_seq;
            if got != wrote {
                return Err(format!("{label}: wrote {got} rows, the reference {wrote}"));
            }
            checked += 1;
            continue;
        }
        if let Some(class) = native.strip_prefix("not_ported:") {
            match &outcome {
                Err(WriteError::NotPorted(n)) if n.name() == class => {}
                other => return Err(format!("{label}: expected {native}, got {other:?}")),
            }
            let parts = differing(&before, &state(&c, &root, store_id, pragmas));
            if !parts.is_empty() {
                return Err(format!("{label}: a not-ported refusal wrote {parts:?}"));
            }
            checked += 1;
            continue;
        }
        let want = &step["outcome"];
        match (&outcome, want["written"].as_bool().expect("written")) {
            (Ok(_), true) => {}
            (Err(WriteError::Refused { class, text }), false) => {
                let refusal = &want["refusal"];
                // sqlite3's wording is not compared (the fixture has none).
                let text_differs = match (text, refusal["text"].as_str()) {
                    (Some(t), Some(want)) => t != want,
                    (None, None) | (Some(_), None) => false,
                    (None, Some(_)) => true,
                };
                if refusal["class"] != *class || text_differs {
                    return Err(format!(
                        "{label}: refused {class} {text:?}, the reference {refusal}"
                    ));
                }
            }
            (got, _) => return Err(format!("{label}: got {got:?}, the reference {want}")),
        }
        let rows = written::log_rows(&file, before_seq);
        if rows != step["rows"] {
            return Err(format!(
                "{label}: rows differ\nwant {}\ngot  {rows}",
                step["rows"]
            ));
        }
        let full = state(&c, &root, store_id, pragmas);
        let digest = written::state_digest(&full);
        if digest != step["state"] {
            return Err(format!(
                "{label}: the state differs\nwant {}\ngot  {digest}\nfull {full}",
                step["state"]
            ));
        }
        let fts = written::fts_view(&file);
        if fts != step["fts"] {
            return Err(format!(
                "{label}: the index differs: want {}, got {fts}",
                step["fts"]
            ));
        }
        checked += 1;
    }
    store.close().map_err(|e| format!("{name}: close: {e}"))?;
    // The fold check replays what the writes left as the reference's
    // `refold` replays the reference's, when the two stores are the same.
    let last = seq["steps"]
        .as_array()
        .expect("steps")
        .iter()
        .rev()
        .find(|s| s.get("edit").is_none());
    if last.is_some_and(|s| s["native"] == "same") {
        let got = written::refold(&file);
        if got != seq["refold"] {
            return Err(format!(
                "{name}: refold {got}, the reference {}",
                seq["refold"]
            ));
        }
    }
    Ok(checked)
}

#[test]
fn every_sequence_writes_as_the_reference() {
    let (header, seqs) = written::sequences();
    let mut failures = Vec::new();
    let mut steps = 0;
    for seq in &seqs {
        match run(seq, &header) {
            Ok(n) => steps += n,
            Err(e) => failures.push(e),
        }
    }
    assert!(steps > 0 || !failures.is_empty(), "no step checked");
    eprintln!(
        "store_write.jsonl: {} sequences, {steps} steps checked",
        seqs.len()
    );
    let ledger = Ledger::new(
        "every_sequence_writes_as_the_reference",
        "store_write.jsonl",
    );
    ledger.settle(seqs.len(), &failures);
}

/// The models' field lists the unknown-field check reads are the
/// reference's.
#[test]
fn the_field_lists_are_the_models() {
    let (header, _) = written::sequences();
    let list = |fields: &[&str]| Value::Array(fields.iter().map(|f| json!(f)).collect());
    assert_eq!(header["memory_fields"], list(&MEMORY_FIELDS));
    assert_eq!(header["tombstone_fields"], list(&TOMBSTONE_FIELDS));
}
