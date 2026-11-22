//! Cases a plausible wrong port of the settlement half fails: the window's
//! strict bounds, the empty fallback, the empty reply, the warning's
//! spelling and what propagates.

mod common;

use common::hook_settle::run_row;
use serde_json::{json, Value};

fn stop() -> Value {
    json!({"recent": ["e"], "session_id": "s", "user_message": "q",
           "assistant_response": "a", "worktree_root": null, "client_model": null,
           "server_session": "srv", "harness": null, "prompt_id": null,
           "reply_source": null, "transcript_path": null, "now": 10_000,
           "previous_audit": null, "excluded_scopes": [], "probe_mode": "hybrid",
           "repeat": false, "settled_count": 0})
}

fn world(first_seen: i64) -> Value {
    json!({"settled": ["a", "b"], "shown": ["c"], "miss": true,
           "first_seen": first_seen, "joined": []})
}

fn since_of(log: &[Value]) -> Value {
    let review = log.iter().find(|c| c[0] == "review_unanswered").unwrap();
    review[1]["since"].clone()
}

#[test]
fn the_window_opens_at_the_segment_only_strictly_inside() {
    let s = stop();
    for (first, want) in [
        (6_400, 6_400),
        (6_401, 6_401),
        (9_999, 9_999),
        (10_000, 6_400),
    ] {
        let (log, _) = run_row("count_stop_reviews", &s, &world(first));
        assert_eq!(since_of(&log), json!(want), "first seen {first}");
    }
    let (log, _) = run_row("count_stop_reviews", &s, &world(6_399));
    assert_eq!(since_of(&log), json!(6_400));
}

#[test]
fn a_previous_audit_skips_the_segment_even_in_the_future() {
    let mut s = stop();
    s["previous_audit"] = json!(20_000);
    let (log, _) = run_row("count_stop_reviews", &s, &world(9_000));
    assert!(log.iter().all(|c| c[0] != "segment"));
    assert_eq!(since_of(&log), json!(20_000));
}

#[test]
fn the_segment_key_defaults_an_empty_harness_but_reviews_get_it_as_given() {
    let mut s = stop();
    s["harness"] = json!("");
    let (log, _) = run_row("count_stop_reviews", &s, &world(1));
    assert_eq!(log[0][1]["_1"], json!(["claude-code", "s"]));
    let review = log.iter().find(|c| c[0] == "review_unanswered").unwrap();
    assert_eq!(review[1]["harness"], json!(""));
}

#[test]
fn no_joined_session_falls_back_to_the_server_session_even_empty() {
    let mut s = stop();
    s["server_session"] = json!("");
    let (log, _) = run_row("count_stop_reviews", &s, &world(1));
    let review = log.iter().find(|c| c[0] == "review_unanswered").unwrap();
    assert_eq!(review[1]["sessions"], json!([""]));
    s["server_session"] = Value::Null;
    let (log, _) = run_row("count_stop_reviews", &s, &world(1));
    let review = log.iter().find(|c| c[0] == "review_unanswered").unwrap();
    assert_eq!(review[1]["sessions"], json!([]));
}

#[test]
fn an_empty_reply_settles_nothing_but_still_joins() {
    let mut s = stop();
    s["assistant_response"] = json!("");
    let (log, out) = run_row("settle_stop_turn", &s, &world(1));
    assert_eq!(out["r"], json!(0));
    assert_eq!(log.len(), 1);
    s["assistant_response"] = json!("a");
    s["user_message"] = json!("");
    let (log, out) = run_row("settle_stop_turn", &s, &world(1));
    assert_eq!(out["r"], json!(3));
    assert_eq!(log[2][1]["exclude"], json!(["a", "b"]));
}

#[test]
fn an_empty_reply_is_present_and_a_repeat_records_no_miss() {
    let mut s = stop();
    s["assistant_response"] = json!("");
    s["repeat"] = json!(true);
    let (log, _) = run_row("record_stop_rows", &s, &world(1));
    assert_eq!(log.len(), 2);
    assert_eq!(log[0][1]["assistant_present"], json!(true));
}

#[test]
fn the_warning_names_class_and_text_and_a_base_exception_propagates() {
    let s = stop();
    let mut w = world(1);
    w["raise"] = json!(["settle_interrupted", "Boom", "x: y"]);
    let (log, out) = run_row("settle_stop_interrupted", &s, &w);
    assert_eq!(out["r"], Value::Null);
    assert_eq!(
        log[1],
        json!(["warning", {"_0": "interrupted turns not settled at the Stop: Boom: x: y"}])
    );
    w["raise"] = json!(["settle_interrupted", "Halt", "x"]);
    let (log, out) = run_row("settle_stop_interrupted", &s, &w);
    assert_eq!(out["x"], json!("Halt"));
    assert_eq!(log.len(), 1);
}
