//! Every case of `pysyntax_fstring.jsonl` (gated on the Python release)
//! against `pysyntax::module_lex` and its f-string modes: a source the
//! tokenizer of 3.13.13 refuses is refused, any other is read into the
//! tokens of the parser's mode, kind by kind with spans. Only a source
//! holding a `\N{...}` escape may be declined. A row whose tokens the
//! generator could not judge (`"tok": "unjudged"`, the C entry's
//! SystemError on a source `ast.parse` accepts) is held to acceptance.
//! The spelling is the generator's (`tests/contract/gen_pysyntax_fstring.py`).

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

/// The tokens as the generator spells them (`_spelled_tokens`): the
/// parser's mode reads no COMMENT and no NL.
pub fn spelled(m: &Module) -> Vec<String> {
    m.tokens
        .iter()
        .filter_map(|t| match t.type_name() {
            "COMMENT" | "NL" => None,
            n @ ("NEWLINE" | "INDENT" | "DEDENT" | "ENDMARKER") => Some(n.to_string()),
            n => Some(format!("{n}:{}:{}", t.start, t.end)),
        })
        .collect()
}

fn row_tokens(v: &Value) -> Vec<String> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|t| {
            t.as_array()
                .unwrap()
                .iter()
                .map(|x| match x {
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                })
                .collect::<Vec<_>>()
                .join(":")
        })
        .collect()
}

/// True when the case was checked, false when the reader declined it.
fn check(row: &Value) -> bool {
    let t = text(&row["t"]);
    let got = module_tokens(&t);
    if let Err(ModuleError::NeedsNameTable) = got {
        assert!(
            t.windows(2).any(|w| w == [0x5C, 0x4E]),
            "declined {}",
            row["t"]
        );
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
        Value::String(s) if s == "unjudged" => {}
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

const OPENERS: &[&str] = &["f'", "f\"", "f'''", "rf'", "F\"\"\"", "fr'"];
const PIECES: &[&str] = &[
    "a",
    "a b",
    "{x}",
    "{x}",
    "{{",
    "}}",
    "{x!r}",
    "{x:>3}",
    "{x:{y}}",
    "{x=}",
    "{",
    "}",
    "{x",
    ":",
    "!",
    "!r",
    "=",
    "!=",
    ":=",
    "x",
    "(",
    ")",
    "[",
    "]",
    "'",
    "\"",
    "\\",
    "\\x41",
    "\\x",
    "\\{",
    "\n",
    " ",
    "# c",
    "{'a'}",
    "{f'{x}'}",
    "{f\"{x}\"}",
    "{lambda: 1}",
    "\\\n",
];
const TAILS: &[&str] = &["\n", " y\n", "", "\nz\n", " + f'{x}'\n", ")\n"];

/// The generator's `sweep_cases`.
pub fn sweep_cases(count: usize) -> Vec<Vec<u32>> {
    let mut state: u64 = 0x9E37_79B9_7F4A_7C15;
    let mut draw = || {
        state = state
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        (state >> 33) as usize
    };
    (0..count)
        .map(|_| {
            let opener = OPENERS[draw() % OPENERS.len()];
            let n = 1 + draw() % 8;
            let mut t = opener.to_string();
            for _ in 0..n {
                t.push_str(PIECES[draw() % PIECES.len()]);
            }
            if draw() % 8 != 0 {
                t.push_str(opener.trim_start_matches(['f', 'F', 'r', 'R']));
            } else {
                t.push_str(PIECES[draw() % PIECES.len()]);
            }
            t.push_str(TAILS[draw() % TAILS.len()]);
            t.chars().map(u32::from).collect()
        })
        .collect()
}

/// One sweep source's answer line (`_answer_line`).
pub fn answer_line(t: &[u32]) -> String {
    match module_tokens(t) {
        Ok(m) => spelled(&m).join(" "),
        Err(ModuleError::Syntax(_)) => "E".to_string(),
        Err(e) => panic!("declined: {e:?}"),
    }
}

fn sweep(row: &Value) {
    assert_eq!(row["sweep"], "fstring_pieces");
    let answers: Vec<String> = sweep_cases(20000).iter().map(|t| answer_line(t)).collect();
    assert_eq!(answers.len() as u64, row["count"].as_u64().unwrap());
    let h = common::hex(&Sha256::digest(answers.join("\n").as_bytes()));
    assert_eq!(h, row["sha256"].as_str().unwrap(), "digest");
}

#[test]
fn pysyntax_fstring_every_case() {
    let name = "pysyntax_fstring.jsonl";
    let rows = jsonl(name);
    let mut ledger = Ledger::new("pysyntax_fstring_every_case", name);
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
        assert!(checked > 150, "checked {checked}, declined {declined}");
        assert!(declined <= 3, "declined {declined}");
    }
    ledger.finish(rows.len());
}
