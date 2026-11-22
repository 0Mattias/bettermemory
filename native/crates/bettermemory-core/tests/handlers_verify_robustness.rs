//! Hostile input to `handlers/verify.py`'s gates: long and deep paths,
//! many entries, huge notes. No panic, and time grows linearly.

mod common;
mod timing;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::handlers::verify::{
    check_verify_args, refuse_evidence_free_stamp, relative_to_root, within, Arg, DriftLists,
    Machine, Refused,
};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;

fn fs() -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/w", Node::Dir);
    fs
}

fn m(fs: &MemFs) -> Machine<'_, MemFs> {
    Machine {
        fs,
        flavour: Flavour::Posix,
        cwd: Some(b"/w"),
    }
}

#[test]
fn deep_and_dotted_paths_do_not_panic() {
    let fs = fs();
    let mut deep = cps("/w");
    for _ in 0..5000 {
        deep.extend(cps("/a/.."));
    }
    assert_eq!(within(&m(&fs), Some(&deep), Some(&cps("/w"))), Ok(true));
    let slashes: PyStr = vec![u32::from(b'/'); 100_000];
    assert_eq!(
        relative_to_root(&m(&fs), &slashes, Some(&cps("/"))),
        Ok(cps("."))
    );
    let nul = vec![0u32; 10];
    assert_eq!(within(&m(&fs), Some(&nul), Some(&nul)), Ok(false));
    let bad: PyStr = vec![0x10FFFF, 0xD800, 0xDFFF];
    assert_eq!(
        relative_to_root(&m(&fs), &bad, Some(&cps("/"))),
        Ok(bad.clone())
    );
}

#[test]
fn huge_arguments_are_refused_quickly() {
    let note: PyStr = vec![u32::from(b'x'); 10_000_000];
    assert!(check_verify_args(Arg::Given(&note), [Arg::Absent; 4]).is_err());
    let many: Vec<PyStr> = vec![cps("p"); 1_000_000];
    assert!(check_verify_args(
        Arg::Absent,
        [Arg::Given(&many), Arg::Absent, Arg::Absent, Arg::Absent]
    )
    .is_err());
}

#[test]
fn the_evidence_gate_grows_linearly_in_the_citations() {
    let fs = fs();
    let make = |n: usize| -> Vec<PyStr> { (0..n).map(|i| cps(&format!("/w/p{i}"))).collect() };
    let small = make(2_000);
    let large = make(20_000);
    let run = |checked: &Vec<PyStr>| {
        let missing = checked[..checked.len() / 2].to_vec();
        let r = refuse_evidence_free_stamp(
            &m(&fs),
            &cps("m"),
            &cps("b"),
            Some(&cps("/w")),
            false,
            false,
            false,
            None,
            |_| true,
            |_, _| DriftLists {
                checked: checked.clone(),
                missing,
                expected_absent: Vec::new(),
            },
        );
        assert!(matches!(r, Err(Refused::ValueError(_))));
    };
    // `gone` is a set in the reference; a list scan here would be quadratic.
    timing::grows_within(
        "evidence gate",
        timing::TENFOLD,
        &mut || timing::once(|| run(&small)),
        &mut || timing::once(|| run(&large)),
    );
}
