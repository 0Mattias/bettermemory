//! Every case of `canonical_json.jsonl`: each value, built from its neutral
//! form, through each JSON form the reference spells, byte for byte.

mod common;

use common::{assert_count, json_form, jsonl, neutral};
use serde_json::json;

#[test]
fn canonical_json_every_case() {
    let lines = jsonl("canonical_json.jsonl");
    let header = &lines[0];
    assert_eq!(header["fixture"], "canonical_json");
    let forms: Vec<&String> = header["forms"].as_object().expect("forms").keys().collect();
    assert_eq!(forms.len(), 10);
    let mut checked = 0;
    let mut cells = 0;
    let mut failures = Vec::new();
    for record in &lines[1..] {
        let value = neutral(&record["input"]);
        let out = record["out"].as_object().expect("out");
        assert_eq!(
            out.keys().collect::<Vec<_>>(),
            forms,
            "{}: forms",
            record["name"]
        );
        for (name, expected) in out {
            let got = json_form(name, &value).unwrap_or_else(|e| json!({ "error": e.name() }));
            if &got != expected {
                failures.push(format!(
                    "{} / {name}: expected {expected}, got {got}",
                    record["name"]
                ));
            }
            cells += 1;
        }
        checked += 1;
    }
    eprintln!("canonical_json: {cells} form cells over {checked} values");
    assert!(
        failures.is_empty(),
        "{} failures:\n{}",
        failures.len(),
        failures.join("\n")
    );
    assert_count("canonical_json.jsonl", checked, lines.len() - 1);
}
