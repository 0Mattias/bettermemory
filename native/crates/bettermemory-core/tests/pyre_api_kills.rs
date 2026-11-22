//! Cases a plausible wrong `pyre::regex` fails, each with the value
//! CPython 3.13.13's `re` gives (run under that interpreter when the unit
//! was built): the attempt after an empty match, `findall`'s shape, a
//! group that took no part, `lastgroup`, the type check, the window, and a
//! failed build that is an error and not a panic.

use std::sync::LazyLock;

use bettermemory_core::pyre::regex::{advance, CallError, Item, Regex, RegexError, Source};
use bettermemory_core::pyre::tree::{BuildError, Feature, Why};

fn cp(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn re(p: &str) -> Regex {
    Regex::new_str(p, 0).unwrap_or_else(|e| panic!("{p}: {e:?}"))
}

fn spans(p: &str, t: &str) -> Vec<(isize, isize)> {
    re(p)
        .finditer(cp(t).as_slice(), 0, isize::MAX)
        .expect("a str text")
        .iter()
        .map(|m| m.regs[0])
        .collect()
}

fn findall(p: &str, t: &str) -> Vec<Item<String>> {
    re(p)
        .findall(cp(t).as_slice(), 0, isize::MAX)
        .expect("a str text")
        .into_iter()
        .map(|item| {
            let s = |v: Vec<u32>| v.into_iter().filter_map(char::from_u32).collect::<String>();
            match item {
                Item::One(v) => Item::One(s(v)),
                Item::Tuple(vs) => Item::Tuple(vs.into_iter().map(s).collect()),
            }
        })
        .collect()
}

fn one(s: &str) -> Item<String> {
    Item::One(s.to_string())
}

/// `[m.span() for m in re.finditer('a*?', 'aab')]`: after the empty match
/// at 0 the attempt at 0 goes on to take one `a`; a scan that steps to 1
/// gives `(1, 1)` next, one that drops the empty match gives `(0, 1)`
/// first.
#[test]
fn the_attempt_after_an_empty_match_backtracks() {
    assert_eq!(
        spans("a*?", "aab"),
        [(0, 0), (0, 1), (1, 1), (1, 2), (2, 2), (3, 3)]
    );
    // `re.findall('|a', 'ab')` is `['', 'a', '', '']`: the empty match at
    // 1 is followed by one at 2, since `a` fails on `b`.
    assert_eq!(findall("|a", "ab"), [one(""), one("a"), one(""), one("")]);
    // `re.findall('(?:ab)*?', 'abab')`.
    assert_eq!(
        findall("(?:ab)*?", "abab"),
        [one(""), one("ab"), one(""), one("ab"), one("")]
    );
    // A greedy match that is empty: the next attempt finds nothing at 1
    // and the scan goes on. `re.findall('a*', 'baaa')`.
    assert_eq!(findall("a*", "baaa"), [one(""), one("aaa"), one("")]);
    // `re.findall('x*', '')` is `['']`: one empty match at the end.
    assert_eq!(findall("x*", ""), [one("")]);
    // `[m.span() for m in re.finditer(r'\b', 'ab cd')]`.
    assert_eq!(spans(r"\b", "ab cd"), [(0, 0), (2, 2), (3, 3), (5, 5)]);
    // `re.findall('a??', 'aa')`.
    assert_eq!(
        findall("a??", "aa"),
        [one(""), one("a"), one(""), one("a"), one("")]
    );
}

/// The second copy of the program fails at `MATCH` and never before a
/// node consumed: the program grows to twice its nodes and one more per
/// repeat of minimum 0.
#[test]
fn the_advance_program_doubles_the_arena() {
    let codes = cp("a*?b|c");
    let (tree, state) = bettermemory_core::pyre::parse::parse(&codes, true, 0).expect("parsed");
    let prog = bettermemory_core::pyre::lower::lower(&tree, state.flags, false).expect("lowered");
    let adv = advance(&prog);
    assert_eq!(adv.insts.len(), 2 * prog.insts.len() + 1);
    assert_eq!(adv.start, prog.start + prog.insts.len());
}

/// `re.findall('(a)|b', 'ab')` is `['a', '']`, `re.findall('(a)|(b)',
/// 'ab')` is `[('a', ''), ('', 'b')]`, and a pattern without groups gives
/// the whole match.
#[test]
fn findall_takes_the_shape_of_the_groups() {
    assert_eq!(findall("(a)|b", "ab"), [one("a"), one("")]);
    assert_eq!(
        findall("(a)|(b)", "ab"),
        [
            Item::Tuple(vec!["a".into(), String::new()]),
            Item::Tuple(vec![String::new(), "b".into()])
        ]
    );
    assert_eq!(findall("ab", "abab"), [one("ab"), one("ab")]);
    // `re.findall('((a)b)', 'ab')`.
    assert_eq!(
        findall("((a)b)", "ab"),
        [Item::Tuple(vec!["ab".into(), "a".into()])]
    );
}

/// A group that took no part is `(-1, -1)` in `regs` and `None` for
/// `group`, where `findall` gives `''`.
#[test]
fn a_group_that_took_no_part_is_none() {
    let m = re("(a)|b")
        .r#match(cp("b").as_slice(), 0, isize::MAX)
        .expect("str")
        .expect("a match");
    assert_eq!(m.regs, [(0, 1), (-1, -1)]);
    assert_eq!(m.group(1), None);
    assert_eq!(m.span(1), Some((-1, -1)));
    assert_eq!(m.span(2), None);
    assert_eq!(m.lastindex, None);
    // An empty group took part: `re.match('(a?)', 'b').group(1)` is `''`.
    let m = re("(a?)")
        .r#match(cp("b").as_slice(), 0, isize::MAX)
        .expect("str")
        .expect("a match");
    assert_eq!(m.group(1), Some((0, 0)));
}

/// `re.match('(?P<x>a)(?P<y>b)?', 'a').lastgroup` is `'x'`; with an
/// unnamed last group it is `None`; `lastindex` names the group closed
/// last, the outer one: `re.match('((?P<in>a))', 'a').lastgroup` is
/// `None` and `lastindex` 1.
#[test]
fn lastgroup_is_the_name_of_lastindex() {
    let at = |p: &str, t: &str| {
        re(p)
            .r#match(cp(t).as_slice(), 0, isize::MAX)
            .expect("str")
            .expect("a match")
    };
    assert_eq!(at("(?P<x>a)(?P<y>b)?", "a").lastgroup, Some(cp("x")));
    assert_eq!(at("(?P<x>a)(?P<y>b)?", "ab").lastgroup, Some(cp("y")));
    assert_eq!(at("(?P<x>a)(b)", "ab").lastgroup, None);
    let m = at("((?P<in>a))", "a");
    assert_eq!((m.lastindex, m.lastgroup), (Some(1), None));
    assert_eq!(at("(?P<x>(a))", "a").lastgroup, Some(cp("x")));
}

/// `re.compile('a').search('xa', -5, 99)` has `pos` 0 and `endpos` 2, and
/// a reversed window finds nothing through `finditer` and `findall`.
#[test]
fn the_window_is_clamped() {
    let r = re("a");
    let m = r
        .search(cp("xa").as_slice(), -5, 99)
        .expect("str")
        .expect("a match");
    assert_eq!((m.pos, m.endpos, m.regs[0]), (0, 2, (1, 2)));
    assert!(re("")
        .finditer(cp("abc").as_slice(), 2, 1)
        .expect("str")
        .is_empty());
    // `re.compile('').match('abc', 2, 1).span()` is `(2, 2)`.
    let m = re("")
        .r#match(cp("abc").as_slice(), 2, 1)
        .expect("str")
        .expect("a match");
    assert_eq!((m.regs[0], m.pos, m.endpos), ((2, 2), 2, 1));
    // `re.findall('a*', 'aaa', 1, 2)` is `['a', '']`.
    let found = re("a*").findall(cp("aaa").as_slice(), 1, 2).expect("str");
    assert_eq!(found, [Item::One(cp("a")), Item::One(vec![])]);
}

/// `_sre`'s TypeError, both ways, from every method.
#[test]
fn a_text_of_the_other_type_is_refused() {
    let s = re("a");
    let b = Regex::new_bytes(b"a", 0).expect("built");
    let str_on_bytes = CallError::Type { str_pattern: true };
    assert_eq!(s.search(b"a".as_slice(), 0, 9), Err(str_on_bytes));
    assert_eq!(s.findall(b"a".as_slice(), 0, 9), Err(str_on_bytes));
    assert_eq!(
        b.finditer(cp("a").as_slice(), 0, 9).map(|v| v.len()),
        Err(CallError::Type { str_pattern: false })
    );
    assert_eq!(
        str_on_bytes.to_string(),
        "cannot use a string pattern on a bytes-like object"
    );
    assert_eq!(
        CallError::Type { str_pattern: false }.to_string(),
        "cannot use a bytes pattern on a string-like object"
    );
    assert_eq!(b.pattern(), &Source::Bytes(b"a".to_vec()));
    // A bytes pattern carries no UNICODE: `re.compile(b'a').flags` is 0.
    assert_eq!(b.flags(), 0);
    // `re.compile('(?i)a', re.M).flags` is 42.
    assert_eq!(Regex::new_str("(?i)a", 8).expect("built").flags(), 42);
}

/// A pattern the engine does not build is an error, and a static of one
/// holds it; `re`'s own errors are kept by name.
#[test]
fn a_failed_build_is_an_error() {
    static BACKREF: LazyLock<Result<Regex, RegexError>> =
        LazyLock::new(|| Regex::new_str(r"(a)\1", 0));
    assert_eq!(
        BACKREF.as_ref().err(),
        Some(&RegexError::Build(BuildError::Unsupported {
            at: 3,
            what: Feature::Backreference
        }))
    );
    assert!(matches!(
        Regex::new_str("(", 0),
        Err(RegexError::Build(BuildError::Invalid { .. }))
    ));
    assert_eq!(
        Regex::new_str("a", 4).err(),
        Some(RegexError::Build(BuildError::Value(Why::LocaleStr)))
    );
    // `\b` on bytes is refused when the tree is lowered.
    assert!(matches!(
        Regex::new_bytes(br"\b", 0),
        Err(RegexError::Lower(_))
    ));
}

/// A budget is a declared refusal of `try_*` only.
#[test]
fn a_budget_refuses_and_the_plain_call_answers() {
    let r = re("(?:a|b)*c");
    let t = cp(&"ab".repeat(50));
    assert_eq!(
        r.try_search(t.as_slice(), 0, isize::MAX, Some(10)),
        Err(CallError::Budget)
    );
    assert_eq!(r.search(t.as_slice(), 0, isize::MAX), Ok(None));
    assert_eq!(
        r.try_findall(t.as_slice(), 0, isize::MAX, Some(10))
            .map(|v| v.len()),
        Err(CallError::Budget)
    );
}

/// `[m.span() for m in re.finditer(r'|a*a', 'aa')]` is
/// `[(0, 0), (0, 2), (2, 2)]` and `[m.span() for m in
/// re.finditer(r'|a{2,3}', 'aaa')]` is `[(0, 0), (0, 3), (3, 3)]`: in the
/// attempt that must advance, a greedy repeat tries its counts from 1
/// before its count 0, and a repeat of minimum above 0 leaves copy A.
#[test]
fn the_attempt_after_an_empty_match_keeps_repeat_order() {
    assert_eq!(spans(r"|a*a", "aa"), vec![(0, 0), (0, 2), (2, 2)]);
    assert_eq!(spans(r"|a{2,3}", "aaa"), vec![(0, 0), (0, 3), (3, 3)]);
}

/// `re.compile('a', 2**31)` and `re.compile('a', 0xffff0000)` raise OverflowError "Python int too large to
/// convert to C int" for a `str` and a bytes pattern;
/// `re.compile('(?P<x>a', 2**31)` raises `re.error` first.
#[test]
fn flags_past_a_c_int_overflow() {
    for flags in [0x8000_0000, 0x8000_0020, 0xffff_0000] {
        assert_eq!(
            Regex::new_str("a", flags).err(),
            Some(RegexError::FlagsOverflow)
        );
    }
    assert_eq!(
        Regex::new_bytes(b"a", 0x8000_0000).err(),
        Some(RegexError::FlagsOverflow)
    );
    assert!(matches!(
        Regex::new_str("(?P<x>a", 0x8000_0000),
        Err(RegexError::Build(BuildError::Invalid { .. }))
    ));
    // `re.compile('a', 2**30).flags` is 2**30 + 32, of bytes 2**30.
    assert_eq!(
        Regex::new_str("a", 0x4000_0000).map(|r| r.flags()),
        Ok(0x4000_0020)
    );
    assert_eq!(
        Regex::new_bytes(b"a", 0x4000_0000).map(|r| r.flags()),
        Ok(0x4000_0000)
    );
}

/// `m = re.compile(r'(?P<x>a)|(?P<y>b)(c)?').search('zb')`: `m.groups()`
/// is `(None, 'b', None)`, `m.groups((9, 9))` puts the default in both
/// gaps, `m.groupdict()` is `{'x': None, 'y': 'b'}` and
/// `m.groupdict(7)` is `{'x': 7, 'y': 'b'}`.
#[test]
fn groups_and_groupdict_take_a_default() {
    let r = re(r"(?P<x>a)|(?P<y>b)(c)?");
    let m = r
        .search(cp("zb").as_slice(), 0, isize::MAX)
        .expect("str")
        .expect("match");
    assert_eq!(m.groups(None), vec![None, Some((1, 2)), None]);
    assert_eq!(
        m.groups(Some((9, 9))),
        vec![Some((9, 9)), Some((1, 2)), Some((9, 9))]
    );
    assert_eq!(
        m.groupdict(r.names(), None),
        vec![(cp("x"), None), (cp("y"), Some((1, 2)))]
    );
    assert_eq!(
        m.groupdict(r.names(), Some((7, 7))),
        vec![(cp("x"), Some((7, 7))), (cp("y"), Some((1, 2)))]
    );
}
