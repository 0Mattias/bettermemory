//! What the tests of `pyre::lower` share: the generator's trees read back
//! from JSON (`gen_pyre_tree._item`), its random trees (`_sub` with
//! `Lcg`), the answer line of `gen_pyre_lower._answer` and its test of
//! IGNORECASE in force.
#![allow(dead_code)]

use bettermemory_core::pyre::lower::{combine_flags, lower, LowerError};
use bettermemory_core::pyre::tree::{
    At, Category, Member, Node, SubPattern, IGNORECASE, MAXREPEAT,
};
use serde_json::Value;

const MULT: u64 = 6364136223846793005;
const INC: u64 = 1442695040888963407;

/// `gen_pyre_tree.Lcg`.
pub struct Lcg(pub u64);

impl Lcg {
    pub fn draw(&mut self) -> u64 {
        self.0 = self.0.wrapping_mul(MULT).wrapping_add(INC);
        self.0 >> 33
    }

    fn pick<T: Copy>(&mut self, from: &[T]) -> T {
        from[(self.draw() % from.len() as u64) as usize]
    }
}

pub fn text(v: &Value) -> &str {
    v.as_str().unwrap_or_else(|| panic!("a text: {v}"))
}

fn list(v: &Value) -> &Vec<Value> {
    v.as_array().unwrap_or_else(|| panic!("a list: {v}"))
}

fn number(v: &Value) -> u64 {
    v.as_u64().unwrap_or_else(|| panic!("a number: {v}"))
}

const ATS: [At; 6] = [
    At::AT_BEGINNING,
    At::AT_BEGINNING_STRING,
    At::AT_BOUNDARY,
    At::AT_NON_BOUNDARY,
    At::AT_END,
    At::AT_END_STRING,
];
const CATEGORIES: [Category; 6] = [
    Category::CATEGORY_DIGIT,
    Category::CATEGORY_NOT_DIGIT,
    Category::CATEGORY_SPACE,
    Category::CATEGORY_NOT_SPACE,
    Category::CATEGORY_WORD,
    Category::CATEGORY_NOT_WORD,
];

fn at_named(name: &str) -> At {
    *ATS.iter()
        .find(|at| format!("{at:?}") == name)
        .unwrap_or_else(|| panic!("an AT code: {name}"))
}

fn category_named(name: &str) -> Category {
    *CATEGORIES
        .iter()
        .find(|category| format!("{category:?}") == name)
        .unwrap_or_else(|| panic!("a category: {name}"))
}

fn member(v: &Value) -> Member {
    let a = list(v);
    match text(&a[0]) {
        "negate" => Member::Negate,
        "lit" => Member::Literal(number(&a[1]) as u32),
        "range" => Member::Range(number(&a[1]) as u32, number(&a[2]) as u32),
        "cat" => Member::Category(category_named(text(&a[1]))),
        other => panic!("a member kind: {other}"),
    }
}

fn node(v: &Value) -> Node {
    let a = list(v);
    let n = |i: usize| number(&a[i]) as u32;
    let max = |i: usize| a[i].as_u64().map_or(MAXREPEAT, |m| m as u32);
    let dir = |i: usize| a[i].as_i64().expect("a direction") as i8;
    match text(&a[0]) {
        "lit" => Node::Literal(n(1)),
        "not" => Node::NotLiteral(n(1)),
        "any" => Node::Any,
        "in" => Node::In(list(&a[1]).iter().map(member).collect()),
        "at" => Node::At(at_named(text(&a[1]))),
        "branch" => Node::Branch(list(&a[1]).iter().map(sub).collect()),
        "group" => Node::Subpattern {
            group: a[1].as_u64().map(|gid| gid as usize),
            add_flags: n(2),
            del_flags: n(3),
            body: sub(&a[4]),
        },
        "max" => Node::MaxRepeat(n(1), max(2), sub(&a[3])),
        "min" => Node::MinRepeat(n(1), max(2), sub(&a[3])),
        "assert" => Node::Assert(dir(1), sub(&a[2])),
        "assert_not" => Node::AssertNot(dir(1), sub(&a[2])),
        "failure" => Node::Failure,
        other => panic!("an item kind: {other}"),
    }
}

/// `gen_pyre_tree._item`'s list, read back as a tree.
pub fn sub(v: &Value) -> SubPattern {
    SubPattern(list(v).iter().map(node).collect())
}

const CODES: [u32; 9] = [0, 10, 97, 98, 0x5c, 0xe9, 0xd800, 0x1f600, 0x10ffff];
const BOUNDS: [u32; 6] = [0, 1, 2, 65535, 4294967294, 4294967295];
const FLAG_SETS: [u32; 4] = [0, 2, 16, 26];

/// `gen_pyre_tree._sub`.
pub fn random_sub(lcg: &mut Lcg, depth: u32) -> SubPattern {
    let count = lcg.draw() % 4;
    SubPattern((0..count).map(|_| random_item(lcg, depth)).collect())
}

/// `gen_pyre_tree._random_item`: the same draws in the same order.
fn random_item(lcg: &mut Lcg, depth: u32) -> Node {
    let kind = lcg.draw() % if depth < 3 { 12 } else { 6 };
    match kind {
        0 => Node::Literal(lcg.pick(&CODES)),
        1 => Node::NotLiteral(lcg.pick(&CODES)),
        2 => Node::Any,
        3 => {
            let mut members = Vec::new();
            if lcg.draw() % 2 == 1 {
                members.push(Member::Negate);
            }
            for _ in 0..1 + lcg.draw() % 3 {
                members.push(match lcg.draw() % 3 {
                    0 => Member::Literal(lcg.pick(&CODES)),
                    1 => {
                        let lo = lcg.pick(&CODES);
                        Member::Range(lo, lcg.pick(&CODES))
                    }
                    _ => Member::Category(lcg.pick(&CATEGORIES)),
                });
            }
            Node::In(members)
        }
        4 => Node::At(lcg.pick(&ATS)),
        5 => Node::Failure,
        6 => {
            let count = lcg.draw() % 4;
            Node::Branch((0..count).map(|_| random_sub(lcg, depth + 1)).collect())
        }
        7 => {
            let numbered = !lcg.draw().is_multiple_of(3);
            let group = numbered.then(|| (lcg.draw() % 12) as usize);
            let add_flags = lcg.pick(&FLAG_SETS);
            let del_flags = lcg.pick(&FLAG_SETS);
            let body = random_sub(lcg, depth + 1);
            Node::Subpattern {
                group,
                add_flags,
                del_flags,
                body,
            }
        }
        8 | 9 => {
            let low = lcg.pick(&BOUNDS[..5]);
            let high = lcg.pick(&BOUNDS);
            let body = random_sub(lcg, depth + 1);
            if kind == 8 {
                Node::MaxRepeat(low, high, body)
            } else {
                Node::MinRepeat(low, high, body)
            }
        }
        _ => {
            let dir = if lcg.draw() % 2 == 1 { 1 } else { -1 };
            let body = random_sub(lcg, depth + 1);
            if kind == 10 {
                Node::Assert(dir, body)
            } else {
                Node::AssertNot(dir, body)
            }
        }
    }
}

/// `gen_pyre_lower._answer`.
pub fn answer(tree: &SubPattern, flags: u32, bytes: bool) -> String {
    match lower(tree, flags, bytes) {
        Ok(program) => format!("={} #{}", program.trace.join(" "), program.insts.len()),
        Err(LowerError::Unsupported(what)) => format!("?{what:?}"),
        Err(why) => format!("!{why:?}"),
    }
}

/// `gen_pyre_lower._ignorecase`: IGNORECASE in force anywhere.
pub fn folds(p: &SubPattern, flags: u32) -> bool {
    if flags & IGNORECASE != 0 {
        return true;
    }
    p.0.iter().any(|node| match node {
        Node::Subpattern {
            add_flags,
            del_flags,
            body,
            ..
        } => folds(body, combine_flags(flags, *add_flags, *del_flags)),
        Node::Branch(alternatives) => alternatives.iter().any(|a| folds(a, flags)),
        Node::MaxRepeat(_, _, body)
        | Node::MinRepeat(_, _, body)
        | Node::Assert(_, body)
        | Node::AssertNot(_, body) => folds(body, flags),
        Node::Literal(_)
        | Node::NotLiteral(_)
        | Node::Any
        | Node::In(_)
        | Node::At(_)
        | Node::Failure => false,
    })
}

/// The generator's `FLAGS`: UNICODE, with MULTILINE, DOTALL or IGNORECASE.
pub const FLAGS: [u32; 4] = [32, 40, 48, 34];
