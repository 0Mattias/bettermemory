//! Every case of `githead.jsonl` (regular files, directories and missing
//! files, under both flavours, since the reference answers them alike on
//! every platform) and, on POSIX, of `githead_posix.jsonl` (links, FIFOs,
//! ENOTDIR, names not every file system stores): `head_bytes`,
//! `_resolve`, `head_sha`, `head_ref`, `head_unborn` and `packed_names`
//! against the native reader over the case's files in memory.

mod common;

use bettermemory_core::githead::Flavour;
use common::githead::answers;
use common::jsonl;
use common::parity::Ledger;

fn every_case(file: &str, test: &str, flavours: &[Flavour]) {
    let lines = jsonl(file);
    let header = &lines[0];
    let cases = &lines[1..];
    assert_eq!(header["cases"].as_u64(), Some(cases.len() as u64));
    let ledger = Ledger::new(test, file);
    let mut failures = Vec::new();
    let mut declined = 0;
    for (i, case) in cases.iter().enumerate() {
        for &flavour in flavours {
            let got = answers(case, flavour);
            if flavour == flavours[0] && (got["v"] == "x" || got["v"] == "o") {
                declined += 1;
            }
            if got != case["r"] {
                let shown: String = case.to_string().chars().take(200).collect();
                failures.push(format!(
                    "#{i} {flavour:?} {shown}: native {got} reference {}",
                    case["r"]
                ));
            }
        }
    }
    assert_eq!(header["declined"].as_u64(), Some(declined));
    ledger.settle(cases.len(), &failures);
}

#[test]
fn githead_every_case() {
    every_case(
        "githead.jsonl",
        "golden_githead",
        &[Flavour::Posix, Flavour::Windows],
    );
}

#[cfg(unix)]
#[test]
fn githead_posix_every_case() {
    every_case(
        "githead_posix.jsonl",
        "golden_githead_posix",
        &[Flavour::Posix],
    );
}
