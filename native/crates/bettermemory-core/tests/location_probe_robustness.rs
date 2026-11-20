//! Hostile input to `location_probe`: paths of many separators and many
//! `.` parts, a long chain of links, every byte as a path: no panic, and
//! time growing linearly (the fastest of three runs at 2n against n, on
//! any of three attempts).

use std::time::{Duration, Instant};

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::location_probe::{probe, view_path, Host};

struct H;

impl Host for H {
    fn hostname(&self) -> Option<Vec<u8>> {
        Some(vec![b'h'; 1 << 16])
    }
    fn strerror(&self, _errno: &str) -> Option<String> {
        Some("text".into())
    }
}

fn world() -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/w", Node::Dir);
    fs.insert(b"/w/d", Node::Dir);
    for i in 0..30 {
        fs.insert(
            format!("/w/c{i}").as_bytes(),
            Node::Link(format!("c{}", i + 1).into_bytes()),
        );
    }
    fs.insert(b"/w/c30", Node::Link(b"d".to_vec()));
    fs
}

fn fastest(f: impl Fn()) -> Duration {
    (0..3)
        .map(|_| {
            let t = Instant::now();
            f();
            t.elapsed()
        })
        .min()
        .unwrap()
}

#[test]
fn every_byte_and_short_paths_answer_without_panic() {
    let fs = world();
    for b in 0..=255u8 {
        for path in [vec![b], vec![b'/', b], vec![b'/', b'w', b'/', b, b'/']] {
            let _ = view_path(&fs, Flavour::Posix, &path, Some(b"/w"));
            let _ = view_path(&fs, Flavour::Posix, &path, None);
            let _ = probe(&fs, &H, Flavour::Posix, &path, Some(b"/w"));
            let _ = probe(&fs, &H, Flavour::Posix, &path, None);
        }
    }
    let p = probe(&fs, &H, Flavour::Posix, b"/w/c0", None).unwrap();
    assert_eq!(p.cwd, b"/w/d");
}

fn separators(n: usize) -> Vec<u8> {
    let mut path = b"/w/d".to_vec();
    path.extend(std::iter::repeat_n(b'/', n));
    path
}

fn dots(n: usize) -> Vec<u8> {
    let mut path = b"/w".to_vec();
    for _ in 0..n {
        path.extend_from_slice(b"/.");
    }
    path
}

#[test]
fn time_grows_linearly() {
    let fs = world();
    for make in [separators as fn(usize) -> Vec<u8>, dots] {
        let n = 200_000;
        let small = make(n);
        let large = make(2 * n);
        // A loaded machine can stall one measurement; a quadratic walk
        // misses the bound on every attempt.
        let mut seen = Vec::new();
        let linear = (0..3).any(|_| {
            let t1 = fastest(|| {
                let _ = view_path(&fs, Flavour::Posix, &small, None);
            });
            let t2 = fastest(|| {
                let _ = view_path(&fs, Flavour::Posix, &large, None);
            });
            seen.push((t1, t2));
            t2 < t1 * 4 + Duration::from_millis(50)
        });
        assert!(linear, "n against 2n: {seen:?}");
    }
}
