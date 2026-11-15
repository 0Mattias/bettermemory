//! Every case of `canonical_json.jsonl`: each value, built from its neutral
//! form, through each JSON form the reference spells, byte for byte.

mod common;

use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::pyvalue::{PyError, PyStr, PyValue};
use bettermemory_core::unicode;
use common::{assert_count, hex, jsonl, neutral, str_output};
use serde_json::{json, Value};

fn with_newline(mut text: PyStr) -> PyStr {
    text.push(u32::from(b'\n'));
    text
}

/// A form of the fixture header: the call site's exact call.
fn form(name: &str, value: &PyValue) -> Result<Value, PyError> {
    let text = |options: DumpOptions| json::dumps(value, options);
    Ok(match name {
        "log_payload" => json!({ "hex": hex(json::canonical_payload(value)?.as_bytes()) }),
        "native_ascii" => str_output(&text(DumpOptions::SORTED_ASCII_COMPACT)?),
        "column_text" => str_output(&text(DumpOptions::SORTED_UNICODE_SPACED)?),
        "ascii_spaced" => str_output(&text(DumpOptions::SORTED_ASCII_SPACED)?),
        "ascii_compact_line" => str_output(&with_newline(text(DumpOptions::SORTED_ASCII_COMPACT)?)),
        "review_sha_bytes" => {
            let out = text(DumpOptions::SORTED_UNICODE_COMPACT)?;
            let utf8 = unicode::to_string(&out).ok_or(PyError::UnicodeEncodeError)?;
            json!({ "hex": hex(utf8.as_bytes()) })
        }
        "spelling_tables" => str_output(&text(DumpOptions::SORTED_UNICODE_COMPACT)?),
        "document_indent2" => str_output(&with_newline(text(DumpOptions::DOCUMENT_INDENT2)?)),
        "keyring_file" => str_output(&text(DumpOptions::DEFAULT)?),
        "unsorted_compact_line" => {
            str_output(&with_newline(text(DumpOptions::UNSORTED_ASCII_COMPACT)?))
        }
        other => panic!("form {other} is not ported"),
    })
}

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
            let got = form(name, &value).unwrap_or_else(|e| json!({ "error": e.name() }));
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
