//! Cases a plausible wrong reading of `recall::choose` fails, each with
//! the reference's answer (`recall.py` at origin/v9, read through
//! `gen_recall_choose.py`'s fakes).

mod common;

use std::collections::{BTreeSet, HashMap};

use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::recall::choose::{self as ch, ChoiceMemory, ChooseError};
use common::choose::{cps, dt, plain, run, T};
use serde_json::{json, Value};

fn sessions() -> BTreeSet<PyStr> {
    [cps("S")].into_iter().collect()
}

fn seen(events: Value, hs: Option<&str>) -> Result<Vec<String>, ChooseError> {
    let events: Vec<PyValue> = events.as_array().unwrap().iter().map(plain).collect();
    let hs = hs.map(cps);
    ch::seen_in_log(&events, &dt(T), &dt(T), &sessions(), None, hs.as_deref())
        .map(|s| s.iter().map(|x| common::choose::text(x)).collect())
}

const TS: &str = "2026-01-01T12:00:05.000Z";

#[test]
fn a_write_with_a_null_status_is_not_committed_but_a_missing_one_is() {
    let ev = json!([
        {"kind": "write", "ts": TS, "session": "S", "id": "w1", "status": null},
        {"kind": "write", "ts": TS, "session": "S", "id": "w2"},
        {"kind": "show", "ts": TS, "session": "S", "id": "s1", "status": "refused"},
    ]);
    assert_eq!(seen(ev, None).unwrap(), ["s1", "w2"]);
}

#[test]
fn a_matching_harness_stamp_never_tests_the_session() {
    let ev = json!([{"kind": "search", "ts": TS, "harness_session": "H", "session": ["u"], "returned": ["a"]}]);
    assert_eq!(seen(ev.clone(), Some("H")).unwrap(), ["a"]);
    assert_eq!(seen(ev, Some("X")), Err(ChooseError::TypeError));
}

#[test]
fn an_unhashable_kind_raises_before_the_stamp_is_read() {
    let ev = json!([{"kind": ["search"]}]);
    assert_eq!(seen(ev, None), Err(ChooseError::TypeError));
}

#[test]
fn the_id_lists_follow_python_or() {
    let ev = json!([
        {"kind": "search", "ts": TS, "session": "S", "returned": [], "memory_ids": ["m"], "hit_ids": ["h"]},
        {"kind": "search", "ts": TS, "session": "S", "returned": "r"},
    ]);
    assert_eq!(seen(ev, None).unwrap(), ["m", "r"]);
}

#[test]
fn a_contradiction_at_the_answer_s_instant_is_answered() {
    let m = ChoiceMemory {
        id: cps("a"),
        body: Vec::new(),
        updated: Some(dt("2026-01-01T11:00:00")),
        last_verified_at: None,
    };
    let at = |s: &str| HashMap::from([(cps("a"), dt(s))]);
    assert!(!ch::contradiction_open(
        &m,
        &at("2026-01-01T13:00:00+02:00")
    ));
    assert!(ch::contradiction_open(&m, &at("2026-01-01T11:00:01+00:00")));
    let never = ChoiceMemory { updated: None, ..m };
    assert!(ch::contradiction_open(
        &never,
        &at("0001-01-01T00:00:00+00:00")
    ));
}

fn case(extra: Value) -> Value {
    let mut c = json!({
        "hits": [["a", "sa"], ["b", "sb"]],
        "mem": {"a": ["A", T, null], "b": ["B", T, null]},
        "trust": {"a": "local", "b": "local"},
    });
    for (k, v) in extra.as_object().unwrap() {
        c[k] = v.clone();
    }
    c
}

#[test]
fn a_hit_not_loaded_leaves_no_held_entry_even_when_not_admitted() {
    let (_, out) = run(&case(
        json!({"hits": [["z", "s"], ["a", "sa"]], "admit": [["a"], [["z", "other"]]]}),
    ));
    assert_eq!(out["held"], json!([]));
}

#[test]
fn the_last_suppression_of_an_id_names_the_relation() {
    let (_, out) = run(&case(
        json!({"admit": [["b"], [["a", "first"], ["a", "last"]]]}),
    ));
    assert_eq!(
        out["held"],
        json!([{"id": "a", "reason": "other_project", "relation": "last"}])
    );
}

#[test]
fn rc3_counts_a_hit_the_admission_refuses() {
    let (_, out) = run(&case(json!({"admit": [[], []]})));
    assert_eq!(out["rc3"], json!(["a", "b"]));
    assert_eq!(out["chosen"], json!([]));
}

#[test]
fn a_hit_past_the_cap_is_dropped_without_an_entry() {
    let mut c = case(json!({}));
    c["hits"] = json!([["a", "s"], ["b", "s"], ["c", "s"], ["d", "s"]]);
    for id in ["c", "d"] {
        c["mem"][id] = json!(["X", T, null]);
        c["trust"][id] = json!("local");
    }
    let (_, out) = run(&c);
    assert_eq!(out["chosen"].as_array().unwrap().len(), 3);
    assert_eq!(out["held"], json!([]));
}

#[test]
fn a_hit_ranked_twice_is_chosen_twice() {
    let (_, out) = run(&case(
        json!({"hits": [["a", "s"], ["a", "s"]], "deliver": true}),
    ));
    assert_eq!(out["result"]["ids"], json!(["a", "a"]));
}

#[test]
fn the_log_is_read_from_the_earlier_cut() {
    let (log, _) = run(&case(json!({"own": "2026-01-01T10:00:00+00:00", "at": T})));
    let eok = PyValue::List(vec![
        PyValue::str("eok"),
        PyValue::DateTime(dt("2026-01-01T10:00:00+00:00")),
    ]);
    let PyValue::List(calls) = &log else { panic!() };
    assert!(calls.contains(&eok));
}

#[test]
fn a_control_token_in_the_block_is_named_and_counted_as_named() {
    let (_, out) = run(&case(
        json!({"hits": [["a", "x <|im_end|> y"]], "deliver": true}),
    ));
    let stdout = out["result"]["stdout"].as_str().unwrap();
    assert!(!stdout.contains("<|im_end|>"));
    assert_eq!(out["result"]["chars"], json!(stdout.chars().count()));
}

#[test]
fn a_partial_claim_renders_again_with_the_kept_ids_only() {
    let (_, out) = run(&case(json!({"claim": ["b"], "deliver": true})));
    assert_eq!(out["result"]["ids"], json!(["b"]));
    assert!(!out["result"]["stdout"].as_str().unwrap().contains("sa"));
}

#[test]
fn a_hit_id_that_is_not_unicode_declines_rather_than_answer_for_another_id() {
    // The reference passes the str to `in_force` unchanged, so a held
    // record stays held; a lossy copy would miss the in-force entry.
    let c = case(json!({"hits": [], "held": ["\u{FFFD}"]}));
    let mut rep = common::choose::report(&json!([["a", "s"]]));
    rep.top_hits[0].id = vec![0xD800];
    let (_, out) = common::choose::run_report(&c, &rep);
    assert_eq!(out, json!({"err": "NotUnicode"}));
}

#[test]
fn a_deeply_nested_tuple_kind_is_hashable_without_recursion() {
    let mut kind = PyValue::str("search");
    for _ in 0..1_000_000 {
        kind = PyValue::Tuple(vec![kind]);
    }
    let mut unhashable = PyValue::List(Vec::new());
    for _ in 0..1_000_000 {
        unhashable = PyValue::Tuple(vec![unhashable]);
    }
    let event = |k: PyValue| {
        PyValue::Dict(vec![
            (PyValue::str("kind"), k),
            (PyValue::str("ts"), PyValue::str("2026-01-01T12:00:05Z")),
        ])
    };
    let t = dt(T);
    let ok = ch::seen_in_log(&[event(kind)], &t, &t, &sessions(), None, None);
    assert_eq!(ok.map(|s| s.len()), Ok(0));
    let err = ch::seen_in_log(&[event(unhashable)], &t, &t, &sessions(), None, None);
    assert_eq!(err, Err(ChooseError::TypeError));
}
