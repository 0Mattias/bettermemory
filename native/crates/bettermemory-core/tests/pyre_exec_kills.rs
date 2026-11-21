//! Cases a plausible wrong matcher fails, each with the answer CPython
//! 3.13.13's `re` gives (checked there; the reversed windows are decision
//! section 3, item 1).

mod pyre_exec_support;
mod pyre_lower_support;

use bettermemory_core::pyre::exec::{exec, supports, MatchError, Mode, Unsupported};
use pyre_exec_support::{compile, run, span, Naive};

const M: u32 = 8;
const S: u32 = 16;
use Mode::{Fullmatch, Match, Search};

#[test]
fn a_repeat_of_one_unit_fails_on_a_reversed_window_even_for_zero() {
    // REPEAT_ONE tests its minimum against a negative room.
    assert_eq!(span("a*", 0, "ab\nc", 2, 1, Match), None);
    assert_eq!(span("(?i:a)*", 0, "ab\nc", 2, 1, Match), None);
    assert_eq!(span("(?:c)?", 0, "ab\nc", 2, 1, Match), None);
    assert_eq!(span("a*?", 0, "ab\nc", 2, 1, Match), None);
}

#[test]
fn an_unrolled_repeat_matches_empty_on_a_reversed_window() {
    assert_eq!(span("(?:ab)*", 0, "ab\nc", 2, 1, Match), Some((2, 2)));
    assert_eq!(span("(c)?", 0, "ab\nc", 2, 1, Match), Some((2, 2)));
    assert_eq!(span("(?:cd)?", 0, "ab\nc", 2, 1, Match), Some((2, 2)));
    assert_eq!(span("", 0, "abc", 2, 1, Match), Some((2, 2)));
}

#[test]
fn fullmatch_and_search_find_nothing_in_a_reversed_window() {
    assert_eq!(span("", 0, "abc", 2, 1, Fullmatch), None);
    assert_eq!(span("", 0, "abc", 2, 1, Search), None);
}

#[test]
fn anchors_on_a_reversed_window_read_past_endpos() {
    assert_eq!(span("$", M, "ab\nc", 2, 1, Match), Some((2, 2)));
    assert_eq!(span(r"\b", 0, "ab\nc", 2, 1, Match), Some((2, 2)));
    // `\b` is false wherever `endpos` is 0.
    assert_eq!(span(r"\b", 0, "ab", 1, 0, Match), None);
    assert_eq!(span(r"\b", 0, "ab", 0, 0, Search), None);
}

#[test]
fn a_boundary_at_endpos_counts_the_far_side_as_not_a_word() {
    assert_eq!(span(r"a\b", 0, "ab", 0, 1, Match), Some((0, 1)));
    assert_eq!(span(r"a\b", 0, "ab", 0, 2, Match), None);
    assert_eq!(span(r"\b", 0, "ab", 1, 1, Match), Some((1, 1)));
}

#[test]
fn end_anchors_differ_on_a_final_line_feed() {
    assert_eq!(span("$", 0, "a\n", 0, 2, Search), Some((1, 1)));
    assert_eq!(span("$", 0, "a\n\n", 0, 3, Search), Some((2, 2)));
    assert_eq!(span(r"\Z", 0, "a\n", 0, 2, Search), Some((2, 2)));
    assert_eq!(span("$", M, "ab", 1, 1, Match), Some((1, 1)));
    assert_eq!(span("^", M, "a\nb", 1, 3, Search), Some((2, 2)));
    assert_eq!(span("^", 0, "a\nb", 1, 3, Search), None);
}

#[test]
fn fullmatch_backtracks_past_a_shorter_match() {
    assert_eq!(span("a|ab", 0, "ab", 0, 2, Fullmatch), Some((0, 2)));
    assert_eq!(span("a*?", 0, "aaa", 0, 3, Fullmatch), Some((0, 3)));
    assert_eq!(
        span("(?:a|ab)(?:c|bcd)", 0, "abcd", 0, 4, Fullmatch),
        Some((0, 4))
    );
    assert_eq!(span("a*", 0, "aab", 0, 3, Fullmatch), None);
    assert_eq!(span("a*", 0, "aab", 0, 2, Fullmatch), Some((0, 2)));
}

#[test]
fn the_first_start_that_matches_wins_even_empty() {
    assert_eq!(span("a*", 0, "baa", 0, 3, Search), Some((0, 0)));
    assert_eq!(span("a+", 0, "baa", 0, 3, Search), Some((1, 3)));
    // No start past endpos.
    assert_eq!(span("b", 0, "ab", 0, 1, Search), None);
}

fn marks(pattern: &str, text: &str) -> (Vec<isize>, isize) {
    let prog = compile(pattern, 0, false);
    let t: Vec<u32> = text.chars().map(u32::from).collect();
    let m = run(&prog, t.as_slice(), 0, t.len() as isize, Match).expect("a match");
    (m.marks, m.lastindex)
}

#[test]
fn a_failed_path_leaves_no_mark_and_no_lastindex() {
    assert_eq!(marks("(?:(a)x|a)(b)", "ab"), (vec![-1, -1, 1, 2], 2));
    assert_eq!(marks("(a)?(?:(b)x|b)", "ab"), (vec![0, 1, -1, -1], 1));
    assert_eq!(marks("(?:(a)x|ab)", "ab"), (vec![-1, -1], -1));
    assert_eq!(marks("(a)(b)?", "a"), (vec![0, 1, -1, -1], 1));
}

fn search_marks(pattern: &str, text: &str) -> (Option<(isize, isize)>, Vec<isize>, isize) {
    let prog = compile(pattern, 0, false);
    let t: Vec<u32> = text.chars().map(u32::from).collect();
    let m = run(&prog, t.as_slice(), 0, t.len() as isize, Search).expect("a match");
    (Some((m.start, m.end)), m.marks, m.lastindex)
}

// `_sre` resets the marks and lastindex before each start of `search`, so
// a mark written by a failed attempt at an earlier start is not reported.
#[test]
fn search_drops_the_marks_of_a_failed_earlier_start() {
    assert_eq!(
        search_marks("(.|(b))[^a]", "bab"),
        (Some((1, 3)), vec![1, 2, -1, -1], 1)
    );
    assert_eq!(
        search_marks("c|(a)b", "ac"),
        (Some((1, 2)), vec![-1, -1], -1)
    );
    assert_eq!(
        search_marks("(a)??c", "abc"),
        (Some((2, 3)), vec![-1, -1], -1)
    );
    assert_eq!(
        // A capture inside a repeat is refused (pyre-3c), so group 2 is
        // written once and the repeat follows it.
        search_marks("(?s)(?:(b+?)|([ab])[ab]*[^a]b)", "ab"),
        (Some((1, 2)), vec![1, 2, -1, -1], 1)
    );
}

#[test]
fn lastindex_is_the_group_closed_last() {
    assert_eq!(marks("((a)b)", "ab"), (vec![0, 2, 0, 1], 1));
    assert_eq!(marks("(a*)(a)", "aaa"), (vec![0, 2, 2, 3], 2));
    assert_eq!(marks("(a*?)(a)", "aaa"), (vec![0, 0, 0, 1], 2));
}

#[test]
fn lazy_and_greedy_single_unit_repeats() {
    assert_eq!(span("a*?", 0, "aaa", 0, 3, Match), Some((0, 0)));
    assert_eq!(span("a*", 0, "aaa", 0, 3, Match), Some((0, 3)));
    assert_eq!(span("a{2,}?", 0, "aaaa", 0, 4, Match), Some((0, 2)));
    assert_eq!(span("a{2,3}", 0, "aaaa", 0, 4, Match), Some((0, 3)));
    assert_eq!(span("a+?b", 0, "aaab", 0, 4, Match), Some((0, 4)));
    // A lazy repeat stops at its maximum.
    assert_eq!(span("a{1,2}?b", 0, "aaab", 0, 4, Match), None);
    assert_eq!(span("(?:ab)*?b", 0, "ababb", 0, 5, Match), Some((0, 5)));
    assert_eq!(span("(?:ab)*", 0, "ababa", 0, 5, Match), Some((0, 4)));
}

#[test]
fn any_stops_at_a_line_feed_unless_dotall() {
    assert_eq!(span(".*", 0, "a\nb", 0, 3, Match), Some((0, 1)));
    assert_eq!(span(".*", S, "a\nb", 0, 3, Match), Some((0, 3)));
}

#[test]
fn windows_are_clamped() {
    assert_eq!(span("a", 0, "ba", -5, 99, Search), Some((1, 2)));
    assert_eq!(span("", 0, "ab", 9, 9, Match), Some((2, 2)));
    assert_eq!(span("", 0, "ab", isize::MIN, isize::MAX, Fullmatch), None);
    assert_eq!(
        span("ab", 0, "ab", isize::MIN, isize::MAX, Fullmatch),
        Some((0, 2))
    );
}

#[test]
fn bytes_run_through_the_same_loop() {
    let prog = compile("a*b", 0, true);
    let m = run(&prog, b"xaab".as_slice(), 0, 4, Search).expect("a match");
    assert_eq!((m.start, m.end), (1, 4));
}

#[test]
fn a_budget_is_a_declared_refusal() {
    let prog = compile("(?:a|b)*c", 0, false);
    let t: Vec<u32> = "ab".repeat(50).chars().map(u32::from).collect();
    let got = exec(&prog, &Naive, t.as_slice(), 0, 100, Search, Some(10));
    assert_eq!(got, Err(MatchError::Budget));
    let free = exec(&prog, &Naive, t.as_slice(), 0, 100, Search, None).expect("no refusal");
    assert_eq!(free, None);
}

#[test]
fn steps_are_counted_on_every_call() {
    let prog = compile("a*b", 0, false);
    let t: Vec<u32> = "aab".chars().map(u32::from).collect();
    let m = run(&prog, t.as_slice(), 0, 3, Match).expect("a match");
    assert!(m.steps > 0);
    let budget = exec(&prog, &Naive, t.as_slice(), 0, 3, Match, Some(m.steps));
    assert_eq!(budget.expect("within budget").map(|f| f.end), Some(3));
}

#[test]
fn a_lookaround_is_refused_by_name() {
    assert_eq!(
        supports(&compile("(?=a)", 0, false)),
        Err(Unsupported::Lookaround)
    );
    assert_eq!(
        supports(&compile("(?<!a)b", 0, false)),
        Err(Unsupported::Lookaround)
    );
    assert_eq!(supports(&compile("a|(b)", 0, false)), Ok(()));
}
