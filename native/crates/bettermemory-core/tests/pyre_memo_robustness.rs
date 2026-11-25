//! The memo's hostile families at scale (unit pyre-9): each family of
//! `pyre_memo.jsonl` run as shipped on a text of one megabyte (a
//! sixteenth of it in a debug build, which runs every step unoptimised),
//! with its steps pinned linear by a constant written per family
//! (`steps <= c * (len + 64)`), the growth from half the size held under 2.2,
//! the peak stack pinned, and every way of the memo agreeing at the
//! smaller size.

mod common;

use bettermemory_core::pyre::exec::MatchError;
use bettermemory_core::pyre::memo::{last_call, Memo, Setting, Stats, Way};
use bettermemory_core::pyre::regex::{CallError, Regex};
use serde_json::Value;

fn spelled(v: &Value) -> Vec<u32> {
    match v.as_str() {
        Some(s) => s.chars().map(u32::from).collect(),
        None => v
            .as_array()
            .expect("code points")
            .iter()
            .map(|c| c.as_u64().expect("a code point") as u32)
            .collect(),
    }
}

fn recipe(row: &Value, n: usize) -> Vec<u32> {
    let r = &row["recipe"];
    let unit = spelled(&r["unit"]);
    let div = r["div"].as_u64().expect("div") as usize;
    let mut t = spelled(&r["head"]);
    for _ in 0..n / div {
        t.extend(&unit);
    }
    t.extend(spelled(&r["tail"]));
    t
}

fn regex(row: &Value) -> Regex {
    let flags = row["flags"].as_u64().expect("flags") as u32;
    Regex::new(&spelled(&row["pattern"]), flags).expect("built")
}

/// Runs the row's method; answers a fingerprint of the answer and the
/// call's stats.
fn call(re: &Regex, method: &str, t: &[u32]) -> (String, Stats) {
    tried(re, method, t, None)
}

/// [`call`] under a budget per run, so a pin that a broken memo would
/// turn exponential fails instead of running for ever.
fn tried(re: &Regex, method: &str, t: &[u32], budget: Option<u64>) -> (String, Stats) {
    let m = isize::MAX;
    let got = match method {
        "sub" => format!(
            "{:?}",
            re.try_subn(&[45], t, 0, budget).map(|(o, n)| (o.len(), n))
        ),
        "finditer" => format!(
            "{:?}",
            re.try_finditer(t, 0, m, budget)
                .map(|ms| ms.iter().map(|m| m.regs[0]).collect::<Vec<_>>())
        ),
        "match" => format!(
            "{:?}",
            re.try_match(t, 0, m, budget).map(|m| m.map(|m| m.regs[0]))
        ),
        "fullmatch" => format!(
            "{:?}",
            re.try_fullmatch(t, 0, m, budget)
                .map(|m| m.map(|m| m.regs[0]))
        ),
        "search" => format!(
            "{:?}",
            re.try_search(t, 0, m, budget).map(|m| m.map(|m| m.regs[0]))
        ),
        other => panic!("a method: {other}"),
    };
    (got, last_call())
}

/// The size of the scale run: one megabyte, a sixteenth in debug.
const SCALE: usize = if cfg!(debug_assertions) {
    1 << 16
} else {
    1 << 20
};

/// Per family, by its note: the most steps per character as shipped
/// (`c`), and the most choice points held at once, `peak + per * len`.
/// Measured in a release build at one megabyte (each `c` the next integer above
/// the measured steps per character, which leaves room for the fixed cost
/// of a call at the debug build's smaller size) and written here; a constant is never
/// moved to make a run pass. As shipped the memo starts after
/// `64 * (len + 64)` steps, so a family that wakes it spends about 64 steps
/// per character before it does. A peak that grows with the text is the
/// heap stack of choice points of a repeat over a group (one frame per
/// iteration), never the Rust call stack.
const PINS: [(&str, u64, usize, f64); 36] = [
    ("openers, never closed", 71, 5, 0.0),
    ("openers without '>'", 68, 3, 0.0),
    ("digit-comma run", 72, 2, 0.0),
    ("digit-dot run", 119, 12, 0.0),
    ("blank lines", 72, 2, 0.0),
    ("'%(' never closed", 72, 2, 0.0),
    ("one long word, then '!'", 71, 4, 0.0),
    ("statement heads, never closed", 70, 2, 0.0),
    ("prefix words", 81, 26, 0.0),
    ("keyword then spaces", 39, 4, 0.0),
    ("keyword, then 'is' repeated", 18, 6, 0.0),
    ("userinfo never closed", 6, 1, 0.0),
    ("capitalised words, no separator", 12, 8, 0.34),
    ("the same, many starts", 12, 22, 0.0),
    ("dotted run", 4, 2, 0.0),
    ("folders without a file", 8, 3, 0.0),
    ("labels without a slash", 9, 8, 0.5),
    ("hyphen run", 7, 1, 0.0),
    ("symbol then spaces", 5, 1, 0.0),
    ("symbol and filler, no 'in'", 6, 1, 0.0),
    ("opener then words", 3, 2, 0.0),
    ("opener repeated", 34, 3, 0.0),
    ("anchored branch, words", 25, 3, 0.0),
    ("one long word", 11, 3, 0.0),
    ("capitalised words", 23, 2, 0.0),
    ("sentence ends", 13, 2, 0.0),
    ("list markers", 3, 8, 1.0),
    ("indented blank lines", 20, 2, 0.0),
    ("number then spaces", 23, 3, 0.0),
    ("near misses of the id", 5, 1, 0.0),
    ("segments then '!'", 5, 8, 0.4),
    ("backtick never closed", 6, 1, 0.0),
    ("value never closed", 6, 1, 0.0),
    ("exponential for re", 87, 8, 2.0),
    ("exponential for re", 82, 25, 0.0),
    ("exponential after an empty match", 81, 2, 1.0),
];

fn rows() -> Vec<Value> {
    let mut file = common::jsonl("pyre_memo.jsonl");
    file.remove(0);
    assert_eq!(file.len(), PINS.len());
    file
}

/// Prints each family's steps per character and peak (`--ignored`).
#[test]
#[ignore = "a measurement, printed"]
fn measure_families() {
    for row in rows() {
        let re = regex(&row);
        let method = row["method"].as_str().expect("method");
        let t = recipe(&row, SCALE);
        let (_, s) = call(&re, method, &t);
        let (_, h) = call(&re, method, &recipe(&row, SCALE / 2));
        eprintln!(
            "{:40} steps/char {:6.1} growth {:.3} peak {} woke {} bytes {}",
            row["note"].as_str().expect("note"),
            s.steps as f64 / t.len() as f64,
            s.steps as f64 / h.steps as f64,
            s.peak,
            s.woke,
            s.bytes
        );
    }
}

#[test]
fn families_are_linear_as_shipped() {
    let mut failures = Vec::new();
    for (row, &(note, c, peak, per)) in rows().iter().zip(PINS.iter()) {
        assert_eq!(row["note"].as_str(), Some(note));
        let re = regex(row);
        let method = row["method"].as_str().expect("method");
        let t = recipe(row, SCALE);
        let len = t.len() as u64;
        let budget = Some(c * (len + 64));
        let (got, s) = tried(&re, method, &t, budget);
        if got.contains("Budget") {
            failures.push(format!("{note}: over the budget of {c} * ({len} + 64)"));
            continue;
        }
        let (_, h) = tried(&re, method, &recipe(row, SCALE / 2), budget);
        // `len + 64`, the switch's own measure, so the pin holds at every
        // size the switch is reached.
        if s.steps > c * (len + 64) {
            failures.push(format!("{note}: {} steps over {c} * ({len} + 64)", s.steps));
        }
        if s.steps as f64 > 2.2 * h.steps as f64 {
            failures.push(format!("{note}: {} steps after {}", s.steps, h.steps));
        }
        let most = peak + (per * len as f64) as usize;
        if s.peak > most {
            failures.push(format!("{note}: peak {} over {most}", s.peak));
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

/// Every way agrees at a size where off still finishes in time.
#[test]
fn ways_agree_at_scale() {
    for row in rows() {
        let re = regex(&row);
        let method = row["method"].as_str().expect("method");
        let n = if row["part"] == "memo.extra" {
            22
        } else {
            4000
        };
        let t = recipe(&row, n);
        let want = call(&re.clone().with_memo(Setting::OFF), method, &t).0;
        for way in [Way::On, Way::Auto, Way::After(0), Way::After(1000)] {
            for intervals in [true, false] {
                let got = call(
                    &re.clone().with_memo(Setting { way, intervals }),
                    method,
                    &t,
                )
                .0;
                assert_eq!(got, want, "{} {way:?} {intervals}", row["note"]);
            }
        }
    }
}

/// Two of the shapes outside the inventory: exponential for `re` and for
/// plain backtracking, linear as shipped.
#[test]
fn exponential_shapes_end() {
    for pattern in [r"(?:a|aa)+b", r"(?:\w+\s?)+$"] {
        let re = Regex::new_str(pattern, 0).expect("built");
        let mut t = vec![97u32; SCALE];
        t.push(33);
        let budget = Some(87 * t.len() as u64);
        assert_eq!(re.try_search(t.as_slice(), 0, isize::MAX, budget), Ok(None));
        let s = last_call();
        assert!(s.woke && s.steps < 87 * t.len() as u64, "{pattern}: {s:?}");
    }
}

/// The attempt that must advance records copy B's bits: after the empty
/// match at 0, `(?:a|aa)*?(?<!a)` tries every way to split the run of
/// `a` there, which plain backtracking (and `re`) pays for exponentially.
/// As shipped the call is linear: four times the text costs at most
/// about four times the steps, under a constant per character.
#[test]
fn the_attempt_that_must_advance_is_linear() {
    for pattern in [r"(?:a|aa)*?(?<!a)", r"(?:(?:.|..)?[^b])*?(?<!.)"] {
        let re = Regex::new_str(pattern, 0).expect("built");
        let mut steps = Vec::new();
        for n in [SCALE / 4, SCALE] {
            let t = vec![97u32; n];
            // The bound as a budget per run, so a memo that stops
            // recording ends the test instead of running for ever.
            let budget = ADVANCE_PER_CHAR * (n as u64 + 64);
            let spans = re
                .try_finditer(t.as_slice(), 0, isize::MAX, Some(budget))
                .map(|ms| ms.iter().map(|m| m.regs[0]).collect::<Vec<_>>());
            assert_eq!(spans, Ok(vec![(0, 0)]), "{pattern}");
            let s = last_call();
            eprintln!(
                "{pattern} at {n}: steps/char {:.1} peak {} bytes {}",
                s.steps as f64 / n as f64,
                s.peak,
                s.bytes
            );
            assert!(s.woke && s.bytes > 0, "{pattern}: {s:?}");
            assert!(
                s.steps < ADVANCE_PER_CHAR * (n as u64 + 64),
                "{pattern}: {s:?}"
            );
            steps.push(s.steps);
        }
        assert!(
            steps[1] < 4 * steps[0] + steps[0] / 10,
            "{pattern}: {steps:?}"
        );
    }
}

/// The most steps per character of the two shapes above, measured in a
/// release build at one megabyte and written here.
const ADVANCE_PER_CHAR: u64 = 88;

/// The budget still cuts a call once the memo has woken: `(?=[^b]*?b)c`
/// reads the rest of the text from every start (the stated limit, a
/// lookaround that succeeds is not remembered), so as shipped it wakes the
/// memo and still spends more than a million steps over 1,000 `a` and a
/// `b`.
#[test]
fn the_budget_cuts_a_call_after_the_memo_woke() {
    let re = Regex::new_str(r"(?=[^b]*?b)c", 0).expect("built");
    let mut t = vec![97u32; 1000];
    t.push(98);
    assert_eq!(
        re.try_search(t.as_slice(), 0, isize::MAX, Some(1_000_000)),
        Err(CallError::Budget)
    );
    let s = last_call();
    assert!(s.woke, "{s:?}");
    assert_eq!(s.steps, 1_000_001, "{s:?}");
    assert_eq!(re.search(t.as_slice(), 0, isize::MAX), Ok(None));
    assert!(last_call().steps > 1_000_000, "{:?}", last_call());
}

/// The steps of every family at 250 and 2,000 characters with the memo on
/// from the first step, summed and pinned exactly, with the intervals and
/// without: a skip that saves less than it should (an end known to fail
/// tried again, a run read again) changes no answer, only these sums.
#[test]
fn the_steps_with_the_memo_on_are_pinned() {
    let mut sums = [0u64; 2];
    for (i, intervals) in [true, false].into_iter().enumerate() {
        for row in rows() {
            let re = regex(&row).with_memo(Setting {
                way: Way::On,
                intervals,
            });
            let method = row["method"].as_str().expect("method");
            let sizes: &[usize] = if row["part"] == "memo.extra" {
                &[12, 20]
            } else {
                &[250, 2000]
            };
            for &n in sizes {
                let (_, s) = call(&re, method, &recipe(&row, n));
                sums[i] += s.steps;
            }
        }
    }
    eprintln!("the steps with the memo on: {sums:?}");
    assert_eq!(sums, STEPS_ON);
}

/// The sums as measured, with the intervals and without.
const STEPS_ON: [u64; 2] = [1_053_354, 22_887_191];

/// A pattern with no loop over a group runs on a 64 KiB thread: the Rust
/// call stack does not grow with the text, the memo or the matches.
#[test]
fn a_small_thread_suffices() {
    let rows = rows();
    let handle = std::thread::Builder::new()
        .stack_size(64 * 1024)
        .spawn(move || {
            for row in rows.iter().filter(|r| r["part"] == "memo.family") {
                let re = regex(row);
                let method = row["method"].as_str().expect("method");
                call(&re, method, &recipe(row, 20_000));
            }
        })
        .expect("a thread");
    handle.join().expect("no overflow");
}

/// The bits of the largest pattern of the families, stated: a row of
/// `len / 64 + 1` words for each Split or Rep1 node that records a
/// failure, and four integers per Rep1 node.
#[test]
fn the_memory_of_the_bits_is_bounded() {
    let mut most = (0usize, String::new());
    for (row, &(note, c, _, _)) in rows().iter().zip(PINS.iter()) {
        let re = regex(row);
        let method = row["method"].as_str().expect("method");
        let t = recipe(row, 1 << 14);
        let (got, s) = tried(&re, method, &t, Some(2 * c * (t.len() as u64 + 64)));
        assert!(!got.contains("Budget"), "{note}: {s:?}");
        if s.bytes > most.0 {
            most = (s.bytes, row["note"].as_str().expect("note").to_string());
        }
    }
    eprintln!(
        "the most memo memory at 16 KiB: {} bytes ({})",
        most.0, most.1
    );
    // At most 300 rows of bits over a text of 2^14: 300 * 257 * 8 bytes.
    assert!(most.0 <= 300 * 257 * 8 + 4096, "{most:?}");
}

/// A growth the allocator refuses is `MatchError::Memory`, not an abort:
/// a bit past what the address space can hold.
#[test]
fn refused_growth_is_memory() {
    let mut memo = Memo::new(Setting::SHIPPED, usize::MAX / 2);
    assert_eq!(memo.set_for_test(0, 0), Err(MatchError::Memory));
}
