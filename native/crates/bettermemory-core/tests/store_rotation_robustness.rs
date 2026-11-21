//! Hostile input to `store::rotation`: a pending key file of 8 MiB, a
//! binding nested 100,000 deep, a recorded fingerprint of a megabyte, a
//! store path past PATH_MAX, segment lists of 100,000; no panic, and
//! `_signs_a_segment` linear in the segments.

#![cfg(unix)]

mod timing;

use std::fs;
use std::path::{Path, PathBuf};

use bettermemory_core::log::ring::KeyFiles;
use bettermemory_core::log::{fingerprint, LogRow};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::rotation::{
    finish_rotation, keys_dir_unreadable, signs_a_segment, Declined, SegmentStart,
};

const ID: &str = "0123456789abcdef0123456789abcdef";

fn scratch(tag: &str) -> PathBuf {
    let root = fs::canonicalize(std::env::temp_dir())
        .unwrap()
        .join(format!("bm-rot-r-{tag}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&root);
    fs::create_dir_all(root.join("keys")).unwrap();
    root
}

fn ring(root: &Path) -> KeyFiles {
    KeyFiles::new(&root.join("keys"), &PyValue::str(ID)).unwrap()
}

#[test]
fn a_huge_pending_file_is_left_alone() {
    let root = scratch("huge");
    let pending = root.join(format!("keys/{ID}.key.pending"));
    fs::write(&pending, vec![7u8; 8 << 20]).unwrap();
    let recorded = "a".repeat(1 << 20);
    let got = finish_rotation(
        &ring(&root),
        &root.join("s.db"),
        &recorded,
        b"/",
        false,
        &mut |_| {},
    )
    .unwrap();
    assert!(got.key.is_none() && got.log.is_empty());
    assert!(pending.exists());
    fs::remove_dir_all(&root).unwrap();
}

#[test]
fn a_deep_binding_raises_recursion_error() {
    let root = scratch("deep");
    let key: Vec<u8> = (32u8..64).collect();
    fs::write(root.join(format!("keys/{ID}.key.pending")), &key).unwrap();
    let n = 100_000;
    fs::write(
        root.join(format!("keys/{ID}.path")),
        format!("{}{}", "[".repeat(n), "]".repeat(n)),
    )
    .unwrap();
    let got = finish_rotation(
        &ring(&root),
        &root.join("s.db"),
        &fingerprint(&key),
        b"/",
        false,
        &mut |_| {},
    );
    match got {
        Err(Declined::Py(e)) => assert_eq!(e.name(), "RecursionError"),
        other => panic!("{other:?}"),
    }
    fs::remove_dir_all(&root).unwrap();
}

#[test]
fn a_store_path_past_path_max_is_not_bound() {
    let root = scratch("long");
    let key: Vec<u8> = (32u8..64).collect();
    fs::write(root.join(format!("keys/{ID}.key.pending")), &key).unwrap();
    fs::write(
        root.join(format!("keys/{ID}.path")),
        b"{\"path\": \"/elsewhere.db\"}",
    )
    .unwrap();
    let store = root.join("x/".repeat(3000)).join("s.db");
    let got = finish_rotation(
        &ring(&root),
        &store,
        &fingerprint(&key),
        b"/",
        false,
        &mut |_| {},
    )
    .unwrap();
    assert!(got.key.is_none());
    fs::remove_dir_all(&root).unwrap();
}

#[test]
fn hostile_reasons_do_not_panic() {
    let long = "it ".repeat(100_000);
    let (reason, message) = keys_dir_unreadable(Path::new("/k"), Path::new("/"), &long);
    assert_eq!(reason.len(), "/, on the way to it, ".len() + long.len() - 3);
    assert!(message.contains(&reason));
    let _ = keys_dir_unreadable(Path::new(""), Path::new(""), "\u{0}\u{fffd}");
}

fn rows(n: usize) -> Vec<(String, LogRow)> {
    (0..n)
        .map(|i| {
            (
                format!("{i:064x}"),
                LogRow {
                    seq: i as i64,
                    ts: String::new(),
                    session: None,
                    kind: "rekey".into(),
                    payload: "{}".into(),
                    prev_mac: String::new(),
                    mac: String::new(),
                },
            )
        })
        .collect()
}

#[test]
fn signs_a_segment_grows_linearly() {
    let s = rows(10_000);
    let l = rows(100_000);
    let key = [1u8; 32];
    timing::grows_within(
        "signs_a_segment",
        30,
        &mut || {
            let segs: Vec<SegmentStart<'_>> = s
                .iter()
                .map(|(f, r)| SegmentStart {
                    key_fingerprint: f,
                    first_row: Some(r),
                })
                .collect();
            timing::once(|| assert!(!signs_a_segment(&segs, &key)))
        },
        &mut || {
            let segs: Vec<SegmentStart<'_>> = l
                .iter()
                .map(|(f, r)| SegmentStart {
                    key_fingerprint: f,
                    first_row: Some(r),
                })
                .collect();
            timing::once(|| assert!(!signs_a_segment(&segs, &key)))
        },
    );
}
