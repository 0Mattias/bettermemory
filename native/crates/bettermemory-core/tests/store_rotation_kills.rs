//! Cases a plausible wrong `store::rotation` fails: the Windows rename
//! retry, a finish that succeeds on a later attempt, `str(exc)`'s quoting
//! of a path holding a quote, the retired copy never written over, the
//! length and fingerprint checks, the prefix of `KeysDirUnreadableError`
//! removed once. What the reference does is read from `store_keys.py`
//! and `_fsutil.replace_atomic`; the file-system cases are POSIX only.

#![cfg(unix)]

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use bettermemory_core::log::fingerprint;
use bettermemory_core::log::ring::KeyFiles;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::rotation::{
    finish_for_signing, finish_rotation, keys_dir_unreadable, rename_read_attempts, write_key_once,
    Level,
};

const ID: &str = "0123456789abcdef0123456789abcdef";

fn new_key() -> Vec<u8> {
    (32u8..64).collect()
}

fn scratch(tag: &str) -> PathBuf {
    let root = fs::canonicalize(std::env::temp_dir())
        .unwrap()
        .join(format!("bm-rot-k-{tag}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&root);
    fs::create_dir_all(root.join("keys")).unwrap();
    fs::write(root.join("store.db"), b"db").unwrap();
    root
}

fn ring(root: &Path) -> KeyFiles {
    KeyFiles::new(&root.join("keys"), &PyValue::str(ID)).unwrap()
}

fn cleanup(root: &Path) {
    let _ = fs::set_permissions(root.join("keys"), fs::Permissions::from_mode(0o700));
    let _ = fs::remove_dir_all(root);
}

fn cwd(root: &Path) -> &[u8] {
    root.to_str().unwrap().as_bytes()
}

/// `replace_atomic` on Windows tries a rename refused as PermissionError
/// five times, pausing 0.01 s doubling between; off Windows once.
#[test]
fn the_windows_rename_is_tried_five_times() {
    let root = scratch("win");
    let pending = root.join(format!("keys/{ID}.key.pending"));
    fs::write(&pending, new_key()).unwrap();
    fs::set_permissions(root.join("keys"), fs::Permissions::from_mode(0o500)).unwrap();
    let recorded = fingerprint(&new_key());
    let mut sleeps = Vec::new();
    let got = finish_rotation(
        &ring(&root),
        &root.join("store.db"),
        &recorded,
        cwd(&root),
        true,
        &mut |s| sleeps.push(s),
    )
    .unwrap();
    assert!(got.key.is_none());
    assert_eq!(sleeps, vec![0.01, 0.02, 0.04, 0.08]);
    assert_eq!(got.log.len(), 1);
    let mut none = Vec::new();
    let _ = finish_rotation(
        &ring(&root),
        &root.join("store.db"),
        &recorded,
        cwd(&root),
        false,
        &mut |s| none.push(s),
    );
    assert!(none.is_empty());
    cleanup(&root);
}

#[test]
fn rename_read_attempts_follows_the_platform() {
    assert_eq!(rename_read_attempts(true), 5);
    assert_eq!(rename_read_attempts(false), 1);
}

/// `_finish_for_signing` returns the key a later attempt finishes: here
/// the binding to another file is gone by the second attempt.
#[test]
fn a_later_attempt_finishes() {
    let root = scratch("later");
    fs::write(root.join("other.db"), b"x").unwrap();
    let binding = root.join(format!("keys/{ID}.path"));
    fs::write(
        &binding,
        format!("{{\"path\": \"{}\"}}", root.join("other.db").display()),
    )
    .unwrap();
    fs::write(root.join(format!("keys/{ID}.key.pending")), new_key()).unwrap();
    let recorded = fingerprint(&new_key());
    let mut sleeps = Vec::new();
    let got = finish_for_signing(
        &ring(&root),
        &root.join("store.db"),
        &recorded,
        cwd(&root),
        false,
        3,
        &mut |s| {
            sleeps.push(s);
            let _ = fs::remove_file(&binding);
        },
    )
    .unwrap();
    assert_eq!(got.key.unwrap().bytes(), new_key().as_slice());
    assert_eq!(sleeps, vec![0.01]);
    assert_eq!(got.log.len(), 1);
    assert_eq!(got.log[0].level, Level::Info);
    cleanup(&root);
}

/// `repr` of a path with a quote and no double quote uses double quotes.
#[test]
fn a_quote_in_the_path_is_quoted_as_repr_does() {
    let root = scratch("q").join("it's");
    fs::create_dir_all(root.join("keys")).unwrap();
    fs::create_dir(root.join(format!("keys/{ID}.key.pending"))).unwrap();
    let got = finish_rotation(
        &ring(&root),
        &root.join("store.db"),
        "x",
        cwd(&root),
        false,
        &mut |_| {},
    )
    .unwrap();
    let text = &got.log[0].text;
    assert!(
        text.ends_with(&format!("\"{}/keys/{ID}.key.pending\"", root.display())),
        "{text}"
    );
    cleanup(root.parent().unwrap());
}

/// A retired copy that is there is never written over, and a current key
/// meta names is not copied.
#[test]
fn the_retired_copy_is_written_once() {
    let root = scratch("ret");
    let old: Vec<u8> = (0u8..32).collect();
    let retired = root.join(format!("keys/{ID}.{}.key", &fingerprint(&old)[..16]));
    assert!(write_key_once(&retired, &old).unwrap());
    assert_eq!(
        fs::metadata(&retired).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert!(!write_key_once(&retired, b"other").unwrap());
    assert_eq!(fs::read(&retired).unwrap(), old);
    cleanup(&root);
}

/// The pending key must be 32 bytes and its fingerprint meta's exactly.
#[test]
fn a_pending_key_meta_does_not_name_is_left_alone() {
    let root = scratch("left");
    let pending = root.join(format!("keys/{ID}.key.pending"));
    let mut long = new_key();
    long.push(0);
    for (bytes, recorded) in [
        (long.clone(), fingerprint(&long)),
        (new_key(), fingerprint(&new_key()).to_uppercase()),
        (new_key(), fingerprint(&new_key())[..63].to_owned()),
    ] {
        fs::write(&pending, &bytes).unwrap();
        let got = finish_rotation(
            &ring(&root),
            &root.join("store.db"),
            &recorded,
            cwd(&root),
            false,
            &mut |_| {},
        )
        .unwrap();
        assert!(got.key.is_none() && got.log.is_empty());
        assert_eq!(fs::read(&pending).unwrap(), bytes);
    }
    cleanup(&root);
}

/// `str.removeprefix` removes one `it `, not every one.
#[test]
fn the_reason_loses_one_prefix() {
    let (reason, _) = keys_dir_unreadable(Path::new("/k/keys"), Path::new("/k"), "it it x");
    assert_eq!(reason, "/k, on the way to it, it x");
    let (same, _) = keys_dir_unreadable(Path::new("/k/keys"), Path::new("/k/keys/"), "it x");
    assert_eq!(same, "it x");
}
