//! Hostile markers for `restore_lock.admit` and `restore_state`: nested
//! far past the decoder's depth (RecursionError, as the reference), a
//! 4 MiB marker (only 64 KiB read), code points past U+10FFFF in UTF-32,
//! a lone surrogate in UTF-8, thousands of keys, every byte value; no
//! panic, and the time to read a marker does not grow with the file past
//! its first 64 KiB. POSIX only, as the fixture.

#![cfg(unix)]

mod common;
mod timing;

use std::fs;
use std::path::PathBuf;
use std::time::Duration;

use bettermemory_core::store::restore_lock::{
    admit, lock_path, read_marker, restore_state, Admission, AdmitError, State,
};
use common::stores::{scratch, Scratch};

const WAIT: Duration = Duration::from_millis(50);

fn store_with(marker: &[u8]) -> (Scratch, PathBuf) {
    let root = scratch("admit-robust");
    let store = root.0.join("memory.sqlite");
    fs::write(&store, b"").expect("store");
    fs::write(lock_path(&store), marker).expect("marker");
    (root, store)
}

#[test]
fn deep_nesting_is_recursion_error() {
    for open in [b'[', b'{'] {
        let bytes = if open == b'[' {
            vec![b'['; 200_000]
        } else {
            b"{\"a\":".repeat(20_000)
        };
        let (_root, store) = store_with(&bytes);
        assert!(matches!(
            admit(&store, WAIT, |_| false),
            Err(AdmitError::Recursion)
        ));
        assert!(matches!(
            restore_state(&store, WAIT, |_| false),
            Err(AdmitError::Recursion)
        ));
    }
}

#[test]
fn odd_encodings_read_as_an_empty_marker() {
    // UTF-32 LE past U+10FFFF, a lone surrogate in UTF-8, a NUL-led pair.
    let cases: [&[u8]; 4] = [
        b"{\x00\x00\x00\xff\xff\x11\x00}\x00\x00\x00",
        b"\xed\xa0\x80",
        b"\x00\x00",
        b"\xff\xfe\x00",
    ];
    for bytes in cases {
        let (_root, store) = store_with(bytes);
        let got = read_marker(&store).expect("no recursion");
        assert_eq!(got.map(|m| m.len()), Some(0), "{bytes:?}");
    }
}

#[test]
fn every_byte_and_many_keys_do_not_panic() {
    let all: Vec<u8> = (0..=255u8).cycle().take(70_000).collect();
    let (_root, store) = store_with(&all);
    assert!(admit(&store, WAIT, |_| false).is_err());
    let mut many = b"{".to_vec();
    for i in 0..3000 {
        many.extend_from_slice(format!("\"k{i}\": {i}, ").as_bytes());
    }
    many.extend_from_slice(b"\"pid\": -1}");
    let (_root, store) = store_with(&many);
    let (state, marker) = restore_state(&store, WAIT, |_| false).expect("state");
    assert_eq!(state, State::Unfinished);
    assert_eq!(marker.expect("marker").len(), 3001);
}

#[test]
fn a_huge_marker_costs_what_64_kib_does() {
    let mut small = vec![b' '; 1 << 16];
    small.extend_from_slice(b"x");
    let mut large = small.clone();
    large.resize(64 << 16, b' ');
    let (_a, small_store) = store_with(&small);
    let (_b, large_store) = store_with(&large);
    assert!(matches!(
        admit(&large_store, WAIT, |_| false),
        Ok(Admission::Shared(_))
    ));
    timing::grows_within(
        "marker read",
        3,
        &mut || timing::wall(|| read_marker(&small_store).expect("read")),
        &mut || timing::wall(|| read_marker(&large_store).expect("read")),
    );
}
