//! The queue over real stores against `conflicts_queue_store.jsonl`:
//! each case replayed from the store `store_create.json` pins, opened for
//! writing natively, then each step: a memory put in a transaction of its
//! own, a statement run past the log as the generator ran it, or a queue
//! call whose batch is a transaction of its own ([`Batched`]), with a
//! provider that replays the step's clock readings and minted ids in the
//! reference's order. A `nested` step runs its calls inside one batch
//! held open (`WritableStore::batch`): each queue call joins it through
//! `Transaction`'s savepoint, an error is that call's outcome, and the
//! batch commits after the last. The answer or the error (its class, and
//! its text where it is the product's own), the warnings, every reading
//! read, the log rows the call appended (every column, byte for byte) and
//! the `conflicts` table after it all equal the reference's; the file's
//! cases and calls are the header's counts. POSIX only, as the fixture is
//! (`store_rows_written.jsonl`'s store); `conflicts_queue_not_posix.rs`
//! records the gap elsewhere.

#![cfg(unix)]

mod common;

use bettermemory_core::conflicts::{Batched, QueueError};
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::tables::list_conflicts;
use common::conflicts_queue::{cell, outcome, queue_call};
use common::parity::Ledger;
use common::rows::{self, Replay};
use common::stores::scratch;
use common::{jsonl, opened, written};
use serde_json::{json, Value};

fn table(store: &WritableStore) -> Value {
    match list_conflicts(store.connection()) {
        Ok(rows) => Value::Array(
            rows.iter()
                .map(|r| Value::Array(r.iter().map(cell).collect()))
                .collect(),
        ),
        Err(e) => json!({ "err": QueueError::Store(e).class() }),
    }
}

/// A queue step's call with the generator's defaults (`_queue_step`).
fn with_defaults(call: &Value) -> Value {
    let mut c = call.clone();
    for (key, default) in [("stamp", json!([])), ("session", Value::Null)] {
        if c.get(key).is_none() {
            c[key] = default;
        }
    }
    c
}

/// The answer of one queue call, or its error (`_queue_step`).
fn answered(got: Result<Value, QueueError>) -> Value {
    match got {
        Ok(v) => json!({ "r": v }),
        Err(e) => outcome(&e),
    }
}

/// A queue step: one call in a batch of its own, or (`nested`) its calls
/// inside one batch the caller holds open, which commits after the last.
fn queue_step(
    store: &mut WritableStore,
    call: &Value,
    header: &Value,
    path: &str,
    clocks: &mut Replay,
    warns: &mut Vec<String>,
) -> Result<Value, String> {
    if call["q"] != "nested" {
        let mut batched = Batched::new(store, clocks);
        return Ok(answered(queue_call(
            &mut batched,
            &with_defaults(call),
            header,
            path,
            warns,
        )));
    }
    let calls = call["calls"].as_array().expect("calls");
    let (outs, _) = store
        .batch(clocks, None, |tx| -> Result<Vec<Value>, QueueError> {
            Ok(calls
                .iter()
                .map(|c| answered(queue_call(tx, &with_defaults(c), header, path, warns)))
                .collect())
        })
        .map_err(|e| format!("the outer batch raised {}", e.class()))?;
    Ok(json!({ "r": outs }))
}

fn run(case: &Value, header: &Value) -> Result<usize, String> {
    let name = case["case"].as_str().expect("name");
    let dir = scratch("queue");
    let c = dir.0.join("c");
    let file = c.join("memory.sqlite");
    opened::made("created", &c);
    let provider = opened::provider(header);
    let mut store = open_for_writing(&file, &c.join("keys"), &provider)
        .map_err(|e| format!("{name}: the open refused: {e}"))?;
    let path = store.path().display().to_string();
    let mut checked = 0;
    for step in case["steps"].as_array().expect("steps") {
        if let Some(sql) = step.get("sql") {
            store
                .connection()
                .execute_batch(sql.as_str().expect("sql"))
                .map_err(|e| format!("{name}: {sql}: {e}"))?;
            continue;
        }
        let label = format!("{name} step {}", step["step"]);
        let call = &step["call"];
        let mut clocks = Replay::of(step);
        let before = written::last_seq(&file);
        let got = if call.get("call").is_some() {
            rows::run(&mut store, call, &mut clocks)
                .map(|_| json!({ "r": null }))
                .map_err(|e| format!("{label}: {e}"))?
        } else {
            let mut warns = Vec::new();
            let mut out = queue_step(&mut store, call, header, &path, &mut clocks, &mut warns)
                .map_err(|e| format!("{label}: {e}"))?;
            if !warns.is_empty() {
                let warns: Vec<String> =
                    warns.iter().map(|w| w.replace(&path, "<store>")).collect();
                out["warn"] = json!(warns);
            }
            out
        };
        if got != step["outcome"] {
            return Err(format!(
                "{label}: answered {got}, the reference {}",
                step["outcome"]
            ));
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
        let after = table(&store);
        if after != step["table"] {
            return Err(format!(
                "{label}: the table differs\nwant {}\ngot  {after}",
                step["table"]
            ));
        }
        checked += 1;
    }
    store.close().map_err(|e| format!("{name}: close: {e}"))?;
    Ok(checked)
}

#[test]
fn every_queue_call_writes_as_the_reference() {
    let name = "conflicts_queue_store.jsonl";
    let mut lines = jsonl(name);
    let header = lines.remove(0);
    // The file holds the cases and calls its header counts.
    let count = |key: &str| header[key].as_u64().expect("a count") as usize;
    let in_file: usize = lines
        .iter()
        .map(|case| {
            case["steps"]
                .as_array()
                .expect("steps")
                .iter()
                .filter(|s| s.get("sql").is_none())
                .count()
        })
        .sum();
    assert_eq!(
        lines.len(),
        count("cases"),
        "{name}: cases against the header"
    );
    assert_eq!(in_file, count("calls"), "{name}: calls against the header");
    let mut failures = Vec::new();
    let mut calls = 0;
    for case in &lines {
        match run(case, &header) {
            Ok(n) => calls += n,
            Err(e) => failures.push(e),
        }
    }
    assert!(calls > 0 || !failures.is_empty(), "no call checked");
    assert!(
        !failures.is_empty() || calls == count("calls"),
        "{name}: {calls} calls checked of the header's {}",
        count("calls")
    );
    eprintln!("{name}: {} cases, {calls} calls checked", lines.len());
    let ledger = Ledger::new("every_queue_call_writes_as_the_reference", name);
    ledger.settle(lines.len(), &failures);
}
