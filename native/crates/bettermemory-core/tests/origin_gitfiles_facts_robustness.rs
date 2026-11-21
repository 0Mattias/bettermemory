//! Hostile input for `origin_gitfiles::facts`: very long texts and lines,
//! many lines, deep missing paths, integer bounds. No panic, and time grows
//! linearly with the input.

mod timing;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::origin_gitfiles::facts::{
    facts_of, hold_directory, merge_word_dies, rev_parse_dies, Held,
};

const NONE: GitFacts = GitFacts {
    release: None,
    reads_compat: None,
};

fn lines(n: usize) -> Vec<u8> {
    b"[core]\n"
        .iter()
        .copied()
        .chain(b"\t bare \t= \t0 \t\r\n".repeat(n))
        .collect()
}

#[test]
fn facts_of_grows_linearly_in_lines() {
    let small = lines(20_000);
    let large = lines(200_000);
    grows(&small, &large);
}

fn grows(small: &[u8], large: &[u8]) {
    timing::grows_within(
        "facts_of",
        30,
        &mut || timing::once(|| facts_of(small, &NONE)),
        &mut || timing::once(|| facts_of(large, &NONE)),
    );
}

#[test]
fn facts_of_grows_linearly_in_one_line() {
    let line = |n: usize| [b"bare".repeat(n), b"=false".to_vec()].concat();
    let (small, large) = (line(50_000), line(500_000));
    grows(&small, &large);
    let blanks = |n: usize| {
        [
            b" ".repeat(n),
            b"bare".to_vec(),
            b" ".repeat(n),
            b"=x".to_vec(),
        ]
        .concat()
    };
    let (small, large) = (blanks(50_000), blanks(500_000));
    grows(&small, &large);
}

#[test]
fn integer_bounds_and_long_values_do_not_panic() {
    for name in [
        &b"core.bigfilethreshold"[..],
        b"core.repositoryformatversion",
        b"core.filemode",
    ] {
        for v in [
            &b"9223372036854775807"[..],
            b"-9223372036854775808",
            b"99999999999999999999999999999999",
            b"0x7fffffffffffffffffff",
            b"-",
            b"",
        ] {
            rev_parse_dies(name, Some(v), &NONE);
        }
        rev_parse_dies(name, Some(&b"9".repeat(1 << 20)), &NONE);
    }
    // Past CPython's 4300 digits the reference's int() raises ValueError;
    // the port reads the number as out of range: git dies (README).
    let long = b"9".repeat(5000);
    assert!(rev_parse_dies(
        b"core.repositoryformatversion",
        Some(&long),
        &NONE
    ));
    let mut text = b"[core]\nrepositoryformatversion=".to_vec();
    text.extend_from_slice(&long);
    text.push(b'\n');
    assert!(facts_of(&text, &NONE).git_dies);
    let max = GitFacts {
        release: Some((u32::MAX, u32::MAX)),
        reads_compat: None,
    };
    assert!(!merge_word_dies(Some(b"simple"), &max));
    assert!(merge_word_dies(Some(&b"x".repeat(1 << 20)), &max));
}

#[test]
fn a_deep_missing_path_walks_up_in_linear_time() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/w", Node::Dir);
    let deep = |n: usize| [b"/w".to_vec(), b"/a".repeat(n)].concat();
    let (small, large) = (deep(40), deep(400));
    let mut held = Held::new();
    // Past the model's 1,024 bytes the stat raises ENAMETOOLONG, which is
    // no absence: it propagates, as from the reference's `os.stat`.
    assert!(hold_directory(&fs, Flavour::Posix, &deep(1_000), &mut held).is_err());
    hold_directory(&fs, Flavour::Posix, &large, &mut held).unwrap();
    assert_eq!(held.keys().next().map(Vec::as_slice), Some(&b"/w"[..]));
    // Each step stats a path as long as the input: quadratic in bytes,
    // as the reference's walk is.
    timing::grows_within(
        "hold_directory",
        400,
        &mut || timing::once(|| hold_directory(&fs, Flavour::Posix, &small, &mut Held::new())),
        &mut || timing::once(|| hold_directory(&fs, Flavour::Posix, &large, &mut Held::new())),
    );
}
