//! Cases a plausible wrong reading of the capture rules fails, each with
//! the answer CPython 3.13.13 gives (`durability.find_capture_rule_hits`).
//!
//! `_first_unquoted`'s `break` cannot be told apart by dropping it alone:
//! `finditer` hands a pattern's matches in rising order of start, so a
//! later kept match never has a smaller start than the first kept one.
//! What a wrong port gets wrong is where the break sits: breaking at a
//! pattern's first match whether or not it is quoted or kept loses the
//! later match the reference keeps. Those cases are here.

use bettermemory_core::durability::{
    find_capture_rule_hits, unanchored_test_total, CaptureRuleHit, CAPTURE_PATTERNS,
};
use bettermemory_core::pyre::regex::Regex;
use bettermemory_core::unicode::{cps, to_string};

fn hits(body: &str) -> Vec<(String, String)> {
    find_capture_rule_hits(&cps(body))
        .expect("every pattern builds")
        .iter()
        .map(|CaptureRuleHit { name, snippet, .. }| {
            ((*name).to_owned(), to_string(snippet).unwrap())
        })
        .collect()
}

fn one(name: &str, snippet: &str) -> Vec<(String, String)> {
    vec![(name.to_owned(), snippet.to_owned())]
}

#[test]
fn every_pattern_builds_on_the_engine() {
    for &(id, text, flags) in &CAPTURE_PATTERNS {
        assert!(Regex::new_str(text, flags).is_ok(), "{id}");
    }
}

/// The break after the first kept match, not after the first match: a
/// quoted first match or one `keep` refuses lets the pattern's next match
/// through.
#[test]
fn a_skipped_first_match_lets_the_next_one_through() {
    assert_eq!(
        hits("\"The user's view\" is quoted. The user's laptop"),
        one("plain_subject", "...'s view\" is quoted. The user's laptop")
    );
    assert_eq!(
        hits("We saw The user's project. The user's laptop"),
        one("plain_subject", "...The user's project. The user's laptop")
    );
    assert_eq!(
        hits("The repo had 1.2k GitHub stars on 2025-01-01. It has 30 forks."),
        one("drifting_count", "...2025-01-01. It has 30 forks.")
    );
    assert_eq!(
        hits("CI at abc1234: 5 passed. CI: 7 failed."),
        one("test_total", "...1234: 5 passed. CI: 7 failed.")
    );
    assert_eq!(
        hits("\"recently\" and recently"),
        one("absolute_date", "\"recently\" and recently")
    );
}

/// Two patterns of one rule at one start: the earlier pattern wins
/// (`_TODO_HEADING_RE`'s `TODO:` ends one past the tag's `TODO`, so the
/// snippet's right edge tells them apart).
#[test]
fn a_tie_goes_to_the_earlier_pattern() {
    assert_eq!(
        hits("TODO: fix the hook test in the daemon before release"),
        one("no_todo", "TODO: fix the hook test i...")
    );
}

/// An `or` reads its second search only when the first found nothing:
/// a later opener beats an earlier assistant phrase, and a to-do pattern
/// beats the imperative at the body's start.
#[test]
fn an_or_falls_through_only_on_nothing() {
    assert_eq!(
        hits("x the assistant said so. The user's y"),
        one("plain_subject", "...assistant said so. The user's y")
    );
    assert_eq!(
        hits("Fix the hook. TODO later"),
        one("no_todo", "Fix the hook. TODO later")
    );
    assert_eq!(
        hits("Fix the hook test in the daemon before. TODO later"),
        one("no_todo", "...the daemon before. TODO later")
    );
}

/// IGNORECASE is `re`'s Unicode folding of one code point against one:
/// the long s and the Kelvin sign fold, `ß` is not `ss`.
#[test]
fn ignorecase_folds_past_ascii_one_code_point_at_a_time() {
    assert_eq!(
        hits("the assi\u{17f}tant said so"),
        one("plain_subject", "the assi\u{17f}tant said so")
    );
    assert_eq!(
        hits("THE ASS\u{130}STANT SAID so"),
        one("plain_subject", "THE ASS\u{130}STANT SAID so")
    );
    assert_eq!(
        hits("Chec\u{212a} the hook"),
        one("no_todo", "Chec\u{212a} the hook")
    );
    assert_eq!(hits("the a\u{df}istant said so"), vec![]);
    // `TODO` has no IGNORECASE.
    assert_eq!(hits("the todo tag"), vec![]);
}

/// The fixed-width lookbehinds, read case-insensitively on the text as
/// written, and the lookaheads after `recently` and `now`.
#[test]
fn the_lookarounds_hold() {
    for body in [
        "the least recently, x",
        "the most-recently, x",
        "LEAST RECENTLY, x",
        "a recently used cache",
        "what is now done",
        "WHAT IS NOW done",
        "it is now known as X",
    ] {
        assert_eq!(hits(body), vec![], "{body}");
    }
    assert_eq!(
        hits("the least  recently, x"),
        one("absolute_date", "the least recently, x")
    );
    assert_eq!(
        hits("what  is now done"),
        one("absolute_date", "what is now done")
    );
}

/// `drifting_count` keeps a count whose sentence has no ISO date;
/// `test_total` one whose sentence has a suite word and neither a date
/// nor a commit hash.
#[test]
fn the_keep_predicates_are_not_swapped() {
    assert_eq!(
        hits("It has 5 stars at abc1234."),
        one("drifting_count", "It has 5 stars at abc1234.")
    );
    assert_eq!(hits("5 passed at abc1234 in CI"), vec![]);
    assert_eq!(hits("5 passed today"), vec![]);
    assert_eq!(hits("It has 5 stars, see the tests"), {
        one("drifting_count", "It has 5 stars, see the tests")
    });
    assert!(!unanchored_test_total(&cps("5 passed")).unwrap());
    assert!(unanchored_test_total(&cps("Suite x")).unwrap());
}

/// The opener counts only at a sentence start.
#[test]
fn the_opener_is_kept_only_at_a_sentence_start() {
    assert_eq!(hits("We saw The user's project"), vec![]);
    assert_eq!(
        hits("Notes:  \t The user's project"),
        one("plain_subject", "Notes: The user's project")
    );
    assert_eq!(hits("x:\u{a0}The user's project"), vec![]);
}

/// A commit hash is a run of 7 to 40 lowercase hex digits holding both a
/// letter and a digit.
#[test]
fn a_hash_needs_a_letter_and_a_digit() {
    for (body, total) in [
        ("Tests: 1024 passed at 1234567.", true),
        ("Tests: 1024 passed at abcdefa.", true),
        ("Tests: 1024 passed at deadbeef.", true),
        ("Tests: 1024 passed at ABC1234.", true),
        ("Tests: 1024 passed at abc123.", true),
        ("Tests: 1024 passed at deadbee1.", false),
        ("Tests: 1024 passed at abc1234.", false),
    ] {
        assert_eq!(!hits(body).is_empty(), total, "{body}");
    }
    let forty = format!("Tests: 5 failed at {}.", "a1".repeat(20));
    assert_eq!(hits(&forty), vec![]);
    let forty_one = format!("Tests: 5 failed at {}b.", "a1".repeat(20));
    assert_eq!(hits(&forty_one).len(), 1);
}

/// `\A` is the body's start only; `^` under MULTILINE follows a newline.
#[test]
fn the_anchors_hold() {
    assert_eq!(hits("x\nFix the hook"), vec![]);
    assert_eq!(
        hits("x\n- [ ] write the docs"),
        one("no_todo", "x - [ ] write the docs")
    );
    assert_eq!(hits("x - [ ] write the docs"), vec![]);
    assert_eq!(
        hits("Intro line\nAction items: ship it"),
        one("no_todo", "Intro line Action items: ship it")
    );
    assert_eq!(hits("Intro line Action items: ship it"), vec![]);
}

/// The quoted test on spans in text order: a curly span closed by its
/// second opener is no span, so the match after it counts.
#[test]
fn the_quoted_test_reads_every_span() {
    assert_eq!(
        hits("\u{201c}recently\u{201c} recently\u{201d}"),
        one("absolute_date", "\u{201c}recently\u{201c} recently\u{201d}")
    );
    let many = "\"x\" ".repeat(50) + "\"recently\" " + &"\"y\" ".repeat(50);
    assert_eq!(hits(&many), vec![]);
    let after = many.clone() + "recently";
    assert_eq!(hits(&after).len(), 1);
}
