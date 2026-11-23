//! Cutoff, native path bytes, effect order and thread-local counting.

use bettermemory_core::fsdata::realpath::Unresolved;
use bettermemory_core::fsdata::{Flavour, PyErr};
use bettermemory_core::git_calls::RunRaised;
use bettermemory_core::git_read_budget::{ReadBudget, Scope};
use bettermemory_core::git_run::{GitProcess, Spawned};
use bettermemory_core::gitfile_rule::discovery::Reading;
use bettermemory_core::gitfile_rule::gitdir::Failure;
use bettermemory_core::gitfile_rule::trust::Refusal;
use bettermemory_core::gitsystem::{answers, ask::Env};
use bettermemory_core::origin_dates::OriginGit;
use bettermemory_core::origin_git::{
    Completed, Context, Counts, Error, Files, Level, Logger, OriginRunner, Output,
};
use bettermemory_core::origin_gitfiles::root::NotADirectorySeen;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::records::Origin;
use bettermemory_core::unicode::cps;
use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::sync::atomic::{AtomicI64, Ordering};

struct Script {
    reply: Spawned,
    calls: Cell<usize>,
    reading: Reading,
    reading_error: Option<Error>,
    discovery: Cell<usize>,
    resolve: Option<Result<Vec<u8>, Unresolved>>,
    directory: bool,
    agrees: bool,
    refused: bool,
}
impl Default for Script {
    fn default() -> Self {
        Self {
            reply: Spawned::TimedOut,
            calls: Cell::new(0),
            reading: Reading::default(),
            reading_error: None,
            discovery: Cell::new(0),
            resolve: None,
            directory: true,
            agrees: false,
            refused: false,
        }
    }
}
impl Files for &Script {
    fn reading(&mut self, _: &[u8]) -> Result<Option<Reading>, Error> {
        self.discovery.set(self.discovery.get() + 1);
        self.reading_error
            .map_or_else(|| Ok(Some(self.reading.clone())), Err)
    }
    fn stat_dir(&self, _: &[u8]) -> Result<bool, Failure> {
        Ok(self.directory)
    }
    fn refused_root(&mut self, _: &[u8]) -> Result<(Option<Vec<u8>>, bool), Error> {
        Ok((None, self.refused))
    }
    fn agrees(&mut self, _: &[u8], _: &[u8]) -> Result<bool, Error> {
        Ok(self.agrees)
    }
    fn resolve(&mut self, _: &[u8]) -> Result<Vec<u8>, Unresolved> {
        self.resolve.clone().expect("unexpected resolve")
    }
}
impl GitProcess for &Script {
    fn run(
        &mut self,
        _: &[PyStr],
        _: Option<&[u32]>,
        _: &Env,
        _: Option<&[u8]>,
        _: f64,
    ) -> Spawned {
        self.calls.set(self.calls.get() + 1);
        self.reply.clone()
    }
}
fn with<T>(
    script: &Script,
    flavour: Flavour,
    scope: &Scope,
    clock: &dyn Fn() -> f64,
    global: &AtomicI64,
    body: impl FnOnce(&mut OriginRunner<'_>) -> T,
) -> T {
    let (mut proc, mut files, mut counts, mut seen) =
        (script, script, Counts::default(), NotADirectorySeen::new());
    let env = Env::new();
    let context = Context {
        env: &env,
        global_unanswered: global,
        scope,
        clock,
        flavour,
        first_not_a_directory: &mut seen,
    };
    body(&mut OriginRunner::new(
        context,
        &mut proc,
        &mut files,
        &mut counts,
    ))
}
fn done(out: &[u8], err: &[u8], code: i64) -> Spawned {
    Spawned::Exited {
        returncode: code,
        stdout: out.to_vec(),
        stderr: err.to_vec(),
    }
}

#[test]
fn denial_precedes_budget_and_deadlines_stop_followups() {
    let budget = Rc::new(RefCell::new(ReadBudget {
        deadline: Some(1.),
        ..ReadBudget::default()
    }));
    let scope = Scope {
        active: Some(budget.clone()),
        ..Scope::default()
    };
    let global = AtomicI64::new(0);
    let refusal = Script {
        reading: Reading {
            why: Some(Refusal {
                why: b"foreign".to_vec(),
                format_unknown: false,
            }),
            ..Reading::default()
        },
        ..Script::default()
    };
    with(&refusal, Flavour::Posix, &scope, &|| 2., &global, |r| {
        let result = r.identity_result(b"p", &[cps("test")]).unwrap().unwrap();
        assert_eq!(result.returncode, 128);
        assert_eq!(result.args, vec![cps("git"), cps("test")]);
        assert_eq!(r.unanswered_git_calls(), 0);
        assert_eq!(r.failed_git_calls(), 1);
        assert_eq!(r.unanswered(), 0);
        assert!(budget.borrow().last_root.is_none());
    });
    let script = Script::default();
    for late in [false, true] {
        let initial = global.load(Ordering::SeqCst);
        let ticks = Cell::new(0);
        let clock = || {
            let n = ticks.replace(ticks.get() + 1);
            if late && n == 0 {
                0.75
            } else {
                2.
            }
        };
        with(&script, Flavour::Posix, &scope, &clock, &global, |r| {
            let before = script.calls.get();
            assert!(OriginGit::git(r, b"p", &[cps("status")], 1., false, false).is_none());
            assert_eq!(r.unanswered(), 0);
            assert_eq!(r.unanswered_git_calls() - initial, 1);
            assert_eq!(r.failed_git_calls(), 1);
            assert_eq!(r.counts(), (i64::from(late), 0));
            assert_eq!(script.calls.get() - before, usize::from(late));
            let entered = script.discovery.get();
            assert!(r.deadline());
            assert_eq!(r.branch(b"after"), Err(Error::Run(RunRaised::ReadDeadline)));
            r.note_failed();
            r.note_timed_out();
            assert_eq!((script.discovery.get(), r.failed_git_calls()), (entered, 1));
            assert_eq!(r.counts(), (i64::from(late), 0));
            assert_eq!(r.take_error(), Some(Error::Run(RunRaised::ReadDeadline)));
            assert!(!r.deadline());
        });
    }
}

#[test]
fn path_streams_keep_native_names_and_separate_stderr_decoding() {
    let raw_subcommand = vec![0xd800];
    assert_eq!(
        bettermemory_core::origin_git::log_subcommand(&[
            cps("-c"),
            cps("x=1"),
            raw_subcommand.clone()
        ]),
        raw_subcommand
    );
    let scope = Scope::default();
    let global = AtomicI64::new(0);
    let script = Script {
        reply: done(b"\xff\r\n", b"\xff", 0),
        ..Script::default()
    };
    for flavour in [Flavour::Posix, Flavour::Windows] {
        with(&script, flavour, &scope, &|| 0., &global, |r| {
            let args = [cps("remote")];
            let result = r.result(b"here", &args, 1., None, true);
            if flavour == Flavour::Windows {
                assert_eq!(
                    result,
                    Err(Error::Decode(answers::Raised::UnicodeDecodeError))
                );
            } else {
                let Completed { stdout, stderr, .. } = result.unwrap().unwrap();
                assert_eq!(stdout, vec![0xdcff, 13, 10]);
                assert_eq!(stderr, vec![0xfffd]);
            }
            assert_eq!(
                r.output(b"here", &args, Output::default()).unwrap(),
                Some(vec![0xfffd])
            );
            assert_eq!((r.unanswered(), r.failed_git_calls()), (0, 0));
        });
    }
    let encoded = Script {
        reply: done(&[0xed, 0xa0, 0x80, 10], b"", 0),
        ..Script::default()
    };
    with(&encoded, Flavour::Windows, &scope, &|| 0., &global, |r| {
        assert_eq!(r.branch(b"here").unwrap(), Some(vec![0xd800]))
    });
}

#[test]
fn worktree_first_warning_stderr_fallback_and_resolution_order() {
    let scope = Scope::default();
    let global = AtomicI64::new(0);
    let file = Script {
        directory: false,
        ..Script::default()
    };
    with(&file, Flavour::Posix, &scope, &|| 0., &global, |r| {
        for level in [Level::Warning, Level::Debug] {
            assert_eq!(r.probe_worktree_root(b"no").unwrap(), (None, true, None));
            assert_eq!(
                r.lines.last().map(|(l, v, _)| (*l, *v)),
                Some((Logger::Origin, level))
            );
        }
        assert_eq!(
            (file.calls.get(), file.discovery.get(), r.failed_git_calls()),
            (0, 0, 2)
        );
    });
    for (stderr, reason) in [
        (&b" \x0b "[..], "exit 23"),
        (" \u{2028}first\u{85}second ".as_bytes(), "first"),
    ] {
        let refused = Script {
            reply: done(b"", stderr, 23),
            refused: true,
            ..Script::default()
        };
        with(&refused, Flavour::Posix, &scope, &|| 0., &global, |r| {
            assert_eq!(
                r.probe_worktree_root(b"p").unwrap(),
                (None, true, Some(cps(reason)))
            );
            assert_eq!(r.lines[0].0, Logger::Git);
            assert_eq!(r.lines[1].1, Level::Info);
            assert_eq!(r.failed_git_calls(), 0);
        });
    }
    for agree in [false, true] {
        let script = Script {
            reply: done(b"/raw \n", b"", 0),
            agrees: agree,
            resolve: agree.then_some(Err(Unresolved::Raised(PyErr::Permission))),
            ..Script::default()
        };
        with(&script, Flavour::Posix, &scope, &|| 0., &global, |r| {
            assert_eq!(r.worktree_root(b"p").unwrap(), agree.then(|| cps("/raw ")));
            assert_eq!(r.lines.len(), usize::from(!agree));
        });
    }
}

#[test]
fn provisional_follow_checks_only_a_record_with_a_repository() {
    let scope = Scope::default();
    let global = AtomicI64::new(0);
    let script = Script {
        reading: Reading {
            provisional: true,
            ..Reading::default()
        },
        ..Script::default()
    };
    with(&script, Flavour::Posix, &scope, &|| 0., &global, |r| {
        for (cwd, root, yes) in [
            (None, None, false),
            (Some(vec![]), Some(cps("/")), false),
            (Some(cps("x")), None, false),
            (Some(cps("x")), Some(vec![]), true),
        ] {
            assert_eq!(
                r.follow_unsettled(&Origin {
                    cwd,
                    worktree_root: root,
                    ..Origin::default()
                })
                .unwrap(),
                yes
            );
        }
    });
    assert_eq!((script.discovery.get(), script.calls.get()), (1, 0));
}

#[test]
fn worker_threads_have_separate_local_counts_and_one_global_total() {
    let global = AtomicI64::new(0);
    std::thread::scope(|threads| {
        let workers: Vec<_> = (1..=4)
            .map(|i| {
                let shared = &global;
                threads.spawn(move || {
                    with(
                        &Script::default(),
                        Flavour::Posix,
                        &Scope::default(),
                        &|| 0.,
                        shared,
                        |r| {
                            for _ in 0..i {
                                assert_eq!(r.branch(b"p").unwrap(), None);
                            }
                            assert_eq!(r.unanswered(), i);
                            assert_eq!(r.failed_git_calls(), i as i64);
                        },
                    );
                })
            })
            .collect();
        for worker in workers {
            worker.join().unwrap();
        }
    });
    assert_eq!(global.load(Ordering::SeqCst), 10);
}

#[test]
fn unavailable_windows_relative_directory_propagates_before_process_or_counts() {
    use bettermemory_core::fsdata::StdFs;
    use bettermemory_core::gitfile_rule::discovery::Rule;
    use bettermemory_core::gitfile_rule::follow::NoRecords;
    use bettermemory_core::gitfile_rule::format::GitFacts;
    use bettermemory_core::gitfile_rule::gitdir::Ctx;
    use bettermemory_core::origin_git::RepositoryFiles;
    use bettermemory_core::origin_gitfiles::root::ConfigMemo;
    let ctx = Ctx {
        fs: &StdFs,
        flavour: Flavour::Windows,
        cwd: b"D:\\scratch",
    };
    let facts = GitFacts {
        release: Some((2, 54)),
        reads_compat: Some(true),
    };
    let rule = Rule {
        ctx: &ctx,
        answers: &StdFs,
        records: &NoRecords,
        env: None,
        git: &facts,
    };
    let env = Env::new();
    let scope = Scope::default();
    let global = AtomicI64::new(0);
    let mut seen = NotADirectorySeen::new();
    let mut memo = ConfigMemo::new();
    let mut files = RepositoryFiles {
        rule: &rule,
        config_memo: &mut memo,
        head_env: &Default::default(),
        darwin: false,
    };
    let process = Script::default();
    let mut proc = &process;
    let mut counts = Counts::default();
    let mut r = OriginRunner::new(
        Context {
            flavour: Flavour::Windows,
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
    assert_eq!(
        r.probe_worktree_root(b"relative"),
        Err(Error::Files(Failure::NotPorted))
    );
    assert_eq!((r.failed_git_calls(), process.calls.get()), (0, 0));
}

#[test]
fn subcommand_borrows_the_selected_argument_even_with_a_long_trailing_pathspec() {
    use bettermemory_core::origin_git::log_subcommand;
    let mut args = vec![cps("-c"), cps("x=1"), cps("-c"), cps("y=2"), vec![0xd800]];
    args.extend((0..20_000).map(|_| vec![97; 96]));
    let named = log_subcommand(&args);
    assert!(std::ptr::eq(named, args[4].as_slice()));
    let standalone = vec![cps("-c")];
    assert!(std::ptr::eq(
        log_subcommand(&standalone),
        standalone[0].as_slice()
    ));
    assert!(log_subcommand(&standalone[..0]).is_empty());
}

#[test]
fn an_unencodable_record_cwd_is_unsettled_only_if_discovery_can_read_it() {
    let script = Script {
        reading: Reading {
            provisional: true,
            ..Reading::default()
        },
        ..Script::default()
    };
    with(
        &script,
        Flavour::Posix,
        &Scope::default(),
        &|| 0.,
        &AtomicI64::default(),
        |r| {
            for (code, expected) in [
                (0xd800, false),
                (0xdc00, false),
                (0xdc7f, false),
                (0xdc80, true),
                (0xdcff, true),
            ] {
                let cwd = [cps("/bad"), vec![code]].concat();
                let found = Origin {
                    cwd: Some(cwd.clone()),
                    worktree_root: Some(cps("/world")),
                    ..Origin::default()
                };
                let before = script.discovery.get();
                assert_eq!(r.follow_unsettled(&found), Ok(expected));
                assert_eq!(script.discovery.get() - before, usize::from(expected));
                if !expected {
                    assert_eq!(r.agreed(b"/world", &cwd), Ok(false));
                    assert_eq!(
                        r.lines.pop(),
                        Some((
                            Logger::Origin,
                            Level::Info,
                            [
                                cps("git names "),
                                cwd,
                                cps(" for /world, not membership's root; no origin is kept")
                            ]
                            .concat()
                        ))
                    );
                    assert_eq!(script.discovery.get() - before, 1);
                }
            }
        },
    );
    let failed = Script {
        reading_error: Some(Error::Files(Failure::NotPorted)),
        ..Script::default()
    };
    with(
        &failed,
        Flavour::Posix,
        &Scope::default(),
        &|| 0.,
        &AtomicI64::default(),
        |r| {
            assert_eq!(
                r.agreed(b"/world", &[0xd800]),
                Err(Error::Files(Failure::NotPorted))
            );
            assert!(r.lines.is_empty());
            assert_eq!(failed.discovery.get(), 1);
        },
    );
}

#[test]
fn manual_timeout_after_a_completed_read_only_counts_a_failed_and_timed_out_call() {
    let script = Script {
        reply: done(b"main\n", b"", 0),
        ..Script::default()
    };
    with(
        &script,
        Flavour::Posix,
        &Scope::default(),
        &|| 0.,
        &AtomicI64::default(),
        |r| {
            assert_eq!(r.branch(b"project"), Ok(Some(cps("main"))));
            assert_eq!((r.counts(), r.failed_git_calls()), ((0, 0), 0));
            r.note_timed_out();
            assert_eq!((r.counts(), r.failed_git_calls()), ((1, 0), 1));
            assert_eq!(
                (r.unanswered(), r.unanswered_git_calls(), script.calls.get()),
                (0, 0, 1)
            );
        },
    );
}
