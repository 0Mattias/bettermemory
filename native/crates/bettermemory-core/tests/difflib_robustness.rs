//! `difflib` over hostile input, and its cost.
//!
//! - Long lines, a million identical lines, every line the same below and
//!   at the autojunk bound, and lone surrogates: no panic, and the answer
//!   keeps its shape.
//! - Twice the lines cost under 4.5 times the time for identical,
//!   disjoint and interleaved sequences. The reference is equally
//!   quadratic in the worst case (`find_longest_match` walks every `j` of
//!   a line for every `i`, and `get_matching_blocks` calls it once per
//!   block): the claim is that the port grows no faster than that, and
//!   that these three shapes stay within twice-quadratic growth.

mod timing;

use std::time::Duration;

use bettermemory_core::difflib::{body_diff, splitlines, unified_diff, SequenceMatcher};
use bettermemory_core::pyvalue::PyStr;

fn s(t: &str) -> PyStr {
    t.chars().map(u32::from).collect()
}

fn numbered(prefix: &str, n: usize) -> Vec<PyStr> {
    (0..n).map(|i| s(&format!("{prefix}{i}"))).collect()
}

#[test]
fn identical_million_lines_have_no_diff() {
    let a = vec![s("same"); 1_000_000];
    assert!(unified_diff(&a, &a, &s("o"), &s("n")).is_empty());
    let a = numbered("l", 200_000);
    assert!(unified_diff(&a, &a, &s("o"), &s("n")).is_empty());
}

#[test]
fn one_line_everywhere_below_and_at_the_bound() {
    for n in [199usize, 200, 201, 5000] {
        let a = vec![s("x"); n];
        let b = vec![s("x"); n + 1];
        let out = unified_diff(&a, &b, &s("o"), &s("n"));
        assert_eq!(
            out.iter()
                .filter(|l| l.first() == Some(&u32::from(b'+')))
                .count(),
            2,
            "{n}"
        );
        let blocks = SequenceMatcher::new(&a, &b).get_matching_blocks();
        assert_eq!(blocks[0], (0, 0, n), "{n}");
    }
}

#[test]
fn long_lines_and_surrogates() {
    let long: PyStr = std::iter::repeat_n(0xd800, 1 << 20).collect();
    let mut after = long.clone();
    after.push(0x0a);
    after.extend_from_slice(&[0xdfff, 0x2028, 0x85]);
    let out = body_diff(Some(&long), &after, &[0xdc00], &[0xd83d]);
    assert_eq!(splitlines(&out).len(), 6);
}

#[test]
fn deep_alternation_does_not_recurse() {
    let a = numbered("x", 5_000);
    let b: Vec<PyStr> = (0..5_000).rev().map(|i| s(&format!("x{i}"))).collect();
    let out = unified_diff(&a, &b, &s("o"), &s("n"));
    assert!(out.len() > 5_000);
}

fn claim(name: &str, make: impl Fn(usize) -> (Vec<PyStr>, Vec<PyStr>), n: usize) {
    let (a1, b1) = make(n);
    let (a2, b2) = make(2 * n);
    // 4.5 times as one integer factor: two runs of the large size against
    // nine of the small one.
    let mut small = || -> Duration {
        timing::once(|| {
            for _ in 0..9 {
                std::hint::black_box(unified_diff(&a1, &b1, &[], &[]));
            }
        })
    };
    let mut large = || -> Duration {
        timing::once(|| {
            for _ in 0..2 {
                std::hint::black_box(unified_diff(&a2, &b2, &[], &[]));
            }
        })
    };
    timing::grows_within(name, 1, &mut small, &mut large);
}

#[test]
fn twice_identical_lines_cost_under_four_and_a_half_times() {
    claim("identical", |n| (numbered("l", n), numbered("l", n)), 5_000);
}

#[test]
fn twice_disjoint_lines_cost_under_four_and_a_half_times() {
    claim("disjoint", |n| (numbered("a", n), numbered("b", n)), 5_000);
}

#[test]
fn twice_interleaved_lines_cost_under_four_and_a_half_times() {
    claim(
        "interleaved",
        |n| {
            let a = numbered("x", n);
            let b = (0..n)
                .flat_map(|i| [s(&format!("x{i}")), s(&format!("y{i}"))])
                .collect();
            (a, b)
        },
        1_000,
    );
}

/// The public helpers at usize's edges answer as Python's unbounded
/// integers do, alike in debug and release: `_format_range_unified(5, 3)`
/// is "6,-2", and `get_grouped_opcodes(n)` with `n + n` past usize::MAX
/// keeps every line of context.
#[test]
fn helpers_at_usize_edges_answer_as_python() {
    use bettermemory_core::difflib::{format_range_unified, Tag};
    let m = usize::MAX;
    assert_eq!(format_range_unified(5, 3), "6,-2");
    assert_eq!(format_range_unified(m, m), format!("{m},0"));
    assert_eq!(format_range_unified(0, m), format!("1,{m}"));
    assert_eq!(
        format_range_unified(m, 0),
        format!("{},-{m}", m as u128 + 1)
    );
    let (a, b) = (vec![s("a"), s("b"), s("c")], vec![s("a"), s("x"), s("c")]);
    assert_eq!(
        SequenceMatcher::new(&a, &b).get_grouped_opcodes(m),
        vec![vec![
            (Tag::Equal, 0, 1, 0, 1),
            (Tag::Replace, 1, 2, 1, 2),
            (Tag::Equal, 2, 3, 2, 3)
        ]]
    );
    let same = vec![s("a"); 5];
    assert!(SequenceMatcher::new(&same, &same)
        .get_grouped_opcodes(m)
        .is_empty());
}
