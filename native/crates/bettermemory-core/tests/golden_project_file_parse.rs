//! Every case of `project_file_parse.jsonl` (not gated) and
//! `project_file_parse_python.jsonl` (Python-gated) against
//! `project_file::parse_stat`: a stat the row describes and the row's
//! bytes, read through a `MemFs`.

mod common;

use bettermemory_core::fsdata::{Flavour, Kind, MemFs, Node, PyErr, StatResult};
use bettermemory_core::gitfile_rule::hostpath::text::Process;
use bettermemory_core::project_file::{parse_stat, Found, Host};
use bettermemory_core::pyvalue::PyStr;
use common::parity::Ledger;
use serde_json::{json, Value};

struct NoCwd;

impl Process for NoCwd {
    fn getcwd(&self) -> Result<PyStr, PyErr> {
        panic!("no working directory is asked")
    }
}

fn s(cps: &[u32]) -> String {
    cps.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

/// The answer as the generator spells it.
pub fn answer(found: &Found) -> Value {
    match found {
        Found::File(f) => json!({"id": s(&f.id), "name": s(&f.name), "b": f.boundary}),
        Found::Error(e) if e.detail.contains(&u32::from(b'\\')) => {
            json!({"r": e.reason.name(), "d_cps": e.detail})
        }
        Found::Error(e) => json!({"r": e.reason.name(), "d": s(&e.detail)}),
    }
}

fn check(r: &Value) {
    let raw: Vec<u8> = match r.get("raw") {
        Some(t) => t.as_str().unwrap().as_bytes().to_vec(),
        None => {
            let x = r["hex"].as_str().unwrap();
            (0..x.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&x[i..i + 2], 16).unwrap())
                .collect()
        }
    };
    let path = format!("/p/{}/bettermemory.toml", r["dir"].as_str().unwrap());
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(path.as_bytes(), Node::File(raw));
    let kind = match r["kind"].as_str().unwrap() {
        "reg" => Kind::Regular,
        "dir" => Kind::Directory,
        _ => Kind::Fifo,
    };
    let st = StatResult {
        kind,
        mode: None,
        ino: None,
        dev: None,
        size: 0,
        mtime_ns: 0,
        ctime_ns: None,
        flags: r.get("flags").map_or(0, |v| v.as_u64().unwrap() as u32),
        file_attributes: r.get("attrs").map_or(0, |v| v.as_u64().unwrap() as u32),
        reparse_tag: Some(0),
    };
    let host = Host {
        fs: &fs,
        flavour: Flavour::Posix,
        process: &NoCwd,
        darwin: r["plat"] == "darwin",
    };
    let cps: PyStr = path.chars().map(u32::from).collect();
    let got = match parse_stat(&host, &cps, &st) {
        Ok(found) => {
            if let Found::File(f) = &found {
                assert_eq!(f.path, cps);
            } else if let Found::Error(e) = &found {
                assert_eq!(e.path, cps);
            }
            answer(&found)
        }
        Err(bettermemory_core::project_file::Fault::Raised(class)) => json!({"x": class}),
        Err(fault) => panic!("{fault:?}"),
    };
    assert_eq!(got, r["o"]);
}

fn run(test: &str, file: &str) {
    let lines = common::jsonl(file);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64().unwrap() as usize, rows.len());
    assert!(!rows.is_empty());
    let mut ledger = Ledger::new(test, file);
    for (i, r) in rows.iter().enumerate() {
        ledger.case(format!("line {}", i + 2), || check(r));
    }
    ledger.finish(rows.len());
}

#[test]
fn every_scripted_stat_parses_as_the_reference() {
    run(
        "every_scripted_stat_parses_as_the_reference",
        "project_file_parse.jsonl",
    );
}

#[test]
fn every_toml_refusal_reads_as_the_reference() {
    run(
        "every_toml_refusal_reads_as_the_reference",
        "project_file_parse_python.jsonl",
    );
}
