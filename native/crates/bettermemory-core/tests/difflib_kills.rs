//! Cases a plausible wrong `difflib` fails, each with the answer CPython
//! 3.13.13 gives.

use bettermemory_core::difflib::{
    body_diff, format_range_unified, splitlines, SequenceMatcher, Tag,
};
use bettermemory_core::pyvalue::PyStr;

fn s(t: &str) -> PyStr {
    t.chars().map(u32::from).collect()
}

fn ls(t: &[&str]) -> Vec<PyStr> {
    t.iter().map(|x| s(x)).collect()
}

fn text(p: &[u32]) -> String {
    p.iter().map(|&x| char::from_u32(x).unwrap()).collect()
}

fn diff(a: &str, b: &str) -> String {
    text(&body_diff(Some(&s(a)), &s(b), &s("o"), &s("n")))
}

#[test]
fn equal_texts_have_no_header() {
    assert_eq!(diff("a\nb", "a\r\nb\n"), "");
    assert_eq!(text(&body_diff(None, &[], &s("o"), &s("n"))), "");
}

#[test]
fn ties_go_to_the_earliest_i_then_j() {
    assert_eq!(
        diff("x\ny\nx\ny", "y\nx"),
        "--- o\n+++ n\n@@ -1,4 +1,2 @@\n-x\n y\n x\n-y"
    );
    let a = ls(&["x", "a", "b", "x", "a", "b"]);
    let b = ls(&["a", "b"]);
    assert_eq!(
        SequenceMatcher::new(&a, &b).find_longest_match(0, 6, 0, 2),
        (1, 0, 2)
    );
    assert_eq!(
        diff("a\nb\nc", "c\na\nb"),
        "--- o\n+++ n\n@@ -1,3 +1,3 @@\n+c\n a\n b\n-c"
    );
}

#[test]
fn autojunk_starts_at_two_hundred_lines() {
    let a = ls(&["P", "u0", "P"]);
    let mut b: Vec<PyStr> = (0..196).map(|i| s(&format!("u{i}"))).collect();
    b.extend(ls(&["P", "P", "P", "P"]));
    let popular = SequenceMatcher::new(&a, &b).get_opcodes();
    assert_eq!(
        popular,
        vec![
            (Tag::Delete, 0, 1, 0, 0),
            (Tag::Equal, 1, 2, 0, 1),
            (Tag::Replace, 2, 3, 1, 200)
        ]
    );
    b.remove(0);
    let short = SequenceMatcher::new(&a, &b).get_opcodes();
    assert_eq!(
        short,
        vec![
            (Tag::Insert, 0, 0, 0, 195),
            (Tag::Equal, 0, 1, 195, 196),
            (Tag::Delete, 1, 2, 196, 196),
            (Tag::Equal, 2, 3, 196, 197),
            (Tag::Insert, 3, 3, 197, 199)
        ]
    );
}

#[test]
fn popular_lines_are_matched_by_extension() {
    let mut b = ls(&["P", "P", "P", "P"]);
    b.extend((0..196).map(|i| s(&format!("u{i}"))));
    let a = ls(&["P", "P", "P", "P", "Q"]);
    assert_eq!(
        SequenceMatcher::new(&a, &b).get_opcodes(),
        vec![(Tag::Equal, 0, 4, 0, 4), (Tag::Replace, 4, 5, 4, 200)]
    );
}

#[test]
fn groups_split_past_twice_the_context() {
    let ctx: Vec<String> = (0..20).map(|i| format!("l{i}")).collect();
    let a = ctx.join("\n");
    let mut b6 = ctx.clone();
    b6[3] = "L".into();
    b6[10] = "M".into();
    assert_eq!(diff(&a, &b6.join("\n")).matches("@@ -").count(), 1);
    let mut b7 = ctx.clone();
    b7[3] = "L".into();
    b7[11] = "M".into();
    assert_eq!(diff(&a, &b7.join("\n")).matches("@@ -").count(), 2);
    let g = SequenceMatcher::new(
        &ls(&["a", "b", "x", "c", "d"]),
        &ls(&["a", "b", "y", "c", "d"]),
    )
    .get_grouped_opcodes(1);
    assert_eq!(
        g,
        vec![vec![
            (Tag::Equal, 1, 2, 1, 2),
            (Tag::Replace, 2, 3, 2, 3),
            (Tag::Equal, 3, 4, 3, 4)
        ]]
    );
}

#[test]
fn ranges_are_spelled_as_ed_does() {
    assert_eq!(format_range_unified(0, 0), "0,0");
    assert_eq!(format_range_unified(4, 4), "4,0");
    assert_eq!(format_range_unified(4, 5), "5");
    assert_eq!(format_range_unified(4, 7), "5,3");
    assert_eq!(diff("", "x"), "--- o\n+++ n\n@@ -0,0 +1 @@\n+x");
}

#[test]
fn splitlines_boundaries() {
    assert_eq!(
        splitlines(&s("a\u{1f}b\r\nc\n\rd")),
        ls(&["a\u{1f}b", "c", "", "d"])
    );
    for sep in [
        "\n", "\r", "\r\n", "\u{b}", "\u{c}", "\u{1c}", "\u{1d}", "\u{1e}", "\u{85}", "\u{2028}",
        "\u{2029}",
    ] {
        assert_eq!(
            splitlines(&s(&format!("a{sep}b{sep}"))),
            ls(&["a", "b"]),
            "{sep:?}"
        );
    }
    assert_eq!(splitlines(&s("\r\r\n")), ls(&["", ""]));
    assert_eq!(splitlines(&[0xd800, 0x85]), vec![vec![0xd800]]);
}
