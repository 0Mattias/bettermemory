//! Every case of `verify_basis.jsonl` (not gated) against the native
//! `verify_basis` and `verify::status`. The spelling is the generator's
//! (`tests/contract/gen_verify_basis.py`).

mod common;

use std::collections::{BTreeMap, HashMap, HashSet};

use bettermemory_core::claim_drift::{Entry, Index};
use bettermemory_core::claims::{Claim, Kind};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::origin_history::{HistoryUnread, ReachableWalk, Unwalked};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;
use bettermemory_core::verify::status::{
    claim_drift_recommendation, commit_drift_status, drift_recommendation, gated_claims,
    index_paths, staleness_verdict_from_report, unreadable, CommitDriftStatus, ResolvedCommitDrift,
};
use bettermemory_core::verify_anchoring::report::PathDriftReport;
use bettermemory_core::verify_basis::{
    count_since_verify, date_count_uncertainty, dates_since, unread_recommendation, AnchorWalk,
    SinceVerify, BASIS_AUTHOR_DATE, BASIS_REACHABILITY, UNREAD_RECOMMENDATIONS,
};
use bettermemory_core::verify_status::{Status, VerificationStatus};
use common::parity::Ledger;
use serde_json::{json, Value};

const FILE: &str = "verify_basis.jsonl";

fn tf(v: &Value) -> PyStr {
    common::text_field(v, "v")
}

fn spell(text: &[u32]) -> Value {
    match char_text(text) {
        Some(s) => json!({ "v": s }),
        None => json!({ "v_cps": text }),
    }
}

fn char_text(text: &[u32]) -> Option<String> {
    text.iter().map(|&c| char::from_u32(c)).collect()
}

fn texts(v: &Value) -> Vec<PyStr> {
    v.as_array().expect("texts").iter().map(tf).collect()
}

fn dt(v: &Value) -> DateTime {
    DateTime::from_isoformat(&cps(v.as_str().expect("an instant"))).expect("a datetime")
}

fn why(v: &Value) -> HistoryUnread {
    HistoryUnread::from_value(v.as_str().expect("a value")).expect("a member")
}

fn history(v: &Value) -> Option<Result<Vec<DateTime>, HistoryUnread>> {
    match v {
        Value::Null => None,
        Value::String(_) => Some(Err(why(v))),
        _ => Some(Ok(v.as_array().expect("dates").iter().map(dt).collect())),
    }
}

/// A `PyValue` as the generator's `_spelled_dict` writes it.
fn spelled(v: &PyValue) -> Value {
    match v {
        PyValue::None => Value::Null,
        PyValue::Int(t) => serde_json::from_str(t).expect("an int"),
        PyValue::Str(s) => spell(s),
        PyValue::List(items) => Value::Array(items.iter().map(spelled).collect()),
        PyValue::Dict(pairs) => Value::Array(
            pairs
                .iter()
                .map(|(k, v)| {
                    let PyValue::Str(k) = k else { panic!("a key") };
                    json!([char_text(k).expect("a key"), spelled(v)])
                })
                .collect(),
        ),
        other => panic!("unexpected {other:?}"),
    }
}

struct Scripted<'a> {
    case: &'a Value,
    calls: Vec<Value>,
}

fn text(v: &[u32]) -> String {
    char_text(v).expect("text")
}

impl AnchorWalk for Scripted<'_> {
    fn toplevel_and_head(&mut self, cwd: &[u32]) -> Option<(PyStr, PyStr)> {
        self.calls.push(json!(["loc", text(cwd)]));
        let loc = self.case["loc"].as_array()?;
        Some((
            cps(loc[0].as_str().expect("top")),
            cps(loc[1].as_str().expect("head")),
        ))
    }

    fn commits_since_anchor(
        &mut self,
        cwd: &[u32],
        verified_head: &[u32],
        toplevel: &[u32],
        head: &[u32],
        unwalked: &mut Unwalked,
    ) -> Option<ReachableWalk> {
        self.calls.push(json!([
            "walk",
            text(cwd),
            text(verified_head),
            text(toplevel),
            text(head)
        ]));
        if !self.case["give"].is_null() {
            unwalked.note(why(&self.case["give"]));
        }
        let commits = self.case["walk"].as_array()?;
        Some(ReachableWalk {
            anchor: verified_head.to_vec(),
            head: head.to_vec(),
            commits: commits
                .iter()
                .map(|c| cps(c.as_str().expect("a sha")))
                .collect(),
            touched: BTreeMap::new(),
            listed: true,
            timed_out: false,
        })
    }
}

fn count_case(case: &Value) -> bool {
    let mut origin = Scripted {
        case,
        calls: Vec::new(),
    };
    let mut unwalked = Unwalked::new();
    for _ in 0..case["pre"].as_u64().expect("pre") {
        unwalked.note(HistoryUnread::Error);
    }
    let vh = case["vh"].as_str().map(cps);
    let mut dates_calls = Vec::new();
    let got = count_since_verify(
        &mut origin,
        &mut unwalked,
        &cps("w/cwd"),
        &dt(&case["s"]),
        vh.as_deref(),
        || {
            dates_calls.push(json!(["dates", "w/cwd"]));
            history(&case["h"])
        },
    );
    let o = match got {
        Err(_) => json!("TypeError"),
        Ok(None) => Value::Null,
        Ok(Some(Err(w))) => json!(w.value()),
        Ok(Some(Ok(SinceVerify {
            count,
            basis,
            walk,
            toplevel,
        }))) => json!([
            count,
            basis,
            walk.map(|w| w.commits.iter().map(|c| text(c)).collect::<Vec<_>>()),
            toplevel.map(|t| text(&t))
        ]),
    };
    let mut calls = origin.calls;
    calls.extend(dates_calls);
    o == case["o"] && Value::Array(calls) == case["calls"]
}

fn resolved(r: &Value) -> ResolvedCommitDrift {
    ResolvedCommitDrift {
        count: r[0].as_i64().expect("count"),
        claims_checked: r[1].as_i64().expect("checked"),
        claims_drifted: texts(&r[2]),
        basis: r[3].as_str().expect("basis").to_string(),
    }
}

fn index_of(spec: &Value) -> Index {
    let list = |k: &str| spec.get(k).map(texts).unwrap_or_default();
    let mut index = Index {
        files: list("files").into_iter().collect(),
        deleted: list("deleted").into_iter().collect(),
        ..Index::default()
    };
    for p in list("changed_text") {
        index.changed_text.insert(p, HashMap::new());
    }
    for p in list("changed_fragments") {
        index.changed_fragments.insert(p, HashMap::new());
    }
    for p in list("header_only") {
        index.header_only.insert(p, HashSet::new());
    }
    for (i, p) in list("bindings").into_iter().enumerate() {
        index
            .bindings
            .insert((p, "symbol", cps(&format!("n{i}"))), Entry::default());
    }
    index
}

fn claims(v: &Value) -> Vec<Claim> {
    texts(v)
        .into_iter()
        .map(|p| Claim {
            kind: Kind::Path,
            rel_path: p,
            name: Vec::new(),
            value: Vec::new(),
        })
        .collect()
}

fn status(name: &str) -> Status {
    match name {
        "never" => Status::Never,
        "stale" => Status::Stale,
        "fresh" => Status::Fresh,
        "remote" => Status::Remote,
        other => panic!("status {other}"),
    }
}

fn names(n: &Value, prefix: &str) -> Vec<String> {
    (0..n.as_u64().expect("a length"))
        .map(|i| format!("{prefix}{i}"))
        .collect()
}

fn check(case: &Value) -> bool {
    match case["k"].as_str().expect("a kind") {
        "unread" => unread_recommendation(why(&case["why"])) == case["o"],
        "since" => {
            let h = history(&case["h"]);
            let view = h
                .as_ref()
                .map(|r| r.as_ref().map(Vec::as_slice).map_err(|w| *w));
            let o = match dates_since(view, &dt(&case["s"])) {
                Err(_) => json!("TypeError"),
                Ok(None) => Value::Null,
                Ok(Some(Err(w))) => json!(w.value()),
                Ok(Some(Ok(n))) => json!(n),
            };
            o == case["o"]
        }
        "unc" => {
            let o = date_count_uncertainty(
                case["c"].as_i64().expect("c"),
                case["b"].as_str().expect("b"),
            );
            json!(o.map(HistoryUnread::value)) == case["o"]
        }
        "count" => count_case(case),
        "status" => spelled(&commit_drift_status(&resolved(&case["r"])).to_dict()) == case["o"],
        "cds" => {
            let f = &case["f"];
            let cds = CommitDriftStatus {
                status: f[0].as_str().expect("status").to_string(),
                commits_since_verify: f[1].as_i64().expect("count"),
                recommendation: (!f[2].is_null()).then(|| tf(&f[2])),
                claims_checked: f[3].as_i64().expect("checked"),
                claims_drifted: texts(&f[4]),
                basis: f[5].as_str().expect("basis").to_string(),
            };
            spelled(&cds.to_dict()) == case["o"]
        }
        "rec" => {
            let c = case["c"].as_i64().expect("c");
            drift_recommendation(c) == case["o"]
                && spell(&claim_drift_recommendation(c, &texts(&case["d"]))) == case["oc"]
        }
        "index" => {
            let got: Vec<Value> = index_paths(&index_of(&case["i"]))
                .iter()
                .map(|p| spell(p))
                .collect();
            Value::Array(got) == case["o"]
        }
        "claims" => {
            let c = claims(&case["p"]);
            let kept: Vec<Value> = gated_claims(&c)
                .iter()
                .map(|c| spell(&c.rel_path))
                .collect();
            json!(unreadable(&c)) == case["u"] && Value::Array(kept) == case["g"]
        }
        "verdict" => {
            let vs = VerificationStatus {
                status: status(case["st"].as_str().expect("st")),
                last_verified_at: None,
                age_days: None,
                recommendation: None,
                stale_after_days: 30,
            };
            let report = PathDriftReport {
                missing: names(&case["m"], "m"),
                claim_anchored_missing: names(&case["cam"], "m"),
                present_but_attested_absent: names(&case["pa"], "p"),
                ..PathDriftReport::default()
            };
            staleness_verdict_from_report(&vs, &report, case["c"].as_i64()) == case["o"]
        }
        other => panic!("unknown kind {other}"),
    }
}

#[test]
fn golden_verify_basis() {
    let lines = common::jsonl(FILE);
    let header = &lines[0];
    assert_eq!(header["k"], "header");
    assert_eq!(
        header["bases"],
        json!([BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
    );
    assert_eq!(header["unread_n"], json!(UNREAD_RECOMMENDATIONS.len()));
    let cases = &lines[1..];
    assert_eq!(header["count"], json!(cases.len()));
    let mut ledger = Ledger::new("golden_verify_basis", FILE);
    let mut unread_order = Vec::new();
    for (n, case) in cases.iter().enumerate() {
        if case["k"] == "unread" {
            unread_order.push(case["why"].clone());
        }
        let label = format!("case {n} ({})", case["k"]);
        let same = check(case);
        assert!(
            !ledger.differs_fatally(&label, same),
            "{label} differs: {case}"
        );
    }
    let order: Vec<Value> = UNREAD_RECOMMENDATIONS
        .iter()
        .map(|(w, _)| json!(w.value()))
        .collect();
    assert_eq!(unread_order, order);
    ledger.finish(cases.len());
}
