//! Cases a plausible wrong port of `audit::probe` fails: the inclusive
//! bounds of both windows, which session field each reader takes first,
//! the cohort read on the top hit only, the empty ids left out of the
//! shield's set, the one prime before any ask, the order of the shield
//! overflow and the mode check, a naive `now` (coerced by the probe,
//! refused by the dedup only where it compares), and the v2 label's arms.

mod common;

use bettermemory_core::audit::probe::{
    count_recent_retrievals, is_duplicate_audit, probe_for_miss, too_thin_to_probe, ProbeAdmission,
    ProbeArgs, ProbeError, ProbeHit,
};
use bettermemory_core::audit::report::Verdict;
use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::Memory;
use bettermemory_core::search::lane::relevance_label_v2;
use bettermemory_core::unicode::cps;
use common::audit_probe::{plain, FakeAdmission, FakeRanker};
use serde_json::json;

fn now() -> DateTime {
    DateTime::from_isoformat(&cps("2026-03-01T12:00:00+00:00")).unwrap()
}

fn ev(v: serde_json::Value) -> PyValue {
    plain(&v)
}

fn memory(created: &str) -> Memory {
    Memory::from_payload(&plain(&json!({
        "id": "01J0000000000000000000000A", "scopes": ["global"], "body": "b",
        "confidence": "high", "source": "explicit-statement",
        "created": created, "updated": created,
    })))
    .unwrap()
}

fn hit(id: &str, rel: &str, scopes: &[&str]) -> ProbeHit {
    ProbeHit {
        id: cps(id),
        score: 1.0,
        relevance: cps(rel),
        scopes: scopes.iter().map(|s| cps(s)).collect(),
        snippet: cps("x"),
        matched_unique: 2,
        query_unique: 2,
    }
}

fn ranker(hits: Vec<ProbeHit>) -> FakeRanker {
    FakeRanker {
        hits,
        calls: Vec::new(),
    }
}

const MSG: &str = "deploy the backup server";

#[test]
fn the_creation_shield_keeps_a_memory_created_at_its_bound() {
    let mems = [
        memory("2026-03-01T11:59:00Z"),
        memory("2026-03-01T11:59:00.000001Z"),
    ];
    let mut r = ranker(vec![]);
    let args = ProbeArgs::new(&cps("S"), now());
    probe_for_miss(&mems, &cps(MSG), &[], None, &mut r, &args).unwrap();
    assert_eq!(r.calls[0]["pool"].as_array().unwrap().len(), 1);
}

#[test]
fn the_retrieval_window_counts_an_event_at_its_bound() {
    let e = [ev(
        json!({"kind": "search", "ts": "2026-03-01T11:59:00Z", "session": "S"}),
    )];
    assert_eq!(
        count_recent_retrievals(&e, &[cps("S")], &now(), 60, None, None),
        Ok(1)
    );
    assert_eq!(
        count_recent_retrievals(&e, &[cps("S")], &now(), 59, None, None),
        Ok(0)
    );
}

#[test]
fn each_reader_takes_its_own_session_field_first() {
    let e = [ev(
        json!({"kind": "turn_audited", "ts": "2026-03-01T11:59:00Z",
                        "session_id": "A", "session": "B", "probe_query": "q"}),
    )];
    let dup = |sid: &str| is_duplicate_audit(&e, &cps(sid), None, &cps("q"), &now(), 3600);
    assert_eq!(dup("A"), Ok(true));
    assert_eq!(dup("B"), Ok(false));
    let r = [ev(json!({"kind": "search", "ts": "2026-03-01T11:59:30Z",
                        "session_id": "A", "session": "B"}))];
    assert_eq!(
        count_recent_retrievals(&r, &[cps("B")], &now(), 60, None, None),
        Ok(1)
    );
    assert_eq!(
        count_recent_retrievals(&r, &[cps("A")], &now(), 60, None, None),
        Ok(0)
    );
}

#[test]
fn the_dedup_stops_at_the_first_old_event_of_the_session() {
    let e = [
        ev(
            json!({"kind": "turn_audited", "ts": "2026-03-01T11:59:00Z", "session_id": "S", "probe_query": "q"}),
        ),
        ev(json!({"kind": "turn_audited", "ts": "2026-03-01T09:00:00Z", "session_id": "S"})),
    ];
    assert_eq!(
        is_duplicate_audit(&e, &cps("S"), None, &cps("q"), &now(), 3600),
        Ok(false)
    );
}

#[test]
fn only_the_top_hit_can_explain_a_miss_away() {
    let mems = [memory("2026-01-01T00:00:00Z")];
    let mut adm = FakeAdmission {
        same: vec![cps("b")],
        ..Default::default()
    };
    let mut r = ranker(vec![
        hit("a", "high", &["global"]),
        hit("b", "high", &["projects:x"]),
    ]);
    let args = ProbeArgs::new(&cps("S"), now());
    let rep = probe_for_miss(&mems, &cps(MSG), &[], Some(&mut adm), &mut r, &args).unwrap();
    assert_eq!(rep.verdict, Verdict::Miss);
    assert!(adm.asked.is_empty());
}

#[test]
fn empty_session_ids_never_shield() {
    let mems = [memory("2026-01-01T00:00:00Z")];
    let e = [ev(
        json!({"kind": "search", "ts": "2026-03-01T11:59:30Z", "session": ""}),
    )];
    let mut r = ranker(vec![hit("a", "high", &[])]);
    let mut args = ProbeArgs::new(&cps(""), now());
    args.retrieval_session_id = Some(cps(""));
    let rep = probe_for_miss(&mems, &cps(MSG), &e, None, &mut r, &args).unwrap();
    assert_eq!(rep.verdict, Verdict::Miss);
    // An event whose `session or session_id` comes out as "" stays out of the set.
    let e = [ev(json!({"kind": "search", "ts": "2026-03-01T11:59:30Z",
                        "session": "", "session_id": ""}))];
    let mut r = ranker(vec![hit("a", "high", &[])]);
    let rep = probe_for_miss(&mems, &cps(MSG), &e, None, &mut r, &args).unwrap();
    assert_eq!(rep.verdict, Verdict::Miss);
    assert_eq!(rep.recent_retrieval_count, 0);
    assert_eq!(rep.suppressed_by, None);
}

#[test]
fn a_dict_probe_query_never_matches_the_text_when_no_hash_is_given() {
    let e = [ev(
        json!({"kind": "turn_audited", "ts": "2026-03-01T12:00:00Z",
                        "session_id": "S", "probe_query": {"hash": "q"}}),
    )];
    assert_eq!(
        is_duplicate_audit(&e, &cps("S"), None, &cps("q"), &now(), 3600),
        Ok(false)
    );
    assert_eq!(
        is_duplicate_audit(&e, &cps("S"), Some(&cps("q")), &cps("x"), &now(), 3600),
        Ok(true)
    );
}

#[test]
fn the_admission_is_primed_once_with_every_window_id_before_any_ask() {
    struct Log(Vec<String>);
    impl ProbeAdmission for Log {
        fn prime(&mut self, ids: &[Vec<u32>]) {
            self.0.push(format!("prime {}", ids.len()));
        }
        fn same_project(&mut self, _: &[u32]) -> bool {
            self.0.push("ask".into());
            false
        }
    }
    let e = [
        ev(json!({"kind": "search", "ts": "2026-03-01T11:59:30Z", "returned": ["a", "b"]})),
        ev(json!({"kind": "show", "ts": "2026-03-01T11:59:30Z", "id": "c"})),
    ];
    let mut log = Log(Vec::new());
    count_recent_retrievals(&e, &[], &now(), 60, Some(&mut log), None).unwrap();
    assert_eq!(log.0, ["prime 3", "ask", "ask", "ask"]);
}

#[test]
fn the_shield_overflow_comes_before_the_mode_check() {
    let mems = [memory("0001-01-01T00:00:00Z")];
    let early = DateTime::from_isoformat(&cps("0001-01-01T00:00:30+00:00")).unwrap();
    let mut args = ProbeArgs::new(&cps("S"), early);
    args.mode = cps("nope");
    let got = probe_for_miss(&mems, &cps(MSG), &[], None, &mut ranker(vec![]), &args);
    assert_eq!(got, Err(ProbeError::OverflowError));
    args.creation_shield_seconds = 0;
    let got = probe_for_miss(&mems, &cps(MSG), &[], None, &mut ranker(vec![]), &args);
    assert!(matches!(got, Err(ProbeError::ValueError(_))));
}

#[test]
fn a_naive_now_is_coerced_by_the_probe_and_refused_by_the_dedup_where_it_compares() {
    let naive = DateTime::from_isoformat(&cps("2026-03-01T12:00:00")).unwrap();
    let mems = [memory("2026-01-01T00:00:00Z")];
    let args = ProbeArgs::new(&cps("S"), naive);
    let rep = probe_for_miss(
        &mems,
        &cps(MSG),
        &[],
        None,
        &mut ranker(vec![hit("a", "high", &[])]),
        &args,
    )
    .unwrap();
    assert_eq!(rep.checked_at.offset_us, Some(0));
    let other = [ev(
        json!({"kind": "search", "ts": "2026-03-01T11:59:00Z", "session_id": "S"}),
    )];
    assert_eq!(
        is_duplicate_audit(&other, &cps("S"), None, &cps("q"), &naive, 3600),
        Ok(false)
    );
    let same = [ev(
        json!({"kind": "turn_audited", "ts": "2026-03-01T11:59:00Z", "session_id": "S"}),
    )];
    assert_eq!(
        is_duplicate_audit(&same, &cps("S"), None, &cps("q"), &naive, 3600),
        Err(ProbeError::TypeError)
    );
}

#[test]
fn the_v2_label_arms() {
    assert_eq!(relevance_label_v2(4, 1000), "high");
    assert_eq!(relevance_label_v2(3, 1000), "low");
    assert_eq!(relevance_label_v2(3, 4), "high");
    assert_eq!(relevance_label_v2(2, 5), "medium");
    assert_eq!(relevance_label_v2(1, 3), "low");
    assert_eq!(relevance_label_v2(9, 0), "low");
    assert_eq!(relevance_label_v2(9, -1), "low");
}

#[test]
fn the_thin_gate_drops_digits_and_reads_the_ack_set_unstemmed() {
    assert!(too_thin_to_probe(&cps("deploy 42")));
    assert!(too_thin_to_probe(&cps("sounds works")));
    assert!(!too_thin_to_probe(&cps("sound work")));
    assert!(!too_thin_to_probe(&cps("all done deploy")));
}

#[test]
fn an_unhashable_session_raises_only_inside_the_window() {
    let inside = [ev(
        json!({"kind": "search", "ts": "2026-03-01T11:59:30Z", "session": ["S"]}),
    )];
    let outside = [ev(
        json!({"kind": "search", "ts": "2026-03-01T10:00:00Z", "session": ["S"]}),
    )];
    assert_eq!(
        count_recent_retrievals(&inside, &[], &now(), 60, None, None),
        Err(ProbeError::TypeError)
    );
    assert_eq!(
        count_recent_retrievals(&outside, &[], &now(), 60, None, None),
        Ok(0)
    );
}
