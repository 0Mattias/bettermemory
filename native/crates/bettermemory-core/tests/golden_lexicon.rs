//! Every case of `lexicon.jsonl` and the header's fingerprint; the
//! the hand-written tables and the hashed copy of the spelling tables against `lexicon_spelling_tables.json`.

mod common;

use std::collections::BTreeSet;

use bettermemory_core::lexicon::{
    self, ACRONYM_PLURALS, FINGERPRINT_PROBES, STEM_EXCEPTIONS, STOPWORD_LISTS,
};
use bettermemory_core::unicode;
use common::parity::Ledger;
use common::{committed_path, golden_path, hex, jsonl, text_field};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

#[test]
fn lexicon_every_case() {
    let lines = jsonl("lexicon.jsonl");
    let header = &lines[0];
    assert_eq!(header["fixture"], "lexicon");
    let mut checked = 0;
    let mut failures = Vec::new();
    for record in &lines[1..] {
        let input = text_field(record, "input");
        let tokens = lexicon::tokenize(&input);
        let mut kebab = Map::new();
        for t in &tokens {
            kebab
                .entry(t.clone())
                .or_insert_with(|| json!(lexicon::kebab_parts(t)));
        }
        let folded = lexicon::fold_lowered(&input);
        let mut got = json!({
            "tokens": tokens,
            "unstemmed": lexicon::tokenize_unstemmed(&input),
            "content_tokens": lexicon::strip_stopwords(&tokens),
            "index_text": lexicon::fts_index_text(&input),
            "match_query": lexicon::fts_match_query(&input),
            "kebab_parts": kebab,
        });
        match unicode::to_string(&folded) {
            Some(s) => got["folded"] = json!(s),
            None => got["folded_cps"] = json!(folded),
        }
        match unicode::to_string(&input) {
            Some(s) => got["input"] = json!(s),
            None => got["input_cps"] = json!(input),
        }
        if &got != record {
            let fields: Vec<&String> = record
                .as_object()
                .unwrap()
                .keys()
                .filter(|k| got.get(*k) != record.get(*k))
                .collect();
            failures.push(format!(
                "{input:X?}: {fields:?}\n  expected {record}\n  got      {got}"
            ));
        }
        checked += 1;
    }
    assert_eq!(checked, lines.len() - 1, "lexicon.jsonl cases");
    // Unicode 15.1's (class ii): set aside, named, under another Unicode.
    Ledger::new("lexicon_every_case", "lexicon.jsonl").settle(checked, &failures);
}

#[test]
fn lexicon_fingerprint() {
    let mut ledger = Ledger::new("lexicon_fingerprint", "lexicon.jsonl");
    ledger.case("the header's fingerprint", fingerprint_of_the_header);
    ledger.finish(1);
}

fn fingerprint_of_the_header() {
    let lines = jsonl("lexicon.jsonl");
    let header = &lines[0];
    let probes: Vec<&str> = header["probes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| p.as_str().unwrap())
        .collect();
    assert_eq!(probes, FINGERPRINT_PROBES);
    assert_eq!(
        lexicon::probe_index_text(),
        header["probe_index_text"].as_str().unwrap()
    );
    let tables = lexicon::REFERENCE_SPELLING_TABLES;
    assert_eq!(
        hex(&Sha256::digest(tables.as_bytes())),
        header["spelling_tables_sha256"]
    );
    assert_eq!(
        lexicon::python_tokenizer_fingerprint(),
        header["tokenizer_fingerprint"]
    );
    let native = lexicon::native_tokenizer_fingerprint();
    assert_ne!(native, lexicon::python_tokenizer_fingerprint());
    eprintln!(
        "python fingerprint {}, native fingerprint {native}",
        lexicon::python_tokenizer_fingerprint()
    );
}

#[test]
fn spelling_tables_match_the_golden_file_and_the_rust_tables() {
    // The crate compiles in the committed file's text: pinned to it always.
    let committed = std::fs::read(committed_path("lexicon_spelling_tables.json")).unwrap();
    assert_eq!(
        lexicon::REFERENCE_SPELLING_TABLES.as_bytes(),
        committed.as_slice(),
        "the compiled-in copy is the committed file"
    );
    // The directory under test (BM_GOLDEN_DIR) must hold the same text: the
    // tables are Python source lists, the same on every platform. A
    // difference fails, unless its Unicode is not the port's, where it is
    // set aside and named (the file is in the Unicode-gated generator).
    let golden = std::fs::read(golden_path("lexicon_spelling_tables.json")).unwrap();
    let mut ledger = Ledger::new(
        "spelling_tables_match_the_golden_file_and_the_rust_tables",
        "lexicon_spelling_tables.json",
    );
    ledger.case("the file under test is the compiled-in copy", || {
        assert!(
            lexicon::REFERENCE_SPELLING_TABLES.as_bytes() == golden.as_slice(),
            "lexicon_spelling_tables.json under test differs from the compiled-in copy"
        );
    });
    ledger.finish(1);
    let doc: Value = serde_json::from_slice(&golden).unwrap();
    let tables = &doc["tables"];
    let words = |v: &Value| -> BTreeSet<String> {
        v.as_array()
            .unwrap()
            .iter()
            .map(|w| w.as_str().unwrap().to_owned())
            .collect()
    };
    let mut union = BTreeSet::new();
    for (name, list) in STOPWORD_LISTS {
        let ours: BTreeSet<String> = list.iter().map(|w| (*w).to_owned()).collect();
        assert_eq!(ours.len(), list.len(), "{name} has a repeat");
        assert_eq!(ours, words(&tables[name]), "{name}");
        union.extend(ours);
    }
    assert_eq!(union, words(&tables["_STOPWORDS"]));
    for word in &union {
        assert!(lexicon::is_stopword(word));
    }
    let exceptions: BTreeSet<String> = STEM_EXCEPTIONS.iter().map(|w| (*w).to_owned()).collect();
    assert_eq!(exceptions, words(&tables["_STEM_EXCEPTIONS"]));
    assert_eq!(json!(ACRONYM_PLURALS), tables["_ACRONYM_PLURALS"]);
    assert_eq!(json!(FINGERPRINT_PROBES), tables["_FINGERPRINT_PROBES"]);
    // The Latin fold table: each code point below U+0180 folded alone.
    let fold: Vec<String> = (0..0x180u32)
        .map(|cp| unicode::to_string(&unicode::nfd_fold(&[cp])).unwrap())
        .collect();
    assert_eq!(json!(fold), tables["_LATIN_FOLD_TABLE"]);
    // The unspaced-script class, as the pattern spells its ranges.
    let class: String = lexicon::UNSEGMENTED
        .iter()
        .map(|&(a, b)| {
            format!(
                "{}-{}",
                char::from_u32(a).unwrap(),
                char::from_u32(b).unwrap()
            )
        })
        .collect();
    assert_eq!(
        tables["_UNSEGMENTED_RE"],
        json!(["re", format!("[{class}]+"), 32])
    );
    // The patterns hand-ported in lexicon.rs, as the reference spells them.
    assert_eq!(
        tables["_TOKEN_RE"],
        json!(["re", r"\d+(?:\.\d+)+|\w(?:[\w\-]*\w)?", 32])
    );
    assert_eq!(
        tables["_CONTRACTION_RE"],
        json!(["re", "(?<=\\w)['’](?:s|t|d|m|ll|re|ve)\\b", 32])
    );
    assert_eq!(tables["_KEBAB_SPLIT_RE"], json!(["re", "[-_]+", 32]));
    assert_eq!(
        tables["_SYMBOL_ALIASES"],
        json!([
            [["re", r"(?<!\w)c\+\+", 32], "cpp ", "c++"],
            [["re", r"(?<!\w)c#(?!\w)", 32], "csharp", "c#"],
            [["re", r"(?<!\w)f#(?!\w)", 32], "fsharp", "f#"],
            [["re", r"(?<!\w)\.net(?!\w)", 32], "dotnet", ".net"]
        ])
    );
    let names: BTreeSet<&String> = tables.as_object().unwrap().keys().collect();
    assert_eq!(
        names.len(),
        17,
        "a table was added to the reference: port it"
    );
}
