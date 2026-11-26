//! What the search hit tests share: the stand-ins of `gen_search_hit.py`
//! (a `detect_path_drift` that logs its call and answers a scripted
//! report, an anchor with only a root and a rule, an anchoring that logs
//! `prime` and `for_record`) and the hit spelled as the generator spells
//! it.

use bettermemory_core::attestation_paths::{Kind, Paths};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::float_repr;
use bettermemory_core::json;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::records::summaries::MemoryHit;
use bettermemory_core::records::{Category, Memory};
use bettermemory_core::search::hit::{Drift, RecordAnchors};
use bettermemory_core::unicode::{self, cps};
use bettermemory_core::verify_anchoring::anchor::ClaimAnchor;
use bettermemory_core::verify_anchoring::detect::DriftError;
use bettermemory_core::verify_anchoring::passes::ReadAnchor;
use bettermemory_core::verify_anchoring::report::PathDriftReport;
use serde_json::{json, Value};

pub fn t(s: &[u32]) -> Value {
    json!(unicode::to_string(s).expect("text"))
}

fn ts(v: &[PyStr]) -> Value {
    json!(v.iter().map(|s| t(s)).collect::<Vec<_>>())
}

fn ot(v: &Option<PyStr>) -> Value {
    v.as_ref().map_or(Value::Null, |s| t(s))
}

fn dt(d: &DateTime) -> Value {
    json!(d.isoformat())
}

fn cat(c: &Option<Category>) -> Value {
    c.map_or(Value::Null, |c| json!(c.value()))
}

/// `_common.neutral` of a float.
pub fn float(f: f64) -> Value {
    json!({"t": "float", "bits": format!("{:016x}", f.to_bits()), "repr": float_repr::repr(f)})
}

/// The float `_common.neutral` spelled.
pub fn float_of(v: &Value) -> f64 {
    f64::from_bits(u64::from_str_radix(v["bits"].as_str().expect("bits"), 16).expect("hex"))
}

/// The hit by field in the model's order (`gen_models_hits._spell`).
pub fn hit(h: &MemoryHit) -> Value {
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
        [
            "last_verified_at",
            h.last_verified_at.as_ref().map_or(Value::Null, dt)
        ],
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

/// A memory of the header (its `model_dump(mode="json")`).
pub fn memory(v: &Value) -> Memory {
    let payload = json::loads(&cps(&v.to_string())).expect("payload");
    Memory::from_payload(&payload).expect("valid")
}

fn strs(v: &Value) -> Vec<String> {
    v.as_array()
        .map(|a| {
            a.iter()
                .map(|s| s.as_str().expect("text").to_string())
                .collect()
        })
        .unwrap_or_default()
}

/// `PathDriftReport(**rep)`.
pub fn report(rep: &Value) -> PathDriftReport {
    PathDriftReport {
        checked: strs(&rep["checked"]),
        missing: strs(&rep["missing"]),
        verified: strs(&rep["verified"]),
        expected_absent: strs(&rep["expected_absent"]),
        dropped_as_route: strs(&rep["dropped_as_route"]),
        claim_anchored_missing: strs(&rep["claim_anchored_missing"]),
        unconfirmed: strs(&rep["unconfirmed"]),
        unconfirmed_reasons: strs(&rep["unconfirmed_reasons"]),
        present_but_attested_absent: strs(&rep["present_but_attested_absent"]),
        anchor: rep["anchor"].as_str().map(str::to_string),
    }
}

/// A `ClaimAnchor` with a root and a rule, as the drift read sees it.
pub struct Anchor {
    pub root: Option<PyStr>,
    pub rule: String,
}

impl Anchor {
    /// `[root, rule]`, or None for null.
    pub fn of(v: &Value) -> Option<Anchor> {
        let a = v.as_array()?;
        Some(Anchor {
            root: a[0].as_str().map(cps),
            rule: a[1].as_str().unwrap_or("").to_string(),
        })
    }
}

impl ReadAnchor for Anchor {
    fn root(&self) -> Option<PyStr> {
        self.root.clone()
    }
    fn rule(&self) -> String {
        self.rule.clone()
    }
    fn convicting(&self) -> bool {
        false
    }
    fn absolute(&self, path: &[u32]) -> (Option<PyStr>, String) {
        (Some(path.to_vec()), "absolute".to_string())
    }
}

fn kind(p: &Paths) -> &'static str {
    match p.kind {
        Kind::Reference => "ReferencePaths",
        Kind::Literal => "LiteralPaths",
        Kind::Plain => "tuple",
    }
}

/// The stand-in `detect_path_drift`: each call logged, the report
/// answered.
pub struct Logged {
    pub report: PathDriftReport,
    pub log: Vec<Value>,
}

impl Drift for Logged {
    fn detect(
        &mut self,
        body: &[u32],
        verified_paths: &Paths,
        absent_paths: &Paths,
        worktree_root: Option<&[u32]>,
        anchor: Option<&dyn ReadAnchor>,
    ) -> Result<PathDriftReport, DriftError> {
        self.log.push(json!([
            t(body),
            kind(verified_paths),
            ts(&verified_paths.items),
            kind(absent_paths),
            ts(&absent_paths.items),
            worktree_root.map_or(Value::Null, t),
            anchor.map_or(Value::Null, |a| json!([ot(&a.root()), a.rule()])),
        ]));
        Ok(self.report.clone())
    }
}

/// An anchor's `[root, rule]`, or null.
pub fn anchor_desc(a: &Option<ClaimAnchor>) -> Value {
    a.as_ref().map_or(Value::Null, |a| json!([a.root, a.rule]))
}

/// The stand-in anchoring: `prime` and `for_record` logged, each id's
/// anchor from the table.
pub struct Table {
    pub table: Value,
    pub log: Vec<Value>,
}

impl RecordAnchors for Table {
    fn prime(&mut self, record_ids: &[String]) {
        self.log.push(json!(["prime", record_ids]));
    }
    fn for_record(&mut self, record_id: &str, origin_root: Option<&[u32]>) -> Option<ClaimAnchor> {
        self.log.push(json!([
            "for_record",
            record_id,
            origin_root.map_or(Value::Null, t)
        ]));
        let entry = self.table.get(record_id)?.as_array()?;
        Some(ClaimAnchor::new(
            entry[0].as_str().map(str::to_string),
            entry[1].as_str().unwrap_or(""),
        ))
    }
}
