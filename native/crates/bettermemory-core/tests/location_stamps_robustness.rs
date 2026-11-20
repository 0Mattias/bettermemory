//! Hostile input to `location_stamps`: a mount table of many megabytes in
//! one line or in many, `\r` runs, integer bounds; no panic, and the
//! table's read grows linearly (the fastest of three runs at 8n against
//! n stays under 12 times, where a quadratic read takes 64).

mod common;

use std::time::{Duration, Instant};

use bettermemory_core::location_stamps::{self as ls, Machine, Platform};
use common::location_stamps::{stat, TableFs};

const MOUNTS: &[u8] = b"/m";

fn drvfs_of(table: Vec<u8>, device: i128) -> bool {
    let fs = TableFs {
        file: Some((MOUNTS.to_vec(), Ok(table))),
        ..TableFs::default()
    };
    let m = Machine {
        fs: &fs,
        platform: Platform::Linux,
        mountinfo: MOUNTS,
    };
    ls::drvfs(&m, device).unwrap()
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

fn one_line(n: usize) -> Vec<u8> {
    let mut t = vec![b'a'; n];
    t.extend_from_slice(b"\n36 35 0:7 / /m rw - drvfs\n");
    t
}

fn many_lines(n: usize) -> Vec<u8> {
    b"36 35 0:9 / /m rw - ext4 /dev/x rw\n"
        .iter()
        .copied()
        .cycle()
        .take(n / 35 * 35)
        .collect()
}

#[test]
fn a_long_line_is_read_in_linear_time() {
    let n = if cfg!(debug_assertions) {
        1 << 19
    } else {
        4 << 20
    };
    assert!(drvfs_of(one_line(n), 7));
    let (small, large) = (one_line(n), one_line(8 * n));
    let a = fastest(|| assert!(drvfs_of(small.clone(), 7)));
    let b = fastest(|| assert!(drvfs_of(large.clone(), 7)));
    assert!(b < a * 12 + Duration::from_millis(200), "n {a:?}, 8n {b:?}");
}

#[test]
fn many_lines_are_read_in_linear_time() {
    let n = if cfg!(debug_assertions) {
        1 << 19
    } else {
        4 << 20
    };
    let (small, large) = (many_lines(n), many_lines(8 * n));
    let a = fastest(|| assert!(!drvfs_of(small.clone(), 7)));
    let b = fastest(|| assert!(!drvfs_of(large.clone(), 7)));
    assert!(b < a * 12 + Duration::from_millis(200), "n {a:?}, 8n {b:?}");
}

#[test]
fn carriage_return_runs_and_whitespace_floods() {
    assert!(!drvfs_of(vec![b'\r'; 1 << 20], 7));
    assert!(!drvfs_of(vec![b' '; 1 << 20], 7));
    assert!(!drvfs_of(b"\xc2\x85".repeat(1 << 19), 7));
    assert!(!drvfs_of(vec![0xff; 1 << 20], 7));
    let mut fields = b"36 35 0:7 / /m rw".to_vec();
    fields.extend(std::iter::repeat_n(b" -".as_slice(), 1 << 18).flatten());
    assert!(!drvfs_of(fields, 7));
}

#[test]
fn integer_bounds_never_panic() {
    for v in [
        i128::MIN,
        i128::MIN + 1,
        -1,
        0,
        i128::MAX,
        i128::from(u64::MAX),
    ] {
        for p in [
            Platform::Linux,
            Platform::LinuxMusl,
            Platform::Darwin,
            Platform::Windows,
            Platform::Other,
        ] {
            let _ = ls::device_name(p, v);
        }
        let _ = ls::filetime_ns(v);
        let _ = ls::in_ticks(&[v, v]);
        let fs = TableFs {
            file: Some((MOUNTS.to_vec(), Ok(b"36 35 0:7 / /m rw - drvfs\n".to_vec()))),
            ..TableFs::default()
        };
        let m = Machine {
            fs: &fs,
            platform: Platform::Linux,
            mountinfo: MOUNTS,
        };
        assert_eq!(
            ls::drvfs(&m, v).ok(),
            (v == -1 || v == 0 || v == i128::from(u64::MAX)).then_some(false)
        );
        // musl's CPython has no NODEV: -1 raises as any negative does.
        let m = Machine {
            platform: Platform::LinuxMusl,
            ..m
        };
        assert_eq!(
            ls::drvfs(&m, v).ok(),
            (v == 0 || v == i128::from(u64::MAX)).then_some(false)
        );
    }
    let times = vec![100i128; 1 << 20];
    assert!(ls::in_ticks(&times));
    let fs = TableFs::default();
    let m = Machine {
        fs: &fs,
        platform: Platform::Darwin,
        mountinfo: MOUNTS,
    };
    let read: Vec<_> = (0..100_000)
        .map(|i| Some(stat(1, 7, i * 100, i * 100 + 1_000_000_000, 0)))
        .collect();
    let st = stat(1, 7, 100, 100, 0);
    assert_eq!(
        ls::capture_ticks(&m, Some(&st), None, &read, &[(i128::MAX, i128::MIN)]),
        Ok(Some(false))
    );
    let dir = vec![b'g'; 1 << 20];
    assert_eq!(
        ls::init_stamps(&m, bettermemory_core::fsdata::Flavour::Windows, &dir, true),
        Ok(Vec::new())
    );
}
