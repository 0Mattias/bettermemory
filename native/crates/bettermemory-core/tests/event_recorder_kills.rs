//! Cases a plausible wrong `Recorder` fails, each read from the
//! reference's `events.Recorder.record`.

mod common;
mod event_recorder_run;

use serde_json::{json, Value};

fn row(over: Value) -> Value {
    let mut base = json!({
        "rc": ["proc-s", true, null, "", ""],
        "pub": null,
        "sup": false,
        "dl": null,
        "cap": "/w/captured",
        "fail": null,
        "kind": "write",
        "f": [],
    });
    for (k, v) in over.as_object().unwrap() {
        base[k] = v.clone();
    }
    base
}

fn s(text: &str) -> Value {
    json!({"t": "str", "v": text})
}

fn calls(r: &Value) -> &Vec<Value> {
    r["calls"].as_array().unwrap()
}

fn kw<'a>(r: &'a Value, name: &str) -> Option<&'a Value> {
    calls(r)[0][1]
        .as_array()
        .unwrap()
        .iter()
        .find(|p| p[0] == name)
        .map(|p| &p[1])
}

fn names(r: &Value) -> Vec<String> {
    calls(r)[0][1]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| p[0].as_str().unwrap().to_owned())
        .collect()
}

#[test]
fn telemetry_off_drops_measurement_and_keeps_state() {
    let off = json!(["p", false, null, "", ""]);
    let r = event_recorder_run::run(&row(json!({"rc": off, "kind": "recall_held"})));
    assert!(calls(&r).is_empty());
    assert_eq!(r["records"], false);
    let f = json!([["id", s("m")], ["extra", s("x")]]);
    let r = event_recorder_run::run(&row(json!({"rc": off, "f": f})));
    assert_eq!(
        names(&r),
        ["session", "actor", "best_effort", "attempts", "id"]
    );
}

#[test]
fn stamps_never_override_a_given_field() {
    let rc = json!(["p", true, "/w/proc", "cli_a", "hook"]);
    let f = json!([["attribution", s("mine")], ["worktree_root", {"t": "null"}]]);
    let r = event_recorder_run::run(&row(json!({"rc": rc, "f": f})));
    assert_eq!(kw(&r, "attribution"), Some(&s("mine")));
    assert_eq!(kw(&r, "worktree_root"), Some(&json!({"t": "null"})));
    assert_eq!(kw(&r, "triggered_from"), Some(&s("hook")));
}

#[test]
fn actor_none_falls_back_and_leaves_the_fields() {
    let pub_ = json!({"client": "c", "session": "q", "source": "env"});
    let f = json!([["actor", {"t": "null"}], ["session", {"t": "null"}]]);
    let r = event_recorder_run::run(&row(json!({"pub": pub_, "f": f})));
    assert_eq!(kw(&r, "actor"), Some(&json!({"t": "published"})));
    assert_eq!(kw(&r, "session"), Some(&json!({"t": "null"})));
    assert_eq!(names(&r).iter().filter(|n| *n == "actor").count(), 1);
}

#[test]
fn an_env_session_is_not_the_request_session() {
    let pub_ = json!({"session": "q", "source": "env"});
    let r = event_recorder_run::run(&row(json!({"pub": pub_})));
    assert_eq!(r["sfr"], "proc-s");
    let pub_ = json!({"session": "q", "source": "header"});
    let r = event_recorder_run::run(&row(json!({"pub": pub_})));
    assert_eq!(kw(&r, "session"), Some(&s("q")));
}

#[test]
fn suppressed_writes_no_state_row() {
    let r = event_recorder_run::run(&row(json!({"sup": true})));
    assert!(calls(&r).is_empty());
    assert_eq!(r["records"], false);
}

#[test]
fn deadline_only_inside_a_hook_body() {
    let r = event_recorder_run::run(&row(json!({})));
    assert!(kw(&r, "deadline").is_none());
    let r = event_recorder_run::run(&row(json!({"dl": 1.0})));
    assert!(kw(&r, "deadline").is_some());
}

#[test]
fn state_failure_raises_and_measurement_failure_warns() {
    let fail = json!(["OSError", "full"]);
    let r = event_recorder_run::run(&row(json!({"fail": fail})));
    assert_eq!(r["raised"], json!(["OSError", "full"]));
    assert_eq!(
        r["warn"],
        json!([{"w": "state row write not recorded, the call fails: OSError: full"}])
    );
    let r = event_recorder_run::run(&row(json!({"fail": fail, "kind": "recall_held"})));
    assert_eq!(r["raised"], Value::Null);
    assert_eq!(
        r["warn"],
        json!([{"w": "event recall_held not recorded: full"}])
    );
}

#[test]
fn harness_session_only_on_stamped_kinds() {
    let pub_ = json!({"harness": "h"});
    let r = event_recorder_run::run(&row(json!({"pub": pub_})));
    assert_eq!(kw(&r, "harness_session"), Some(&s("h")));
    let r = event_recorder_run::run(&row(json!({"pub": pub_, "kind": "recall_held"})));
    assert!(kw(&r, "harness_session").is_none());
}

#[test]
fn a_failed_capture_raises_on_a_measurement_row() {
    let pub_ = json!({"workspace": ["/w/d", "header"]});
    let cap = json!({"raise": "no origin"});
    let r = event_recorder_run::run(&row(
        json!({"pub": pub_, "cap": cap, "kind": "recall_held"}),
    ));
    assert_eq!(r["raised"], json!(["RuntimeError", "no origin"]));
    assert!(calls(&r).is_empty());
}

#[test]
fn a_colliding_field_state_fields_drops_raises_nothing() {
    let off = json!(["p", false, null, "", ""]);
    let f = json!([["best_effort", {"t": "bool", "v": true}]]);
    let r = event_recorder_run::run(&row(json!({"rc": off, "f": f})));
    assert_eq!(r["raised"], Value::Null);
    assert_eq!(calls(&r).len(), 1);
    let on = json!(["p", true, null, "", ""]);
    let r = event_recorder_run::run(&row(json!({"rc": on, "f": f})));
    assert_eq!(r["raised"][0], "TypeError");
    let f = json!([["deadline", {"t": "int", "v": "3"}]]);
    let r = event_recorder_run::run(&row(json!({"f": f, "kind": "recall_held"})));
    assert_eq!(kw(&r, "deadline"), Some(&json!({"t": "int", "v": "3"})));
}
