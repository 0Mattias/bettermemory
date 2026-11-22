//! Hostile input to `store_refusal`: a manifest and a marker nested past
//! any recursion limit, a manifest at the cap of many keys (time grows
//! with the size), a chain of symbolic links and a long name. No panic in
//! debug or release.

mod common;
mod store_refusal_case;
mod timing;

use std::fs;

use bettermemory_core::pyvalue::PyError;
use bettermemory_core::store_refusal::{
    may_be_bundle_manifest, restore_refusal, restore_state, store_refusal, Failure,
};

#[test]
fn deep_nesting_counts_as_a_bundle_and_raises_out_of_a_marker() {
    let d = store_refusal_case::base("r-deep");
    let m = d.join("manifest.json");
    fs::write(&m, "[".repeat(1 << 20)).unwrap();
    assert!(may_be_bundle_manifest(&m));
    fs::remove_file(&m).unwrap();
    fs::write(d.join("s.db.restore-lock"), "[".repeat(60_000)).unwrap();
    match restore_state(&d.join("s.db")) {
        Err(Failure::Raised(PyError::RecursionError)) => {}
        other => panic!("{other:?}"),
    }
    assert!(matches!(
        restore_refusal(&d.join("s.db")),
        Err(Failure::Raised(PyError::RecursionError))
    ));
    // On Windows `bundle_refusal` declines first: `Path.resolve` is not
    // ported there (ENG-124).
    let expected = if cfg!(unix) {
        Err(Failure::Raised(PyError::RecursionError))
    } else {
        Err(Failure::NotPorted)
    };
    assert_eq!(store_refusal(&d.join("s.db")), expected);
    let _ = fs::remove_dir_all(&d);
}

#[test]
fn a_manifest_reads_in_time_that_grows_with_its_size() {
    let d = store_refusal_case::base("r-size");
    let doc = |keys: usize| {
        let body: Vec<String> = (0..keys).map(|i| format!("\"k{i}\": {i}")).collect();
        format!("{{{}}}", body.join(", "))
    };
    let small = d.join("small.json");
    let large = d.join("large.json");
    fs::write(&small, doc(4_000)).unwrap();
    fs::write(&large, doc(40_000)).unwrap();
    assert!(fs::metadata(&large).unwrap().len() < 1 << 20);
    timing::grows_within(
        "manifest: 4,000 and 40,000 keys",
        timing::TENFOLD,
        &mut || timing::once(|| may_be_bundle_manifest(&small)),
        &mut || timing::once(|| may_be_bundle_manifest(&large)),
    );
    let _ = fs::remove_dir_all(&d);
}

#[cfg(unix)]
#[test]
fn a_chain_of_links_and_a_long_name_answer() {
    let d = store_refusal_case::base("r-chain");
    fs::write(d.join("l0"), b"x").unwrap();
    for i in 1..200 {
        std::os::unix::fs::symlink(d.join(format!("l{}", i - 1)), d.join(format!("l{i}"))).unwrap();
    }
    fs::write(
        d.join("manifest.json"),
        br#"{"format": "bettermemory.backup"}"#,
    )
    .unwrap();
    assert!(store_refusal(&d.join("l199")).unwrap().is_some());
    let long = d.join("n".repeat(250));
    assert!(store_refusal(&long).unwrap().is_some());
    assert!(store_refusal(&d.join("x".repeat(5_000))).unwrap().is_some());
    let _ = fs::remove_dir_all(&d);
}
