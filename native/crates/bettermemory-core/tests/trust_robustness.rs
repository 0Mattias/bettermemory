//! `gitfile_rule::trust` on hostile trees, on a 512 KiB thread (debug
//! build): typed answers, no panic, a `commondir` read to its bound only;
//! a config of many entries costs linearly (a quadratic pass fails the
//! ratio); the Windows flavour declines; `GIT_COMMON_DIR` is the argument
//! alone.

mod timing;

use std::cell::Cell;

use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind, MemFs, Node, OsError, PyErr, Stat, StatResult,
};
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::{Ctx, Failure};
use bettermemory_core::gitfile_rule::trust::{
    common_of, commondir, foreign_commondir, inside_reading, is_git_directory, object_format_at,
    own_refusal, reads_worktree_config, registered, tree_format, TreeFormat,
};
use bettermemory_core::gitfile_rule::GITFILE_LIMIT;

const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};

fn on_small_stack(work: impl FnOnce() + Send + 'static) {
    std::thread::Builder::new()
        .stack_size(512 * 1024)
        .spawn(work)
        .expect("a thread")
        .join()
        .expect("no panic on a 512 KiB stack");
}

fn ctx(fs: &MemFs) -> Ctx<'_, MemFs> {
    Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: b"/",
    }
}

fn repo(fs: &mut MemFs, at: &str) {
    fs.insert(
        format!("{at}/HEAD").as_bytes(),
        Node::File(b"ref: refs/heads/main\n".to_vec()),
    );
    fs.insert(format!("{at}/objects").as_bytes(), Node::Dir);
    fs.insert(format!("{at}/refs").as_bytes(), Node::Dir);
}

/// Counts the bytes handed out by reads.
struct Counting {
    inner: MemFs,
    bytes: Cell<usize>,
}

impl Fs for Counting {
    type File = <MemFs as Fs>::File;
    fn open(&self, path: &[u8]) -> Result<Self::File, PyErr> {
        self.inner.open(path)
    }
    fn open_result(&self, path: &[u8]) -> Result<Self::File, OsError> {
        self.inner.open_result(path)
    }
    fn fstat(&self, file: &mut Self::File) -> Result<Stat, PyErr> {
        self.inner.fstat(file)
    }
    fn read(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, PyErr> {
        let got = self.inner.read(file, max)?;
        self.bytes.set(self.bytes.get() + got.len());
        Ok(got)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.inner.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<Self::File, OsError> {
        self.inner.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
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
fn hostile_trees_answer_typed_on_a_small_stack() {
    on_small_stack(|| {
        let mut fs = MemFs::new(Flavour::Posix);
        repo(&mut fs, "/m");
        repo(&mut fs, "/w");
        fs.insert(b"/w/commondir", Node::Endless);
        fs.insert(b"/w/config", Node::Endless);
        fs.insert(b"/loop", Node::Link(b"/loop".to_vec()));
        fs.insert(b"/l/commondir", Node::Link(b"/loop".to_vec()));
        let mut chain = b"/m".to_vec();
        for i in 0..200 {
            let name = format!("/c{i}");
            fs.insert(name.as_bytes(), Node::Link(chain.clone()));
            chain = name.into_bytes();
        }
        let c = ctx(&fs);
        let long = b"/a".repeat(5000);
        for path in [
            &b""[..],
            b".",
            b"/",
            b"/w",
            b"/l",
            b"/loop",
            b"/\xff\xfe",
            &chain,
            &long,
        ] {
            let _ = tree_format(&c, path, None, &GIT).expect("ported");
            let _ = own_refusal(&c, path, b"/", None, &GIT).expect("ported");
            let _ = inside_reading(&c, path, &GIT).expect("ported");
            let _ = reads_worktree_config(&c, path, &GIT).expect("ported");
            let _ = object_format_at(&c, path, &GIT).expect("ported");
            let _ = common_of(&c, path, None).expect("ported");
            let _ = foreign_commondir(&c, path).expect("ported");
            let _ = is_git_directory(&c, path, Some(path)).expect("ported");
            let _ = registered(&c, path, b"/m").expect("ported");
        }
        // A NUL in the git directory's path: `open` raises ValueError,
        // which `_commondir` does not catch.
        assert_eq!(commondir(&c, b"/w\x00"), Err(Failure::Value));
        assert_eq!(is_git_directory(&c, b"/w\x00", None), Ok(false));
        // An endless commondir is read to the bound and cut there.
        let got = commondir(&c, b"/w").expect("ported").expect("named");
        assert_eq!(got.len(), b"/w/".len() + GITFILE_LIMIT, "cut at the bound");
    });
}

#[test]
fn a_commondir_is_read_to_its_bound_only() {
    let mut inner = MemFs::new(Flavour::Posix);
    let mut big = b"../m".to_vec();
    big.resize(50 << 20, b'/');
    inner.insert(b"/w/commondir", Node::File(big));
    let fs = Counting {
        inner,
        bytes: Cell::new(0),
    };
    let c = Ctx {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/",
    };
    assert_eq!(commondir(&c, b"/w"), Ok(Some(b"/m".to_vec())));
    assert!(fs.bytes.get() <= GITFILE_LIMIT, "{}", fs.bytes.get());
}

/// `config.worktree` of 3,000 entries costs about 10 times one of 300
/// (under 60 times plus 50 ms on the least times seen, `timing`): a pass over the entries per
/// entry would be 100 times and fails after one run of the larger tree,
/// in a few seconds.
#[test]
fn many_entries_cost_linearly() {
    fn tree(n: usize) -> MemFs {
        let mut fs = MemFs::new(Flavour::Posix);
        repo(&mut fs, "/g");
        fs.insert(
            b"/g/config",
            Node::File(
                b"[core]\n\trepositoryformatversion = 1\n[extensions]\n\tworktreeconfig\n".to_vec(),
            ),
        );
        let mut text = b"[core]\n".to_vec();
        for i in 0..n {
            text.extend_from_slice(if i % 2 == 0 {
                b"\tbare = false\n"
            } else {
                b"\tworktree = /g/w\n"
            });
        }
        fs.insert(b"/g/config.worktree", Node::File(text));
        fs
    }
    let once = |fs: &MemFs| {
        let c = ctx(fs);
        timing::once(|| {
            assert_eq!(
                tree_format(&c, b"/g", None, &GIT),
                Ok(TreeFormat {
                    why: None,
                    worktree: Some(b"/g/w".to_vec()),
                    bare: false,
                })
            )
        })
    };
    let (small, large) = (tree(300), tree(3_000));
    timing::grows_within(
        "300 and 3,000 entries",
        timing::TENFOLD,
        &mut || once(&small),
        &mut || once(&large),
    );
}

#[test]
fn the_windows_flavour_declines() {
    let mut fs = MemFs::new(Flavour::Windows);
    fs.insert(b"C:/g/HEAD", Node::File(b"ref: refs/heads/main\n".to_vec()));
    let c = Ctx {
        fs: &fs,
        flavour: Flavour::Windows,
        cwd: b"C:/",
    };
    let no = Failure::NotPorted;
    assert_eq!(tree_format(&c, b"C:/g", None, &GIT), Err(no));
    assert_eq!(own_refusal(&c, b"C:/g", b"C:/", None, &GIT), Err(no));
    assert_eq!(inside_reading(&c, b"C:/g", &GIT), Err(no));
    assert_eq!(reads_worktree_config(&c, b"C:/g", &GIT), Err(no));
    assert_eq!(object_format_at(&c, b"C:/g", &GIT), Err(no));
    assert_eq!(common_of(&c, b"C:/g", None), Err(no));
    assert_eq!(commondir(&c, b"C:/g"), Err(no));
    assert_eq!(foreign_commondir(&c, b"C:/g"), Err(no));
    assert_eq!(registered(&c, b"C:/g", b"C:/"), Err(no));
    assert_eq!(is_git_directory(&c, b"C:/g", None), Err(no));
}

/// `GIT_COMMON_DIR` is the argument alone: an empty one is unset, a
/// relative one is taken from the git directory, lexically.
#[test]
fn the_common_dir_is_an_argument() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/m");
    repo(&mut fs, "/w");
    let c = ctx(&fs);
    assert_eq!(common_of(&c, b"/w", None), Ok((b"/w".to_vec(), false)));
    assert_eq!(common_of(&c, b"/w", Some(b"")), Ok((b"/w".to_vec(), false)));
    assert_eq!(
        common_of(&c, b"/w", Some(b"../m")),
        Ok((b"/m".to_vec(), true))
    );
    assert_eq!(
        common_of(&c, b"/w", Some(b"/x/../m")),
        Ok((b"/m".to_vec(), true))
    );
}
