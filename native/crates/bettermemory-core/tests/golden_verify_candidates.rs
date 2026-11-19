//! Every case of `verify_candidates.jsonl` and
//! `verify_candidates_unicode.jsonl` against `verify_candidates::validate`:
//! single calls, exhaustive runs over small alphabets, the scan bound,
//! the module's constants and the code points `str.islower` reads as
//! lowercase.

mod common;

use bettermemory_core::verify_candidates::{self as vc, validate};
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Value};

fn text(v: &Value) -> Vec<u32> {
    match v {
        Value::String(s) => s.chars().map(u32::from).collect(),
        _ => v["cps"]
            .as_array()
            .expect("a text")
            .iter()
            .map(|c| c.as_u64().expect("a code point") as u32)
            .collect(),
    }
}

/// The generator's `_t`: code points for a text holding a surrogate or
/// two backslashes in a row, else the text.
fn spell(t: &[u32]) -> Value {
    let doubled = t.windows(2).any(|w| w == [0x5c, 0x5c]);
    match t
        .iter()
        .map(|&c| char::from_u32(c))
        .collect::<Option<String>>()
    {
        Some(s) if !doubled => json!(s),
        _ => json!({ "cps": t }),
    }
}

fn opt(t: Option<&[u32]>) -> Value {
    t.map_or(Value::Null, spell)
}

fn raised<T>(r: Result<T, validate::IndexError>, ok: impl FnOnce(T) -> Value) -> Value {
    match r {
        Ok(v) => ok(v),
        Err(_) => json!({"raise": "IndexError"}),
    }
}

fn call(f: &str, args: &[Value]) -> Value {
    let a = text(&args[0]);
    match f {
        "normalize_candidate" => raised(validate::normalize_candidate(&a), |v| opt(v.as_deref())),
        "trim_candidate" => spell(&validate::trim_candidate(&a)),
        "command_shaped" => raised(validate::command_shaped(&a), |v| json!(v)),
        "anchored_candidate" => opt(validate::anchored_candidate(&a)),
        "line_suffix" => opt(validate::line_suffix(&a)),
        "is_placeholder_path" => json!(validate::is_placeholder_path(&a)),
        "is_single_segment_routelike" => json!(validate::is_single_segment_routelike(&a)),
        "refused_spelling" => json!(validate::refused_spelling(&a)),
        "escapes_root" => json!(validate::escapes_root(&a)),
        "fold_altsep" => {
            let alt = (!args[2].is_null()).then(|| text(&args[2]));
            spell(&validate::fold_altsep(&a, &text(&args[1]), alt.as_deref()))
        }
        _ => panic!("no function {f}"),
    }
}

fn check_exhaustive(row: &Value) {
    let f = row["f"].as_str().unwrap();
    let alphabet: Vec<u32> = text(&row["alphabet"]);
    let top = row["max"].as_u64().unwrap() as u32;
    let mut hits: std::collections::HashMap<Vec<u32>, Value> = row["hits"]
        .as_array()
        .unwrap()
        .iter()
        .map(|h| (text(&h[0]), h[1].clone()))
        .collect();
    let mut seen = 0usize;
    for n in 1..=top {
        let total = alphabet.len().pow(n);
        for mut index in 0..total {
            let mut t = vec![0u32; n as usize];
            for slot in t.iter_mut().rev() {
                *slot = alphabet[index % alphabet.len()];
                index /= alphabet.len();
            }
            let got = call(f, &[spell(&t)]);
            let want = match hits.remove(&t) {
                Some(w) => w,
                None if row["default"] == json!("self") => spell(&t),
                None => row["default"].clone(),
            };
            assert_eq!(got, want, "{f}({:?})", spell(&t));
            seen += 1;
        }
    }
    assert!(seen > 0);
    assert!(hits.is_empty(), "{f}: hits never enumerated: {hits:?}");
}

fn check_row(row: &Value) {
    let f = row["f"].as_str().expect("a function");
    if row.get("hits").is_some() {
        return check_exhaustive(row);
    }
    if f == "bounded_scan_text" {
        let mut body = Vec::new();
        for run in row["runs"].as_array().unwrap() {
            let piece = text(&run[0]);
            for _ in 0..run[1].as_u64().unwrap() {
                body.extend_from_slice(&piece);
            }
        }
        let got = validate::bounded_scan_text(&body);
        assert_eq!(
            json!(got.len()),
            row["r"],
            "bounded_scan_text {:?}",
            row["runs"]
        );
        return;
    }
    if f == "lower" {
        let ranges = row["r"].as_array().unwrap();
        let mut at = 0u32;
        for r in ranges {
            let (lo, hi) = (r[0].as_u64().unwrap() as u32, r[1].as_u64().unwrap() as u32);
            assert!((at..lo).all(|c| !validate::is_lower(c)), "below {lo:#x}");
            assert!((lo..=hi).all(validate::is_lower), "in {lo:#x}..={hi:#x}");
            at = hi + 1;
        }
        assert!((at..0x110000).all(|c| !validate::is_lower(c)));
        return;
    }
    let args = row["a"].as_array().expect("arguments");
    assert_eq!(call(f, args), row["r"], "{f}{args:?}");
}

fn run(name: &str, test: &str) {
    let lines = jsonl(name);
    let mut ledger = Ledger::new(test, name);
    for (i, row) in lines[1..].iter().enumerate() {
        ledger.case(format!("line {}", i + 2), || check_row(row));
    }
    ledger.finish(lines.len() - 1);
}

#[test]
fn verify_candidates_every_case() {
    run("verify_candidates.jsonl", "verify_candidates_every_case");
}

#[test]
fn verify_candidates_unicode_every_case() {
    let lines = jsonl("verify_candidates_unicode.jsonl");
    assert_eq!(lines[0]["unicode"], "15.1.0");
    run(
        "verify_candidates_unicode.jsonl",
        "verify_candidates_unicode_every_case",
    );
}

#[test]
fn verify_candidates_constants() {
    let c = &jsonl("verify_candidates.jsonl")[0]["constants"];
    assert_eq!(c["MAX_PATHS_PER_BODY"], json!(vc::MAX_PATHS_PER_BODY));
    assert_eq!(c["MAX_PATH_LENGTH"], json!(vc::MAX_PATH_LENGTH));
    assert_eq!(c["MAX_BODY_SCAN_BYTES"], json!(vc::MAX_BODY_SCAN_BYTES));
    assert_eq!(c["MAX_ANCHOR_CITATIONS"], json!(vc::MAX_ANCHOR_CITATIONS));
    assert_eq!(
        c["MAX_ANCHORED_CITATION_STATS"],
        json!(vc::MAX_ANCHORED_CITATION_STATS)
    );
    assert_eq!(c["BODY_PARSE_ENTRIES"], json!(vc::BODY_PARSE_ENTRIES));
    assert_eq!(c["TRAILING_PUNCT"], json!(vc::TRAILING_PUNCT));
    let sorted = |list: &[&str]| {
        let mut v: Vec<&str> = list.to_vec();
        v.sort_unstable();
        json!(v)
    };
    assert_eq!(
        c["WELLKNOWN_ROUTE_SEGMENTS"],
        sorted(&vc::WELLKNOWN_ROUTE_SEGMENTS)
    );
    assert_eq!(c["PLACEHOLDER_PATHS"], sorted(&vc::PLACEHOLDER_PATHS));
    assert_eq!(c["PLACEHOLDER_PREFIXES"], json!(vc::PLACEHOLDER_PREFIXES));
    assert_eq!(
        c["CHECKABLE_CITATION_EXTENSIONS"],
        sorted(&vc::CHECKABLE_CITATION_EXTENSIONS)
    );
}
