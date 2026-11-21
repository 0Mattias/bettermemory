//! Every scenario of `origin_gitfiles_root_posix.jsonl` (class i, POSIX
//! only), replayed on a scratch tree through `StdFs`: `config_facts`,
//! `worktree_config_facts`, `config_moves_worktree`,
//! `refused_worktree_root` and the memo's keys. The git on PATH is
//! release 2.54, as the generator patches it. A run as root, which reads a
//! mode-000 file, skips the comparison and records it as a gap of class
//! os.

#![cfg(unix)]

mod common;

use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::githead::discovery::{locate, Env};
use bettermemory_core::origin_gitfiles::facts::ConfigFacts;
use bettermemory_core::origin_gitfiles::root::{
    config_facts, config_moves_worktree, refused_worktree_root, worktree_config_facts, ConfigMemo,
    ConfigRead,
};
use common::parity::Ledger;
use serde_json::{json, Value};

const FILE: &str = "origin_gitfiles_root_posix.jsonl";
/// The generator's `PAUSE`.
const PAUSE: Duration = Duration::from_millis(50);
const DARWIN: bool = cfg!(target_os = "macos");
const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};

struct Scratch(PathBuf);

impl Scratch {
    fn new(n: usize) -> Scratch {
        let dir = std::env::temp_dir().join(format!("ogf-root-{}-{n}", std::process::id()));
        std::fs::create_dir(&dir).expect("a new scratch directory");
        Scratch(std::fs::canonicalize(&dir).expect("a resolved scratch directory"))
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn running_as_root(base: &Path) -> bool {
    let probe = base.join("probe");
    std::fs::write(&probe, b"x").unwrap();
    std::fs::set_permissions(&probe, std::fs::Permissions::from_mode(0o000)).unwrap();
    let seen = std::fs::read(&probe).is_ok();
    std::fs::remove_file(&probe).unwrap();
    seen
}

fn spell(facts: &ConfigFacts) -> Value {
    let names = facts
        .names
        .as_ref()
        .map(|names| names.iter().map(|n| common::hex(n)).collect::<Vec<_>>());
    json!([
        names,
        facts.attributes_elsewhere,
        facts.bare_moves,
        facts.git_dies,
        facts.includes
    ])
}

fn read(read: Option<ConfigRead>) -> Value {
    match read {
        None => Value::Null,
        Some((stamp, facts)) => json!([stamp.is_none(), spell(&facts)]),
    }
}

fn text(path: &[u8], real: &str) -> String {
    String::from_utf8(path.to_vec())
        .expect("a UTF-8 path")
        .replace(real, "<root>")
}

fn step(step: &[Value], root: &Path, memo: &mut ConfigMemo) -> Value {
    let kind = step[0].as_str().expect("a step");
    let real = root.to_str().expect("a UTF-8 scratch path");
    let at = || root.join(step[1].as_str().expect("a path"));
    if matches!(
        kind,
        "dir" | "write" | "big" | "link" | "fifo" | "chmod" | "rm"
    ) {
        std::thread::sleep(PAUSE);
    }
    match kind {
        "dir" => std::fs::create_dir_all(at()).unwrap(),
        "write" => std::fs::write(at(), step[2].as_str().unwrap().replace("<root>", real)).unwrap(),
        "big" => {
            let head = b"[core]\n#";
            let mut data = head.to_vec();
            data.resize(step[2].as_u64().unwrap() as usize, b'x');
            std::fs::write(at(), data).unwrap();
        }
        "link" => std::os::unix::fs::symlink(step[2].as_str().unwrap(), at()).unwrap(),
        "fifo" => {
            let status = std::process::Command::new("mkfifo")
                .arg(at())
                .status()
                .expect("mkfifo");
            assert!(status.success());
        }
        "chmod" => {
            let mode = step[2].as_u64().unwrap() as u32;
            std::fs::set_permissions(at(), std::fs::Permissions::from_mode(mode)).unwrap();
        }
        "rm" => std::fs::remove_file(at()).unwrap(),
        "memo" => {
            return json!(memo
                .keys()
                .iter()
                .map(|k| text(k, real))
                .collect::<Vec<_>>())
        }
        _ => return query(kind, &at(), real, memo),
    }
    Value::Null
}

fn query(kind: &str, at: &Path, real: &str, memo: &mut ConfigMemo) -> Value {
    let at = at.to_str().unwrap().as_bytes();
    let ctx = Ctx {
        fs: &StdFs,
        flavour: Flavour::Posix,
        cwd: real.as_bytes(),
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    let env = Env::default();
    match kind {
        "facts" => read(config_facts(memo, &ctx, at, &GIT).expect("ported")),
        "root" => {
            let (found, indeterminate) =
                refused_worktree_root(memo, &rule, &env, at, DARWIN).expect("ported");
            json!([found.map(|f| text(&f, real)), indeterminate])
        }
        _ => {
            let Some(found) = locate(&rule, &env, at, true, false) else {
                return Value::Null;
            };
            match kind {
                "moves" => {
                    json!(config_moves_worktree(memo, &ctx, &found.gd, &GIT).expect("ported"))
                }
                "wtc" => {
                    let mut common_path = found.gd.commondir.clone();
                    common_path.extend_from_slice(b"/config");
                    let Some((_, common)) =
                        config_facts(memo, &ctx, &common_path, &GIT).expect("ported")
                    else {
                        return json!("common unread");
                    };
                    read(
                        worktree_config_facts(memo, &ctx, &found.gd, &common, &GIT)
                            .expect("ported"),
                    )
                }
                other => panic!("no step {other}"),
            }
        }
    }
}

#[test]
fn origin_gitfiles_root_posix_every_case() {
    let lines = common::jsonl(FILE);
    let header = &lines[0];
    let ledger = Ledger::new("golden_origin_gitfiles_root_posix", FILE);
    let base = Scratch::new(usize::MAX);
    let as_root = running_as_root(&base.0);
    let mut failures = Vec::new();
    let mut count = 0u64;
    for (i, row) in lines[1..].iter().enumerate() {
        let row = row.as_array().expect("a row");
        assert_eq!(row[0], "scenario");
        count += 1;
        if as_root {
            continue;
        }
        let scratch = Scratch::new(i);
        let mut memo = ConfigMemo::new();
        let got: Vec<Value> = row[1]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| step(s.as_array().unwrap(), &scratch.0, &mut memo))
            .collect();
        let got = json!(got);
        if got != row[2] {
            failures.push(format!("#{i}: native {got} reference {}", row[2]));
        }
    }
    assert_eq!(header["scenarios"].as_u64(), Some(count));
    assert!(count > 0, "no case");
    ledger.settle(if as_root { 0 } else { count as usize }, &failures);
    if as_root {
        common::parity::record(
            "golden_origin_gitfiles_root_posix",
            FILE,
            "os",
            0,
            &[format!("{count} scenarios")],
            &["a run as root reads a mode-000 file; the scenarios are not compared".to_owned()],
        );
    }
}
