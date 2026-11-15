//! Every case of `log_keyring.jsonl`: a small chain, a key ring given as
//! the files of a keys directory (head and origin files included), and
//! the `verify_chain` report the reference gave, or the exception it
//! raised. Seqs at the ends of i64 are among them.

mod common;

use bettermemory_core::log::{self, ChainReport, Key, KeyRing, LogRow, ReportDict, ReportValue};
use bettermemory_core::unicode;
use common::{assert_count, jsonl, text_field, unhex};
use serde_json::{json, Map, Value};

/// The report's JSON as the fixture spells it: an int outside i64 as
/// `{"int": decimal}`, a str holding a lone surrogate as `{"cps": [...]}`.
fn report_value(v: &ReportValue) -> Value {
    match v {
        ReportValue::Null => Value::Null,
        ReportValue::Int(i) => json!(i),
        ReportValue::BigInt(text) => json!({ "int": text }),
        ReportValue::Str(s) => match unicode::to_string(s) {
            Some(text) => json!(text),
            None => json!({ "cps": s }),
        },
    }
}

fn dict(d: &ReportDict) -> Value {
    let mut m = Map::new();
    for (k, v) in d {
        assert!(
            m.insert((*k).to_owned(), report_value(v)).is_none(),
            "repeated key {k}"
        );
    }
    Value::Object(m)
}

pub fn report_json(r: &ChainReport) -> Value {
    json!({
        "status": r.status,
        "rows": r.rows,
        "segments": r.segments.iter().map(dict).collect::<Vec<_>>(),
        "problems": r.problems.iter().map(dict).collect::<Vec<_>>(),
        "head": dict(&r.head),
        "origin": dict(&r.origin),
    })
}

fn row_of(record: &Value) -> LogRow {
    let s = |k: &str| record[k].as_str().expect(k).to_owned();
    LogRow {
        seq: record["seq"].as_i64().expect("seq"),
        ts: s("ts"),
        session: record["session"].as_str().map(str::to_owned),
        kind: s("kind"),
        payload: unicode::to_string(&text_field(record, "payload")).expect("UTF-8 payload"),
        prev_mac: s("prev_mac"),
        mac: s("mac"),
    }
}

fn file(value: &Value) -> Option<Vec<u8>> {
    value.as_str().map(unhex)
}

/// The keys directory of a case, in memory.
fn ring_of(record: &Value) -> KeyRing {
    let mut ring = KeyRing {
        current: file(&record["current"]).map(Key::new),
        head_file: file(&record["head"]),
        origin_file: file(&record["origin"]),
        ..KeyRing::default()
    };
    for entry in record["retired"].as_array().expect("retired") {
        let name = entry[0].as_str().expect("a file name part").to_owned();
        let key = unhex(entry[1].as_str().expect("key hex"));
        assert!(
            ring.retired.insert(name, Key::new(key)).is_none(),
            "one file per name"
        );
    }
    ring
}

#[test]
fn log_keyring_every_case() {
    let lines = jsonl("log_keyring.jsonl");
    let header = &lines[0];
    assert_eq!(header["fixture"], "log_keyring");
    let keys = &header["keys"];
    for name in ["A", "B", "C"] {
        let key = unhex(keys[name].as_str().unwrap());
        assert_eq!(log::fingerprint(&key), keys[format!("{name}_fingerprint")]);
    }
    let mut failures = Vec::new();
    let mut checked = 0;
    let mut groups = std::collections::BTreeMap::<String, usize>::new();
    for record in &lines[1..] {
        let name = record["case"].as_str().expect("case");
        // `iter_rows`: the rows from seq 1 on, in seq order.
        let mut rows: Vec<LogRow> = record["rows"]
            .as_array()
            .expect("rows")
            .iter()
            .map(row_of)
            .filter(|r| r.seq > 0)
            .collect();
        rows.sort_by_key(|r| r.seq);
        let ring = ring_of(&record["ring"]);
        let current = record["current_fingerprint"].as_str().expect("current");
        let got = match log::verify_chain(&rows, &ring, current) {
            Ok(report) => json!({ "report": report_json(&report) }),
            Err(e) => json!({ "error": e.name() }),
        };
        let expected = match (record.get("report"), record.get("error")) {
            (Some(report), None) => json!({ "report": report }),
            (None, Some(error)) => json!({ "error": error }),
            _ => panic!("{name}: a report or an error"),
        };
        if got != expected {
            failures.push(format!("{name}:\n  expected {expected}\n  got      {got}"));
        }
        *groups
            .entry(record["group"].as_str().unwrap().to_owned())
            .or_default() += 1;
        checked += 1;
    }
    eprintln!("log_keyring groups: {groups:?}");
    assert!(
        failures.is_empty(),
        "{} failures:\n{}",
        failures.len(),
        failures.join("\n")
    );
    assert_count("log_keyring.jsonl", checked, lines.len() - 1);
}
