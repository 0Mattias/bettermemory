//! `commits_since_anchor` and its memo under load and hostile input.
//!
//! - 100,000 calls over 1,000 keys take under `TENFOLD` times 10,000 calls
//!   (the memo's look-ups and evictions are bounded by its cap, so a call
//!   costs the same at any count).
//! - Eight threads sharing one memo fill both tables past their caps: each
//!   thread is answered its own walks, and each table ends at its cap with
//!   no key twice.
//! - Hash-length anchors of SHA-256, a root holding undecodable bytes or a
//!   code point no file name holds, a walk of 200,000 commits, clocks at
//!   the float bounds, NaN and infinities, and exit codes at the integer
//!   bounds reach the walk with no panic.
//! - The Windows flavour declines (`walk_files_signature` is not ported
//!   there) before any git runs, the root and head passed.
//! - A `Reads` that leaves `directories_held` or `monotonic` to its
//!   default declines where the call would read it, and a declined clock
//!   leaves a kept entry in place.
//!
//! Git and the reads are scripted: no process is started.

mod common;
mod timing;

use std::collections::HashSet;
use std::sync::atomic::AtomicI64;

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
use bettermemory_core::origin_gitfiles::facts::Held;
use bettermemory_core::origin_gitfiles::root::NotADirectorySeen;
use bettermemory_core::origin_gitfiles::walk::WalkFiles;
use bettermemory_core::origin_history::{Cut, HistoryUnread, ReachableWalk, Unwalked};
use bettermemory_core::origin_reach::walk::WALK_MEMO_CAP;
use bettermemory_core::origin_reach::{
    commits_since_anchor, Error, Kept, Reads, WalkKey, WalkMemo,
};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::{cps, to_string};

const C: &str = "cccccccccccccccccccccccccccccccccccccccc";

/// Git that lists a range's head and boundary, with `commits` commits
/// before the head; a range whose anchor ends in an odd digit meets its
/// timeout (names and listing both) where `timeouts`; every `cat-file`
/// exits `check`.
struct Lean {
    commits: usize,
    timeouts: bool,
    check: i64,
}

impl GitProcess for Lean {
    fn run(
        &mut self,
        argv: &[PyStr],
        _: Option<&[u32]>,
        _: &Env,
        _: Option<&[u8]>,
        _: f64,
    ) -> Spawned {
        let last = to_string(argv.last().unwrap()).unwrap();
        let Some((anchor, head)) = last.split_once("..") else {
            return Spawned::Exited {
                returncode: self.check,
                stdout: vec![],
                stderr: vec![],
            };
        };
        let odd = anchor.ends_with(['1', '3', '5', '7', '9', 'b', 'd', 'f']);
        if self.timeouts && odd {
            return Spawned::TimedOut;
        }
        let mut out = format!("\x01>{head}\n\nf.txt\n");
        for i in 0..self.commits {
            out.push_str(&format!("\x01>{i:040x}\n"));
        }
        out.push_str(&format!("\x01-{anchor}\n"));
        Spawned::Exited {
            returncode: 0,
            stdout: out.into_bytes(),
            stderr: vec![],
        }
    }
}

/// Git that fails both walks (exit 128) and exits its code on the check.
struct Failing(i64);

impl GitProcess for Failing {
    fn run(
        &mut self,
        argv: &[PyStr],
        _: Option<&[u32]>,
        _: &Env,
        _: Option<&[u8]>,
        _: f64,
    ) -> Spawned {
        let log = argv.iter().any(|a| *a == cps("log"));
        Spawned::Exited {
            returncode: if log { 128 } else { self.0 },
            stdout: vec![],
            stderr: vec![],
        }
    }
}

struct Quiet;

impl Files for Quiet {
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

/// The one signature the reads here give.
fn still_files() -> WalkFiles {
    WalkFiles {
        config: Some([1, 0, 0, 0, 0]),
        worktree_config: None,
        gitmodules: None,
        index: None,
        shallow: None,
    }
}

/// The reads every `Reads` here gives: no repository found by its files,
/// no shallow cut, one signature.
macro_rules! still_reads {
    () => {
        fn flavour(&self) -> Flavour {
            Flavour::Posix
        }
        fn find_gitdir(&mut self, _: &[u8]) -> Option<Found> {
            None
        }
        fn head_sha(&mut self, _: &GitDir) -> Option<String> {
            None
        }
        fn config_moves_worktree(&mut self, _: &GitDir) -> Result<bool, Error> {
            Ok(false)
        }
        fn machine_config_dies(&mut self, _: &[u8]) -> Result<bool, Error> {
            Ok(false)
        }
        fn spelled_as_git(&mut self, _: &[u8]) -> Result<Option<Vec<u8>>, Error> {
            Ok(None)
        }
        fn resolve(&mut self, path: &[u8]) -> Result<Vec<u8>, Unresolved> {
            Ok(path.to_vec())
        }
        fn cuts(&mut self, _: &[u8]) -> Result<Option<Cut>, Error> {
            Ok(Some(Cut::new()))
        }
        fn walk_files(
            &mut self,
            _: &[u8],
            _: Option<&mut Held>,
        ) -> Result<Option<WalkFiles>, Error> {
            Ok(Some(still_files()))
        }
    };
}

/// Reads that hold still: one signature, every directory held, the clock
/// at `now`, no shallow cut.
struct Still {
    now: f64,
}

impl Reads for Still {
    still_reads!();
    fn directories_held(&mut self, _: &Held) -> Result<bool, Error> {
        Ok(true)
    }
    fn monotonic(&mut self) -> Result<f64, Error> {
        Ok(self.now)
    }
}

/// [`Still`] with `directories_held` and `monotonic` left to the trait.
struct NoHold;

impl Reads for NoHold {
    still_reads!();
}

/// [`Still`] with `monotonic` left to the trait.
struct NoClock;

impl Reads for NoClock {
    still_reads!();
    fn directories_held(&mut self, _: &Held) -> Result<bool, Error> {
        Ok(true)
    }
}

/// One call per anchor, to `head` C at `toplevel`, over one runner and
/// [`Still`] reads at `now`.
fn calls(
    memo: &WalkMemo,
    process: &mut Lean,
    now: f64,
    toplevel: &[u32],
    anchors: &[PyStr],
) -> Vec<Result<Option<ReachableWalk>, Error>> {
    calls_over(memo, process, &mut Still { now }, toplevel, anchors)
}

/// [`calls`] over any git and reads.
fn calls_over(
    memo: &WalkMemo,
    process: &mut dyn GitProcess,
    reads: &mut dyn Reads,
    toplevel: &[u32],
    anchors: &[PyStr],
) -> Vec<Result<Option<ReachableWalk>, Error>> {
    let (scope, env, global) = (Scope::default(), Env::new(), AtomicI64::new(0));
    let mut seen = NotADirectorySeen::new();
    let mut counts = Counts::default();
    let mut files = Quiet;
    let mut runner = OriginRunner::new(
        Context {
            flavour: Flavour::Posix,
            scope: &scope,
            clock: &|| 0.,
            env: &env,
            global_unanswered: &global,
            first_not_a_directory: &mut seen,
        },
        process,
        &mut files,
        &mut counts,
    );
    let mut unwalked = Unwalked::new();
    anchors
        .iter()
        .map(|anchor| {
            commits_since_anchor(
                memo,
                &mut runner,
                reads,
                &mut unwalked,
                Some(b"sample"),
                anchor,
                (Some(toplevel), Some(&cps(C))),
                None,
            )
            .map(|w| w.map(|w| (*w).clone()))
        })
        .collect()
}

fn anchors(count: usize, keys: usize) -> Vec<PyStr> {
    (0..count)
        .map(|i| cps(&format!("{:040x}", 2 * (i % keys))))
        .collect()
}

#[test]
fn many_calls_grow_linearly() {
    let (small, large) = (anchors(10_000, 1_000), anchors(100_000, 1_000));
    let lean = || Lean {
        commits: 0,
        timeouts: false,
        check: 0,
    };
    let top = cps("top");
    let run = |list: &[PyStr]| {
        let (memo, mut process) = (WalkMemo::new(), lean());
        timing::once(|| calls(&memo, &mut process, 0.0, &top, list))
    };
    timing::grows_within(
        "commits_since_anchor",
        timing::TENFOLD,
        &mut || run(&small),
        &mut || run(&large),
    );
    let memo = WalkMemo::new();
    let answers = calls(&memo, &mut lean(), 0.0, &top, &large[..2_000]);
    assert!(answers.iter().all(|a| matches!(a, Ok(Some(w)) if w.listed)));
    assert_eq!(memo.memo_keys().len(), WALK_MEMO_CAP);
}

#[test]
fn the_memo_at_its_caps_under_eight_threads() {
    let memo = WalkMemo::new();
    let top = cps("top");
    std::thread::scope(|s| {
        for t in 0..8 {
            let (memo, top) = (&memo, &top);
            s.spawn(move || {
                let mut process = Lean {
                    commits: 2,
                    timeouts: true,
                    check: 0,
                };
                let list: Vec<PyStr> = (0..2_000)
                    .map(|i| cps(&format!("{:040x}", (i * 7 + t * 13) % 600)))
                    .collect();
                let answers = calls(memo, &mut process, 5.0, top, &list);
                for (anchor, answer) in list.iter().zip(answers) {
                    let text = to_string(anchor).unwrap();
                    let odd = text.ends_with(['1', '3', '5', '7', '9', 'b', 'd', 'f']);
                    match answer.unwrap() {
                        Some(walk) => {
                            assert!(!odd, "{text}");
                            assert_eq!(&walk.anchor, anchor);
                            assert_eq!(walk.commits.len(), 3);
                        }
                        None => assert!(odd, "{text}"),
                    }
                }
            });
        }
    });
    let keys = memo.memo_keys();
    let kept = memo.timed_out_entries();
    assert_eq!(keys.len(), WALK_MEMO_CAP);
    assert_eq!(kept.len(), WALK_MEMO_CAP);
    let distinct: HashSet<_> = keys.iter().map(|k| k.anchor.clone()).collect();
    assert_eq!(distinct.len(), keys.len());
    let distinct: HashSet<_> = kept.iter().map(|k| k.0.anchor.clone()).collect();
    assert_eq!(distinct.len(), kept.len());
}

#[test]
fn hostile_inputs_do_not_panic() {
    let sha256 = cps(&"ab".repeat(32));
    let mut process = Lean {
        commits: 0,
        timeouts: false,
        check: 0,
    };
    let memo = WalkMemo::new();
    // Hash-length anchors of SHA-256 are walked.
    let found = calls(
        &memo,
        &mut process,
        0.0,
        &cps("top"),
        std::slice::from_ref(&sha256),
    );
    assert!(matches!(&found[0], Ok(Some(w)) if w.anchor == sha256));
    // A root holding escaped bytes encodes; a lone surrogate no file
    // name holds is the runner's encoding error.
    let escaped = [cps("top"), vec![0xDCFF, 0xDC80]].concat();
    let found = calls(&memo, &mut process, 0.0, &escaped, &[cps(&"a".repeat(40))]);
    assert!(matches!(&found[0], Ok(Some(_))));
    let lone = [cps("top"), vec![0xD800]].concat();
    let found = calls(&memo, &mut process, 0.0, &lone, &[cps(&"a".repeat(40))]);
    assert_eq!(found[0], Err(Error::Git(origin_git::Error::UnicodeEncode)));
    // A walk of 200,000 commits is stored whole.
    let mut long = Lean {
        commits: 200_000,
        timeouts: false,
        check: 0,
    };
    let found = calls(&memo, &mut long, 0.0, &cps("top"), &[cps(&"e".repeat(40))]);
    assert!(matches!(&found[0], Ok(Some(w)) if w.commits.len() == 200_001));
    // Clocks at the float bounds keep or drop a timed-out walk, never panic.
    for now in [
        f64::NAN,
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::MAX,
        f64::MIN,
        0.0,
    ] {
        let memo = WalkMemo::new();
        let mut timing_out = Lean {
            commits: 0,
            timeouts: true,
            check: 0,
        };
        let odd = cps(&format!("{:040x}", 1));
        let found = calls(
            &memo,
            &mut timing_out,
            now,
            &cps("top"),
            &[odd.clone(), odd],
        );
        assert!(found.iter().all(|a| *a == Ok(None)), "{now}");
    }
    // Exit codes at the integer bounds: above 0 absent, else held.
    for check in [i64::MIN, -1, 0, 1, 128, i64::MAX] {
        let memo = WalkMemo::new();
        let (scope, env, global) = (Scope::default(), Env::new(), AtomicI64::new(0));
        let mut seen = NotADirectorySeen::new();
        let mut counts = Counts::default();
        let (mut process, mut files) = (Failing(check), Quiet);
        let mut runner = OriginRunner::new(
            Context {
                flavour: Flavour::Posix,
                scope: &scope,
                clock: &|| 0.,
                env: &env,
                global_unanswered: &global,
                first_not_a_directory: &mut seen,
            },
            &mut process,
            &mut files,
            &mut counts,
        );
        let mut unwalked = Unwalked::new();
        let found = commits_since_anchor(
            &memo,
            &mut runner,
            &mut Still { now: 0.0 },
            &mut unwalked,
            Some(b"sample"),
            &cps(&"a".repeat(40)),
            (Some(&cps("top")), Some(&cps(C))),
            None,
        );
        assert_eq!(found, Ok(None));
        assert_eq!(unwalked.count(), u64::from(check <= 0), "exit {check}");
    }
}

#[test]
fn the_windows_flavour_declines() {
    common::origin_reach_walk::windows_flavour_declines();
}

#[test]
fn reads_without_the_hold_or_the_clock_decline() {
    let top = cps("top");
    let lean = |timeouts| Lean {
        commits: 0,
        timeouts,
        check: 0,
    };
    let (even, odd) = (cps(&format!("{:040x}", 2)), cps(&format!("{:040x}", 1)));
    // No hold: a listed walk declines where it would be stored, and a
    // timed-out one where it would be kept.
    let memo = WalkMemo::new();
    let found = calls_over(
        &memo,
        &mut lean(true),
        &mut NoHold,
        &top,
        &[even.clone(), odd.clone()],
    );
    assert_eq!(found, [Err(Error::NotPorted), Err(Error::NotPorted)]);
    assert!(memo.memo_keys().is_empty());
    assert!(memo.timed_out_entries().is_empty());
    // No clock: a listed walk is stored; a timed-out one declines where it
    // would be kept.
    let found = calls_over(
        &memo,
        &mut lean(true),
        &mut NoClock,
        &top,
        &[even.clone(), odd.clone()],
    );
    assert!(matches!(&found[0], Ok(Some(w)) if w.listed));
    assert_eq!(found[1], Err(Error::NotPorted));
    assert_eq!(memo.memo_keys().len(), 1);
    assert!(memo.timed_out_entries().is_empty());
    // A kept entry declines where it would be served, and stays.
    let key = WalkKey {
        root: top.clone(),
        anchor: odd.clone(),
        head: cps(C),
        files: still_files(),
    };
    memo.keep_timed_out(&key, Kept::Unread(HistoryUnread::Timeout), 0.0);
    let found = calls_over(&memo, &mut lean(true), &mut NoClock, &top, &[odd]);
    assert_eq!(found, [Err(Error::NotPorted)]);
    assert_eq!(memo.timed_out_entries().len(), 1);
}
