//! Every case of `control_tokens.jsonl` (not gated) and
//! `control_tokens_unicode.jsonl` (gated on the Unicode version) against
//! the native `control_tokens`. The spelling of texts and values is the
//! generator's (`tests/contract/gen_control_tokens.py`).

mod common;

use bettermemory_core::control_tokens as ct;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn text(record: &Value, name: &str) -> PyStr {
    common::text_field(record, name)
}

fn opt_text(record: &Value, name: &str) -> Option<PyStr> {
    match record.get(name) {
        Some(Value::Null) => None,
        _ => Some(text(record, name)),
    }
}

fn fold(t: &[u32]) -> PyStr {
    let words: Vec<&[u32]> = t
        .split(|&c| unicode::is_space(c))
        .filter(|w| !w.is_empty())
        .collect();
    words.join(&0x20)
}

/// The part of a refusal of `note` between its fixed words.
fn named_part(message: &[u32]) -> PyStr {
    let head = unicode::cps(
        "`note` carries a chat-template control token, named here by template family and bare name: ",
    );
    let tail = unicode::cps(&format!(
        ". {} {} Then send the call again.",
        ct::FIELD_WHY,
        ct::BARE_NAME_REMEDY
    ));
    assert!(message.starts_with(&head) && message.ends_with(&tail));
    message[head.len()..message.len() - tail.len()].to_vec()
}

fn check_text(r: &Value) {
    let t = text(r, "t");
    let hits: Vec<Value> = ct::find_control_tokens(&t)
        .iter()
        .map(|h| json!({"kind": h.kind, "token": unicode::to_string(&h.token).unwrap()}))
        .collect();
    assert_eq!(Value::Array(hits), r["find"], "find");
    let off: Vec<Value> = ct::control_token_offsets(&t)
        .iter()
        .map(|&(k, a, n)| json!([k, a, n]))
        .collect();
    assert_eq!(Value::Array(off), r["off"], "offsets");
    let defanged = ct::defang_control_tokens(&t);
    let want_def = if r.get("def").is_some() || r.get("def_cps").is_some() {
        text(r, "def")
    } else {
        t.clone()
    };
    assert_eq!(defanged, want_def, "defang");
    let want_fq = if r.get("fq").is_some() || r.get("fq_cps").is_some() {
        text(r, "fq")
    } else {
        fold(&defanged)
    };
    assert_eq!(ct::folded_quote(&t), want_fq, "folded_quote");
    let got = ct::refuse_control_tokens(
        &unicode::cps("note"),
        Some(&t),
        &unicode::cps(ct::BARE_NAME_REMEDY),
        &unicode::cps(ct::FIELD_WHY),
    );
    let got = got.err().map(|e| named_part(&e.message));
    assert_eq!(got, opt_text(r, "ref"), "refuse");
}

fn check_refuse(r: &Value) {
    let t = opt_text(r, "t");
    let got = ct::refuse_control_tokens(
        &text(r, "f"),
        t.as_deref(),
        &text(r, "rem"),
        &text(r, "why"),
    );
    assert_eq!(got.err().map(|e| e.message), opt_text(r, "o"));
}

fn check_named(r: &Value) {
    let got = ct::named_tokens(&common::neutral(&r["v"]));
    assert!(got == common::neutral(&r["o"]), "named_tokens: {got:?}");
}

/// A sweep block's hash, as the generator joins and encodes it.
fn sweep(lo: u32) -> String {
    let mut blob: Vec<u8> = Vec::new();
    let mut first = true;
    for cp in lo..lo + 0x1000 {
        let mut parts: Vec<PyStr> = Vec::new();
        let mut t = vec![cp];
        t.extend(unicode::cps("<|end|>"));
        t.push(cp);
        parts.push(ct::defang_control_tokens(&t));
        let mut w = vec![u32::from(b'<'), 0xFF5C, cp, 0xFF5C, u32::from(b'>')];
        parts.push(unicode::cps(&ct::find_control_tokens(&w).len().to_string()));
        w = vec![cp];
        w.extend(unicode::cps("Human:"));
        parts.push(unicode::cps(&ct::find_control_tokens(&w).len().to_string()));
        for p in parts {
            if !first {
                blob.push(0);
            }
            first = false;
            for c in p {
                // UTF-8 with surrogates passed.
                let mut buf = [0u8; 4];
                match char::from_u32(c) {
                    Some(ch) => blob.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes()),
                    None => blob.extend_from_slice(&[
                        0xE0 | (c >> 12) as u8,
                        0x80 | ((c >> 6) & 0x3F) as u8,
                        0x80 | (c & 0x3F) as u8,
                    ]),
                }
            }
        }
    }
    Sha256::digest(&blob)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn run(test: &str, file: &str) {
    let lines = common::jsonl(file);
    let header = &lines[0];
    let rows = &lines[1..];
    assert_eq!(header["cases"].as_u64().unwrap() as usize, rows.len());
    if header["k"] == "tables" {
        let table: Vec<Value> = ct::CONTROL_TOKENS
            .iter()
            .map(|(k, ts)| json!([k, ts]))
            .collect();
        assert_eq!(Value::Array(table), header["tokens"]);
        assert_eq!(header["why"], ct::FIELD_WHY);
        assert_eq!(header["remedy"], ct::BARE_NAME_REMEDY);
    }
    let mut ledger = Ledger::new(test, file);
    for (i, r) in rows.iter().enumerate() {
        let label = format!("line {} {}", i + 2, r);
        ledger.case(label, || match r["k"].as_str().unwrap() {
            "text" => check_text(r),
            "refuse" => check_refuse(r),
            "named" => check_named(r),
            "sweep" => {
                let lo = r["lo"].as_u64().unwrap() as u32;
                assert_eq!(sweep(lo), r["sha"].as_str().unwrap(), "block {lo:#x}");
            }
            other => panic!("unknown row kind {other}"),
        });
    }
    ledger.finish(rows.len());
}

#[test]
fn control_tokens_every_case() {
    run("control_tokens_every_case", "control_tokens.jsonl");
}

#[test]
fn control_tokens_unicode_every_case() {
    run(
        "control_tokens_unicode_every_case",
        "control_tokens_unicode.jsonl",
    );
}
