//! `claim_drift` over hostile input: very long streams, huge hunk counts,
//! counts at `u64`'s edge, long fragments against a long claimed value.
//! No panic, and time linear in the input: ten times the input costs under
//! sixty times the time (`timing`).

mod timing;

use bettermemory_core::claim_drift::{build_binding_index, claim_level_drift, LiteralLines};
use bettermemory_core::claims::{Claim, Kind};
use bettermemory_core::pyvalue::PyStr;

fn c(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

/// Every line a fragment of itself, every value its own `str`.
struct Echo;

impl LiteralLines for Echo {
    fn string_fragment(&self, content: &[u32]) -> Option<PyStr> {
        Some(content.to_vec())
    }
    fn rhs_repr(&self, content: &[u32]) -> Option<PyStr> {
        Some(content.to_vec())
    }
    fn anchors_from_value(&self, value: &[u32]) -> Vec<PyStr> {
        vec![value.to_vec()]
    }
    fn claimed_str(&self, value: &[u32]) -> Option<PyStr> {
        Some(value.to_vec())
    }
}

const A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

fn stream(n: usize) -> Vec<u32> {
    let mut s = format!("\u{1}{A}\ndiff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,{n} +1,{n} @@\n");
    for k in 0..n {
        s.push_str(&format!("-V{k} = {k}\n+def f{k}(x):\n"));
    }
    c(&s)
}

#[test]
fn many_lines_grow_linearly() {
    let (small, large) = (stream(2_000), stream(20_000));
    let ix = build_binding_index(&large, &Echo).unwrap();
    assert_eq!((ix.hunks, ix.parse_mismatches), (1, 0));
    timing::grows_within(
        "many_lines",
        timing::TENFOLD,
        &mut || timing::once(|| build_binding_index(&small, &Echo).unwrap()),
        &mut || timing::once(|| build_binding_index(&large, &Echo).unwrap()),
    );
}

#[test]
fn huge_counts_end_at_the_stream() {
    for count in [
        "18446744073709551615",
        "18446744073709551616",
        &"9".repeat(4300),
    ] {
        let s = c(&format!("--- a/f\n@@ -1,{count} +1,{count} @@\n-x\n+y"));
        let ix = build_binding_index(&s, &Echo).unwrap();
        assert_eq!((ix.hunks, ix.parse_mismatches), (1, 1));
    }
}

#[test]
fn many_headers_and_marks() {
    let mut s = String::new();
    for k in 0..50_000 {
        s.push_str(&format!(
            "\u{1}{k:040x}\ndiff --git a/p{k} b/p{k}\nnew mode 100755\n"
        ));
    }
    let ix = build_binding_index(&c(&s), &Echo).unwrap();
    assert_eq!(ix.header_only.len(), 50_000);
    assert_eq!(ix.commits, 50_000);
    assert!(ix.unindexed_commits.is_empty());
}

#[test]
fn a_long_fragment_search_is_linear() {
    let make = |n: usize| {
        let frag = format!("{}b", "a".repeat(n));
        let s = c(&format!("\u{1}{A}\n--- a/f\n@@ -0,0 +1 @@\n+{frag}"));
        let ix = build_binding_index(&s, &Echo).unwrap();
        let claim = Claim {
            kind: Kind::Literal,
            rel_path: c("f"),
            name: c("N"),
            value: c(&"a".repeat(4 * n)),
        };
        (ix, claim)
    };
    let (small, large) = (make(20_000), make(200_000));
    assert!(!claim_level_drift(&large.1, &large.0, &Echo).strict);
    timing::grows_within(
        "fragment_search",
        timing::TENFOLD,
        &mut || timing::once(|| claim_level_drift(&small.1, &small.0, &Echo)),
        &mut || timing::once(|| claim_level_drift(&large.1, &large.0, &Echo)),
    );
}

#[test]
fn many_foreign_fragments_cost_their_own_length() {
    let make = |n: usize| {
        let mut s = format!("\u{1}{A}\n+++ b/p\n@@ -0,0 +1,{n} @@\n");
        for k in 0..n {
            s.push_str(&format!("+X = 'fragment number {k} long'\n"));
        }
        let ix = build_binding_index(&c(&s), &Echo).unwrap();
        let claim = Claim {
            kind: Kind::Literal,
            rel_path: c("p"),
            name: c("X"),
            value: c(&"b".repeat(10 * n)),
        };
        (ix, claim)
    };
    let (small, large) = (make(2_000), make(20_000));
    assert_eq!(
        claim_level_drift(&large.1, &large.0, &Echo).anchor_commits,
        0
    );
    timing::grows_within(
        "foreign_fragments",
        timing::TENFOLD,
        &mut || timing::once(|| claim_level_drift(&small.1, &small.0, &Echo)),
        &mut || timing::once(|| claim_level_drift(&large.1, &large.0, &Echo)),
    );
}

#[test]
fn empty_and_odd_streams() {
    for s in ["", "\n", "\u{1}", "@@", "diff --git ", "--- ", "+++ ", "\\"] {
        let ix = build_binding_index(&c(s), &Echo).unwrap();
        let claim = Claim {
            kind: Kind::Absent,
            rel_path: Vec::new(),
            name: Vec::new(),
            value: Vec::new(),
        };
        let _ = claim_level_drift(&claim, &ix, &Echo);
    }
}
