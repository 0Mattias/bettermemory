//! Hostile input to `audit.py`'s builders: many hits, long texts, integer
//! bounds and a deeply nested `settled_count`. No panic, and the builders
//! grow linearly in the hits they copy.

mod timing;

use bettermemory_core::audit::report::{
    self as rp, AuditError, MissHit, MissReport, TurnAuditedOptions,
};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode::cps;

fn report(hits: usize, text_len: usize) -> MissReport {
    let mut r = rp::no_owner_prompt_report(
        &cps("s"),
        DateTime::from_isoformat(&cps("0001-01-01T00:00:00+00:00")).unwrap(),
        i64::MIN,
    );
    r.recent_retrieval_count = i64::MAX;
    r.no_signal_reason = None;
    r.probe_query = Some(vec![0xD800; text_len]);
    r.top_hits = (0..hits)
        .map(|i| MissHit {
            id: cps(&format!("m{i}")),
            score: if i % 2 == 0 { f64::NAN } else { f64::INFINITY },
            relevance: vec![0x10FFFF; 3],
            scopes: vec![vec![0; 2]; 4],
            snippet: vec![u32::from(b'x'); text_len],
            matched_unique: i64::MIN,
            query_unique: i64::MAX,
            relevance_v2: Vec::new(),
        })
        .collect();
    r
}

fn mint() -> String {
    "ID".into()
}

#[test]
fn large_reports_build_without_panic() {
    let r = report(2_000, 10_000);
    let v = rp::turn_audited_fields(
        &r,
        &cps("s"),
        &cps("p"),
        true,
        &cps("stop_hook"),
        &Default::default(),
    )
    .unwrap();
    assert!(matches!(v.get("top_hits"), Some(PyValue::List(l)) if l.len() == 2_000));
    rp::search_miss_fields(&r, &cps("s"), &cps("mcp_tool"), None, None, None, &mut mint).unwrap();
    let _ = r.to_dict();
}

#[test]
fn a_long_bad_triggered_from_is_quoted_whole() {
    let bad = vec![u32::from(b'\''); 100_000];
    let e = rp::search_miss_fields(&report(1, 1), &cps("s"), &bad, None, None, None, &mut mint);
    let Err(AuditError::ValueError(m)) = e else {
        panic!("expected a ValueError")
    };
    assert!(m.len() > 100_000);
}

#[test]
fn a_deeply_nested_settled_count_is_refused_without_overflow() {
    let mut v = PyValue::int(1);
    for _ in 0..200_000 {
        v = PyValue::List(vec![v]);
    }
    let o = TurnAuditedOptions {
        settled_count: Some(v),
        ..Default::default()
    };
    let e = rp::turn_audited_fields(
        &report(0, 0),
        &cps("s"),
        &cps("p"),
        true,
        &cps("stop_hook"),
        &o,
    );
    // The reference's f-string raises RecursionError from repr.
    assert!(matches!(e, Err(AuditError::Repr(_))));
}

#[test]
fn the_builders_grow_linearly_in_hits() {
    let (small, large) = (report(500, 50), report(5_000, 50));
    let time = |r: &MissReport| {
        timing::once(|| {
            rp::search_miss_fields(r, &cps("s"), &cps("stop_hook"), None, None, None, &mut mint)
        })
    };
    timing::grows_within(
        "500 and 5,000 hits",
        timing::TENFOLD,
        &mut || time(&small),
        &mut || time(&large),
    );
}

#[test]
fn a_hand_built_settled_count_that_is_not_canonical_is_refused() {
    for text in [" -1", "-0", "+1", "01", "", "1 "] {
        let o = TurnAuditedOptions {
            settled_count: Some(PyValue::Int(text.into())),
            ..Default::default()
        };
        let e = rp::turn_audited_fields(
            &report(0, 0),
            &cps("s"),
            &cps("p"),
            true,
            &cps("stop_hook"),
            &o,
        );
        assert!(e.is_err(), "{text:?} was taken");
    }
    let o = TurnAuditedOptions {
        settled_count: Some(PyValue::Int("0".into())),
        ..Default::default()
    };
    assert!(rp::turn_audited_fields(
        &report(0, 0),
        &cps("s"),
        &cps("p"),
        true,
        &cps("stop_hook"),
        &o
    )
    .is_ok());
}

#[test]
fn a_repeated_entry_key_keeps_its_first_place_and_last_value() {
    let held = vec![vec![
        (cps("id"), cps("a")),
        (cps("x"), cps("1")),
        (cps("id"), cps("b")),
    ]];
    let f = rp::recall_held_fields(
        &report(0, 0),
        &cps("s"),
        &cps("p"),
        &cps("no_hits"),
        &held,
        &cps("r"),
        &cps("prompt_hook"),
        &rp::RecallTail::default(),
        &mut mint,
    )
    .unwrap();
    // dict([("id", "a"), ("x", "1"), ("id", "b")]) == {"id": "b", "x": "1"}
    let want = PyValue::List(vec![PyValue::Dict(vec![
        (PyValue::str("id"), PyValue::str("b")),
        (PyValue::str("x"), PyValue::str("1")),
    ])]);
    assert_eq!(f.get("held"), Some(&want));
}
