//! `pysyntax_expr_posix.jsonl` has no Windows case: its sources are read
//! into trees deeper than `ast2obj` converts under Windows'
//! `Py_C_RECURSION_LIMIT` (3,000 on x64, 1,000 on arm64), where the
//! reference raises RecursionError and the port refuses them as
//! `recursion` (`pysyntax_expr_kills::the_recursion_limit_follows_the_host`
//! holds that rule at all three limits). `golden_pysyntax_expr_posix.rs`
//! runs on POSIX only; here the gap is recorded.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_expression_depth_fixture_has_no_case_here() {
    common::parity::record(
        "golden_pysyntax_expr_posix",
        "pysyntax_expr_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &[
            "the trees are deeper than `ast2obj` converts on Windows; Windows has no case"
                .to_owned(),
        ],
    );
}
