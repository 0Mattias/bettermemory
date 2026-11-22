//! Cases a plausible wrong port of `recall_prompt` fails, each stated
//! against what the reference does (`tests/contract/gen_recall_prompt.py`
//! pins the same behaviour in `recall_prompt.jsonl`).

mod common;

use bettermemory_core::pyvalue::PyValue;
use common::recall_prompt::{dt, run};
use serde_json::json;

fn kinds(log: &[PyValue]) -> Vec<String> {
    log.iter()
        .map(|e| match e {
            PyValue::List(items) => match &items[0] {
                PyValue::Str(s) => s.iter().map(|&c| char::from_u32(c).unwrap()).collect(),
                _ => String::new(),
            },
            _ => String::new(),
        })
        .collect()
}

fn field<'a>(fields: &'a PyValue, name: &str) -> Option<(usize, &'a PyValue)> {
    match fields {
        PyValue::Dict(pairs) => pairs
            .iter()
            .position(|(k, _)| *k == PyValue::str(name))
            .map(|i| (i, &pairs[i].1)),
        _ => None,
    }
}

fn first(answer: &PyValue) -> PyValue {
    match answer {
        PyValue::List(items) => items[0].clone(),
        other => other.clone(),
    }
}

#[test]
fn window_reads_the_segment_start_only_past_the_first_segment() {
    let at = "2026-01-01T09:00:00+00:00";
    let (a, _) = run("win", &json!({"seg": {"n": 1, "at": at}}));
    assert_eq!(first(&a), PyValue::DateTime(dt(at)));
    let (a, _) = run("win", &json!({"seg": {"n": true, "at": at}}));
    assert_eq!(first(&a), PyValue::DateTime(dt(at)));
    for n in [json!(0), json!(-1), json!(false)] {
        let (a, _) = run("win", &json!({"seg": {"n": n, "at": at}}));
        assert_eq!(
            first(&a),
            PyValue::DateTime(dt("2025-12-31T12:00:00+00:00"))
        );
    }
}

#[test]
fn window_compares_before_reading_the_log() {
    let (a, log) = run("win", &json!({"seg": {"n": null}}));
    assert_eq!(
        a,
        PyValue::Dict(vec![(PyValue::str("err"), PyValue::str("TypeError"))])
    );
    assert!(log.is_empty());
    // The dedup cut overflows first; past it, the idle cut only when the
    // segment is the first.
    let (a, _) = run(
        "win",
        &json!({"now": "0001-01-01T00:30:00+00:00", "seg": {"n": null}}),
    );
    assert_eq!(
        a,
        PyValue::Dict(vec![(PyValue::str("err"), PyValue::str("OverflowError"))])
    );
    let (a, _) = run(
        "win",
        &json!({"now": "0001-01-01T02:00:00+00:00", "seg": {"n": 1}}),
    );
    assert!(matches!(a, PyValue::List(_)));
}

#[test]
fn settle_failure_of_the_audit_stamp_is_a_warning_and_skips_the_settlement() {
    let (_, log) = run("settle", &json!({"now": "0001-01-01T02:00:00+00:00"}));
    assert_eq!(kinds(&log), ["on", "warn"]);
    let (_, log) = run("settle", &json!({}));
    assert_eq!(kinds(&log), ["on", "eok", "settle"]);
}

#[test]
fn probe_admission_follows_the_surface() {
    let (_, log) = run("probe", &json!({"rep": {}}));
    assert_eq!(kinds(&log), ["on", "probe"]);
    let (_, log) = run("probe", &json!({"surface": "search_unscoped", "rep": {}}));
    assert_eq!(kinds(&log), ["probe"]);
}

#[test]
fn hold_asks_the_recorder_before_building() {
    let (_, log) = run("hold", &json!({"tel": false}));
    assert_eq!(kinds(&log), ["recorder"]);
    let (_, log) = run("hold", &json!({"sup": true}));
    assert_eq!(kinds(&log), ["recorder"]);
    let (a, log) = run("hold", &json!({"tel": false, "cause": "bogus"}));
    assert_eq!(a, PyValue::None);
    assert_eq!(kinds(&log), ["recorder"]);
}

#[test]
fn hold_probe_mode_defaults_to_hybrid() {
    for sm in [json!(""), json!(null)] {
        let (_, log) = run("hold", &json!({"sm": sm}));
        let PyValue::List(rec) = &log[2] else {
            panic!()
        };
        assert_eq!(
            field(&rec[2], "probe_mode").unwrap().1,
            &PyValue::str("hybrid")
        );
    }
}

#[test]
fn delivery_overwrites_the_event_id_in_place_and_counts_code_points() {
    let (a, _) = run("deliv", &json!({"block": "a\u{1F600}", "eid": "D9"}));
    assert_eq!(field(&a, "event_id"), Some((0, &PyValue::str("D9"))));
    assert_eq!(field(&a, "injected_chars").unwrap().1, &PyValue::int(2));
    assert_eq!(field(&a, "chars").unwrap().1, &PyValue::int(2));
}

#[test]
fn delivery_drops_only_chosen_pointers_not_shown() {
    let (a, _) = run(
        "deliv",
        &json!({"chosen": ["a", "b"], "shown": ["a"], "rc3": ["a", "b", "c", "c"]}),
    );
    let ids = field(&a, "rc3_admitted").unwrap().1.clone();
    assert_eq!(
        ids,
        PyValue::List(vec![
            PyValue::str("a"),
            PyValue::str("c"),
            PyValue::str("c")
        ])
    );
}

#[test]
fn delivery_forgets_the_claim_when_the_row_fails() {
    let (a, log) = run("deliv", &json!({"rraise": "locked"}));
    assert_eq!(
        a,
        PyValue::Dict(vec![(PyValue::str("err"), PyValue::str("OSError"))])
    );
    assert_eq!(kinds(&log), ["recorder", "dm", "rc3", "rec", "forget"]);
    let (_, log) = run("hold", &json!({"rraise": "locked"}));
    assert!(!kinds(&log).contains(&"forget".to_string()));
}

#[test]
fn delivery_project_only_follows_the_surface() {
    let (_, log) = run("deliv", &json!({"surface": "search_unscoped"}));
    let PyValue::List(rc3) = &log[2] else {
        panic!()
    };
    assert_eq!(rc3[3], PyValue::Bool(false));
}
