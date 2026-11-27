//! Hostile input for `eval_model::split`: a log of 100,000 rows holding
//! 20,000 shows within linear growth against one of 10,000 rows, shows of
//! 10,000 ids each against rc3 lists as long, and deeply nested, very
//! long and past-64-bit values at `rule` and `rc3_admitted`. Nothing
//! panics. The split's own work is one pass over the shows plus a set
//! per show; the two reports are `compute_surface_report`'s, linear in
//! conversations at a fixed size per conversation (its own robustness
//! file), so the claim here is linear in conversations too.

mod timing;

use bettermemory_core::eval_model::{self as em, MembershipDelta};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::recall_state::RULE;

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

/// `conversations` conversations of 100 rows each, interleaved: per
/// conversation 20 shows (every fifth row; a brief, then recalls under
/// rs-v8 with rc3 lists, under rs-v4 and under the empty rule in turn),
/// opens and uses of the shown ids between them.
fn big_log(conversations: usize) -> Vec<PyValue> {
    let mut events = Vec::with_capacity(conversations * 100);
    for step in 0..100 {
        for c in 0..conversations {
            let i = step * conversations + c;
            let session = st(&format!("c{c}"));
            let shown: Vec<String> = (0..3)
                .map(|j| format!("m{}", (c + j + step) % 11))
                .collect();
            let mut row = match (step % 5, (step / 5) % 4) {
                (0, 0) => ev(vec![
                    ("kind", st("session_brief")),
                    ("session", session),
                    ("ids", ids(&shown)),
                ]),
                (0, k) => {
                    let rule = ["", RULE, "rs-v4", ""][k];
                    let admitted: Vec<String> = (1..4)
                        .map(|j| format!("m{}", (c + j + step) % 11))
                        .collect();
                    ev(vec![
                        ("kind", st("prompt_recall")),
                        ("session", session),
                        ("shown_ids", ids(&shown)),
                        ("rule", st(rule)),
                        ("rc3_admitted", ids(&admitted)),
                    ])
                }
                (1 | 3, _) => ev(vec![
                    ("kind", st("show")),
                    ("session", session),
                    ("id", st(&shown[step % 3])),
                ]),
                _ => ev(vec![
                    ("kind", st("use")),
                    ("session", session),
                    ("ids", ids(&shown[..2])),
                    ("outcome", st("applied")),
                    ("claim_excerpts", ids(&["a".into(), "b".into()])),
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

fn int(v: &PyValue, path: &[&str]) -> String {
    match path.iter().fold(v, |v, k| v.get(k).expect("a key")) {
        PyValue::Int(n) => n.clone(),
        other => panic!("{path:?} is {other:?}"),
    }
}

#[test]
fn a_hundred_thousand_rows_grow_linearly() {
    let small = big_log(100);
    let large = big_log(1_000);
    assert_eq!(large.len(), 100_000);
    let (by_rule, delta) = em::surface_rule_split(&large, None, None).unwrap();
    // The reference's answers over the same log.
    assert_eq!(int(&by_rule, &[RULE, "recall", "deliveries"]), "5000");
    assert_eq!(int(&by_rule, &["earlier", "recall", "deliveries"]), "10000");
    assert_eq!(int(&by_rule, &["earlier", "brief", "deliveries"]), "5000");
    assert_eq!(
        delta,
        MembershipDelta {
            rows: 5000,
            rows_differing: 5000,
            delivered_not_rc3: 5000,
            rc3_not_delivered: 5000
        }
    );
    let run = |log: &[PyValue]| {
        let section = PyValue::Dict(Vec::new());
        em::with_rule_split(Some(&section), log, None, None).unwrap()
    };
    timing::grows_within(
        "eval_model with_rule_split",
        timing::TENFOLD,
        &mut || timing::once(|| run(&small)),
        &mut || timing::once(|| run(&large)),
    );
}

#[test]
fn shows_of_ten_thousand_ids() {
    let shown: Vec<String> = (0..10_000).map(|i| format!("m{i}")).collect();
    let admitted: Vec<String> = (5_000..15_000).map(|i| format!("m{i}")).collect();
    let rows: Vec<PyValue> = (0..20)
        .map(|i| {
            ev(vec![
                ("kind", st("prompt_recall")),
                ("session", st("c1")),
                ("shown_ids", ids(&shown)),
                ("rule", st(RULE)),
                ("rc3_admitted", ids(&admitted)),
                ("ts", stamp(i)),
            ])
        })
        .collect();
    let (_, delta) = em::surface_rule_split(&rows, None, None).unwrap();
    assert_eq!(
        delta,
        MembershipDelta {
            rows: 20,
            rows_differing: 20,
            delivered_not_rc3: 100_000,
            rc3_not_delivered: 100_000
        }
    );
}

fn deep_list(depth: usize) -> PyValue {
    let mut v = st(RULE);
    for _ in 0..depth {
        v = PyValue::List(vec![v]);
    }
    v
}

#[test]
fn hostile_rules_and_rc3_values() {
    let hostile = [
        deep_list(100_000),
        PyValue::Tuple(vec![deep_list(1_000)]),
        PyValue::Int(format!("-{}", "7".repeat(5_000))),
        PyValue::Float(f64::NAN),
        st(&"r".repeat(1_000_000)),
        PyValue::List(
            (0..100_000)
                .map(|i| {
                    if i % 2 == 0 {
                        st("m1")
                    } else {
                        PyValue::Int(i.to_string())
                    }
                })
                .collect(),
        ),
        PyValue::Dict(vec![(st(RULE), deep_list(1_000))]),
    ];
    for bad in &hostile {
        for key in ["rule", "rc3_admitted", "shown_ids", "ids", "ts", "kind"] {
            for kind in ["session_brief", "prompt_recall"] {
                let mut row = vec![
                    ("kind", st(kind)),
                    ("session", st("c1")),
                    ("ids", ids(&["m1".into()])),
                    ("shown_ids", ids(&["m1".into()])),
                    ("rule", st(RULE)),
                    ("rc3_admitted", ids(&["m1".into()])),
                    ("ts", stamp(5)),
                ];
                row.retain(|(k, _)| *k != key);
                row.push((key, bad.clone()));
                let rows = vec![ev(row), ev(vec![("kind", st("show")), ("id", st("m1"))])];
                let got = em::surface_rule_split(&rows, None, None);
                // An unhashable kind (a list, a dict, the tuple holding a
                // list) raises in the report; nothing else does.
                let unhashable = key == "kind"
                    && matches!(bad, PyValue::List(_) | PyValue::Dict(_) | PyValue::Tuple(_));
                assert_eq!(got.is_err(), unhashable, "{key} {kind}");
                if let Ok((_, delta)) = got {
                    assert!(delta.rows <= 1);
                }
            }
        }
    }
}
