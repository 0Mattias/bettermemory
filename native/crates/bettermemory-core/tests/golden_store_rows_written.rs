//! The row writers (`store::home`, `store::tables`) against
//! `store_rows_written.jsonl`: every case replayed from the store
//! `store_create.json` pins, opened for writing natively, then each step:
//! an edit made as the generator made it, or a call run with a provider
//! that replays the step's log-clock and wall-clock readings and minted
//! ids, each exactly once and in the reference's order of reads (one
//! sequence of log, wall and mint reads per call). The returned value or the refusal (class and
//! text), every reading read, the rows the call appended (every column,
//! byte for byte), the whole state's digest, the head's text and the FTS5
//! index as queries see it all equal the reference's; then the native fold
//! check's replay equals the reference's `refold`. POSIX only, as the
//! fixture is; `store_tx_not_posix.rs` records the gap elsewhere.

#![cfg(unix)]

mod common;

use bettermemory_core::store::open::open_for_writing;
use common::opened::{self, differing, state};
use common::parity::Ledger;
use common::rows::{self, refusal, Replay};
use common::stores::scratch;
use common::written;
use serde_json::Value;

fn run(case: &Value, header: &Value) -> Result<usize, String> {
    let name = case["case"].as_str().expect("name");
    let store_id = header["store_id"].as_str().expect("id");
    let pragmas = header["pragmas"].as_array().expect("pragmas");
    let dir = scratch("rows");
    let root = dir.0.clone();
    let c = root.join("c");
    let file = c.join("memory.sqlite");
    opened::made("created", &c);
    let provider = opened::provider(header);
    let mut store = open_for_writing(&file, &c.join("keys"), &provider)
        .map_err(|e| format!("{name}: the open refused: {e}"))?;
    let parts = differing(&header["opened"], &state(&c, &root, store_id, pragmas));
    if !parts.is_empty() {
        return Err(format!("{name}: the opened store differs in {parts:?}"));
    }
    let mut checked = 0;
    for step in case["steps"].as_array().expect("steps") {
        if let Some(edit) = step.get("edit") {
            match edit["edit"].as_str().expect("edit") {
                "drop" => {
                    let table = edit["table"].as_str().expect("table");
                    store
                        .connection()
                        .execute_batch(&format!("DROP TABLE {table}"))
                        .expect("drop");
                }
                _ => written::edit(edit, &store, &c, store_id),
            }
            continue;
        }
        let label = format!("{name} step {}", step["step"]);
        let mut clocks = Replay::of(step);
        let before = written::last_seq(&file);
        let got = rows::run(&mut store, &step["call"], &mut clocks);
        let want = &step["outcome"];
        match (&got, want.get("returned")) {
            (Ok(v), Some(w)) if v == w => {}
            (Err(e), None) => {
                let (class, text) = refusal(e);
                let w = &want["refusal"];
                // The reference records no text only for a sqlite3 error
                // (`_refusal`: its wording is the interpreter's SQLite's):
                // the IntegrityErrors of `conflicts_imports_quarantine`
                // (a NOT NULL column left empty) and the OperationalErrors
                // of `homes_table_dropped` (no such table). The class is
                // compared there.
                let same_text = w["text"].is_null() || w["text"].as_str() == text.as_deref();
                if w["class"] != class.as_str() || !same_text {
                    return Err(format!(
                        "{label}: refused {class} {text:?}, the reference {w}"
                    ));
                }
            }
            (got, _) => return Err(format!("{label}: got {got:?}, the reference {want}")),
        }
        if !clocks.drained() {
            return Err(format!(
                "{label}: read less often than the reference: {} log, {} wall, {} ids left",
                clocks.log.len(),
                clocks.wall.len(),
                clocks.ids.len()
            ));
        }
        let reads: Vec<&str> = step["reads"]
            .as_array()
            .expect("reads")
            .iter()
            .map(|r| r.as_str().expect("read"))
            .collect();
        if clocks.order != reads {
            return Err(format!(
                "{label}: read in the order {:?}, the reference {reads:?}",
                clocks.order
            ));
        }
        let appended = written::log_rows(&file, before);
        if appended != step["rows"] {
            return Err(format!(
                "{label}: rows differ\nwant {}\ngot  {appended}",
                step["rows"]
            ));
        }
        let digest = written::state_digest(&state(&c, &root, store_id, pragmas));
        if digest != step["state"] {
            return Err(format!(
                "{label}: the state differs\nwant {}\ngot  {digest}",
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
    let got = written::refold(&file);
    if got != case["refold"] {
        return Err(format!(
            "{name}: refold {got}, the reference {}",
            case["refold"]
        ));
    }
    Ok(checked)
}

#[test]
fn every_row_writer_call_writes_as_the_reference() {
    let (header, cases) = rows::cases();
    let mut failures = Vec::new();
    let mut calls = 0;
    for case in &cases {
        match run(case, &header) {
            Ok(n) => calls += n,
            Err(e) => failures.push(e),
        }
    }
    assert!(calls > 0 || !failures.is_empty(), "no call checked");
    eprintln!(
        "store_rows_written.jsonl: {} cases, {calls} calls checked",
        cases.len()
    );
    let ledger = Ledger::new(
        "every_row_writer_call_writes_as_the_reference",
        "store_rows_written.jsonl",
    );
    ledger.settle(cases.len(), &failures);
}
