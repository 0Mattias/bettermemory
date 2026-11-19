//! Cases a plausible wrong implementation of the `origin_gitfiles`
//! decoders fails, each with the reference's answer (read from the
//! reference under CPython 3.13.13).

use bettermemory_core::origin_gitfiles::{
    decode_varint, dequote_named_header, dequote_patch_headers, gitmodules_entries, hunk_lines,
    quoted_header_path, shortening_refs, unquote_c_path, IndexFault, Raised,
};
use bettermemory_core::unicode::{cps, to_string};

fn s(text: &[u32]) -> String {
    to_string(text).expect("text")
}

fn unq(text: &str) -> Option<String> {
    unquote_c_path(&cps(text)).expect("no raise").map(|t| s(&t))
}

#[test]
fn octal_takes_at_most_three_digits_and_masks_to_a_byte() {
    assert_eq!(unq("\"\\1011\"").as_deref(), Some("A1"));
    assert_eq!(unq("\"\\400\"").as_deref(), Some("\0"));
    assert_eq!(unq("\"\\777\"").as_deref(), None); // 0xFF alone is no UTF-8
    assert_eq!(unq("\"\\18\"").as_deref(), Some("\u{1}8"));
    assert_eq!(unq("\"\\8\"").as_deref(), None);
}

#[test]
fn escapes_are_bytes_decoded_once() {
    assert_eq!(unq("\"\\303\\251\"").as_deref(), Some("\u{e9}"));
    assert_eq!(unq("\"\\303\"").as_deref(), None);
    // A surrogate's UTF-8 spelling is refused as Python's decoder does.
    assert_eq!(unq("\"\\355\\240\\200\"").as_deref(), None);
    assert_eq!(unq("\"\\300\\200\"").as_deref(), None);
    assert_eq!(unq("\"\\e\"").as_deref(), None);
}

#[test]
fn quotes_must_wrap_exactly() {
    assert_eq!(unq("\"").as_deref(), None);
    assert_eq!(unq("\"\"").as_deref(), Some(""));
    assert_eq!(unq("\"a\" \"b\"").as_deref(), None);
    assert_eq!(unq("\"x\\\"").as_deref(), None);
}

#[test]
fn a_lone_surrogate_raises_only_when_reached() {
    let mut text = cps("\"a");
    text.push(0xD800);
    text.extend(cps("\\q\""));
    assert_eq!(unquote_c_path(&text), Err(Raised::UnicodeEncodeError));
    let mut text = cps("\"\\q");
    text.push(0xD800);
    text.push(u32::from(b'"'));
    assert_eq!(unquote_c_path(&text), Ok(None));
}

#[test]
fn a_header_path_names_no_line_break() {
    assert_eq!(quoted_header_path(&cps("\"a/\\n\"")), Ok(None));
    assert_eq!(quoted_header_path(&cps("\"b/\\r\"")), Ok(None));
    assert_eq!(
        quoted_header_path(&cps("\"a/\\t\""))
            .unwrap()
            .map(|t| s(&t)),
        Some("a/\t".into())
    );
}

#[test]
fn diff_git_splits_at_the_first_working_quote_space_quote() {
    let line = cps("diff --git \"a/\\\" \\\"\" \"b/\\\" \\\"\"");
    assert_eq!(
        dequote_named_header(&line).unwrap().map(|t| s(&t)),
        Some("diff --git a/\" \" b/\" \"".into())
    );
    let line = cps("diff --git \"a/x\" \"b/y\"");
    assert_eq!(dequote_named_header(&line), Ok(None));
    let line = cps("diff --git \"a/x\" \"b/x\" ");
    assert_eq!(dequote_named_header(&line), Ok(None));
    let line = cps("rename from \"a\\tb\"");
    assert_eq!(
        dequote_named_header(&line).unwrap().map(|t| s(&t)),
        Some("rename from a\tb".into())
    );
    assert_eq!(dequote_named_header(&cps("rename  from \"x\"")), Ok(None));
}

#[test]
fn the_last_split_decodes_the_old_side_first() {
    // The reference decodes the old side before it looks at the new one,
    // so a lone surrogate there raises even on the final split.
    let mut line = cps("diff --git \"");
    line.push(0xD800);
    line.extend(cps("\" \""));
    assert_eq!(dequote_named_header(&line), Err(Raised::UnicodeEncodeError));
    let mut stream = cps("--- x\n");
    stream.extend(line);
    assert_eq!(
        dequote_patch_headers(&stream),
        Err(Raised::UnicodeEncodeError)
    );
}

#[test]
fn a_hunk_count_of_zero_is_zero_and_a_missing_one_is_one() {
    assert_eq!(hunk_lines(&cps("@@ -1,0 +1,0 @@")), Ok(Some("0".into())));
    assert_eq!(hunk_lines(&cps("@@ -1 +1 @@")), Ok(Some("2".into())));
    assert_eq!(hunk_lines(&cps("@@ -1, +1 @@")), Ok(None));
    assert_eq!(hunk_lines(&cps("@@ -1 +1 @@@")), Ok(Some("2".into())));
    assert_eq!(
        hunk_lines(&cps("@@ -1,\u{663} +1,\u{662} @@")),
        Ok(Some("5".into()))
    );
    assert_eq!(hunk_lines(&cps("@@ -1,\u{b2} +1 @@")), Ok(None));
    let long = format!("@@ -1,{} +1 @@", "1".repeat(4301));
    assert_eq!(hunk_lines(&cps(&long)), Err(Raised::ValueError));
    let edge = format!("@@ -1,{} +1 @@", "1".repeat(4300));
    assert!(hunk_lines(&cps(&edge)).is_ok());
}

#[test]
fn a_hunks_lines_are_read_past_and_a_marker_does_not_count() {
    // Four lines: the two removals and the two headers read as lines.
    let stream = "@@ -1,3 +1 @@\n-a\n\\ No newline\n-b\n--- \"a/s\"\n+++ \"b/s\"\n";
    let out = s(&dequote_patch_headers(&cps(stream)).unwrap());
    assert_eq!(out, stream);
    let stream = "@@ -1,2 +1,0 @@\n-a\n\\ No newline\n-b\n--- \"a/s\"\n+++ \"b/s\"\n";
    let out = s(&dequote_patch_headers(&cps(stream)).unwrap());
    assert_eq!(
        out,
        "@@ -1,2 +1,0 @@\n-a\n\\ No newline\n-b\n--- a/s\n+++ b/s\n"
    );
    let stream = "@@ -1,3 +1 @@\n-a\n context\n--- \"a/s\"\n+++ \"b/s\"\n";
    let out = s(&dequote_patch_headers(&cps(stream)).unwrap());
    assert_eq!(out, "@@ -1,3 +1 @@\n-a\n context\n--- a/s\n+++ b/s\n");
}

#[test]
fn one_label_tab_is_dropped_and_the_side_must_match() {
    let out = s(&dequote_patch_headers(&cps("--- \"a/x\"\t\n+++ \"b/x\" \t\n")).unwrap());
    assert_eq!(out, "--- a/x\n+++ \"b/x\" \t\n");
    let out = s(&dequote_patch_headers(&cps("--- \"b/x\"\n+++ \"a/x\"\n")).unwrap());
    assert_eq!(out, "--- \"b/x\"\n+++ \"a/x\"\n");
}

#[test]
fn varint_bounds() {
    assert_eq!(decode_varint(&[0x80, 0x00], 0, true), Ok((128, 2)));
    let mut big = vec![0xFF; 8];
    big.push(0x7F);
    assert_eq!(
        decode_varint(&big, 0, true),
        Ok((9_295_997_013_522_923_647, 9))
    );
    let mut edge = vec![0x80; 9];
    edge.push(0x00);
    assert_eq!(
        decode_varint(&edge, 0, true),
        Ok((9_295_997_013_522_923_648, 10))
    );
    let mut over = vec![0x80; 10];
    over.push(0x00);
    assert_eq!(decode_varint(&over, 0, true), Err(IndexFault::Unsettled));
    assert_eq!(decode_varint(&[0x80], 0, false), Err(IndexFault::Short));
    assert_eq!(decode_varint(&[0x80], 0, true), Err(IndexFault::Unsettled));
}

fn entry(name: &[u8], flags: u16) -> Vec<u8> {
    let mut out = vec![0u8; 24];
    out.extend(0o100644u32.to_be_bytes());
    out.extend([0u8; 12]);
    out.extend([0x11u8; 20]);
    out.extend(flags.to_be_bytes());
    out.extend(name);
    let pad = (62 + name.len() + 8) & !7;
    out.resize(pad, 0);
    out
}

fn index(version: u32, entries: &[Vec<u8>]) -> Vec<u8> {
    let mut out = b"DIRC".to_vec();
    out.extend(version.to_be_bytes());
    out.extend((entries.len() as u32).to_be_bytes());
    for e in entries {
        out.extend(e);
    }
    out
}

#[test]
fn the_scan_stops_after_the_name_and_reads_stages() {
    let data = index(2, &[entry(b".gitmodules", 0x200b), entry(b"z", 1)]);
    let found = gitmodules_entries(&data, true).unwrap();
    assert_eq!(found, vec![(2, 0o100644, vec![0x11; 20])]);
    // A name sorting before it keeps the scan going; one after stops it.
    let data = index(2, &[entry(b"z", 1), entry(b".gitmodules", 11)]);
    assert_eq!(gitmodules_entries(&data, true), Ok(vec![]));
    // A truncated prefix is short only while incomplete.
    let data = index(2, &[entry(b".gitmodules", 11)]);
    assert_eq!(
        gitmodules_entries(&data[..70], false),
        Err(IndexFault::Short)
    );
    assert_eq!(
        gitmodules_entries(&data[..70], true),
        Err(IndexFault::Unsettled)
    );
    // Version 2 with the extended flag is refused.
    assert_eq!(
        gitmodules_entries(&index(2, &[entry(b".gitmodules", 0x400b)]), true),
        Err(IndexFault::Unsettled)
    );
    // A 0xFFF length is found by its NUL, even when shorter.
    let data = index(2, &[entry(b".gitmodules", 0x0FFF)]);
    assert_eq!(gitmodules_entries(&data, true).unwrap().len(), 1);
}

#[test]
fn shortening_refs_order_and_dedupe() {
    let got: Vec<String> = shortening_refs(&cps("refs/heads/main"))
        .iter()
        .map(|t| s(t))
        .collect();
    assert_eq!(
        got,
        [
            "main",
            "refs/main",
            "refs/tags/main",
            "refs/remotes/main",
            "refs/remotes/main/HEAD",
            "heads/main",
            "refs/tags/heads/main",
            "refs/heads/heads/main",
            "refs/remotes/heads/main",
            "refs/remotes/heads/main/HEAD",
        ]
    );
    let got: Vec<String> = shortening_refs(&cps("refs/remotes/x/HEAD"))
        .iter()
        .map(|t| s(t))
        .collect();
    assert_eq!(got[0], "x");
    // The ref itself is not among them; its spelling under the last rule is.
    assert!(!got.contains(&"refs/remotes/x/HEAD".to_string()));
    assert_eq!(got.len(), 15);
    assert!(got.contains(&"x/HEAD".to_string()));
    // An empty short name under one rule is no match; `refs/` still is.
    let got = shortening_refs(&cps("refs/heads/"));
    assert_eq!(got.len(), 5);
    assert_eq!(s(&got[0]), "heads/");
    assert_eq!(shortening_refs(&cps("refs/remotes//HEAD")).len(), 10);
}
