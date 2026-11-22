//! Hostile `shallow` files for `cuts_in`: a million one-byte records, a
//! file at the cap of LF alone, a path of many parts; no panic, and time
//! that grows linearly with the records.

mod timing;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::origin_history::{cuts_in, CUTS_MAX};

const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};

fn run(data: Vec<u8>, path: &[u8]) -> Option<usize> {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/w", Node::Dir);
    fs.insert(b"/w/shallow", Node::File(data));
    let ctx = Ctx {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/w",
    };
    cuts_in(
        &Rule {
            ctx: &ctx,
            answers: &Hung::default(),
            records: &NoRecords,
            env: None,
            git: &GIT,
        },
        path,
    )
    .unwrap()
    .map(|s| s.len())
}

#[test]
fn lf_alone_at_the_cap() {
    assert_eq!(run(vec![b'\n'; CUTS_MAX], b"shallow"), None);
}

#[test]
fn many_distinct_records() {
    let data: Vec<u8> = (0..100_000u32)
        .flat_map(|i| format!("{i:040x}\n").into_bytes())
        .collect();
    assert_eq!(run(data, b"shallow"), Some(100_000));
}

#[test]
fn a_path_of_many_parts() {
    let mut path = b"./".repeat(100_000);
    path.extend_from_slice(b"shallow");
    assert_eq!(run(b"x".to_vec(), &path), None);
}

#[test]
fn time_grows_linearly_with_the_records() {
    let make = |n: u32| -> MemFs {
        let data: Vec<u8> = (0..n)
            .flat_map(|i| format!("{i:040x}\n").into_bytes())
            .collect();
        let mut fs = MemFs::new(Flavour::Posix);
        fs.insert(b"/w", Node::Dir);
        fs.insert(b"/w/shallow", Node::File(data));
        fs
    };
    let (small, large) = (make(2_000), make(20_000));
    let ctx = |fs| Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: b"/w",
    };
    let (cs, cl) = (ctx(&small), ctx(&large));
    let hung = Hung::default();
    let rule = |ctx| Rule {
        ctx,
        answers: &hung,
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    timing::grows_within(
        "cuts_in",
        timing::TENFOLD,
        &mut || timing::once(|| cuts_in(&rule(&cs), b"shallow")),
        &mut || timing::once(|| cuts_in(&rule(&cl), b"shallow")),
    );
}
