//! Cases of `origin_gitfiles::root` the golden file does not reach: the
//! memo's bound and order, facts kept under one git reused under another
//! while the stamp holds, as the reference keys them, the once-per-path memo, and the declines (the Windows
//! flavour, a file system with no full `fstat`), and the races of a read:
//! stamps that differ, a file gone before the open, a read failing
//! partway, a root whose spelling cannot be read.

mod common;

use std::cell::Cell;

use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind, MemFile, MemFs, Node, OsError, PyErr, Refused, Stat, StatResult,
};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::discovery::Rule;
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use bettermemory_core::githead::discovery::{stamp, Env};
use bettermemory_core::githead::GitDir;
use bettermemory_core::origin_gitfiles::root::{
    config_facts, config_moves_worktree, refused_worktree_root, ConfigMemo, NotADirectorySeen,
    RootFault, CONFIG_LIMIT, CONFIG_MEMO_CAP, NOT_A_DIRECTORY_CAP, WORKTREE_CONFIG_EXTENSION,
};

const NEW: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};
const OLD: GitFacts = GitFacts {
    release: Some((2, 30)),
    reads_compat: Some(true),
};

fn fs_with(files: &[(&str, &[u8])]) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/t", Node::Dir);
    for (path, data) in files {
        fs.insert(path.as_bytes(), Node::File(data.to_vec()));
    }
    fs
}

fn ctx(fs: &MemFs) -> Ctx<'_, MemFs> {
    Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: b"/t",
    }
}

#[test]
fn the_constants_are_the_reference_s() {
    assert_eq!(CONFIG_LIMIT, 1 << 20);
    assert_eq!(CONFIG_MEMO_CAP, 64);
    assert_eq!(NOT_A_DIRECTORY_CAP, 64);
    assert_eq!(WORKTREE_CONFIG_EXTENSION, b"extensions.worktreeconfig");
}

#[test]
fn the_memo_keeps_the_last_sixty_four_and_a_hit_moves_last() {
    let names: Vec<String> = (0..=CONFIG_MEMO_CAP).map(|i| format!("/t/c{i}")).collect();
    let files: Vec<(&str, &[u8])> = names
        .iter()
        .map(|n| (n.as_str(), &b"[core]\n"[..]))
        .collect();
    let fs = fs_with(&files);
    let ctx = ctx(&fs);
    let mut memo = ConfigMemo::new();
    for name in &names[..CONFIG_MEMO_CAP] {
        assert!(config_facts(&mut memo, &ctx, name.as_bytes(), &NEW)
            .unwrap()
            .is_some());
    }
    assert_eq!(memo.len(), CONFIG_MEMO_CAP);
    // A hit on the oldest moves it last, so the next one in goes out
    // second, not first.
    config_facts(&mut memo, &ctx, names[0].as_bytes(), &NEW).unwrap();
    assert_eq!(memo.keys().last().unwrap(), names[0].as_bytes());
    config_facts(&mut memo, &ctx, names[CONFIG_MEMO_CAP].as_bytes(), &NEW).unwrap();
    assert_eq!(memo.len(), CONFIG_MEMO_CAP);
    let keys = memo.keys();
    assert!(keys.contains(&names[0].as_bytes().to_vec()));
    assert!(!keys.contains(&names[1].as_bytes().to_vec()));
    assert_eq!(keys.first().unwrap(), names[2].as_bytes());
    memo.clear();
    assert!(memo.is_empty());
}

#[test]
fn facts_kept_under_one_git_are_reused_under_another() {
    // `branch.autosetupmerge = simple`, which git reads from 2.37: the
    // dies fact turns on the release. The reference keys its memo on the
    // stamp alone, so a git replaced on PATH reads the kept facts until
    // the file changes or the memo is cleared.
    let text = b"[branch]\n\tautosetupmerge = simple\n";
    let fs = fs_with(&[("/t/config", text)]);
    let ctx = ctx(&fs);
    let mut memo = ConfigMemo::new();
    let new = config_facts(&mut memo, &ctx, b"/t/config", &NEW)
        .unwrap()
        .unwrap();
    assert!(!new.1.git_dies, "2.54 reads simple");
    let kept = config_facts(&mut memo, &ctx, b"/t/config", &OLD)
        .unwrap()
        .unwrap();
    assert!(!kept.1.git_dies, "the kept read under 2.54 answers");
    assert_eq!(memo.len(), 1);
    memo.clear();
    let old = config_facts(&mut memo, &ctx, b"/t/config", &OLD)
        .unwrap()
        .unwrap();
    assert!(old.1.git_dies, "2.30 dies on simple");
}

#[test]
fn a_missing_config_sets_nothing_and_is_not_kept() {
    let fs = fs_with(&[]);
    let ctx = ctx(&fs);
    let mut memo = ConfigMemo::new();
    let (stamp, facts) = config_facts(&mut memo, &ctx, b"/t/none", &NEW)
        .unwrap()
        .unwrap();
    assert!(stamp.is_none());
    assert_eq!(facts.names.map(|n| n.len()), Some(0));
    assert!(memo.is_empty());
}

#[test]
fn the_limit_is_inclusive() {
    let mut at = b"[core]\n#".to_vec();
    at.resize(CONFIG_LIMIT, b'x');
    let mut past = at.clone();
    past.push(b'x');
    let fs = fs_with(&[("/t/at", &at), ("/t/past", &past)]);
    let ctx = ctx(&fs);
    let mut memo = ConfigMemo::new();
    assert!(config_facts(&mut memo, &ctx, b"/t/at", &NEW)
        .unwrap()
        .is_some());
    assert!(config_facts(&mut memo, &ctx, b"/t/past", &NEW)
        .unwrap()
        .is_none());
}

#[test]
fn core_worktree_in_either_file_or_a_bare_true_moves_the_tree() {
    let gd = GitDir {
        gitdir: b"/t/r/.git/worktrees/w".to_vec(),
        commondir: b"/t/r/.git".to_vec(),
    };
    for (common, worktree, moves) in [
        (&b"[core]\n\tbare = false\n"[..], None, false),
        (b"[core]\n\tworktree = /x\n", None, true),
        (b"[core]\n\tbare = true\n", None, true),
        (b"[core]\n\tbare\n", None, true),
        (b"[core\n", None, true),
        (b"[core]\n\trepositoryformatversion = 2\n", None, true),
        (b"[core]\n", Some(&b"[core]\n\tworktree = /x\n"[..]), true),
        (b"[core]\n", Some(b"[core]\n\tbare = yes\n"), true),
        (b"[core]\n", Some(b"[x\n"), true),
        (
            b"[core]\n",
            Some(b"[remote \"o\"]\n\tworktree = x\n"),
            false,
        ),
    ] {
        let mut files: Vec<(&str, &[u8])> = vec![("/t/r/.git/config", common)];
        if let Some(w) = worktree {
            files.push(("/t/r/.git/worktrees/w/config.worktree", w));
        }
        let fs = fs_with(&files);
        let ctx = ctx(&fs);
        let mut memo = ConfigMemo::new();
        assert_eq!(
            config_moves_worktree(&mut memo, &ctx, &gd, &NEW),
            Ok(moves),
            "{common:?} {worktree:?}"
        );
    }
}

#[test]
fn the_windows_flavour_declines_the_root() {
    let fs = MemFs::new(Flavour::Windows);
    let ctx = Ctx {
        fs: &fs,
        flavour: Flavour::Windows,
        cwd: b"C:\\t",
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &NEW,
    };
    let mut memo = ConfigMemo::new();
    assert_eq!(
        refused_worktree_root(&mut memo, &rule, &Env::default(), b"C:\\t", false),
        Err(RootFault::NotPorted)
    );
}

#[test]
fn a_path_is_first_once_among_the_last_sixty_four() {
    let mut seen = NotADirectorySeen::new();
    assert!(seen.first(b"/a"));
    assert!(!seen.first(b"/a"));
    for i in 0..NOT_A_DIRECTORY_CAP - 1 {
        assert!(seen.first(format!("/p{i}").as_bytes()));
    }
    // Sixty-four held: /a is still there.
    assert!(!seen.first(b"/a"));
    assert!(seen.first(b"/one-more"));
    // /a was the oldest and went out.
    assert!(seen.first(b"/a"));
    seen.clear();
    assert!(seen.first(b"/p0"));
}

/// `MemFs` that counts opens and can make a read race: the second
/// `fstat` of an open file answers a later mtime, the open after the
/// stamp finds nothing, reads come `chunk` bytes at a time and the second
/// fails, or `F_GETPATH` fails.
/// The times of every stat are distinct (see [`timed`]).
struct Racy {
    inner: MemFs,
    opens: Cell<usize>,
    fstats: Cell<usize>,
    reads: Cell<usize>,
    moves: bool,
    vanishes: bool,
    chunk: usize,
    second_read_fails: bool,
    no_kernel_path: bool,
}

impl Racy {
    fn new(inner: MemFs) -> Racy {
        Racy {
            inner,
            opens: Cell::new(0),
            fstats: Cell::new(0),
            reads: Cell::new(0),
            moves: false,
            vanishes: false,
            chunk: usize::MAX,
            second_read_fails: false,
            no_kernel_path: false,
        }
    }
}

impl Fs for Racy {
    type File = MemFile;
    fn open(&self, path: &[u8]) -> Result<MemFile, PyErr> {
        self.inner.open(path)
    }
    fn open_result(&self, path: &[u8]) -> Result<MemFile, OsError> {
        self.opens.set(self.opens.get() + 1);
        if self.vanishes {
            return Err(OsError::new(PyErr::FileNotFound, "ENOENT"));
        }
        self.inner.open_result(path)
    }
    fn fstat(&self, file: &mut MemFile) -> Result<Stat, PyErr> {
        self.inner.fstat(file)
    }
    fn fstat_full(&self, file: &mut MemFile) -> Option<Result<StatResult, OsError>> {
        self.fstats.set(self.fstats.get() + 1);
        let mut answer = self.inner.fstat_full(file).map(|r| r.map(timed));
        if self.moves && self.fstats.get().is_multiple_of(2) {
            if let Some(Ok(st)) = answer.as_mut() {
                st.mtime_ns += 1;
            }
        }
        answer
    }
    fn read(&self, file: &mut MemFile, max: usize) -> Result<Vec<u8>, PyErr> {
        self.inner.read(file, max)
    }
    fn read_once(&self, file: &mut MemFile, max: usize) -> Result<Vec<u8>, OsError> {
        self.reads.set(self.reads.get() + 1);
        if self.second_read_fails && self.reads.get() == 2 {
            return Err(OsError::new(PyErr::Os, "EIO"));
        }
        self.inner.read_once(file, max.min(self.chunk))
    }
    fn close(&self, file: MemFile) -> Result<(), OsError> {
        self.inner.close(file)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.inner.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<MemFile, OsError> {
        self.inner.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.inner.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.inner.readlink(path)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.inner.stat(path).map(timed)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.inner.lstat_result(path).map(timed)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.inner.listdir(path)
    }
    fn kernel_path(&self, file: &mut MemFile) -> Result<Vec<u8>, Refused> {
        if self.no_kernel_path {
            return Err(Refused::Raised(PyErr::Os));
        }
        self.inner.kernel_path(file)
    }
}

/// Distinct modification and change times, which `MemFs` leaves at 0,
/// so a stamp with its fields out of order differs.
fn timed(mut st: StatResult) -> StatResult {
    st.mtime_ns = 3;
    st.ctime_ns = st.ctime_ns.map(|_| 7);
    st
}

fn racy_ctx(fs: &Racy) -> Ctx<'_, Racy> {
    Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: b"/t",
    }
}

const CONFIG: &[u8] = b"[core]\n\tbare = false\n";

#[test]
fn a_read_whose_stamps_differ_is_answered_but_not_kept() {
    let mut fs = Racy::new(fs_with(&[("/t/config", CONFIG)]));
    fs.moves = true;
    let ctx = racy_ctx(&fs);
    let mut memo = ConfigMemo::new();
    let (stamp, facts) = config_facts(&mut memo, &ctx, b"/t/config", &NEW)
        .unwrap()
        .unwrap();
    assert!(stamp.is_some());
    assert!(facts.names.is_some());
    assert!(memo.is_empty());
}

#[test]
fn the_stamp_kept_is_the_path_stamp_so_a_second_call_does_not_open() {
    let fs = Racy::new(fs_with(&[("/t/config", CONFIG)]));
    let ctx = racy_ctx(&fs);
    let mut memo = ConfigMemo::new();
    let first = config_facts(&mut memo, &ctx, b"/t/config", &NEW)
        .unwrap()
        .unwrap();
    assert_eq!(first.0, stamp(&ctx, b"/t/config", true).unwrap());
    assert_eq!(fs.opens.get(), 1);
    let second = config_facts(&mut memo, &ctx, b"/t/config", &NEW)
        .unwrap()
        .unwrap();
    assert_eq!(fs.opens.get(), 1, "a hit reads nothing");
    assert_eq!(second, first);
}

#[test]
fn a_file_gone_between_the_stat_and_the_open_has_no_config() {
    let mut fs = Racy::new(fs_with(&[("/t/config", CONFIG)]));
    fs.vanishes = true;
    let ctx = racy_ctx(&fs);
    let mut memo = ConfigMemo::new();
    let (stamp, facts) = config_facts(&mut memo, &ctx, b"/t/config", &NEW)
        .unwrap()
        .unwrap();
    assert!(stamp.is_none());
    assert_eq!(facts.names.map(|n| n.len()), Some(0));
    assert!(memo.is_empty());
}

#[test]
fn a_read_that_fails_partway_is_none_not_a_short_config() {
    let mut fs = Racy::new(fs_with(&[("/t/config", CONFIG)]));
    fs.chunk = 4;
    fs.second_read_fails = true;
    let ctx = racy_ctx(&fs);
    let mut memo = ConfigMemo::new();
    assert_eq!(config_facts(&mut memo, &ctx, b"/t/config", &NEW), Ok(None));
    assert!(memo.is_empty());
}

#[test]
fn a_root_whose_spelling_cannot_be_read_is_indeterminate_without_a_root() {
    let repo = fs_with(&[
        ("/t/r/.git/HEAD", b"ref: refs/heads/main\n"),
        ("/t/r/.git/config", CONFIG),
    ]);
    let mut repo = repo;
    repo.insert(b"/t/r/.git/objects", Node::Dir);
    repo.insert(b"/t/r/.git/refs", Node::Dir);
    let run = |no_kernel_path: bool| {
        let mut fs = Racy::new(repo.clone());
        fs.no_kernel_path = no_kernel_path;
        let ctx = racy_ctx(&fs);
        let rule = Rule {
            ctx: &ctx,
            answers: &Hung::default(),
            records: &NoRecords,
            env: None,
            git: &NEW,
        };
        let mut memo = ConfigMemo::new();
        refused_worktree_root(&mut memo, &rule, &Env::default(), b"/t/r", true)
    };
    assert_eq!(run(false), Ok((Some(b"/t/r".to_vec()), true)));
    assert_eq!(run(true), Ok((None, true)));
}
