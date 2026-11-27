//! Every case of `pysyntax_compound_posix.jsonl` (POSIX only, gated on
//! the Python release) against
//! `pysyntax::module_expr::parse_module_blocks`, row by row as
//! `common/pysyntax_expr.rs` (`check_block`) compares them: the sources
//! the parser reads whose trees are deeper than the port answers under
//! Windows' `Py_C_RECURSION_LIMIT` (each slot's deepest accepted chain,
//! the `elif` chains and the bracket sources below their boundary), which
//! `ast2obj` converts where the limit is 10,000; then every case of
//! `pysyntax_stmt_posix.jsonl` read by the same entry.

#![cfg(unix)]

mod common;
#[path = "common/pysyntax_expr.rs"]
mod expr;

use bettermemory_core::pysyntax::module_expr::C_RECURSION_LIMIT;
use common::jsonl;
use common::parity::Ledger;
use expr::{check_block, check_stmt_as_block, Counts};
use serde_json::Value;

fn every_case(test: &str, name: &str, check: fn(&Value, &mut Counts)) -> Counts {
    let rows = jsonl(name);
    let mut ledger = Ledger::new(test, name);
    let mut n = Counts::default();
    for (i, row) in rows.iter().enumerate() {
        let label = format!("{name}:{}", i + 1);
        if i == 0 {
            ledger.case(label, || {
                assert_eq!(row["python"], "3.13.13");
                assert_eq!(row["c_recursion_limit"], C_RECURSION_LIMIT);
                assert!(row.get("declined").is_none(), "declined: {row}");
                assert_eq!(row["rows"].as_u64().unwrap() as usize, rows.len() - 1);
            });
        } else {
            let short: String = row["t"].to_string().chars().take(120).collect();
            ledger.case(format!("{label} {short}"), || check(row, &mut n));
        }
    }
    if ledger.strict() {
        eprintln!(
            "accepted {}, band {} ({} declined)",
            n.accepted, n.banded, n.band_declined
        );
    }
    ledger.finish(rows.len());
    n
}

#[test]
fn pysyntax_compound_posix_every_case() {
    let n = every_case(
        "pysyntax_compound_posix_every_case",
        "pysyntax_compound_posix.jsonl",
        check_block,
    );
    // Each slot's deepest chain, the `elif` chains 60 branches below
    // their boundary and the brackets 150 calls below theirs are answered.
    if n.accepted + n.refused + n.statements + n.band_declined > 0 {
        assert!(n.accepted >= 70, "accepted {}", n.accepted);
    }
}

#[test]
fn pysyntax_stmt_posix_read_as_blocks() {
    every_case(
        "pysyntax_stmt_posix_read_as_blocks",
        "pysyntax_stmt_posix.jsonl",
        check_stmt_as_block,
    );
}
