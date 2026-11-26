//! Cases a plausible wrong port of `sessions` fails, each with the
//! reference's answer: the bridge's walk read forwards, `runs_beside`
//! with `>`, the worktree filter in the harness join, the stamps check
//! skipped, `min` keeping the later of two equal instants, `or` read as
//! is-None, and an unhashable trigger answered as absent.

use std::collections::BTreeSet;

use bettermemory_core::pyvalue::{PyError, PyStr, PyValue};
use bettermemory_core::sessions as ss;
use serde_json::{json, Value};

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn value(v: &Value) -> PyValue {
    match v {
        Value::Null => PyValue::None,
        Value::Bool(b) => PyValue::Bool(*b),
        Value::Number(n) => PyValue::Int(n.to_string()),
        Value::String(s) => PyValue::Str(cps(s)),
        Value::Array(items) => PyValue::List(items.iter().map(value).collect()),
        Value::Object(map) => PyValue::Dict(
            map.iter()
                .map(|(k, v)| (PyValue::Str(cps(k)), value(v)))
                .collect(),
        ),
    }
}

fn log(v: Value) -> Vec<PyValue> {
    v.as_array().unwrap().iter().map(value).collect()
}

fn ts(minute: u32) -> String {
    format!("2026-01-01T12:{minute:02}:00+00:00")
}

fn hook(session: &str, minute: u32) -> Value {
    json!({"kind": "turn_audited", "session": session,
        "triggered_from": "stop_hook", "ts": ts(minute)})
}

fn tool(session: &str, minute: u32, stamp: Option<&str>, worktree: Option<&str>) -> Value {
    let mut e = json!({"kind": "search", "session": session, "ts": ts(minute)});
    if let Some(s) = stamp {
        e["harness_session"] = json!(s);
    }
    if let Some(w) = worktree {
        e["worktree_root"] = json!(w);
    }
    e
}

fn some(s: &str) -> Result<Option<PyStr>, PyError> {
    Ok(Some(cps(s)))
}

fn set(v: &[&str]) -> Result<BTreeSet<PyStr>, PyError> {
    Ok(v.iter().map(|s| cps(s)).collect())
}

fn h(s: &str) -> Option<Vec<u32>> {
    Some(cps(s))
}

#[test]
fn the_newest_stamped_server_is_the_bridge() {
    let events = log(json!([
        hook("h1", 1),
        tool("sess_old", 2, Some("h1"), None),
        tool("sess_new", 3, Some("h1"), None),
        tool("sess_other", 4, None, None),
    ]));
    let me = h("h1");
    assert_eq!(
        ss::bridged_session(&events, me.as_deref(), None),
        some("sess_new")
    );
    assert_eq!(
        ss::joined_sessions(&events, me.as_deref(), None),
        set(&["sess_new", "sess_old"])
    );
    // Without a stamp the newest in-process event names the session.
    assert_eq!(
        ss::latest_in_process_session(&events, None),
        some("sess_other")
    );
}

#[test]
fn a_session_keeps_its_newest_stamp() {
    // sess_a is stamped h_x, then h_y; the newest (h_y) runs beside h_new
    // and sets sess_a aside. Read forwards, h_x (quiet) would keep it.
    let events = log(json!([
        hook("h_x", 1),
        hook("h_new", 2),
        tool("sess_a", 3, Some("h_x"), None),
        tool("sess_a", 4, Some("h_y"), None),
        hook("h_y", 5),
    ]));
    let me = h("h_new");
    assert_eq!(ss::bridged_session(&events, me.as_deref(), None), Ok(None));
}

#[test]
fn a_last_hook_row_at_the_first_runs_beside() {
    let events = log(json!([
        hook("h_a", 1),
        {"kind": "turn_audited", "session": "h_b", "triggered_from": "prompt_hook",
            "ts": "2026-01-01T14:03:00+02:00"},
        {"kind": "turn_audited", "session": "h_a", "triggered_from": "stop_hook",
            "ts": "2026-01-01T06:33:00-05:30"},
    ]));
    let spans = ss::hook_spans(&events).unwrap();
    assert!(ss::runs_beside(&spans, &cps("h_a"), &cps("h_b")));
    assert!(ss::runs_beside(&spans, &cps("h_b"), &cps("h_a")));
    assert!(!ss::runs_beside(&spans, &cps("h_none"), &cps("h_a")));
    assert!(ss::runs_beside(&spans, &cps("h_a"), &cps("h_none")));
    assert_eq!(
        ss::live_harness_sessions(&events, h("h_b").as_deref()),
        set(&["h_a"])
    );
    let short = log(json!([
        hook("h_a", 1),
        {"kind": "x", "session": "h_a", "triggered_from": "stop_hook",
            "ts": "2026-01-01T12:02:59.999999+00:00"},
        hook("h_b", 3),
    ]));
    let spans = ss::hook_spans(&short).unwrap();
    assert!(!ss::runs_beside(&spans, &cps("h_a"), &cps("h_b")));
}

#[test]
fn the_harness_join_ignores_the_worktree() {
    let events = log(json!([
        tool("sess_a", 1, Some("h1"), Some("/wt/b")),
        tool("sess_u", 2, None, Some("/wt/a")),
    ]));
    let me = h("h1");
    let wt = cps("/wt/a");
    assert_eq!(
        ss::bridged_session(&events, me.as_deref(), Some(&wt)),
        some("sess_a")
    );
    assert_eq!(
        ss::joined_sessions(&events, me.as_deref(), Some(&wt)),
        set(&["sess_a"])
    );
    assert_eq!(
        ss::harness_joined_sessions(&events, me.as_deref()),
        set(&["sess_a"])
    );
}

#[test]
fn a_clear_beside_another_windows_turn_sets_both_servers_aside() {
    let events = log(json!([
        hook("h_old", 1),
        tool("sess_a", 2, Some("h_old"), Some("/wt/a")),
        hook("h_b", 3),
        tool("sess_b", 4, Some("h_b"), Some("/wt/a")),
        hook("h_new", 5),
        tool("sess_a", 6, Some("h_old"), Some("/wt/a")),
        tool("sess_b", 7, Some("h_b"), Some("/wt/a")),
        tool("sess_u", 8, Some(""), Some("/wt/a")),
    ]));
    let me = h("h_new");
    let wt = cps("/wt/a");
    assert_eq!(
        ss::bridged_session(&events, me.as_deref(), None),
        some("sess_u")
    );
    assert_eq!(
        ss::bridged_session(&events, me.as_deref(), Some(&wt)),
        some("sess_u")
    );
    // The other window's server in another worktree: with the worktree
    // given one stamp counts and this window's own server is the bridge.
    let other = cps("/wt/b");
    let events = log(json!([
        hook("h_old", 1),
        tool("sess_a", 2, Some("h_old"), Some("/wt/a")),
        hook("h_b", 3),
        hook("h_new", 5),
        tool("sess_a", 6, Some("h_old"), Some("/wt/a")),
        tool("sess_b", 7, Some("h_b"), Some("/wt/b")),
    ]));
    assert_eq!(
        ss::bridged_session(&events, me.as_deref(), Some(&wt)),
        some("sess_a")
    );
    assert_eq!(ss::bridged_session(&events, me.as_deref(), None), Ok(None));
    // In the other worktree only the other window's server wrote: the
    // foreign join the reference documents.
    assert_eq!(
        ss::bridged_session(&events, me.as_deref(), Some(&other)),
        some("sess_b")
    );
}

#[test]
fn equal_instants_keep_the_earlier_rows_offset() {
    let events = log(json!([
        {"kind": "a", "session": "h_a", "triggered_from": "stop_hook",
            "ts": "2026-01-01T12:05:00+00:00"},
        {"kind": "a", "session": "h_a", "triggered_from": "stop_hook",
            "ts": "2026-01-01T14:05:00+02:00"},
        {"kind": "a", "session": "h_a", "triggered_from": "stop_hook",
            "ts": "2026-01-01T07:05:00-05:00"},
    ]));
    let spans = ss::hook_spans(&events).unwrap();
    let (first, last) = spans.get(&cps("h_a")).unwrap();
    assert_eq!(first.isoformat(), "2026-01-01T12:05:00+00:00");
    assert_eq!(last.isoformat(), "2026-01-01T12:05:00+00:00");
}

#[test]
fn every_falsy_session_hands_over_to_session_id() {
    for falsy in [
        json!(""),
        json!(0),
        json!(false),
        json!([]),
        json!({}),
        json!(null),
    ] {
        let events = log(json!([
            tool("sess_a", 1, None, None),
            {"kind": "search", "session": falsy, "session_id": "sess_legacy",
                "harness_session": "h1"},
            {"kind": "scope_disable", "session": falsy, "session_id": "sess_legacy",
                "scope": "s1"},
        ]));
        assert_eq!(
            ss::latest_in_process_session(&events, None),
            some("sess_legacy")
        );
        assert_eq!(ss::session_of(&events[1]), Some(&cps("sess_legacy")));
        assert_eq!(
            ss::harness_joined_sessions(&events, h("h1").as_deref()),
            set(&["sess_legacy"])
        );
        assert_eq!(
            ss::disabled_scopes_from_events(&events, None, None),
            set(&["s1"])
        );
    }
    // A truthy non-str is kept by `or` and then skipped.
    let events = log(json!([
        tool("sess_a", 1, None, None),
        {"kind": "search", "session": 5, "session_id": "sess_b"},
    ]));
    assert_eq!(ss::latest_in_process_session(&events, None), some("sess_a"));
    // The empty session_id `or` hands back is a session here, not in spans.
    let events = log(json!([
        {"kind": "search", "session": "", "session_id": ""},
        {"kind": "x", "session": "", "session_id": "", "triggered_from": "stop_hook",
            "ts": ts(1)},
    ]));
    assert_eq!(ss::latest_in_process_session(&events, None), some(""));
    assert!(ss::hook_spans(&events).unwrap().entries().is_empty());
}

#[test]
fn an_unhashable_trigger_raises_where_the_walk_reaches_it() {
    for bad in [json!(["stop_hook"]), json!({"a": 1}), json!([])] {
        let mut newest = tool("sess_a", 2, None, None);
        newest["triggered_from"] = bad.clone();
        let events = log(json!([tool("sess_a", 1, Some("h1"), None), newest]));
        let te = Err(PyError::TypeError);
        assert_eq!(ss::latest_in_process_session(&events, None), te);
        assert_eq!(
            ss::harness_joined_sessions(&events, h("h1").as_deref()),
            Err(PyError::TypeError)
        );
        assert_eq!(ss::hook_spans(&events), Err(PyError::TypeError));
        assert_eq!(
            ss::live_harness_sessions(&events, h("h1").as_deref()),
            Err(PyError::TypeError)
        );
        assert_eq!(
            ss::disabled_scopes_from_events(&events, None, None),
            Err(PyError::TypeError)
        );
        // A given anchor reads no trigger; an empty harness session walks
        // nothing.
        assert_eq!(
            ss::disabled_scopes_from_events(&events, None, Some(&cps("sess_a"))),
            set(&[])
        );
        assert_eq!(
            ss::live_harness_sessions(&events, h("").as_deref()),
            set(&[])
        );
        assert_eq!(ss::harness_joined_sessions(&events, None), set(&[]));
        // The oldest row: the reversed walks return before reaching it.
        let mut oldest = hook("h1", 1);
        oldest["triggered_from"] = bad;
        let events = log(json!([
            oldest,
            tool("sess_a", 2, Some("h2"), None),
            hook("h2", 3)
        ]));
        assert_eq!(ss::latest_in_process_session(&events, None), some("sess_a"));
        assert_eq!(
            ss::bridged_session(&events, h("h2").as_deref(), None),
            some("sess_a")
        );
        assert_eq!(
            ss::bridged_session(&events, h("h9").as_deref(), None),
            Err(PyError::TypeError)
        );
        assert_eq!(
            ss::joined_sessions(&events, h("h2").as_deref(), None),
            Err(PyError::TypeError)
        );
    }
}

#[test]
fn only_a_trigger_str_hides_a_row() {
    for other in [
        json!(null),
        json!(3),
        json!(true),
        json!("STOP_HOOK"),
        json!(""),
    ] {
        let mut row = tool("sess_b", 2, None, None);
        row["triggered_from"] = other;
        let events = log(json!([tool("sess_a", 1, None, None), row]));
        assert_eq!(ss::latest_in_process_session(&events, None), some("sess_b"));
    }
    for t in ss::OUT_OF_PROCESS_TRIGGERS {
        let mut row = tool("sess_b", 2, None, None);
        row["triggered_from"] = json!(t);
        let events = log(json!([tool("sess_a", 1, None, None), row]));
        assert_eq!(ss::latest_in_process_session(&events, None), some("sess_a"));
    }
}
