//! `recall`'s words table and recall block over hostile input: a quote
//! that matches nearly everywhere, megabyte prompts and bodies, lone
//! surrogates, thousands of pointers and conflict pairs. No panic, in
//! debug or release, and each reading grows about linearly (ten times the
//! input within the timing helper's tenfold bound), where the reference's
//! `str.find` loop and dict are linear too.

mod timing;

use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::recall::render::{self, RecallPointer};
use bettermemory_core::recall::words::{self, WordsSeen};
use bettermemory_core::unicode::cps;

fn run_of(c: u32, n: usize) -> PyStr {
    vec![c; n]
}

#[test]
fn bounded_is_linear_where_every_index_nearly_matches() {
    let mut wanted = run_of(u32::from(b'a'), 1000);
    wanted.push(u32::from(b'b'));
    let small = run_of(u32::from(b'a'), 20_000);
    let large = run_of(u32::from(b'a'), 200_000);
    timing::grows_within(
        "bounded near misses",
        timing::TENFOLD,
        &mut || timing::once(|| words::bounded(&wanted, &small)),
        &mut || timing::once(|| words::bounded(&wanted, &large)),
    );
    // Every index matches and every one fails its boundary.
    let short = cps("aaaaaaaa");
    let small = run_of(u32::from(b'a'), 20_000);
    let large = run_of(u32::from(b'a'), 200_000);
    assert_eq!(words::bounded(&short, &large), Some(false));
    timing::grows_within(
        "bounded every index",
        timing::TENFOLD,
        &mut || timing::once(|| words::bounded(&short, &small)),
        &mut || timing::once(|| words::bounded(&short, &large)),
    );
}

#[test]
fn holds_over_megabyte_prompts_and_surrogates() {
    let w = WordsSeen::new();
    let (s, c) = (cps("/s"), cps("c"));
    let mut big = run_of(0xD800, 1 << 20);
    big.extend(cps(" <|im_start|> the end"));
    for _ in 0..3 {
        w.remember(&s, &c, &big);
    }
    assert!(w.holds(&s, &c, &cps("im_start the end")));
    assert!(!w.holds(&s, &c, &run_of(0xDFFF, 1 << 16)));
}

#[test]
fn render_and_summarize_over_large_input() {
    let body: PyStr = cps(&"word ".repeat(200_000));
    assert_eq!(render::summarize(&body, i64::MAX).len(), body.len() - 1);
    assert_eq!(render::summarize(&body, i64::MIN), cps("\u{2026}"));
    let ps: Vec<RecallPointer> = (0..2000)
        .map(|i| RecallPointer {
            id: cps(&format!("m{i}")),
            summary: cps("s"),
            snippet: body[..400].to_vec(),
            ..RecallPointer::default()
        })
        .collect();
    let (text, ids) = render::render_recall_block(&ps);
    assert!(text.len() <= 900 && !ids.is_empty());
}

#[test]
fn marks_are_linear_in_the_pairs() {
    let pairs = |n: usize| -> Vec<(PyStr, PyStr, PyStr)> {
        (0..n)
            .map(|i| {
                (
                    cps(&format!("a{i}")),
                    cps(&format!("b{}", i % 7)),
                    cps(&format!("cf-{i}")),
                )
            })
            .collect()
    };
    let (small, large) = (pairs(2_000), pairs(20_000));
    timing::grows_within(
        "disputed_marks",
        timing::TENFOLD,
        &mut || timing::once(|| render::disputed_marks(&small)),
        &mut || timing::once(|| render::disputed_marks(&large)),
    );
}
