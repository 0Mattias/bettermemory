//! `unicode_names.jsonl`: the digest of every name and alias the table
//! holds (`unicode::names::stored`, each looked up), the digest of every
//! name a rule makes (built here from the header's short names and
//! ranges, each looked up), and each whole text through `literal_eval`.

mod common;

use bettermemory_core::pysyntax::literal::{canonical_repr, literal_eval, Literal};
use bettermemory_core::unicode::names::{lookup, stored};
use common::parity::Ledger;
use common::{jsonl, text_field};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const NAME: &str = "unicode_names.jsonl";

fn cps(text: &str) -> Vec<u32> {
    text.chars().map(u32::from).collect()
}

/// The generator's `_digest` over `(name, code point)` pairs.
fn digest(pairs: &[(String, Option<u32>)]) -> (usize, String) {
    let lines: Vec<String> = pairs
        .iter()
        .map(|(name, cp)| match cp {
            Some(cp) => format!("{name}\t{cp}"),
            None => format!("{name}\tNone"),
        })
        .collect();
    let h = common::hex(&Sha256::digest(lines.join("\n").as_bytes()));
    (pairs.len(), h)
}

fn check_digest(pinned: &Value, pairs: &[(String, Option<u32>)]) {
    let (count, h) = digest(pairs);
    assert_eq!(count as u64, pinned["count"].as_u64().unwrap(), "count");
    assert_eq!(h, pinned["sha256"].as_str().unwrap(), "sha256");
}

/// Every name and alias of the table, by name, with what it looks up to.
fn table() -> Vec<(String, Option<u32>)> {
    stored()
        .map(|(name, cp)| {
            let got = lookup(&cps(name));
            assert_eq!(got, Some(cp), "{name}");
            (name.to_string(), got)
        })
        .collect()
}

/// Every rule-made name, from the header's own short names and ranges, in
/// code point order, with what it looks up to.
fn ruled(pinned: &Value) -> Vec<(String, Option<u32>)> {
    let column = |i: usize| -> Vec<String> {
        pinned["jamo"][i]
            .as_array()
            .unwrap()
            .iter()
            .map(|j| j.as_str().unwrap().to_string())
            .collect()
    };
    let (l, v, t) = (column(0), column(1), column(2));
    let mut names: Vec<(String, u32)> = Vec::new();
    for i in 0..l.len() * v.len() * t.len() {
        let name = format!(
            "HANGUL SYLLABLE {}{}{}",
            l[i / (v.len() * t.len())],
            v[i / t.len() % v.len()],
            t[i % t.len()]
        );
        names.push((name, 0xAC00 + i as u32));
    }
    for range in pinned["ranges"].as_array().unwrap() {
        let prefix = range[0].as_str().unwrap();
        let (first, last) = (range[1].as_u64().unwrap(), range[2].as_u64().unwrap());
        for cp in first as u32..=last as u32 {
            names.push((format!("{prefix}{cp:04X}"), cp));
        }
    }
    names.sort_by_key(|&(_, cp)| cp);
    names
        .into_iter()
        .map(|(name, _)| {
            let got = lookup(&cps(&name));
            (name, got)
        })
        .collect()
}

/// `literal_eval`'s answer in the generator's spelling.
fn answer(text: &[u32]) -> Value {
    match literal_eval(text) {
        Ok(Literal::Str(s)) => json!({ "str": s }),
        Ok(other) => match canonical_repr(&other) {
            // Spelled as code points where it holds two backslashes.
            Ok(r) if r.windows(2).any(|w| w == [0x5C, 0x5C]) => json!({ "repr_cps": r }),
            Ok(r) => {
                json!({ "repr": r.iter().map(|&c| char::from_u32(c).unwrap()).collect::<String>() })
            }
            Err(e) => panic!("no canonical text: {e:?}"),
        },
        Err(_) => json!("raised"),
    }
}

fn check(row: &Value) {
    let t = text_field(row, "t");
    let got = answer(&t);
    let want = &row["v"];
    if want.get("raised").is_some() {
        assert_eq!(got, json!("raised"), "{}", row["t"]);
    } else {
        assert_eq!(&got, want, "{}", row["t"]);
    }
}

#[test]
fn unicode_names_every_record() {
    let rows = jsonl(NAME);
    let mut ledger = Ledger::new("unicode_names_every_record", NAME);
    for (i, row) in rows.iter().enumerate() {
        let label = format!("{NAME}:{}", i + 1);
        if i == 0 {
            ledger.case(label, || {
                assert_eq!(row["fixture"], "unicode_names");
                assert!(row.get("declined").is_none(), "declined: {row}");
                assert_eq!(row["unidata_version"], "15.1.0");
                assert_eq!(row["rows"].as_u64().unwrap() as usize, rows.len() - 1);
                check_digest(&row["table"], &table());
                check_digest(&row["ruled"], &ruled(&row["ruled"]));
            });
        } else {
            ledger.case(format!("{label} {}", row["t"]), || check(row));
        }
    }
    ledger.finish(rows.len());
}
