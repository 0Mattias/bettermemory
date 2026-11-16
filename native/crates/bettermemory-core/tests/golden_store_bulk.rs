//! Native chunked runs, `batch`, `rename_scope` and `prune_tombstones`
//! against `store_bulk.jsonl`: every case replayed from the store
//! `store_create.json` pins, opened for writing natively, its setup made
//! with the native writers under the setup's one stamp, then each call
//! with a provider that replays every clock the reference read (the log
//! clock, the wall clock, the mint, the monotonic clock), each once and
//! in the recorded order, with every pause and every chunk's begin and
//! commit in the same place; the returned value or the refusal's class and
//! text, the rows appended (count, kinds, digest), the state's digest, the
//! head's text and the FTS5 index all equal the reference's; then the
//! native fold check's replay equals the reference's `refold`. A call
//! marked `not_ported` is refused as not ported with nothing written.
//! POSIX only,
//! as the fixture is; `store_bulk_not_posix.rs` records the gap elsewhere.

#![cfg(unix)]

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::store::open::{open_for_writing, WritableStore};
use bettermemory_core::store::writers::RecordWriteError;
use bettermemory_core::unicode;
use common::bulk::{self, batch_items, boundaries, renamed, same_refusal, Failed, Replay};
use common::opened::{self, differing, state};
use common::parity::Ledger;
use common::stores::scratch;
use common::written::{self, py_dumps};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn call(store: &mut WritableStore, step: &Value, p: &mut Replay) -> Result<Value, Failed> {
    let c = &step["call"];
    let session = c["session"].as_str();
    let order = p.order.clone();
    let mut hook = boundaries(&order);
    match c["call"].as_str().expect("call") {
        "rename" => {
            let got = store.rename_scope_hooked(
                p,
                &unicode::cps(c["old"].as_str().expect("old")),
                &unicode::cps(c["new"].as_str().expect("new")),
                c["tomb"].as_bool().expect("tomb"),
                session,
                Some(&mut hook),
            )?;
            Ok(renamed(&got.value))
        }
        "prune" => {
            let now = c["now"]
                .as_str()
                .map(|t| DateTime::from_isoformat(&unicode::cps(t)).expect("now"));
            let older = i128::from(c["older_s"].as_i64().expect("older")) * 1_000_000;
            let got = store.prune_tombstones_hooked(p, older, now, session, Some(&mut hook))?;
            Ok(bulk::ids(&got.value))
        }
        "batch" => {
            let items = c["items"].as_array().expect("items");
            store.batch(p, Some(&mut hook), |tx| batch_items(tx, items, session))?;
            Ok(Value::Null)
        }
        other => panic!("an unknown call {other}"),
    }
}

fn run(case: &Value, header: &Value) -> Result<usize, String> {
    let name = case["case"].as_str().expect("name");
    let store_id = header["store_id"].as_str().expect("id");
    let pragmas = header["pragmas"].as_array().expect("pragmas");
    let at = DateTime::from_isoformat(&unicode::cps(header["setup_ts"].as_str().expect("setup")))
        .expect("setup ts");
    let dir = scratch("bulk");
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
    let mut first_call = true;
    for step in case["steps"].as_array().expect("steps") {
        if let Some(edit) = step.get("edit") {
            bulk::setup(edit, &mut store, at);
            continue;
        }
        let label = format!("{name} step {}", step["step"]);
        let mut p = Replay::of(step);
        if let Some(lock) = case["lock"].as_str() {
            if first_call {
                p.lock = Some((file.clone(), lock.to_owned()));
            }
        }
        first_call = false;
        let before = written::last_seq(&file);
        let as_was = state(&c, &root, store_id, pragmas);
        let got = call(&mut store, step, &mut p);
        if let Some(other) = p.held.take() {
            other.execute_batch("ROLLBACK").expect("release");
            drop(other);
        }
        if step["call"]["native"] == "not_ported" {
            match &got {
                Err(Failed::Write(RecordWriteError::NotPorted(_))) => {}
                other => return Err(format!("{label}: expected not ported, got {other:?}")),
            }
            let parts = differing(&as_was, &state(&c, &root, store_id, pragmas));
            if !parts.is_empty() {
                return Err(format!("{label}: a not-ported refusal wrote {parts:?}"));
            }
            checked += 1;
            continue;
        }
        let want = &step["outcome"];
        match (&got, want["written"].as_bool().expect("written")) {
            (Ok(v), true) if *v == want["returned"] => {}
            (Err(e), false) if same_refusal(&e.refusal(), &want["refusal"]) => {}
            (got, _) => {
                let got = got.as_ref().map_err(Failed::refusal);
                return Err(format!("{label}: got {got:?}, the reference {want}"));
            }
        }
        let mut order: Vec<String> = p.order.borrow().clone();
        // The reference's trace names a statement as it starts: a BEGIN or
        // COMMIT the busy database refused is in its reads, where the
        // native hook marks only a boundary passed. That one statement,
        // last, is the step's refusal.
        let busy = want["refusal"]["class"] == "OperationalError";
        let reads = step["reads"].as_array().expect("reads");
        if busy && reads.len() == order.len() + 1 {
            if let Some(last @ ("begin" | "commit")) = reads.last().and_then(Value::as_str) {
                order.push(last.to_owned());
            }
        }
        if json!(order) != step["reads"] {
            return Err(format!(
                "{label}: reads {order:?}, the reference {}",
                step["reads"]
            ));
        }
        if !p.drained() {
            return Err(format!("{label}: a clock was read less often"));
        }
        let rows = written::log_rows(&file, before);
        let list = rows.as_array().expect("rows");
        let digest = hex(&Sha256::digest(py_dumps(&rows).as_bytes()));
        let kinds: Vec<Value> = list.iter().map(|r| r[3].clone()).collect();
        let summary = json!({"count": list.len(), "kinds": kinds, "sha256": digest});
        if summary != step["rows"] {
            return Err(format!(
                "{label}: rows {summary}, the reference {}",
                step["rows"]
            ));
        }
        let full = state(&c, &root, store_id, pragmas);
        let st = written::state_digest(&full);
        if st != step["state"] {
            return Err(format!(
                "{label}: the state differs\nwant {}\ngot  {st}",
                step["state"]
            ));
        }
        let fts = written::fts_view(&file);
        if fts != step["fts"] {
            return Err(format!("{label}: the index differs"));
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

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

#[test]
fn every_chunked_call_writes_as_the_reference() {
    let (header, cases) = bulk::cases();
    let mut failures = Vec::new();
    let mut steps = 0;
    for case in &cases {
        match run(case, &header) {
            Ok(n) => steps += n,
            Err(e) => failures.push(e),
        }
    }
    assert!(steps > 0 || !failures.is_empty(), "no call checked");
    eprintln!(
        "store_bulk.jsonl: {} cases, {steps} calls checked",
        cases.len()
    );
    let ledger = Ledger::new(
        "every_chunked_call_writes_as_the_reference",
        "store_bulk.jsonl",
    );
    ledger.settle(cases.len(), &failures);
}
