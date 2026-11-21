//! `store_rotation.jsonl` and `store_rotation_python.jsonl` have no
//! Windows case: their generator makes links, hard links, mode-000 files
//! and read-only folders, which Windows answers otherwise. And under the Windows flavour a finish whose ring is
//! bound to a file declines (`Declined::NotPorted`) where the reference
//! compares the binding with `Path.resolve()`: `fsdata` does not resolve
//! a Windows path (ENG-124). A ring bound to no file still finishes.

#![cfg(not(unix))]

mod common;

use std::fs;

use bettermemory_core::log::fingerprint;
use bettermemory_core::log::ring::KeyFiles;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::rotation::{finish_rotation, Declined};

#[test]
fn the_posix_rotation_fixture_has_no_case_here() {
    common::parity::record(
        "golden_store_rotation",
        "store_rotation.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture makes links and mode-000 files; Windows has no case".to_owned()],
    );
    common::parity::record(
        "golden_store_rotation",
        "store_rotation_python.jsonl",
        "os",
        0,
        &["every case of the fixture".to_owned()],
        &["the fixture makes links and mode-000 files; Windows has no case".to_owned()],
    );
}

#[test]
fn a_bound_ring_declines_and_an_unbound_one_finishes() {
    let id = "0123456789abcdef0123456789abcdef";
    let root = std::env::temp_dir().join(format!("bm-rot-np-{}", std::process::id()));
    let _ = fs::remove_dir_all(&root);
    fs::create_dir_all(root.join("keys")).unwrap();
    let ring = KeyFiles::new(&root.join("keys"), &PyValue::str(id)).unwrap();
    let key: Vec<u8> = (32u8..64).collect();
    fs::write(root.join(format!("keys/{id}.key.pending")), &key).unwrap();
    fs::write(
        root.join(format!("keys/{id}.path")),
        b"{\"path\": \"C:\\\\s.db\"}",
    )
    .unwrap();
    let store = root.join("s.db");
    let got = finish_rotation(&ring, &store, &fingerprint(&key), b"", true, &mut |_| {});
    assert!(matches!(got, Err(Declined::NotPorted)));
    fs::remove_file(root.join(format!("keys/{id}.path"))).unwrap();
    let got = finish_rotation(&ring, &store, &fingerprint(&key), b"", true, &mut |_| {}).unwrap();
    assert_eq!(got.key.unwrap().bytes(), key.as_slice());
    fs::remove_dir_all(&root).unwrap();
}
