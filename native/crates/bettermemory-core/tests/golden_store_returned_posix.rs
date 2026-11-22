//! The backfill of `store::returned` against
//! `store_returned_posix.jsonl`: each case replayed from the store
//! `store_create.json` pins, as `golden_store_records_e.rs` replays its
//! steps, with the unit's own steps (`exec`, `backfill`, `ensure`,
//! `missing`, `marks`) dispatched here; each step's outcome, clock reads,
//! rows, state and index compared. POSIX only, as the fixture is;
//! `store_returned_not_posix.rs` records the gap elsewhere.

#![cfg(unix)]

mod common;

use bettermemory_core::store::create::Provider;
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::returned::{missing_links, returned_rows};
use bettermemory_core::store::writers::RecordWriteError;
use common::jsonl;
use common::opened::{self, differing, state};
use common::parity::Ledger;
use common::record_calls::{call_e, refusal, Reads};
use common::rows::Replay;
use common::stores::scratch;
use common::written;
use serde_json::{json, Value};

/// One step of `gen_store_returned._run_store`.
fn call(
    store: &mut WritableStore,
    provider: &mut dyn Provider,
    call: &Value,
    reads: &mut Reads,
) -> Result<Option<Value>, RecordWriteError> {
    match call["call"].as_str().expect("call") {
        "exec" => {
            for statement in call["sql"].as_array().expect("sql") {
                store
                    .connection()
                    .execute_batch(statement.as_str().expect("statement"))
                    .expect("the hand edit");
            }
            Ok(Some(Value::Null))
        }
        "backfill" => {
            let done = store.backfill(provider, call["session"].as_str(), reads.relink())?;
            Ok(Some(json!(done.value)))
        }
        "ensure" => {
            let has_key = call["has_key"].as_bool().expect("has_key");
            let got = store.ensure_backfilled(provider, has_key, reads.relink())?;
            let log: Vec<Value> = got
                .log
                .iter()
                .map(|l| json!([l.level, l.message]))
                .collect();
            Ok(Some(json!({"marked": got.marked, "log": log})))
        }
        "missing" => Ok(Some(json!(missing_links(store.connection())?))),
        "marks" => {
            let ids: Option<Vec<String>> = call["ids"].as_array().map(|ids| {
                ids.iter()
                    .map(|i| i.as_str().expect("id").to_owned())
                    .collect()
            });
            let rows = returned_rows(store.connection(), ids.as_deref())?;
            Ok(Some(json!(rows
                .into_iter()
                .map(|(a, b, c)| vec![a, b, c])
                .collect::<Vec<_>>())))
        }
        _ => call_e(store, provider, call, reads),
    }
}

fn run(case: &Value, header: &Value) -> Result<usize, String> {
    let name = case["case"].as_str().expect("name");
    let store_id = header["store_id"].as_str().expect("id");
    let pragmas = header["pragmas"].as_array().expect("pragmas");
    let dir = scratch("returned");
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
    let mut reads = Reads {
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
        let outcome = call(&mut store, &mut clocks, the_call, &mut reads);
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
fn every_backfill_step_reads_as_the_reference() {
    let mut cases = jsonl("store_returned_posix.jsonl");
    let header = cases.remove(0);
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
        "store_returned_posix.jsonl: {} cases, {calls} calls checked",
        cases.len()
    );
    let ledger = Ledger::new(
        "every_backfill_step_reads_as_the_reference",
        "store_returned_posix.jsonl",
    );
    ledger.settle(cases.len(), &failures);
}
