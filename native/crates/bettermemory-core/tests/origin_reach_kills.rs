//! Inputs a plausible wrong port of `origin_reach` answers otherwise than
//! the reference: the anchor listed as a commit rather than a boundary,
//! the shallow cut left unchecked, a quoted path kept quoted, a
//! `commit_reachable` False where git did not establish one, the file
//! reads of `toplevel_and_head_from_files` reordered, `rpartition` read
//! as `partition`, the root's newline not put back under the Windows
//! flavour, and [`RepositoryReads`] spelling the root without the
//! kernel on macOS or with it elsewhere.

use bettermemory_core::fsdata::realpath::Unresolved;
use bettermemory_core::fsdata::{Flavour, MemFs, Node, OsError};
use bettermemory_core::git_read_budget::Scope;
use bettermemory_core::git_run::{GitProcess, Spawned};
use bettermemory_core::gitconfig::paths::Machine;
use bettermemory_core::gitconfig::remotes::{self, Inputs, Sources};
use bettermemory_core::gitconfig::stamped::StampedReads;
use bettermemory_core::gitfile_rule::discovery::{Reading, Rule};
use bettermemory_core::gitfile_rule::follow::NoRecords;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitfile_rule::gitdir::{Ctx, Failure};
use bettermemory_core::githead::discovery::{Env as HeadEnv, Found, Host, Owners};
use bettermemory_core::githead::GitDir;
use bettermemory_core::gitsystem::ask::{Env, PINS};
use bettermemory_core::origin_git::{self, Context, Counts, Files, OriginRunner};
use bettermemory_core::origin_gitfiles::root::{ConfigMemo, NotADirectorySeen};
use bettermemory_core::origin_history::{Cut, AS_STORED_CONFIG};
use bettermemory_core::origin_reach::{self, Error, Reads, RepositoryReads};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::{cps, to_string};
use std::collections::VecDeque;
use std::sync::atomic::AtomicI64;

const A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const C: &str = "cccccccccccccccccccccccccccccccccccccccc";

struct Proc {
    replies: VecDeque<(i64, String, String)>,
    asked: Vec<String>,
}
impl GitProcess for Proc {
    fn run(
        &mut self,
        argv: &[PyStr],
        _: Option<&[u32]>,
        _: &Env,
        _: Option<&[u8]>,
        _: f64,
    ) -> Spawned {
        let skip = 1 + PINS.len() + AS_STORED_CONFIG.len();
        let words: Vec<_> = argv[skip..].iter().map(|a| to_string(a).unwrap()).collect();
        self.asked.push(words.join(" "));
        let (returncode, out, err) = self.replies.pop_front().expect("a scripted reply");
        Spawned::Exited {
            returncode,
            stdout: out.into_bytes(),
            stderr: err.into_bytes(),
        }
    }
}

struct Agree;
impl Files for Agree {
    fn reading(&mut self, _: &[u8]) -> Result<Option<Reading>, origin_git::Error> {
        Ok(None)
    }
    fn stat_dir(&self, _: &[u8]) -> Result<bool, Failure> {
        Ok(true)
    }
    fn refused_root(&mut self, _: &[u8]) -> Result<(Option<Vec<u8>>, bool), origin_git::Error> {
        unreachable!()
    }
    fn agrees(&mut self, _: &[u8], _: &[u8]) -> Result<bool, origin_git::Error> {
        Ok(true)
    }
    fn resolve(&mut self, _: &[u8]) -> Result<Vec<u8>, Unresolved> {
        unreachable!()
    }
}

#[derive(Default)]
struct Leaf {
    read: Vec<&'static str>,
    found: bool,
    head: Option<String>,
    moves: bool,
    dies: bool,
    cut: Option<Cut>,
    windows: bool,
}
impl Reads for Leaf {
    fn flavour(&self) -> Flavour {
        match self.windows {
            true => Flavour::Windows,
            false => Flavour::Posix,
        }
    }
    fn find_gitdir(&mut self, _: &[u8]) -> Option<Found> {
        self.read.push("find");
        self.found.then(|| Found {
            gd: GitDir {
                gitdir: b"g".to_vec(),
                commondir: b"g".to_vec(),
            },
            worktree_root: b"w".to_vec(),
        })
    }
    fn head_sha(&mut self, _: &GitDir) -> Option<String> {
        self.read.push("head");
        self.head.clone()
    }
    fn config_moves_worktree(&mut self, _: &GitDir) -> Result<bool, Error> {
        self.read.push("moves");
        Ok(self.moves)
    }
    fn machine_config_dies(&mut self, _: &[u8]) -> Result<bool, Error> {
        self.read.push("dies");
        Ok(self.dies)
    }
    fn spelled_as_git(&mut self, root: &[u8]) -> Result<Option<Vec<u8>>, Error> {
        self.read.push("spelled");
        Ok(Some(root.to_vec()))
    }
    fn resolve(&mut self, path: &[u8]) -> Result<Vec<u8>, Unresolved> {
        self.read.push("resolve");
        Ok(path.to_vec())
    }
    fn cuts(&mut self, _: &[u8]) -> Result<Option<Cut>, Error> {
        self.read.push("cuts");
        Ok(self.cut.clone())
    }
}

/// Runs `f` over a runner answering `replies` in turn; gives its answer
/// and the git commands asked.
fn with<T>(
    replies: &[(i64, &str, &str)],
    f: impl FnOnce(&mut OriginRunner<'_>) -> T,
) -> (T, Vec<String>) {
    with_in(Flavour::Posix, replies, f)
}

/// [`with`] over a runner of `flavour`.
fn with_in<T>(
    flavour: Flavour,
    replies: &[(i64, &str, &str)],
    f: impl FnOnce(&mut OriginRunner<'_>) -> T,
) -> (T, Vec<String>) {
    let mut proc = Proc {
        replies: replies
            .iter()
            .map(|(c, o, e)| (*c, (*o).to_owned(), (*e).to_owned()))
            .collect(),
        asked: vec![],
    };
    let (scope, global, env) = (Scope::default(), AtomicI64::new(0), Env::new());
    let (mut seen, mut counts, mut files) = (NotADirectorySeen::new(), Counts::default(), Agree);
    let mut runner = OriginRunner::new(
        Context {
            flavour,
            scope: &scope,
            clock: &|| 0.,
            env: &env,
            global_unanswered: &global,
            first_not_a_directory: &mut seen,
        },
        &mut proc,
        &mut files,
        &mut counts,
    );
    let answer = f(&mut runner);
    drop(runner);
    assert!(proc.replies.is_empty(), "every reply read");
    (answer, proc.asked)
}

fn walk(stdout: &str, cut: Option<Cut>) -> Option<Vec<String>> {
    let mut leaf = Leaf {
        cut,
        ..Leaf::default()
    };
    let (found, _) = with(&[(0, stdout, "")], |r| {
        origin_reach::walk_reachable(r, &mut leaf, b"top", &cps(A), &cps(C), true).unwrap()
    });
    found.map(|w| w.touched.keys().map(|p| to_string(p).unwrap()).collect())
}

#[test]
fn the_anchor_must_be_a_boundary_not_a_listed_commit() {
    let listed = format!("\x01>{C}\nf\n\x01>{A}\ng\n\x01-{B}\n");
    assert_eq!(walk(&listed, Some(Cut::new())), None);
    let bounded = format!("\x01>{C}\nf\n\x01-{A}\ng\n");
    assert_eq!(walk(&bounded, Some(Cut::new())), Some(vec!["f".to_owned()]));
}

#[test]
fn the_cut_is_checked_against_the_commits() {
    let out = format!("\x01>{C}\nf\n\x01>{B}\n\x01-{A}\n");
    assert_eq!(walk(&out, Some([cps(B)].into())), None);
    assert_eq!(walk(&out, None), None);
    let elsewhere: Cut = [cps(A), cps("d".repeat(40).as_str())].into();
    assert!(walk(&out, Some(elsewhere)).is_some());
    // The first listed commit is checked too.
    let head_only = format!("\x01>{C}\nf\n\x01-{A}\n");
    assert_eq!(walk(&head_only, Some([cps(C)].into())), None);
    assert!(walk(&head_only, Some(Cut::new())).is_some());
}

#[test]
fn a_quoted_path_is_decoded_and_a_bad_quote_kept() {
    let out = format!("\x01>{C}\n\"caf\\303\\251\"\n\"x\\qy\"\n\"\\377\"\n\x01-{A}\n");
    let paths = walk(&out, Some(Cut::new())).unwrap();
    assert_eq!(paths, ["\"\\377\"", "\"x\\qy\"", "caf\u{e9}"]);
}

#[test]
fn commit_reachable_answers_none_where_git_established_nothing() {
    let reach = |replies: &[(i64, &str, &str)]| {
        with(replies, |r| {
            origin_reach::commit_reachable(r, &mut Leaf::default(), Some(b"x"), &cps(A)).unwrap()
        })
    };
    assert_eq!(reach(&[(1, "", ""), (0, "true\n", "")]).0, None);
    assert_eq!(reach(&[(1, "", "error: corrupt\n")]).0, None);
    assert_eq!(reach(&[(-15, "", "")]).0, None);
    assert_eq!(reach(&[(1, "", ""), (0, "false\n", "")]).0, Some(false));
    let (present, asked) = reach(&[(128, "", "fatal"), (0, "false", ""), (0, B, ""), (0, A, "")]);
    assert_eq!(present, None);
    assert_eq!(
        asked[3],
        format!("rev-parse --verify --quiet {A}^{{commit}}")
    );
    let (absent, _) = reach(&[
        (128, "", "fatal"),
        (0, "false", ""),
        (0, B, ""),
        (1, "", ""),
    ]);
    assert_eq!(absent, Some(false));
    let (no_head, asked) = reach(&[(128, "", "fatal"), (0, "false", ""), (1, "", "")]);
    assert_eq!((no_head, asked.len()), (None, 3));
}

#[test]
fn the_file_reads_stop_at_the_first_that_declines() {
    let read = |leaf: &mut Leaf| {
        let found = origin_reach::toplevel_and_head_from_files(leaf, Some(b"x")).unwrap();
        (found.is_some(), leaf.read.clone())
    };
    let head = Some(A.to_owned());
    let mut moved = Leaf {
        found: true,
        head: head.clone(),
        moves: true,
        dies: true,
        ..Leaf::default()
    };
    assert_eq!(read(&mut moved), (false, vec!["find", "head", "moves"]));
    let mut unborn = Leaf {
        found: true,
        moves: true,
        ..Leaf::default()
    };
    assert_eq!(read(&mut unborn), (false, vec!["find", "head"]));
    let mut dies = Leaf {
        found: true,
        head: head.clone(),
        dies: true,
        ..Leaf::default()
    };
    assert_eq!(
        read(&mut dies),
        (false, vec!["find", "head", "moves", "dies"])
    );
    let mut whole = Leaf {
        found: true,
        head,
        ..Leaf::default()
    };
    let all = vec!["find", "head", "moves", "dies", "spelled"];
    assert_eq!(read(&mut whole), (true, all));
}

#[test]
fn the_root_is_all_before_the_last_newline() {
    let mut leaf = Leaf::default();
    let out = format!("a\nb\n{A}\n");
    let (found, _) = with(&[(0, &out, "")], |r| {
        origin_reach::repo_toplevel_and_head(r, &mut leaf, Some(b"x")).unwrap()
    });
    let (root, head) = found.unwrap();
    assert_eq!(
        (to_string(&root).unwrap(), to_string(&head).unwrap()),
        ("a\nb".into(), A.into())
    );
    let (found, _) = with(&[(0, &format!("top\n{A}\n\n"), "")], |r| {
        origin_reach::repo_toplevel_and_head(r, &mut leaf, Some(b"x")).unwrap()
    });
    assert_eq!(found, None);
}

#[test]
fn head_sha_lowers_and_strips() {
    let (sha, asked) = with(&[(0, &format!(" {}\t\n", A.to_uppercase()), "")], |r| {
        origin_reach::head_sha(r, Some(b"x")).unwrap()
    });
    assert_eq!(sha, Some(cps(A)));
    assert_eq!(asked, ["rev-parse --verify --quiet HEAD^{commit}"]);
    let (short, _) = with(&[(0, "abc\n", "")], |r| {
        origin_reach::head_sha(r, Some(b"x")).unwrap()
    });
    assert_eq!(short, None);
}

#[test]
fn a_bad_mark_fails_the_walk_and_is_counted() {
    let mut leaf = Leaf::default();
    let (failed, _) = with(&[(0, &format!("\x01>{C}\n\x01>{}\n", &A[1..]), "")], |r| {
        let walked = origin_reach::walk_reachable(r, &mut leaf, b"t", &cps(A), &cps(C), true);
        (walked.unwrap().is_none(), r.failed_git_calls())
    });
    assert_eq!(failed, (true, 1));
    assert!(leaf.read.is_empty(), "no cut read after a bad mark");
}

#[test]
fn under_windows_the_root_loses_a_cr_before_its_newline() {
    // `printed_path(root + newline)`: under `_WINDOWS` the CR the root
    // ends with comes off with the newline put back; under POSIX it is
    // the name's own and stays.
    let out = format!("top\r\n{A}\n");
    for (flavour, windows, root) in [
        (Flavour::Windows, true, "top"),
        (Flavour::Posix, false, "top\r"),
    ] {
        let mut leaf = Leaf {
            windows,
            ..Leaf::default()
        };
        let (found, asked) = with_in(flavour, &[(0, &out, "")], |r| {
            origin_reach::repo_toplevel_and_head(r, &mut leaf, Some(b"x")).unwrap()
        });
        let (got, head) = found.unwrap();
        assert_eq!(
            (to_string(&got).unwrap(), to_string(&head).unwrap()),
            (root.to_owned(), A.to_owned()),
            "{flavour:?}"
        );
        assert_eq!(asked, ["rev-parse --show-toplevel HEAD"]);
        assert_eq!(leaf.read, ["find", "resolve"]);
    }
}

/// Every path owned by the process.
struct Owned;
impl Owners for Owned {
    fn owner_uid(&self, _: &[u8], _: bool) -> Result<u32, OsError> {
        Ok(0)
    }
}

const GIT: GitFacts = GitFacts {
    release: Some((2, 54)),
    reads_compat: Some(true),
};

#[test]
fn repository_reads_ask_the_kernel_for_the_root_only_on_macos() {
    // `_spelled_as_git` reads `F_GETPATH` where `sys.platform` is darwin
    // (the link's target, as the kernel names it) and keeps the given
    // spelling elsewhere; a NUL raises ValueError on macOS only.
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/w/d", Node::Dir);
    fs.insert(b"/w/ld", Node::Link(b"/w/d".to_vec()));
    let ctx = Ctx {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/",
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &fs,
        records: &NoRecords,
        env: None,
        git: &GIT,
    };
    let host = Host {
        euid: 0,
        owners: &Owned,
    };
    let machine = Machine {
        flavour: Flavour::Posix,
        home: None,
        profile: None,
        user_home: &|_| None,
    };
    let inputs = Inputs {
        machine: &machine,
        env: remotes::Env::default(),
        sources: Sources::default(),
        git: GIT,
        cwd: b"/".to_vec(),
        asked_branch: None,
    };
    let head_env = HeadEnv::default();
    let spell = |darwin: bool, root: &[u8]| {
        let (mut memo, mut stamped, mut clock) = (ConfigMemo::new(), StampedReads::new(), || 0.);
        let mut reads = RepositoryReads {
            rule: &rule,
            host: &host,
            env: &head_env,
            config_memo: &mut memo,
            machine_reads: &mut stamped,
            inputs: &inputs,
            clock: &mut clock,
            darwin,
        };
        reads.spelled_as_git(root)
    };
    assert_eq!(spell(true, b"/w/ld"), Ok(Some(b"/w/d".to_vec())));
    assert_eq!(spell(false, b"/w/ld"), Ok(Some(b"/w/ld".to_vec())));
    assert_eq!(spell(true, b"/w/a\0b"), Err(Error::Files(Failure::Value)));
    assert_eq!(spell(false, b"/w/a\0b"), Ok(Some(b"/w/a\0b".to_vec())));
}
