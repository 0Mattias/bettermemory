//! Every case of `location_view.jsonl`: `continuity` row by row and over
//! the digest of its cross product, `_same_number`, and the path
//! `_carries_project_file` parses.

mod common;

use bettermemory_core::capture::Key;
use bettermemory_core::fsdata::Flavour;
use bettermemory_core::location_view::{carries_project_file, same_number};
use bettermemory_core::pyvalue::PyValue;
use common::location_view::{answer, cross, line, parse};
use common::parity::Ledger;
use sha2::{Digest, Sha256};

const FILE: &str = "location_view.jsonl";

fn rows() -> Vec<PyValue> {
    let text = std::fs::read_to_string(common::golden_path(FILE)).expect("the golden file");
    let mut lines: Vec<PyValue> = text.lines().map(parse).collect();
    let header = lines.remove(0);
    let PyValue::Dict(h) = &header else {
        panic!("a header")
    };
    let cases = h
        .iter()
        .find(|(k, _)| *k == PyValue::Str("cases".chars().map(u32::from).collect()))
        .map(|(_, v)| v.clone());
    assert_eq!(cases, Some(PyValue::Int(lines.len().to_string())));
    lines
}

fn list(v: &PyValue) -> &[PyValue] {
    match v {
        PyValue::List(x) => x,
        other => panic!("a list, not {other:?}"),
    }
}

fn s(v: &PyValue) -> String {
    match v {
        PyValue::Str(x) => x.iter().map(|&c| char::from_u32(c).unwrap()).collect(),
        other => panic!("a text, not {other:?}"),
    }
}

fn key(v: &PyValue) -> Option<Key> {
    match v {
        PyValue::None => None,
        PyValue::List(x) => Some((
            s(&x[0]).chars().map(u32::from).collect(),
            s(&x[1]).chars().map(u32::from).collect(),
            match &x[2] {
                PyValue::None => None,
                b => Some(s(b).chars().map(u32::from).collect()),
            },
        )),
        other => panic!("a key, not {other:?}"),
    }
}

fn expected_line(v: &PyValue) -> String {
    let a = list(v);
    match &a[0] {
        PyValue::Bool(ok) => format!("[{ok},\"{}\",\"{}\"]", s(&a[1]), s(&a[2])),
        _ => format!("[\"err\",\"TypeError\",\"{}\"]", s(&a[2])),
    }
}

#[test]
fn location_view_every_case() {
    let rows = rows();
    let ledger = Ledger::new("golden_location_view", FILE);
    let mut failures = Vec::new();
    let mut checked = 0;
    for (i, row) in rows.iter().enumerate() {
        let r = list(row);
        let got_want = match s(&r[0]).as_str() {
            "c" => (line(&answer(list(&r[1]))), expected_line(&r[2])),
            "cx" => {
                let mut digest = Sha256::new();
                let all = cross();
                for args in &all {
                    digest.update(line(&answer(args)).as_bytes());
                    digest.update(b"\n");
                }
                let hex: String = digest
                    .finalize()
                    .iter()
                    .map(|b| format!("{b:02x}"))
                    .collect();
                (
                    format!("{} {hex}", all.len()),
                    format!(
                        "{} {}",
                        match &r[1] {
                            PyValue::Int(n) => n.clone(),
                            _ => panic!(),
                        },
                        s(&r[2])
                    ),
                )
            }
            "same" => (
                same_number(key(&r[1]).as_ref(), key(&r[2]).as_ref()).to_string(),
                matches!(r[3], PyValue::Bool(true)).to_string(),
            ),
            "pf" => {
                let flavour = if s(&r[1]) == "posix" {
                    Flavour::Posix
                } else {
                    Flavour::Windows
                };
                let mut asked = Vec::new();
                carries_project_file(flavour, s(&r[2]).as_bytes(), &mut |p| {
                    asked = p.to_vec();
                    Ok(true)
                })
                .expect("a path");
                (String::from_utf8(asked).unwrap(), s(&r[3]))
            }
            other => panic!("a row of kind {other}"),
        };
        checked += 1;
        if got_want.0 != got_want.1 {
            failures.push(format!(
                "#{i}: native {} reference {}",
                got_want.0, got_want.1
            ));
        }
    }
    assert!(checked > 0);
    assert_eq!(checked, rows.len());
    ledger.settle(checked, &failures);
}
