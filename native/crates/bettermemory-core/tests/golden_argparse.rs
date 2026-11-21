//! Every case of `argparse.jsonl` and `argparse_python.jsonl` against
//! `argparse::ArgumentParser::parse_args`: each parser of the header built
//! through the port, each argv parsed at the case's width, the namespace or
//! the exit status, stdout and stderr compared; and the seeded sweep of
//! `argparse_python.jsonl` regenerated and compared by count and digest.

mod common;

use bettermemory_core::argparse::{ArgumentParser, HelpText};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use common::argparse::{build, outcome, text};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// The help text the case printed, as argparse-2's formatter will give it.
struct Printed(PyStr);
impl HelpText for Printed {
    fn format_help(&self, _: &ArgumentParser, _: i64) -> Option<PyStr> {
        Some(self.0.clone())
    }
}

fn want(row: &Value) -> Value {
    let mut w = row.clone();
    let m = w.as_object_mut().unwrap();
    for k in ["a", "p", "w"] {
        m.remove(k);
    }
    w
}

fn check(parsers: &[ArgumentParser], row: &Value) {
    let p = &parsers[row["p"].as_u64().unwrap() as usize];
    let argv: Vec<PyStr> = row["a"].as_array().unwrap().iter().map(text).collect();
    let hook = Printed(row.get("out").map(text).unwrap_or_default());
    let got = p
        .parse_args(&argv, row["w"].as_i64().unwrap(), &hook)
        .unwrap();
    assert_eq!(outcome(&got), want(row), "{row}");
}

const MUL: u64 = 6364136223846793005;
const INC: u64 = 1442695040888963407;

fn sweep(parsers: &[ArgumentParser], row: &Value) {
    let s = &row["sweep"];
    let tokens: Vec<PyStr> = s["tokens"].as_array().unwrap().iter().map(text).collect();
    let p = &parsers[s["parser"].as_u64().unwrap() as usize];
    let width = s["w"].as_i64().unwrap();
    let mut state = s["seed"].as_u64().unwrap();
    let count = row["count"].as_u64().unwrap() as usize;
    let mut lines = Vec::with_capacity(count);
    let hook = Printed(cps("unused"));
    for _ in 0..count {
        state = state.wrapping_mul(MUL).wrapping_add(INC);
        let n = (state >> 33) % 5;
        let mut argv = Vec::new();
        for _ in 0..n {
            state = state.wrapping_mul(MUL).wrapping_add(INC);
            argv.push(tokens[((state >> 33) % tokens.len() as u64) as usize].clone());
        }
        let got = p.parse_args(&argv, width, &hook).unwrap();
        lines.push(serde_json::to_string(&outcome(&got)).unwrap());
    }
    let digest = hex(&Sha256::digest(lines.join("\n").as_bytes()));
    assert_eq!(json!(digest), row["sha256"]);
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn file(test: &str, name: &str) {
    let rows = jsonl(name);
    let header = &rows[0];
    let mut ledger = Ledger::new(test, name);
    let parsers: Vec<ArgumentParser> = header["parsers"]
        .as_array()
        .unwrap()
        .iter()
        .map(build)
        .collect();
    let mut cases = 0;
    for (i, row) in rows.iter().enumerate() {
        let label = format!("{name}:{}", i + 1);
        if i == 0 {
            ledger.case(label, || {
                assert_eq!(row["python"], "3.13.13");
                assert!(row.get("declined").is_none(), "declined: {row}");
            });
        } else if row.get("sweep").is_some() {
            ledger.case(label, || sweep(&parsers, row));
        } else {
            cases += 1;
            ledger.case(format!("{label} {}", row["a"]), || check(&parsers, row));
        }
    }
    if header.get("declined").is_none() {
        assert_eq!(header["cases"].as_u64(), Some(cases));
    }
    ledger.finish(rows.len());
}

#[test]
fn argparse_every_case() {
    file("golden_argparse", "argparse.jsonl");
}

#[test]
fn argparse_python_every_case() {
    file("golden_argparse_python", "argparse_python.jsonl");
}
