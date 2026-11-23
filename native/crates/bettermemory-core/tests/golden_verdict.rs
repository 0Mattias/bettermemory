//! Every case of `verdict.jsonl` (not gated) against `verdict`, with the
//! commit legs, the anchoring and the path drift read scripted and logged
//! as the generator scripts them (`tests/contract/gen_verdict.py`).

mod common;

use std::cell::RefCell;
use std::rc::Rc;

use bettermemory_core::attestation_paths::{Kind, PathRefs, Record, Targets};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::origin_history::HistoryUnread;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode;
use bettermemory_core::verdict::{
    memoised_verdicts, memory_verdict, resolve_verdicts, CommitLegs, Reads, Resolved, Verdict,
    VerdictAnchoring, VerdictError, VerdictMemory,
};
use bettermemory_core::verify::status::ResolvedCommitDrift;
use bettermemory_core::verify_anchoring::report::PathDriftReport;
use common::parity::Ledger;
use serde_json::{json, Map, Value};

const FILE: &str = "verdict.jsonl";

type Log = Rc<RefCell<Vec<Value>>>;

/// `commit_legs.unchecked_verdict`.
pub fn unchecked_verdict(v: &str) -> String {
    if v == "fresh" {
        "spot_check_recommended".to_string()
    } else {
        v.to_string()
    }
}

fn dt(v: &Value) -> Option<DateTime> {
    v.as_str()
        .map(|t| DateTime::from_isoformat(&unicode::cps(t)).expect("a stamp"))
}

fn strs(v: &Value) -> Vec<Vec<u32>> {
    v.as_array()
        .map(|a| {
            a.iter()
                .map(|s| unicode::cps(s.as_str().unwrap()))
                .collect()
        })
        .unwrap_or_default()
}

fn targets(v: &Value) -> Option<Targets> {
    v.as_object().map(|m| {
        m.iter()
            .map(|(k, t)| (unicode::cps(k), t.as_str().map(unicode::cps)))
            .collect()
    })
}

fn memory(m: &Value) -> VerdictMemory {
    let id = m["id"].as_str().unwrap().to_string();
    VerdictMemory {
        body: unicode::cps(&id),
        last_verified_at: dt(&m["lva"]),
        worktree_root: m["wr"].as_str().map(unicode::cps),
        record: Record {
            verified_paths: strs(&m["vp"]),
            verified_absent_paths: strs(&m["vap"]),
            verified_path_targets: targets(&m["vt"]),
            verified_absent_path_targets: targets(&m["vat"]),
        },
        id,
    }
}

/// The opaque `CommitLegUnchecked`: (why, rule).
type Unchecked = (String, String);

pub struct Legs {
    log: Log,
    spec: Value,
}

impl CommitLegs for Legs {
    type Unchecked = Unchecked;
    fn active(&self) -> bool {
        self.spec["active"].as_bool().unwrap()
    }
    fn prime(&mut self, ids: &[String]) {
        self.log.borrow_mut().push(json!(["prime_legs", ids]));
    }
    fn lookup(
        &mut self,
        memory: &VerdictMemory,
        lva: Option<&DateTime>,
        resolve: bool,
    ) -> (bool, Resolved<Unchecked>) {
        self.log.borrow_mut().push(json!([
            "lookup",
            memory.id,
            lva.map(|d| d.isoformat()),
            resolve
        ]));
        let row = &self.spec["look"][&memory.id];
        let answered = match &row[0] {
            Value::Bool(b) => *b,
            Value::Number(n) => n.as_i64().unwrap() != 0,
            other => panic!("answered {other}"),
        };
        let spec = &row[1];
        let resolved = if spec.is_null() {
            Resolved::None
        } else if let Some(d) = spec.get("d") {
            Resolved::Drift(ResolvedCommitDrift {
                count: d[0].as_i64().unwrap(),
                claims_checked: d[1].as_i64().unwrap(),
                claims_drifted: strs(&d[2]),
                basis: d[3].as_str().unwrap().to_string(),
            })
        } else if let Some(u) = spec.get("u") {
            Resolved::Unread(HistoryUnread::from_value(u.as_str().unwrap()).unwrap())
        } else {
            let c = &spec["c"];
            Resolved::Unchecked((
                c[0].as_str().unwrap().to_string(),
                c[1].as_str().unwrap().to_string(),
            ))
        };
        (answered, resolved)
    }
    fn store(&mut self) {
        self.log.borrow_mut().push(json!(["store"]));
    }
}

pub struct Anch {
    log: Log,
    spec: Value,
}

impl VerdictAnchoring for Anch {
    type Anchor = String;
    fn prime(&mut self, ids: &[String]) {
        self.log.borrow_mut().push(json!(["prime_anch", ids]));
    }
    fn for_record(&mut self, id: &str, origin_root: Option<&[u32]>) -> Option<String> {
        self.log.borrow_mut().push(json!([
            "for_record",
            id,
            origin_root.map(|r| unicode::to_string(r).unwrap())
        ]));
        self.spec[id].as_str().map(str::to_string)
    }
}

fn paths_json(p: &PathRefs<'_>) -> Value {
    let kind = match p.kind {
        Kind::Literal => "LiteralPaths",
        Kind::Reference => "ReferencePaths",
        Kind::Plain => "tuple",
    };
    let items: Vec<String> = p
        .items
        .iter()
        .map(|s| unicode::to_string(s).unwrap())
        .collect();
    json!([kind, items])
}

fn svec(v: &Value) -> Vec<String> {
    v.as_array()
        .map(|a| a.iter().map(|s| s.as_str().unwrap().to_string()).collect())
        .unwrap_or_default()
}

fn js(v: &PyValue) -> Value {
    match v {
        PyValue::None => Value::Null,
        PyValue::Bool(b) => json!(b),
        PyValue::Int(t) => serde_json::from_str(t).unwrap(),
        PyValue::Str(s) => json!(unicode::to_string(s).unwrap()),
        PyValue::List(a) | PyValue::Tuple(a) => Value::Array(a.iter().map(js).collect()),
        PyValue::Dict(d) => Value::Object(
            d.iter()
                .map(|(k, v)| {
                    let PyValue::Str(k) = k else { panic!("a key") };
                    (unicode::to_string(k).unwrap(), js(v))
                })
                .collect(),
        ),
        other => panic!("unexpected {other:?}"),
    }
}

/// The generator's `_short`.
fn short(d: &PyValue) -> Value {
    let mut v = js(d);
    if let Some(Value::String(rec)) = v.get("recommendation").cloned() {
        let cut: String = rec.chars().take(24).collect();
        v["recommendation"] = json!([cut, rec.chars().count()]);
    }
    v
}

fn base(
    id: &str,
    v: &bettermemory_core::verify_status::VerificationStatus,
    pd: &PathDriftReport,
) -> Map<String, Value> {
    let mut m = Map::new();
    m.insert("id".into(), json!(id));
    m.insert("v".into(), short(&v.to_dict()));
    m.insert("pd".into(), json!([pd.checked, pd.missing]));
    m
}

fn out(v: &Verdict<Unchecked>) -> Value {
    match v {
        Verdict::Unresolved(u) => {
            let mut m = base(&u.id, &u.verification, &u.path_drift);
            m.insert("unresolved".into(), json!(true));
            Value::Object(m)
        }
        Verdict::Full(f) => {
            let mut m = base(&f.id, &f.verification, &f.path_drift);
            m.insert(
                "cd".into(),
                f.commit_drift
                    .as_ref()
                    .map_or(Value::Null, |c| short(&c.to_dict())),
            );
            m.insert("sv".into(), json!(f.staleness_verdict));
            m.insert("u".into(), json!(f.commit_drift_unread.map(|u| u.value())));
            m.insert(
                "c".into(),
                f.commit_drift_unchecked
                    .as_ref()
                    .map_or(Value::Null, |(w, r)| json!([w, r])),
            );
            Value::Object(m)
        }
    }
}

/// One case run through the port: the log and the verdicts, or the class
/// raised.
pub fn run(case: &Value) -> Value {
    let log: Log = Rc::new(RefCell::new(Vec::new()));
    let mems: Vec<VerdictMemory> = case["mem"].as_array().unwrap().iter().map(memory).collect();
    let rep = case["rep"].clone();
    let plog = log.clone();
    let mut pd = move |body: &Vec<u32>,
                       verified: &PathRefs<'_>,
                       absent: &PathRefs<'_>,
                       root: Option<&[u32]>,
                       anchor: Option<&String>| {
        let body = unicode::to_string(body).unwrap();
        plog.borrow_mut().push(json!([
            "pd",
            body,
            paths_json(verified),
            paths_json(absent),
            root.map(|r| unicode::to_string(r).unwrap()),
            anchor
        ]));
        let r = &rep[&body];
        PathDriftReport {
            checked: svec(&r["c"]),
            missing: svec(&r["m"]),
            claim_anchored_missing: svec(&r["cam"]),
            present_but_attested_absent: svec(&r["pbaa"]),
            ..Default::default()
        }
    };
    let mut anch = (!case["anch"].is_null()).then(|| Anch {
        log: log.clone(),
        spec: case["anch"].clone(),
    });
    let mut reads = Reads {
        anchoring: anch.as_mut(),
        path_drift: &mut pd,
        unchecked_verdict: &unchecked_verdict,
    };
    let llog = log.clone();
    let spec = case["legs"].clone();
    let make = move || {
        llog.borrow_mut().push(json!(["legs"]));
        Legs { log: llog, spec }
    };
    let now = dt(&case["now"]).unwrap();
    let s = case["s"].as_i64().unwrap();
    let got: Result<Vec<Verdict<Unchecked>>, VerdictError> = match case["f"].as_str().unwrap() {
        "r" => resolve_verdicts(&mems, make, now, s, &mut reads)
            .map(|v| v.into_iter().map(Verdict::Full).collect()),
        "m" => memoised_verdicts(&mems, make, now, s, &mut reads),
        _ => memory_verdict(&mems[0], make, now, s, &mut reads).map(|v| vec![Verdict::Full(v)]),
    };
    let log = Value::Array(log.borrow().clone());
    match got {
        Ok(v) => json!({"log": log, "out": v.iter().map(out).collect::<Vec<_>>()}),
        Err(VerdictError::NaiveAwareMix) => json!({"log": log, "raised": "TypeError"}),
        Err(VerdictError::Unanswered) => json!({"log": log, "raised": "AssertionError"}),
    }
}

/// The expected answer with the `cvs` entries taken out of its log. The
/// port calls `compute_verification_status` itself, so a log of it cannot
/// be scripted here; each entry is checked instead to come first in its
/// row (the log's end, or right before the row's path drift read, its
/// anchor read when anchored) with the case's `now` and `s` and a stamp of
/// one of the case's memories.
fn without_cvs(case: &Value) -> Value {
    let mut o = case["o"].clone();
    let log = o["log"].as_array().unwrap().clone();
    let stamps: Vec<&Value> = case["mem"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| &m["lva"])
        .collect();
    let mut kept = Vec::new();
    for (j, e) in log.iter().enumerate() {
        if e[0] != "cvs" {
            kept.push(e.clone());
            continue;
        }
        assert_eq!(e[2], case["now"], "cvs now");
        assert_eq!(e[3], case["s"], "cvs s");
        let lva = &e[1];
        assert!(
            stamps.iter().any(|s| match (s.as_str(), lva.as_str()) {
                (None, None) => true,
                (Some(a), Some(b)) => dt(&json!(a)) == dt(&json!(b)),
                _ => false,
            }),
            "cvs stamp"
        );
        if let Some(next) = log.get(j + 1) {
            assert!(next[0] == "pd" || next[0] == "for_record", "cvs first");
        } else {
            assert!(o.get("raised").is_some(), "cvs last only when raising");
        }
    }
    o["log"] = Value::Array(kept);
    o
}

#[test]
fn verdict_every_case() {
    let lines = common::jsonl(FILE);
    let header = &lines[0];
    let cases = &lines[1..];
    assert_eq!(header["count"].as_u64().unwrap() as usize, cases.len());
    let mut ledger = Ledger::new("golden_verdict", FILE);
    for (i, case) in cases.iter().enumerate() {
        ledger.case(format!("#{i} {}", case["f"]), || {
            assert_eq!(run(case), without_cvs(case), "case #{i}");
        });
    }
    ledger.finish(cases.len());
}
