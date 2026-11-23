//! Every case of `verify_anchoring_anchor.jsonl` (not gated) against
//! `verify_anchoring::report` and `verify_anchoring::anchor`. The stub
//! model, path rules and logs are the generator's
//! (`tests/contract/gen_verify_anchoring_anchor.py`).

mod common;

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode;
use bettermemory_core::verify_anchoring::anchor::{
    AnchorPaths, Anchoring, CallerView, Checkout, ClaimAnchor, HoldsFn, Home, MembershipModel,
    TranslateFn, UnknownCheckout, View, CONVICTING_ANCHORS, GLOBAL_RECORD, HELD_COMMIT_MEMO_CAP,
    SPARSE_CHECKOUT, UNANCHORED, UNCONFIRMED_COMMIT,
};
use bettermemory_core::verify_anchoring::report::{DictField, PathDriftReport};
use common::parity::Ledger;
use serde_json::{json, Value};

const FILE: &str = "verify_anchoring_anchor.jsonl";

type Log = Rc<RefCell<Vec<Value>>>;

fn strs(v: &Value) -> Vec<String> {
    match v {
        Value::Null => Vec::new(),
        v => v
            .as_array()
            .expect("a list")
            .iter()
            .map(|s| s.as_str().expect("text").to_string())
            .collect(),
    }
}

fn opt(v: &Value) -> Option<String> {
    v.as_str().map(str::to_string)
}

fn inside(p: &str, r: &str) -> bool {
    p == r || p.starts_with(&format!("{r}/"))
}

struct Paths(Log);

impl AnchorPaths for Paths {
    fn expected_home(&self, path: &str, roots: &[PyStr]) -> bool {
        let roots: Vec<_> = roots
            .iter()
            .map(|r| unicode::to_string(r).unwrap())
            .collect();
        self.0
            .borrow_mut()
            .push(json!(["expected_home", path, roots]));
        roots.iter().any(|r| inside(path, r))
    }
    fn relative_under(&self, path: &str, root: Option<&str>) -> Option<String> {
        self.0
            .borrow_mut()
            .push(json!(["relative_under", path, root]));
        let root = root?;
        if !inside(path, root) {
            return None;
        }
        let rest = path.get(root.len() + 1..).unwrap_or("");
        Some(if rest.is_empty() { "." } else { rest }.to_string())
    }
    fn inside(&self, path: &str, root: &str) -> bool {
        self.0.borrow_mut().push(json!(["inside", path, root]));
        inside(path, root)
    }
}

struct Model {
    w: Value,
    log: Log,
}

impl Model {
    fn push(&self, v: Value) {
        self.log.borrow_mut().push(v);
    }
    fn home_of(v: &Value) -> Home {
        Home {
            project: v[0].as_str().expect("project").to_string(),
            checkout: opt(&v[1]),
        }
    }
    fn checkout_of(id: &str, spec: &Value) -> Checkout {
        Checkout {
            id: id.to_string(),
            project: spec["project"].as_str().expect("project").to_string(),
            remotes: strs(&spec["remotes"]),
            views: spec["views"]
                .as_array()
                .expect("views")
                .iter()
                .map(|v| View {
                    machine: v[0].as_str().expect("m").to_string(),
                    path: v[1].as_str().expect("p").to_string(),
                })
                .collect(),
        }
    }
}

fn caller_id(caller: Option<&CallerView>) -> Value {
    match caller {
        None => Value::Null,
        Some(c) => json!(c.checkout),
    }
}

impl MembershipModel for Model {
    fn homes(&self, ids: &[String]) -> HashMap<String, Home> {
        self.push(json!(["homes", ids]));
        ids.iter()
            .filter_map(|i| {
                self.w["homes"]
                    .get(i)
                    .map(|h| (i.clone(), Model::home_of(h)))
            })
            .collect()
    }
    fn home(&self, id: &str) -> Option<Home> {
        self.push(json!(["home", id]));
        self.w["homes"].get(id).map(Model::home_of)
    }
    fn live_view(&self, k: &Checkout) -> Option<String> {
        self.push(json!(["live_view", k.id]));
        self.w["live"].get(&k.id).and_then(opt)
    }
    fn checkout(&self, id: &str) -> Result<Checkout, UnknownCheckout> {
        self.push(json!(["checkout", id]));
        self.w["checkouts"]
            .get(id)
            .map(|s| Model::checkout_of(id, s))
            .ok_or(UnknownCheckout)
    }
    fn anchor(&self, id: &str, caller: Option<&CallerView>) -> (Option<String>, String) {
        self.push(json!(["anchor", id, caller_id(caller)]));
        let a = &self.w["anchors"][id];
        (opt(&a[0]), a[1].as_str().expect("rule").to_string())
    }
    fn checkouts(&self) -> Vec<Checkout> {
        self.push(json!(["checkouts"]));
        self.w["checkouts"]
            .as_object()
            .expect("checkouts")
            .iter()
            .map(|(k, v)| Model::checkout_of(k, v))
            .collect()
    }
    fn canonical(&self, project: Option<&str>) -> Option<String> {
        self.push(json!(["canonical", project]));
        let p = project?;
        Some(
            self.w["canonical"]
                .get(p)
                .and_then(Value::as_str)
                .unwrap_or(p)
                .to_string(),
        )
    }
    fn translate(&self, path: &str, caller: Option<&CallerView>) -> (String, String) {
        self.push(json!(["translate", path, caller_id(caller)]));
        match self.w["translate"].get(path) {
            Some(t) => (opt(&t[0]).expect("t"), opt(&t[1]).expect("r")),
            None => (path.to_string(), "checkout".to_string()),
        }
    }
    fn machine(&self) -> String {
        self.push(json!(["machine"]));
        self.w["machine"].as_str().expect("machine").to_string()
    }
}

fn held_fn(pairs: &Value, log: &Log) -> HoldsFn {
    let table: Vec<(String, String)> = pairs
        .as_array()
        .expect("pairs")
        .iter()
        .map(|p| (opt(&p[0]).expect("r"), opt(&p[1]).expect("s")))
        .collect();
    let log = log.clone();
    Rc::new(move |r: &str, s: &str| {
        log.borrow_mut().push(json!(["holds_commit", r, s]));
        table.iter().any(|(a, b)| a == r && b == s)
    })
}

fn anchor_json(a: &Option<ClaimAnchor>) -> Value {
    match a {
        None => Value::Null,
        Some(a) => json!({
            "root": a.root, "rule": a.rule, "caller_root": a.caller_root,
            "origin_root": a.origin_root.as_deref().map(|r| unicode::to_string(r).unwrap()), "recorded_roots": a.recorded_roots,
            "commit_standin": a.commit_standin,
        }),
    }
}

fn anchor_op(op: &[Value], a: &ClaimAnchor, paths: &Paths, hc: &HoldsFn) -> Value {
    match op[0].as_str().expect("op") {
        "convicting" => json!(a.convicting()),
        "declared_root" => json!(a.declared_root()),
        "absolute" => json!(a.absolute(op[2].as_str().expect("path"), paths)),
        "commit_root" => json!(a.commit_root(op[2].as_str(), &|r: &str, s: &str| hc(r, s))),
        other => panic!("op {other}"),
    }
}

fn report_case(case: &Value) -> Value {
    let r = &case["r"];
    let rep = PathDriftReport {
        checked: strs(&r["checked"]),
        missing: strs(&r["missing"]),
        verified: strs(&r["verified"]),
        expected_absent: strs(&r["expected_absent"]),
        dropped_as_route: strs(&r["dropped_as_route"]),
        claim_anchored_missing: strs(&r["claim_anchored_missing"]),
        unconfirmed: strs(&r["unconfirmed"]),
        unconfirmed_reasons: strs(&r["unconfirmed_reasons"]),
        present_but_attested_absent: strs(&r["present_but_attested_absent"]),
        anchor: opt(&r["anchor"]),
    };
    let dict = match rep.to_dict() {
        Err(_) => json!("ValueError"),
        Ok(pairs) => Value::Array(
            pairs
                .into_iter()
                .map(|(k, v)| {
                    let v = match v {
                        DictField::Paths(p) => json!(p),
                        DictField::Text(t) => json!(t),
                        DictField::Unconfirmed(u) => Value::Array(
                            u.into_iter()
                                .map(|(p, r)| json!({"path": p, "reason": r}))
                                .collect(),
                        ),
                    };
                    json!([k, v])
                })
                .collect(),
        ),
    };
    json!({"has_drift": rep.has_drift(), "has_claim": rep.has_claim_anchored_drift(),
           "should": rep.should_report(), "dict": dict})
}

fn anchor_case(case: &Value) -> (Value, Value) {
    let log: Log = Rc::new(RefCell::new(Vec::new()));
    let paths = Paths(log.clone());
    let a = &case["a"];
    let translate: Option<TranslateFn> = a.get("t").map(|t| {
        let t = t.clone();
        let log = log.clone();
        Rc::new(move |p: &str| {
            log.borrow_mut().push(json!(["translate", p]));
            match t.get(p) {
                Some(v) => (opt(&v[0]).expect("t"), opt(&v[1]).expect("r")),
                None => (p.to_string(), "absolute".to_string()),
            }
        }) as TranslateFn
    });
    let anchor = ClaimAnchor {
        root: opt(&a["root"]),
        rule: opt(&a["rule"]).expect("rule"),
        translate,
        caller_root: opt(&a["caller_root"]),
        holds: a
            .get("h")
            .filter(|h| !h.is_null())
            .map(|h| held_fn(h, &log)),
        origin_root: a["origin_root"].as_str().map(unicode::cps),
        recorded_roots: strs(&a["recorded_roots"]),
        commit_standin: a["commit_standin"].as_bool().unwrap_or(false),
    };
    let hc = held_fn(&case["hc"], &log);
    let mut out = Vec::new();
    for op in case["ops"].as_array().expect("ops") {
        let op = op.as_array().expect("op");
        let mut full = vec![op[0].clone(), json!(0)];
        full.extend(op[1..].iter().cloned());
        out.push(anchor_op(&full, &anchor, &paths, &hc));
    }
    let log = log.borrow().clone();
    (Value::Array(out), Value::Array(log))
}

fn anchoring_case(case: &Value) -> (Value, Value) {
    let log: Log = Rc::new(RefCell::new(Vec::new()));
    let paths = Rc::new(Paths(log.clone()));
    let model = Rc::new(Model {
        w: case["w"].clone(),
        log: log.clone(),
    });
    let caller = case["c"].as_array().map(|c| CallerView {
        checkout: opt(&c[0]),
    });
    let hc = held_fn(&case["hc"], &log);
    let elog = log.clone();
    let expand = Rc::new(move |p: &str| {
        elog.borrow_mut().push(json!(["expanduser", p]));
        match p.strip_prefix('~') {
            Some(rest) => format!("/h/u{rest}"),
            None => p.to_string(),
        }
    });
    let mut anchoring = Anchoring::new(model, caller, hc.clone(), paths.clone(), expand);
    let mut anchors: Vec<Option<ClaimAnchor>> = Vec::new();
    let mut out = Vec::new();
    let mut fill = false;
    for op in case["ops"].as_array().expect("ops") {
        let op = op.as_array().expect("op");
        let got = match op[0].as_str().expect("op") {
            "prime" => {
                anchoring.prime(&strs(&op[1]));
                Value::Null
            }
            "home" => match anchoring.home(op[1].as_str().expect("id")) {
                None => Value::Null,
                Some(h) => json!([h.project, h.checkout]),
            },
            "caller_root" => json!(anchoring.caller_root()),
            "for_record" => {
                let a = anchoring.for_record(op[1].as_str().expect("id"), op[2].as_str());
                let v = anchor_json(&a);
                anchors.push(a);
                v
            }
            "holds" => {
                json!(anchoring.holds(op[1].as_str().expect("r"), op[2].as_str().expect("s")))
            }
            "fill" => {
                fill = true;
                let r = op[1].as_str().expect("r");
                let n = op[2].as_u64().expect("n");
                json!((0..n)
                    .filter(|i| anchoring.holds(r, &format!("s{i}")))
                    .count())
            }
            "translate" => json!(anchoring.translate(op[1].as_str().expect("path"))),
            _ => {
                let i = op[1].as_u64().expect("index") as usize;
                let a = anchors[i].as_ref().expect("an anchor");
                anchor_op(op, a, &paths, &hc)
            }
        };
        out.push(got);
    }
    let mut log = log.borrow().clone();
    if fill {
        let n = log.iter().filter(|e| e[0] == "holds_commit").count();
        log.retain(|e| e[0] != "holds_commit");
        log.push(json!(["holds_commit_count", n]));
    }
    (Value::Array(out), Value::Array(log))
}

#[test]
fn golden_verify_anchoring_anchor() {
    let lines = common::jsonl(FILE);
    let header = &lines[0];
    assert_eq!(header["k"], "header");
    let mut convicting: Vec<&str> = CONVICTING_ANCHORS.to_vec();
    convicting.sort();
    assert_eq!(
        header["constants"],
        json!({
            "CONVICTING_ANCHORS": convicting, "UNANCHORED": UNANCHORED,
            "SPARSE_CHECKOUT": SPARSE_CHECKOUT, "UNCONFIRMED_COMMIT": UNCONFIRMED_COMMIT,
            "GLOBAL_RECORD": GLOBAL_RECORD, "HELD_COMMIT_MEMO_CAP": HELD_COMMIT_MEMO_CAP,
        })
    );
    let count = header["count"].as_u64().expect("a count") as usize;
    let mut ledger = Ledger::new("golden_verify_anchoring_anchor", FILE);
    let mut checked = 0;
    for (i, case) in lines[1..].iter().enumerate() {
        let (got, want) = match case["f"].as_str().expect("f") {
            "report" => (report_case(case), case["o"].clone()),
            "anchor" => {
                let (o, log) = anchor_case(case);
                (json!([o, log]), json!([case["o"], case["log"]]))
            }
            "anchoring" => {
                let (o, log) = anchoring_case(case);
                (json!([o, log]), json!([case["o"], case["log"]]))
            }
            other => panic!("unknown case {other}"),
        };
        let label = format!("line {}", i + 2);
        assert!(
            !ledger.differs_fatally(&label, got == want),
            "{label} {case}: got {got}"
        );
        checked += 1;
    }
    assert!(checked > 0);
    assert_eq!(checked, count);
    ledger.finish(count);
}
