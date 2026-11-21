//! Hostile input to `location_cache`: a HEAD past `HEAD_LIMIT`, a HEAD of
//! whitespace and of bytes that are not UTF-8, a ref name of many empty
//! components, a cwd hundreds of folders below the root (the project
//! files walk grows linearly), extreme clocks, and a cache filled far past
//! its cap. No panic, debug or release.

mod common;
mod timing;

use bettermemory_core::capture::{Capture, Kind, State};
use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::location_cache::{
    fresh, signature, Cache, CwdPart, DiscoverFailed, Entry, Signature, CACHE_CAP, HEAD_LIMIT,
};
use bettermemory_core::location_git::Git;
use bettermemory_core::unicode::cps;

fn fs_with(head: Vec<u8>, depth: usize) -> (MemFs, Vec<u8>) {
    let mut fs = MemFs::new(Flavour::Posix);
    for d in [
        &b"/r"[..],
        b"/r/.git",
        b"/r/.git/refs",
        b"/r/.git/refs/heads",
    ] {
        fs.insert(d, Node::Dir);
    }
    fs.insert(b"/r/.git/HEAD", Node::File(head));
    let mut cwd = b"/r".to_vec();
    for _ in 0..depth {
        cwd.extend_from_slice(b"/d");
        fs.insert(&cwd, Node::Dir);
    }
    (fs, cwd)
}

fn sig(fs: &MemFs, cwd: &[u8]) -> Option<Signature> {
    let ctx = Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: b"/",
    };
    let git = Git {
        dotgit: Some(b"/r/.git".to_vec()),
        git_dir: b"/r/.git".to_vec(),
        common_dir: b"/r/.git".to_vec(),
        root: Some(b"/r".to_vec()),
        kind: Kind::Primary,
    };
    let mut discover = |_: &[u8], read_head: bool| -> Result<Option<Git>, DiscoverFailed> {
        Ok((!read_head).then(|| git.clone()))
    };
    signature(&ctx, cwd, false, false, &mut discover).unwrap()
}

#[test]
fn a_long_head_is_read_to_the_limit() {
    let mut head = b"ref: refs/heads/main".to_vec();
    head.resize(HEAD_LIMIT * 4, b' ');
    let (fs, cwd) = fs_with(head, 0);
    let Some(Signature::Repo(r)) = sig(&fs, &cwd) else {
        panic!("a signature")
    };
    assert_eq!(r.head.len(), HEAD_LIMIT);
    // `_HEAD_LIMIT = 1 << 16` in the reference.
    assert_eq!(r.head.len(), 65536);
}

#[test]
fn odd_heads_answer() {
    let empty = b"ref:"
        .iter()
        .copied()
        .chain(std::iter::repeat_n(b'/', 50_000));
    let heads: Vec<Vec<u8>> = vec![
        Vec::new(),
        vec![b' '; 100_000],
        b"ref: refs/heads/\xff\xfe\xc0\x80".to_vec(),
        empty.collect(),
        [b"ref: refs/heads/".as_slice(), &[0u8; 10]].concat(),
    ];
    for head in heads {
        let (fs, cwd) = fs_with(head, 0);
        let _ = sig(&fs, &cwd);
    }
}

#[test]
fn the_project_files_walk_grows_linearly() {
    let (small_fs, small) = fs_with(b"ref: refs/heads/main\n".to_vec(), 40);
    let (large_fs, large) = fs_with(b"ref: refs/heads/main\n".to_vec(), 400);
    let Some(Signature::Repo(r)) = sig(&large_fs, &large) else {
        panic!("a signature")
    };
    assert_eq!(r.inner.len(), 401);
    timing::grows_within(
        "location_cache signature depth",
        timing::TENFOLD,
        &mut || timing::once(|| sig(&small_fs, &small)),
        &mut || timing::once(|| sig(&large_fs, &large)),
    );
}

#[test]
fn extreme_clocks_are_not_fresh() {
    for (m, w, n, nw) in [
        (f64::MAX, 0.0, f64::MIN, 0.0),
        (f64::INFINITY, 0.0, f64::INFINITY, 0.0),
        (0.0, f64::NEG_INFINITY, 0.0, f64::INFINITY),
        (f64::NAN, 0.0, 0.0, 0.0),
    ] {
        assert!(!fresh(m, w, n, nw));
    }
}

#[test]
fn a_cache_filled_far_past_its_cap_keeps_the_newest() {
    let mut cache = Cache::default();
    let sig = Signature::Outside {
        cwd: CwdPart {
            path: b"/w".to_vec(),
            id: Some((u64::MAX, u64::MAX)),
        },
        project: Some([i128::MIN; 5]),
    };
    for i in 0..CACHE_CAP * 20 {
        let entry = Entry {
            capture: Capture::new(cps("/w"), State::Repo),
            signature: sig.clone(),
            monotonic: 0.0,
            wall: 0.0,
        };
        cache.put((i.to_string().into_bytes(), i % 2 == 0, true), entry);
    }
    assert_eq!(cache.entries().len(), CACHE_CAP);
    let last = (CACHE_CAP * 20 - 1).to_string().into_bytes();
    assert_eq!(cache.entries()[CACHE_CAP - 1].0 .0, last);
}
