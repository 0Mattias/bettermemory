//! Every case of `pysyntax_module.jsonl` (gated on the Python release)
//! against `pysyntax::module_lex`: a source the tokenizer of 3.13.13
//! refuses is refused, any other is read into the tokens `tokenize`
//! reads. A source holding an f-string with a field, or a `\N{...}`
//! escape, may be declined. The spelling is the generator's
//! (`tests/contract/gen_pysyntax_module.py`).

mod common;

use bettermemory_core::pysyntax::module_lex::{module_tokens, Module, ModuleError};
use common::jsonl;
use common::parity::Ledger;
use serde_json::Value;
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

/// The tokens as the generator spells them (`_spelled_tokens`).
fn spelled(m: &Module) -> Vec<String> {
    m.tokens
        .iter()
        .map(|t| match t.type_name() {
            n @ ("NEWLINE" | "NL" | "INDENT" | "DEDENT" | "ENDMARKER") => n.to_string(),
            n => format!("{n}:{}:{}", t.start, t.end),
        })
        .collect()
}

fn row_tokens(v: &Value) -> Vec<String> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|t| {
            let t = t.as_array().unwrap();
            t.iter()
                .map(|x| match x {
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                })
                .collect::<Vec<_>>()
                .join(":")
        })
        .collect()
}

/// Whether the reader may decline `t`: it holds an f-string prefix or
/// a `\N`.
fn may_decline(t: &[u32]) -> bool {
    t.windows(2)
        .any(|w| (w[0] | 0x20) == 0x66 && (w[1] == 0x27 || w[1] == 0x22))
        || t.windows(2).any(|w| w == [0x5C, 0x4E])
}

/// True when the case was checked, false when the reader declined it.
fn check(row: &Value) -> bool {
    let t = text(&row["t"]);
    let got = module_tokens(&t);
    if let Err(ModuleError::NeedsFString | ModuleError::NeedsNameTable) = got {
        assert!(may_decline(&t), "declined {}: {got:?}", row["t"]);
        return false;
    }
    if row["lex"].as_bool().unwrap() {
        assert!(
            matches!(got, Err(ModuleError::Syntax(_))),
            "accepted {}: {got:?}",
            row["t"]
        );
        return true;
    }
    let m = got.unwrap_or_else(|e| panic!("refused {}: {e:?}", row["t"]));
    let ours = spelled(&m);
    match &row["tok"] {
        Value::Null => {}
        Value::Object(d) => {
            assert_eq!(
                ours.len() as u64,
                d["n"].as_u64().unwrap(),
                "count {}",
                row["t"]
            );
            let h = common::hex(&Sha256::digest(ours.join(" ").as_bytes()));
            assert_eq!(h, d["h"].as_str().unwrap(), "digest {}", row["t"]);
        }
        want => assert_eq!(ours, row_tokens(want), "tokens {}", row["t"]),
    }
    true
}

const PIECES: &[&str] = &[
    "\n",
    "\n",
    "\n",
    " ",
    "    ",
    "\t",
    "\x0c",
    "\\\n",
    "# c",
    "x",
    "if y:",
    "1",
    "0x1",
    "1.5",
    "'a'",
    "'''b\n'''",
    "f'c'",
    "(",
    ")",
    "[",
    "]",
    ":",
    "=",
    "+",
    "\n  ",
    "\n\t",
    "\n    ",
    "\n        ",
    "\n \t",
    "\n\t ",
];

/// The generator's `sweep_cases`.
pub fn sweep_cases(count: usize) -> Vec<Vec<u32>> {
    let mut state: u64 = 0x2545_F491_4F6C_DD1D;
    let mut draw = || {
        state = state
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        state >> 33
    };
    (0..count)
        .map(|_| {
            let n = 1 + draw() % 12;
            (0..n)
                .flat_map(|_| PIECES[(draw() % PIECES.len() as u64) as usize].chars())
                .map(u32::from)
                .collect()
        })
        .collect()
}

fn sweep(row: &Value) {
    assert_eq!(row["sweep"], "pieces");
    let answers: Vec<String> = sweep_cases(20000)
        .iter()
        .map(|t| match module_tokens(t) {
            Ok(m) => spelled(&m).join(" "),
            Err(ModuleError::Syntax(_)) => "E".to_string(),
            Err(e) => panic!("declined: {e:?}"),
        })
        .collect();
    assert_eq!(answers.len() as u64, row["count"].as_u64().unwrap());
    let h = common::hex(&Sha256::digest(answers.join("\n").as_bytes()));
    assert_eq!(h, row["sha256"].as_str().unwrap(), "digest");
}

#[test]
fn pysyntax_module_every_case() {
    let name = "pysyntax_module.jsonl";
    let rows = jsonl(name);
    let mut ledger = Ledger::new("pysyntax_module_every_case", name);
    let mut checked = 0usize;
    let mut declined = 0usize;
    for (i, row) in rows.iter().enumerate() {
        let label = format!("{name}:{}", i + 1);
        if i == 0 {
            ledger.case(label, || {
                assert_eq!(row["python"], "3.13.13");
                assert!(row.get("declined").is_none(), "declined: {row}");
                assert_eq!(row["rows"].as_u64().unwrap() as usize, rows.len() - 1);
            });
        } else if row.get("sweep").is_some() {
            ledger.case(label, || sweep(row));
        } else {
            ledger.case(format!("{label} {}", row["t"]), || {
                if check(row) {
                    checked += 1;
                } else {
                    declined += 1;
                }
            });
        }
    }
    if ledger.strict() {
        assert!(checked > 200, "checked {checked}, declined {declined}");
        assert!(declined <= 5, "declined {declined}");
    }
    ledger.finish(rows.len());
}
