//! Hostile transcripts for `interrupted_turns`, `owner_words` and
//! `compact_marker`: lines nested past the decoder's depth, stamps at the
//! ends of the datetime range, windows and counts at the integer bounds,
//! many turns. No panic, and time that grows with the input as the
//! reference's does.

mod common;
mod timing;

use std::num::NonZeroUsize;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::transcript::{compact_marker, interrupted_turns, owner_words, CHUNK_BYTES};
use bettermemory_core::unicode::cps;
use common::transcript::MemTranscript;
use common::transcript_owner::Plain;

fn c() -> NonZeroUsize {
    NonZeroUsize::new(CHUNK_BYTES).unwrap()
}

fn dt(text: &str) -> DateTime {
    DateTime::from_isoformat(&cps(text)).unwrap()
}

#[test]
fn nested_lines_raise_without_a_panic() {
    let mut line = b"{\"type\":\"user\",\"x\":".to_vec();
    line.extend(std::iter::repeat_n(b'[', 100_000));
    let file = MemTranscript::new(line.clone());
    let since = dt("2026-01-01T00:00:00+00:00");
    assert!(interrupted_turns(&file, &Plain::default(), b"t", &since, c(), i64::MAX).is_err());
    assert!(owner_words(&file, &Plain::default(), b"t", 10, c(), i64::MAX).is_err());
    let mut line = b"{\"subtype\":\"compact_boundary\",\"x\":".to_vec();
    line.extend(std::iter::repeat_n(b'[', 100_000));
    let file = MemTranscript::new(line);
    assert!(compact_marker(&file, b"t", &since, 0, c(), i64::MAX).is_err());
}

#[test]
fn stamps_and_windows_at_the_bounds() {
    let rows = concat!(
        r#"{"type":"system","subtype":"compact_boundary","timestamp":"0001-01-01T00:00:00+23:59","uuid":"lo"}"#,
        "\n",
        r#"{"type":"system","subtype":"compact_boundary","timestamp":"9999-12-31T23:59:59.999999-23:59","uuid":"hi"}"#,
        "\n",
    );
    let file = MemTranscript::new(rows.as_bytes().to_vec());
    for near in [
        "0001-01-01T00:00:00+00:00",
        "9999-12-31T23:59:59.999999+00:00",
    ] {
        for window in [i128::MIN, -1, 0, 1, i128::from(i64::MAX), i128::MAX] {
            let got = compact_marker(&file, b"t", &dt(near), window, c(), i64::MAX).unwrap();
            if window >= 400_000 * 365 * 86_400_000_000 {
                assert_eq!(got.unwrap().key, cps("hi"));
            } else if window < 0 {
                assert!(got.is_none());
            }
        }
    }
    let users = concat!(
        r#"{"type":"user","timestamp":"0001-01-01T00:00:00+23:59","message":{"content":"a"}}"#,
        "\n",
        r#"{"type":"user","timestamp":"9999-12-31T23:59:59.999999-23:59","message":{"content":"[Request interrupted"}}"#,
        "\n",
    );
    let file = MemTranscript::new(users.as_bytes().to_vec());
    let got = interrupted_turns(
        &file,
        &Plain::default(),
        b"t",
        &dt("0001-01-01T00:00:00-23:59"),
        c(),
        i64::MAX,
    )
    .unwrap();
    assert!(got.is_empty(), "two turns without ids, neither with both");
    for chars in [i64::MIN, -1, 0, 1, i64::MAX] {
        let got = owner_words(&file, &Plain::default(), b"t", chars, c(), i64::MAX).unwrap();
        assert_eq!(got.len(), usize::try_from(chars.max(0)).unwrap().min(23));
    }
}

fn interrupted(n: usize) -> Vec<u8> {
    let mut out = Vec::new();
    for i in 0..n {
        let ts = 1_000_000 + i;
        out.extend(
            format!(
                "{{\"type\":\"user\",\"promptId\":\"p{i}\",\"timestamp\":\"2026-01-01T00:00:00.{:06}Z\",\"message\":{{\"content\":\"w{i}\"}}}}\n\
                 {{\"type\":\"assistant\",\"message\":{{\"model\":\"m\",\"content\":[]}}}}\n\
                 {{\"type\":\"user\",\"promptId\":\"p{i}\",\"timestamp\":\"2026-01-01T00:00:01.{:06}Z\",\"message\":{{\"content\":\"[Request interrupted by user]\"}}}}\n",
                ts % 1_000_000,
                ts % 1_000_000
            )
            .bytes(),
        );
    }
    out
}

#[test]
fn many_turns_grow_linearly() {
    let run = |n: usize| {
        let file = MemTranscript::new(interrupted(n));
        let since = dt("2025-01-01T00:00:00+00:00");
        move || {
            let got =
                interrupted_turns(&file, &Plain::default(), b"t", &since, c(), i64::MAX).unwrap();
            assert_eq!(got.len(), n);
        }
    };
    let mut small = run(2_000);
    let mut large = run(20_000);
    timing::grows_within(
        "interrupted_turns over 2,000 and 20,000 turns",
        timing::TENFOLD,
        &mut || timing::once(&mut small),
        &mut || timing::once(&mut large),
    );
}

#[test]
fn owner_words_grow_linearly() {
    let run = |n: usize| {
        let file = MemTranscript::new(interrupted(n));
        move || {
            let got = owner_words(&file, &Plain::default(), b"t", i64::MAX, c(), i64::MAX);
            assert!(!got.unwrap().is_empty());
        }
    };
    let mut small = run(2_000);
    let mut large = run(20_000);
    timing::grows_within(
        "owner_words over 2,000 and 20,000 turns",
        timing::TENFOLD,
        &mut || timing::once(&mut small),
        &mut || timing::once(&mut large),
    );
}
