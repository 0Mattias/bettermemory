//! Every case of `recall_settle_interrupted.jsonl` (not gated) against
//! `recall_settle::interrupted`: the header's constant, and each
//! `_probe_message` and `settle_interrupted` answer with every call its
//! fakes saw, in order. The spelling is the generator's
//! (`tests/contract/gen_recall_settle.py`).

mod common;
mod settle_int;

use bettermemory_core::hook::ATTRIBUTION_LOOKBACK_SECONDS;
use serde_json::json;
use settle_int::{plain, run};

#[test]
fn recall_settle_interrupted_every_case() {
    let name = "recall_settle_interrupted.jsonl";
    let lines = common::jsonl(name);
    let header = &lines[0];
    assert_eq!(header["k"], "consts");
    assert_eq!(
        header["attribution_lookback_seconds"],
        json!(ATTRIBUTION_LOOKBACK_SECONDS)
    );
    let mut checked = 0;
    for r in &lines[1..] {
        let (answer, log) = run(r["k"].as_str().unwrap(), &r["c"]);
        let want = plain(&r["log"]);
        assert!(
            bettermemory_core::pyvalue::PyValue::List(log.clone()) == want,
            "log of {r}\n got {log:?}\n want {want:?}"
        );
        assert!(answer == plain(&r["o"]), "answer of {r}\n got {answer:?}");
        checked += 1;
    }
    let in_file = header["count"].as_u64().unwrap() as usize;
    assert_eq!(in_file, lines.len() - 1);
    common::assert_count(name, checked, in_file);
}
