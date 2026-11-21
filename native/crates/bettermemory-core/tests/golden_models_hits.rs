//! Every case of `models_hits.jsonl` against `records::summaries`:
//! `MemoryHit` with its defaults, and the two listings' summaries under
//! each scope filter over the header's records.

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::float_repr;
use bettermemory_core::json;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::records::summaries::{
    summaries, tombstone_summaries, MemoryHit, MemorySummary, TombstonedSummary,
};
use bettermemory_core::records::{Actor, Category, Confidence, Memory, Tombstone};
use bettermemory_core::unicode;
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

fn t(s: &[u32]) -> Value {
    json!(unicode::to_string(s).expect("text"))
}

fn ts(v: &[PyStr]) -> Value {
    json!(v.iter().map(|s| t(s)).collect::<Vec<_>>())
}

fn dt(d: &DateTime) -> Value {
    json!(d.isoformat())
}

fn odt(d: &Option<DateTime>) -> Value {
    d.as_ref().map_or(Value::Null, dt)
}

fn cat(c: &Option<Category>) -> Value {
    c.map_or(Value::Null, |c| json!(c.value()))
}

fn float(f: f64) -> Value {
    json!({"t": "float", "bits": format!("{:016x}", f.to_bits()), "repr": float_repr::repr(f)})
}

fn ot(v: &Option<PyStr>) -> Value {
    v.as_ref().map_or(Value::Null, |s| t(s))
}

fn actor(a: &Option<Actor>) -> Value {
    let Some(a) = a else { return Value::Null };
    json!([
        ot(&a.client),
        ot(&a.client_version),
        ot(&a.model),
        ot(&a.principal),
        ot(&a.session),
        a.sources
            .iter()
            .map(|(k, v)| json!([t(k), t(v)]))
            .collect::<Vec<_>>()
    ])
}

fn summary(s: &MemorySummary) -> Value {
    json!([
        ["id", t(&s.id)],
        ["scopes", ts(&s.scopes)],
        ["confidence", s.confidence.value()],
        ["summary", t(&s.summary)],
        ["created", dt(&s.created)],
        ["updated", dt(&s.updated)],
        ["last_verified_at", odt(&s.last_verified_at)],
        ["category", cat(&s.category)],
        ["actor", actor(&s.actor)],
    ])
}

fn tomb(s: &TombstonedSummary) -> Value {
    json!([
        ["id", t(&s.id)],
        ["scopes", ts(&s.scopes)],
        ["confidence", s.confidence.value()],
        ["summary", t(&s.summary)],
        ["created", dt(&s.created)],
        ["updated", dt(&s.updated)],
        ["last_verified_at", odt(&s.last_verified_at)],
        ["category", cat(&s.category)],
        ["removed", dt(&s.removed)],
        ["removed_reason", t(&s.removed_reason)],
        ["removed_session", ot(&s.removed_session)],
    ])
}

fn hit(h: &MemoryHit) -> Value {
    json!([
        ["id", t(&h.id)],
        ["scopes", ts(&h.scopes)],
        ["confidence", h.confidence.value()],
        ["snippet", t(&h.snippet)],
        ["score", float(h.score)],
        ["relevance", t(&h.relevance)],
        ["match_terms", ts(&h.match_terms)],
        ["created", dt(&h.created)],
        ["updated", dt(&h.updated)],
        ["last_verified_at", odt(&h.last_verified_at)],
        ["path_drift_checked", h.path_drift_checked],
        ["path_drift_missing", h.path_drift_missing],
        ["path_drift_checked_paths", ts(&h.path_drift_checked_paths)],
        ["path_drift_missing_paths", ts(&h.path_drift_missing_paths)],
        [
            "path_drift_verified_paths",
            ts(&h.path_drift_verified_paths)
        ],
        [
            "path_drift_expected_absent_paths",
            ts(&h.path_drift_expected_absent_paths)
        ],
        [
            "path_drift_present_but_attested_absent",
            ts(&h.path_drift_present_but_attested_absent)
        ],
        [
            "path_drift_dropped_as_route_paths",
            ts(&h.path_drift_dropped_as_route_paths)
        ],
        [
            "path_drift_claim_anchored_missing_paths",
            ts(&h.path_drift_claim_anchored_missing_paths)
        ],
        ["category", cat(&h.category)],
        ["query_unique", h.query_unique],
    ])
}

fn payload(v: &Value) -> bettermemory_core::pyvalue::PyValue {
    json::loads(&unicode::cps(&v.to_string())).expect("payload")
}

fn cps(v: &Value) -> PyStr {
    unicode::cps(v.as_str().expect("text"))
}

fn confidence(v: &Value) -> Confidence {
    match v.as_str().expect("confidence") {
        "high" => Confidence::High,
        "medium" => Confidence::Medium,
        "low" => Confidence::Low,
        other => panic!("confidence {other}"),
    }
}

fn when(v: &Value) -> DateTime {
    DateTime::from_isoformat(&cps(v)).expect("isoformat")
}

#[test]
fn models_hits_every_case() {
    let name = "models_hits.jsonl";
    let lines = jsonl(name);
    let header = &lines[0];
    let memories: Vec<Memory> = header["memories"]
        .as_array()
        .expect("memories")
        .iter()
        .map(|v| Memory::from_payload(&payload(v)).expect("a memory"))
        .collect();
    let tombstones: Vec<Tombstone> = header["tombstones"]
        .as_array()
        .expect("tombstones")
        .iter()
        .map(|v| Tombstone::from_payload(&payload(v)).expect("a tombstone"))
        .collect();
    let in_file = header["cases"].as_u64().expect("cases") as usize;
    assert_eq!(in_file, lines.len() - 1);
    let mut ledger = Ledger::new("models_hits_every_case", name);
    for (i, case) in lines[1..].iter().enumerate() {
        ledger.case(format!("case {i} ({})", case["k"]), || {
            let scopes: Option<Vec<PyStr>> =
                case["s"].as_array().map(|a| a.iter().map(cps).collect());
            let r = match case["k"].as_str().expect("k") {
                "ls" => json!(summaries(&memories, scopes.as_deref())
                    .iter()
                    .map(summary)
                    .collect::<Vec<_>>()),
                "lt" => json!(tombstone_summaries(&tombstones, scopes.as_deref())
                    .iter()
                    .map(tomb)
                    .collect::<Vec<_>>()),
                "hit" => {
                    let a = &case["in"];
                    let score = match common::neutral(&a[4]) {
                        bettermemory_core::pyvalue::PyValue::Float(f) => f,
                        other => panic!("score {other:?}"),
                    };
                    hit(&MemoryHit::new(
                        cps(&a[0]),
                        a[1].as_array().expect("scopes").iter().map(cps).collect(),
                        confidence(&a[2]),
                        cps(&a[3]),
                        score,
                        when(&a[5]),
                        when(&a[6]),
                    ))
                }
                other => panic!("unknown kind {other}"),
            };
            let mut got = case.clone();
            got["r"] = r;
            assert_eq!(got, *case, "{name} case {i}");
        });
    }
    ledger.finish(in_file);
}
