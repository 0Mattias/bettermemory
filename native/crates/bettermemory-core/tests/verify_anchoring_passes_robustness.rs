//! Hostile input for the read passes: a body of many matches, long
//! spellings, many attestations, code points at the bounds; no panic, and
//! time that grows in proportion to the entries.

mod common;
mod timing;

use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::verify_anchoring::passes::is_checkable_citation;
use common::attestation_paths::cps;
use common::verify_anchoring_attest::paths;
use common::verify_anchoring_passes::{run, Pass, Stub};

#[test]
fn long_and_odd_spellings_do_not_panic() {
    let long: PyStr = std::iter::repeat_n(u32::from(b'a'), 1 << 20).collect();
    let mut slashed = long.clone();
    slashed.extend(cps("/b.py"));
    assert!(!is_checkable_citation(&long));
    let _ = is_checkable_citation(&slashed);
    let _ = is_checkable_citation(&[0x10_ffff, u32::from(b'/'), 0xd800]);
    let deep = cps(&"../".repeat(100_000));
    let odd = paths(
        "R",
        &[
            long.clone(),
            deep.clone(),
            vec![0xd800],
            vec![0],
            slashed.clone(),
        ],
    );
    let none = paths("P", &[]);
    for nt in [false, true] {
        let a = Stub {
            root: Some(cps(if nt { "C:\\r" } else { "/r" })),
            rule: "home_checkout".into(),
            convicting: true,
        };
        let _ = run(nt, Pass::ReadAttestations(&a, &odd, &odd), &[], &[]);
        let _ = run(
            nt,
            Pass::CheckAttestations(Some(&long), &odd, &none),
            &[],
            &[],
        );
        let spans = [
            (0, slashed.len()),
            (0, 3),
            (0, slashed.len() + 5),
            (slashed.len() + 1, slashed.len() + 9),
            (9, 2),
        ];
        let _ = run(nt, Pass::ReadCitations(&a, &slashed), &spans, &[]);
        let _ = run(nt, Pass::CheckCitations(&cps("/r"), &slashed), &spans, &[]);
    }
}

#[test]
fn many_matches_stop_at_the_budget() {
    let body = cps(&"d/x.py ".repeat(200_000));
    let spans: Vec<(usize, usize)> = (0..200_000).map(|i| (i * 7, i * 7 + 6)).collect();
    let (_, out, _) = run(false, Pass::CheckCitations(&cps("/r"), &body), &spans, &[]);
    assert_eq!(out.checked.len(), 1);
}

#[test]
fn the_attestation_passes_grow_with_their_entries() {
    let entry = |i: usize| cps(&format!("src/gone{i}.py"));
    let small: Vec<PyStr> = (0..300).map(entry).collect();
    let large: Vec<PyStr> = (0..3000).map(entry).collect();
    let (ps, pl, none) = (paths("P", &small), paths("P", &large), paths("P", &[]));
    let a = Stub {
        root: Some(cps("/r")),
        rule: "home_checkout".into(),
        convicting: true,
    };
    timing::grows_within(
        "read_attestations",
        timing::TENFOLD,
        &mut || timing::once(|| run(false, Pass::ReadAttestations(&a, &ps, &ps), &[], &[])),
        &mut || timing::once(|| run(false, Pass::ReadAttestations(&a, &pl, &pl), &[], &[])),
    );
    let root = cps("/r");
    timing::grows_within(
        "check_anchored_attestations",
        timing::TENFOLD,
        &mut || {
            timing::once(|| {
                run(
                    false,
                    Pass::CheckAttestations(Some(&root), &ps, &none),
                    &[],
                    &[],
                )
            })
        },
        &mut || {
            timing::once(|| {
                run(
                    false,
                    Pass::CheckAttestations(Some(&root), &pl, &none),
                    &[],
                    &[],
                )
            })
        },
    );
}
