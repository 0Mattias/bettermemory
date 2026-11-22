//! Every case of `checkout_token_remint_posix.jsonl` (POSIX only):
//! `remint` through `StdFs` over real git directories in a scratch
//! folder. A token written is random, so a row holds that the file then
//! reads as it and whether it succeeds the lineage.

#![cfg(unix)]

mod common;
mod ct_remint_script;

use std::path::{Path, PathBuf};

use bettermemory_core::checkout_token::read::read_at;
use bettermemory_core::checkout_token::remint::remint;
use bettermemory_core::checkout_token::succeeds;
use bettermemory_core::checkout_token::write::{Machine, WriteStatus};
use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::pyvalue::PyValue;
use common::{assert_count, jsonl};
use ct_remint_script::lineage;
use serde_json::{json, Value};

const FILE: &str = "checkout_token_remint_posix.jsonl";
const TOK: &str = "0123456789ABCDEFGHJKMNPQRS";
const OTHER: &str = "ZZZZZZZZZZZZZZZZZZZZZZZZZZ";
const SIB: &str = "0000000000000BE8CTNH4HS6C8";

/// This process: its effective uid, the system's random bytes, the real
/// clock.
struct Real {
    euid: u32,
    start: std::time::Instant,
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

fn git(root: &Path, name: &str, body: Option<&[u8]>) -> PathBuf {
    let path = root.join(name).join(".git");
    std::fs::create_dir_all(path.join("info")).expect("info");
    if let Some(body) = body {
        std::fs::write(path.join("info/bettermemory"), body).expect("token");
    }
    path
}

fn file(token: &str) -> Vec<u8> {
    format!("bettermemory checkout token v1\n{token}\n").into_bytes()
}

fn layout(root: &Path, case: &str) -> PathBuf {
    match case {
        "fresh, no lineage" => git(root, "a", None),
        "token, no lineage" => git(root, "b", Some(&file(OTHER))),
        "token named" => git(root, "c", Some(&file(TOK))),
        "no token, lineage" => git(root, "d", None),
        "other token" => git(root, "e", Some(&file(OTHER))),
        "other token, only siblings" => git(root, "f", Some(&file(OTHER))),
        "sibling, only siblings" => git(root, "g", Some(&file(SIB))),
        "claim name a folder" => {
            let g = git(root, "h", Some(&file(TOK)));
            std::fs::create_dir(g.join("info/bettermemory.tmp-claim")).expect("folder");
            g
        }
        "malformed, lineage" => git(root, "i", Some(b"hello\n")),
        "missing git dir" => root.join("missing/.git"),
        other => panic!("a case this test lays out: {other}"),
    }
}

#[test]
fn every_real_remint_answers_as_the_reference() {
    use std::os::unix::fs::MetadataExt;
    let lines = jsonl(FILE);
    let rows = &lines[1..];
    assert_eq!(lines[0]["count"].as_u64(), Some(rows.len() as u64));
    let root = std::env::temp_dir().join(format!("bm-ct-remint-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(&root).expect("root");
    let root = root.canonicalize().expect("root");
    let mut machine = Real {
        euid: std::fs::metadata(&root).expect("root").uid(),
        start: std::time::Instant::now(),
    };
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
        let rep = lineage(row);
        let sib = row["sib"].as_bool().expect("sib");
        let path = g.to_str().expect("UTF-8");
        let got =
            remint(&ctx, &mut machine, path, true, false, rep.as_ref(), sib).expect("an answer");
        let mut answer = json!({"status": got.status.name(), "detail": got.detail});
        if got.status == WriteStatus::Written {
            let token = g.join("info/bettermemory");
            let found = read_at(&ctx, token.to_str().expect("UTF-8").as_bytes(), &mut |_| {})
                .expect("a read");
            answer["placed"] = Value::Bool(found.token == got.token && found.token.is_some());
            let value = PyValue::Str(
                got.token
                    .clone()
                    .unwrap_or_default()
                    .chars()
                    .map(u32::from)
                    .collect(),
            );
            answer["succeeds"] = Value::Bool(succeeds(&value, rep.as_ref()).expect("succeeds"));
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
