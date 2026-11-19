//! `gitfile_rule::follow` on hostile and changing trees, on a 512 KiB
//! thread (debug build): typed answers, no panic; a pin re-read on every
//! call, so a tree changed underneath is judged as it is now; a recorded
//! tree or a view that does not answer refuses or takes the record; a
//! record nested past the decoder's limit rises as RecursionError; the
//! store's views cost linearly (a quadratic pass fails in a few seconds);
//! the Windows flavour declines.

mod timing;

use std::collections::BTreeSet;

use bettermemory_core::fsdata::{
    samestat, Access, Flavour, Fs, Kind, MemFs, Node, OsError, PyErr, Stat, StatResult,
};
use bettermemory_core::gitfile_rule::bounded::Hung;
use bettermemory_core::gitfile_rule::follow::{
    earliest_naming, entry_names, fsencode, gitfile_refusal, names_git_dir, recorded, refusal,
    shown, Names, NoRecords, Records,
};
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::{Ctx, Failure};
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
    fs.insert(
        format!("{at}/config").as_bytes(),
        Node::File(b"[core]\n\trepositoryformatversion = 0\n".to_vec()),
    );
}

/// Records by the git directory's path, looked up by its stat now.
struct ByPath<'a> {
    fs: &'a MemFs,
    records: Vec<(Vec<u8>, Vec<u8>)>,
}

impl Records for ByPath<'_> {
    fn record(&self, git_dir: &StatResult) -> Option<Vec<u8>> {
        self.records.iter().find_map(|(path, data)| {
            let st = self.fs.stat(path).ok()?;
            (samestat(&st, git_dir) == Some(true)).then(|| data.clone())
        })
    }
}

fn pin(git_dir: &str, tree: &str) -> Vec<u8> {
    format!("{{\"format\": 1, \"git_dir\": \"{git_dir}\", \"tree\": \"{tree}\"}}\n").into_bytes()
}

fn separate() -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/sep.git");
    fs.insert(b"/a/.git", Node::File(b"gitdir: /sep.git\n".to_vec()));
    fs.insert(b"/b/.git", Node::File(b"gitdir: /sep.git\n".to_vec()));
    fs
}

fn refuse(fs: &MemFs, records: &dyn Records, directory: &[u8]) -> Option<Vec<u8>> {
    gitfile_refusal(
        &ctx(fs),
        &Hung::default(),
        records,
        directory,
        b"/sep.git",
        None,
        &GIT,
    )
    .expect("ported")
    .map(|r| r.why)
}

#[test]
fn hostile_trees_answer_typed_on_a_small_stack() {
    on_small_stack(|| {
        let mut fs = separate();
        fs.insert(b"/loop", Node::Link(b"/loop".to_vec()));
        fs.insert(b"/l/.git", Node::Link(b"/loop".to_vec()));
        fs.insert(b"/e/.git", Node::Endless);
        let mut chain = b"/sep.git".to_vec();
        for i in 0..200 {
            let name = format!("/c{i}");
            fs.insert(name.as_bytes(), Node::Link(chain.clone()));
            chain = name.into_bytes();
        }
        let c = ctx(&fs);
        let long = b"/a".repeat(5000);
        let paths: Vec<&[u8]> = vec![
            b"", b".", b"/", b"/a", b"/l", b"/loop", b"/e", b"/\xff", &chain, &long,
        ];
        for &dir in &paths {
            for &target in &paths {
                let _ = refusal(
                    &c,
                    &Hung::default(),
                    &NoRecords,
                    dir,
                    target,
                    None,
                    None,
                    &GIT,
                )
                .expect("ported");
                let _ = entry_names(&c, &[dir, b"/.git"].concat(), target).expect("ported");
            }
        }
        assert_eq!(entry_names(&c, b"/e/.git", b"/sep.git"), Ok(Names::Other));
        // A loop is ELOOP, which `path_gone` reads as gone.
        assert_eq!(entry_names(&c, b"/l/.git", b"/sep.git"), Ok(Names::Gone));
        assert_eq!(names_git_dir(&c, b"/a/.git", b"/sep.git"), Ok(true));
        let views: Vec<Vec<u8>> = paths.iter().map(|p| p.to_vec()).collect();
        let _ = earliest_naming(&c, &Hung::default(), &views, Some(b"/b"), b"/sep.git")
            .expect("ported");
    });
}

/// Every call reads the record and the recorded tree afresh: a tree whose
/// `.git` is rewritten, removed and remade, or reached by a link swapped,
/// is judged as it is at the call.
#[test]
fn a_tree_changed_underneath_is_judged_as_it_is_now() {
    let mut fs = separate();
    let record = pin("/sep.git", "/a");
    let refused = b"/sep.git is the git directory of the working tree /a".to_vec();
    assert_eq!(refuse(&fs, &by(&fs, &record), b"/b"), Some(refused.clone()));
    // Rewritten to another target: gives way.
    repo(&mut fs, "/other.git");
    fs.insert(b"/a/.git", Node::File(b"gitdir: /other.git\n".to_vec()));
    assert_eq!(refuse(&fs, &by(&fs, &record), b"/b"), None);
    // Remade naming it again: holds again.
    fs.insert(b"/a/.git", Node::File(b"gitdir: /sep.git\n".to_vec()));
    assert_eq!(refuse(&fs, &by(&fs, &record), b"/b"), Some(refused.clone()));
    // The same length, other bytes.
    fs.insert(b"/a/.git", Node::File(b"gitdir: /sep.gix\n".to_vec()));
    assert_eq!(refuse(&fs, &by(&fs, &record), b"/b"), None);
    // A link at the recorded tree's `.git`, swapped.
    fs.insert(b"/a/.git", Node::Link(b"/sep.git".to_vec()));
    assert_eq!(refuse(&fs, &by(&fs, &record), b"/b"), Some(refused.clone()));
    fs.insert(b"/a/.git", Node::Link(b"/other.git".to_vec()));
    assert_eq!(refuse(&fs, &by(&fs, &record), b"/b"), None);
    // The git directory removed and remade: another object, the record
    // names it no more.
    fs.insert(b"/a/.git", Node::File(b"gitdir: /sep.git\n".to_vec()));
    let before = fs.stat(b"/sep.git").unwrap();
    let stale = by(&fs, &record);
    assert!(recorded(&ctx(&fs), &stale, b"/sep.git").unwrap().is_some());
    struct Frozen(StatResult, Vec<u8>);
    impl Records for Frozen {
        fn record(&self, st: &StatResult) -> Option<Vec<u8>> {
            (samestat(&self.0, st) == Some(true)).then(|| self.1.clone())
        }
    }
    let frozen = Frozen(before, pin("/old.git", "/a"));
    assert_eq!(recorded(&ctx(&fs), &frozen, b"/sep.git"), Ok(None));
}

/// The record `data` for `/sep.git`, read against `fs` as it is now.
fn by<'a>(fs: &'a MemFs, data: &[u8]) -> ByPath<'a> {
    ByPath {
        fs,
        records: vec![(b"/sep.git".to_vec(), data.to_vec())],
    }
}

#[test]
fn a_recorded_tree_or_view_that_does_not_answer() {
    let fs = separate();
    let records = ByPath {
        fs: &fs,
        records: vec![(b"/sep.git".to_vec(), pin("/sep.git", "/a"))],
    };
    let hung = Hung([b"/a".to_vec()].into_iter().collect::<BTreeSet<_>>());
    let c = ctx(&fs);
    let (why, provisional) =
        refusal(&c, &hung, &records, b"/b", b"/sep.git", None, None, &GIT).expect("ported");
    assert_eq!(
        why.map(|r| r.why),
        Some(b"/sep.git is recorded as the git directory of the working tree /a, which did not answer".to_vec())
    );
    assert!(!provisional);
    // From the recorded folder itself no read is made.
    assert_eq!(
        refusal(&c, &hung, &records, b"/a", b"/sep.git", None, None, &GIT),
        Ok((None, false))
    );
    // A view that does not answer takes the record.
    let views = vec![b"/a".to_vec(), b"/b".to_vec()];
    assert_eq!(
        earliest_naming(&c, &hung, &views, Some(b"/b"), b"/sep.git"),
        Ok(Some(b"/a".to_vec()))
    );
}

#[test]
fn a_record_nested_too_deep_rises() {
    let fs = separate();
    let mut deep = vec![b'['; 10_000];
    deep.resize(20_000, b']');
    let records = ByPath {
        fs: &fs,
        records: vec![(b"/sep.git".to_vec(), deep)],
    };
    assert_eq!(
        gitfile_refusal(
            &ctx(&fs),
            &Hung::default(),
            &records,
            b"/b",
            b"/sep.git",
            None,
            &GIT
        ),
        Err(Failure::Raised("RecursionError"))
    );
}

/// 1,000 views cost about 10 times 100 (under 60 times plus 50 ms on the
/// least times seen, `timing`): a pass over the views per view would be
/// 100 times.
#[test]
fn many_views_cost_linearly() {
    fn tree(n: usize) -> (MemFs, Vec<Vec<u8>>) {
        let mut fs = separate();
        let mut views = Vec::new();
        for i in 0..n {
            let v = format!("/v{i}");
            fs.insert(format!("{v}/f").as_bytes(), Node::File(Vec::new()));
            views.push(v.into_bytes());
        }
        (fs, views)
    }
    let once = |(fs, views): &(MemFs, Vec<Vec<u8>>)| {
        timing::once(|| {
            assert_eq!(
                earliest_naming(&ctx(fs), &Hung::default(), views, Some(b"/b"), b"/sep.git"),
                Ok(None)
            )
        })
    };
    let (small, large) = (tree(100), tree(1_000));
    timing::grows_within(
        "100 and 1,000 views",
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
    let no = Err(Failure::NotPorted);
    assert_eq!(
        refusal(
            &c,
            &Hung::default(),
            &NoRecords,
            b"C:/w",
            b"C:/g",
            None,
            None,
            &GIT
        ),
        no
    );
    assert_eq!(
        entry_names(&c, b"C:/w/.git", b"C:/g"),
        Err(Failure::NotPorted)
    );
    assert_eq!(recorded(&c, &NoRecords, b"C:/g"), Err(Failure::NotPorted));
    assert_eq!(
        earliest_naming(&c, &Hung::default(), &[b"C:/w".to_vec()], None, b"C:/g"),
        Err(Failure::NotPorted)
    );
}

/// A FIFO, a socket or a device at `.git` names nothing and is not read;
/// a provider's placeholder is not read either, and cannot be told.
#[test]
fn entries_that_are_not_read() {
    use bettermemory_core::fsdata::SF_DATALESS;
    let mut fs = separate();
    fs.insert(b"/p/.git", Node::Fifo);
    fs.insert(b"/s/.git", Node::Socket);
    fs.insert(b"/z/.git", Node::Endless);
    fs.insert(b"/h/.git", Node::File(b"gitdir: /sep.git\n".to_vec()));
    fs.set_flags(b"/h/.git", SF_DATALESS);
    let c = ctx(&fs);
    for entry in [&b"/p/.git"[..], b"/s/.git", b"/z/.git"] {
        assert_eq!(entry_names(&c, entry, b"/sep.git"), Ok(Names::Other));
    }
    assert_eq!(entry_names(&c, b"/h/.git", b"/sep.git"), Ok(Names::Unknown));
}

/// `os.fsencode` escapes U+DC80-U+DCFF alone: U+DC7F, just below, raises
/// as U+D800 does.
#[test]
fn the_escaped_range_and_its_edges() {
    assert_eq!(fsencode(&[0x2F, 0xDC80]), Some(vec![b'/', 0x80]));
    assert_eq!(fsencode(&[0x2F, 0xDCFF]), Some(vec![b'/', 0xFF]));
    assert_eq!(fsencode(&[0x2F, 0xDC7F]), None);
    assert_eq!(fsencode(&[0x2F, 0xDD00]), None);
    assert_eq!(fsencode(&[0x2F, 0xD800]), None);
    // A reason keeps a surrogate it cannot encode, as the reference's
    // f-string keeps it in the `str`, in UTF-8's three-byte form.
    assert_eq!(
        shown(&[0x2F, 0xD800, 0xDCC3]),
        b"/\xed\xa0\x80\xc3".to_vec()
    );
}

/// A recorded tree that cannot be encoded and does not answer: the reason
/// names it with its surrogate kept.
#[test]
fn an_unencodable_tree_that_does_not_answer() {
    let fs = separate();
    let records = by(&fs, &pin("/sep.git", "/x\\ud800"));
    let hung = Hung(
        [b"/x\xed\xa0\x80".to_vec()]
            .into_iter()
            .collect::<BTreeSet<_>>(),
    );
    let (why, provisional) = refusal(
        &ctx(&fs),
        &hung,
        &records,
        b"/b",
        b"/sep.git",
        None,
        None,
        &GIT,
    )
    .expect("ported");
    assert_eq!(
        why.map(|r| r.why),
        Some(b"/sep.git is recorded as the git directory of the working tree /x\xed\xa0\x80, which did not answer".to_vec())
    );
    assert!(!provisional);
}

/// `MemFs` with reads cut to `chunk` bytes, and `stat` of the paths in
/// `missing` answering ENOENT.
struct Odd {
    inner: MemFs,
    chunk: usize,
    missing: Vec<Vec<u8>>,
}

impl Fs for Odd {
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
        self.inner.read(file, max.min(self.chunk))
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
        if self.missing.iter().any(|m| m == path) {
            return Err(OsError::new(PyErr::FileNotFound, "ENOENT"));
        }
        self.inner.stat(path)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.inner.lstat_result(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.inner.listdir(path)
    }
}

/// A `.git` file read in short pieces is bounded as one read whole: one
/// past the bound names nothing, one at it is read.
#[test]
fn a_gitfile_read_in_short_pieces() {
    let mut inner = MemFs::new(Flavour::Posix);
    repo(&mut inner, "/g");
    let mut over = b"gitdir: /g".to_vec();
    over.resize(GITFILE_LIMIT + 1, b' ');
    inner.insert(b"/big/.git", Node::File(over));
    let mut at = b"gitdir: /g".to_vec();
    at.resize(GITFILE_LIMIT, b' ');
    inner.insert(b"/edge/.git", Node::File(at));
    let fs = Odd {
        inner,
        chunk: 4096,
        missing: Vec::new(),
    };
    let c = Ctx {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/",
    };
    assert_eq!(entry_names(&c, b"/big/.git", b"/g"), Ok(Names::Other));
    assert_eq!(entry_names(&c, b"/edge/.git", b"/g"), Ok(Names::Names));
}

/// A `.git` link whose owner working tree reaches nothing when compared
/// (`_same_folder` None, not False) names nothing: `is not True`.
#[test]
fn a_link_whose_owner_reaches_nothing() {
    let mut inner = MemFs::new(Flavour::Posix);
    repo(&mut inner, "/own/.git");
    inner.insert(b"/own/f", Node::File(Vec::new()));
    inner.insert(b"/l/.git", Node::Link(b"/own/.git".to_vec()));
    let fs = Odd {
        inner,
        chunk: usize::MAX,
        missing: vec![b"/own".to_vec()],
    };
    let c = Ctx {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/",
    };
    assert_eq!(entry_names(&c, b"/l/.git", b"/own/.git"), Ok(Names::Other));
}
