//! Reading and answering the cases of `gen_search_dedup`: its records,
//! its scripted store, and each answer spelled as the generator spells it.

use std::collections::HashMap;
use std::sync::Arc;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::records::Confidence;
use bettermemory_core::search_dedup::{
    content_token_set, find_similar, find_similar_in, find_similar_tombstones,
    find_similar_tombstones_in, pairwise_content_jaccard, raw_content_token_set, similarity_bound,
    snippet_for, Candidate, Computed, DedupError, DigestDoc, DigestStore, MemoKey, RawTokenCache,
    SimilarHit, TokenSet,
};
use bettermemory_core::unicode;
use serde_json::{json, Value};

/// A text: a string, or `{"cps": [...]}` where it holds a lone surrogate.
pub fn text(v: &Value) -> PyStr {
    match v {
        Value::String(s) => unicode::cps(s),
        other => super::cps_of(&other["cps"]),
    }
}

pub fn text_out(t: &[u32]) -> Value {
    match unicode::to_string(t) {
        Some(s) => json!(s),
        None => json!({ "cps": t }),
    }
}

pub fn bits(x: f64) -> String {
    format!("{:016x}", x.to_bits())
}

pub fn from_bits(v: &Value) -> Option<f64> {
    v.as_str()
        .map(|s| f64::from_bits(u64::from_str_radix(s, 16).expect("hex bits")))
}

fn dt(v: &Value) -> DateTime {
    DateTime::from_isoformat(&unicode::cps(v.as_str().expect("isoformat"))).expect("a datetime")
}

/// A record as the cases give it.
#[derive(Clone, Debug)]
pub struct Rec {
    pub id: PyStr,
    pub body: PyStr,
    pub scopes: Vec<PyStr>,
    pub confidence: Confidence,
    pub created: DateTime,
    pub updated: DateTime,
    pub removed: Option<DateTime>,
    pub removed_reason: Option<PyStr>,
    pub extra: bool,
}

impl Rec {
    pub fn of(v: &Value) -> Rec {
        let conf = v["conf"].as_str().expect("conf");
        Rec {
            id: text(&v["id"]),
            body: text(&v["b"]),
            scopes: v["s"]
                .as_array()
                .expect("scopes")
                .iter()
                .map(text)
                .collect(),
            confidence: Confidence::MEMBERS
                .iter()
                .find(|(n, _)| *n == conf)
                .expect("a confidence")
                .1,
            created: dt(&v["c"]),
            updated: dt(&v["u"]),
            removed: v.get("rm").map(dt),
            removed_reason: v.get("rr").map(text),
            extra: v.get("x").is_some_and(|x| x.as_bool() == Some(true)),
        }
    }
}

impl Candidate for Rec {
    fn id(&self) -> &[u32] {
        &self.id
    }
    fn scopes(&self) -> &[PyStr] {
        &self.scopes
    }
    fn confidence(&self) -> Confidence {
        self.confidence
    }
    fn body(&self) -> &[u32] {
        &self.body
    }
    fn created(&self) -> DateTime {
        self.created
    }
    fn updated(&self) -> DateTime {
        self.updated
    }
    fn removed(&self) -> Option<DateTime> {
        self.removed
    }
    fn removed_reason(&self) -> Option<&[u32]> {
        self.removed_reason.as_deref()
    }
}

fn recs(v: &Value) -> Vec<Rec> {
    v.as_array().expect("records").iter().map(Rec::of).collect()
}

fn set_of(v: &Value) -> TokenSet {
    v.as_array()
        .expect("tokens")
        .iter()
        .map(|t| t.as_str().expect("a token").to_owned())
        .collect()
}

pub fn hit_out(h: &SimilarHit) -> Value {
    json!({
        "c": h.created.isoformat(),
        "conf": h.confidence.value(),
        "id": text_out(&h.id),
        "rel": h.relevance,
        "rm": h.removed_at.map(|d| d.isoformat()),
        "rr": h.removed_reason.as_deref().map(text_out),
        "s": h.scopes.iter().map(|s| text_out(s)).collect::<Vec<_>>(),
        "sim": bits(h.similarity),
        "snip": text_out(&h.snippet),
        "u": h.updated.isoformat(),
    })
}

pub fn answer_out(r: Result<Vec<SimilarHit>, DedupError>) -> Value {
    match r {
        Ok(hits) => Value::Array(hits.iter().map(hit_out).collect()),
        Err(e) => json!({ "err": e.class() }),
    }
}

fn sorted(set: &TokenSet) -> Value {
    json!(set.iter().collect::<Vec<_>>())
}

fn keys_out(cache: &RawTokenCache) -> Value {
    Value::Array(cache.keys().iter().map(|k| text_out(k)).collect())
}

/// The generator's scripted store.
pub struct Fake<'a> {
    pub case: &'a Value,
    pub log: Vec<Value>,
    held: HashMap<MemoKey, Vec<SimilarHit>>,
}

impl<'a> Fake<'a> {
    pub fn new(case: &'a Value) -> Fake<'a> {
        Fake {
            case,
            log: Vec::new(),
            held: HashMap::new(),
        }
    }
}

impl DigestStore for Fake<'_> {
    type Light = Rec;
    type Active = Rec;
    type Removed = Rec;

    fn digest_candidates(
        &mut self,
        tokens: &TokenSet,
        removed: bool,
        floor: f64,
    ) -> Option<Vec<DigestDoc>> {
        self.log
            .push(json!(["dc", sorted(tokens), removed, bits(floor)]));
        let docs = self.case["docs"].as_array()?;
        Some(
            docs.iter()
                .map(|d| DigestDoc {
                    id: text(&d[0]),
                    tokens: Arc::new(set_of(&d[1])),
                })
                .collect(),
        )
    }

    fn light_records(&mut self, ids: &[PyStr], removed: bool) -> Vec<Rec> {
        let wanted: Vec<Value> = ids.iter().map(|i| text_out(i)).collect();
        self.log.push(json!(["lr", wanted, removed]));
        recs(&self.case["light"])
            .into_iter()
            .filter(|r| r.extra || ids.contains(&r.id))
            .collect()
    }

    fn digest_memo(
        &mut self,
        key: &MemoKey,
        compute: &mut dyn FnMut(&mut Self) -> Computed,
    ) -> Computed {
        self.log.push(json!(["memo", key.removed]));
        if self.case["memo"] == "none" {
            return Ok(None);
        }
        if let Some(h) = self.held.get(key) {
            return Ok(Some(h.clone()));
        }
        let answer = compute(self)?;
        if let Some(h) = &answer {
            self.held.insert(key.clone(), h.clone());
        }
        Ok(answer)
    }

    fn load_all(&mut self) -> Vec<Rec> {
        self.log.push(json!(["all"]));
        recs(&self.case["all"])
    }

    fn load_tombstones(&mut self) -> Vec<Rec> {
        self.log.push(json!(["tomb"]));
        recs(&self.case["tomb"])
    }
}

/// The case answered natively, spelled as the generator spells it.
pub fn answer(case: &Value) -> Value {
    let mut out = case.clone();
    let k = case["k"].as_str().expect("a kind");
    match k {
        "tok" => {
            let t = text(&case["t"]);
            out["raw"] = sorted(&raw_content_token_set(&t));
            out["content"] = sorted(&content_token_set(&t));
        }
        "jac" => {
            let r = pairwise_content_jaccard(&set_of(&case["a"]), &set_of(&case["b"]));
            out["r"] = json!(bits(r));
        }
        "bound" => {
            let a: Vec<i64> = case["a"]
                .as_array()
                .expect("args")
                .iter()
                .map(|x| x.as_i64().expect("an i64"))
                .collect();
            out["r"] = json!(bits(similarity_bound(a[0], a[1], a[2])));
        }
        "snip" => out["r"] = text_out(&snippet_for(&text(&case["t"]))),
        "cache" => {
            let cache = RawTokenCache::with_entries(case["entries"].as_i64().expect("entries"));
            let answers: Vec<Value> = case["seq"]
                .as_array()
                .expect("seq")
                .iter()
                .map(|t| match cache.tokens(&text(t)) {
                    Ok(s) => sorted(&s),
                    Err(e) => json!({ "err": e.class() }),
                })
                .collect();
            out["r"] = Value::Array(answers);
            out["keys"] = keys_out(&cache);
        }
        "fs" | "ft" => {
            let cache = RawTokenCache::default();
            let records = recs(&case["recs"]);
            let (hi, me) = (from_bits(&case["hi"]), from_bits(&case["me"]));
            let body = text(&case["t"]);
            let r = if k == "fs" {
                find_similar(&cache, &body, &records, hi, me)
            } else {
                find_similar_tombstones(&cache, &body, &records, hi, me)
            };
            out["r"] = answer_out(r);
            out["keys"] = keys_out(&cache);
        }
        "in" => {
            let cache = RawTokenCache::default();
            let mut store = Fake::new(case);
            let body = text(&case["t"]);
            let n = case.get("n").and_then(Value::as_u64).unwrap_or(1);
            let removed = case["removed"].as_bool().expect("removed");
            let answers: Vec<Value> = (0..n)
                .map(|_| {
                    answer_out(if removed {
                        find_similar_tombstones_in(&cache, &mut store, &body)
                    } else {
                        find_similar_in(&cache, &mut store, &body)
                    })
                })
                .collect();
            out["r"] = Value::Array(answers);
            out["log"] = Value::Array(store.log);
            out["keys"] = keys_out(&cache);
        }
        other => panic!("unknown kind {other}"),
    }
    out
}
