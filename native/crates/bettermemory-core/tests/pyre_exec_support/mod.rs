//! What the tests of `pyre::exec` share: a naive test of class membership
//! over the tree's members (the class unit, pyre-4a, is not in the crate),
//! a row's program, and the answer line of `gen_pyre_exec._answer`.
#![allow(dead_code)]

use bettermemory_core::pyre::exec::{exec, ClassTest, Found, Mode, Text};
use bettermemory_core::pyre::lower::{lower, Class, Program};
use bettermemory_core::pyre::tree::{Category, Member, IGNORECASE};
use bettermemory_core::unicode::{is_re_digit, is_space, is_word};
use serde_json::Value;

/// Membership by the members as written. A class under IGNORECASE is not
/// folded: the fixtures hold none whose members are cased.
pub struct Naive;

impl ClassTest for Naive {
    fn contains(&self, class: &Class, c: u32) -> bool {
        assert_eq!(class.flags & IGNORECASE, 0, "a folded class in a fixture");
        let mut negate = false;
        let mut hit = false;
        for member in class.members.iter() {
            match *member {
                Member::Negate => negate = true,
                Member::Literal(l) => hit |= c == l,
                Member::Range(lo, hi) => hit |= lo <= c && c <= hi,
                Member::Category(cat) => {
                    hit |= match cat {
                        Category::CATEGORY_DIGIT => is_re_digit(c),
                        Category::CATEGORY_NOT_DIGIT => !is_re_digit(c),
                        Category::CATEGORY_SPACE => is_space(c),
                        Category::CATEGORY_NOT_SPACE => !is_space(c),
                        Category::CATEGORY_WORD => is_word(c),
                        Category::CATEGORY_NOT_WORD => !is_word(c),
                    }
                }
            }
        }
        hit != negate
    }
}

pub fn text(v: &Value) -> &str {
    v.as_str().unwrap_or_else(|| panic!("a text: {v}"))
}

/// A row's tree lowered under its flags.
pub fn program(row: &Value) -> Program {
    let tree = crate::pyre_lower_support::sub(&row["tree"]);
    let flags = row["flags"].as_u64().expect("flags") as u32;
    let bytes = row["bytes"].as_bool().expect("bytes");
    lower(&tree, flags, bytes).unwrap_or_else(|e| panic!("{e:?}: {}", row["pattern"]))
}

pub fn codes(v: &Value) -> Vec<u32> {
    v.as_array()
        .expect("code points")
        .iter()
        .map(|c| c.as_u64().expect("a code point") as u32)
        .collect()
}

/// `gen_pyre_exec._answer`.
pub fn show(found: Option<&Found>, groups: usize) -> String {
    let Some(m) = found else {
        return "-".to_string();
    };
    let mut parts = vec![format!("{},{}", m.start, m.end)];
    for g in 0..groups {
        let (a, b) = (
            m.marks.get(2 * g).copied().unwrap_or(-1),
            m.marks.get(2 * g + 1).copied().unwrap_or(-1),
        );
        parts.push(if a >= 0 && b >= 0 {
            format!("{a},{b}")
        } else {
            "-1,-1".to_string()
        });
    }
    parts.push(m.lastindex.to_string());
    parts.join("|")
}

pub fn mode_named(name: &str) -> Mode {
    match name {
        "match" => Mode::Match,
        "fullmatch" => Mode::Fullmatch,
        "search" => Mode::Search,
        other => panic!("a mode: {other}"),
    }
}

/// One call's answer line, on code points or on bytes.
pub fn answer(
    prog: &Program,
    bytes: bool,
    text: &[u32],
    pos: isize,
    endpos: isize,
    mode: Mode,
    groups: usize,
) -> String {
    let found = if bytes {
        let b: Vec<u8> = text.iter().map(|&c| c as u8).collect();
        run(prog, b.as_slice(), pos, endpos, mode)
    } else {
        run(prog, text, pos, endpos, mode)
    };
    show(found.as_ref(), groups)
}

pub fn run<T: Text + ?Sized>(
    prog: &Program,
    text: &T,
    pos: isize,
    endpos: isize,
    mode: Mode,
) -> Option<Found> {
    exec(prog, &Naive, text, pos, endpos, mode, None).expect("no budget, no failure")
}

/// `gen_pyre_exec._windows`.
pub fn windows(n: isize, short: isize) -> Vec<(isize, isize)> {
    if n <= short {
        (-1..n + 2)
            .flat_map(|p| (-1..n + 2).map(move |e| (p, e)))
            .collect()
    } else {
        vec![(0, n), (1, n), (0, n - 1), (1, n - 1)]
    }
}

/// `gen_pyre_exec._reversed`.
pub fn reversed(n: isize, pos: isize, endpos: isize) -> bool {
    pos.clamp(0, n) > endpos.clamp(0, n)
}

/// A pattern parsed and lowered by the crate, as `re.compile(pattern,
/// flags)` would build it; `bytes` for a bytes pattern of these code points.
pub fn compile(pattern: &str, flags: u32, bytes: bool) -> Program {
    let codes: Vec<u32> = pattern.chars().map(u32::from).collect();
    let (tree, state) = bettermemory_core::pyre::parse::parse(&codes, !bytes, flags)
        .unwrap_or_else(|e| panic!("{pattern}: {e:?}"));
    lower(&tree, state.flags, bytes).unwrap_or_else(|e| panic!("{pattern}: {e:?}"))
}

/// The span of a call on a `str` text, or `None`.
pub fn span(
    pattern: &str,
    flags: u32,
    text: &str,
    pos: isize,
    endpos: isize,
    mode: Mode,
) -> Option<(isize, isize)> {
    let prog = compile(pattern, flags, false);
    let t: Vec<u32> = text.chars().map(u32::from).collect();
    run(&prog, t.as_slice(), pos, endpos, mode).map(|m| (m.start, m.end))
}
