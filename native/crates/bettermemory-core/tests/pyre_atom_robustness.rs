//! `pyre::atom` over hostile input: long patterns, wide sets, a megabyte
//! of backslashes, code points and group numbers at the edges of their
//! integers, and every short pattern over the characters that matter. No
//! panic, every position within the pattern, and time linear in the input,
//! held by `tests/timing`: ten times the input costs under `TENFOLD` times
//! the time.

mod timing;

use bettermemory_core::pyre::atom::{escape, item_escape, parse_flat};
use bettermemory_core::pyre::token::Tokenizer;
use bettermemory_core::pyre::tree::{BuildError, Feature, Member, Node, State, Why};

/// Holds `run` over 20,000 and 200,000 to `timing::TENFOLD`. `make`
/// builds each input outside the clock, and what `run` returns is
/// dropped after it.
fn linear<T, R>(name: &str, make: impl Fn(usize) -> T, run: impl Fn(&T, usize) -> R) {
    drop(run(&make(1_000), 1_000));
    let (small, large) = (make(20_000), make(200_000));
    timing::grows_within(
        name,
        timing::TENFOLD,
        &mut || timing::once(|| run(&small, 20_000)),
        &mut || timing::once(|| run(&large, 200_000)),
    );
}

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

/// A flat pattern of about `n` characters: escapes of every reader, sets,
/// the dot and the anchors, `n / 20` rounds of twenty characters.
fn flat(n: usize) -> Vec<u32> {
    let round = cps(r"ab\x41[^\d-]\0.\b$\t");
    round.iter().copied().cycle().take(n / 20 * 20).collect()
}

#[test]
fn a_long_flat_pattern_is_read_in_linear_time() {
    assert_eq!(flat(20).len(), 20);
    linear("parse_flat", flat, |pattern, n| {
        let tree = parse_flat(pattern, true, false).unwrap();
        // Nine items a round.
        assert_eq!(tree.0.len(), n / 20 * 9);
        tree
    });
}

#[test]
fn a_wide_set_is_read_in_linear_time() {
    // Members all different, then all the same: `_uniq` keeps each once.
    let different = |n: usize| {
        let mut pattern = vec![u32::from('[')];
        pattern.extend((0..n as u32).map(|i| 0x100 + i));
        pattern.push(u32::from(']'));
        pattern
    };
    linear("set", different, |pattern, n| {
        let tree = parse_flat(pattern, true, false).unwrap();
        assert!(matches!(&tree.0[..], [Node::In(set)] if set.len() == n));
        tree
    });
    let same = |n: usize| {
        let mut pattern = cps("[^");
        pattern.extend(cps(r"a-\x7a").iter().cycle().take(6 * n));
        pattern.push(u32::from(']'));
        pattern
    };
    linear("uniq", same, |pattern, _| {
        let tree = parse_flat(pattern, true, false).unwrap();
        assert!(matches!(
            &tree.0[..],
            [Node::In(set)] if set[..] == [Member::Negate, Member::Range(97, 122)]
        ));
        tree
    });
}

#[test]
fn a_megabyte_of_backslashes_through_the_parser() {
    // An even run is escaped backslashes; an odd one ends in the error at
    // the last index, and so does a set that holds it.
    let even = vec![0x5c; 1 << 20];
    let tree = parse_flat(&even, true, false).unwrap();
    assert_eq!(tree.0.len(), 1 << 19);
    assert!(tree
        .0
        .iter()
        .all(|node| matches!(node, Node::Literal(0x5c))));
    let odd = vec![0x5c; (1 << 20) + 1];
    let at = 1 << 20;
    let why = Why::BadEscapeEnd;
    assert_eq!(
        parse_flat(&odd, true, false).map(|_| ()),
        Err(BuildError::Invalid { at, why })
    );
    let mut set = vec![u32::from('[')];
    set.extend_from_slice(&odd);
    let at = (1 << 20) + 1;
    assert_eq!(
        parse_flat(&set, false, false).map(|_| ()),
        Err(BuildError::Invalid { at, why })
    );
}

#[test]
fn re_escape_of_a_long_text_is_linear_and_reads_back() {
    let text = |n: usize| (0..n as u32).map(|i| i % 0x200).collect::<Vec<u32>>();
    linear("escape", text, |text, n| {
        let escaped = escape(text);
        let tree = parse_flat(&escaped, true, false).unwrap();
        assert_eq!(tree.0.len(), n);
        escaped
    });
}

#[test]
fn code_points_and_numbers_at_the_edges() {
    let read = |pattern: &[u32]| parse_flat(pattern, true, false).map(|tree| tree.dump());
    // No `str` holds these code points; a `u32` does, and each is a
    // literal, escaped or in a set.
    assert_eq!(read(&[u32::MAX]), Ok("[(LITERAL, 4294967295)]".into()));
    assert_eq!(
        read(&[0x5c, u32::MAX]),
        Ok("[(LITERAL, 4294967295)]".into())
    );
    let range = [0x5b, 0x11_0000, 0x2d, u32::MAX, 0x5d];
    assert_eq!(
        read(&range),
        Ok("[(IN, [(RANGE, (1114112, 4294967295))])]".into())
    );
    let reversed = [0x5b, u32::MAX, 0x2d, 0x11_0000, 0x5d];
    let (at, why) = (1, Why::BadRange);
    assert_eq!(read(&reversed), Err(BuildError::Invalid { at, why }));
    // The largest values the escapes can write.
    assert_eq!(
        read(&cps(r"\U0010FFFF\377\xFF")),
        Ok("[(LITERAL, 1114111), (LITERAL, 255), (LITERAL, 255)]".into())
    );
    let (at, why) = (0, Why::BadEscape);
    assert_eq!(
        read(&cps(r"\UFFFFFFFF")),
        Err(BuildError::Invalid { at, why })
    );
    let (at, why) = (0, Why::OctalRange);
    assert_eq!(read(&cps(r"\777")), Err(BuildError::Invalid { at, why }));
    let (at, why) = (0, Why::IncompleteEscape);
    assert_eq!(
        read(&[0x5c, u32::from('x'), u32::MAX]),
        Err(BuildError::Invalid { at, why })
    );
    // A reference to group 99 of 100 closed groups passes every check.
    let mut state = State::new();
    for gid in 1..=100 {
        assert_eq!(state.opengroup(None), Ok(gid));
        assert!(state.closegroup(gid, (0, u128::MAX)));
    }
    let text = cps(r"\99");
    let mut source = Tokenizer::new(&text, true).unwrap();
    let token = source.get().unwrap().unwrap();
    let (at, what) = (0, Feature::Backreference);
    assert_eq!(
        item_escape(&mut source, token.c, &state).map(|_| ()),
        Err(BuildError::Unsupported { at, what })
    );
}

/// The characters every reader of a pattern treats apart.
const ALPHABET: &str = r"\[]-^a0187xuUN{}(|) #&";

#[test]
fn every_short_pattern_answers_within_itself() {
    let alphabet = cps(ALPHABET);
    let mut patterns = vec![Vec::new()];
    for length in 1..=3 {
        let shorter: Vec<Vec<u32>> = patterns
            .iter()
            .filter(|p| p.len() == length - 1)
            .cloned()
            .collect();
        for prefix in shorter {
            for &c in &alphabet {
                let mut pattern = prefix.clone();
                pattern.push(c);
                patterns.push(pattern);
            }
        }
    }
    assert_eq!(patterns.len(), 1 + 22 + 22 * 22 + 22 * 22 * 22);
    for pattern in &patterns {
        for (istext, verbose) in [(true, false), (false, false), (true, true)] {
            let at = match parse_flat(pattern, istext, verbose) {
                Ok(_) => continue,
                Err(BuildError::Invalid { at, .. } | BuildError::Unsupported { at, .. }) => at,
            };
            let len = pattern.len() as isize;
            assert!((0..=len).contains(&at), "{pattern:?}: {at}");
        }
    }
}
