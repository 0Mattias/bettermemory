//! Cases a plausible wrong `run_audit` fails, over the fakes of
//! `gen_hook_stop` (`common::hook_stop`).

mod common;

use common::hook_stop::run_row;
use serde_json::{json, Value};

fn base() -> (Value, Value) {
    (
        json!({"user_message": "m", "assistant_response": "r", "session_id": "s",
               "client_model": "c", "config": true, "store": true, "cwd": "cwd",
               "harness": "h", "prompt_id": "p", "reply_source": "payload",
               "transcript_path": "T", "state": true, "posted": "posted"}),
        json!({"cfg": {"label": "C", "search_mode": "bm25", "telemetry": true},
               "window": ["e"], "wr": "W", "ss": "srv", "scopes": [], "prev": null,
               "dup": false, "settled": 1}),
    )
}

fn call<'a>(log: &'a [Value], name: &str) -> Option<&'a Value> {
    log.iter().find(|e| e[0] == name).map(|e| &e[1])
}

fn names(log: &[Value]) -> Vec<&str> {
    log.iter().map(|e| e[0].as_str().unwrap()).collect()
}

#[test]
fn an_empty_harness_keys_the_default() {
    // `harness or "claude-code"`: an `unwrap_or` reads "" as a harness.
    let (mut a, w) = base();
    a["harness"] = json!("");
    let (log, _) = run_row(&a, &w);
    assert_eq!(
        call(&log, "confirm_compaction").unwrap()["key"],
        json!(["claude-code", "s"])
    );
    // The settlements are handed the harness as given, not the key.
    assert_eq!(
        call(&log, "_settle_stop_turn").unwrap()["harness"],
        json!("")
    );
}

#[test]
fn an_empty_search_mode_probes_hybrid() {
    let (a, mut w) = base();
    w["cfg"]["search_mode"] = json!("");
    let (log, _) = run_row(&a, &w);
    assert_eq!(
        call(&log, "_record_stop_rows").unwrap()["probe_mode"],
        json!("hybrid")
    );
}

#[test]
fn a_dry_run_recurses_once_with_no_state() {
    let (mut a, w) = base();
    a["dry_run"] = json!(true);
    let (log, out) = run_row(&a, &w);
    let n = names(&log);
    assert_eq!(
        &n[..3],
        &["within_hook_budget", "suppressed", "within_hook_budget"]
    );
    assert_eq!(n.iter().filter(|x| **x == "RecallState").count(), 1);
    assert_eq!(
        &n[n.len() - 3..],
        &[
            "within_hook_budget_exit",
            "suppressed_exit",
            "within_hook_budget_exit"
        ]
    );
    assert_eq!(out["r"], json!({"report": "R-probe"}));
}

#[test]
fn the_default_store_is_not_budgeted() {
    // `hook_body` reads the `store=` it was passed, None here.
    let (mut a, w) = base();
    a["store"] = Value::Null;
    let (log, _) = run_row(&a, &w);
    assert_eq!(
        log[0],
        json!(["within_hook_budget", {"_0": "stop", "_1": null}])
    );
    assert_eq!(
        call(&log, "open_or_create").unwrap()["_0"],
        json!("D/memory.sqlite")
    );
}

#[test]
fn no_prompt_asks_no_dedup_and_an_empty_one_does() {
    let (mut a, mut w) = base();
    w["dup"] = json!(true);
    a["user_message"] = Value::Null;
    let (log, _) = run_row(&a, &w);
    assert!(call(&log, "redact_query").is_none());
    assert_eq!(
        call(&log, "_record_stop_rows").unwrap()["repeat"],
        json!(false)
    );
    a["user_message"] = json!("");
    let (log, _) = run_row(&a, &w);
    assert_eq!(call(&log, "redact_query").unwrap()["_0"], json!(""));
    assert_eq!(
        call(&log, "_record_stop_rows").unwrap()["repeat"],
        json!(true)
    );
}

#[test]
fn an_empty_reply_is_present() {
    // `assistant_response is not None`, not its truth.
    let (mut a, w) = base();
    a["assistant_response"] = json!("");
    let (log, _) = run_row(&a, &w);
    assert_eq!(
        call(&log, "_record_stop_rows").unwrap()["assistant_present"],
        json!(true)
    );
}

#[test]
fn the_clock_reads_in_order() {
    // The probe's `now`, the turn's `now`, then the dedup's: three reads.
    let (a, w) = base();
    let (log, _) = run_row(&a, &w);
    assert_eq!(call(&log, "_probe_message").unwrap()["now"], json!("N1"));
    assert_eq!(call(&log, "previous_audit_at").unwrap()["now"], json!("N2"));
    assert_eq!(
        call(&log, "is_duplicate_audit").unwrap()["now"],
        json!("N3")
    );
    assert_eq!(call(&log, "_settle_stop_turn").unwrap()["now"], json!("N2"));
    assert_eq!(call(&log, "events_since").unwrap()["_1"], json!("U1-3600"));
}

#[test]
fn a_raise_closes_what_is_open() {
    let (mut a, mut w) = base();
    a["dry_run"] = json!(true);
    w["raise"] = json!("_settle_stop_turn");
    let (log, out) = run_row(&a, &w);
    assert_eq!(out["x"], json!("_settle_stop_turn"));
    let n = names(&log);
    assert_eq!(n[n.len() - 4], "_settle_stop_turn");
    assert_eq!(
        &n[n.len() - 3..],
        &[
            "within_hook_budget_exit",
            "suppressed_exit",
            "within_hook_budget_exit"
        ]
    );
}
