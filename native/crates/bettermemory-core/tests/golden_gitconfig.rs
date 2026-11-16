//! Every case of `gitconfig.jsonl`: the entries `gitconfig.git_entries`
//! returned for a config text (names, values, the `_spelled` split, the
//! NUL cut, `spaced`, and the reading under each git version) or its
//! refusal, and `git_int` and `git_bool` of a value, against the native
//! reader.

mod common;

use bettermemory_core::gitconfig::{self, GitVersion};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// A fixture byte string: `=` and itself, or hex.
fn bytes_of(text: &str) -> Vec<u8> {
    match text.strip_prefix('=') {
        Some(plain) => plain.as_bytes().to_vec(),
        None => common::unhex(text),
    }
}

fn written(bytes: &[u8]) -> String {
    let safe = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .,=_:-+*~%()[]#;!@'\"\n\t\r";
    if bytes.iter().all(|b| safe.contains(b)) {
        format!("={}", String::from_utf8(bytes.to_vec()).expect("ASCII"))
    } else {
        bytes.iter().map(|b| format!("{b:02x}")).collect()
    }
}

fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn mutated(real: &[u8], op: &str, at: usize) -> Vec<u8> {
    let replaced = |to: &[u8]| {
        let mut out = Vec::new();
        for &b in real {
            if b == b'\n' {
                out.extend_from_slice(to);
            } else {
                out.push(b);
            }
        }
        out
    };
    match op {
        "as" => real.to_vec(),
        "crlf" => replaced(b"\r\n"),
        "cr" => replaced(b"\r"),
        "strip" => {
            let mut end = real.len();
            while end > 0 && real[end - 1] == b'\n' {
                end -= 1;
            }
            real[..end].to_vec()
        }
        "flip" => {
            let mut out = real.to_vec();
            out[at] ^= 0x20;
            out
        }
        "del" => [&real[..at], &real[at + 1..]].concat(),
        "dup" => [&real[..at + 1], &real[at..]].concat(),
        "cut" => real[..at].to_vec(),
        other => panic!("no mutation {other}"),
    }
}

fn text_of(record: &Value, reals: &[Vec<u8>]) -> Vec<u8> {
    if let Some(c) = record["c"].as_str() {
        return bytes_of(c);
    }
    if let Some(m) = record["m"].as_array() {
        let i = m[0].as_u64().expect("an index") as usize;
        let at = m[2].as_u64().expect("a position") as usize;
        return mutated(&reals[i], m[1].as_str().expect("an op"), at);
    }
    let mut out = Vec::new();
    for piece in record["p"].as_array().expect("a recipe") {
        let bytes = bytes_of(piece[0].as_str().expect("a piece"));
        for _ in 0..piece[1].as_u64().expect("a count") {
            out.extend_from_slice(&bytes);
        }
    }
    out
}

fn native_text(data: &[u8], versions: &[GitVersion]) -> Value {
    let Ok(entries) = gitconfig::git_entries(data) else {
        return json!({"e": null});
    };
    let mut rows = Vec::new();
    let mut cut = Vec::new();
    for entry in &entries {
        let name = entry.spelled();
        cut.push(entry.value_cut());
        let value = entry.value.as_deref().map(|v| {
            if v.len() > 256 {
                json!({"n": v.len(), "sha256": sha256_hex(v)})
            } else {
                json!(written(v))
            }
        });
        let mut row = json!([
            written(&entry.name()),
            value,
            name.section,
            name.key,
            name.subsection.as_deref().map(written),
            u8::from(entry.value.as_deref().is_some_and(gitconfig::spaced)),
        ]);
        // The cut value, recorded only where it differs from the value.
        if let (Some(raw), Some(cut)) = (entry.value.as_deref(), entry.value_cut()) {
            if cut != raw {
                let cut = if cut.len() > 256 {
                    json!({"n": cut.len(), "sha256": sha256_hex(cut)})
                } else {
                    json!(written(cut))
                };
                row.as_array_mut().expect("a row").push(cut);
            }
        }
        rows.push(row);
    }
    let rows = Value::Array(rows);
    let shown = serde_json::to_string(&rows).expect("JSON");
    let mut out = if shown.len() > 400 {
        json!({"e": {"n": entries.len(), "sha256": sha256_hex(shown.as_bytes())}})
    } else {
        json!({"e": rows})
    };
    let answers: String = versions
        .iter()
        .map(|&v| {
            let all = cut
                .iter()
                .all(|c| c.is_none_or(|c| gitconfig::reads_as_written(c, v)));
            if all {
                '1'
            } else {
                '0'
            }
        })
        .collect();
    if answers.contains('0') {
        out["w"] = json!(answers);
    }
    out
}

#[test]
fn gitconfig_every_case() {
    let lines = jsonl("gitconfig.jsonl");
    let header = &lines[0];
    assert_eq!(header["fixture"], "gitconfig");
    let reals: Vec<Vec<u8>> = header["reals"]
        .as_array()
        .expect("reals")
        .iter()
        .map(|r| bytes_of(r.as_str().expect("a real")))
        .collect();
    let versions: Vec<GitVersion> = header["versions"]
        .as_array()
        .expect("versions")
        .iter()
        .map(|v| {
            v.as_array().map(|p| {
                let n = |i: usize| p[i].as_u64().expect("a number") as u32;
                (n(0), n(1), n(2))
            })
        })
        .collect();
    let records = &lines[1..];
    let texts = records.iter().filter(|r| r["k"] == "text").count();
    assert_eq!(header["texts"].as_u64(), Some(texts as u64));
    assert_eq!(
        header["values"].as_u64(),
        Some((records.len() - texts) as u64)
    );
    let ledger = Ledger::new("golden_gitconfig", "gitconfig.jsonl");
    let mut failures = Vec::new();
    let mut refused = 0;
    for (i, record) in records.iter().enumerate() {
        let (got, want) = if record["k"] == "text" {
            let data = text_of(record, &reals);
            let got = native_text(&data, &versions);
            if got["e"].is_null() {
                refused += 1;
            }
            let mut want = json!({"e": record["e"]});
            if let Some(w) = record.get("w") {
                want["w"] = w.clone();
            }
            (got, want)
        } else {
            let value = record["v"].as_str().map(bytes_of);
            let got = json!({
                "i": gitconfig::git_int(value.as_deref()).map(|n| n.to_string()),
                "b": gitconfig::git_bool(value.as_deref()),
            });
            (got, json!({"i": record["i"], "b": record["b"]}))
        };
        if got != want {
            let shown: String = record.to_string().chars().take(160).collect();
            failures.push(format!("#{i} {shown}: native {got} reference {want}"));
        }
    }
    assert_eq!(header["refused"].as_u64(), Some(refused));
    ledger.settle(records.len(), &failures);
}
