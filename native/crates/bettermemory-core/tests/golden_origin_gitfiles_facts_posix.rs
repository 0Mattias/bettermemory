//! Every case of `origin_gitfiles_facts_posix.jsonl` (class i, POSIX
//! only): `_global_attributes_path` under each environment, and each
//! `_hold_directory` and `directories_held` scenario replayed on a scratch
//! tree through `StdFs`. A run as root, which reads a mode-000 folder,
//! skips the scenarios and records them as a gap of class os.

#![cfg(unix)]

mod common;

use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::origin_gitfiles::facts::{
    directories_held, global_attributes_path, hold_directory, Held, HoldFault,
};
use common::parity::Ledger;
use serde_json::{json, Value};

const FILE: &str = "origin_gitfiles_facts_posix.jsonl";
/// The generator's `PAUSE`.
const PAUSE: Duration = Duration::from_millis(50);

fn opt(v: &Value) -> Option<Vec<u8>> {
    v.as_str().map(|s| s.as_bytes().to_vec())
}

struct Scratch(PathBuf);

impl Scratch {
    fn new(n: usize) -> Scratch {
        let dir = std::env::temp_dir().join(format!("ogf-facts-{}-{n}", std::process::id()));
        std::fs::create_dir(&dir).expect("a new scratch directory");
        Scratch(std::fs::canonicalize(&dir).expect("a resolved scratch directory"))
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let locked = self.0.join("locked");
        let _ = std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o700));
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn running_as_root(base: &Path) -> bool {
    let probe = base.join("probe");
    std::fs::create_dir_all(probe.join("in")).unwrap();
    std::fs::set_permissions(&probe, std::fs::Permissions::from_mode(0o000)).unwrap();
    let seen = std::fs::metadata(probe.join("in")).is_ok();
    std::fs::set_permissions(&probe, std::fs::Permissions::from_mode(0o700)).unwrap();
    std::fs::remove_dir_all(&probe).unwrap();
    seen
}

fn step(step: &[Value], root: &Path, held: &mut Held) -> Value {
    let kind = step[0].as_str().expect("a step");
    let at = |i: usize| root.join(step[i].as_str().expect("a path"));
    let real = root.to_str().expect("a UTF-8 scratch path");
    let path = |i: usize| step[i].as_str().expect("a path").replace("<root>", real);
    if matches!(
        kind,
        "mkdir" | "write" | "rm" | "rmdir" | "link" | "rename" | "chmod"
    ) {
        std::thread::sleep(PAUSE);
    }
    match kind {
        "mkdir" => std::fs::create_dir(at(1)).unwrap(),
        "write" => std::fs::write(at(1), step[2].as_str().unwrap()).unwrap(),
        "rm" => std::fs::remove_file(at(1)).unwrap(),
        "rmdir" => std::fs::remove_dir(at(1)).unwrap(),
        "link" => std::os::unix::fs::symlink(step[2].as_str().unwrap(), at(1)).unwrap(),
        "rename" => std::fs::rename(at(1), at(2)).unwrap(),
        "chmod" => {
            let mode = step[2].as_u64().unwrap() as u32;
            std::fs::set_permissions(at(1), std::fs::Permissions::from_mode(mode)).unwrap();
        }
        "fake" => {
            held.insert(path(1).into_bytes(), (0, 0));
        }
        "hold" => {
            return match hold_directory(&StdFs, Flavour::Posix, path(1).as_bytes(), held) {
                Ok(()) => json!(held
                    .keys()
                    .map(|k| String::from_utf8_lossy(k).replace(real, "<root>"))
                    .collect::<Vec<_>>()),
                Err(HoldFault::Raised(e)) => json!({"e": e.class.name()}),
                Err(HoldFault::NotPorted) => json!("not ported"),
            };
        }
        "held" => return json!(directories_held(&StdFs, held)),
        other => panic!("no step {other}"),
    }
    Value::Null
}

#[test]
fn origin_gitfiles_facts_posix_every_case() {
    let lines = common::jsonl(FILE);
    let header = &lines[0];
    let ledger = Ledger::new("golden_origin_gitfiles_facts_posix", FILE);
    let base = Scratch::new(usize::MAX);
    let as_root = running_as_root(&base.0);
    let mut failures = Vec::new();
    let (mut gaps, mut holds) = (0u64, 0u64);
    for (i, row) in lines[1..].iter().enumerate() {
        let row = row.as_array().expect("a row");
        let (got, want) = match row[0].as_str().expect("a kind") {
            "gap" => {
                gaps += 1;
                let got = global_attributes_path(
                    Flavour::Posix,
                    opt(&row[1]).as_deref(),
                    opt(&row[2]).as_deref(),
                    row[3].as_str().unwrap().as_bytes(),
                );
                (
                    got.map_or(Value::Null, |g| json!(String::from_utf8(g).unwrap())),
                    row[4].clone(),
                )
            }
            "hold" => {
                holds += 1;
                if as_root {
                    continue;
                }
                let scratch = Scratch::new(i);
                let mut held = Held::new();
                let got: Vec<Value> = row[1]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|s| step(s.as_array().unwrap(), &scratch.0, &mut held))
                    .collect();
                (json!(got), row[2].clone())
            }
            other => panic!("no kind {other}"),
        };
        if got != want {
            failures.push(format!("#{i}: native {got} reference {want}"));
        }
    }
    assert_eq!(header["gap"].as_u64(), Some(gaps));
    assert_eq!(header["hold"].as_u64(), Some(holds));
    assert!(gaps > 0 && holds > 0, "no case");
    let total = if as_root { gaps } else { gaps + holds };
    ledger.settle(total as usize, &failures);
    if as_root {
        common::parity::record(
            "golden_origin_gitfiles_facts_posix",
            FILE,
            "os",
            0,
            &[format!("{holds} hold scenarios")],
            &[
                "a run as root reads a mode-000 folder; the hold scenarios are not compared"
                    .to_owned(),
            ],
        );
    }
}
