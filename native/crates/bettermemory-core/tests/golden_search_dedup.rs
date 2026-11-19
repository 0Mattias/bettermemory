//! Every case of `search_dedup.jsonl` and `search_dedup_unicode.jsonl`
//! against `search_dedup`: token sets, the pairwise score and its bound,
//! snippets, the token cache, `find_similar`, `find_similar_tombstones`
//! and the two digest readers over a scripted store, each answer whole.

mod common;

use bettermemory_core::search_dedup::{
    CONTAINMENT_CEILING, CONTAINMENT_MIN_TOKENS, HIGH_SIMILARITY, MEDIUM_SIMILARITY,
    RAW_TOKEN_SET_ENTRIES,
};
use common::dedup::{answer, bits};
use common::jsonl;
use common::parity::Ledger;
use serde_json::json;

fn every_case(name: &str, test: &str) {
    let lines = jsonl(name);
    let header = &lines[0];
    assert_eq!(header["high"], json!(bits(HIGH_SIMILARITY)));
    assert_eq!(header["medium"], json!(bits(MEDIUM_SIMILARITY)));
    assert_eq!(header["ceiling"], json!(bits(CONTAINMENT_CEILING)));
    assert_eq!(header["min_tokens"], json!(CONTAINMENT_MIN_TOKENS));
    assert_eq!(header["entries"], json!(RAW_TOKEN_SET_ENTRIES));
    let in_file = header["cases"].as_u64().expect("cases") as usize;
    assert_eq!(in_file, lines.len() - 1, "{name}: header count");
    let mut ledger = Ledger::new(test, name);
    for (i, case) in lines[1..].iter().enumerate() {
        ledger.case(format!("case {i} ({})", case["k"]), || {
            assert_eq!(answer(case), *case, "{name} case {i}");
        });
    }
    ledger.finish(in_file);
}

#[test]
fn search_dedup_every_case() {
    every_case("search_dedup.jsonl", "search_dedup_every_case");
}

#[test]
fn search_dedup_unicode_every_case() {
    every_case(
        "search_dedup_unicode.jsonl",
        "search_dedup_unicode_every_case",
    );
}
