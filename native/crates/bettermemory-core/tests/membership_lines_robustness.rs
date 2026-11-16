//! Hostile sizes through `printable`, `quoted` and the membership lines:
//! no panic, and each output's length is the sum its rule gives, so the
//! work is linear in the input (sizes and buffer capacities are counted,
//! never timed).

use bettermemory_core::membership_lines::{self as ml, Candidate, ImportCandidate};
use bettermemory_core::printable::{printable, printable_into, quoted, quoted_into};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode;

const MIB: usize = 1024 * 1024;

fn cps(text: &str) -> PyStr {
    unicode::cps(text)
}

/// A turn of the hostile text, and what each of its characters becomes:
/// `printable` writes `\x1b`, `\u202e`, `\U000e0001`, `\ud800` and keeps
/// `"`, U+201D and `\`; `quoted` doubles each escape's backslash and
/// writes `\"`, `\u201d` and `\\`.
const TURN: [u32; 7] = [0x1B, 0x202E, 0xE0001, 0xD800, 0x22, 0x201D, 0x5C];
const PRINTABLE_WIDTH: [usize; 7] = [4, 6, 10, 6, 1, 1, 1];
const QUOTED_WIDTH: [usize; 7] = [5, 7, 11, 7, 2, 6, 2];

fn hostile(n: usize) -> PyStr {
    TURN.iter().copied().cycle().take(n).collect()
}

/// The length the rule gives for the first `n` characters of the turn.
fn by_rule(widths: &[usize; 7], n: usize) -> usize {
    widths.iter().cycle().take(n).sum()
}

#[test]
fn a_mebibyte_of_text() {
    let plain = vec![u32::from(b'a'); MIB];
    assert_eq!(printable(&plain), plain);
    assert_eq!(quoted(&plain).len(), MIB + 2);
    // 1,048,576 is 149,796 turns of seven and four characters more.
    let (turns, rest) = (MIB / 7, MIB % 7);
    assert_eq!((turns, rest), (149_796, 4));
    let text = hostile(MIB);
    assert_eq!(printable(&text).len(), turns * 29 + (4 + 6 + 10 + 6));
    assert_eq!(quoted(&text).len(), turns * 40 + (5 + 7 + 11 + 7) + 2);
}

/// Linear work without a clock: at 64 KiB, 256 KiB and 1 MiB, the output
/// appended to one buffer (`printable_into`, `quoted_into`) is exactly the
/// length the per-character rule gives, and the buffer's capacity stays
/// within twice that length, so its growth (amortised doubling) copies at
/// most a constant times the output.
#[test]
fn output_and_buffer_grow_linearly() {
    for n in [64 * 1024, 256 * 1024, MIB] {
        let text = hostile(n);
        let mut out = Vec::new();
        printable_into(&text, &mut out);
        assert_eq!(out.len(), by_rule(&PRINTABLE_WIDTH, n), "printable at {n}");
        assert!(out.capacity() <= 2 * out.len(), "printable buffer at {n}");
        let mut out = Vec::new();
        quoted_into(&text, &mut out);
        assert_eq!(out.len(), by_rule(&QUOTED_WIDTH, n) + 2, "quoted at {n}");
        assert!(out.capacity() <= 2 * out.len(), "quoted buffer at {n}");
    }
}

#[test]
fn every_code_point_once() {
    let all: PyStr = (0..0x11_0000).collect();
    let p = printable(&all);
    let q = quoted(&all);
    assert!(p.len() <= 10 * all.len());
    assert!(q.len() <= 12 * all.len() + 2);
    // Nothing `printable` leaves is a control, format, surrogate or line
    // separator, the tab aside; nothing `quoted` leaves raw is quote-like.
    for &c in &p {
        let cat = unicode::category(c);
        assert!(
            c == 0x09 || !matches!(cat, "Cc" | "Cf" | "Cs" | "Zl" | "Zp"),
            "U+{c:04X} survives printable"
        );
    }
    for &c in &q {
        assert!(
            !matches!(unicode::category(c), "Pi" | "Pf")
                && !bettermemory_core::printable::QUOTE_LIKE.contains(&c),
            "U+{c:04X} survives quoted"
        );
    }
}

#[test]
fn deep_combining_runs() {
    let n = 1_000_000;
    let mut on_letter = vec![u32::from(b'o')];
    on_letter.extend(std::iter::repeat_n(0x030B, n));
    assert_eq!(quoted(&on_letter).len(), n + 3);
    let mut on_space = vec![u32::from(b' ')];
    on_space.extend(std::iter::repeat_n(0x030E, n));
    // The first mark is escaped; the escape ends in a letter, so the
    // rest stand on it and are kept (as the reference does).
    assert_eq!(quoted(&on_space).len(), n + 8);
    let mut mixed = vec![u32::from(b'o')];
    mixed.extend(std::iter::repeat_n(0x0301, n));
    mixed.push(0x030B);
    assert_eq!(quoted(&mixed).len(), n + 4);
}

#[test]
fn a_hundred_thousand_names() {
    let n = 100_000;
    let names: Vec<PyStr> = (0..n).map(|i| cps(&format!("n{i:06}"))).collect();
    let across = ml::line_across_boundary(&cps("/work/alpha"), &names);
    let fixed = ml::line_across_boundary(&cps("/work/alpha"), &[]).len();
    assert_eq!(across.len(), fixed + n * 7 + (n - 1) * 2);
    let cands: Vec<Candidate> = (0..n)
        .map(|i| Candidate {
            name: cps(&format!("n{i:06}")),
            id: cps(&format!("P{i:06}")),
        })
        .collect();
    let one = ml::line_first_sight(
        &cps("/work/alpha"),
        &cands[..1],
        &cps("e"),
        None,
        false,
        None,
    );
    let two = ml::line_first_sight(
        &cps("/work/alpha"),
        &cands[..2],
        &cps("e"),
        None,
        false,
        None,
    );
    let all = ml::line_first_sight(&cps("/work/alpha"), &cands, &cps("e"), None, false, None);
    assert_eq!(all.len(), one.len() + (n - 1) * (two.len() - one.len()));
    let codes: Vec<PyStr> = (0..n).map(|i| cps(["T", "H", "zz", "O"][i % 4])).collect();
    let joined = ml::line_joined(&cps("/work/alpha"), &cps("a"), &cps("P"), &codes);
    let base = ml::line_joined(&cps("/work/alpha"), &cps("a"), &cps("P"), &[]).len();
    let words = n / 4 * ("holds its commits".len() + "is its git directory".len())
        + n / 4 * "carries its checkout token".len();
    assert_eq!(joined.len(), base + words + (3 * n / 4 - 1) * 2);
    let imports = vec![ImportCandidate {
        name: cps("a"),
        id: cps("P"),
        codes,
    }];
    let line = ml::line_import(&cps("a"), &cps("s"), &imports, Some(&cps("Q")));
    assert!(line.len() < 2_000 + 3 * n);
}

use bettermemory_core::membership_lines::{line_restated, restated_row, Named};
use bettermemory_core::pyvalue::PyValue;

const COLUMNS: [&str; 12] = [
    "id",
    "checkout",
    "kind",
    "candidates_json",
    "evidence_json",
    "reason",
    "line",
    "state",
    "answer",
    "answered_by",
    "signature_json",
    "opened",
];

fn base_row(field: &str, value: PyValue) -> PyValue {
    let mut value = Some(value);
    PyValue::Dict(
        COLUMNS
            .iter()
            .map(|&c| {
                let v = if c == field {
                    value.take().expect("one field")
                } else if c == "candidates_json" {
                    PyValue::List(vec![PyValue::str("P1")])
                } else if c == "evidence_json" {
                    PyValue::Dict(Vec::new())
                } else if matches!(c, "answer" | "answered_by" | "signature_json") {
                    PyValue::None
                } else {
                    PyValue::str("x")
                };
                (PyValue::str(c), v)
            })
            .collect(),
    )
}

fn nested(depth: usize) -> PyValue {
    let mut v = PyValue::int(1);
    for _ in 0..depth {
        v = PyValue::List(vec![v]);
    }
    v
}

/// Each column holding a list nested 9,998 and 100,000 deep, read on a
/// 512 KiB thread: an answer or a raise, never a panic or an overflow.
#[test]
fn rows_nested_deep_in_each_field() {
    let worker = std::thread::Builder::new()
        .stack_size(512 * 1024)
        .spawn(|| {
            let mut outcomes = Vec::new();
            for depth in [9_998, 100_000] {
                for field in COLUMNS {
                    let row = base_row(field, nested(depth));
                    let got = restated_row(&row).map(|r| {
                        let named: Vec<Named> = r
                            .candidates
                            .iter()
                            .map(|c| Named {
                                name: None,
                                id: c.clone(),
                            })
                            .collect();
                        line_restated(&r.id, &r.kind, &r.reason, &named, false, None, None).len()
                    });
                    outcomes.push((depth, field, got.map_err(|e| e.class)));
                }
            }
            outcomes
        })
        .expect("a thread");
    let outcomes = worker.join().expect("no panic");
    for (depth, field, got) in outcomes {
        let want_raise = match (depth, field) {
            (_, "checkout" | "answer" | "answered_by" | "signature_json") => None,
            (9_998, "evidence_json") | (100_000, "evidence_json") => Some("ValueError"),
            (9_998, _) => None,
            (_, _) => Some("RecursionError"),
        };
        match (want_raise, got) {
            (None, Ok(_)) => {}
            (Some(w), Err(e)) => assert_eq!(e, w, "{depth} {field}"),
            (w, g) => panic!("{depth} {field}: want {w:?}, got {g:?}"),
        }
    }
}

/// A mebibyte of text in each text argument: the line's length is the
/// sum of its parts, linear in the input.
#[test]
fn restated_line_of_a_mebibyte() {
    let big = vec![u32::from(b'a'); MIB];
    let one = |n: &[u32], at: Option<&[u32]>| {
        line_restated(
            n,
            &cps("file_contradiction"),
            &cps("unknown"),
            &[Named {
                name: Some(n.to_vec()),
                id: n.to_vec(),
            }],
            false,
            None,
            at,
        )
        .len()
    };
    let small = vec![u32::from(b'a'); 1];
    let (l1, lb) = (one(&small, Some(&small)), one(&big, Some(&big)));
    // The text appears a fixed number of times; each copy grows by MIB - 1.
    let copies = (lb - l1) / (MIB - 1);
    assert_eq!((lb - l1) % (MIB - 1), 0);
    assert!(copies <= 16, "{copies} copies");
}

/// Every code point once in the question (through `printable`) and in
/// `at` (through `quoted`): nothing `printable` escapes survives, the tab
/// aside. A name and an id go through the same two functions.
#[test]
fn restated_line_of_every_code_point() {
    let all: PyStr = (0..0x11_0000).collect();
    let kind = cps("file_contradiction");
    let reason = cps("unknown");
    let lines = [line_restated(
        &all,
        &kind,
        &reason,
        &[],
        false,
        None,
        Some(&all),
    )];
    for line in &lines {
        for &c in line {
            assert!(
                c == 0x09 || !matches!(unicode::category(c), "Cc" | "Cf" | "Cs" | "Zl" | "Zp"),
                "U+{c:04X} survives"
            );
        }
    }
}
