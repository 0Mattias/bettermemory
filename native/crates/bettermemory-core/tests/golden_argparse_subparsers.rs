//! Every case of `argparse_subparsers.jsonl` and
//! `argparse_subparsers_python.jsonl` against `parse_args` over a tree of
//! subparsers: each tree of the header built through the port at the
//! case's width, the namespace or the exit status, stdout and stderr
//! compared; and the `add_subparsers` and `add_parser` calls the reference
//! refuses.

mod common;

use bettermemory_core::argparse::{ArgumentParser, HelpText, SpecError};
use bettermemory_core::pyvalue::PyStr;
use common::argparse::{build_tree, outcome, text};
use common::jsonl;
use common::parity::Ledger;
use serde_json::Value;

/// The help text the case printed, as argparse-2's formatter will give it.
struct Printed(PyStr);
impl HelpText for Printed {
    fn format_help(&self, _: &ArgumentParser, _: i64) -> Option<PyStr> {
        Some(self.0.clone())
    }
}

fn check(trees: &[Value], row: &Value) {
    let width = row["w"].as_i64().unwrap();
    let p = build_tree(&trees[row["p"].as_u64().unwrap() as usize], width);
    let argv: Vec<PyStr> = row["a"].as_array().unwrap().iter().map(text).collect();
    let hook = Printed(row.get("out").map(text).unwrap_or_default());
    let got = p.parse_args(&argv, width, &hook).unwrap();
    let mut want = row.clone();
    for k in ["a", "p", "w"] {
        want.as_object_mut().unwrap().remove(k);
    }
    assert_eq!(outcome(&got), want, "{row}");
}

fn spec(trees: &[Value], row: &Value) {
    assert_eq!(row["raises"], "ArgumentError");
    let mut p = build_tree(&trees[0], 78);
    let got = match row["spec"].as_str().unwrap() {
        "multiple" => p.add_subparsers(Some("again"), 78).unwrap_err(),
        _ => p.add_parser("hook", None, None).err().unwrap(),
    };
    let want = match row["spec"].as_str().unwrap() {
        "multiple" => SpecError::MultipleSubparsers,
        _ => SpecError::ConflictingSubparser,
    };
    assert_eq!(got, want);
}

fn file(test: &str, name: &str) {
    let rows = jsonl(name);
    let header = &rows[0];
    let mut ledger = Ledger::new(test, name);
    let trees = header["trees"].as_array().unwrap().clone();
    let mut cases = 0;
    for (i, row) in rows.iter().enumerate() {
        let label = format!("{name}:{}", i + 1);
        if i == 0 {
            ledger.case(label, || {
                assert_eq!(row["python"], "3.13.13");
                assert!(row.get("declined").is_none(), "declined: {row}");
            });
        } else if row.get("spec").is_some() {
            ledger.case(label, || spec(&trees, row));
        } else {
            cases += 1;
            ledger.case(format!("{label} {}", row["a"]), || check(&trees, row));
        }
    }
    if header.get("declined").is_none() {
        assert!(cases > 0);
        assert_eq!(header["cases"].as_u64(), Some(cases));
    }
    ledger.finish(rows.len());
}

#[test]
fn argparse_subparsers_every_case() {
    file("golden_argparse_subparsers", "argparse_subparsers.jsonl");
}

#[test]
fn argparse_subparsers_python_every_case() {
    file(
        "golden_argparse_subparsers_python",
        "argparse_subparsers_python.jsonl",
    );
}
