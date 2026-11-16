//! The provenance readers write nothing: every `store_provenance.jsonl`
//! case's store directory (the file, its keys directory, anything beside
//! them) has the same names, sizes and bytes after every query as before,
//! and every directory the same modification time (so nothing was made and
//! removed in between),
//! and a store whose files and directories are read-only answers as the
//! reference did. POSIX only (modes), as the fixture is.

#![cfg(unix)]

mod common;

use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use common::provenance::{answers, build, cases, same, scratch};
use serde_json::Value;

/// Every file under `dir`: its path, size and bytes; every directory,
/// `dir` included, with its modification time, which an entry made and
/// removed during the calls (a `-wal`, a `-shm`, a journal) moves even
/// when the listing after is the same.
fn listing(dir: &Path) -> BTreeMap<PathBuf, (u64, Vec<u8>)> {
    let mut out = BTreeMap::new();
    let stamp = |p: &Path| {
        let t = fs::metadata(p).expect("meta").modified().expect("mtime");
        let d = t.duration_since(std::time::UNIX_EPOCH).expect("epoch");
        d.as_nanos().to_le_bytes().to_vec()
    };
    out.insert(dir.to_path_buf(), (0, stamp(dir)));
    let mut todo = vec![dir.to_path_buf()];
    while let Some(d) = todo.pop() {
        for entry in fs::read_dir(&d).expect("read_dir") {
            let path = entry.expect("entry").path();
            let meta = fs::symlink_metadata(&path).expect("meta");
            if meta.is_dir() {
                out.insert(path.clone(), (0, stamp(&path)));
                todo.push(path);
            } else {
                out.insert(path.clone(), (meta.len(), fs::read(&path).expect("read")));
            }
        }
    }
    out
}

fn set_modes(dir: &Path, file_mode: u32, dir_mode: u32) {
    let mut todo = vec![dir.to_path_buf()];
    let mut dirs = Vec::new();
    while let Some(d) = todo.pop() {
        for entry in fs::read_dir(&d).expect("read_dir") {
            let path = entry.expect("entry").path();
            if path.is_dir() {
                todo.push(path);
            } else {
                fs::set_permissions(&path, fs::Permissions::from_mode(file_mode)).expect("chmod");
            }
        }
        dirs.push(d);
    }
    for d in dirs {
        fs::set_permissions(&d, fs::Permissions::from_mode(dir_mode)).expect("chmod dir");
    }
}

fn check(case: &Value, got: &[Value]) {
    for (q, g) in case["queries"].as_array().expect("queries").iter().zip(got) {
        let mut want = serde_json::Map::new();
        for k in ["returned", "raised"] {
            if let Some(v) = q.get(k) {
                want.insert(k.to_owned(), v.clone());
            }
        }
        assert!(
            same(&Value::Object(want), g),
            "{}: {q} got {g}",
            case["case"]
        );
    }
}

#[test]
fn no_query_changes_a_byte_of_the_store_or_its_keys() {
    let (header, cases) = cases();
    let id = header["store_id"].as_str().expect("id");
    for case in &cases {
        let dir = scratch("ro");
        let (file, keys) = build(case, id, &dir.0);
        let before = listing(&dir.0);
        let got = answers(
            &file,
            &keys,
            id,
            !case["verifies"].is_null(),
            case["strict"].as_bool().expect("strict"),
            case["queries"].as_array().expect("queries"),
        )
        .expect("answers");
        check(case, &got);
        let after = listing(&dir.0);
        assert_eq!(
            before.keys().collect::<Vec<_>>(),
            after.keys().collect::<Vec<_>>(),
            "{}: the files changed",
            case["case"]
        );
        assert!(before == after, "{}: a file's bytes changed", case["case"]);
    }
}

#[test]
fn a_store_whose_files_are_read_only_answers_as_the_reference() {
    let (header, cases) = cases();
    let id = header["store_id"].as_str().expect("id");
    for case in &cases {
        let dir = scratch("ro-modes");
        let (file, keys) = build(case, id, &dir.0);
        let before = listing(&dir.0);
        set_modes(&dir.0, 0o444, 0o555);
        let got = answers(
            &file,
            &keys,
            id,
            !case["verifies"].is_null(),
            case["strict"].as_bool().expect("strict"),
            case["queries"].as_array().expect("queries"),
        );
        set_modes(&dir.0, 0o644, 0o755);
        check(case, &got.expect("answers"));
        assert!(before == listing(&dir.0), "{}: changed", case["case"]);
    }
}
