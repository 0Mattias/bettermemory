//! `gitsystem::answers` on hostile input: random bytes never panic, under
//! either flavour, and each parser's cost is linear. The cost check runs
//! each input at 2,000 units and at 20,000, takes the best of five runs
//! of each, and asks the large run to stay under 60 times the small one
//! plus 50 ms (a quadratic parser takes about 100 times as long).

mod timing;

use bettermemory_core::fsdata::Flavour;
use bettermemory_core::gitsystem::answers::{
    absolute, older_system, own_files, parse_version, reads_remotes, text, variables,
};

fn all_on(bytes: &[u8]) {
    for fl in [Flavour::Posix, Flavour::Windows] {
        let _ = own_files(fl, bytes);
        let _ = older_system(fl, 0, bytes);
        let _ = absolute(fl, &text(bytes));
    }
    let t = text(bytes);
    let _ = parse_version(&t);
    let _ = variables(&t);
    let _ = reads_remotes(Some((0, bytes)), Some((0, bytes)), Some(0), true);
}

#[test]
fn random_bytes_never_panic() {
    let pieces: [&[u8]; 14] = [
        b"\0",
        b"system",
        b"unknown",
        b"file:",
        b"/",
        b"\\",
        b"C:",
        b"\\\\?\\UNC\\",
        b"git version ",
        b"2.",
        b"\n",
        b"\r",
        b"bettermemory.prefix\n",
        b"/z",
    ];
    let mut seed: u64 = 0x2545_F491_4F6C_DD1D;
    let mut next = || {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        seed
    };
    for _ in 0..5000 {
        let n = (next() % 30) as usize;
        let mut data = Vec::new();
        for _ in 0..n {
            let r = next();
            if r % 3 == 0 {
                data.push((r >> 8) as u8);
            } else {
                data.extend_from_slice(pieces[(r >> 8) as usize % pieces.len()]);
            }
        }
        all_on(&data);
    }
}

fn linear(name: &str, make: &dyn Fn(usize) -> Vec<u8>, run: &dyn Fn(&[u8])) {
    let small = make(2_000);
    let large = make(20_000);
    timing::grows_within(
        &format!("{name}: 2,000 and 20,000"),
        timing::TENFOLD,
        &mut || timing::once(|| run(&small)),
        &mut || timing::once(|| run(&large)),
    );
}

#[test]
fn every_parser_is_linear() {
    let distinct_files = |n: usize| {
        let mut v = Vec::new();
        for i in 0..n {
            v.extend_from_slice(format!("system\0file:C:\\{i}\0k\nv\0").as_bytes());
        }
        v
    };
    for fl in [Flavour::Posix, Flavour::Windows] {
        linear("own_files", &distinct_files, &|b| {
            let _ = own_files(fl, b);
        });
        linear(
            "older_system",
            &|n| [b"file:".as_slice(), &vec![b'\\'; n]].concat(),
            &|b| {
                let _ = older_system(fl, 0, b);
            },
        );
        linear(
            "absolute",
            &|n| [b"\\\\".as_slice(), &vec![b'a'; n]].concat(),
            &|b| {
                let _ = absolute(fl, &text(b));
            },
        );
    }
    let distinct_names = |n: usize| {
        let mut v = Vec::new();
        for i in 0..n {
            v.extend_from_slice(format!("n{i}\nv\0").as_bytes());
        }
        v
    };
    linear("variables", &distinct_names, &|b| {
        let _ = variables(&text(b));
    });
    linear("reads_remotes", &distinct_names, &|b| {
        let _ = reads_remotes(Some((0, b)), Some((0, b)), Some(0), true);
    });
    linear(
        "parse_version",
        &|n| [vec![b' '; n], b"git version 2.3.4".to_vec(), vec![b' '; n]].concat(),
        &|b| {
            let _ = parse_version(&text(b));
        },
    );
}

/// A Windows-flavour path past 32,767 characters, which the fixtures
/// leave out (Windows' C `splitroot` may refuse it): the port reads it as
/// the pure-Python `ntpath` does, not compared with a Windows host.
#[test]
fn a_windows_path_past_32767_characters() {
    let mut long = b"C:\\".to_vec();
    long.extend(std::iter::repeat_n(b'a', 1 << 20));
    assert!(absolute(Flavour::Windows, &text(&long)));
    let listing = [b"file:".as_slice(), &long].concat();
    assert_eq!(older_system(Flavour::Windows, 0, &listing), Ok(Vec::new()));
    let unc = [b"\\\\s\\h\\".as_slice(), &vec![b'a'; 1 << 20]].concat();
    assert!(absolute(Flavour::Windows, &text(&unc)));
}
