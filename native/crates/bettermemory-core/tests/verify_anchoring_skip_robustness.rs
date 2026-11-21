//! Hostile input for `verify_anchoring::skip`: a path thousands of
//! components deep below a missing folder, NUL and non-UTF-8 bytes, a
//! huge config, and an index of 200,000 entries read in time linear in
//! its size. No panic in debug or release.

mod common;
mod timing;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::githead::discovery::Env;
use bettermemory_core::githead::GitDir;
use bettermemory_core::verify_anchoring::skip::{
    absent_by_sparsity, skip_index, sparse_enabled, SkipMemo,
};
use common::verify_anchoring_skip::{git, Moving};

fn index(n: usize) -> Vec<u8> {
    let mut out = b"DIRC".to_vec();
    out.extend_from_slice(&3u32.to_be_bytes());
    out.extend_from_slice(&u32::try_from(n).unwrap().to_be_bytes());
    for i in 0..n {
        let name = format!("d/{i:08}");
        let mut e = vec![0u8; 40];
        e.extend_from_slice(&[0xAB; 20]);
        e.extend_from_slice(&(u16::try_from(name.len()).unwrap() | 0x4000).to_be_bytes());
        e.extend_from_slice(&0x4000u16.to_be_bytes());
        e.extend_from_slice(name.as_bytes());
        let pad = 8 - e.len() % 8;
        e.extend(std::iter::repeat_n(0, pad));
        out.extend(e);
    }
    out.extend_from_slice(&[0xEE; 20]);
    out
}

fn world(config: Vec<u8>, idx: Vec<u8>) -> Moving {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(
        b"/w/r/.git/HEAD",
        Node::File(b"ref: refs/heads/main\n".to_vec()),
    );
    fs.insert(b"/w/r/.git/objects", Node::Dir);
    fs.insert(b"/w/r/.git/refs", Node::Dir);
    fs.insert(b"/w/r/.git/config", Node::File(config));
    fs.insert(b"/w/r/.git/index", Node::File(idx));
    Moving::new(fs)
}

fn with<T>(fs: &Moving, f: impl FnOnce(&Rule<'_, Moving, Hung, NoRecords>) -> T) -> T {
    let ctx = Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: b"/",
    };
    let git = git();
    let hung = Hung::default();
    let rule = Rule {
        ctx: &ctx,
        answers: &hung,
        records: &NoRecords,
        env: None,
        git: &git,
    };
    f(&rule)
}

fn same(p: &[u8]) -> Vec<u8> {
    p.to_vec()
}

const GD: fn() -> GitDir = || GitDir {
    gitdir: b"/w/r/.git".to_vec(),
    commondir: b"/w/r/.git".to_vec(),
};

#[test]
fn deep_and_odd_paths_answer() {
    let fs = world(b"[core]\n\tsparseCheckout = true\n".to_vec(), index(3));
    let mut memo = SkipMemo::new();
    with(&fs, |rule| {
        let mut deep = b"/w/r/d".to_vec();
        for _ in 0..3000 {
            deep.extend_from_slice(b"/x");
        }
        for path in [
            deep.as_slice(),
            b"/w/r/d/\0/x",
            b"/w/r/\xff\xfe/\x80",
            b"",
            b"/",
            b"////",
            b"/w/r/d/00000001/",
        ] {
            // The reference answers False for each: '' and a trailing
            // slash end the walk on an empty name, '/' and '////' find
            // no repository, and the rest are held by no index entry
            // (a NUL fails `isdir`, so the walk goes up past it).
            assert_eq!(
                absent_by_sparsity(rule, &Env::default(), &same, &mut memo, path),
                Ok(false),
                "{:?}",
                String::from_utf8_lossy(&path[path.len().saturating_sub(20)..])
            );
        }
        assert_eq!(
            absent_by_sparsity(rule, &Env::default(), &same, &mut memo, b"/w/r/d"),
            Ok(true)
        );
        assert_eq!(
            absent_by_sparsity(rule, &Env::default(), &same, &mut memo, b"/w/r/d/00000001"),
            Ok(true)
        );
    });
}

#[test]
fn a_huge_config_answers() {
    let mut config = b"[core]\n".to_vec();
    for _ in 0..100_000 {
        config.extend_from_slice(b"\tsparseCheckout = \xff\r");
    }
    config.extend_from_slice(b"\tsparseCheckout = true\n");
    let fs = world(config, index(1));
    with(&fs, |rule| assert!(sparse_enabled(rule.ctx, &GD())));
}

#[test]
fn an_index_is_read_in_linear_time() {
    let small = world(Vec::new(), index(20_000));
    let large = world(Vec::new(), index(200_000));
    let read = |fs: &Moving| {
        with(fs, |rule| {
            let mut memo = SkipMemo::new();
            skip_index(rule, &GD(), &mut memo)
                .unwrap()
                .map(|i| i.skipped.len())
        })
    };
    assert_eq!(read(&large), Some(200_000));
    timing::grows_within(
        "20k and 200k entries",
        timing::TENFOLD,
        &mut || timing::once(|| read(&small)),
        &mut || timing::once(|| read(&large)),
    );
}

/// The walk up from an absent path splits off one component a step, and
/// each split copies what is left, as `posixpath.split` does in the
/// reference: quadratic in the depth on both sides (the reference took
/// 0.09 s at 5,000 components and 2.8 s at 20,000 on a real file
/// system). Four times the depth stays within sixteen times the time,
/// with room for the load: nothing worse than the reference's growth.
#[test]
fn a_deep_absent_path_walks_up_in_quadratic_time() {
    let fs = world(b"[core]\n\tsparseCheckout = true\n".to_vec(), index(3));
    let deep = |n: usize| {
        let mut p = b"/w/r/d".to_vec();
        for _ in 0..n {
            p.extend_from_slice(b"/x");
        }
        p
    };
    let (small, large) = (deep(5_000), deep(20_000));
    let walk = |p: &[u8]| {
        with(&fs, |rule| {
            let mut memo = SkipMemo::new();
            absent_by_sparsity(rule, &Env::default(), &same, &mut memo, p)
        })
    };
    assert_eq!(walk(&large), Ok(false));
    timing::grows_within(
        "5,000 and 20,000 components",
        32,
        &mut || timing::once(|| walk(&small)),
        &mut || timing::once(|| walk(&large)),
    );
    eprintln!(
        "walk: 5,000 components {:?}, 20,000 components {:?}",
        timing::once(|| walk(&small)),
        timing::once(|| walk(&large))
    );
}

/// A NUL fails `isdir`, so the walk goes up past it to the repository,
/// and a sparse directory entry `d/` holds what lies below it: the
/// reference answers True for both paths and False below a kept file.
#[test]
fn a_nul_below_a_sparse_directory_reads_left_out() {
    let mut idx = b"DIRC".to_vec();
    idx.extend_from_slice(&3u32.to_be_bytes());
    idx.extend_from_slice(&2u32.to_be_bytes());
    for (name, skip) in [(&b"d/"[..], true), (&b"k"[..], false)] {
        let mut e = vec![0u8; 40];
        e.extend_from_slice(&[0xAB; 20]);
        let len = u16::try_from(name.len()).unwrap();
        if skip {
            e.extend_from_slice(&(len | 0x4000).to_be_bytes());
            e.extend_from_slice(&0x4000u16.to_be_bytes());
        } else {
            e.extend_from_slice(&len.to_be_bytes());
        }
        e.extend_from_slice(name);
        let pad = 8 - e.len() % 8;
        e.extend(std::iter::repeat_n(0, pad));
        idx.extend(e);
    }
    idx.extend_from_slice(&[0xEE; 20]);
    let fs = world(b"[core]\n\tsparseCheckout = true\n".to_vec(), idx);
    let mut memo = SkipMemo::new();
    with(&fs, |rule| {
        for (path, want) in [
            (&b"/w/r/d/\0q"[..], true),
            (&b"/w/r/d/q/\0"[..], true),
            (&b"/w/r/k/\0"[..], false),
        ] {
            assert_eq!(
                absent_by_sparsity(rule, &Env::default(), &same, &mut memo, path),
                Ok(want),
                "{:?}",
                String::from_utf8_lossy(path)
            );
        }
    });
}
