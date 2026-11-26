//! Unit origin-3a of `origin.py`: the repository root and head reads and
//! the reachable walk (see the README's "Repository roots, heads and the
//! reachable walk").
//!
//! - [`repo_toplevel`] (reference lines 1046-1065): `rev-parse
//!   --show-toplevel`, agreed with membership's root and resolved;
//! - [`repo_toplevel_and_head`] (1068-1100): the files first, else one
//!   `rev-parse --show-toplevel HEAD`;
//! - [`toplevel_and_head_from_files`] (1103-1149): `githead`'s discovery
//!   and HEAD, declined where the repository's or the machine's config
//!   may move the root or kill the process;
//! - [`head_sha`] (1152-1162) and [`commit_reachable`] (1165-1229);
//! - [`walk_reachable`] (1507-1588): `_walk_reachable`, the commits from
//!   an anchor to a head and the paths each one changed.
//!
//! Git runs through [`OriginRunner`] (origin's `_git` and `_git_result`,
//! with their tallies and log lines). The files are read through
//! [`Reads`]: [`RepositoryReads`] over the ported readers, or a script.

use crate::claims::strip;
use crate::fsdata::realpath::{realpath, Unresolved};
use crate::fsdata::{Flavour, Fs, PyErr, Refused};
use crate::git_calls::printed_path;
use crate::git_run::{self, Out};
use crate::gitconfig::remotes::Inputs;
use crate::gitconfig::stamped::{machine_config_reads, StampedReads};
use crate::gitfile_rule::bounded::Answers;
use crate::gitfile_rule::discovery::Rule;
use crate::gitfile_rule::follow::Records;
use crate::gitfile_rule::gitdir::Failure;
use crate::githead::discovery::{find_gitdir, pure, Env as HeadEnv, Found, Host, Owners};
use crate::githead::signature::CtxRefs;
use crate::githead::GitDir;
use crate::gitsystem::answers;
use crate::origin_dates::{is_full_commit_sha, OriginGit, WALK_MARK};
use crate::origin_git::{self, Completed, Level, Logger, OriginRunner, Output};
use crate::origin_gitfiles::facts::machine_config_dies;
use crate::origin_gitfiles::machine::spelled_as_git;
use crate::origin_gitfiles::root::{config_moves_worktree, ConfigMemo};
use crate::origin_gitfiles::{unquote_c_path, Raised};
use crate::origin_history::{cuts, Cut, ReachableWalk, AS_STORED_DIFF};
use crate::pyvalue::PyStr;
use crate::unicode::{cps, lower};
use std::collections::{BTreeMap, HashSet};

/// What the reference lets out of these functions, or where the port
/// declines.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    /// What the runner lets out (`origin_git::Error`).
    Git(origin_git::Error),
    /// A reader raised: ValueError (a NUL in a path) or what a file
    /// reader lets out.
    Files(Failure),
    /// `_unquote_c_path` raised (a lone surrogate in a quoted path).
    Unquote(Raised),
    /// A reader is not ported here (the Windows flavour's `realpath`,
    /// a file system without `F_GETPATH`, a machine config not judged).
    NotPorted,
}

impl From<origin_git::Error> for Error {
    fn from(error: origin_git::Error) -> Error {
        Error::Git(error)
    }
}

/// The in-process reads these functions take beside git.
pub trait Reads {
    /// The flavour of the paths, and `_WINDOWS` for `printed_path`.
    fn flavour(&self) -> Flavour;
    /// `githead.find_gitdir(cwd)`.
    fn find_gitdir(&mut self, cwd: &[u8]) -> Option<Found>;
    /// `githead.head_sha(gd)`.
    fn head_sha(&mut self, gd: &GitDir) -> Option<String>;
    /// `_config_moves_worktree(gd)`.
    fn config_moves_worktree(&mut self, gd: &GitDir) -> Result<bool, Error>;
    /// `_machine_config_dies(machine_config_reads, git_dir)`.
    fn machine_config_dies(&mut self, git_dir: &[u8]) -> Result<bool, Error>;
    /// `_spelled_as_git(Path(root))`.
    fn spelled_as_git(&mut self, root: &[u8]) -> Result<Option<Vec<u8>>, Error>;
    /// `Path(path).resolve()`, given `str(Path(path))`.
    fn resolve(&mut self, path: &[u8]) -> Result<Vec<u8>, Unresolved>;
    /// `origin_history.cuts(toplevel)`.
    fn cuts(&mut self, toplevel: &[u8]) -> Result<Option<Cut>, Error>;
}

/// [`Reads`] over the ported readers: discovery through the membership
/// rule, the config memo, the machine's config reads and the host's
/// `realpath`.
pub struct RepositoryReads<'a, F: Fs, A: Answers + ?Sized, R: Records + ?Sized, O: Owners + ?Sized>
{
    pub rule: &'a Rule<'a, F, A, R>,
    pub host: &'a Host<'a, O>,
    pub env: &'a HeadEnv,
    pub config_memo: &'a mut ConfigMemo,
    pub machine_reads: &'a mut StampedReads,
    pub inputs: &'a Inputs<'a>,
    /// The clock `machine_config_reads` reads before a first read.
    pub clock: &'a mut dyn FnMut() -> f64,
    /// `sys.platform == "darwin"`, which `_spelled_as_git` reads.
    pub darwin: bool,
}

impl<F: Fs, A: Answers + ?Sized, R: Records + ?Sized, O: Owners + ?Sized> Reads
    for RepositoryReads<'_, F, A, R, O>
{
    fn flavour(&self) -> Flavour {
        self.rule.ctx.flavour
    }
    fn find_gitdir(&mut self, cwd: &[u8]) -> Option<Found> {
        find_gitdir(self.rule, self.host, self.env, cwd)
    }
    fn head_sha(&mut self, gd: &GitDir) -> Option<String> {
        crate::githead::head_sha(&CtxRefs(self.rule.ctx), gd)
    }
    fn config_moves_worktree(&mut self, gd: &GitDir) -> Result<bool, Error> {
        config_moves_worktree(self.config_memo, self.rule.ctx, gd, self.rule.git)
            .map_err(|_| Error::NotPorted)
    }
    fn machine_config_dies(&mut self, git_dir: &[u8]) -> Result<bool, Error> {
        let fs = self.rule.ctx.fs;
        let reads = machine_config_reads(
            self.machine_reads,
            fs,
            self.inputs,
            git_dir,
            &mut *self.clock,
        );
        machine_config_dies(reads.as_deref(), self.rule.git).map_err(|_| Error::NotPorted)
    }
    fn spelled_as_git(&mut self, root: &[u8]) -> Result<Option<Vec<u8>>, Error> {
        match spelled_as_git(self.rule.ctx.fs, root, self.darwin) {
            Ok(spelled) => Ok(spelled),
            Err(Refused::Raised(PyErr::Value)) => Err(Error::Files(Failure::Value)),
            Err(_) => Err(Error::NotPorted),
        }
    }
    fn resolve(&mut self, path: &[u8]) -> Result<Vec<u8>, Unresolved> {
        let ctx = self.rule.ctx;
        realpath(ctx.fs, ctx.flavour, path, ctx.cwd)
    }
    fn cuts(&mut self, toplevel: &[u8]) -> Result<Option<Cut>, Error> {
        match cuts(self.rule, self.host, self.env, toplevel, false) {
            Ok(cut) => Ok(cut),
            Err(Failure::NotPorted) => Err(Error::NotPorted),
            Err(failure) => Err(Error::Files(failure)),
        }
    }
}

fn args(words: &[&str]) -> Vec<PyStr> {
    words.iter().map(|w| cps(w)).collect()
}

fn decode(reads: &dyn Reads, bytes: &[u8]) -> Result<PyStr, Error> {
    answers::fsdecode(reads.flavour(), bytes).map_err(|e| Error::Git(origin_git::Error::Decode(e)))
}

fn encode(reads: &dyn Reads, text: &[u32]) -> Result<Vec<u8>, Error> {
    let encoding = match reads.flavour() {
        Flavour::Posix => crate::response_write::FsEncoding::SurrogateEscape,
        Flavour::Windows => crate::response_write::FsEncoding::SurrogatePass,
    };
    crate::response_write::fsencode_as(text, encoding)
        .ok_or(Error::Git(origin_git::Error::UnicodeEncode))
}

/// `Path(raw).resolve()` as a string, None where it raises an OSError.
fn resolved(reads: &mut dyn Reads, raw: &[u32]) -> Result<Option<PyStr>, Error> {
    let mut path = encode(reads, raw)?;
    if reads.flavour() == Flavour::Posix {
        path = pure(&path);
    }
    match reads.resolve(&path) {
        Ok(found) => decode(reads, &found).map(Some),
        Err(Unresolved::Raised(PyErr::Value)) => Err(Error::Files(Failure::Value)),
        Err(Unresolved::Raised(_)) => Ok(None),
        Err(Unresolved::NotPorted) => Err(Error::NotPorted),
    }
}

fn printed<'t>(reads: &dyn Reads, text: &'t [u32]) -> &'t [u32] {
    printed_path(text, reads.flavour() == Flavour::Windows)
}

/// `_git(cwd, *args, verbatim=True, paths=True)`.
fn path_output(
    runner: &mut OriginRunner<'_>,
    cwd: &[u8],
    words: &[&str],
) -> Result<Option<PyStr>, Error> {
    let options = Output {
        verbatim: true,
        paths: true,
        ..Output::default()
    };
    Ok(runner.output(cwd, &args(words), options)?)
}

/// `repo_toplevel(cwd)`: the resolved root git prints, where membership
/// roots the checkout there.
pub fn repo_toplevel(
    runner: &mut OriginRunner<'_>,
    reads: &mut dyn Reads,
    cwd: Option<&[u8]>,
) -> Result<Option<PyStr>, Error> {
    let Some(cwd) = cwd else { return Ok(None) };
    let Some(raw) = path_output(runner, cwd, &["rev-parse", "--show-toplevel"])? else {
        return Ok(None);
    };
    let top = printed(reads, &raw);
    if top.is_empty() || !runner.agreed(cwd, top)? {
        return Ok(None);
    }
    resolved(reads, top)
}

/// `repo_toplevel_and_head(cwd)`: `(str(root), head)`.
pub fn repo_toplevel_and_head(
    runner: &mut OriginRunner<'_>,
    reads: &mut dyn Reads,
    cwd: Option<&[u8]>,
) -> Result<Option<(PyStr, PyStr)>, Error> {
    let Some(cwd) = cwd else { return Ok(None) };
    if let Some(located) = toplevel_and_head_from_files(reads, Some(cwd))? {
        return Ok(Some(located));
    }
    let words = ["rev-parse", "--show-toplevel", "HEAD"];
    let Some(raw) = path_output(runner, cwd, &words)? else {
        return Ok(None);
    };
    // `printed_path(raw).rpartition("\n")`, then the root with its
    // newline put back and taken off again as a printed path's.
    let whole = printed(reads, &raw);
    let (root, head) = match whole.iter().rposition(|&c| c == 10) {
        Some(at) => (printed(reads, &whole[..=at]), &whole[at + 1..]),
        None => (&whole[..0], whole),
    };
    if root.is_empty() || !is_full_commit_sha(head) || !runner.agreed(cwd, root)? {
        return Ok(None);
    }
    Ok(resolved(reads, root)?.map(|root| (root, head.to_vec())))
}

/// `toplevel_and_head_from_files(cwd)`: `(str(root), head)` read from
/// the repository's files.
pub fn toplevel_and_head_from_files(
    reads: &mut dyn Reads,
    cwd: Option<&[u8]>,
) -> Result<Option<(PyStr, PyStr)>, Error> {
    let Some(cwd) = cwd else { return Ok(None) };
    let Some(found) = reads.find_gitdir(cwd) else {
        return Ok(None);
    };
    let Some(head) = reads.head_sha(&found.gd) else {
        return Ok(None);
    };
    if reads.config_moves_worktree(&found.gd)? || reads.machine_config_dies(&found.gd.gitdir)? {
        return Ok(None);
    }
    let Some(root) = reads.spelled_as_git(&found.worktree_root)? else {
        return Ok(None);
    };
    Ok(Some((decode(reads, &root)?, cps(&head))))
}

/// `head_sha(cwd)`: the full hash HEAD names, lowercased.
pub fn head_sha(runner: &mut OriginRunner<'_>, cwd: Option<&[u8]>) -> Result<Option<PyStr>, Error> {
    let Some(cwd) = cwd else { return Ok(None) };
    let words = ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"];
    let Some(raw) = runner.output(cwd, &args(&words), Output::default())? else {
        return Ok(None);
    };
    let sha = lower(strip(&raw));
    Ok(is_full_commit_sha(&sha).then_some(sha))
}

fn log_nonzero(
    runner: &mut OriginRunner<'_>,
    reads: &dyn Reads,
    cwd: &[u8],
    done: &Completed,
) -> Result<(), Error> {
    let at = decode(reads, cwd)?;
    let (_, line) = git_run::log_nonzero_exit(
        &args(&["merge-base", "--is-ancestor"]),
        done.returncode,
        &Out::Text(done.stderr.clone()),
        Some(&at),
    );
    runner.lines.push((Logger::Git, Level::Debug, line));
    Ok(())
}

/// `commit_reachable(cwd, sha)`: True where `sha` is HEAD or an ancestor
/// of it, False where git establishes it is not, None where git could
/// not answer or the history it read is not complete.
pub fn commit_reachable(
    runner: &mut OriginRunner<'_>,
    reads: &mut dyn Reads,
    cwd: Option<&[u8]>,
    sha: &[u32],
) -> Result<Option<bool>, Error> {
    let Some(cwd) = cwd.filter(|_| is_full_commit_sha(sha)) else {
        return Ok(None);
    };
    let mut ask = args(&["-c", "advice.graftFileDeprecated=false", "merge-base"]);
    ask.extend([cps("--is-ancestor"), sha.to_vec(), cps("HEAD")]);
    let Some(result) = runner.result(cwd, &ask, 1.0, None, false)? else {
        return Ok(None);
    };
    if result.returncode == 0 {
        return Ok(Some(true));
    }
    log_nonzero(runner, reads, cwd, &result)?;
    if result.returncode < 0 || (result.returncode == 1 && !strip(&result.stderr).is_empty()) {
        return Ok(None);
    }
    let shallow = args(&["rev-parse", "--is-shallow-repository"]);
    let complete = runner.result(cwd, &shallow, 1.0, None, false)?;
    if !complete.is_some_and(|c| c.returncode == 0 && strip(&c.stdout) == cps("false").as_slice()) {
        return Ok(None);
    }
    if result.returncode == 1 {
        return Ok(Some(false));
    }
    if head_sha(runner, Some(cwd))?.is_none() {
        return Ok(None);
    }
    let mut verify = args(&["rev-parse", "--verify", "--quiet"]);
    verify.push([sha, &cps("^{commit}")].concat());
    let present = runner.result(cwd, &verify, 1.0, None, false)?;
    Ok(present
        .filter(|p| p.returncode == 1 && strip(&p.stderr).is_empty())
        .map(|_| false))
}

/// `_WALK_MARK` ([`WALK_MARK`]) as a code point.
const MARK: u32 = 0x01;

/// The walk's git arguments (`_walk_reachable`'s argv).
pub fn walk_args(anchor: &[u32], head: &[u32], names: bool) -> Vec<PyStr> {
    let mut out = Vec::new();
    if names {
        out.extend(args(&["-c", "core.quotePath=false"]));
    }
    let format = format!("--format={WALK_MARK}%m%H");
    out.extend(args(&["log", "--boundary", &format]));
    if names {
        out.extend(args(&AS_STORED_DIFF));
        out.extend(args(&["--no-renames", "-c", "--name-only"]));
    }
    out.push([anchor, &cps(".."), head].concat());
    out
}

/// `_walk_reachable(toplevel, anchor, head, names=names)`.
pub fn walk_reachable(
    runner: &mut OriginRunner<'_>,
    reads: &mut dyn Reads,
    toplevel: &[u8],
    anchor: &[u32],
    head: &[u32],
    names: bool,
) -> Result<Option<ReachableWalk>, Error> {
    let mut walk = ReachableWalk {
        anchor: anchor.to_vec(),
        head: head.to_vec(),
        commits: Vec::new(),
        touched: BTreeMap::new(),
        listed: true,
        timed_out: false,
    };
    if anchor == head {
        return Ok(Some(walk));
    }
    let options = Output {
        timeout: 5.0,
        empty_ok: true,
        verbatim: true,
        ..Output::default()
    };
    let raw = runner.output(toplevel, &walk_args(anchor, head, names), options)?;
    let Some(raw) = raw.filter(|r| !r.is_empty()) else {
        return Ok(None);
    };
    let mut boundary: HashSet<PyStr> = HashSet::new();
    let mut current: Option<PyStr> = None;
    for line in raw.split(|&c| c == 10) {
        if line.first() == Some(&MARK) {
            let mark = &line[1..line.len().min(2)];
            let sha = lower(strip(line.get(2..).unwrap_or_default()));
            if !is_full_commit_sha(&sha) {
                OriginGit::note_failed(runner);
                return Ok(None);
            }
            if mark == [u32::from(b'-')] {
                boundary.insert(sha);
                current = None;
            } else {
                walk.commits.push(sha.clone());
                current = Some(sha);
            }
            continue;
        }
        let Some(commit) = current.as_ref().filter(|_| !line.is_empty()) else {
            continue;
        };
        let quoted = line.first() == Some(&0x22) && line.last() == Some(&0x22);
        let path = match quoted {
            true => unquote_c_path(line).map_err(Error::Unquote)?,
            false => None,
        }
        .unwrap_or_else(|| line.to_vec());
        walk.touched.entry(path).or_default().insert(commit.clone());
    }
    if !boundary.contains(anchor) {
        return Ok(None);
    }
    match reads.cuts(toplevel)? {
        Some(cut) if !walk.commits.iter().any(|c| cut.contains(c)) => {}
        _ => return Ok(None),
    }
    walk.listed = names;
    Ok(Some(walk))
}
