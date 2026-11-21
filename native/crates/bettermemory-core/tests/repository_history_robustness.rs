//! Hostile input for `RepositoryHistory`: large git outputs, many ids,
//! wide trees, integer bounds. No panic in debug or release, and time
//! grows as the reference's does.

mod common;
mod timing;

use common::repository_history::run;
use serde_json::{json, Value};

fn id(i: usize) -> String {
    format!("{i:040x}")
}

fn paired(n: usize) -> Value {
    let ids: Vec<String> = (0..n).map(id).collect();
    let out: String = ids.iter().map(|i| format!("{i} commit 9\n")).collect();
    json!({"r": {"o": {}, "c": false}, "g": [{"o": out}], "s": [["holds", ids]]})
}

#[test]
fn paired_batch_check_grows_linearly() {
    let small = paired(2_000);
    let large = paired(20_000);
    timing::grows_within(
        "holds: 2,000 and 20,000 ids through git",
        timing::TENFOLD,
        &mut || timing::once(|| run(&small)),
        &mut || timing::once(|| run(&large)),
    );
    assert_eq!(run(&small)[0]["a"].as_array().unwrap().len(), 2_000);
}

#[test]
fn hostile_git_output() {
    let long = "f".repeat(1 << 20);
    let got = run(&json!({
        "r": {"o": {}, "c": false},
        "g": [
            {"o": format!("{long} commit\n{}", "\r".repeat(100_000))},
            {"ox": "ff".repeat(100_000)},
            {"o": "\u{2028}".repeat(50_000)},
        ],
        "s": [["holds", [id(1)]], ["holds", [id(1)]], ["holds", [id(1), id(2)]]],
    }));
    for step in got {
        assert_eq!(step["a"], json!([]));
    }
}

#[test]
fn independent_bounds_and_huge_answers() {
    let many: Vec<String> = (0..10_000).map(id).collect();
    let got = run(&json!({"s": [["independent", many]]}));
    assert_eq!(got[0]["a"], Value::Null);
    assert_eq!(got[0]["q"], json!([]));
    let two: Vec<String> = (0..256).map(id).collect();
    let out: String = two
        .iter()
        .map(|i| format!("{i}\n"))
        .collect::<String>()
        .repeat(100);
    let got = run(&json!({"g": [{"o": out}], "s": [["independent", two]]}));
    assert_eq!(got[0]["a"].as_array().unwrap().len(), 256);
}

#[test]
fn wide_trees_and_extreme_times() {
    let root: Vec<Value> = (0..100_000)
        .map(|i| json!([0o040000, "d", id(i)]))
        .collect();
    let got = run(&json!({
        "r": {"ct": {id(1): "t"}, "tr": {"t": root}},
        "s": [["blobs", id(1)]],
    }));
    assert_eq!(got[0]["a"], json!([]));
    let body = format!(
        "tree {}\nauthor a <a> 1 +0000\ncommitter c <c> {} +0000\n\nm\n",
        id(0),
        "9".repeat(5_000)
    );
    let hex: String = body.bytes().map(|b| format!("{b:02x}")).collect();
    let got = run(&json!({
        "r": {"o": {id(1): ["commit", hex]}},
        "s": [["committed", id(1)], ["reach", [id(1), id(2)]]],
    }));
    assert_eq!(got[0]["a"], json!(u64::MAX));
}
