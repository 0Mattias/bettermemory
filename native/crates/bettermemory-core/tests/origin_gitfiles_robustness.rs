//! Hostile input for the `origin_gitfiles` decoders: 10 MB texts, long
//! escape runs, counts at their bounds and indexes that claim more than
//! they hold. Each answers without a panic, in debug and release, within
//! a bound set well above the reference's time on the same input.

use std::time::{Duration, Instant};

use bettermemory_core::origin_gitfiles::{
    decode_varint, dequote_named_header, dequote_patch_headers, gitmodules_entries, hunk_lines,
    quoted_header_path, shortening_refs, unquote_c_path, IndexFault, Raised,
};
use bettermemory_core::unicode::cps;

const TEN_MB: usize = 10 << 20;

/// The bound for one call. The reference (CPython 3.13.13, Apple
/// M-series) takes 1.1 s on the 10 MB header stream and 3.5 s to 7.7 s on
/// each 10 MB `diff --git` line; a release build stays under its time. A
/// debug build, run beside the crate's other tests, is held only to a
/// loose bound against a hang.
fn bound() -> Duration {
    Duration::from_secs(if cfg!(debug_assertions) { 120 } else { 4 })
}

fn timed<T>(f: impl FnOnce() -> T) -> T {
    let start = Instant::now();
    let out = f();
    assert!(start.elapsed() < bound(), "took {:?}", start.elapsed());
    out
}

#[test]
fn ten_megabyte_quoted_paths_answer() {
    let mut text = vec![u32::from(b'"')];
    text.extend(cps(&"\\303\\251".repeat(TEN_MB / 8)));
    text.push(u32::from(b'"'));
    let out = timed(|| unquote_c_path(&text)).unwrap().unwrap();
    assert_eq!(out.len(), TEN_MB / 8);
    let mut text = vec![u32::from(b'"')];
    text.extend(std::iter::repeat_n(0x1F600, TEN_MB / 4));
    text.push(u32::from(b'"'));
    assert!(timed(|| quoted_header_path(&text)).unwrap().is_some());
    let mut text = vec![u32::from(b'"')];
    text.extend(cps(&"\\".repeat(TEN_MB - 1)));
    text.push(u32::from(b'"'));
    assert_eq!(timed(|| unquote_c_path(&text)), Ok(None));
    let mut text = vec![u32::from(b'"')];
    text.extend(std::iter::repeat_n(0xDFFF, TEN_MB));
    text.push(u32::from(b'"'));
    assert_eq!(
        timed(|| unquote_c_path(&text)),
        Err(Raised::UnicodeEncodeError)
    );
}

#[test]
fn diff_git_lines_of_many_splits_answer() {
    for unit in ["a\" \"", "\\\" \"", "\" \"", "a/\\\" \"b/"] {
        let line = cps(&format!("diff --git \"{}x\"", unit.repeat(TEN_MB / 64)));
        assert_eq!(timed(|| dequote_named_header(&line)), Ok(None));
    }
    let half = "\\\"".repeat(TEN_MB / 16);
    let line = cps(&format!("diff --git \"a/{half}\" \"b/{half}\""));
    assert!(timed(|| dequote_named_header(&line)).unwrap().is_some());
}

#[test]
fn ten_megabyte_streams_answer() {
    let stream = cps(&"--- \"a/\\303\\251\"\n+++ \"b/x\"\n".repeat(TEN_MB / 30));
    let out = timed(|| dequote_patch_headers(&stream)).unwrap();
    assert!(out.len() < stream.len());
    let stream = cps(&format!(
        "@@ -1,{} +1 @@\n{}--- \"a/x\"\n",
        "9".repeat(4300),
        "-x\n".repeat(TEN_MB / 3)
    ));
    let out = timed(|| dequote_patch_headers(&stream)).unwrap();
    assert_eq!(out, stream);
    let stream = cps(&format!(
        "@@ -1,{} +1 @@\n--- \"a/x\"\n",
        "9".repeat(TEN_MB)
    ));
    assert_eq!(
        timed(|| dequote_patch_headers(&stream)),
        Err(Raised::ValueError)
    );
    let stream = cps(&"\n".repeat(TEN_MB));
    assert_eq!(timed(|| dequote_patch_headers(&stream)).unwrap(), stream);
}

#[test]
fn hunk_counts_at_their_bounds() {
    let nines = "9".repeat(4300);
    let line = cps(&format!("@@ -1,{nines} +1,{nines} @@"));
    let sum = hunk_lines(&line).unwrap().unwrap();
    assert_eq!(sum.len(), 4301);
    assert!(sum.starts_with('1') && sum.ends_with('8'));
    let line = cps(&format!("@@ -{} +1 @@", "0".repeat(TEN_MB)));
    assert_eq!(timed(|| hunk_lines(&line)), Ok(Some("2".into())));
    let line = cps(&format!("@@ -1,{} +1 @@", "0".repeat(4301)));
    assert_eq!(hunk_lines(&line), Err(Raised::ValueError));
}

#[test]
fn varints_at_their_bounds() {
    let run = vec![0x80u8; TEN_MB];
    assert_eq!(decode_varint(&run, 0, false), Err(IndexFault::Unsettled));
    assert_eq!(decode_varint(&run, TEN_MB, false), Err(IndexFault::Short));
    assert_eq!(
        decode_varint(&run, usize::MAX, true),
        Err(IndexFault::Unsettled)
    );
    let max = [0xFE, 0xFE, 0xFE, 0xFE, 0xFE, 0xFE, 0xFE, 0x7F];
    assert!(decode_varint(&max, 0, true).is_ok());
}

#[test]
fn indexes_that_claim_more_than_they_hold() {
    let mut data = b"DIRC\0\0\0\x04\xff\xff\xff\xff".to_vec();
    assert_eq!(gitmodules_entries(&data, false), Err(IndexFault::Short));
    // Many version 4 entries, each stripping nothing and adding one byte
    // past a long shared name.
    let mut first = vec![0u8; 62];
    first[60] = 0x0F;
    first[61] = 0xFF;
    first.push(0);
    first.extend(b".a".repeat(1000));
    first.push(0);
    data.extend(&first);
    for _ in 0..5_000 {
        let mut e = vec![0u8; 60];
        e.extend([0x0F, 0xFF, 0x00, b'b', 0]);
        data.extend(e);
    }
    let out = timed(|| gitmodules_entries(&data, false));
    assert_eq!(out, Err(IndexFault::Short));
    let mut big = b"DIRC\0\0\0\x02\0\0\0\x01".to_vec();
    big.extend(vec![0u8; 60]);
    big.extend([0x0F, 0xFF]);
    big.extend(vec![b'z'; TEN_MB]);
    assert_eq!(
        timed(|| gitmodules_entries(&big, true)),
        Err(IndexFault::Unsettled)
    );
    big.push(0);
    assert_eq!(timed(|| gitmodules_entries(&big, true)), Ok(vec![]));
}

#[test]
fn ten_megabyte_refs_answer() {
    let name = cps(&format!("refs/remotes/{}/HEAD", "x".repeat(TEN_MB)));
    let out = timed(|| shortening_refs(&name));
    assert_eq!(out.len(), 15);
}
