//! Hostile input for `pyre::exec`: long texts, the pathological shapes of
//! backtracking, code units past U+10FFFF, empty texts and extreme
//! windows. Nothing panics; the native stack does not grow with the text
//! (the loop does not recurse); steps grow linearly on linear patterns.

mod common;
mod pyre_exec_support;
mod pyre_lower_support;
mod timing;

use bettermemory_core::pyre::exec::{exec, MatchError, Mode};
use pyre_exec_support::{compile, run, Naive};

fn codes(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn steps(pattern: &str, text: &[u32], mode: Mode) -> u64 {
    let prog = compile(pattern, 0, false);
    exec(&prog, &Naive, text, 0, text.len() as isize, mode, None)
        .expect("no refusal")
        .map_or(0, |m| m.steps)
}

/// The steps of a search that fails, by running it under a budget large
/// enough and reading the budget's answer back as a bound.
fn failing_steps(pattern: &str, text: &[u32]) -> u64 {
    let prog = compile(pattern, 0, false);
    let mut budget = 1u64;
    loop {
        match exec(
            &prog,
            &Naive,
            text,
            0,
            text.len() as isize,
            Mode::Search,
            Some(budget),
        ) {
            Ok(None) => return budget,
            Ok(Some(_)) => panic!("{pattern}: a match"),
            Err(MatchError::Budget) => budget *= 2,
            Err(MatchError::Memory) => panic!("memory"),
        }
    }
}

#[test]
fn steps_grow_linearly_on_linear_patterns() {
    let cases = [
        ("a*b", "a", "b"),
        ("(?:ab)*c", "ab", "c"),
        ("[ab]+$", "ab", ""),
        ("(?:a|b)*c", "ab", "c"),
        ("(?:a|b)*?c", "ab", "c"),
    ];
    for (pattern, unit, last) in cases {
        let n = 2000;
        let small = codes(&format!("{}{last}", unit.repeat(n)));
        let large = codes(&format!("{}{last}", unit.repeat(2 * n)));
        let (s, l) = (
            steps(pattern, &small, Mode::Fullmatch),
            steps(pattern, &large, Mode::Fullmatch),
        );
        assert!(s > 0, "{pattern}: no match");
        assert!(l * 10 <= s * 22, "{pattern}: {s} then {l}");
    }
    // A failing search over starts: `a` then not `b`, linear per start.
    let small = codes(&"ac".repeat(2000));
    let large = codes(&"ac".repeat(4000));
    let (s, l) = (failing_steps("ab", &small), failing_steps("ab", &large));
    assert!(l <= s * 3, "ab: {s} then {l}");
}

#[test]
fn a_long_text_needs_no_native_stack() {
    // Run on a thread with a small stack: recursion per character or per
    // node would overflow it.
    let handle = std::thread::Builder::new()
        .stack_size(64 * 1024)
        .spawn(|| {
            let t = codes(&"ab".repeat(200_000));
            let prog = compile("(?:ab)*", 0, false);
            let m =
                run(&prog, t.as_slice(), 0, t.len() as isize, Mode::Fullmatch).expect("a match");
            assert_eq!(m.end, 400_000);
            // One choice point per pass of the unrolled loop.
            assert!(m.peak <= 200_001, "{}", m.peak);
            let prog = compile("a*?b", 0, false);
            let t = codes(&format!("{}b", "a".repeat(400_000)));
            let m = run(&prog, t.as_slice(), 0, t.len() as isize, Mode::Match).expect("a match");
            assert_eq!(m.peak, 1);
        })
        .expect("a thread");
    handle.join().expect("no overflow");
}

#[test]
fn a_single_unit_repeat_holds_one_choice_point() {
    let t = codes(&"a".repeat(100_000));
    let prog = compile("a*a", 0, false);
    let m = run(&prog, t.as_slice(), 0, t.len() as isize, Mode::Match).expect("a match");
    assert_eq!((m.end, m.peak), (100_000, 1));
}

#[test]
fn exponential_shapes_end_under_a_budget() {
    // `(?:a|a)*b` fails in 2^n ways; the budget refuses it.
    let prog = compile("(?:a|a)*b", 0, false);
    let t = codes(&"a".repeat(40));
    let got = exec(
        &prog,
        &Naive,
        t.as_slice(),
        0,
        40,
        Mode::Match,
        Some(1_000_000),
    );
    assert_eq!(got, Err(MatchError::Budget));
}

#[test]
fn code_units_past_the_last_code_point() {
    let t = [0x11_0000u32, u32::MAX, 97];
    let prog = compile(r".\b.\ba", 0, false);
    assert_eq!(run(&prog, t.as_slice(), 0, 3, Mode::Match), None);
    let prog = compile("..a", 0, false);
    let m = run(&prog, t.as_slice(), 0, 3, Mode::Fullmatch).expect("a match");
    assert_eq!(m.end, 3);
    let prog = compile(r"[^a]+\b", 0, false);
    let m = run(&prog, t.as_slice(), 0, 3, Mode::Match).expect("a match");
    assert_eq!(m.end, 2);
}

#[test]
fn empty_texts_and_extreme_windows() {
    let empty: [u32; 0] = [];
    for pattern in ["", "a*", "$", r"\b", "(?:ab)*", "a|", "^"] {
        let prog = compile(pattern, 0, false);
        for (pos, endpos) in [
            (0, 0),
            (isize::MIN, isize::MAX),
            (isize::MAX, isize::MIN),
            (-1, 1),
        ] {
            for mode in [Mode::Match, Mode::Fullmatch, Mode::Search] {
                let _ = run(&prog, empty.as_slice(), pos, endpos, mode);
            }
        }
    }
    let prog = compile("a{4294967294,}", 0, false);
    assert_eq!(
        run(&prog, codes("aaa").as_slice(), 0, 3, Mode::Search),
        None
    );
    let prog = compile("a{0,4294967294}?$", 0, false);
    let m = run(&prog, codes("aaa").as_slice(), 0, 3, Mode::Match).expect("a match");
    assert_eq!(m.end, 3);
}

#[test]
fn time_grows_linearly_on_a_failing_search() {
    let prog = compile("a*b", 0, false);
    let small = codes(&"ac".repeat(5_000));
    let large = codes(&"ac".repeat(50_000));
    let time = |t: &Vec<u32>| {
        let prog = &prog;
        timing::once(|| run(prog, t.as_slice(), 0, t.len() as isize, Mode::Search))
    };
    timing::grows_within("a*b search", 30, &mut || time(&small), &mut || time(&large));
}

#[test]
fn nested_lookarounds_need_no_native_stack() {
    // Thirty nested lookaheads, each body a long greedy run, run on a small
    // stack: a body is a sub-attempt of the loop, not a recursive call.
    // The parser recurses by nesting, so the programs are built first.
    let pattern = format!("{}a*b{}", "(?=".repeat(30), ")".repeat(30));
    let nested = compile(&pattern, 0, false);
    let tempered = compile("(?:(?!b)a)*b", 0, false);
    let handle = std::thread::Builder::new()
        .stack_size(64 * 1024)
        .spawn(move || {
            let t = codes(&format!("{}b", "a".repeat(100_000)));
            let m = run(&nested, t.as_slice(), 0, t.len() as isize, Mode::Match).expect("a match");
            assert_eq!((m.start, m.end), (0, 0));
            // The cut leaves only the markers of the open lookarounds.
            assert!(m.peak <= 31, "{}", m.peak);
            let t = codes(&format!("{}b", "a".repeat(50_000)));
            let m = run(
                &tempered,
                t.as_slice(),
                0,
                t.len() as isize,
                Mode::Fullmatch,
            )
            .expect("a match");
            assert_eq!(m.end, 50_001);
        })
        .expect("a thread");
    handle.join().expect("no overflow");
}

#[test]
fn a_lookaround_in_a_failing_search_stays_linear() {
    let small = codes(&"ac".repeat(2000));
    let large = codes(&"ac".repeat(4000));
    for pattern in ["a(?=b)", "(?<=c)a(?!c)", "(?<!c)(?=a)c"] {
        let (s, l) = (
            failing_steps(pattern, &small),
            failing_steps(pattern, &large),
        );
        assert!(l <= s * 3, "{pattern}: {s} then {l}");
    }
}
