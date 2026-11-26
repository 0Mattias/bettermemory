//! Every case of `pysyntax_expr_posix.jsonl` (POSIX only, gated on the
//! Python release) against `pysyntax::module_expr::parse_expression_module`,
//! row by row as `common/pysyntax_expr.rs` compares them: the sources the
//! parser reads whose trees are deeper than `ast2obj` converts on Windows
//! (the chains' deepest accepted sources, the bracket stacks' sources
//! below their boundary) and the sums and attribute chains about as deep
//! as it converts where `Py_C_RECURSION_LIMIT` is 10,000.

#![cfg(unix)]

mod common;
#[path = "common/pysyntax_expr.rs"]
mod expr;

use bettermemory_core::pysyntax::module_expr::C_RECURSION_LIMIT;
use common::jsonl;
use common::parity::Ledger;
use expr::{check, Counts};

#[test]
fn pysyntax_expr_posix_every_case() {
    let name = "pysyntax_expr_posix.jsonl";
    let rows = jsonl(name);
    let mut ledger = Ledger::new("pysyntax_expr_posix_every_case", name);
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
        let Counts {
            accepted,
            refused,
            banded,
            band_declined,
            ..
        } = n;
        eprintln!(
            "accepted {accepted}, refused {refused}, band {banded} ({band_declined} declined)"
        );
        // Each chain's deepest source, the stacks' sources 150 calls below
        // their boundary and the sums are answered; the stacks' boundary
        // sources and the sums near `ast2obj`'s depth may be declined.
        assert!(
            accepted >= 50 && refused >= 4,
            "accepted {accepted}, refused {refused}"
        );
        assert!(banded >= 32, "band {banded}");
    }
    ledger.finish(rows.len());
}
