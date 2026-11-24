//! Every case of `log_replaced_store.jsonl`: `append_history_replaced` on
//! the store `store_create.json` pins, opened for writing natively, each
//! step called as the reference called it: with no transaction open
//! ([`append_history_replaced`]), or inside one that commits or fails
//! after the call ([`append_history_replaced_in`]); with a kind that is
//! not one; on a connection under `PRAGMA query_only`. The answer, the
//! error's text, the rows appended (every column, byte for byte), the head
//! file's text and the clock readings equal the reference's. POSIX only,
//! as `store_tx.jsonl` is; `log_replaced_store_not_posix.rs` records the
//! gap elsewhere.

#![cfg(unix)]

mod common;

use bettermemory_core::json;
use bettermemory_core::log::replaced::{
    append_history_replaced, append_history_replaced_in, Appended, ReplacedError,
};
use bettermemory_core::store::open::open_for_writing;
use bettermemory_core::store::write::WriteError;
use bettermemory_core::unicode;
use common::opened;
use common::stores::scratch;
use common::txs::Replay;
use common::written;
use common::{assert_count, jsonl, text_field};
use serde_json::{json, Value};

fn answer_json(answer: &Option<Appended>) -> Value {
    match answer {
        Some(a) => json!({"seq": a.seq, "mac": a.mac}),
        None => Value::Null,
    }
}

fn outcome(result: Result<Option<Appended>, ReplacedError>, failed: bool) -> Value {
    match result {
        Ok(answer) if failed => json!({"answer": answer_json(&answer), "owner_failed": true}),
        Ok(answer) => json!({"answer": answer_json(&answer)}),
        Err(ReplacedError::NotAKind(text)) => {
            json!({"error": "ValueError", "text": common::str_output(&text)})
        }
        Err(ReplacedError::Write(WriteError::Sqlite(_))) => json!({"error": "OperationalError"}),
        Err(other) => json!({"error": format!("{other:?}")}),
    }
}

fn expected_outcome(step: &Value) -> Value {
    if let Some(error) = step.get("error") {
        let mut out = json!({"error": error});
        if step.get("text").is_some() || step.get("text_cps").is_some() {
            out["text"] = common::str_output(&text_field(step, "text"));
        }
        return out;
    }
    let mut out = json!({"answer": step["answer"]});
    if step.get("owner_failed").is_some() {
        out["owner_failed"] = json!(true);
    }
    out
}

#[test]
fn log_replaced_store_every_case() {
    let lines = jsonl("log_replaced_store.jsonl");
    let header = &lines[0];
    assert_eq!(header["fixture"], "log_replaced_store");
    let store_id = header["store_id"].as_str().expect("id");
    let mut checked = 0;
    let mut failures = Vec::new();
    for case in &lines[1..] {
        let name = case["case"].as_str().expect("case");
        let dir = scratch("replaced");
        let c = dir.0.join("c");
        let file = c.join("memory.sqlite");
        let head = c.join("keys").join(format!("{store_id}.head"));
        opened::made("created", &c);
        let provider = opened::provider(header);
        let mut store = open_for_writing(&file, &c.join("keys"), &provider).expect("open");
        for (i, step) in case["steps"].as_array().expect("steps").iter().enumerate() {
            let label = format!("{name} step {i}");
            let kind = text_field(step, "kind");
            let entry = json::loads(&unicode::cps(step["entry"].as_str().unwrap())).unwrap();
            let query_only = step["query_only"].as_bool().unwrap();
            if query_only {
                store
                    .connection()
                    .execute_batch("PRAGMA query_only = ON")
                    .unwrap();
            }
            let before = written::last_seq(&file);
            let mut clocks = Replay::of(step);
            let mode = step["mode"].as_str().unwrap();
            let got = if mode == "own" {
                outcome(
                    append_history_replaced(&mut store, &mut clocks, &kind, &entry),
                    false,
                )
            } else {
                let fails = mode == "joined_fails";
                let result: Result<Option<Appended>, ReplacedError> = (|| {
                    let mut tx = store.begin(&mut clocks)?;
                    let answer =
                        append_history_replaced_in(&mut tx, opened::ENGINE, &kind, &entry)?;
                    if !fails {
                        tx.commit()?;
                    }
                    Ok(answer)
                })();
                outcome(result, fails)
            };
            if query_only {
                store
                    .connection()
                    .execute_batch("PRAGMA query_only = OFF")
                    .unwrap();
            }
            let want = expected_outcome(step);
            if got != want {
                failures.push(format!("{label}: got {got}, the reference {want}"));
            }
            if !clocks.drained() {
                failures.push(format!(
                    "{label}: clocks read less often than the reference"
                ));
            }
            let rows = written::log_rows(&file, before);
            if rows != step["rows"] {
                failures.push(format!(
                    "{label}: rows {rows}, the reference {}",
                    step["rows"]
                ));
            }
            let head_text = std::fs::read_to_string(&head).ok();
            if json!(head_text) != step["head"] {
                failures.push(format!(
                    "{label}: head {head_text:?}, the reference {}",
                    step["head"]
                ));
            }
            checked += 1;
        }
        store.close().expect("close");
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
    let in_file: usize = lines[1..]
        .iter()
        .map(|c| c["steps"].as_array().unwrap().len())
        .sum();
    assert_count("log_replaced_store.jsonl", checked, in_file);
}

/// The joined form's savepoint (`_joined`, `store.py` 1252-1282). In the
/// reference the append's only write is one `INSERT` (`log.append_row`)
/// and nothing after it in `_append` can raise an ordinary exception, so
/// the one failure that can follow the inserted row is an interrupt (a
/// `BaseException` such as `KeyboardInterrupt`) before the call returns.
/// `_joined` then rolls back to its savepoint, and an owner that catches
/// the interrupt and commits commits no row and leaves the head where it
/// stood. Here the interrupt is a panic from the boundary hook right
/// after the insert, caught by the owner, which then commits.
#[test]
fn an_interrupt_after_the_joined_insert_leaves_no_row_for_the_owner_to_commit() {
    use bettermemory_core::store::write::WriteBoundary;
    use std::panic::{catch_unwind, AssertUnwindSafe};

    let lines = jsonl("log_replaced_store.jsonl");
    let header = &lines[0];
    let store_id = header["store_id"].as_str().expect("id");
    let step = lines[1..]
        .iter()
        .flat_map(|c| c["steps"].as_array().unwrap().iter())
        .find(|s| s["mode"] == "joined" && s["query_only"] == false)
        .expect("a joined step");
    let kind = text_field(step, "kind");
    let entry = json::loads(&unicode::cps(step["entry"].as_str().unwrap())).unwrap();
    let dir = scratch("replaced-interrupt");
    let c = dir.0.join("c");
    let file = c.join("memory.sqlite");
    let head = c.join("keys").join(format!("{store_id}.head"));
    opened::made("created", &c);
    let provider = opened::provider(header);
    let mut store = open_for_writing(&file, &c.join("keys"), &provider).expect("open");
    let before = written::last_seq(&file);
    let head_before = std::fs::read_to_string(&head).ok();
    let mut clocks = Replay::of(step);
    let mut hook = |b: WriteBoundary| {
        if b == WriteBoundary::Appended {
            panic!("interrupted after the insert");
        }
    };
    let mut tx = store.begin_with(&mut clocks, &mut hook).expect("begin");
    let caught = catch_unwind(AssertUnwindSafe(|| {
        append_history_replaced_in(&mut tx, opened::ENGINE, &kind, &entry)
    }));
    assert!(caught.is_err(), "the hook's panic reaches the owner");
    assert!(
        tx.rows().is_empty(),
        "the taken-back row is not the owner's"
    );
    tx.commit().expect("the owner commits");
    assert_eq!(written::last_seq(&file), before, "no row committed");
    assert_eq!(std::fs::read_to_string(&head).ok(), head_before);
    store.close().expect("close");
}
