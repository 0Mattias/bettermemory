//! Every case of `gitfile_leaves.jsonl` (class i, every platform): the
//! `gitdir:` parse under both flavours, `_headref_content`, the format's
//! reading (`_judge_format`, `object_format`), `path_gone` and
//! `path_missing`, and the bounded reads with the time bound as data.

mod common;

use std::collections::BTreeSet;

use bettermemory_core::fsdata::{Flavour, PyErr};
use bettermemory_core::gitconfig::git_entries;
use bettermemory_core::gitfile_rule::bounded::{bounded_read, bounded_reads, Hung, Job, Read};
use bettermemory_core::gitfile_rule::format::{judge_format, object_format, GitFacts};
use bettermemory_core::gitfile_rule::probe::{path_gone, path_missing, Errno, Raised};
use bettermemory_core::gitfile_rule::{headref_content, parse_gitfile};
use common::gitconfig_file::content;
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const FILE: &str = "gitfile_leaves.jsonl";

/// `_b`: printable ASCII without a backslash as `=` and the text, else hex.
fn b(data: &[u8]) -> String {
    if data
        .iter()
        .all(|&c| (0x20..0x7f).contains(&c) && c != b'\\')
    {
        return format!("={}", std::str::from_utf8(data).expect("ASCII"));
    }
    common::hex(data)
}

/// `_path_out`: a path's bytes as `b`, or a digest past 256 bytes.
fn path_out(data: Option<Vec<u8>>) -> Value {
    match data {
        None => Value::Null,
        Some(data) if data.len() > 256 => {
            let digest: String = Sha256::digest(&data)
                .iter()
                .map(|x| format!("{x:02x}"))
                .collect();
            json!({"n": data.len(), "sha256": digest})
        }
        Some(data) => json!(b(&data)),
    }
}

fn parse_case(case: &Value) -> Value {
    let data = content(&case["d"]);
    let mut out = case.clone();
    for (key, flavour) in [("px", Flavour::Posix), ("nt", Flavour::Windows)] {
        if case.get(key).is_none() {
            continue;
        }
        let dotgit = content(&case["g"][key]);
        out[key] = path_out(parse_gitfile(&data, &dotgit, flavour));
    }
    out
}

fn format_case(case: &Value) -> Value {
    let data = content(&case["d"]);
    let git = GitFacts {
        release: case["gv"]
            .as_array()
            .map(|v| (v[0].as_u64().unwrap() as u32, v[1].as_u64().unwrap() as u32)),
        reads_compat: case["rc"].as_bool(),
    };
    let mut out = case.clone();
    out["of"] = object_format(&data, &git).map_or(Value::Null, |f| json!(b(&f)));
    let parsed = if data.len() <= bettermemory_core::gitconfig::FORMAT_LIMIT {
        git_entries(&data).ok()
    } else {
        None
    };
    match parsed {
        None => {
            out.as_object_mut().unwrap().remove("j");
        }
        Some(entries) => {
            out["j"] = match judge_format(b"config", &entries, &git) {
                Err(why) => json!({"why": String::from_utf8(why).expect("UTF-8")}),
                Ok(f) => {
                    let mut taken = Vec::new();
                    if let Some(bare) = &f.bare {
                        taken.push(json!([b(b"core.bare"), bare.as_deref().map(b)]));
                    }
                    if let Some(tree) = &f.worktree {
                        taken.push(json!([b(b"core.worktree"), b(tree)]));
                    }
                    json!({"v": f.version, "wc": f.worktree_config, "t": taken,
                           "h": b(&f.object_format)})
                }
            };
        }
    }
    out
}

fn raised(case: &Value) -> Raised {
    match case["e"].as_str().unwrap() {
        "value" => Raised::Value,
        "other" => Raised::Other,
        _ => Raised::Os {
            class: match case["c"].as_str().unwrap() {
                "FileNotFoundError" => PyErr::FileNotFound,
                "NotADirectoryError" => PyErr::NotADirectory,
                "PermissionError" => PyErr::Permission,
                "IsADirectoryError" => PyErr::IsADirectory,
                _ => PyErr::Os,
            },
            errno: match case["n"].as_str().unwrap() {
                "ENOENT" => Errno::Enoent,
                "ENOTDIR" => Errno::Enotdir,
                "ELOOP" => Errno::Eloop,
                "ENAMETOOLONG" => Errno::Enametoolong,
                _ => Errno::Other,
            },
            winerror: case.get("w").and_then(Value::as_i64),
        },
    }
}

fn probe_case(case: &Value) -> Value {
    let exc = raised(case);
    let mut out = case.clone();
    out["gone"] = json!(path_gone(exc));
    out["missing"] = json!(path_missing(exc));
    out
}

fn shown(read: &Read<Value>) -> Value {
    match read {
        Read::Value(v) => json!({"v": v}),
        Read::Unanswered => json!("U"),
        Read::NotRead => json!("N"),
    }
}

fn bounded_case(case: &Value) -> Value {
    let hung = Hung(
        case["hung"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| p.as_str().unwrap().as_bytes().to_vec())
            .collect::<BTreeSet<_>>(),
    );
    let jobs = || -> Vec<Job<'static, Value, &'static str>> {
        case["jobs"]
            .as_array()
            .unwrap()
            .iter()
            .map(|job| {
                let path = job[0].as_str().unwrap().as_bytes().to_vec();
                let outcome = job[2].clone();
                let read: Box<dyn FnOnce() -> Result<Value, &'static str>> =
                    Box::new(move || match outcome.get("v") {
                        Some(v) => Ok(v.clone()),
                        None => Err("OSError"),
                    });
                (path, read)
            })
            .collect()
    };
    let mut out = case.clone();
    if case.get("one").is_some() {
        let (path, read) = jobs().into_iter().next().unwrap();
        out["a"] = match bounded_read(&hung, &path, read) {
            Ok(read) => shown(&read),
            Err(class) => json!({"raised": class}),
        };
        return out;
    }
    let all = |raises| match bounded_reads(&hung, jobs(), raises) {
        Ok(reads) => Value::Array(reads.iter().map(shown).collect()),
        Err(class) => json!({"raised": class}),
    };
    out["a"] = all(false);
    out["raises"] = all(true);
    out
}

#[test]
fn gitfile_leaves_every_case() {
    let lines = jsonl(FILE);
    let cases = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(cases.len() as u64));
    let ledger = Ledger::new("golden_gitfile_leaves", FILE);
    let mut failures = Vec::new();
    let mut kinds = BTreeSet::new();
    for (i, case) in cases.iter().enumerate() {
        let kind = case["k"].as_str().unwrap();
        kinds.insert(kind.to_owned());
        let got = match kind {
            "pf" => parse_case(case),
            "hr" => {
                let mut out = case.clone();
                out["a"] = json!(headref_content(&content(&case["d"])));
                out
            }
            "jf" => format_case(case),
            "pg" => probe_case(case),
            "br" => bounded_case(case),
            other => panic!("a case of kind {other}"),
        };
        if &got != case {
            let mut shown = case.to_string();
            shown.truncate(400);
            failures.push(format!("#{i} {shown}: native {got}"));
        }
    }
    assert_eq!(kinds.len(), 5, "every kind of case is present");
    ledger.settle(cases.len(), &failures);
}
