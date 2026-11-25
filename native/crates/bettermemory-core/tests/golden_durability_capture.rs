//! Every case of `durability_capture.jsonl` (not gated) and
//! `durability_capture_unicode.jsonl` (gated on the Unicode version)
//! against the native `durability` capture rules, unit 3: each pattern's
//! text and flags, `find_capture_rule_hits` with `to_dict`,
//! `_unanchored_test_total`, the hostile bodies, and the seeded sweep by
//! count and digest. The spelling of rows is the generator's
//! (`tests/contract/gen_durability.py`).

mod common;

use std::collections::BTreeMap;

use bettermemory_core::durability as d;
use bettermemory_core::pyre::regex::Regex;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode;
use common::parity::Ledger;
use serde_json::Value;
use sha2::{Digest, Sha256};

fn text(record: &Value, name: &str) -> PyStr {
    common::text_field(record, name)
}

fn plain(record: &Value, name: &str) -> PyStr {
    unicode::cps(record[name].as_str().unwrap())
}

/// The hits as the generator writes them, through `to_dict`.
fn hits_value(hits: &[d::CaptureRuleHit]) -> Vec<(i64, String, PyStr)> {
    hits.iter()
        .map(|h| {
            let dict = h.to_dict();
            let PyValue::Dict(entries) = &dict else {
                panic!("to_dict is a dict");
            };
            let keys: Vec<PyValue> = entries.iter().map(|(k, _)| k.clone()).collect();
            assert_eq!(
                keys,
                vec![
                    PyValue::str("rule"),
                    PyValue::str("name"),
                    PyValue::str("snippet")
                ]
            );
            let PyValue::Int(rule) = &entries[0].1 else {
                panic!("rule is an int");
            };
            let PyValue::Str(name) = &entries[1].1 else {
                panic!("name is a str");
            };
            let PyValue::Str(snippet) = &entries[2].1 else {
                panic!("snippet is a str");
            };
            assert_eq!(name, &unicode::cps(h.name));
            (
                rule.parse().unwrap(),
                unicode::to_string(name).unwrap(),
                snippet.clone(),
            )
        })
        .collect()
}

fn want_hits(v: &Value) -> Vec<(i64, String, PyStr)> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|h| {
            (
                h["rule"].as_i64().unwrap(),
                h["name"].as_str().unwrap().to_owned(),
                text(h, "s"),
            )
        })
        .collect()
}

fn check_hits(body: &[u32], want: &Value) {
    let got = d::find_capture_rule_hits(body).expect("every pattern builds");
    assert_eq!(hits_value(&got), want_hits(want));
}

fn lcg(state: u64) -> u64 {
    state
        .wrapping_mul(6364136223846793005)
        .wrapping_add(1442695040888963407)
}

fn pick(state: u64, len: usize) -> usize {
    ((state >> 33) % len as u64) as usize
}

/// The generator's `capture_sweep_bodies`.
fn sweep_bodies(r: &Value) -> Vec<PyStr> {
    let list = |name: &str| -> Vec<PyStr> {
        r[name]
            .as_array()
            .unwrap()
            .iter()
            .map(|w| unicode::cps(w.as_str().unwrap()))
            .collect()
    };
    let (words, seps) = (list("words"), list("seps"));
    let most = r["most"].as_u64().unwrap();
    let count = r["count"].as_u64().unwrap();
    let mut state = r["seed"].as_u64().unwrap();
    (0..count)
        .map(|_| {
            state = lcg(state);
            let n = 1 + (state >> 33) % most;
            let mut body = Vec::new();
            for _ in 0..n {
                state = lcg(state);
                body.extend_from_slice(&words[pick(state, words.len())]);
                state = lcg(state);
                body.extend_from_slice(&seps[pick(state, seps.len())]);
            }
            body
        })
        .collect()
}

fn check_sweep(r: &Value) {
    let bodies = sweep_bodies(r);
    let mut h = Sha256::new();
    let mut rules: BTreeMap<String, u64> = BTreeMap::new();
    for body in &bodies {
        for hit in d::find_capture_rule_hits(body).expect("every pattern builds") {
            *rules.entry(hit.name.to_owned()).or_default() += 1;
            let line = format!(
                "{}|{}|{}\n",
                hit.rule,
                hit.name,
                unicode::to_string(&hit.snippet).expect("the sweep's words hold no surrogate")
            );
            h.update(line.as_bytes());
        }
        h.update([0u8]);
    }
    assert_eq!(bodies.len() as u64, r["count"].as_u64().unwrap());
    let want: BTreeMap<String, u64> = r["rules"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.clone(), v.as_u64().unwrap()))
        .collect();
    assert_eq!(rules, want, "bodies per rule");
    assert_eq!(common::hex(&h.finalize()), r["sha"].as_str().unwrap());
}

fn check_pattern(r: &Value) {
    let id = r["id"].as_str().unwrap();
    let &(_, pattern, flags) = d::CAPTURE_PATTERNS
        .iter()
        .find(|(name, _, _)| *name == id)
        .unwrap_or_else(|| panic!("no pattern {id}"));
    assert_eq!(pattern, r["p"].as_str().unwrap(), "{id}");
    let built = Regex::new_str(pattern, flags).expect("the engine builds it");
    assert_eq!(u64::from(built.flags()), r["f"].as_u64().unwrap(), "{id}");
}

fn check(r: &Value) {
    match r["k"].as_str().unwrap() {
        "pat" => check_pattern(r),
        "hits" => check_hits(&text(r, "t"), &r["o"]),
        "tt" => assert_eq!(
            d::unanchored_test_total(&plain(r, "t")).expect("every pattern builds"),
            r["o"].as_bool().unwrap()
        ),
        "rep" => {
            let n = r["n"].as_u64().unwrap() as usize;
            let body = [
                plain(r, "head"),
                plain(r, "unit").repeat(n),
                plain(r, "tail"),
            ]
            .concat();
            check_hits(&body, &r["o"]);
        }
        "sweep" => check_sweep(r),
        other => panic!("unknown row kind {other}"),
    }
}

fn run(test: &str, file: &str) -> Vec<String> {
    let lines = common::jsonl(file);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64().unwrap() as usize, rows.len());
    assert!(!rows.is_empty());
    let mut ledger = Ledger::new(test, file);
    for (i, r) in rows.iter().enumerate() {
        let shown: String = r.to_string().chars().take(200).collect();
        ledger.case(format!("line {} {shown}", i + 2), || check(r));
    }
    ledger.finish(rows.len());
    rows.iter()
        .filter(|r| r["k"] == "pat")
        .map(|r| r["id"].as_str().unwrap().to_owned())
        .collect()
}

#[test]
fn durability_capture_every_case() {
    let ids = run("durability_capture_every_case", "durability_capture.jsonl");
    let mine: Vec<&str> = d::CAPTURE_PATTERNS.iter().map(|(id, _, _)| *id).collect();
    assert_eq!(ids, mine, "the file pins every pattern, in order");
}

#[test]
fn durability_capture_unicode_every_case() {
    run(
        "durability_capture_unicode_every_case",
        "durability_capture_unicode.jsonl",
    );
}
