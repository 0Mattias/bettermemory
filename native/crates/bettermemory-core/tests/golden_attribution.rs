//! Every case of `attribution.jsonl` (not gated) and
//! `attribution_unicode.jsonl` (gated on the Unicode version) against the
//! native `attribution`. The spelling is the generator's
//! (`tests/contract/gen_attribution.py`).

mod common;

use bettermemory_core::attribution as at;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode;
use common::parity::Ledger;
use serde_json::{json, Value};

/// A text: a string, `{"c": cps}` or `{"r": [[text, n], ...]}`.
fn text(v: &Value) -> PyStr {
    if let Some(s) = v.as_str() {
        return unicode::cps(s);
    }
    if let Some(c) = v.get("c") {
        return c
            .as_array()
            .expect("code points")
            .iter()
            .map(|x| x.as_u64().expect("a code point") as u32)
            .collect();
    }
    let mut out = Vec::new();
    for pair in v["r"].as_array().expect("a recipe") {
        let piece = text(&pair[0]);
        for _ in 0..pair[1].as_u64().expect("a count") {
            out.extend_from_slice(&piece);
        }
    }
    out
}

/// A text as the generator spells an output.
fn spelled(t: &[u32]) -> Value {
    match unicode::to_string(t) {
        Some(s) => json!(s),
        None => json!({ "c": t }),
    }
}

fn answer(case: &Value) -> Value {
    match case["f"].as_str().expect("a function") {
        "cand" => Value::Array(
            at::candidate_sentences(&text(&case["a"]))
                .iter()
                .map(|s| spelled(s))
                .collect(),
        ),
        "norm" => spelled(&at::normalize(&text(&case["a"]))),
        "tok" => Value::Array(
            at::content_token_set(&text(&case["a"]))
                .into_iter()
                .map(|t| json!(String::from_utf8(t).expect("ASCII")))
                .collect(),
        ),
        "match" => {
            let reply = text(&case["a"][1]);
            json!(at::sentence_matches(
                &text(&case["a"][0]),
                &at::normalize(&reply),
                &at::content_token_set(&reply),
            ))
        }
        "uses" => {
            let memories: Vec<(PyStr, PyStr)> = case["m"]
                .as_array()
                .expect("memories")
                .iter()
                .map(|p| (text(&p[0]), text(&p[1])))
                .collect();
            Value::Array(
                at::attribute_uses(&memories, &text(&case["t"]))
                    .iter()
                    .map(|m| json!([spelled(&m.memory_id), spelled(&m.claim_excerpt)]))
                    .collect(),
            )
        }
        other => panic!("unknown function {other}"),
    }
}

fn run(test: &str, file: &str) {
    let lines = common::jsonl(file);
    let header = &lines[0];
    assert_eq!(header["k"], "header");
    let count = header["count"].as_u64().expect("a count") as usize;
    let mut ledger = Ledger::new(test, file);
    for (i, case) in lines[1..].iter().enumerate() {
        let got = answer(case);
        let label = format!("line {}", i + 2);
        assert!(
            !ledger.differs_fatally(&label, got == case["o"]),
            "{label} {case}: got {got}"
        );
    }
    assert_eq!(lines.len() - 1, count);
    ledger.finish(count);
}

#[test]
fn golden_attribution() {
    let header = &common::jsonl("attribution.jsonl")[0];
    let words: Vec<Value> = at::STOPWORDS.iter().map(|w| json!(w)).collect();
    assert_eq!(header["stopwords"], Value::Array(words));
    let limits = &header["limits"];
    assert_eq!(limits["MIN_TOKEN_COUNT"], json!(at::MIN_TOKEN_COUNT));
    assert_eq!(limits["MIN_CHAR_COUNT"], json!(at::MIN_CHAR_COUNT));
    assert_eq!(limits["MAX_EXCERPT_CHARS"], json!(at::MAX_EXCERPT_CHARS));
    assert_eq!(limits["MIN_CONTAINMENT"], json!(at::MIN_CONTAINMENT));
    assert_eq!(
        limits["MIN_CONTAINMENT_TOKENS"],
        json!(at::MIN_CONTAINMENT_TOKENS)
    );
    assert_eq!(limits["STOPWORD_RATIO_MAX"], json!(at::STOPWORD_RATIO_MAX));
    run("golden_attribution", "attribution.jsonl");
}

#[test]
fn golden_attribution_unicode() {
    run("golden_attribution_unicode", "attribution_unicode.jsonl");
}
