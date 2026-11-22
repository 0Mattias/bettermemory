//! Hostile transcripts: one very long line, many tiny lines, lines nested
//! past the decoder's depth, budgets at the integer bounds. No panic, and
//! time that grows with the input as the reference's does.

mod common;
mod timing;

use std::num::NonZeroUsize;

use bettermemory_core::transcript::{row, stop_turn, ReverseLines, CHUNK_BYTES};
use common::transcript::{MemTranscript, Replay};
use serde_json::json;

fn c(n: usize) -> NonZeroUsize {
    NonZeroUsize::new(n).unwrap()
}

fn count(data: Vec<u8>, chunk: usize, budget: i64) -> (usize, bool) {
    let file = MemTranscript::new(data);
    let mut lines = ReverseLines::new(&file, b"t", c(chunk), budget, None);
    let n = lines.by_ref().count();
    (n, lines.reached_start)
}

#[test]
fn budgets_at_the_integer_bounds() {
    for budget in [i64::MIN, -1, 0, 1, i64::MAX] {
        let (n, rs) = count(b"a\nb\n".to_vec(), 1, budget);
        assert_eq!(
            (n, rs),
            if budget >= 4 {
                (3, true)
            } else if budget > 0 {
                (n, false)
            } else {
                (0, false)
            }
        );
    }
    let (n, rs) = count(b"a\nb\n".to_vec(), usize::MAX, i64::MAX);
    assert_eq!((n, rs), (3, true));
}

#[test]
fn one_line_of_eight_mebibytes() {
    let mut data = vec![b'z'; 8 << 20];
    data.push(b'\n');
    assert_eq!(count(data.clone(), CHUNK_BYTES, i64::MAX), (2, true));
    // A budget that ends inside it yields only the empty last line.
    assert_eq!(count(data, CHUNK_BYTES, 4 << 20), (1, false));
}

#[test]
fn nested_lines_raise_without_a_panic() {
    let deep = vec![b'['; 1 << 20];
    assert!(row(&deep).is_err());
    let mut line = b"{\"type\":\"user\",\"x\":".to_vec();
    line.extend(std::iter::repeat_n(b'[', 100_000));
    let file = MemTranscript::new(line);
    let replay = Replay::of(&json!([]));
    assert!(stop_turn(&file, &replay, b"t", None, c(CHUNK_BYTES), i64::MAX).is_err());
}

#[test]
fn tiny_lines_grow_linearly() {
    let run = |n: usize| {
        let data = b"\n".repeat(n);
        let file = MemTranscript::new(data);
        let replay = Replay::of(&json!([]));
        move || {
            assert_eq!(
                stop_turn(&file, &replay, b"t", None, c(4096), i64::MAX).unwrap(),
                None
            );
        }
    };
    let mut small = run(100_000);
    let mut large = run(1_000_000);
    timing::grows_within(
        "stop_turn over 100,000 and 1,000,000 blank lines",
        timing::TENFOLD,
        &mut || timing::once(&mut small),
        &mut || timing::once(&mut large),
    );
}

#[test]
fn chunked_walks_grow_linearly() {
    let run = |n: usize| {
        let data = b"0123456789abcde\n".repeat(n);
        move || count(data.clone(), 4096, i64::MAX)
    };
    let mut small = run(20_000);
    let mut large = run(200_000);
    timing::grows_within(
        "ReverseLines over 20,000 and 200,000 lines",
        timing::TENFOLD,
        &mut || timing::once(&mut small),
        &mut || timing::once(&mut large),
    );
}
