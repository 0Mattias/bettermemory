//! Inputs a plausible wrong port of `entry_above` and `signature` answers
//! otherwise: each test names the mistake it catches. The answers are the
//! reference's, read from `githead.py` at v9 (and, where the fixture holds
//! the same shape, `githead_signature.jsonl`). In memory, on every
//! platform: the device boundary and a FIFO ref, which the fixture leaves
//! out, are here.

mod common;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::githead::discovery::{find_gitdir, Env};
use bettermemory_core::githead::signature::{
    entry_above, git_dir_of, ref_stamps, signature, stops_at, would_be_git_directory, CtxRefs,
    First, Signature,
};
use bettermemory_core::githead::{FsError, GitDir, RefFs};
use common::githead_discovery::{asking, repo, ME};

const SHA: &[u8] = b"1111111111111111111111111111111111111111\n";

fn branch(head: &[u8]) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/w/r/.git");
    fs.insert(b"/w/r/.git/HEAD", Node::File(head.to_vec()));
    fs.insert(b"/w/r/in", Node::Dir);
    fs
}

fn sig(fs: &MemFs, env: &Env, start: &str, owner: bool, foreign: &[&str], euid: u32) -> Signature {
    asking(fs, env, euid, foreign, |a| {
        signature(a.rule, a.host, a.env, start.as_bytes(), owner).unwrap()
    })
}

fn above(fs: &MemFs, env: &Env, start: &str) -> Option<bool> {
    asking(fs, env, ME, &[], |a| {
        entry_above(a.ctx, a.env, start.as_bytes())
    })
}

fn undetermined(s: &Signature) -> bool {
    matches!(s.first, First::Undetermined(_)) && s.head.is_none() && s.refs.is_none()
}

/// A shared marker (a unit or None) would make two undetermined
/// signatures equal, and a cache would keep a stale value.
#[test]
fn two_undetermined_signatures_differ() {
    let fs = branch(b"ref: refs/heads/.invalid\n");
    let one = sig(&fs, &Env::default(), "/w/r", true, &[], ME);
    let two = sig(&fs, &Env::default(), "/w/r", true, &[], ME);
    assert!(undetermined(&one) && undetermined(&two));
    assert_ne!(one, two);
}

/// Outside any repository the first slot is None, not the marker: two
/// such reads compare equal.
#[test]
fn outside_is_none_and_stable() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/w/plain/f", Node::File(Vec::new()));
    let one = sig(&fs, &Env::default(), "/w/plain", true, &[], ME);
    assert_eq!(one.first, First::Outside);
    assert_eq!(one, sig(&fs, &Env::default(), "/w/plain", true, &[], ME));
}

/// The three variables are kept in every slot, the undetermined one too.
#[test]
fn the_environment_is_kept_when_undetermined() {
    let fs = branch(b"ref: refs/heads/main\n");
    let env = Env {
        git_dir: Some(b"/w/r/.git".to_vec()),
        git_work_tree: Some(b"/w/r".to_vec()),
        ceilings: Some(b"/x".to_vec()),
        ..Env::default()
    };
    let s = sig(&fs, &env, "/w/r", true, &[], ME);
    assert!(undetermined(&s));
    assert_eq!(s.git_dir.as_deref(), Some(&b"/w/r/.git"[..]));
    assert_eq!(s.git_work_tree.as_deref(), Some(&b"/w/r"[..]));
    assert_eq!(s.ceilings.as_deref(), Some(&b"/x"[..]));
}

/// Off by one in the chain's depth: HEAD and four refs are read, a fifth
/// is not.
#[test]
fn the_chain_stops_at_git_depth() {
    for (depth, settled) in [(4usize, true), (5, false)] {
        let mut fs = branch(b"ref: refs/heads/b0\n");
        for i in 0..depth {
            let text = if i + 1 == depth {
                SHA.to_vec()
            } else {
                format!("ref: refs/heads/b{}\n", i + 1).into_bytes()
            };
            fs.insert(
                format!("/w/r/.git/refs/heads/b{i}").as_bytes(),
                Node::File(text),
            );
        }
        let s = sig(&fs, &Env::default(), "/w/r", true, &[], ME);
        assert_eq!(!undetermined(&s), settled, "depth {depth}");
        if settled {
            assert_eq!(s.refs.unwrap().len(), depth);
        }
    }
}

/// A FIFO where a ref would be: neither a file nor a directory, so the
/// signature declines rather than open it.
#[test]
fn a_fifo_ref_is_undetermined() {
    let mut fs = branch(b"ref: refs/heads/main\n");
    fs.insert(b"/w/r/.git/refs/heads/main", Node::Fifo);
    assert!(undetermined(&sig(
        &fs,
        &Env::default(),
        "/w/r",
        true,
        &[],
        ME
    )));
}

/// A ref that is a directory ends the chain with its stamp: git reads
/// packed-refs next. A port that read it as a file would decline.
#[test]
fn a_directory_ref_ends_the_chain() {
    let mut fs = branch(b"ref: refs/heads/main\n");
    fs.insert(b"/w/r/.git/refs/heads/main/x", Node::File(SHA.to_vec()));
    let refs = sig(&fs, &Env::default(), "/w/r", true, &[], ME)
        .refs
        .unwrap();
    assert_eq!(refs.len(), 1);
    assert_eq!(refs[0].unwrap()[4] >> 12, 0o4);
}

/// A missing branch: one None stamp, not an empty list.
#[test]
fn a_packed_only_branch_has_a_none_stamp() {
    let fs = branch(b"ref: refs/heads/main\n");
    let s = sig(&fs, &Env::default(), "/w/r", true, &[], ME);
    assert_eq!(s.refs, Some(vec![None]));
    assert_eq!(s.packed, None);
}

/// A per-worktree ref is stamped under the git directory, not the common
/// one.
#[test]
fn a_per_worktree_ref_is_read_in_the_git_directory() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/g/HEAD", Node::File(SHA.to_vec()));
    fs.insert(b"/g/refs/worktree/x", Node::File(SHA.to_vec()));
    fs.insert(b"/c/refs/worktree/x", Node::Fifo);
    let gd = GitDir {
        gitdir: b"/g".to_vec(),
        commondir: b"/c".to_vec(),
    };
    let stamps = asking(&fs, &Env::default(), ME, &[], |a| {
        ref_stamps(a.ctx, &gd, b"ref: refs/worktree/x\n")
    })
    .unwrap();
    assert_eq!(stamps.len(), 1);
    assert_eq!(stamps[0].unwrap()[2], 41);
}

/// `owner` False reads another account's repository; True declines.
#[test]
fn owner_false_reads_another_accounts_repository() {
    let mut fs = branch(b"ref: refs/heads/main\n");
    fs.insert(b"/w/r/.git/refs/heads/main", Node::File(SHA.to_vec()));
    let foreign = ["/w/r/.git", "/w/r"];
    assert!(undetermined(&sig(
        &fs,
        &Env::default(),
        "/w/r/in",
        true,
        &foreign,
        ME
    )));
    let read = sig(&fs, &Env::default(), "/w/r/in", false, &foreign, ME);
    assert_eq!(read.first, First::GitDir(b"/w/r/.git".to_vec()));
}

/// `signature` walks with `stable`: a git directory kept apart that no
/// record names is undetermined there, though `find_gitdir` follows it.
#[test]
fn the_signature_walk_is_stable() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/w/sep");
    fs.insert(b"/w/s/.git", Node::File(b"gitdir: /w/sep\n".to_vec()));
    fs.insert(b"/w/s/in", Node::Dir);
    let found = asking(&fs, &Env::default(), ME, &[], |a| {
        find_gitdir(a.rule, a.host, a.env, b"/w/s/in")
    });
    assert!(found.is_some());
    assert!(undetermined(&sig(
        &fs,
        &Env::default(),
        "/w/s/in",
        true,
        &[],
        ME
    )));
}

/// A rewrite changes HEAD's stamp; equal content and an equal stamp give
/// equal signatures.
#[test]
fn a_changed_head_changes_the_signature() {
    let fs = branch(SHA);
    let before = sig(&fs, &Env::default(), "/w/r", true, &[], ME);
    assert_eq!(before, sig(&fs, &Env::default(), "/w/r", true, &[], ME));
    let mut moved = fs.clone();
    moved.insert(
        b"/w/r/.git/HEAD",
        Node::File(b"2222222222222222222222222222222222222222\n".to_vec()),
    );
    assert_ne!(before, sig(&moved, &Env::default(), "/w/r", true, &[], ME));
}

/// HEAD alone, a HEAD directory, an empty `.git` and a lowercase `head`
/// are no stop; a `.git` file is one whatever it holds.
#[test]
fn what_stops_the_walk() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(
        b"/w/headonly/HEAD",
        Node::File(b"ref: refs/heads/main\n".to_vec()),
    );
    fs.insert(b"/w/headdir/HEAD/x", Node::File(Vec::new()));
    fs.insert(b"/w/empty/.git", Node::Dir);
    fs.insert(
        b"/w/lower/.git/head",
        Node::File(b"ref: refs/heads/main\n".to_vec()),
    );
    fs.insert(b"/w/lower/.git/objects", Node::Dir);
    fs.insert(b"/w/lower/.git/refs", Node::Dir);
    fs.insert(b"/w/file/.git", Node::File(b"anything\n".to_vec()));
    repo(&mut fs, "/w/ok/.git");
    for (name, stop) in [
        ("headonly", false),
        ("headdir", false),
        ("empty", false),
        ("lower", false),
        ("file", true),
        ("ok", true),
    ] {
        let at = format!("/w/{name}");
        let got = asking(&fs, &Env::default(), ME, &[], |a| {
            stops_at(a.ctx, at.as_bytes())
        });
        assert_eq!(got.unwrap(), stop, "{name}");
    }
}

/// A HEAD nobody may read counts as a git directory's: git dies there.
#[test]
fn an_unreadable_head_would_be_a_git_directory() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/w/.git");
    fs.lock(b"/w/.git/HEAD");
    let got = asking(&fs, &Env::default(), ME, &[], |a| {
        would_be_git_directory(a.ctx, b"/w/.git")
    });
    assert!(got.unwrap());
}

/// The walk stops at a device boundary unless the variable says to cross;
/// a value git dies on crosses.
#[test]
fn entry_above_stops_at_a_device() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/w/r/.git");
    fs.insert(b"/w/r/m/in", Node::Dir);
    fs.mount(b"/w/r/m", 7);
    let across = |value: Option<&str>| Env {
        across_filesystem: value.map(|v| v.as_bytes().to_vec()),
        ..Env::default()
    };
    assert_eq!(above(&fs, &across(None), "/w/r/m/in"), Some(false));
    assert_eq!(above(&fs, &across(Some("false")), "/w/r/m/in"), Some(false));
    assert_eq!(above(&fs, &across(Some("1")), "/w/r/m/in"), Some(true));
    assert_eq!(above(&fs, &across(Some("bogus")), "/w/r/m/in"), Some(true));
}

/// False where the start is missing or a file; True where a stat fails
/// for another reason (a folder that cannot be searched).
#[test]
fn entry_above_edges() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/w/f", Node::File(Vec::new()));
    repo(&mut fs, "/w/locked/.git");
    fs.insert(b"/w/locked/in", Node::Dir);
    fs.set_mode(b"/w/locked/.git", 0o600);
    let env = Env::default();
    assert_eq!(above(&fs, &env, "/w/nothere"), Some(false));
    assert_eq!(above(&fs, &env, "/w/f"), Some(false));
    assert_eq!(above(&fs, &env, "/w/locked/in"), Some(true));
}

/// The Windows flavour's realpath is not ported: a decline, never an
/// answer.
#[test]
fn the_windows_flavour_declines() {
    let mut fs = MemFs::new(Flavour::Windows);
    fs.insert(
        b"C:\\w\\r\\.git\\HEAD",
        Node::File(b"ref: refs/heads/main\n".to_vec()),
    );
    fs.insert(b"C:\\w\\r\\.git\\objects", Node::Dir);
    fs.insert(b"C:\\w\\r\\.git\\refs", Node::Dir);
    assert_eq!(above(&fs, &Env::default(), "C:\\w\\r"), None);
    for owner in [true, false] {
        assert!(undetermined(&sig(
            &fs,
            &Env::default(),
            "C:\\w\\r",
            owner,
            &[],
            ME
        )));
    }
}

/// The adapter reads through the context's file system and tells a
/// missing file from a directory.
#[test]
fn ctx_refs_reads_the_context() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/g/HEAD", Node::File(SHA.to_vec()));
    fs.insert(b"/g/refs/heads/d/x", Node::File(Vec::new()));
    asking(&fs, &Env::default(), ME, &[], |a| {
        let refs = CtxRefs(a.ctx);
        assert_eq!(refs.read(b"/g/HEAD", 100, false).unwrap(), SHA);
        assert_eq!(refs.read(b"/g/none", 100, false), Err(FsError::Missing));
        assert_eq!(
            refs.read(b"/g/refs/heads/d", 100, false),
            Err(FsError::IsADirectory)
        );
        assert_eq!(refs.lstat_is_dir(b"/g/refs/heads/d"), Ok(true));
        assert_eq!(refs.lstat_is_dir(b"/g/HEAD"), Ok(false));
        assert_eq!(refs.lstat_is_dir(b"/g/none"), Err(FsError::Missing));
    });
    let mut repo_fs = MemFs::new(Flavour::Posix);
    repo(&mut repo_fs, "/w/r/.git");
    let found = asking(&repo_fs, &Env::default(), ME, &[], |a| {
        find_gitdir(a.rule, a.host, a.env, b"/w/r").unwrap()
    });
    assert_eq!(git_dir_of(&found), found.gd);
}
