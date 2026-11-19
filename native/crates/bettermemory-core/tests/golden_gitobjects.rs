//! Every case of `gitobjects.jsonl` (class i) and `gitobjects_python.jsonl`
//! (class ii, Python) against `gitobjects`: the loose and pack entry
//! headers, the varints, a delta's sizes and its application, the commit
//! and tree parsers, the committer time, the parent lines, the
//! fingerprint, the C-quoted alternates and a version 2 pack index.

mod common;

use std::collections::BTreeSet;

use bettermemory_core::gitobjects::{self as g, Base, Raised, Wide};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

/// The generator's `_sp`, read back: hex text, or pieces of hex text and
/// `[byte hex, count]` runs.
fn content(v: &Value) -> Vec<u8> {
    match v {
        Value::String(s) => common::unhex(s),
        Value::Array(pieces) => {
            let mut out = Vec::new();
            for piece in pieces {
                match piece {
                    Value::String(s) => out.extend(common::unhex(s)),
                    Value::Array(run) => {
                        let byte = common::unhex(run[0].as_str().unwrap())[0];
                        let n = run[1].as_u64().unwrap() as usize;
                        out.extend(std::iter::repeat_n(byte, n));
                    }
                    other => panic!("a piece {other}"),
                }
            }
            out
        }
        other => panic!("bytes spelled {other}"),
    }
}

/// `_sp` of bytes.
fn sp(data: &[u8]) -> Value {
    let mut pieces = Vec::new();
    let mut plain = Vec::new();
    let mut i = 0;
    while i < data.len() {
        let mut j = i;
        while j < data.len() && data[j] == data[i] {
            j += 1;
        }
        if j - i >= 32 {
            if !plain.is_empty() {
                pieces.push(json!(common::hex(&std::mem::take(&mut plain))));
            }
            pieces.push(json!([common::hex(&data[i..=i]), j - i]));
        } else {
            plain.extend_from_slice(&data[i..j]);
        }
        i = j;
    }
    if pieces.is_empty() {
        return json!(common::hex(data));
    }
    if !plain.is_empty() {
        pieces.push(json!(common::hex(&plain)));
    }
    Value::Array(pieces)
}

fn raised(r: Raised) -> Value {
    let name = match r {
        Raised::ValueError => "ValueError",
        Raised::IndexError => "IndexError",
        Raised::StructError => "error",
    };
    json!({ "raise": name })
}

fn w(v: &Wide) -> Value {
    json!(v.hex())
}

fn n(case: &Value, key: &str) -> usize {
    case[key].as_u64().unwrap() as usize
}

fn answer(case: &Value) -> Value {
    let b = || content(&case["b"]);
    match case["f"].as_str().unwrap() {
        "lh" => match g::loose_header(&b()) {
            Err(r) => raised(r),
            Ok(None) => Value::Null,
            Ok(Some(l)) => json!([l.kind, w(&l.size), l.start]),
        },
        "vi" => g::varint(&b(), n(case, "i")).map_or(Value::Null, |(v, i)| json!([w(&v), i])),
        "eh" => {
            g::entry_header(&b(), n(case, "h")).map_or(Value::Null, |(code, size, base, len)| {
                let base = match base {
                    Base::None => Value::Null,
                    Base::Distance(d) => json!({ "d": d.hex() }),
                    Base::Name(name) => json!({ "n": common::hex(&name) }),
                };
                json!([code, w(&size), base, len])
            })
        }
        "ds" => g::delta_sizes(&b()).map_or(Value::Null, |(s, t, i)| json!([w(&s), w(&t), i])),
        "ad" => g::apply_delta(&b(), &content(&case["c"])).map_or(Value::Null, |out| sp(&out)),
        "hn" => json!(g::hex_name(&b(), n(case, "h"))),
        "ha" => json!(g::hex_at(&b(), n(case, "i"), n(case, "h"))),
        "pc" => g::parse_commit(&b(), n(case, "h")).map_or(Value::Null, |c| {
            json!([
                c.tree,
                c.parents,
                common::hex(&c.author),
                common::hex(&c.committer),
                common::hex(&c.message)
            ])
        }),
        "pt" => g::parse_tree(&b(), n(case, "h")).map_or(Value::Null, |entries| {
            entries
                .iter()
                .map(|e| json!([e.mode, common::hex(&e.name), e.sha]))
                .collect()
        }),
        "ca" => json!(g::committed_at(&b(), n(case, "h")).to_string()),
        "pl" => g::parent_lines(&b(), n(case, "h")).map_or(Value::Null, |(p, at)| json!([p, at])),
        "fp" => json!(g::commit_fingerprint(&b())),
        "uq" => g::unquote_c(&b(), n(case, "i"))
            .map_or(Value::Null, |(t, end)| json!([common::hex(&t), end])),
        "ae" => g::alternate_entries(&b())
            .iter()
            .map(|e| json!(common::hex(e)))
            .collect(),
        "ix" => {
            let Some(index) = g::parse_index(b(), b"p.pack".to_vec(), n(case, "h")) else {
                return Value::Null;
            };
            assert_eq!(index.pack, b"p.pack");
            case["q"]
                .as_array()
                .unwrap()
                .iter()
                .map(|q| match q[0].as_str().unwrap() {
                    "f" => {
                        let raw = common::unhex(q[1].as_str().unwrap());
                        index.find(&raw).map_or_else(raised, |p| json!(p))
                    }
                    "o" => index
                        .offset(q[1].as_u64().unwrap() as usize)
                        .map_or_else(raised, |o| json!(o)),
                    _ => index
                        .starting(q[1].as_str().unwrap(), q[2].as_i64().unwrap())
                        .map_or_else(raised, |names| {
                            names.iter().map(|x| json!(common::hex(x))).collect()
                        }),
                })
                .collect()
        }
        other => panic!("a case of kind {other}"),
    }
}

fn every_case(file: &str, test: &str, kinds: usize) {
    let lines = jsonl(file);
    let cases = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    assert!(!cases.is_empty(), "{file}: no case");
    let ledger = Ledger::new(test, file);
    let mut failures = Vec::new();
    let mut seen = BTreeSet::new();
    for (i, case) in cases.iter().enumerate() {
        seen.insert(case["f"].as_str().unwrap().to_owned());
        let got = answer(case);
        if got != case["r"] {
            let mut shown = case.to_string();
            shown.truncate(400);
            failures.push(format!("#{i} {shown}: native {got}"));
        }
    }
    assert_eq!(seen.len(), kinds, "{file}: every kind of case is present");
    ledger.settle(cases.len(), &failures);
}

#[test]
fn gitobjects_every_case() {
    every_case("gitobjects.jsonl", "golden_gitobjects", 15);
}

#[test]
fn gitobjects_python_every_case() {
    every_case("gitobjects_python.jsonl", "golden_gitobjects_python", 1);
}
