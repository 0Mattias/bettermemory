//! Reading the golden files of `tests/contract/golden` (relative to
//! `native/`, i.e. `../tests/contract/golden`), or of the directory
//! `BM_GOLDEN_DIR` names (`parity`).

#![allow(dead_code)]

pub mod argparse;
pub mod attestation_paths;
pub mod bulk;
pub mod created;
pub mod dedup;
pub mod gitconfig_file;
pub mod gitconfig_tree;
pub mod githead;
pub mod githead_discovery;
pub mod gitobjects;
pub mod gitobjects_files;
pub mod gitobjects_reader;
pub mod hook_stop;
pub mod location_git;
pub mod location_reads;
pub mod location_stamps;
pub mod location_view;
pub mod membership_core;
pub mod membership_proposals;
pub mod membership_queries;
pub mod membership_rows;
pub mod membership_touch;
pub mod membership_views;
pub mod membership_writes;
pub mod opened;
pub mod origin_dates;
pub mod origin_dates_memo;
pub mod origin_history_located;
pub mod parity;
pub mod provenance;
pub mod record_calls;
pub mod repository_history;
pub mod response_leaf;
pub mod rows;
pub mod schema_steps;
pub mod session;
pub mod settings_create;
pub mod settle;
pub mod stores;
pub mod symbols;
pub mod tool_handlers;
pub mod txs;
pub mod use_tokens;
pub mod verify_anchoring_attest;
pub mod verify_anchoring_skip;
pub mod written;

use std::path::PathBuf;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::float_repr;
use bettermemory_core::json::{self, DumpOptions};
use bettermemory_core::pyvalue::{PyError, PyStr, PyValue};
use bettermemory_core::unicode;
use serde_json::Value;

/// A golden file in the directory under test: `BM_GOLDEN_DIR`, else the
/// committed one. Every test reads its golden files through here.
pub fn golden_path(name: &str) -> PathBuf {
    parity::golden_dir().join(name)
}

/// A golden file as committed, whatever `BM_GOLDEN_DIR` says: for the
/// copies compiled into the crate, which are pinned to the committed text.
pub fn committed_path(name: &str) -> PathBuf {
    parity::committed_dir().join(name)
}

/// Every line of a `.jsonl` golden file, parsed: the header first.
pub fn jsonl(name: &str) -> Vec<Value> {
    let text = std::fs::read_to_string(golden_path(name))
        .unwrap_or_else(|e| panic!("cannot read golden file {name}: {e}"));
    let lines: Vec<Value> = text
        .lines()
        .map(|line| serde_json::from_str(line).expect("a JSON line"))
        .collect();
    assert!(!lines.is_empty(), "{name} is empty");
    lines
}

pub fn unhex(text: &str) -> Vec<u8> {
    assert!(text.len().is_multiple_of(2));
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).expect("hex"))
        .collect()
}

pub fn cps_of(value: &Value) -> PyStr {
    value
        .as_array()
        .expect("a code point list")
        .iter()
        .map(|c| c.as_u64().expect("a code point") as u32)
        .collect()
}

/// A `text_field`: `name` as text, or `name_cps` as code points.
pub fn text_field(record: &Value, name: &str) -> PyStr {
    if let Some(text) = record.get(name) {
        return unicode::cps(text.as_str().expect("a string"));
    }
    cps_of(&record[format!("{name}_cps")])
}

/// The Python value a `neutral` description names.
pub fn neutral(value: &Value) -> PyValue {
    let items = || value["v"].as_array().expect("a list").iter();
    match value["t"].as_str().expect("a neutral type") {
        "null" => PyValue::None,
        "bool" => PyValue::Bool(value["v"].as_bool().expect("a bool")),
        "int" => PyValue::int_from_decimal(value["v"].as_str().expect("decimal")).expect("an int"),
        "float" => {
            let bits = u64::from_str_radix(value["bits"].as_str().expect("bits"), 16).expect("hex");
            let f = f64::from_bits(bits);
            assert_eq!(
                float_repr::repr(f),
                value["repr"].as_str().expect("repr"),
                "float repr of bits {bits:016x}"
            );
            PyValue::Float(f)
        }
        "str" => PyValue::Str(text_field(value, "v")),
        "datetime" => {
            let iso = value["iso"].as_str().expect("iso");
            let dt = DateTime::from_isoformat(&unicode::cps(iso)).expect("an isoformat");
            assert_eq!(dt.isoformat(), iso, "datetime round trip");
            PyValue::DateTime(dt)
        }
        "path" => PyValue::Path(unicode::cps(value["v"].as_str().expect("a path"))),
        "tuple" => PyValue::Tuple(items().map(neutral).collect()),
        "list" => PyValue::List(items().map(neutral).collect()),
        "dict" => PyValue::Dict(
            items()
                .map(|pair| (neutral(&pair[0]), neutral(&pair[1])))
                .collect(),
        ),
        other => panic!("unknown neutral type {other}"),
    }
}

/// `_common.output` of a `str` result.
pub fn str_output(text: &[u32]) -> Value {
    match unicode::to_string(text) {
        Some(s) => serde_json::json!({ "hex": hex(s.as_bytes()) }),
        None => serde_json::json!({ "cps": text, "utf8": false }),
    }
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Asserts the number of cases checked is the number the file holds.
pub fn assert_count(name: &str, checked: usize, in_file: usize) {
    assert!(checked > 0, "{name}: no case checked");
    assert_eq!(
        checked, in_file,
        "{name}: checked {checked} of {in_file} cases"
    );
    eprintln!("{name}: {checked} of {in_file} cases checked, all passing");
    let file = name.split_whitespace().next().unwrap_or(name);
    let class = parity::class_of(parity::gates_of(file));
    parity::record(name, file, class, checked, &[], &[]);
}

/// What a JSON form of `canonical_json.jsonl` returns: a `str` or bytes.
pub enum FormOut {
    Text(PyStr),
    Bytes(Vec<u8>),
}

impl FormOut {
    /// `_common.output` of it.
    pub fn value(&self) -> Value {
        match self {
            FormOut::Text(text) => str_output(text),
            FormOut::Bytes(bytes) => serde_json::json!({ "hex": hex(bytes) }),
        }
    }

    /// Its bytes: a `str` as UTF-8 (surrogates passed through).
    pub fn bytes(&self) -> Vec<u8> {
        match self {
            FormOut::Text(text) => {
                let mut out = Vec::with_capacity(text.len());
                for &cp in text {
                    match char::from_u32(cp) {
                        Some(c) => out.extend_from_slice(c.encode_utf8(&mut [0; 4]).as_bytes()),
                        // surrogatepass: the three-byte form.
                        None => out.extend_from_slice(&[
                            0xE0 | (cp >> 12) as u8,
                            0x80 | ((cp >> 6) & 0x3F) as u8,
                            0x80 | (cp & 0x3F) as u8,
                        ]),
                    }
                }
                out
            }
            FormOut::Bytes(bytes) => bytes.clone(),
        }
    }
}

fn with_newline(mut text: PyStr) -> PyStr {
    text.push(u32::from(b'\n'));
    text
}

/// A form of `canonical_json.jsonl`'s header: the call site's exact call.
pub fn json_form_out(name: &str, value: &PyValue) -> Result<FormOut, PyError> {
    let text = |options: DumpOptions| json::dumps(value, options);
    Ok(match name {
        "log_payload" => FormOut::Bytes(json::canonical_payload(value)?.into_bytes()),
        "native_ascii" => FormOut::Text(text(DumpOptions::SORTED_ASCII_COMPACT)?),
        "column_text" => FormOut::Text(text(DumpOptions::SORTED_UNICODE_SPACED)?),
        "ascii_spaced" => FormOut::Text(text(DumpOptions::SORTED_ASCII_SPACED)?),
        "ascii_compact_line" => {
            FormOut::Text(with_newline(text(DumpOptions::SORTED_ASCII_COMPACT)?))
        }
        "review_sha_bytes" => FormOut::Bytes(json::review_canonical_json(value)?),
        "spelling_tables" => FormOut::Text(text(DumpOptions::SORTED_UNICODE_COMPACT)?),
        "document_indent2" => FormOut::Text(with_newline(text(DumpOptions::DOCUMENT_INDENT2)?)),
        "keyring_file" => FormOut::Text(text(DumpOptions::DEFAULT)?),
        "unsorted_compact_line" => {
            FormOut::Text(with_newline(text(DumpOptions::UNSORTED_ASCII_COMPACT)?))
        }
        other => panic!("form {other} is not ported"),
    })
}

/// [`json_form_out`] as the fixture records it, or `{"error": type}`.
pub fn json_form(name: &str, value: &PyValue) -> Result<Value, PyError> {
    json_form_out(name, value).map(|out| out.value())
}
