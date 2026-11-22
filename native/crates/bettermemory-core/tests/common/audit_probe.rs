//! The harness of `audit_probe.jsonl`: a row's arguments read into the
//! native `audit::probe` calls, a recording ranker and admission, and the
//! answers spelled as the generator spells them
//! (`tests/contract/gen_audit.py`, `generate_probe`).

use bettermemory_core::audit::probe::{
    count_recent_retrievals, is_duplicate_audit, probe_for_miss, ProbeAdmission, ProbeArgs,
    ProbeError, ProbeHit, ProbeKnobs, ProbeSearch,
};
use bettermemory_core::audit::report::MissReport;
use bettermemory_core::datetime::DateTime;
use bettermemory_core::float_repr;
use bettermemory_core::json::loads;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::records::Memory;
use bettermemory_core::unicode::{cps, to_string};
use serde_json::{json, Value};

/// A plain JSON value as `json.loads` reads it.
pub fn plain(v: &Value) -> PyValue {
    loads(&cps(&v.to_string())).expect("json")
}

pub fn text(v: &Value) -> PyStr {
    cps(v.as_str().expect("a string"))
}

fn opt_text(v: Option<&Value>) -> Option<PyStr> {
    v.and_then(Value::as_str).map(cps)
}

fn s(t: &[u32]) -> String {
    to_string(t).expect("valid text")
}

pub fn dt(v: &Value) -> DateTime {
    DateTime::from_isoformat(&text(v)).expect("an isoformat")
}

fn neutral_float(f: f64) -> Value {
    json!({"t": "float", "bits": format!("{:016x}", f.to_bits()), "repr": float_repr::repr(f)})
}

/// `_spec` of a report.
pub fn spec(r: &MissReport) -> Value {
    let hits: Vec<Value> = r
        .top_hits
        .iter()
        .map(|h| {
            json!({"id": s(&h.id), "score": neutral_float(h.score), "rel": s(&h.relevance),
                   "scopes": h.scopes.iter().map(|x| s(x)).collect::<Vec<_>>(),
                   "snip": s(&h.snippet), "mu": h.matched_unique, "qu": h.query_unique,
                   "rel2": s(&h.relevance_v2)})
        })
        .collect();
    json!({"v": r.verdict.as_str(), "at": r.checked_at.isoformat(), "sid": s(&r.session_id),
           "lb": r.lookback_seconds, "rrc": r.recent_retrieval_count,
           "rule": s(&r.threshold_rule), "hits": hits,
           "pq": r.probe_query.as_deref().map(s), "nsr": r.no_signal_reason.as_deref().map(s),
           "sb": r.suppressed_by.as_deref().map(s)})
}

pub fn err(e: &ProbeError) -> Value {
    match e {
        ProbeError::ValueError(m) => json!({"err": "ValueError", "msg": s(m)}),
        ProbeError::OverflowError => json!({"err": "OverflowError"}),
        ProbeError::TypeError => json!({"err": "TypeError"}),
        ProbeError::AttributeError => json!({"err": "AttributeError"}),
    }
}

/// The admission fake: answers `same_project` from a list and keeps what
/// it was given.
#[derive(Default)]
pub struct FakeAdmission {
    pub same: Vec<PyStr>,
    pub primed: Vec<Vec<PyStr>>,
    pub asked: Vec<PyStr>,
}

impl ProbeAdmission for FakeAdmission {
    fn prime(&mut self, ids: &[PyStr]) {
        self.primed.push(ids.to_vec());
    }
    fn same_project(&mut self, id: &[u32]) -> bool {
        self.asked.push(id.to_vec());
        self.same.iter().any(|x| x == id)
    }
}

/// The recorded ranker: answers its hits, keeps each call. The reference
/// forwards the caller's admission to `run_search` unchanged (the row's
/// `adm` is true throughout); here the ranker holds its own.
pub struct FakeRanker {
    pub hits: Vec<ProbeHit>,
    pub calls: Vec<Value>,
}

impl ProbeSearch for FakeRanker {
    fn search(
        &mut self,
        pool: &[Memory],
        message: &[u32],
        now: &DateTime,
        k: &ProbeKnobs,
    ) -> Result<Vec<ProbeHit>, ProbeError> {
        let ex = k.excluded_scopes.as_ref().map(|v| {
            let mut v: Vec<String> = v.iter().map(|x| s(x)).collect();
            v.sort();
            v
        });
        self.calls.push(json!({
            "pool": pool.iter().map(|m| s(&m.fields.id)).collect::<Vec<_>>(),
            "msg": s(message), "now": now.isoformat(), "mode": s(&k.mode), "ex": ex,
            "max": k.max_results, "hl": k.half_life_days, "conv": k.conversational,
            "rescue": k.rescue_expansion, "adm": true,
        }));
        Ok(self.hits.clone())
    }
}

pub fn hit(h: &Value) -> ProbeHit {
    let id = text(&h["id"]);
    let mut snippet = cps("snip ");
    snippet.extend(&id);
    ProbeHit {
        id,
        score: h["score"].as_f64().unwrap(),
        relevance: text(&h["rel"]),
        scopes: h["scopes"].as_array().unwrap().iter().map(text).collect(),
        snippet,
        matched_unique: h["mu"].as_i64().unwrap(),
        query_unique: h["qu"].as_i64().unwrap(),
    }
}

fn admission(row: &Value) -> Option<FakeAdmission> {
    row["same"].as_array().map(|same| FakeAdmission {
        same: same.iter().map(text).collect(),
        ..FakeAdmission::default()
    })
}

fn ids_json(v: &[Vec<PyStr>]) -> Value {
    json!(v
        .iter()
        .map(|x| x.iter().map(|y| s(y)).collect::<Vec<_>>())
        .collect::<Vec<_>>())
}

fn adm_json(a: &Option<FakeAdmission>) -> (Value, Value) {
    match a {
        None => (Value::Null, Value::Null),
        Some(a) => (
            ids_json(&a.primed),
            json!(a.asked.iter().map(|y| s(y)).collect::<Vec<_>>()),
        ),
    }
}

pub fn events(row: &Value) -> Vec<PyValue> {
    row["ev"].as_array().unwrap().iter().map(plain).collect()
}

/// The header's memory payloads, validated.
pub fn memories(header: &Value) -> Vec<Memory> {
    header["mems"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| Memory::from_payload(&plain(m)).expect("a memory"))
        .collect()
}

pub fn probe_args(a: &Value) -> ProbeArgs {
    let mut p = ProbeArgs::new(&text(&a["session_id"]), dt(&a["now"]));
    p.retrieval_session_id = opt_text(a.get("retrieval_session_id"));
    p.worktree_root = opt_text(a.get("worktree_root"));
    if let Some(v) = a.get("lookback_seconds") {
        p.lookback_seconds = v.as_i64().unwrap();
    }
    if let Some(v) = a.get("creation_shield_seconds") {
        p.creation_shield_seconds = v.as_i64().unwrap();
    }
    if let Some(v) = a.get("excluded_scopes") {
        p.excluded_scopes = Some(v.as_array().unwrap().iter().map(text).collect());
    }
    if let Some(v) = a.get("mode") {
        p.mode = text(v);
    }
    if let Some(v) = a.get("half_life_days") {
        p.half_life_days = v.as_f64().unwrap();
    }
    for (key, slot) in [
        ("conversational", &mut p.conversational),
        ("floor", &mut p.floor),
        ("searched", &mut p.searched),
    ] {
        if let Some(v) = a.get(key) {
            *slot = v.as_bool().unwrap();
        }
    }
    p
}

/// A probe row's answer, ranker calls, primes and asks, spelled as the
/// row spells them.
pub fn run_probe(row: &Value, mems: &[Memory]) -> Value {
    let pool: Vec<Memory> = row["mem"]
        .as_array()
        .unwrap()
        .iter()
        .map(|i| mems[i.as_u64().unwrap() as usize].clone())
        .collect();
    let mut adm = admission(row);
    let mut ranker = FakeRanker {
        hits: row["hits"].as_array().unwrap().iter().map(hit).collect(),
        calls: Vec::new(),
    };
    let args = probe_args(&row["args"]);
    let got = probe_for_miss(
        &pool,
        &text(&row["msg"]),
        &events(row),
        adm.as_mut().map(|a| a as &mut dyn ProbeAdmission),
        &mut ranker,
        &args,
    );
    let o = match got {
        Ok(r) => spec(&r),
        Err(e) => err(&e),
    };
    let (primed, asked) = adm_json(&adm);
    json!({"o": o, "calls": ranker.calls, "primed": primed, "asked": asked})
}

pub fn run_dup(row: &Value) -> Value {
    let a = &row["args"];
    let win = a.get("window_seconds").map_or(
        bettermemory_core::audit::REAUDIT_DEDUP_WINDOW_SECONDS,
        |v| v.as_i64().unwrap(),
    );
    let hash = opt_text(a.get("probe_query_hash"));
    match is_duplicate_audit(
        &events(row),
        &text(&a["session_id"]),
        hash.as_deref(),
        &text(&a["probe_query_text"]),
        &dt(&a["now"]),
        win,
    ) {
        Ok(b) => json!(b),
        Err(e) => err(&e),
    }
}

pub fn run_count(row: &Value) -> Value {
    let a = &row["args"];
    let mut adm = admission(row);
    let sids: Vec<PyStr> = a["session_ids"]
        .as_array()
        .unwrap()
        .iter()
        .map(text)
        .collect();
    let wt = opt_text(a.get("worktree_root"));
    let got = count_recent_retrievals(
        &events(row),
        &sids,
        &dt(&a["now"]),
        a["lookback_seconds"].as_i64().unwrap(),
        adm.as_mut().map(|x| x as &mut dyn ProbeAdmission),
        wt.as_deref(),
    );
    let o = match got {
        Ok(n) => json!(n),
        Err(e) => err(&e),
    };
    let (primed, asked) = adm_json(&adm);
    json!({"o": o, "primed": primed, "asked": asked})
}
