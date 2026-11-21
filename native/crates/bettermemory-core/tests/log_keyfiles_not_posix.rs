//! `log_keyfiles_posix.jsonl` and `log_keyfiles_darwin.jsonl` have no
//! Windows case: their generator makes links, mode-000 files and folders,
//! which Windows answers otherwise, and the reference locks the keys
//! directory there with `msvcrt.locking`, which `log::ring::FileLock` does
//! not. `golden_log_keyfiles_posix.rs` runs on POSIX only; here the gap is
//! recorded. `golden_log_keyfiles.rs` runs everywhere.

#![cfg(not(unix))]

mod common;

#[test]
fn the_posix_key_ring_fixture_has_no_case_here() {
    common::parity::record(
        "golden_log_keyfiles_posix",
        "log_keyfiles_posix.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture makes links and mode-000 files; Windows has no case".to_owned()],
    );
}

#[test]
fn the_strerror_messages_have_no_case_here() {
    common::parity::record(
        "golden_log_keyfiles_posix",
        "log_keyfiles_darwin.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the C library's strerror over POSIX files; Windows has no case".to_owned()],
    );
}

/// `Path.resolve()` is not strict: a store file that is not there yet is
/// bound by its resolved spelling, as the reference binds it.
#[test]
fn a_missing_store_file_is_bound() {
    use bettermemory_core::log::ring::KeyFiles;
    use bettermemory_core::pyvalue::PyValue;
    let root = std::fs::canonicalize(std::env::temp_dir())
        .unwrap()
        .join(format!("bm-kf-np-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(root.join("keys")).unwrap();
    let r = KeyFiles::new(
        &root.join("keys"),
        &PyValue::str("0123456789abcdef0123456789abcdef"),
    )
    .unwrap();
    let store = root.join("absent").join("s.db");
    assert_eq!(r.rebind_store(&store, b"").unwrap(), None);
    let bound = r.read_binding().unwrap().expect("a binding");
    let bound = bound.to_str().unwrap().to_owned();
    assert!(!bound.starts_with(r"\\?\"), "{bound}");
    assert!(bound.ends_with(r"absent\s.db"), "{bound}");
    std::fs::remove_dir_all(&root).unwrap();
}
