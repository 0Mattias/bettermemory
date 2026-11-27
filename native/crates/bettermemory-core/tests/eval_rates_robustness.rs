//! `eval_rates` over hostile input, with no panic in debug or release:
//!
//! - a log of 1,000,000 events walks, resolves and lists its cold rows
//!   within `TENFOLD` of 100,000 (`tests/timing`);
//! - a `use` row of 100,000 ids, all one id, counts once and keeps the
//!   last excerpt as its upgrade, within `TENFOLD` of 10,000;
//! - an excerpt of 1,000,000 code points, all whitespace past ASCII or
//!   with one letter inside, within `TENFOLD` of 100,000;
//! - 100,000 distinct ids and models: as many cold rows, sorted, and as
//!   many `by_model` buckets in first-seen order, within `TENFOLD` of
//!   10,000;
//! - the floor, the limit and the ratio at their extremes, an `int` of
//!   5,000 digits kept as its text, and values nested 100,000 deep where
//!   the walk reads a field.

mod timing;

use std::collections::{HashMap, HashSet};
use std::hint::black_box;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::eval_rates::{
    cold_endorsement_rows, resolve_audit_telemetry, walk_eval_events, EvalWalk,
};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::records::{Confidence, Memory, RecordFields, Source};
use bettermemory_core::unicode::cps;

fn s(text: &str) -> PyValue {
    PyValue::str(text)
}

fn d(pairs: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(pairs.into_iter().map(|(k, v)| (s(k), v)).collect())
}

fn list(items: Vec<PyValue>) -> PyValue {
    PyValue::List(items)
}

fn walk(events: &[PyValue]) -> EvalWalk<'_> {
    walk_eval_events(events, None, None, &mut |_| true).expect("walks")
}

fn memory(id: &str) -> Memory {
    let at = DateTime::from_isoformat(&cps("2026-10-01T00:00:00+00:00")).expect("a datetime");
    Memory {
        fields: RecordFields {
            id: cps(id),
            created: at,
            updated: at,
            scopes: vec![cps("global")],
            confidence: Confidence::MEMBERS[0].1,
            source: Source::MEMBERS[0].1,
            body: cps("A body. More."),
            origin: None,
            actor: None,
            last_verified_at: None,
            category: None,
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

/// `n` events cycling through every kind the walk counts, over 1,000 ids.
fn many_events(n: usize) -> Vec<PyValue> {
    (0..n)
        .map(|i| {
            let id = s(&format!("m{}", i % 1000));
            match i % 6 {
                0 => d(vec![("kind", s("search")), ("returned", list(vec![id]))]),
                1 => d(vec![("kind", s("show")), ("id", id)]),
                2 => d(vec![
                    ("kind", s("use")),
                    ("outcome", s("applied")),
                    ("auto", PyValue::Bool(i % 1000 < 500)),
                    ("ids", list(vec![id])),
                ]),
                3 => d(vec![
                    ("kind", s("turn_audited")),
                    ("triggered_from", s("stop_hook")),
                ]),
                4 => d(vec![
                    ("kind", s("search_miss")),
                    ("session_id", s("s")),
                    ("ts", s("2026-10-05T00:00:00Z")),
                ]),
                _ => d(vec![("kind", s("miss_ack")), ("event_id", s("e"))]),
            }
        })
        .collect()
}

fn whole(events: &[PyValue], by_id: &HashMap<PyStr, Memory>) -> (i64, usize) {
    let w = walk(events);
    let tally = resolve_audit_telemetry(&w, &HashSet::new(), 20).expect("resolves");
    let cold = cold_endorsement_rows(&w, by_id, 1, 0.0);
    (
        w.retrieval_occurrences + tally.silent_misses,
        cold.rows.len(),
    )
}

#[test]
fn a_million_events_in_linear_time() {
    let by_id: HashMap<PyStr, Memory> = (0..1000)
        .map(|i| {
            let id = format!("m{i}");
            (cps(&id), memory(&id))
        })
        .collect();
    let small = many_events(100_000);
    let large = many_events(1_000_000);
    let (n, rows) = whole(&large, &by_id);
    assert_eq!(n, 2 * 166_667 + 166_666);
    assert!(rows > 0);
    let w = walk(&large);
    assert_eq!(w.total_events_scanned, 1_000_000);
    assert_eq!(w.hook_telemetry_events, 166_667);
    timing::grows_within(
        "walk, resolve and cold rows over a log",
        timing::TENFOLD,
        &mut || timing::once(|| black_box(whole(&small, &by_id))),
        &mut || timing::once(|| black_box(whole(&large, &by_id))),
    );
}

fn duplicate_use(n: usize) -> Vec<PyValue> {
    let mut excerpts = vec![s(""); n];
    excerpts[n - 1] = s("x");
    vec![d(vec![
        ("kind", s("use")),
        ("outcome", s("applied")),
        ("ids", list(vec![s("a"); n])),
        ("claim_excerpts", list(excerpts)),
    ])]
}

#[test]
fn one_use_of_a_hundred_thousand_duplicate_ids() {
    let small = duplicate_use(10_000);
    let large = duplicate_use(100_000);
    let mut calls = 0;
    let w = walk_eval_events(&large, None, None, &mut |_| {
        calls += 1;
        true
    })
    .expect("walks");
    assert_eq!(calls, 1);
    assert_eq!(
        (w.applied_total, w.explicit_endorsements_with_excerpt),
        (1, 1)
    );
    timing::grows_within(
        "a use row of duplicate ids",
        timing::TENFOLD,
        &mut || timing::once(|| black_box(walk(&small).applied_total)),
        &mut || timing::once(|| black_box(walk(&large).applied_total)),
    );
}

fn long_excerpt(n: usize, letter: bool) -> Vec<PyValue> {
    let mut text: PyStr = vec![0x3000; n];
    if letter {
        text[n / 2] = u32::from(b'x');
    }
    vec![d(vec![
        ("kind", s("use")),
        ("outcome", s("applied")),
        ("ids", list(vec![s("a"), s("a")])),
        (
            "claim_excerpts",
            list(vec![PyValue::Str(text.clone()), PyValue::Str(text)]),
        ),
    ])]
}

#[test]
fn an_excerpt_of_a_million_code_points() {
    assert_eq!(
        walk(&long_excerpt(1_000_000, false)).explicit_endorsements_with_excerpt,
        0
    );
    assert_eq!(
        walk(&long_excerpt(1_000_000, true)).explicit_endorsements_with_excerpt,
        1
    );
    let small = long_excerpt(100_000, false);
    let large = long_excerpt(1_000_000, false);
    timing::grows_within(
        "an excerpt of whitespace",
        timing::TENFOLD,
        &mut || timing::once(|| black_box(walk(&small).applied_total)),
        &mut || timing::once(|| black_box(walk(&large).applied_total)),
    );
}

fn distinct(n: usize) -> (Vec<PyValue>, HashMap<PyStr, Memory>) {
    let ids: Vec<String> = (0..n).map(|i| format!("m{:06}", (i * 7919) % n)).collect();
    let mut events = vec![d(vec![
        ("kind", s("turn_audited")),
        ("triggered_from", s("stop_hook")),
    ])];
    events.push(d(vec![
        ("kind", s("search")),
        ("returned", list(ids.iter().map(|i| s(i)).collect())),
    ]));
    events.push(d(vec![
        ("kind", s("use")),
        ("outcome", s("applied")),
        ("auto", PyValue::Bool(true)),
        ("ids", list(ids.iter().map(|i| s(i)).collect())),
    ]));
    for id in &ids {
        events.push(d(vec![
            ("kind", s("turn_audited")),
            ("client_model", s(id)),
        ]));
    }
    let by_id = ids.iter().map(|i| (cps(i), memory(i))).collect();
    (events, by_id)
}

fn rows_and_models(events: &[PyValue], by_id: &HashMap<PyStr, Memory>) -> (usize, usize) {
    let w = walk(events);
    let tally = resolve_audit_telemetry(&w, &HashSet::new(), 20).expect("resolves");
    (
        cold_endorsement_rows(&w, by_id, 1, 0.0).rows.len(),
        tally.by_model.len(),
    )
}

#[test]
fn a_hundred_thousand_ids_and_models() {
    let (events, by_id) = distinct(100_000);
    let w = walk(&events);
    let cold = cold_endorsement_rows(&w, &by_id, 1, 0.0);
    assert_eq!(cold.total, 100_000);
    assert!(cold.rows.windows(2).all(|p| p[0].id < p[1].id));
    let tally = resolve_audit_telemetry(&w, &HashSet::new(), 20).expect("resolves");
    assert_eq!(tally.by_model.len(), 100_000);
    assert_eq!(tally.by_model[0].0, cps("m000000"));
    assert_eq!(tally.by_model[1].0, cps("m007919"));
    let small = distinct(10_000);
    timing::grows_within(
        "cold rows and models",
        timing::TENFOLD,
        &mut || timing::once(|| black_box(rows_and_models(&small.0, &small.1))),
        &mut || timing::once(|| black_box(rows_and_models(&events, &by_id))),
    );
}

#[test]
fn bounds_at_their_extremes() {
    let by_id: HashMap<PyStr, Memory> = [(cps("a"), memory("a"))].into_iter().collect();
    let events = vec![
        d(vec![
            ("kind", s("turn_audited")),
            ("triggered_from", s("stop_hook")),
        ]),
        d(vec![
            ("kind", s("search")),
            ("returned", list(vec![s("a")])),
        ]),
        d(vec![
            ("kind", s("use")),
            ("outcome", s("applied")),
            ("ids", list(vec![s("a"), s("a")])),
            ("auto", PyValue::Bool(true)),
        ]),
        d(vec![
            ("kind", s("use")),
            ("outcome", s("applied")),
            ("ids", list(vec![s("a")])),
        ]),
        d(vec![
            ("kind", s("search_miss")),
            ("session_id", s("s")),
            ("ts", s("2026-10-05T00:00:00Z")),
            ("recent_retrieval_count", PyValue::Int("9".repeat(5000))),
        ]),
    ];
    let w = walk(&events);
    for floor in [i64::MIN, -1, 0, 1] {
        assert_eq!(cold_endorsement_rows(&w, &by_id, floor, 0.0).total, 0);
        // Explicit share 1/2: below a threshold of inf, never below NaN.
        assert_eq!(
            cold_endorsement_rows(&w, &by_id, floor, f64::INFINITY).total,
            1
        );
        assert_eq!(cold_endorsement_rows(&w, &by_id, floor, f64::NAN).total, 0);
        assert_eq!(cold_endorsement_rows(&w, &by_id, floor, 0.5).total, 0);
        assert_eq!(
            cold_endorsement_rows(&w, &by_id, floor, 0.500_000_1).total,
            1
        );
        assert_eq!(cold_endorsement_rows(&w, &by_id, floor, -0.0).total, 0);
    }
    for floor in [2, i64::MAX] {
        assert_eq!(
            cold_endorsement_rows(&w, &by_id, floor, f64::INFINITY).total,
            0
        );
    }
    for (limit, kept) in [(i64::MIN, 0), (0, 0), (1, 1), (i64::MAX, 1)] {
        let tally = resolve_audit_telemetry(&w, &HashSet::new(), limit).expect("resolves");
        assert_eq!(tally.silent_miss_buffer.len(), kept);
        if kept == 1 {
            assert_eq!(
                tally.silent_miss_buffer[0].recent_retrieval_count,
                "9".repeat(5000)
            );
        }
    }
}

fn nested(depth: usize, leaf: PyValue) -> PyValue {
    let mut v = leaf;
    for _ in 0..depth {
        v = list(vec![v]);
    }
    v
}

#[test]
fn values_nested_deep() {
    let deep = || nested(100_000, s("a"));
    let events = vec![
        d(vec![("kind", s("search")), ("returned", deep())]),
        d(vec![
            ("kind", s("use")),
            ("outcome", s("applied")),
            ("ids", deep()),
        ]),
        d(vec![
            ("kind", s("use")),
            ("outcome", s("applied")),
            ("ids", list(vec![s("a")])),
            ("claim_excerpts", deep()),
        ]),
        d(vec![("kind", s("turn_audited")), ("repeat", deep())]),
        d(vec![
            ("kind", s("search_miss")),
            ("top_hits", deep()),
            ("ts", deep()),
        ]),
        d(vec![("kind", s("show")), ("id", deep()), ("other", deep())]),
    ];
    let w = walk(&events);
    assert_eq!((w.retrieval_occurrences, w.applied_total), (0, 1));
    let tombstoned: HashSet<PyStr> = [cps("a")].into_iter().collect();
    let tally = resolve_audit_telemetry(&w, &tombstoned, 20).expect("resolves");
    assert_eq!((tally.repeat_audits, tally.silent_misses), (1, 1));
    assert!(tally.silent_miss_buffer.is_empty());
    // An unhashable kind, however deep, raises TypeError at once.
    let bad = vec![d(vec![("kind", deep())])];
    let err = walk_eval_events(&bad, None, None, &mut |_| true).expect_err("raises");
    assert_eq!(err.name(), "TypeError");
}
