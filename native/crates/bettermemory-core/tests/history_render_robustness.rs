//! `history`'s renderers over hostile input, and their cost.
//!
//! - 100,000 rows and a body diff of a megabyte render whole, each line
//!   where it belongs.
//! - A detail value nested past the budget is RecursionError, in both
//!   renderers, without overflowing the stack; the bounds are those
//!   CPython 3.13.13 showed from a plain script on macOS (9,998 lists
//!   around a value in `_value_text`, 9,994 in `render_json`, whose value
//!   sits under four containers; a detail name of 9,997 tuples around a
//!   scalar). The reference's bound follows the release and the caller's
//!   stack, so no golden file pins it.
//! - An `int` past 4,300 digits is ValueError, as a value and as a name.
//! - Both renderers are linear in the rows and in the diff: ten times
//!   the input costs under sixty times the time (`tests/timing`).
//! - A long `str` value is cut without being copied: its cost does not
//!   follow its length. An actor's three names are read in one pass over
//!   its pairs, which stops once all three are met.
//! - Every answer is compared whole, or by the SHA-256 of the text
//!   CPython 3.13.13 wrote for the same history where the text runs to
//!   tens of megabytes.

mod timing;

use bettermemory_core::history_record::RecordHistory;
use bettermemory_core::history_render::{
    actor_text, render_json, render_text, value_text, RenderError,
};
use bettermemory_core::history_rows::HistoryRow;
use bettermemory_core::json::MAX_DEPTH;
use bettermemory_core::pyvalue::{PyError, PyStr, PyValue};
use bettermemory_core::unicode::cps;
use sha2::{Digest, Sha256};

fn row(seq: i64) -> HistoryRow {
    HistoryRow {
        seq,
        ts: "2026-01-02T03:04:05Z".to_owned(),
        session: Some("s".to_owned()),
        kind: "memory_put".to_owned(),
        role: "record",
        action: "updated".to_owned(),
        verified: Some(seq % 2 == 0),
        actor: Some(PyValue::Dict(vec![(
            PyValue::str("client"),
            PyValue::str("c"),
        )])),
        actor_source: Some("record"),
        changed: vec![cps("body")],
        detail: vec![(PyValue::str("n"), PyValue::int(i128::from(seq)))],
        diff: Some(cps("-a\n+b\n")),
    }
}

fn hist(rows: Vec<HistoryRow>) -> RecordHistory {
    RecordHistory {
        record_id: cps("R"),
        record_kind: Some("memory"),
        state: "active",
        provenance: Some("local".to_owned()),
        key_present: true,
        rows,
    }
}

fn lines(text: &[u32]) -> usize {
    text.iter().filter(|&&c| c == u32::from(b'\n')).count()
}

fn nested(depth: usize) -> PyValue {
    let mut value = PyValue::int(0);
    for _ in 0..depth {
        value = PyValue::List(vec![value]);
    }
    value
}

fn tuples(depth: usize) -> PyValue {
    let mut value = PyValue::int(0);
    for _ in 0..depth {
        value = PyValue::Tuple(vec![value]);
    }
    value
}

/// The SHA-256 of an ASCII text, in hex, fed in pieces so that a text
/// of hundreds of megabytes is not copied whole.
fn ascii_sha256(text: &[u32]) -> String {
    let mut hasher = Sha256::new();
    for piece in text.chunks(1 << 16) {
        let bytes: Vec<u8> = piece
            .iter()
            .map(|&c| u8::try_from(c).ok().filter(u8::is_ascii).expect("ascii"))
            .collect();
        hasher.update(&bytes);
    }
    hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// The text of a history of one row, row 1, whose detail is `name_value`
/// (the rest of `row(1)`), as the reference writes it.
fn one_row_text(name_value: &str) -> PyStr {
    cps(&format!(
        "bettermemory memory history R: memory, active, 1 row\n  provenance local\n  seq 1 \
         2026-01-02T03:04:05Z memory_put updated [MAC FAILS] author client=c changed body \
         {name_value}\n  findings\n    seq 1 (memory_put) fails its MAC\n"
    ))
}

fn with_detail(name: PyValue, value: PyValue) -> RecordHistory {
    let mut r = row(1);
    r.detail = vec![(name, value)];
    hist(vec![r])
}

#[test]
fn a_hundred_thousand_rows() {
    let n = 100_000;
    let h = hist((1..=n).map(row).collect());
    // The header, the provenance, a line per row (with its diff under
    // `diff`), the findings' header and one finding per odd seq.
    let want = |diff: bool| {
        let mut out = String::from(
            "bettermemory memory history R: memory, active, 100000 rows\n  provenance local\n",
        );
        for seq in 1..=n {
            let verdict = if seq % 2 == 0 { "mac ok" } else { "MAC FAILS" };
            out += &format!(
                "  seq {seq} 2026-01-02T03:04:05Z memory_put updated [{verdict}] author \
                 client=c changed body n={seq}\n"
            );
            if diff {
                out += "      -a\n      +b\n      \n";
            }
        }
        out += "  findings\n";
        for seq in (1..=n).step_by(2) {
            out += &format!("    seq {seq} (memory_put) fails its MAC\n");
        }
        cps(&out)
    };
    let text = render_text(&h, false).expect("rendered");
    assert_eq!(lines(&text), 2 + 100_000 + 1 + 50_000);
    assert!(text == want(false));
    assert!(render_text(&h, true).expect("rendered") == want(true));
    // What CPython 3.13.13 writes for the same history: 41,022,435
    // characters.
    let json = render_json(&h, true).expect("rendered");
    assert_eq!(json.len(), 41_022_435);
    assert_eq!(
        ascii_sha256(&json),
        "5cc938202a25b517adc3da12265708b56370248c84cdfe74902ab112b8ebc777"
    );
}

#[test]
fn a_diff_of_a_megabyte() {
    let mut diff = Vec::with_capacity(1 << 20);
    let mut count = 0;
    while diff.len() < 1 << 20 {
        diff.extend(cps(&format!("+line {count} \u{1b}[2J\n")));
        count += 1;
    }
    let mut r = row(2);
    r.diff = Some(diff.clone());
    let h = hist(vec![r]);
    let text = render_text(&h, true).expect("rendered");
    // The header, the provenance, the row, every piece (the last empty).
    assert_eq!(lines(&text), 3 + count + 1);
    let mut want = String::from(
        "bettermemory memory history R: memory, active, 1 row\n  provenance local\n  seq 2 \
         2026-01-02T03:04:05Z memory_put updated [mac ok] author client=c changed body n=2\n",
    );
    for i in 0..count {
        want += &format!("      +line {i} \\x1b[2J\n");
    }
    want += "      \n";
    assert!(
        text == cps(&want),
        "every escape is escaped, every piece a line"
    );
    // What CPython 3.13.13 writes for the same history: 1,423,165
    // characters, the diff one string with its escapes.
    let json = render_json(&h, true).expect("rendered");
    assert_eq!(json.len(), 1_423_165);
    assert_eq!(
        ascii_sha256(&json),
        "397dfa6316841b0ea29e9b7f6cdca0139a757308fa3b896cd13ba82dbe30514d"
    );
}

#[test]
fn nesting_past_the_budget_is_recursion_error() {
    assert_eq!(MAX_DEPTH, 9998);
    let name = || PyValue::str("d");
    // `_value_text`: 9,998 lists around a value are written, 9,999 raise.
    let mut cut = vec![u32::from(b'['); 77];
    cut.extend(cps("..."));
    assert_eq!(value_text(&nested(9998)), Ok(cut));
    assert_eq!(value_text(&nested(9999)), Err(PyError::RecursionError));
    assert_eq!(
        render_text(&with_detail(name(), nested(9998)), false),
        Ok(one_row_text(&format!("d={}...", "[".repeat(77))))
    );
    assert_eq!(
        render_text(&with_detail(name(), nested(9999)), false),
        Err(RenderError::Raised(PyError::RecursionError))
    );
    // `render_json`: the value sits under the document, the rows, the
    // row and the detail.
    // What CPython 3.13.13 writes: 199,960,547 characters.
    let json = render_json(&with_detail(name(), nested(9994)), false).expect("rendered");
    assert_eq!(json.len(), 199_960_547);
    assert_eq!(
        ascii_sha256(&json),
        "a19da4638de4d272b28c86f77663e53e0e5a29e0e5d72aa3805003aa6c302196"
    );
    drop(json);
    assert_eq!(
        render_json(&with_detail(name(), nested(9995)), false),
        Err(PyError::RecursionError)
    );
    // A detail name is spelled by `str()` through an f-string.
    assert_eq!(
        render_text(&with_detail(tuples(9997), PyValue::int(1)), false),
        Ok(one_row_text(&format!(
            "{}0{}=1",
            "(".repeat(9997),
            ",)".repeat(9997)
        )))
    );
    assert_eq!(
        render_text(&with_detail(tuples(9998), PyValue::int(1)), false),
        Err(RenderError::Raised(PyError::RecursionError))
    );
    // Far past it, without overflowing the stack.
    let deep = with_detail(name(), nested(100_000));
    assert_eq!(
        render_text(&deep, false),
        Err(RenderError::Raised(PyError::RecursionError))
    );
    assert_eq!(render_json(&deep, true), Err(PyError::RecursionError));
    assert_eq!(
        render_text(&with_detail(tuples(100_000), PyValue::int(1)), false),
        Err(RenderError::Raised(PyError::RecursionError))
    );
}

#[test]
fn integers_past_the_digit_limit() {
    let big = PyValue::Int("7".repeat(4301));
    assert_eq!(value_text(&big), Err(PyError::ValueError));
    let h = with_detail(PyValue::str("n"), big.clone());
    assert_eq!(
        render_text(&h, false),
        Err(RenderError::Raised(PyError::ValueError))
    );
    assert_eq!(render_json(&h, false), Err(PyError::ValueError));
    let h = with_detail(PyValue::Tuple(vec![PyValue::int(1), big]), PyValue::int(1));
    assert_eq!(
        render_text(&h, false),
        Err(RenderError::Raised(PyError::ValueError))
    );
    let mut cut = cps(&"7".repeat(77));
    cut.extend(cps("..."));
    assert_eq!(value_text(&PyValue::Int("7".repeat(4300))), Ok(cut));
}

#[test]
fn a_long_value_is_cut_to_eighty() {
    let mut text = vec![u32::from(b'x'); 1 << 22];
    text.push(0x1F600);
    let mut cut = vec![u32::from(b'x'); 77];
    cut.extend(cps("..."));
    assert_eq!(value_text(&PyValue::Str(text)), Ok(cut));
}

#[test]
fn rendering_is_linear_in_the_rows() {
    let small = hist((1..=5_000).map(row).collect());
    let large = hist((1..=50_000).map(row).collect());
    timing::grows_within(
        "render_text: 5,000 and 50,000 rows",
        timing::TENFOLD,
        &mut || timing::once(|| render_text(&small, true)),
        &mut || timing::once(|| render_text(&large, true)),
    );
    timing::grows_within(
        "render_json: 5,000 and 50,000 rows",
        timing::TENFOLD,
        &mut || timing::once(|| render_json(&small, true)),
        &mut || timing::once(|| render_json(&large, true)),
    );
}

#[test]
fn rendering_is_linear_in_the_diff() {
    let with = |lines: usize| {
        let mut r = row(2);
        r.diff = Some(cps(&"-old line\n+new line\n".repeat(lines)));
        hist(vec![r])
    };
    let (small, large) = (with(10_000), with(100_000));
    timing::grows_within(
        "render_text: a diff of 10,000 and 100,000 pairs of lines",
        timing::TENFOLD,
        &mut || timing::once(|| render_text(&small, true)),
        &mut || timing::once(|| render_text(&large, true)),
    );
}

#[test]
fn a_deep_name_item_before_a_long_int_raises_at_the_bound() {
    // `str()` of a tuple writes its first item before the second: 9,997
    // tuples deep the first is past the budget, 9,996 deep the second's
    // ValueError is met first (CPython 3.13.13).
    let big = PyValue::Int(format!("1{}", "0".repeat(4300)));
    let name = |n: usize, second: PyValue| {
        with_detail(PyValue::Tuple(vec![tuples(n), second]), PyValue::int(1))
    };
    let raised = |h: &RecordHistory| render_text(h, false).map(|_| ()).map_err(RenderError::name);
    assert_eq!(raised(&name(9996, big.clone())), Err("ValueError"));
    assert_eq!(raised(&name(9997, big)), Err("RecursionError"));
    assert_eq!(
        render_text(&name(9996, PyValue::int(1)), false),
        Ok(one_row_text(&format!(
            "({}0{}, 1)=1",
            "(".repeat(9996),
            ",)".repeat(9996)
        )))
    );
    assert_eq!(raised(&name(9997, PyValue::int(1))), Err("RecursionError"));
}

#[test]
fn a_long_value_s_cost_does_not_follow_its_length() {
    let short = PyValue::Str(vec![u32::from(b'x'); 1_000]);
    let long = PyValue::Str(vec![u32::from(b'x'); 10_000_000]);
    // A hundred calls each: copying ten million code points a hundred
    // times takes seconds; cutting them takes microseconds.
    let calls = |value: &PyValue| {
        timing::once(|| {
            for _ in 0..100 {
                drop(value_text(value));
            }
        })
    };
    timing::grows_within(
        "value_text: a str of 1,000 and of 10,000,000 code points",
        2,
        &mut || calls(&short),
        &mut || calls(&long),
    );
    let (small, large) = (
        with_detail(PyValue::str("v"), short),
        with_detail(PyValue::str("v"), long),
    );
    let renders = |h: &RecordHistory| {
        timing::once(|| {
            for _ in 0..100 {
                drop(render_text(h, false));
            }
        })
    };
    timing::grows_within(
        "render_text: a detail str of 1,000 and of 10,000,000 code points",
        2,
        &mut || renders(&small),
        &mut || renders(&large),
    );
}

#[test]
fn an_actor_s_names_are_read_in_one_pass() {
    let filler = |n: usize| -> Vec<(PyValue, PyValue)> {
        (0..n)
            .map(|i| (PyValue::str(&format!("k{i}")), PyValue::str("v")))
            .collect()
    };
    let names = || {
        vec![
            (PyValue::str("principal"), PyValue::str("p")),
            (PyValue::str("client"), PyValue::str("c")),
            (PyValue::str("model"), PyValue::str("m")),
        ]
    };
    // The three names last: the pass stops at them, whatever precedes.
    let last = |n: usize| {
        let mut items = filler(n);
        items.extend(names());
        PyValue::Dict(items)
    };
    let (small, large) = (last(1_000), last(1_000_000));
    assert_eq!(actor_text(&large), Ok(cps("client=c model=m principal=p")));
    let calls = |actor: &PyValue| {
        timing::once(|| {
            for _ in 0..100 {
                drop(actor_text(actor));
            }
        })
    };
    timing::grows_within(
        "actor_text: the names after 1,000 and 1,000,000 other keys",
        2,
        &mut || calls(&small),
        &mut || calls(&large),
    );
    // The names first, each given again later as a value that is not
    // text: one pass, from the last pair, linear in the pairs.
    let first = |n: usize| {
        let mut items = names();
        items.extend(filler(n));
        items.push((PyValue::str("model"), PyValue::int(1)));
        PyValue::Dict(items)
    };
    let (small, large) = (first(100_000), first(1_000_000));
    assert_eq!(actor_text(&large), Ok(cps("client=c principal=p")));
    timing::grows_within(
        "actor_text: the names before 100,000 and 1,000,000 other keys",
        timing::TENFOLD,
        &mut || timing::once(|| actor_text(&small)),
        &mut || timing::once(|| actor_text(&large)),
    );
}
