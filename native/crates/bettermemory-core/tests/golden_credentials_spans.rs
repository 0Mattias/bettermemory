//! Every case of `credentials_spans.jsonl` (not gated) and
//! `credentials_spans_unicode.jsonl` (gated on the Unicode version)
//! against `credentials::spans`: the value spans of the generic and the
//! URI rule, the merged spans, the redacted text, the markers and the
//! snippets. The spelling and the sweeps' rules are the generator's
//! (`tests/contract/gen_credentials_spans.py`).

mod common;

use bettermemory_core::credentials::spans::{
    find_credential_markers, generic_values, redact_all, safe_snippet, secret_spans, uri_values,
    CONTEXT_CHARS, GENERIC_KEYWORD_PATTERN, URI_USERINFO_PATTERN,
};
use bettermemory_core::unicode;
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

/// A hex of UTF-8 as code points.
fn unhex(value: &Value) -> Vec<u32> {
    let bytes = common::unhex(value.as_str().expect("hex"));
    cps(&String::from_utf8(bytes).expect("UTF-8"))
}

fn text_of(case: &Value) -> Vec<u32> {
    if let Some(hex) = case.get("t_hex") {
        return unhex(hex);
    }
    match case.get("t") {
        Some(Value::Object(o)) => {
            let mut out = Vec::new();
            for pair in o["r"].as_array().expect("a recipe") {
                let piece = unhex(&pair[0]);
                for _ in 0..pair[1].as_u64().expect("a count") {
                    out.extend_from_slice(&piece);
                }
            }
            out
        }
        _ => common::text_field(case, "t"),
    }
}

fn pairs(spans: &[(usize, usize)]) -> Value {
    json!(spans.iter().map(|&(a, b)| [a, b]).collect::<Vec<_>>())
}

fn check(row: &Value, label: &str) {
    let text = text_of(row);
    if row.get("k").and_then(Value::as_str) == Some("snip") {
        let a = &row["a"];
        let got = safe_snippet(
            &text,
            a[0].as_u64().expect("start") as usize,
            a[1].as_u64().expect("end") as usize,
            a[2].as_str().expect("kind"),
        );
        assert_eq!(got, common::text_field(row, "o"), "{label}: snippet");
        return;
    }
    assert_eq!(pairs(&generic_values(&text)), row["g"], "{label}: generic");
    assert_eq!(pairs(&uri_values(&text)), row["u"], "{label}: uri");
    let spans: Vec<Value> = secret_spans(&text)
        .iter()
        .map(|&(a, b, k)| json!([a, b, k]))
        .collect();
    assert_eq!(json!(spans), row["s"], "{label}: spans");
    assert_eq!(
        redact_all(&text),
        common::text_field(row, "r"),
        "{label}: redacted"
    );
    let marks: Vec<(String, Vec<u32>)> = find_credential_markers(&text)
        .into_iter()
        .map(|m| (m.kind.to_string(), m.snippet))
        .collect();
    let want: Vec<(String, Vec<u32>)> = row["m"]
        .as_array()
        .expect("markers")
        .iter()
        .map(|m| {
            (
                m[0].as_str().expect("a kind").to_string(),
                common::text_field(&m[1], "o"),
            )
        })
        .collect();
    assert_eq!(marks, want, "{label}: markers");
}

/// The generator's `_line`.
fn line(text: &[u32]) -> String {
    let s = |t: &[u32]| unicode::to_string(t).expect("no surrogate");
    let spans: Vec<String> = secret_spans(text)
        .iter()
        .map(|(a, b, k)| format!("{a},{b},{k}"))
        .collect();
    let marks: Vec<String> = find_credential_markers(text)
        .iter()
        .map(|m| format!("{}={}", m.kind, s(&m.snippet)))
        .collect();
    format!(
        "S{}|R{}|M{}",
        spans.join(";"),
        s(&redact_all(text)),
        marks.join(";")
    )
}

/// The generator's `sweep_text`.
fn sweep_text(sweep: &Value, i: usize) -> Vec<u32> {
    let name = sweep["name"].as_str().expect("a name");
    let mut data = Vec::new();
    let mut block = 0;
    while data.len() < 64 {
        data.extend(Sha256::digest(
            format!("credentials-spans {name} {i} {block}").as_bytes(),
        ));
        block += 1;
    }
    let counts = sweep["counts"].as_array().expect("counts");
    let pieces = sweep["pieces"].as_array().expect("pieces");
    let n = counts[data[0] as usize % counts.len()]
        .as_u64()
        .expect("a count") as usize;
    let mut out = Vec::new();
    for j in 0..n {
        out.extend(unhex(&pieces[data[1 + j] as usize % pieces.len()]));
    }
    out
}

fn check_sweep(row: &Value) {
    let mut h = Sha256::new();
    let count = row["count"].as_u64().expect("count") as usize;
    for i in 0..count {
        h.update(format!("{}\n", line(&sweep_text(row, i))).as_bytes());
    }
    assert_eq!(
        common::hex(&h.finalize()),
        row["sha256"].as_str().expect("sha256")
    );
}

#[test]
fn credentials_spans_every_case() {
    let lines = jsonl("credentials_spans.jsonl");
    let header = &lines[0];
    assert_eq!(header["generic"], json!(GENERIC_KEYWORD_PATTERN));
    assert_eq!(header["uri"], json!(URI_USERINFO_PATTERN));
    assert_eq!(header["generic_flags"], json!(2));
    assert_eq!(header["uri_flags"], json!(2));
    assert_eq!(header["context"], json!(CONTEXT_CHARS));
    let mut ledger = Ledger::new("golden_credentials_spans", "credentials_spans.jsonl");
    let mut sweeps = 0;
    for (i, row) in lines[1..].iter().enumerate() {
        let label = format!("row {i}");
        if row.get("k").and_then(Value::as_str) == Some("sweep") {
            sweeps += 1;
            ledger.case(label, || check_sweep(row));
        } else {
            ledger.case(label.clone(), || check(row, &label));
        }
    }
    assert_eq!(sweeps, 1, "sweep rows");
    ledger.finish(lines.len() - 1);
}

/// The generator's `asked`: three ranges whole but the surrogates, and
/// elsewhere every thirteenth code point.
fn asked(c: u32) -> bool {
    const WHOLE: [(u32, u32); 3] = [(0, 0x3400), (0xA000, 0xAC00), (0xD7A4, 0x10000)];
    !(0xD800..=0xDFFF).contains(&c)
        && (WHOLE.iter().any(|&(lo, hi)| lo <= c && c < hi) || c.is_multiple_of(13))
}

#[test]
fn credentials_spans_unicode_every_block() {
    let lines = jsonl("credentials_spans_unicode.jsonl");
    assert_eq!(lines[0]["k"], "unicode");
    let templates: Vec<Vec<Vec<u32>>> = lines[0]["templates"]
        .as_array()
        .expect("templates")
        .iter()
        .map(|t| t.as_array().expect("pieces").iter().map(unhex).collect())
        .collect();
    let mut ledger = Ledger::new(
        "golden_credentials_spans",
        "credentials_spans_unicode.jsonl",
    );
    let mut covered = 0u32;
    for rec in &lines[1..] {
        let lo = rec["lo"].as_u64().expect("lo") as u32;
        let hi = rec["hi"].as_u64().expect("hi") as u32;
        assert_eq!(lo, covered, "block {lo:x}");
        covered = hi;
        let mut h = Sha256::new();
        for pieces in &templates {
            for c in (lo..hi).filter(|&c| asked(c)) {
                let text = pieces.join(&c);
                h.update(format!("{c:x}:{}\n", line(&text)).as_bytes());
            }
        }
        let got = common::hex(&h.finalize());
        ledger.case(format!("block {lo:x}"), || {
            assert_eq!(got, rec["sha256"].as_str().unwrap(), "block {lo:x}")
        });
    }
    assert_eq!(covered, 0x40000, "every code point below U+40000");
    ledger.finish(lines.len() - 1);
}
