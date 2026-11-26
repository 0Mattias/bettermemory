//! `origin_reach` over hostile git output, and its cost.
//!
//! - A walk of 100,000 commits and 1,000,000 path lines takes under
//!   `TENFOLD` times the walk of a tenth of it (the parse is linear, the
//!   touched map's inserts logarithmic).
//! - Stdout holding NUL, a lone CR or bytes that are not UTF-8 reaches
//!   every function with no panic, and the answer the reference gives.
//! - A root 4,096 components deep is printed, agreed, resolved and
//!   returned whole; the file reads return one as deep.
//!
//! The runner and the file reads are scripted: no git process is started.

mod timing;

use bettermemory_core::fsdata::realpath::Unresolved;
use bettermemory_core::fsdata::Flavour;
use bettermemory_core::git_read_budget::Scope;
use bettermemory_core::git_run::{GitProcess, Spawned};
use bettermemory_core::gitfile_rule::discovery::Reading;
use bettermemory_core::gitfile_rule::gitdir::Failure;
use bettermemory_core::githead::discovery::Found;
use bettermemory_core::githead::GitDir;
use bettermemory_core::gitsystem::ask::Env;
use bettermemory_core::origin_git::{self, Context, Counts, Files, OriginRunner};
use bettermemory_core::origin_gitfiles::root::NotADirectorySeen;
use bettermemory_core::origin_history::Cut;
use bettermemory_core::origin_reach::{self, Error, Reads};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use std::sync::atomic::AtomicI64;

const A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

struct Proc(Vec<Vec<u8>>);
impl GitProcess for Proc {
    fn run(
        &mut self,
        _: &[PyStr],
        _: Option<&[u32]>,
        _: &Env,
        _: Option<&[u8]>,
        _: f64,
    ) -> Spawned {
        Spawned::Exited {
            returncode: 0,
            stdout: if self.0.is_empty() {
                vec![]
            } else {
                self.0.remove(0)
            },
            stderr: vec![],
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

/// Every file read answers: a repository at `root`, its head `A`.
struct Leaf {
    root: Vec<u8>,
}
impl Reads for Leaf {
    fn flavour(&self) -> Flavour {
        Flavour::Posix
    }
    fn find_gitdir(&mut self, _: &[u8]) -> Option<Found> {
        Some(Found {
            gd: GitDir {
                gitdir: b"g".to_vec(),
                commondir: b"g".to_vec(),
            },
            worktree_root: self.root.clone(),
        })
    }
    fn head_sha(&mut self, _: &GitDir) -> Option<String> {
        Some(A.to_owned())
    }
    fn config_moves_worktree(&mut self, _: &GitDir) -> Result<bool, Error> {
        Ok(false)
    }
    fn machine_config_dies(&mut self, _: &[u8]) -> Result<bool, Error> {
        Ok(false)
    }
    fn spelled_as_git(&mut self, root: &[u8]) -> Result<Option<Vec<u8>>, Error> {
        Ok(Some(root.to_vec()))
    }
    fn resolve(&mut self, path: &[u8]) -> Result<Vec<u8>, Unresolved> {
        Ok(path.to_vec())
    }
    fn cuts(&mut self, _: &[u8]) -> Result<Option<Cut>, Error> {
        Ok(Some(Cut::new()))
    }
}

fn with<T>(replies: Vec<Vec<u8>>, f: impl FnOnce(&mut OriginRunner<'_>) -> T) -> T {
    let mut proc = Proc(replies);
    let (scope, global, env) = (Scope::default(), AtomicI64::new(0), Env::new());
    let (mut seen, mut counts, mut files) = (NotADirectorySeen::new(), Counts::default(), Agree);
    let mut runner = OriginRunner::new(
        Context {
            flavour: Flavour::Posix,
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
    f(&mut runner)
}

/// `commits` commits of ten path lines each, then the anchor's boundary.
fn log(commits: usize) -> Vec<u8> {
    let mut out = Vec::with_capacity(commits * 100);
    for i in 0..commits {
        out.extend_from_slice(format!("\x01>{:040x}\n\n", i + 1).as_bytes());
        for j in 0..9 {
            out.extend_from_slice(format!("src/m{}/f{j}.rs\n", i % 1000).as_bytes());
        }
        out.extend_from_slice(b"\"caf\\303\\251\"\n");
    }
    out.extend_from_slice(format!("\x01-{A}\n").as_bytes());
    out
}

fn walked(stdout: &[u8]) -> Option<usize> {
    let mut leaf = Leaf {
        root: b"t".to_vec(),
    };
    with(vec![stdout.to_vec()], |r| {
        let head = cps(&"f".repeat(40));
        origin_reach::walk_reachable(r, &mut leaf, b"t", &cps(A), &head, true)
            .unwrap()
            .map(|w| w.commits.len())
    })
}

#[test]
fn the_walk_is_linear_in_its_output() {
    let (small, large) = (log(10_000), log(100_000));
    assert_eq!(walked(&small), Some(10_000));
    timing::grows_within(
        "walk_reachable",
        timing::TENFOLD,
        &mut || timing::once(|| walked(&small)),
        &mut || timing::once(|| walked(&large)),
    );
    assert_eq!(walked(&large), Some(100_000));
}

#[test]
fn hostile_bytes_reach_every_function() {
    let sha = A.as_bytes();
    let hostile: Vec<Vec<u8>> = vec![
        [b"top\0x\n", sha, b"\n"].concat(),
        [b"top\rx\n", sha, b"\r\n"].concat(),
        [b"t\xffo\xc3\n", sha, b"\n"].concat(),
        [b"\xed\xa0\x80\n", sha].concat(),
        b"\0\0\0".to_vec(),
        b"\r".to_vec(),
        b"\xff".to_vec(),
    ];
    for out in &hostile {
        let mut leaf = Leaf {
            root: b"t".to_vec(),
        };
        with(vec![out.clone(), out.clone(), out.clone()], |r| {
            let top = origin_reach::repo_toplevel(r, &mut leaf, Some(b"x"));
            let blank = out.iter().all(u8::is_ascii_whitespace);
            assert_eq!(top.map(|t| t.is_some()), Ok(!blank), "{out:?}");
            let head = origin_reach::head_sha(r, Some(b"x")).unwrap();
            assert_eq!(head, None, "{out:?}");
            let walk = origin_reach::walk_reachable(r, &mut leaf, b"t", &cps(A), &cps("b"), true);
            assert_eq!(walk, Ok(None), "{out:?}");
        });
    }
    let nul = [
        b"\x01>",
        "c".repeat(40).as_bytes(),
        b"\n\0\n\r\n\xff\xfe\n\x01-",
        sha,
        b"\n",
    ]
    .concat();
    let paths = with(vec![nul], |r| {
        let mut leaf = Leaf {
            root: b"t".to_vec(),
        };
        let walk = origin_reach::walk_reachable(r, &mut leaf, b"t", &cps(A), &cps("c"), true);
        walk.unwrap()
            .unwrap()
            .touched
            .into_keys()
            .collect::<Vec<_>>()
    });
    assert_eq!(paths, [vec![0], vec![13], vec![0xfffd, 0xfffd]]);
}

#[test]
fn a_root_four_thousand_components_deep() {
    let deep = vec!["d"; 4096].join("/");
    let printed = format!("/{deep}\n{A}\n");
    let mut leaf = Leaf {
        root: format!("/{deep}").into_bytes(),
    };
    let expected: PyStr = cps(&format!("/{deep}"));
    let top = with(vec![format!("/{deep}\n").into_bytes()], |r| {
        origin_reach::repo_toplevel(r, &mut leaf, Some(b"x")).unwrap()
    });
    assert_eq!(top, Some(expected.clone()));
    let from_files = origin_reach::toplevel_and_head_from_files(&mut leaf, Some(b"x")).unwrap();
    assert_eq!(from_files, Some((expected.clone(), cps(A))));
    let pair = with(vec![printed.into_bytes()], |r| {
        let mut leaf = Leaf { root: vec![] };
        let mut declined = Declines(&mut leaf);
        origin_reach::repo_toplevel_and_head(r, &mut declined, Some(b"x")).unwrap()
    });
    assert_eq!(pair, Some((expected, cps(A))));
}

/// [`Leaf`] with no repository found, so `repo_toplevel_and_head` asks git.
struct Declines<'a>(&'a mut Leaf);
impl Reads for Declines<'_> {
    fn flavour(&self) -> Flavour {
        Flavour::Posix
    }
    fn find_gitdir(&mut self, _: &[u8]) -> Option<Found> {
        None
    }
    fn head_sha(&mut self, gd: &GitDir) -> Option<String> {
        self.0.head_sha(gd)
    }
    fn config_moves_worktree(&mut self, gd: &GitDir) -> Result<bool, Error> {
        self.0.config_moves_worktree(gd)
    }
    fn machine_config_dies(&mut self, d: &[u8]) -> Result<bool, Error> {
        self.0.machine_config_dies(d)
    }
    fn spelled_as_git(&mut self, root: &[u8]) -> Result<Option<Vec<u8>>, Error> {
        self.0.spelled_as_git(root)
    }
    fn resolve(&mut self, path: &[u8]) -> Result<Vec<u8>, Unresolved> {
        self.0.resolve(path)
    }
    fn cuts(&mut self, toplevel: &[u8]) -> Result<Option<Cut>, Error> {
        self.0.cuts(toplevel)
    }
}
