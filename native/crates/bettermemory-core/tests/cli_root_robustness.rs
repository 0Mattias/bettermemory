//! Hostile input to `cli::root::main`: very long argvs, long and odd
//! words, a deep chain of double dashes; no panic, and time that grows
//! with the input.

mod common;
mod timing;

use bettermemory_core::argparse::Unfilled;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use common::cli_root::run_main;

fn argv(n: usize) -> Vec<PyStr> {
    let mut v = vec![cps("health")];
    v.extend((0..n).map(|i| cps(if i % 2 == 0 { "--opt" } else { "w" })));
    v
}

#[test]
fn a_long_argv_to_an_arm_runs_it() {
    let got = run_main(&argv(100_000), "1", 78, &Unfilled, None);
    assert_eq!(got["arm"], "health");
}

#[test]
fn hostile_words_end_in_the_parser_without_panic() {
    let long = cps(&"x".repeat(1 << 20));
    let cases: Vec<Vec<PyStr>> = vec![
        vec![long.clone()],
        vec![cps("--"); 10_000],
        vec![vec![0xD800, 0x00, 0x10FFFF]],
        vec![cps("health"), long],
        vec![cps("-"), cps("--=")],
    ];
    for a in cases {
        let got = run_main(&a, "1", 78, &Unfilled, None);
        assert!(
            got["code"] == 2 || got["fault"] == "NotPorted",
            "{got:.200}"
        );
    }
    let got = run_main(&[cps("--version")], &"9".repeat(10_000), 1, &Unfilled, None);
    assert!(
        got["code"] == 0 || got["fault"] == "NotPorted",
        "{got:.200}"
    );
    let got = run_main(&[], "1", i64::MIN, &Unfilled, None);
    assert_eq!(got["shim"], true);
}

#[test]
fn a_long_argv_is_linear() {
    let (small, large) = (argv(20_000), argv(200_000));
    timing::grows_within(
        "cli_root_long_argv",
        timing::TENFOLD,
        &mut || timing::once(|| run_main(&small, "1", 78, &Unfilled, None)),
        &mut || timing::once(|| run_main(&large, "1", 78, &Unfilled, None)),
    );
}
