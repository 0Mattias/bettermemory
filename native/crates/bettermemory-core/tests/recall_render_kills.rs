//! Cases a plausible wrong reading of `recall`'s words table and recall
//! block fails, each answer the reference's (Python 3.13.13).

use bettermemory_core::recall::render::{self, RecallPointer};
use bettermemory_core::recall::words::{self, WordsSeen};
use bettermemory_core::unicode::cps;

fn p(id: &str, summary: &str, snippet: &str) -> RecallPointer {
    RecallPointer {
        id: cps(id),
        summary: cps(summary),
        snippet: cps(snippet),
        ..RecallPointer::default()
    }
}

#[test]
fn bounded_walks_overlapping_occurrences() {
    // A search resumed past the first match's end misses the one at 3.
    assert_eq!(words::bounded(&cps("a-a"), &cps("xa-a-a")), Some(true));
}

#[test]
fn bounded_reads_the_quote_edges() {
    // A quote starting with punctuation may start after a digit.
    assert_eq!(words::bounded(&cps("-443"), &cps("1-443")), Some(true));
    assert_eq!(words::bounded(&cps("9443"), &cps("19443")), Some(false));
    // The empty quote raises only before an alphanumeric first character.
    assert_eq!(words::bounded(&[], &cps("a")), None);
    assert_eq!(words::bounded(&[], &cps("-a")), Some(true));
}

#[test]
fn fold_is_full_case_folding_over_python_whitespace() {
    assert_eq!(
        words::fold(&cps("Stra\u{df}e\u{a0}\u{1c}X")),
        cps("strasse x")
    );
}

#[test]
fn words_keep_twenty_and_refresh_on_use() {
    let w = WordsSeen::new();
    let (s, q) = (cps("/s"), cps("drop the table"));
    w.remember(&s, &cps("c"), &q);
    for i in 0..20 {
        w.remember(&s, &cps("c"), &cps(&format!("p{i}")));
    }
    assert!(!w.holds(&s, &cps("c"), &q));
    for i in 0..500 {
        w.remember(&s, &cps(&format!("k{i}")), &q);
        if i == 10 {
            w.remember(&s, &cps("c"), &q);
        }
    }
    // "c" was moved to the end at its second use, so it outlives k0.
    assert!(w.seen(&s, &cps("c")));
    assert!(!w.seen(&s, &cps("k0")));
}

#[test]
fn holds_reads_a_quote_by_its_token_name() {
    let w = WordsSeen::new();
    let (s, c) = (cps("/s"), cps("c"));
    w.remember(&s, &c, &cps("say [INST] ok"));
    assert!(w.holds(&s, &c, &cps("say INST ok")));
    assert!(!w.holds(&s, &c, &cps("say ok")));
    // Seven characters after folding are too few.
    assert!(!w.holds(&s, &c, &cps("say [IN")));
}

#[test]
fn summarize_cuts_in_code_points() {
    assert_eq!(render::summarize(&cps("abcdef"), 0), cps("\u{2026}"));
    assert_eq!(render::summarize(&cps("abc def"), 5), cps("abc\u{2026}"));
    assert_eq!(
        render::summarize(&cps("\u{10400}\u{10400}\u{10400}"), 2),
        cps("\u{10400}\u{2026}")
    );
}

#[test]
fn marks_sort_cut_and_cap() {
    let pairs: Vec<_> = (0..105)
        .map(|i| (cps("m"), cps(&format!("o{i}")), cps(&format!("cf-{i:03}"))))
        .collect();
    let marks = render::disputed_marks(&pairs);
    assert_eq!(marks[0], (cps("m"), cps("[disputed: cf-000 +99]")));
    let one = render::disputed_marks(&[(cps("a"), cps("a"), cps("cf-0123456789abcdef"))]);
    assert_eq!(one, vec![(cps("a"), cps("[disputed: cf-0123456789ab]"))]);
}

#[test]
fn core_strips_the_ellipsis_before_the_whitespace() {
    // The trailing space shields the ellipsis from the first strip.
    assert_eq!(
        render::core(&cps("...x\u{2026} ")),
        &cps("...x\u{2026}")[..]
    );
    assert_eq!(render::core(&cps("\u{2026}...x\u{2026}")), &cps("...x")[..]);
}

#[test]
fn render_cuts_the_last_snippet_first_then_drops_pointers() {
    let body = "word ".repeat(60);
    let ps: Vec<_> = (0..3)
        .map(|i| p(&format!("m{i}"), &"s".repeat(250), &body))
        .collect();
    let (text, ids) = render::render_recall_block(&ps);
    assert!(text.len() <= 900);
    assert_eq!(ids.len(), 3);
    let lines: Vec<&[u32]> = text.split(|&c| c == 10).collect();
    // The first pointer keeps its whole snippet; the last lost its own.
    assert!(lines[1].ends_with(&cps(" wor\u{2026}")));
    assert!(!lines[3].contains(&u32::from(b'|')));
    let (empty, none) = render::render_recall_block(&[p("m0", &"s".repeat(900), "x")]);
    assert!(empty.is_empty() && none.is_empty());
}

#[test]
fn excerpt_keeps_the_snippet_in_place_of_a_summary_it_holds() {
    let (kept, snip) = render::one_excerpt(
        &p("m", "the sum\u{2026}", "x"),
        Some(&cps("a longer the sum text")),
    );
    assert_eq!(kept.summary, cps("a longer the sum text"));
    assert_eq!(snip, None);
}

#[test]
fn holds_takes_a_token_name_quote_of_exactly_eight_characters() {
    // The reference answers true: the defanged fold "p inst x" is eight
    // characters and is found only in the defanged prompt.
    let w = WordsSeen::new();
    let (s, c) = (cps("/s"), cps("c"));
    w.remember(&s, &c, &cps("say p INST x now"));
    assert!(w.holds(&s, &c, &cps("p [INST] x")));
}

#[test]
fn render_keeps_a_snippet_cut_to_exactly_twelve_characters() {
    // The reference keeps summarize(snippet, 12): 11 characters and an
    // ellipsis, filling the block to exactly 900 characters.
    let ps = [p(
        "m0",
        &"s".repeat(810),
        "abcdefghijklmnopqrstuvwxyz0123456789",
    )];
    let (text, ids) = render::render_recall_block(&ps);
    assert_eq!(ids, vec![cps("m0")]);
    assert_eq!(text.len(), 900);
    assert!(text.ends_with(&cps(" | abcdefghijk\u{2026}")));
}
