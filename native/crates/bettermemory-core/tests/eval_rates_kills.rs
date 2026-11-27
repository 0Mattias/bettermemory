//! Cases a plausible wrong port of `eval_rates` fails, each with the
//! reference's answer (CPython 3.13.13): a marker handled after the window
//! skip, `>=` for the latest cutoff, an unreadable stamp kept under a
//! cutoff, `is not None` for an `or`, a duplicate id counted twice, the
//! first excerpt kept over an upgrade, a settlement counted in the trio,
//! `auto` read by truthiness, the ids tested before the settlement,
//! `by_model` sorted, the buffer kept at a limit of 0, the cold rows
//! sorted by id descending, an ambient memory kept, and the window read by
//! wall clock instead of instant.
//!
//! One listed mutant cannot be killed: asking `_canonical_top_hit_id`
//! when the tombstone set is empty. It answers for every dict without
//! raising, and membership in an empty set is false, so the answer is the
//! reference's either way; only the cost differs.

use std::collections::{HashMap, HashSet};

use bettermemory_core::datetime::DateTime;
use bettermemory_core::eval_rates::{
    cold_endorsement_rows, resolve_audit_telemetry, walk_eval_events, EvalWalk, ModelCounts,
};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::records::{Category, Confidence, Memory, RecordFields, Source};
use bettermemory_core::unicode::{cps, to_string};
use serde_json::{json, Value};

fn plain(v: &Value) -> PyValue {
    match v {
        Value::Null => PyValue::None,
        Value::Bool(b) => PyValue::Bool(*b),
        Value::Number(n) => match n.as_i64() {
            Some(i) => PyValue::int(i128::from(i)),
            None => PyValue::Float(n.as_f64().expect("a float")),
        },
        Value::String(s) => PyValue::str(s),
        Value::Array(items) => PyValue::List(items.iter().map(plain).collect()),
        Value::Object(m) => {
            PyValue::Dict(m.iter().map(|(k, v)| (PyValue::str(k), plain(v))).collect())
        }
    }
}

fn log(events: Value) -> Vec<PyValue> {
    events
        .as_array()
        .expect("a log")
        .iter()
        .map(plain)
        .collect()
}

fn dt(text: &str) -> DateTime {
    DateTime::from_isoformat(&cps(text)).expect("an isoformat")
}

fn t(day: u32) -> String {
    format!("2026-10-{day:02}T00:00:00Z")
}

fn walk<'a>(events: &'a [PyValue], cutoff: Option<&str>, end: Option<&str>) -> EvalWalk<'a> {
    let cutoff = cutoff.map(dt);
    let end = end.map(dt);
    walk_eval_events(events, cutoff.as_ref(), end.as_ref(), &mut |_| true).expect("walks")
}

fn counts(c: &bettermemory_core::eval_rates::Counts) -> Vec<(String, i64)> {
    c.items()
        .iter()
        .map(|(k, n)| (to_string(k).expect("UTF-8"), *n))
        .collect()
}

fn pairs(items: &[(&str, i64)]) -> Vec<(String, i64)> {
    items.iter().map(|(k, n)| ((*k).to_owned(), *n)).collect()
}

fn none() -> HashSet<PyStr> {
    HashSet::new()
}

#[test]
fn markers_outside_the_window_still_apply() {
    // Both markers lie outside the window: a port that tests the window
    // first never sees them and counts all three misses.
    let events = log(json!([
        {"kind": "silent_miss_cutoff", "cutoff_ts": t(6), "ts": t(1)},
        {"kind": "miss_ack", "event_id": "e2", "ts": t(9)},
        {"kind": "search_miss", "ts": t(5), "session_id": "s", "event_id": "e1"},
        {"kind": "search_miss", "ts": t(7), "session_id": "s", "event_id": "e2"},
        {"kind": "search_miss", "ts": t(7), "session_id": "s", "event_id": "e3"},
    ]));
    let w = walk(
        &events,
        Some("2026-10-03T00:00:00+00:00"),
        Some("2026-10-08T00:00:00+00:00"),
    );
    assert_eq!(w.latest_miss_cutoff, Some(dt("2026-10-06T00:00:00+00:00")));
    assert!(w.acknowledged_miss_event_ids.contains(&cps("e2")));
    let tally = resolve_audit_telemetry(&w, &none(), 20).expect("resolves");
    assert_eq!(tally.silent_misses, 1);
    assert_eq!(tally.silent_miss_buffer.len(), 1);
}

#[test]
fn the_latest_cutoff_must_be_strictly_later() {
    // The same instant at another offset is not later: the first stays,
    // and its spelling shows which.
    let events = log(json!([
        {"kind": "silent_miss_cutoff", "cutoff_ts": "2026-10-05T00:00:00Z"},
        {"kind": "silent_miss_cutoff", "cutoff_ts": "2026-10-05T02:00:00+02:00"},
        {"kind": "silent_miss_cutoff", "cutoff_ts": "2026-10-04T23:00:00Z"},
    ]));
    let w = walk(&events, None, None);
    assert_eq!(
        w.latest_miss_cutoff.expect("a cutoff").isoformat(),
        "2026-10-05T00:00:00+00:00"
    );
}

#[test]
fn an_unreadable_stamp_drops_under_a_cutoff() {
    let events = log(json!([
        {"kind": "silent_miss_cutoff", "cutoff_ts": t(1)},
        {"kind": "turn_audited", "ts": "bad"},
        {"kind": "turn_audited", "ts": t(2)},
        {"kind": "turn_audited"},
        {"kind": "turn_audited", "ts": t(1)},
    ]));
    let w = walk(&events, None, None);
    let tally = resolve_audit_telemetry(&w, &none(), 20).expect("resolves");
    assert_eq!(tally.turns_audited, 2);
}

#[test]
fn the_or_chains_read_truthiness() {
    // An empty `returned` or `ids` falls through to the legacy field; an
    // `is not None` chain would stop at it and count nothing.
    let events = log(json!([
        {"kind": "search", "returned": [], "memory_ids": ["a"]},
        {"kind": "search", "returned": null, "memory_ids": [], "hit_ids": ["b"]},
        {"kind": "search", "returned": 0, "hit_ids": ["d"]},
        {"kind": "use", "outcome": "applied", "ids": [], "memory_ids": ["c"]},
    ]));
    let w = walk(&events, None, None);
    assert_eq!(
        counts(&w.search_delivery_count),
        pairs(&[("a", 1), ("b", 1), ("d", 1)])
    );
    assert_eq!(w.applied_total, 1);
    assert_eq!(counts(&w.explicit_applied_count), pairs(&[("c", 1)]));
}

#[test]
fn duplicates_count_once_and_keep_an_upgraded_excerpt() {
    let events = log(json!([
        {"kind": "use", "outcome": "applied", "ids": ["a", "a", "b", "b"],
         "claim_excerpts": ["", "x", "y", " "]},
    ]));
    let w = walk(&events, None, None);
    assert_eq!(w.applied_total, 2);
    // `a` upgrades from "" to "x"; `b` keeps "y" over " ".
    assert_eq!(w.explicit_endorsements_with_excerpt, 2);
    assert_eq!(
        counts(&w.explicit_applied_count),
        pairs(&[("a", 1), ("b", 1)])
    );
}

#[test]
fn a_settlement_counts_per_memory_and_not_in_the_trio() {
    let events = log(json!([
        {"kind": "use", "outcome": "applied", "ids": ["a"], "surface": "brief",
         "claim_excerpts": ["x"]},
        {"kind": "use", "outcome": "applied", "ids": ["a"], "surface": "recall", "auto": true},
    ]));
    let w = walk(&events, None, None);
    assert_eq!(
        (
            w.applied_total,
            w.applied_explicit,
            w.explicit_endorsements_with_excerpt
        ),
        (0, 0, 0)
    );
    assert_eq!(counts(&w.explicit_applied_count), pairs(&[("a", 1)]));
    assert_eq!(counts(&w.auto_applied_count), pairs(&[("a", 1)]));
}

#[test]
fn auto_is_the_bool_true_only() {
    let events = log(json!([
        {"kind": "use", "outcome": "applied", "ids": ["a"], "auto": 1},
        {"kind": "use", "outcome": "applied", "ids": ["b"], "auto": true},
        {"kind": "use", "outcome": "applied", "ids": ["c"], "auto": "true"},
    ]));
    let w = walk(&events, None, None);
    assert_eq!(
        (w.applied_total, w.applied_explicit, w.applied_model),
        (3, 2, 2)
    );
    assert_eq!(counts(&w.auto_applied_count), pairs(&[("b", 1)]));
    assert_eq!(
        counts(&w.explicit_applied_count),
        pairs(&[("a", 1), ("c", 1)])
    );
}

#[test]
fn the_settlement_test_raises_before_the_ids_are_read() {
    let events = log(json!([
        {"kind": "use", "outcome": "applied", "ids": "not a list", "surface": ["brief"]},
    ]));
    let err = walk_eval_events(&events, None, None, &mut |_| true).expect_err("raises");
    assert_eq!(err.name(), "TypeError");
    // An unhashable kind raises at the surface rows, whatever follows.
    let events = log(json!([{"kind": "other"}, {"kind": {"a": 1}}]));
    let err = walk_eval_events(&events, None, None, &mut |_| true).expect_err("raises");
    assert_eq!(err.name(), "TypeError");
}

#[test]
fn by_model_keeps_first_seen_order() {
    let events = log(json!([
        {"kind": "turn_audited", "client_model": "zeta"},
        {"kind": "turn_audited", "client_model": "alpha", "verdict": "no_signal"},
        {"kind": "search_miss", "client_model": "zeta", "session_id": "s", "ts": t(1)},
    ]));
    let w = walk(&events, None, None);
    let tally = resolve_audit_telemetry(&w, &none(), 20).expect("resolves");
    let models: Vec<(String, ModelCounts)> = tally
        .by_model
        .iter()
        .map(|(m, c)| (to_string(m).expect("UTF-8"), *c))
        .collect();
    assert_eq!(
        models,
        vec![
            (
                "zeta".to_owned(),
                ModelCounts {
                    audited: 1,
                    no_signal: 0,
                    misses: 1
                }
            ),
            (
                "alpha".to_owned(),
                ModelCounts {
                    audited: 0,
                    no_signal: 1,
                    misses: 0
                }
            ),
        ]
    );
}

#[test]
fn the_buffer_keeps_the_last_limit_and_nothing_at_zero() {
    let events = log(json!([
        {"kind": "search_miss", "session_id": "s0", "ts": t(1)},
        {"kind": "search_miss", "session_id": "s1", "ts": t(1)},
        {"kind": "search_miss", "session_id": "s2", "ts": t(1)},
    ]));
    let w = walk(&events, None, None);
    for (limit, kept) in [
        (i64::MIN, vec![]),
        (-1, vec![]),
        (0, vec![]),
        (1, vec!["s2"]),
        (2, vec!["s1", "s2"]),
        (3, vec!["s0", "s1", "s2"]),
        (i64::MAX, vec!["s0", "s1", "s2"]),
    ] {
        let tally = resolve_audit_telemetry(&w, &none(), limit).expect("resolves");
        assert_eq!(tally.silent_misses, 3);
        let sessions: Vec<String> = tally
            .silent_miss_buffer
            .iter()
            .map(|c| to_string(&c.session_id).expect("UTF-8"))
            .collect();
        assert_eq!(sessions, kept, "limit {limit}");
    }
}

fn memory(id: &str, category: Option<Category>) -> Memory {
    let at = dt("2026-10-01T00:00:00+00:00");
    Memory {
        fields: RecordFields {
            id: cps(id),
            created: at,
            updated: at,
            scopes: vec![cps("g")],
            confidence: Confidence::MEMBERS[0].1,
            source: Source::MEMBERS[0].1,
            body: cps(&format!("{id}. More.")),
            origin: None,
            actor: None,
            last_verified_at: None,
            category,
            verified_paths: Vec::new(),
            verified_commits: Vec::new(),
            verified_versions: Vec::new(),
            verified_absent_paths: Vec::new(),
            verified_path_targets: None,
            verified_absent_path_targets: None,
            claims: Vec::new(),
            verified_head: None,
        },
        links: Vec::new(),
        corroborations: "0".to_owned(),
        last_corroborated: None,
    }
}

#[test]
fn cold_rows_sort_by_id_ascending_and_leave_ambient_out() {
    let by_id: HashMap<PyStr, Memory> = [
        ("a", Some(Category::Fact)),
        ("b", Some(Category::Ambient)),
        ("c", None),
        ("z", Some(Category::UserInference)),
    ]
    .into_iter()
    .map(|(id, c)| (cps(id), memory(id, c)))
    .collect();
    let events = log(json!([
        {"kind": "turn_audited", "triggered_from": "stop_hook"},
        {"kind": "search", "returned": ["z", "c", "a", "b"]},
        {"kind": "use", "outcome": "applied", "auto": true, "ids": ["z", "c", "a", "b"]},
    ]));
    let w = walk(&events, None, None);
    let cold = cold_endorsement_rows(&w, &by_id, 1, 0.0);
    let ids: Vec<String> = cold
        .rows
        .iter()
        .map(|r| to_string(&r.id).expect("UTF-8"))
        .collect();
    assert_eq!(ids, ["a", "c", "z"]);
    assert_eq!(cold.total, 3);
    assert!(!cold.suppressed);
    assert_eq!(to_string(&cold.rows[0].summary).unwrap(), "a");
}

#[test]
fn the_window_compares_instants() {
    // 01:00 at +02:00 on the 3rd is 23:00 UTC on the 2nd: before the
    // cutoff, though its wall clock is after it. Rows of review are kept
    // whatever the window.
    let events = log(json!([
        {"kind": "review_issued", "ts": t(1)},
        {"kind": "search", "returned": ["a"], "ts": "2026-10-03T01:00:00+02:00"},
        {"kind": "search", "returned": ["b"], "ts": t(3)},
        {"kind": "search", "returned": ["c"], "ts": t(7)},
    ]));
    let w = walk(
        &events,
        Some("2026-10-03T00:00:00+00:00"),
        Some("2026-10-07T00:00:00+00:00"),
    );
    assert_eq!(w.review_rows.len(), 1);
    assert_eq!(counts(&w.search_delivery_count), pairs(&[("b", 1)]));
    assert_eq!((w.events_in_window, w.total_events_scanned), (1, 4));
}
