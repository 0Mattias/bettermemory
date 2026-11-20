//! The dense table behind `sre_lower` (`unicode::bmp`, built on its first
//! use) read first from eight threads at once. The table is one per
//! process, so this file is a test binary of its own with this one test:
//! nothing in the process has read the table when the threads start, and a
//! barrier releases them together, each starting at U+0080, the first code
//! point whose lower case reads it.

use std::sync::Barrier;

use bettermemory_core::pyre::fold::{sre_iscased, sre_lower, sre_upper};

const N: u32 = 0x110000;
const THREADS: usize = 8;

/// Every seventh code point from U+0080 through the three functions.
fn walk() -> u64 {
    (0x80..N)
        .step_by(7)
        .map(|c| u64::from(sre_lower(c)) + u64::from(sre_upper(c)) + u64::from(sre_iscased(c)))
        .sum()
}

#[test]
fn the_tables_read_first_from_many_threads() {
    let barrier = Barrier::new(THREADS);
    let sums: Vec<u64> = std::thread::scope(|scope| {
        let handles: Vec<_> = (0..THREADS)
            .map(|_| {
                scope.spawn(|| {
                    barrier.wait();
                    walk()
                })
            })
            .collect();
        handles.into_iter().map(|h| h.join().unwrap()).collect()
    });
    assert!(sums.iter().all(|&s| s == sums[0]), "{sums:?}");
    // A ninth walk, on a table built long before, answers the same.
    assert_eq!(sums[0], walk());
    // And the answers are the interpreter's: `_sre.unicode_tolower` of
    // U+0130 and U+212A, the first code point of `str.upper` of U+00DF.
    assert_eq!(
        (sre_lower(0x130), sre_lower(0x212A), sre_upper(0xDF)),
        (0x69, 0x6B, 0x53)
    );
}
