//! Cases a plausible wrong port of `recall_counters` fails, each with the
//! reference's answer: `runs_beside` with its arguments swapped, the
//! stamped bridge kept, the row's own session read before its stamp, rows
//! at `end` joined, `bisect_left` for `bisect_right`, `>` for `>=` at the
//! show's instant, `median` for `median_low`, `suppressed` on the brief or
//! unsorted, `auto` read by truthiness, a pick that makes the section
//! None, a list kind answered False, the chains summed in 64 bits, the
//! pick run after the tally, and the latest grade taken by stamp.

use bettermemory_core::datetime::{parse_event_ts, DateTime};
use bettermemory_core::pyvalue::{PyError, PyStr, PyValue};
use bettermemory_core::recall_counters as rc;
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

fn ts(minute: u32) -> String {
    format!("2026-01-01T12:{minute:02}:00+00:00")
}

fn at(minute: u32) -> Option<DateTime> {
    parse_event_ts(&cps(&ts(minute)))
}

fn brief(session: &str, minute: u32, ids: &[&str], worktree: &str) -> Value {
    json!({"kind": "session_brief", "session": session, "ids": ids,
        "triggered_from": "session_hook", "ts": ts(minute), "worktree_root": worktree})
}

fn recall(session: &str, minute: u32, ids: &[&str]) -> Value {
    json!({"kind": "prompt_recall", "session": session, "shown_ids": ids, "rule": "r1",
        "triggered_from": "prompt_hook", "ts": ts(minute)})
}

/// A server's open of `id`, stamped when `stamp` is not empty.
fn open(minute: u32, id: &str, stamp: &str, worktree: &str) -> Value {
    let mut e = json!({"kind": "show", "session": "srv", "id": id, "ts": ts(minute),
        "worktree_root": worktree});
    if !stamp.is_empty() {
        e["harness_session"] = json!(stamp);
    }
    e
}

fn report(
    events: &[Value],
    start: Option<DateTime>,
    end: Option<DateTime>,
) -> Result<Option<PyValue>, PyError> {
    let events: Vec<PyValue> = events.iter().map(value).collect();
    rc::compute_surface_report::<PyError>(&events, start, end, None)
}

fn section(events: &[Value]) -> PyValue {
    report(events, None, None)
        .expect("no error")
        .expect("a section")
}

fn path<'a>(v: &'a PyValue, keys: &[&str]) -> &'a PyValue {
    keys.iter()
        .fold(v, |v, k| v.get(k).unwrap_or_else(|| panic!("no {k}")))
}

fn int(v: &PyValue, keys: &[&str]) -> String {
    match path(v, keys) {
        PyValue::Int(n) => n.clone(),
        other => panic!("{keys:?} is {other:?}"),
    }
}

#[test]
fn runs_beside_reads_the_stamp_against_the_bridge() {
    // A /clear: h_old's hooks stopped before h_new's began, so the
    // server's stamp h_old joins the bridge h_new. Swapped, runs_beside
    // asks whether h_new runs beside h_old, which it does.
    let s = section(&[
        brief("h_old", 1, &["m1"], "/wt/a"),
        brief("h_new", 5, &["m2"], "/wt/a"),
        open(6, "m2", "h_old", "/wt/a"),
    ]);
    assert_eq!(int(&s, &["brief", "opened"]), "1");
}

#[test]
fn a_stamped_conversation_is_never_the_bridge() {
    let s = section(&[
        brief("c1", 1, &["m1", "m2"], "/wt/a"),
        open(2, "m1", "c1", "/wt/b"),
        open(3, "m2", "", "/wt/a"),
    ]);
    assert_eq!(int(&s, &["brief", "opened"]), "1");
}

#[test]
fn the_stamp_comes_before_the_rows_own_session() {
    let mut row = open(2, "m1", "c2", "");
    row["session"] = json!("c1");
    let s = section(&[brief("c1", 1, &["m1"], ""), recall("c2", 1, &["m1"]), row]);
    assert_eq!(int(&s, &["brief", "opened"]), "0");
    assert_eq!(int(&s, &["recall", "opened"]), "1");
}

#[test]
fn rows_at_end_join_nothing() {
    let log = [brief("c1", 1, &["m1"], "/wt/a"), open(3, "m1", "", "/wt/a")];
    let s = report(&log, None, at(3)).unwrap().unwrap();
    assert_eq!(int(&s, &["brief", "opened"]), "0");
    let s = report(&log, None, at(4)).unwrap().unwrap();
    assert_eq!(int(&s, &["brief", "opened"]), "1");
    // A hook row at end does not move the bridge either.
    let log = [
        brief("c1", 1, &["m1"], "/wt/a"),
        brief("c2", 3, &["m9"], "/wt/a"),
        open(2, "m1", "", "/wt/a"),
    ];
    let s = report(&log, None, at(3)).unwrap().unwrap();
    assert_eq!(int(&s, &["brief", "opened"]), "1");
}

#[test]
fn a_reshow_at_the_same_instant_takes_the_follow_ups() {
    let s = section(&[
        brief("c1", 2, &["m1"], "/wt/a"),
        recall("c1", 2, &["m1"]),
        open(2, "m1", "c1", ""),
    ]);
    assert_eq!(int(&s, &["brief", "opened"]), "0");
    assert_eq!(int(&s, &["recall", "opened"]), "1");
}

#[test]
fn an_open_at_the_shows_instant_counts() {
    let s = section(&[brief("c1", 2, &["m1"], ""), open(2, "m1", "c1", "")]);
    assert_eq!(int(&s, &["brief", "opened"]), "1");
    assert_eq!(int(&s, &["brief", "deliveries_helped"]), "1");
}

#[test]
fn chars_p50_is_median_low() {
    let mut log = Vec::new();
    for chars in [4, 1, 3, 2] {
        let mut show = recall("c1", 1, &["m1"]);
        show["chars"] = json!(chars);
        log.push(show);
    }
    assert_eq!(int(&section(&log), &["recall", "chars_p50"]), "2");
}

#[test]
fn suppressed_only_on_the_recall_and_sorted() {
    let mut show = recall("c1", 1, &["m1"]);
    show["suppressed_ids"] = json!([{"reason": "b"}, {"reason": "a"}, {"reason": "b"}]);
    let mut shown = brief("c1", 1, &["m1"], "");
    shown["suppressed_ids"] = json!([{"reason": "z"}]);
    let s = section(&[show, shown]);
    assert!(s.get("brief").unwrap().get("suppressed").is_none());
    let want = PyValue::Dict(vec![
        (PyValue::Str(cps("a")), PyValue::Int("1".into())),
        (PyValue::Str(cps("b")), PyValue::Int("2".into())),
    ]);
    assert_eq!(path(&s, &["recall", "suppressed"]), &want);
}

#[test]
fn auto_is_the_bool_true_only() {
    let use_row = |auto: Value| {
        json!({"kind": "use", "ids": ["m1"], "outcome": "applied", "auto": auto,
            "claim_excerpts": ["x"], "harness_session": "c1", "ts": ts(2)})
    };
    let s = section(&[brief("c1", 1, &["m1"], ""), use_row(json!(1))]);
    assert_eq!(int(&s, &["brief", "used"]), "1");
    let s = section(&[brief("c1", 1, &["m1"], ""), use_row(json!(true))]);
    assert_eq!(int(&s, &["brief", "used"]), "0");
}

#[test]
fn a_pick_never_decides_whether_there_is_a_section() {
    let events: Vec<PyValue> = [brief("c1", 1, &["m1"], "")].iter().map(value).collect();
    let mut none = |_: &PyValue| Ok::<bool, PyError>(false);
    let s = rc::compute_surface_report(&events, None, None, Some(&mut none))
        .unwrap()
        .expect("a section");
    assert_eq!(int(&s, &["brief", "deliveries"]), "0");
}

#[test]
fn a_pick_runs_on_every_show_before_the_tally() {
    let mut bad = recall("c1", 1, &["m1"]);
    bad["suppressed_ids"] = json!(3);
    let events: Vec<PyValue> = [bad, recall("c1", 2, &["m1"])].iter().map(value).collect();
    let mut calls = 0;
    let mut pick = |_: &PyValue| {
        calls += 1;
        if calls == 2 {
            Err(PyError::ValueError)
        } else {
            Ok(true)
        }
    };
    let r = rc::compute_surface_report(&events, None, None, Some(&mut pick));
    assert_eq!(r, Err(PyError::ValueError));
    let r = rc::compute_surface_report::<PyError>(&events, None, None, None);
    assert_eq!(r, Err(PyError::TypeError));
}

#[test]
fn a_list_kind_raises() {
    let event = value(&json!({"kind": ["use"]}));
    assert_eq!(rc::is_surface_row(&event), Err(PyError::TypeError));
    assert_eq!(
        report(&[json!({"kind": ["use"]})], None, None),
        Err(PyError::TypeError)
    );
    let tuple = PyValue::Dict(vec![(
        PyValue::Str(cps("kind")),
        PyValue::Tuple(vec![PyValue::Str(cps("use"))]),
    )]);
    assert_eq!(rc::is_surface_row(&tuple), Ok(false));
}

#[test]
fn chains_add_past_64_bits() {
    let row = |count: &str| {
        PyValue::Dict(vec![
            (
                PyValue::Str(cps("kind")),
                PyValue::Str(cps("review_unanswered")),
            ),
            (PyValue::Str(cps("count")), PyValue::Int(count.into())),
        ])
    };
    let events = [
        row("9223372036854775807"),
        row("9223372036854775807"),
        row("-1"),
        PyValue::Dict(vec![
            (
                PyValue::Str(cps("kind")),
                PyValue::Str(cps("review_unanswered")),
            ),
            (PyValue::Str(cps("count")), PyValue::Bool(true)),
        ]),
    ];
    let s = rc::compute_surface_report::<PyError>(&events, None, None, None)
        .unwrap()
        .unwrap();
    assert_eq!(
        int(&s, &["review_unanswered", "chains"]),
        "18446744073709551613"
    );
    assert_eq!(int(&s, &["review_unanswered", "turns"]), "4");
}

#[test]
fn the_latest_grade_is_the_last_in_the_log() {
    let grade = |minute: u32, stratum: &str, relevant: bool| {
        let key = if relevant {
            "relevant_ids"
        } else {
            "irrelevant_ids"
        };
        let mut g = json!({"kind": "recall_grade", "sample_id": "s1", "stratum": stratum,
            "ts": ts(minute)});
        g[key] = json!(["m1"]);
        g
    };
    // Logged later, stamped earlier: the log order decides.
    let s = section(&[grade(5, "delivery", true), grade(2, "brief", false)]);
    assert_eq!(int(&s, &["grades", "delivery", "graded"]), "0");
    assert_eq!(int(&s, &["grades", "brief", "graded"]), "1");
    assert_eq!(int(&s, &["grades", "brief", "relevant"]), "0");
}
