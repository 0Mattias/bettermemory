//! Native transactions against `store_tx.jsonl`: every case replayed from
//! the store `store_create.json` pins, opened for writing natively, then
//! each step:
//!
//! - an edit, made as the generator made it;
//! - `same`: a [`Transaction`] around the step's items (mutations, nested
//!   scopes, failures, `release_returned`, records named for the commit),
//!   with a provider that replays the step's log-clock and wall-clock
//!   readings, each exactly once; committed or refused as the reference
//!   (class and text), each failed scope's error as the reference's, every
//!   reading read, and after it the rows the commit appended (every column,
//!   byte for byte, the `returned` rows among them), the whole state's
//!   digest, the head's text and the FTS5 index as queries see it all equal
//!   the reference's;
//! - `not_ported:<class>`: refused as that class with nothing written.
//!
//! Then the native fold check's replay of what the case left equals the
//! reference's `refold`. POSIX only, as the fixture is;
//! `store_tx_not_posix.rs` records the gap elsewhere.

#![cfg(unix)]

mod common;

use bettermemory_core::store::open::open_for_writing;
use bettermemory_core::store::write::WriteError;
use common::opened::{self, differing, state};
use common::parity::Ledger;
use common::stores::scratch;
use common::txs::{self, run_items, same_refusal, Failed, Replay};
use common::written;
use serde_json::Value;

fn run(case: &Value, header: &Value) -> Result<usize, String> {
    let name = case["case"].as_str().expect("name");
    let store_id = header["store_id"].as_str().expect("id");
    let pragmas = header["pragmas"].as_array().expect("pragmas");
    let dir = scratch("tx");
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
    let mut last_same = false;
    for step in case["steps"].as_array().expect("steps") {
        if let Some(edit) = step.get("edit") {
            let conn = store.connection();
            match edit["edit"].as_str().expect("edit") {
                "drop_returned" => {
                    conn.execute_batch("DROP TABLE returned").expect("drop");
                }
                _ => written::edit(edit, &store, &c, store_id),
            }
            continue;
        }
        let label = format!("{name} step {}", step["step"]);
        let mut clocks = Replay::of(step);
        let items = step["tx"].as_array().expect("items");
        let before_seq = written::last_seq(&file);
        let before = state(&c, &root, store_id, pragmas);
        let mut errors = Vec::new();
        let outcome: Result<(), Failed> = (|| {
            let mut tx = store.begin(&mut clocks)?;
            run_items(&mut tx, items, &mut errors)?;
            tx.commit()?;
            Ok(())
        })();
        let native = step["native"].as_str().expect("native");
        last_same = native == "same";
        if let Some(class) = native.strip_prefix("not_ported:") {
            match &outcome {
                Err(Failed::Write(WriteError::NotPorted(n))) if n.name() == class => {}
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
            (Ok(()), true) => {}
            (Err(e), false) if same_refusal(&e.refusal(), &want["refusal"]) => {}
            (got, _) => return Err(format!("{label}: got {got:?}, the reference {want}")),
        }
        let want_errors = step["scope_errors"].as_array().expect("scope errors");
        if errors.len() != want_errors.len()
            || !errors
                .iter()
                .zip(want_errors)
                .all(|(g, w)| same_refusal(g, w))
        {
            return Err(format!(
                "{label}: scope errors {errors:?}, the reference {want_errors:?}"
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
        let full = state(&c, &root, store_id, pragmas);
        let digest = written::state_digest(&full);
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
    if last_same {
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
fn every_transaction_writes_as_the_reference() {
    let (header, cases) = txs::cases();
    let mut failures = Vec::new();
    let mut steps = 0;
    for case in &cases {
        match run(case, &header) {
            Ok(n) => steps += n,
            Err(e) => failures.push(e),
        }
    }
    assert!(steps > 0 || !failures.is_empty(), "no transaction checked");
    eprintln!(
        "store_tx.jsonl: {} cases, {steps} transactions checked",
        cases.len()
    );
    let ledger = Ledger::new(
        "every_transaction_writes_as_the_reference",
        "store_tx.jsonl",
    );
    ledger.settle(cases.len(), &failures);
}
