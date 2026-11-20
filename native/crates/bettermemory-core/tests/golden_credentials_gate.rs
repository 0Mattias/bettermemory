//! Every case of `credentials_gate.jsonl` (not gated) and
//! `credentials_gate_unicode.jsonl` (gated on the Unicode version) against
//! `credentials::gate`: for each text whether it is a placeholder, whether
//! it has the datetime shape, whether it looks like a secret, and where
//! the number suffix matches. The spelling, and the rules the sweeps make
//! their texts by, are the generator's
//! (`tests/contract/gen_credentials_gate.py`).

mod common;

use bettermemory_core::credentials::gate::{
    datetime_shape, is_placeholder, looks_like_secret, number_suffix, DATETIME_SHAPE_PATTERN,
    INLINE_PATTERNS, NUMBER_SUFFIX_PATTERN, PLACEHOLDER_VALUES, PLACEHOLDER_WORDS,
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
                let piece = cps(pair[0].as_str().expect("a piece"));
                for _ in 0..pair[1].as_u64().expect("a count") {
                    out.extend_from_slice(&piece);
                }
            }
            out
        }
        _ => common::cps_of(&case["t_cps"]),
    }
}

/// `p`, `d` and `s`, as the generator's `_flags`.
fn flags(text: &[u32]) -> String {
    let mut out = String::new();
    if is_placeholder(text) {
        out.push('p');
    }
    if datetime_shape(text) {
        out.push('d');
    }
    if looks_like_secret(text) {
        out.push('s');
    }
    out
}

fn sorted(table: &[&'static str]) -> Vec<&'static str> {
    let mut out = table.to_vec();
    out.sort_unstable();
    out
}

fn strings(value: &Value) -> Vec<Vec<u32>> {
    value
        .as_array()
        .expect("a list of strings")
        .iter()
        .map(|s| cps(s.as_str().expect("a string")))
        .collect()
}

/// The bytes case `i` of sweep `name` draws: SHA-256 of
/// `credentials-gate <name> <i> <block>` for block 0, 1, ..., joined.
struct Stream {
    name: String,
    i: usize,
    block: usize,
    bytes: Vec<u8>,
    at: usize,
}

impl Stream {
    fn new(name: &str, i: usize) -> Stream {
        Stream {
            name: name.to_string(),
            i,
            block: 0,
            bytes: Vec::new(),
            at: 0,
        }
    }

    fn next(&mut self) -> usize {
        if self.at == self.bytes.len() {
            let seed = format!("credentials-gate {} {} {}", self.name, self.i, self.block);
            self.bytes = Sha256::digest(seed.as_bytes()).to_vec();
            self.block += 1;
            self.at = 0;
        }
        self.at += 1;
        usize::from(self.bytes[self.at - 1])
    }
}

fn joined(name: &str, i: usize, counts: &[u64], pieces: &[Vec<u32>]) -> Vec<u32> {
    let mut stream = Stream::new(name, i);
    let count = counts[stream.next() % counts.len()];
    let mut text = Vec::new();
    for _ in 0..count {
        text.extend_from_slice(&pieces[stream.next() % pieces.len()]);
    }
    text
}

fn mutated(name: &str, i: usize, bases: &[Vec<u32>], chars: &[Vec<u32>]) -> Vec<u32> {
    let mut stream = Stream::new(name, i);
    let mut text = bases[stream.next() % bases.len()].clone();
    for _ in 0..stream.next() % 4 {
        let op = stream.next() % 3;
        let at = stream.next() % (text.len() + 1);
        let c = chars[stream.next() % chars.len()][0];
        if op == 1 {
            text.insert(at, c);
        } else if at < text.len() {
            if op == 0 {
                text[at] = c;
            } else {
                text.remove(at);
            }
        }
    }
    text
}

/// The digest of a sweep's answers, as the generator's `_digest`.
fn digest(texts: impl Iterator<Item = Vec<u32>>) -> String {
    let mut h = Sha256::new();
    for text in texts {
        let span = match number_suffix(&text) {
            Some((s, e)) => format!("{s},{e}"),
            None => String::new(),
        };
        h.update(format!("{}:{span}\n", flags(&text)).as_bytes());
    }
    common::hex(&h.finalize())
}

/// One line of the file after its header: a case or a sweep.
fn check(row: &Value, label: &str) {
    let n = || row["n"].as_u64().expect("a count") as usize;
    match row.get("k").and_then(Value::as_str) {
        Some("join") => {
            let name = row["name"].as_str().expect("a name");
            let counts: Vec<u64> = row["counts"]
                .as_array()
                .expect("counts")
                .iter()
                .map(|c| c.as_u64().expect("a count"))
                .collect();
            let pieces = strings(&row["pieces"]);
            let got = digest((0..n()).map(|i| joined(name, i, &counts, &pieces)));
            assert_eq!(got, row["sha256"].as_str().unwrap(), "{label}: join {name}");
        }
        Some("mutate") => {
            let name = row["name"].as_str().expect("a name");
            let bases = strings(&row["bases"]);
            let chars = strings(&row["chars"]);
            assert!(chars.iter().all(|c| c.len() == 1), "{label}: one character");
            let got = digest((0..n()).map(|i| mutated(name, i, &bases, &chars)));
            assert_eq!(
                got,
                row["sha256"].as_str().unwrap(),
                "{label}: mutate {name}"
            );
        }
        Some(other) => panic!("{label}: an unknown row kind {other}"),
        None => {
            let text = text_of(row);
            assert_eq!(json!(flags(&text)), row["a"], "{label}: flags of {row}");
            let span = match number_suffix(&text) {
                Some((s, e)) => json!([s, e]),
                None => Value::Null,
            };
            let want = row.get("n").cloned().unwrap_or(Value::Null);
            assert_eq!(span, want, "{label}: number suffix of {row}");
        }
    }
}

fn check_header(header: &Value) {
    assert_eq!(header["k"], "gate");
    assert_eq!(header["values"], json!(sorted(&PLACEHOLDER_VALUES)));
    assert_eq!(header["words"], json!(sorted(&PLACEHOLDER_WORDS)));
    assert_eq!(header["number_suffix"], json!(NUMBER_SUFFIX_PATTERN));
    assert_eq!(header["datetime_shape"], json!(DATETIME_SHAPE_PATTERN));
    assert_eq!(header["inline"], json!(INLINE_PATTERNS));
}

#[test]
fn credentials_gate_every_case() {
    let lines = jsonl("credentials_gate.jsonl");
    check_header(&lines[0]);
    let mut ledger = Ledger::new("golden_credentials_gate", "credentials_gate.jsonl");
    let mut sweeps = 0;
    for (i, row) in lines[1..].iter().enumerate() {
        if row.get("k").is_some() {
            sweeps += 1;
        }
        ledger.case(format!("row {i}"), || check(row, &format!("row {i}")));
    }
    // Seven join sweeps and one mutate sweep: a file without them checks
    // only its written rows.
    assert_eq!(sweeps, 8, "sweep rows");
    ledger.finish(lines.len() - 1);
}

fn ask(function: &str, text: &[u32]) -> bool {
    match function {
        "s" => looks_like_secret(text),
        "p" => is_placeholder(text),
        "d" => datetime_shape(text),
        "n" => number_suffix(text).is_some(),
        other => panic!("an unknown function {other}"),
    }
}

#[test]
fn credentials_gate_unicode_every_block() {
    let lines = jsonl("credentials_gate_unicode.jsonl");
    assert_eq!(lines[0]["k"], "unicode");
    let templates: Vec<(String, Vec<u32>, Vec<u32>)> = lines[0]["templates"]
        .as_array()
        .expect("templates")
        .iter()
        .map(|t| {
            (
                t[0].as_str().expect("a function").to_string(),
                cps(t[1].as_str().expect("before")),
                cps(t[2].as_str().expect("after")),
            )
        })
        .collect();
    let mut ledger = Ledger::new("golden_credentials_gate", "credentials_gate_unicode.jsonl");
    let mut covered = 0u32;
    for rec in &lines[1..] {
        let lo = rec["lo"].as_u64().expect("lo") as u32;
        let hi = rec["hi"].as_u64().expect("hi") as u32;
        // The blocks join end to end from U+0000.
        assert_eq!(lo, covered, "block {lo:x}");
        covered = hi;
        let asked: Vec<usize> = rec["t"]
            .as_array()
            .expect("the templates asked")
            .iter()
            .map(|t| t.as_u64().expect("an index") as usize)
            .collect();
        let mut h = Sha256::new();
        let mut text = Vec::new();
        for c in lo..hi {
            let mut bits = String::new();
            for &t in &asked {
                let (function, before, after) = &templates[t];
                text.clear();
                text.extend_from_slice(before);
                text.push(c);
                text.extend_from_slice(after);
                bits.push(if ask(function, &text) { '1' } else { '0' });
            }
            h.update(format!("{c:x}:{bits}\n").as_bytes());
        }
        let got = common::hex(&h.finalize());
        ledger.case(format!("block {lo:x}"), || {
            assert_eq!(got, rec["sha256"].as_str().unwrap(), "block {lo:x}")
        });
    }
    assert_eq!(covered, 0x110000, "every code point");
    ledger.finish(lines.len() - 1);
}

/// The rows of another file in `credentials_gate.jsonl`'s form, named by
/// `BM_CREDENTIALS_GATE_CASES` (a differential run against the reference
/// writes one). Every differing row is printed before the test fails.
#[test]
#[ignore = "runs the rows a BM_CREDENTIALS_GATE_CASES file names"]
fn cases_from_env() {
    let path = std::env::var("BM_CREDENTIALS_GATE_CASES").expect("BM_CREDENTIALS_GATE_CASES");
    let lines = jsonl(&path);
    check_header(&lines[0]);
    let mut differing = 0;
    for (i, row) in lines[1..].iter().enumerate() {
        let label = format!("row {i}");
        if std::panic::catch_unwind(|| check(row, &label)).is_err() {
            differing += 1;
        }
    }
    assert_eq!(differing, 0, "{path}: rows that differ");
    common::assert_count(&path, lines.len() - 1, lines.len() - 1);
}
