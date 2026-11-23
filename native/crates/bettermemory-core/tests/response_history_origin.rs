//! Two passes across a real commit, using the production location, date
//! memo and runner counter adapters. The pair fallback invokes Git in the
//! temporary checkout. Only the shallow guard and file readers are supplied.
#[cfg(unix)]
mod common;

#[cfg(unix)]
mod unix {
    use crate::common::origin_dates_memo::Step;
    use crate::common::origin_history_located::Scratch;
    use bettermemory_core::fsdata::{Flavour, StdFs};
    use bettermemory_core::git_calls::Tally;
    use bettermemory_core::git_read_budget::Scope;
    use bettermemory_core::gitfile_rule::bounded::Hung;
    use bettermemory_core::gitfile_rule::discovery::Rule;
    use bettermemory_core::gitfile_rule::follow::{fsencode, NoRecords};
    use bettermemory_core::gitfile_rule::format::GitFacts;
    use bettermemory_core::gitfile_rule::gitdir::Ctx;
    use bettermemory_core::githead::discovery::Env;
    use bettermemory_core::origin_dates::{
        memo::{Reader, Timestamps},
        GitResult, OriginGit,
    };
    use bettermemory_core::origin_history::{HistoryUnread, TimestampsMemo, ValueError};
    use bettermemory_core::pyvalue::PyStr;
    use bettermemory_core::response_history::{
        posix::{OriginReads, UnsupportedPath},
        HistoryPass, HistoryReads,
    };
    use serde_json::json;
    use std::cell::Cell;
    use std::ffi::OsStr;
    use std::os::unix::ffi::OsStrExt;
    use std::path::Path;
    use std::process::Command;
    use std::rc::Rc;
    use std::sync::Arc;

    fn cps(s: &str) -> PyStr {
        s.chars().map(u32::from).collect()
    }
    fn git(cwd: &Path, args: &[&str]) -> String {
        let out = Command::new("git")
            .env_clear()
            .env("PATH", std::env::var_os("PATH").unwrap_or_default())
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_AUTHOR_NAME", "tester")
            .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
            .env("GIT_COMMITTER_NAME", "tester")
            .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
            .current_dir(cwd)
            .args(args)
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "git {args:?}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8(out.stdout).unwrap()
    }
    #[derive(Debug)]
    enum Error {
        Cut,
        Unsupported(UnsupportedPath),
    }
    impl From<ValueError> for Error {
        fn from(_: ValueError) -> Self {
            Self::Cut
        }
    }
    impl From<UnsupportedPath> for Error {
        fn from(e: UnsupportedPath) -> Self {
            Self::Unsupported(e)
        }
    }

    #[derive(Default)]
    struct Runner {
        calls: Vec<(Vec<u8>, Vec<PyStr>)>,
        tally: Tally,
        deadline: Rc<Cell<bool>>,
    }
    impl OriginGit for Runner {
        fn git(&mut self, cwd: &[u8], args: &[PyStr], _: f64, _: bool, _: bool) -> Option<PyStr> {
            self.calls.push((cwd.to_vec(), args.to_vec()));
            if self.deadline.get() {
                return None;
            }
            let args: Vec<String> = args
                .iter()
                .map(|a| a.iter().map(|&c| char::from_u32(c).unwrap()).collect())
                .collect();
            Some(cps(&git(
                Path::new(OsStr::from_bytes(cwd)),
                &args.iter().map(String::as_str).collect::<Vec<_>>(),
            )))
        }
        fn git_result(&mut self, _: &[u8], _: &[PyStr], _: f64) -> Option<GitResult> {
            panic!("unexpected git_result")
        }
        fn counts(&self) -> (i64, i64) {
            (self.tally.timed_out_calls(), self.tally.missing_calls())
        }
        fn note_failed(&mut self) {
            self.tally.note_failed()
        }
        fn note_timed_out(&mut self) {
            self.tally.note_timed_out()
        }
        fn deadline(&self) -> bool {
            self.deadline.get()
        }
    }

    #[test]
    fn two_passes_observe_their_own_commits() {
        let dir = Scratch::new("drift-pass");
        let sub = dir.0.join("sub");
        std::fs::create_dir(&sub).unwrap();
        git(&dir.0, &["init", "-q", "--initial-branch=main"]);
        git(
            &dir.0,
            &[
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--allow-empty",
                "-qm",
                "first",
            ],
        );
        let sub_text = cps(sub.to_str().unwrap());
        let root = cps(dir.0.to_str().unwrap());
        let ctx = Ctx {
            fs: &StdFs,
            flavour: Flavour::Posix,
            cwd: dir.0.as_os_str().as_bytes(),
        };
        let rule = Rule {
            ctx: &ctx,
            answers: &Hung::default(),
            records: &NoRecords,
            env: None,
            git: &GitFacts {
                release: Some((2, 54)),
                reads_compat: Some(true),
            },
        };
        let state = Step::new(&json!({"g":[],"l":{"cuts":[[],[],[]],"held":[true,true]}}));
        let mut runner = Runner::default();
        let deadline = runner.deadline.clone();
        let mut memo = TimestampsMemo::new();
        let mut scope = Scope::default();
        let moved = Cell::new(false);
        let make_env = || Env {
            git_dir: moved.get().then(|| b"elsewhere".to_vec()),
            ..Env::default()
        };
        let count = Cell::new(0);
        let mut files = |_: &mut Scope, _: &[u32]| Ok(None);
        let mut pair = |_: &mut Scope, path: &[u32]| {
            assert_eq!(path, sub_text);
            count.set(count.get() + 1);
            let out = git(&sub, &["rev-parse", "--show-toplevel", "HEAD"]);
            let mut lines = out.lines();
            Ok(Some((
                cps(lines.next().unwrap()),
                cps(lines.next().unwrap()),
            )))
        };
        let mut reads = OriginReads::<_, _, _, _, _, Error> {
            files_pair: &mut files,
            git_pair: &mut pair,
            env: &make_env,
            rule: &rule,
            reader: Reader {
                git: &mut runner,
                files: &mut state.clone(),
                leaves: &mut state.clone(),
                hold: &mut state.clone(),
                memo: &mut memo,
                clock: &|| 0.0,
            },
        };
        let mut first = HistoryPass::open(&mut reads, &mut scope, sub_text.clone(), false).unwrap();
        let first_key = first
            .keyed_pair(&mut reads, &mut scope, true)
            .unwrap()
            .unwrap()
            .clone();
        assert_eq!(first_key.0, root);
        let Timestamps::Dates(one) = first.timestamps(&mut reads, &mut scope).unwrap() else {
            panic!("first dates")
        };
        assert_eq!(one.len(), 1);
        git(
            &dir.0,
            &[
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--allow-empty",
                "-qm",
                "second",
            ],
        );
        let Timestamps::Dates(hit) = first.timestamps(&mut reads, &mut scope).unwrap() else {
            panic!("cached dates")
        };
        assert!(Arc::ptr_eq(&one, &hit));
        let mut second = HistoryPass::open(&mut reads, &mut scope, sub_text.clone(), true).unwrap();
        let second_key = second
            .keyed_pair(&mut reads, &mut scope, true)
            .unwrap()
            .unwrap()
            .clone();
        assert_eq!(second_key.0, root);
        assert_ne!(second_key.1, first_key.1);
        let Timestamps::Dates(two) = second.timestamps(&mut reads, &mut scope).unwrap() else {
            panic!("second dates")
        };
        assert_eq!(two.len(), 2);
        moved.set(true);
        let mut third = HistoryPass::open(&mut reads, &mut scope, sub_text.clone(), false).unwrap();
        assert!(third
            .keyed_pair(&mut reads, &mut scope, true)
            .unwrap()
            .is_none());
        assert!(third.history_applies(&mut reads, &mut scope).unwrap());
        assert_eq!(count.get(), 3);
        // The memo's own unread decorator turns this Git deadline into a
        // returned answer. The pass keeps it even after the runner recovers.
        reads.reader.memo.clear();
        deadline.set(true);
        assert_eq!(
            third.timestamps(&mut reads, &mut scope).unwrap(),
            Timestamps::Unread(HistoryUnread::CallBudget)
        );
        deadline.set(false);
        assert_eq!(
            third.timestamps(&mut reads, &mut scope).unwrap(),
            Timestamps::Unread(HistoryUnread::CallBudget)
        );
        assert!(reads.located_head(&mut scope, &[0xd800]).unwrap().is_none());
        let Err(bettermemory_core::git_read_budget::Raise::Other(Error::Unsupported(path))) =
            reads.dates(&mut scope, &[0xd800], None)
        else {
            panic!("unsupported date path")
        };
        assert_eq!(path.0, [0xd800]);
        reads
            .note_unread(&mut scope, HistoryUnread::Timeout)
            .unwrap();
        reads
            .note_unread(&mut scope, HistoryUnread::CallBudget)
            .unwrap();
        assert_eq!(runner.calls.len(), 3);
        assert_eq!(
            runner.calls[0],
            (
                fsencode(&sub_text).unwrap(),
                vec![cps("log"), cps("--format=%at"), first_key.1]
            )
        );
        assert_eq!(
            runner.calls[1],
            (
                fsencode(&sub_text).unwrap(),
                vec![cps("log"), cps("--format=%at"), second_key.1]
            )
        );
        assert_eq!(runner.tally.failed_calls(), 3);
        assert_eq!(runner.tally.timed_out_calls(), 1);
    }
}
