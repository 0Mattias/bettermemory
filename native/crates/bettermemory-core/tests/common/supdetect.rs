//! Reading and answering the cases of `gen_supersession_detect`: its
//! records, its scripted store, and each answer spelled as the generator
//! spells it.

use std::collections::{BTreeSet, HashMap};
use std::sync::Arc;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::records::Confidence;
use bettermemory_core::search_dedup::{raw_content_token_set, Candidate, RawTokenCache, TokenSet};
use bettermemory_core::store::digest::{expanded, features};
use bettermemory_core::store::digest_query::DigestShare;
use bettermemory_core::supersession::{
    detect_supersession, detect_supersession_in, one_claim_replaced, strip_provenance, DetectError,
    Detected, SupersessionMatch, SupersessionMemoKey, SupersessionReport, SupersessionStore,
};
use bettermemory_core::unicode;
use serde_json::{json, Value};

pub fn cps(v: &Value) -> PyStr {
    unicode::cps(v.as_str().expect("a string"))
}

pub fn text(t: &[u32]) -> String {
    unicode::to_string(t).expect("no surrogate in these fixtures")
}

pub fn bits(x: f64) -> String {
    format!("{:016x}", x.to_bits())
}

fn strings(v: &Value) -> Vec<String> {
    v.as_array()
        .map(|a| {
            a.iter()
                .map(|s| s.as_str().expect("a string").to_string())
                .collect()
        })
        .unwrap_or_default()
}

/// A stored record: the detector reads its id and body alone.
#[derive(Clone, Debug)]
pub struct Rec {
    pub id: PyStr,
    pub body: PyStr,
    created: DateTime,
}

impl Rec {
    pub fn new(id: &str, body: &str) -> Rec {
        Rec {
            id: unicode::cps(id),
            body: unicode::cps(body),
            created: DateTime::from_isoformat(&unicode::cps("2024-03-01T12:00:00+00:00"))
                .expect("a datetime"),
        }
    }
}

impl Candidate for Rec {
    fn id(&self) -> &[u32] {
        &self.id
    }
    fn scopes(&self) -> &[PyStr] {
        &[]
    }
    fn confidence(&self) -> Confidence {
        Confidence::MEMBERS[0].1
    }
    fn body(&self) -> &[u32] {
        &self.body
    }
    fn created(&self) -> DateTime {
        self.created
    }
    fn updated(&self) -> DateTime {
        self.created
    }
}

/// `[[id, body], ...]` in order.
pub fn pairs(v: &Value) -> Vec<(String, String)> {
    v.as_array()
        .expect("records")
        .iter()
        .map(|r| {
            let id = r[0].as_str().expect("an id").to_string();
            (id, r[1].as_str().expect("a body").to_string())
        })
        .collect()
}

/// The generator's scripted store.
pub struct Fake {
    pub t: PyStr,
    pub recs: Vec<(String, String)>,
    pub shares: Option<Vec<DigestShare>>,
    pub tok: HashMap<String, TokenSet>,
    pub gone: BTreeSet<String>,
    pub bodies: HashMap<String, PyStr>,
    pub memo_none: bool,
    pub log: Vec<Value>,
    held: HashMap<SupersessionMemoKey, SupersessionReport>,
}

pub fn share(id: &str, claim: i64, stamped: bool, shared: i64) -> DigestShare {
    DigestShare {
        id: id.to_string(),
        n: PyValue::Int("0".into()),
        claim: PyValue::Int(claim.to_string()),
        stamped,
        shared,
    }
}

impl Fake {
    pub fn new(t: &str, recs: Vec<(String, String)>, shares: Option<Vec<DigestShare>>) -> Fake {
        let bodies = recs
            .iter()
            .map(|(i, b)| (i.clone(), unicode::cps(b)))
            .collect();
        Fake {
            t: unicode::cps(t),
            recs,
            shares,
            tok: HashMap::new(),
            gone: BTreeSet::new(),
            bodies,
            memo_none: false,
            log: Vec::new(),
            held: HashMap::new(),
        }
    }

    /// The store an `in` case scripts.
    pub fn of(case: &Value) -> Fake {
        let shares = case["shares"].as_array().map(|a| {
            a.iter()
                .map(|s| {
                    share(
                        s[0].as_str().expect("an id"),
                        s[1].as_i64().expect("a claim"),
                        s[2].as_bool().expect("stamped"),
                        s[3].as_i64().expect("shared"),
                    )
                })
                .collect()
        });
        let mut fake = Fake::new(case["t"].as_str().expect("t"), pairs(&case["recs"]), shares);
        if let Some(tok) = case.get("tok").and_then(Value::as_object) {
            for (id, tokens) in tok {
                fake.tok
                    .insert(id.clone(), strings(tokens).into_iter().collect());
            }
        }
        fake.gone = strings(&case["gone"]).into_iter().collect();
        for id in strings(&case["nobody"]) {
            fake.bodies.remove(&id);
        }
        fake.memo_none = case["memo"] == json!("none");
        fake
    }
}

impl SupersessionStore for Fake {
    type Active = Rec;

    fn digest_shares(
        &mut self,
        tokens: &TokenSet,
        removed: bool,
        floor: f64,
        claims: bool,
    ) -> Option<Vec<DigestShare>> {
        self.log
            .push(json!(["shares", tokens, removed, bits(floor), claims]));
        self.shares.clone()
    }

    fn digest_tokens_of(&mut self, ids: &[String]) -> HashMap<String, Arc<TokenSet>> {
        self.log.push(json!(["tokens", ids]));
        let all: HashMap<&String, &String> = self.recs.iter().map(|(i, b)| (i, b)).collect();
        let mut out = HashMap::new();
        for id in ids {
            if self.gone.contains(id) {
                continue;
            }
            if let Some(tok) = self.tok.get(id) {
                out.insert(id.clone(), Arc::new(tok.clone()));
            } else if let Some(body) = all.get(id) {
                let tokens = raw_content_token_set(&unicode::cps(body));
                out.insert(id.clone(), Arc::new(tokens));
            }
        }
        out
    }

    fn bodies_of(&mut self, ids: &[String]) -> HashMap<String, PyStr> {
        self.log.push(json!(["bodies", ids]));
        ids.iter()
            .filter_map(|i| self.bodies.get(i).map(|b| (i.clone(), b.clone())))
            .collect()
    }

    fn digest_memo(
        &mut self,
        key: &SupersessionMemoKey,
        compute: &mut dyn FnMut(&mut Self) -> Detected,
    ) -> Detected {
        let exclude: Vec<String> = key.exclude.iter().map(|e| text(e)).collect();
        self.log
            .push(json!(["memo", "supersession", key.body == self.t, exclude]));
        if self.memo_none {
            return Ok(None);
        }
        if let Some(report) = self.held.get(key) {
            return Ok(Some(report.clone()));
        }
        let answer = compute(self)?;
        if let Some(report) = &answer {
            self.held.insert(key.clone(), report.clone());
        }
        Ok(answer)
    }

    fn load_all(&mut self) -> Vec<Rec> {
        self.log.push(json!(["all"]));
        self.recs.iter().map(|(i, b)| Rec::new(i, b)).collect()
    }
}

pub fn match_out(m: &SupersessionMatch) -> Value {
    json!([
        text(&m.memory_id),
        text(&m.outcome),
        text(&m.evidence),
        m.cue.as_deref().map(text),
        text(&m.new_value),
        text(&m.old_value),
        bits(m.similarity),
        text(&m.summary),
    ])
}

pub fn report_out(r: &Result<SupersessionReport, DetectError>) -> Value {
    match r {
        Ok(r) => json!({
            "el": r.eligible,
            "sup": r.supersedes.iter().map(match_out).collect::<Vec<_>>(),
            "con": r.conflicts.iter().map(match_out).collect::<Vec<_>>(),
        }),
        Err(e) => json!({"err": e.class(), "msg": e.message()}),
    }
}

/// The cache's keys, named as the generator names them.
pub fn keys_out(cache: &RawTokenCache, t: &str, recs: &[(String, String)]) -> Value {
    let mut names: HashMap<PyStr, String> = HashMap::new();
    for (id, body) in recs {
        names
            .entry(strip_provenance(&unicode::cps(body)))
            .or_insert_with(|| id.clone());
    }
    names.insert(strip_provenance(&unicode::cps(t)), "@t".to_string());
    Value::Array(
        cache
            .keys()
            .iter()
            .map(|k| json!(names.get(k).cloned().unwrap_or_else(|| text(k))))
            .collect(),
    )
}

fn exclude_of(case: &Value) -> Vec<PyStr> {
    strings(&case["ex"])
        .iter()
        .map(|e| unicode::cps(e))
        .collect()
}

/// A case's line as the generator writes it.
pub fn answer(case: &Value) -> Value {
    let mut out = case.clone();
    let cache = RawTokenCache::default();
    match case["k"].as_str().expect("k") {
        "scan" => {
            let t = case["t"].as_str().expect("t");
            let recs = pairs(&case["recs"]);
            let rows: Vec<Rec> = recs.iter().map(|(i, b)| Rec::new(i, b)).collect();
            let r = detect_supersession(&cache, &unicode::cps(t), &rows, &exclude_of(case));
            out["r"] = report_out(&r);
            out["keys"] = keys_out(&cache, t, &recs);
        }
        "in" => {
            let t = case["t"].as_str().expect("t");
            let mut store = Fake::of(case);
            let n = case["n"].as_u64().unwrap_or(1);
            let ex = exclude_of(case);
            let reports: Vec<Value> = (0..n)
                .map(|_| {
                    report_out(&detect_supersession_in(
                        &cache,
                        &mut store,
                        &unicode::cps(t),
                        &ex,
                    ))
                })
                .collect();
            out["r"] = json!(reports);
            out["log"] = json!(store.log);
            out["keys"] = keys_out(&cache, t, &store.recs);
        }
        "one" => {
            let ms: Vec<SupersessionMatch> = case["ms"]
                .as_array()
                .expect("ms")
                .iter()
                .map(|m| SupersessionMatch {
                    memory_id: cps(&m[0]),
                    outcome: cps(&m[1]),
                    evidence: cps(&m[2]),
                    cue: m[3].as_str().map(unicode::cps),
                    new_value: cps(&m[4]),
                    old_value: cps(&m[5]),
                    similarity: 0.5,
                    summary: unicode::cps("s"),
                })
                .collect();
            out["r"] = json!(one_claim_replaced(ms)
                .iter()
                .map(|m| json!([text(&m.memory_id), text(&m.old_value)]))
                .collect::<Vec<_>>());
        }
        other => panic!("no case kind {other}"),
    }
    out
}

/// `gen_pyre_tree.Lcg`.
pub struct Lcg(pub u64);

impl Lcg {
    pub fn draw(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0 >> 33
    }
}

/// `sweep_stores()` from the sweep row's seed, counts, words, separators
/// and stamp.
pub fn sweep_stores(case: &Value) -> Vec<Vec<String>> {
    let (words, seps) = (strings(&case["words"]), strings(&case["seps"]));
    let stamp = case["stamp"].as_str().expect("stamp");
    let count = case["count"].as_u64().expect("count");
    let per = case["bodies"].as_u64().expect("bodies") + 1;
    let mut lcg = Lcg(case["seed"].as_u64().expect("seed"));
    let pick =
        |lcg: &mut Lcg, from: &[String]| from[(lcg.draw() % from.len() as u64) as usize].clone();
    (0..count)
        .map(|_| {
            (0..per)
                .map(|_| {
                    let n = 3 + lcg.draw() % 10;
                    let mut body = pick(&mut lcg, &words);
                    for _ in 1..n {
                        body.push_str(&pick(&mut lcg, &seps));
                        body.push_str(&pick(&mut lcg, &words));
                    }
                    if lcg.draw().is_multiple_of(9) {
                        body.push_str(stamp);
                    }
                    body
                })
                .collect()
        })
        .collect()
}

/// The shares a store's digest holds for `recs` (`_shares`).
pub fn derived_shares(t: &str, recs: &[(String, String)]) -> Vec<DigestShare> {
    let new_raw = raw_content_token_set(&strip_provenance(&unicode::cps(t)));
    let asked = expanded(&new_raw);
    let mut out = Vec::new();
    for (id, body) in recs {
        let f = features(&unicode::cps(body));
        let shared = asked.intersection(&expanded(&f.tokens)).count();
        if f.claim > 0 && shared > 0 {
            out.push(share(id, f.claim as i64, f.stamped, shared as i64));
        }
    }
    out
}

fn compact(r: &Value) -> Value {
    json!([r["el"], r["sup"], r["con"]])
}

/// One sweep store's line: `[scan, in, log]`, compact.
pub fn sweep_line(bodies: &[String]) -> String {
    let t = &bodies[0];
    let recs: Vec<(String, String)> = bodies[1..]
        .iter()
        .enumerate()
        .map(|(i, b)| (format!("r{i:02}"), b.clone()))
        .collect();
    let rows: Vec<Rec> = recs.iter().map(|(i, b)| Rec::new(i, b)).collect();
    let scan = report_out(&detect_supersession(
        &RawTokenCache::default(),
        &unicode::cps(t),
        &rows,
        &[],
    ));
    let mut store = Fake::new(t, recs.clone(), Some(derived_shares(t, &recs)));
    let got = report_out(&detect_supersession_in(
        &RawTokenCache::default(),
        &mut store,
        &unicode::cps(t),
        &[],
    ));
    serde_json::to_string(&json!([compact(&scan), compact(&got), store.log])).expect("json")
}
