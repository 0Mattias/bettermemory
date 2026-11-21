//! Every case of `location_leaves_posix.jsonl` against `location::leaves`
//! on disk: `remote_key`'s local keys through `realpath`, `kept_apart`,
//! `_real_git`, `record_working_tree` with the record store scripted, and
//! `_stamp`'s three shapes. Each case is built in a fresh scratch folder,
//! `@` in the file.
//!
//! POSIX only: the trees hold links; `location_leaves_not_posix.rs`
//! records the gap elsewhere.

#![cfg(unix)]

mod common;

use std::cell::Cell;
use std::path::{Path, PathBuf};

use bettermemory_core::capture::remote::{remote_key, RemoteKey};
use bettermemory_core::capture::Kind;
use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::gitfile_rule::follow::PinPaths;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::{Ctx, Failure};
use bettermemory_core::location::leaves::{
    kept_apart, local_key, real_git, record_working_tree, stamp, PinStore,
};
use bettermemory_core::location_git::Git;
use common::parity::Ledger;
use serde_json::{json, Value};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn string(t: &[u32]) -> String {
    t.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

fn scratch(i: usize) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("bm-leaves-{}-{i}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::canonicalize(&dir).unwrap()
}

/// The generator's `_build`.
pub fn build(root: &str, tree: &Value) {
    for entry in tree.as_array().unwrap() {
        let at = Path::new(root).join(entry[1].as_str().unwrap());
        let kind = entry[0].as_str().unwrap();
        if kind == "d" {
            std::fs::create_dir_all(&at).unwrap();
            continue;
        }
        std::fs::create_dir_all(at.parent().unwrap()).unwrap();
        let body = entry[2].as_str().unwrap().replace('@', root);
        if kind == "f" {
            std::fs::write(&at, body).unwrap();
        } else {
            std::os::unix::fs::symlink(body, &at).unwrap();
        }
    }
}

fn at(v: &Value, root: &str) -> Option<String> {
    v.as_str().map(|s| s.replace('@', root))
}

fn kind(s: &str) -> Kind {
    match s {
        "primary" => Kind::Primary,
        "linked" => Kind::Linked,
        "bare" => Kind::Bare,
        _ => Kind::Folder,
    }
}

fn kind_str(k: Kind) -> &'static str {
    k.as_str()
}

/// The generator's scripted `gitdir_pin`: the first `recorded` answers
/// `first`, every later one `second`; `put` answers `put`.
pub struct Script {
    pub answers: [Option<PinPaths>; 2],
    pub put: bool,
    pub asked: Cell<usize>,
}

impl PinStore for Script {
    fn recorded(&self, _: &[u8]) -> Result<Option<PinPaths>, Failure> {
        let i = self.asked.get();
        self.asked.set(i + 1);
        Ok(self.answers[i.min(1)].clone())
    }
    fn put(&self, _: &[u8], _: &[u8], _: Option<&PinPaths>) -> bool {
        self.put
    }
}

fn pin(v: &Value, root: &str) -> Option<PinPaths> {
    v.as_array().map(|p| PinPaths {
        git_dir: cps(&at(&p[0], root).unwrap()),
        tree: cps(&at(&p[1], root).unwrap()),
    })
}

fn back(s: &[u8], root: &str) -> Value {
    json!(String::from_utf8(s.to_vec()).unwrap().replace(root, "@"))
}

fn check(i: usize, row: &Value) {
    let dir = scratch(i);
    let root = dir.to_str().unwrap().to_owned();
    let ctx = Ctx {
        fs: &StdFs,
        flavour: Flavour::Posix,
        cwd: root.as_bytes(),
    };
    let got = match row["k"].as_str().unwrap() {
        "local" => {
            build(
                &root,
                &json!([
                    ["d", "a/repo"],
                    ["l", "link", "a/repo"],
                    ["l", "abs", "@/a"],
                    ["l", "loop", "loop"],
                    ["l", "dangling", "nowhere/x"]
                ]),
            );
            let url = cps(&at(&row["u"], &root).unwrap());
            let base = at(&row["b"], &root).map(|b| cps(&b));
            match remote_key(Some(&url)).unwrap() {
                RemoteKey::Hosted(h) => json!(string(&h)),
                key => {
                    let k = local_key(
                        &StdFs,
                        Flavour::Posix,
                        root.as_bytes(),
                        &key,
                        base.as_deref(),
                    );
                    json!(string(&k.unwrap().unwrap()).replace(&root, "@"))
                }
            }
        }
        "kept" => {
            build(&root, &row["t"]);
            let g = &row["g"];
            let found = Git {
                dotgit: at(&g[0], &root).map(String::into_bytes),
                git_dir: at(&g[1], &root).unwrap().into_bytes(),
                common_dir: at(&g[2], &root).unwrap().into_bytes(),
                root: at(&g[3], &root).map(String::into_bytes),
                kind: kind(g[4].as_str().unwrap()),
            };
            let facts = GitFacts {
                release: None,
                reads_compat: None,
            };
            assert_eq!(
                json!(kept_apart(&ctx, &found, &facts).unwrap()),
                row["r"],
                "row {i}: {row}"
            );
            let real = real_git(&StdFs, Flavour::Posix, root.as_bytes(), &found).unwrap();
            let opt = |p: &Option<Vec<u8>>| p.as_ref().map_or(Value::Null, |p| back(p, &root));
            json!([
                opt(&real.dotgit),
                back(&real.git_dir, &root),
                back(&real.common_dir, &root),
                opt(&real.root),
                kind_str(real.kind)
            ])
        }
        "record" => {
            build(&root, &row["t"]);
            let script = Script {
                answers: [pin(&row["first"], &root), pin(&row["second"], &root)],
                put: row["put"].as_bool().unwrap(),
                asked: Cell::new(0),
            };
            let git_dir = format!("{root}/sep.git");
            let tree = format!("{root}/w");
            let r = record_working_tree(&ctx, &StdFs, &script, git_dir.as_bytes(), tree.as_bytes())
                .unwrap();
            assert_eq!(json!(script.asked.get()), row["n"], "row {i}: {row}");
            json!(r.as_str())
        }
        "stamp" => {
            build(
                &root,
                &json!([
                    ["f", "f", "x"],
                    ["l", "l", "f"],
                    ["l", "dangling", "nowhere"]
                ]),
            );
            let path = format!("{root}/{}", row["path"].as_str().unwrap());
            match stamp(&ctx, path.as_bytes()).unwrap() {
                None => Value::Null,
                Some(s) if s == [-1; 5] => json!("unread"),
                Some(_) => json!("stat"),
            }
        }
        other => panic!("unknown kind {other}"),
    };
    let want = if row["k"] == "kept" {
        &row["real"]
    } else {
        &row["r"]
    };
    let _ = std::fs::remove_dir_all(&dir);
    assert_eq!(&got, want, "row {i}: {row}");
}

#[test]
fn location_leaves_posix_every_case() {
    let name = "location_leaves_posix.jsonl";
    let rows = common::jsonl(name);
    let mut ledger = Ledger::new("golden_location_leaves_posix", name);
    for (i, row) in rows.iter().enumerate().skip(1) {
        ledger.case(format!("{name}:{i}"), || check(i, row));
    }
    assert!(rows.len() > 1, "{name} holds no case");
    assert_eq!(rows[0]["cases"].as_u64(), Some(rows.len() as u64 - 1));
    ledger.finish(rows.len() - 1);
}
