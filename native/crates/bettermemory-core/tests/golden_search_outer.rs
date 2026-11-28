//! Every case of `search_outer.jsonl` (gated on the Unicode data and the
//! platform, as `search.jsonl` is) against `search::outer`: `search` and
//! `rank` over the generator's memories with its stand-in admission,
//! `hydrate`, anchoring and drift read, the hits, the legs dict in its
//! order and every call of the stand-ins (`gen_search_hit.py`).

mod common;

use std::cell::RefCell;
use std::rc::Rc;

use bettermemory_core::attestation_paths::Paths;
use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::records::summaries::MemoryHit;
use bettermemory_core::records::Memory;
use bettermemory_core::search::hit::{Drift, RecordAnchors};
use bettermemory_core::search::outer::{
    self, Admission, EffectError, Effects, LegsOut, OuterError, Selection,
};
use bettermemory_core::search::snippet::SnippetMemo;
use bettermemory_core::search::{Mode, Options};
use bettermemory_core::unicode::{self, cps};
use bettermemory_core::verify_anchoring::anchor::{AnchorPaths, ClaimAnchor};
use bettermemory_core::verify_anchoring::detect::DriftError;
use bettermemory_core::verify_anchoring::passes::ReadAnchor;
use bettermemory_core::verify_anchoring::report::PathDriftReport;
use common::parity::Ledger;
use common::search_hit::{hit, memory, report, t};
use serde_json::{json, Value};

const FILE: &str = "search_outer.jsonl";

type Log = Rc<RefCell<Vec<Value>>>;

fn strs(v: &Value) -> Vec<String> {
    v.as_array()
        .expect("a list")
        .iter()
        .map(|s| s.as_str().expect("text").to_string())
        .collect()
}

fn texts(v: &[PyStr]) -> Value {
    json!(v.iter().map(|s| t(s)).collect::<Vec<_>>())
}

/// The case's admission: `home` from `homes`, `admits_row` the listed
/// pairs, RuntimeError where `raise` says.
struct Stub {
    spec: Value,
    log: Log,
}

impl Stub {
    fn raises(&self, at: &str) -> Result<(), EffectError> {
        match self.spec["raise"].as_str() {
            Some(r) if r == at => Err(EffectError("RuntimeError".into())),
            _ => Ok(()),
        }
    }
}

impl Admission for Stub {
    fn prime(&mut self, record_ids: &[String]) -> Result<(), EffectError> {
        self.log.borrow_mut().push(json!(["prime", record_ids]));
        self.raises("prime")
    }
    fn home(&mut self, record_id: &str) -> Result<Option<PyStr>, EffectError> {
        self.log.borrow_mut().push(json!(["home", record_id]));
        self.raises(record_id)?;
        Ok(self.spec["homes"][record_id].as_str().map(cps))
    }
    fn admits_row(
        &mut self,
        scopes: &[PyStr],
        recorded: Option<&[u32]>,
    ) -> Result<bool, EffectError> {
        let pair = json!([texts(scopes), recorded.map_or(Value::Null, t)]);
        self.log
            .borrow_mut()
            .push(json!(["admits_row", pair[0], pair[1]]));
        Ok(self.spec["admit"]
            .as_array()
            .expect("admit")
            .contains(&pair))
    }
}

/// The anchoring: `prime` logged as "anchors", each anchor from the table.
struct Anchors {
    table: Value,
    log: Log,
}

impl RecordAnchors for Anchors {
    fn prime(&mut self, record_ids: &[String]) {
        self.log.borrow_mut().push(json!(["anchors", record_ids]));
    }
    fn for_record(&mut self, record_id: &str, origin_root: Option<&[u32]>) -> Option<ClaimAnchor> {
        self.log.borrow_mut().push(json!([
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

/// The drift read: each call logged as the `"hit"` cases log it.
struct Detect {
    report: PathDriftReport,
    log: Log,
}

fn kind(p: &Paths) -> &'static str {
    use bettermemory_core::attestation_paths::Kind;
    match p.kind {
        Kind::Reference => "ReferencePaths",
        Kind::Literal => "LiteralPaths",
        Kind::Plain => "tuple",
    }
}

impl Drift for Detect {
    fn detect(
        &mut self,
        body: &[u32],
        verified_paths: &Paths,
        absent_paths: &Paths,
        worktree_root: Option<&[u32]>,
        anchor: Option<&dyn ReadAnchor>,
    ) -> Result<PathDriftReport, DriftError> {
        self.log.borrow_mut().push(json!([
            "detect",
            t(body),
            kind(verified_paths),
            texts(&verified_paths.items),
            kind(absent_paths),
            texts(&absent_paths.items),
            worktree_root.map_or(Value::Null, t),
            anchor.map_or(Value::Null, |a| json!([
                a.root().map_or(Value::Null, |r| t(&r)),
                a.rule()
            ])),
        ]));
        Ok(self.report.clone())
    }
}

/// Path checks no table anchor asks (none translates).
struct NoPaths;

impl AnchorPaths for NoPaths {
    fn expected_home(&self, _: &str, _: &[PyStr]) -> bool {
        unreachable!("a plain anchor checks no path")
    }
    fn relative_under(&self, _: &str, _: Option<&str>) -> Option<String> {
        unreachable!("a plain anchor checks no path")
    }
    fn inside(&self, _: &str, _: &str) -> bool {
        unreachable!("a plain anchor checks no path")
    }
}

/// The hit as `_compact` writes it: the fields' values, the score's bits.
fn compact(h: &MemoryHit, names: &[String]) -> Value {
    let spelled = hit(h);
    let pairs = spelled.as_array().expect("pairs");
    let got: Vec<String> = pairs
        .iter()
        .map(|p| p[0].as_str().unwrap().to_string())
        .collect();
    assert_eq!(got, names, "the hit's fields");
    Value::Array(
        pairs
            .iter()
            .map(|p| match p[0].as_str() {
                Some("score") => p[1]["bits"].clone(),
                _ => p[1].clone(),
            })
            .collect(),
    )
}

fn error(e: &OuterError) -> Value {
    json!({"error": match e {
        OuterError::Search(e) => e.name().to_string(),
        OuterError::Effect(EffectError(class)) => class.clone(),
        OuterError::Drift(e) => format!("{e:?}"),
    }})
}

fn strings(v: &Value) -> Option<Vec<String>> {
    v.as_array().map(|_| strs(v))
}

fn run(case: &Value, all: &[Memory], pool: usize, now: &DateTime, names: &[String]) -> Value {
    let kw = &case["kw"];
    let only: Option<Vec<Memory>> = case["only"].as_array().map(|ix| {
        ix.iter()
            .map(|i| all[i.as_u64().unwrap() as usize].clone())
            .collect()
    });
    let memories = only.as_deref().unwrap_or(&all[..pool]);
    let log: Log = Rc::default();
    let scopes = strings(&kw["scopes"]);
    let excluded = strings(&kw["excluded_scopes"]);
    let client = kw["client_filter"].as_str().map(cps);
    let model = kw["model_filter"].as_str().map(cps);
    let mut stub = (!case["adm"].is_null()).then(|| Stub {
        spec: case["adm"].clone(),
        log: log.clone(),
    });
    let mut legs: Option<LegsOut> = case["legs"].as_array().map(|pairs| {
        pairs
            .iter()
            .map(|p| {
                (
                    p[0].as_str().unwrap().to_string(),
                    p[1].as_str().unwrap().to_string(),
                )
            })
            .collect()
    });
    let query = cps(case["q"].as_str().expect("q"));
    let finish = |o: Value, legs: &Option<LegsOut>, log: &Log| {
        let mut out = json!({"o": o, "log": log.borrow().clone()});
        if case["f"] == "search" {
            out["legs_o"] = legs.as_ref().map_or(Value::Null, |l| json!(l));
        }
        out
    };
    let mode = match Mode::parse(kw["mode"].as_str().unwrap_or("hybrid")) {
        Ok(mode) => mode,
        Err(e) => return finish(json!({"error": e.name()}), &legs, &log),
    };
    let opts = Options {
        mode,
        max_results: kw["max_results"].as_i64().unwrap_or(5),
        rescue_expansion: kw["rescue_expansion"].as_bool().unwrap_or(false),
        allow_empty_query: kw["allow_empty_query"].as_bool().unwrap_or(false),
        scopes: scopes.as_deref(),
        excluded_scopes: excluded.as_deref(),
        ..Options::default()
    };
    let admission = stub.as_mut().map(|s| s as &mut dyn Admission);
    let o = if case["f"] == "rank" {
        match outer::rank(memories, &query, now, &opts, admission) {
            Ok(ranked) => json!(ranked),
            Err(e) => error(&e),
        }
    } else {
        let mut sel = Selection {
            admission,
            client: client.as_deref(),
            model: model.as_deref(),
        };
        let mut anchors = (!case["t"].is_null()).then(|| Anchors {
            table: case["t"].clone(),
            log: log.clone(),
        });
        let mut drift = Detect {
            report: report(&case["rep"]),
            log: log.clone(),
        };
        let hy = case["hy"].clone();
        let hlog = log.clone();
        let mut hydrate = move |records: &[Memory]| -> Result<Vec<Memory>, EffectError> {
            let ids: Vec<Value> = records.iter().map(|r| t(&r.fields.id)).collect();
            hlog.borrow_mut().push(json!(["hydrate", ids]));
            let mut out: Vec<Memory> = records
                .iter()
                .map(|r| {
                    let id = unicode::to_string(&r.fields.id).unwrap();
                    match hy["swap"][id.as_str()].as_u64() {
                        Some(i) => all[i as usize].clone(),
                        None => r.clone(),
                    }
                })
                .collect();
            let drop = hy["drop"].as_u64().unwrap_or(0) as usize;
            out.truncate(out.len().saturating_sub(drop));
            for i in hy["add"].as_array().into_iter().flatten() {
                out.push(all[i.as_u64().unwrap() as usize].clone());
            }
            Ok(out)
        };
        let memo = SnippetMemo::default();
        let mut fx = Effects {
            anchoring: anchors.as_mut().map(|a| a as &mut dyn RecordAnchors),
            paths: &NoPaths,
            drift: &mut drift,
            memo: &memo,
            hydrate: (!case["hy"].is_null()).then_some(&mut hydrate as outer::Hydrate<'_>),
            path_drift: kw["path_drift"].as_bool().unwrap_or(false),
        };
        match outer::search(
            memories,
            &query,
            now,
            &opts,
            &mut sel,
            &mut fx,
            legs.as_mut(),
        ) {
            Ok(hits) => Value::Array(hits.iter().map(|h| compact(h, names)).collect()),
            Err(e) => error(&e),
        }
    };
    finish(o, &legs, &log)
}

#[test]
fn golden_search_outer() {
    let lines = common::jsonl(FILE);
    let header = &lines[0];
    assert_eq!(header["k"], "header");
    let memories: Vec<Memory> = header["memories"]
        .as_array()
        .expect("memories")
        .iter()
        .map(memory)
        .collect();
    let now = DateTime::from_isoformat(&cps(header["now"].as_str().expect("now"))).expect("now");
    let names = strs(&header["hit_fields"]);
    let pool = header["pool"].as_u64().expect("pool") as usize;
    let count = header["count"].as_u64().expect("a count") as usize;
    let mut ledger = Ledger::new("golden_search_outer", FILE);
    let mut checked = 0;
    for (i, case) in lines[1..].iter().enumerate() {
        let got = run(case, &memories, pool, &now, &names);
        let mut want = json!({"o": case["o"], "log": case["log"]});
        if case["f"] == "search" {
            want["legs_o"] = case["legs_o"].clone();
        }
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
