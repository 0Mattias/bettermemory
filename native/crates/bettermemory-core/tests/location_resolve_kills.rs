//! Cases a plausible wrong `location_resolve` fails: each pins one
//! choice of the reference's `_caller`, `_hooked` or `Caller`.

mod common;
mod location_resolve_support;

use std::cell::RefCell;
use std::rc::Rc;

use bettermemory_core::capture::location::to_location;
use bettermemory_core::capture::{Capture, State};
use bettermemory_core::location_resolve::{caller, hooked, resolve_caller, resolve_here, Caller};
use location_resolve_support::*;
use serde_json::{json, Value};

const REPO: &str =
    r#"{"state":"repo","kind":"primary","git_dir":"/w/.git","common_dir":"/w/.git","head":"h1"}"#;
const BARE: &str = r#"{"state":"repo","kind":"bare","git_dir":"/b.git","common_dir":"/b.git"}"#;

fn repo() -> Value {
    serde_json::from_str(REPO).unwrap()
}

fn go(case: Value) -> (Vec<Value>, Option<Caller<Fake<'static>>>) {
    let log: Log = Rc::new(RefCell::new(Vec::new()));
    let process = new_process();
    let mut cs = caps(&case, &log);
    let req = request(&case);
    let got = with_hands(&case, &log, &mut cs, &req, |h| {
        caller(&process, &req, None, h)
    })
    .ok();
    let calls = log.borrow().clone();
    (calls, got)
}

fn names(calls: &[Value]) -> Vec<String> {
    calls
        .iter()
        .map(|c| c[0].as_str().unwrap().to_owned())
        .collect()
}

#[test]
fn a_remint_that_wrote_no_token_still_forgets_the_capture() {
    let (calls, _) = go(
        json!({"cwd": "/w", "caps": [repo()], "writes": ["a"], "remint": [null, "skipped:contended"]}),
    );
    assert_eq!(names(&calls).last().unwrap(), "forget");
}

#[test]
fn no_remint_no_forget() {
    let (calls, _) = go(json!({"cwd": "/w", "caps": [repo()]}));
    assert!(!names(&calls).contains(&"forget".to_owned()));
}

#[test]
fn an_empty_git_dir_writes_nothing() {
    let mut cap = repo();
    cap["git_dir"] = json!("");
    let (calls, _) = go(json!({"cwd": "/w", "caps": [cap], "writes": ["a"]}));
    assert!(!names(&calls).contains(&"remint".to_owned()));
    assert!(!names(&calls).contains(&"forget".to_owned()));
}

#[test]
fn the_second_capture_skips_the_cache_and_rebinds_the_writer() {
    let bare: Value = serde_json::from_str(BARE).unwrap();
    let (calls, got) =
        go(json!({"cwd": "/w", "caps": [repo(), bare], "early": true, "writes": ["a"]}));
    assert_eq!(calls[4], json!(["forget", "/w"]));
    assert_eq!(calls[5], json!(["capture", "/w", true, 0.3, false]));
    let remint = calls.iter().find(|c| c[0] == "remint").unwrap();
    assert_eq!(remint, &json!(["remint", "/b.git", true, true, "a"]));
    assert_eq!(got.unwrap().history.map(|h| s(&h)), Some("/b.git".into()));
}

#[test]
fn the_model_is_made_after_the_capture_and_recorded_before_the_tree_test() {
    let (calls, _) = go(json!({"cwd": "/w", "caps": [repo()]}));
    assert_eq!(
        names(&calls),
        ["capture", "model", "record", "earlier", "resolve"]
    );
}

#[test]
fn file_errors_come_from_the_capture_resolved() {
    let mut late = repo();
    late["file_errors"] = json!(["late"]);
    let mut early = repo();
    early["file_errors"] = json!(["early"]);
    let (_, got) = go(json!({"cwd": "/w", "caps": [early, late], "early": true}));
    assert_eq!(got.unwrap().resolution.lines, vec![text("late")]);
}

#[test]
fn a_given_identity_opens_no_machine_and_a_failed_open_captures_nothing() {
    let (calls, _) = go(json!({"cwd": "/w", "caps": [repo()]}));
    assert!(!names(&calls).contains(&"open".to_owned()));
    let (calls, got) = go(json!({"id": null, "open": null, "cwd": "/w", "caps": [repo()]}));
    assert_eq!(names(&calls), ["open", "machineless"]);
    assert_eq!(s(&got.unwrap().resolution.rule), "machineless");
}

#[test]
fn a_machine_is_recorded_once_per_store() {
    let case = json!({"cwd": null, "session": "s"});
    let log: Log = Rc::new(RefCell::new(Vec::new()));
    let process = new_process();
    let req = request(&case);
    for _ in 0..2 {
        let mut cs = caps(&case, &log);
        with_hands(&case, &log, &mut cs, &req, |h| {
            resolve_caller(&process, &req, h)
        })
        .unwrap();
    }
    let records = names(&log.borrow())
        .iter()
        .filter(|n| *n == "record")
        .count();
    assert_eq!(records, 1);
}

#[test]
fn a_stand_that_answers_skips_the_ladder() {
    let case = json!({"cwd": "/w", "caps": [repo()], "sr": {"o": "silent", "p": "q", "c": "k2"}});
    let log: Log = Rc::new(RefCell::new(Vec::new()));
    let process = new_process();
    let mut cs = caps(&case, &log);
    let req = request(&case);
    let stand = bettermemory_core::location_caller::Stand {
        machine: text("m1"),
        path: text("/w"),
        resolution: resolution(&json!({"o": "silent", "p": "p", "c": "k"})),
    };
    let got = with_hands(&case, &log, &mut cs, &req, |h| {
        caller(&process, &req, Some(&stand), h)
    })
    .unwrap();
    assert!(!names(&log.borrow()).contains(&"resolve".to_owned()));
    assert_eq!(got.resolution.project, Some(text("q")));
}

#[test]
fn a_hook_keeps_a_stand_only_with_a_session_and_a_checkout() {
    for (session, checkout, kept) in [
        (Some("s"), Some("k"), 1),
        (None, Some("k"), 0),
        (Some("s"), None, 0),
    ] {
        let case = json!({"id": null, "open": "m1", "cwd": "/w", "caps": [repo()], "session": session,
            "res": {"o": "silent", "p": "p", "c": checkout}});
        let log: Log = Rc::new(RefCell::new(Vec::new()));
        let process = new_process();
        let mut cs = caps(&case, &log);
        let req = request(&case);
        with_hands(&case, &log, &mut cs, &req, |h| {
            hooked(
                &process,
                &req.store_path,
                req.policy,
                req.cwd.as_deref(),
                req.session.as_deref(),
                h,
            )
        })
        .unwrap();
        assert_eq!(process.stands.keys().len(), kept);
    }
}

fn home_of(
    outcome: &str,
    project: Option<&str>,
    checkout: Option<&str>,
    head: Option<&str>,
) -> (Vec<Value>, Vec<Option<String>>) {
    let log: Log = Rc::new(RefCell::new(Vec::new()));
    let mut cap = Capture::new(text("/w"), State::Repo);
    cap.head = head.map(text);
    let mut who = Caller {
        location: to_location(&cap, &text("m1"), None),
        history: None,
        resolution: resolution(&json!({"o": outcome, "p": project, "c": checkout})),
        model: Fake {
            case: json!({}),
            log: log.clone(),
            writer: None,
        },
    };
    let h = who.home().unwrap();
    who.add_head_witness(&text("write")).unwrap();
    let calls = log.borrow().clone();
    (calls, vec![Some(s(&h.project)), h.checkout.map(|c| s(&c))])
}

#[test]
fn deferred_with_a_project_is_homed_provisionally() {
    let (calls, home) = home_of("deferred", Some("p"), Some("k"), Some("h"));
    assert_eq!(home[0].as_deref(), Some("prov"));
    assert_eq!(calls[0][0], "unresolved");
}

#[test]
fn an_empty_checkout_adds_no_at_and_global_adds_no_witness() {
    let (calls, _) = home_of("silent", Some("p"), Some(""), Some("h"));
    assert_eq!(calls, vec![json!(["witness", "p", "h", "write", null])]);
    let (calls, home) = home_of("global", Some("global"), Some("k"), Some("h"));
    assert!(calls.is_empty());
    assert_eq!(home[0].as_deref(), Some("global"));
    let (calls, _) = home_of("silent", Some("p"), Some("k"), Some(""));
    assert!(calls.is_empty());
}

#[test]
fn a_failed_open_gives_machineless_the_cwd_here_resolved_and_the_stand() {
    let case = json!({"cwd": null, "id": null, "open": null, "session": "s"});
    let log: Log = Rc::new(RefCell::new(Vec::new()));
    let process = new_process();
    let mut cs = caps(&case, &log);
    let req = request(&case);
    let getcwd = || Some(text("/g"));
    with_hands(&case, &log, &mut cs, &req, |h| {
        resolve_here(&process, &req, getcwd, h)
    })
    .unwrap();
    assert_eq!(
        log.borrow().last().unwrap(),
        &json!(["machineless", "no state", "/g", "s", null])
    );
    let stand = bettermemory_core::location_caller::Stand {
        machine: text("m1"),
        path: text("/p"),
        resolution: resolution(&json!({"o": "silent", "p": "p", "c": "k"})),
    };
    let log: Log = Rc::new(RefCell::new(Vec::new()));
    let mut cs = caps(&case, &log);
    with_hands(&case, &log, &mut cs, &req, |h| {
        caller(&process, &req, Some(&stand), h)
    })
    .unwrap();
    assert_eq!(
        log.borrow().last().unwrap(),
        &json!(["machineless", "no state", null, "s", "/p"])
    );
}

#[test]
fn a_failing_earlier_tree_or_model_leaves_before_the_lock() {
    for case in [
        json!({"cwd": "/w", "caps": [repo()], "early_raises": true}),
        json!({"cwd": "/w", "caps": [repo()], "model_raises": true}),
    ] {
        let (calls, got) = go(case);
        assert!(got.is_none());
        let n = names(&calls);
        assert!(!n.contains(&"resolve".to_owned()));
    }
}
