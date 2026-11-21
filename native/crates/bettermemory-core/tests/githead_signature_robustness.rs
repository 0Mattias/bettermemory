//! Hostile input to `entry_above` and `signature`: deep trees, a ref and
//! a HEAD far past their bound, a chain of links, NULs and bytes that are
//! not UTF-8. No panic, and the cost grows with the input as the
//! reference's does (one probe per folder of the walk; a ref read to its
//! bound and no further).

mod common;
mod timing;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::githead::discovery::Env;
use bettermemory_core::githead::signature::{entry_above, signature, First};
use common::githead_discovery::{asking, repo, ME};

fn deep(depth: usize, git: bool) -> (MemFs, Vec<u8>) {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.path_max = usize::MAX;
    if git {
        repo(&mut fs, "/r/.git");
    }
    let mut path = b"/r".to_vec();
    for _ in 0..depth {
        path.extend_from_slice(b"/d");
    }
    fs.insert(&path, Node::Dir);
    (fs, path)
}

#[test]
fn a_deep_walk_answers() {
    for git in [true, false] {
        let (fs, start) = deep(1_000, git);
        asking(&fs, &Env::default(), ME, &[], |a| {
            assert_eq!(entry_above(a.ctx, a.env, &start), Some(git));
            let s = signature(a.rule, a.host, a.env, &start, true).unwrap();
            assert_eq!(s.first == First::Outside, !git);
        });
    }
}

#[test]
fn entry_above_grows_with_its_depth() {
    let run = |depth: usize| {
        let (fs, start) = deep(depth, false);
        timing::once(|| {
            asking(&fs, &Env::default(), ME, &[], |a| {
                entry_above(a.ctx, a.env, &start)
            })
        })
    };
    // Each probe states paths of the walk's length: quadratic in the
    // depth for the reference too.
    timing::grows_within(
        "entry_above: 40 and 400 folders deep",
        150,
        &mut || run(40),
        &mut || run(400),
    );
}

#[test]
fn huge_refs_are_read_to_their_bound() {
    let big = |n: usize| {
        let mut text = b"1111111111111111111111111111111111111111\n".to_vec();
        text.resize(n, b' ');
        text
    };
    let run = |n: usize| {
        let mut fs = MemFs::new(Flavour::Posix);
        repo(&mut fs, "/r/.git");
        fs.insert(b"/r/.git/refs/heads/main", Node::File(big(n)));
        timing::once(|| {
            asking(&fs, &Env::default(), ME, &[], |a| {
                let s = signature(a.rule, a.host, a.env, b"/r", true).unwrap();
                assert!(matches!(s.first, First::Undetermined(_)));
            })
        })
    };
    // Past the bound the read stops at 64 KiB + 1: the size of the file
    // costs nothing more.
    timing::grows_within(
        "signature: a ref of 1 MiB and 10 MiB",
        3,
        &mut || run(1 << 20),
        &mut || run(10 << 20),
    );
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/r/.git");
    fs.insert(b"/r/.git/HEAD", Node::File(big(64 << 20)));
    asking(&fs, &Env::default(), ME, &[], |a| {
        let s = signature(a.rule, a.host, a.env, b"/r", true).unwrap();
        assert!(matches!(s.first, First::Undetermined(_)));
    });
}

#[test]
fn hostile_names_and_links_do_not_panic() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/r/.git");
    fs.insert(
        b"/r/.git/HEAD",
        Node::File(b"ref: refs/heads/\xff\xfe\n".to_vec()),
    );
    fs.insert(b"/loop/a", Node::Link(b"/loop/b".to_vec()));
    fs.insert(b"/loop/b", Node::Link(b"/loop/a".to_vec()));
    fs.insert(
        b"/r/.git/refs/heads/l",
        Node::Link(b"/r/.git/refs/heads/l".to_vec()),
    );
    let ceilings = Env {
        ceilings: Some(b"/r\x00x::".to_vec()),
        ..Env::default()
    };
    for start in [
        &b"/r"[..],
        b"/r\x00x",
        b"/loop/a",
        b"/loop/a/x",
        b"",
        b"/\xff/\xfe",
        b"//",
        b"/r/.git/refs/heads/l",
    ] {
        for env in [&Env::default(), &ceilings] {
            asking(&fs, env, ME, &[], |a| {
                let _ = entry_above(a.ctx, a.env, start);
                for owner in [true, false] {
                    let _ = signature(a.rule, a.host, a.env, start, owner).unwrap();
                }
            });
        }
    }
    // A NUL in the start is a ValueError: True, as any OSError.
    asking(&fs, &Env::default(), ME, &[], |a| {
        assert_eq!(entry_above(a.ctx, a.env, b"/r\x00x"), Some(true));
    });
}
