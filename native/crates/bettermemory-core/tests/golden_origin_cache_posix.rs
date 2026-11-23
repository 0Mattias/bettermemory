//! Compare the origin cache signature on real POSIX repository files.
#![cfg(unix)]
mod common;
use std::os::unix::fs::{symlink, MetadataExt};
use std::path::Path;

use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::gitconfig::legacy::{Files, Stamp};
use bettermemory_core::gitconfig::paths::Machine;
use bettermemory_core::gitconfig::remotes::{self, Inputs, Sources};
use bettermemory_core::gitconfig::stamped::{machine_config_reads, StampedReads};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::githead::discovery::{Env, Host};
use bettermemory_core::githead::signature::First;
use bettermemory_core::origin_cache::{capture_signature, CaptureSignature, Config, Slot};
use common::parity::Ledger;
use serde_json::{json, Value};

const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};

fn first<T: PartialEq>(value: T, past: &mut Vec<T>) -> usize {
    if let Some(i) = past.iter().position(|v| v == &value) {
        return i;
    }
    past.push(value);
    past.len() - 1
}

fn path(raw: &[u8], root: &str) -> String {
    std::str::from_utf8(raw).unwrap().replace(root, "<root>")
}

fn fields(sig: &CaptureSignature, root: &str, stamps: &mut Vec<Stamp>) -> Value {
    let tag = match &sig.base.first {
        First::Outside => Value::Null,
        First::GitDir(d) => json!(path(d, root)),
        First::Undetermined(_) => json!("unknown"),
    };
    let st = |v: Option<Stamp>, seen: &mut Vec<Stamp>| v.map(|s| first(s, seen));
    let files = |f: &Files, seen: &mut Vec<Stamp>| {
        f.iter()
            .map(|(n, s)| json!([path(n, root), st(*s, seen)]))
            .collect::<Vec<_>>()
    };
    let extra = match &sig.extra {
        None => Value::Null,
        Some(Slot::Unknown(_)) => json!("unknown"),
        Some(Slot::Value(e)) => {
            let mut machine = vec![json!(e
                .machine
                .named
                .iter()
                .map(|n| path(n, root))
                .collect::<Vec<_>>())];
            for f in &e.machine.files {
                machine.push(json!(files(f, stamps)));
            }
            let repository = e
                .repository
                .iter()
                .map(|f| files(f, stamps))
                .collect::<Vec<_>>();
            let legacy = e.legacy.map(|s| st(s, stamps));
            let spelling = match &e.spelled_root {
                Slot::Unknown(_) => "unknown".to_owned(),
                Slot::Value(n) => path(n, root),
            };
            let refs = e.refs.iter().map(|s| st(*s, stamps)).collect::<Vec<_>>();
            json!([machine, repository, legacy, spelling, refs])
        }
    };
    json!([tag, extra])
}

fn scenario(steps: &Value, root: &Path) -> Vec<Value> {
    let root = root.to_str().unwrap();
    let ctx = Ctx {
        fs: &StdFs,
        flavour: Flavour::Posix,
        cwd: root.as_bytes(),
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    let host = Host {
        euid: std::fs::metadata(root).unwrap().uid(),
        owners: &StdFs,
    };
    let machine = Machine {
        flavour: Flavour::Posix,
        home: Some(root.as_bytes().into()),
        profile: None,
        user_home: &|_| None,
    };
    let inputs = Inputs {
        machine: &machine,
        env: remotes::Env {
            git_config_nosystem: Some(b"1".to_vec()),
            git_config_global: Some(format!("{root}/machine").into_bytes()),
            ..Default::default()
        },
        sources: Sources::default(),
        git: GIT,
        cwd: root.as_bytes().into(),
        asked_branch: None,
    };
    let mut reads = StampedReads::default();
    let mut signatures = Vec::new();
    let mut stamps = Vec::new();
    steps
        .as_array()
        .unwrap()
        .iter()
        .map(|step| {
            let at = Path::new(root).join(step[1].as_str().unwrap());
            let arg = || step[2].as_str().unwrap().replace("<root>", root);
            match step[0].as_str().unwrap() {
                "prime" => {
                    machine_config_reads(
                        &mut reads,
                        &StdFs,
                        &inputs,
                        at.as_os_str().as_encoded_bytes(),
                        &mut || 0.,
                    )
                    .unwrap();
                }
                "sign" => {
                    let sig = capture_signature(
                        &rule,
                        &host,
                        &Env::default(),
                        at.as_os_str().as_encoded_bytes(),
                        Config {
                            reads: &mut reads,
                            inputs: &inputs,
                            reuse: step.get(2).and_then(Value::as_bool).unwrap_or(false),
                            now: 0.,
                            darwin: cfg!(target_os = "macos"),
                        },
                    )
                    .unwrap();
                    let view = fields(&sig, root, &mut stamps);
                    return json!([first(sig, &mut signatures), view[0], view[1]]);
                }
                "dir" => std::fs::create_dir_all(at).unwrap(),
                "rename" => std::fs::rename(at, Path::new(root).join(arg())).unwrap(),
                "rm" => std::fs::remove_file(at).unwrap(),
                op => {
                    std::fs::create_dir_all(at.parent().unwrap()).unwrap();
                    match op {
                        "link" => symlink(arg(), at).unwrap(),
                        "write" => std::fs::write(at, arg()).unwrap(),
                        _ => panic!("unknown step {op}"),
                    }
                }
            }
            Value::Null
        })
        .collect()
}

#[test]
fn repository_signatures() {
    let file = "origin_cache_posix.jsonl";
    let rows = common::jsonl(file);
    let mut ledger = Ledger::new("golden_origin_cache_posix", file);
    for (i, row) in rows[1..].iter().enumerate() {
        ledger.case(i, || {
            let dir = common::origin_history_located::Scratch::new("origin-cache");
            let root = dir.0.canonicalize().unwrap();
            let got = scenario(&row[0], &root);
            for (step, (actual, want)) in got.iter().zip(row[1].as_array().unwrap()).enumerate() {
                assert_eq!(actual, want, "case {i} step {step}");
            }
            assert_eq!(got.len(), row[1].as_array().unwrap().len());
        });
    }
    ledger.finish(rows[0]["rows"].as_u64().unwrap() as usize);
}
