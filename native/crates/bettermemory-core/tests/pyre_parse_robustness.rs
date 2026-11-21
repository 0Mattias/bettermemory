//! `pyre::parse` and `SubPattern::getwidth` over hostile input: long runs
//! of repeats, a million digits in a bound, unclosed braces that send the
//! tokenizer back, trees nested far deeper than a parse allows, and every
//! short pattern over the characters a repeat reads. No panic, every
//! position within the pattern, and time linear in the input, held by
//! `tests/timing`: ten times the input costs under `TENFOLD` times the
//! time.

mod timing;

use std::thread;

use bettermemory_core::pyre::atom::parse_flat;
use bettermemory_core::pyre::parse::INT_MAX_STR_DIGITS;
use bettermemory_core::pyre::tree::{BuildError, Node, SubPattern, MAXREPEAT, MAXWIDTH};

/// Holds `run` over 20,000 and 200,000 to `timing::TENFOLD`.
fn linear<T, R>(name: &str, make: impl Fn(usize) -> T, run: impl Fn(&T) -> R) {
    drop(run(&make(1_000)));
    let (small, large) = (make(20_000), make(200_000));
    timing::grows_within(
        name,
        timing::TENFOLD,
        &mut || timing::once(|| run(&small)),
        &mut || timing::once(|| run(&large)),
    );
}

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

/// `n / 20` rounds of twenty characters, each a repeat of every kind.
fn repeats(n: usize) -> Vec<u32> {
    let round = cps("a*b+?c?d{2,3}e{,}f{}");
    round.iter().copied().cycle().take(n / 20 * 20).collect()
}

#[test]
fn a_long_run_of_repeats_is_read_and_measured_in_linear_time() {
    let tree = parse_flat(&repeats(2_000), true, false).expect("repeats");
    assert_eq!(tree.0.len(), 100 * 8);
    assert_eq!(tree.getwidth(), (100 * 6, MAXWIDTH));
    linear("repeats", repeats, |pattern| {
        parse_flat(pattern, true, false).map(|tree| tree.getwidth())
    });
}

/// A bound of a million digits is past `int`'s limit of 4,300 digits,
/// zeros or nines alike, and is ValueError; either side of the limit, and
/// the limit itself, is read in time linear in its digits.
#[test]
fn a_million_digits_in_a_bound() {
    let bound = |digits: &str, n: usize| {
        let mut text = cps("a{");
        text.extend(cps(digits).iter().copied().cycle().take(n));
        text.extend(cps("1}"));
        text
    };
    for digits in ["0", "9"] {
        assert_eq!(
            parse_flat(&bound(digits, 1_000_000), true, false).err(),
            Some(BuildError::DigitLimit)
        );
    }
    let last = INT_MAX_STR_DIGITS - 1;
    let tree = parse_flat(&bound("0", last), true, false).expect("a bound of one");
    assert_eq!(tree.getwidth(), (1, 1));
    assert_eq!(
        parse_flat(&bound("0", last + 1), true, false).err(),
        Some(BuildError::DigitLimit)
    );
    assert_eq!(
        parse_flat(&bound("9", last), true, false).err(),
        Some(BuildError::Overflow)
    );
    linear("digits", |n| bound("9", n), |p| parse_flat(p, true, false));
}

/// An unclosed `{` sends the tokenizer back to read its digits again, as
/// literals: twice each digit, not once per brace before it.
#[test]
fn unclosed_braces_are_read_back_in_linear_time() {
    let unclosed = |n: usize| {
        let mut text = cps("a");
        text.extend(cps("{12,34").iter().copied().cycle().take(n));
        text
    };
    let tree = parse_flat(&unclosed(6_000), true, false).expect("literals");
    assert_eq!(tree.getwidth(), (6_001, 6_001));
    linear("unclosed", unclosed, |p| parse_flat(p, true, false));
    let open = |n: usize| {
        let mut text = cps("a{");
        text.extend(std::iter::repeat_n(u32::from('7'), n));
        text
    };
    linear("one open brace", open, |p| parse_flat(p, true, false));
}

/// A tree 200,000 repeats deep, far past what a parse makes, measured on
/// a thread of 64 KiB: `getwidth` keeps its own stack.
#[test]
fn a_deep_tree_is_measured_on_a_small_stack() {
    let handle = thread::Builder::new()
        .stack_size(64 * 1024)
        .spawn(|| {
            let mut tree = SubPattern(vec![Node::Literal(97)]);
            for _ in 0..200_000 {
                tree = SubPattern(vec![Node::MaxRepeat(1, 2, tree), Node::Any]);
            }
            let deep = tree.getwidth();
            let mut wide = SubPattern(vec![]);
            for _ in 0..1_000 {
                wide = SubPattern(vec![Node::Branch(vec![wide, SubPattern(vec![Node::Any])])]);
            }
            (deep, wide.getwidth())
        })
        .expect("a thread");
    let (deep, wide) = handle.join().expect("no overflow of the stack");
    assert_eq!(deep, (200_001, MAXWIDTH));
    assert_eq!(wide, (0, 1));
}

/// Every pattern of up to four characters over what a repeat reads, as
/// text and as bytes: no panic, every position within the pattern, and a
/// width no larger than `MAXWIDTH`.
#[test]
fn every_short_pattern_answers_within_itself() {
    let alphabet = cps(r"a*+?{},0^\(|");
    let mut patterns: Vec<Vec<u32>> = vec![Vec::new()];
    let mut from = 0;
    for _ in 0..4 {
        let to = patterns.len();
        for i in from..to {
            for &c in &alphabet {
                let mut pattern = patterns[i].clone();
                pattern.push(c);
                patterns.push(pattern);
            }
        }
        from = to;
    }
    assert_eq!(patterns.len(), 1 + 12 + 144 + 1_728 + 20_736);
    for pattern in &patterns {
        for istext in [true, false] {
            match parse_flat(pattern, istext, false) {
                Ok(tree) => {
                    let (lo, hi) = tree.getwidth();
                    assert!(lo <= MAXWIDTH && hi <= MAXWIDTH, "{pattern:?}");
                }
                Err(BuildError::Invalid { at, .. } | BuildError::Unsupported { at, .. }) => {
                    let len = pattern.len() as isize;
                    assert!((0..=len).contains(&at), "{pattern:?}: {at}");
                }
                Err(BuildError::Overflow | BuildError::DigitLimit) => {
                    panic!("no short bound overflows: {pattern:?}")
                }
            }
        }
    }
}

/// The bounds at the edges of `u32`, each in a repeat over a repeat of
/// the widest kind: products held exactly in `u128`, then capped.
#[test]
fn bounds_at_the_edges_of_their_integers() {
    let top = MAXREPEAT - 1;
    for min in [0, 1, top] {
        for max in [0, 1, top, MAXREPEAT] {
            let inner = SubPattern(vec![Node::MinRepeat(min, max, SubPattern(vec![Node::Any]))]);
            let tree = SubPattern(vec![Node::MaxRepeat(min, max, inner), Node::Any]);
            let (lo, hi) = tree.getwidth();
            let product = |a: u32, b: u32| u128::from(a) * u128::from(b);
            assert_eq!(lo, (product(min, min) + 1).min(MAXWIDTH));
            let high = if max == MAXREPEAT {
                MAXWIDTH
            } else {
                product(max, max) + 1
            };
            let high = if max == 0 { 1 } else { high.min(MAXWIDTH) };
            assert_eq!(hi, high, "{min} {max}");
        }
    }
}
