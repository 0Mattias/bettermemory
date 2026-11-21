//! Every case of `checkout_token_write_posix.jsonl` (POSIX only): `write`
//! through `StdFs` over real git directories in a scratch folder. A token
//! written is random, so a row holds that the file then reads as it.

#![cfg(unix)]

mod common;

use std::os::unix::fs::PermissionsExt;
use std::path::Path;

use bettermemory_core::checkout_token::read::read_at;
use bettermemory_core::checkout_token::write::{write, Machine, WriteStatus};
use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use common::{assert_count, jsonl};
use serde_json::{json, Value};

const FILE: &str = "checkout_token_write_posix.jsonl";
const FILE_TEXT: &[u8] = b"bettermemory checkout token v1\n0123456789ABCDEFGHJKMNPQRS\n";

/// This process: its effective uid read from a file it made, the system's
/// random bytes, the real clock.
pub struct Real {
    pub euid: u32,
    pub start: std::time::Instant,
}

impl Real {
    pub fn new(scratch: &Path) -> Real {
        use std::os::unix::fs::MetadataExt;
        let probe = scratch.join("uid-probe");
        std::fs::write(&probe, b"").expect("probe");
        let euid = std::fs::metadata(&probe).expect("probe").uid();
        std::fs::remove_file(&probe).expect("probe");
        Real {
            euid,
            start: std::time::Instant::now(),
        }
    }
}

impl Machine for Real {
    fn euid(&mut self) -> u32 {
        self.euid
    }
    fn win32(&self) -> bool {
        false
    }
    fn urandom(&mut self, count: usize) -> Vec<u8> {
        let mut out = vec![0u8; count];
        getrandom::fill(&mut out).expect("random bytes");
        out
    }
    fn monotonic(&mut self) -> f64 {
        self.start.elapsed().as_secs_f64()
    }
    fn sleep(&mut self, seconds: f64) {
        std::thread::sleep(std::time::Duration::from_secs_f64(seconds));
    }
}

fn git(root: &Path, name: &str, info: bool) -> std::path::PathBuf {
    let path = root.join(name).join(".git");
    std::fs::create_dir_all(&path).expect("git dir");
    if info {
        std::fs::create_dir(path.join("info")).expect("info");
    }
    path
}

fn aged(path: &Path, seconds: u64) {
    std::fs::write(path, b"x").expect("temp");
    let past = std::time::SystemTime::now() - std::time::Duration::from_secs(seconds);
    let file = std::fs::File::options()
        .write(true)
        .open(path)
        .expect("temp");
    file.set_times(
        std::fs::FileTimes::new()
            .set_accessed(past)
            .set_modified(past),
    )
    .expect("times");
}

fn layout(root: &Path, case: &str) -> std::path::PathBuf {
    match case {
        "fresh" => git(root, "fresh", true),
        "no info" => git(root, "noinfo", false),
        "existing token" => {
            let g = git(root, "token", true);
            std::fs::write(g.join("info/bettermemory"), FILE_TEXT).expect("token");
            g
        }
        "existing malformed" => {
            let g = git(root, "malformed", true);
            std::fs::write(g.join("info/bettermemory"), b"hello\n").expect("file");
            g
        }
        "token name a folder" => {
            let g = git(root, "folder", true);
            std::fs::create_dir(g.join("info/bettermemory")).expect("folder");
            g
        }
        "stale temps" => {
            let g = git(root, "stale", true);
            aged(&g.join("info/bettermemory.tmp-OLD"), 7200);
            aged(&g.join("info/bettermemory.tmp-NEW"), 60);
            aged(&g.join("info/other.tmp-OLD"), 7200);
            g
        }
        "missing git dir" => root.join("missing/.git"),
        "read-only info" => {
            let g = git(root, "readonly", true);
            let mode = std::fs::Permissions::from_mode(0o555);
            std::fs::set_permissions(g.join("info"), mode).expect("mode");
            g
        }
        other => panic!("a case this test lays out: {other}"),
    }
}

#[test]
fn every_real_write_answers_as_the_reference() {
    let lines = jsonl(FILE);
    let rows = &lines[1..];
    assert_eq!(lines[0]["count"].as_u64(), Some(rows.len() as u64));
    let root = std::env::temp_dir().join(format!("bm-ct-write-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(&root).expect("root");
    let root = root.canonicalize().expect("root");
    let mut machine = Real::new(&root);
    let base = root.to_str().expect("UTF-8 scratch").to_owned();
    let ctx = Ctx {
        fs: &StdFs,
        flavour: Flavour::Posix,
        cwd: base.as_bytes(),
    };
    let mut checked = 0;
    for row in rows {
        let name = row["n"].as_str().expect("a name");
        let g = layout(&root, name);
        let got =
            write(&ctx, &mut machine, g.to_str().expect("UTF-8"), true, false).expect("an answer");
        if name == "read-only info" {
            let mode = std::fs::Permissions::from_mode(0o755);
            std::fs::set_permissions(g.join("info"), mode).expect("mode");
        }
        let mut answer = json!({"status": got.status.name(), "detail": got.detail});
        if got.status == WriteStatus::Written {
            let path = g.join("info/bettermemory");
            let found = read_at(&ctx, path.to_str().expect("UTF-8").as_bytes(), &mut |_| {})
                .expect("a read");
            answer["placed"] = Value::Bool(found.token == got.token && found.token.is_some());
        } else {
            answer["token"] = json!(got.token);
        }
        let info = g.join("info");
        answer["info"] = if info.is_dir() {
            let mut names: Vec<String> = std::fs::read_dir(&info)
                .expect("info")
                .map(|e| e.expect("entry").file_name().into_string().expect("UTF-8"))
                .collect();
            names.sort();
            json!(names)
        } else {
            Value::Null
        };
        assert_eq!(answer, row["r"], "{name}");
        checked += 1;
    }
    let _ = std::fs::remove_dir_all(&root);
    assert_count(FILE, checked, rows.len());
}
