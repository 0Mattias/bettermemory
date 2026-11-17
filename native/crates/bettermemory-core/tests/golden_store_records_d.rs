//! The native `put_tombstone`, `restore` and `restore_trimmed` (with the
//! relink a restore runs) against
//! `store_records_d.jsonl`: every case replayed from the store
//! `store_create.json` pins, opened for writing natively, then each step's
//! call with a provider that replays the step's log-clock and wall-clock
//! readings and minted ids, each exactly once and in the recorded order of
//! reads; the returned record's canonical payload text (null for a hand
//! edit) or the refusal's class and text, the
//! rows appended (every column), the state's digest, the head's text and
//! the FTS5 index all equal the reference's. Then the native fold check's
//! replay equals the reference's `refold`. POSIX only, as the fixture is;
//! `store_records_d_not_posix.rs` records the gap elsewhere.

#![cfg(unix)]

mod common;

use bettermemory_core::store::open::open_for_writing;
use common::opened::{self, differing, state};
use common::parity::Ledger;
use common::record_calls::{self, call_d, refusal};
use common::rows::Replay;
use common::stores::scratch;
use common::written;
use serde_json::{json, Value};

fn run(case: &Value, header: &Value) -> Result<usize, String> {
    let name = case["case"].as_str().expect("name");
    let store_id = header["store_id"].as_str().expect("id");
    let pragmas = header["pragmas"].as_array().expect("pragmas");
    let dir = scratch("records-d");
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
    let reads = record_calls::Reads::default();
    let mut checked = 0;
    let mut whole = true;
    for step in case["steps"].as_array().expect("steps") {
        let label = format!("{name} step {}", step["step"]);
        let the_call = &step["call"];
        if the_call.get("native").and_then(Value::as_str) == Some("reference_only") {
            whole = false;
            break;
        }
        let mut clocks = Replay::of(step);
        let before_seq = written::last_seq(&file);
        let outcome = call_d(&mut store, &mut clocks, the_call, &reads);
        let got = match &outcome {
            Ok(returned) => json!({"written": true, "returned": returned}),
            Err(e) => json!({"written": false, "refusal": refusal(e)}),
        };
        if got != step["outcome"] {
            return Err(format!(
                "{label}: got {got}, the reference {}",
                step["outcome"]
            ));
        }
        if !clocks.drained() || step["reads"] != json!(clocks.order) {
            return Err(format!(
                "{label}: reads {:?}, the reference {}",
                clocks.order, step["reads"]
            ));
        }
        let rows = written::log_rows(&file, before_seq);
        if rows != step["rows"] {
            return Err(format!(
                "{label}: rows differ\nwant {}\ngot  {rows}",
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
    if whole {
        let got = written::refold(&file);
        if got != case["refold"] {
            return Err(format!(
                "{name}: refold {got}, the reference {}",
                case["refold"]
            ));
        }
    }
    Ok(checked)
}

#[test]
fn every_restore_and_tombstone_put_writes_as_the_reference() {
    let (header, cases) = record_calls::cases_d();
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
        "store_records_d.jsonl: {} cases, {calls} calls checked",
        cases.len()
    );
    let ledger = Ledger::new(
        "every_restore_and_tombstone_put_writes_as_the_reference",
        "store_records_d.jsonl",
    );
    ledger.settle(cases.len(), &failures);
}
