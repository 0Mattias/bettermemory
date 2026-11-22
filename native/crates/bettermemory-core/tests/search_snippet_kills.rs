//! Cases a plausible wrong `search::snippet` or `search::token_cache`
//! fails, each with the reference's answer (read from CPython 3.13.13).

use bettermemory_core::search::snippet::{top_hit_leads_runner_up, SnippetMemo};
use bettermemory_core::search::token_cache::TokenCache;
use bettermemory_core::unicode::cps;

const PAD: &str = "Opening paragraph about nothing in particular here. ";

fn snip(body: &str, matched: &[&str], mc: i64) -> String {
    let memo = SnippetMemo::default();
    let m: Vec<String> = matched.iter().map(|s| (*s).to_owned()).collect();
    memo.query_biased_snippet(&cps(body), &m, mc)
        .iter()
        .map(|&c| char::from_u32(c).unwrap())
        .collect()
}

fn padded(tail: &str) -> String {
    format!("{}{tail}", PAD.repeat(6))
}

#[test]
fn the_margin_is_two_rank_slots_not_one() {
    // 62/61 is a one-slot lead: not enough.
    assert!(!top_hit_leads_runner_up(62.0, 61.0));
    assert!(top_hit_leads_runner_up(63.0, 61.0));
}

#[test]
fn a_zero_runner_up_needs_a_positive_top() {
    assert!(!top_hit_leads_runner_up(0.0, 0.0));
    assert!(top_hit_leads_runner_up(5e-324, -0.0));
    assert!(!top_hit_leads_runner_up(f64::NAN, 1.0));
}

#[test]
fn alias_anchors_ignore_case() {
    assert_eq!(
        snip(&padded("We write C++ code here."), &["cpp"], 200),
        "...nothing in particular here. We write C++ code here."
    );
}

#[test]
fn an_alias_inside_a_word_is_no_anchor() {
    assert!(snip(&padded("We write aC++ code here."), &["cpp"], 200).starts_with("Opening"));
    assert!(snip(&padded("x.net here"), &["dotnet"], 200).starts_with("Opening"));
}

#[test]
fn a_kebab_part_anchors_only_without_the_compound() {
    assert_eq!(
        snip(
            &padded("We moved to Make Target last week, it is fine."),
            &["mak-target"],
            200
        ),
        "...nothing in particular here. We moved to Make Target last week, it is fine."
    );
}

#[test]
fn the_scan_stops_at_eight_thousand() {
    let body = format!("{}needle", "pad ".repeat(1999));
    assert!(snip(&body, &["needl"], 200).starts_with("pad pad"));
    assert!(!snip(&body, &["needl"], 200).starts_with("..."));
}

#[test]
fn a_head_anchor_serves_the_head() {
    assert!(snip(&format!("c++ {}", PAD.repeat(6)), &["cpp"], 200).starts_with("c++ Opening"));
}

#[test]
fn unstemmed_terms_find_nothing() {
    assert!(snip(&padded("needle here"), &["needle"], 200).starts_with("Opening"));
    assert_eq!(
        snip(&padded("needle here"), &["needl"], 200),
        "...about nothing in particular here. needle here"
    );
}

#[test]
fn the_token_cache_evicts_least_recently_used() {
    let cache = TokenCache::with_entries(2);
    let s: Vec<String> = Vec::new();
    cache.memory_tokens(&cps("a"), &s);
    cache.memory_tokens(&cps("b"), &s);
    cache.memory_tokens(&cps("a"), &s);
    cache.memory_tokens(&cps("c"), &s);
    let keys: Vec<PyStrKey> = cache.keys().into_iter().map(|(b, _)| b).collect();
    assert_eq!(keys, vec![cps("a"), cps("c")]);
}

type PyStrKey = Vec<u32>;

#[test]
fn a_seed_never_replaces_a_held_entry() {
    let cache = TokenCache::default();
    let s = vec!["x".to_owned()];
    let first = cache.memory_tokens(&cps("one two"), &s);
    cache.seed_memory_tokens(&cps("one two"), &s, "other");
    assert_eq!(cache.memory_tokens(&cps("one two"), &s), first);
}

#[test]
fn a_seed_splits_on_single_spaces() {
    let cache = TokenCache::default();
    cache.seed_memory_tokens(&cps("b"), &[], "uno  uno");
    let t = cache.memory_tokens(&cps("b"), &[]);
    assert_eq!(t.body, vec!["uno", "", "uno"]);
    assert_eq!(t.body_tf, vec![("uno".to_owned(), 2), (String::new(), 1)]);
}

#[test]
fn scopes_in_another_order_are_another_key() {
    let cache = TokenCache::default();
    cache.memory_tokens(&cps("b"), &["a".to_owned(), "b".to_owned()]);
    cache.memory_tokens(&cps("b"), &["b".to_owned(), "a".to_owned()]);
    assert_eq!(cache.keys().len(), 2);
}
