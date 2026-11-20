//! Hostile input to `membership_records` unit 1: very long paths, many
//! separators, many parts, code points at the bounds. No panic, and time
//! grows linearly (the fastest of three runs at 2n within a small
//! multiple of twice the time at n).

use bettermemory_core::fsdata::{Flavour, PyErr};
use bettermemory_core::gitfile_rule::hostpath::text::Process;
use bettermemory_core::membership_records::paths;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use std::time::{Duration, Instant};

struct Cwd;

impl Process for Cwd {
    fn getcwd(&self) -> Result<PyStr, PyErr> {
        Ok(cps("/w/cwd"))
    }
}

fn repeat(piece: &str, n: usize) -> PyStr {
    cps(&piece.repeat(n))
}

/// Everything the unit answers, over one path of size `n`.
fn exercise(n: usize, piece: &str) {
    let p = [cps("/r"), repeat(piece, n)].concat();
    let w = [cps("C:\\r"), repeat(&piece.replace('/', "\\"), n)].concat();
    for h in [Flavour::Posix, Flavour::Windows] {
        let k = paths::dir_key(h, &p);
        let _ = paths::dir_below(h, &k, &paths::dir_key(h, &cps("/r")));
        let _ = paths::common_by_pointer(h, Some(&cps("linked")), Some(&p));
    }
    let _ = paths::rebase(&p, &cps("/r"), &cps("C:\\t"), &Cwd);
    let _ = paths::rebase(&w, &cps("C:\\r"), &cps("/t"), &Cwd);
    let _ = paths::rebase(&cps("/r"), &p, &cps("/t"), &Cwd);
    let _ = paths::same_path(&p, &w);
    let _ = paths::basename(&w);
    let _ = paths::flavour(&p);
    let parts: Vec<PyStr> = (0..n / 8 + 1).map(|_| cps("c:x\\")).collect();
    let refs: Vec<&[u32]> = parts.iter().map(Vec::as_slice).collect();
    let _ = paths::path_under(&w, &refs);
}

fn fastest(n: usize, piece: &str) -> Duration {
    (0..3)
        .map(|_| {
            let t = Instant::now();
            exercise(n, piece);
            t.elapsed()
        })
        .min()
        .expect("three runs")
}

fn linear(piece: &str) {
    let n = 20_000;
    let small = fastest(n, piece);
    let large = fastest(2 * n, piece);
    assert!(
        large <= small * 6 + Duration::from_millis(50),
        "{piece:?}: {small:?} at n, {large:?} at 2n"
    );
}

#[test]
fn components_grow_linearly() {
    linear("/a");
}

#[test]
fn parent_steps_grow_linearly() {
    linear("/..");
}

#[test]
fn separators_alone_grow_linearly() {
    linear("/");
}

#[test]
fn dots_and_drives_grow_linearly() {
    linear("/.:/c:/.");
}

#[test]
fn bounds_of_the_code_space_do_not_panic() {
    for c in [0u32, 0x7f, 0xd800, 0xdfff, 0xffff, 0x10ffff] {
        let p = vec![u32::from(b'/'), c, u32::from(b'\\'), c];
        exercise(1, "/");
        for h in [Flavour::Posix, Flavour::Windows] {
            let _ = paths::dir_key(h, &p);
            let _ = paths::dir_below(h, &p, &p);
        }
        let _ = paths::rebase(&p, &p[..2], &cps("C:\\t"), &Cwd);
        let _ = paths::basename(&p);
    }
}
