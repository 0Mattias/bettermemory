//! Every case of `claim_drift.jsonl` (not gated), `claim_drift_unicode.jsonl`
//! (gated on the Unicode version) and `claim_drift_python.jsonl` (not
//! gated: the digit limit is the same on every release) against
//! `claim_drift`. The literal reader replays
//! the answers each row records; the spelling of texts and renders is the
//! generator's (`tests/contract/gen_claim_drift.py`).

mod common;

use std::collections::HashMap;

use bettermemory_core::claim_drift::{build_binding_index, claim_level_drift, Index, LiteralLines};
use bettermemory_core::claims::{Claim, IntTooLong, Kind};
use bettermemory_core::json::{dumps, DumpOptions};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const COMPACT: DumpOptions = DumpOptions {
    sort_keys: false,
    ensure_ascii: true,
    item_separator: ",",
    key_separator: ":",
    indent: None,
    default_str: false,
};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

/// A text as the generator spells it: a string, a code point list, or a
/// recipe `{"r": [[text, n], ...]}`.
fn text(v: &Value) -> Vec<u32> {
    match v {
        Value::String(s) => cps(s),
        Value::Array(items) => items.iter().map(|c| c.as_u64().unwrap() as u32).collect(),
        Value::Object(_) => {
            let mut out = Vec::new();
            for pair in v["r"].as_array().unwrap() {
                let piece = text(&pair[0]);
                for _ in 0..pair[1].as_u64().unwrap() {
                    out.extend_from_slice(&piece);
                }
            }
            out
        }
        other => panic!("not a text: {other:?}"),
    }
}

fn opt(v: &Value) -> Option<PyStr> {
    (!v.is_null()).then(|| text(v))
}

fn dump(v: &PyValue) -> String {
    dumps(v, COMPACT)
        .unwrap()
        .into_iter()
        .map(|c| char::from_u32(c).unwrap())
        .collect()
}

/// The recorded literal answers; a question no row recorded fails.
#[derive(Default)]
struct Replay {
    frag: HashMap<PyStr, Option<PyStr>>,
    rhs: HashMap<PyStr, Option<PyStr>>,
    anchors: HashMap<PyStr, Vec<PyStr>>,
    claimed: HashMap<PyStr, Option<PyStr>>,
}

impl Replay {
    fn pairs(into: &mut HashMap<PyStr, Option<PyStr>>, v: &Value) {
        for pair in v.as_array().unwrap() {
            into.insert(text(&pair[0]), opt(&pair[1]));
        }
    }

    fn claims(&mut self, v: &Value) {
        for c in v.as_array().unwrap() {
            let value = text(&c["claim"][3]);
            let anchors = c["anchors"].as_array().unwrap().iter().map(text).collect();
            self.anchors.insert(value.clone(), anchors);
            self.claimed.insert(value, opt(&c["claimed"]));
        }
    }
}

fn asked<T: Clone>(map: &HashMap<PyStr, T>, key: &[u32], what: &str) -> T {
    map.get(key)
        .unwrap_or_else(|| panic!("{what} of {key:?} was not recorded"))
        .clone()
}

impl LiteralLines for Replay {
    fn string_fragment(&self, content: &[u32]) -> Option<PyStr> {
        asked(&self.frag, content, "string_fragment")
    }
    fn rhs_repr(&self, content: &[u32]) -> Option<PyStr> {
        asked(&self.rhs, content, "_rhs_repr")
    }
    fn anchors_from_value(&self, value: &[u32]) -> Vec<PyStr> {
        asked(&self.anchors, value, "anchors_from_value")
    }
    fn claimed_str(&self, value: &[u32]) -> Option<PyStr> {
        asked(&self.claimed, value, "literal_eval")
    }
}

/// Answers None and nothing: for streams whose lines ask nothing that
/// is read.
struct Silent;

impl LiteralLines for Silent {
    fn string_fragment(&self, _: &[u32]) -> Option<PyStr> {
        None
    }
    fn rhs_repr(&self, _: &[u32]) -> Option<PyStr> {
        None
    }
    fn anchors_from_value(&self, _: &[u32]) -> Vec<PyStr> {
        Vec::new()
    }
    fn claimed_str(&self, _: &[u32]) -> Option<PyStr> {
        None
    }
}

fn claim_of(v: &Value) -> Claim {
    let kind = match v[0].as_str().unwrap() {
        "path" => Kind::Path,
        "symbol" => Kind::Symbol,
        "literal" => Kind::Literal,
        "absent" => Kind::Absent,
        other => panic!("no kind {other}"),
    };
    Claim {
        kind,
        rel_path: text(&v[1]),
        name: text(&v[2]),
        value: text(&v[3]),
    }
}

fn lcg(state: u64) -> u64 {
    state
        .wrapping_mul(6364136223846793005)
        .wrapping_add(1442695040888963407)
}

/// The generator's `sweep_streams`.
fn sweep_streams(vocab: &[PyStr], seed: u64, count: usize, lines: usize) -> Vec<PyStr> {
    let mut state = seed;
    (0..count)
        .map(|_| {
            let mut out = Vec::new();
            for k in 0..lines {
                state = lcg(state);
                if k > 0 {
                    out.push(0x0A);
                }
                out.extend_from_slice(&vocab[((state >> 33) % vocab.len() as u64) as usize]);
            }
            out
        })
        .collect()
}

#[test]
fn claim_drift_every_case() {
    let name = "claim_drift.jsonl";
    let rows = jsonl(name);
    let mut ledger = Ledger::new("claim_drift_every_case", name);
    let mut vocab_row: Option<Value> = None;
    for (i, row) in rows.iter().enumerate() {
        let label = format!("{name}:{} {}", i + 1, row["f"].as_str().unwrap());
        match row["f"].as_str().unwrap() {
            "stream" => {
                let mut lit = Replay::default();
                Replay::pairs(&mut lit.frag, &row["frag"]);
                Replay::pairs(&mut lit.rhs, &row["rhs"]);
                lit.claims(&row["claims"]);
                let ix = build_binding_index(&text(&row["stream"]), &lit).expect("no long count");
                let got = dump(&ix.render());
                ledger.case(label.clone(), || {
                    assert_eq!(got, row["index"].as_str().unwrap(), "{}", row["name"]);
                });
                for (k, c) in row["claims"].as_array().unwrap().iter().enumerate() {
                    let got = dump(&claim_level_drift(&claim_of(&c["claim"]), &ix, &lit).render());
                    ledger.case(format!("{label} claim {k}"), || {
                        assert_eq!(got, c["o"].as_str().unwrap(), "{:?}", c["claim"]);
                    });
                }
            }
            "vocab" => {
                vocab_row = Some(row.clone());
                ledger.case(label, || {});
            }
            "sweep" => {
                let v = vocab_row.as_ref().expect("the vocab row comes first");
                let mut lit = Replay::default();
                Replay::pairs(&mut lit.frag, &v["frag"]);
                Replay::pairs(&mut lit.rhs, &v["rhs"]);
                lit.claims(&v["claims"]);
                let claims: Vec<Claim> = v["claims"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|c| claim_of(&c["claim"]))
                    .collect();
                let vocab: Vec<PyStr> = v["lines"].as_array().unwrap().iter().map(text).collect();
                let count = row["count"].as_u64().unwrap() as usize;
                let streams = sweep_streams(
                    &vocab,
                    row["seed"].as_u64().unwrap(),
                    count,
                    row["lines"].as_u64().unwrap() as usize,
                );
                let mut h = Sha256::new();
                for s in &streams {
                    let ix: Index = build_binding_index(s, &lit).unwrap();
                    h.update(dump(&ix.render()).as_bytes());
                    h.update(b"\n");
                    for c in &claims {
                        h.update(dump(&claim_level_drift(c, &ix, &lit).render()).as_bytes());
                        h.update(b"\n");
                    }
                }
                let got = common::hex(&h.finalize());
                ledger.case(label, || {
                    assert_eq!(streams.len(), count);
                    assert_eq!(got, row["h"].as_str().unwrap());
                });
            }
            other => panic!("no row kind {other}"),
        }
    }
    let claims: usize = rows
        .iter()
        .filter(|r| r["f"] == "stream")
        .map(|r| r["claims"].as_array().unwrap().len())
        .sum();
    ledger.finish(rows.len() + claims);
}

#[test]
fn claim_drift_python_every_case() {
    let name = "claim_drift_python.jsonl";
    let rows = jsonl(name);
    let mut ledger = Ledger::new("claim_drift_python_every_case", name);
    for (i, row) in rows.iter().enumerate() {
        let got = match build_binding_index(&text(&row["stream"]), &Silent) {
            Err(IntTooLong) => json!({"raised": "ValueError"}),
            Ok(ix) => json!(dump(&ix.render())),
        };
        ledger.case(format!("{name}:{}", i + 1), || assert_eq!(got, row["o"]));
    }
    ledger.finish(rows.len());
}

fn unicode_stream(cp: u32) -> Vec<u32> {
    let mut s = cps("\u{1}aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\ndiff --git a/f b/f\n+++ b/f\n@@ -0,0 +1 @@\n+");
    s.push(cp);
    s.extend(cps("#abcdefghijkl"));
    s.push(cp);
    s.extend(cps("\n@@ -1,"));
    s.push(cp);
    s.extend(cps(" +1 @@\n-x\n+y"));
    s
}

fn unicode_answer(cp: u32) -> String {
    let ix = build_binding_index(&unicode_stream(cp), &Silent).unwrap();
    let mut keys: Vec<&PyStr> = ix
        .changed_text
        .get(&cps("f"))
        .map(|m| m.keys().collect())
        .unwrap_or_default();
    keys.sort();
    let keys: Vec<String> = keys
        .iter()
        .map(|k| {
            k.iter()
                .map(|c| format!("{c:x}"))
                .collect::<Vec<_>>()
                .join("-")
        })
        .collect();
    format!("{}|{},{}", keys.join(","), ix.hunks, ix.parse_mismatches)
}

#[test]
fn claim_drift_unicode_every_case() {
    let name = "claim_drift_unicode.jsonl";
    let rows = jsonl(name);
    let mut ledger = Ledger::new("claim_drift_unicode_every_case", name);
    for row in &rows {
        let lo = row["lo"].as_u64().unwrap() as u32;
        let hi = row["hi"].as_u64().unwrap() as u32;
        let mut h = Sha256::new();
        for cp in lo..hi {
            h.update(format!("{cp:x}:{}\n", unicode_answer(cp)).as_bytes());
        }
        let got = common::hex(&h.finalize());
        ledger.case(format!("{lo:x}..{hi:x}"), || {
            assert_eq!(got, row["h"].as_str().unwrap());
        });
    }
    ledger.finish(rows.len());
}
