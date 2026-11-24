//! Every case of `log_replaced.jsonl`: `history_replaced_kind` and
//! `history_replaced_row` over an entry parsed as `json.loads` parses it,
//! and the `HISTORY_REPLACED` table in its insertion order.

mod common;

use bettermemory_core::json;
use bettermemory_core::log::replaced::{
    history_replaced_kind, history_replaced_row, HISTORY_REPLACED,
};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode;
use common::{assert_count, jsonl};

fn parse(text: &str) -> PyValue {
    json::loads(&unicode::cps(text)).expect("a JSON text")
}

#[test]
fn log_replaced_every_case() {
    let lines = jsonl("log_replaced.jsonl");
    let header = &lines[0];
    assert_eq!(header["fixture"], "log_replaced");
    let table: Vec<(String, (String, String))> = HISTORY_REPLACED
        .iter()
        .map(|(k, (noun, action))| ((*k).to_owned(), ((*noun).to_owned(), (*action).to_owned())))
        .collect();
    let want: Vec<(String, (String, String))> = header["table"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| {
            (
                e[0].as_str().unwrap().to_owned(),
                (
                    e[1][0].as_str().unwrap().to_owned(),
                    e[1][1].as_str().unwrap().to_owned(),
                ),
            )
        })
        .collect();
    assert_eq!(table, want);
    let mut failures = Vec::new();
    let mut checked = 0;
    for record in &lines[1..] {
        let text = record["entry"].as_str().expect("entry");
        let entry = parse(text);
        let kind = history_replaced_kind(&entry);
        if kind != record["kind"] {
            failures.push(format!(
                "{text}: kind {kind}, the reference {}",
                record["kind"]
            ));
        }
        let row = history_replaced_row(&entry).map_or(PyValue::None, |r| r.to_value());
        let expected = parse(record["row"].as_str().expect("row"));
        if row != expected {
            failures.push(format!("{text}: row {row:?}, the reference {expected:?}"));
        }
        checked += 1;
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
    assert_count("log_replaced.jsonl", checked, lines.len() - 1);
}
