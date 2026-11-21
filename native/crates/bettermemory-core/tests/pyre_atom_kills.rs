//! Cases a plausible wrong `pyre::atom` fails. Each expected value is what
//! CPython 3.13.13's `re._parser` answers, read through the generator's
//! hooks (`gen_pyre_atom._line`); the golden test pins the same facts from
//! the interpreter, and these name the wrong reading each one rules out.
//! An answer is one line: the dump, `!Why@at` for an error of `re`,
//! `?Feature@at` for a refusal.

use bettermemory_core::pyre::atom::{escape, item_escape, parse_flat};
use bettermemory_core::pyre::token::{Built, Tok, Tokenizer};
use bettermemory_core::pyre::tree::{BuildError, Feature, State, SubPattern, Why};

/// A backslash and `u`: the patterns below join four hexadecimal digits
/// to it.
const U: &str = r"\u";

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn line(answer: Built<SubPattern>) -> String {
    match answer {
        Ok(tree) => tree.dump(),
        Err(BuildError::Invalid { at, why }) => format!("!{why:?}@{at}"),
        Err(BuildError::Unsupported { at, what }) => format!("?{what:?}@{at}"),
        Err(BuildError::Overflow) => "%Overflow".into(),
        Err(BuildError::DigitLimit) => "%DigitLimit".into(),
        Err(BuildError::Value(why)) => format!("%{why:?}"),
    }
}

/// A str pattern.
fn read(pattern: &str) -> String {
    line(parse_flat(&cps(pattern), true, false))
}

/// A bytes pattern, given as its bytes.
fn read_bytes(pattern: &[u8]) -> String {
    let bytes: Vec<u32> = pattern.iter().map(|&b| u32::from(b)).collect();
    line(parse_flat(&bytes, false, false))
}

fn read_verbose(pattern: &str) -> String {
    line(parse_flat(&cps(pattern), true, true))
}

/// `\b` read by one table for both places: a boundary inside a set, or
/// U+0008 outside it; `\B` taken in a set.
#[test]
fn a_backspace_in_a_set_and_a_boundary_outside() {
    assert_eq!(read(r"[\b]"), "[(LITERAL, 8)]");
    assert_eq!(read(r"\b"), "[(AT, AT_BOUNDARY)]");
    assert_eq!(read(r"\B"), "[(AT, AT_NON_BOUNDARY)]");
    assert_eq!(read(r"[\B]"), "!BadEscape@1");
    assert_eq!(read(r"x\Z"), "[(LITERAL, 120), (AT, AT_END_STRING)]");
}

/// A `-` first or last read as a range, or `[--]` read as a warning.
#[test]
fn a_hyphen_first_or_last_in_a_set_is_a_literal() {
    assert_eq!(read("[-a]"), "[(IN, [(LITERAL, 45), (LITERAL, 97)])]");
    assert_eq!(read("[a-]"), "[(IN, [(LITERAL, 97), (LITERAL, 45)])]");
    assert_eq!(read("[-a-]"), "[(IN, [(LITERAL, 45), (LITERAL, 97)])]");
    assert_eq!(read("[-]"), "[(LITERAL, 45)]");
    assert_eq!(read("[--]"), "[(LITERAL, 45)]");
    assert_eq!(read(r"[\--]"), "[(LITERAL, 45)]");
    assert_eq!(read("[---]"), "?SetWarning@2");
}

/// `\d` in a set read as a literal `d`, kept inside its own `IN`, or
/// taken as the end of a range.
#[test]
fn a_category_inside_a_set() {
    assert_eq!(read(r"[\d]"), "[(IN, [(CATEGORY, CATEGORY_DIGIT)])]");
    assert_eq!(
        read(r"[^\d]"),
        "[(IN, [(NEGATE, None), (CATEGORY, CATEGORY_DIGIT)])]"
    );
    assert_eq!(
        read(r"[\d-]"),
        "[(IN, [(CATEGORY, CATEGORY_DIGIT), (LITERAL, 45)])]"
    );
    assert_eq!(read(r"[\d-a]"), "!BadRange@1");
    assert_eq!(read(r"[a-\d]"), "!BadRange@1");
    assert_eq!(
        read(r"[^\s\S]"),
        "[(IN, [(NEGATE, None), (CATEGORY, CATEGORY_SPACE), (CATEGORY, CATEGORY_NOT_SPACE)])]"
    );
    assert_eq!(
        read(r"\d\W"),
        "[(IN, [(CATEGORY, CATEGORY_DIGIT)]), (IN, [(CATEGORY, CATEGORY_NOT_WORD)])]"
    );
}

/// Digits after a backslash outside a set: an octal escape only with
/// three octal digits or a leading 0, else a group reference, placed
/// after the backslash; the third digit taken when it is not octal.
#[test]
fn octal_escapes_against_group_references() {
    assert_eq!(read(r"\100"), "[(LITERAL, 64)]");
    assert_eq!(read(r"\400"), "!OctalRange@0");
    assert_eq!(read(r"\08"), "[(LITERAL, 0), (LITERAL, 56)]");
    assert_eq!(read(r"\0777"), "[(LITERAL, 63), (LITERAL, 55)]");
    for reference in [r"\1", r"\10", r"\18", r"\81", r"\128"] {
        assert_eq!(read(reference), "!InvalidGroupReference@1", "{reference}");
    }
    assert_eq!(read(r"ab\17"), "!InvalidGroupReference@3");
    // With group 1 closed, `\1` passes every check and is refused at its
    // backslash; `\10` is still a reference to a group that is not there.
    let escaped = |text: &str, state: &State| {
        let text = cps(text);
        let mut source = Tokenizer::new(&text, true).unwrap();
        let Some(Tok { esc: true, c }) = source.get().unwrap() else {
            panic!("an escape");
        };
        item_escape(&mut source, c, state).map(|node| node.to_string())
    };
    let invalid = |at: isize, why: Why| Err(BuildError::Invalid { at, why });
    let mut state = State::new();
    assert_eq!(state.opengroup(None), Ok(1));
    assert!(state.closegroup(1, (1, 1)));
    let (at, what) = (0, Feature::Backreference);
    let refused = Err(BuildError::Unsupported { at, what });
    assert_eq!(escaped(r"\1", &state), refused);
    assert_eq!(
        escaped(r"\10", &state),
        invalid(1, Why::InvalidGroupReference)
    );
    assert_eq!(escaped(r"\100", &state), Ok("(LITERAL, 64)".into()));
    // Group 2 open inside a lookbehind that began after group 1: "open
    // group" is asked first, and placed at the backslash; the lookbehind's
    // own errors are placed after the reference.
    assert_eq!(state.opengroup(None), Ok(2));
    state.lookbehindgroups = Some(1);
    assert_eq!(escaped(r"\2", &state), invalid(0, Why::OpenGroup));
    assert_eq!(escaped(r"\1", &state), invalid(2, Why::LookbehindGroup));
    state.lookbehindgroups = Some(2);
    assert_eq!(escaped(r"\1", &state), refused);
}

/// Digits after a backslash inside a set: always octal, at most three,
/// and 8 or 9 a bad escape.
#[test]
fn octal_in_a_set() {
    assert_eq!(read(r"[\1]"), "[(LITERAL, 1)]");
    assert_eq!(read(r"[\378]"), "[(IN, [(LITERAL, 31), (LITERAL, 56)])]");
    assert_eq!(read(r"[\08]"), "[(IN, [(LITERAL, 0), (LITERAL, 56)])]");
    assert_eq!(read(r"[\400]"), "!OctalRange@1");
    assert_eq!(read(r"[\8]"), "!BadEscape@1");
    // The first digit and two more, never a fourth.
    assert_eq!(read(r"[\1234]"), "[(IN, [(LITERAL, 83), (LITERAL, 52)])]");
}

/// A code point past U+10FFFF read as a range error or taken; an escape
/// short of its digits read anyway.
#[test]
fn unicode_escapes_and_their_bounds() {
    assert_eq!(read(r"\U0010ffff"), "[(LITERAL, 1114111)]");
    assert_eq!(read(r"\U00110000"), "!BadEscape@0");
    assert_eq!(read(r"\U7fffffff"), "!BadEscape@0");
    // 3.11 and 3.12 raise OverflowError here; 3.13 a bad escape.
    assert_eq!(read(r"\Uffffffff"), "!BadEscape@0");
    assert_eq!(read(r"\U0001f60"), "!IncompleteEscape@0");
    assert_eq!(read(&format!("{U}00e9")), "[(LITERAL, 233)]");
    assert_eq!(read(r"\ud800"), "[(LITERAL, 55296)]");
    assert_eq!(read(r"\x4g"), "!IncompleteEscape@0");
    assert_eq!(read(r"\xfF"), "[(LITERAL, 255)]");
}

/// A bytes pattern read with the escapes of a str pattern.
#[test]
fn a_bytes_pattern_has_no_unicode_escapes() {
    assert_eq!(read_bytes(format!("{U}00e9").as_bytes()), "!BadEscape@0");
    assert_eq!(read_bytes(br"\U00000041"), "!BadEscape@0");
    assert_eq!(read_bytes(br"\N{x}"), "!BadEscape@0");
    assert_eq!(read_bytes(format!("[{U}0041]").as_bytes()), "!BadEscape@1");
    assert_eq!(read_bytes(br"\x41"), "[(LITERAL, 65)]");
    assert_eq!(read_bytes(br"\d"), "[(IN, [(CATEGORY, CATEGORY_DIGIT)])]");
    assert_eq!(read_bytes(&[0xe9]), "[(LITERAL, 233)]");
}

/// "bad character range" placed by what the escapes read rather than as
/// two characters each.
#[test]
fn a_bad_range_counts_each_escape_as_two() {
    assert_eq!(read(r"[\xff-\x00]"), "!BadRange@5");
    assert_eq!(read(&format!("[{U}00e9-a]")), "!BadRange@5");
    assert_eq!(read("[z-a]"), "!BadRange@1");
    assert_eq!(read("[a-a]"), "[(IN, [(RANGE, (97, 97))])]");
}

/// A set sorted, or its repeats kept; one literal left as a set.
#[test]
fn a_set_keeps_its_members_in_order_each_once() {
    assert_eq!(read("[ba]"), "[(IN, [(LITERAL, 98), (LITERAL, 97)])]");
    assert_eq!(read("[aba]"), "[(IN, [(LITERAL, 97), (LITERAL, 98)])]");
    assert_eq!(read("[aa]"), "[(LITERAL, 97)]");
    assert_eq!(read(r"[a\x61]"), "[(LITERAL, 97)]");
    assert_eq!(read("[^aa]"), "[(NOT_LITERAL, 97)]");
    assert_eq!(read("[a-ca-c]"), "[(IN, [(RANGE, (97, 99))])]");
    assert_eq!(read(r"[\d\d]"), "[(IN, [(CATEGORY, CATEGORY_DIGIT)])]");
    assert_eq!(
        read("[a-z0-9_]"),
        "[(IN, [(RANGE, (97, 122)), (RANGE, (48, 57)), (LITERAL, 95)])]"
    );
}

/// A `]` first read as the end, and the error of a set without one placed
/// at the end of the pattern instead of at its `[`.
#[test]
fn a_closing_bracket_first_is_a_member() {
    assert_eq!(read("[]]"), "[(LITERAL, 93)]");
    assert_eq!(read("[]a]"), "[(IN, [(LITERAL, 93), (LITERAL, 97)])]");
    assert_eq!(read("[^]]"), "[(NOT_LITERAL, 93)]");
    assert_eq!(read(r"[\]]"), "[(LITERAL, 93)]");
    assert_eq!(read("[]"), "!UnterminatedSet@0");
    assert_eq!(read("["), "!UnterminatedSet@0");
    assert_eq!(read("a[]"), "!UnterminatedSet@1");
    assert_eq!(read("]}"), "[(LITERAL, 93), (LITERAL, 125)]");
}

/// The three warnings of a set ignored (another answer than `re`'s
/// warning carries) or raised where `re` is silent.
#[test]
fn the_warnings_of_a_set_are_refusals_at_their_positions() {
    assert_eq!(read("[[]"), "?SetWarning@1");
    assert_eq!(read("[^[]"), "[(NOT_LITERAL, 91)]");
    assert_eq!(read("[a[]"), "[(IN, [(LITERAL, 97), (LITERAL, 91)])]");
    for doubled in ["[a&&b]", "[a~~b]", "[a||b]", "[a--b]"] {
        assert_eq!(read(doubled), "?SetWarning@2", "{doubled}");
    }
    assert_eq!(read("[a-z--x]"), "?SetWarning@4");
    assert_eq!(read("[&&]"), "[(LITERAL, 38)]");
    assert_eq!(read(r"[a&\&]"), "[(IN, [(LITERAL, 97), (LITERAL, 38)])]");
}

/// `\N{...}` accepted, refused before "missing {" or the name's errors,
/// or refused somewhere other than its backslash.
#[test]
fn a_named_character_is_read_then_refused() {
    assert_eq!(read(r"\N{EM DASH}"), "?NamedCharacter@0");
    assert_eq!(read(r"a\N{x}b"), "?NamedCharacter@1");
    assert_eq!(read(r"[\N{x}]"), "?NamedCharacter@1");
    assert_eq!(read(r"\N{a\}b}"), "?NamedCharacter@0");
    assert_eq!(read(r"\N"), "!MissingBrace@2");
    assert_eq!(read(r"\N{}"), "!MissingCharacterName@3");
    assert_eq!(read(r"\N{abc"), "!UnterminatedName@3");
}

/// A backslash before an ASCII letter with no meaning taken as the
/// letter, or one before anything else refused.
#[test]
fn an_escaped_letter_is_an_error_and_anything_else_a_literal() {
    assert_eq!(read(r"\q"), "!BadEscape@0");
    // `\z` is the end of the string from 3.14.
    assert_eq!(read(r"\z"), "!BadEscape@0");
    assert_eq!(read("\\\u{e9}"), "[(LITERAL, 233)]");
    assert_eq!(read("\\\u{1f600}"), "[(LITERAL, 128512)]");
    assert_eq!(read(r"\-"), "[(LITERAL, 45)]");
    assert_eq!(read(r"\_"), "[(LITERAL, 95)]");
    assert_eq!(read(r"\ "), "[(LITERAL, 32)]");
    assert_eq!(read(r"a\"), "!BadEscapeEnd@1");
}

/// A digit past ASCII read as a digit: `DIGITS`, `OCTDIGITS` and
/// `HEXDIGITS` are ASCII, so after a backslash one is a literal, in a set
/// too, and it ends a group reference, an octal escape and a `\x` escape.
#[test]
fn a_digit_past_ascii_is_never_a_digit() {
    // U+0661, U+0663, U+0664 and U+0669 are Arabic-Indic 1, 3, 4 and 9;
    // U+00B2 is a superscript 2, which a bytes pattern can hold.
    assert_eq!(read("\\\u{663}"), "[(LITERAL, 1635)]");
    assert_eq!(read("\\\u{669}"), "[(LITERAL, 1641)]");
    assert_eq!(read("\\\u{b2}"), "[(LITERAL, 178)]");
    assert_eq!(read_bytes(b"\\\xb2"), "[(LITERAL, 178)]");
    assert_eq!(read_bytes(b"[\\\xb2]"), "[(LITERAL, 178)]");
    assert_eq!(read("[\\\u{663}]"), "[(LITERAL, 1635)]");
    assert_eq!(read("[\\\u{669}]"), "[(LITERAL, 1641)]");
    assert_eq!(
        read("[\\1\u{661}]"),
        "[(IN, [(LITERAL, 1), (LITERAL, 1633)])]"
    );
    assert_eq!(read("\\0\u{661}"), "[(LITERAL, 0), (LITERAL, 1633)]");
    // "invalid group reference 10": the third digit is not octal.
    assert_eq!(read("\\10\u{661}"), "!InvalidGroupReference@1");
    assert_eq!(read("\\x\u{664}\u{661}"), "!IncompleteEscape@0");
    assert_eq!(read("[\\x\u{664}\u{661}]"), "!IncompleteEscape@1");
    // With group 1 closed, `\1` before a digit past ASCII is a reference
    // to group 1, refused at its backslash, and the digit is left as the
    // next token.
    let mut state = State::new();
    assert_eq!(state.opengroup(None), Ok(1));
    assert!(state.closegroup(1, (1, 1)));
    let text = cps("\\1\u{663}");
    let mut source = Tokenizer::new(&text, true).unwrap();
    let Some(Tok { esc: true, c }) = source.get().unwrap() else {
        panic!("an escape");
    };
    let (at, what) = (0, Feature::Backreference);
    assert_eq!(
        item_escape(&mut source, c, &state).map(|node| node.to_string()),
        Err(BuildError::Unsupported { at, what })
    );
    assert_eq!(source.tell(), 2);
    let (esc, c) = (false, 0x663);
    assert_eq!(source.next, Some(Tok { esc, c }));
}

/// What `_parse` reads beyond atoms refused late, early, or elsewhere;
/// `)` refused rather than raised; the token after `|` loaded before the
/// alternative is refused. A repeat is read since unit pyre-3a.
#[test]
fn what_the_next_unit_reads_is_refused_where_it_begins() {
    assert_eq!(read("{"), "[(LITERAL, 123)]");
    assert_eq!(read("(a)"), "?Group@0");
    assert_eq!(read("[a]("), "?Group@3");
    assert_eq!(read("a|b"), "?Alternation@1");
    assert_eq!(read(r"a|\"), "!BadEscapeEnd@2");
    assert_eq!(read("a)"), "!UnbalancedParenthesis@1");
    assert_eq!(read("a)|b"), "!UnbalancedParenthesis@1");
    assert_eq!(read("[(*]"), "[(IN, [(LITERAL, 40), (LITERAL, 42)])]");
    assert_eq!(
        read(".^$"),
        "[(ANY, None), (AT, AT_BEGINNING), (AT, AT_END)]"
    );
}

/// VERBOSE refused at every token, at an escaped space, inside a set, at
/// a space `WHITESPACE` does not hold, or not at all; a line break, a
/// vertical tab or a form feed read as a literal.
#[test]
fn verbose_is_refused_at_the_first_token_it_changes() {
    assert_eq!(
        read_verbose("abc"),
        "[(LITERAL, 97), (LITERAL, 98), (LITERAL, 99)]"
    );
    assert_eq!(read_verbose("a b"), "?Verbose@1");
    assert_eq!(read_verbose("a#b"), "?Verbose@1");
    // Each of the six characters of `WHITESPACE`.
    for space in ["a b", "a\tb", "a\nb", "a\x0bb", "a\x0cb", "a\rb"] {
        assert_eq!(read_verbose(space), "?Verbose@1", "{space:?}");
    }
    assert_eq!(read_verbose("ab\n  c"), "?Verbose@2");
    // Spaces of `str.isspace` and of Unicode that `WHITESPACE` does not
    // hold are literals under the flag as without it.
    assert_eq!(
        read_verbose("a\x1c\x1f\u{85}\u{a0}\u{2028}\u{3000}b"),
        "[(LITERAL, 97), (LITERAL, 28), (LITERAL, 31), (LITERAL, 133), \
         (LITERAL, 160), (LITERAL, 8232), (LITERAL, 12288), (LITERAL, 98)]"
    );
    assert_eq!(
        read_verbose("[\n\x0b]"),
        "[(IN, [(LITERAL, 10), (LITERAL, 11)])]"
    );
    assert_eq!(
        read_verbose(r"a\ b"),
        "[(LITERAL, 97), (LITERAL, 32), (LITERAL, 98)]"
    );
    assert_eq!(read_verbose(r"a\#"), "[(LITERAL, 97), (LITERAL, 35)]");
    assert_eq!(
        read_verbose(r"a\tb"),
        "[(LITERAL, 97), (LITERAL, 9), (LITERAL, 98)]"
    );
    assert_eq!(
        read_verbose("[ #]"),
        "[(IN, [(LITERAL, 32), (LITERAL, 35)])]"
    );
}

/// `re.escape` escaping more (`_`, `!`, quotes, the non-ASCII) or less
/// (`\v`, `\f`, `#`, `~`, `&`) than its set.
#[test]
fn re_escape_escapes_exactly_its_set() {
    let given = "a-b_c!\"'%,/:;<=>@`\x0b\x0c\u{e9}#~&";
    let expected = "a\\-b_c!\"'%,/:;<=>@`\\\x0b\\\x0c\u{e9}\\#\\~\\&";
    assert_eq!(escape(&cps(given)), cps(expected));
    let special = "()[]{}?*+-|^$\\.&~# \t\n\r\x0b\x0c";
    let escaped: Vec<u32> = special.chars().flat_map(|c| [0x5c, u32::from(c)]).collect();
    assert_eq!(escape(&cps(special)), escaped);
    // Bytes past ASCII are left as they are.
    let high: Vec<u32> = (0x80..0x100).collect();
    assert_eq!(escape(&high), high);
}
