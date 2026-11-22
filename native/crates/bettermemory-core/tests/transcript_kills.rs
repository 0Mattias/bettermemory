//! Cases a plausible wrong `ReverseLines`, `_row` or `stop_turn` fails.
//! Each answer is the reference's (`transcript.py` at 793bee47), worked by
//! hand from its code and checked against it.

mod common;

use std::num::NonZeroUsize;

use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::transcript::{row, stop_turn, OwnerPrompt, ReverseLines, StopTurn};
use bettermemory_core::unicode::cps;
use common::transcript::MemTranscript;

fn c(n: usize) -> NonZeroUsize {
    NonZeroUsize::new(n).unwrap()
}

fn walk(data: &[u8], chunk: usize, budget: i64, needle: Option<&[u8]>) -> (Vec<String>, bool) {
    let file = MemTranscript::new(data.to_vec());
    let mut lines = ReverseLines::new(&file, b"t", c(chunk), budget, needle);
    let got = lines
        .by_ref()
        .map(|l| String::from_utf8(l).unwrap())
        .collect();
    (got, lines.reached_start)
}

#[test]
fn a_line_the_budget_cuts_is_not_yielded() {
    // "ab\ncd\n": a budget of 3 starts at "cd"'s first byte: yielded.
    assert_eq!(
        walk(b"ab\ncd\n", 2, 3, None),
        (vec!["".into(), "cd".into()], false)
    );
    // A budget of 2 cuts "cd": only the empty last line.
    assert_eq!(walk(b"ab\ncd\n", 2, 2, None), (vec!["".into()], false));
    // The whole file: reached_start.
    assert_eq!(
        walk(b"ab\ncd\n", 2, 6, None),
        (vec!["".into(), "cd".into(), "ab".into()], true)
    );
}

#[test]
fn a_line_longer_than_chunks_accumulates() {
    let data = b"x\nlonglonglong\ny";
    assert_eq!(
        walk(data, 3, 1 << 30, None),
        (vec!["y".into(), "longlonglong".into(), "x".into()], true)
    );
}

#[test]
fn the_needle_skips_whole_chunks_only() {
    // One chunk holds both lines: both are yielded though one lacks it.
    let (got, _) = walk(b"aa\nbz\n", 64, 1 << 30, Some(b"z"));
    assert_eq!(got, vec!["", "bz", "aa"]);
    // Chunks of 3: the chunk "aa" (no z) yields nothing.
    let (got, rs) = walk(b"aa\nbz\n", 3, 1 << 30, Some(b"z"));
    assert_eq!(got, vec!["", "bz"]);
    assert!(rs);
}

#[test]
fn a_failed_read_ends_the_walk_and_keeps_reached_start_false() {
    let mut file = MemTranscript::new(b"a\nb\nc\n".to_vec());
    file.fail_read = Some(1);
    let mut lines = ReverseLines::new(&file, b"t", c(2), 1 << 30, None);
    let got: Vec<Vec<u8>> = lines.by_ref().collect();
    assert_eq!(got, vec![b"".to_vec(), b"c".to_vec()]);
    assert!(!lines.reached_start);
}

#[test]
fn a_negative_budget_reads_nothing_and_reaches_the_start_only_when_empty() {
    assert_eq!(walk(b"a\n", 4, -1, None), (vec![], false));
    assert_eq!(walk(b"", 4, -1, None), (vec![], true));
}

#[test]
fn row_keeps_dicts_only() {
    assert!(row(b" \x0b\x0c\t\r\n").unwrap().is_none());
    assert!(row(b"[{}]").unwrap().is_none());
    assert!(row(b"\xef\xbb\xbf{}").unwrap().is_none());
    assert!(matches!(
        row(b"{\"a\":\"\xff\"}").unwrap(),
        Some(PyValue::Dict(_))
    ));
    assert!(row(&[b'['; 20_000]).is_err());
}

/// A small definition: a string content is submitted, and the owner's
/// unless it starts with `!`.
struct Simple;

fn content(row: &PyValue) -> Option<&PyValue> {
    let PyValue::Dict(f) = row else { return None };
    let msg = f.iter().find(|(k, _)| *k == PyValue::Str(cps("message")))?;
    let PyValue::Dict(m) = &msg.1 else {
        return None;
    };
    m.iter()
        .find(|(k, _)| *k == PyValue::Str(cps("content")))
        .map(|p| &p.1)
}

impl OwnerPrompt for Simple {
    fn is_submitted(&self, row: &PyValue) -> bool {
        matches!(content(row), Some(PyValue::Str(_)))
    }
    fn owner_prompt(&self, row: &PyValue, _cut: bool) -> Option<PyStr> {
        match content(row) {
            Some(PyValue::Str(s)) if s.first() != Some(&u32::from('!')) => Some(s.clone()),
            _ => None,
        }
    }
    fn submitted_text(&self, _row: &PyValue) -> Option<PyStr> {
        None
    }
    fn text_blocks(&self, content: &PyValue) -> Option<PyStr> {
        match content {
            PyValue::Str(s) => Some(s.clone()),
            _ => None,
        }
    }
}

fn st(text: &str, pid: Option<&str>, chunk: usize, budget: i64) -> Option<StopTurn> {
    let file = MemTranscript::new(text.as_bytes().to_vec());
    let pid = pid.map(cps);
    stop_turn(&file, &Simple, b"t", pid.as_deref(), c(chunk), budget).unwrap()
}

fn s(t: &str) -> Option<PyStr> {
    Some(cps(t))
}

const U1: &str = r#"{"type":"user","promptId":"p1","message":{"content":"one"}}"#;
const U2: &str = r#"{"type":"user","promptId":"p2","message":{"content":"two"}}"#;
const A: &str = r#"{"type":"assistant","message":{"content":"said","model":"m"}}"#;

#[test]
fn a_newer_turn_s_replies_are_dropped() {
    let text = format!("{U1}\n{U2}\n{A}\n");
    assert_eq!(
        st(&text, Some("p1"), 64, 1 << 30),
        Some(StopTurn {
            prompt: s("one"),
            reply: None,
            model: None
        })
    );
    assert_eq!(
        st(&text, None, 64, 1 << 30),
        Some(StopTurn {
            prompt: s("two"),
            reply: s("said"),
            model: s("m")
        })
    );
}

#[test]
fn an_older_turn_ends_the_turn_even_when_the_budget_did_not_reach_the_start() {
    let meta = r#"{"type":"user","promptId":"p2","message":{"content":"!x"}}"#;
    let text = format!("{U1}\n{meta}\n{A}\n");
    let budget = (text.len() - U1.len() + 3) as i64;
    // The older user row is read whole only with the full budget.
    assert_eq!(
        st(&text, None, 8, 1 << 30),
        Some(StopTurn {
            prompt: None,
            reply: s("said"),
            model: s("m")
        })
    );
    // Cut inside U1: the walk ends inside the turn, undecided.
    assert_eq!(st(&text, None, 8, budget), None);
}

#[test]
fn without_ids_a_submitted_row_that_is_not_the_owner_s_ends_the_turn() {
    let first = r#"{"type":"user","message":{"content":"first"}}"#;
    let bang = r#"{"type":"user","message":{"content":"!no"}}"#;
    let text = format!("{first}\n{bang}\n");
    assert_eq!(
        st(&text, None, 64, 1 << 30),
        Some(StopTurn {
            prompt: None,
            reply: None,
            model: None
        })
    );
}

#[test]
fn no_submitted_row_is_none() {
    let tool = r#"{"type":"user","promptId":"p1","message":{"content":[]}}"#;
    assert_eq!(st(&format!("{tool}\n{A}"), None, 64, 1 << 30), None);
    assert_eq!(st("", None, 64, 1 << 30), None);
}

#[test]
fn an_empty_prompt_id_on_a_row_counts_as_no_id() {
    let first = r#"{"type":"user","message":{"content":"first"}}"#;
    let second = r#"{"type":"user","promptId":"","message":{"content":"second"}}"#;
    let text = format!("{first}\n{A}\n{second}\n{A}\n");
    // Without ids the newest submitted row ends the turn; an empty id
    // taken as a target would split the turn elsewhere.
    assert_eq!(
        st(&text, None, 64, 1 << 30),
        Some(StopTurn {
            prompt: s("second"),
            reply: s("said"),
            model: s("m")
        })
    );
}
