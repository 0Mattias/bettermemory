//! Reading the golden files of `tests/contract/golden` (relative to
//! `native/`, i.e. `../tests/contract/golden`).

#![allow(dead_code)]

use std::path::PathBuf;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::float_repr;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode;
use serde_json::Value;

pub fn golden_path(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../tests/contract/golden")
        .join(name)
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
}
