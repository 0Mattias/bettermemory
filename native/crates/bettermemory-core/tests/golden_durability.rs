//! Every case of `durability.jsonl` (not gated) and
//! `durability_unicode.jsonl` (gated on the Unicode version) against the
//! native `durability`, unit 1. The spelling of texts is the generator's
//! (`tests/contract/gen_durability.py`).

mod common;

use bettermemory_core::durability as d;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn text(record: &Value, name: &str) -> PyStr {
    common::text_field(record, name)
}

fn usize_of(v: &Value) -> usize {
    v.as_u64().unwrap() as usize
}

fn spans(v: &Value) -> Vec<(usize, usize)> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|p| (usize_of(&p[0]), usize_of(&p[1])))
        .collect()
}

fn opt_bool(b: Option<bool>) -> Value {
    b.map_or(Value::Null, Value::Bool)
}

fn str_value(t: &[u32]) -> Value {
    match unicode::to_string(t) {
        Some(s) => Value::String(s),
        None => json!(t),
    }
}

fn check_text(r: &Value) {
    let t = text(r, "t");
    let qs: Vec<Value> = d::quoted_spans(&t)
        .iter()
        .map(|&(a, b)| json!([a, b]))
        .collect();
    assert_eq!(Value::Array(qs), r["qs"], "quoted_spans");
    let sa: Vec<Value> = (0..t.len() + 2)
        .map(|p| opt_bool(d::at_sentence_start(&t, p)))
        .collect();
    assert_eq!(Value::Array(sa), r["sa"], "at_sentence_start");
    let want = r["ss"].as_array().unwrap();
    assert_eq!(want.len(), t.len() + 2);
    for (p, w) in want.iter().enumerate() {
        let got = d::sentence_around(&t, p);
        let w = match w {
            Value::String(s) => unicode::cps(s),
            other => common::cps_of(other),
        };
        assert_eq!(got, w, "sentence_around at {p}");
    }
}

/// `str()` of an answer, as the generator joins them.
fn py_bool(b: Option<bool>) -> &'static str {
    match b {
        None => "None",
        Some(true) => "True",
        Some(false) => "False",
    }
}

fn utf8_passing(out: &mut Vec<u8>, t: &[u32]) {
    for &c in t {
        match char::from_u32(c) {
            Some(ch) => out.extend_from_slice(ch.encode_utf8(&mut [0u8; 4]).as_bytes()),
            None => out.extend_from_slice(&[
                0xE0 | (c >> 12) as u8,
                0x80 | ((c >> 6) & 0x3F) as u8,
                0x80 | (c & 0x3F) as u8,
            ]),
        }
    }
}

fn answers(c: u32) -> Vec<PyStr> {
    let cat = |parts: &[&[u32]]| -> PyStr { parts.concat() };
    let s = |x: &str| unicode::cps(x);
    let cv = [c];
    vec![
        s(py_bool(d::at_sentence_start(&cat(&[&cv, &s(" x")]), 2))),
        s(py_bool(d::is_titlecase_name(&cv, 0, 1))),
        s(py_bool(d::is_titlecase_name(
            &cat(&[&s("x "), &cv, &s("a")]),
            2,
            3,
        ))),
        s(py_bool(d::is_titlecase_name(
            &cat(&[&s("Today"), &cv, &s("Ab")]),
            0,
            5,
        ))),
        d::sentence_around(&cat(&[&cv, &s("."), &cv]), 3),
        d::snippet_around(&cat(&[&cv, &s("a"), &cv, &cv, &s("b"), &cv]), 0, 6),
        s(&d::quoted_spans(&cat(&[&s("\""), &cv, &s("\""), &cv]))
            .len()
            .to_string()),
    ]
}

fn sweep(lo: u32, n: u32) -> String {
    let mut blob = Vec::new();
    let mut first = true;
    for c in lo..lo + n {
        for a in answers(c) {
            if !first {
                blob.push(0);
            }
            first = false;
            utf8_passing(&mut blob, &a);
        }
    }
    common::hex(&Sha256::digest(&blob))
}

fn check(r: &Value) {
    match r["k"].as_str().unwrap() {
        "cm" => assert_eq!(d::canonical_marker(&text(r, "m")), text(r, "o")),
        "text" => check_text(r),
        "iq" => assert_eq!(
            Value::Bool(d::in_quoted_span(
                &spans(&r["sp"]),
                usize_of(&r["s"]),
                usize_of(&r["e"])
            )),
            r["o"]
        ),
        "sn" => assert_eq!(
            str_value(&d::snippet_around(
                &text(r, "t"),
                usize_of(&r["s"]),
                usize_of(&r["e"])
            )),
            str_value(&text(r, "o"))
        ),
        "tn" => assert_eq!(
            opt_bool(d::is_titlecase_name(
                &text(r, "t"),
                usize_of(&r["s"]),
                usize_of(&r["e"])
            )),
            r["o"]
        ),
        "sweep" => {
            let lo = r["lo"].as_u64().unwrap() as u32;
            let n = r["n"].as_u64().unwrap() as u32;
            assert_eq!(sweep(lo, n), r["sha"].as_str().unwrap(), "block {lo:#x}");
        }
        other => panic!("unknown row kind {other}"),
    }
}

fn run(test: &str, file: &str) {
    let lines = common::jsonl(file);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64().unwrap() as usize, rows.len());
    assert!(!rows.is_empty());
    let mut ledger = Ledger::new(test, file);
    for (i, r) in rows.iter().enumerate() {
        ledger.case(format!("line {} {}", i + 2, r), || check(r));
    }
    ledger.finish(rows.len());
}

#[test]
fn durability_every_case() {
    run("durability_every_case", "durability.jsonl");
}

#[test]
fn durability_unicode_every_case() {
    run("durability_unicode_every_case", "durability_unicode.jsonl");
}
