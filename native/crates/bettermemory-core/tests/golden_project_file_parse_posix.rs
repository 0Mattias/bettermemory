//! Every case of `project_file_parse_posix.jsonl` (POSIX only): `parse`
//! through `StdFs` over a real tree in a scratch directory, with sizes at
//! the bound, a FIFO, a folder, links, a loop, a mode-000 file and a name
//! too long.

#![cfg(unix)]

mod common;

use std::os::unix::fs::{symlink, PermissionsExt};
use std::path::Path;

use bettermemory_core::fsdata::{Flavour, PyErr, StdFs};
use bettermemory_core::gitfile_rule::hostpath::text::Process;
use bettermemory_core::project_file::{parse, Found, Host};
use bettermemory_core::pyvalue::PyStr;
use common::{assert_count, jsonl};
use serde_json::json;

const FILE: &str = "project_file_parse_posix.jsonl";
const GOOD: &str = "[project]\nid = \"bmp_0123456789ABCDEFGHJKMNPQRS\"\nname = \"n\"\n";

struct NoCwd;

impl Process for NoCwd {
    fn getcwd(&self) -> Result<PyStr, PyErr> {
        panic!("every path is absolute")
    }
}

fn mkfifo(path: &Path) {
    let status = std::process::Command::new("mkfifo")
        .arg(path)
        .status()
        .expect("mkfifo runs");
    assert!(status.success(), "mkfifo");
}

fn sized(size: usize) -> Vec<u8> {
    let mut out = format!("{GOOD}#").into_bytes();
    let pad = size - out.len() - 1;
    out.extend(std::iter::repeat_n(b'a', pad));
    out.push(b'\n');
    out
}

fn layout(root: &Path) {
    std::fs::create_dir_all(root).expect("root");
    std::fs::write(root.join("good"), GOOD).expect("good");
    for size in [65535, 65536, 65537, 70000] {
        std::fs::write(root.join(format!("s{size}")), sized(size)).expect("sized");
    }
    std::fs::write(root.join("latin"), b"\xe9").expect("latin");
    std::fs::write(root.join("empty"), b"").expect("empty");
    mkfifo(&root.join("fifo"));
    std::fs::create_dir(root.join("dir")).expect("dir");
    symlink("good", root.join("link")).expect("link");
    symlink("nowhere", root.join("dangling")).expect("dangling");
    symlink("loop", root.join("loop")).expect("loop");
    symlink("fifo", root.join("fifolink")).expect("fifolink");
    std::fs::write(root.join("locked"), GOOD).expect("locked");
    std::fs::set_permissions(root.join("locked"), std::fs::Permissions::from_mode(0o000))
        .expect("mode");
}

fn s(cps: &[u32]) -> String {
    cps.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

#[test]
fn every_real_parse_answers_as_the_reference() {
    let lines = jsonl(FILE);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(rows.len() as u64));
    let root = std::env::temp_dir().join(format!("bm-pf-parse-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    layout(&root);
    let base = root.to_str().expect("UTF-8 scratch").to_owned();
    let host = Host {
        fs: &StdFs,
        flavour: Flavour::Posix,
        process: &NoCwd,
        darwin: cfg!(target_os = "macos"),
    };
    let mut checked = 0;
    for r in rows {
        let n = r["n"].as_str().unwrap();
        let name = match n.strip_prefix("x*") {
            Some(count) => "x".repeat(count.parse().unwrap()),
            None => n.to_owned(),
        };
        let path: PyStr = format!("{base}/{name}").chars().map(u32::from).collect();
        let got = match parse(&host, &path).expect("ported") {
            Found::File(f) => json!({"id": s(&f.id), "name": s(&f.name), "b": f.boundary,
                "p": s(&f.path)[base.len()..]}),
            Found::Error(e) => json!({"r": e.reason.name(), "d": s(&e.detail),
                "p": s(&e.path)[base.len()..]}),
        };
        assert_eq!(got, r["o"], "{n}");
        checked += 1;
    }
    let _ = std::fs::set_permissions(root.join("locked"), std::fs::Permissions::from_mode(0o644));
    let _ = std::fs::remove_dir_all(&root);
    assert_count(FILE, checked, rows.len());
}
