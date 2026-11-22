//! Every case of `owner_prompt.jsonl` against `owner_prompt`: the row and
//! content readers over `json.loads` values, and `owner_text` and
//! `owner_prompt` with `probe_text` as the identity, as the generator ran
//! them.

mod common;

use bettermemory_core::json::loads;
use bettermemory_core::owner_prompt::{
    is_submitted, owner_prompt, owner_text, queued_text, submitted_text, text_blocks,
};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode;
use common::parity::Ledger;
use common::{jsonl, text_field};
use serde_json::Value;

fn identity(text: &[u32], _cut: bool) -> PyStr {
    text.to_vec()
}

fn want_text(case: &Value) -> Option<PyStr> {
    if case.get("r").is_some_and(Value::is_null) {
        None
    } else {
        Some(text_field(case, "r"))
    }
}

#[test]
fn owner_prompt_every_case() {
    let mut lines = jsonl("owner_prompt.jsonl");
    let header = lines.remove(0);
    assert_eq!(header["cases"].as_u64(), Some(lines.len() as u64));
    let ledger = Ledger::new("golden_owner_prompt", "owner_prompt.jsonl");
    let mut failures = Vec::new();
    for (i, case) in lines.iter().enumerate() {
        let f = case["f"].as_str().unwrap();
        let ok = if f == "ot" {
            let text = text_field(case, "in");
            let got = owner_text(&text, true, &identity);
            let uncut = owner_text(&text, false, &identity);
            got == want_text(case) && uncut == got
        } else {
            let value = loads(&unicode::cps(case["in"].as_str().unwrap())).expect("json");
            match f {
                "sub" => Some(is_submitted(&value)) == case["r"].as_bool(),
                "tb" => text_blocks(&value) == want_text(case),
                "st" => submitted_text(&value) == want_text(case),
                "qt" => queued_text(&value) == want_text(case),
                "op" => owner_prompt(&value, true, &identity) == want_text(case),
                other => panic!("unknown f {other}"),
            }
        };
        if !ok {
            failures.push(format!("#{i}: {case}"));
        }
    }
    assert!(!lines.is_empty());
    ledger.settle(lines.len(), &failures);
}
