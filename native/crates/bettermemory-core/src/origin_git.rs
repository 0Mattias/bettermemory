//! The Git runner and repository identity probes of `origin.py`. Each thread
//! owns its counts; all runners of the process share the could-not-run counter.

use std::collections::HashSet;
use std::sync::atomic::AtomicI64;

use crate::claims::strip;
use crate::fsdata::realpath::{realpath, Unresolved};
use crate::fsdata::{Flavour, Fs, Kind, PyErr};
use crate::git_calls::{self, RunRaised, Tally};
use crate::git_read_budget::Scope;
pub use crate::git_run::subcommand_of as log_subcommand;
use crate::git_run::{self, Call, GitProcess, Out};
use crate::gitfile_rule::bounded::Answers;
use crate::gitfile_rule::discovery::{self, Reading, Rule};
use crate::gitfile_rule::follow::Records;
use crate::gitfile_rule::gitdir::Failure;
use crate::githead::discovery::Env as HeadEnv;
use crate::gitsystem::answers;
use crate::gitsystem::ask::Env;
use crate::origin_dates::{GitResult, OriginGit};
use crate::origin_gitfiles::root::{self, ConfigMemo, NotADirectorySeen, RootFault};
use crate::origin_history::{AS_STORED_CONFIG, AS_STORED_ENV};
use crate::pyvalue::PyStr;
use crate::records::Origin;
use crate::response_write::{fsencode_as, FsEncoding};
use crate::unicode::cps;

/// All three identities are distinct: the logger, the level and the text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Logger {
    Git,
    Origin,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Level {
    Debug,
    Info,
    Warning,
}
pub type Line = (Logger, Level, PyStr);

/// A failure the Python code does not fold into an unanswered Git call.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Error {
    Run(RunRaised),
    Files(Failure),
    RefusedRoot(RootFault),
    Resolve(Unresolved),
    Decode(answers::Raised),
    UnicodeEncode,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Completed {
    pub args: Vec<PyStr>,
    pub returncode: i64,
    pub stdout: PyStr,
    pub stderr: PyStr,
}

/// `(worktree_root, indeterminate, refusal)`.
pub type Root = (Option<PyStr>, bool, Option<PyStr>);
/// `(remote_url, alternates, indeterminate)`.
pub type Remote = (Option<PyStr>, Vec<PyStr>, bool);

/// The file reads alongside each Git process. A scripted provider may
/// supply either platform; [`RepositoryFiles`] uses the ported OS readers.
pub trait Files {
    fn reading(&mut self, cwd: &[u8]) -> Result<Option<Reading>, Error>;
    fn stat_dir(&self, cwd: &[u8]) -> Result<bool, Failure>;
    fn refused_root(&mut self, cwd: &[u8]) -> Result<(Option<Vec<u8>>, bool), Error>;
    fn agrees(&mut self, cwd: &[u8], top: &[u8]) -> Result<bool, Error>;
    fn resolve(&mut self, path: &[u8]) -> Result<Vec<u8>, Unresolved>;
}

/// The discovery rules and file-signature memo used by origin's Git reads.
pub struct RepositoryFiles<'a, F: Fs, A: Answers + ?Sized, R: Records + ?Sized> {
    pub rule: &'a Rule<'a, F, A, R>,
    pub config_memo: &'a mut ConfigMemo,
    pub head_env: &'a HeadEnv,
    pub darwin: bool,
}

impl<F: Fs, A: Answers + ?Sized, R: Records + ?Sized> Files for RepositoryFiles<'_, F, A, R> {
    fn reading(&mut self, cwd: &[u8]) -> Result<Option<Reading>, Error> {
        discovery::discovery_reading(self.rule, cwd).map_err(Error::Files)
    }
    fn stat_dir(&self, cwd: &[u8]) -> Result<bool, Failure> {
        let path = self.rule.ctx.at(cwd)?;
        self.rule
            .ctx
            .fs
            .stat(&path)
            .map(|s| s.kind == Kind::Directory)
            .map_err(Failure::from)
    }
    fn refused_root(&mut self, cwd: &[u8]) -> Result<(Option<Vec<u8>>, bool), Error> {
        root::refused_worktree_root(self.config_memo, self.rule, self.head_env, cwd, self.darwin)
            .map_err(Error::RefusedRoot)
    }
    fn agrees(&mut self, cwd: &[u8], top: &[u8]) -> Result<bool, Error> {
        discovery::toplevel_agrees(self.rule, cwd, top).map_err(Error::Files)
    }
    fn resolve(&mut self, path: &[u8]) -> Result<Vec<u8>, Unresolved> {
        let ctx = self.rule.ctx;
        realpath(ctx.fs, ctx.flavour, path, ctx.cwd)
    }
}

#[derive(Default, Debug)]
pub struct Counts {
    /// Shared with other users of `_git` on the calling thread.
    pub tally: Tally,
    /// `origin._UNANSWERED.count`, for _git and _identity_result only.
    unanswered: u64,
}

pub struct Context<'a> {
    pub flavour: Flavour,
    pub scope: &'a Scope,
    pub clock: &'a dyn Fn() -> f64,
    pub env: &'a Env,
    /// Use `git_calls::UNANSWERED` in the process.
    pub global_unanswered: &'a AtomicI64,
    pub first_not_a_directory: &'a mut NotADirectorySeen,
}

pub struct OriginRunner<'a> {
    context: Context<'a>,
    process: &'a mut dyn GitProcess,
    files: &'a mut dyn Files,
    counts: &'a mut Counts,
    /// The lines the two Python loggers received, in order.
    pub lines: Vec<Line>,
    pending: Option<Error>,
}

fn argv(words: &[&str]) -> Vec<PyStr> {
    words.iter().map(|s| cps(s)).collect()
}

fn without_lf(text: &[u32]) -> &[u32] {
    text.strip_suffix(&[10]).unwrap_or(text)
}

impl<'a> OriginRunner<'a> {
    pub fn new(
        context: Context<'a>,
        process: &'a mut dyn GitProcess,
        files: &'a mut dyn Files,
        counts: &'a mut Counts,
    ) -> Self {
        Self {
            context,
            process,
            files,
            counts,
            lines: Vec::new(),
            pending: None,
        }
    }

    pub fn unanswered(&self) -> u64 {
        self.counts.unanswered
    }

    pub fn unanswered_git_calls(&self) -> i64 {
        git_calls::unanswered_calls(self.context.global_unanswered)
    }

    pub fn failed_git_calls(&self) -> i64 {
        self.counts.tally.failed
    }

    /// Recover the original error after a caller used the Option-returning
    /// OriginGit adapter. It suppresses subsequent effects until consumed.
    pub fn take_error(&mut self) -> Option<Error> {
        self.pending.take()
    }

    fn decode(&self, bytes: &[u8]) -> Result<PyStr, Error> {
        answers::fsdecode(self.context.flavour, bytes).map_err(Error::Decode)
    }

    fn encode(&self, text: &[u32]) -> Option<Vec<u8>> {
        let encoding = match self.context.flavour {
            Flavour::Posix => FsEncoding::SurrogateEscape,
            Flavour::Windows => FsEncoding::SurrogatePass,
        };
        fsencode_as(text, encoding)
    }

    fn origin_line(&mut self, level: Level, parts: &[&[u32]]) {
        self.lines.push((Logger::Origin, level, parts.concat()));
    }

    fn git_line(&mut self, (level, message): git_run::Line) {
        let level = match level {
            git_run::Level::Warning => Level::Warning,
            git_run::Level::Debug => Level::Debug,
        };
        self.lines.push((Logger::Git, level, message));
    }

    /// `_git_result`: the refused entry is an answered exit 128, before
    /// asking the budget. Otherwise only run_git counts an unstarted Git.
    pub fn result(
        &mut self,
        cwd: &[u8],
        args: &[PyStr],
        timeout: f64,
        stdin: Option<&[u8]>,
        paths: bool,
    ) -> Result<Option<Completed>, Error> {
        if let Some(error) = self.pending {
            return Err(error);
        }
        let cwd_text = self.decode(cwd)?;
        if let Some(why) = self.files.reading(cwd)?.and_then(|r| r.why) {
            let why = self.decode(&why.why)?;
            self.origin_line(
                Level::Debug,
                &[&cps("git is not run in "), &cwd_text, &cps(": "), &why],
            );
            let mut original = argv(&["git"]);
            original.extend_from_slice(args);
            return Ok(Some(Completed {
                args: original,
                returncode: 128,
                stdout: Vec::new(),
                stderr: [&cps("fatal: not a git repository: ")[..], &why, &[10]].concat(),
            }));
        }
        let mut as_stored = argv(&AS_STORED_CONFIG);
        as_stored.extend_from_slice(args);
        let extra: Env = AS_STORED_ENV
            .iter()
            .map(|(key, value)| (cps(key), cps(value)))
            .collect();
        let call = Call {
            cwd: Some(&cwd_text),
            timeout,
            stdin,
            text: !paths,
            env: &extra,
            ..Call::new(&as_stored)
        };
        let (result, lines) = git_run::run_git(
            self.context.scope,
            self.context.clock,
            self.context.global_unanswered,
            &mut self.counts.tally,
            self.process,
            self.context.env,
            &call,
            || self.files.stat_dir(cwd).unwrap_or(false),
        );
        for line in lines {
            self.git_line(line);
        }
        let Some(done) = result.map_err(Error::Run)? else {
            return Ok(None);
        };
        let stdout = match done.stdout {
            Out::Text(value) => value,
            Out::Bytes(raw) => self.decode(&raw)?,
        };
        let stderr = match done.stderr {
            Out::Text(value) => value,
            Out::Bytes(raw) => answers::text(&raw),
        };
        Ok(Some(Completed {
            args: done.args,
            returncode: done.returncode,
            stdout,
            stderr,
        }))
    }

    fn nonzero(&mut self, args: &[PyStr], done: &Completed, cwd: &[u8]) -> Result<(), Error> {
        let cwd_text = self.decode(cwd)?;
        self.git_line(git_run::log_nonzero_exit(
            args,
            done.returncode,
            &Out::Text(done.stderr.clone()),
            Some(&cwd_text),
        ));
        Ok(())
    }

    pub fn output(
        &mut self,
        cwd: &[u8],
        args: &[PyStr],
        options: Output<'_>,
    ) -> Result<Option<PyStr>, Error> {
        let Some(done) = self.result(cwd, args, options.timeout, options.stdin, options.paths)?
        else {
            self.counts.unanswered += 1;
            return Ok(None);
        };
        if done.returncode != 0 {
            self.nonzero(args, &done, cwd)?;
            self.counts.tally.note_failed();
            return Ok(None);
        }
        let trimmed = strip(&done.stdout);
        if trimmed.is_empty() && !options.empty_ok {
            self.counts.tally.note_failed();
            return Ok(None);
        }
        Ok(Some(if options.verbatim && !trimmed.is_empty() {
            done.stdout
        } else {
            trimmed.to_vec()
        }))
    }

    pub fn identity_result(
        &mut self,
        cwd: &[u8],
        args: &[PyStr],
    ) -> Result<Option<Completed>, Error> {
        let result = self.result(cwd, args, 1.0, None, true)?;
        match &result {
            None => self.counts.unanswered += 1,
            Some(done) if done.returncode != 0 => {
                self.nonzero(args, done, cwd)?;
                self.counts.tally.note_failed();
            }
            _ => {}
        }
        Ok(result)
    }

    pub fn branch(&mut self, cwd: &[u8]) -> Result<Option<PyStr>, Error> {
        let result = self.identity_result(cwd, &argv(&["symbolic-ref", "--short", "HEAD"]))?;
        Ok(result.filter(|r| r.returncode == 0).and_then(|r| {
            let name = without_lf(&r.stdout);
            (!name.is_empty()).then(|| name.to_vec())
        }))
    }

    pub fn remote_url_and_alternates(&mut self, cwd: &[u8]) -> Result<Remote, Error> {
        let unknown = (None, vec![], true);
        let absent = (None, vec![], false);
        let mut name = cps("origin");
        let mut get = argv(&["remote", "get-url", "origin"]);
        let Some(mut found) = self.identity_result(cwd, &get)? else {
            return Ok(unknown);
        };
        if found.returncode != 0 {
            let list = self.identity_result(cwd, &argv(&["remote"]))?;
            let Some(list) = list.filter(|r| r.returncode == 0) else {
                return Ok(unknown);
            };
            let mut names = without_lf(&list.stdout).split(|c| *c == 10);
            let first = names.next().unwrap_or_default();
            if first.is_empty() {
                return Ok(absent);
            }
            if first == name || names.any(|other| other == name) {
                return Ok(unknown);
            }
            name = first.to_vec();
            get[2].clone_from(&name);
            let next = self.identity_result(cwd, &get)?;
            let Some(next) = next.filter(|r| r.returncode == 0) else {
                return Ok(unknown);
            };
            found = next;
        }
        let url = without_lf(&found.stdout);
        if url.is_empty() {
            return Ok(absent);
        }
        let mut query = argv(&["config", "--null", "--get-all"]);
        query.push([&cps("remote.")[..], &name, &cps(".url")].concat());
        let raw = self.identity_result(cwd, &query)?;
        let mut seen = HashSet::from([url]);
        let mut alternates = Vec::new();
        if let Some(raw) = raw.as_ref().filter(|r| r.returncode == 0) {
            for value in raw.stdout.split(|c| *c == 0) {
                if !value.is_empty() && seen.insert(value) {
                    alternates.push(value.to_vec());
                }
            }
        }
        Ok((Some(url.to_vec()), alternates, false))
    }

    pub fn follow_unsettled(&mut self, found: &Origin) -> Result<bool, Error> {
        if let Some(error) = self.pending {
            return Err(error);
        }
        let Some(cwd) = found
            .cwd
            .as_ref()
            .filter(|x| !x.is_empty() && found.worktree_root.is_some())
        else {
            return Ok(false);
        };
        let Some(cwd) = self.encode(cwd) else {
            return Ok(false); // discovery_reading catches realpath's UnicodeEncodeError.
        };
        Ok(self
            .files
            .reading(&cwd)?
            .is_some_and(|r| r.why.is_some() || r.provisional))
    }

    pub fn agreed(&mut self, cwd: &[u8], top: &[u32]) -> Result<bool, Error> {
        if let Some(error) = self.pending {
            return Err(error);
        }
        if let Some(top) = self.encode(top) {
            if self.files.agrees(cwd, &top)? {
                return Ok(true);
            }
        } else {
            // Discovery runs before _same_folder catches the unencodable top.
            self.files.reading(cwd)?;
        }
        let cwd_text = self.decode(cwd)?;
        self.origin_line(
            Level::Info,
            &[
                &cps("git names "),
                top,
                &cps(" for "),
                &cwd_text,
                &cps(", not membership's root; no origin is kept"),
            ],
        );
        Ok(false)
    }

    pub fn probe_worktree_root(&mut self, cwd: &[u8]) -> Result<Root, Error> {
        if let Some(error) = self.pending {
            return Err(error);
        }
        let missing = match self.files.stat_dir(cwd) {
            Ok(directory) => !directory,
            // pathlib.Path.is_dir in CPython 3.13 suppresses these errno
            // values and ValueError. The caller catches other OSErrors
            // and proceeds to ask Git. 3.14 suppresses every OSError.
            Err(Failure::Value) => true,
            Err(Failure::Os(err)) => {
                matches!(err.errno, Some("ENOENT" | "ENOTDIR" | "EBADF" | "ELOOP"))
                    || matches!(err.winerror, Some(21 | 123 | 1921))
            }
            Err(error) => return Err(Error::Files(error)),
        };
        let cwd_text = self.decode(cwd)?;
        if missing {
            let level = if self.context.first_not_a_directory.first(cwd) {
                Level::Warning
            } else {
                Level::Debug
            };
            self.origin_line(
                level,
                &[
                    &cps("the origin of "),
                    &cwd_text,
                    &cps(" is indeterminate: it is not a directory this host holds"),
                ],
            );
            self.counts.tally.note_failed();
            return Ok((None, true, None));
        }
        let args = argv(&["rev-parse", "--show-toplevel"]);
        let Some(result) = self.result(cwd, &args, 1.0, None, true)? else {
            return Ok((None, true, None));
        };
        if result.returncode != 0 {
            self.nonzero(&args, &result, cwd)?;
            let (root, refused) = self.files.refused_root(cwd)?;
            let top = root.as_deref().map(|r| self.decode(r)).transpose()?;
            if !refused {
                return Ok((top, false, None));
            }
            let lines = crate::machine::facts::splitlines(strip(&result.stderr));
            let reason = lines.first().map_or_else(
                || cps(&format!("exit {}", result.returncode)),
                |line| line.to_vec(),
            );
            let root_text = top.as_deref().map_or_else(|| cps("None"), <[u32]>::to_vec);
            self.origin_line(
                Level::Info,
                &[
                    &cps("git refused the repository holding "),
                    &cwd_text,
                    &cps(" ("),
                    &reason,
                    &cps("); its root is read from the files ("),
                    &root_text,
                    &cps(") and the origin is indeterminate"),
                ],
            );
            return Ok((top, true, Some(reason)));
        }
        let raw = git_calls::printed_path(&result.stdout, self.context.flavour == Flavour::Windows);
        if raw.is_empty() || !self.agreed(cwd, raw)? {
            return Ok((None, false, None));
        }
        let raw_bytes = self.encode(raw).ok_or(Error::UnicodeEncode)?;
        let resolved = match self.files.resolve(&raw_bytes) {
            Ok(path) => self.decode(&path)?,
            Err(Unresolved::Raised(err)) if err != PyErr::Value => raw.to_vec(),
            Err(err) => return Err(Error::Resolve(err)),
        };
        Ok((Some(resolved), false, None))
    }

    pub fn worktree_root(&mut self, cwd: &[u8]) -> Result<Option<PyStr>, Error> {
        self.probe_worktree_root(cwd).map(|root| root.0)
    }

    fn adapt<T>(&mut self, value: Result<Option<T>, Error>) -> Option<T> {
        match value {
            Ok(result) => result,
            Err(error) => {
                self.pending = Some(error);
                None
            }
        }
    }
}

/// The keyword arguments of `origin._git`.
#[derive(Clone, Copy)]
pub struct Output<'a> {
    pub timeout: f64,
    pub stdin: Option<&'a [u8]>,
    pub paths: bool,
    pub verbatim: bool,
    pub empty_ok: bool,
}
impl Default for Output<'_> {
    fn default() -> Self {
        Self {
            timeout: 1.0,
            stdin: None,
            paths: false,
            verbatim: false,
            empty_ok: false,
        }
    }
}

impl OriginGit for OriginRunner<'_> {
    fn git(
        &mut self,
        cwd: &[u8],
        args: &[PyStr],
        timeout: f64,
        verbatim: bool,
        empty_ok: bool,
    ) -> Option<PyStr> {
        let result = self.output(
            cwd,
            args,
            Output {
                timeout,
                verbatim,
                empty_ok,
                ..Output::default()
            },
        );
        self.adapt(result)
    }
    fn git_result(&mut self, cwd: &[u8], args: &[PyStr], timeout: f64) -> Option<GitResult> {
        let result = self.result(cwd, args, timeout, None, false).map(|done| {
            done.map(|r| GitResult {
                returncode: r.returncode,
                stdout: r.stdout,
            })
        });
        self.adapt(result)
    }
    fn counts(&self) -> (i64, i64) {
        (self.counts.tally.timed_out, self.counts.tally.missing)
    }
    fn note_failed(&mut self) {
        if self.pending.is_none() {
            self.counts.tally.note_failed();
        }
    }
    fn note_timed_out(&mut self) {
        if self.pending.is_none() {
            self.counts.tally.note_timed_out();
        }
    }
    fn deadline(&self) -> bool {
        self.pending == Some(Error::Run(RunRaised::ReadDeadline))
    }
}
