//! Hostile bodies for the candidate scan and the comparison form: long,
//! packed with backtick spans, refused candidates and slash runs, odd code
//! points. No panic, and growth within `TENFOLD` below the scan bound.

mod timing;

use bettermemory_core::verify_candidates::scan::{
    normalize_for_compare, scan_candidates, UserHome,
};
use bettermemory_core::verify_candidates::MAX_BODY_SCAN_BYTES;

fn none(_: &[u32]) -> UserHome {
    UserHome::Unknown
}

fn repeat(piece: &str, n: usize) -> Vec<u32> {
    piece.chars().map(u32::from).cycle().take(n).collect()
}

const PIECES: [&str; 6] = [
    "`/a/b` ",
    "/a* ",
    "`",
    "/a/b Cx ",
    "x.co/q /q/r ",
    "`\\ \\ .x` ",
];

#[test]
fn hostile_bodies_do_not_panic() {
    let home: Vec<u32> = "/h".chars().map(u32::from).collect();
    for piece in PIECES {
        for n in [0, 1, 7, MAX_BODY_SCAN_BYTES + 5, 4 * MAX_BODY_SCAN_BYTES] {
            let _ = scan_candidates(&repeat(piece, n), Some(&home), &mut none);
            let _ = scan_candidates(&repeat(piece, n), None, &mut none);
        }
    }
    let odd = [0xD800, 0xDFFF, 0x10FFFF, 0x60, 0x2F, 0x7E, 0x20, 0];
    let t: Vec<u32> = odd.iter().copied().cycle().take(100_000).collect();
    let _ = scan_candidates(&t, Some(&home), &mut none);
    let _ = normalize_for_compare(&t, Some(&home), &mut none);
    let _ = normalize_for_compare(&repeat("/", 1_000_000), None, &mut none);
    let _ = normalize_for_compare(&repeat("~/", 1_000_000), Some(&home), &mut none);
}

#[test]
fn hostile_bodies_are_linear() {
    let home: Vec<u32> = "/h".chars().map(u32::from).collect();
    let n = MAX_BODY_SCAN_BYTES - 8;
    for piece in PIECES {
        let (small, large) = (repeat(piece, n / 10), repeat(piece, n));
        let f = |t: &[u32]| {
            let _ = scan_candidates(t, Some(&home), &mut none);
        };
        timing::grows_within(
            &format!("scan {piece:?}"),
            timing::TENFOLD,
            &mut || timing::once(|| f(&small)),
            &mut || timing::once(|| f(&large)),
        );
    }
    let (small, large) = (repeat("/a/.", 100_000), repeat("/a/.", 1_000_000));
    timing::grows_within(
        "normalize_for_compare",
        timing::TENFOLD,
        &mut || timing::once(|| drop(normalize_for_compare(&small, None, &mut none))),
        &mut || timing::once(|| drop(normalize_for_compare(&large, None, &mut none))),
    );
}
