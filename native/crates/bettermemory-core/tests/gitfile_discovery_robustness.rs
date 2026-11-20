//! `gitfile_rule::discovery` on hostile and changing trees, in memory, on
//! every platform: typed answers and no panic on a 512 KiB thread (debug
//! build); a NUL rises only where the reference raises; a walk asks each
//! folder for two names and nothing else, so its calls are linear in its
//! depth, and it ends from far below the longest path the OS takes; the
//! calls of a walk do not grow with the tree around it, nor those of a
//! link into a crowded tree with that tree (all counted, a listing by the
//! names it returns, so none waits on a clock); `commondir` is read once;
//! a `.git` file is read to its bound; a recorded tree that does not
//! answer refuses through the walk; a record nested past the decoder's
//! limit rises from every function; a tree changed between calls is read
//! as it is now; `lstat` results at the integer boundaries are judged by
//! their kind; a FIFO where the reference's open would wait is not waited
//! on; a relative path under the path limit whose join to `cwd` is over
//! it is answered as the port joins it (a stated difference); the
//! Windows flavour declines. One test is on disk (unix): a
//! spelling in another case or Unicode form, answered as the volume
//! folds it.

use std::cell::{Cell, RefCell};
use std::collections::BTreeSet;

use bettermemory_core::fsdata::{
    samestat, Access, Flavour, Fs, Kind, MemFs, Node, OsError, PyErr, Stat, StatResult,
    JUNCTION_TAG,
};
use bettermemory_core::gitfile_rule::bounded::{Answers, Hung};
use bettermemory_core::gitfile_rule::discovery::{
    discovery_reading, discovery_refusal, entry_reading, judge, reading_at, toplevel_agrees, Entry,
    Reading, Rule,
};
use bettermemory_core::gitfile_rule::follow::{NoRecords, Records};
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::{Ctx, Failure};
use bettermemory_core::gitfile_rule::trust::Refusal;
use bettermemory_core::gitfile_rule::GITFILE_LIMIT;

const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};
const V0: &[u8] = b"[core]\n\trepositoryformatversion = 0\n";

fn on_small_stack(work: impl FnOnce() + Send + 'static) {
    std::thread::Builder::new()
        .stack_size(512 * 1024)
        .spawn(work)
        .expect("a thread")
        .join()
        .expect("no panic on a 512 KiB stack");
}

fn repo(fs: &mut MemFs, at: &str) {
    fs.insert(
        format!("{at}/HEAD").as_bytes(),
        Node::File(b"ref: refs/heads/main\n".to_vec()),
    );
    fs.insert(format!("{at}/objects").as_bytes(), Node::Dir);
    fs.insert(format!("{at}/refs").as_bytes(), Node::Dir);
    fs.insert(format!("{at}/config").as_bytes(), Node::File(V0.to_vec()));
}

fn file(fs: &mut MemFs, path: &str, text: &str) {
    fs.insert(path.as_bytes(), Node::File(text.as_bytes().to_vec()));
}

/// The rule over `fs` from `/`, with no record and nothing hung.
fn with<F: Fs, T>(fs: &F, ask: impl FnOnce(&Rule<'_, F, Hung, NoRecords>) -> T) -> T {
    given(fs, &Hung::default(), &NoRecords, ask)
}

fn given<F: Fs, A: Answers, R: Records, T>(
    fs: &F,
    answers: &A,
    records: &R,
    ask: impl FnOnce(&Rule<'_, F, A, R>) -> T,
) -> T {
    let ctx = Ctx {
        fs,
        flavour: Flavour::Posix,
        cwd: b"/",
    };
    ask(&Rule {
        ctx: &ctx,
        answers,
        records,
        env: None,
        git: &GIT,
    })
}

fn why(text: &str) -> Option<Refusal> {
    Some(Refusal {
        why: text.as_bytes().to_vec(),
        format_unknown: false,
    })
}

fn followed(root: &str, provisional: bool) -> Result<Option<Reading>, Failure> {
    Ok(Some(Reading {
        why: None,
        provisional,
        root: Some(root.as_bytes().to_vec()),
    }))
}

fn refused(text: &str) -> Result<Option<Reading>, Failure> {
    Ok(Some(Reading {
        why: why(text),
        provisional: false,
        root: None,
    }))
}

/// A tree with one of everything a walk can meet.
fn hostile() -> (MemFs, Vec<u8>) {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/sep.git");
    repo(&mut fs, "/own/.git");
    file(&mut fs, "/own/f", "");
    file(&mut fs, "/a/.git", "gitdir: /sep.git\n");
    file(&mut fs, "/junk/.git", "junk\n");
    fs.insert(b"/loop", Node::Link(b"/loop".to_vec()));
    fs.insert(b"/l/.git", Node::Link(b"/loop".to_vec()));
    fs.insert(b"/self/.git", Node::Link(b".git".to_vec()));
    fs.insert(b"/e/.git", Node::Endless);
    fs.insert(b"/p/.git", Node::Fifo);
    fs.insert(b"/s/.git", Node::Socket);
    fs.insert(b"/d/.git", Node::Device);
    fs.insert(b"/w/.git", Node::Link(b"/own/.git".to_vec()));
    let mut chain = b"/own".to_vec();
    for i in 0..200 {
        let name = format!("/c{i}");
        fs.insert(name.as_bytes(), Node::Link(chain.clone()));
        chain = name.into_bytes();
    }
    (fs, chain)
}

#[test]
fn hostile_paths_answer_typed_on_a_small_stack() {
    on_small_stack(|| {
        let (fs, chain) = hostile();
        let long = b"/a".repeat(5000);
        let paths: Vec<&[u8]> = vec![
            b"",
            b".",
            b"..",
            b"/",
            b"//",
            b"/a",
            b"/a/",
            b"/a/.git",
            b"/own",
            b"/own/.git",
            b"/own/.git/objects",
            b"/junk",
            b"/l",
            b"/l/.git",
            b"/self",
            b"/loop",
            b"/e",
            b"/p",
            b"/s",
            b"/d",
            b"/w",
            b"/\xff",
            b"/a\0b",
            b"\0",
            &chain,
            &long,
        ];
        let stats: Vec<StatResult> = [
            &b"/a/.git"[..],
            b"/own/.git",
            b"/l/.git",
            b"/w/.git",
            b"/e/.git",
            b"/p/.git",
            b"/s/.git",
        ]
        .iter()
        .map(|p| fs.lstat_result(p).expect("the entry"))
        .collect();
        // Only a ValueError rises, and only with a NUL in a path given.
        fn typed<T>(got: Result<T, Failure>, nul: bool) {
            match got {
                Ok(_) => {}
                Err(Failure::Value) if nul => {}
                Err(other) => panic!("{other:?}"),
            }
        }
        with(&fs, |rule| {
            for &dir in &paths {
                let nul = dir.contains(&0);
                typed(reading_at(rule, dir), nul);
                typed(discovery_reading(rule, dir), nul);
                typed(discovery_refusal(rule, dir), nul);
                for &other in &paths {
                    let nul = nul || other.contains(&0);
                    typed(toplevel_agrees(rule, dir, other), nul);
                    for st in &stats {
                        typed(entry_reading(rule, dir, other, st), nul);
                        for (fetch, read) in [(true, true), (false, false)] {
                            typed(judge(rule, dir, other, st, fetch, read), nul);
                        }
                    }
                }
            }
            // A link chain past the hops the OS follows: `realpath` has no
            // such bound, so a walk resolves it; the entry below it cannot
            // be stated, so the folder asked directly gives nothing.
            assert_eq!(discovery_reading(rule, &chain), followed("/own", false));
            assert_eq!(reading_at(rule, &chain), Ok(None));
            // Devices and loops name nothing; none is read.
            for folder in [&b"/e"[..], b"/p", b"/s", b"/d", b"/l", b"/self"] {
                assert_eq!(reading_at(rule, folder), Ok(None));
            }
        });
    });
}

/// `gitfile_discovery.jsonl`'s NUL rows, held on every platform: `judge`
/// raises where it resolves or opens the entry (a folder or a file by the
/// `lstat` given), and answers None where the reference catches the error
/// (a link's `stat`, a kind that names nothing, the walk's `realpath`).
#[test]
fn a_nul_rises_only_where_the_reference_raises() {
    let mut fs = MemFs::new(Flavour::Posix);
    file(&mut fs, "/plain/sub/f", "");
    file(&mut fs, "/skip/.git/keep", "");
    fs.insert(b"/dangle/.git", Node::Link(b"/nothing".to_vec()));
    fs.insert(b"/fifo/.git", Node::Fifo);
    let lstat = |p: &[u8]| fs.lstat_result(p).expect("the entry");
    with(&fs, |rule| {
        let ask = |st: &StatResult| judge(rule, b"/plain", b"/pl\0/.git", st, true, true);
        assert_eq!(ask(&lstat(b"/skip/.git")), Err(Failure::Value));
        assert_eq!(ask(&lstat(b"/plain/sub/f")), Err(Failure::Value));
        assert_eq!(ask(&lstat(b"/dangle/.git")), Ok(None));
        assert_eq!(ask(&lstat(b"/fifo/.git")), Ok(None));
        // A link by a junction's tag on a folder: its `stat` raises, and
        // `judge` answers None there, never reading the entry as the
        // folder its `lstat` says (which would rise from `realpath`).
        let junction = StatResult {
            reparse_tag: Some(JUNCTION_TAG),
            ..lstat(b"/skip/.git")
        };
        assert_eq!(ask(&junction), Ok(None));
        let st = lstat(b"/skip/.git");
        assert_eq!(
            judge(rule, b"/pl\0", b"/skip/.git", &st, true, true),
            Ok(None)
        );
        assert_eq!(
            entry_reading(rule, b"/plain", b"/pl\0/.git", &st),
            Err(Failure::Value)
        );
        assert_eq!(reading_at(rule, b"/pl\0ain"), Ok(None));
        assert_eq!(discovery_reading(rule, b"/pl\0ain"), Ok(None));
        assert_eq!(discovery_refusal(rule, b"/pl\0ain"), Ok(None));
        assert_eq!(toplevel_agrees(rule, b"/pl\0ain", b"/plain"), Ok(false));
        assert_eq!(toplevel_agrees(rule, b"/plain", b"/pl\0ain"), Ok(false));
    });
}

/// `MemFs` with every call counted by kind, a listing counted once and
/// once more for each name it returns (a pass over a folder's names costs
/// what the folder holds, however few calls make it), the paths opened
/// kept in order, and `stat` of the paths in `gone` answering ENOENT.
struct Counted<'a> {
    inner: &'a MemFs,
    lstats: Cell<usize>,
    others: Cell<usize>,
    opened: RefCell<Vec<Vec<u8>>>,
    gone: Vec<Vec<u8>>,
}

impl<'a> Counted<'a> {
    fn new(inner: &'a MemFs) -> Counted<'a> {
        Counted {
            inner,
            lstats: Cell::new(0),
            others: Cell::new(0),
            opened: RefCell::new(Vec::new()),
            gone: Vec::new(),
        }
    }

    fn other(&self) {
        self.others.set(self.others.get() + 1);
    }

    fn open_of(&self, path: &[u8]) {
        self.other();
        self.opened.borrow_mut().push(path.to_vec());
    }

    /// How many opens asked for a path ending in `tail`.
    fn opens_of(&self, tail: &[u8]) -> usize {
        self.opened
            .borrow()
            .iter()
            .filter(|path| path.ends_with(tail))
            .count()
    }

    fn lstated(&self) {
        self.lstats.set(self.lstats.get() + 1);
    }
}

impl Fs for Counted<'_> {
    type File = <MemFs as Fs>::File;
    fn open(&self, path: &[u8]) -> Result<Self::File, PyErr> {
        self.open_of(path);
        self.inner.open(path)
    }
    fn open_result(&self, path: &[u8]) -> Result<Self::File, OsError> {
        self.open_of(path);
        self.inner.open_result(path)
    }
    fn fstat(&self, file: &mut Self::File) -> Result<Stat, PyErr> {
        self.other();
        self.inner.fstat(file)
    }
    fn read(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, PyErr> {
        self.other();
        self.inner.read(file, max)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.other();
        self.inner.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<Self::File, OsError> {
        self.open_of(path);
        self.inner.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.lstated();
        self.inner.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.other();
        self.inner.readlink(path)
    }
    fn readlink_result(&self, path: &[u8]) -> Result<Vec<u8>, OsError> {
        self.other();
        self.inner.readlink_result(path)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.other();
        if self.gone.iter().any(|gone| gone == path) {
            return Err(OsError::new(PyErr::FileNotFound, "ENOENT"));
        }
        self.inner.stat(path)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.lstated();
        self.inner.lstat_result(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.other();
        let names = self.inner.listdir(path)?;
        self.others.set(self.others.get() + names.len());
        Ok(names)
    }
    fn holds_other_than(&self, path: &[u8], name: &[u8]) -> Result<bool, OsError> {
        self.other();
        self.inner.holds_other_than(path, name)
    }
}

/// A walk that finds nothing asks `lstat` once per part to resolve its
/// start, then twice per folder up to and with the root (`.git`, `HEAD`),
/// and asks nothing else: `3n + 2` calls from `n` folders deep. Resolving
/// the folder again at each level, or reading a folder that holds neither
/// name, costs more calls than this.
#[test]
fn a_walk_asks_each_folder_for_two_names_and_nothing_else() {
    for n in [1usize, 10, 100, 400] {
        let mut fs = MemFs::new(Flavour::Posix);
        let start = b"/d".repeat(n);
        fs.insert(&start, Node::Dir);
        let counted = Counted::new(&fs);
        assert_eq!(
            with(&counted, |rule| discovery_reading(rule, &start)),
            Ok(None)
        );
        assert_eq!(counted.lstats.get(), 3 * n + 2, "depth {n}");
        assert_eq!(counted.others.get(), 0, "depth {n}");
    }
}

/// A link at `.git` whose owner reaches nothing when it is compared with
/// the folder (`_same_folder` None, not False: the tree went between the
/// two reads) is refused all the same: the test is `is not True`.
#[test]
fn a_link_whose_owner_cannot_be_compared_is_refused() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/own/.git");
    file(&mut fs, "/own/f", "");
    fs.insert(b"/l/.git", Node::Link(b"/own/.git".to_vec()));
    let mut odd = Counted::new(&fs);
    odd.gone.push(b"/own".to_vec());
    let st = fs.lstat_result(b"/l/.git").unwrap();
    assert_eq!(
        with(&odd, |rule| judge(rule, b"/l", b"/l/.git", &st, true, true)),
        Ok(Some(Entry {
            git_dir: b"/own/.git".to_vec(),
            why: why("/l/.git links to the .git of /own"),
            own: false,
            provisional: false,
        }))
    );
    assert_eq!(
        with(&odd, |rule| reading_at(rule, b"/l")),
        refused("/l/.git links to the .git of /own")
    );
}

/// From 5,000 folders below the root, far past the longest path the OS
/// takes (every `lstat` answers ENAMETOOLONG or ENOENT): the walk climbs
/// by the text alone and ends at the root, without recursion.
#[test]
fn a_walk_from_far_below_the_longest_path_ends() {
    on_small_stack(|| {
        let mut fs = MemFs::new(Flavour::Posix);
        fs.insert(b"/d", Node::Dir);
        let start = b"/d".repeat(5000);
        with(&fs, |rule| {
            assert_eq!(discovery_reading(rule, &start), Ok(None));
            assert_eq!(discovery_refusal(rule, &start), Ok(None));
            assert_eq!(toplevel_agrees(rule, &start, b"/d"), Ok(false));
        });
        // A repository at the root is still reached from there.
        repo(&mut fs, "/.git");
        with(&fs, |rule| {
            assert_eq!(discovery_reading(rule, &start), followed("/", false));
        });
    });
}

/// The calls a walk makes do not grow with the tree around it: walks from
/// 100 and from 1,000 sibling folders make exactly 10 and 100 times the
/// calls of walks from 10 (a pass over the siblings per walk would make
/// more, whether by a call per sibling or by one listing, which `Counted`
/// counts by the names it returns). Counted, not timed: the in-memory
/// tree's own cost per call grows with its size, and a count holds on a
/// loaded machine.
#[test]
fn the_calls_of_a_walk_do_not_grow_with_the_tree() {
    fn calls(n: usize) -> usize {
        let mut fs = MemFs::new(Flavour::Posix);
        repo(&mut fs, "/g/sep.git");
        for i in 0..n {
            let body = if i % 2 == 0 {
                "gitdir: /g/sep.git\n"
            } else {
                "junk\n"
            };
            file(&mut fs, &format!("/t{i}/.git"), body);
            file(&mut fs, &format!("/t{i}/in/f"), "");
        }
        let counted = Counted::new(&fs);
        with(&counted, |rule| {
            for i in 0..n {
                let got = discovery_reading(rule, format!("/t{i}/in").as_bytes());
                assert!(matches!(got, Ok(Some(_))), "{i}: {got:?}");
            }
        });
        counted.lstats.get() + counted.others.get()
    }
    let ten = calls(10);
    assert!(ten > 0);
    assert_eq!(calls(100), 10 * ten);
    assert_eq!(calls(1_000), 100 * ten);
}

/// A link at `.git` to another working tree's own `.git` costs the same
/// calls whether that tree holds 10 names or 1,000: its owner is read up
/// to the first name other than `.git`, as the reference's `scandir`
/// under `all` reads it, never listed whole.
#[test]
fn a_link_into_a_crowded_tree_reads_one_name_of_it() {
    fn calls(n: usize) -> usize {
        let mut fs = MemFs::new(Flavour::Posix);
        repo(&mut fs, "/own/.git");
        for i in 0..n {
            file(&mut fs, &format!("/own/f{i}"), "");
        }
        fs.insert(b"/l/.git", Node::Link(b"/own/.git".to_vec()));
        let counted = Counted::new(&fs);
        let st = fs.lstat_result(b"/l/.git").unwrap();
        with(&counted, |rule| {
            let links = why("/l/.git links to the .git of /own");
            assert_eq!(
                judge(rule, b"/l", b"/l/.git", &st, true, true),
                Ok(Some(Entry {
                    git_dir: b"/own/.git".to_vec(),
                    why: links.clone(),
                    own: false,
                    provisional: false,
                }))
            );
            assert_eq!(
                reading_at(rule, b"/l"),
                refused("/l/.git links to the .git of /own")
            );
            assert_eq!(discovery_refusal(rule, b"/l"), Ok(links));
        });
        counted.lstats.get() + counted.others.get()
    }
    assert_eq!(calls(1_000), calls(10));
}

/// `judge` hands the common directory it read to the follow's test of a
/// folder's own `.git`, which reads it no second time: `commondir` is
/// opened once, as the reference opens it (a second read could answer
/// from another state of a tree that changes in between).
#[test]
fn a_folders_own_git_reads_its_commondir_once() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/own/.git");
    file(&mut fs, "/own/f", "");
    let counted = Counted::new(&fs);
    let st = fs.lstat_result(b"/own/.git").unwrap();
    assert_eq!(
        with(&counted, |rule| judge(
            rule,
            b"/own",
            b"/own/.git",
            &st,
            true,
            true
        )),
        Ok(Some(Entry {
            git_dir: b"/own/.git".to_vec(),
            why: None,
            own: true,
            provisional: false,
        }))
    );
    assert_eq!(counted.opens_of(b"/commondir"), 1);
}

/// A `.git` file of `_GITFILE_LIMIT` bytes is read and followed; one byte
/// more names nothing, and git would follow it or die: a refusal.
#[test]
fn a_gitfile_at_the_bound_and_one_past_it() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/g");
    let mut at = b"gitdir: /g".to_vec();
    at.resize(GITFILE_LIMIT, b' ');
    let mut over = at.clone();
    over.push(b' ');
    fs.insert(b"/edge/.git", Node::File(at));
    fs.insert(b"/big/.git", Node::File(over));
    with(&fs, |rule| {
        assert_eq!(reading_at(rule, b"/edge"), followed("/edge", true));
        let st = fs.lstat_result(b"/big/.git").unwrap();
        assert_eq!(
            judge(rule, b"/big", b"/big/.git", &st, true, true),
            Ok(None)
        );
        assert_eq!(
            reading_at(rule, b"/big"),
            refused("/big/.git names no git directory membership follows")
        );
    });
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

fn separate() -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/sep.git");
    file(&mut fs, "/a/.git", "gitdir: /sep.git\n");
    file(&mut fs, "/b/.git", "gitdir: /sep.git\n");
    file(&mut fs, "/b/in/f", "");
    fs
}

fn by(fs: &MemFs, data: Vec<u8>) -> ByPath<'_> {
    ByPath {
        fs,
        records: vec![(b"/sep.git".to_vec(), data)],
    }
}

/// A recorded tree whose read does not answer within the budget refuses
/// the follow, and the refusal reaches every reader of the walk; from the
/// recorded folder itself no read is made.
#[test]
fn a_recorded_tree_that_does_not_answer_refuses_through_the_walk() {
    let fs = separate();
    let records = by(
        &fs,
        b"{\"format\": 1, \"git_dir\": \"/sep.git\", \"tree\": \"/a\"}\n".to_vec(),
    );
    let hung = Hung([b"/a".to_vec()].into_iter().collect::<BTreeSet<_>>());
    let unanswered = "/sep.git is recorded as the git directory of the working tree /a, \
                      which did not answer";
    given(&fs, &hung, &records, |rule| {
        let st = fs.lstat_result(b"/b/.git").unwrap();
        assert_eq!(
            judge(rule, b"/b", b"/b/.git", &st, true, true),
            Ok(Some(Entry {
                git_dir: b"/sep.git".to_vec(),
                why: why(unanswered),
                own: false,
                provisional: false,
            }))
        );
        assert_eq!(reading_at(rule, b"/b"), refused(unanswered));
        assert_eq!(discovery_reading(rule, b"/b/in"), refused(unanswered));
        assert_eq!(discovery_refusal(rule, b"/b/in"), Ok(why(unanswered)));
        assert_eq!(toplevel_agrees(rule, b"/b/in", b"/b"), Ok(false));
        assert_eq!(reading_at(rule, b"/a"), followed("/a", false));
    });
}

#[test]
fn a_record_nested_too_deep_rises_from_every_function() {
    let fs = separate();
    let mut deep = vec![b'['; 10_000];
    deep.resize(20_000, b']');
    let records = by(&fs, deep);
    let rises = Failure::Raised("RecursionError");
    given(&fs, &Hung::default(), &records, |rule| {
        let st = fs.lstat_result(b"/b/.git").unwrap();
        assert_eq!(judge(rule, b"/b", b"/b/.git", &st, true, true), Err(rises));
        assert_eq!(entry_reading(rule, b"/b", b"/b/.git", &st), Err(rises));
        assert_eq!(reading_at(rule, b"/b"), Err(rises));
        assert_eq!(discovery_reading(rule, b"/b/in"), Err(rises));
        assert_eq!(discovery_refusal(rule, b"/b/in"), Err(rises));
        assert_eq!(toplevel_agrees(rule, b"/b/in", b"/b"), Err(rises));
        // The git directory itself is read without its record.
        assert_eq!(reading_at(rule, b"/sep.git"), Ok(Some(Reading::default())));
    });
}

/// Every call reads the tree afresh: nothing is kept between calls.
#[test]
fn a_tree_changed_between_calls_is_read_as_it_is_now() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/top/.git");
    repo(&mut fs, "/top/in/.git");
    file(&mut fs, "/top/in/deep/f", "");
    let walk = |fs: &MemFs| with(fs, |rule| discovery_reading(rule, b"/top/in/deep"));
    assert_eq!(walk(&fs), followed("/top/in", false));
    // Its HEAD made no HEAD: git passes over it, to the repository above.
    file(&mut fs, "/top/in/.git/HEAD", "garbage\n");
    assert_eq!(walk(&fs), followed("/top", false));
    // The same bytes' length, a HEAD again.
    file(&mut fs, "/top/in/.git/HEAD", "ref: refs/heads/main\n");
    assert_eq!(walk(&fs), followed("/top/in", false));
    // A `core.worktree` written into its config.
    file(
        &mut fs,
        "/top/in/.git/config",
        "[core]\n\trepositoryformatversion = 0\n\tworktree = /top\n",
    );
    assert_eq!(
        walk(&fs),
        refused("/top/in/.git's config names the working tree /top (core.worktree)")
    );
    // The entry swapped for a file that names nothing, then for a link
    // to the repository above's own `.git`.
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/top/.git");
    file(&mut fs, "/top/f", "");
    file(&mut fs, "/top/in/deep/f", "");
    assert_eq!(walk(&fs), followed("/top", false));
    file(&mut fs, "/top/in/.git", "junk\n");
    assert_eq!(
        walk(&fs),
        refused("/top/in/.git names no git directory membership follows")
    );
    fs.insert(b"/top/in/.git", Node::Link(b"/top/.git".to_vec()));
    assert_eq!(walk(&fs), refused("/top/in/.git links to the .git of /top"));
}

/// Where the port parts from the reference on purpose (unit 1): the
/// reference opens a worktree's `gitdir` file with a blocking `open`, so a
/// FIFO there holds its walk until a writer comes; the port's open never
/// waits, the FIFO reads as no text, and the follow is refused.
#[test]
fn a_fifo_at_a_worktrees_gitdir_is_not_waited_on() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/main/.git");
    file(
        &mut fs,
        "/main/.git/worktrees/wt/HEAD",
        "ref: refs/heads/main\n",
    );
    file(&mut fs, "/main/.git/worktrees/wt/commondir", "../..\n");
    fs.insert(b"/main/.git/worktrees/wt/gitdir", Node::Fifo);
    file(&mut fs, "/wt/.git", "gitdir: /main/.git/worktrees/wt\n");
    file(&mut fs, "/wt/in/f", "");
    let names_none = "/main/.git/worktrees/wt is a worktree's git directory that names no worktree";
    with(&fs, |rule| {
        assert_eq!(discovery_reading(rule, b"/wt/in"), refused(names_none));
        assert_eq!(toplevel_agrees(rule, b"/wt/in", b"/wt"), Ok(false));
    });
    // The same tree with the file git writes there is followed.
    file(&mut fs, "/main/.git/worktrees/wt/gitdir", "/wt/.git\n");
    with(&fs, |rule| {
        assert_eq!(discovery_reading(rule, b"/wt/in"), followed("/wt", false));
    });
}

/// `judge` reads the kind and the link test of the `lstat` given and
/// nothing else of it: sizes, times and identities at their bounds change
/// no answer; a junction's tag is a link; a result whose tag is not known
/// (the Windows flavour's) is declined.
#[test]
fn lstat_results_at_the_integer_boundaries() {
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, "/own/.git");
    file(&mut fs, "/own/f", "");
    let real = fs.lstat_result(b"/own/.git").unwrap();
    let bent = |kind: Kind, reparse_tag: Option<u32>| StatResult {
        kind,
        mode: Some(u32::MAX),
        ino: Some(u64::MAX),
        dev: Some(u64::MAX),
        size: u64::MAX,
        mtime_ns: i128::MIN,
        ctime_ns: Some(i128::MAX),
        flags: u32::MAX,
        file_attributes: u32::MAX,
        reparse_tag,
    };
    with(&fs, |rule| {
        let ask = |st: &StatResult| judge(rule, b"/own", b"/own/.git", st, true, true);
        let own = Ok(Some(Entry {
            git_dir: b"/own/.git".to_vec(),
            why: None,
            own: true,
            provisional: false,
        }));
        let pointer = Ok(Some(Entry {
            git_dir: b"/own/.git".to_vec(),
            why: None,
            own: false,
            provisional: true,
        }));
        assert_eq!(ask(&real), own);
        assert_eq!(ask(&bent(Kind::Directory, Some(0))), own);
        assert_eq!(ask(&bent(Kind::Directory, Some(u32::MAX))), own);
        // A link by its kind, or by the junction's tag on a folder.
        assert_eq!(ask(&bent(Kind::Link, Some(0))), pointer);
        assert_eq!(ask(&bent(Kind::Link, None)), pointer);
        assert_eq!(ask(&bent(Kind::Directory, Some(JUNCTION_TAG))), pointer);
        assert_eq!(ask(&bent(Kind::Regular, Some(JUNCTION_TAG))), pointer);
        // A file by the `lstat` given, a folder now: its open fails.
        assert_eq!(ask(&bent(Kind::Regular, Some(0))), Ok(None));
        for kind in [Kind::Fifo, Kind::Socket, Kind::Other] {
            assert_eq!(ask(&bent(kind, Some(0))), Ok(None));
        }
        // No tag to tell a junction by: declined, never guessed.
        for kind in [Kind::Directory, Kind::Regular, Kind::Fifo, Kind::Other] {
            assert_eq!(ask(&bent(kind, None)), Err(Failure::NotPorted));
        }
    });
}

/// The tree of `a_spelling_in_another_case_or_form_is_answered_as_the_volume_answers`
/// under `r`: files (path, text) and links (path, target), with `W1/.GIT`
/// and the NFC folder made only where the volume does not fold them into
/// `w1/.git` and the NFD folder (`apart_case`, `apart_form`).
#[cfg(unix)]
fn spelled(r: &str, apart_case: bool, apart_form: bool) -> Vec<(String, Option<String>)> {
    let (nfd, nfc) = ("e\u{301}", "\u{e9}");
    let mut out: Vec<(String, Option<String>)> = Vec::new();
    let mut put = |path: String, text: &str| out.push((path, Some(text.replace("<r>", r))));
    put(format!("{r}/m/.git/HEAD"), "ref: refs/heads/main\n");
    put(format!("{r}/m/.git/objects/.keep"), "");
    put(format!("{r}/m/.git/refs/.keep"), "");
    put(
        format!("{r}/m/.git/config"),
        std::str::from_utf8(V0).unwrap(),
    );
    put(format!("{r}/m/f"), "");
    for (wt, back, folder) in [
        ("w1", "<r>/W1/.GIT", "w1"),
        ("w2", &*format!("<r>/{nfc}/.git"), nfd),
    ] {
        put(
            format!("{r}/m/.git/worktrees/{wt}/HEAD"),
            "ref: refs/heads/main\n",
        );
        put(format!("{r}/m/.git/worktrees/{wt}/commondir"), "../..\n");
        put(
            format!("{r}/m/.git/worktrees/{wt}/gitdir"),
            &format!("{back}\n"),
        );
        let gitfile = format!("gitdir: <r>/m/.git/worktrees/{wt}\n");
        put(format!("{r}/{folder}/.git"), &gitfile);
        put(format!("{r}/{folder}/f"), "");
    }
    if apart_case {
        put(format!("{r}/W1/.GIT"), "gitdir: <r>/m/.git/worktrees/w1\n");
        put(format!("{r}/W1/f"), "");
    }
    if apart_form {
        put(
            format!("{r}/{nfc}/.git"),
            "gitdir: <r>/m/.git/worktrees/w2\n",
        );
        put(format!("{r}/{nfc}/f"), "");
    }
    put(format!("{r}/own/.git/HEAD"), "ref: refs/heads/main\n");
    put(format!("{r}/own/.git/objects/.keep"), "");
    put(format!("{r}/own/.git/refs/.keep"), "");
    put(
        format!("{r}/own/.git/config"),
        std::str::from_utf8(V0).unwrap(),
    );
    put(format!("{r}/own/f"), "");
    out.push((format!("{r}/lc/.git"), None));
    out
}

/// The answers of `judge` and `reading_at` for the three folders of
/// [`spelled`] (`w1`, the NFD folder, `lc`), the volume folding case
/// (`case`) and Unicode form (`form`) or not, as the reference answers
/// them (on APFS, which folds both; where neither is folded, as
/// `MemFs` does not fold them).
#[cfg(unix)]
fn spelled_answers(rule: &Rule<'_, impl Fs, Hung, NoRecords>, r: &str, case: bool, form: bool) {
    let ctx = rule.ctx;
    let lstat = |p: &str| ctx.fs.lstat_result(p.as_bytes()).expect("the entry");
    let worktree = |wt: &str, folder: &str, folds: bool, back: &str| {
        let (directory, dotgit) = (format!("{r}/{folder}"), format!("{r}/{folder}/.git"));
        let git_dir = format!("{r}/m/.git/worktrees/{wt}");
        let reason =
            (!folds).then(|| format!("{git_dir} is the git directory of the worktree at {back}"));
        assert_eq!(
            judge(
                rule,
                directory.as_bytes(),
                dotgit.as_bytes(),
                &lstat(&dotgit),
                true,
                true
            ),
            Ok(Some(Entry {
                git_dir: git_dir.clone().into_bytes(),
                why: reason.as_deref().and_then(why),
                own: false,
                provisional: false,
            })),
            "{folder}"
        );
        let want = match &reason {
            None => followed(&directory, false),
            Some(text) => refused(text),
        };
        assert_eq!(reading_at(rule, directory.as_bytes()), want, "{folder}");
    };
    worktree("w1", "w1", case, &format!("{r}/W1/.GIT"));
    worktree("w2", "e\u{301}", form, &format!("{r}/\u{e9}/.git"));
    let (directory, dotgit) = (format!("{r}/lc"), format!("{r}/lc/.git"));
    let links = format!("{dotgit} links to the .git of {r}/OWN");
    let judged = judge(
        rule,
        directory.as_bytes(),
        dotgit.as_bytes(),
        &lstat(&dotgit),
        true,
        true,
    );
    if case {
        assert_eq!(
            judged,
            Ok(Some(Entry {
                git_dir: format!("{r}/OWN/.GIT").into_bytes(),
                why: why(&links),
                own: false,
                provisional: false,
            }))
        );
        assert_eq!(reading_at(rule, directory.as_bytes()), refused(&links));
    } else {
        // The link reaches nothing: none, and the walk goes on.
        assert_eq!(judged, Ok(None));
        assert_eq!(reading_at(rule, directory.as_bytes()), Ok(None));
    }
}

/// A folder spelled in another case, or with a name in another Unicode
/// form, is compared as a file, so the answer is the volume's own: a
/// worktree's `gitdir` naming this folder so names it where the volume
/// folds the spelling (macOS's by default) and names another folder where
/// it does not (Linux's; the test makes that folder there), and a link to
/// another tree's `.git` in another case is that tree's where the volume
/// folds case and reaches nothing where it does not. Asked on disk, as the
/// volume folds, and in memory, which folds neither. Not in the shared
/// fixture, as the platforms differ.
#[cfg(unix)]
#[test]
fn a_spelling_in_another_case_or_form_is_answered_as_the_volume_answers() {
    use bettermemory_core::fsdata::StdFs;
    // In memory: neither folded.
    let mut fs = MemFs::new(Flavour::Posix);
    for (path, text) in spelled("/r", true, true) {
        match text {
            Some(text) => file(&mut fs, &path, &text),
            None => fs.insert(path.as_bytes(), Node::Link(b"/r/OWN/.GIT".to_vec())),
        }
    }
    with(&fs, |rule| spelled_answers(rule, "/r", false, false));
    // On disk: as the volume folds.
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let made = std::env::temp_dir().join(format!("bm-spelled-{}-{n}", std::process::id()));
    std::fs::create_dir(&made).expect("a new scratch directory");
    let base = std::fs::canonicalize(&made).expect("resolved");
    let r = base.to_str().expect("a UTF-8 scratch directory").to_owned();
    std::fs::create_dir(base.join("w1")).unwrap();
    std::fs::create_dir(base.join("e\u{301}")).unwrap();
    let case = std::fs::metadata(base.join("W1")).is_ok();
    let form = std::fs::metadata(base.join("\u{e9}")).is_ok();
    for (path, text) in spelled(&r, !case, !form) {
        let path = std::path::Path::new(&path);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        match text {
            Some(text) => std::fs::write(path, text).unwrap(),
            None => std::os::unix::fs::symlink(format!("{r}/OWN/.GIT"), path).unwrap(),
        }
    }
    let ctx = Ctx {
        fs: &StdFs,
        flavour: Flavour::Posix,
        cwd: r.as_bytes(),
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    let asked = std::panic::catch_unwind(|| spelled_answers(&rule, &r, case, form));
    let _ = std::fs::remove_dir_all(&base);
    if let Err(panic) = asked {
        std::panic::resume_unwind(panic);
    }
}

#[test]
fn the_windows_flavour_declines_every_function() {
    let mut fs = MemFs::new(Flavour::Windows);
    fs.insert(
        b"C:/w/.git/HEAD",
        Node::File(b"ref: refs/heads/main\n".to_vec()),
    );
    fs.insert(b"C:/w/.git/objects", Node::Dir);
    fs.insert(b"C:/w/.git/refs", Node::Dir);
    let ctx = Ctx {
        fs: &fs,
        flavour: Flavour::Windows,
        cwd: b"C:/",
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    let no = Failure::NotPorted;
    let st = StatResult {
        kind: Kind::Directory,
        mode: None,
        ino: None,
        dev: None,
        size: 0,
        mtime_ns: 0,
        ctime_ns: None,
        flags: 0,
        file_attributes: 0,
        reparse_tag: Some(0),
    };
    // Whatever the `lstat` given: a kind that names nothing on POSIX is
    // declined here too, before it is looked at.
    let of = |kind: Kind| StatResult { kind, ..st.clone() };
    for st in [
        st.clone(),
        of(Kind::Link),
        of(Kind::Regular),
        of(Kind::Fifo),
        of(Kind::Other),
        StatResult {
            reparse_tag: None,
            ..st.clone()
        },
    ] {
        assert_eq!(
            judge(&rule, b"C:/w", b"C:/w/.git", &st, true, true),
            Err(no)
        );
        assert_eq!(entry_reading(&rule, b"C:/w", b"C:/w/.git", &st), Err(no));
    }
    for path in [&b"C:/w"[..], b"C:/w/.git", b"", b"w", b"C:/"] {
        assert_eq!(reading_at(&rule, path), Err(no));
        assert_eq!(discovery_reading(&rule, path), Err(no));
        assert_eq!(discovery_refusal(&rule, path), Err(no));
        assert_eq!(toplevel_agrees(&rule, path, b"C:/w"), Err(no));
    }
}

/// The port's side of its second stated difference (README, unit 5): a
/// relative path is joined to `Ctx::cwd` before the file system is asked,
/// so the path limit (`MemFs::path_max`, macOS's 1,024) bounds the join,
/// where the OS would bound the relative text alone. Each path here is
/// under the limit and its join is over it. The reference, run from the
/// same folder, answers otherwise in every case (shown beside each); no
/// caller of it passes a relative path, and the walk resolves its start
/// first. A change of either side shows here.
#[test]
fn a_relative_path_under_the_limit_whose_join_is_over_it() {
    // An absolute working directory of 131 bytes.
    let r = format!("/{}", "r".repeat(130));
    // `./` repeated, a `/` to make it up, then `last`: a relative path of
    // exactly `n` bytes.
    let chain = |n: usize, last: &str| {
        let mut out = "./".repeat((n - last.len()) / 2);
        if out.len() + last.len() < n {
            out.push('/');
        }
        out.push_str(last);
        assert_eq!(out.len(), n, "a chain of {n} bytes");
        out
    };
    let mut fs = MemFs::new(Flavour::Posix);
    repo(&mut fs, &format!("{r}/m/.git"));
    file(&mut fs, &format!("{r}/m/f"), "");
    file(
        &mut fs,
        &format!("{r}/b/.git"),
        &format!("gitdir: {r}/m/.git\n"),
    );
    fs.insert(
        format!("{r}/a/HEAD").as_bytes(),
        Node::File(b"ref: refs/heads/main\n".to_vec()),
    );
    let ctx = Ctx {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: r.as_bytes(),
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &Hung::default(),
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    // The same folders spelled short are read as the reference reads the
    // long spellings: the tree is not what parts the answers.
    let tree_of_m = format!("{r}/m/.git is the git directory of the working tree {r}/m");
    assert_eq!(reading_at(&rule, b"b"), refused(&tree_of_m));
    assert_eq!(
        toplevel_agrees(&rule, format!("{r}/m").as_bytes(), b"m/../m"),
        Ok(true)
    );
    assert_eq!(reading_at(&rule, b"a"), Ok(None));
    // (1) A folder of 888 bytes whose `.git` names `R/m/.git` (the join of
    // its `.git` is 1,025 bytes). Reference: refused, `R/m/.git` is the
    // git directory of the working tree `R/m`. Port: None.
    let b = chain(888, "b");
    assert_eq!(r.len() + 1 + b.len() + 5, 1025);
    assert_eq!(reading_at(&rule, b.as_bytes()), Ok(None));
    // (2) The judge of the same entry with the `lstat` of a file. Reference:
    // an entry for `R/m/.git` with the same reason. Port: None.
    let st = fs
        .lstat_result(format!("{r}/m/f").as_bytes())
        .expect("a file");
    let dotgit = format!("{b}/.git");
    assert_eq!(
        judge(&rule, b.as_bytes(), dotgit.as_bytes(), &st, true, true),
        Ok(None)
    );
    // (3) `m/../m` and `/../m` 190 times: 956 bytes, 1,088 joined.
    // Reference: `toplevel_agrees` True, and a reading rooted at it. Port:
    // False, and None.
    let c = format!("m/../m{}", "/../m".repeat(190));
    assert_eq!(c.len(), 956);
    assert_eq!(
        toplevel_agrees(&rule, format!("{r}/m").as_bytes(), c.as_bytes()),
        Ok(false)
    );
    assert_eq!(reading_at(&rule, c.as_bytes()), Ok(None));
    // (4) A folder of 882 bytes holding HEAD alone (the join of its
    // `commondir` is 1,024 bytes). Reference: None. Port: refused, as git
    // cannot read it as a git directory.
    let a = chain(882, "a");
    assert_eq!(r.len() + 1 + a.len() + 10, 1024);
    let reason = format!("git cannot read {a} as a git directory");
    assert_eq!(reading_at(&rule, a.as_bytes()), refused(&reason));
}
