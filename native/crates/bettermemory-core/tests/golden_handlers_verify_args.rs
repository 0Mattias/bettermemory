//! Every case of `handlers_verify_args.jsonl` (class i) and
//! `handlers_verify_args_unicode.jsonl` (class ii, Unicode) against
//! `handlers::verify::check_verify_args`. The spelling is the generator's
//! (`tests/contract/gen_handlers_verify.py`).

mod common;

use bettermemory_core::handlers::verify::{check_verify_args, Arg};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::{cps, to_string};
use common::parity::Ledger;
use serde_json::{json, Value};

/// A string as the generator spells it: plain, or `{"r": [[unit, n]]}`.
fn text(v: &Value) -> PyStr {
    if let Some(s) = v.as_str() {
        return cps(s);
    }
    let mut out = PyStr::new();
    for part in v["r"].as_array().expect("runs") {
        let unit = cps(part[0].as_str().expect("a unit"));
        for _ in 0..part[1].as_u64().expect("a count") {
            out.extend_from_slice(&unit);
        }
    }
    out
}

fn run(file: &str, test: &str) {
    let lines = common::jsonl(file);
    let count = lines[0]["count"].as_u64().expect("a count") as usize;
    let mut ledger = Ledger::new(test, file);
    for (i, case) in lines[1..].iter().enumerate() {
        let note_v = &case["note"];
        let note_t;
        let note = if note_v.is_null() {
            Arg::Absent
        } else if note_v.get("wrong").is_some() {
            Arg::WrongType
        } else {
            note_t = text(note_v);
            Arg::Given(&note_t[..])
        };
        let held: Vec<Option<Vec<PyStr>>> = case["lists"]
            .as_array()
            .expect("four lists")
            .iter()
            .map(|v| v.as_array().map(|a| a.iter().map(text).collect()))
            .collect();
        let lists: Vec<Arg<&[PyStr]>> = case["lists"]
            .as_array()
            .expect("four lists")
            .iter()
            .zip(&held)
            .map(|(v, h)| match h {
                Some(items) => Arg::Given(&items[..]),
                None if v.is_null() => Arg::Absent,
                None => Arg::WrongType,
            })
            .collect();
        let got = match check_verify_args(note, [lists[0], lists[1], lists[2], lists[3]]) {
            Ok(()) => Value::Null,
            Err(m) => json!(to_string(&m).expect("text")),
        };
        let label = format!("line {}", i + 2);
        assert!(
            !ledger.differs_fatally(&label, got == case["o"]),
            "{label}: got {got}, reference {}",
            case["o"]
        );
    }
    assert_eq!(lines.len() - 1, count);
    assert!(count > 0);
    ledger.finish(count);
}

#[test]
fn golden_handlers_verify_args() {
    run("handlers_verify_args.jsonl", "golden_handlers_verify_args");
}

#[test]
fn golden_handlers_verify_args_unicode() {
    run(
        "handlers_verify_args_unicode.jsonl",
        "golden_handlers_verify_args_unicode",
    );
}
