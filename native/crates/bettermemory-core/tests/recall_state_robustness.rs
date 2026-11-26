//! Hostile input through `recall_state`: long logs, many subagents' rows,
//! a segment count driven below zero, segment numbers past every machine
//! integer, deep values. No panic in debug or release, and the rebuild
//! grows linearly with the rows (the reference's walk is one pass whose
//! per-row cost is bounded by the row, as long as no hold or reopen
//! copies a large segment on every row).

mod common;
mod timing;

use std::collections::BTreeSet;

use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::recall_state::{
    hold_signal, rebuild_session, reopen, replay, withdraw, RebuildError, SessionRecall, Signal,
};
use bettermemory_core::unicode::cps;
use common::recall_state::{dt, key, ts, Rows, NOW};

fn st(t: &str) -> PyValue {
    PyValue::str(t)
}

fn row(pairs: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(pairs.into_iter().map(|(k, v)| (st(k), v)).collect())
}

fn stamp(i: usize) -> PyValue {
    st(&ts(i % 60))
}

/// `n` rows of one session: recalls, briefs, settlements, plain uses,
/// audits and review rows in turn, a new segment every 1,000 rows.
fn session_rows(n: usize) -> Vec<PyValue> {
    (0..n)
        .map(|i| {
            let id = st(&format!("m{}", i % 500));
            let segment = PyValue::int((i / 1000) as i128);
            match i % 6 {
                0 => row(vec![
                    ("kind", st("prompt_recall")),
                    ("ts", stamp(i)),
                    ("segment", segment),
                    ("shown_ids", PyValue::List(vec![id])),
                    ("event_id", st("E")),
                ]),
                1 => row(vec![
                    ("kind", st("session_brief")),
                    ("ts", stamp(i)),
                    ("ids", PyValue::List(vec![id])),
                ]),
                2 => row(vec![
                    ("kind", st("use")),
                    ("ts", stamp(i)),
                    ("ids", PyValue::List(vec![id])),
                    ("surface", st("recall")),
                ]),
                3 => row(vec![
                    ("kind", st("use")),
                    ("ts", stamp(i)),
                    ("ids", PyValue::List(vec![id])),
                ]),
                4 => row(vec![("kind", st("turn_audited")), ("ts", stamp(i))]),
                _ => row(vec![
                    ("kind", st("review_unanswered")),
                    ("ts", stamp(i)),
                    ("root_ids", PyValue::List(vec![id])),
                ]),
            }
        })
        .collect()
}

/// The rows of `agents` subagents, 20 each, which the session's scope
/// skips, with the session's own row first.
fn agent_rows(agents: usize) -> Vec<PyValue> {
    let mut rows = vec![row(vec![
        ("kind", st("session_brief")),
        ("ts", stamp(0)),
        ("ids", PyValue::List(vec![st("a")])),
    ])];
    for a in 0..agents {
        for i in 0..20 {
            rows.push(row(vec![
                ("kind", st("prompt_recall")),
                ("ts", stamp(i)),
                ("agent_id", st(&format!("agent-{a}"))),
                ("shown_ids", PyValue::List(vec![st(&format!("x{i}"))])),
            ]));
        }
    }
    rows
}

fn rebuild(rows: &[PyValue], agent: Option<&str>) -> SessionRecall {
    let agent = agent.map(cps);
    rebuild_session(
        &mut Rows::new(rows.to_vec()),
        &key(),
        dt(NOW),
        agent.as_deref(),
    )
    .expect("no raise")
}

#[test]
fn two_hundred_thousand_rows_of_one_session() {
    let rows = session_rows(200_000);
    let e = rebuild(&rows, None);
    assert_eq!(e.segment, PyValue::int(199));
    assert_eq!(e.review_roots.len(), 250);
    assert!(e.deliveries > 0);
}

#[test]
fn the_rebuild_is_linear_in_the_rows() {
    let (small, large) = (session_rows(20_000), session_rows(200_000));
    timing::grows_within(
        "rebuild_session: 20,000 and 200,000 rows",
        timing::TENFOLD,
        &mut || {
            let store = Rows::new(small.clone());
            timing::once(move || {
                let mut store = store;
                rebuild_session(&mut store, &key(), dt(NOW), None)
            })
        },
        &mut || {
            let store = Rows::new(large.clone());
            timing::once(move || {
                let mut store = store;
                rebuild_session(&mut store, &key(), dt(NOW), None)
            })
        },
    );
}

#[test]
fn ten_thousand_subagents_rows() {
    let rows = agent_rows(10_000);
    let own = rebuild(&rows, None);
    assert_eq!(own.shown.len(), 1);
    let one = rebuild(&rows, Some("agent-9999"));
    assert_eq!(one.shown.len(), 20);
    assert_eq!(one.deliveries, 20);
    let (small, large) = (agent_rows(1_000), agent_rows(10_000));
    timing::grows_within(
        "rebuild_session: 1,000 and 10,000 subagents' rows",
        timing::TENFOLD,
        &mut || {
            let store = Rows::new(small.clone());
            timing::once(move || {
                let mut store = store;
                rebuild_session(&mut store, &key(), dt(NOW), None)
            })
        },
        &mut || {
            let store = Rows::new(large.clone());
            timing::once(move || {
                let mut store = store;
                rebuild_session(&mut store, &key(), dt(NOW), None)
            })
        },
    );
}

#[test]
fn deliveries_go_below_zero_as_an_int_does() {
    let mut e = SessionRecall::new(0, dt(NOW), dt(NOW));
    let signal = Signal {
        at: dt(NOW),
        shown: BTreeSet::new(),
        deliveries: 7,
        event_id: None,
    };
    reopen(&mut e, &signal, PyValue::int(1), dt(NOW), None);
    assert_eq!(e.deliveries, -7);
    reopen(&mut e, &signal, PyValue::int(2), dt(NOW), None);
    assert_eq!(e.deliveries, -14);
    hold_signal(&mut e, dt(NOW), None);
    assert_eq!(e.signal.as_ref().unwrap().deliveries, -14);
    withdraw(&mut e, &[cps("absent")]);
    assert!(e.signal.is_none());
}

#[test]
fn segment_numbers_past_every_machine_int() {
    let huge = format!("-{}", "9".repeat(5_000));
    let rows = vec![
        row(vec![
            ("kind", st("prompt_recall")),
            ("ts", stamp(1)),
            ("segment", PyValue::int_from_decimal(&huge).unwrap()),
            ("shown_ids", PyValue::List(vec![st("a")])),
        ]),
        row(vec![
            ("kind", st("prompt_recall")),
            ("ts", stamp(2)),
            ("segment", PyValue::int_from_decimal(&huge).unwrap()),
            ("shown_ids", PyValue::List(vec![st("b")])),
        ]),
        row(vec![
            ("kind", st("prompt_recall")),
            ("ts", stamp(3)),
            ("segment", PyValue::int(i128::MIN)),
            ("shown_ids", PyValue::List(vec![st("c")])),
        ]),
    ];
    let e = rebuild(&rows, None);
    assert_eq!(e.segment, PyValue::int(i128::MIN));
    assert_eq!(e.shown.len(), 1);
}

fn nested(depth: usize) -> PyValue {
    let mut v = PyValue::int(0);
    for _ in 0..depth {
        v = PyValue::List(vec![v]);
    }
    v
}

#[test]
fn a_deep_kind_raises_where_str_does() {
    // The port raises RecursionError at `repr`'s bound (a kind of
    // MAX_DEPTH = 9,998 lists), chosen. CPython 3.13.13's `str()` bound
    // follows the call context: it first raises at 9,999 lists in a
    // direct call, through `rebuild_session` and under `map(str, ...)`,
    // and earlier where a C caller sits between (9,997 under
    // `sorted(key=str)`, 9,996 under `functools.reduce`), so no constant
    // matches it. A row is a dict and `json.loads` raises past 9,998
    // containers, so a kind read from the log holds at most 9,997 lists:
    // port and reference agree there unless such a C caller is on the
    // reference's stack. Two levels less is a recall row.
    let deep = nested(bettermemory_core::json::MAX_DEPTH);
    let mut e = SessionRecall::new(0, dt(NOW), dt(NOW));
    let r = replay(&mut e, &row(vec![("kind", deep)]), dt(NOW));
    assert_eq!(r, Err(PyError::RecursionError));
    let fits = nested(bettermemory_core::json::MAX_DEPTH - 2);
    let mut e = SessionRecall::new(0, dt(NOW), dt(NOW));
    assert_eq!(replay(&mut e, &row(vec![("kind", fits)]), dt(NOW)), Ok(()));
    assert_eq!(e.deliveries, 1);
    let raised = rebuild_session(
        &mut Rows::new(vec![row(vec![
            ("kind", nested(bettermemory_core::json::MAX_DEPTH)),
            ("ts", stamp(0)),
        ])]),
        &key(),
        dt(NOW),
        None,
    );
    assert!(matches!(
        raised,
        Err(RebuildError::Raised(PyError::RecursionError))
    ));
}

#[test]
fn deep_and_wide_fields_are_read_as_the_reference_reads_them() {
    let wide: Vec<PyValue> = (0..100_000).map(|i| st(&format!("w{i}"))).collect();
    let rows = vec![
        row(vec![
            ("kind", st("segment_opened")),
            ("ts", stamp(0)),
            ("withdrawn", PyValue::List(wide.clone())),
            ("marker_absent", PyValue::Bool(true)),
        ]),
        row(vec![
            ("kind", st("prompt_recall")),
            ("ts", stamp(1)),
            ("top_hits", PyValue::List(vec![nested(50_000)])),
            ("ids", nested(50_000)),
        ]),
        row(vec![
            ("kind", st("use")),
            ("ts", stamp(2)),
            ("surface", st("brief")),
            ("ids", PyValue::List(wide)),
        ]),
        row(vec![
            ("kind", st("segment_opened")),
            ("ts", stamp(3)),
            ("agent_id", st("A")),
            ("withdrawn", PyValue::List(vec![st("w1"), nested(50_000)])),
        ]),
    ];
    let e = rebuild(&rows, None);
    assert_eq!(e.shown.len(), 99_999);
    assert_eq!(e.settled.len(), 99_999);
    assert_eq!(e.deliveries, 1);
    assert!(e.signal.is_none());
}
