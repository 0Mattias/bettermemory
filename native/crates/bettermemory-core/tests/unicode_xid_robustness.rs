//! Hostile inputs: long strings, every u32, and linear time.

mod timing;

use bettermemory_core::unicode::xid::{is_identifier, is_xid_continue, is_xid_start};

#[test]
fn every_u32_boundary_answers_without_panic() {
    for cp in [0x10FFFF, 0x110000, 0x7FFF_FFFF, u32::MAX - 1, u32::MAX] {
        let _ = (is_xid_start(cp), is_xid_continue(cp));
        assert!(!is_identifier(&[0x61, cp]) || cp <= 0x10FFFF);
    }
}

#[test]
fn a_long_identifier_and_a_late_failure() {
    let mut text = vec![0x4E00u32; 1_000_000];
    assert!(is_identifier(&text));
    text.push(0x20);
    assert!(!is_identifier(&text));
}

#[test]
fn time_grows_linearly_with_the_length() {
    // Code points across the planes, so every lookup takes the ranges.
    let make = |n: usize| -> Vec<u32> {
        (0..n)
            .map(|i| [0x4E00, 0x1D400, 0x0300, 0x0660][i % 4])
            .collect()
    };
    let (small, large) = (make(100_000), make(1_000_000));
    timing::grows_within(
        "is_identifier: 100,000 and 1,000,000 code points",
        timing::TENFOLD,
        &mut || timing::once(|| assert!(is_identifier(&small))),
        &mut || timing::once(|| assert!(is_identifier(&large))),
    );
}
