//! Every case of `search_snippet.jsonl` and `search_snippet_unicode.jsonl`
//! against `search::snippet` and `search::token_cache`: the score gate,
//! query-biased snippets, the snippet memos and the token cache with their
//! bounds and eviction order, and the seeded sweeps by digest.

mod common;

use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::search::snippet::{
    top_hit_leads_runner_up, SnippetMemo, EXPAND_TOP_SCORE_MARGIN, SNIPPET_LEAD_CHARS,
    SNIPPET_SCAN_CHARS, SNIPPET_TOKEN_ENTRIES, TOKEN_SURFACE_ENTRIES,
};
use bettermemory_core::search::token_cache::{MemoryTokens, TokenCache, TOKEN_CACHE_ENTRIES};
use bettermemory_core::unicode::cps;
use common::parity::Ledger;
use common::{cps_of, jsonl};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn bits(x: f64) -> String {
    format!("{:016x}", x.to_bits())
}

fn unbits(v: &Value) -> f64 {
    f64::from_bits(u64::from_str_radix(v.as_str().expect("bits"), 16).expect("hex"))
}

/// A text, `{"cps": [...]}`, or `[[piece, count], ...]`.
fn text(v: &Value) -> PyStr {
    match v {
        Value::String(s) => cps(s),
        Value::Object(o) => cps_of(&o["cps"]),
        Value::Array(parts) => parts
            .iter()
            .flat_map(|p| {
                let piece = text(&p[0]);
                let n = p[1].as_u64().expect("count") as usize;
                piece.repeat(n)
            })
            .collect(),
        _ => panic!("not a text: {v}"),
    }
}

/// UTF-8 with lone surrogates as their three bytes (`surrogatepass`).
pub fn utf8(t: &[u32]) -> Vec<u8> {
    let mut out = Vec::new();
    for &c in t {
        match char::from_u32(c) {
            Some(ch) => out.extend_from_slice(ch.encode_utf8(&mut [0; 4]).as_bytes()),
            None => out.extend_from_slice(&[
                0xE0 | (c >> 12) as u8,
                0x80 | ((c >> 6) & 0x3F) as u8,
                0x80 | (c & 0x3F) as u8,
            ]),
        }
    }
    out
}

/// A text as the golden file writes it.
fn shown(t: &[u32]) -> Value {
    if t.iter().any(|&c| (0xD800..0xE000).contains(&c)) {
        json!({ "cps": t })
    } else {
        json!(t
            .iter()
            .map(|&c| char::from_u32(c).expect("char"))
            .collect::<String>())
    }
}

fn key(t: &[u32]) -> Value {
    if t.len() <= 200 {
        return shown(t);
    }
    let sha = Sha256::digest(utf8(t));
    let hex: String = sha.iter().map(|b| format!("{b:02x}")).collect();
    json!([t.len(), &hex[..16]])
}

fn strings(v: &Value) -> Vec<String> {
    v.as_array()
        .expect("a list")
        .iter()
        .map(|s| s.as_str().expect("a string").to_owned())
        .collect()
}

fn toks(t: &MemoryTokens) -> Value {
    json!([t.body, t.content, t.scope_set, t.body_tf])
}

/// The sweep's LCG and bodies; see the generator's docstring.
pub fn sweep(seed: u64, n: usize, words: &[String]) -> String {
    let mut x = seed;
    let mut draw = |m: usize| {
        x = x
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        ((x >> 33) % m as u64) as usize
    };
    let memo = SnippetMemo::default();
    let mut h = Sha256::new();
    for i in 0..n {
        let len = 5 + draw(120);
        let body: Vec<&str> = (0..len)
            .map(|_| words[draw(words.len())].as_str())
            .collect();
        let k = 1 + draw(3);
        let matched: Vec<String> = (0..k).map(|_| words[draw(words.len())].clone()).collect();
        let mc = 20 + draw(200) as i64;
        if i > 0 {
            h.update([0u8]);
        }
        memo.clear();
        h.update(utf8(&memo.query_biased_snippet(
            &cps(&body.join(" ")),
            &matched,
            mc,
        )));
    }
    h.finalize().iter().map(|b| format!("{b:02x}")).collect()
}

fn answer(c: &Value) -> Value {
    let mut out = c.clone();
    match c["k"].as_str().expect("k") {
        "top" => {
            let a = c["a"].as_array().expect("a");
            out["r"] = json!(top_hit_leads_runner_up(unbits(&a[0]), unbits(&a[1])));
        }
        "snip" => {
            let memo = SnippetMemo::default();
            let mc = c["mc"].as_i64().expect("mc");
            out["r"] = shown(&memo.query_biased_snippet(&text(&c["t"]), &strings(&c["m"]), mc));
        }
        "seq" => {
            let memo = SnippetMemo::with_entries(
                c["et"].as_u64().expect("et") as usize,
                c["es"].as_u64().expect("es") as usize,
            );
            let mut r = Vec::new();
            for call in c["calls"].as_array().expect("calls") {
                if call == "clear" {
                    memo.clear();
                    r.push(Value::Null);
                } else {
                    let mc = call[2].as_i64().expect("mc");
                    r.push(shown(&memo.query_biased_snippet(
                        &text(&call[0]),
                        &strings(&call[1]),
                        mc,
                    )));
                }
            }
            out["r"] = json!(r);
            out["scans"] = json!(memo.scan_keys().iter().map(|k| key(k)).collect::<Vec<_>>());
            out["surf"] = json!(memo.surface_keys());
        }
        "mt" => {
            let cache = TokenCache::with_entries(c["e"].as_u64().expect("e") as usize);
            let mut r = Vec::new();
            for op in c["ops"].as_array().expect("ops") {
                match op[0].as_str().expect("op") {
                    "c" => {
                        cache.clear();
                        r.push(Value::Null);
                    }
                    "m" => r.push(toks(&cache.memory_tokens(&text(&op[1]), &strings(&op[2])))),
                    _ => {
                        let index = op[3].as_str().expect("index text");
                        cache.seed_memory_tokens(&text(&op[1]), &strings(&op[2]), index);
                        r.push(Value::Null);
                    }
                }
            }
            out["r"] = json!(r);
            out["keys"] = json!(cache
                .keys()
                .iter()
                .map(|(b, s)| json!([shown(b), s]))
                .collect::<Vec<_>>());
        }
        "sweep" => {
            out["sha"] = json!(sweep(
                c["seed"].as_u64().expect("seed"),
                c["n"].as_u64().expect("n") as usize,
                &strings(&c["words"]),
            ));
        }
        k => panic!("unknown case kind {k}"),
    }
    out
}

fn every_case(name: &str, test: &str) {
    let lines = jsonl(name);
    let header = &lines[0];
    assert_eq!(header["scan"], json!(SNIPPET_SCAN_CHARS));
    assert_eq!(header["lead"], json!(SNIPPET_LEAD_CHARS));
    assert_eq!(header["token_entries"], json!(SNIPPET_TOKEN_ENTRIES));
    assert_eq!(header["surface_entries"], json!(TOKEN_SURFACE_ENTRIES));
    assert_eq!(header["cache_entries"], json!(TOKEN_CACHE_ENTRIES));
    assert_eq!(header["margin"], json!(bits(EXPAND_TOP_SCORE_MARGIN)));
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
fn search_snippet_every_case() {
    every_case("search_snippet.jsonl", "search_snippet_every_case");
}

#[test]
fn search_snippet_unicode_every_case() {
    every_case(
        "search_snippet_unicode.jsonl",
        "search_snippet_unicode_every_case",
    );
}
