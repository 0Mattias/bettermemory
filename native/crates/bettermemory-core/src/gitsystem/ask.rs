//! The ask: `shutil.which("git")`, `_git.run_git`'s command shaping,
//! `_ask`, `_older_system`, `_version`, `_reads_remotes` and `_probe_ask`,
//! with the monotonic deadline of `ASK_SECONDS` and `git_read_budget`.
//!
//! The port runs nothing and reads no environment, clock or working
//! directory of its own: git is the [`Git`] trait, which is handed each
//! whole [`Command`] (argv, working directory, the complete environment,
//! timeout, output limit) and gives back an exit status with its output,
//! "not run" or "timed out"; the clock is the [`Clock`] trait, read
//! exactly where the reference reads `time.monotonic` (and
//! `git_read_budget`'s `_clock`, the same clock) and `time.time`; the
//! environment (`os.environ`, in its order), the default search path
//! (`os.confstr("CS_PATH")`) and the working directory are inputs.
//!
//! One divergence: the reference reads all of git's output; a command
//! here carries [`OUTPUT_LIMIT`], and an answer past it is taken as one
//! git did not give (as `run_git`'s None).
//!
//! Under the Windows flavour, [`which`] is ported; the ask declines
//! (`NotPorted`) before asking anything (its stamp, ask folder and probe
//! are not ported there, and `os.environ` there is another map).

use std::collections::HashMap;

use crate::fsdata::{Access, Flavour, Fs, Kind};
use crate::gitconfig::legacy::{file_stamp, NotPorted, Stamp, UNSTATED};
use crate::gitconfig::paths;
use crate::pyvalue::PyStr;
use crate::unicode;

use super::answers::{self, Raised};
use super::probe::COMPAT_PROBE;
use super::record::{
    surrogateescape, surrogatepass, Answer, Natural, Place, RecordedStamp, Sources,
};

/// `ASK_SECONDS`.
pub const ASK_SECONDS: f64 = 0.3;
/// `git_read_budget.READ_SECONDS`.
pub const READ_SECONDS: f64 = 5.0;
/// The most output of either stream a command takes; the trait gives at
/// most one byte more, which marks it past.
pub const OUTPUT_LIMIT: usize = 1 << 20;
/// `_FALLBACK` on POSIX.
pub const FALLBACK: &str = "/etc/gitconfig";
/// `os.devnull` on POSIX.
const DEVNULL: &str = "/dev/null";
/// `shutil._WIN_DEFAULT_PATHEXT`.
pub const WIN_DEFAULT_PATHEXT: &str = ".COM;.EXE;.BAT;.CMD;.VBS;.JS;.WS;.MSC";

/// `_git.SCRUBBED_ENV`.
pub const SCRUBBED_ENV: [&str; 7] = [
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_SHALLOW_FILE",
    "GIT_INDEX_FILE",
];
/// `_git.RESHAPING_ENV`.
pub const RESHAPING_ENV: [&str; 5] = [
    "GIT_DIFF_OPTS",
    "GIT_ATTR_SOURCE",
    "GIT_EXTERNAL_DIFF",
    "GIT_DIFF_PATH_COUNTER",
    "GIT_DIFF_PATH_TOTAL",
];
/// `_git.NO_FETCH`, in its order.
pub const NO_FETCH: [(&str, &str); 2] = [("GIT_NO_LAZY_FETCH", "1"), ("GIT_ALLOW_PROTOCOL", "")];
/// `_git.PINS`.
pub const PINS: [&str; 8] = [
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "log.showSignature=false",
    "-c",
    "diff.submodule=short",
];

/// `_VAR`.
pub const VAR: [&str; 2] = ["var", "GIT_CONFIG_SYSTEM"];
/// `_SYSTEM_LIST`.
pub const SYSTEM_LIST: [&str; 5] = ["config", "--system", "--list", "--show-origin", "-z"];
/// `_LIST`.
pub const LIST: [&str; 5] = ["config", "--list", "--show-scope", "--show-origin", "-z"];
/// `_VERSION_ASK`.
pub const VERSION_ASK: [&str; 1] = ["version"];
/// `_REMOTE_ASK`.
pub const REMOTE_ASK: [&str; 3] = ["ls-remote", "--get-url", "p"];
/// `_PREFIX_ASK`.
pub const PREFIX_ASK: [&str; 5] = [
    "config",
    "-z",
    "--type=path",
    "--get-regexp",
    r"^bettermemory\.",
];
/// `_COMPAT_ASK`.
pub const COMPAT_ASK: [&str; 2] = ["rev-parse", "--git-dir"];
/// `_OVERRIDES`.
pub const OVERRIDES: [&str; 2] = ["GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"];
/// `_PROBE_UNSET`.
pub const PROBE_UNSET: [&str; 4] = [
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
];

/// An environment: names and values in their order, each name once.
pub type Env = Vec<(PyStr, PyStr)>;

/// One run of git, whole.
#[derive(Clone, Debug, PartialEq)]
pub struct Command {
    /// `git`, the pins, `--git-dir=` where given, the question.
    pub argv: Vec<PyStr>,
    /// The working directory, as the OS names it.
    pub cwd: Vec<u8>,
    /// The complete environment git is given.
    pub env: Env,
    /// Seconds.
    pub timeout: f64,
    /// The most output of either stream taken (`OUTPUT_LIMIT`).
    pub limit: usize,
}

/// What became of a command.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Ran {
    /// It exited with `status`; each stream at most `limit + 1` bytes.
    Exited { status: i32, stdout: Vec<u8> },
    /// It could not run (no binary, no folder, the spawn failed).
    NotRun,
    /// It did not end within its timeout.
    TimedOut,
}

/// The git the port asks.
pub trait Git {
    fn run(&mut self, command: &Command) -> Ran;
}

/// The clocks the reference reads.
pub trait Clock {
    /// `time.monotonic()` (and `git_read_budget._clock()`).
    fn monotonic(&mut self) -> f64;
    /// `time.time()`.
    fn wall(&mut self) -> f64;
}

/// `git_read_budget`'s active budget, as the ask meets it.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Budget {
    /// No reading scope is active: `allowance` gives the timeout back.
    Inactive,
    /// A scope is active; its deadline on the monotonic clock, None until
    /// the first read starts it.
    Active { deadline: Option<f64> },
}

/// Why the ask gave no answer: the reference raises out of `_ask`, or
/// the flavour is not ported.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Declined {
    /// The class name the reference raises (`ReadDeadline`, an OSError's
    /// class from `_probe_holds`, `ValueError` from `gitversion.parse`).
    Raised(&'static str),
    NotPorted,
}

impl From<NotPorted> for Declined {
    fn from(_: NotPorted) -> Declined {
        Declined::NotPorted
    }
}

fn cps(s: &str) -> PyStr {
    unicode::cps(s)
}

/// A name looked up in `env`: exactly under POSIX; under Windows as
/// `os.environ` there does, its names upper-cased (the last of a name
/// standing).
fn lookup<'e>(flavour: Flavour, env: &'e Env, name: &str) -> Option<&'e PyStr> {
    let want = cps(name);
    match flavour {
        Flavour::Posix => env.iter().find(|(k, _)| *k == want).map(|(_, v)| v),
        Flavour::Windows => env
            .iter()
            .rev()
            .find(|(k, _)| unicode::upper(k) == unicode::upper(&want))
            .map(|(_, v)| v),
    }
}

/// A text path as the bytes the OS is handed (`os.fsencode`): None where
/// POSIX's `surrogateescape` refuses it (a ValueError).
fn encoded(flavour: Flavour, path: &[u32]) -> Option<Vec<u8>> {
    match flavour {
        Flavour::Posix => {
            let mut out = Vec::with_capacity(path.len());
            for &cp in path {
                match cp {
                    0xDC80..=0xDCFF => out.push((cp - 0xDC00) as u8),
                    0xD800..=0xDFFF => return None,
                    _ => out.extend(surrogatepass(&[cp])),
                }
            }
            Some(out)
        }
        Flavour::Windows => Some(surrogatepass(path)),
    }
}

/// `shutil._access_check(name, F_OK | X_OK)`: it exists, git may run it,
/// and it is no folder.
fn runnable<F: Fs>(fs: &F, flavour: Flavour, cwd: &[u8], name: &[u32]) -> bool {
    let Some(bytes) = encoded(flavour, name) else {
        return false;
    };
    if bytes.contains(&0) {
        return false;
    }
    let path = paths::opened(flavour, &bytes, cwd);
    let Ok(st) = fs.stat(&path) else {
        return false;
    };
    if !fs.access(&path, Access::EXECUTE).unwrap_or(false) {
        return false;
    }
    st.kind != Kind::Directory
}

/// `os.path.normcase(dir)`: itself on POSIX; under Windows, `/` as `\`
/// and lower case, as `ntpath.normcase` off Windows has it (Windows' own
/// asks `LCMapStringEx`).
fn normcase(flavour: Flavour, dir: &[u32]) -> PyStr {
    match flavour {
        Flavour::Posix => dir.to_vec(),
        Flavour::Windows => {
            let swapped: PyStr = dir
                .iter()
                .map(|&c| {
                    if c == u32::from(b'/') {
                        u32::from(b'\\')
                    } else {
                        c
                    }
                })
                .collect();
            unicode::lower(&swapped)
        }
    }
}

/// `os.path.join(dir, file)` of text.
fn join_text(flavour: Flavour, dir: &[u32], file: &[u32]) -> PyStr {
    // Both separators are ASCII: joining the UTF-8 (surrogatepass) bytes
    // and decoding them back keeps every code point.
    let joined = paths::join(flavour, &surrogatepass(dir), &surrogatepass(file));
    decode_pass(&joined)
}

/// `bytes.decode("utf-8", "surrogatepass")` of what [`surrogatepass`]
/// made.
fn decode_pass(mut b: &[u8]) -> PyStr {
    let mut out = PyStr::new();
    loop {
        match std::str::from_utf8(b) {
            Ok(s) => {
                out.extend(s.chars().map(u32::from));
                return out;
            }
            Err(e) => {
                let (good, rest) = b.split_at(e.valid_up_to());
                out.extend(
                    std::str::from_utf8(good)
                        .unwrap_or_default()
                        .chars()
                        .map(u32::from),
                );
                match rest {
                    [0xed, x @ 0xa0..=0xbf, y @ 0x80..=0xbf, ..] => {
                        out.push(0xd000 | (u32::from(x & 0x3f) << 6) | u32::from(y & 0x3f));
                        b = &rest[3..];
                    }
                    _ => {
                        out.push(0xfffd);
                        b = &rest[1..];
                    }
                }
            }
        }
    }
}

/// `shutil.which("git")` under `flavour`, the environment `env` and the
/// working directory `cwd`; `cs_path` is what `os.confstr("CS_PATH")`
/// gives where PATH is unset (`os.defpath` where it is unavailable, None
/// where it names nothing). Under Windows, `.` is searched first unless
/// `NoDefaultCurrentDirectoryInExePath` is set
/// (`NeedCurrentDirectoryForExePath` of a name with no `\`), and each of
/// PATHEXT's extensions is tried.
pub fn which<F: Fs>(
    fs: &F,
    flavour: Flavour,
    cwd: &[u8],
    env: &Env,
    cs_path: Option<&[u32]>,
) -> Option<PyStr> {
    let path: PyStr = match lookup(flavour, env, "PATH") {
        Some(p) => p.clone(),
        None => cs_path?.to_vec(),
    };
    if path.is_empty() {
        return None;
    }
    let sep = u32::from(match flavour {
        Flavour::Posix => b':',
        Flavour::Windows => b';',
    });
    let mut dirs: Vec<PyStr> = path.split(|&c| c == sep).map(<[u32]>::to_vec).collect();
    let cmd = cps("git");
    let mut files: Vec<PyStr> = vec![];
    if flavour == Flavour::Windows {
        if lookup(flavour, env, "NoDefaultCurrentDirectoryInExePath").is_none() {
            dirs.insert(0, cps("."));
        }
        let source = match lookup(flavour, env, "PATHEXT") {
            Some(v) if !v.is_empty() => v.clone(),
            _ => cps(WIN_DEFAULT_PATHEXT),
        };
        let exts: Vec<PyStr> = source
            .split(|&c| c == sep)
            .filter(|e| !e.is_empty())
            .map(|e| {
                let end = e
                    .iter()
                    .rposition(|&c| c != u32::from(b'.'))
                    .map_or(0, |i| i + 1);
                e[..end].to_vec()
            })
            .collect();
        files = exts
            .iter()
            .map(|e| {
                let mut f = cmd.clone();
                f.extend_from_slice(e);
                f
            })
            .collect();
        let norm = unicode::upper(&cmd);
        if exts.iter().any(|e| norm.ends_with(&unicode::upper(e))) {
            files.insert(0, cmd.clone());
        }
    } else {
        files.push(cmd);
    }
    let mut seen: std::collections::HashSet<PyStr> = std::collections::HashSet::new();
    for dir in &dirs {
        if !seen.insert(normcase(flavour, dir)) {
            continue;
        }
        for file in &files {
            let name = join_text(flavour, dir, file);
            if runnable(fs, flavour, cwd, &name) {
                return Some(name);
            }
        }
    }
    None
}

/// `_git._environment(extra, unset)` over `env`: `dict(os.environ)`,
/// `unset` removed, `extra` set, `GIT_OPTIONAL_LOCKS=0` and `NO_FETCH`
/// set, `SCRUBBED_ENV` and `RESHAPING_ENV` removed; a name keeps the
/// place it first had, as a dict's does.
pub fn environment(env: &Env, extra: &[(PyStr, PyStr)], unset: &[&str]) -> Env {
    let mut slots: Vec<Option<(PyStr, PyStr)>> = Vec::with_capacity(env.len() + 8);
    let mut at: HashMap<PyStr, usize> = HashMap::with_capacity(env.len() + 8);
    let set = |slots: &mut Vec<Option<(PyStr, PyStr)>>,
               at: &mut HashMap<PyStr, usize>,
               k: PyStr,
               v: PyStr| match at.get(&k) {
        Some(&i) => slots[i] = Some((k, v)),
        None => {
            at.insert(k.clone(), slots.len());
            slots.push(Some((k, v)));
        }
    };
    let pop = |slots: &mut Vec<Option<(PyStr, PyStr)>>, at: &mut HashMap<PyStr, usize>, k: &str| {
        if let Some(i) = at.remove(&cps(k)) {
            slots[i] = None;
        }
    };
    for (k, v) in env {
        set(&mut slots, &mut at, k.clone(), v.clone());
    }
    for name in unset {
        pop(&mut slots, &mut at, name);
    }
    for (k, v) in extra {
        set(&mut slots, &mut at, k.clone(), v.clone());
    }
    set(&mut slots, &mut at, cps("GIT_OPTIONAL_LOCKS"), cps("0"));
    for (k, v) in NO_FETCH {
        set(&mut slots, &mut at, cps(k), cps(v));
    }
    for name in SCRUBBED_ENV.iter().chain(RESHAPING_ENV.iter()) {
        pop(&mut slots, &mut at, name);
    }
    slots.into_iter().flatten().collect()
}

/// What stops the ask: the reference raises.
#[derive(Debug)]
enum Halt {
    Raised(&'static str),
    NotPorted,
}

impl From<NotPorted> for Halt {
    fn from(_: NotPorted) -> Halt {
        Halt::NotPorted
    }
}

/// An answer git gave: its exit status and its stdout.
type Done = Option<(i32, Vec<u8>)>;

/// The ask over one place, git, clock, environment and budget.
pub struct Asker<'a, 'p, F: Fs, G: Git, C: Clock> {
    pub place: &'a Place<'p, F>,
    pub git: &'a mut G,
    pub clock: &'a mut C,
    pub env: &'a Env,
    pub cs_path: Option<&'a [u32]>,
    pub budget: &'a mut Budget,
}

impl<F: Fs, G: Git, C: Clock> Asker<'_, '_, F, G, C> {
    /// `run_git(args, cwd=cwd, git_dir=git_dir, timeout=timeout,
    /// env=extra, unset=unset)`: None where git could not run, timed out
    /// or wrote past [`OUTPUT_LIMIT`]; Err where the budget raises
    /// ReadDeadline.
    fn run_git(
        &mut self,
        args: &[&str],
        cwd: &[u8],
        git_dir: Option<&[u32]>,
        timeout: f64,
        extra: &[(PyStr, PyStr)],
        unset: &[&str],
    ) -> Result<Done, Halt> {
        let (timeout, shared) = match self.budget {
            Budget::Inactive => (timeout, false),
            Budget::Active { deadline } => {
                let now = self.clock.monotonic();
                let end = *deadline.get_or_insert(now + READ_SECONDS);
                let remaining = end - now;
                if remaining <= 0.0 {
                    return Err(Halt::Raised("ReadDeadline"));
                }
                // `min(requested, remaining)`.
                let given = if remaining < timeout {
                    remaining
                } else {
                    timeout
                };
                (given, remaining < timeout)
            }
        };
        let mut argv: Vec<PyStr> = vec![cps("git")];
        argv.extend(PINS.iter().map(|p| cps(p)));
        if let Some(dir) = git_dir {
            let mut a = cps("--git-dir=");
            a.extend_from_slice(dir);
            argv.push(a);
        }
        argv.extend(args.iter().map(|a| cps(a)));
        let command = Command {
            argv,
            cwd: cwd.to_vec(),
            env: environment(self.env, extra, unset),
            timeout,
            limit: OUTPUT_LIMIT,
        };
        match self.git.run(&command) {
            Ran::Exited { status, stdout } if stdout.len() <= OUTPUT_LIMIT => {
                Ok(Some((status, stdout)))
            }
            Ran::Exited { .. } | Ran::NotRun => Ok(None),
            Ran::TimedOut => {
                if shared {
                    if let Budget::Active {
                        deadline: Some(end),
                    } = *self.budget
                    {
                        if self.clock.monotonic() >= end {
                            return Err(Halt::Raised("ReadDeadline"));
                        }
                    }
                }
                Ok(None)
            }
        }
    }

    /// `_ask(key, now)`: git's answer for the PATH `key` names, or the
    /// fallback, not ok. Err where the reference raises, or under the
    /// Windows flavour.
    pub fn ask(&mut self, key: &str, now: f64) -> Result<Answer, Declined> {
        if self.place.flavour == Flavour::Windows {
            return Err(Declined::NotPorted);
        }
        match self.asked(key, now) {
            Ok(a) => Ok(a),
            Err(Halt::Raised(c)) => Err(Declined::Raised(c)),
            Err(Halt::NotPorted) => Err(Declined::NotPorted),
        }
    }

    fn failed(key: &str, now: f64) -> Answer {
        Answer {
            key: key.to_owned(),
            git: None,
            stamp: None,
            asked: now,
            ok: false,
            sources: Sources {
                system: Some(cps(FALLBACK)),
                ..Sources::default()
            },
            tried: None,
        }
    }

    fn asked(&mut self, key: &str, now: f64) -> Result<Answer, Halt> {
        let place = self.place;
        let failed = Self::failed(key, now);
        let Some(git) = which(place.fs, place.flavour, place.cwd, self.env, self.cs_path) else {
            return Ok(failed);
        };
        let stamp = match encoded(place.flavour, &git) {
            Some(bytes) => file_stamp(place.fs, &paths::opened(place.flavour, &bytes, place.cwd))?,
            None => Some(UNSTATED),
        };
        let stamp = match stamp {
            Some(s) if s != UNSTATED => s,
            _ => return Ok(failed),
        };
        let deadline = self.clock.monotonic() + ASK_SECONDS;
        let Some((real, ceiling_dir)) = place.ask_folder()? else {
            return Ok(failed);
        };
        let ceiling = vec![(
            cps("GIT_CEILING_DIRECTORIES"),
            surrogateescape(&ceiling_dir),
        )];
        let named = self.run_git(&VAR, &real, None, ASK_SECONDS, &ceiling, &OVERRIDES)?;
        let left = deadline - self.clock.monotonic();
        let Some((status, out)) = named else {
            return Ok(failed);
        };
        if left <= 0.0 {
            return Ok(failed);
        }
        let system = if status == 0 {
            Some(strip(&answers::text(&out)).to_vec())
        } else {
            self.older_system(&real, &ceiling, deadline)?
        };
        let Some(system) = system else {
            return Ok(failed);
        };
        let mut devnull = devnull_env();
        devnull.extend(ceiling.iter().cloned());
        // `max(deadline - time.monotonic(), 0.0)`: the first unless the
        // second is greater.
        let left = deadline - self.clock.monotonic();
        let timeout = if 0.0 > left { 0.0 } else { left };
        let Some((status, listing)) =
            self.run_git(&LIST, &real, None, timeout, &devnull, &OVERRIDES)?
        else {
            return Ok(failed);
        };
        let own = if status == 0 {
            answers::own_files(place.flavour, &listing).map_err(raised)?
        } else {
            Vec::new()
        };
        let version = self.version(&real, &ceiling, deadline)?;
        let probed = self.reads_remotes(&real, &ceiling, deadline)?;
        let sources = Sources {
            own,
            system: Some(if system.is_empty() {
                cps(FALLBACK)
            } else {
                system
            }),
            legacy: probed.legacy,
            resets: probed.resets,
            prefix: probed.prefix,
            compat_differ: probed.compat_differ,
            hasconfig: probed.hasconfig,
            version,
        };
        Ok(Answer {
            key: key.to_owned(),
            git: Some(git),
            stamp: Some(recorded(&stamp)),
            asked: now,
            ok: true,
            sources,
            tried: None,
        })
    }

    /// `_older_system(folder, ceiling, deadline)`.
    fn older_system(
        &mut self,
        folder: &[u8],
        ceiling: &[(PyStr, PyStr)],
        deadline: f64,
    ) -> Result<Option<PyStr>, Halt> {
        let left = deadline - self.clock.monotonic();
        if left <= 0.0 {
            return Ok(None);
        }
        let mut extra = vec![(cps("GIT_CONFIG_GLOBAL"), cps(DEVNULL))];
        extra.extend(ceiling.iter().cloned());
        let Some((status, out)) =
            self.run_git(&SYSTEM_LIST, folder, None, left, &extra, &OVERRIDES)?
        else {
            return Ok(None);
        };
        answers::older_system(self.place.flavour, status, &out)
            .map(Some)
            .map_err(raised)
    }

    /// `_version(folder, ceiling, deadline)`.
    fn version(
        &mut self,
        folder: &[u8],
        ceiling: &[(PyStr, PyStr)],
        deadline: f64,
    ) -> Result<Option<[Natural; 3]>, Halt> {
        let left = deadline - self.clock.monotonic();
        if left <= 0.0 {
            return Ok(None);
        }
        match self.run_git(&VERSION_ASK, folder, None, left, ceiling, &OVERRIDES)? {
            Some((0, out)) => answers::parse_version(&answers::text(&out)).map_err(raised),
            _ => Ok(None),
        }
    }

    /// `_probe_ask(question, folder, git_dir, ceiling, deadline)`.
    fn probe_ask(
        &mut self,
        question: &[&str],
        folder: &[u8],
        git_dir: &[u32],
        ceiling: &[(PyStr, PyStr)],
        deadline: f64,
    ) -> Result<Done, Halt> {
        let left = deadline - self.clock.monotonic();
        if left <= 0.0 {
            return Ok(None);
        }
        let mut extra = devnull_env();
        extra.push((cps("GIT_CONFIG_NOSYSTEM"), cps("1")));
        extra.extend(ceiling.iter().cloned());
        self.run_git(question, folder, Some(git_dir), left, &extra, &PROBE_UNSET)
    }

    /// `_reads_remotes(folder, ceiling, deadline)`.
    fn reads_remotes(
        &mut self,
        folder: &[u8],
        ceiling: &[(PyStr, PyStr)],
        deadline: f64,
    ) -> Result<answers::Probed, Halt> {
        let place = self.place;
        let clock = &mut *self.clock;
        let Some((root, before)) = place.probe_repository_at(&mut || clock.wall())? else {
            return Ok(answers::Probed::default());
        };
        let root_text = surrogateescape(&root);
        let mut said: Vec<(i32, Vec<u8>)> = Vec::with_capacity(2);
        for question in [&REMOTE_ASK[..], &PREFIX_ASK[..]] {
            match self.probe_ask(question, folder, &root_text, ceiling, deadline)? {
                Some((0, out)) => said.push((0, out)),
                _ => return Ok(answers::Probed::default()),
            }
        }
        let compat_dir =
            surrogateescape(&paths::join(place.flavour, &root, COMPAT_PROBE.as_bytes()));
        let compat = self.probe_ask(&COMPAT_ASK, folder, &compat_dir, ceiling, deadline)?;
        let held = place.probe_stamps(&root)? == before
            && place
                .probe_holds(&root)
                .map_err(|e| Halt::Raised(e.name()))?;
        Ok(answers::reads_remotes(
            Some((0, &said[0].1)),
            Some((0, &said[1].1)),
            compat.map(|c| c.0),
            held,
        ))
    }
}

fn raised(r: Raised) -> Halt {
    Halt::Raised(match r {
        Raised::ValueError => "ValueError",
        Raised::UnicodeDecodeError => "UnicodeDecodeError",
    })
}

fn devnull_env() -> Vec<(PyStr, PyStr)> {
    vec![
        (cps("GIT_CONFIG_SYSTEM"), cps(DEVNULL)),
        (cps("GIT_CONFIG_GLOBAL"), cps(DEVNULL)),
    ]
}

/// A stamp as the record holds it.
fn recorded(stamp: &Stamp) -> RecordedStamp {
    std::array::from_fn(|i| stamp[i].to_string())
}

/// `str.strip()`.
fn strip(s: &[u32]) -> &[u32] {
    let Some(start) = s.iter().position(|&c| !unicode::is_space(c)) else {
        return &[];
    };
    let end = s
        .iter()
        .rposition(|&c| !unicode::is_space(c))
        .unwrap_or(start)
        + 1;
    &s[start..end]
}
