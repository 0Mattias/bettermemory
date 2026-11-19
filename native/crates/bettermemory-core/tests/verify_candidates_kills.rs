//! Inputs a plausible wrong reading of `verify_candidates`' candidate
//! validation answers otherwise than the reference. Each expected answer
//! was read from the reference (CPython 3.13.13).

use bettermemory_core::verify_candidates::validate::{
    self, bounded_scan_text, escapes_root, line_suffix, normalize_candidate, IndexError,
};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn norm(s: &str) -> Result<Option<String>, IndexError> {
    normalize_candidate(&cps(s))
        .map(|o| o.map(|v| v.iter().map(|&c| char::from_u32(c).unwrap()).collect()))
}

fn same(s: &str) {
    assert_eq!(norm(s), Ok(Some(s.to_string())), "{s:?} kept as spelled");
}

fn none(s: &str) {
    assert_eq!(norm(s), Ok(None), "{s:?} refused");
}

/// `$` matches before a newline that ends the text, so the line suffix is
/// cut even then (a reader taking `$` as the end alone keeps the colon).
#[test]
fn the_end_anchor_admits_a_final_newline() {
    assert_eq!(norm("/a/foo.py:1\n."), Ok(Some("/a/foo.py".into())));
    assert_eq!(norm("/a/foo.py:1\n"), Ok(Some("/a/foo.py".into())));
    assert!(line_suffix(&cps("/a/b:1\n\n")).is_none());
    assert_eq!(line_suffix(&cps("a/b:1:2\n")), Some(&cps("a/b")[..]));
}

/// `.` does not match a newline; `[^\\/:]` does.
#[test]
fn the_newline_reads_differently_on_each_side_of_the_separator() {
    same("/a\n/b:1");
    assert!(line_suffix(&cps("a\n/b:1")).is_none());
    assert_eq!(line_suffix(&cps("/a/b\nc:1")), Some(&cps("/a/b\nc")[..]));
}

/// `\d` is Unicode decimal digits, not ASCII.
#[test]
fn line_numbers_are_unicode_digits() {
    assert_eq!(norm("/a/b:\u{661}\u{662}"), Ok(Some("/a/b".into())));
    assert!(line_suffix(&cps("/a/b:\u{b2}")).is_none());
}

/// A text that trims to whitespace alone raises, as `parts[0]` does.
#[test]
fn a_whitespace_candidate_raises() {
    assert_eq!(norm("\\ \\ ."), Err(IndexError));
    assert_eq!(validate::command_shaped(&cps("  ")), Err(IndexError));
}

/// Only a space or a tab makes a candidate spaced; any whitespace splits.
#[test]
fn a_no_break_space_is_no_space_for_the_command_rule() {
    none("/a/b\u{a0}x c/d");
    same("/a/b\u{a0}x/c");
}

/// A balanced `)` stays; one more is cut.
#[test]
fn closing_parentheses_are_cut_while_unbalanced() {
    assert_eq!(norm("/a/b (x86))"), Ok(Some("/a/b (x86)".into())));
    same("/a/b (x86)");
}

/// `str.isalpha` reads `Nl` as no letter; `str.islower` reads
/// `Other_Lowercase` as lowercase and a titlecase letter as not.
#[test]
fn the_unicode_predicates_are_pythons() {
    none("\u{2160}:\\x");
    same("\u{e9}:/x");
    none("/a/b/c \u{aa}x");
    same("/a/b/c \u{1c5}x");
    same("/a/b/c \u{2170}x");
}

/// The route names compare after `str.lower`, the whole name.
#[test]
fn well_known_routes_compare_lowered() {
    none("/ROBOTS.TXT");
    same("/x/ROBOTS.TXT");
    same("/\u{130}NDEX.HTML");
    same("/\u{212a}ads.txt");
}

/// One extension comes off before the placeholder test, no more.
#[test]
fn placeholders_drop_one_extension() {
    none("/etc/foo.conf");
    same("/etc/foo.bar.baz");
    none("/path/to.d");
}

/// The `@` rule needs the `@` before the first slash and the first colon.
#[test]
fn the_remote_rule_reads_first_positions() {
    none("a@b/c:d");
    same("/a@b:c/d");
}

/// The home prefix needs its slash; trailing slashes go down to one code
/// point, which a one-segment candidate then fails.
#[test]
fn home_spellings_and_slashes() {
    none("$HOME");
    assert_eq!(norm("$HOME/x"), Ok(Some("~/x".into())));
    none("/tmp///");
}

/// A `..` that climbs out is caught even when a later segment comes back.
#[test]
fn escaping_reads_the_whole_walk() {
    assert!(escapes_root(&cps("a/../../x")));
    assert!(!escapes_root(&cps("a/b/../../c")));
    assert!(escapes_root(&cps("x\\..\\..")));
    assert!(escapes_root(&cps("c:x")));
    assert!(!escapes_root(&cps("\u{e9}:x")));
}

/// Whitespace at the first code point keeps the hard cut.
#[test]
fn the_scan_bound_ignores_whitespace_at_zero() {
    let mut body = cps(" ");
    body.extend(std::iter::repeat_n(u32::from('a'), 40_000));
    assert_eq!(bounded_scan_text(&body).len(), 32_768);
    let mut body = cps("a ");
    body.extend(std::iter::repeat_n(u32::from('a'), 40_000));
    assert_eq!(bounded_scan_text(&body).len(), 1);
    let mut body = cps("a\r");
    body.extend(std::iter::repeat_n(u32::from('a'), 40_000));
    assert_eq!(bounded_scan_text(&body).len(), 32_768);
}

/// The route test reads `s[1:]` whatever `s[0]` is.
#[test]
fn single_segment_reads_from_the_second_code_point() {
    assert!(validate::is_single_segment_routelike(&cps("a/b")));
    assert!(!validate::is_single_segment_routelike(&cps("/")));
    assert!(!validate::is_single_segment_routelike(&cps("/a.b")));
}

/// A multi-character altsep that overlaps itself: after a partial match
/// fails, the search resumes from the longest proper border of the matched
/// prefix (a search that restarts from nothing misses each of these).
#[test]
fn fold_altsep_finds_a_needle_that_overlaps_itself() {
    let fold = |p: &str, sep: &str, alt: &str| -> String {
        validate::fold_altsep(&cps(p), &cps(sep), Some(&cps(alt)))
            .iter()
            .map(|&c| char::from_u32(c).unwrap())
            .collect()
    };
    assert_eq!(fold("aab", "/", "ab"), "a/");
    assert_eq!(fold("aaab", "/", "aab"), "a/");
    assert_eq!(fold("abababc", "/", "ababc"), "ab/");
    assert_eq!(fold("aabaabaaab", "-", "aabaaab"), "aab-");
}
