//! Every case of `cli_root.jsonl` and `cli_root_python.jsonl` against
//! `cli::root::main` with the generator's stand-in commands: the hook
//! route, the shim, the arm with its parsers and namespace, or the
//! parser's exit.

mod common;

use bettermemory_core::pyvalue::PyStr;
use common::argparse::text;
use common::cli_root::{run_main, Printed};
use common::jsonl;
use common::parity::Ledger;
use serde_json::Value;

fn check(header: &Value, row: &Value) {
    let argv: Vec<PyStr> = row["a"].as_array().unwrap().iter().map(text).collect();
    let hook = Printed(row.get("out").map(text).unwrap_or_default());
    let got = run_main(
        &argv,
        header["version"].as_str().unwrap(),
        header["w"].as_i64().unwrap(),
        &hook,
        None,
    );
    let mut want = row.clone();
    want.as_object_mut().unwrap().remove("a");
    assert_eq!(got, want, "{row}");
}

fn file(test: &str, name: &str) {
    let rows = jsonl(name);
    let header = &rows[0];
    let mut ledger = Ledger::new(test, name);
    let mut cases = 0;
    for (i, row) in rows.iter().enumerate() {
        let label = format!("{name}:{}", i + 1);
        if i == 0 {
            ledger.case(label, || {
                assert_eq!(row["python"], "3.13.13");
                assert!(row.get("declined").is_none(), "declined: {row}");
            });
        } else {
            cases += 1;
            ledger.case(format!("{label} {}", row["a"]), || check(header, row));
        }
    }
    if header.get("declined").is_none() {
        assert!(cases > 0);
        assert_eq!(header["cases"].as_u64(), Some(cases));
    }
    ledger.finish(rows.len());
}

#[test]
fn cli_root_every_case() {
    file("cli_root_every_case", "cli_root.jsonl");
}

#[test]
fn cli_root_python_every_case() {
    file("cli_root_python_every_case", "cli_root_python.jsonl");
}
