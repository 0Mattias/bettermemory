//! Cases a plausible wrong `pyre::tree` or `pyre::token` fails. Each
//! expected value is what CPython 3.13.13's `re._parser` answers; the
//! golden test pins the same facts from the interpreter, and these name
//! the wrong reading each one rules out.

use bettermemory_core::pyre::token::{Tok, Tokenizer};
use bettermemory_core::pyre::tree::{
    At, BuildError, Category, Member, Node, State, SubPattern, Why, MAXREPEAT,
};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn lit(c: char) -> Node {
    Node::Literal(u32::from(c))
}

fn sub(items: Vec<Node>) -> SubPattern {
    SubPattern(items)
}

fn invalid(at: isize, why: Why) -> BuildError {
    BuildError::Invalid { at, why }
}

/// A dump that printed the unbounded maximum as its number (4294967295),
/// as `inf`, or printed a written bound of 4294967294 by name.
#[test]
fn an_unbounded_repeat_prints_by_name_and_a_written_bound_by_number() {
    let star = sub(vec![Node::MaxRepeat(0, MAXREPEAT, sub(vec![lit('a')]))]);
    assert_eq!(
        star.dump(),
        "[(MAX_REPEAT, (0, MAXREPEAT, [(LITERAL, 97)]))]"
    );
    let written = sub(vec![Node::MinRepeat(1, MAXREPEAT - 1, sub(vec![lit('a')]))]);
    assert_eq!(
        written.dump(),
        "[(MIN_REPEAT, (1, 4294967294, [(LITERAL, 97)]))]"
    );
    // The minimum is a number whatever it is.
    let odd = sub(vec![Node::MaxRepeat(MAXREPEAT, 0, sub(vec![]))]);
    assert_eq!(odd.dump(), "[(MAX_REPEAT, (4294967295, 0, []))]");
}

/// A dump in another spelling: a trailing comma, `null` for None, a
/// branch without its `None`, an empty alternative dropped, a group's
/// flags left out, a direction of 1 for a lookbehind.
#[test]
fn the_dump_is_the_repr_of_the_list() {
    assert_eq!(sub(vec![]).dump(), "[]");
    assert_eq!(sub(vec![Node::Any]).dump(), "[(ANY, None)]");
    assert_eq!(sub(vec![Node::Failure]).dump(), "[(FAILURE, ())]");
    let branch = Node::Branch(vec![sub(vec![lit('a')]), sub(vec![]), sub(vec![lit('b')])]);
    assert_eq!(
        sub(vec![branch]).dump(),
        "[(BRANCH, (None, [[(LITERAL, 97)], [], [(LITERAL, 98)]]))]"
    );
    let scoped = Node::Subpattern {
        group: None,
        add_flags: 2,
        del_flags: 64,
        body: sub(vec![Node::Any]),
    };
    let numbered = Node::Subpattern {
        group: Some(10),
        add_flags: 0,
        del_flags: 0,
        body: sub(vec![]),
    };
    assert_eq!(
        sub(vec![scoped, numbered]).dump(),
        "[(SUBPATTERN, (None, 2, 64, [(ANY, None)])), (SUBPATTERN, (10, 0, 0, []))]"
    );
    let behind = Node::AssertNot(-1, sub(vec![Node::NotLiteral(10)]));
    let ahead = Node::Assert(1, sub(vec![Node::At(At::AT_END_STRING)]));
    assert_eq!(
        sub(vec![behind, ahead]).dump(),
        "[(ASSERT_NOT, (-1, [(NOT_LITERAL, 10)])), (ASSERT, (1, [(AT, AT_END_STRING)]))]"
    );
}

/// A set printed sorted, with a range as two numbers outside a tuple, or
/// with the negation as a flag instead of the first member.
#[test]
fn a_set_prints_its_members_in_order() {
    let set = Node::In(vec![
        Member::Negate,
        Member::Range(122, 97),
        Member::Literal(0x10ffff),
        Member::Category(Category::CATEGORY_NOT_WORD),
        Member::Literal(0),
    ]);
    assert_eq!(
        sub(vec![set]).dump(),
        "[(IN, [(NEGATE, None), (RANGE, (122, 97)), (LITERAL, 1114111), \
         (CATEGORY, CATEGORY_NOT_WORD), (LITERAL, 0)])]"
    );
}

/// A tree with derived equality: `a+b|a+c` would lose its common prefix,
/// and `re.match('a*ab|a*', 'aab')` would answer `aa` for `aab`.
#[test]
fn an_item_that_holds_a_subpattern_equals_no_other() {
    let plus = || Node::MaxRepeat(1, MAXREPEAT, sub(vec![lit('a')]));
    let (one, other) = (plus(), plus());
    assert_eq!(sub(vec![plus()]).dump(), sub(vec![plus()]).dump());
    assert!(!one.py_eq(&other), "the same text, two objects");
    assert!(one.py_eq(&one), "an object equals itself");
    let group = || Node::Subpattern {
        group: None,
        add_flags: 2,
        del_flags: 0,
        body: sub(vec![]),
    };
    let pairs = [
        (group(), group()),
        (Node::Assert(1, sub(vec![])), Node::Assert(1, sub(vec![]))),
        (
            Node::AssertNot(-1, sub(vec![])),
            Node::AssertNot(-1, sub(vec![])),
        ),
        (
            Node::MinRepeat(0, 1, sub(vec![])),
            Node::MinRepeat(0, 1, sub(vec![])),
        ),
        // A branch holds a list of sub-patterns: one alternative each,
        // or one against none, and the two lists differ.
        (
            Node::Branch(vec![sub(vec![])]),
            Node::Branch(vec![sub(vec![])]),
        ),
        (Node::Branch(vec![]), Node::Branch(vec![sub(vec![])])),
        (
            Node::Branch(vec![sub(vec![]), sub(vec![])]),
            Node::Branch(vec![sub(vec![])]),
        ),
    ];
    for (a, b) in &pairs {
        assert!(!a.py_eq(b), "{a:?}");
        assert!(!b.py_eq(a), "{a:?}");
    }
    // By value where no sub-pattern is held.
    assert!(lit('a').py_eq(&lit('a')));
    assert!(!lit('a').py_eq(&lit('b')));
    assert!(!lit('a').py_eq(&Node::NotLiteral(97)));
    assert!(Node::Any.py_eq(&Node::Any));
    assert!(Node::Failure.py_eq(&Node::Failure));
    assert!(!Node::Any.py_eq(&Node::Failure));
    assert!(Node::At(At::AT_END).py_eq(&Node::At(At::AT_END)));
    assert!(!Node::At(At::AT_END).py_eq(&Node::At(At::AT_END_STRING)));
    // A set by its members in their order: `[ab]` is not `[ba]`.
    let ab = Node::In(vec![Member::Literal(97), Member::Literal(98)]);
    let ba = Node::In(vec![Member::Literal(98), Member::Literal(97)]);
    assert!(ab.py_eq(&ab.clone()));
    assert!(!ab.py_eq(&ba));
    assert!(!Node::In(vec![Member::Literal(97)]).py_eq(&lit('a')));
}

/// A copy that leaves out a sub-pattern: the alternatives of a branch
/// copied empty, or only the first of them copied. The copy prints as
/// the tree does, and is another object.
#[test]
fn a_copy_holds_every_sub_pattern() {
    let group = Node::Subpattern {
        group: Some(1),
        add_flags: 0,
        del_flags: 0,
        body: sub(vec![lit('c'), Node::Any]),
    };
    let branch = Node::Branch(vec![
        sub(vec![lit('a')]),
        sub(vec![]),
        sub(vec![Node::MinRepeat(0, 2, sub(vec![group, lit('b')]))]),
    ]);
    let tree = sub(vec![Node::Assert(-1, sub(vec![branch])), lit('d')]);
    let text = "[(ASSERT, (-1, [(BRANCH, (None, [[(LITERAL, 97)], [], [(MIN_REPEAT, \
                (0, 2, [(SUBPATTERN, (1, 0, 0, [(LITERAL, 99), (ANY, None)])), \
                (LITERAL, 98)]))]]))])), (LITERAL, 100)]";
    assert_eq!(tree.dump(), text);
    let copy = tree.clone();
    assert_eq!(copy.dump(), text);
    let item = text.strip_prefix('[').unwrap();
    let item = item.strip_suffix(", (LITERAL, 100)]").unwrap();
    assert_eq!(tree.0[0].clone().to_string(), item);
    assert!(!copy.0[0].py_eq(&tree.0[0]));
    assert!(copy.0[1].py_eq(&tree.0[1]));
}

/// An equality answered by the address alone: an item without a
/// sub-pattern that equals only itself, or every branch unequal to every
/// other. `(BRANCH, (None, [])) == (BRANCH, (None, []))` is True in
/// Python: two lists of no alternative. Each pair here is two elements of
/// one list, so no pair is one object.
#[test]
fn an_item_without_a_subpattern_equals_its_like_at_another_address() {
    let set = || Node::In(vec![Member::Negate, Member::Range(97, 98)]);
    let pairs = [
        [Node::Any, Node::Any],
        [Node::Failure, Node::Failure],
        [lit('a'), lit('a')],
        [Node::NotLiteral(0), Node::NotLiteral(0)],
        [Node::At(At::AT_BOUNDARY), Node::At(At::AT_BOUNDARY)],
        [set(), set()],
        [Node::In(vec![]), Node::In(vec![])],
        [Node::Branch(vec![]), Node::Branch(vec![])],
    ];
    for [a, b] in &pairs {
        assert!(!std::ptr::eq(a, b), "{a:?}");
        assert!(a.py_eq(b), "{a:?}");
        assert!(b.py_eq(a), "{a:?}");
    }
    // No two of the eight kinds are equal to each other.
    for (i, [a, _]) in pairs.iter().enumerate() {
        for [_, b] in &pairs[i + 1..] {
            assert!(!a.py_eq(b) && !b.py_eq(a), "{a:?} {b:?}");
        }
    }
}

/// A set compared by its length, by its first member, without its
/// negation, by the low end of a range alone, or with every category
/// alike.
#[test]
fn a_set_differs_from_each_near_miss() {
    let set = |members: &[Member]| Node::In(members.to_vec());
    let (a, b, c) = (
        Member::Literal(97),
        Member::Literal(98),
        Member::Literal(99),
    );
    let digit = Member::Category(Category::CATEGORY_DIGIT);
    let space = Member::Category(Category::CATEGORY_SPACE);
    let near = [
        (set(&[a, b]), set(&[Member::Negate, a, b])),
        (set(&[a, b]), set(&[a, b, c])),
        (set(&[a, b]), set(&[a, c])),
        (set(&[a, b]), set(&[Member::Range(97, 98)])),
        (set(&[Member::Range(97, 98)]), set(&[Member::Range(97, 99)])),
        (set(&[Member::Range(97, 98)]), set(&[Member::Range(96, 98)])),
        (set(&[digit]), set(&[space])),
        (
            set(&[digit]),
            set(&[Member::Category(Category::CATEGORY_NOT_DIGIT)]),
        ),
        (set(&[Member::Negate]), set(&[])),
        (set(&[a]), set(&[Member::Range(97, 97)])),
    ];
    for (one, other) in &near {
        assert!(!one.py_eq(other), "{one:?} {other:?}");
        assert!(!other.py_eq(one), "{one:?} {other:?}");
    }
}

/// A state that starts with a flag set, a group or a lookbehind:
/// `State.__init__` sets `flags = 0`, and `parse` ors the pattern's flags
/// into whatever is there.
#[test]
fn a_new_state_holds_group_0_and_nothing_else() {
    let state = State::new();
    assert_eq!(state.flags, 0);
    assert!(state.groupdict.is_empty());
    assert_eq!(state.groupwidths, vec![None]);
    assert_eq!(state.lookbehindgroups, None);
    assert_eq!(State::default().flags, 0);
    assert_eq!(State::default().groups(), 1);
}

/// "too many groups" raised at the bound instead of past it, never
/// raised, raised before the group is counted, or raised after the name
/// is looked at. With `MAXGROUPS` set to 3 the reference opens groups 1
/// and 2 and refuses every later one, a name already taken included.
#[test]
fn a_group_past_the_bound_is_counted_then_refused() {
    let mut state = State::new();
    assert_eq!(state.opengroup_within(None, 3), Ok(1));
    assert_eq!(state.opengroup_within(Some(&cps("a")), 3), Ok(2));
    assert_eq!(state.groups(), 3, "at the bound, not past it");
    assert_eq!(
        state.opengroup_within(Some(&cps("b")), 3),
        Err(Why::TooManyGroups)
    );
    assert_eq!(state.groups(), 4, "the refused group was appended");
    assert!(
        !state.groupdict.contains_key(&cps("b")),
        "its name was not kept"
    );
    assert_eq!(
        state.opengroup_within(Some(&cps("a")), 3),
        Err(Why::TooManyGroups),
        "the bound is asked before the name"
    );
    assert_eq!(state.groups(), 5);
    assert_eq!(state.groupdict.len(), 1);
    // The same state under the bound of the parser: the next group opens.
    assert_eq!(state.opengroup(Some(&cps("b"))), Ok(5));
    assert_eq!(state.opengroup(Some(&cps("b"))), Err(Why::Redefinition));
    // A bound of nothing refuses group 1.
    let mut none = State::new();
    assert_eq!(none.opengroup_within(None, 0), Err(Why::TooManyGroups));
    assert_eq!(none.opengroup_within(None, 1), Err(Why::TooManyGroups));
    assert_eq!(none.groups(), 3);
}

/// A state that refuses a second name before it counts the group, or
/// that counts group 0 as closed.
#[test]
fn a_group_is_counted_before_its_name_is_refused() {
    let mut state = State::new();
    assert_eq!(state.groups(), 1);
    assert!(!state.checkgroup(0), "group 0 has no width");
    assert_eq!(state.opengroup(Some(&cps("a"))), Ok(1));
    assert_eq!(state.opengroup(Some(&cps("a"))), Err(Why::Redefinition));
    assert_eq!(state.groups(), 3, "the refused group was appended");
    assert_eq!(state.opengroup(Some(&cps("A"))), Ok(3));
    assert_eq!(state.opengroup(None), Ok(4));
    assert_eq!(state.groupdict.len(), 2);
    assert_eq!(state.groupdict[&cps("a")], 1);
    assert_eq!(state.groupdict[&cps("A")], 3);
}

/// A group that counts as closed once opened, or a width of zero read as
/// no width.
#[test]
fn a_group_is_closed_by_its_width_alone() {
    let mut state = State::new();
    assert_eq!(state.opengroup(None), Ok(1));
    assert!(!state.checkgroup(1), "open");
    assert!(state.closegroup(1, (0, 0)));
    assert!(state.checkgroup(1), "closed with a width of nothing");
    assert!(!state.checkgroup(2), "never opened");
    assert!(!state.closegroup(2, (1, 1)), "IndexError in the reference");
    assert_eq!(state.groupwidths, vec![None, Some((0, 0))]);
    // Closed again, the group has the second width, not the first.
    assert!(state.closegroup(1, (2, 5)));
    assert_eq!(state.groupwidths, vec![None, Some((2, 5))]);
    assert!(state.closegroup(1, (0, 0)));
    assert_eq!(state.groupwidths, vec![None, Some((0, 0))]);
}

/// The lookbehind check in another order, or made outside a lookbehind.
#[test]
fn the_lookbehind_check_asks_open_first_and_only_inside_one() {
    let mut state = State::new();
    assert_eq!(state.opengroup(None), Ok(1));
    assert_eq!(state.opengroup(None), Ok(2));
    assert!(state.closegroup(1, (1, 1)));
    // Outside a lookbehind nothing is asked, even of a group that is open
    // or does not exist.
    assert_eq!(state.checklookbehindgroup(2), Ok(()));
    assert_eq!(state.checklookbehindgroup(99), Ok(()));
    state.lookbehindgroups = Some(1);
    // Group 2 is open and was opened inside: "open" is the answer.
    assert_eq!(state.checklookbehindgroup(2), Err(Why::OpenGroup));
    assert_eq!(state.checklookbehindgroup(99), Err(Why::OpenGroup));
    // Group 1 is closed and is the first of the lookbehind's own.
    assert_eq!(state.checklookbehindgroup(1), Err(Why::LookbehindGroup));
    state.lookbehindgroups = Some(2);
    assert_eq!(state.checklookbehindgroup(1), Ok(()));
}

/// A tokenizer whose `tell` is the index after the next token, or that
/// reads a backslash and its character as two tokens.
#[test]
fn tell_is_where_the_next_token_begins() {
    let text = cps("a\\bc");
    let mut source = Tokenizer::new(&text, true).unwrap();
    assert_eq!(
        (source.tell(), source.next),
        (0, Some(Tok { esc: false, c: 97 }))
    );
    assert_eq!(source.get(), Ok(Some(Tok { esc: false, c: 97 })));
    assert_eq!(
        (source.tell(), source.next),
        (1, Some(Tok { esc: true, c: 98 }))
    );
    assert_eq!(source.get(), Ok(Some(Tok { esc: true, c: 98 })));
    assert_eq!(
        (source.tell(), source.next),
        (3, Some(Tok { esc: false, c: 99 }))
    );
    assert_eq!(source.get(), Ok(Some(Tok { esc: false, c: 99 })));
    assert_eq!((source.tell(), source.next), (4, None));
    assert_eq!(source.get(), Ok(None));
    assert_eq!(source.tell(), 4);
}

/// A tokenizer that reports the lone backslash when it is consumed, at
/// its own index minus one, or that moves on after raising.
#[test]
fn a_final_backslash_raises_when_it_is_loaded_and_changes_nothing() {
    assert_eq!(
        Tokenizer::new(&cps("\\"), true).err(),
        Some(invalid(0, Why::BadEscapeEnd))
    );
    let text = cps("ab\\");
    let mut source = Tokenizer::new(&text, true).unwrap();
    assert_eq!(source.get(), Ok(Some(Tok { esc: false, c: 97 })));
    // Consuming `b` loads the backslash: the error is at the last index,
    // and `b` is still the next token.
    assert_eq!(source.get(), Err(invalid(2, Why::BadEscapeEnd)));
    assert_eq!(
        (source.tell(), source.next),
        (1, Some(Tok { esc: false, c: 98 }))
    );
    assert_eq!(source.matches('b'), Err(invalid(2, Why::BadEscapeEnd)));
    assert_eq!(
        source.getwhile(1, |c| c == 98),
        Err(invalid(2, Why::BadEscapeEnd))
    );
    assert_eq!(source.tell(), 1);
    // An escaped backslash before the end is one token and no error.
    let text = cps("\\\\");
    let mut source = Tokenizer::new(&text, true).unwrap();
    assert_eq!(source.get(), Ok(Some(Tok { esc: true, c: 0x5c })));
    assert_eq!(source.get(), Ok(None));
}

/// `match` that treats an escaped character as the character, or that
/// consumes on a miss.
#[test]
fn match_takes_the_bare_character_only() {
    let text = cps("\\-a-");
    let mut source = Tokenizer::new(&text, true).unwrap();
    assert_eq!(source.matches('-'), Ok(false), "an escaped hyphen");
    assert_eq!(source.tell(), 0);
    assert_eq!(source.get(), Ok(Some(Tok { esc: true, c: 45 })));
    assert_eq!(source.matches('-'), Ok(false));
    assert_eq!(source.matches('a'), Ok(true));
    assert_eq!(source.matches('-'), Ok(true));
    assert_eq!(source.matches('-'), Ok(false), "the end");
    assert!(Tok { esc: false, c: 45 }.is('-'));
    assert!(!Tok { esc: true, c: 45 }.is('-'));
    assert_eq!(Tok { esc: true, c: 45 }.width(), 2);
    assert_eq!(
        Tok {
            esc: false,
            c: 0x1f600
        }
        .width(),
        1
    );
}

/// `getwhile` that takes an escaped digit, runs past `n`, or reads a
/// digit past ASCII as one.
#[test]
fn getwhile_takes_at_most_n_bare_characters_of_the_set() {
    let hex = |c: u32| char::from_u32(c).is_some_and(|c| c.is_ascii_hexdigit());
    let text = cps("4aF\\19");
    let mut source = Tokenizer::new(&text, true).unwrap();
    assert_eq!(source.getwhile(0, hex), Ok(vec![]));
    assert_eq!(source.getwhile(2, hex), Ok(cps("4a")));
    assert_eq!(source.getwhile(8, hex), Ok(cps("F")), "stops at the escape");
    assert_eq!(source.tell(), 3);
    let text = vec![0xff11, 0x31];
    let mut source = Tokenizer::new(&text, true).unwrap();
    let digit = |c: u32| (0x30..0x3a).contains(&c);
    assert_eq!(source.getwhile(2, digit), Ok(vec![]), "U+FF11 is no digit");
    assert_eq!(source.tell(), 0);
}

/// `getuntil` that drops the backslash of an escaped character, takes an
/// escaped terminator for the end, or places its errors elsewhere.
#[test]
fn getuntil_keeps_escapes_and_places_its_three_errors() {
    let missing = Why::MissingGroupName;
    let text = cps("a\\>b>c");
    let mut source = Tokenizer::new(&text, true).unwrap();
    assert_eq!(source.getuntil('>', missing), Ok(cps("a\\>b")));
    assert_eq!(source.tell(), 5, "the terminator is consumed");
    // The terminator first: the error is at the terminator.
    let text = cps("x>y");
    let mut source = Tokenizer::new(&text, true).unwrap();
    source.get().unwrap();
    assert_eq!(source.getuntil('>', missing), Err(invalid(1, missing)));
    // The end first: the error is at the end, under the name asked for.
    let mut source = Tokenizer::new(&[], true).unwrap();
    assert_eq!(
        source.getuntil('}', Why::MissingCharacterName),
        Err(invalid(0, Why::MissingCharacterName))
    );
    // No terminator: the error is where the name began, and an escape
    // counts as the two characters it is.
    let text = cps("(a\\bc");
    let mut source = Tokenizer::new(&text, true).unwrap();
    source.get().unwrap();
    assert_eq!(
        source.getuntil(')', missing),
        Err(invalid(1, Why::UnterminatedName))
    );
    assert_eq!(source.tell(), 5);
}

/// A `seek` that leaves the index alone when it fails, or positions kept
/// unsigned: after `seek(1)` on an escaped backslash the reference's
/// `tell()` is -1 and an error raised there is placed below zero.
#[test]
fn a_failed_seek_has_moved_the_index() {
    let text = cps("\\\\");
    let mut source = Tokenizer::new(&text, true).unwrap();
    assert_eq!(source.seek(1), Err(invalid(1, Why::BadEscapeEnd)));
    assert_eq!(source.tell(), -1);
    assert_eq!(source.next, Some(Tok { esc: true, c: 0x5c }));
    assert_eq!(source.error(Why::BadEscape, 2), invalid(-3, Why::BadEscape));
    // A seek that succeeds reads from any index, a boundary or not.
    let text = cps("a\\bc");
    let mut source = Tokenizer::new(&text, true).unwrap();
    assert_eq!(source.seek(2), Ok(()));
    assert_eq!(
        (source.tell(), source.next),
        (2, Some(Tok { esc: false, c: 98 }))
    );
    assert_eq!(source.seek(9), Ok(()));
    assert_eq!((source.tell(), source.next), (9, None));
    assert_eq!(source.seek(1), Ok(()));
    assert_eq!(
        (source.tell(), source.next),
        (1, Some(Tok { esc: true, c: 98 }))
    );
}

/// An error placed by a difference of two numbers each narrowed first,
/// or wrapped: the reference subtracts integers without a bound. On
/// `Tokenizer('abc')`, `error(msg, 2**63).pos` is -2**63, after one `get`
/// it is 1 - 2**63, and after `seek(2**63 - 1)` an offset of 2**64 - 1
/// gives -2**63.
#[test]
fn an_error_is_placed_by_the_exact_difference() {
    let at = |source: &Tokenizer, offset: usize| match source.error(Why::BadEscape, offset) {
        BuildError::Invalid { at, .. } => at,
        other => panic!("{other:?}"),
    };
    let half = 1_usize << 63;
    let text = cps("abc");
    let mut source = Tokenizer::new(&text, true).unwrap();
    assert_eq!(at(&source, half), isize::MIN);
    assert_eq!(at(&source, half - 1), isize::MIN + 1);
    source.get().unwrap();
    assert_eq!(at(&source, half), isize::MIN + 1);
    source.get().unwrap();
    assert_eq!(at(&source, half + 2), isize::MIN);
    assert_eq!(source.seek(half - 1), Ok(()));
    assert_eq!(source.tell(), isize::MAX);
    assert_eq!(at(&source, 0), isize::MAX);
    assert_eq!(at(&source, half - 1), 0);
    assert_eq!(at(&source, half), -1);
    assert_eq!(at(&source, usize::MAX), isize::MIN);
}

/// A bytes pattern read otherwise than its bytes as code points.
#[test]
fn a_bytes_pattern_is_its_bytes() {
    let text: Vec<u32> = [0xff, 0x5c, 0xfe, 0x00].to_vec();
    let mut source = Tokenizer::new(&text, false).unwrap();
    assert!(!source.istext);
    assert_eq!(
        source.get(),
        Ok(Some(Tok {
            esc: false,
            c: 0xff
        }))
    );
    assert_eq!(source.get(), Ok(Some(Tok { esc: true, c: 0xfe })));
    assert_eq!(source.get(), Ok(Some(Tok { esc: false, c: 0 })));
    assert_eq!(source.error(Why::BadEscape, 1), invalid(3, Why::BadEscape));
}
