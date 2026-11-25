//! Cases a plausible wrong memo fails (unit pyre-9). A failure bit is
//! sound only when it is a function of (node, position) for one call: a
//! marker cut by a lookaround's end, a bit carried from one acceptance
//! rule into another, an interval written while a repeat still had counts
//! to try, an interval of failed ends merged across an end never tried or
//! a known run read one unit short would each change an answer, but only
//! once the memo runs, so each shape here is
//! run over every text of a small alphabet under every way of the memo,
//! with the switch forced to each budget from 0 to 40 steps, and held to
//! the answer with the memo off. The premises (decision condition 6), the
//! census of lookaround bodies (condition 8) and the stated quadratic limit,
//! with its cubic nesting, are pinned beside them.

mod common;

use bettermemory_core::pyre::lower::{Op, Program};
use bettermemory_core::pyre::memo::{last_call, premises, Setting, Stats, Way};
use bettermemory_core::pyre::parse::parse;
use bettermemory_core::pyre::regex::Regex;
use bettermemory_core::pyre::tree::MAXREPEAT;
use serde_json::Value;

fn ways() -> Vec<Setting> {
    let mut out = vec![
        Setting {
            way: Way::On,
            intervals: true,
        },
        Setting {
            way: Way::On,
            intervals: false,
        },
        Setting::SHIPPED,
    ];
    for b in 0..=40 {
        for intervals in [true, false] {
            out.push(Setting {
                way: Way::After(b),
                intervals,
            });
        }
    }
    out
}

fn texts(alphabet: &str, long: usize) -> Vec<Vec<u32>> {
    let a: Vec<u32> = alphabet.chars().map(u32::from).collect();
    let mut out = vec![Vec::new()];
    let mut last = vec![Vec::new()];
    for _ in 0..long {
        let next: Vec<Vec<u32>> = last
            .iter()
            .flat_map(|t: &Vec<u32>| {
                a.iter().map(move |&c| {
                    let mut u = t.clone();
                    u.push(c);
                    u
                })
            })
            .collect();
        out.extend(next.iter().cloned());
        last = next;
    }
    out
}

/// Every method's answer on `t`, over three windows.
fn answers(re: &Regex, t: &[u32]) -> String {
    let n = t.len() as isize;
    let mut out = String::new();
    for (p, e) in [(0, n), (1, n), (0, n - 1)] {
        out += &format!(
            "{:?}{:?}{:?}{:?}",
            re.r#match(t, p, e),
            re.fullmatch(t, p, e),
            re.search(t, p, e),
            re.finditer(t, p, e)
        );
    }
    out += &format!("{:?}{:?}", re.subn(&[45], t, 0), re.split(t, 0));
    out
}

/// Every way agrees with off on every text of `alphabet` up to `long`.
fn agree(pattern: &str, alphabet: &str, long: usize) {
    let plain = Regex::new_str(pattern, 0).unwrap_or_else(|e| panic!("{pattern}: {e:?}"));
    let off = plain.clone().with_memo(Setting::OFF);
    let ways: Vec<(Setting, Regex)> = ways()
        .into_iter()
        .map(|w| (w, plain.clone().with_memo(w)))
        .collect();
    let mut compared = 0;
    for t in texts(alphabet, long) {
        let want = answers(&off, &t);
        for (w, re) in &ways {
            assert_eq!(answers(re, &t), want, "{pattern} on {t:?} under {w:?}");
            compared += 1;
        }
    }
    assert!(compared > 1000, "{compared}");
}

/// A marker cut by `LookEnd` when a positive lookaround's body matches
/// must not record a failure: the body's Split is reached again from a
/// later entry of the lookaround at the same position.
#[test]
fn a_marker_cut_inside_a_positive_lookaround_records_nothing() {
    agree(r"(?:x|)(?=.?(?:ab|a))\w", "abx", 6);
    agree(r"(?=[ab]?(?:b|a)c)..", "abc", 6);
}

/// The same inside a negative lookaround, where a wrong bit turns the
/// body's match into a failure and so the assertion into a success.
#[test]
fn a_marker_cut_inside_a_negative_lookaround_records_nothing() {
    agree(r"(?:x|)(?!.?(?:ab|a))\w", "abx", 6);
    agree(r"(?<!a|b)c|(?!a?b)\w", "abc", 6);
}

/// The attempt after an empty match runs `advance`'s program. Its copy B
/// is the plain program with the plain acceptance, entered only once a
/// unit is consumed or as a lookaround's body, so the attempt reads and
/// records copy B's bits and intervals as a search does (unlike the
/// prototype's `nowrite`; without it `(?:a|aa)*?(?<!a)` stays exponential,
/// `pyre_memo_robustness`). Copy A, which accepts only past the attempt's
/// start, has no bit and no interval.
#[test]
fn the_attempt_that_must_advance_records_copy_b() {
    agree(r"a*", "ab", 7);
    agree(r"(?:a|aa)*?(?<!a)", "ab", 7);
    agree(r"(?:(?:.|..)?[^b])*?(?<!.)", "ab", 7);
    agree(r"(?:a|b)*?", "ab", 7);
    agree(r"\b|a+?", "a b", 6);
    agree(r"x*|(?:ab)+", "abx", 6);
}

/// One call has one acceptance rule: a memo is never kept from a
/// `fullmatch` into a `search` or across calls.
#[test]
fn bits_never_outlive_a_call() {
    agree(r"(?:a|ab)(?:c|b)*", "abc", 6);
    let re = Regex::new_str(r"(?:a|aa)+", 0)
        .expect("built")
        .with_memo(Setting {
            way: Way::On,
            intervals: true,
        });
    let t: Vec<u32> = "aaab".chars().map(u32::from).collect();
    assert_eq!(re.fullmatch(t.as_slice(), 0, isize::MAX), Ok(None));
    assert!(re
        .search(t.as_slice(), 0, isize::MAX)
        .expect("search")
        .is_some());
    assert!(re
        .fullmatch(t.as_slice(), 0, 3)
        .expect("fullmatch")
        .is_some());
}

/// An interval is written only once a repeat has tried every count: a
/// bounded repeat stopped by its maximum, a lazy one and a greedy one
/// stepping back over ends already known to fail.
#[test]
fn an_interval_is_written_only_when_the_counts_are_spent() {
    agree(r"a{0,3}?ab", "ab", 7);
    agree(r"a{1,3}b|a+c", "abc", 6);
    agree(r"[ab]*?b[ab]*c", "abc", 6);
    agree(r"(?:a+b|a)+c", "abc", 6);
    agree(r"(?:x|)(?!.?(?:ab|a))\w", "abx", 6);
    agree(r"x?a?b|x?a?c", "abcx", 5);
}

/// The failed ends of a repeat are exact: an interval starts at the
/// repeat's minimum past its entry, not below it (`.*a{2,}(?=aaa)`); two
/// intervals merge only when they touch, never across an end not tried
/// (`a*[ab]([^a]*b)`); a merge widens to the lower start and no further
/// (`.*?x|.*a{3,}(?=aaaa)`); and reading into a known run jumps to its
/// true end, not one short (`([^a]{2,}?)?b??b{2}`). Each shape fails a
/// mutant of `Memo::failed_ends` or of `count_run` that the shapes above
/// pass.
#[test]
fn the_failed_ends_and_runs_are_exact() {
    agree(r"a*[ab]([^a]*b)", "abc", 6);
    agree(r".*a{2,}(?=aaa)", "ab", 7);
    agree(r"[ab]*a{2,}(?=aaa)", "ab", 8);
    agree(r".*?x|.*a{3,}(?=aaaa)", "ax", 9);
    agree(r"(b.?(?:b*?.+a{1,3}|.+?b{1,3}[^a]{2})*?)?", "abc", 6);
    agree(r"([^a]{2,}?)?b??b{2}", "abc", 6);
}

/// A call refused by the type check records its zeros for `last_call`,
/// whichever method refused it, as `last_call` says.
#[test]
fn a_refused_call_records_zeros() {
    let re = Regex::new_str(r"(?:a|aa)*c", 0).expect("built");
    let t = vec![97u32; 200];
    let bytes = vec![97u8; 200];
    let calls: [&dyn Fn() -> bool; 4] = [
        &|| re.search(bytes.as_slice(), 0, isize::MAX).is_err(),
        &|| re.r#match(bytes.as_slice(), 0, isize::MAX).is_err(),
        &|| re.fullmatch(bytes.as_slice(), 0, isize::MAX).is_err(),
        &|| re.finditer(bytes.as_slice(), 0, isize::MAX).is_err(),
    ];
    for (i, refused) in calls.iter().enumerate() {
        // A budget, so a memo that fails to wake ends the test, not the run.
        assert_eq!(
            re.try_search(t.as_slice(), 0, isize::MAX, Some(10_000_000)),
            Ok(None)
        );
        assert!(last_call().woke, "{:?}", last_call());
        assert!(refused(), "call {i}");
        assert_eq!(last_call(), Stats::default(), "call {i}");
    }
}

/// Off is plain backtracking: the memo never wakes however long the call
/// runs, so every other way is held here to the answer of the engine as
/// it was before this unit.
#[test]
fn the_memo_off_never_wakes() {
    let re = Regex::new_str(r"(?:a|aa)*c", 0)
        .expect("built")
        .with_memo(Setting::OFF);
    let t = vec![97u32; 24];
    assert_eq!(re.search(t.as_slice(), 0, isize::MAX), Ok(None));
    let s = last_call();
    assert!(!s.woke && s.bytes == 0, "{s:?}");
    assert!(s.steps > 64 * (24 + 64), "{s:?}");
}

/// The switch counts the steps of the whole call, not of one search: each
/// search over `1,` repeated reads the rest of the run, so a switch spent
/// per search would never start and the call would be quadratic.
#[test]
fn the_switch_counts_the_whole_call() {
    let re = Regex::new_str(r"\b\d[\d,]*\s+(?:tests?\s+)?passed\b", 2).expect("built");
    let t: Vec<u32> = "1,".repeat(10_000).chars().map(u32::from).collect();
    assert_eq!(
        re.finditer(t.as_slice(), 0, isize::MAX).map(|m| m.len()),
        Ok(0)
    );
    let s = last_call();
    assert!(s.woke, "{s:?}");
    assert!(s.steps < 80 * (t.len() as u64 + 64), "{s:?}");
}

/// Decision condition 6: the memo leans on these refusals and on no op
/// reading a mark. Lifting a refusal fails here.
#[test]
fn condition_6_the_premises_hold() {
    for refused in [
        r"(a)\1",
        r"(?P<x>a)(?P=x)",
        r"(a)?(?(1)b|c)",
        r"(?:a?)*",
        r"(?:a*)+",
        r"(?:|a){2,}",
    ] {
        assert!(Regex::new_str(refused, 0).is_err(), "{refused} is built");
    }
    for row in inventory_rows() {
        let prog = program(&row);
        assert!(premises(&prog), "{}", row["pattern"]);
    }
}

fn inventory_rows() -> Vec<Value> {
    let mut out: Vec<Value> = common::jsonl("pyre_inventory.jsonl")
        .into_iter()
        .filter(|r| r["part"] == "inventory.pattern")
        .collect();
    out.extend(
        common::jsonl("pyre_unicode.jsonl")
            .into_iter()
            .filter(|r| r["part"] == "inventory.pattern"),
    );
    assert!(out.len() > 100, "{}", out.len());
    out
}

fn program(row: &Value) -> Program {
    let pattern: Vec<u32> = match row["pattern"].as_str() {
        Some(s) => s
            .chars()
            .map(|c| if c == '~' { 0x5c } else { u32::from(c) })
            .collect(),
        None => row["pattern"]
            .as_array()
            .expect("code points")
            .iter()
            .map(|c| c.as_u64().expect("a code point") as u32)
            .collect(),
    };
    let bytes = row["bytes"].as_bool().expect("bytes");
    let flags = row.get("flags").and_then(Value::as_u64).unwrap_or(0) as u32;
    let (tree, state) = parse(&pattern, !bytes, flags).expect("parsed");
    bettermemory_core::pyre::lower::lower(&tree, state.flags, bytes).expect("lowered")
}

/// Whether a lookaround body of `prog` holds an unbounded repeat: a Rep1
/// without a maximum, or a loop (a node reached again from itself).
fn unbounded_in_lookaround(prog: &Program) -> bool {
    prog.insts.iter().any(|inst| {
        let Op::Look { body, .. } = inst.op else {
            return false;
        };
        let mut seen = vec![false; prog.insts.len()];
        let mut on_path = vec![false; prog.insts.len()];
        fn walk(prog: &Program, pc: usize, seen: &mut [bool], on_path: &mut [bool]) -> bool {
            if on_path[pc] {
                return true;
            }
            if seen[pc] {
                return false;
            }
            seen[pc] = true;
            on_path[pc] = true;
            let inst = &prog.insts[pc];
            let found = match &inst.op {
                Op::LookEnd | Op::Fail | Op::Match => false,
                Op::Rep1 { max, .. } if *max == MAXREPEAT => true,
                Op::Split(second) => {
                    walk(prog, inst.next, seen, on_path) || walk(prog, *second, seen, on_path)
                }
                Op::Look { body, .. } => {
                    walk(prog, *body, seen, on_path) || walk(prog, inst.next, seen, on_path)
                }
                _ => walk(prog, inst.next, seen, on_path),
            };
            on_path[pc] = false;
            found
        }
        walk(prog, body, &mut seen, &mut on_path)
    })
}

/// Decision condition 8: the census of lookaround bodies holding an
/// unbounded repeat over the inventory. Only failures are remembered, so
/// such a body that succeeds after a scan is run again at every start.
/// The patterns flagged today are listed; a new one fails here until it
/// is looked at.
#[test]
fn condition_8_census_of_unbounded_repeats_in_lookarounds() {
    let mut flagged: Vec<String> = inventory_rows()
        .iter()
        .filter(|row| unbounded_in_lookaround(&program(row)))
        .map(|row| row["sites"].to_string())
        .collect();
    flagged.sort();
    eprintln!("flagged: {flagged:#?}");
    assert_eq!(flagged, FLAGGED.map(String::from), "the census changed");
    // The check itself finds the shape.
    let prog = program(&serde_json::json!({"pattern": "(?=[^b]*?b)a", "bytes": false}));
    assert!(unbounded_in_lookaround(&prog));
    let prog = program(&serde_json::json!({"pattern": "(?=[^b]{0,9}b)a", "bytes": false}));
    assert!(!unbounded_in_lookaround(&prog));
}

/// The inventory's sites whose lookaround bodies hold an unbounded repeat.
/// Each body is entered only at a word boundary after a fixed literal
/// (`(?!\s+you\b)`, `(?!\s+(?:of|when|that)\b)`, `(?!\s+\w+s\b)`) or at the
/// word boundary that opens a hex run (`_COMMIT_HASH_RE`'s two lookaheads),
/// so the scans of two entries do not overlap; a body that fails is
/// remembered. This is the reading of the patterns, not a measurement.
const FLAGGED: [&str; 4] = [
    "[\"durability.py:_COMMIT_HASH_RE\"]",
    "[\"durability.py:_PATTERN_OVERRIDES#3\"]",
    "[\"durability.py:_PATTERN_OVERRIDES#4\"]",
    "[\"handlers/write_gates.py:_PREFERENCE_RE\"]",
];

/// The stated limit (decision section 3 item 9): a lookaround that
/// succeeds after a scan is not remembered, so `(?=[^b]*?b)a` over
/// `a * n + b` reads the rest of the text from every start, as shipped.
#[test]
fn a_successful_lookaround_is_the_stated_quadratic_limit() {
    let re = Regex::new_str(r"(?=[^b]*?b)a", 0).expect("built");
    let mut steps = Vec::new();
    for n in [1000usize, 2000] {
        let mut t = vec![97u32; n];
        t.push(98);
        let found = re.finditer(t.as_slice(), 0, isize::MAX).expect("finditer");
        assert_eq!(found.len(), n);
        steps.push(last_call().steps);
    }
    eprintln!("(?=[^b]*?b)a steps at 1000 and 2000: {steps:?}");
    assert_eq!(steps, LIMIT_STEPS);
}

/// The steps as shipped: four times the steps at twice the text.
const LIMIT_STEPS: [u64; 2] = [1_007_010, 4_014_010];

/// The limit above nests: a lookaround that succeeds holding a repeat of
/// one that succeeds runs the inner one again at each unit of the outer
/// one, at every start, and neither is remembered. So
/// `(?=(?:a(?=[^b]*?b))*)c` with `finditer` over `a * n + b` is cubic in
/// steps in every way, as plain backtracking and `re` are: the memo holds
/// no bit and changes no step.
#[test]
fn a_nested_successful_lookaround_is_cubic() {
    let re = Regex::new_str(r"(?=(?:a(?=[^b]*?b))*)c", 0).expect("built");
    let ways = [
        Setting::OFF,
        Setting {
            way: Way::On,
            intervals: true,
        },
        Setting::SHIPPED,
    ];
    for way in ways {
        let r = re.clone().with_memo(way);
        let mut steps = Vec::new();
        for n in [100usize, 200] {
            let mut t = vec![97u32; n];
            t.push(98);
            let found = r.finditer(t.as_slice(), 0, isize::MAX).expect("finditer");
            assert!(found.is_empty());
            let s = last_call();
            assert_eq!(s.bytes, 0, "{way:?}: {s:?}");
            steps.push(s.steps);
        }
        assert_eq!(steps, NESTED_STEPS, "{way:?}");
    }
}

/// About eight times the steps at twice the text.
const NESTED_STEPS: [u64; 2] = [364_212, 2_788_412];
