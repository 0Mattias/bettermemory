//! Hostile input for `sessions`: 100,000 events over 10,000 sessions
//! through every function within a linear growth claim, unhashable and
//! deeply nested values at every field read, and extreme stamps. Nothing
//! panics.

mod timing;

use bettermemory_core::pyvalue::{PyError, PyStr, PyValue};
use bettermemory_core::sessions as ss;

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn st(s: &str) -> PyValue {
    PyValue::Str(cps(s))
}

fn ev(fields: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(fields.into_iter().map(|(k, v)| (st(k), v)).collect())
}

fn stamp(i: usize) -> PyValue {
    let s = i % 60;
    let m = (i / 60) % 60;
    let h = (i / 3600) % 24;
    st(&format!("2026-01-01T{h:02}:{m:02}:{s:02}+00:00"))
}

/// `n` events over `n / 10` conversations: per conversation a hook row,
/// its server's stamped tool row and a scope toggle, in turn; this
/// conversation (`h_me`) has one hook row first and no server.
fn big_log(n: usize) -> Vec<PyValue> {
    let sessions = n / 10;
    let mut events = vec![ev(vec![
        ("kind", st("turn_audited")),
        ("session", st("h_me")),
        ("triggered_from", st("prompt_hook")),
        ("ts", stamp(0)),
    ])];
    let mut i = 1;
    while events.len() < n {
        let k = i % sessions;
        let event = match i % 3 {
            0 => ev(vec![
                ("kind", st("turn_audited")),
                ("session", st(&format!("h{k}"))),
                ("triggered_from", st("stop_hook")),
                ("ts", stamp(i)),
            ]),
            1 => ev(vec![
                ("kind", st("search")),
                ("session", st(&format!("sess_{k}"))),
                ("harness_session", st(&format!("h{k}"))),
                (
                    "worktree_root",
                    st(if k.is_multiple_of(2) {
                        "/wt/a"
                    } else {
                        "/wt/b"
                    }),
                ),
                ("ts", stamp(i)),
            ]),
            _ => ev(vec![
                (
                    "kind",
                    st(if i.is_multiple_of(7) {
                        "scope_enable"
                    } else {
                        "scope_disable"
                    }),
                ),
                ("session", st(&format!("sess_{k}"))),
                ("scope", st(&format!("s{}", i % 50))),
                ("ts", stamp(i)),
            ]),
        };
        events.push(event);
        i += 1;
    }
    events
}

fn every_function(events: &[PyValue]) -> usize {
    let me = cps("h_me");
    let wt = cps("/wt/a");
    let mut n = 0;
    n += ss::hook_spans(events).map_or(0, |s| s.entries().len());
    n += ss::latest_in_process_session(events, Some(&wt))
        .unwrap()
        .map_or(0, |s| s.len());
    n += ss::harness_joined_sessions(events, Some(&cps("h3")))
        .unwrap()
        .len();
    n += ss::live_harness_sessions(events, Some(&me)).unwrap().len();
    n += ss::bridged_session(events, Some(&me), Some(&wt))
        .unwrap()
        .map_or(0, |s| s.len());
    n += ss::joined_sessions(events, Some(&me), None).unwrap().len();
    n += ss::disabled_scopes_from_events(events, Some(&wt), None)
        .unwrap()
        .len();
    n += ss::disabled_scopes_from_events(events, None, Some(&cps("sess_4")))
        .unwrap()
        .len();
    n
}

#[test]
fn every_function_grows_linearly() {
    let small = big_log(10_000);
    let large = big_log(100_000);
    assert!(every_function(&large) > 0);
    timing::grows_within(
        "sessions every function",
        timing::TENFOLD,
        &mut || timing::once(|| every_function(&small)),
        &mut || timing::once(|| every_function(&large)),
    );
}

fn deep_list(depth: usize) -> PyValue {
    let mut v = st("stop_hook");
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
        PyValue::Dict(vec![(st("a"), deep_list(1_000))]),
        PyValue::List((0..100_000).map(|_| st("x")).collect()),
        PyValue::Int("-340282366920938463463374607431768211456".to_string()),
        PyValue::Float(f64::NAN),
        PyValue::Tuple(vec![st("stop_hook")]),
        st(&"h".repeat(1_000_000)),
    ];
    let me = cps("h_me");
    let wt = cps("/wt/a");
    for bad in &hostile {
        for key in [
            "kind",
            "session",
            "session_id",
            "harness_session",
            "worktree_root",
            "scope",
            "ts",
        ] {
            let mut row = vec![
                ("kind", st("scope_disable")),
                ("session", st("sess_a")),
                ("harness_session", st("h_old")),
                ("worktree_root", st("/wt/a")),
                ("scope", st("s1")),
                ("ts", stamp(5)),
            ];
            row.retain(|(k, _)| *k != key);
            row.push((key, bad.clone()));
            let events = vec![
                ev(vec![
                    ("session", st("h_me")),
                    ("triggered_from", st("stop_hook")),
                    ("ts", stamp(1)),
                ]),
                ev(row),
            ];
            let _ = ss::hook_spans(&events).unwrap();
            let _ = ss::latest_in_process_session(&events, Some(&wt)).unwrap();
            let _ = ss::harness_joined_sessions(&events, Some(&me)).unwrap();
            let _ = ss::live_harness_sessions(&events, Some(&me)).unwrap();
            let _ = ss::bridged_session(&events, Some(&me), Some(&wt)).unwrap();
            let _ = ss::joined_sessions(&events, Some(&me), None).unwrap();
            let _ = ss::disabled_scopes_from_events(&events, Some(&wt), None).unwrap();
        }
        // At the trigger: a list, a dict or a tuple holding one raises.
        let events = vec![ev(vec![
            ("triggered_from", bad.clone()),
            ("session", st("s")),
        ])];
        let unhashable = matches!(bad, PyValue::List(_) | PyValue::Dict(_));
        let got = ss::latest_in_process_session(&events, None);
        if unhashable {
            assert_eq!(got, Err(PyError::TypeError));
        } else {
            assert_eq!(got, Ok(Some(cps("s"))));
        }
    }
    let events = vec![ev(vec![("triggered_from", deep_tuple(100_000))])];
    assert_eq!(ss::hook_spans(&events), Err(PyError::TypeError));
}

#[test]
fn extreme_stamps_do_not_panic() {
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
    for (i, s) in stamps.iter().enumerate() {
        events.push(ev(vec![
            (
                "session",
                st(if i.is_multiple_of(2) { "h_a" } else { "h_me" }),
            ),
            ("triggered_from", st("session_hook")),
            ("ts", st(s)),
        ]));
        events.push(ev(vec![
            ("session", st("sess_a")),
            ("harness_session", st("h_a")),
            ("ts", st(s)),
        ]));
    }
    let spans = ss::hook_spans(&events).unwrap();
    assert_eq!(spans.entries().len(), 2);
    let me = cps("h_me");
    let _ = ss::bridged_session(&events, Some(&me), None).unwrap();
    let _ = ss::live_harness_sessions(&events, Some(&me)).unwrap();
}
