//! Inputs a plausible wrong port of `githead`'s discovery answers
//! otherwise: each test names the mistake it catches. The answers are the
//! reference's, read from `githead.py` at v9 (and, where the fixture holds
//! the same shape, `githead_discovery.jsonl`). In memory, on every
//! platform.

mod common;

use bettermemory_core::config::pure_path;
use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::githead::discovery::{
    across_filesystems, across_filesystems_or_true, below, discovery_moved, find_gitdir, gitdir_at,
    locate, located, parts, probed, pure, stamp, Env, GITFILE_LIMIT,
};
use common::githead_discovery::{asking, asking_owned, repo, ME};

fn tree() -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/w/r/.git");
    fs.insert(b"/w/r/sub/deep", Node::Dir);
    fs.insert(b"/w/rx/y", Node::Dir);
    repo(&mut fs, "/w/sep");
    fs.insert(b"/w/g/.git", Node::File(b"gitdir: ../sep\n".to_vec()));
    fs
}

fn ceilings(raw: &str) -> Env {
    Env {
        ceilings: Some(raw.as_bytes().to_vec()),
        ..Env::default()
    }
}

fn found_root(fs: &MemFs, env: &Env, start: &str) -> (Option<Vec<u8>>, Option<Vec<u8>>) {
    asking(fs, env, ME, &[], |a| {
        (
            find_gitdir(a.rule, a.host, a.env, start.as_bytes()).map(|f| f.worktree_root),
            locate(a.rule, a.env, start.as_bytes(), true, false).map(|f| f.worktree_root),
        )
    })
}

/// Comparing ceilings by string prefix: `/w/r` is no ancestor of `/w/rx`.
#[test]
fn a_ceiling_is_a_folder_not_a_prefix() {
    assert!(!below(b"/w/rx/y", b"/w/r"));
    assert!(below(b"/w/r/y", b"/w/r"));
    let fs = tree();
    let walked = asking(&fs, &ceilings("/w/r"), ME, &[], |a| {
        probed(a.ctx, a.env, b"/w/rx/y", Some(0))
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap()
    });
    assert_eq!(walked.len(), 4, "{walked:?}");
}

/// A ceiling equal to the start is not a strict ancestor: no stop.
#[test]
fn a_ceiling_at_the_start_stops_nothing() {
    let fs = tree();
    let (f, l) = found_root(&fs, &ceilings("/w/r/sub/deep"), "/w/r/sub/deep");
    assert_eq!(f.as_deref(), Some(&b"/w/r"[..]));
    assert_eq!(l.as_deref(), Some(&b"/w/r"[..]));
    let (f, l) = found_root(&fs, &ceilings("/w/r"), "/w/r/sub/deep");
    assert_eq!((f, l), (None, None));
}

/// A ceiling that does not resolve declines the whole of `find_gitdir`
/// (`_real` in `_ceiling_offset`) and is dropped by `locate`
/// (`_ceilings`).
#[test]
fn a_missing_ceiling_declines_discovery_but_not_locate() {
    let fs = tree();
    let (f, l) = found_root(&fs, &ceilings("/w/nothere"), "/w/r/sub");
    assert_eq!(f, None);
    assert_eq!(l.as_deref(), Some(&b"/w/r"[..]));
}

/// After an empty entry a ceiling is read as written: `find_gitdir`
/// compares strings (`/w/./r` matches nothing), `locate` parts (`.` is
/// dropped, so it stops).
#[test]
fn a_verbatim_ceiling_is_read_two_ways() {
    let fs = tree();
    let (f, l) = found_root(&fs, &ceilings(":/w/./r"), "/w/r/sub");
    assert_eq!(f.as_deref(), Some(&b"/w/r"[..]));
    assert_eq!(l, None);
    // Before the empty entry it resolves, and both stop.
    let (f, l) = found_root(&fs, &ceilings("/w/./r"), "/w/r/sub");
    assert_eq!((f, l), (None, None));
    // A relative entry is dropped by both.
    let (f, l) = found_root(&fs, &ceilings("w/r"), "/w/r/sub");
    assert!(f.is_some() && l.is_some());
}

/// `PurePosixPath.parts`: two leading slashes are a root of their own,
/// three are one; `.` and empty parts go, `..` stays. The bytes rule is
/// `config::pure_path`'s POSIX branch.
#[test]
fn parts_are_pathlibs() {
    assert_eq!(parts(b"//a/b"), vec![&b"//"[..], b"a", b"b"]);
    assert_eq!(parts(b"///a"), vec![&b"/"[..], b"a"]);
    assert_eq!(parts(b"a/./b//../"), vec![&b"a"[..], b"b", b".."]);
    assert!(!below(b"/a/b", b"//a"));
    for text in [
        "",
        ".",
        "/",
        "//",
        "///",
        "//x/",
        "a//b/./c/",
        "/../x",
        "./a",
        "x/..",
    ] {
        assert_eq!(
            pure(text.as_bytes()),
            pure_path(text, bettermemory_core::config::Flavour::Posix).as_bytes(),
            "{text:?}"
        );
    }
}

/// Root's paths under `SUDO_UID` decline; root without it is held to its
/// own uid like any user.
#[test]
fn root_declines_only_under_sudo() {
    let fs = tree();
    let as_root = |env: &Env| {
        asking(&fs, env, 0, &[], |a| {
            find_gitdir(a.rule, a.host, a.env, b"/w/r").is_some()
        })
    };
    // Every path is ME's, not root's.
    assert!(!as_root(&Env::default()));
    let sudo = Env {
        sudo_uid: Some(b"501".to_vec()),
        ..Env::default()
    };
    assert!(!as_root(&sudo));
    // SUDO_UID alone does not matter to another user.
    let mine = asking(&fs, &sudo, ME, &[], |a| {
        find_gitdir(a.rule, a.host, a.env, b"/w/r").is_some()
    });
    assert!(mine);
    // Root's own paths answer for root without `SUDO_UID` and decline
    // with it: the check of `SUDO_UID` is not the owner's check.
    let root_owns = |env: &Env| {
        asking_owned(&fs, env, 0, 0, &[], |a| {
            find_gitdir(a.rule, a.host, a.env, b"/w/r").map(|f| f.worktree_root)
        })
    };
    assert_eq!(root_owns(&Env::default()).as_deref(), Some(&b"/w/r"[..]));
    assert_eq!(root_owns(&sudo), None);
}

/// The owner's check covers the `.git` file, its folder and the git
/// directory it names, and `locate` makes none.
#[test]
fn every_owner_is_checked_but_not_by_locate() {
    let fs = tree();
    for foreign in ["/w/g/.git", "/w/g", "/w/sep"] {
        let (f, l) = asking(&fs, &Env::default(), ME, &[foreign], |a| {
            (
                gitdir_at(a.rule, a.host, a.env, b"/w/g"),
                locate(a.rule, a.env, b"/w/g", false, true),
            )
        });
        assert_eq!(f, None, "{foreign}");
        assert_eq!(l.unwrap().gd.gitdir, b"/w/sep", "{foreign}");
    }
}

/// A relative `gitdir:` is joined to the `.git` file's folder, not to the
/// working directory.
#[test]
fn a_relative_gitfile_is_read_from_its_folder() {
    let fs = tree();
    let found = asking(&fs, &Env::default(), ME, &[], |a| {
        locate(a.rule, a.env, b"/w/g", true, true)
    });
    let found = found.unwrap();
    assert_eq!(found.gd.gitdir, b"/w/sep");
    assert_eq!(found.gd.commondir, b"/w/sep");
    assert_eq!(found.worktree_root, b"/w/g");
}

/// `gitdir: ` with no path declines even where the folder itself is a
/// git directory: the empty name is not joined to the folder.
#[test]
fn an_empty_gitfile_path_declines() {
    for text in [&b"gitdir: \n"[..], b"gitdir: \r\n", b"gitdir: "] {
        let mut fs = MemFs::new(Flavour::Posix);
        repo(&mut fs, "/w/r");
        fs.insert(b"/w/r/.git", Node::File(text.to_vec()));
        let (f, l) = asking(&fs, &Env::default(), ME, &[], |a| {
            (
                gitdir_at(a.rule, a.host, a.env, b"/w/r"),
                locate(a.rule, a.env, b"/w/r", false, true),
            )
        });
        assert_eq!((f, l), (None, None), "{text:?}");
    }
}

/// git refuses a `.git` file over 1 MiB: exactly 1 MiB is read.
#[test]
fn the_gitfile_bound_is_inclusive() {
    for (extra, answers) in [(0, true), (1, false)] {
        let mut fs = tree();
        let mut text = b"gitdir: /w/sep\n".to_vec();
        text.resize(GITFILE_LIMIT + extra, b'\n');
        fs.insert(b"/w/g/.git", Node::File(text));
        let found = asking(&fs, &Env::default(), ME, &[], |a| {
            locate(a.rule, a.env, b"/w/g", false, true)
        });
        assert_eq!(found.is_some(), answers, "{extra}");
    }
}

/// Each discovery variable declines `find_gitdir` and `_located`, set to
/// any value, the empty one included; `locate` reads none of them.
#[test]
fn each_discovery_variable_moves_discovery() {
    let fs = tree();
    let empty = Some(Vec::new());
    let envs = [
        Env {
            git_dir: empty.clone(),
            ..Env::default()
        },
        Env {
            git_work_tree: empty.clone(),
            ..Env::default()
        },
        Env {
            git_common_dir: empty.clone(),
            ..Env::default()
        },
        Env {
            across_filesystem: empty.clone(),
            ..Env::default()
        },
        Env {
            assume_different_owner: empty,
            ..Env::default()
        },
    ];
    for env in &envs {
        assert!(discovery_moved(env));
        asking(&fs, env, ME, &[], |a| {
            assert!(find_gitdir(a.rule, a.host, a.env, b"/w/r").is_none());
            assert!(located(a.rule, a.env, b"/w/r").is_err());
            assert!(locate(a.rule, a.env, b"/w/r", true, false).is_some());
        });
    }
    let others = Env {
        ceilings: Some(b"/".to_vec()),
        sudo_uid: Some(b"0".to_vec()),
        ..Env::default()
    };
    assert!(!discovery_moved(&others));
}

/// `git_env_bool`: unset is false, a value git dies on is None (and the
/// walk then crosses).
#[test]
fn across_filesystems_reads_a_git_boolean() {
    let with = |v: &str| Env {
        across_filesystem: Some(v.as_bytes().to_vec()),
        ..Env::default()
    };
    assert_eq!(across_filesystems(&Env::default()), Some(false));
    assert_eq!(across_filesystems(&with("")), Some(false));
    assert_eq!(across_filesystems(&with("0")), Some(false));
    assert_eq!(across_filesystems(&with("On")), Some(true));
    assert_eq!(across_filesystems(&with("2")), Some(true));
    assert_eq!(across_filesystems(&with("1k")), Some(true));
    assert_eq!(across_filesystems(&with("bogus")), None);
    assert!(across_filesystems_or_true(&with("bogus")));
}

/// The walk stops at a device boundary, but `probed` without a device
/// crosses it.
#[test]
fn a_device_boundary_stops_the_walk() {
    let mut fs = tree();
    fs.mount(b"/w/r/sub", 7);
    let (f, l, crossed) = asking(&fs, &Env::default(), ME, &[], |a| {
        (
            find_gitdir(a.rule, a.host, a.env, b"/w/r/sub/deep"),
            locate(a.rule, a.env, b"/w/r/sub/deep", true, false),
            probed(a.ctx, a.env, b"/w/r/sub/deep", None)
                .unwrap()
                .count(),
        )
    });
    assert_eq!((f, l), (None, None));
    assert_eq!(crossed, 5);
}

/// `stamp` follows a link unless told not to, and is None for nothing
/// there, ENOTDIR included.
#[test]
fn stamp_follows_unless_told() {
    let mut fs = tree();
    fs.insert(b"/w/f", Node::File(b"abc".to_vec()));
    fs.insert(b"/w/lf", Node::Link(b"/w/f".to_vec()));
    asking(&fs, &Env::default(), ME, &[], |a| {
        let file = stamp(a.ctx, b"/w/f", true).unwrap().unwrap();
        assert_eq!(stamp(a.ctx, b"/w/lf", true).unwrap(), Some(file));
        let link = stamp(a.ctx, b"/w/lf", false).unwrap().unwrap();
        assert_ne!(link, file);
        assert_eq!(link[4] >> 12, 0o12);
        assert_eq!(file[2], 3);
        assert_eq!(stamp(a.ctx, b"/w/f/x", true).unwrap(), None);
        assert_eq!(stamp(a.ctx, b"/w/none", true).unwrap(), None);
    });
}

/// The root `/` counts as length 0 against a ceiling: a ceiling of `/`
/// stops the walk before it probes `/`.
#[test]
fn a_root_ceiling_keeps_the_root_unprobed() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/.git");
    fs.insert(b"/a", Node::Dir);
    let (f, l) = found_root(&fs, &ceilings("/"), "/a");
    assert_eq!(f, None);
    assert_eq!(l, None);
    let (f, l) = found_root(&fs, &Env::default(), "/a");
    assert_eq!(f.as_deref(), Some(&b"/"[..]));
    assert_eq!(l.as_deref(), Some(&b"/"[..]));
}

/// A ceiling with a multibyte or undecodable part stops the walk at the
/// same folder as an ASCII one: the reference counts `str` characters
/// and the port bytes, both positions in the same path.
#[test]
fn a_non_ascii_ceiling_stops_where_an_ascii_one_does() {
    for name in [&b"\xc3\xa9"[..], b"\xff", b"e"] {
        let mut fs = MemFs::new(Flavour::Posix);
        repo(&mut fs, "/w/.git");
        let mut deep = b"/w/".to_vec();
        deep.extend_from_slice(name);
        let ceiling = deep.clone();
        deep.extend_from_slice(b"/x/y");
        fs.insert(&deep, Node::Dir);
        let env = Env {
            ceilings: Some(ceiling.clone()),
            ..Env::default()
        };
        let start = deep.clone();
        let asked = |env: &Env| {
            asking(&fs, env, ME, &[], |a| {
                (
                    find_gitdir(a.rule, a.host, a.env, &start).map(|f| f.worktree_root),
                    locate(a.rule, a.env, &start, true, false).map(|f| f.worktree_root),
                )
            })
        };
        assert_eq!(asked(&env), (None, None), "{name:?}");
        let (f, l) = asked(&Env::default());
        assert_eq!(f.as_deref(), Some(&b"/w"[..]), "{name:?}");
        assert_eq!(l.as_deref(), Some(&b"/w"[..]), "{name:?}");
        // A ceiling one folder down lets the walk reach `/w/<name>` only.
        let mut lower = ceiling.clone();
        lower.extend_from_slice(b"/x");
        let env = Env {
            ceilings: Some(lower),
            ..Env::default()
        };
        assert_eq!(asked(&env), (None, None), "{name:?}");
    }
}

/// Membership's reading of the probed folder comes before the `.git`
/// stat: a refused `.git` entry declines every probe, and a provisional
/// follow (a `.git` file naming a git directory with no record) declines
/// only `_located`.
#[test]
fn membership_refuses_before_the_probe() {
    let mut fs = tree();
    fs.insert(
        b"/w/f/.git/HEAD",
        Node::File(b"ref: refs/heads/main\n".to_vec()),
    );
    fs.insert(b"/w/f/.git/commondir", Node::File(b"/w/r/.git\n".to_vec()));
    fs.insert(b"/w/f/in", Node::Dir);
    asking(&fs, &Env::default(), ME, &[], |a| {
        assert!(find_gitdir(a.rule, a.host, a.env, b"/w/f/in").is_none());
        assert!(gitdir_at(a.rule, a.host, a.env, b"/w/f").is_none());
        assert!(locate(a.rule, a.env, b"/w/f/in", true, false).is_none());
        assert!(located(a.rule, a.env, b"/w/f/in").is_err());
        // Provisional: followed by all but `_located`.
        let root = find_gitdir(a.rule, a.host, a.env, b"/w/g").map(|f| f.worktree_root);
        assert_eq!(root.as_deref(), Some(&b"/w/g"[..]));
        let root = locate(a.rule, a.env, b"/w/g", true, false).map(|f| f.worktree_root);
        assert_eq!(root.as_deref(), Some(&b"/w/g"[..]));
        assert!(located(a.rule, a.env, b"/w/g").is_err());
    });
}

/// A bare repository, or the inside of a git directory, declines rather
/// than answering no repository; `_located` then declines too.
#[test]
fn a_git_directory_start_declines() {
    let fs = tree();
    asking(&fs, &Env::default(), ME, &[], |a| {
        assert!(find_gitdir(a.rule, a.host, a.env, b"/w/sep").is_none());
        assert!(located(a.rule, a.env, b"/w/sep").is_err());
        assert!(located(a.rule, a.env, b"/w/rx/y").unwrap().is_none());
    });
}

/// Everything declines under the Windows flavour.
#[test]
fn the_windows_flavour_declines() {
    let mut fs = MemFs::new(Flavour::Windows);
    repo(&mut fs, "C:/w/r/.git");
    asking(&fs, &Env::default(), ME, &[], |a| {
        assert!(find_gitdir(a.rule, a.host, a.env, b"C:/w/r").is_none());
        assert!(gitdir_at(a.rule, a.host, a.env, b"C:/w/r").is_none());
        assert!(locate(a.rule, a.env, b"C:/w/r", true, true).is_none());
        assert!(stamp(a.ctx, b"C:/w/r", true).is_err());
    });
}
