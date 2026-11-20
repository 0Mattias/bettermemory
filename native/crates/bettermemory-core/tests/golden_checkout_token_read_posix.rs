//! Every case of `checkout_token_read_posix.jsonl` (POSIX only): `_read_at`
//! and `read` through `StdFs` over a real tree in a scratch directory,
//! with links, a FIFO, a folder, a loop, a name too long and `/dev/null`.

#![cfg(unix)]

mod common;

use std::os::unix::fs::symlink;
use std::path::Path;

use bettermemory_core::checkout_token::read::{read, read_at, Fault};
use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use common::{assert_count, jsonl};
use serde_json::json;

const FILE: &str = "checkout_token_read_posix.jsonl";
const FILE_TEXT: &[u8] = b"bettermemory checkout token v1\n0123456789ABCDEFGHJKMNPQRS\n";

fn mkfifo(path: &Path) {
    let status = std::process::Command::new("mkfifo")
        .arg(path)
        .status()
        .expect("mkfifo runs");
    assert!(status.success(), "mkfifo");
}

fn layout(root: &Path) {
    std::fs::create_dir_all(root).expect("root");
    std::fs::write(root.join("token"), FILE_TEXT).expect("token");
    std::fs::write(root.join("empty"), b"").expect("empty");
    std::fs::write(root.join("big"), vec![b'a'; 5000]).expect("big");
    std::fs::write(root.join("exact"), vec![b'a'; 4096]).expect("exact");
    std::fs::write(root.join("nonascii"), b"\xff").expect("nonascii");
    std::fs::write(root.join("nonascii80"), b"\x80").expect("nonascii80");
    mkfifo(&root.join("fifo"));
    std::fs::create_dir(root.join("dir")).expect("dir");
    symlink("token", root.join("link")).expect("link");
    symlink("nowhere", root.join("dangling")).expect("dangling");
    symlink("loop", root.join("loop")).expect("loop");
    symlink("fifo", root.join("fifolink")).expect("fifolink");
    let info = root.join("repo/.git/info");
    std::fs::create_dir_all(&info).expect("info");
    std::fs::write(info.join("bettermemory"), FILE_TEXT).expect("repo token");
}

#[test]
fn every_real_read_answers_as_the_reference() {
    let lines = jsonl(FILE);
    let rows = &lines[1..];
    assert_eq!(lines[0]["count"].as_u64(), Some(rows.len() as u64));
    let root = std::env::temp_dir().join(format!("bm-ct-read-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    layout(&root);
    let base = root.to_str().expect("UTF-8 scratch").to_owned();
    let ctx = Ctx {
        fs: &StdFs,
        flavour: Flavour::Posix,
        cwd: base.as_bytes(),
    };
    let mut checked = 0;
    for row in rows {
        let name = row["n"].as_str().expect("a name");
        let mut pauses = Vec::new();
        let mut pause = |seconds: f64| pauses.push(seconds);
        let answer = if let Some(git) = name.strip_prefix("git:") {
            read(&ctx, &format!("{base}/{git}"), &mut pause)
        } else if name.starts_with('/') {
            read_at(&ctx, name.as_bytes(), &mut pause)
        } else {
            let entry = match name.strip_prefix("x*") {
                Some(n) => "x".repeat(n.parse().expect("a count")),
                None => name.to_owned(),
            };
            read_at(&ctx, format!("{base}/{entry}").as_bytes(), &mut pause)
        };
        let found = match answer {
            Ok(found) => found,
            Err(Fault::Raised(e)) => panic!("{name}: raised {e:?}"),
            Err(Fault::NotPorted) => panic!("{name}: not ported"),
        };
        let got =
            json!({"state": found.state.name(), "token": found.token, "detail": found.detail});
        assert_eq!(got, row["r"], "{name}");
        assert_eq!(json!(pauses), row["pauses"], "{name}: the pauses");
        checked += 1;
    }
    std::fs::remove_dir_all(&root).expect("remove scratch");
    assert_count(FILE, checked, rows.len());
}
