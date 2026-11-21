//! Inputs a plausible wrong `location_discovery` answers otherwise than
//! the reference: the order of the `GIT_DIR` test and `DiscoveryDies`,
//! the lazy walk, `refused` None, the unnormalized start a `GIT_DIR`
//! roots at, the refusal written before `GitDies`, a stat that fails, a
//! NUL in a probed directory, the Windows flavour's decline, a relative
//! `GIT_WORK_TREE` joined to the start, a directory git dies on passed by
//! the walk, a placeholder HEAD `_at_dotgit` does not fetch and the
//! commondir read only with a list. Every
//! tree is in memory (`fsdata::MemFs`), so these run on every platform.

use bettermemory_core::capture::Kind;
use std::cell::RefCell;

use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind as FileKind, MemFs, Node, OsError, PyErr, Stat, StatResult,
    SF_DATALESS,
};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::{Ctx, Failure};
use bettermemory_core::location_discovery::{
    discover, from_env, Dies, Env, Raised, Refused, DISCOVERY_DIES,
};
use bettermemory_core::location_git::Git;

const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};

fn repo(fs: &mut MemFs, at: &[u8], config: &[u8], head: &[u8]) {
    let mut parts = Vec::new();
    for (i, &c) in at.iter().enumerate() {
        if c == b'/' && i > 0 {
            parts.push(at[..i].to_vec());
        }
    }
    for p in parts {
        fs.insert(&p, Node::Dir);
    }
    fs.insert(at, Node::Dir);
    let j = |n: &[u8]| [at, b"/", n].concat();
    fs.insert(&j(b"HEAD"), Node::File(head.to_vec()));
    fs.insert(&j(b"config"), Node::File(config.to_vec()));
    fs.insert(&j(b"objects"), Node::Dir);
    fs.insert(&j(b"refs"), Node::Dir);
}

const CONFIG: &[u8] = b"[core]\n\trepositoryformatversion = 0\n\tbare = false\n";
const HEAD: &[u8] = b"ref: refs/heads/main\n";

fn tree() -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, b"/s/repo/.git", CONFIG, HEAD);
    fs.insert(b"/s/repo/sub", Node::Dir);
    fs
}

type Probed = std::vec::IntoIter<Result<Vec<u8>, Failure>>;

fn run(
    fs: &MemFs,
    flavour: Flavour,
    env: Env<'_>,
    start: &[u8],
    across: Option<bool>,
    dirs: &[&[u8]],
    refused: Option<&mut Vec<Refused>>,
) -> (Result<Option<Git>, Raised>, bool) {
    let ctx = Ctx {
        fs,
        flavour,
        cwd: b"/s",
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    let mut called = false;
    let list: Vec<Result<Vec<u8>, Failure>> = dirs.iter().map(|d| Ok(d.to_vec())).collect();
    let got = discover(
        &rule,
        &env,
        start,
        across,
        |_: &[u8], _: Option<u64>| -> Result<Probed, Failure> {
            called = true;
            Ok(list.into_iter())
        },
        refused,
        true,
    );
    (got, called)
}

#[test]
fn git_dir_set_is_read_before_the_across_value_dies() {
    let fs = tree();
    let env = Env {
        git_dir: Some(b"/s/repo/.git"),
        work_tree: None,
    };
    let (got, called) = run(&fs, Flavour::Posix, env, b"/s/repo/sub", None, &[], None);
    let git = got.unwrap().unwrap();
    assert_eq!(git.root.as_deref(), Some(&b"/s/repo/sub"[..]));
    assert_eq!(git.dotgit, None);
    assert!(!called);
}

#[test]
fn across_none_dies_before_any_read_even_where_start_is_missing() {
    let fs = MemFs::new(Flavour::Posix);
    for flavour in [Flavour::Posix, Flavour::Windows] {
        let env = Env {
            git_dir: Some(b""),
            work_tree: None,
        };
        let (got, called) = run(&fs, flavour, env, b"/nowhere", None, &[], None);
        assert_eq!(got, Err(Raised::Dies(Dies::Discovery)));
        assert!(!called);
    }
    assert_eq!(
        Dies::Discovery.text(),
        b"GIT_DISCOVERY_ACROSS_FILESYSTEM holds no boolean git reads: git dies on it"
    );
    assert_eq!(Dies::Discovery.text(), DISCOVERY_DIES);
}

#[test]
fn a_missing_start_raises_its_stat_unless_across() {
    let fs = MemFs::new(Flavour::Posix);
    let (got, called) = run(
        &fs,
        Flavour::Posix,
        Env::default(),
        b"/nowhere",
        Some(false),
        &[],
        None,
    );
    assert!(
        matches!(got, Err(Raised::Failure(Failure::Os(_)))),
        "{got:?}"
    );
    assert!(!called);
    let (got, called) = run(
        &fs,
        Flavour::Posix,
        Env::default(),
        b"/nowhere",
        Some(true),
        &[b"/nowhere"],
        None,
    );
    assert_eq!(got, Ok(None));
    assert!(called);
}

#[test]
fn the_walk_stops_at_the_first_repository() {
    let fs = tree();
    let ctx = Ctx {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/s",
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    let taken = std::cell::Cell::new(0);
    let got = discover(
        &rule,
        &Env::default(),
        b"/s/repo",
        Some(true),
        |here: &[u8], dev: Option<u64>| {
            assert_eq!(here, b"/s/repo");
            assert_eq!(dev, None);
            Ok((0..).map(|i| {
                taken.set(taken.get() + 1);
                assert!(i == 0, "the walk went past the repository");
                Ok(b"/s/repo".to_vec())
            }))
        },
        None,
        true,
    );
    assert_eq!(got.unwrap().unwrap().kind, Kind::Primary);
    assert_eq!(taken.get(), 1);
}

#[test]
fn a_failure_of_the_walk_rises() {
    let fs = tree();
    let ctx = Ctx {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/s",
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    let got = discover(
        &rule,
        &Env::default(),
        b"/s/repo/sub",
        Some(true),
        |_: &[u8], _: Option<u64>| -> Result<Probed, Failure> { Err(Failure::Value) },
        None,
        true,
    );
    assert_eq!(got, Err(Raised::Failure(Failure::Value)));
}

#[test]
fn a_foreign_commondir_is_refused_only_with_a_list() {
    let mut fs = tree();
    fs.insert(b"/s/x", Node::Dir);
    fs.insert(b"/s/x/HEAD", Node::File(b"junk\n".to_vec()));
    fs.insert(b"/s/x/commondir", Node::File(b"/s/repo/.git\n".to_vec()));
    let mut refused = Vec::new();
    let (got, _) = run(
        &fs,
        Flavour::Posix,
        Env::default(),
        b"/s/x",
        Some(true),
        &[b"/s/x"],
        Some(&mut refused),
    );
    assert_eq!(got, Ok(None));
    assert_eq!(refused.len(), 1);
    assert_eq!(refused[0].git_dir, b"/s/repo/.git");
    assert_eq!(
        refused[0].why.why,
        b"/s/x names the common directory /s/repo/.git but is not one of its worktrees"
    );
    assert!(!refused[0].why.format_unknown);
}

#[test]
fn an_unknown_format_is_refused_then_dies() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(
        &mut fs,
        b"/s/u/.git",
        b"[core]\n\trepositoryformatversion = 2\n",
        HEAD,
    );
    let mut refused = Vec::new();
    let (got, _) = run(
        &fs,
        Flavour::Posix,
        Env::default(),
        b"/s/u",
        Some(true),
        &[b"/s/u"],
        Some(&mut refused),
    );
    let why = b"/s/u/.git's config cannot be read as git reads it: /s/u/.git/config sets a repository format version git does not read".to_vec();
    assert_eq!(got, Err(Raised::Dies(Dies::Git(why.clone()))));
    assert_eq!(refused.len(), 1);
    assert_eq!(refused[0].why.why, why);
    assert!(refused[0].why.format_unknown);
}

#[test]
fn git_dir_roots_at_the_start_as_given() {
    let fs = tree();
    let ctx = Ctx {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/s",
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    let env = Env {
        git_dir: Some(b"../.git/"),
        work_tree: None,
    };
    let git = from_env(&rule, &env, b"/s/repo/./sub/", None)
        .unwrap()
        .unwrap();
    assert_eq!(git.git_dir, b"/s/repo/.git");
    assert_eq!(git.root.as_deref(), Some(&b"/s/repo/./sub/"[..]));
    let env = Env {
        git_dir: Some(b"../.git"),
        work_tree: Some(b"../x/../wt/"),
    };
    let git = from_env(&rule, &env, b"/s/repo/sub", None)
        .unwrap()
        .unwrap();
    assert_eq!(git.root.as_deref(), Some(&b"/s/repo/wt"[..]));
}

#[test]
fn an_empty_core_worktree_dies_with_its_text() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(
        &mut fs,
        b"/s/c",
        b"[core]\n\trepositoryformatversion = 0\n\tworktree =\n",
        HEAD,
    );
    let ctx = Ctx {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/s",
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    let env = Env {
        git_dir: Some(b"."),
        work_tree: None,
    };
    let mut refused = Vec::new();
    let got = from_env(&rule, &env, b"/s/c", Some(&mut refused));
    let why = b"/s/c's config names no working tree (an empty core.worktree)".to_vec();
    assert_eq!(got, Err(Raised::Dies(Dies::Git(why.clone()))));
    assert_eq!(refused[0].path, b"/s/c");
    assert_eq!(refused[0].git_dir, b"/s/c");
    assert!(!refused[0].why.format_unknown);
    let env = Env {
        git_dir: Some(b"."),
        work_tree: Some(b""),
    };
    assert!(from_env(&rule, &env, b"/s/c", None).is_err());
    let env = Env {
        git_dir: Some(b"."),
        work_tree: Some(b"t"),
    };
    let git = from_env(&rule, &env, b"/s/c", None).unwrap().unwrap();
    assert_eq!(git.root.as_deref(), Some(&b"/s/c/t"[..]));
}

#[test]
fn a_nul_in_a_probed_directory_reads_as_nothing_there() {
    let fs = tree();
    let (got, _) = run(
        &fs,
        Flavour::Posix,
        Env::default(),
        b"/s/repo",
        Some(true),
        &[b"/s/a\0b"],
        None,
    );
    assert_eq!(got, Ok(None));
}

#[test]
fn the_windows_flavour_declines() {
    let fs = MemFs::new(Flavour::Windows);
    let env = Env {
        git_dir: Some(b"C:\\r\\.git"),
        work_tree: None,
    };
    let (got, called) = run(&fs, Flavour::Windows, env, b"C:\\r", Some(true), &[], None);
    assert_eq!(got, Err(Raised::Failure(Failure::NotPorted)));
    assert!(!called);
    let (got, called) = run(
        &fs,
        Flavour::Windows,
        Env::default(),
        b"C:\\r",
        Some(true),
        &[],
        None,
    );
    assert_eq!(got, Err(Raised::Failure(Failure::NotPorted)));
    assert!(!called);
}

#[test]
fn a_relative_git_work_tree_joins_the_start_not_the_git_directory() {
    let mut fs = tree();
    fs.insert(b"/s/x", Node::Dir);
    let env = Env {
        git_dir: Some(b"/s/repo/.git"),
        work_tree: Some(b"tree"),
    };
    let (got, called) = run(&fs, Flavour::Posix, env, b"/s/x", None, &[], None);
    let git = got.unwrap().unwrap();
    assert_eq!(git.git_dir, b"/s/repo/.git");
    assert_eq!(git.root.as_deref(), Some(&b"/s/x/tree"[..]));
    assert!(!called);
}

#[test]
fn a_directory_git_dies_on_is_passed_by_the_walk() {
    // `_git_reads` is None (falsy) where HEAD is a FIFO: `_discover`
    // reads no membership there and walks on to the repository above.
    let mut fs = tree();
    fs.insert(b"/s/repo/sub/HEAD", Node::Fifo);
    fs.insert(b"/s/repo/sub/objects", Node::Dir);
    fs.insert(b"/s/repo/sub/refs", Node::Dir);
    for list in [false, true] {
        let mut refused = Vec::new();
        let (got, _) = run(
            &fs,
            Flavour::Posix,
            Env::default(),
            b"/s/repo/sub",
            Some(true),
            &[b"/s/repo/sub", b"/s/repo"],
            list.then_some(&mut refused),
        );
        let git = got.unwrap().unwrap();
        assert_eq!(git.git_dir, b"/s/repo/.git");
        assert_eq!(git.root.as_deref(), Some(&b"/s/repo"[..]));
        assert!(refused.is_empty());
    }
}

#[test]
fn a_placeholder_head_in_a_dot_git_is_not_fetched() {
    // `_at_dotgit` judges with fetch False: a provider's placeholder
    // HEAD passes unread, so its junk text is not seen (D19).
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, b"/s/p/.git", CONFIG, b"junk\n");
    fs.set_flags(b"/s/p/.git/HEAD", SF_DATALESS);
    let (got, _) = run(
        &fs,
        Flavour::Posix,
        Env::default(),
        b"/s/p",
        Some(true),
        &[b"/s/p"],
        None,
    );
    let git = got.unwrap().unwrap();
    assert_eq!(git.dotgit.as_deref(), Some(&b"/s/p/.git"[..]));
    assert_eq!(git.root.as_deref(), Some(&b"/s/p"[..]));
    assert_eq!(git.kind, Kind::Primary);
    // The same HEAD as a plain file is read and refused: no repository.
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, b"/s/p/.git", CONFIG, b"junk\n");
    let (got, _) = run(
        &fs,
        Flavour::Posix,
        Env::default(),
        b"/s/p",
        Some(true),
        &[b"/s/p"],
        None,
    );
    assert_eq!(got, Ok(None));
}

/// MemFs, recording every path opened.
struct Opens {
    inner: MemFs,
    opened: RefCell<Vec<Vec<u8>>>,
}

impl Fs for Opens {
    type File = <MemFs as Fs>::File;
    fn open(&self, path: &[u8]) -> Result<Self::File, PyErr> {
        self.opened.borrow_mut().push(path.to_vec());
        self.inner.open(path)
    }
    fn open_result(&self, path: &[u8]) -> Result<Self::File, OsError> {
        self.opened.borrow_mut().push(path.to_vec());
        self.inner.open_result(path)
    }
    fn fstat(&self, file: &mut Self::File) -> Result<Stat, PyErr> {
        self.inner.fstat(file)
    }
    fn read(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, PyErr> {
        self.inner.read(file, max)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.inner.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<Self::File, OsError> {
        self.opened.borrow_mut().push(path.to_vec());
        self.inner.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<FileKind, PyErr> {
        self.inner.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.inner.readlink(path)
    }
    fn readlink_result(&self, path: &[u8]) -> Result<Vec<u8>, OsError> {
        self.inner.readlink_result(path)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.inner.stat(path)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.inner.lstat_result(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.inner.listdir(path)
    }
}

#[test]
fn without_a_list_the_commondir_is_never_read() {
    // `refused is not None and os.path.isfile(...)`: with refused None the
    // reference opens no commondir (one that is a FIFO would block it),
    // with a list it reads one and records the refusal.
    let mut inner = tree();
    inner.insert(b"/s/x", Node::Dir);
    inner.insert(b"/s/x/HEAD", Node::File(b"junk\n".to_vec()));
    inner.insert(b"/s/x/commondir", Node::File(b"/s/repo/.git\n".to_vec()));
    let fs = Opens {
        inner,
        opened: RefCell::new(Vec::new()),
    };
    let ctx = Ctx {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/s",
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    for list in [false, true] {
        fs.opened.borrow_mut().clear();
        let mut refused = Vec::new();
        let got = discover(
            &rule,
            &Env::default(),
            b"/s/x",
            Some(true),
            |_: &[u8], _: Option<u64>| -> Result<Probed, Failure> {
                Ok(vec![Ok(b"/s/x".to_vec())].into_iter())
            },
            list.then_some(&mut refused),
            true,
        );
        assert_eq!(got, Ok(None));
        let read = fs
            .opened
            .borrow()
            .iter()
            .any(|p| p.as_slice() == b"/s/x/commondir");
        assert_eq!(read, list, "commondir opened: {read}, with a list: {list}");
        assert_eq!(refused.len(), usize::from(list));
    }
}
