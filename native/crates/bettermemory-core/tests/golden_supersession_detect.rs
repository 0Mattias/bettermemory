//! Every case of `supersession_detect.jsonl` and
//! `supersession_detect_unicode.jsonl` against `supersession`'s detector:
//! each report whole, the scripted store's call log, the token cache's
//! keys after, `_one_claim_replaced`, and the seeded sweep of stores by
//! count and digest.

mod common;

use bettermemory_core::supersession::{
    MAX_LINKS_PER_WRITE, MIN_CONFLICT_JACCARD, MIN_CONFLICT_KIN_JACCARD, MIN_CONTEXT_SHARED,
    MIN_JACCARD, SCORE_BATCH,
};
use common::jsonl;
use common::parity::Ledger;
use common::supdetect::{answer, bits, sweep_line, sweep_stores};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn sweep(case: &Value) -> Value {
    let stores = sweep_stores(case);
    let mut digest = Sha256::new();
    for bodies in &stores {
        digest.update(sweep_line(bodies).as_bytes());
        digest.update(b"\n");
    }
    let mut out = case.clone();
    out["count"] = json!(stores.len());
    out["sha256"] = json!(common::hex(&digest.finalize()));
    out
}

fn every_case(name: &str, test: &str) {
    let lines = jsonl(name);
    let header = &lines[0];
    assert_eq!(header["batch"], json!(SCORE_BATCH));
    assert_eq!(header["cap"], json!(MAX_LINKS_PER_WRITE));
    assert_eq!(
        header["floors"],
        json!([
            bits(MIN_JACCARD),
            bits(MIN_CONFLICT_JACCARD),
            bits(MIN_CONFLICT_KIN_JACCARD)
        ])
    );
    assert_eq!(header["context"], json!(MIN_CONTEXT_SHARED));
    let in_file = header["cases"].as_u64().expect("cases") as usize;
    assert_eq!(in_file, lines.len() - 1, "{name}: header count");
    let mut ledger = Ledger::new(test, name);
    for (i, case) in lines[1..].iter().enumerate() {
        ledger.case(format!("case {i} ({})", case["k"]), || {
            let got = if case["k"] == json!("sweep") {
                sweep(case)
            } else {
                answer(case)
            };
            assert_eq!(got, *case, "{name} case {i}");
        });
    }
    ledger.finish(in_file);
}

#[test]
fn supersession_detect_every_case() {
    every_case(
        "supersession_detect.jsonl",
        "supersession_detect_every_case",
    );
}

#[test]
fn supersession_detect_unicode_every_case() {
    every_case(
        "supersession_detect_unicode.jsonl",
        "supersession_detect_unicode_every_case",
    );
}
