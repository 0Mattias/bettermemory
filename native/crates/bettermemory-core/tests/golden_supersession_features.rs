//! Every case of `supersession_features.jsonl` and
//! `supersession_features_unicode.jsonl` against `supersession`'s
//! per-body features: each body's cues, anchored values, proper nouns,
//! value and anchor tokens, numeric tokens, surface forms, contexts and
//! claim size, each pair's kinship and shapes, each match's note and
//! detector, and the seeded sweep by count and digest.

mod common;

use std::collections::BTreeSet;

use bettermemory_core::search_dedup::raw_content_token_set;
use bettermemory_core::supersession::{
    anchor_tokens, anchored_values, change_cues, claim_sized, content_stream, context, cue_pattern,
    cue_tokens, numeric_shape, numeric_token_set, proper_nouns, strip_provenance, surface_form,
    surface_forms, value_tokens, values_are_kin, SupersessionMatch, CHANGE_CUES, CONTEXT_WIDTH,
    MAX_CLAIM_SENTENCES, MAX_CLAIM_TOKENS, MAX_LINKS_PER_WRITE, MIN_CONFLICT_JACCARD,
    MIN_CONFLICT_KIN_JACCARD, MIN_CONTEXT_SHARED, MIN_JACCARD, VALUE_PREPS, WORD_TOKEN_ENTRIES,
};
use bettermemory_core::unicode;
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

fn text(t: &[u32]) -> String {
    unicode::to_string(t).expect("no surrogate in these fixtures")
}

fn cps(v: &Value) -> Vec<u32> {
    unicode::cps(v.as_str().expect("a string"))
}

fn set(s: &BTreeSet<String>) -> Value {
    json!(s.iter().collect::<Vec<_>>())
}

fn cues(t: &[u32]) -> Value {
    json!(change_cues(t)
        .expect("cues")
        .iter()
        .map(|c| text(c))
        .collect::<Vec<_>>())
}

fn forms(t: &[u32]) -> Value {
    json!(surface_forms(t)
        .expect("surface forms")
        .iter()
        .map(|(tok, form)| json!([tok, text(form)]))
        .collect::<Vec<_>>())
}

/// A body row's answers, as the generator's `_body` writes them.
fn body(t: &[u32], full: bool) -> Map<String, Value> {
    let raw = raw_content_token_set(&strip_provenance(t));
    let vt = value_tokens(t, &raw).expect("value tokens");
    let anch = anchored_values(t).expect("anchored values");
    let mut out = Map::new();
    out.insert("cues".into(), cues(t));
    out.insert("anch".into(), set(&anch));
    out.insert("pn".into(), set(&proper_nouns(t).expect("proper nouns")));
    out.insert("vt".into(), set(&vt));
    out.insert(
        "at".into(),
        set(&anchor_tokens(t, &raw).expect("anchor tokens")),
    );
    out.insert(
        "num".into(),
        set(&numeric_token_set(t).expect("numeric tokens")),
    );
    out.insert("claim".into(), json!(claim_sized(t)));
    if full {
        let stream = content_stream(t);
        out.insert("raw".into(), set(&raw));
        out.insert("sf".into(), forms(t));
        let mut ctx = Map::new();
        for v in vt.union(&anch) {
            ctx.insert(v.clone(), set(&context(&stream, v)));
        }
        out.insert("ctx".into(), Value::Object(ctx));
        let mut one = Map::new();
        for v in vt.iter().map(String::as_str).chain(["zz-none"]) {
            let form = surface_form(t, v).expect("surface form");
            one.insert(v.to_string(), json!(form.map(|f| text(&f))));
        }
        out.insert("one".into(), Value::Object(one));
    }
    out
}

fn shape(t: &str) -> Value {
    json!(numeric_shape(&unicode::cps(t)).expect("shape"))
}

/// `gen_pyre_tree.Lcg`.
struct Lcg(u64);

impl Lcg {
    fn draw(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0 >> 33
    }
}

/// `sweep_bodies()` from the case's seed, count, words and separators.
fn sweep_bodies(case: &Value) -> Vec<String> {
    let strings = |k: &str| -> Vec<String> {
        case[k]
            .as_array()
            .expect("a list")
            .iter()
            .map(|w| w.as_str().expect("a string").to_string())
            .collect()
    };
    let (words, seps) = (strings("words"), strings("seps"));
    let mut lcg = Lcg(case["seed"].as_u64().expect("seed"));
    let count = case["count"].as_u64().expect("count");
    let pick =
        |lcg: &mut Lcg, from: &[String]| from[(lcg.draw() % from.len() as u64) as usize].clone();
    (0..count)
        .map(|_| {
            let n = 2 + lcg.draw() % 14;
            let mut body = pick(&mut lcg, &words);
            for _ in 1..n {
                body.push_str(&pick(&mut lcg, &seps));
                body.push_str(&pick(&mut lcg, &words));
            }
            body
        })
        .collect()
}

fn sweep(case: &Value) -> Value {
    let bodies = sweep_bodies(case);
    let mut digest = Sha256::new();
    for b in &bodies {
        let t = unicode::cps(b);
        let raw = raw_content_token_set(&t);
        let line = json!([
            cues(&t),
            set(&anchored_values(&t).expect("anchored")),
            set(&proper_nouns(&t).expect("proper nouns")),
            set(&value_tokens(&t, &raw).expect("value tokens")),
            set(&anchor_tokens(&t, &raw).expect("anchor tokens")),
            set(&numeric_token_set(&t).expect("numeric")),
            forms(&t),
        ]);
        digest.update(serde_json::to_string(&line).expect("json").as_bytes());
        digest.update(b"\n");
    }
    let mut out = case.clone();
    out["count"] = json!(bodies.len());
    out["sha256"] = json!(common::hex(&digest.finalize()));
    out
}

fn answer(case: &Value) -> Value {
    match case["k"].as_str().expect("a kind") {
        "b" => {
            let (t, full) = match case.get("t") {
                Some(t) => (cps(t), true),
                None => {
                    let mut t = Vec::new();
                    for run in case["rep"].as_array().expect("runs") {
                        let piece = cps(&run[0]);
                        for _ in 0..run[1].as_u64().expect("times") {
                            t.extend_from_slice(&piece);
                        }
                    }
                    (t, false)
                }
            };
            let mut out = body(&t, full);
            out.insert("k".into(), json!("b"));
            for k in ["t", "rep"] {
                if let Some(v) = case.get(k) {
                    out.insert(k.into(), v.clone());
                }
            }
            Value::Object(out)
        }
        "kin" => {
            let (a, b) = (
                case["a"].as_str().expect("a"),
                case["b"].as_str().expect("b"),
            );
            json!({
                "k": "kin", "a": a, "b": b,
                "r": values_are_kin(a, b).expect("kin"),
                "sa": shape(a), "sb": shape(b),
            })
        }
        "note" => {
            let m = SupersessionMatch {
                memory_id: unicode::cps("m1"),
                outcome: unicode::cps("supersedes"),
                evidence: cps(&case["ev"]),
                cue: case["cue"].as_str().map(unicode::cps),
                new_value: cps(&case["new"]),
                old_value: cps(&case["old"]),
                similarity: 0.5,
                summary: unicode::cps("s"),
            };
            let mut out = case.clone();
            out["note"] = json!(text(&m.note()));
            out["det"] = json!(m.detector().expect("detector"));
            out
        }
        "sweep" => sweep(case),
        other => panic!("an unknown kind {other}"),
    }
}

fn header_holds(header: &Value) {
    assert_eq!(header["cue_pattern"], json!(text(&cue_pattern())));
    assert_eq!(header["cue_tokens"], set(cue_tokens()));
    let table: Vec<Value> = CHANGE_CUES.iter().map(|(c, d)| json!([c, d])).collect();
    assert_eq!(header["cues"], json!(table));
    let mut preps = VALUE_PREPS.to_vec();
    preps.sort_unstable();
    assert_eq!(header["preps"], json!(preps));
    assert_eq!(header["entries"], json!(WORD_TOKEN_ENTRIES));
    assert_eq!(
        header["consts"],
        json!([
            MAX_CLAIM_TOKENS,
            MAX_CLAIM_SENTENCES,
            CONTEXT_WIDTH,
            MIN_CONTEXT_SHARED,
            MAX_LINKS_PER_WRITE
        ])
    );
    assert_eq!(
        header["floors"],
        json!([MIN_JACCARD, MIN_CONFLICT_JACCARD, MIN_CONFLICT_KIN_JACCARD])
    );
}

fn every_case(name: &str, test: &str) {
    let lines = jsonl(name);
    let header = &lines[0];
    header_holds(header);
    let in_file = header["cases"].as_u64().expect("cases") as usize;
    assert_eq!(in_file, lines.len() - 1, "{name}: header count");
    let mut ledger = Ledger::new(test, name);
    for (i, case) in lines[1..].iter().enumerate() {
        ledger.case(format!("case {i} ({})", case["k"]), || {
            assert_eq!(answer(case), *case, "{name} case {i}");
        });
    }
    ledger.finish(in_file);
}

#[test]
fn supersession_features_every_case() {
    every_case(
        "supersession_features.jsonl",
        "supersession_features_every_case",
    );
}

#[test]
fn supersession_features_unicode_every_case() {
    every_case(
        "supersession_features_unicode.jsonl",
        "supersession_features_unicode_every_case",
    );
}
