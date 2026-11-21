//! Cases a plausible wrong `RepositoryHistory` fails: each names the
//! mistake it catches.

mod common;

use bettermemory_core::repository_history::{askable, split, splitlines};
use common::repository_history::run;
use serde_json::{json, Value};

const A: &str = "ca978112ca1bbdcafac231b39a23dc4da786eff8";
const D: &str = "18ac3e7343f016890c510e93f935261169d9e3f5";
const E: &str = "3f79bb7b435b05321651daefd374cdc681dc06fa";

fn answers(case: Value) -> Vec<Value> {
    run(&case).into_iter().map(|s| s["a"].clone()).collect()
}

#[test]
fn python_line_breaks_and_spaces() {
    // `str.lines` and `char::is_whitespace` are not Python's.
    assert_eq!(
        splitlines("a\x1cb\x1dc\x1ed\x0be\x0cf\r\ng\rh\u{85}i\u{2028}j\u{2029}"),
        ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]
    );
    assert_eq!(splitlines("\n\n"), ["", ""]);
    assert_eq!(splitlines("x\x1fy"), ["x\x1fy"]);
    assert_eq!(split("a\x1fb\x1cc\u{a0}d"), ["a", "b", "c", "d"]);
    assert_eq!(split("a\u{200b}b"), ["a\u{200b}b"]);
}

#[test]
fn askable_is_ascii_hex_four_to_sixty_four() {
    assert!(askable("abcD"));
    assert!(!askable("abc"));
    assert!(askable(&"f".repeat(64)));
    assert!(!askable(&"f".repeat(65)));
    assert!(!askable("abc\u{ff10}"));
}

#[test]
fn incomplete_reader_answer_stands_where_git_declines() {
    let got = answers(json!({
        "r": {"o": {A: ["blob", "00"]}, "c": false},
        "g": [null],
        "s": [["expand", [A, &A[..6]]]],
    }));
    assert_eq!(got[0], json!([[A, A], [&A[..6], A]]));
}

#[test]
fn git_overrides_the_reader_for_an_abbreviation() {
    // An abbreviation git does not find is dropped, though the reader found it.
    let got = answers(json!({
        "r": {"o": {A: ["blob", "00"]}, "c": false},
        "g": [{"o": format!("{} missing\n", &A[..6])}],
        "s": [["expand", [&A[..6]]]],
    }));
    assert_eq!(got[0], json!([]));
}

#[test]
fn mispaired_lines_match_full_ids_by_name_only() {
    let got = answers(json!({
        "r": {"o": {}, "c": false},
        "g": [{"o": format!("{} commit 1\n{} tree 1\n{} blob 1\n", D.to_uppercase(), E, E)}],
        "s": [["_expand", [D, E, &A[..8], A]]],
    }));
    // D's line is in uppercase: it does not match; E's later line wins.
    assert_eq!(got[0], json!([[E, E, "blob"]]));
}

#[test]
fn independent_hands_git_sorted_lowercase_ids_and_logs_short_args() {
    let out = run(&json!({
        "g": [{"rc": 1}],
        "s": [["independent", [E, D.to_uppercase(), E]]],
    }));
    let q = &out[0]["q"];
    let mut ids = [D, E];
    ids.sort();
    assert_eq!(
        q[0][1].as_array().unwrap()[5..],
        [json!(ids[0]), json!(ids[1])]
    );
    assert_eq!(q[0][4], json!({"GIT_GRAFT_FILE": "<devnull>"}));
    assert_eq!(
        q[1],
        json!(["log", ["merge-base", "--independent"], 1, "C"])
    );
    assert_eq!(out[0]["a"], Value::Null);
}

#[test]
fn independent_needs_a_non_empty_subset() {
    let got = answers(json!({
        "g": [{"o": "\n"}, {"o": format!("{D}\n{A}\n")}, {"o": format!(" {}\r\n", D.to_uppercase())}],
        "s": [["independent", [D, E]], ["independent", [D, E]], ["independent", [D, E]]],
    }));
    assert_eq!(got, [Value::Null, Value::Null, json!([D])]);
}

#[test]
fn parents_cache_an_unread_body() {
    let out = run(&json!({
        "r": {"o": {}, "c": false},
        "g": [null],
        "s": [["parents", D], ["committed", D], ["parents", D.to_uppercase()]],
    }));
    assert_eq!(out[0]["q"].as_array().unwrap().len(), 1);
    assert_eq!(out[1]["q"], json!([]));
    assert_eq!(out[2]["q"], json!([]));
    assert_eq!(out[1]["a"], json!(0));
}

#[test]
fn blobs_sort_before_cutting_and_skip_depth_two() {
    let names: Vec<String> = (0..12).map(|i| format!("{:040x}", 100 - i)).collect();
    let mut root: Vec<Value> = names.iter().map(|n| json!([0o100644, "f", n])).collect();
    root.insert(0, json!([0o040000, "d", "t1"]));
    let sizes: serde_json::Map<String, Value> = names
        .iter()
        .map(|n| (n.clone(), json!(64)))
        .chain([(A.to_owned(), json!(64))])
        .collect();
    let got = answers(json!({
        "r": {
            "ct": {D: "t0"},
            "tr": {"t0": root, "t1": [[0o040000, "dd", "t2"]], "t2": [[0o100644, "x", A]]},
            "sz": sizes,
        },
        "s": [["blobs", D]],
    }));
    let mut want = names.clone();
    want.sort();
    want.truncate(8);
    assert_eq!(got[0], json!(want));
}

#[test]
fn blob_look_counts_only_regular_files() {
    // 64 links and the project file before one blob: none of them counts.
    let mut root: Vec<Value> = (0..64)
        .map(|i| json!([0o120000, format!("l{i}"), A]))
        .collect();
    root.push(json!([0o100644, "bettermemory.toml", A]));
    root.push(json!([0o100644, "f", E]));
    let got = answers(json!({
        "r": {"ct": {D: "t0"}, "tr": {"t0": root}, "sz": {A: 100, E: 100}},
        "s": [["blobs", D]],
    }));
    assert_eq!(got[0], json!([E]));
}

#[test]
fn blobs_never_enter_a_depth_two_tree() {
    // The only blob sits two directories down: nothing is sampled.
    let low = format!("{:040x}", 1);
    let got = answers(json!({
        "r": {
            "ct": {D: "t0"},
            "tr": {
                "t0": [[0o040000, "d", "t1"]],
                "t1": [[0o040000, "dd", "t2"]],
                "t2": [[0o100644, "x", low]],
            },
            "sz": {low.clone(): 64},
        },
        "s": [["blobs", D]],
    }));
    assert_eq!(got[0], json!([]));
}

#[test]
fn blob_look_cuts_inside_one_tree() {
    // 65 regular files in the root; the 65th, with the smallest id, is
    // past BLOB_LOOK and stays out of the sample.
    let mut names: Vec<String> = (0..64).map(|i| format!("{:040x}", 1000 + i)).collect();
    names.push(format!("{:040x}", 1));
    let root: Vec<Value> = names
        .iter()
        .enumerate()
        .map(|(i, n)| json!([0o100644, format!("f{i}"), n]))
        .collect();
    let sizes: serde_json::Map<String, Value> =
        names.iter().map(|n| (n.clone(), json!(64))).collect();
    let got = answers(json!({
        "r": {"ct": {D: "t0"}, "tr": {"t0": root}, "sz": sizes},
        "s": [["blobs", D]],
    }));
    let mut want = names[..64].to_vec();
    want.sort();
    want.truncate(8);
    assert_eq!(got[0], json!(want));
}

#[test]
fn parents_of_an_id_longer_than_64_read_forty_hex_ids() {
    // A 65-character id is parsed with hexsz 40, so a body of 64-hex
    // lines yields no parents.
    let long = format!("{:065x}", 7);
    let body = format!(
        "tree {t}\nparent {p}\ncommitter a <a> 1 +0000\n\nm\n",
        t = "a".repeat(64),
        p = "b".repeat(64)
    );
    let hexbody: String = body.bytes().map(|b| format!("{b:02x}")).collect();
    let got = answers(json!({
        "r": {"o": {long.clone(): ["commit", hexbody]}, "c": true},
        "s": [["parents", long]],
    }));
    assert_eq!(got[0], json!([]));
}
