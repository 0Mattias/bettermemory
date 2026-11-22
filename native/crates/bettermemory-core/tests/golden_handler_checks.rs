//! Every case of `handler_checks.jsonl` against `handler_checks`: the
//! scope count, the content size and floor, `validate_scope`, the write
//! payload, and the digest of the seeded payloads.

mod common;

use bettermemory_core::handler_checks::{
    validate_content_floor, validate_content_size, validate_scope, validate_scope_count,
    validate_write_payload, CheckError, WriteArgs, WritePayload,
};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::{cps, to_string};
use common::{assert_count, jsonl};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// The generator's `V` read back: a text.
fn t(v: &Value) -> PyStr {
    match v {
        Value::String(s) => cps(s),
        _ => common::cps_of(&v["$cps"]),
    }
}

fn texts(v: &Value) -> Vec<PyStr> {
    v.as_array().expect("a list").iter().map(t).collect()
}

fn pairs(v: &Value) -> Vec<(PyStr, Value)> {
    v["$d"]
        .as_array()
        .expect("a dict")
        .iter()
        .map(|p| (t(&p[0]), p[1].clone()))
        .collect()
}

fn get<'a>(d: &'a [(PyStr, Value)], k: &str) -> &'a Value {
    &d.iter().find(|(key, _)| *key == cps(k)).expect(k).1
}

/// The generator's `V` of a text.
fn w(text: &[u32]) -> Value {
    match to_string(text) {
        Some(s) => json!(s),
        None => json!({ "$cps": text }),
    }
}

fn wl(items: &[PyStr]) -> Value {
    Value::Array(items.iter().map(|s| w(s)).collect())
}

fn err(e: &CheckError) -> Value {
    let cls = match e {
        CheckError::Value(_) => "Value",
        CheckError::Encode(_) => "Encode",
    };
    json!({"err": cls, "m": w(e.message())})
}

fn unit(r: Result<(), CheckError>) -> Value {
    match r {
        Ok(()) => json!({ "ok": null }),
        Err(e) => err(&e),
    }
}

fn payload_value(p: &WritePayload) -> Value {
    json!({"ok": {"$d": [
        ["content", w(&p.content)],
        ["scopes", wl(&p.scopes)],
        ["confidence", p.confidence.value()],
        ["source", p.source.value()],
        ["category", p.category.value()],
    ]}})
}

/// The generator's `_draws`.
struct Lcg(u64);

impl Lcg {
    fn next(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0 >> 33
    }
    fn pick<'a, T>(&mut self, pool: &'a [T]) -> &'a T {
        &pool[(self.next() % pool.len() as u64) as usize]
    }
}

fn digest(r: &Value) {
    let pools = r["pools"].as_array().expect("pools");
    let alpha = texts(&pools[0]);
    let scopes = texts(&pools[1]);
    let conf = texts(&pools[2]);
    let src = texts(&pools[3]);
    let cat = texts(&pools[4]);
    let ints = |i: usize| -> Vec<i64> {
        pools[i]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_i64().unwrap())
            .collect()
    };
    let (maxb, mint, maxs) = (ints(5), ints(6), ints(7));
    let n = r["n"].as_u64().expect("n");
    let mut g = Lcg(r["seed"].as_u64().expect("seed"));
    let mut answers: Vec<String> = Vec::new();
    for _ in 0..n {
        let len = g.next() % 12;
        let content: PyStr = (0..len).flat_map(|_| g.pick(&alpha).clone()).collect();
        let k = g.next() % 4;
        let sc: Vec<PyStr> = (0..k).map(|_| g.pick(&scopes).clone()).collect();
        let a = g.next() % 3;
        let allowed: Vec<PyStr> = (0..a).map(|_| g.pick(&scopes).clone()).collect();
        let confidence = g.pick(&conf).clone();
        let source = g.pick(&src).clone();
        let category = g.pick(&cat).clone();
        let (b, m, s) = (*g.pick(&maxb), *g.pick(&mint), *g.pick(&maxs));
        let capture = g.next() % 2 == 1;
        let out = validate_write_payload(&WriteArgs {
            content: &content,
            scopes: &sc,
            confidence: &confidence,
            source: &source,
            allowed_scopes: &allowed,
            category: &category,
            max_content_bytes: b,
            min_content_tokens: m,
            max_scopes_per_write: s,
            capture,
        });
        answers.push(match out {
            Ok(p) => {
                let mut parts = vec![
                    p.confidence.value().to_owned(),
                    p.source.value().to_owned(),
                    p.category.value().to_owned(),
                ];
                parts.extend(p.scopes.iter().map(|s| to_string(s).unwrap()));
                format!("ok:{}", parts.join("|"))
            }
            Err(e) => {
                let cls = if matches!(e, CheckError::Encode(_)) {
                    "Encode"
                } else {
                    "Value"
                };
                format!(
                    "{cls}:{}",
                    to_string(e.message()).expect("no surrogate in a message")
                )
            }
        });
    }
    let sha: String = Sha256::digest(answers.join("\n").as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    assert_eq!(answers.len() as u64, n);
    assert_eq!(sha, r["sha"].as_str().unwrap(), "digest of {n} payloads");
}

#[test]
fn every_case_matches_the_reference() {
    let all = jsonl("handler_checks.jsonl");
    let rows = &all[1..];
    assert_eq!(rows.len() as u64, all[0]["cases"].as_u64().unwrap());
    let mut checked = 0;
    for (i, r) in rows.iter().enumerate() {
        let i64_of = |k: &str| r[k].as_i64().expect("an int");
        let got = match r["k"].as_str().unwrap() {
            "count" => unit(validate_scope_count(
                r["n"].as_u64().unwrap() as usize,
                i64_of("max"),
            )),
            "size" => unit(validate_content_size(
                &t(&r["c"]),
                i64_of("max"),
                &t(&r["f"]),
            )),
            "floor" => unit(validate_content_floor(&t(&r["c"]), i64_of("min"))),
            "scope" => match validate_scope(&t(&r["s"])) {
                Ok(s) => json!({ "ok": w(&s) }),
                Err(e) => err(&e),
            },
            "pay" => {
                let d = pairs(&r["a"]);
                let text = |k: &str| t(get(&d, k));
                let list = |k: &str| texts(get(&d, k));
                let int = |k: &str| get(&d, k).as_i64().expect("an int");
                let (content, confidence, source, category) = (
                    text("content"),
                    text("confidence"),
                    text("source"),
                    text("category"),
                );
                let (scopes, allowed) = (list("scopes"), list("allowed_scopes"));
                let out = validate_write_payload(&WriteArgs {
                    content: &content,
                    scopes: &scopes,
                    confidence: &confidence,
                    source: &source,
                    allowed_scopes: &allowed,
                    category: &category,
                    max_content_bytes: int("max_content_bytes"),
                    min_content_tokens: int("min_content_tokens"),
                    max_scopes_per_write: int("max_scopes_per_write"),
                    capture: get(&d, "capture").as_bool().expect("a bool"),
                });
                match out {
                    Ok(p) => payload_value(&p),
                    Err(e) => err(&e),
                }
            }
            "digest" => {
                digest(r);
                checked += 1;
                continue;
            }
            other => panic!("unknown row kind {other}"),
        };
        assert_eq!(got, r["o"], "row {i}: {r}");
        checked += 1;
    }
    assert_count("handler_checks.jsonl", checked, rows.len());
}
