//! Cases a plausible wrong `response_rows` fails: the verdict read from the
//! full missing count, the `path_drift` gate widened to the route bucket or
//! to the claim-anchored bucket, the additive keys emitted when empty or in
//! another order, an empty `actor` dict, and `+00:00` kept in a stamp.

use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::summaries::{MemoryHit, MemorySummary};
use bettermemory_core::records::{Actor, Confidence};
use bettermemory_core::response_rows::{ResponseBuilder, SIMILAR_HITS_SHOWN};
use bettermemory_core::unicode;

fn now() -> DateTime {
    DateTime::from_isoformat(&unicode::cps("2026-03-01T12:00:00+00:00")).expect("now")
}

fn stamp(text: &str) -> DateTime {
    DateTime::from_isoformat(&unicode::cps(text)).expect("stamp")
}

fn paths(items: &[&str]) -> Vec<Vec<u32>> {
    items.iter().map(|p| unicode::cps(p)).collect()
}

fn base() -> MemoryHit {
    MemoryHit::new(
        unicode::cps("01J0000000000000000000000A"),
        vec![],
        Confidence::High,
        unicode::cps("s"),
        1.0,
        now(),
        now(),
    )
}

fn keys(v: &PyValue) -> Vec<String> {
    match v {
        PyValue::Dict(items) => items
            .iter()
            .map(|(k, _)| match k {
                PyValue::Str(s) => s.iter().map(|&c| char::from_u32(c).expect("c")).collect(),
                _ => panic!("a str key"),
            })
            .collect(),
        _ => panic!("a dict"),
    }
}

fn verdict(v: &PyValue) -> PyValue {
    v.get("staleness_verdict").expect("verdict").clone()
}

#[test]
fn constant() {
    assert_eq!(SIMILAR_HITS_SHOWN, 5);
}

#[test]
fn prose_missing_only_recommends_even_when_never_verified() {
    let mut h = base();
    h.path_drift_missing_paths = paths(&["a"]);
    let out = ResponseBuilder::new(30).hit_to_dict(&h, now()).expect("ok");
    assert_eq!(verdict(&out), PyValue::str("spot_check_recommended"));
}

#[test]
fn anchored_missing_requires_under_never() {
    let mut h = base();
    h.path_drift_claim_anchored_missing_paths = paths(&["a"]);
    let out = ResponseBuilder::new(30).hit_to_dict(&h, now()).expect("ok");
    assert_eq!(verdict(&out), PyValue::str("spot_check_required"));
    assert!(
        out.get("path_drift").is_none(),
        "anchored alone opens no block"
    );
}

#[test]
fn route_bucket_alone_opens_no_block() {
    let mut h = base();
    h.path_drift_dropped_as_route_paths = paths(&["/x"]);
    let out = ResponseBuilder::new(30).hit_to_dict(&h, now()).expect("ok");
    assert!(out.get("path_drift").is_none());
    assert_eq!(keys(&out).len(), 15);
}

#[test]
fn additive_keys_only_when_set_and_in_order() {
    let mut h = base();
    h.path_drift_verified_paths = paths(&["v"]);
    let out = ResponseBuilder::new(30).hit_to_dict(&h, now()).expect("ok");
    let drift = out.get("path_drift").expect("block");
    assert_eq!(keys(drift), ["checked", "missing", "verified"]);
    h.path_drift_expected_absent_paths = paths(&["e"]);
    h.path_drift_present_but_attested_absent = paths(&["p"]);
    h.path_drift_dropped_as_route_paths = paths(&["/r"]);
    h.path_drift_claim_anchored_missing_paths = paths(&["c"]);
    let out = ResponseBuilder::new(30).hit_to_dict(&h, now()).expect("ok");
    assert_eq!(
        keys(out.get("path_drift").expect("block")),
        [
            "checked",
            "missing",
            "verified",
            "expected_absent",
            "present_but_attested_absent",
            "dropped_as_route",
            "claim_anchored_missing"
        ]
    );
    assert_eq!(*keys(&out).last().expect("last"), "path_drift");
}

#[test]
fn verdict_ignores_the_counts_fields() {
    let mut h = base();
    h.path_drift_missing = 9;
    h.path_drift_checked = 9;
    let out = ResponseBuilder::new(30).hit_to_dict(&h, now()).expect("ok");
    assert_eq!(verdict(&out), PyValue::str("fresh"));
    assert_eq!(out.get("path_drift_missing"), Some(&PyValue::int(9)));
}

#[test]
fn naive_now_against_aware_stamp_raises() {
    let mut h = base();
    h.last_verified_at = Some(stamp("2026-02-01T00:00:00+00:00"));
    let naive = stamp("2026-03-01T12:00:00");
    assert!(ResponseBuilder::new(30).hit_to_dict(&h, naive).is_err());
}

#[test]
fn stamps_spell_z_and_keep_other_offsets() {
    let mut h = base();
    h.created = stamp("2025-01-01T00:00:00+05:30");
    let out = ResponseBuilder::new(30).hit_to_dict(&h, now()).expect("ok");
    assert_eq!(
        out.get("created"),
        Some(&PyValue::str("2025-01-01T00:00:00+05:30"))
    );
    assert_eq!(
        out.get("updated"),
        Some(&PyValue::str("2026-03-01T12:00:00Z"))
    );
}

fn summary(actor: Option<Actor>) -> MemorySummary {
    MemorySummary {
        id: unicode::cps("i"),
        scopes: vec![],
        confidence: Confidence::Low,
        summary: unicode::cps("x"),
        created: now(),
        updated: now(),
        last_verified_at: None,
        category: None,
        actor,
    }
}

#[test]
fn actor_omitted_when_nothing_declared() {
    let b = ResponseBuilder::new(30);
    let out = b.summary_to_dict(&summary(None), now()).expect("ok");
    assert!(out.get("actor").is_none());
    let blank = Actor {
        principal: Some(unicode::cps("p")),
        ..Actor::default()
    };
    let out = b.summary_to_dict(&summary(Some(blank)), now()).expect("ok");
    assert!(out.get("actor").is_none());
    let model_only = Actor {
        model: Some(unicode::cps("")),
        ..Actor::default()
    };
    let out = b
        .summary_to_dict(&summary(Some(model_only)), now())
        .expect("ok");
    assert_eq!(keys(out.get("actor").expect("actor")), ["model"]);
}
