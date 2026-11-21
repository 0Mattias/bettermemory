//! Every case of `project_file.jsonl` (not gated) against `project_file`:
//! the error's line, `render` and `mint_id`.

mod common;

use bettermemory_core::project_file::{
    bad_char, id_text, mint_id, render, ErrorReason, ProjectFile, ProjectFileError,
};
use bettermemory_core::pyvalue::PyStr;
use common::parity::Ledger;
use serde_json::{json, Value};

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn check_line(r: &Value) {
    let e = ProjectFileError {
        path: cps(r["path"].as_str().unwrap()),
        reason: ErrorReason::Toml,
        detail: cps(r["d"].as_str().unwrap()),
    };
    assert_eq!(json!(e.line()), r["o"]);
}

fn check_render(r: &Value) {
    let name: PyStr = r["name"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c.as_u64().unwrap() as u32)
        .collect();
    let project = ProjectFile {
        id: cps("bmp_0123456789ABCDEFGHJKMNPQRS"),
        name,
        boundary: r["b"].as_bool().unwrap(),
        path: cps("/x"),
    };
    let got = render(&project).map(|b| String::from_utf8(b).unwrap());
    assert_eq!(json!(got), r["o"]);
}

fn check_mint(r: &Value) {
    let x = r["x"].as_str().unwrap();
    let raw: Vec<u8> = (0..x.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&x[i..i + 2], 16).unwrap())
        .collect();
    assert_eq!(json!(mint_id(&raw).unwrap()), r["o"]);
}

fn check_id(r: &Value) {
    assert_eq!(json!(id_text(&cps(r["t"].as_str().unwrap()))), r["o"]);
}

fn check_bad(r: &Value) {
    let bad: Vec<u32> = (0..0x110000).filter(|&c| bad_char(c)).collect();
    assert_eq!(json!(bad), r["o"]);
}

fn run(test: &str, file: &str) {
    let lines = common::jsonl(file);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64().unwrap() as usize, rows.len());
    assert!(!rows.is_empty());
    let mut ledger = Ledger::new(test, file);
    for (i, r) in rows.iter().enumerate() {
        ledger.case(format!("line {} {}", i + 2, r["k"]), || {
            match r["k"].as_str() {
                Some("line") => check_line(r),
                Some("render") => check_render(r),
                Some("mint") => check_mint(r),
                Some("id") => check_id(r),
                Some("bad") => check_bad(r),
                other => panic!("unknown row kind {other:?}"),
            }
        });
    }
    ledger.finish(rows.len());
}

#[test]
fn project_file_every_case() {
    run("project_file_every_case", "project_file.jsonl");
}
