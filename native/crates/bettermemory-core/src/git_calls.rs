//! The call tallies of `_git.py` and its text helpers: `_unanswered` and
//! the per-thread counts (`_FAILED`, `_TIMED_OUT`, `_MISSING`), the
//! counts `run_git` adds around one call, `printed_path`, `subcommand`
//! and the line `log_nonzero_exit` logs.
//!
//! `_unanswered` is a process-wide atomic ([`UNANSWERED`]); a caller may
//! hand in another. The per-thread counts are a [`Tally`] each thread
//! owns. The spawn stays `gitsystem`'s `Git`: here it is one call's
//! outcome, [`Spawn`].

use std::sync::atomic::{AtomicI64, Ordering};

use crate::git_read_budget::{self as budget, ReadDeadline, Scope};
use crate::origin_history::RunnerNote;
use crate::unicode;

/// `_unanswered`: git invocations that could not run at all, on any thread.
pub static UNANSWERED: AtomicI64 = AtomicI64::new(0);

/// `unanswered_calls()` of a counter.
pub fn unanswered_calls(counter: &AtomicI64) -> i64 {
    counter.load(Ordering::SeqCst)
}

/// `_FAILED`, `_TIMED_OUT` and `_MISSING` of one thread.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Tally {
    pub failed: i64,
    pub timed_out: i64,
    pub missing: i64,
}

impl Tally {
    pub fn failed_calls(&self) -> i64 {
        self.failed
    }
    pub fn timed_out_calls(&self) -> i64 {
        self.timed_out
    }
    pub fn missing_calls(&self) -> i64 {
        self.missing
    }
    /// `note_failed()`.
    pub fn note_failed(&mut self) {
        self.failed += 1;
    }
    /// `note_timed_out()`.
    pub fn note_timed_out(&mut self) {
        self.failed += 1;
        self.timed_out += 1;
    }
    /// The note `origin_history.note_unread` makes.
    pub fn apply(&mut self, note: RunnerNote) {
        match note {
            RunnerNote::Failed => self.note_failed(),
            RunnerNote::TimedOut => self.note_timed_out(),
        }
    }
}

/// What `subprocess.run` did with one call.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Spawn {
    /// It completed: `run_git` returns the process.
    Ran,
    /// FileNotFoundError: no binary, or (POSIX) no working directory.
    NotFound,
    /// TimeoutExpired.
    TimedOut,
    /// Another OSError.
    OsError,
    /// Another exception (a ValueError of a NUL in an argument): it
    /// passes through `run_git` uncounted.
    Other,
}

/// What `run_git` raises past its counts.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RunRaised {
    ReadDeadline,
    /// `exhausted()` with no active budget (unreachable in the reference).
    AssertionError,
    /// [`Spawn::Other`], unchanged.
    Other,
}

/// `run_git`'s counts around one call: `spawn` is given the allowed
/// timeout; `cwd_is_dir` is `os.path.isdir(cwd)`, asked only where the
/// reference asks it. Ok(true) where the process is returned, Ok(false)
/// for None.
#[allow(clippy::too_many_arguments)]
pub fn run_git(
    scope: &Scope,
    clock: &dyn Fn() -> f64,
    unanswered: &AtomicI64,
    tally: &mut Tally,
    timeout: f64,
    cwd: Option<&[u32]>,
    git_dir: Option<&[u32]>,
    cwd_is_dir: impl FnOnce() -> bool,
    spawn: impl FnOnce(f64) -> Spawn,
) -> Result<bool, RunRaised> {
    let (timeout, shared) = match budget::allowance(scope, clock, timeout, cwd, git_dir) {
        Ok(given) => given,
        Err(ReadDeadline) => {
            unanswered.fetch_add(1, Ordering::SeqCst);
            tally.failed += 1;
            return Err(RunRaised::ReadDeadline);
        }
    };
    match spawn(timeout) {
        Spawn::Ran => return Ok(true),
        Spawn::Other => return Err(RunRaised::Other),
        Spawn::NotFound => {
            // The reference asks only to choose its log line.
            if cwd.is_some() {
                cwd_is_dir();
            }
            tally.missing += 1;
        }
        Spawn::TimedOut => {
            if shared && budget::expired(scope, clock) {
                unanswered.fetch_add(1, Ordering::SeqCst);
                tally.failed += 1;
                tally.timed_out += 1;
                return Err(match budget::exhausted(scope) {
                    budget::Exhausted::ReadDeadline => RunRaised::ReadDeadline,
                    budget::Exhausted::AssertionError => RunRaised::AssertionError,
                });
            }
            tally.timed_out += 1;
        }
        Spawn::OsError => {
            if cwd.is_some() && !cwd_is_dir() {
                tally.missing += 1;
            }
        }
    }
    unanswered.fetch_add(1, Ordering::SeqCst);
    tally.failed += 1;
    Ok(false)
}

/// `printed_path(stdout)`; `windows` is `_WINDOWS` (`os.name == "nt"`).
pub fn printed_path(stdout: &[u32], windows: bool) -> &[u32] {
    let Some(path) = stdout.strip_suffix(&[0x0a]) else {
        return stdout;
    };
    if windows {
        return path.strip_suffix(&[0x0d]).unwrap_or(path);
    }
    path
}

/// The first argument after leading `-c setting` pairs. A trailing
/// `-c` without a setting is itself the subcommand.
pub(crate) fn subcommand_index<T>(args: &[T], config: impl Fn(&T) -> bool) -> usize {
    let mut i = 0;
    while i + 1 < args.len() && config(&args[i]) {
        i += 2;
    }
    i
}

/// `subcommand(args)`.
pub fn subcommand<S: AsRef<str>>(args: &[S]) -> &str {
    let i = subcommand_index(args, |arg| arg.as_ref() == "-c");
    args.get(i).map_or("", AsRef::as_ref)
}

/// Whether `str.splitlines` ends a line at `c`.
fn line_break(c: char) -> bool {
    "\n\r\x0b\x0c\x1c\x1d\x1e\u{85}\u{2028}\u{2029}".contains(c)
}

/// The message `log_nonzero_exit` logs: `stderr` is the text (bytes are
/// decoded `utf-8`, `replace` first), `where_` is `str(where)`.
pub fn nonzero_exit_line<S: AsRef<str>>(
    args: &[S],
    returncode: i64,
    where_: &str,
    stderr: &str,
) -> String {
    let stripped = stderr.trim_matches(|c: char| unicode::is_space(u32::from(c)));
    let first = stripped.split(line_break).next().unwrap_or("");
    format!(
        "git {} exited {returncode} in {where_}: {first}",
        subcommand(args)
    )
}
