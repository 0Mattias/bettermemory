//! Every row of the `atom.*` parts of `pyre.jsonl` and `pyre_python.jsonl`
//! against `pyre::atom`: patterns read by `parse_flat`, direct calls of
//! the two escape readers, the sweep and the inventory regenerated here by
//! the generator's rule and compared by digest, and `re.escape` of every
//! code point. An answer is one line: the dump of the tree, `!Why@at` for
//! an error of `re`, `?Feature@at` for a refusal. Rows of other parts
//! belong to other units' tests.

mod common;

use std::collections::BTreeSet;

use bettermemory_core::pyre::atom::{class_escape, escape, item_escape, parse_flat};
use bettermemory_core::pyre::token::{Built, Tok, Tokenizer};
use bettermemory_core::pyre::tree::{BuildError, Node, State, SubPattern};
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

/// The line of an answer, as the generator's `_line` and `_failed` write
/// it.
fn line<T>(answer: Built<T>, shown: impl FnOnce(T) -> String) -> String {
    match answer {
        Ok(value) => shown(value),
        Err(BuildError::Invalid { at, why }) => format!("!{why:?}@{at}"),
        Err(BuildError::Unsupported { at, what }) => format!("?{what:?}@{at}"),
    }
}

fn answer(pattern: &[u32], istext: bool, verbose: bool) -> String {
    line(parse_flat(pattern, istext, verbose), |tree| tree.dump())
}

/// The generator's `_token`.
fn shown(token: Option<Tok>) -> String {
    match token {
        None => "-".into(),
        Some(Tok { esc: true, c }) => format!("e{c}"),
        Some(Tok { esc: false, c }) => format!("c{c}"),
    }
}

fn check_parse(row: &Value) {
    let pattern = unspell(&row["pattern"]);
    let istext = row.get("bytes").is_none();
    let verbose = row.get("verbose").is_some();
    assert_eq!(
        answer(&pattern, istext, verbose),
        text(&row["answer"]),
        "{}",
        row["pattern"]
    );
}

/// The state the ops of a row build, as `tree.state` runs them.
fn state_of(ops: &Value) -> State {
    let mut state = State::new();
    for op in list(ops) {
        let a = list(op);
        match text(&a[0]) {
            "open" => {
                assert!(a[1].is_null(), "a named group: {op}");
                state.opengroup(None).expect("a group");
            }
            "close" => {
                let width = |i: usize| text(&a[i]).parse::<u128>().expect("a width");
                assert!(state.closegroup(number(&a[1]) as usize, (width(2), width(3))));
            }
            "lookbehind" => state.lookbehindgroups = a[1].as_u64().map(|gid| gid as usize),
            other => panic!("a state op: {other}"),
        }
    }
    state
}

fn check_escape(row: &Value) {
    let string = unspell(&row["s"]);
    let state = state_of(&row["state"]);
    let mut source = Tokenizer::new(&string, true).expect("a tokenizer");
    let Some(Tok { esc: true, c }) = source.get().expect("a token") else {
        panic!("the text begins with an escape: {}", row["s"]);
    };
    let out = match text(&row["in"]) {
        "item" => line(item_escape(&mut source, c, &state), |node| node.to_string()),
        "class" => line(class_escape(&mut source, c), |member| member.to_string()),
        other => panic!("a reader: {other}"),
    };
    let out = format!("{out} {} {}", source.tell(), shown(source.next));
    assert_eq!(out, text(&row["out"]), "{} {}", row["in"], row["s"]);
}

/// The generator's `_sweep_case`: the same draws in the same order.
fn sweep_case(seed: u64, fragments: &[Vec<u32>]) -> String {
    let mut lcg = Lcg(seed);
    let count = lcg.draw() % 8;
    let mut pattern = Vec::new();
    for _ in 0..count {
        let i = (lcg.draw() % fragments.len() as u64) as usize;
        pattern.extend_from_slice(&fragments[i]);
    }
    let mode = lcg.draw() % 4;
    if mode == 2 && pattern.iter().all(|&c| c < 256) {
        return answer(&pattern, false, false);
    }
    answer(&pattern, true, mode == 3)
}

fn check_sweep(row: &Value) {
    let seed = number(&row["seed"]);
    let count = number(&row["count"]);
    assert!(count >= 1000, "a sweep of {count} cases");
    let fragments: Vec<Vec<u32>> = list(&row["fragments"]).iter().map(unspell).collect();
    let mut digest = Sha256::new();
    for i in 0..count {
        digest.update(sweep_case(seed + i, &fragments).as_bytes());
        digest.update(b"\n");
    }
    assert_eq!(common::hex(&digest.finalize()), text(&row["sha256"]));
}

/// The generator's `_chain`: the answers for the pattern, read again from
/// one past each error or refusal until a read gives a tree.
fn chain(pattern: &[u32], istext: bool) -> Vec<String> {
    let mut lines = Vec::new();
    let mut start = 0;
    loop {
        let rest = &pattern[start.min(pattern.len())..];
        let read = parse_flat(rest, istext, false);
        let at = match read {
            Ok(_) => None,
            Err(BuildError::Invalid { at, .. } | BuildError::Unsupported { at, .. }) => Some(at),
        };
        lines.push(line(read, |tree| tree.dump()));
        let Some(at) = at else { return lines };
        let at = usize::try_from(at).expect("a position from the start");
        start += at + 1;
    }
}

fn check_inventory(row: &Value) {
    let texts = list(&row["str"]).iter().map(|v| (unspell(v), true));
    let raw = list(&row["bytes"]).iter().map(|v| (unspell(v), false));
    let patterns: Vec<(Vec<u32>, bool)> = texts.chain(raw).collect();
    let chains = list(&row["chains"]);
    assert_eq!(patterns.len(), chains.len());
    assert!(patterns.len() >= 100, "{} patterns", patterns.len());
    for ((pattern, istext), expected) in patterns.iter().zip(chains) {
        let mut digest = Sha256::new();
        let lines = chain(pattern, *istext);
        for line in &lines {
            digest.update(line.as_bytes());
            digest.update(b"\n");
        }
        let shown: String = pattern.iter().filter_map(|&c| char::from_u32(c)).collect();
        assert_eq!(
            &common::hex(&digest.finalize())[..16],
            text(expected),
            "{shown:?} (istext {istext}): {lines:?}"
        );
    }
}

fn check_re_escape(row: &Value) {
    let every: Vec<u32> = (0..0x11_0000).collect();
    let escaped = escape(&every);
    let mut digest = Sha256::new();
    for c in &escaped {
        digest.update(c.to_le_bytes());
    }
    assert_eq!(common::hex(&digest.finalize()), text(&row["sha256"]));
    let special: Vec<u32> = every
        .iter()
        .copied()
        .filter(|&c| escape(&[c]) != [c])
        .collect();
    assert_eq!(json!(special), row["special"]);
    let bytes: Vec<u32> = (0..256).collect();
    let raw = escape(&bytes);
    assert_eq!(raw, unspell(&row["bytes"]));
    // What `re.escape` writes reads back as the code points it was given.
    for (pattern, istext, given) in [(&escaped, true, &every), (&raw, false, &bytes)] {
        let tree: SubPattern = parse_flat(pattern, istext, false).expect("a flat pattern");
        assert_eq!(tree.0.len(), given.len());
        for (node, &c) in tree.0.iter().zip(given) {
            assert!(matches!(node, Node::Literal(l) if *l == c), "{c}: {node}");
        }
    }
}

#[test]
fn pyre_atom_every_row() {
    let file = common::jsonl("pyre.jsonl");
    let ours = |row: &&Value| text(&row["part"]).starts_with("atom.");
    let rows: Vec<&Value> = file.iter().filter(ours).collect();
    let mut checked = 0;
    let mut parts = BTreeSet::new();
    for row in &rows {
        let part = text(&row["part"]);
        match part {
            "atom.parse" => check_parse(row),
            "atom.escape" => check_escape(row),
            "atom.sweep" => check_sweep(row),
            "atom.inventory" => check_inventory(row),
            "atom.re_escape" => check_re_escape(row),
            other => panic!("a part this test does not know: {other}"),
        }
        parts.insert(part);
        checked += 1;
    }
    assert_eq!(parts.len(), 5, "a part with no row: {parts:?}");
    common::assert_count("pyre.jsonl atom parts", checked, rows.len());
}

/// The patterns CPython 3.13 reads otherwise than another release: `\z`
/// is a bad escape, and so is a `\U` escape of 2**31 or more.
#[test]
fn pyre_atom_python_every_row() {
    let file = common::jsonl("pyre_python.jsonl");
    let ours = |row: &&Value| text(&row["part"]).starts_with("atom.");
    let rows: Vec<&Value> = file.iter().filter(ours).collect();
    let mut ledger = Ledger::new("pyre_atom_python_every_row", "pyre_python.jsonl");
    for row in &rows {
        let part = text(&row["part"]);
        assert_eq!(part, "atom.parse", "a part this test does not know");
        ledger.case(format!("{part} {}", row["pattern"]), || check_parse(row));
    }
    ledger.finish(rows.len());
}
