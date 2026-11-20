//! Cases a plausible wrong reading of `durability.py`'s unit 1 answers
//! differently from the reference (each answer is the reference's, run).

use bettermemory_core::durability as d;
use bettermemory_core::unicode::cps;

fn s(t: &[u32]) -> String {
    t.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

#[test]
fn the_sha_fold_takes_one_trailing_newline_and_only_lowercase_hex() {
    // `$` in `re.match` also matches before a final newline.
    assert_eq!(
        s(&d::canonical_marker(&cps("sha:abcdef0\n"))),
        "sha:<commit>"
    );
    assert_eq!(
        s(&d::canonical_marker(&cps("sha:abcdef0\n\n"))),
        "sha:abcdef0\n\n"
    );
    assert_eq!(s(&d::canonical_marker(&cps("sha:abcdeF0"))), "sha:abcdeF0");
    assert_eq!(s(&d::canonical_marker(&cps("sha:0000000"))), "sha:<commit>");
    assert_eq!(
        s(&d::canonical_marker(&cps("sha:abcdef01"))),
        "sha:abcdef01"
    );
}

#[test]
fn quoted_spans_pair_left_to_right_and_stop_at_a_line_break() {
    assert_eq!(
        d::quoted_spans(&cps("odd \" one \"two\" three")),
        vec![(4, 11)]
    );
    assert_eq!(d::quoted_spans(&cps("\"open\n\"closed\"")), vec![(6, 14)]);
    // A second opening curly quote ends the attempt; no nesting.
    assert_eq!(
        d::quoted_spans(&cps("\u{201c}now\u{201c} later\u{201d}")),
        vec![(4, 12)]
    );
    // Curly quotes are directional: a closing one opens nothing.
    assert!(d::quoted_spans(&cps("\u{201d}back\u{201c}")).is_empty());
    assert!(d::in_quoted_span(&[(0, 5)], 0, 5));
    assert!(!d::in_quoted_span(&[(0, 3), (3, 6)], 2, 4));
}

#[test]
fn a_dot_just_before_the_position_always_ends_a_sentence() {
    // `finditer(text, 0, pos)` hides the digit after the dot.
    assert_eq!(s(&d::sentence_around(&cps("3.13"), 2)), "13");
    assert_eq!(s(&d::sentence_around(&cps("3.13"), 3)), "3.13");
    assert_eq!(s(&d::sentence_around(&cps("x9.9y"), 3)), "9y");
    assert_eq!(s(&d::sentence_around(&cps("It ships. Then"), 12)), " Then");
    assert_eq!(s(&d::sentence_around(&cps("a.b"), 99)), "b");
}

#[test]
fn sentence_start_reads_dashes_and_symbols_but_not_arrows() {
    assert_eq!(
        d::at_sentence_start(&cps("x \u{2014} Today"), 4),
        Some(true)
    );
    assert_eq!(d::at_sentence_start(&cps("\u{1f680} Today"), 2), Some(true));
    assert_eq!(d::at_sentence_start(&cps("\u{2192} Today"), 2), Some(false));
    assert_eq!(d::at_sentence_start(&cps("[Today"), 1), Some(true));
    assert_eq!(d::at_sentence_start(&cps("ab"), 3), None);
    assert_eq!(d::at_sentence_start(&cps("ab"), 2), Some(false));
}

#[test]
fn titlecase_names_skip_only_as_the_reference_does() {
    let t = |x: &str, a, b| d::is_titlecase_name(&cps(x), a, b);
    assert_eq!(t("Today we ship", 0, 5), Some(false));
    assert_eq!(t("Today We Ship", 0, 5), Some(true));
    assert_eq!(t("Today I broke it", 0, 5), Some(false));
    assert_eq!(t("TODAY Night", 0, 5), Some(false));
    assert_eq!(t("a USA Today column", 6, 11), Some(true));
    assert_eq!(t("This Week in Rust", 0, 9), Some(true));
    assert_eq!(t("Today \u{2160}\u{2161}", 0, 5), Some(false));
    // A circled capital is upper but not `\w`: no word follows.
    assert_eq!(t("Today \u{24b6}b", 0, 5), Some(false));
    assert_eq!(t("Today \u{1c5}ungla", 0, 5), Some(true));
    assert_eq!(t("\u{1c5}ungla", 0, 6), Some(false));
    assert_eq!(t("   ", 0, 3), None);
}

#[test]
fn the_snippet_marks_each_cut_side() {
    let text = format!("{}today{}", "x".repeat(20), "y".repeat(20));
    assert_eq!(s(&d::snippet_around(&cps(&text), 20, 25)), text);
    let text = format!("{}today{}", "x".repeat(21), "y".repeat(21));
    let got = s(&d::snippet_around(&cps(&text), 21, 26));
    assert!(got.starts_with("...x") && got.ends_with("y..."));
    assert_eq!(s(&d::snippet_around(&cps("\n\n  hit  \n\n"), 4, 7)), "hit");
    assert_eq!(
        s(&d::snippet_around(&cps(&"a".repeat(50)), 100, 120)),
        "..."
    );
}

#[test]
fn a_tab_before_the_word_is_skipped_like_a_space() {
    assert_eq!(d::at_sentence_start(&cps("\tToday"), 1), Some(true));
    assert_eq!(d::at_sentence_start(&cps("x\tToday"), 2), Some(false));
}

#[test]
fn a_follower_needs_whitespace_after_the_match() {
    let t = |x: &str| d::is_titlecase_name(&cps(x), 0, 5);
    assert_eq!(t("TodayNight Ab"), Some(false));
    assert_eq!(t("Today Night Ab"), Some(true));
    assert_eq!(t("Today\tNight"), Some(true));
}

#[test]
fn every_other_uppercase_range_reads_as_upper_at_both_ends() {
    // `(chr(c) + "a").istitle()` in the reference: true at each range's
    // first and last code point, false just outside it. Not gated on the
    // Unicode version, so the hard-coded ranges are checked on every runner.
    let ranges = [
        (0x2160, 0x216F),
        (0x24B6, 0x24CF),
        (0x1F130, 0x1F149),
        (0x1F150, 0x1F169),
        (0x1F170, 0x1F189),
    ];
    for (a, b) in ranges {
        for (cp, want) in [(a - 1, false), (a, true), (b, true), (b + 1, false)] {
            assert_eq!(d::istitle(&[cp, 0x61]), want, "{cp:#X}");
        }
    }
}
