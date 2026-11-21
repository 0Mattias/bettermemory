//! Cases a plausible wrong `pyre::parse` or `SubPattern::getwidth` fails.
//! Each expected value is what CPython 3.13.13's `re._parser` answers,
//! read through the generator's hooks (`gen_pyre_atom._line` for a tree,
//! `gen_pyre_parse._outcome` for a width); the golden test pins the same
//! facts from the interpreter, and these name the wrong reading each one
//! rules out. An answer is one line: the dump or `=lo,hi` (with ` gN fF`,
//! the groups and the flags of the state, where `counted` reads it),
//! `!Why@at` for an error of `re`, `?Feature@at` for a refusal,
//! `%Overflow`, or `%Why` for a ValueError of `fix_flags`.

use bettermemory_core::pyre::atom::parse_flat;
use bettermemory_core::pyre::parse::{parse, refusal, repeat, MATCH_DEPTH, MAX_DEPTH};
use bettermemory_core::pyre::token::{Built, Tokenizer};
use bettermemory_core::pyre::tree::{
    BuildError, Node, SubPattern, ASCII, DEBUG, LOCALE, MAXREPEAT, MAXWIDTH, UNICODE,
};

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
        Err(BuildError::Value(why)) => format!("%{why:?}"),
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

/// `parse(pattern, flags)` of a str pattern: the dump, or the error.
fn full(pattern: &str, flags: u32) -> String {
    line(
        parse(&cps(pattern), true, flags).map(|(tree, _)| tree),
        |tree| tree.dump(),
    )
}

/// `full` with the groups and flags the state is left with.
fn counted(pattern: &str, istext: bool, flags: u32) -> String {
    match parse(&cps(pattern), istext, flags) {
        Ok((tree, state)) => {
            let (lo, hi) = tree.getwidth();
            format!("={lo},{hi} g{} f{}", state.groups(), state.flags)
        }
        Err(error) => line(Err(error), |tree| tree.dump()),
    }
}

/// The common prefix of the alternatives compares items as Python compares
/// tuples: an item that holds a sub-pattern (a repeat, a lookaround) equals
/// no other, so `a+b|a+c` stays a branch, while two `FAILURE` items are
/// equal and are taken out. A derived structural equality factors both.
#[test]
fn the_common_prefix_is_taken_by_python_equality() {
    assert_eq!(
        full("a+b|a+c", 0),
        "[(BRANCH, (None, [[(MAX_REPEAT, (1, MAXREPEAT, [(LITERAL, 97)])), (LITERAL, 98)], \
         [(MAX_REPEAT, (1, MAXREPEAT, [(LITERAL, 97)])), (LITERAL, 99)]]))]"
    );
    assert_eq!(
        full("(?=a)b|(?=a)c", 0),
        "[(BRANCH, (None, [[(ASSERT, (1, [(LITERAL, 97)])), (LITERAL, 98)], \
         [(ASSERT, (1, [(LITERAL, 97)])), (LITERAL, 99)]]))]"
    );
    assert_eq!(
        full("a(?!)|a(?!)", 0),
        "[(LITERAL, 97), (FAILURE, ()), (BRANCH, (None, [[], []]))]"
    );
    assert_eq!(
        full("ab|ac", 0),
        "[(LITERAL, 97), (IN, [(LITERAL, 98), (LITERAL, 99)])]"
    );
    // The prefix stops at the shortest alternative, which is left empty.
    assert_eq!(
        full("abc|abd|ab", 0),
        "[(LITERAL, 97), (LITERAL, 98), (BRANCH, (None, [[(LITERAL, 99)], [(LITERAL, 100)], []]))]"
    );
    assert_eq!(
        full("a|a", 0),
        "[(LITERAL, 97), (BRANCH, (None, [[], []]))]"
    );
}

/// Alternatives of one literal or one set that is not negated fold into one
/// set, members once each in the order met; anything else stays a branch.
#[test]
fn alternatives_fold_into_a_set_only_when_each_is_one_member() {
    assert_eq!(full("a|b", 0), "[(IN, [(LITERAL, 97), (LITERAL, 98)])]");
    assert_eq!(
        full("[ab]|[ba]", 0),
        "[(IN, [(LITERAL, 97), (LITERAL, 98)])]"
    );
    assert_eq!(
        full("\\d|\\s", 0),
        "[(IN, [(CATEGORY, CATEGORY_DIGIT), (CATEGORY, CATEGORY_SPACE)])]"
    );
    assert_eq!(
        full("[^a]|b", 0),
        "[(BRANCH, (None, [[(NOT_LITERAL, 97)], [(LITERAL, 98)]]))]"
    );
    // A negated set of two members is a set and still does not fold.
    assert_eq!(
        full("[^ab]|c", 0),
        "[(BRANCH, (None, [[(IN, [(NEGATE, None), (LITERAL, 97), (LITERAL, 98)])], [(LITERAL, 99)]]))]"
    );
    assert_eq!(
        full("a|bc", 0),
        "[(BRANCH, (None, [[(LITERAL, 97)], [(LITERAL, 98), (LITERAL, 99)]]))]"
    );
    // A plain group is unpacked first, so its alternatives meet the outer.
    assert_eq!(
        full("(?:ab|ac)|ad", 0),
        "[(LITERAL, 97), (IN, [(LITERAL, 98), (LITERAL, 99), (LITERAL, 100)])]"
    );
}

/// A plain group is unpacked into the items around it; a group with a
/// number or with flags is kept; numbers count in order of the `(`.
#[test]
fn groups_unpack_only_when_plain() {
    assert_eq!(
        full("a(?:bc)d", 0),
        "[(LITERAL, 97), (LITERAL, 98), (LITERAL, 99), (LITERAL, 100)]"
    );
    assert_eq!(
        full("(?i:a)", 0),
        "[(SUBPATTERN, (None, 2, 0, [(LITERAL, 97)]))]"
    );
    assert_eq!(
        full("((a))", 0),
        "[(SUBPATTERN, (1, 0, 0, [(SUBPATTERN, (2, 0, 0, [(LITERAL, 97)]))]))]"
    );
    assert_eq!(counted("(a)(bc)", true, 0), "=3,3 g3 f32");
    let (_, state) = parse(&cps("(a)(?P<n>bc)"), true, 0).expect("a tree");
    assert_eq!(state.groupwidths, vec![None, Some((1, 1)), Some((2, 2))]);
    assert_eq!(state.groupdict.get(&cps("n")), Some(&2));
}

/// An empty negative lookaround is FAILURE; an empty positive one is kept.
#[test]
fn an_empty_negative_lookaround_is_failure() {
    assert_eq!(full("(?!)", 0), "[(FAILURE, ())]");
    assert_eq!(full("(?<!)", 0), "[(FAILURE, ())]");
    assert_eq!(full("(?=)", 0), "[(ASSERT, (1, []))]");
}

/// Global flags only first: at the top, in the first alternative, with
/// nothing read before but comments and other global flags.
#[test]
fn global_flags_stand_only_at_the_start() {
    assert_eq!(counted("(?#c)(?i)a", true, 0), "=1,1 g1 f34");
    assert_eq!(counted("(?i)|b", true, 0), "=0,1 g1 f34");
    assert_eq!(full("a(?i)", 0), "!GlobalFlagsNotAtStart@1");
    assert_eq!(full("x|(?x)", 0), "!GlobalFlagsNotAtStart@2");
    assert_eq!(full("(?:(?i))", 0), "!GlobalFlagsNotAtStart@3");
    // VERBOSE from a leading `(?x)` holds from the next alternative on.
    assert_eq!(full("(?x)a|b c", 0), "?Verbose@7");
    // The parse takes it; the matcher refuses VERBOSE in the flags.
    assert_eq!(counted("(?x)a|(?-x:b c)", true, 0), "?Verbose@0");
}

/// The errors of `_parse_flags` by name and position, `isalpha` deciding
/// between "unknown flag" and the missing terminator.
#[test]
fn inline_flags_fail_as_re_does() {
    assert_eq!(full("(?i-i:a)", 0), "!FlagOnAndOff@5");
    assert_eq!(full("(?-a:a)", 0), "!TurnOffTypeFlags@4");
    assert_eq!(full("(?au)a", 0), "!InlineIncompatible@4");
    assert_eq!(counted("(?aa)a", true, 0), "?Ascii@0");
    assert_eq!(full("(?iz)", 0), "!UnknownFlag@3");
    assert_eq!(full("(?i\u{e9})", 0), "!UnknownFlag@3");
    assert_eq!(full("(?i1)", 0), "!MissingFlagEnd@3");
    assert_eq!(full("(?i\\m)", 0), "!MissingFlagEnd@3");
    assert_eq!(full("(?i-1:a)", 0), "!MissingFlag@4");
    assert_eq!(full("(?i-m)", 0), "!MissingColon@5");
    assert_eq!(full("(?L)a", 0), "!InlineLocaleStr@3");
    assert_eq!(counted("(?u)a", false, 0), "!InlineUnicodeBytes@3");
    assert_eq!(counted("(?L)a", false, 0), "?Locale@0");
}

/// `fix_flags` runs after the parse and before "unbalanced parenthesis",
/// and its errors are ValueError with no position.
#[test]
fn fix_flags_comes_between_the_parse_and_the_balance() {
    assert_eq!(full("a)", LOCALE), "%LocaleStr");
    assert_eq!(full("(a", LOCALE), "!UnterminatedSubpattern@0");
    assert_eq!(full("(?a)a", UNICODE), "%AsciiUnicode");
    assert_eq!(counted("a", false, UNICODE), "%UnicodeBytes");
    assert_eq!(counted("(?L)a", false, ASCII), "%AsciiLocale");
    assert_eq!(counted("a", true, ASCII), "?Ascii@0");
    assert_eq!(counted("a", false, 0), "=1,1 g1 f0");
}

/// Group names: `str.isidentifier` over ASCII, so a trailing line feed,
/// a leading digit or a backslash is "bad character in group name" at the
/// name; a str name past ASCII is refused there; a bytes one is an error.
#[test]
fn group_names_are_identifiers() {
    assert_eq!(full("(?P<a\n>x)", 0), "!BadGroupName@4");
    assert_eq!(full("(?P<1a>x)", 0), "!BadGroupName@4");
    assert_eq!(full("(?P<a\\b>x)", 0), "!BadGroupName@4");
    assert_eq!(counted("(?P<a1_>x)", true, 0), "=1,1 g2 f32");
    assert_eq!(full("(?P<\u{e9}>x)", 0), "?UnicodeGroupName@4");
    assert_eq!(counted("(?P<\u{e9}>x)", false, 0), "!BadGroupName@4");
    assert_eq!(full("(?P<n>a)(?P<n>b)", 0), "!Redefinition@12");
}

/// References to groups pass re's checks before the refusal: an unknown
/// name, an open group, a group of the same lookbehind are errors.
#[test]
fn references_are_checked_then_refused() {
    assert_eq!(full("(?P<n>a)(?P=n)", 0), "?Backreference@8");
    assert_eq!(full("(?P=n)", 0), "!UnknownGroupName@4");
    assert_eq!(full("(?P<n>(?P=n))", 0), "!OpenGroup@10");
    assert_eq!(full("(?<=(a))\\1", 0), "?Backreference@8");
    assert_eq!(full("(?<=(?<=(a))\\1)", 0), "!LookbehindGroup@14");
    assert_eq!(full("(?<=(?P<n>a)(?P=n))", 0), "!LookbehindGroup@18");
}

/// What the engine does not build is refused where it begins.
#[test]
fn conditionals_atomic_groups_and_depth_are_refused() {
    assert_eq!(full("(a)(?(1)a|b)", 0), "?Conditional@3");
    assert_eq!(full("(?>a)", 0), "?AtomicGroup@0");
    assert_eq!(full("(a)++", 0), "?Possessive@4");
    let deep = |n: usize| format!("{}{}", "(".repeat(n), ")".repeat(n));
    // Past the matcher's bound, inside the parser's: refused at 0.
    assert_eq!(counted(&deep(MAX_DEPTH), true, 0), "?Nesting@0");
    assert_eq!(
        full(&deep(MAX_DEPTH + 1), 0),
        format!("?Nesting@{}", MAX_DEPTH + 1)
    );
    assert_eq!(full("(?:)", DEBUG), "?Debug@0");
}

/// Errors at the group's `(`: unterminated subpatterns and comments.
#[test]
fn unterminated_groups_fail_at_their_start() {
    assert_eq!(full("a(b", 0), "!UnterminatedSubpattern@1");
    assert_eq!(full("a(?#x", 0), "!UnterminatedComment@1");
    assert_eq!(full("(?#\\)x)", 0), "[]");
    assert_eq!(full("(?", 0), "!UnexpectedEnd@2");
    assert_eq!(full("(?<x)", 0), "!UnknownExtension@1");
    assert_eq!(full("(?P\\x)", 0), "!UnknownExtension@1");
    assert_eq!(full("(a))", 0), "!UnbalancedParenthesis@3");
}

/// The categories and the anchor the matcher has no rule for are refused
/// wherever they stand, in a set or not; their near misses are built.
#[test]
fn the_negated_categories_and_not_boundary_are_refused() {
    assert_eq!(full("a\\Ba", 0), "?NotBoundary@0");
    assert_eq!(full("[a\\D]", 0), "?NotDigit@0");
    assert_eq!(full("[^\\W]", 0), "?NotWord@0");
    assert_eq!(full("\\W", 0), "?NotWord@0");
    assert_eq!(counted("\\S", true, 0), "=1,1 g1 f32");
    assert_eq!(counted("\\b", true, 0), "=0,0 g1 f32");
    // A set is read member by member: the first refused member names it.
    assert_eq!(full("[\\W\\D]", 0), "?NotWord@0");
    assert_eq!(full("\\D\\B", 0), "?NotDigit@0");
}

/// The flags: global VERBOSE, ASCII, LOCALE in that order, and the same
/// letters in scoped flags, turned on or off; IGNORECASE, MULTILINE,
/// DOTALL and UNICODE are built.
#[test]
fn flags_without_a_rule_are_refused() {
    assert_eq!(counted("a", true, 64 | ASCII), "?Verbose@0");
    assert_eq!(counted("a", false, 64 | LOCALE), "?Verbose@0");
    assert_eq!(counted("a", false, ASCII), "?Ascii@0");
    assert_eq!(counted("(?x:a)", true, 0), "?Verbose@0");
    assert_eq!(counted("(?-x:a)", true, 0), "?Verbose@0");
    assert_eq!(counted("(?L:a)", false, 0), "?Locale@0");
    assert_eq!(counted("(?u:a)", true, 0), "=1,1 g1 f32");
    assert_eq!(counted("(?ms-i:a)", true, 0), "=1,1 g1 f32");
    assert_eq!(counted("a", true, 2 | 8 | 16 | UNICODE), "=1,1 g1 f58");
}

/// A loop over a body that can match empty is refused only when it may
/// run more than once; a body of width one or more, or a repeat of at most
/// one, is built.
#[test]
fn nullable_loops_are_refused_by_their_maximum() {
    assert_eq!(full("(?:a?)*", 0), "?NullableLoop@0");
    assert_eq!(full("(?:a?){2}", 0), "?NullableLoop@0");
    assert_eq!(full("(?:a?)*?", 0), "?NullableLoop@0");
    assert_eq!(full("(?:)*", 0), "?NullableLoop@0");
    assert_eq!(full("(?:(?=a))+", 0), "?NullableLoop@0");
    assert_eq!(full("(?:a|)*", 0), "?NullableLoop@0");
    assert_eq!(full("(?:a{0,2})+", 0), "?NullableLoop@0");
    assert_eq!(counted("(?:a?)?", true, 0), "=0,1 g1 f32");
    assert_eq!(counted("(?:a?){1}", true, 0), "=0,1 g1 f32");
    assert_eq!(
        counted("(?:a?b)*", true, 0),
        "=0,18446744073709551616 g1 f32"
    );
    assert_eq!(
        counted("(?:a{1,2})+", true, 0),
        "=1,18446744073709551616 g1 f32"
    );
}

/// Captures: under a repeat of more than one at any depth, and inside a
/// lookaround, refused; under `?` or `{1}`, and a lookaround inside a
/// capture, built. In a lookaround the lookaround's name comes first.
#[test]
fn captures_under_loops_and_in_lookarounds_are_refused() {
    assert_eq!(full("(a)*", 0), "?CaptureInRepeat@0");
    assert_eq!(full("(a){2}", 0), "?CaptureInRepeat@0");
    assert_eq!(full("(?:(a)b)*", 0), "?CaptureInRepeat@0");
    // The loop is met before the capture it holds.
    assert_eq!(full("(?:(a)?)+", 0), "?NullableLoop@0");
    assert_eq!(counted("(a)?", true, 0), "=0,1 g2 f32");
    assert_eq!(counted("((a)?)?", true, 0), "=0,1 g3 f32");
    assert_eq!(counted("(a){1}", true, 0), "=1,1 g2 f32");
    assert_eq!(full("(?=(a))", 0), "?CaptureInLookaround@0");
    assert_eq!(full("(?<!(a))", 0), "?CaptureInLookaround@0");
    assert_eq!(full("(?=a(?=(b)))", 0), "?CaptureInLookaround@0");
    assert_eq!(counted("((?=a))", true, 0), "=0,0 g2 f32");
    assert_eq!(counted("(?=(?i:a))", true, 0), "=0,0 g1 f32");
    assert_eq!(full("(?<=(a|bc))", 0), "?CaptureInLookaround@0");
}

/// A choice in a lookbehind, at any depth below it, a lookahead included;
/// alternatives that fold into a set are no choice; a lookahead may hold one.
#[test]
fn a_choice_in_a_lookbehind_is_refused() {
    assert_eq!(full("(?<=ab|cd)", 0), "?ChoiceInLookbehind@0");
    assert_eq!(full("(?<=(?=ab|cd)x)", 0), "?ChoiceInLookbehind@0");
    assert_eq!(full("(?<=x(?:ab|cd))", 0), "?ChoiceInLookbehind@0");
    assert_eq!(counted("(?<=a|b)", true, 0), "=0,0 g1 f32");
    assert_eq!(counted("(?=ab|cd)", true, 0), "=0,0 g1 f32");
    assert_eq!(counted("(?=(?<=ab|cd))", true, 0), "?ChoiceInLookbehind@0");
}

/// The matcher's bound: 32 levels of bodies are built, 33 refused, for
/// captures, lookarounds and repeats alike; plain groups are unpacked and
/// do not nest. The walk meets an item before what it holds.
#[test]
fn nesting_past_thirty_two_is_refused() {
    let caps = |n: usize| format!("{}a{}", "(".repeat(n), ")".repeat(n));
    assert_eq!(MATCH_DEPTH, 32);
    assert_eq!(counted(&caps(32), true, 0), "=1,1 g33 f32");
    assert_eq!(full(&caps(33), 0), "?Nesting@0");
    let looks = |n: usize| format!("{}a{}", "(?=".repeat(n), ")".repeat(n));
    assert_eq!(counted(&looks(32), true, 0), "=0,0 g1 f32");
    assert_eq!(full(&looks(33), 0), "?Nesting@0");
    let opts = |n: usize| format!("{}{}", "(?:a".repeat(n), ")?".repeat(n));
    assert_eq!(counted(&opts(32), true, 0), "=0,32 g1 f32");
    assert_eq!(full(&opts(33), 0), "?Nesting@0");
    let plain = format!("{}a{}", "(?:".repeat(40), ")".repeat(40));
    assert_eq!(counted(&plain, true, 0), "=1,1 g1 f32");
    let scoped = |n: usize| format!("{}a{}", "(?i:".repeat(n), ")".repeat(n));
    assert_eq!(counted(&scoped(32), true, 0), "=1,1 g1 f32");
    assert_eq!(full(&scoped(33), 0), "?Nesting@0");
    // What stands before the deep part is met first, what stands after it
    // is not reached.
    assert_eq!(full(&format!("\\B{}", caps(33)), 0), "?NotBoundary@0");
    assert_eq!(full(&format!("{}\\B", caps(33)), 0), "?Nesting@0");
    assert_eq!(
        full(&format!("{}\\B){}", "(".repeat(33), ")".repeat(32)), 0),
        "?Nesting@0"
    );
}

/// `refusal` reads only the tree and the flags it is given: a tree built
/// by hand is judged as a parsed one, and the flags come first.
#[test]
fn the_predicate_over_a_tree_built_by_hand() {
    let capture = Node::Subpattern {
        group: Some(1),
        add_flags: 0,
        del_flags: 0,
        body: sub(vec![lit('a')]),
    };
    let tree = sub(vec![Node::MaxRepeat(0, 2, sub(vec![capture]))]);
    assert_eq!(format!("{:?}", refusal(&tree, 0)), "Some(CaptureInRepeat)");
    assert_eq!(format!("{:?}", refusal(&tree, LOCALE)), "Some(Locale)");
    assert_eq!(format!("{:?}", refusal(&sub(vec![]), 0)), "None");
    // A branch of no alternatives holds no body and does not nest.
    let mut deep = sub(vec![Node::Branch(vec![])]);
    for _ in 0..32 {
        deep = sub(vec![Node::Assert(1, deep)]);
    }
    assert_eq!(format!("{:?}", refusal(&deep, 0)), "None");
    let deeper = sub(vec![Node::Assert(1, deep)]);
    assert_eq!(format!("{:?}", refusal(&deeper, 0)), "Some(Nesting)");
}
