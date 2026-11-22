//! Every case of `recall_prompt.jsonl` (not gated) against the native
//! `recall_prompt`: the header's constants and each step's answer with
//! every call its fakes saw, in order. The spelling is the generator's
//! (`tests/contract/gen_recall_prompt.py`).

mod common;

use bettermemory_core::recall_prompt as rp;
use bettermemory_core::recall_settle as rs;
use common::recall_prompt::{pv, run};
use serde_json::json;

#[test]
fn recall_prompt_every_case() {
    let lines = common::jsonl("recall_prompt.jsonl");
    let header = &lines[0];
    assert_eq!(header["k"], "consts");
    assert_eq!(header["pool_prefilter"], json!(rp::POOL_PREFILTER));
    assert_eq!(header["prompt_recall"], json!(rp::PROMPT_RECALL));
    assert_eq!(header["rule"], json!(rs::RULE));
    let mut checked = 0;
    for r in &lines[1..] {
        let (answer, log) = run(r["k"].as_str().unwrap(), &r["c"]);
        assert!(
            bettermemory_core::pyvalue::PyValue::List(log.clone()) == pv(&r["log"]),
            "log of {r}\n got {log:?}"
        );
        assert!(answer == pv(&r["o"]), "answer of {r}\n got {answer:?}");
        checked += 1;
    }
    let in_file = header["count"].as_u64().unwrap() as usize;
    assert_eq!(in_file, lines.len() - 1);
    common::assert_count("recall_prompt.jsonl", checked, in_file);
}
