//! Hostile input for `attestation_paths`: very long spellings, many
//! separators and many repeated inputs. No panic, and time that grows
//! within the helper's bound for ten times the input.

mod common;
mod timing;

use bettermemory_core::attestation_paths::{compile_targets, interpret_path, target_path, Mode};
use bettermemory_core::pyvalue::PyStr;
use common::attestation_paths::{cps, Scripted};

fn long(n: usize, unit: &str) -> PyStr {
    cps(&unit.repeat(n))
}

#[test]
fn long_spellings_answer_without_panic() {
    for nt in [false, true] {
        let host = Scripted { nt };
        for unit in ["a/", "../", "\\", "x", "~/", "C:\\", "\u{2003}", "\0"] {
            let s = long(20_000, unit);
            for mode in [Mode::Auto, Mode::Literal, Mode::Reference] {
                let _ = interpret_path(&host, &s, mode, Some(&cps("/r")));
                let _ = interpret_path(&host, &s, mode, Some(&cps("C:\\r")));
            }
            let _ = target_path(&host, &s, Some(&cps("\\\\s\\h")));
        }
    }
}

#[test]
fn target_path_grows_linearly() {
    let host = Scripted { nt: false };
    let small = long(2_000, "a/");
    let large = long(20_000, "a/");
    let root = cps("/r");
    timing::grows_within(
        "target_path",
        timing::TENFOLD,
        &mut || timing::once(|| target_path(&host, &small, Some(&root))),
        &mut || timing::once(|| target_path(&host, &large, Some(&root))),
    );
}

fn inputs(n: usize) -> Vec<PyStr> {
    (0..n).map(|i| cps(&format!("d{i}/x"))).collect()
}

#[test]
fn compile_targets_grows_linearly_in_distinct_inputs() {
    // The targets keep an index of their keys, as a dict does.
    let host = Scripted { nt: false };
    let root = cps("/r");
    let (small, large) = (inputs(1_000), inputs(10_000));
    let (legacy, targets) = compile_targets(&host, &large, Some(&root), Mode::Auto, false).unwrap();
    assert_eq!((legacy.len(), targets.len()), (10_000, 10_000));
    timing::grows_within(
        "compile_targets",
        timing::TENFOLD,
        &mut || timing::once(|| compile_targets(&host, &small, Some(&root), Mode::Auto, false)),
        &mut || timing::once(|| compile_targets(&host, &large, Some(&root), Mode::Auto, false)),
    );
}
