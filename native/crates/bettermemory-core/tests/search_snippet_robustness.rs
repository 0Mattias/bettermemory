//! Hostile inputs to `search::snippet` and `search::token_cache`: huge
//! bodies, bodies of one-letter tokens, a token as long as the scan,
//! thousands of matched terms, integer bounds of `max_chars`. None may
//! panic, in debug or release, and the scan's cost stops growing past
//! `_SNIPPET_SCAN_CHARS`.

mod timing;

use bettermemory_core::search::snippet::{top_hit_leads_runner_up, SnippetMemo};
use bettermemory_core::search::token_cache::TokenCache;
use bettermemory_core::unicode::cps;

fn m(terms: &[&str]) -> Vec<String> {
    terms.iter().map(|s| (*s).to_owned()).collect()
}

#[test]
fn max_chars_at_the_integer_bounds() {
    let memo = SnippetMemo::default();
    let body = cps(&format!("{} needle", "pad ".repeat(500)));
    for mc in [
        i64::MIN,
        i64::MIN + 1,
        -1,
        0,
        1,
        3,
        4,
        43,
        44,
        i64::MAX - 1,
        i64::MAX,
    ] {
        let _ = memo.query_biased_snippet(&body, &m(&["needl", "pad"]), mc);
    }
}

#[test]
fn a_body_of_a_million_characters_scans_only_its_head() {
    let memo = SnippetMemo::default();
    let small = cps(&"a b ".repeat(10_000));
    let large = cps(&"a b ".repeat(250_000));
    let terms = m(&["b"]);
    let run = |body: &Vec<u32>| {
        timing::once(|| {
            memo.clear();
            memo.query_biased_snippet(body, &terms, 200)
        })
    };
    // Twenty-five times the body; the scan is the same 8,000 characters,
    // so the time is the strip and the head's copy, linear and small.
    timing::grows_within("one-letter tokens", 60, &mut || run(&small), &mut || {
        run(&large)
    });
}

#[test]
fn a_token_as_long_as_the_scan() {
    let memo = SnippetMemo::default();
    let body = cps(&format!("{} tail", "x".repeat(20_000)));
    let out = memo.query_biased_snippet(&body, &m(&["tail"]), 200);
    // 200 characters and the ellipsis: `_truncate_at_word` keeps 200 when
    // no whitespace falls in them (the reference answers 203 too).
    assert_eq!(out.len(), 203);
}

#[test]
fn thousands_of_matched_terms_and_aliases() {
    let memo = SnippetMemo::default();
    let body = cps(&"c++ c# f# .net a-b ".repeat(2_000));
    let terms: Vec<String> = (0..5_000)
        .map(|i| format!("t{i}-u{i}"))
        .chain(["cpp", "csharp", "fsharp", "dotnet", "a-b"].map(str::to_owned))
        .collect();
    let _ = memo.query_biased_snippet(&body, &terms, 200);
}

#[test]
fn memos_at_tiny_bounds_and_lone_surrogates() {
    let memo = SnippetMemo::with_entries(0, 0);
    let mut body = cps(&"word ".repeat(100));
    body.extend([0xD800, 0xDFFF, 0x20, 0x6E]);
    let _ = memo.query_biased_snippet(&body, &m(&["n", "word"]), 50);
    assert!(memo.scan_keys().is_empty() && memo.surface_keys().is_empty());
    let cache = TokenCache::with_entries(0);
    let _ = cache.memory_tokens(&body, &[]);
    cache.seed_memory_tokens(&body, &[], "a  b ");
    assert!(cache.keys().is_empty());
}

#[test]
fn the_gate_over_extreme_floats() {
    for a in [
        f64::MIN,
        f64::MAX,
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::NAN,
        0.0,
        -0.0,
    ] {
        for b in [
            f64::MIN,
            f64::MAX,
            f64::INFINITY,
            f64::NEG_INFINITY,
            f64::NAN,
            0.0,
            -0.0,
        ] {
            let _ = top_hit_leads_runner_up(a, b);
        }
    }
}
