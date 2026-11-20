//! `pyre::tree` and `pyre::token` over hostile input: very long patterns
//! and very wide trees, nesting eight times past the parser's bound and
//! 200,000 levels deep on a small stack, code points and counts at the
//! edges of their integers. No panic, and
//! time linear in the input, held by `tests/timing`: ten times the input
//! costs under `TENFOLD` times the time.

mod timing;

use bettermemory_core::pyre::token::{Tok, Tokenizer};
use bettermemory_core::pyre::tree::{
    BuildError, Member, Node, State, SubPattern, Why, MAXGROUPS, MAXREPEAT, MAXWIDTH,
};

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

/// A pattern of `n` characters: every third token an escape.
fn pattern(n: usize) -> Vec<u32> {
    (0..n)
        .map(|i| {
            if i % 4 == 2 {
                0x5c
            } else {
                0x61 + (i % 7) as u32
            }
        })
        .collect()
}

#[test]
fn a_long_pattern_is_read_in_linear_time() {
    linear("get", pattern, |text, n| {
        let mut source = Tokenizer::new(text, true).unwrap();
        let mut tokens = 0;
        while source.get().unwrap().is_some() {
            tokens += 1;
        }
        assert_eq!(tokens, n - n / 4);
        assert_eq!(source.tell(), n as isize);
    });
}

#[test]
fn a_long_run_and_a_long_name_are_read_in_linear_time() {
    linear(
        "getwhile",
        |n| vec![0x37; n],
        |text, n| {
            let mut source = Tokenizer::new(text, true).unwrap();
            let run = source.getwhile(usize::MAX, |c| c == 0x37).unwrap();
            assert_eq!(run.len(), n);
            assert_eq!(source.next, None);
            run
        },
    );
    let named = |n| {
        let mut text = pattern(n);
        text.push(u32::from('>'));
        text
    };
    linear("getuntil", named, |text, n| {
        let mut source = Tokenizer::new(text, true).unwrap();
        let name = source.getuntil('>', Why::MissingGroupName).unwrap();
        assert_eq!(name.len(), n);
        // And without its terminator: the error is where the name began.
        let mut source = Tokenizer::new(&text[..n], true).unwrap();
        let error = source.getuntil('>', Why::MissingGroupName);
        let at = 0;
        assert_eq!(
            error,
            Err(BuildError::Invalid {
                at,
                why: Why::UnterminatedName
            })
        );
        name
    });
}

#[test]
fn a_megabyte_of_backslashes() {
    // An even run is escaped backslashes; an odd one ends in the error,
    // raised by the step that loads the last, at the last index.
    let even = vec![0x5c; 1 << 20];
    let mut source = Tokenizer::new(&even, true).unwrap();
    let mut tokens = 0;
    while let Some(token) = source.get().unwrap() {
        assert_eq!(token, Tok { esc: true, c: 0x5c });
        tokens += 1;
    }
    assert_eq!(tokens, 1 << 19);
    let odd = vec![0x5c; (1 << 20) + 1];
    let mut source = Tokenizer::new(&odd, true).unwrap();
    let error = loop {
        match source.get() {
            Ok(Some(_)) => {}
            other => break other,
        }
    };
    let at = 1 << 20;
    assert_eq!(
        error,
        Err(BuildError::Invalid {
            at,
            why: Why::BadEscapeEnd
        })
    );
    assert_eq!(source.tell(), (1 << 20) - 2);
}

#[test]
fn code_points_and_positions_at_the_edges() {
    // No `str` holds these code points; a `u32` does, and a token is
    // whatever follows.
    let text = [u32::MAX, 0x5c, u32::MAX, 0x11_0000, 0xd800, 0x5c, 0];
    let mut source = Tokenizer::new(&text, true).unwrap();
    assert_eq!(
        source.get(),
        Ok(Some(Tok {
            esc: false,
            c: u32::MAX
        }))
    );
    assert_eq!(
        source.get(),
        Ok(Some(Tok {
            esc: true,
            c: u32::MAX
        }))
    );
    assert_eq!(source.matches('\u{10ffff}'), Ok(false));
    assert_eq!(
        source.getwhile(usize::MAX, |_| true),
        Ok(vec![0x11_0000, 0xd800])
    );
    // An escaped terminator is not the terminator.
    let at = 5;
    assert_eq!(
        source.getuntil('\0', Why::MissingGroupName),
        Err(BuildError::Invalid {
            at,
            why: Why::UnterminatedName
        })
    );
    // A seek past every pattern, an offset past every position. Python's
    // answers here are 2**64 - 1, 2**64 - 2 and 1 - 2**64, which no
    // `isize` holds: each is the end of an `isize` it lies past. The
    // offset is subtracted first, so 2**64 - 1 less 2**64 - 1 is 0.
    assert_eq!(source.seek(usize::MAX), Ok(()));
    assert_eq!((source.tell(), source.next), (isize::MAX, None));
    assert_eq!(source.get(), Ok(None));
    let placed = |source: &Tokenizer, offset: usize| match source.error(Why::BadEscape, offset) {
        BuildError::Invalid { at, .. } => at,
        other => panic!("{other:?}"),
    };
    assert_eq!(placed(&source, 1), isize::MAX);
    assert_eq!(placed(&source, usize::MAX), 0);
    assert_eq!(
        placed(&source, usize::MAX - isize::MAX as usize),
        isize::MAX
    );
    assert_eq!(
        placed(&source, usize::MAX - isize::MAX as usize + 1),
        isize::MAX - 1
    );
    assert_eq!(source.seek(1 << 63), Ok(()));
    assert_eq!(source.tell(), isize::MAX, "2**63 in the reference");
    assert_eq!(
        placed(&source, 1),
        isize::MAX,
        "2**63 - 1 is the last an isize holds"
    );
    assert_eq!(placed(&source, 2), isize::MAX - 1);
    assert_eq!(source.seek(0), Ok(()));
    assert_eq!(placed(&source, isize::MAX as usize + 1), isize::MIN);
    assert_eq!(
        placed(&source, isize::MAX as usize + 2),
        isize::MIN,
        "past the end"
    );
    assert_eq!(placed(&source, usize::MAX), isize::MIN);
    assert_eq!(source.seek(text.len() + 1), Ok(()));
    assert_eq!(source.tell(), 8);
    // The empty pattern.
    let mut source = Tokenizer::new(&[], false).unwrap();
    assert_eq!((source.tell(), source.next), (0, None));
    assert_eq!(source.getwhile(usize::MAX, |_| true), Ok(vec![]));
    assert_eq!(source.matches('a'), Ok(false));
}

#[test]
fn a_wide_tree_dumps_in_linear_time() {
    let wide = |n| SubPattern((0..n).map(|i| Node::Literal(i as u32)).collect());
    linear("dump", wide, |wide, n| {
        let text = wide.dump();
        assert!(text.starts_with("[(LITERAL, 0), (LITERAL, 1)"));
        assert_eq!(text.matches("LITERAL").count(), n);
        text
    });
    let set = |n| {
        let members = (0..n).map(|i| Member::Range(i as u32, u32::MAX)).collect();
        SubPattern(vec![Node::In(members)])
    };
    linear("set", set, |set, n| {
        let text = set.dump();
        assert_eq!(text.matches("RANGE").count(), n);
        text
    });
}

/// A tree `depth` levels deep: branches (the nested one between two
/// others), repeats, lookarounds and groups in turn around one `FAILURE`.
fn nested(depth: usize) -> SubPattern {
    let mut tree = SubPattern(vec![Node::Failure]);
    for level in 0..depth {
        tree = SubPattern(vec![match level % 4 {
            0 => Node::Branch(vec![
                SubPattern::default(),
                tree,
                SubPattern(vec![Node::Any]),
            ]),
            1 => Node::MaxRepeat(0, MAXREPEAT, tree),
            2 => Node::AssertNot(-1, tree),
            _ => Node::Subpattern {
                group: Some(level),
                add_flags: 0,
                del_flags: 0,
                body: tree,
            },
        }]);
    }
    tree
}

#[test]
fn nesting_past_the_parsers_bound_dumps_and_drops() {
    // The parser refuses nesting past 32 (pyre-3); eight times that.
    let tree = nested(256);
    let text = tree.dump();
    assert_eq!(text.matches("(FAILURE, ())").count(), 1);
    assert_eq!(text.matches("BRANCH").count(), 64);
    assert_eq!(text.matches('[').count(), text.matches(']').count());
    let copy = tree.clone();
    drop(tree);
    assert_eq!(copy.dump(), text);
}

#[test]
fn nesting_past_any_stack_dumps_copies_and_drops_on_a_small_one() {
    // The reference's `str(tree)` raises RecursionError from 998 levels.
    // Nothing here recurses per level: 200,000 levels on a thread of
    // 64 KiB, where a call per level would overflow within a few hundred.
    let run = || {
        let tree = nested(200_000);
        let text = tree.dump();
        assert_eq!(text.matches("(FAILURE, ())").count(), 1);
        assert_eq!(text.matches("(ANY, None)").count(), 50_000);
        assert_eq!(text.matches("BRANCH").count(), 50_000);
        assert_eq!(text.matches('[').count(), text.matches(']').count());
        assert!(text.starts_with("[(SUBPATTERN, (199999, 0, 0, [(ASSERT_NOT, (-1, [(MAX"));
        assert_eq!(format!("{tree:?}"), text);
        let item = &tree.0[0];
        assert_eq!(item.to_string(), text[1..text.len() - 1]);
        assert_eq!(format!("{item:?}"), item.to_string());
        let copy = tree.clone();
        let one = item.clone();
        assert!(!one.py_eq(item), "a copy is another object");
        drop(tree);
        assert_eq!(copy.dump(), text);
        assert_eq!(one.to_string(), text[1..text.len() - 1]);
    };
    let small = std::thread::Builder::new().stack_size(64 << 10);
    small.spawn(run).unwrap().join().unwrap();
    // And in time linear in the depth.
    linear("deep", nested, |tree, depth| {
        let copy = tree.clone();
        assert_eq!(copy.dump().matches("(FAILURE, ())").count(), 1);
        drop(copy);
        depth
    });
}

#[test]
fn numbers_at_the_edges_print_and_compare() {
    let odd = SubPattern(vec![
        Node::Literal(u32::MAX),
        Node::NotLiteral(0),
        Node::In(vec![Member::Range(u32::MAX, 0), Member::Literal(u32::MAX)]),
        Node::Subpattern {
            group: Some(usize::MAX),
            add_flags: u32::MAX,
            del_flags: u32::MAX,
            body: SubPattern::default(),
        },
        Node::MinRepeat(MAXREPEAT - 1, MAXREPEAT - 1, SubPattern::default()),
        Node::Assert(i8::MIN, SubPattern::default()),
        Node::AssertNot(i8::MAX, SubPattern::default()),
    ]);
    let text = odd.dump();
    assert!(text
        .starts_with("[(LITERAL, 4294967295), (NOT_LITERAL, 0), (IN, [(RANGE, (4294967295, 0))"));
    assert!(text.contains(&format!(
        "(SUBPATTERN, ({}, 4294967295, 4294967295, []))",
        usize::MAX
    )));
    assert!(text.contains("(MIN_REPEAT, (4294967294, 4294967294, []))"));
    assert!(text.ends_with("(ASSERT, (-128, [])), (ASSERT_NOT, (127, []))]"));
    assert!(odd.0[0].py_eq(&Node::Literal(u32::MAX)));
    assert!(odd.0[3].py_eq(&odd.0[3]), "the same object");
    assert!(!odd.0[3].py_eq(&odd.0[3].clone()), "another object");
}

#[test]
fn a_state_with_many_groups_stays_linear() {
    let names = |n| (0..n).map(|i| [0x67, i as u32]).collect::<Vec<_>>();
    linear("opengroup", names, |names, n| {
        let mut state = State::new();
        for (i, name) in names.iter().enumerate() {
            assert_eq!(state.opengroup(Some(name)), Ok(i + 1));
            assert!(state.closegroup(i + 1, (0, MAXWIDTH)));
        }
        assert_eq!(state.groups(), n + 1);
        assert_eq!(state.opengroup(Some(&[0x67, 0])), Err(Why::Redefinition));
        assert!(state.checkgroup(n) && !state.checkgroup(n + 1));
        state
    });
}

#[test]
fn a_long_group_name_is_kept_in_linear_time() {
    // Each time a name is opened it is copied and hashed once, and
    // compared once when it is taken.
    let name = |n| (0..10 * n as u32).collect::<Vec<u32>>();
    linear("name", name, |name, _| {
        let mut state = State::new();
        assert_eq!(state.opengroup(Some(name)), Ok(1));
        assert_eq!(state.opengroup(Some(name)), Err(Why::Redefinition));
        assert_eq!(state.opengroup(Some(&name[1..])), Ok(3));
        assert_eq!(state.groupdict.len(), 2);
        state
    });
}

#[test]
fn group_numbers_past_every_group() {
    let mut state = State::new();
    assert!(!state.checkgroup(usize::MAX));
    assert!(!state.closegroup(usize::MAX, (u128::MAX, u128::MAX)));
    assert_eq!(state.checklookbehindgroup(usize::MAX), Ok(()));
    state.lookbehindgroups = Some(usize::MAX);
    assert_eq!(state.checklookbehindgroup(usize::MAX), Err(Why::OpenGroup));
    state.lookbehindgroups = Some(0);
    assert_eq!(state.checklookbehindgroup(0), Err(Why::OpenGroup));
    // The limit on groups is past what a test can open: `opengroup`
    // passes it to `opengroup_within`, which the kills file bounds at 3.
    assert_eq!(MAXGROUPS, (i32::MAX / 2) as usize);
    assert_eq!(
        state.opengroup_within(None, usize::MAX),
        Ok(1),
        "no bound overflows"
    );
    assert_eq!(state.opengroup_within(None, 0), Err(Why::TooManyGroups));
    assert!(state.closegroup(0, (0, 0)));
    assert_eq!(state.checklookbehindgroup(0), Err(Why::LookbehindGroup));
}
