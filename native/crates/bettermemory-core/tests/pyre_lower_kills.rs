//! Cases a plausible wrong lowering fails: each names the wrong reading it
//! tells apart from `re._compiler` (CPython 3.13.13).

use bettermemory_core::pyre::fold::Lit;
use bettermemory_core::pyre::lower::{
    combine_flags, lower, simple, Anchor, Inst, LowerError, Op, Program, Unit, MAX_NODES,
};
use bettermemory_core::pyre::tree::{
    At, Feature, Member, Node, SubPattern, ASCII, DOTALL, IGNORECASE, MAXREPEAT, MULTILINE, UNICODE,
};

const U: u32 = UNICODE;

fn sp(nodes: Vec<Node>) -> SubPattern {
    SubPattern(nodes)
}

fn lit(c: char) -> Node {
    Node::Literal(c as u32)
}

fn lits(s: &str) -> SubPattern {
    sp(s.chars().map(lit).collect())
}

fn group(g: Option<usize>, add: u32, del: u32, body: SubPattern) -> Node {
    Node::Subpattern {
        group: g,
        add_flags: add,
        del_flags: del,
        body,
    }
}

fn prog(tree: Vec<Node>, flags: u32) -> Program {
    lower(&sp(tree), flags, false).expect("lowers")
}

fn at(p: &Program, i: usize) -> &Inst {
    &p.insts[i]
}

/// `(?i:a)*` is REPEAT_ONE: `_simple` looks through a group with scoped
/// flags (a prototype that declined them gave a different window answer).
#[test]
fn simple_looks_through_scoped_flags() {
    let tree = vec![Node::MaxRepeat(
        0,
        MAXREPEAT,
        sp(vec![group(None, IGNORECASE, 0, lits("a"))]),
    )];
    let p = prog(tree, U);
    assert_eq!(p.trace, ["REPEAT_ONE", "LITERAL_UNI_IGNORE"]);
    let Op::Rep1 { unit, .. } = &at(&p, p.start).op else {
        panic!("not one node: {p:?}")
    };
    assert!(
        matches!(unit, Unit::Char(Lit::Fold { lower: 97, .. })),
        "{unit:?}"
    );
}

/// `(a)?` is REPEAT: a capture is not `_simple`, so it is unrolled.
#[test]
fn capture_is_not_simple() {
    let body = sp(vec![group(Some(1), 0, 0, lits("a"))]);
    assert!(simple(&body, U).is_none());
    let p = prog(vec![Node::MaxRepeat(0, 1, body)], U);
    assert_eq!(p.trace, ["MAX_UNTIL"]);
    assert_eq!(p.insts.len(), 5);
}

/// Group 0 is not None, so not `_simple`; and `if group:` marks nothing.
#[test]
fn group_zero_is_neither_simple_nor_marked() {
    let body = sp(vec![group(Some(0), 0, 0, lits("a"))]);
    assert!(simple(&body, U).is_none());
    let p = prog(vec![Node::MaxRepeat(0, MAXREPEAT, body)], U);
    assert_eq!(p.trace, ["MAX_UNTIL"]);
    assert!(p.insts.iter().all(|i| !matches!(i.op, Op::Mark(_))));
    assert_eq!(p.insts.len(), 3);
}

/// Group 2 marks slots 2 and 3, around its body.
#[test]
fn mark_slots() {
    let p = prog(vec![group(Some(2), 0, 0, lits("a"))], U);
    assert_eq!(at(&p, p.start).op, Op::Mark(2));
    let body = at(&p, p.start).next;
    assert!(matches!(
        at(&p, body).op,
        Op::Unit(Unit::Char(Lit::Exact(97)))
    ));
    assert_eq!(at(&p, at(&p, body).next).op, Op::Mark(3));
}

/// A greedy optional tries the body first; a lazy one the exit first.
#[test]
fn greedy_and_lazy_split_order() {
    let g = prog(vec![Node::MaxRepeat(0, 1, lits("ab")), lit('c')], U);
    let Op::Split(other) = at(&g, g.start).op else {
        panic!("{g:?}")
    };
    assert!(matches!(
        at(&g, at(&g, g.start).next).op,
        Op::Unit(Unit::Char(Lit::Exact(97)))
    ));
    assert!(matches!(
        at(&g, other).op,
        Op::Unit(Unit::Char(Lit::Exact(99)))
    ));
    let l = prog(vec![Node::MinRepeat(0, 1, lits("ab")), lit('c')], U);
    let Op::Split(other) = at(&l, l.start).op else {
        panic!("{l:?}")
    };
    assert!(matches!(
        at(&l, at(&l, l.start).next).op,
        Op::Unit(Unit::Char(Lit::Exact(99)))
    ));
    assert!(matches!(
        at(&l, other).op,
        Op::Unit(Unit::Char(Lit::Exact(97)))
    ));
}

/// `(?:ab)*`: the body returns to the loop node.
#[test]
fn unbounded_loop_returns_to_itself() {
    let p = prog(vec![Node::MaxRepeat(0, MAXREPEAT, lits("ab"))], U);
    let lp = p.start;
    let Op::Split(exit) = at(&p, lp).op else {
        panic!("{p:?}")
    };
    assert_eq!(at(&p, exit).op, Op::Match);
    let b = at(&p, at(&p, lp).next).next;
    assert_eq!(at(&p, b).next, lp);
}

/// `(?:ab){2,4}` is `ab ab (ab (ab)?)?`: each optional skips to the exit,
/// not to the next optional, and two forced copies come first.
#[test]
fn bounded_unrolling() {
    let p = prog(vec![Node::MaxRepeat(2, 4, lits("ab"))], U);
    assert_eq!(p.insts.len(), 1 + 2 * 2 + 2 * 3);
    let mut pc = p.start;
    for _ in 0..4 {
        assert!(matches!(at(&p, pc).op, Op::Unit(_)), "{p:?}");
        pc = at(&p, pc).next;
    }
    for _ in 0..2 {
        let Op::Split(exit) = at(&p, pc).op else {
            panic!("{p:?}")
        };
        assert_eq!(at(&p, exit).op, Op::Match);
        pc = at(&p, at(&p, at(&p, pc).next).next).next;
    }
    assert_eq!(at(&p, pc).op, Op::Match);
}

/// A branch tries its alternatives in order.
#[test]
fn branch_order() {
    let p = prog(vec![Node::Branch(vec![lits("a"), lits("b"), lits("c")])], U);
    let first = at(&p, p.start);
    assert!(matches!(
        at(&p, first.next).op,
        Op::Unit(Unit::Char(Lit::Exact(97)))
    ));
    let Op::Split(rest) = first.op else { panic!() };
    assert!(matches!(
        at(&p, at(&p, rest).next).op,
        Op::Unit(Unit::Char(Lit::Exact(98)))
    ));
    let empty = prog(vec![Node::Branch(vec![])], U);
    assert_eq!(at(&empty, empty.start).op, Op::Fail);
}

/// `_combine_flags` drops the type flags when a type flag is added.
#[test]
fn combine_flags_type_flags() {
    assert_eq!(combine_flags(U | MULTILINE, ASCII, 0), ASCII | MULTILINE);
    assert_eq!(combine_flags(U, DOTALL, 0), U | DOTALL);
    assert_eq!(combine_flags(U | DOTALL, 0, DOTALL), U);
    let tree = vec![group(None, ASCII, 0, lits("a"))];
    assert_eq!(
        lower(&sp(tree), U, false),
        Err(LowerError::Unsupported(Feature::Ascii))
    );
}

/// DOTALL taken off inside a group is ANY there.
#[test]
fn dotall_scoped() {
    let p = prog(
        vec![Node::Any, group(None, 0, DOTALL, sp(vec![Node::Any]))],
        U | DOTALL,
    );
    assert_eq!(p.trace, ["ANY_ALL", "ANY"]);
}

/// MULTILINE maps the two anchors that have a line form, and `\b` is the
/// Unicode boundary.
#[test]
fn anchors() {
    let tree = || {
        vec![
            Node::At(At::AT_BEGINNING),
            Node::At(At::AT_END),
            Node::At(At::AT_BOUNDARY),
        ]
    };
    assert_eq!(
        prog(tree(), U).trace,
        ["AT_BEGINNING", "AT_END", "AT_UNI_BOUNDARY"]
    );
    let p = prog(tree(), U | MULTILINE);
    assert_eq!(
        p.trace,
        ["AT_BEGINNING_LINE", "AT_END_LINE", "AT_UNI_BOUNDARY"]
    );
    assert_eq!(at(&p, p.start).op, Op::At(Anchor::AT_BEGINNING_LINE));
    let b = lower(&sp(vec![Node::At(At::AT_BOUNDARY)]), 0, true);
    assert_eq!(b, Err(LowerError::Unsupported(Feature::BytesBoundary)));
    assert!(lower(&sp(vec![Node::At(At::AT_BEGINNING)]), 0, true).is_ok());
}

/// A loop over a body that can match empty is refused only past one.
#[test]
fn nullable_loop_only_past_one() {
    let star = || sp(vec![Node::MaxRepeat(0, MAXREPEAT, lits("a"))]);
    assert!(lower(&sp(vec![Node::MaxRepeat(0, 1, star())]), U, false).is_ok());
    let two = lower(&sp(vec![Node::MaxRepeat(0, 2, star())]), U, false);
    assert_eq!(two, Err(LowerError::Unsupported(Feature::NullableLoop)));
}

/// A lookbehind carries its width; an error of `_compile` comes before a
/// refusal earlier in the tree.
#[test]
fn lookbehind() {
    let p = prog(vec![Node::Assert(-1, lits("ab")), lit('c')], U);
    let Op::Look { behind, negate, .. } = at(&p, p.start).op else {
        panic!()
    };
    assert_eq!((behind, negate), (Some(2), false));
    let star = || sp(vec![Node::MaxRepeat(0, MAXREPEAT, lits("a"))]);
    let tree = sp(vec![
        Node::MaxRepeat(0, MAXREPEAT, star()),
        Node::AssertNot(-1, star()),
    ]);
    assert_eq!(lower(&tree, U, false), Err(LowerError::LookbehindWidth));
}

/// The cap is on the whole arena, the closing Match included.
#[test]
fn arena_cap_exact() {
    let n = (MAX_NODES - 2) / 2;
    let ok = sp(vec![
        Node::MaxRepeat(n as u32, n as u32, lits("ab")),
        lit('c'),
    ]);
    assert_eq!(lower(&ok, U, false).expect("fits").insts.len(), MAX_NODES);
    let over = sp(vec![
        Node::MaxRepeat(n as u32, n as u32, lits("ab")),
        lit('c'),
        lit('d'),
    ]);
    assert_eq!(
        lower(&over, U, false),
        Err(LowerError::Unsupported(Feature::Nodes))
    );
}

/// A negated literal under IGNORECASE is one NOT node over the fold.
#[test]
fn not_literal_folds() {
    let p = prog(vec![Node::NotLiteral('A' as u32)], U | IGNORECASE);
    assert_eq!(p.trace, ["NOT_LITERAL_UNI_IGNORE"]);
    assert!(matches!(
        at(&p, p.start).op,
        Op::Unit(Unit::NotChar(Lit::Fold { lower: 97, .. }))
    ));
}

/// A lookaround's body ends in its own `LookEnd`, never in the rest of the
/// pattern: in `(?=a)b` the chain from the body reaches `LookEnd` before
/// the node after the assertion.
#[test]
fn look_body_ends_in_look_end() {
    for negate in [false, true] {
        let body = lits("a");
        let look = if negate {
            Node::AssertNot(1, body)
        } else {
            Node::Assert(1, body)
        };
        let p = prog(vec![look, lit('b')], U);
        let Inst {
            op: Op::Look { body, .. },
            next,
        } = at(&p, p.start)
        else {
            panic!("not a look: {p:?}")
        };
        let mut i = *body;
        let mut steps = 0;
        while at(&p, i).op != Op::LookEnd {
            assert_ne!(i, *next, "the body runs into the rest: {p:?}");
            assert!(!matches!(at(&p, i).op, Op::Match), "{p:?}");
            i = at(&p, i).next;
            steps += 1;
            assert!(steps < p.insts.len(), "{p:?}");
        }
        assert_eq!(at(&p, i).next, *next, "{p:?}");
    }
}

/// A bytes pattern under IGNORECASE is refused by its flags, even with no
/// literal for `lit` to refuse: `(?i)[a]` and `(?i).`.
#[test]
fn bytes_ignorecase_refused_without_a_literal() {
    let set = Node::In(vec![Member::Literal(97)]);
    for tree in [vec![set], vec![Node::Any]] {
        assert_eq!(
            lower(&sp(tree), IGNORECASE, true),
            Err(LowerError::Unsupported(Feature::BytesIgnorecase))
        );
    }
}

/// `_compile` reads a direction of 0 as a lookahead (`av[0] >= 0`): a
/// built `Assert(0, a|bc)` lowers with no lookbehind width.
#[test]
fn direction_zero_is_ahead() {
    let body = sp(vec![Node::Branch(vec![lits("a"), lits("bc")])]);
    let p = prog(vec![Node::Assert(0, body)], U);
    assert!(
        matches!(at(&p, p.start).op, Op::Look { behind: None, .. }),
        "{p:?}"
    );
}
