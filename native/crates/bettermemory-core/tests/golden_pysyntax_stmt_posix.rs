//! Every case of `pysyntax_stmt_posix.jsonl` (POSIX only, gated on the
//! Python release) against `pysyntax::module_expr::parse_module`, row by
//! row as `common/pysyntax_expr.rs` (`check_stmt`) compares them: the
//! sources the parser reads whose trees are deeper than the port answers
//! under Windows' `Py_C_RECURSION_LIMIT` (each statement slot's deepest
//! accepted chain, the bracket stacks' sources below their boundary),
//! which `ast2obj` converts where the limit is 10,000.

#![cfg(unix)]

mod common;
#[path = "common/pysyntax_expr.rs"]
mod expr;

use bettermemory_core::pysyntax::module_expr::C_RECURSION_LIMIT;
use common::jsonl;
use common::parity::Ledger;
use expr::{check_stmt, Counts};

#[test]
fn pysyntax_stmt_posix_every_case() {
    let name = "pysyntax_stmt_posix.jsonl";
    let rows = jsonl(name);
    let mut ledger = Ledger::new("pysyntax_stmt_posix_every_case", name);
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
            ledger.case(format!("{label} {short}"), || check_stmt(row, &mut n));
        }
    }
    if ledger.strict() {
        let Counts {
            accepted,
            banded,
            band_declined,
            ..
        } = n;
        eprintln!("accepted {accepted}, band {banded} ({band_declined} declined)");
        // Each slot's deepest chain and the stacks' sources 150 calls below
        // their boundary are answered; the stacks' boundary sources may be
        // declined.
        assert!(accepted >= 60, "accepted {accepted}");
        assert!(banded >= 20, "band {banded}");
    }
    ledger.finish(rows.len());
}
