//! Hostile input for `recall_counters`: 100,000 rows over 1,000
//! conversations and 100 worktrees within the reference's growth family,
//! ints past 64 bits in `chars`, `segment` and `count`, unhashable and
//! deeply nested values at every field read, and extreme stamps. Nothing
//! panics.
//!
//! The family: the tally reads, per show, the rows its conversation
//! joined (`later`) and, per shown id, those rows again, so the work is
//! the sum over conversations of shows times joined rows times ids. At a
//! fixed size per conversation it is linear in the number of
//! conversations, which is the claim held here (ten times the
//! conversations and rows within `TENFOLD`).

mod timing;

use bettermemory_core::datetime::{parse_event_ts, DateTime};
use bettermemory_core::pyvalue::{PyError, PyStr, PyValue};
use bettermemory_core::recall_counters as rc;

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn st(s: &str) -> PyValue {
    PyValue::Str(cps(s))
}

fn ev(fields: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(fields.into_iter().map(|(k, v)| (st(k), v)).collect())
}

fn ids(v: &[String]) -> PyValue {
    PyValue::List(v.iter().map(|s| st(s)).collect())
}

fn stamp(i: usize) -> PyValue {
    let s = i % 60;
    let m = (i / 60) % 60;
    let h = (i / 3600) % 24;
    st(&format!("2026-01-01T{h:02}:{m:02}:{s:02}+00:00"))
}

fn int(n: usize) -> PyValue {
    PyValue::Int(n.to_string())
}

/// `conversations` conversations of 100 rows each over `conversations /
/// 10` worktrees: per conversation a brief of five ids, then rounds of a
/// recall show, opens (stamped, bridged and by the row's own session),
/// uses, a settlement, a segment, a review count and a grade, the
/// conversations interleaved in the log.
fn big_log(conversations: usize) -> Vec<PyValue> {
    let worktrees = (conversations / 10).max(1);
    let mut events = Vec::with_capacity(conversations * 100);
    let memory = |c: usize, j: usize| format!("m{}", (c + j) % 7);
    for step in 0..100 {
        for c in 0..conversations {
            let i = step * conversations + c;
            let session = format!("c{c}");
            let worktree = format!("/wt/{}", c % worktrees);
            let shown: Vec<String> = (0..5).map(|j| memory(c, j + step)).collect();
            let mut row = match step % 10 {
                0 => ev(vec![
                    ("kind", st("session_brief")),
                    ("session", st(&session)),
                    ("ids", ids(&shown)),
                    ("triggered_from", st("session_hook")),
                    ("worktree_root", st(&worktree)),
                    ("segment", int(step / 10 + 1)),
                    ("chars", int(i)),
                ]),
                1 | 5 => ev(vec![
                    ("kind", st("prompt_recall")),
                    ("session", st(&session)),
                    ("shown_ids", ids(&shown[..2])),
                    ("rule", st(if step % 20 < 10 { "r1" } else { "r2" })),
                    ("triggered_from", st("prompt_hook")),
                    ("worktree_root", st(&worktree)),
                    (
                        "suppressed_ids",
                        PyValue::List(vec![ev(vec![("reason", st("cap"))])]),
                    ),
                    ("chars", int(i % 977)),
                ]),
                2 | 6 => ev(vec![
                    ("kind", st("show")),
                    ("session", st("srv")),
                    ("id", st(&shown[step % 5])),
                    ("harness_session", st(&session)),
                ]),
                3 => ev(vec![
                    ("kind", st("show")),
                    ("session", st("srv")),
                    ("id", st(&shown[1])),
                    ("worktree_root", st(&worktree)),
                ]),
                4 | 7 => ev(vec![
                    ("kind", st("use")),
                    ("session", st(&session)),
                    ("ids", ids(&shown[..3])),
                    (
                        "outcome",
                        st(if step.is_multiple_of(4) {
                            "ignored"
                        } else {
                            "applied"
                        }),
                    ),
                    ("claim_excerpts", ids(&["a".into(), " ".into(), "c".into()])),
                ]),
                8 => ev(vec![
                    (
                        "kind",
                        st(if c.is_multiple_of(2) {
                            "use"
                        } else {
                            "segment_opened"
                        }),
                    ),
                    ("session", st(&session)),
                    ("surface", st("recall")),
                    ("ids", ids(&shown)),
                    ("segment", int(step)),
                    ("agent_id", st("a1")),
                ]),
                _ => ev(vec![
                    (
                        "kind",
                        st(if c.is_multiple_of(2) {
                            "review_unanswered"
                        } else {
                            "recall_grade"
                        }),
                    ),
                    ("count", int(c)),
                    ("sample_id", st(&format!("s{}", c % 13))),
                    ("stratum", st("delivery")),
                    ("relevant_ids", ids(&shown[..1])),
                ]),
            };
            if let PyValue::Dict(fields) = &mut row {
                fields.push((st("ts"), stamp(i)));
            }
            events.push(row);
        }
    }
    events
}

fn section(events: &[PyValue], start: Option<DateTime>, end: Option<DateTime>) -> PyValue {
    rc::compute_surface_report::<PyError>(events, start, end, None)
        .expect("no error")
        .expect("a section")
}

fn count(v: &PyValue, keys: &[&str]) -> String {
    match keys.iter().fold(v, |v, k| v.get(k).expect("a key")) {
        PyValue::Int(n) => n.clone(),
        other => panic!("{keys:?} is {other:?}"),
    }
}

#[test]
fn a_hundred_thousand_rows_grow_linearly_in_conversations() {
    let small = big_log(100);
    let large = big_log(1_000);
    assert_eq!(large.len(), 100_000);
    let s = section(&large, None, None);
    // The reference's answers over the same log (0.79 s there).
    assert_eq!(count(&s, &["brief", "deliveries"]), "10000");
    assert_eq!(count(&s, &["brief", "opened"]), "17400");
    assert_eq!(count(&s, &["brief", "used"]), "15000");
    assert_eq!(count(&s, &["recall", "opened"]), "2000");
    timing::grows_within(
        "recall_counters compute_surface_report",
        timing::TENFOLD,
        &mut || timing::once(|| section(&small, None, None)),
        &mut || timing::once(|| section(&large, None, None)),
    );
}

#[test]
fn ints_past_64_bits() {
    let big = "1".repeat(400);
    let negative = format!("-{}", "9".repeat(300));
    let mut events = Vec::new();
    for (k, chars) in [big.as_str(), negative.as_str(), "0", "18446744073709551616"]
        .iter()
        .enumerate()
    {
        events.push(ev(vec![
            ("kind", st("prompt_recall")),
            ("session", st("c1")),
            ("shown_ids", ids(&["m1".into()])),
            ("rule", st("r")),
            ("chars", PyValue::Int((*chars).to_string())),
            ("segment", PyValue::Int((*chars).to_string())),
            ("ts", stamp(k)),
        ]));
        events.push(ev(vec![
            ("kind", st("review_unanswered")),
            ("count", PyValue::Int((*chars).to_string())),
        ]));
    }
    let s = section(&events, None, None);
    // Sorted: the nines, 0, 2^64, the ones; median_low is the second.
    assert_eq!(count(&s, &["recall", "chars_p50"]), "0");
    // segment 0 is not a segment.
    assert_eq!(count(&s, &["segments", "opened"]), "3");
    // 1...1 (400 ones) - 9...9 (300 nines) + 0 + 2^64, as the reference
    // adds them.
    let want = format!("{}0{}29557855184820662728", "1".repeat(99), "1".repeat(280));
    assert_eq!(count(&s, &["review_unanswered", "chains"]), want);
    // Cancelling sums answer exactly zero.
    let row = |n: &str| {
        ev(vec![
            ("kind", st("review_unanswered")),
            ("count", PyValue::Int(n.into())),
        ])
    };
    let events = [row(&big), row(&format!("-{big}")), row("0")];
    assert_eq!(
        count(
            &section(&events, None, None),
            &["review_unanswered", "chains"]
        ),
        "0"
    );
    let events = [row("-1000000000"), row("1")];
    assert_eq!(
        count(
            &section(&events, None, None),
            &["review_unanswered", "chains"]
        ),
        "-999999999"
    );
}

fn deep_list(depth: usize) -> PyValue {
    let mut v = st("m1");
    for _ in 0..depth {
        v = PyValue::List(vec![v]);
    }
    v
}

fn deep_tuple(depth: usize) -> PyValue {
    let mut v = PyValue::List(Vec::new());
    for _ in 0..depth {
        v = PyValue::Tuple(vec![v]);
    }
    v
}

#[test]
fn unhashable_and_nested_values_at_every_read() {
    let hostile = [
        deep_list(100_000),
        deep_tuple(100_000),
        PyValue::Dict(vec![(st("reason"), deep_list(1_000))]),
        PyValue::List((0..100_000).map(|i| st(&format!("m{}", i % 3))).collect()),
        PyValue::Int(format!("-{}", "7".repeat(5_000))),
        PyValue::Float(f64::NAN),
        st(&"h".repeat(1_000_000)),
    ];
    let keys = [
        "kind",
        "session",
        "session_id",
        "harness_session",
        "worktree_root",
        "triggered_from",
        "ts",
        "ids",
        "shown_ids",
        "id",
        "memory_ids",
        "rule",
        "outcome",
        "auto",
        "attribution",
        "claim_excerpts",
        "surface",
        "segment",
        "agent_id",
        "chars",
        "suppressed_ids",
        "count",
        "sample_id",
        "stratum",
        "relevant_ids",
        "irrelevant_ids",
    ];
    for bad in &hostile {
        for key in keys {
            for kind in [
                "session_brief",
                "prompt_recall",
                "show",
                "use",
                "recall_grade",
            ] {
                let mut row = vec![
                    ("kind", st(kind)),
                    ("session", st("c1")),
                    ("harness_session", st("c1")),
                    ("worktree_root", st("/wt/a")),
                    ("ids", ids(&["m1".into()])),
                    ("shown_ids", ids(&["m1".into()])),
                    ("id", st("m1")),
                    ("rule", st("r")),
                    ("outcome", st("applied")),
                    ("claim_excerpts", ids(&["x".into()])),
                    ("sample_id", st("s")),
                    ("stratum", st("brief")),
                    ("ts", stamp(5)),
                ];
                row.retain(|(k, _)| *k != key);
                row.push((key, bad.clone()));
                let events = vec![
                    ev(vec![
                        ("kind", st("session_brief")),
                        ("session", st("c1")),
                        ("ids", ids(&["m1".into()])),
                        ("triggered_from", st("session_hook")),
                        ("ts", stamp(1)),
                    ]),
                    ev(row),
                ];
                let got = rc::compute_surface_report::<PyError>(&events, None, None, None);
                // Only an unhashable kind, trigger or settlement surface, or
                // suppressed_ids that is not iterable, raises.
                if let Err(e) = got {
                    assert_eq!(e, PyError::TypeError, "{key} {kind}");
                    assert!(
                        matches!(
                            key,
                            "kind" | "triggered_from" | "surface" | "suppressed_ids"
                        ),
                        "{key} {kind}"
                    );
                }
                for event in &events {
                    let _ = rc::is_surface_row(event);
                    let _ = rc::is_surface_show(event);
                    let _ = rc::surface_of(event);
                    let _ = rc::shown_ids(event);
                    let _ = rc::session_of(event);
                    let _ = rc::attested(event, &cps("m1"));
                }
            }
        }
    }
}

#[test]
fn extreme_stamps_and_bounds_do_not_panic() {
    let stamps = [
        "0001-01-01T00:00:00+23:59",
        "0001-01-01T00:00:00-23:59",
        "9999-12-31T23:59:59.999999-23:59:59.999999",
        "9999-12-31T23:59:59.999999+23:59",
        "2026-01-01T12:00:00Z",
        "2026-01-01",
        "",
        "Z",
    ];
    let mut events = Vec::new();
    for s in stamps {
        events.push(ev(vec![
            ("kind", st("session_brief")),
            ("session", st("c1")),
            ("ids", ids(&["m1".into()])),
            ("triggered_from", st("session_hook")),
            ("ts", st(s)),
        ]));
        events.push(ev(vec![
            ("kind", st("show")),
            ("id", st("m1")),
            ("harness_session", st("c1")),
            ("ts", st(s)),
        ]));
    }
    for start in stamps {
        for end in stamps {
            let (start, end) = (parse_event_ts(&cps(start)), parse_event_ts(&cps(end)));
            let _ = rc::compute_surface_report::<PyError>(&events, start, end, None).unwrap();
            let _ = rc::in_window(start, start, end);
        }
    }
}
