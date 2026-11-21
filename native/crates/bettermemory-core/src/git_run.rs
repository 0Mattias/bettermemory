//! `_git.run_git` whole and `_git.log_nonzero_exit`: one git call over a
//! process the caller hands in ([`GitProcess`]), with the environment and
//! the command `gitsystem::ask` shapes, the counts `git_calls::run_git`
//! keeps, stdin, both streams as text or bytes, and the lines the
//! reference logs, returned as values in order.

use std::cell::Cell;
use std::sync::atomic::AtomicI64;

use crate::float_repr;
use crate::git_calls::{self as calls, RunRaised, Spawn, Tally};
use crate::git_read_budget::Scope;
use crate::gitsystem::answers;
use crate::gitsystem::ask::{environment, Env, PINS};
use crate::pyvalue::PyStr;
use crate::unicode::{cps, to_string};

/// What `subprocess.run` did with one command.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Spawned {
    /// The process ran to its end: its exit code and both streams.
    Exited {
        returncode: i64,
        stdout: Vec<u8>,
        stderr: Vec<u8>,
    },
    /// FileNotFoundError, with `str(exc)`.
    NotFound(PyStr),
    /// TimeoutExpired.
    TimedOut,
    /// Another OSError, with `str(exc)`.
    OsError(PyStr),
    /// Another exception: it passes through uncounted.
    Other,
}

/// The process `run_git` starts: `subprocess.run(argv, cwd=, input=,
/// capture_output=True, timeout=, env=)`. Which errno a platform gives
/// for a working directory that is not there is the implementer's.
pub trait GitProcess {
    fn run(
        &mut self,
        argv: &[PyStr],
        cwd: Option<&[u32]>,
        env: &Env,
        stdin: Option<&[u8]>,
        timeout: f64,
    ) -> Spawned;
}

/// One stream of a completed process: decoded text or the bytes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Out {
    Text(PyStr),
    Bytes(Vec<u8>),
}

/// `subprocess.CompletedProcess`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Completed {
    pub args: Vec<PyStr>,
    pub returncode: i64,
    pub stdout: Out,
    pub stderr: Out,
}

/// A logging level of `_git.log`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Level {
    Debug,
    Warning,
}

/// One logged line: its level and its message.
pub type Line = (Level, PyStr);

/// The keyword arguments of `run_git`.
#[derive(Clone, Copy, Debug)]
pub struct Call<'a> {
    pub args: &'a [PyStr],
    pub cwd: Option<&'a [u32]>,
    pub git_dir: Option<&'a [u32]>,
    pub stdin: Option<&'a [u8]>,
    /// `timeout`, 1.0 in the reference's signature.
    pub timeout: f64,
    /// `text`, True in the reference's signature.
    pub text: bool,
    /// `env`: set beneath the scrub and the pins.
    pub env: &'a [(PyStr, PyStr)],
    /// `unset`: removed beneath the scrub and the pins.
    pub unset: &'a [&'a str],
}

impl<'a> Call<'a> {
    /// A call with the reference's defaults.
    pub fn new(args: &'a [PyStr]) -> Call<'a> {
        Call {
            args,
            cwd: None,
            git_dir: None,
            stdin: None,
            timeout: 1.0,
            text: true,
            env: &[],
            unset: &[],
        }
    }
}

/// What `run_git` answers, with the lines it logged.
pub type Ran = (Result<Option<Completed>, RunRaised>, Vec<Line>);

/// `subcommand(args)` over code points: the element `git_calls::subcommand`
/// picks. An element no `&str` holds (a lone surrogate) is never `-c`,
/// so a stand-in that is not `-c` chooses the same place.
fn subcommand_of(args: &[PyStr]) -> PyStr {
    let spelled: Vec<String> = args
        .iter()
        .map(|a| to_string(a).unwrap_or_else(|| "\u{fffd}".to_owned()))
        .collect();
    let picked = calls::subcommand(&spelled);
    spelled
        .iter()
        .position(|s| std::ptr::eq(s.as_str(), picked))
        .map_or_else(Vec::new, |i| args[i].clone())
}

fn join(parts: &[&[u32]]) -> PyStr {
    parts.concat()
}

/// `str(x)` of an optional path: `None` for None.
fn spelled(x: Option<&[u32]>) -> PyStr {
    x.map_or_else(|| cps("None"), <[u32]>::to_vec)
}

/// `run_git(args, cwd=, git_dir=, stdin=, timeout=, text=, env=, unset=)`
/// over `os_env` (`os.environ`), the budget's `scope` and `clock`, the
/// counts, the process and `os.path.isdir(cwd)`, asked at most once and
/// only where the reference asks it.
#[allow(clippy::too_many_arguments)]
pub fn run_git(
    scope: &Scope,
    clock: &dyn Fn() -> f64,
    unanswered: &AtomicI64,
    tally: &mut Tally,
    process: &mut dyn GitProcess,
    os_env: &Env,
    call: &Call<'_>,
    isdir: impl FnOnce() -> bool,
) -> Ran {
    let mut argv: Vec<PyStr> = vec![cps("git")];
    argv.extend(PINS.iter().map(|p| cps(p)));
    if let Some(g) = call.git_dir {
        argv.push(join(&[&cps("--git-dir="), g]));
    }
    argv.extend(call.args.iter().cloned());
    let asked: Cell<Option<bool>> = Cell::new(None);
    let mut spawned: Option<Spawned> = None;
    let mut allowed: f64 = call.timeout;
    let outcome = calls::run_git(
        scope,
        clock,
        unanswered,
        tally,
        call.timeout,
        call.cwd,
        call.git_dir,
        || {
            let answer = isdir();
            asked.set(Some(answer));
            answer
        },
        |timeout| {
            allowed = timeout;
            let env = environment(os_env, call.env, call.unset);
            let done = process.run(&argv, call.cwd, &env, call.stdin, timeout);
            let kind = match &done {
                Spawned::Exited { .. } => Spawn::Ran,
                Spawned::NotFound(_) => Spawn::NotFound,
                Spawned::TimedOut => Spawn::TimedOut,
                Spawned::OsError(_) => Spawn::OsError,
                Spawned::Other => Spawn::Other,
            };
            spawned = Some(done);
            kind
        },
    );
    let mut lines: Vec<Line> = Vec::new();
    let ran = match outcome {
        Err(raised) => return (Err(raised), lines),
        Ok(ran) => ran,
    };
    let where_ = match call.cwd {
        Some(c) => c.to_vec(),
        None => spelled(call.git_dir),
    };
    match spawned {
        Some(Spawned::Exited {
            returncode,
            stdout,
            stderr,
        }) if ran => {
            let (stdout, stderr) = if call.text {
                (
                    Out::Text(answers::text(&stdout)),
                    Out::Text(answers::text(&stderr)),
                )
            } else {
                (Out::Bytes(stdout), Out::Bytes(stderr))
            };
            let done = Completed {
                args: argv,
                returncode,
                stdout,
                stderr,
            };
            return (Ok(Some(done)), lines);
        }
        Some(Spawned::NotFound(exc)) => {
            let message = match (call.cwd, asked.get()) {
                (Some(c), Some(false)) => join(&[
                    &cps("git cannot run in "),
                    c,
                    &cps(": the directory does not exist"),
                ]),
                _ => join(&[&cps("git binary not found on PATH: "), &exc]),
            };
            lines.push((Level::Warning, message));
        }
        Some(Spawned::TimedOut) => {
            let message = join(&[
                &cps("git "),
                &subcommand_of(call.args),
                &cps(" timed out after "),
                &cps(&float_repr::repr(allowed)),
                &cps("s in "),
                &where_,
            ]);
            lines.push((Level::Warning, message));
        }
        Some(Spawned::OsError(exc)) => {
            let message = join(&[&cps("git invocation failed in "), &where_, &cps(": "), &exc]);
            lines.push((Level::Warning, message));
        }
        // `git_calls::run_git` answers Ok(true) for a process that ran and
        // passes Other through as an error, so neither reaches here.
        _ => {}
    }
    (Ok(None), lines)
}

/// `log_nonzero_exit(args, result, where)`: the DEBUG line, `stderr`
/// decoded `utf-8`/`replace` where it is bytes, `where_` spelled by
/// `str` (`None` for None). The line is built over code points, as
/// `git_calls::nonzero_exit_line` builds it over `&str`, so a lone
/// surrogate in an argument, in `where_` or in a text `stderr` is kept.
pub fn log_nonzero_exit(
    args: &[PyStr],
    returncode: i64,
    stderr: &Out,
    where_: Option<&[u32]>,
) -> Line {
    let text = match stderr {
        Out::Text(t) => t.clone(),
        Out::Bytes(b) => answers::text(b),
    };
    let first = first_line(&text);
    let line = join(&[
        &cps("git "),
        &subcommand_of(args),
        &cps(&format!(" exited {returncode} in ")),
        &spelled(where_),
        &cps(": "),
        first,
    ]);
    (Level::Debug, line)
}

/// `stderr.strip().splitlines()[0]`, or empty where no line is left.
fn first_line(text: &[u32]) -> &[u32] {
    let is_space = |c: &u32| crate::unicode::is_space(*c);
    let start = text.iter().position(|c| !is_space(c)).unwrap_or(text.len());
    let end = text
        .iter()
        .rposition(|c| !is_space(c))
        .map_or(start, |i| i + 1);
    let stripped = &text[start..end];
    let brk = |c: &u32| {
        matches!(
            *c,
            0x0a | 0x0d | 0x0b | 0x0c | 0x1c | 0x1d | 0x1e | 0x85 | 0x2028 | 0x2029
        )
    };
    stripped
        .iter()
        .position(brk)
        .map_or(stripped, |i| &stripped[..i])
}
