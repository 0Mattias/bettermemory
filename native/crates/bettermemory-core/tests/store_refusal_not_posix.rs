//! `store_refusal_posix.jsonl` has no Windows case: its generator holds
//! `flock` locks, makes links and a FIFO, and counts a directory's links,
//! which Windows answers otherwise. The port's Windows reading of the
//! restore lock (`try_lock` on the whole record file for the reference's
//! one byte at 1 MiB) and its link count (`nNumberOfLinks`) are untested
//! here. `Path.resolve` (`ntpath.realpath`) is not ported for the Windows
//! flavour (ENG-124): `bundle_refusal` declines where the spelled path
//! finds no manifest, and the test below checks that decline.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_store_refusal_fixture_has_no_case_here() {
    common::parity::record(
        "golden_store_refusal_posix",
        "store_refusal_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture holds flock locks, makes links and a FIFO; Windows has no case".to_owned()],
    );
}

#[test]
fn a_store_with_no_manifest_beside_it_is_declined() {
    use bettermemory_core::store_refusal::{bundle_refusal, store_refusal, Failure};
    let d = std::env::temp_dir().join(format!("bm-srefusal-np-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    assert_eq!(bundle_refusal(&d.join("s.db")), Err(Failure::NotPorted));
    assert_eq!(store_refusal(&d.join("s.db")), Err(Failure::NotPorted));
    std::fs::write(
        d.join("manifest.json"),
        br#"{"format": "bettermemory.backup"}"#,
    )
    .unwrap();
    assert!(bundle_refusal(&d.join("s.db")).unwrap().is_some());
    let _ = std::fs::remove_dir_all(&d);
    common::parity::record(
        "store_refusal_not_posix",
        "store_refusal.jsonl",
        "os",
        0,
        &["bundle_refusal past the spelled candidate".to_owned()],
        &[
            "Path.resolve (ntpath.realpath) is not ported for the Windows flavour (ENG-124)"
                .to_owned(),
        ],
    );
}
