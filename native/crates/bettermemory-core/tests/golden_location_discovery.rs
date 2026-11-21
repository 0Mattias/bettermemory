//! Every case of `location_discovery.jsonl`, POSIX only: `_discover`
//! (and through it `_from_env` and `_at_dotgit`) over each tree in memory
//! (`fsdata::MemFs`) and on disk (`fsdata::StdFs`), with `githead.probed`
//! replayed from the row and its laziness checked (how many directories
//! the walk took).

#![cfg(unix)]

mod common;

use std::cell::{Cell, RefCell};
use std::os::unix::ffi::OsStrExt;

use bettermemory_core::fsdata::{Flavour, Fs, StdFs};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::{Ctx, Failure};
use bettermemory_core::location_discovery::{discover, Dies, Env, Raised, Refused};
use common::location_git::{disk_tree, mem_tree, unb, Entry};
use common::parity::Ledger;
use serde_json::{json, Value};

const FILE: &str = "location_discovery.jsonl";

fn sub(text: &[u8], base: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < text.len() {
        if text[i..].starts_with(b"<s>") {
            out.extend_from_slice(base);
            i += 3;
        } else {
            out.push(text[i]);
            i += 1;
        }
    }
    out
}

fn rel(path: &[u8], base: &[u8]) -> String {
    let text = String::from_utf8(path.to_vec()).unwrap();
    text.replace(std::str::from_utf8(base).unwrap(), "<s>")
}

fn tree(case: &Value, base: &[u8]) -> Vec<Entry> {
    let mut out = Vec::new();
    for item in case["t"].as_array().unwrap() {
        let path = item[1].as_str().unwrap().as_bytes().to_vec();
        match item[0].as_str().unwrap() {
            "f" => out.push(Entry::File(
                path,
                sub(&unb(item[2].as_str().unwrap()), base),
            )),
            "d" => out.push(Entry::Dir(path)),
            "l" => out.push(Entry::Link(
                path,
                sub(item[2].as_str().unwrap().as_bytes(), base),
            )),
            other => panic!("an item kind {other}"),
        }
    }
    out
}

fn env_of<'a>(case: &'a Value, name: &str) -> Option<&'a str> {
    case.get("env")
        .and_then(|e| e.get(name))
        .and_then(Value::as_str)
}

/// The case's answer, refusals and probed call over `fs`, `base` the
/// case's real folder.
fn answer<F: Fs>(case: &Value, fs: &F, base: &[u8]) -> Value {
    let ctx = Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: base,
    };
    let common = env_of(case, "GIT_COMMON_DIR").map(|v| sub(v.as_bytes(), base));
    let git = GitFacts {
        release: Some((2, 54)),
        reads_compat: Some(true),
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: common.as_deref(),
        git: &git,
    };
    let git_dir = env_of(case, "GIT_DIR").map(|v| sub(v.as_bytes(), base));
    let work = env_of(case, "GIT_WORK_TREE").map(|v| sub(v.as_bytes(), base));
    let env = Env {
        git_dir: git_dir.as_deref(),
        work_tree: work.as_deref(),
    };
    let start = [base, b"/", case["start"].as_str().unwrap().as_bytes()].concat();
    let across = case["ac"].as_bool();
    let called: RefCell<Option<Value>> = RefCell::new(None);
    let taken = Cell::new(0u64);
    let listed: Vec<Vec<u8>> = case
        .get("pr")
        .map(|pr| {
            pr["p"]
                .as_array()
                .unwrap()
                .iter()
                .map(|d| sub(d.as_str().unwrap().as_bytes(), base))
                .collect()
        })
        .unwrap_or_default();
    let probed = |here: &[u8], dev: Option<u64>| {
        *called.borrow_mut() = Some(json!({"here": rel(here, base), "dev": dev.is_some()}));
        Ok(listed.clone().into_iter().map(|d| {
            taken.set(taken.get() + 1);
            Ok(d)
        }))
    };
    let mut refused: Vec<Refused> = Vec::new();
    let list = if case.get("nr").is_some() {
        None
    } else {
        Some(&mut refused)
    };
    let read_head = case.get("rh").is_none();
    let got = discover(&rule, &env, &start, across, probed, list, read_head);
    let a = match got {
        Ok(None) => Value::Null,
        Ok(Some(g)) => json!({
            "dotgit": g.dotgit.map(|p| rel(&p, base)),
            "git_dir": rel(&g.git_dir, base),
            "common": rel(&g.common_dir, base),
            "root": g.root.map(|p| rel(&p, base)),
            "kind": g.kind.as_str(),
        }),
        Err(Raised::Dies(Dies::Discovery)) => {
            json!({"disc": String::from_utf8(Dies::Discovery.text().to_vec()).unwrap()})
        }
        Err(Raised::Dies(Dies::Git(why))) => json!({"die": rel(&why, base)}),
        Err(Raised::Failure(f)) => json!({"e": class_of(&f)}),
    };
    let r: Vec<Value> = refused
        .iter()
        .map(|x| {
            json!([
                rel(&x.path, base),
                rel(&x.git_dir, base),
                rel(&x.why.why, base),
                u8::from(x.why.format_unknown)
            ])
        })
        .collect();
    let pr = called.into_inner().map(|mut c| {
        c["p"] = case["pr"]["p"].clone();
        c["u"] = json!(taken.get());
        c
    });
    json!({"a": a, "r": r, "pr": pr})
}

fn expected(case: &Value) -> Value {
    json!({"a": case["a"], "r": case["r"], "pr": case.get("pr").cloned().unwrap_or(Value::Null)})
}

fn cases() -> Vec<Value> {
    let lines = common::jsonl(FILE);
    assert_eq!(lines[0]["cases"].as_u64(), Some(lines.len() as u64 - 1));
    lines[1..].to_vec()
}

#[test]
fn location_discovery_every_case_in_memory() {
    let cases = cases();
    let ledger = Ledger::new("golden_location_discovery", FILE);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let base = format!("/s{i}").into_bytes();
        let fs = mem_tree(&tree(case, &base), &base);
        let got = answer(case, &fs, &base);
        if got != expected(case) {
            failures.push(format!(
                "#{i} {}: native {got} reference {}",
                case["note"],
                expected(case)
            ));
        }
    }
    ledger.settle(cases.len(), &failures);
}

struct Scratch(std::path::PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[test]
fn location_discovery_every_case_on_disk() {
    let cases = cases();
    let ledger = Ledger::new("golden_location_discovery_stdfs", FILE);
    let dir = std::env::temp_dir().join(format!("bm-locdisc-{}", std::process::id()));
    std::fs::create_dir(&dir).expect("a new scratch directory");
    let scratch = Scratch(std::fs::canonicalize(&dir).unwrap());
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let here = scratch.0.join(i.to_string());
        std::fs::create_dir(&here).unwrap();
        let base = here.as_os_str().as_bytes().to_vec();
        disk_tree(&tree(case, &base), &here);
        let got = answer(case, &StdFs, &base);
        if got != expected(case) {
            failures.push(format!(
                "#{i} {}: native {got} reference {}",
                case["note"],
                expected(case)
            ));
        }
    }
    drop(scratch);
    ledger.settle(cases.len(), &failures);
}

/// `type(e).__name__` of the exception the reference raises where the
/// port answers `f`; a failure with no Python class keeps its Debug text,
/// which no golden row holds.
fn class_of(f: &Failure) -> String {
    match f {
        Failure::Os(e) => e.class.name().to_string(),
        Failure::Value => "ValueError".to_string(),
        Failure::Raised(name) => (*name).to_string(),
        other => format!("{other:?}"),
    }
}
