//! Every case of `origin_history_cuts.jsonl` against
//! `origin_history::cuts_in`, each tree built in a `MemFs` whose working
//! directory is the tree's root; and the file one byte over `CUTS_MAX`,
//! built here by rule (not stored), whose answer is None.

mod common;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::{Ctx, Failure};
use bettermemory_core::origin_history::{cuts_in, CUTS_MAX};
use common::{assert_count, jsonl};
use serde_json::{json, Value};

const ROOT: &str = "/w";
const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};

fn content(v: &Value) -> Vec<u8> {
    match v {
        Value::String(t) => t.as_bytes().to_vec(),
        Value::Object(_) => {
            let mut out = v["p"].as_str().unwrap().as_bytes().to_vec();
            let x = v["x"].as_str().unwrap().as_bytes();
            for _ in 0..v["n"].as_u64().unwrap() {
                out.extend_from_slice(x);
            }
            out
        }
        other => panic!("content {other}"),
    }
}

fn world(files: &[(String, Vec<u8>)]) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(ROOT.as_bytes(), Node::Dir);
    for (rel, data) in files {
        let parts: Vec<&str> = rel.split('/').collect();
        for i in 1..parts.len() {
            let dir = format!("{ROOT}/{}", parts[..i].join("/"));
            fs.insert(dir.as_bytes(), Node::Dir);
        }
        fs.insert(format!("{ROOT}/{rel}").as_bytes(), Node::File(data.clone()));
    }
    fs
}

fn answer(fs: &MemFs, path: &[u8]) -> Value {
    let ctx = Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: ROOT.as_bytes(),
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    match cuts_in(&rule, path) {
        Ok(None) => Value::Null,
        Ok(Some(set)) => json!(set
            .iter()
            .map(|c| c
                .iter()
                .map(|&u| char::from_u32(u).unwrap())
                .collect::<String>())
            .collect::<Vec<_>>()),
        Err(Failure::Value) => json!({"e": "ValueError"}),
        Err(f) => panic!("failure {f:?}"),
    }
}

#[test]
fn origin_history_cuts_every_case() {
    let lines = jsonl("origin_history_cuts.jsonl");
    let total = lines[0]["cases"].as_u64().unwrap() as usize;
    let mut checked = 0;
    for case in &lines[1..] {
        let files: Vec<(String, Vec<u8>)> = case["t"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(k, v)| (k.clone(), content(v)))
            .collect();
        let fs = world(&files);
        let path = case["p"].as_str().unwrap().as_bytes();
        assert_eq!(answer(&fs, path), case["r"], "case {case}");
        checked += 1;
    }
    assert_eq!(lines.len() - 1, total);
    assert_count("origin_history_cuts.jsonl", checked, total);
}

#[test]
fn one_byte_over_the_cap_is_none() {
    let mut data = b"0123456789abcdef0123456789abcdef01234567".to_vec();
    data.resize(CUTS_MAX + 1, b'0');
    let fs = world(&[
        ("repo/shallow".into(), data),
        ("repo/config".into(), b"[core]\n".to_vec()),
    ]);
    assert_eq!(answer(&fs, b"repo/shallow"), Value::Null);
}
