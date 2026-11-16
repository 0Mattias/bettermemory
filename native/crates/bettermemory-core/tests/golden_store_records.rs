//! The native record writers against `store_records.jsonl`: every case
//! replayed from the store `store_create.json` pins, opened for writing
//! natively, then each step:
//!
//! - an edit, made as the generator made it;
//! - `same`: the writer call (`put_memory`, `tombstone`,
//!   `delete_tombstone`) with a provider that replays the call's log-clock
//!   and wall-clock readings, each exactly once; the returned record's
//!   canonical payload text or the refusal's class and text as the
//!   reference's, every reading read, and after it the rows appended
//!   (every column, the `returned` rows among them), the state's digest,
//!   the head's text and the FTS5 index as queries see it all equal the
//!   reference's;
//! - `not_ported`: refused as not ported with nothing written.
//!
//! Then the native fold check's replay of what the case left equals the
//! reference's `refold`. POSIX only, as the fixture is;
//! `store_records_not_posix.rs` records the gap elsewhere.

#![cfg(unix)]

mod common;

use bettermemory_core::store::open::open_for_writing;
use bettermemory_core::store::writers::RecordWriteError;
use common::opened::{self, differing, state};
use common::parity::Ledger;
use common::record_calls::{self, call, refusal};
use common::stores::scratch;
use common::txs::Replay;
use common::written;
use serde_json::{json, Value};

fn run(case: &Value, header: &Value) -> Result<usize, String> {
    let name = case["case"].as_str().expect("name");
    let store_id = header["store_id"].as_str().expect("id");
    let pragmas = header["pragmas"].as_array().expect("pragmas");
    let dir = scratch("records");
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
            let conn = store.connection();
            match edit["edit"].as_str().expect("edit") {
                "drop_returned" => conn.execute_batch("DROP TABLE returned").expect("drop"),
                "sql" => conn
                    .execute_batch(edit["sql"].as_str().expect("sql"))
                    .expect("sql"),
                _ => written::edit(edit, &store, &c, store_id),
            }
            continue;
        }
        let label = format!("{name} step {}", step["step"]);
        let mut clocks = Replay::of(step);
        let the_call = &step["call"];
        let before_seq = written::last_seq(&file);
        let before = state(&c, &root, store_id, pragmas);
        let outcome = call(&mut store, &mut clocks, the_call);
        if the_call["native"] != "same" {
            match &outcome {
                Err(RecordWriteError::NotPorted(_)) => {}
                other => return Err(format!("{label}: expected not ported, got {other:?}")),
            }
            let parts = differing(&before, &state(&c, &root, store_id, pragmas));
            if !parts.is_empty() {
                return Err(format!("{label}: a not-ported refusal wrote {parts:?}"));
            }
            checked += 1;
            continue;
        }
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
        if !clocks.drained() {
            return Err(format!(
                "{label}: clocks read less often than the reference: {} log, {} wall left",
                clocks.log.len(),
                clocks.wall.len()
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
fn every_writer_call_writes_as_the_reference() {
    let (header, cases) = record_calls::cases();
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
        "store_records.jsonl: {} cases, {calls} calls checked",
        cases.len()
    );
    let ledger = Ledger::new(
        "every_writer_call_writes_as_the_reference",
        "store_records.jsonl",
    );
    ledger.settle(cases.len(), &failures);
}
