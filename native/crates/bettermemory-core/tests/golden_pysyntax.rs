//! Every case of `pysyntax_python.jsonl` (gated on the Python release) and
//! of `pysyntax.jsonl` (the values alone, not gated) against
//! `pysyntax::lex`. The spelling of texts and answers is the generator's
//! (`tests/contract/gen_pysyntax.py`).

mod common;

use bettermemory_core::pysyntax::lex::{literal_tokens, LexError, Lexed, Tok, Token};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// A text as the generator spells it (`_built`).
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

/// The generator's `_digest` (and `_spelled` below 81 code points).
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

fn bits(x: f64) -> String {
    common::hex(&x.to_be_bytes())
}

fn tokens(lexed: &Lexed) -> Value {
    Value::Array(
        lexed
            .tokens
            .iter()
            .map(|t| match t.tok {
                Tok::FString => json!(["FSTRING", null]),
                _ => json!([t.type_name(), digest(&lexed.source[t.start..t.end])]),
            })
            .collect(),
    )
}

/// The generator's `_value` of a single literal token, or None.
fn value(t: &Token) -> Option<Value> {
    Some(match &t.tok {
        Tok::Int(d) if d.len() > 80 => {
            json!({"int": {"n": d.len(), "h": common::hex(&Sha256::digest(d.as_bytes()))}})
        }
        Tok::Int(d) => json!({ "int": d }),
        Tok::Float(x) => json!({ "float": bits(*x) }),
        Tok::Imag(x) => json!({"complex": [bits(0.0), bits(*x)]}),
        Tok::Str(s) => json!({ "str": digest(s) }),
        Tok::Bytes(b) if b.len() > 40 => {
            json!({"bytes": {"n": b.len(), "h": common::hex(&Sha256::digest(b))}})
        }
        Tok::Bytes(b) => json!({ "bytes": common::hex(b) }),
        _ => return None,
    })
}

fn check(row: &Value) {
    let t = text(&row["t"]);
    let row_text = &row["t"];
    let got = literal_tokens(&t);
    let p = row["p"].as_str().unwrap();
    match row["d"].as_str() {
        Some("ident") => assert_eq!(got, Err(LexError::NeedsIdentTable)),
        Some("names") => assert_eq!(got, Err(LexError::NeedsNameTable)),
        Some(other) => panic!("no table {other}"),
        None => match &got {
            // A refusal is a text literal_eval refuses; where the parse
            // reads it, only a name past ASCII (no name but the four the
            // deferred table settles evaluates).
            Err(LexError::Syntax(k)) => {
                assert!(row["v"].get("raised").is_some(), "refused {row_text}: {k}");
                assert!(
                    p != "ok" || *k == "name past ASCII",
                    "refused {row_text}: {k}"
                );
            }
            Err(e) => panic!("deferred without a mark {row_text}: {e:?}"),
            // Lexing stops at an f-string, which the literal reader refuses
            // whatever follows it.
            Ok(l) => assert!(
                p != "lex" || l.tokens.last().is_some_and(|t| t.tok == Tok::FString),
                "lexed {row_text}"
            ),
        },
    }
    if let Ok(lexed) = &got {
        assert_eq!(tokens(lexed), row["k"], "tokens of {row_text}");
        if let [one] = lexed.tokens.as_slice() {
            if let Some(v) = value(one) {
                assert_eq!(v, row["v"], "value of {row_text}");
            }
        }
    }
}

fn hexes(t: &[u32]) -> String {
    t.iter()
        .map(|c| format!("{c:x}"))
        .collect::<Vec<_>>()
        .join("-")
}

/// The generator's `_answer_literal` through the port.
fn answer_literal(t: &[u32]) -> String {
    match literal_tokens(t) {
        Err(_) => "E".into(),
        Ok(l) => match l.tokens.as_slice() {
            [Token {
                tok: Tok::Str(s), ..
            }] => format!("s:{}", hexes(s)),
            [Token {
                tok: Tok::Bytes(b), ..
            }] => format!("b:{}", common::hex(b)),
            _ => "?".into(),
        },
    }
}

/// The generator's `_answer_number` through the port.
fn answer_number(t: &[u32]) -> String {
    match literal_tokens(t) {
        Err(_) => "E".into(),
        Ok(l) => match l.tokens.as_slice() {
            [Token {
                tok: Tok::Int(d), ..
            }] => format!("i:{d}"),
            [Token {
                tok: Tok::Float(x), ..
            }] => format!("f:{}", bits(*x)),
            [Token {
                tok: Tok::Imag(x), ..
            }] => format!("j:{}", bits(*x)),
            _ => "M".into(),
        },
    }
}

const ALPHABET: &[u8] = b"0123456789_.eEjJxXoObBaAfF+-";

/// The generator's `number_cases`.
fn number_cases(count: usize) -> Vec<Vec<u32>> {
    let mut state: u64 = 0x2545_F491_4F6C_DD1D;
    let mut draw = || {
        state = state
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        state >> 33
    };
    (0..count)
        .map(|_| {
            let n = 1 + draw() % 7;
            (0..n)
                .map(|_| u32::from(ALPHABET[(draw() % ALPHABET.len() as u64) as usize]))
                .collect()
        })
        .collect()
}

fn sweep(row: &Value) {
    let answers: Vec<String> = match row["sweep"].as_str().unwrap() {
        "escapes" => {
            let mut out = Vec::new();
            for prefix in ["", "b", "r", "rb"] {
                for cp in 0..0x250u32 {
                    for suffix in ["", "7", "41"] {
                        let mut t: Vec<u32> = prefix.chars().map(u32::from).collect();
                        t.extend([0x27, 0x5C, cp]);
                        t.extend(suffix.chars().map(u32::from));
                        t.push(0x27);
                        out.push(answer_literal(&t));
                    }
                }
            }
            out
        }
        "numbers" => number_cases(20000)
            .iter()
            .map(|t| answer_number(t))
            .collect(),
        // `_answer_value`: a text without a sign that literal_eval reads
        // as a number is one NUMBER token; anything else is `E` (`...`
        // reads as Ellipsis).
        "values" => number_cases(20000)
            .iter()
            .filter(|t| !t.contains(&0x2B) && !t.contains(&0x2D))
            .map(|t| match answer_number(t).as_str() {
                "M" => "E".to_string(),
                a => a.to_string(),
            })
            .collect(),
        other => panic!("no sweep {other}"),
    };
    assert_eq!(
        answers.len() as u64,
        row["count"].as_u64().unwrap(),
        "count"
    );
    let h = common::hex(&Sha256::digest(answers.join("\n").as_bytes()));
    assert_eq!(h, row["sha256"].as_str().unwrap(), "digest");
}

/// A row of `pysyntax.jsonl`: a refusal is a text literal_eval refuses,
/// and a single literal token holds the value literal_eval returns.
fn check_value(row: &Value) {
    let t = text(&row["t"]);
    let row_text = &row["t"];
    match literal_tokens(&t) {
        Err(LexError::Syntax(k)) => {
            assert!(row["v"].get("raised").is_some(), "refused {row_text}: {k}");
        }
        Err(e) => panic!("deferred {row_text}: {e:?}"),
        Ok(lexed) => {
            if let [one] = lexed.tokens.as_slice() {
                if let Some(v) = value(one) {
                    assert_eq!(v, row["v"], "value of {row_text}");
                }
            }
        }
    }
}

#[test]
fn pysyntax_values() {
    let name = "pysyntax.jsonl";
    let rows = jsonl(name);
    let mut ledger = Ledger::new("pysyntax_values", name);
    for (i, row) in rows.iter().enumerate() {
        let label = format!("{name}:{}", i + 1);
        if i == 0 {
            ledger.case(label, || {
                assert_eq!(row["fixture"], "pysyntax");
                assert_eq!(row["rows"].as_u64().unwrap() as usize, rows.len() - 1);
            });
        } else if row.get("sweep").is_some() {
            ledger.case(label, || sweep(row));
        } else {
            ledger.case(format!("{label} {}", row["t"]), || check_value(row));
        }
    }
    ledger.finish(rows.len());
}

#[test]
fn pysyntax_every_case() {
    let name = "pysyntax_python.jsonl";
    let rows = jsonl(name);
    let mut ledger = Ledger::new("pysyntax_every_case", name);
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
            ledger.case(format!("{label} {}", row["t"]), || check(row));
        }
    }
    ledger.finish(rows.len());
}
