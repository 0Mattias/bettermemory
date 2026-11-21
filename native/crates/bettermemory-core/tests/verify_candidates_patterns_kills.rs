//! Cases a plausible wrong reading of the three patterns fails: the bare
//! boundary read as a lookbehind or `^` read as the search start, a
//! continuation that keeps a spaced word without its slash, a lowercase
//! or one-letter spaced word, a drive letter past ASCII, a backtick scan
//! that overlaps, and a domain start at a later label of a dotted run.
//! Each answer is what `re` answers under CPython 3.13.13.

use bettermemory_core::verify_candidates::patterns::{
    backtick_iter, bare_iter, domain_route_iter, Hit,
};

fn c(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn spans(hits: Vec<Hit>) -> Vec<[usize; 4]> {
    hits.iter()
        .map(|h| [h.start, h.end, h.group_start, h.group_end])
        .collect()
}

fn bare(s: &str) -> Vec<[usize; 4]> {
    spans(bare_iter(&c(s)))
}

fn domain(s: &str) -> Vec<[usize; 4]> {
    spans(domain_route_iter(&c(s)))
}

fn backtick(s: &str) -> Vec<[usize; 4]> {
    spans(backtick_iter(&c(s)))
}

#[test]
fn backtick_does_not_overlap() {
    assert_eq!(backtick("`a`b`c`"), [[0, 3, 1, 2], [4, 7, 5, 6]]);
    assert_eq!(backtick("``a``"), [[1, 4, 2, 3]]);
    // A newline before the close fails that start; the scan moves on.
    assert_eq!(backtick("`a\n`b`"), [[3, 6, 4, 5]]);
    assert!(backtick("`unclosed").is_empty());
}

#[test]
fn bare_boundary_is_consumed() {
    // The match starts at the boundary, not at the path.
    assert_eq!(bare("((/a"), [[1, 4, 2, 4]]);
    assert_eq!(bare("/a /b"), [[0, 2, 0, 2], [2, 5, 3, 5]]);
    assert!(bare("x/a").is_empty());
    // Each of the four typographic quotes opens a path.
    for q in ["\u{201c}", "\u{201d}", "\u{2018}", "\u{2019}"] {
        assert_eq!(bare(&format!("{q}/a")), [[0, 3, 1, 3]]);
    }
    // U+00A0 and U+0085 are `\s`; U+200B is not.
    assert_eq!(bare("\u{a0}/a\u{85}/b").len(), 2);
    assert!(bare("\u{200b}/a").is_empty());
}

#[test]
fn bare_caret_is_position_zero_only() {
    // `$` is not a body character: the search resumes at 2, where `^`
    // does not hold and no boundary stands.
    assert_eq!(bare("/a$HOME/x"), [[0, 2, 0, 2]]);
    assert_eq!(bare("$HOME/x"), [[0, 7, 0, 7]]);
    // `^` is tried first, then the boundary at 0.
    assert_eq!(bare(" /a"), [[0, 3, 1, 3]]);
    assert!(bare("a\n").is_empty());
}

#[test]
fn bare_continuation_gives_back() {
    assert_eq!(bare("~/a Bb Cc/d Ee"), [[0, 11, 0, 11]]);
    assert_eq!(bare("~/a Bb Cc/d Ee/f"), [[0, 16, 0, 16]]);
    assert_eq!(bare("/a Bc d/e"), [[0, 2, 0, 2]]);
    assert_eq!(bare("/a Bc/ x"), [[0, 2, 0, 2]]);
    assert_eq!(bare("~/Library/Application Support/x"), [[0, 31, 0, 31]]);
    assert_eq!(bare("~/Library/Application Support"), [[0, 21, 0, 21]]);
}

#[test]
fn bare_spaced_word_starts_uppercase_ascii_and_is_two_long() {
    assert_eq!(bare("/a bc/d"), [[0, 2, 0, 2]]);
    assert_eq!(bare("/a B/c"), [[0, 2, 0, 2]]);
    // `[` and `@` stand just past and before `[A-Z]`.
    assert_eq!(bare("/a [b/c"), [[0, 2, 0, 2]]);
    assert_eq!(bare("/a @b/c"), [[0, 2, 0, 2]]);
    assert_eq!(bare("/a Zb/c"), [[0, 7, 0, 7]]);
    // Greek capital alpha is not `[A-Z]`; the spaced word's tail is `\w`.
    assert_eq!(bare("/a \u{391}b/c"), [[0, 2, 0, 2]]);
    assert_eq!(bare("/a A\u{3b2}/c"), [[0, 7, 0, 7]]);
}

#[test]
fn bare_drive_letter_is_ascii() {
    assert_eq!(bare("C:\\x"), [[0, 4, 0, 4]]);
    assert_eq!(bare("c:/x"), [[0, 4, 0, 4]]);
    assert!(bare("\u{e9}:/x").is_empty());
    assert!(bare("\u{212a}:/x").is_empty());
    // The low byte of U+0141 and U+0161 is an ASCII letter.
    assert!(bare("\u{141}:/x").is_empty());
    assert!(bare("\u{161}:/x").is_empty());
    assert!(bare("C:x").is_empty());
    assert!(bare("C:/").is_empty());
}

#[test]
fn bare_prefixes() {
    assert_eq!(bare("${HOME}/x"), [[0, 9, 0, 9]]);
    assert!(bare("$HOME").is_empty());
    assert!(bare("~x").is_empty());
    assert!(bare("/").is_empty());
}

#[test]
fn domain_lookbehinds() {
    // The `.` at 2 follows `a.` and is refused; the start at 3 follows `..`.
    assert_eq!(domain("a..b.c/d"), [[3, 8, 7, 8]]);
    assert_eq!(domain("x.a.b/c"), [[0, 7, 6, 7]]);
    assert_eq!(domain(" .a.b/c"), [[1, 7, 6, 7]]);
    assert_eq!(domain("-a.b/c"), [[0, 6, 5, 6]]);
    assert_eq!(domain("see...example.com/x"), [[5, 19, 18, 19]]);
}

#[test]
fn domain_needs_two_labels_and_a_tail() {
    assert!(domain("a/b").is_empty());
    assert!(domain("a.b/").is_empty());
    assert!(domain("a.b./c").is_empty());
    assert_eq!(domain("a.b/."), [[0, 5, 4, 5]]);
    assert_eq!(domain("a.b/c a.b/d"), [[0, 5, 4, 5], [6, 11, 10, 11]]);
    assert_eq!(domain("a.b/c/d.e/f"), [[0, 5, 4, 5], [6, 11, 10, 11]]);
}
