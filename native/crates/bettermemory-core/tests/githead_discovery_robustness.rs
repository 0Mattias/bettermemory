//! Hostile input to `githead`'s discovery: deep trees, long and many
//! ceilings, a `.git` file far past its bound, NULs and bytes that are not
//! UTF-8. No panic, and the cost grows with the input as the reference's
//! does (a walk is one probe per folder; the ceilings are read once).

mod common;
mod timing;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::githead::discovery::{
    below, find_gitdir, gitdir_at, locate, located, parts, probed, pure, stamp, Env,
};
use common::githead_discovery::{asking, repo, ME};

fn deep(depth: usize) -> (MemFs, Vec<u8>) {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.path_max = usize::MAX;
    repo(&mut fs, "/r/.git");
    let mut path = b"/r".to_vec();
    for _ in 0..depth {
        path.extend_from_slice(b"/d");
    }
    fs.insert(&path, Node::Dir);
    (fs, path)
}

#[test]
fn a_deep_walk_finds_the_repository() {
    let (fs, start) = deep(1_000);
    asking(&fs, &Env::default(), ME, &[], |a| {
        let found = find_gitdir(a.rule, a.host, a.env, &start).unwrap();
        assert_eq!(found.worktree_root, b"/r");
        assert_eq!(
            locate(a.rule, a.env, &start, true, false)
                .unwrap()
                .worktree_root,
            b"/r"
        );
    });
}

#[test]
fn a_walk_grows_with_its_depth() {
    let run = |depth: usize| {
        let (fs, start) = deep(depth);
        timing::once(|| {
            asking(&fs, &Env::default(), ME, &[], |a| {
                probed(a.ctx, a.env, &start, Some(0)).unwrap().count()
            })
        })
    };
    // Each probe states one path of the walk's length: the walk is
    // quadratic in the depth for the reference too.
    timing::grows_within(
        "probed: 40 and 400 folders deep",
        150,
        &mut || run(40),
        &mut || run(400),
    );
}

#[test]
fn many_ceilings_are_read_once() {
    let (fs, start) = deep(50);
    let run = |n: usize| {
        let mut raw = Vec::new();
        for i in 0..n {
            raw.extend_from_slice(format!(":/nothere{i}/./x").as_bytes());
        }
        let env = Env {
            ceilings: Some(raw),
            ..Env::default()
        };
        timing::once(|| {
            asking(&fs, &env, ME, &[], |a| {
                locate(a.rule, a.env, &start, true, false).is_some()
            })
        })
    };
    timing::grows_within(
        "locate: 1,000 and 10,000 ceilings",
        timing::TENFOLD,
        &mut || run(1_000),
        &mut || run(10_000),
    );
}

#[test]
fn a_huge_gitfile_is_read_to_its_bound() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/sep");
    let mut text = b"gitdir: /sep\n".to_vec();
    text.resize(64 << 20, b'\n');
    fs.insert(b"/g/.git", Node::File(text));
    asking(&fs, &Env::default(), ME, &[], |a| {
        assert!(locate(a.rule, a.env, b"/g", true, true).is_none());
        assert!(gitdir_at(a.rule, a.host, a.env, b"/g").is_none());
    });
}

#[test]
fn hostile_bytes_never_panic() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/r/.git");
    fs.insert(b"/r/\xff\xfe", Node::Dir);
    fs.insert(b"/g/.git", Node::File(b"gitdir: /\x00x\n".to_vec()));
    fs.insert(b"/h/.git", Node::Endless);
    fs.insert(b"/l", Node::Link(b"/l".to_vec()));
    let starts: [&[u8]; 9] = [
        b"/r/\xff\xfe",
        b"/r/\x00",
        b"",
        b"//",
        b"/g",
        b"/h",
        b"/l",
        b"../../..",
        b"/r/./../r//",
    ];
    for raw in [&b""[..], b":", b"::::", b"\xff:/\x00", b"/:/r:/r/"] {
        let env = Env {
            ceilings: Some(raw.to_vec()),
            sudo_uid: Some(b"\xff".to_vec()),
            across_filesystem: Some(raw.to_vec()),
            ..Env::default()
        };
        for start in starts {
            asking(&fs, &env, 0, &["/r"], |a| {
                let _ = find_gitdir(a.rule, a.host, a.env, start);
                let _ = gitdir_at(a.rule, a.host, a.env, start);
                let _ = locate(a.rule, a.env, start, true, true);
                let _ = located(a.rule, a.env, start);
                let _ = stamp(a.ctx, start, false);
                if let Ok(walk) = probed(a.ctx, a.env, start, Some(u64::MAX)) {
                    assert!(walk.take(10_000).count() < 10_000);
                }
            });
        }
        let _ = (parts(raw), pure(raw), below(raw, raw));
    }
}
