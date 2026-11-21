//! Cases a plausible wrong `pyre::parse` or `SubPattern::getwidth` fails.
//! Each expected value is what CPython 3.13.13's `re._parser` answers,
//! read through the generator's hooks (`gen_pyre_atom._line` for a tree,
//! `gen_pyre_parse._outcome` for a width); the golden test pins the same
//! facts from the interpreter, and these name the wrong reading each one
//! rules out. An answer is one line: the dump or `=lo,hi`, `!Why@at` for
//! an error of `re`, `?Feature@at` for a refusal, `%Overflow`.

use bettermemory_core::pyre::atom::parse_flat;
use bettermemory_core::pyre::parse::repeat;
use bettermemory_core::pyre::token::{Built, Tokenizer};
use bettermemory_core::pyre::tree::{BuildError, Node, SubPattern, MAXREPEAT, MAXWIDTH};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn line(answer: Built<SubPattern>, shown: impl FnOnce(SubPattern) -> String) -> String {
    match answer {
        Ok(tree) => shown(tree),
        Err(BuildError::Invalid { at, why }) => format!("!{why:?}@{at}"),
        Err(BuildError::Unsupported { at, what }) => format!("?{what:?}@{at}"),
        Err(BuildError::Overflow) => "%Overflow".into(),
        Err(BuildError::DigitLimit) => "%DigitLimit".into(),
    }
}

fn read(pattern: &str) -> String {
    line(parse_flat(&cps(pattern), true, false), |tree| tree.dump())
}

fn width(pattern: &str) -> String {
    line(parse_flat(&cps(pattern), true, false), |tree| {
        let (lo, hi) = tree.getwidth();
        format!("={lo},{hi}")
    })
}

fn lit(c: char) -> Node {
    Node::Literal(u32::from(c))
}

fn sub(items: Vec<Node>) -> SubPattern {
    SubPattern(items)
}

const TOP: u32 = MAXREPEAT - 1;

/// `{` read as a repeat only when a `}` closes it: `{}` and an unclosed
/// `{` are literals, and after an unclosed one the tokenizer is back
/// after the `{`, so its digits are literals too. `{,}` is a repeat.
#[test]
fn a_brace_is_a_repeat_only_when_closed() {
    assert_eq!(
        read("a{,}"),
        "[(MAX_REPEAT, (0, MAXREPEAT, [(LITERAL, 97)]))]"
    );
    assert_eq!(
        read("a{}"),
        "[(LITERAL, 97), (LITERAL, 123), (LITERAL, 125)]"
    );
    assert_eq!(
        read("a{2"),
        "[(LITERAL, 97), (LITERAL, 123), (LITERAL, 50)]"
    );
    assert_eq!(
        read("a{\u{661}}"),
        "[(LITERAL, 97), (LITERAL, 123), (LITERAL, 1633), (LITERAL, 125)]"
    );
    // A literal `{` after a repeat is no second repeat.
    assert_eq!(
        read("a*{"),
        "[(MAX_REPEAT, (0, MAXREPEAT, [(LITERAL, 97)])), (LITERAL, 123)]"
    );
}

/// The bound is refused at `MAXREPEAT` itself, not past it; leading zeros
/// count for nothing; the minimum overflows before the order of the two
/// is looked at, and before what the repeat goes over is.
#[test]
fn the_overflow_is_at_maxrepeat_and_comes_first() {
    assert_eq!(width("a{4294967294}"), "=4294967294,4294967294");
    assert_eq!(width("a{0004294967294}"), "=4294967294,4294967294");
    assert_eq!(width("a{4294967295}"), "%Overflow");
    assert_eq!(width("a{99999999999,5}"), "%Overflow");
    assert_eq!(width("^{4294967295}"), "%Overflow");
}

/// Positions: "min repeat greater than max repeat" after the `{`, the two
/// others at the repeat character, whatever its length.
#[test]
fn each_error_has_its_position() {
    assert_eq!(read("a{3,2}"), "!MinGreaterThanMax@2");
    assert_eq!(read("a{2}*"), "!MultipleRepeat@4");
    assert_eq!(read("a{2}{3}"), "!MultipleRepeat@4");
    assert_eq!(read(r"\b*"), "!NothingToRepeat@2");
    assert_eq!(read("{2}"), "!NothingToRepeat@0");
}

/// The lazy `?` makes a repeat that a second suffix finds; a possessive
/// `+` is refused only after the repeat is valid, and only once the
/// tokenizer has read past it.
#[test]
fn the_suffixes_in_re_s_order() {
    assert_eq!(read("a{2,3}?"), "[(MIN_REPEAT, (2, 3, [(LITERAL, 97)]))]");
    assert_eq!(read("a*?+"), "!MultipleRepeat@3");
    assert_eq!(read("a{3,2}+"), "!MinGreaterThanMax@2");
    assert_eq!(read("a{2,3}+"), "?Possessive@6");
    assert_eq!(read(r"a*+\"), "!BadEscapeEnd@3");
}

/// A group without a number or flags is repeated as its body, an empty
/// one included; any other group is repeated whole.
#[test]
fn a_plain_group_is_unpacked() {
    let run = |group: Option<usize>, add_flags: u32, body: Vec<Node>| {
        let text = cps("*");
        let mut source = Tokenizer::new(&text, true).expect("a pattern");
        let this = source.get().expect("a token").expect("the star");
        let body = sub(body);
        let mut items = vec![Node::Subpattern {
            group,
            add_flags,
            del_flags: 0,
            body,
        }];
        repeat(&mut source, this, &mut items).expect("a repeat");
        sub(items).dump()
    };
    assert_eq!(
        run(None, 0, vec![lit('a'), lit('b')]),
        "[(MAX_REPEAT, (0, MAXREPEAT, [(LITERAL, 97), (LITERAL, 98)]))]"
    );
    assert_eq!(run(None, 0, vec![]), "[(MAX_REPEAT, (0, MAXREPEAT, []))]");
    assert_eq!(
        run(Some(1), 0, vec![lit('a')]),
        "[(MAX_REPEAT, (0, MAXREPEAT, [(SUBPATTERN, (1, 0, 0, [(LITERAL, 97)]))]))]"
    );
    assert_eq!(
        run(None, 2, vec![lit('a')]),
        "[(MAX_REPEAT, (0, MAXREPEAT, [(SUBPATTERN, (None, 2, 0, [(LITERAL, 97)]))]))]"
    );
}

/// `getwidth`: an unbounded repeat of a body that matches nothing adds
/// nothing; a branch of no alternatives is as wide as `MAXWIDTH` at
/// least and zero at most; a bound written by hand below the minimum is
/// kept; products past 64 bits are exact before the cap.
#[test]
fn widths_as_re_sums_them() {
    let star = |body: Vec<Node>| Node::MaxRepeat(0, MAXREPEAT, sub(body));
    assert_eq!(sub(vec![star(vec![lit('a')])]).getwidth(), (0, MAXWIDTH));
    let nothing = Node::MaxRepeat(0, 0, sub(vec![lit('a')]));
    assert_eq!(sub(vec![star(vec![nothing])]).getwidth(), (0, 0));
    assert_eq!(sub(vec![Node::Branch(vec![])]).getwidth(), (MAXWIDTH, 0));
    let odd = Node::MaxRepeat(3, 1, sub(vec![lit('a')]));
    assert_eq!(sub(vec![odd]).getwidth(), (3, 1));
    let lazy = Node::MinRepeat(2, 5, sub(vec![lit('a'), lit('b')]));
    assert_eq!(sub(vec![lazy]).getwidth(), (4, 10));
    let square = Node::MaxRepeat(
        TOP,
        TOP,
        sub(vec![Node::MaxRepeat(TOP, TOP, sub(vec![lit('a')]))]),
    );
    let exact = u128::from(TOP) * u128::from(TOP);
    assert_eq!(exact, 18_446_744_056_529_682_436);
    assert_eq!(sub(vec![square]).getwidth(), (exact, exact));
    let cube = Node::MaxRepeat(
        TOP,
        TOP,
        sub(vec![Node::MaxRepeat(
            TOP,
            TOP,
            sub(vec![Node::MaxRepeat(TOP, TOP, sub(vec![lit('a')]))]),
        )]),
    );
    assert_eq!(sub(vec![cube]).getwidth(), (MAXWIDTH, MAXWIDTH));
    let alternatives = Node::Branch(vec![
        sub(vec![lit('a')]),
        sub(vec![lit('a'), lit('b')]),
        sub(vec![]),
    ]);
    assert_eq!(sub(vec![alternatives, Node::Failure]).getwidth(), (0, 2));
    let marks = vec![
        Node::Assert(1, sub(vec![lit('a')])),
        Node::AssertNot(-1, sub(vec![lit('a')])),
    ];
    assert_eq!(sub(marks).getwidth(), (0, 0));
}
