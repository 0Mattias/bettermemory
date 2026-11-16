//! Every case of `gitconfig_wildmatch.jsonl`: the reference's `wildmatch`
//! answer (match, no match, or spent by steps or by depth) and the steps
//! it took, for each pattern, text, case flag and budget; and its
//! `_any_match` and `_literal_head`, against the native matcher.

mod common;

use bettermemory_core::gitconfig::wildmatch::{
    any_match, literal_head, wildmatch, Spent, Steps, WM_DEPTH, WM_STEPS,
};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

/// A fixture byte string: `=` and itself, hex, or a recipe of pieces.
fn bytes_of(value: &Value) -> Vec<u8> {
    if let Some(text) = value.as_str() {
        return match text.strip_prefix('=') {
            Some(plain) => plain.as_bytes().to_vec(),
            None => common::unhex(text),
        };
    }
    let mut out = Vec::new();
    for piece in value.as_array().expect("a recipe") {
        let bytes = bytes_of(&piece[0]);
        for _ in 0..piece[1].as_u64().expect("a count") {
            out.extend_from_slice(&bytes);
        }
    }
    out
}

#[test]
fn gitconfig_wildmatch_every_case() {
    let lines = jsonl("gitconfig_wildmatch.jsonl");
    let header = &lines[0];
    assert_eq!(header["fixture"], "gitconfig_wildmatch");
    assert_eq!(header["steps"].as_i64(), Some(WM_STEPS));
    assert_eq!(header["depth"].as_u64(), Some(WM_DEPTH as u64));
    let ledger = Ledger::new("golden_gitconfig_wildmatch", "gitconfig_wildmatch.jsonl");
    let mut failures = Vec::new();
    let (mut cases, mut others) = (0u64, 0u64);
    for (i, record) in lines[1..].iter().enumerate() {
        let (got, want) = if let Some(row) = record.as_array() {
            cases += 1;
            let (pattern, text) = (bytes_of(&row[0]), bytes_of(&row[1]));
            let icase = row[2] == 1;
            let budget = row
                .get(5)
                .map_or(WM_STEPS, |b| b.as_i64().expect("a budget"));
            let mut steps = Steps { left: budget };
            let answer = match wildmatch(&pattern, &text, icase, &mut steps) {
                Ok(found) => json!(u8::from(found)),
                Err(Spent::Steps) => json!("s"),
                Err(Spent::Depth) => json!("d"),
            };
            (
                json!([answer, budget - steps.left]),
                json!([row[3], row[4]]),
            )
        } else if let Some(any) = record.get("any") {
            others += 1;
            let pattern = bytes_of(&any[0]);
            let texts: Vec<Vec<u8>> = any[1]
                .as_array()
                .expect("texts")
                .iter()
                .map(bytes_of)
                .collect();
            let mut steps = Steps::default();
            let r = any_match(
                &pattern,
                texts.iter().map(Vec::as_slice),
                &mut steps,
                any[2] == 1,
            );
            (
                json!({"r": r, "taken": WM_STEPS - steps.left}),
                json!({"r": record["r"], "taken": record["taken"]}),
            )
        } else {
            others += 1;
            let head = &record["head"];
            let r = literal_head(
                &bytes_of(&head[0]),
                &bytes_of(&head[1]),
                head[2].as_u64().expect("a length") as usize,
                head[3] == 1,
            );
            (json!(r), record["r"].clone())
        };
        if got != want {
            let shown: String = record.to_string().chars().take(160).collect();
            failures.push(format!("#{i} {shown}: native {got} reference {want}"));
        }
    }
    assert_eq!(header["cases"].as_u64(), Some(cases));
    assert_eq!(header["others"].as_u64(), Some(others));
    ledger.settle((cases + others) as usize, &failures);
}
