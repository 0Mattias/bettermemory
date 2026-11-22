//! Every case of `response_hits.jsonl` (not gated) against the native
//! `response_rows`. The spelling is the generator's
//! (`tests/contract/gen_response_hits.py`).

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::json::{dumps, DumpOptions};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::records::summaries::{MemoryHit, MemorySummary, TombstonedSummary};
use bettermemory_core::records::{Actor, Category, Confidence};
use bettermemory_core::response_rows::{ResponseBuilder, SIMILAR_HITS_SHOWN};
use bettermemory_core::unicode;
use common::parity::Ledger;
use serde_json::{json, Value};

const FILE: &str = "response_hits.jsonl";

fn dt(v: &Value) -> DateTime {
    DateTime::from_isoformat(&unicode::cps(v.as_str().expect("a datetime"))).expect("isoformat")
}

fn odt(v: &Value) -> Option<DateTime> {
    (!v.is_null()).then(|| dt(v))
}

fn s(v: &Value) -> PyStr {
    unicode::cps(v.as_str().expect("a string"))
}

fn os(v: &Value) -> Option<PyStr> {
    (!v.is_null()).then(|| s(v))
}

fn list(v: &Value) -> Vec<PyStr> {
    v.as_array().expect("a list").iter().map(s).collect()
}

fn member<T: Copy>(members: &[(&str, T)], v: &Value) -> T {
    let name = v.as_str().expect("an enum value");
    members
        .iter()
        .find(|(value, _)| *value == name)
        .map(|(_, m)| *m)
        .expect("a member")
}

fn category(v: &Value) -> Option<Category> {
    (!v.is_null()).then(|| member(Category::MEMBERS, v))
}

fn hit(i: &Value) -> MemoryHit {
    let bits = u64::from_str_radix(i["score"].as_str().expect("bits"), 16).expect("hex");
    let mut h = MemoryHit::new(
        s(&i["id"]),
        list(&i["scopes"]),
        member(Confidence::MEMBERS, &i["confidence"]),
        common::text_field(i, "snippet"),
        f64::from_bits(bits),
        dt(&i["created"]),
        dt(&i["updated"]),
    );
    h.relevance = s(&i["relevance"]);
    h.match_terms = list(&i["match_terms"]);
    h.last_verified_at = odt(&i["last_verified_at"]);
    h.path_drift_checked = i["path_drift_checked"].as_i64().expect("an int");
    h.path_drift_missing = i["path_drift_missing"].as_i64().expect("an int");
    h.path_drift_checked_paths = list(&i["path_drift_checked_paths"]);
    h.path_drift_missing_paths = list(&i["path_drift_missing_paths"]);
    h.path_drift_verified_paths = list(&i["path_drift_verified_paths"]);
    h.path_drift_expected_absent_paths = list(&i["path_drift_expected_absent_paths"]);
    h.path_drift_present_but_attested_absent = list(&i["path_drift_present_but_attested_absent"]);
    h.path_drift_dropped_as_route_paths = list(&i["path_drift_dropped_as_route_paths"]);
    h.path_drift_claim_anchored_missing_paths = list(&i["path_drift_claim_anchored_missing_paths"]);
    h.category = category(&i["category"]);
    h
}

fn summary(i: &Value) -> MemorySummary {
    MemorySummary {
        id: s(&i["id"]),
        scopes: list(&i["scopes"]),
        confidence: member(Confidence::MEMBERS, &i["confidence"]),
        summary: common::text_field(i, "summary"),
        created: dt(&i["created"]),
        updated: dt(&i["updated"]),
        last_verified_at: odt(&i["last_verified_at"]),
        category: category(&i["category"]),
        actor: (!i["actor"].is_null()).then(|| Actor {
            client: os(&i["actor"]["client"]),
            model: os(&i["actor"]["model"]),
            ..Actor::default()
        }),
    }
}

fn tomb(i: &Value) -> TombstonedSummary {
    TombstonedSummary {
        id: s(&i["id"]),
        scopes: list(&i["scopes"]),
        confidence: member(Confidence::MEMBERS, &i["confidence"]),
        summary: common::text_field(i, "summary"),
        created: dt(&i["created"]),
        updated: dt(&i["updated"]),
        last_verified_at: odt(&i["last_verified_at"]),
        category: category(&i["category"]),
        removed: dt(&i["removed"]),
        removed_reason: s(&i["removed_reason"]),
        removed_session: os(&i["removed_session"]),
    }
}

fn text(value: &PyValue) -> String {
    let out = dumps(value, DumpOptions::DEFAULT).expect("dumps");
    String::from_utf8(
        out.iter()
            .map(|&c| u8::try_from(c).expect("ascii"))
            .collect(),
    )
    .expect("ascii")
}

#[test]
fn golden_response_hits() {
    let lines = common::jsonl(FILE);
    let header = &lines[0];
    assert_eq!(header["k"], "header");
    assert_eq!(header["shown"], json!(SIMILAR_HITS_SHOWN));
    let count = header["count"].as_u64().expect("a count") as usize;
    assert!(count > 0);
    let mut ledger = Ledger::new("golden_response_hits", FILE);
    for (n, case) in lines[1..].iter().enumerate() {
        let b = ResponseBuilder::new(case["s"].as_i64().expect("an i64"));
        let i = &case["i"];
        let got = match case["k"].as_str().expect("a kind") {
            "hit" => b.hit_to_dict(&hit(i), dt(&case["now"])),
            "summary" => b.summary_to_dict(&summary(i), dt(&case["now"])),
            "tomb" => Ok(b.tombstone_summary_to_dict(&tomb(i))),
            other => panic!("unknown kind {other}"),
        };
        let got = match got {
            Ok(v) => json!({ "o": text(&v) }),
            Err(_) => json!({ "e": "TypeError" }),
        };
        let want = match case.get("o") {
            Some(o) => json!({ "o": o }),
            None => json!({ "e": case["e"] }),
        };
        let label = format!("line {}", n + 2);
        assert!(
            !ledger.differs_fatally(&label, got == want),
            "{label}: got {got}, want {want}"
        );
    }
    assert_eq!(lines.len() - 1, count);
    ledger.finish(count);
}
