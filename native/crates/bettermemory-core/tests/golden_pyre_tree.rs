//! Every row of the `tree.*` and `token.*` parts of `pyre.jsonl` and
//! `pyre_python.jsonl` against `pyre::tree` and `pyre::token`: the
//! constants and names, the dump of trees the reference parsed, the item
//! comparison, `State` scripts, `Tokenizer` scripts, and the two sweeps
//! regenerated here by the generator's rule and compared by count and
//! digest. Rows of other parts belong to other units' tests.

mod common;

use std::collections::BTreeSet;

use bettermemory_core::pyre::token::{Tok, Tokenizer};
use bettermemory_core::pyre::tree::{
    self, At, BuildError, Category, Feature, Member, Node, State, SubPattern, Why,
};
use common::parity::Ledger;
use serde_json::{json, Value};
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
const WHYS: [Why; 16] = [
    Why::BadEscapeEnd,
    Why::BadEscape,
    Why::IncompleteEscape,
    Why::OctalRange,
    Why::MissingBrace,
    Why::MissingCharacterName,
    Why::MissingGroupName,
    Why::UnterminatedName,
    Why::InvalidGroupReference,
    Why::OpenGroup,
    Why::LookbehindGroup,
    Why::UnterminatedSet,
    Why::BadRange,
    Why::UnbalancedParenthesis,
    Why::TooManyGroups,
    Why::Redefinition,
];
const FEATURES: [Feature; 27] = [
    Feature::Backreference,
    Feature::NamedCharacter,
    Feature::SetWarning,
    Feature::Verbose,
    Feature::Ascii,
    Feature::Locale,
    Feature::Debug,
    Feature::Conditional,
    Feature::AtomicGroup,
    Feature::Possessive,
    Feature::NotBoundary,
    Feature::NotDigit,
    Feature::NotWord,
    Feature::NullableLoop,
    Feature::CaptureInRepeat,
    Feature::CaptureInLookaround,
    Feature::ChoiceInLookbehind,
    Feature::ClassPastBmpIgnorecase,
    Feature::BytesCategory,
    Feature::BytesBoundary,
    Feature::BytesIgnorecase,
    Feature::Nesting,
    Feature::Nodes,
    Feature::Template,
    Feature::Repeat,
    Feature::Group,
    Feature::Alternation,
];

/// The place of a name in its list. No wildcard arm: a variant added to
/// the enum fails to compile until it is listed, and the golden row then
/// has to list it too.
fn why_index(why: Why) -> usize {
    match why {
        Why::BadEscapeEnd => 0,
        Why::BadEscape => 1,
        Why::IncompleteEscape => 2,
        Why::OctalRange => 3,
        Why::MissingBrace => 4,
        Why::MissingCharacterName => 5,
        Why::MissingGroupName => 6,
        Why::UnterminatedName => 7,
        Why::InvalidGroupReference => 8,
        Why::OpenGroup => 9,
        Why::LookbehindGroup => 10,
        Why::UnterminatedSet => 11,
        Why::BadRange => 12,
        Why::UnbalancedParenthesis => 13,
        Why::TooManyGroups => 14,
        Why::Redefinition => 15,
    }
}

fn feature_index(feature: Feature) -> usize {
    match feature {
        Feature::Backreference => 0,
        Feature::NamedCharacter => 1,
        Feature::SetWarning => 2,
        Feature::Verbose => 3,
        Feature::Ascii => 4,
        Feature::Locale => 5,
        Feature::Debug => 6,
        Feature::Conditional => 7,
        Feature::AtomicGroup => 8,
        Feature::Possessive => 9,
        Feature::NotBoundary => 10,
        Feature::NotDigit => 11,
        Feature::NotWord => 12,
        Feature::NullableLoop => 13,
        Feature::CaptureInRepeat => 14,
        Feature::CaptureInLookaround => 15,
        Feature::ChoiceInLookbehind => 16,
        Feature::ClassPastBmpIgnorecase => 17,
        Feature::BytesCategory => 18,
        Feature::BytesBoundary => 19,
        Feature::BytesIgnorecase => 20,
        Feature::Nesting => 21,
        Feature::Nodes => 22,
        Feature::Template => 23,
        Feature::Repeat => 24,
        Feature::Group => 25,
        Feature::Alternation => 26,
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

/// The generator's `_member`, read back.
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

/// The generator's `_item`, read back.
fn node(v: &Value) -> Node {
    let a = list(v);
    let n = |i: usize| number(&a[i]) as u32;
    let max = |i: usize| a[i].as_u64().map_or(tree::MAXREPEAT, |m| m as u32);
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

fn bits(ours: &[Node], theirs: &[Node]) -> String {
    let mut out = String::new();
    for a in ours {
        for b in theirs {
            out.push(if a.py_eq(b) { '1' } else { '0' });
        }
    }
    out
}

const CODES: [u32; 9] = [0, 10, 97, 98, 0x5c, 0xe9, 0xd800, 0x1f600, 0x10ffff];
const BOUNDS: [u32; 6] = [0, 1, 2, 65535, 4294967294, 4294967295];
const FLAG_SETS: [u32; 4] = [0, 2, 16, 26];

/// The generator's `_sub`.
fn random_sub(lcg: &mut Lcg, depth: u32) -> SubPattern {
    let count = lcg.draw() % 4;
    SubPattern((0..count).map(|_| random_item(lcg, depth)).collect())
}

/// The generator's `_random_item`: the same draws in the same order.
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

/// The generator's `_tree_case`.
fn tree_case(seed: u64) -> String {
    let ours = random_sub(&mut Lcg(seed), 0);
    let theirs = random_sub(&mut Lcg(seed), 0);
    format!("{} {}", ours.dump(), bits(&ours.0, &theirs.0))
}

fn check_constants(row: &Value) {
    assert_eq!(number(&row["MAXREPEAT"]), u64::from(tree::MAXREPEAT));
    assert_eq!(number(&row["MAXGROUPS"]) as usize, tree::MAXGROUPS);
    assert_eq!(text(&row["MAXWIDTH"]), tree::MAXWIDTH.to_string());
    let flags = json!({
        "IGNORECASE": tree::IGNORECASE, "LOCALE": tree::LOCALE, "MULTILINE": tree::MULTILINE,
        "DOTALL": tree::DOTALL, "UNICODE": tree::UNICODE, "VERBOSE": tree::VERBOSE,
        "DEBUG": tree::DEBUG, "ASCII": tree::ASCII,
    });
    assert_eq!(row["flags"], flags);
    let ats: Vec<String> = ATS.iter().map(|at| format!("{at:?}")).collect();
    assert_eq!(row["at"], json!(ats));
    let categories: Vec<String> = CATEGORIES.iter().map(|c| format!("{c:?}")).collect();
    assert_eq!(row["category"], json!(categories));
}

fn check_names(row: &Value) {
    for (i, why) in WHYS.iter().enumerate() {
        assert_eq!(why_index(*why), i, "{why:?}");
    }
    for (i, feature) in FEATURES.iter().enumerate() {
        assert_eq!(feature_index(*feature), i, "{feature:?}");
    }
    let ours: BTreeSet<String> = WHYS.iter().map(|why| format!("{why:?}")).collect();
    let named = row["why"].as_object().expect("names and messages");
    let theirs: BTreeSet<String> = named.keys().cloned().collect();
    assert_eq!(ours, theirs);
    assert!(named.values().all(|message| !text(message).is_empty()));
    let features: Vec<String> = FEATURES.iter().map(|f| format!("{f:?}")).collect();
    assert_eq!(row["feature"], json!(features));
}

fn check_dump(row: &Value) {
    let built = sub(&row["tree"]);
    assert_eq!(built.dump(), text(&row["dump"]), "{}", row["pattern"]);
    assert_eq!(built.to_string(), built.dump());
    assert_eq!(built.clone().dump(), built.dump());
}

fn check_equal(row: &Value) {
    let items: Vec<Node> = list(&row["items"]).iter().map(node).collect();
    let again: Vec<Node> = list(&row["items"]).iter().map(node).collect();
    let expected: String = list(&row["equal"]).iter().map(text).collect();
    assert_eq!(bits(&items, &again), expected);
    assert_eq!(
        bits(&again, &items),
        expected,
        "the comparison is symmetric"
    );
}

fn check_tree_sweep(row: &Value) {
    let seed = number(&row["seed"]);
    let count = number(&row["count"]);
    assert!(count >= 1000, "a sweep of {count} cases");
    let mut digest = Sha256::new();
    for i in 0..count {
        digest.update(tree_case(seed + i).as_bytes());
        digest.update(b"\n");
    }
    assert_eq!(common::hex(&digest.finalize()), text(&row["sha256"]));
}

fn chars(v: &Value) -> Vec<u32> {
    text(v).chars().map(u32::from).collect()
}

fn check_state(row: &Value) {
    let mut state = State::new();
    // `maxgroups`: the script ran with the parser's `MAXGROUPS` set to it.
    let bound = row.get("maxgroups").map(|n| number(n) as usize);
    let mut out = Vec::new();
    for op in list(&row["ops"]) {
        let a = list(op);
        let gid = |i: usize| number(&a[i]) as usize;
        let width = |i: usize| text(&a[i]).parse::<u128>().expect("a width");
        let failed = |why: Why| json!(format!("!{why:?}"));
        out.push(match text(&a[0]) {
            "open" => {
                let name = a[1].as_str().map(|_| chars(&a[1]));
                let opened = match bound {
                    Some(bound) => state.opengroup_within(name.as_deref(), bound),
                    None => state.opengroup(name.as_deref()),
                };
                opened.map_or_else(failed, |g| json!(g))
            }
            "close" if state.closegroup(gid(1), (width(2), width(3))) => Value::Null,
            "close" => json!("!IndexError"),
            "check" => json!(state.checkgroup(gid(1))),
            "behind" => state
                .checklookbehindgroup(gid(1))
                .map_or_else(failed, |()| Value::Null),
            "lookbehind" => {
                state.lookbehindgroups = a[1].as_u64().map(|n| n as usize);
                Value::Null
            }
            "groups" => json!(state.groups()),
            other => panic!("a state op: {other}"),
        });
    }
    assert_eq!(json!(out), row["out"], "{}", row["ops"]);
    assert_eq!(json!(state.flags), row["flags"], "{}", row["ops"]);
    let mut names: Vec<(usize, Vec<u32>)> = state
        .groupdict
        .iter()
        .map(|(name, gid)| (*gid, name.clone()))
        .collect();
    names.sort();
    let expected: Vec<(usize, Vec<u32>)> = list(&row["groupdict"])
        .iter()
        .map(|pair| (number(&pair[1]) as usize, unspell(&pair[0])))
        .collect();
    assert_eq!(names, expected, "{}", row["ops"]);
    let widths: Vec<Value> = state
        .groupwidths
        .iter()
        .map(|width| {
            width.map_or(Value::Null, |(lo, hi)| {
                json!([lo.to_string(), hi.to_string()])
            })
        })
        .collect();
    assert_eq!(json!(widths), row["groupwidths"], "{}", row["ops"]);
}

enum Op {
    Get,
    Match(char),
    While(usize, u32),
    Until(char, Why),
    Seek(usize),
    Error(usize),
    Tell,
}

fn shown(token: Option<Tok>) -> String {
    match token {
        None => "-".into(),
        Some(Tok { esc: true, c }) => format!("e{c}"),
        Some(Tok { esc: false, c }) => format!("c{c}"),
    }
}

fn shown_chars(chars: Vec<u32>) -> String {
    let parts: Vec<String> = chars.iter().map(u32::to_string).collect();
    format!("[{}]", parts.join("."))
}

fn raised(error: BuildError) -> String {
    match error {
        BuildError::Invalid { at, why } => format!("!{why:?}@{at}"),
        other => panic!("the tokenizer refuses nothing: {other:?}"),
    }
}

/// The generator's `_line`: the op's result, `tell()`, the next token.
fn line(source: &mut Tokenizer, op: &Op) -> String {
    let result = match *op {
        Op::Get => source.get().map(shown),
        Op::Match(c) => source
            .matches(c)
            .map(|hit| if hit { "T" } else { "F" }.into()),
        Op::While(n, base) => {
            let charset = move |c| char::from_u32(c).is_some_and(|c| c.is_digit(base));
            source.getwhile(n, charset).map(shown_chars)
        }
        Op::Until(terminator, missing) => source.getuntil(terminator, missing).map(shown_chars),
        Op::Seek(index) => source.seek(index).map(|()| "-".into()),
        Op::Error(offset) => match source.error(Why::BadEscape, offset) {
            BuildError::Invalid { at, .. } => Ok(at.to_string()),
            other => panic!("{other:?}"),
        },
        Op::Tell => Ok("-".into()),
    };
    let result = result.unwrap_or_else(raised);
    format!("{result} {} {}", source.tell(), shown(source.next))
}

fn base_named(name: &str) -> u32 {
    match name {
        "hex" => 16,
        "oct" => 8,
        "dec" => 10,
        other => panic!("a charset: {other}"),
    }
}

fn missing_named(name: &str) -> Why {
    match name {
        "group name" => Why::MissingGroupName,
        "character name" => Why::MissingCharacterName,
        other => panic!("a name: {other}"),
    }
}

fn op_of(v: &Value) -> Op {
    let a = list(v);
    let first = |i: usize| text(&a[i]).chars().next().expect("a character");
    match text(&a[0]) {
        "get" => Op::Get,
        "match" => Op::Match(first(1)),
        "while" => Op::While(number(&a[1]) as usize, base_named(text(&a[2]))),
        "until" => Op::Until(first(1), missing_named(text(&a[2]))),
        "seek" => Op::Seek(number(&a[1]) as usize),
        "error" => Op::Error(number(&a[1]) as usize),
        "tell" => Op::Tell,
        other => panic!("a tokenizer op: {other}"),
    }
}

fn check_script(row: &Value) {
    let string = unspell(&row["s"]);
    let istext = row.get("bytes").is_none();
    let mut source = match Tokenizer::new(&string, istext) {
        Ok(source) => source,
        Err(error) => {
            assert_eq!(raised(error), text(&row["new"]), "{}", row["s"]);
            return;
        }
    };
    assert!(row.get("new").is_none(), "{}", row["s"]);
    assert_eq!(source.istext, istext);
    let start = format!("- {} {}", source.tell(), shown(source.next));
    assert_eq!(start, text(&row["start"]), "{}", row["s"]);
    let out: Vec<String> = list(&row["ops"])
        .iter()
        .map(|op| line(&mut source, &op_of(op)))
        .collect();
    assert_eq!(json!(out), row["out"], "{} {}", row["s"], row["ops"]);
}

/// The generator's `_token_case`: the same draws in the same order.
fn token_case(seed: u64, alphabet: &[u32]) -> Vec<String> {
    let mut lcg = Lcg(seed);
    let length = lcg.draw() % 9;
    let string: Vec<u32> = (0..length).map(|_| lcg.pick(alphabet)).collect();
    let mut source = match Tokenizer::new(&string, true) {
        Ok(source) => source,
        Err(error) => return vec![raised(error)],
    };
    let mut lines = vec![format!("- {} {}", source.tell(), shown(source.next))];
    for _ in 0..1 + lcg.draw() % 8 {
        let op = match lcg.draw() % 8 {
            0 | 1 => Op::Get,
            2 => Op::Match(lcg.pick(&['a', '7', '>', '}', '{'])),
            3 => {
                let count = lcg.draw() % 4;
                Op::While(count as usize, lcg.pick(&[16, 8, 10]))
            }
            4 => {
                let terminator = lcg.pick(&['>', '}']);
                Op::Until(
                    terminator,
                    lcg.pick(&[Why::MissingGroupName, Why::MissingCharacterName]),
                )
            }
            5 => Op::Seek((lcg.draw() % (length + 2)) as usize),
            6 => Op::Error((lcg.draw() % 4) as usize),
            _ => Op::Tell,
        };
        lines.push(line(&mut source, &op));
    }
    lines
}

fn check_token_sweep(row: &Value) {
    let seed = number(&row["seed"]);
    let count = number(&row["count"]);
    assert!(count >= 1000, "a sweep of {count} cases");
    let alphabet: Vec<u32> = list(&row["alphabet"])
        .iter()
        .map(|c| number(c) as u32)
        .collect();
    let mut digest = Sha256::new();
    for i in 0..count {
        for line in token_case(seed + i, &alphabet) {
            digest.update(line.as_bytes());
            digest.update(b"\n");
        }
    }
    assert_eq!(common::hex(&digest.finalize()), text(&row["sha256"]));
}

#[test]
fn pyre_tree_and_token_every_row() {
    let file = common::jsonl("pyre.jsonl");
    let ours = |row: &&Value| {
        let part = text(&row["part"]);
        part.starts_with("tree.") || part.starts_with("token.")
    };
    let rows: Vec<&Value> = file.iter().filter(ours).collect();
    let mut checked = 0;
    let mut parts = BTreeSet::new();
    for row in &rows {
        let part = text(&row["part"]);
        match part {
            "tree.constants" => check_constants(row),
            "tree.names" => check_names(row),
            "tree.dump" => check_dump(row),
            "tree.eq" => check_equal(row),
            "tree.sweep" => check_tree_sweep(row),
            "tree.state" => check_state(row),
            "token.script" => check_script(row),
            "token.sweep" => check_token_sweep(row),
            other => panic!("a part this test does not know: {other}"),
        }
        parts.insert(part);
        checked += 1;
    }
    assert_eq!(parts.len(), 8, "a part with no row: {parts:?}");
    common::assert_count("pyre.jsonl tree and token parts", checked, rows.len());
}

/// The trees CPython 3.13 parses otherwise than 3.12: an empty negative
/// lookaround is `(FAILURE, ())`. Rows of the `atom.` parts belong to
/// `golden_pyre_atom`.
#[test]
fn pyre_tree_python_every_row() {
    let file = common::jsonl("pyre_python.jsonl");
    let ours = |row: &&Value| text(&row["part"]).starts_with("tree.");
    let rows: Vec<&Value> = file.iter().filter(ours).collect();
    let mut ledger = Ledger::new("pyre_tree_python_every_row", "pyre_python.jsonl");
    let mut parts = BTreeSet::new();
    for row in &rows {
        let part = text(&row["part"]);
        // Chosen outside the case: an open gate sets a difference aside,
        // never a part this test does not know.
        let (check, named): (fn(&Value), &Value) = match part {
            "tree.dump" => (check_dump, &row["pattern"]),
            "tree.eq" => (check_equal, &row["patterns"]),
            other => panic!("a part this test does not know: {other}"),
        };
        ledger.case(format!("{part} {named}"), || check(row));
        parts.insert(part);
    }
    assert_eq!(parts.len(), 2, "a part with no row: {parts:?}");
    ledger.finish(rows.len());
}
