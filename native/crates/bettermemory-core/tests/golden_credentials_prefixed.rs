//! Every case of `credentials_prefixed.jsonl` (not gated) and
//! `credentials_prefixed_unicode.jsonl` (gated on the Unicode version)
//! against `credentials::prefixed`: each detector's every match, with its
//! span and whether it is a hit, a mask or a spent run. The spelling is
//! the generator's (`tests/contract/gen_credentials_prefixed.py`).

mod common;

use bettermemory_core::credentials::prefixed::{
    is_masked_token, is_spent_run, prefixed_spans, Detector, DETECTORS, MIN_BODY_DIVERSITY,
};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn text_of(case: &Value) -> Vec<u32> {
    match case.get("t") {
        Some(Value::String(s)) => cps(s),
        Some(Value::Object(o)) => {
            let mut out = Vec::new();
            for pair in o["r"].as_array().expect("a recipe") {
                // A piece is a text, or the list of its parts: a piece
                // shaped like a key is written in parts, so no line of the
                // file holds a whole one.
                let piece: Vec<u32> = match &pair[0] {
                    Value::String(s) => cps(s),
                    parts => parts
                        .as_array()
                        .expect("a piece in parts")
                        .iter()
                        .flat_map(|part| cps(part.as_str().expect("a part")))
                        .collect(),
                };
                for _ in 0..pair[1].as_u64().expect("a count") {
                    out.extend_from_slice(&piece);
                }
            }
            out
        }
        _ => common::cps_of(&case["t_cps"]),
    }
}

fn flag(text: &[u32], m: &bettermemory_core::credentials::prefixed::Match) -> &'static str {
    if is_spent_run(m) {
        "s"
    } else if is_masked_token(text, m) {
        "m"
    } else {
        "h"
    }
}

fn matches(text: &[u32], only: Option<&[usize]>) -> Vec<(usize, usize, usize, &'static str)> {
    let mut out = Vec::new();
    for (d, det) in DETECTORS.iter().enumerate() {
        if only.is_some_and(|o| !o.contains(&d)) {
            continue;
        }
        for m in det.find_iter(text) {
            out.push((d, m.start, m.end, flag(text, &m)));
        }
    }
    out
}

#[test]
fn credentials_prefixed_every_case() {
    let lines = jsonl("credentials_prefixed.jsonl");
    let header = &lines[0];
    assert_eq!(header["k"], "detectors");
    let kinds: Vec<&str> = DETECTORS.iter().map(|d| d.kind()).collect();
    assert_eq!(header["kinds"], json!(kinds));
    assert_eq!(header["min_body_diversity"], json!(MIN_BODY_DIVERSITY));
    let patterns: Vec<&str> = DETECTORS.iter().map(|d| d.pattern()).collect();
    assert_eq!(header["patterns"], json!(patterns));
    let mut ledger = Ledger::new("golden_credentials_prefixed", "credentials_prefixed.jsonl");
    let mut checked = 0;
    for (i, case) in lines[1..].iter().enumerate() {
        let text = text_of(case);
        let got = matches(&text, None);
        let want: Vec<(usize, usize, usize, String)> = case["o"]
            .as_array()
            .expect("matches")
            .iter()
            .map(|m| {
                (
                    m[0].as_u64().unwrap() as usize,
                    m[1].as_u64().unwrap() as usize,
                    m[2].as_u64().unwrap() as usize,
                    m[3].as_str().unwrap().to_string(),
                )
            })
            .collect();
        ledger.case(format!("case {i}"), || {
            let got: Vec<(usize, usize, usize, String)> = got
                .iter()
                .map(|&(d, s, e, f)| (d, s, e, f.to_string()))
                .collect();
            assert_eq!(got, want, "case {i}: {:?}", case);
            let spans = prefixed_spans(&text);
            let hits: Vec<(usize, usize, &str)> = want
                .iter()
                .filter(|m| m.3 == "h")
                .map(|m| (m.1, m.2, DETECTORS[m.0].kind()))
                .collect();
            assert_eq!(spans, hits, "case {i}: spans");
        });
        checked += 1;
    }
    assert!(checked > 0);
    ledger.finish(lines.len() - 1);
}

const TEMPLATE_AWS: &str = concat!("AKIA", "IOSF", "ODNN", "7EXA", "MPL0");
const TEMPLATE_SLACK: &str = concat!("xoxb", "-123", "4567", "890-", "ab");

#[test]
fn credentials_prefixed_unicode_every_block() {
    let lines = jsonl("credentials_prefixed_unicode.jsonl");
    assert_eq!(lines[0]["k"], "unicode");
    let block = lines[0]["block"].as_u64().expect("a block") as u32;
    let mut ledger = Ledger::new(
        "golden_credentials_prefixed",
        "credentials_prefixed_unicode.jsonl",
    );
    let aws = cps(TEMPLATE_AWS);
    let slack = cps(TEMPLATE_SLACK);
    for rec in &lines[1..] {
        let lo = rec["lo"].as_u64().expect("lo") as u32;
        let mut h = Sha256::new();
        for cp in lo..lo + block {
            let mut texts = Vec::new();
            let mut t0 = vec![cp];
            t0.extend_from_slice(&aws);
            texts.push((t0, 0usize));
            let mut t1 = aws.clone();
            t1.push(cp);
            texts.push((t1, 0));
            let mut t2 = slack.clone();
            t2.push(cp);
            texts.push((t2, 5));
            let parts: Vec<String> = texts
                .iter()
                .map(|(t, d)| {
                    matches(t, Some(&[*d]))
                        .iter()
                        .map(|(d, s, e, f)| format!("{d},{s},{e},{f}"))
                        .collect::<Vec<_>>()
                        .join(";")
                })
                .collect();
            h.update(format!("{cp:x}:{}\n", parts.join("|")).as_bytes());
        }
        let got = common::hex(&h.finalize());
        ledger.case(format!("block {lo:x}"), || {
            assert_eq!(got, rec["sha256"].as_str().unwrap(), "block {lo:x}")
        });
    }
    ledger.finish(lines.len() - 1);
}

#[test]
fn every_detector_named() {
    assert_eq!(DETECTORS.len(), 12);
    assert_eq!(Detector::GithubPat.kind(), Detector::GithubToken.kind());
}
