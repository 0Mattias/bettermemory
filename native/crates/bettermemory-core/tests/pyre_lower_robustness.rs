//! Hostile trees for `pyre::lower`: nested past any stack, counts at the
//! integer bounds, bodies of no node repeated four billion times, and
//! trees far past the arena's cap. None panics, none recurses past
//! `MAX_DEPTH` levels, and the cost grows with the tree, not the counts.

mod timing;

use std::thread;

use bettermemory_core::pyre::lower::{lower, LowerError, Op, Unit, MAX_DEPTH, MAX_NODES};
use bettermemory_core::pyre::tree::{Feature, Member, Node, SubPattern, MAXREPEAT, UNICODE};

const U: u32 = UNICODE;

fn nested(levels: usize, leaf: Vec<Node>) -> SubPattern {
    let mut p = SubPattern(leaf);
    for _ in 0..levels {
        p = SubPattern(vec![Node::Subpattern {
            group: None,
            add_flags: 0,
            del_flags: 0,
            body: p,
        }]);
    }
    p
}

fn refused(what: Feature) -> Result<(), LowerError> {
    Err(LowerError::Unsupported(what))
}

/// A million levels is refused as nesting, measured without recursion,
/// on a thread of a small stack.
#[test]
fn deep_nesting_on_a_small_stack() {
    let answer = thread::Builder::new()
        .stack_size(256 * 1024)
        .spawn(|| {
            let deep = nested(1_000_000, vec![Node::Literal(97)]);
            let lowered = lower(&deep, U, false).map(|_| ());
            let at_bound = lower(&nested(MAX_DEPTH, vec![Node::Literal(97)]), U, false);
            (lowered, at_bound.map(|p| p.insts.len()))
        })
        .expect("spawn")
        .join()
        .expect("no panic");
    assert_eq!(answer, (refused(Feature::Nesting), Ok(2)));
}

/// Counts at the bounds of `u32` end at the cap, or at once for a body of
/// no node.
#[test]
fn integer_bounds() {
    let ab = || SubPattern(vec![Node::Literal(97), Node::Literal(98)]);
    for (min, max) in [
        (0, MAXREPEAT - 1),
        (MAXREPEAT - 1, MAXREPEAT - 1),
        (MAXREPEAT - 1, MAXREPEAT),
    ] {
        let tree = SubPattern(vec![Node::MaxRepeat(min, max, ab())]);
        assert_eq!(lower(&tree, U, false).map(|_| ()), refused(Feature::Nodes));
    }
    for max in [0, 1] {
        let tree = SubPattern(vec![Node::MinRepeat(
            MAXREPEAT - 1,
            max,
            SubPattern(vec![]),
        )]);
        // `min` past `max`: no optional, and copies of nothing.
        let p = lower(&tree, U, false).expect("an empty body lowers");
        assert_eq!(p.insts.len(), 1);
    }
    let one = SubPattern(vec![Node::MaxRepeat(
        MAXREPEAT - 1,
        MAXREPEAT,
        SubPattern(vec![Node::Any]),
    )]);
    assert_eq!(lower(&one, U, false).expect("one node").insts.len(), 2);
}

/// Nested counted repeats: each level multiplies, the cap stops it.
#[test]
fn nested_counts_stop_at_the_cap() {
    let mut p = SubPattern(vec![Node::Literal(97), Node::Literal(98)]);
    for _ in 0..MAX_DEPTH {
        p = SubPattern(vec![Node::MaxRepeat(0, 30, p), Node::Literal(99)]);
    }
    assert_eq!(lower(&p, U, false).map(|_| ()), refused(Feature::Nodes));
}

/// A tree of millions of items past the cap is refused in time linear in
/// the tree: the checks walk it once, the arena stops at `MAX_NODES`.
#[test]
fn wide_trees_are_linear() {
    let make = |n: usize| {
        let body = SubPattern(
            (0..n)
                .map(|i| Node::Literal(97 + (i % 26) as u32))
                .collect(),
        );
        SubPattern(vec![Node::Assert(-1, body)])
    };
    let run = |tree: &SubPattern| lower(tree, U, false).map(|p| p.insts.len());
    assert_eq!(run(&make(MAX_NODES)), refused(Feature::Nodes).map(|_| 0));
    let (small, large) = (make(20_000), make(200_000));
    timing::grows_within(
        "pyre_lower wide",
        timing::TENFOLD,
        &mut || timing::once(|| run(&small)),
        &mut || timing::once(|| run(&large)),
    );
}

/// Every unrolled copy of a set shares one member list, so a large set
/// repeated to the cap holds its members once.
#[test]
fn unrolled_sets_share_members() {
    let body = SubPattern(vec![
        Node::In(vec![Member::Literal(97); 50_000]),
        Node::Literal(98),
    ]);
    let tree = SubPattern(vec![Node::MaxRepeat(2000, 2000, body)]);
    let p = lower(&tree, U, false).expect("lowers");
    let sets: Vec<_> = p
        .insts
        .iter()
        .filter_map(|i| match &i.op {
            Op::Unit(Unit::Class(c)) => Some(&c.members),
            _ => None,
        })
        .collect();
    assert_eq!(sets.len(), 2000);
    assert!(sets.iter().all(|m| std::sync::Arc::ptr_eq(m, sets[0])));
}
