//! Cases a plausible wrong port of `audit.py`'s builders fails: the order
//! of the checks, the key order of each shape, the keys written only
//! when set, and the bool and int rule of `settled_count`.

use bettermemory_core::audit::report::{
    self as rp, AuditError, MissHit, MissReport, PromptRecallOptions, TurnAuditedOptions, Verdict,
};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;

fn at() -> DateTime {
    DateTime::from_isoformat(&cps("2026-01-02T03:04:05+00:00")).unwrap()
}

fn hit() -> MissHit {
    MissHit {
        id: cps("m1"),
        score: 0.5,
        relevance: cps("high"),
        scopes: vec![cps("global")],
        snippet: cps("body"),
        matched_unique: 1,
        query_unique: 2,
        relevance_v2: cps("low"),
    }
}

fn report() -> MissReport {
    let mut r = rp::no_owner_prompt_report(&cps("s"), at(), 600);
    r.verdict = Verdict::Miss;
    r.no_signal_reason = None;
    r.top_hits = vec![hit()];
    r
}

fn keys(v: &PyValue) -> Vec<String> {
    let PyValue::Dict(items) = v else {
        panic!("not a dict")
    };
    items
        .iter()
        .map(|(k, _)| match k {
            PyValue::Str(s) => s.iter().map(|&c| char::from_u32(c).unwrap()).collect(),
            _ => panic!("key not a str"),
        })
        .collect()
}

fn err(e: Result<PyValue, AuditError>) -> String {
    match e {
        Err(AuditError::ValueError(m)) => m.iter().map(|&c| char::from_u32(c).unwrap()).collect(),
        other => panic!("expected a ValueError, got {other:?}"),
    }
}

fn mint() -> String {
    "ID".into()
}

#[test]
fn settled_count_is_checked_before_triggered_from() {
    let o = TurnAuditedOptions {
        settled_count: Some(PyValue::int(-1)),
        ..Default::default()
    };
    let m = err(rp::turn_audited_fields(
        &report(),
        &cps("s"),
        &cps("p"),
        true,
        &cps("bad"),
        &o,
    ));
    assert!(m.starts_with("settled_count"), "{m}");
}

#[test]
fn a_bool_settled_count_is_refused_and_zero_is_kept() {
    for b in [true, false] {
        let o = TurnAuditedOptions {
            settled_count: Some(PyValue::Bool(b)),
            ..Default::default()
        };
        let m = err(rp::turn_audited_fields(
            &report(),
            &cps("s"),
            &cps("p"),
            true,
            &cps("stop_hook"),
            &o,
        ));
        assert!(m.ends_with(if b { "got True" } else { "got False" }), "{m}");
    }
    let o = TurnAuditedOptions {
        settled_count: Some(PyValue::int(0)),
        ..Default::default()
    };
    let v = rp::turn_audited_fields(
        &report(),
        &cps("s"),
        &cps("p"),
        false,
        &cps("stop_hook"),
        &o,
    )
    .unwrap();
    assert_eq!(keys(&v).last().unwrap(), "settled_count");
    let o = TurnAuditedOptions {
        settled_count: Some(PyValue::Float(3.0)),
        ..Default::default()
    };
    let m = err(rp::turn_audited_fields(
        &report(),
        &cps("s"),
        &cps("p"),
        true,
        &cps("stop_hook"),
        &o,
    ));
    assert!(m.ends_with("got 3.0"), "{m}");
}

#[test]
fn the_message_spells_repr_of_the_sorted_list() {
    let m = err(rp::search_miss_fields(
        &report(),
        &cps("s"),
        &cps("it's"),
        None,
        None,
        None,
        &mut mint,
    ));
    assert_eq!(
        m,
        "triggered_from must be one of ['mcp_tool', 'prompt_hook', 'session_hook', 'stop_hook'], got \"it's\""
    );
}

#[test]
fn compact_and_full_hit_orders_differ() {
    assert_eq!(
        keys(&hit().to_compact_dict()),
        [
            "id",
            "score",
            "relevance",
            "relevance_v2",
            "matched_unique",
            "query_unique"
        ]
    );
    assert_eq!(
        keys(&hit().to_dict()),
        [
            "id",
            "score",
            "relevance",
            "scopes",
            "snippet",
            "matched_unique",
            "query_unique",
            "relevance_v2"
        ]
    );
}

#[test]
fn turn_audited_omits_empty_hits_and_a_false_repeat() {
    let mut r = report();
    r.top_hits.clear();
    let v = rp::turn_audited_fields(
        &r,
        &cps("s"),
        &cps("p"),
        true,
        &cps("stop_hook"),
        &Default::default(),
    )
    .unwrap();
    let k = keys(&v);
    assert!(
        !k.contains(&"top_hits".into())
            && !k.contains(&"repeat".into())
            && !k.contains(&"probe_query".into())
    );
}

#[test]
fn search_miss_keeps_an_empty_event_id_and_mints_only_on_none() {
    let mut calls = 0;
    let mut counting = || {
        calls += 1;
        "M".to_string()
    };
    let v = rp::search_miss_fields(
        &report(),
        &cps("s"),
        &cps("stop_hook"),
        Some(&[]),
        None,
        None,
        &mut counting,
    )
    .unwrap();
    assert_eq!(rp_get(&v, "event_id"), PyValue::Str(PyStr::new()));
    assert_eq!(calls, 0);
}

fn rp_get(v: &PyValue, key: &str) -> PyValue {
    v.get(key).cloned().expect("key present")
}

#[test]
fn a_rule_writes_chars_beside_it_even_when_empty() {
    let mut o = PromptRecallOptions::reference();
    o.rule = Some(PyStr::new());
    let v = rp::prompt_recall_fields(&report(), &cps("s"), &cps("p"), 9, &o, &mut mint).unwrap();
    let k = keys(&v);
    let i = k.iter().position(|x| x == "rule").unwrap();
    assert_eq!(k[i + 1], "chars");
    assert_eq!(rp_get(&v, "chars"), PyValue::int(9));
}

#[test]
fn recall_held_checks_triggered_from_before_cause() {
    let m = err(rp::recall_held_fields(
        &report(),
        &cps("s"),
        &cps("p"),
        &cps("bad"),
        &[],
        &cps("r"),
        &cps("bad"),
        &Default::default(),
        &mut mint,
    ));
    assert!(m.starts_with("triggered_from"), "{m}");
}

#[test]
fn checked_at_replaces_every_zero_offset() {
    let mut r = report();
    r.checked_at = DateTime::from_isoformat(&cps("2026-06-01T00:00:00+00:00:30")).unwrap();
    let v = r.to_dict();
    assert_eq!(
        rp_get(&v, "checked_at"),
        PyValue::str("2026-06-01T00:00:00Z:30")
    );
}
