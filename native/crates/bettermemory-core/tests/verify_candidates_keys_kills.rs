//! Cases a plausible wrong `verify_candidates::keys` fails, over trees in
//! memory (`MemFs`), each answer the reference's (read from
//! `verify_candidates.py` and checked against it):
//!
//! - `posixpath.realpath` in place of `_ClaimKeys`' walk: the two differ on
//!   a loop of two links met again after `..`, and on a component that
//!   does not encode (`islink` swallows the `ValueError` `realpath`
//!   raises);
//! - a link resolved against its unresolved parent;
//! - the memo shared across reads, so a link changed between two reads
//!   keeps its old key;
//! - `str.lower` or `str.swapcase` read as ASCII, so a home past ASCII
//!   never probes, or probes the wrong spelling;
//! - the probe run before the byte comparison misses;
//! - a home of `/`, `~` or none that does not disable the exemption;
//! - a `.` in the last part that does not exclude a route;
//! - an existence check that does not follow links;
//! - the bytes of a path decoded back in place of the `str` it was;
//! - `RecursionError` raised at a different depth, or after the disk was
//!   asked, or a `~` key counted at the four frames a `~user` key takes;
//! - the memo of keys dropped, so a read keys a path again (under a home
//!   changed since, or asking `pwd.getpwnam` again);
//! - a `..` at the root that leaves the path empty;
//! - the walk's bytes left on a link met again while its code points move
//!   on to the link's target.

#![cfg(unix)]

mod verify_candidates_keys_fs;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::verify_candidates::keys::{
    claim_key, home_ignores_case, is_multi_segment_routelike, is_under_home, path_exists, swapcase,
    ClaimKeys, Raised, RECURSION_LIMIT,
};
use bettermemory_core::verify_candidates::scan::UserHome;
use verify_candidates_keys_fs::{cps, Traced};

fn unknown(_: &[u32]) -> UserHome {
    UserHome::Unknown
}

fn tree() -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    for (path, node) in [
        ("/m/real/sub/f.py", Node::File(Vec::new())),
        ("/m/a/b/c/x", Node::File(Vec::new())),
        ("/m/abs", Node::Link(b"/m/real".to_vec())),
        ("/m/deep", Node::Link(b"/m/a/b/c".to_vec())),
        ("/m/a/b/c/up", Node::Link(b"../x".to_vec())),
        ("/m/la", Node::Link(b"lb".to_vec())),
        ("/m/lb", Node::Link(b"la".to_vec())),
        ("/m/dangling", Node::Link(b"/m/nowhere".to_vec())),
        ("/m/hm/proj", Node::Dir),
    ] {
        fs.insert(path.as_bytes(), node);
    }
    fs
}

fn key(fs: &MemFs, path: &[u32]) -> Vec<u32> {
    ClaimKeys::new(RECURSION_LIMIT)
        .key(fs, path, None, &mut unknown)
        .unwrap()
}

#[test]
fn the_walk_is_not_realpath_on_a_loop_met_again() {
    let fs = tree();
    // _ClaimKeys: dirname(realpath("/m/la")) is "/m", then realpath("/m/lb")
    // afresh stops at "/m/lb". realpath of the whole path keeps "/m/la" as
    // what lb resolved to while la was being resolved.
    let p = cps("/m/la/../lb");
    assert_eq!(key(&fs, &p), cps("/m/lb"));
    assert_eq!(
        claim_key(&fs, &p, None, &mut unknown).unwrap(),
        cps("/m/la")
    );
}

#[test]
fn the_walk_is_not_realpath_on_a_component_that_does_not_encode() {
    let fs = tree();
    let mut p = cps("/m/abs/x");
    p.push(0xD800);
    // islink of the joined path swallows the ValueError: the parent's
    // resolution stands. realpath raises it: the expanded spelling stands.
    let mut walked = cps("/m/real/x");
    walked.push(0xD800);
    assert_eq!(key(&fs, &p), walked);
    assert_eq!(claim_key(&fs, &p, None, &mut unknown).unwrap(), p);
    let mut up = p.clone();
    up.extend(cps("/../sub"));
    assert_eq!(key(&fs, &up), cps("/m/real/sub"));
    assert_eq!(claim_key(&fs, &up, None, &mut unknown).unwrap(), up);
}

#[test]
fn a_link_resolves_against_its_resolved_parent() {
    let fs = tree();
    // deep is /m/a/b/c, and up there is ../x: /m/a/b/x, not /m/x.
    assert_eq!(key(&fs, &cps("/m/deep/up")), cps("/m/a/b/x"));
    assert_eq!(key(&fs, &cps("/m/deep/up/..")), cps("/m/a/b"));
    assert_eq!(key(&fs, &cps("/m/abs/sub/f.py")), cps("/m/real/sub/f.py"));
}

#[test]
fn the_memo_is_one_read_s() {
    let mut fs = tree();
    let mut read = ClaimKeys::new(RECURSION_LIMIT);
    let p = cps("/m/abs/sub");
    assert_eq!(
        read.key(&fs, &p, None, &mut unknown).unwrap(),
        cps("/m/real/sub")
    );
    fs.insert(b"/m/abs", Node::Link(b"/m/a".to_vec()));
    // The same read keeps its key; a new read reads the disk again.
    assert_eq!(
        read.key(&fs, &p, None, &mut unknown).unwrap(),
        cps("/m/real/sub")
    );
    assert_eq!(key(&fs, &p), cps("/m/a/sub"));
    assert_eq!(
        read.key(&fs, &cps("/m/abs/sub/f.py"), None, &mut unknown)
            .unwrap(),
        cps("/m/real/sub/f.py")
    );
}

#[test]
fn each_component_is_asked_once_a_read() {
    let fs = tree();
    let traced = Traced::new(&fs);
    let mut read = ClaimKeys::new(RECURSION_LIMIT);
    read.key(&traced, &cps("/m/real/sub/f.py"), None, &mut unknown)
        .unwrap();
    assert_eq!(traced.take().len(), 4);
    read.key(&traced, &cps("/m/real/sub/g.py"), None, &mut unknown)
        .unwrap();
    assert_eq!(traced.take(), vec![("lstat", b"/m/real/sub/g.py".to_vec())]);
    read.key(&traced, &cps("/m/real/sub/g.py"), None, &mut unknown)
        .unwrap();
    assert!(traced.take().is_empty());
}

#[test]
fn a_read_keys_each_path_once() {
    // The reference: one `_ClaimKeys()` with `HOME` changed between two
    // calls keys `~/x` under the first home both times; a fresh read
    // keys it under the second.
    let fs = MemFs::new(Flavour::Posix);
    let (a, b) = (cps("/nonexist-a"), cps("/nonexist-b"));
    let p = cps("~/x");
    let mut read = ClaimKeys::new(RECURSION_LIMIT);
    assert_eq!(
        read.key(&fs, &p, Some(&a), &mut unknown),
        Ok(cps("/nonexist-a/x"))
    );
    assert_eq!(
        read.key(&fs, &p, Some(&b), &mut unknown),
        Ok(cps("/nonexist-a/x"))
    );
    assert_eq!(
        ClaimKeys::new(RECURSION_LIMIT).key(&fs, &p, Some(&b), &mut unknown),
        Ok(cps("/nonexist-b/x"))
    );
    // `pwd.getpwnam` is asked once for a `~user` key the read repeats.
    let mut asked = 0;
    let mut users = |_: &[u32]| {
        asked += 1;
        UserHome::Found(cps("/nonexist-u"))
    };
    for _ in 0..2 {
        assert_eq!(
            read.key(&fs, &cps("~u/x"), None, &mut users),
            Ok(cps("/nonexist-u/x"))
        );
    }
    assert_eq!(asked, 1);
}

#[test]
fn a_dot_dot_at_the_root_is_the_root() {
    let mut fs = tree();
    assert_eq!(
        claim_key(&fs, &cps("/.."), None, &mut unknown),
        Ok(cps("/"))
    );
    assert_eq!(
        claim_key(&fs, &cps("/m/.."), None, &mut unknown),
        Ok(cps("/"))
    );
    // A link to `/..` is resolved by `realpath` in the read as well.
    fs.insert(b"/m/top", Node::Link(b"/..".to_vec()));
    assert_eq!(key(&fs, &cps("/m/top")), cps("/"));
    assert_eq!(
        claim_key(&fs, &cps("/m/top"), None, &mut unknown),
        Ok(cps("/"))
    );
}

#[test]
fn swapcase_and_lower_are_unicode_s() {
    assert_eq!(swapcase(&cps("Stra\u{df}e")), cps("sTRASSE"));
    assert_eq!(swapcase(&cps("/\u{391}\u{3a3}")), cps("/\u{3b1}\u{3c2}"));
    assert_eq!(swapcase(&cps("\u{391}\u{3a3}.x")), cps("\u{3b1}\u{3c3}.X"));
    assert_eq!(swapcase(&cps("\u{1c5}x")), cps("\u{1c5}X"));
    assert_eq!(swapcase(&cps("\u{130}")), cps("i\u{307}"));
    assert_eq!(swapcase(&cps("\u{2160}\u{24d0}")), cps("\u{2170}\u{24b6}"));
    assert_eq!(swapcase(&cps("\u{2b0}\u{aa}")), cps("\u{2b0}\u{aa}"));
    assert_eq!(swapcase(&cps("\u{fb00}")), cps("FF"));
    // A home past ASCII that matches modulo case is probed.
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert("/hd/\u{391}\u{3a3}".as_bytes(), Node::Dir);
    fs.insert(
        "/HD/\u{3b1}\u{3c2}".as_bytes(),
        Node::Link("/hd/\u{391}\u{3a3}".as_bytes().to_vec()),
    );
    let home = cps("/hd/\u{391}\u{3a3}");
    let traced = Traced::new(&fs);
    assert_eq!(
        is_under_home(
            &traced,
            &cps("/hd/\u{3b1}\u{3c2}/x"),
            Some(&home),
            &mut unknown
        ),
        Ok(true)
    );
    assert_eq!(
        traced.take(),
        vec![
            ("stat", "/hd/\u{391}\u{3a3}".as_bytes().to_vec()),
            ("stat", "/HD/\u{3b1}\u{3c2}".as_bytes().to_vec())
        ]
    );
    // Lowered alone, the sigma before `/` is final: no case-insensitive
    // match for a medial sigma, so no probe.
    assert_eq!(
        is_under_home(
            &traced,
            &cps("/hd/\u{3b1}\u{3c3}x/y"),
            Some(&home),
            &mut unknown
        ),
        Ok(false)
    );
    assert!(traced.take().is_empty());
}

#[test]
fn the_volume_decides_the_probe() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/hd/u", Node::Dir);
    let home = cps("/hd/u");
    assert_eq!(home_ignores_case(&fs, &home), Ok(false));
    assert_eq!(
        is_under_home(&fs, &cps("/HD/u/x"), Some(&home), &mut unknown),
        Ok(false)
    );
    // A volume that folds case: the flipped spelling names the home.
    fs.insert(b"/HD/U", Node::Link(b"/hd/u".to_vec()));
    assert_eq!(home_ignores_case(&fs, &home), Ok(true));
    assert_eq!(
        is_under_home(&fs, &cps("/HD/u/x"), Some(&home), &mut unknown),
        Ok(true)
    );
    assert_eq!(
        is_multi_segment_routelike(&fs, &cps("/HD/u/gone/x"), Some(&home), &mut unknown),
        Ok(false)
    );
    // A home with nothing to flip is never probed.
    assert_eq!(home_ignores_case(&fs, &cps("/1/2")), Ok(false));
}

#[test]
fn the_byte_comparison_settles_first() {
    let fs = MemFs::new(Flavour::Posix);
    let traced = Traced::new(&fs);
    let home = cps("/hd/u");
    for (s, want) in [
        ("/hd/u", true),
        ("/hd/u/x", true),
        ("~/x", true),
        ("/hd/ux", false),
        ("/elsewhere/x", false),
    ] {
        assert_eq!(
            is_under_home(&traced, &cps(s), Some(&home), &mut unknown),
            Ok(want),
            "{s}"
        );
        assert!(traced.take().is_empty(), "{s} probed");
    }
    assert_eq!(
        is_under_home(&traced, &cps("/HD/U/x"), Some(&home), &mut unknown),
        Ok(false)
    );
    assert_eq!(traced.take(), vec![("stat", b"/hd/u".to_vec())]);
}

#[test]
fn a_root_or_unknown_home_disables_the_exemption() {
    let fs = MemFs::new(Flavour::Posix);
    let traced = Traced::new(&fs);
    for home in [Some(cps("/")), Some(cps("")), Some(cps("//")), None] {
        assert_eq!(
            is_under_home(&traced, &cps("/x/y"), home.as_deref(), &mut unknown),
            Ok(false)
        );
        assert_eq!(
            is_multi_segment_routelike(&traced, &cps("/api/v1/x"), home.as_deref(), &mut unknown),
            Ok(true)
        );
    }
    // `~h` is not `~`: `~/x` expands under it.
    assert_eq!(
        is_under_home(&traced, &cps("~/x"), Some(&cps("~h")), &mut unknown),
        Ok(true)
    );
    assert!(traced
        .take()
        .iter()
        .all(|(name, path)| *name == "stat" && path == b"/api/v1"));
}

#[test]
fn a_dot_in_the_last_part_is_a_file() {
    let fs = MemFs::new(Flavour::Posix);
    let home = cps("/hd/u");
    for (s, want) in [
        ("/api/v1/e.json", false),
        ("/api/v1.0/e", true),
        ("/api/v1/", true),
        ("/a", false),
        ("//", false),
    ] {
        assert_eq!(
            is_multi_segment_routelike(&fs, &cps(s), Some(&home), &mut unknown),
            Ok(want),
            "{s}"
        );
    }
}

#[test]
fn existence_follows_links() {
    let fs = tree();
    let mut users = |name: &[u32]| {
        if name == cps("m") {
            UserHome::Found(cps("/m/hm"))
        } else if name.contains(&0) {
            UserHome::Refused
        } else {
            UserHome::Unknown
        }
    };
    let home = cps("/m/hm");
    for (s, want) in [
        ("/m/abs/sub/f.py", Ok(true)),
        ("/m/abs", Ok(true)),
        ("/m/dangling", Ok(false)),
        ("/m/la", Ok(false)),
        ("~/proj", Ok(true)),
        ("~m/proj", Ok(true)),
        ("~m\u{0}/x", Ok(false)),
        ("~nobody/x", Err("RuntimeError")),
    ] {
        let got = path_exists(&fs, &cps(s), Some(&home), &mut users).map_err(|e| match e {
            Raised::Home(_) => "RuntimeError",
            other => panic!("{other:?}"),
        });
        assert_eq!(got, want, "{s}");
    }
    assert_eq!(
        path_exists(&fs, &cps("~/x"), None, &mut users),
        Err(Raised::Home(
            bettermemory_core::verify_candidates::scan::HomeError
        ))
    );
}

#[test]
fn a_spelling_is_kept_as_the_str_spelled_it() {
    let fs = tree();
    // "\udcc3\udca9" encodes to the bytes of "é"; the key keeps the str.
    let p: Vec<u32> = cps("/m/real/")
        .into_iter()
        .chain([0xDCC3, 0xDCA9])
        .collect();
    assert_eq!(key(&fs, &p), p);
    assert_eq!(claim_key(&fs, &p, None, &mut unknown).unwrap(), p);
    let mut fs2 = tree();
    fs2.insert("/m/real/\u{e9}".as_bytes(), Node::Dir);
    assert_eq!(key(&fs2, &p), p);
}

#[test]
fn the_recursion_limit_is_met_where_the_reference_meets_it() {
    let fs = MemFs::new(Flavour::Posix);
    let traced = Traced::new(&fs);
    let deep = |n: usize| [cps("/q"), cps("/a").repeat(n)].concat();
    // 196 components and the root miss the memo: 197 frames of `_resolve`
    // and four more is 201, one past the room.
    let mut read = ClaimKeys::new(200);
    assert_eq!(
        read.key(&traced, &deep(195), None, &mut unknown),
        Err(Raised::Recursion)
    );
    assert!(traced.take().is_empty(), "the disk was asked");
    assert_eq!(
        read.key(&traced, &deep(194), None, &mut unknown),
        Ok(deep(194))
    );
    traced.take();
    // The memo shortens the next: one miss.
    assert_eq!(
        read.key(&traced, &deep(195), None, &mut unknown),
        Ok(deep(195))
    );
    assert_eq!(traced.take().len(), 1);
    assert_eq!(
        ClaimKeys::new(0).key(&fs, &[], None, &mut unknown),
        Err(Raised::Recursion)
    );
    assert_eq!(
        ClaimKeys::new(2).key(&fs, &[], None, &mut unknown),
        Ok(vec![])
    );
    assert_eq!(
        ClaimKeys::new(2).key(&fs, &cps("//x"), None, &mut unknown),
        Err(Raised::Recursion)
    );
    assert_eq!(
        ClaimKeys::new(5).key(&fs, &cps("/q"), None, &mut unknown),
        Err(Raised::Recursion)
    );
    assert_eq!(
        ClaimKeys::new(5).key(&fs, &cps("/"), None, &mut unknown),
        Ok(cps("/"))
    );
}

#[test]
fn a_tilde_key_needs_six_frames() {
    // `posixpath.expanduser` of `~` or `~/...` reads `'HOME' not in
    // os.environ`: `Mapping.__contains__`, `_Environ.__getitem__` and its
    // `encodekey` are three Python frames under it, `HOME` set or not.
    // Measured under 3.13.13: a home of `/` or `rel` and none (where
    // `pwd.getpwuid` knows no entry) need room 6 for `~` and `~/x`; a
    // `~user` name `pwd.getpwnam` does not know needs 4, as `rel` does.
    let fs = MemFs::new(Flavour::Posix);
    let traced = Traced::new(&fs);
    let (root, rel) = (cps("/"), cps("rel"));
    for (home, tilde, slash_x) in [
        (Some(&root[..]), cps("/"), cps("/x")),
        (Some(&rel[..]), cps("rel"), cps("rel/x")),
        (None, cps("~"), cps("~/x")),
    ] {
        for (path, want) in [(cps("~"), tilde), (cps("~/x"), slash_x)] {
            assert_eq!(
                ClaimKeys::new(5).key(&traced, &path, home, &mut unknown),
                Err(Raised::Recursion)
            );
            assert!(traced.take().is_empty(), "the disk was asked");
            assert_eq!(
                ClaimKeys::new(6).key(&traced, &path, home, &mut unknown),
                Ok(want)
            );
            traced.take();
        }
    }
    // The root keyed first leaves the `~` that expands to it one frame
    // short all the same.
    let mut read = ClaimKeys::new(5);
    assert_eq!(
        read.key(&fs, &root, Some(&root), &mut unknown),
        Ok(cps("/"))
    );
    assert_eq!(
        read.key(&fs, &cps("~"), Some(&root), &mut unknown),
        Err(Raised::Recursion)
    );
    for path in [cps("~zz"), cps("~zz/x")] {
        assert_eq!(
            ClaimKeys::new(4).key(&fs, &path, Some(&root), &mut unknown),
            Ok(path.clone())
        );
        assert_eq!(
            ClaimKeys::new(3).key(&fs, &path, Some(&root), &mut unknown),
            Err(Raised::Recursion)
        );
    }
}

#[test]
fn a_link_met_again_in_one_walk_reads_on_from_its_target() {
    // `posixpath.realpath` keeps each link it resolved in `seen` and, when
    // the walk meets that link again, carries on from its target. The port
    // keeps the walk as code points and as bytes, and both must move to the
    // target: bytes left on the link make the `lstat` after a `..` ask
    // `/m/x` (a link) where the reference asks `/m/b/x` (a directory).
    // The reference: `_claim_key` and `_ClaimKeys()` of
    // `/m/l/../../l/../x` and of `/m/w` (a link to `l/../../l/../x`, which
    // the read hands to `realpath`) are `/m/b/x`.
    let mut fs = MemFs::new(Flavour::Posix);
    for (path, node) in [
        ("/m/b/c", Node::Dir),
        ("/m/b/x", Node::Dir),
        ("/m/l", Node::Link(b"b/c".to_vec())),
        ("/m/x", Node::Link(b"zzz".to_vec())),
        ("/m/w", Node::Link(b"l/../../l/../x".to_vec())),
    ] {
        fs.insert(path.as_bytes(), node);
    }
    for path in ["/m/l/../../l/../x", "/m/w"] {
        assert_eq!(
            claim_key(&fs, &cps(path), None, &mut unknown),
            Ok(cps("/m/b/x"))
        );
        assert_eq!(key(&fs, &cps(path)), cps("/m/b/x"));
    }
}
