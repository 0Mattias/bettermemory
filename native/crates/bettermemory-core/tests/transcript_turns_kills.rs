//! Cases a plausible wrong `interrupted_turns`, `owner_words` or
//! `compact_marker` fails. The answers are the reference's, read from
//! `transcript.py` 259-450.

mod common;

use std::num::NonZeroUsize;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::transcript::{
    compact_marker, interrupted_turns, owner_words, InterruptedTurn, CHUNK_BYTES,
};
use bettermemory_core::transcript::{Opener, OsFailure};
use bettermemory_core::unicode::cps;
use common::transcript::{MemHandle, MemTranscript};
use common::transcript_owner::Plain;

const STOP: &str = "[Request interrupted by user]";

fn c() -> NonZeroUsize {
    NonZeroUsize::new(CHUNK_BYTES).unwrap()
}

fn dt(text: &str) -> DateTime {
    DateTime::from_isoformat(&cps(text)).unwrap()
}

fn user(pid: &str, text: &str, ts: &str) -> String {
    format!(
        r#"{{"type":"user","promptId":"{pid}","timestamp":{ts},"message":{{"content":"{text}"}}}}"#
    )
}

fn file(lines: &[String]) -> MemTranscript {
    MemTranscript::new((lines.join("\n") + "\n").into_bytes())
}

fn turns(lines: &[String], since: &str) -> Vec<InterruptedTurn> {
    interrupted_turns(
        &file(lines),
        &Plain::default(),
        b"t",
        &dt(since),
        c(),
        i64::MAX,
    )
    .unwrap()
}

fn s(text: &str) -> Option<Vec<u32>> {
    Some(cps(text))
}

/// A user row stamped exactly at `since` ends the walk: `<=`, not `<`.
#[test]
fn a_row_at_since_ends_the_walk() {
    let rows = [
        user("p1", "typed", r#""2026-01-01T00:00:10Z""#),
        user("p1", STOP, r#""2026-01-01T00:00:11Z""#),
    ];
    assert_eq!(turns(&rows, "2026-01-01T00:00:10+00:00").len(), 0);
    assert_eq!(turns(&rows, "2026-01-01T00:00:09+00:00").len(), 1);
}

/// Stamps are ordered by instant, not by their wall-clock text, and `at`
/// keeps the row's own offset.
#[test]
fn stamps_compare_by_instant() {
    let rows = [
        user("p1", "typed", r#""2026-01-01T05:00:00+05:00""#),
        user("p1", STOP, r#""2026-01-01T05:00:01+05:00""#),
    ];
    let got = turns(&rows, "2025-12-31T23:00:00+00:00");
    assert_eq!(got.len(), 1);
    assert_eq!(got[0].at.isoformat(), "2026-01-01T05:00:01+05:00");
    // Later than both instants, earlier than both wall clocks.
    assert!(turns(&rows, "2026-01-01T04:00:00+00:00").is_empty());
}

/// `at` is the newest submitted row's stamp, the interrupt line's when it
/// is newest; a stamp that is not a str is no stamp, and the next older
/// row's is taken.
#[test]
fn at_is_the_newest_stamp_held() {
    let rows = [
        user("p1", "typed", r#""2026-01-01T00:00:10Z""#),
        user("p1", STOP, "1767225611"),
    ];
    let got = turns(&rows, "2026-01-01T00:00:00Z");
    assert_eq!(got[0].at.isoformat(), "2026-01-01T00:00:10+00:00");
    assert_eq!(got[0].prompt, s("typed"));
}

/// The interrupt line is found after `lstrip`; the newest owner row is the
/// prompt; a turn with only the interrupt line is not returned.
#[test]
fn the_interrupt_line_and_the_prompt() {
    let rows = [
        user("p1", "older words", r#""2026-01-01T00:00:09Z""#),
        user("p1", "newer words", r#""2026-01-01T00:00:10Z""#),
        user("p1", &format!(" \\t{STOP}"), r#""2026-01-01T00:00:11Z""#),
        user("p2", STOP, r#""2026-01-01T00:00:12Z""#),
    ];
    let got = turns(&rows, "2026-01-01T00:00:00Z");
    assert_eq!(got.len(), 1);
    assert_eq!(got[0].prompt, s("newer words"));
    assert_eq!(got[0].prompt_id, s("p1"));
}

/// A naive `since` is Python's TypeError once a stamp is compared, and
/// nothing when no user row is stamped.
#[test]
fn a_naive_since() {
    let rows = [user("p1", "x", r#""2026-01-01T00:00:10Z""#)];
    let since = dt("2026-01-01T00:00:00");
    let err = interrupted_turns(&file(&rows), &Plain::default(), b"t", &since, c(), i64::MAX);
    assert_eq!(err.unwrap_err().name(), "TypeError");
    let rows = [user("p1", "x", "null"), user("p1", STOP, "null")];
    let got = interrupted_turns(&file(&rows), &Plain::default(), b"t", &since, c(), i64::MAX);
    assert_eq!(got.unwrap(), vec![]);
}

fn words(lines: &[String], chars: i64, owner: &Plain) -> String {
    let got = owner_words(&file(lines), owner, b"t", chars, c(), i64::MAX).unwrap();
    got.iter().map(|&u| char::from_u32(u).unwrap()).collect()
}

/// Code points, not bytes; the blank line between pieces counts; the cut
/// is from the front; the walk stops once `chars` are held.
#[test]
fn owner_words_counts_code_points() {
    let rows = [
        user("p1", "first", "null"),
        user("p2", "\u{e9}\u{1f600}ab", "null"),
    ];
    let owner = Plain::default();
    assert_eq!(words(&rows, 4, &owner), "\u{e9}\u{1f600}ab");
    assert_eq!(owner.asked.get(), 1, "the walk stops at four code points");
    let owner = Plain::default();
    assert_eq!(words(&rows, 5, &owner), "\n\u{e9}\u{1f600}ab");
    assert_eq!(owner.asked.get(), 2);
    assert_eq!(words(&rows, 7, &Plain::default()), "t\n\n\u{e9}\u{1f600}ab");
    assert_eq!(words(&rows, 0, &Plain::default()), "");
    assert_eq!(
        words(&rows, i64::MAX, &Plain::default()),
        "first\n\n\u{e9}\u{1f600}ab"
    );
}

/// A line without the bytes `"user"` is not parsed, even a user row
/// spelled with an escape; an empty text is no piece.
#[test]
fn owner_words_skips_lines_without_the_needle() {
    let rows = [
        user("p1", "kept", "null"),
        format!(
            "{{\"type\":\"{}ser\",\"message\":{{\"content\":\"escaped\"}}}}",
            "\\u0075"
        ),
        user("p2", "", "null"),
    ];
    assert_eq!(words(&rows, 100, &Plain::default()), "kept");
}

fn marker(lines: &[String], near: &str, window_us: i128) -> Option<(String, String)> {
    compact_marker(&file(lines), b"t", &dt(near), window_us, c(), i64::MAX)
        .unwrap()
        .map(|m| {
            (
                m.key.iter().map(|&u| char::from_u32(u).unwrap()).collect(),
                m.at.isoformat(),
            )
        })
}

fn boundary(ts: &str, uuid: &str) -> String {
    format!(r#"{{"type":"system","subtype":"compact_boundary","timestamp":{ts},"uuid":{uuid}}}"#)
}

/// The window is inclusive; a negative one holds nothing; the key falls
/// back to the timestamp's text when the uuid is empty or not a str.
#[test]
fn compact_marker_window_and_key() {
    let rows = [
        boundary(r#""2026-01-01T00:00:10Z""#, r#""u1""#),
        boundary(r#""2026-01-01T00:00:20Z""#, "3"),
    ];
    let sec = 1_000_000;
    assert_eq!(
        marker(&rows, "2026-01-01T00:00:21+00:00", sec),
        Some((
            "2026-01-01T00:00:20Z".into(),
            "2026-01-01T00:00:20+00:00".into()
        ))
    );
    assert_eq!(marker(&rows, "2026-01-01T00:00:21+00:00", sec - 1), None);
    assert_eq!(
        marker(&rows, "2026-01-01T00:00:09+00:00", sec).map(|m| m.0),
        Some("u1".into())
    );
    assert_eq!(marker(&rows, "2026-01-01T00:00:20+00:00", -1), None);
    let rows = [boundary("20", r#""u""#)];
    assert_eq!(marker(&rows, "1970-01-01T00:00:20+00:00", sec), None);
}

/// A row of another subtype is no marker, even one whose line holds the
/// needle and whose stamp is in the window.
#[test]
fn compact_marker_reads_the_subtype() {
    let rows = [String::from(
        r#"{"type":"system","subtype":"other","timestamp":"2026-01-01T00:00:40Z","uuid":"x","note":"compact_boundary"}"#,
    )];
    assert_eq!(marker(&rows, "2026-01-01T00:00:40+00:00", 0), None);
}

/// A path that opens but is no regular file (a FIFO, a device): the
/// reference's `path.is_file()` is False, so it answers "" and opens
/// nothing.
struct NotAFile(MemTranscript);

impl Opener for NotAFile {
    type File = MemHandle;
    fn open(&self, _path: &[u8]) -> Result<MemHandle, OsFailure> {
        panic!("owner_words opened a path that is no regular file")
    }
    fn size(&self, file: &mut MemHandle) -> Result<u64, OsFailure> {
        self.0.size(file)
    }
    fn seek(&self, file: &mut MemHandle, offset: u64) -> Result<(), OsFailure> {
        self.0.seek(file, offset)
    }
    fn read(&self, file: &mut MemHandle, n: usize) -> Result<Vec<u8>, OsFailure> {
        self.0.read(file, n)
    }
    fn is_file(&self, _path: &[u8]) -> bool {
        false
    }
}

#[test]
fn owner_words_opens_only_a_regular_file() {
    let rows = [user("p1", "kept", "null")];
    assert_eq!(words(&rows, 100, &Plain::default()), "kept");
    let opener = NotAFile(file(&rows));
    let got = owner_words(&opener, &Plain::default(), b"t", 100, c(), i64::MAX).unwrap();
    assert!(got.is_empty());
}
