//! Hostile sizes through `run_audit` over the fakes of `gen_hook_stop`:
//! a window of many events and a very long prompt, with no panic and time
//! growing with the input.

mod common;
mod timing;

use common::hook_stop::run_row;
use serde_json::{json, Value};

fn row(events: usize, prompt: usize) -> (Value, Value) {
    let window: Vec<String> = (0..events).map(|i| format!("e{i}")).collect();
    (
        json!({"user_message": "x".repeat(prompt), "assistant_response": "r",
               "session_id": "s", "config": true, "store": true, "state": null}),
        json!({"cfg": {"label": "C"}, "window": window, "scopes": ["a"], "dup": true}),
    )
}

#[test]
fn a_large_window_and_prompt_answer() {
    let (a, w) = row(100_000, 1 << 20);
    let (log, out) = run_row(&a, &w);
    assert_eq!(out["r"], json!({"report": "R-probe"}));
    assert!(log.len() < 40);
}

#[test]
fn time_grows_with_the_window() {
    let small = row(2_000, 2_000);
    let large = row(20_000, 20_000);
    timing::grows_within(
        "run_audit: 2,000 and 20,000 events",
        timing::TENFOLD,
        &mut || timing::once(|| run_row(&small.0, &small.1)),
        &mut || timing::once(|| run_row(&large.0, &large.1)),
    );
}
