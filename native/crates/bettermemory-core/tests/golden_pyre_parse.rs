//! Every row of the `parse.*` parts of `pyre.jsonl` and `pyre_python.jsonl`
//! against `pyre::parse::parse` and `SubPattern::getwidth`: written
//! patterns (repeats, groups, flags, alternatives), trees built by hand,
//! the reference's own patterns read as chains, and three sweeps
//! regenerated here by the generator's rule and compared by count and
//! digest. An answer of `pyre.jsonl` holds no tree text: `=lo,hi` for the
//! width of the tree (with ` gN fF`, the groups and the flags of the
//! state, in `parse.group`, `parse.fuzz` and `parse.inventory`), `!Why@at`
//! for an error of `re`, `?Feature@at` for a refusal, `%Overflow`,
//! `%DigitLimit` and `%Why` for the OverflowError and the two ValueErrors.
//! `pyre_python.jsonl` holds the tree texts. Rows of other parts belong to
//! other units' tests.

mod common;

use std::collections::BTreeSet;

use bettermemory_core::pyre::parse::parse;
use bettermemory_core::pyre::token::Built;
use bettermemory_core::pyre::tree::{
    self, At, BuildError, Category, Member, Node, State, SubPattern, MAXREPEAT,
};
use common::parity::Ledger;
use serde_json::Value;
use sha2::{Digest, Sha256};

const MULT: u64 = 6364136223846793005;
const INC: u64 = 1442695040888963407;

/// The generator's `Lcg`.
struct Lcg(u64);

impl Lcg {
    fn draw(&mut self) -> u64 {
        self.0 = self.0.wrapping_mul(MULT).wrapping_add(INC);
        self.0 >> 33
    }

    fn pick<T: Copy>(&mut self, from: &[T]) -> T {
        from[(self.draw() % from.len() as u64) as usize]
    }
}

fn text(v: &Value) -> &str {
    v.as_str().unwrap_or_else(|| panic!("a text: {v}"))
}

fn list(v: &Value) -> &Vec<Value> {
    v.as_array().unwrap_or_else(|| panic!("a list: {v}"))
}

fn number(v: &Value) -> u64 {
    v.as_u64().unwrap_or_else(|| panic!("a number: {v}"))
}

/// The generator's `spell`, undone: `~` is a backslash, a list is code
/// points.
fn unspell(v: &Value) -> Vec<u32> {
    match v {
        Value::String(s) => s
            .chars()
            .map(|c| if c == '~' { 0x5c } else { u32::from(c) })
            .collect(),
        other => list(other).iter().map(|c| number(c) as u32).collect(),
    }
}

/// The generator's `_failed`, or `shown` of the tree.
fn line<T>(answer: Built<T>, shown: impl FnOnce(T) -> String) -> String {
    match answer {
        Ok(tree) => shown(tree),
        Err(BuildError::Invalid { at, why }) => format!("!{why:?}@{at}"),
        Err(BuildError::Unsupported { at, what }) => format!("?{what:?}@{at}"),
        Err(BuildError::Overflow) => "%Overflow".into(),
        Err(BuildError::DigitLimit) => "%DigitLimit".into(),
        Err(BuildError::Value(why)) => format!("%{why:?}"),
    }
}

/// The generator's `_outcome`.
fn outcome(pattern: &[u32], istext: bool) -> String {
    line(parse(pattern, istext, 0), |(tree, _)| {
        let (lo, hi) = tree.getwidth();
        format!("={lo},{hi}")
    })
}

/// The generator's `_counted`.
fn counted(pattern: &[u32], istext: bool, flags: u32) -> String {
    line(
        parse(pattern, istext, flags),
        |(tree, state): (SubPattern, State)| {
            let (lo, hi) = tree.getwidth();
            format!("={lo},{hi} g{} f{}", state.groups(), state.flags)
        },
    )
}

/// The generator's `_line`.
fn dumped_with(pattern: &[u32], istext: bool, flags: u32) -> String {
    line(parse(pattern, istext, flags), |(tree, _)| tree.dump())
}

fn dumped(pattern: &[u32], istext: bool) -> String {
    dumped_with(pattern, istext, 0)
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

/// `gen_pyre_tree._item`, read back.
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

fn sub(v: &Value) -> SubPattern {
    SubPattern(list(v).iter().map(node).collect())
}

const CODES: [u32; 9] = [0, 10, 97, 98, 0x5c, 0xe9, 0xd800, 0x1f600, 0x10ffff];
const BOUNDS: [u32; 6] = [0, 1, 2, 65535, 4294967294, 4294967295];
const FLAG_SETS: [u32; 4] = [0, 2, 16, 26];

/// `gen_pyre_tree._sub`.
fn random_sub(lcg: &mut Lcg, depth: u32) -> SubPattern {
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

fn check_repeat(row: &Value) {
    let pattern = unspell(&row["pattern"]);
    let istext = row.get("bytes").is_none();
    let got = outcome(&pattern, istext);
    assert_eq!(got, text(&row["answer"]), "{}", row["pattern"]);
}

/// A row's pattern, whether it is text, and its flags.
fn case(row: &Value) -> (Vec<u32>, bool, u32) {
    let flags = row.get("flags").map_or(0, |f| number(f) as u32);
    (unspell(&row["pattern"]), row.get("bytes").is_none(), flags)
}

fn check_group(row: &Value) {
    let (pattern, istext, flags) = case(row);
    let got = counted(&pattern, istext, flags);
    assert_eq!(got, text(&row["answer"]), "{}", row["pattern"]);
}

fn check_tree(row: &Value) {
    let (pattern, istext, flags) = case(row);
    let got = dumped_with(&pattern, istext, flags);
    assert_eq!(got, text(&row["dump"]), "{}", row["pattern"]);
}

fn check_width(row: &Value) {
    let tree = sub(&row["tree"]);
    let (lo, hi) = tree.getwidth();
    let width = [text(&row["width"][0]), text(&row["width"][1])];
    assert_eq!([lo.to_string(), hi.to_string()], width, "{}", row["tree"]);
    assert!(lo <= tree::MAXWIDTH && hi <= tree::MAXWIDTH);
}

/// The generator's `_sweep`, over `answer`.
fn check_sweep(row: &Value, answer: fn(&[u32], bool) -> String) {
    let seed = number(&row["seed"]);
    let count = number(&row["count"]);
    assert!(count >= 1000, "a sweep of {count} cases");
    let fragments: Vec<Vec<u32>> = list(&row["fragments"]).iter().map(unspell).collect();
    let mut digest = Sha256::new();
    for i in 0..count {
        let mut lcg = Lcg(seed + i);
        let mut pattern = Vec::new();
        for _ in 0..lcg.draw() % 8 {
            let at = (lcg.draw() % fragments.len() as u64) as usize;
            pattern.extend_from_slice(&fragments[at]);
        }
        digest.update(answer(&pattern, true).as_bytes());
        digest.update(b"\n");
    }
    assert_eq!(common::hex(&digest.finalize()), text(&row["sha256"]));
}

/// The generator's `_fuzz`, over `answer`.
fn check_fuzz(row: &Value, answer: fn(&[u32], bool, u32) -> String) {
    let seed = number(&row["seed"]);
    let count = number(&row["count"]);
    assert!(count >= 1000, "a fuzz of {count} cases");
    let fragments: Vec<Vec<u32>> = list(&row["fragments"]).iter().map(unspell).collect();
    let flag_sets: Vec<u32> = list(&row["flags"])
        .iter()
        .map(|f| number(f) as u32)
        .collect();
    let mut digest = Sha256::new();
    for i in 0..count {
        let mut lcg = Lcg(seed + i);
        let mut pattern = Vec::new();
        for _ in 0..lcg.draw() % 8 {
            let at = (lcg.draw() % fragments.len() as u64) as usize;
            pattern.extend_from_slice(&fragments[at]);
        }
        let flags = flag_sets[(lcg.draw() % flag_sets.len() as u64) as usize];
        let istext = !(lcg.draw().is_multiple_of(3) && pattern.iter().all(|&c| c < 256));
        digest.update(answer(&pattern, istext, flags).as_bytes());
        digest.update(b"\n");
    }
    assert_eq!(common::hex(&digest.finalize()), text(&row["sha256"]));
}

/// The patterns of `atom.inventory` in its order, each with whether it is
/// text.
fn inventory() -> Vec<(Vec<u32>, bool)> {
    let file = common::jsonl("pyre.jsonl");
    let row = file
        .iter()
        .find(|row| text(&row["part"]) == "atom.inventory")
        .expect("the atom.inventory row");
    let texts = list(&row["str"]).iter().map(|p| (unspell(p), true));
    let raw = list(&row["bytes"]).iter().map(|p| (unspell(p), false));
    texts.chain(raw).collect()
}

/// Where an answer's error or refusal is, `None` for a tree or an error
/// without a position.
fn position(line: &str) -> Option<isize> {
    if !line.starts_with(['!', '?']) {
        return None;
    }
    line.rsplit_once('@')
        .map(|(_, at)| at.parse().expect("a position"))
}

/// The generator's `_chain` and `_inventory`: 16 hex digits per pattern.
fn chains(answer: fn(&[u32], bool, u32) -> String) -> Vec<String> {
    let mut out = Vec::new();
    for (pattern, istext) in inventory() {
        let mut lines = String::new();
        let mut start = 0usize;
        loop {
            let rest = &pattern[start.min(pattern.len())..];
            let line = answer(rest, istext, 0);
            lines.push_str(&line);
            lines.push('\n');
            match position(&line) {
                Some(at) => start += usize::try_from(at).expect("a position") + 1,
                None => break,
            }
        }
        out.push(common::hex(&Sha256::digest(lines.as_bytes()))[..16].to_string());
    }
    out
}

fn check_inventory(row: &Value, answer: fn(&[u32], bool, u32) -> String) {
    let want: Vec<&str> = list(&row["chains"]).iter().map(text).collect();
    assert!(want.len() >= 100, "an inventory of {} patterns", want.len());
    let got = chains(answer);
    assert_eq!(got.len(), want.len());
    for (i, (got, want)) in got.iter().zip(&want).enumerate() {
        assert_eq!(got, want, "the chain of inventory pattern {i}");
    }
}

fn check_width_sweep(row: &Value) {
    let seed = number(&row["seed"]);
    let count = number(&row["count"]);
    assert!(count >= 1000, "a sweep of {count} cases");
    let mut digest = Sha256::new();
    for i in 0..count {
        let (lo, hi) = random_sub(&mut Lcg(seed + i), 0).getwidth();
        digest.update(format!("{lo},{hi}\n").as_bytes());
    }
    assert_eq!(common::hex(&digest.finalize()), text(&row["sha256"]));
}

#[test]
fn pyre_parse_every_row() {
    let file = common::jsonl("pyre.jsonl");
    let ours = |row: &&Value| text(&row["part"]).starts_with("parse.");
    let rows: Vec<&Value> = file.iter().filter(ours).collect();
    let mut checked = 0;
    let mut parts = BTreeSet::new();
    for row in &rows {
        let part = text(&row["part"]);
        match part {
            "parse.repeat" => check_repeat(row),
            "parse.width" => check_width(row),
            "parse.sweep" => check_sweep(row, outcome),
            "parse.width_sweep" => check_width_sweep(row),
            "parse.group" => check_group(row),
            "parse.fuzz" => check_fuzz(row, counted),
            "parse.inventory" => check_inventory(row, counted),
            other => panic!("a part this test does not know: {other}"),
        }
        parts.insert(part);
        checked += 1;
    }
    assert_eq!(parts.len(), 7, "a part with no row: {parts:?}");
    common::assert_count("pyre.jsonl parse parts", checked, rows.len());
}

/// The tree texts, which name the opcodes of `re._constants` and so
/// follow the release.
#[test]
fn pyre_parse_python_every_row() {
    let file = common::jsonl("pyre_python.jsonl");
    let ours = |row: &&Value| text(&row["part"]).starts_with("parse.");
    let rows: Vec<&Value> = file.iter().filter(ours).collect();
    let mut ledger = Ledger::new("pyre_parse_python_every_row", "pyre_python.jsonl");
    for row in &rows {
        let part = text(&row["part"]);
        match part {
            "parse.tree" => ledger.case(format!("{part} {}", row["pattern"]), || check_tree(row)),
            "parse.group" => ledger.case(format!("{part} {}", row["pattern"]), || check_group(row)),
            "parse.sweep" => ledger.case(part, || check_sweep(row, dumped)),
            "parse.fuzz" => ledger.case(part, || check_fuzz(row, dumped_with)),
            "parse.inventory" => ledger.case(part, || check_inventory(row, dumped_with)),
            other => panic!("a part this test does not know: {other}"),
        }
    }
    ledger.finish(rows.len());
}
