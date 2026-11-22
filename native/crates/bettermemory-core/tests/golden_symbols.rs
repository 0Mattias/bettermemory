//! Every case of `symbols.jsonl` (class i): `check_symbol_citations`
//! over the case's tree in memory (`MemFs`, POSIX flavour), with
//! `ast.parse` replayed from the reference's names by the text's digest.
//! The same trees under the Windows flavour decline wherever a root is
//! resolved (`realpath` is not ported there).

mod common;

use bettermemory_core::fsdata::Flavour;
use bettermemory_core::symbols::Declined;
use common::jsonl;
use common::parity::Ledger;
use common::symbols::{mem_tree, report_json, run, Replay};

const FILE: &str = "symbols.jsonl";
const HERE: &str = "/S";

#[test]
fn symbols_every_case() {
    let lines = jsonl(FILE);
    let cases = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    assert!(!cases.is_empty());
    let ledger = Ledger::new("golden_symbols", FILE);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let fs = mem_tree(case, HERE);
        let names = Replay::of(case);
        let got = run(case, &fs, Flavour::Posix, HERE, &names).map(|r| report_json(&r));
        if got.as_ref() != Ok(&case["r"]) || *names.misses.borrow() != 0 {
            failures.push(format!(
                "#{i}: native {got:?} misses {} reference {}",
                names.misses.borrow(),
                case["r"]
            ));
        }
    }
    ledger.settle(cases.len(), &failures);
}

#[test]
fn symbols_windows_flavour_declines_or_answers_empty() {
    let lines = jsonl(FILE);
    let mut declined = 0;
    for case in &lines[1..] {
        let fs = mem_tree(case, HERE);
        let names = Replay::of(case);
        match run(case, &fs, Flavour::Windows, HERE, &names) {
            Err(Declined::NotPorted) => declined += 1,
            Ok(report) => {
                assert!(!report.truthy(), "an answer under Windows: {case}");
            }
        }
    }
    assert!(declined > 0);
}
