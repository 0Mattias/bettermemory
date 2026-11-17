//! What the native relink reads against `store_records_e.jsonl`: stores
//! whose key changed (the old key held, dropped or foreign; the origin
//! anchor present or gone; strict provenance off and on) and a refold's
//! unaccounted ids, each case replayed from the store `store_create.json`
//! pins, its steps as `golden_store_records_d.rs` replays them; the key
//! ring's changes replayed byte for byte, the refold native's own (its ids
//! equal the reference's). POSIX only, as the fixture is;
//! `store_records_e_not_posix.rs` records the gap elsewhere.

#![cfg(unix)]

mod common;

use bettermemory_core::store::open::open_for_writing;
use common::opened::{self, differing, state};
use common::parity::Ledger;
use common::record_calls::{self, call_e, refusal};
use common::rows::Replay;
use common::stores::scratch;
use common::written;
use serde_json::{json, Value};

fn run(case: &Value, header: &Value) -> Result<usize, String> {
    let name = case["case"].as_str().expect("name");
    let store_id = header["store_id"].as_str().expect("id");
    let pragmas = header["pragmas"].as_array().expect("pragmas");
    let dir = scratch("records-e");
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
    let mut reads = record_calls::Reads {
        strict: case["strict"].as_bool().unwrap_or(false),
        memo: Vec::new(),
    };
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
        let outcome = call_e(&mut store, &mut clocks, the_call, &mut reads);
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
fn every_relink_reads_as_the_reference() {
    let (header, cases) = record_calls::cases_e();
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
        "store_records_e.jsonl: {} cases, {calls} calls checked",
        cases.len()
    );
    let ledger = Ledger::new(
        "every_relink_reads_as_the_reference",
        "store_records_e.jsonl",
    );
    ledger.settle(cases.len(), &failures);
}
