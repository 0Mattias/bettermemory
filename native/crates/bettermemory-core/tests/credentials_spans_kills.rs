//! Cases a plausible wrong port of `credentials::spans` or `builder`
//! fails, each answer read from the reference (CPython 3.13.13): an
//! ASCII-only IGNORECASE or lookbehind, a merge that keeps the later kind,
//! a snippet that keeps PEM key material or collapses only ASCII space, a
//! marker list that counts a kind twice, a fold that compares whole dicts
//! loosely or appends a key dict.update keeps in place.

use bettermemory_core::builder::{fold_nullable, strip_titles};
use bettermemory_core::credentials::{find_credential_markers_str, secret_spans_str};
use bettermemory_core::json;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::unicode;

const S: &str = concat!("Xy7kQ9", "pLm2Zt");

fn markers(text: &str) -> Vec<(&'static str, String)> {
    find_credential_markers_str(text)
        .into_iter()
        .map(|m| {
            (
                m.kind,
                unicode::to_string(&m.snippet).expect("no surrogate"),
            )
        })
        .collect()
}

#[test]
fn ignorecase_reaches_past_ascii() {
    // U+017F folds to `s` and U+212A to `k`, so both keywords match.
    assert_eq!(
        secret_spans_str(&format!("pa\u{17f}sword={S}")),
        vec![(9, 21, "generic-secret-assignment")]
    );
    assert_eq!(
        secret_spans_str(&format!("api_\u{212a}ey={S}")),
        vec![(8, 20, "generic-secret-assignment")]
    );
    assert_eq!(
        secret_spans_str(&format!("PASSWORD={S}")),
        vec![(9, 21, "generic-secret-assignment")]
    );
}

#[test]
fn the_lookbehind_class_folds_too() {
    // `[A-Za-z0-9]` under IGNORECASE holds U+0130 and U+0131, so neither
    // may stand before the keyword; an ASCII letter may not either.
    assert!(secret_spans_str(&format!("\u{130}password={S}")).is_empty());
    assert!(secret_spans_str(&format!("a\u{131}password={S}")).is_empty());
    assert!(secret_spans_str(&format!("apassword={S}")).is_empty());
}

#[test]
fn the_merge_keeps_the_first_kind() {
    let text = concat!("api_key = AKIA", "0189ABCDEFGHIJKL");
    assert_eq!(secret_spans_str(text), vec![(10, 30, "aws-access-key-id")]);
    assert_eq!(
        markers(text),
        vec![
            (
                "aws-access-key-id",
                "api_key = [redacted:aws-access-key-id]".to_string()
            ),
            (
                "generic-secret-assignment",
                "api_key = [redacted:aws-access-key-id]".to_string()
            ),
        ]
    );
}

#[test]
fn snippets_collapse_unicode_space_and_cut_pem() {
    assert_eq!(
        markers(&format!("x\u{3000}\u{3000}password={S}\u{2028}tail")),
        vec![(
            "generic-secret-assignment",
            "x password=[redacted:generic-secret-assignment] tail".to_string()
        )]
    );
    assert_eq!(
        markers("key:\n-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"),
        vec![(
            "private-key-pem",
            "key: [redacted:private-key-pem]...".to_string()
        )]
    );
    let q = "q".repeat(40);
    let r = "r".repeat(40);
    assert_eq!(
        markers(&format!("{q} password={S} {r}")),
        vec![(
            "generic-secret-assignment",
            format!(
                "...{} password=[redacted:generic-secret-assignment] {}...",
                "q".repeat(14),
                "r".repeat(23)
            )
        )]
    );
}

#[test]
fn a_kind_is_reported_once() {
    let text = format!("ghp_{0}a1B2c3 github_pat_{0}", "a1B2c3D4e5".repeat(3));
    assert_eq!(
        markers(&text),
        vec![(
            "github-token",
            "[redacted:github-token] [redacted:github-token]...".to_string()
        )]
    );
}

#[test]
fn the_bounds_of_the_value_and_the_userinfo() {
    assert!(secret_spans_str("password=Ab1cdefg").is_empty());
    let user = "u".repeat(65);
    assert!(secret_spans_str(&format!("postgres://{user}:{S}@h")).is_empty());
    let user = "u".repeat(64);
    assert_eq!(
        secret_spans_str(&format!("postgres://{user}:{S}@h")),
        vec![(76, 88, "connection-uri-password")]
    );
}

fn tree(text: &str) -> PyValue {
    json::loads(&unicode::cps(text)).expect("JSON")
}

#[test]
fn the_null_branch_is_exactly_one_pair() {
    let mut t = tree(r#"{"anyOf": [{"type": "string"}, {"type": "null", "title": "N"}]}"#);
    let before = t.clone();
    fold_nullable(&mut t);
    assert_eq!(t, before);
    let mut t = tree(r#"{"anyOf": [{"type": "string"}, {"type": "NULL"}]}"#);
    let before = t.clone();
    fold_nullable(&mut t);
    assert_eq!(t, before);
}

#[test]
fn an_existing_key_keeps_its_place() {
    let mut t = tree(
        r#"{"description": "d", "anyOf": [{"description": "e", "type": "string"}, {"type": "null"}], "z": 1}"#,
    );
    fold_nullable(&mut t);
    assert_eq!(
        t,
        tree(r#"{"description": "e", "z": 1, "type": ["string", "null"]}"#)
    );
}

#[test]
fn named_maps_are_descended_by_value_only() {
    let mut t =
        tree(r#"{"properties": {"title": {"title": "T", "type": "string"}}, "title": "A"}"#);
    strip_titles(&mut t);
    assert_eq!(t, tree(r#"{"properties": {"title": {"type": "string"}}}"#));
    let mut t = tree(r#"{"properties": {"anyOf": [{"type": "string"}, {"type": "null"}]}}"#);
    let before = t.clone();
    fold_nullable(&mut t);
    assert_eq!(t, before);
    let mut t = tree(r#"{"default": null, "x": {"default": false, "d": {"default": null}}}"#);
    strip_titles(&mut t);
    assert_eq!(t, tree(r#"{"x": {"default": false, "d": {}}}"#));
}

#[test]
fn a_later_span_inside_an_earlier_one_keeps_its_end() {
    let text = concat!("password=Qz9-AKIA", "0189ABCDEFGHIJKL-tailXy7");
    assert_eq!(
        secret_spans_str(text),
        vec![(9, 41, "generic-secret-assignment")]
    );
}

#[test]
fn touching_spans_stay_apart() {
    let text = concat!(
        "xox",
        "b-1234567890-abcdefghijKL-",
        "ghp_",
        "aB3dE5gH7jK9mN1pQ3sT5vX7zA9cD1fG3hJ5"
    );
    assert_eq!(
        secret_spans_str(text),
        vec![(0, 29, "slack-token"), (29, 69, "github-token")]
    );
}

fn snippet(text: &str, start: usize, end: usize) -> String {
    let cps = unicode::cps(text);
    let out = bettermemory_core::credentials::spans::safe_snippet(&cps, start, end, "x");
    out.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

#[test]
fn a_span_at_the_window_edge_is_outside() {
    let x30 = "x".repeat(30);
    let t1 = format!("password={S} {x30}");
    assert_eq!(snippet(&t1, 45, 46), format!("...{x30}"));
    assert_eq!(
        snippet(&t1, 44, 46),
        format!("...[redacted:generic-secret-assignment] {x30}")
    );
    let y24 = "y".repeat(24);
    let t2 = format!("{y24} password={S}");
    assert_eq!(snippet(&t2, 10, 10), format!("{y24} password=..."));
    assert_eq!(
        snippet(&t2, 11, 11),
        format!("{y24} password=[redacted:generic-secret-assignment]...")
    );
}

#[test]
fn huge_snippet_bounds_clamp_without_wrapping() {
    let t = format!("abc password={S} def");
    let full = "abc password=[redacted:generic-secret-assignment] def";
    assert_eq!(snippet(&t, 0, usize::MAX), full);
    assert_eq!(snippet(&t, usize::MAX, usize::MAX), "...");
    assert_eq!(snippet(&t, 5, isize::MAX as usize - 5), full);
    assert_eq!(snippet(&t, 1 << 63, 3), "......");
}
