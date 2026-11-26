//! `verify_candidates::keys` over hostile input, in memory (`MemFs`):
//!
//! - a path 4,096 components deep: with the reference's room it raises
//!   `RecursionError` before the disk is asked; with room enough it is
//!   resolved by a loop, one `lstat` per component, and so is
//!   `claim_key`'s `realpath`;
//! - `claim_key` of a path 40,000 components deep on the disk (`StdFs`):
//!   within ten times the reference's time in release;
//! - link loops and a chain of links: every function ends;
//! - 100,000 paths in one read sharing 100 folders: one `lstat` per new
//!   component, counted, and ten times the paths within `TENFOLD` of a
//!   tenth of the time;
//! - names holding every byte (each as its code point and as its escaped
//!   surrogate) and every code point through `swapcase`: no panic.

#![cfg(unix)]

mod timing;
mod verify_candidates_keys_fs;

use std::time::Duration;

use bettermemory_core::fsdata::{Flavour, MemFs, Node, StdFs};
use bettermemory_core::verify_candidates::keys::{
    claim_key, home_ignores_case, is_multi_segment_routelike, is_under_home, path_exists, swapcase,
    ClaimKeys, Raised, RECURSION_LIMIT,
};
use bettermemory_core::verify_candidates::scan::UserHome;
use verify_candidates_keys_fs::{cps, Traced};

fn unknown(_: &[u32]) -> UserHome {
    UserHome::Unknown
}

#[test]
fn a_path_four_thousand_components_deep() {
    let fs = MemFs::new(Flavour::Posix);
    let traced = Traced::new(&fs);
    let deep = [cps("/q"), cps("/a").repeat(4095)].concat();
    assert_eq!(
        ClaimKeys::new(RECURSION_LIMIT).key(&traced, &deep, None, &mut unknown),
        Err(Raised::Recursion)
    );
    assert!(traced.take().is_empty());
    let mut read = ClaimKeys::new(usize::MAX);
    assert_eq!(
        read.key(&traced, &deep, None, &mut unknown),
        Ok(deep.clone())
    );
    assert_eq!(traced.take().len(), 4096);
    assert_eq!(
        claim_key(&traced, &deep, None, &mut unknown),
        Ok(deep.clone())
    );
    assert_eq!(traced.take().len(), 4096);
    let mut dotted = cps("/q");
    for _ in 0..1024 {
        dotted.extend(cps("/a/./b/.."));
    }
    assert_eq!(
        ClaimKeys::new(usize::MAX).key(&fs, &dotted, None, &mut unknown),
        Ok([cps("/q"), cps("/a").repeat(1024)].concat())
    );
    let home = cps("/h");
    assert_eq!(
        is_under_home(&fs, &deep, Some(&home), &mut unknown),
        Ok(false)
    );
    assert_eq!(
        is_multi_segment_routelike(&fs, &deep, Some(&home), &mut unknown),
        Ok(true)
    );
    assert_eq!(
        path_exists(&fs, &deep, Some(&home), &mut unknown),
        Ok(false)
    );
}

/// `realpath` makes one `lstat` per component on a prefix that grows, so
/// it is quadratic in the components, as `posixpath.realpath` is; each
/// name is encoded once, not with every prefix. The root does not exist,
/// so each `lstat` fails and nothing is resolved. The reference (CPython
/// 3.13.13, Apple M-series) takes 0.14 s on this path; a release build is
/// held to ten times that. A debug build, run beside the crate's other
/// tests, is held only to a loose bound against a hang.
#[test]
fn a_path_forty_thousand_components_deep_on_the_disk() {
    let deep = [cps("/nonexistent-claim-keys"), cps("/a").repeat(40_000)].concat();
    assert_eq!(
        claim_key(&StdFs, &deep, None, &mut unknown),
        Ok(deep.clone())
    );
    let bound = Duration::from_millis(if cfg!(debug_assertions) {
        60_000
    } else {
        1_400
    });
    timing::ends_within(
        "claim_key of a path 40,000 components deep",
        bound,
        &mut || timing::wall(|| claim_key(&StdFs, &deep, None, &mut unknown)),
    );
}

#[test]
fn link_loops_and_chains_end() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/m/self", Node::Link(b"self".to_vec()));
    fs.insert(b"/m/a", Node::Link(b"b/x/../y".to_vec()));
    fs.insert(b"/m/b", Node::Link(b"/m/a/..".to_vec()));
    for i in 0..200 {
        fs.insert(
            format!("/m/c{i}").as_bytes(),
            Node::Link(format!("c{}", i + 1).into_bytes()),
        );
    }
    fs.insert(b"/m/c200", Node::Dir);
    let home = cps("/m/self");
    for p in [
        "/m/self/x/../y",
        "/m/a/b/a",
        "/m/b/../a/self",
        "/m/c0/z",
        "/m/c0/..",
    ] {
        let p = cps(p);
        let mut read = ClaimKeys::new(RECURSION_LIMIT);
        read.key(&fs, &p, None, &mut unknown).unwrap();
        read.key(&fs, &p, None, &mut unknown).unwrap();
        claim_key(&fs, &p, None, &mut unknown).unwrap();
        is_under_home(&fs, &p, Some(&home), &mut unknown).unwrap();
        is_multi_segment_routelike(&fs, &p, Some(&home), &mut unknown).unwrap();
        path_exists(&fs, &p, Some(&home), &mut unknown).unwrap();
    }
    assert_eq!(
        ClaimKeys::new(RECURSION_LIMIT)
            .key(&fs, &cps("/m/c0"), None, &mut unknown)
            .unwrap(),
        cps("/m/c200")
    );
    assert_eq!(home_ignores_case(&fs, &home), Ok(false));
}

fn shared_read(fs: &MemFs, n: usize) -> usize {
    let traced = Traced::new(fs);
    let mut read = ClaimKeys::new(RECURSION_LIMIT);
    for i in 0..n {
        let p = cps(&format!("/m/d{}/f{i}", i % 100));
        read.key(&traced, &p, None, &mut unknown).unwrap();
    }
    let n = traced.calls.borrow().len();
    n
}

#[test]
fn a_read_of_paths_sharing_folders_asks_each_component_once() {
    let mut fs = MemFs::new(Flavour::Posix);
    for d in 0..100 {
        fs.insert(format!("/m/d{d}/f{d}").as_bytes(), Node::File(Vec::new()));
    }
    // `/m`, 100 folders, and each path's own last component.
    assert_eq!(shared_read(&fs, 100_000), 1 + 100 + 100_000);
    timing::grows_within(
        "100,000 claimed paths in one read",
        timing::TENFOLD,
        &mut || timing::once(|| shared_read(&fs, 10_000)),
        &mut || timing::once(|| shared_read(&fs, 100_000)),
    );
}

#[test]
fn names_with_every_byte() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/m/d", Node::Dir);
    let home = cps("/m/H\u{df}");
    let mut users = |name: &[u32]| {
        if name.len() == 1 {
            UserHome::Found(cps("/m"))
        } else if bettermemory_core::verify_candidates::scan::getpwnam_refuses(name) {
            UserHome::Refused
        } else {
            UserHome::Unknown
        }
    };
    let mut read = ClaimKeys::new(RECURSION_LIMIT);
    for b in 0u32..=0xFF {
        for c in [b, 0xDC00 + b, 0xD800 + b] {
            for shape in ["/m/d/@", "~/@/x", "~@/y", "/m/@/z.q", "@", "/M/h\u{df}/@"] {
                let p: Vec<u32> = cps(shape)
                    .into_iter()
                    .flat_map(|x| {
                        if x == u32::from('@') {
                            vec![c]
                        } else {
                            vec![x]
                        }
                    })
                    .collect();
                let _ = read.key(&fs, &p, Some(&home), &mut users);
                let _ = claim_key(&fs, &p, Some(&home), &mut users);
                let _ = is_under_home(&fs, &p, Some(&home), &mut users);
                let _ = is_multi_segment_routelike(&fs, &p, Some(&home), &mut users);
                let _ = path_exists(&fs, &p, Some(&home), &mut users);
                let _ = home_ignores_case(&fs, &p);
            }
        }
    }
}

#[test]
fn swapcase_of_every_code_point() {
    let mut changed = 0;
    for c in 0..=0x10FFFFu32 {
        let s = [u32::from('A'), c, 0x3A3];
        let out = swapcase(&s);
        assert!(!out.is_empty() && out.len() <= 3 * s.len());
        changed += usize::from(out[1..out.len() - 1] != [c]);
    }
    assert!(changed > 2_000, "{changed}");
}
