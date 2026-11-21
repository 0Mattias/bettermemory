//! Cases a plausible wrong `verify_anchoring::skip` fails: a config read
//! without universal newlines, a memo that answers a stale index, a memo
//! that keeps a read whose stamp moved, eviction by insertion instead of
//! use, the worktree's switch read from the common directory, and a
//! Windows flavour that answers instead of declining.

mod common;

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::gitdir::{Ctx, Failure};
use bettermemory_core::githead::discovery::Env;
use bettermemory_core::githead::GitDir;
use bettermemory_core::verify_anchoring::skip::{
    absent_by_sparsity, skip_index, sparse_enabled, SkipMemo, SKIP_MEMO_CAP,
};
use common::verify_anchoring_skip::{git, Moving};

const ON: &[u8] = b"[core]\n\tsparseCheckout = true\n";

/// A version-3 index of `names` (bit set where true), SHA-1 lengths.
fn index(names: &[(&[u8], bool)]) -> Vec<u8> {
    let mut out = b"DIRC".to_vec();
    out.extend_from_slice(&3u32.to_be_bytes());
    out.extend_from_slice(&u32::try_from(names.len()).unwrap().to_be_bytes());
    for (name, skip) in names {
        let mut e = vec![0u8; 40];
        e.extend_from_slice(&[0xAB; 20]);
        let flags = u16::try_from(name.len()).unwrap() | if *skip { 0x4000 } else { 0 };
        e.extend_from_slice(&flags.to_be_bytes());
        if *skip {
            e.extend_from_slice(&0x4000u16.to_be_bytes());
        }
        e.extend_from_slice(name);
        let pad = 8 - e.len() % 8;
        e.extend(std::iter::repeat_n(0, pad));
        out.extend(e);
    }
    out.extend_from_slice(&[0xEE; 20]);
    out
}

fn repo(fs: &mut MemFs, at: &str, config: &[u8], idx: Option<Vec<u8>>) {
    fs.insert(
        format!("{at}/.git/HEAD").as_bytes(),
        Node::File(b"ref: refs/heads/main\n".to_vec()),
    );
    fs.insert(format!("{at}/.git/objects").as_bytes(), Node::Dir);
    fs.insert(format!("{at}/.git/refs").as_bytes(), Node::Dir);
    fs.insert(
        format!("{at}/.git/config").as_bytes(),
        Node::File(config.to_vec()),
    );
    if let Some(data) = idx {
        fs.insert(format!("{at}/.git/index").as_bytes(), Node::File(data));
    }
}

fn gd(at: &str) -> GitDir {
    GitDir {
        gitdir: format!("{at}/.git").into_bytes(),
        commondir: format!("{at}/.git").into_bytes(),
    }
}

fn with<T>(
    fs: &Moving,
    flavour: Flavour,
    f: impl FnOnce(&Rule<'_, Moving, Hung, NoRecords>) -> T,
) -> T {
    let ctx = Ctx {
        fs,
        flavour,
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

#[test]
fn a_lone_carriage_return_ends_a_line() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(
        &mut fs,
        "/w/r",
        b"[core]\n\tsparseCheckout = false\r\tsparseCheckout = true\n",
        None,
    );
    let fs = Moving::new(fs);
    with(&fs, Flavour::Posix, |rule| {
        assert!(sparse_enabled(rule.ctx, &gd("/w/r")));
    });
}

#[test]
fn the_worktree_file_is_read_from_the_git_directory_and_wins() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/w/r", ON, None);
    fs.insert(
        b"/w/r/.git/wt/config.worktree",
        Node::File(b"[core]\n\tsparseCheckout = false\n".to_vec()),
    );
    let fs = Moving::new(fs);
    with(&fs, Flavour::Posix, |rule| {
        let linked = GitDir {
            gitdir: b"/w/r/.git/wt".to_vec(),
            commondir: b"/w/r/.git".to_vec(),
        };
        assert!(!sparse_enabled(rule.ctx, &linked));
        assert!(sparse_enabled(rule.ctx, &gd("/w/r")));
    });
}

#[test]
fn an_index_without_a_config_is_read_as_sha1() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(
        b"/w/r/.git/HEAD",
        Node::File(b"ref: refs/heads/main\n".to_vec()),
    );
    fs.insert(b"/w/r/.git/objects", Node::Dir);
    fs.insert(b"/w/r/.git/refs", Node::Dir);
    fs.insert(
        b"/w/r/.git/index",
        Node::File(index(&[(b"a", true), (b"b", false)])),
    );
    let fs = Moving::new(fs);
    let mut memo = SkipMemo::new();
    with(&fs, Flavour::Posix, |rule| {
        let got = skip_index(rule, &gd("/w/r"), &mut memo).unwrap().unwrap();
        assert_eq!(got.skipped, vec![b"a".to_vec()]);
        assert_eq!(got.kept, vec![b"b".to_vec()]);
    });
    assert_eq!(memo.len(), 1);
}

#[test]
fn a_rewritten_index_is_read_again() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/w/r", ON, Some(index(&[(b"a", true)])));
    let fs = Moving::new(fs);
    let mut memo = SkipMemo::new();
    with(&fs, Flavour::Posix, |rule| {
        let first = skip_index(rule, &gd("/w/r"), &mut memo).unwrap().unwrap();
        assert_eq!(first.skipped, vec![b"a".to_vec()]);
    });
    fs.inner.borrow_mut().insert(
        b"/w/r/.git/index",
        Node::File(index(&[(b"a", false), (b"b", true)])),
    );
    with(&fs, Flavour::Posix, |rule| {
        let second = skip_index(rule, &gd("/w/r"), &mut memo).unwrap().unwrap();
        assert_eq!(second.kept, vec![b"a".to_vec()]);
    });
    assert_eq!(memo.len(), 1);
}

#[test]
fn a_read_whose_stamp_moved_answers_and_is_not_kept() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/w/r", ON, Some(index(&[(b"a", true)])));
    let fs = Moving::new(fs);
    fs.moves
        .borrow_mut()
        .insert(b"/w/r/.git/index".to_vec(), index(&[(b"q", false)]));
    fs.armed.set(true);
    let mut memo = SkipMemo::new();
    with(&fs, Flavour::Posix, |rule| {
        let got = skip_index(rule, &gd("/w/r"), &mut memo).unwrap().unwrap();
        assert_eq!(got.kept, vec![b"q".to_vec()]);
        assert!(memo.is_empty());
        skip_index(rule, &gd("/w/r"), &mut memo).unwrap();
        assert_eq!(memo.len(), 1);
    });
}

#[test]
fn the_least_recently_used_index_goes_first() {
    let mut fs = MemFs::new(Flavour::Posix);
    for i in 0..=SKIP_MEMO_CAP {
        repo(
            &mut fs,
            &format!("/w/r{i}"),
            ON,
            Some(index(&[(b"x", true)])),
        );
    }
    let fs = Moving::new(fs);
    let mut memo = SkipMemo::new();
    with(&fs, Flavour::Posix, |rule| {
        for i in 0..SKIP_MEMO_CAP {
            skip_index(rule, &gd(&format!("/w/r{i}")), &mut memo).unwrap();
        }
        skip_index(rule, &gd("/w/r0"), &mut memo).unwrap();
        skip_index(rule, &gd(&format!("/w/r{SKIP_MEMO_CAP}")), &mut memo).unwrap();
    });
    let keys: Vec<Vec<u8>> = memo.keys().into_iter().map(<[u8]>::to_vec).collect();
    assert_eq!(keys.len(), SKIP_MEMO_CAP);
    assert!(!keys.contains(&b"/w/r1/.git/index".to_vec()));
    assert!(keys.contains(&b"/w/r0/.git/index".to_vec()));
}

#[test]
fn an_unread_index_reads_every_absent_path_unconfirmed() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/w/r", ON, Some(b"DIRC\0\0\0\x09".to_vec()));
    let fs = Moving::new(fs);
    let mut memo = SkipMemo::new();
    with(&fs, Flavour::Posix, |rule| {
        assert_eq!(
            absent_by_sparsity(rule, &Env::default(), &same, &mut memo, b"/w/r/gone"),
            Ok(true)
        );
        // The working tree itself is no path below it.
        assert_eq!(
            absent_by_sparsity(rule, &Env::default(), &same, &mut memo, b"/w/r"),
            Ok(false)
        );
    });
}

#[test]
fn the_windows_flavour_declines() {
    let mut fs = MemFs::new(Flavour::Windows);
    fs.insert(b"C:\\w", Node::Dir);
    let fs = Moving::new(fs);
    let mut memo = SkipMemo::new();
    with(&fs, Flavour::Windows, |rule| {
        assert_eq!(
            absent_by_sparsity(rule, &Env::default(), &same, &mut memo, b"C:\\w\\x"),
            Err(Failure::NotPorted)
        );
    });
}
