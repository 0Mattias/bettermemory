//! Inputs a plausible wrong port of `models_text` answers otherwise. Each
//! expected value is the reference's (CPython 3.13.13).

use bettermemory_core::datetime::{year_field, DateTime, STRFTIME_PADS_YEAR};
use bettermemory_core::models_text::{
    build_filename, first_summary_line, looks_truncated, make_slug, snippet_window,
    truncate_at_word, word_ending_at,
};
use bettermemory_core::unicode::cps;

fn slug(t: &str) -> String {
    String::from_iter(
        make_slug(&cps(t), 6, 60)
            .into_iter()
            .map(|c| char::from_u32(c).unwrap()),
    )
}

fn s(v: Vec<u32>) -> String {
    v.into_iter().map(|c| char::from_u32(c).unwrap()).collect()
}

#[test]
fn the_date_is_stripped_only_at_the_start() {
    assert_eq!(slug("x 2026-05-07 notes"), "x-2026-05-07-notes");
}

#[test]
fn the_trailing_class_reads_unicode_space() {
    assert_eq!(slug("2026-05-07\u{3000}notes"), "notes");
}

#[test]
fn a_short_zone_is_not_consumed() {
    assert_eq!(slug("2026-05-07T10:30+02:0 notes"), "02-0-notes");
    assert_eq!(slug("2026-05-07T10:30+0200 notes"), "notes");
}

#[test]
fn the_date_digits_are_unicode_decimals() {
    assert_eq!(slug("2024-01-0\u{663} x"), "x");
}

#[test]
fn a_file_separator_ends_the_first_line() {
    assert_eq!(slug("one\u{1c}two"), "one");
}

#[test]
fn a_negative_word_bound_slices_from_the_end() {
    assert_eq!(s(make_slug(&cps("ab cd ef gh"), -1, 60)), "ab-cd-ef");
}

#[test]
fn no_whitespace_under_forty_drops_one_more_character() {
    assert_eq!(s(truncate_at_word(&cps("abcdef"), 3)), "ab...");
}

#[test]
fn the_leading_ellipsis_is_charged_to_the_budget() {
    assert_eq!(s(snippet_window(&cps("ab cd ef gh ij"), 3, 8)), "...cd...");
}

#[test]
fn abbreviations_are_case_insensitive_and_need_a_space_after() {
    assert_eq!(
        s(first_summary_line(&cps("E.G. upper works. Next."), 80)),
        "E.G. upper works"
    );
    assert_eq!(
        s(first_summary_line(&cps("user.name is set. Then"), 80)),
        "user.name is set"
    );
}

#[test]
fn a_long_first_sentence_stops_the_scan() {
    let t = format!("{}. short. ", "x".repeat(90));
    assert_eq!(
        s(first_summary_line(&cps(&t), 80)),
        format!("{}...", "x".repeat(80))
    );
}

#[test]
fn a_superscript_is_alnum_so_the_run_is_not_an_abbreviation() {
    assert_eq!(
        s(first_summary_line(&cps("vs\u{b2}. Next"), 80)),
        "vs\u{b2}"
    );
}

#[test]
fn the_guillemet_is_terminal_and_the_curly_quote_is_not() {
    assert!(!looks_truncated(&cps("a\u{bb}")));
    assert!(looks_truncated(&cps("a\u{201d}")));
}

#[test]
fn an_end_past_the_text_raises() {
    assert_eq!(word_ending_at(&cps("abc"), 4), None);
    assert_eq!(word_ending_at(&cps("abc"), -1), Some(vec![]));
}

#[test]
fn only_a_full_stop_is_read_as_an_abbreviation() {
    assert_eq!(s(first_summary_line(&cps("No! next"), 80)), "No");
    assert_eq!(s(first_summary_line(&cps("e.g? next"), 80)), "e.g");
}

#[test]
fn the_year_is_spelt_padded_or_not_as_the_c_library_writes_it() {
    for (year, unpadded, padded) in [
        (1, "1", "0001"),
        (9, "9", "0009"),
        (99, "99", "0099"),
        (999, "999", "0999"),
        (1000, "1000", "1000"),
        (9999, "9999", "9999"),
    ] {
        assert_eq!(year_field(year, false), unpadded);
        assert_eq!(year_field(year, true), padded);
    }
}

#[test]
fn build_filename_spells_the_year_as_this_targets_c_library() {
    assert_eq!(
        STRFTIME_PADS_YEAR,
        !cfg!(all(target_os = "linux", target_env = "gnu"))
    );
    let created = DateTime {
        year: 999,
        month: 1,
        day: 2,
        hour: 0,
        minute: 0,
        second: 0,
        microsecond: 0,
        offset_us: None,
    };
    let want = format!("{}-01-02-x.md", year_field(999, STRFTIME_PADS_YEAR));
    assert_eq!(s(build_filename(&created, &cps("x"))), want);
    assert_eq!(
        created.strftime("%Y-%m-%d").expect("ported directives"),
        want[..want.len() - 5]
    );
}
