//! Cases a plausible wrong port of `recall_state`'s rebuild fails. Each
//! answer is the reference's (`rebuild_session` and `is_settlement` under
//! CPython 3.13.13 over the same rows); a single-segment log passes most
//! of these mistakes, so each case is built to tell one of them apart.

mod common;

use std::collections::BTreeSet;

use bettermemory_core::pyvalue::{PyError, PyStr, PyValue};
use bettermemory_core::recall_state::{
    closing_view, is_settlement, rebuild_session, view, RebuildError, SessionRecall,
};
use bettermemory_core::unicode::cps;
use common::recall_state::{dt, event, events, key, Rows, NOW};

fn run(rows: &str, agent: Option<&str>) -> SessionRecall {
    let agent = agent.map(cps);
    rebuild_session(
        &mut Rows::new(events(rows)),
        &key(),
        dt(NOW),
        agent.as_deref(),
    )
    .expect("no raise")
}

fn ids(items: &[&str]) -> BTreeSet<PyStr> {
    items.iter().map(|i| cps(i)).collect()
}

fn by(e: &SessionRecall) -> Vec<(String, String, Option<String>)> {
    let t = |v: &PyStr| {
        v.iter()
            .map(|&c| char::from_u32(c).unwrap())
            .collect::<String>()
    };
    e.shown_by
        .iter()
        .map(|(k, (s, ev))| (t(k), t(s), ev.as_ref().map(t)))
        .collect()
}

#[test]
fn a_bool_segment_opens_no_segment() {
    // True == 1 in Python, but `isinstance(True, bool)` keeps it out: the
    // entry stays in segment 0 with both shows.
    let e = run(
        r#"[{"kind":"prompt_recall","ts":"2026-01-01T12:01:00Z","shown_ids":["a"]},
            {"kind":"prompt_recall","ts":"2026-01-01T12:02:00Z","shown_ids":["b"],"segment":true}]"#,
        None,
    );
    assert_eq!(e.segment, PyValue::int(0));
    assert_eq!(e.shown, ids(&["a", "b"]));
    assert_eq!(e.deliveries, 2);
}

#[test]
fn a_held_signal_reopens_instead_of_restarting() {
    let e = run(
        r#"[{"kind":"session_brief","ts":"2026-01-01T12:01:00Z","ids":["a"]},
            {"kind":"prompt_recall","ts":"2026-01-01T12:02:00Z","shown_ids":["c"],"marker_absent":true},
            {"kind":"use","ts":"2026-01-01T12:03:00Z","ids":["a","c"],"surface":"recall"},
            {"kind":"segment_opened","ts":"2026-01-01T12:08:00Z","segment":1,"opened_at":"2026-01-01T12:05:00+00:00"}]"#,
        None,
    );
    assert_eq!(e.segment, PyValue::int(1));
    assert_eq!(e.started_at, dt("2026-01-01T12:05:00+00:00"));
    assert_eq!(e.shown, ids(&["c"]));
    assert_eq!(e.settled, ids(&["c"]));
    assert_eq!(e.deliveries, 1);
    assert!(e.signal.is_none());
}

#[test]
fn opened_at_without_a_signal_restarts_at_opened_at() {
    let e = run(
        r#"[{"kind":"session_brief","ts":"2026-01-01T12:01:00Z","ids":["a"]},
            {"kind":"segment_opened","ts":"2026-01-01T12:08:00Z","segment":1,"opened_at":"2026-01-01T12:05:00+00:00"}]"#,
        None,
    );
    assert_eq!(e.started_at, dt("2026-01-01T12:05:00+00:00"));
    assert!(e.shown.is_empty());
    assert_eq!(e.deliveries, 0);
}

#[test]
fn a_plain_use_neither_starts_the_entry_nor_settles() {
    let e = run(
        r#"[{"kind":"use","ts":"2026-01-01T12:00:00Z","ids":["p"]},
            {"kind":"use","ts":"2026-01-01T12:01:00Z","ids":["q"],"surface":"other"},
            {"kind":"turn_audited","ts":"2026-01-01T12:09:00Z"}]"#,
        None,
    );
    assert_eq!(e.started_at, dt("2026-01-01T12:09:00+00:00"));
    assert!(e.settled.is_empty());
}

#[test]
fn a_shown_ids_of_none_is_present() {
    let e = run(
        r#"[{"kind":"prompt_recall","ts":"2026-01-01T12:01:00Z","shown_ids":null,"top_hits":[{"id":"h1"}]}]"#,
        None,
    );
    assert!(e.shown.is_empty());
    assert_eq!(e.deliveries, 1);
}

#[test]
fn shown_by_keeps_the_first_show() {
    let e = run(
        r#"[{"kind":"prompt_recall","ts":"2026-01-01T12:01:00Z","shown_ids":["b","a"],"event_id":"E1"},
            {"kind":"session_brief","ts":"2026-01-01T12:02:00Z","ids":["a","c"],"event_id":"E2"}]"#,
        None,
    );
    let r = |a: &str, b: &str, c: &str| (a.to_string(), b.to_string(), Some(c.to_string()));
    assert_eq!(
        by(&e),
        vec![
            r("b", "recall", "E1"),
            r("a", "recall", "E1"),
            r("c", "brief", "E2")
        ]
    );
    // The view's map is keyed by id; the entry keeps the dict's order.
    let v = view(&e);
    assert_eq!(
        v.shown_by.keys().cloned().collect::<Vec<_>>(),
        vec![cps("a"), cps("b"), cps("c")]
    );
}

#[test]
fn the_agent_scope_is_kept() {
    let rows = r#"[{"kind":"session_brief","ts":"2026-01-01T12:01:00Z","ids":["a"]},
            {"kind":"prompt_recall","ts":"2026-01-01T12:02:00Z","shown_ids":["b"],"agent_id":"A"},
            {"kind":"review_unanswered","ts":"2026-01-01T12:03:00Z","root_ids":["r"],"agent_id":"A"}]"#;
    let own = run(rows, None);
    assert_eq!(own.shown, ids(&["a"]));
    assert!(own.review_roots.is_empty());
    let sub = run(rows, Some("A"));
    assert_eq!(sub.shown, ids(&["b"]));
    assert_eq!(sub.started_at, dt("2026-01-01T12:02:00+00:00"));
    assert_eq!(sub.review_roots, ids(&["r"]));
}

#[test]
fn only_the_session_scope_withdraws() {
    let rows = r#"[{"kind":"session_brief","ts":"2026-01-01T12:01:00Z","ids":["a","b"],"marker_absent":true},
            {"kind":"segment_opened","ts":"2026-01-01T12:02:00Z","agent_id":"A","withdrawn":["b"]},
            {"kind":"segment_opened","ts":"2026-01-01T12:03:00Z","agent_id":"B","withdrawn":["a"]}]"#;
    let own = run(rows, None);
    assert!(own.shown.is_empty());
    assert!(own.signal.is_none());
    // Agent B's scope reads its own row (a show of `a`) and withdraws
    // nothing for agent A's.
    let b = run(rows, Some("B"));
    assert_eq!(b.shown, ids(&["a"]));
}

#[test]
fn the_harness_filter_reads_only_a_str_harness() {
    let e = run(
        r#"[{"kind":"session_brief","ts":"2026-01-01T12:01:00Z","ids":["a"],"harness":5},
            {"kind":"session_brief","ts":"2026-01-01T12:02:00Z","ids":["b"],"harness":"codex"}]"#,
        None,
    );
    assert_eq!(e.shown, ids(&["a"]));
}

#[test]
fn review_roots_span_the_session() {
    let e = run(
        r#"[{"kind":"review_unanswered","ts":"2026-01-01T12:00:00Z","root_ids":["r1"]},
            {"kind":"prompt_recall","ts":"2026-01-01T12:01:00Z","shown_ids":["a"],"segment":4}]"#,
        None,
    );
    assert_eq!(e.review_roots, ids(&["r1"]));
    assert_eq!(e.segment, PyValue::int(4));
    assert_eq!(e.started_at, dt("2026-01-01T12:01:00+00:00"));
}

#[test]
fn a_withdraw_needs_no_ts() {
    let e = run(
        r#"[{"kind":"session_brief","ts":"2026-01-01T12:01:00Z","ids":["a","b"],"marker_absent":true},
            {"kind":"segment_opened","agent_id":"A","withdrawn":["a"]}]"#,
        None,
    );
    assert_eq!(e.shown, ids(&["b"]));
}

#[test]
fn the_brief_signal_names_its_event_and_others_none() {
    let e = run(
        r#"[{"kind":"session_brief","ts":"2026-01-01T12:01:00Z","ids":["a"],"event_id":"E1","marker_absent":true}]"#,
        None,
    );
    assert_eq!(e.signal.as_ref().unwrap().event_id, Some(cps("E1")));
    let e = run(
        r#"[{"kind":"prompt_recall","ts":"2026-01-01T12:01:00Z","shown_ids":["a"],"event_id":"E1","marker_absent":true}]"#,
        None,
    );
    assert_eq!(e.signal.as_ref().unwrap().event_id, None);
    assert!(e.signal.as_ref().unwrap().shown.is_empty());
}

#[test]
fn big_segments_compare_as_ints() {
    let e = run(
        r#"[{"kind":"prompt_recall","ts":"2026-01-01T12:01:00Z","shown_ids":["a"],"segment":1180591620717411303424},
            {"kind":"prompt_recall","ts":"2026-01-01T12:02:00Z","shown_ids":["b"],"segment":1180591620717411303424}]"#,
        None,
    );
    assert_eq!(e.shown, ids(&["a", "b"]));
    assert_eq!(e.started_at, dt("2026-01-01T12:01:00+00:00"));
}

#[test]
fn the_closing_view_cuts_settled_to_shown() {
    let e = run(
        r#"[{"kind":"session_brief","ts":"2026-01-01T12:01:00Z","ids":["a"]},
            {"kind":"use","ts":"2026-01-01T12:02:00Z","ids":["a","z"],"surface":"brief"}]"#,
        None,
    );
    assert_eq!(view(&e).settled, ids(&["a", "z"]));
    assert_eq!(closing_view(&e, None).settled, ids(&["a"]));
    assert!(closing_view(&e, Some(&ids(&["q"]))).shown_by.is_empty());
}

#[test]
fn an_unhashable_surface_raises_only_on_a_use() {
    assert_eq!(
        is_settlement(&event(r#"{"kind":"use","surface":["brief"]}"#)),
        Err(PyError::TypeError)
    );
    assert_eq!(
        is_settlement(&event(r#"{"kind":"prompt_recall","surface":["brief"]}"#)),
        Ok(false)
    );
    let raised = rebuild_session(
        &mut Rows::new(events(
            r#"[{"kind":"use","ts":"2026-01-01T12:00:00Z","surface":{"a":1}}]"#,
        )),
        &key(),
        dt(NOW),
        None,
    );
    assert!(matches!(
        raised,
        Err(RebuildError::Raised(PyError::TypeError))
    ));
}

#[test]
fn a_list_or_dict_inside_a_tuple_surface_raises() {
    // `hash` of a tuple hashes its items: a list or dict at any depth
    // inside it raises TypeError, a tuple of strs hashes.
    let use_with = |surface: PyValue| {
        PyValue::Dict(vec![
            (PyValue::str("kind"), PyValue::str("use")),
            (PyValue::str("surface"), surface),
        ])
    };
    let tuple = |items: Vec<PyValue>| PyValue::Tuple(items);
    assert_eq!(
        is_settlement(&use_with(tuple(vec![PyValue::List(vec![])]))),
        Err(PyError::TypeError)
    );
    assert_eq!(
        is_settlement(&use_with(tuple(vec![
            PyValue::str("a"),
            tuple(vec![PyValue::Dict(vec![])]),
        ]))),
        Err(PyError::TypeError)
    );
    assert_eq!(
        is_settlement(&use_with(tuple(vec![PyValue::str("a"), PyValue::str("b")]))),
        Ok(false)
    );
}

/// A kind of `10**digits`, negated when `negative`.
fn power_of_ten(digits: usize, negative: bool) -> PyValue {
    let sign = if negative { "-" } else { "" };
    PyValue::Int(format!("{sign}1{}", "0".repeat(digits - 1)))
}

#[test]
fn an_int_kind_past_4300_digits_raises_where_str_does() {
    // `str(kind)` of an int of 4,301 digits raises ValueError, also of a
    // negative one and inside a container (`repr` spells each item);
    // 4,300 digits is a recall row. A scripted store only: the log's
    // read refuses such an int first.
    let rebuild = |kind: PyValue| {
        rebuild_session(
            &mut Rows::new(vec![PyValue::Dict(vec![
                (PyValue::str("kind"), kind),
                (PyValue::str("ts"), PyValue::str("2026-01-01T12:00:00Z")),
            ])]),
            &key(),
            dt(NOW),
            None,
        )
    };
    let fits = rebuild(power_of_ten(4300, false)).expect("no raise");
    assert_eq!(fits.deliveries, 1);
    let fits = rebuild(power_of_ten(4300, true)).expect("no raise");
    assert_eq!(fits.deliveries, 1);
    let too_long = [
        power_of_ten(4301, false),
        power_of_ten(4301, true),
        PyValue::List(vec![PyValue::str("a"), power_of_ten(4301, false)]),
        PyValue::Tuple(vec![power_of_ten(4301, false)]),
        PyValue::Dict(vec![(power_of_ten(4301, false), PyValue::int(1))]),
        PyValue::Dict(vec![(
            PyValue::str("a"),
            PyValue::List(vec![power_of_ten(4301, false)]),
        )]),
    ];
    for kind in too_long {
        assert!(matches!(
            rebuild(kind),
            Err(RebuildError::Raised(PyError::ValueError))
        ));
    }
}
