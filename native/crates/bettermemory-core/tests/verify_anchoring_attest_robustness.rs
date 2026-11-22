//! Hostile input for the attestation checks: long spellings, many
//! entries, every code point before a drive colon; no panic, and time
//! that grows in proportion to the entries.

mod common;
mod timing;

use bettermemory_core::fsdata::Flavour;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::verify_anchoring::attest::{
    is_absolute_attestation, join_under, normalize_attestations, resolves_under, unverifiable_at,
    worktree_root_is_live, FlavouredFs, Reads,
};
use common::attestation_paths::{cps, Scripted};
use common::verify_anchoring_attest::{gate, keys, paths, Stub};

#[test]
fn long_and_odd_spellings_do_not_panic() {
    let long: PyStr = std::iter::repeat_n(u32::from(b'a'), 1 << 20).collect();
    let deep: PyStr = cps(&"../".repeat(100_000));
    let max = vec![0x10_ffff, u32::from(b':'), u32::from(b'/')];
    assert!(!is_absolute_attestation(&long));
    let _ = is_absolute_attestation(&max);
    for f in [Flavour::Posix, Flavour::Windows] {
        let _ = join_under(f, &cps("/r"), &long);
        assert_eq!(join_under(f, &cps("/r"), &deep).unwrap(), None);
        let _ = join_under(f, &long, &cps("a.py"));
    }
    let p = paths("R", &[long.clone(), deep, vec![0xd800], vec![0]]);
    let _ = gate(false, &p, Some(&cps("/r")), None, false);
    let _ = gate(true, &p, Some(&cps("C:\\r")), None, false);
}

#[test]
fn the_gate_grows_with_its_entries() {
    let entry = |i: usize| cps(&format!("src/gone{i}.py"));
    let small: Vec<PyStr> = (0..300).map(entry).collect();
    let large: Vec<PyStr> = (0..3000).map(entry).collect();
    let (ps, pl) = (paths("P", &small), paths("P", &large));
    let root = cps("/r");
    timing::grows_within(
        "unverifiable_attestations",
        timing::TENFOLD,
        &mut || timing::once(|| gate(false, &ps, Some(&root), None, false)),
        &mut || timing::once(|| gate(false, &pl, Some(&root), None, false)),
    );
}

#[test]
fn the_other_checks_take_hostile_input_without_panic() {
    let long: PyStr = std::iter::repeat_n(u32::from(b'a'), 1 << 20).collect();
    let mut deep = cps("/bm-vaat-absent");
    deep.extend(cps(&"/a".repeat(20_000)));
    let odd = vec![u32::from(b'/'), 0xd800, 0, 0x10_ffff];
    let host = Scripted { nt: false };
    let fs = FlavouredFs {
        fs: &bettermemory_core::fsdata::StdFs,
        flavour: Flavour::Posix,
    };
    let wfs = FlavouredFs {
        fs: &bettermemory_core::fsdata::StdFs,
        flavour: Flavour::Windows,
    };
    for p in [&long, &deep, &odd] {
        let _ = resolves_under(&host, &fs, b"/", p, &cps("/bm-vaat-absent"));
        let _ = resolves_under(&host, &fs, b"/", &cps("/x"), p);
        let _ = resolves_under(&host, &wfs, b"C:\\", p, &cps("C:\\r"));
        let _ = worktree_root_is_live(&host, &fs, p);
        let _ = worktree_root_is_live(&host, &wfs, p);
    }
    let items = vec![long, deep, odd, Vec::new(), vec![0xdc80]];
    for kind in ["P", "R", "L"] {
        let p = paths(kind, &items);
        let _ = normalize_attestations(&host, &p, &keys);
        let anchor = Stub {
            root: Some(cps("/r")),
            convicting: true,
            declared: Some(cps("/r")),
        };
        for stored in [false, true] {
            let mut sparse = |_: &[u32]| false;
            let mut reads = Reads {
                normalize_for_compare: &|p: &[u32]| p.to_vec(),
                path_exists: &|_: &[u32]| false,
                absent_by_sparsity: &mut sparse,
            };
            let _ = unverifiable_at(&host, &p, &anchor, stored, &mut reads);
        }
    }
}

#[test]
fn the_claim_keys_grow_with_their_entries() {
    let host = Scripted { nt: false };
    let entry = |i: usize| cps(&format!("/src/gone{i}/k"));
    let small: Vec<PyStr> = (0..300).map(entry).collect();
    let large: Vec<PyStr> = (0..3000).map(entry).collect();
    let (ps, pl) = (paths("P", &small), paths("P", &large));
    timing::grows_within(
        "normalize_attestations",
        timing::TENFOLD,
        &mut || timing::once(|| normalize_attestations(&host, &ps, &keys)),
        &mut || timing::once(|| normalize_attestations(&host, &pl, &keys)),
    );
}
