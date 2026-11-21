//! Every case of `pysyntax_literal.jsonl` (not gated) and of
//! `pysyntax_literal_python.jsonl` (mixed real and complex arithmetic,
//! gated on the Python release) against `pysyntax::literal`: the canonical text of what
//! `ast.literal_eval` returns, or a refusal. The spelling of texts and
//! answers is the generator's (`tests/contract/gen_pysyntax_literal.py`).

mod common;

use bettermemory_core::pysyntax::literal::{canonical_repr, literal_eval, LiteralError};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// A text as the generator spells it (`gen_pysyntax._built`).
fn text(v: &Value) -> Vec<u32> {
    match v {
        Value::String(s) => s.chars().map(u32::from).collect(),
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

/// `gen_pysyntax._digest`.
fn digest(t: &[u32]) -> Value {
    if t.len() > 80 {
        let bytes: Vec<u8> = t.iter().flat_map(|c| c.to_le_bytes()).collect();
        return json!({"n": t.len(), "h": common::hex(&Sha256::digest(bytes))});
    }
    if t.windows(2).any(|w| w == [0x5C, 0x5C]) || t.iter().any(|c| (0xD800..=0xDFFF).contains(c)) {
        return json!(t);
    }
    json!(t
        .iter()
        .map(|&c| char::from_u32(c).unwrap())
        .collect::<String>())
}

fn answer(t: &[u32]) -> Result<Vec<u32>, LiteralError> {
    canonical_repr(&literal_eval(t)?)
}

fn check(row: &Value) {
    let t = text(&row["t"]);
    let got = answer(&t);
    match &row["v"] {
        Value::Null => assert!(
            matches!(got, Err(LiteralError::Invalid(_))),
            "accepted {}: {got:?}",
            row["t"]
        ),
        want => match got {
            Ok(r) => assert_eq!(&digest(&r), want, "text {}", row["t"]),
            Err(e) => panic!("refused {}: {e:?}", row["t"]),
        },
    }
}

const PIECES: &[&str] = &[
    "1", "0", "-", "+", "1.0", "-0.0", "1j", "True", "None", "'a'", "b'a'", "(", ")", "[", "]",
    "{", "}", ",", ":", "set", "()", " ", "...", "2", "0.5",
];

/// The generator's `sweep_cases` over `pieces`.
fn sweep_cases(count: usize, pieces: &[&str]) -> Vec<Vec<u32>> {
    let mut state: u64 = 0x2545_F491_4F6C_DD1D;
    let mut draw = || {
        state = state
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        state >> 33
    };
    (0..count)
        .map(|_| {
            let n = 1 + draw() % 8;
            (0..n)
                .flat_map(|_| pieces[(draw() % pieces.len() as u64) as usize].chars())
                .map(u32::from)
                .collect()
        })
        .collect()
}

fn sweep(row: &Value) {
    let real: Vec<&str> = PIECES.iter().copied().filter(|p| *p != "1j").collect();
    let pieces: &[&str] = match row["sweep"].as_str().unwrap() {
        "pieces" => PIECES,
        "real" => &real,
        other => panic!("sweep {other}"),
    };
    let answers: Vec<String> = sweep_cases(20000, pieces)
        .iter()
        .map(|t| match answer(t) {
            Ok(r) => r.iter().map(|&c| char::from_u32(c).unwrap()).collect(),
            Err(LiteralError::Invalid(_)) => "E".to_string(),
            Err(e) => panic!("deferred: {e:?}"),
        })
        .collect();
    assert_eq!(
        answers.len() as u64,
        row["count"].as_u64().unwrap(),
        "count"
    );
    let h = common::hex(&Sha256::digest(answers.join("\n").as_bytes()));
    assert_eq!(h, row["sha256"].as_str().unwrap(), "digest");
}

fn every_case(test: &str, name: &str, gated: bool) {
    let rows = jsonl(name);
    let mut ledger = Ledger::new(test, name);
    for (i, row) in rows.iter().enumerate() {
        let label = format!("{name}:{}", i + 1);
        if i == 0 {
            ledger.case(label, || {
                if gated {
                    assert_eq!(row["python"], "3.13.13");
                    assert!(row.get("declined").is_none(), "declined: {row}");
                } else {
                    assert!(row.get("python").is_none(), "{row}");
                }
                assert_eq!(row["rows"].as_u64().unwrap() as usize, rows.len() - 1);
            });
        } else if row.get("sweep").is_some() {
            ledger.case(label, || sweep(row));
        } else {
            ledger.case(format!("{label} {}", row["t"]), || check(row));
        }
    }
    ledger.finish(rows.len());
}

#[test]
fn pysyntax_literal_every_case() {
    every_case(
        "pysyntax_literal_every_case",
        "pysyntax_literal.jsonl",
        false,
    );
}

#[test]
fn pysyntax_literal_python_every_case() {
    every_case(
        "pysyntax_literal_python_every_case",
        "pysyntax_literal_python.jsonl",
        true,
    );
}
