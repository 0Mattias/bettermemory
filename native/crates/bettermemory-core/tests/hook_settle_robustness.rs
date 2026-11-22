//! Hostile sizes through the settlement half over the fakes of
//! `gen_hook_settle`: many events, a megabyte reply, time bounds at the
//! integer extremes, with no panic and time growing with the input.

mod common;
mod timing;

use common::hook_settle::run_row;
use serde_json::{json, Value};

fn stop(events: usize, reply: usize) -> Value {
    let recent: Vec<String> = (0..events).map(|i| format!("e{i}")).collect();
    json!({"recent": recent, "session_id": "s", "user_message": "q",
           "assistant_response": "r".repeat(reply), "worktree_root": null,
           "client_model": null, "server_session": null, "harness": null,
           "prompt_id": null, "reply_source": null, "transcript_path": null,
           "now": 0, "previous_audit": null, "excluded_scopes": [],
           "probe_mode": "hybrid", "repeat": false, "settled_count": i64::MAX})
}

fn world() -> Value {
    let settled: Vec<String> = (0..1000).map(|i| format!("m{i}")).collect();
    json!({"settled": settled, "shown": ["x"], "miss": true,
           "first_seen": i64::MIN, "joined": []})
}

#[test]
fn large_inputs_and_integer_bounds_answer() {
    let s = stop(100_000, 1 << 20);
    let (_, out) = run_row("settle_stop_turn", &s, &world());
    assert_eq!(out["r"], json!(1001));
    let (log, _) = run_row("record_stop_rows", &s, &world());
    assert_eq!(log[0][1]["settled_count"], json!(i64::MAX));
    let (log, _) = run_row("count_stop_reviews", &s, &world());
    assert_eq!(log.last().unwrap()[1]["since"], json!(-3600));
}

#[test]
fn time_grows_with_the_window() {
    let small = stop(2_000, 2_000);
    let large = stop(20_000, 20_000);
    let w = world();
    timing::grows_within(
        "settle_stop_turn: 2,000 and 20,000 events",
        timing::TENFOLD,
        &mut || timing::once(|| run_row("settle_stop_turn", &small, &w)),
        &mut || timing::once(|| run_row("settle_stop_turn", &large, &w)),
    );
}
