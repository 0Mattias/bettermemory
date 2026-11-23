//! The rest of discovery and the change signature (`githead.py` 195-203,
//! 576-666 and 1097-1209 at v9): [`entry_above`] with [`stops_at`] and
//! [`would_be_git_directory`], and [`signature`] with [`ref_stamps`].
//!
//! Nothing ambient: the file system, its flavour and the working
//! directory come in the membership rule's [`Rule`], the environment in
//! an [`Env`], the effective user and the owners in a [`Host`]. HEAD, the
//! refs and the stamps are read from that one file system: [`CtxRefs`]
//! hands it to the resolution half's [`RefFs`] reader.

use std::sync::atomic::{AtomicU64, Ordering};

use crate::fsdata::realpath::{realpath, Unresolved};
use crate::fsdata::Flavour;
use crate::fsdata::{Fs, Kind, OsError, PyErr};
use crate::gitconfig::legacy::Stamp;
use crate::gitfile_rule::bounded::Answers;
use crate::gitfile_rule::discovery::Rule;
use crate::gitfile_rule::follow::Records;
use crate::gitfile_rule::gitdir::{git_common, read, valid_headref, Ctx, Failure};
use crate::gitfile_rule::hostpath;
use crate::gitfile_rule::probe::{path_missing, Raised};
use crate::gitfile_rule::{headref_content, HEADREF_READ};

use super::discovery::{
    across_filesystems_or_true, discover, located, probed, pure, stamp, Env, Found, Host, Owners,
};
use super::{head_bytes, parse, FsError, GitDir, RefFs, MAX_READS, PER_WORKTREE, REF_LIMIT};

const S_IFMT: i128 = 0o170_000;
const S_IFDIR: i128 = 0o040_000;
const S_IFREG: i128 = 0o100_000;

/// A [`RefFs`] over a [`Ctx`]: `read` is `gitfile_rule._read`, and
/// `lstat_is_dir` an `lstat`, both through the context's file system.
pub struct CtxRefs<'a, F: Fs + ?Sized>(pub &'a Ctx<'a, F>);

fn fs_error(failure: Failure) -> FsError {
    match failure {
        Failure::Os(e) => os_error(e),
        _ => FsError::Other,
    }
}

fn os_error(e: OsError) -> FsError {
    match e.class {
        PyErr::FileNotFound if path_missing(Raised::from(e)) => FsError::Missing,
        PyErr::NotADirectory => FsError::NotADirectory,
        PyErr::IsADirectory => FsError::IsADirectory,
        _ => FsError::Other,
    }
}

impl<F: Fs + ?Sized> RefFs for CtxRefs<'_, F> {
    fn read(&self, path: &[u8], limit: usize, follow: bool) -> Result<Vec<u8>, FsError> {
        read(self.0, path, limit, follow).map_err(fs_error)
    }

    fn lstat_is_dir(&self, path: &[u8]) -> Result<bool, FsError> {
        let at = self.0.at(path).map_err(fs_error)?;
        match self.0.fs.lstat_result(&at) {
            Ok(st) => Ok(st.kind == Kind::Directory),
            Err(e) => Err(os_error(e)),
        }
    }
}

/// The [`GitDir`] resolution reads, of a discovered repository.
pub fn git_dir_of(found: &Found) -> GitDir {
    found.gd.clone()
}

fn join(a: &[u8], b: &[u8]) -> Vec<u8> {
    hostpath::join(Flavour::Posix, a, b)
}

/// `os.stat(path)` where absence is `None`; any other failure rises.
fn present<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    path: &[u8],
    follow: bool,
) -> Result<Option<Kind>, Failure> {
    let at = ctx.at(path)?;
    let answer = if follow {
        ctx.fs.stat(&at)
    } else {
        ctx.fs.lstat_result(&at)
    };
    match answer {
        Ok(st) => Ok(Some(st.kind)),
        Err(e) if e.class.missing() && path_missing(Raised::from(e)) => Ok(None),
        Err(e) => Err(e.into()),
    }
}

/// `_would_be_git_directory(candidate)`: git's `is_git_directory` read
/// past what git may not read.
pub fn would_be_git_directory<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    candidate: &[u8],
) -> Result<bool, Failure> {
    let head = join(candidate, b"HEAD");
    let Some(kind) = present(ctx, &head, false)? else {
        return Ok(false);
    };
    let settled = || -> Result<Option<Vec<u8>>, Failure> {
        if kind == Kind::Regular {
            let content = match read(ctx, &head, HEADREF_READ, true) {
                Ok(content) => Some(content),
                Err(Failure::Os(e)) if path_missing(Raised::from(e)) => return Ok(None),
                Err(Failure::Os(_)) => None,
                Err(other) => return Err(other),
            };
            if content.is_some_and(|c| !headref_content(&c)) {
                return Ok(None);
            }
        } else if !valid_headref(ctx, &head, true, true)? {
            return Ok(None);
        }
        Ok(Some(git_common(ctx, candidate)?.0))
    };
    let common = match settled() {
        Ok(Some(common)) => common,
        Ok(None) => return Ok(false),
        Err(Failure::Unsettled) => return Ok(true),
        Err(other) => return Err(other),
    };
    for name in [&b"objects"[..], b"refs"] {
        if present(ctx, &join(&common, name), true)?.is_none() {
            return Ok(false);
        }
    }
    Ok(true)
}

/// `_stops_at(directory)`: a `.git` that is a regular file, or a `.git`
/// or the directory itself that would be a git directory.
pub fn stops_at<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, directory: &[u8]) -> Result<bool, Failure> {
    let dotgit = join(directory, b".git");
    if let Some(kind) = present(ctx, &dotgit, true)? {
        if kind == Kind::Regular || would_be_git_directory(ctx, &dotgit)? {
            return Ok(true);
        }
    }
    would_be_git_directory(ctx, directory)
}

/// `entry_above(start)`: whether git's walk from `start` passes a
/// directory it would stop at. True on any OSError or ValueError, False
/// where `start` is missing or no directory. None (a decline) where the
/// flavour's resolution is not ported, or for an exception the reference
/// does not catch.
pub fn entry_above<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, env: &Env, start: &[u8]) -> Option<bool> {
    let walk = || -> Result<bool, Failure> {
        let here = match realpath(ctx.fs, ctx.flavour, &pure(start), ctx.cwd) {
            Ok(here) => here,
            Err(Unresolved::NotPorted) => return Err(Failure::NotPorted),
            Err(Unresolved::Raised(class)) => return Err(OsError::of_class(class).into()),
        };
        let status = match ctx.fs.stat(&ctx.at(&here)?) {
            Ok(st) => st,
            Err(e) if path_missing(Raised::from(e)) => return Ok(false),
            Err(e) => return Err(e.into()),
        };
        if status.kind != Kind::Directory {
            return Ok(false);
        }
        let device = if across_filesystems_or_true(env) {
            None
        } else {
            Some(status.dev.ok_or(Failure::NotPorted)?)
        };
        for directory in probed(ctx, env, &here, device)? {
            if stops_at(ctx, &directory?)? {
                return Ok(true);
            }
        }
        Ok(false)
    };
    match walk() {
        Ok(answer) => Some(answer),
        Err(Failure::Os(_) | Failure::Value) => Some(true),
        Err(_) => None,
    }
}

/// `_ref_stamps(gd, head)`: the stamp of each loose ref file the chain
/// from HEAD reads, in order.
pub fn ref_stamps<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    gd: &GitDir,
    head: &[u8],
) -> Result<Vec<Option<Stamp>>, Failure> {
    let (mut name, _) = parse(head).map_err(|_| Failure::Unsettled)?;
    let mut stamps = Vec::new();
    let mut reads = 1;
    while let Some(current) = name {
        if reads == MAX_READS {
            return Err(Failure::Unsettled);
        }
        reads += 1;
        let base = if PER_WORKTREE.iter().any(|p| current.starts_with(p)) {
            &gd.gitdir
        } else {
            &gd.commondir
        };
        let mut path = base.clone();
        path.push(b'/');
        path.extend_from_slice(&current);
        let found = stamp(ctx, &path, false)?;
        stamps.push(found);
        let Some(found) = found else { break };
        if found[4] & S_IFMT == S_IFDIR {
            break;
        }
        if found[4] & S_IFMT != S_IFREG {
            return Err(Failure::Unsettled);
        }
        let content = read(ctx, &path, REF_LIMIT, false)?;
        if content.len() > REF_LIMIT {
            return Err(Failure::Unsettled);
        }
        name = parse(&content).map_err(|_| Failure::Unsettled)?.0;
    }
    Ok(stamps)
}

/// `_Undetermined()`'s counter: each undetermined signature takes a new
/// value, so no two compare equal.
static UNDETERMINED: AtomicU64 = AtomicU64::new(0);

/// The first slot of a [`Signature`].
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum First {
    /// None: outside any repository.
    Outside,
    /// The git directory, as `str(gd.gitdir)`.
    GitDir(Vec<u8>),
    /// `_Undetermined()`: equal to no other signature.
    Undetermined(u64),
}

/// `githead.Signature`.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Signature {
    pub first: First,
    pub head: Option<Vec<u8>>,
    pub config: Option<Stamp>,
    pub head_file: Option<Stamp>,
    pub refs: Option<Vec<Option<Stamp>>>,
    pub packed: Option<Stamp>,
    pub git_dir: Option<Vec<u8>>,
    pub git_work_tree: Option<Vec<u8>>,
    pub ceilings: Option<Vec<u8>>,
}

/// A fresh `_Undetermined()`: a first slot equal to no other.
pub fn undetermined() -> First {
    First::Undetermined(UNDETERMINED.fetch_add(1, Ordering::Relaxed))
}

fn bare(first: First, env: &Env) -> Signature {
    Signature {
        first,
        head: None,
        config: None,
        head_file: None,
        refs: None,
        packed: None,
        git_dir: env.git_dir.clone(),
        git_work_tree: env.git_work_tree.clone(),
        ceilings: env.ceilings.clone(),
    }
}

/// `signature(start, owner=...)`. An exception the reference does not
/// catch (another class than `_Unsettled`, OSError and ValueError) is
/// `Err` with its class name.
pub fn signature<F: Fs, A: Answers + ?Sized, R: Records + ?Sized, O: Owners + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    host: &Host<'_, O>,
    env: &Env,
    start: &[u8],
    owner: bool,
) -> Result<Signature, &'static str> {
    let ctx = rule.ctx;
    let read_all = || -> Result<Signature, Failure> {
        let found = if owner {
            discover(rule, host, env, start, true)?
        } else {
            located(rule, env, start)?
        };
        let Some(found) = found else {
            return Ok(bare(First::Outside, env));
        };
        let gd = git_dir_of(&found);
        let head = head_bytes(&CtxRefs(ctx), &gd).ok_or(Failure::Unsettled)?;
        let refs = ref_stamps(ctx, &gd, &head)?;
        let head_file = stamp(ctx, &join(&gd.gitdir, b"HEAD"), false)?;
        let config = stamp(ctx, &join(&gd.commondir, b"config"), true)?;
        let packed = stamp(ctx, &join(&gd.commondir, b"packed-refs"), true)?;
        Ok(Signature {
            first: First::GitDir(gd.gitdir.clone()),
            head: Some(head),
            config,
            head_file,
            refs: Some(refs),
            packed,
            ..bare(First::Outside, env)
        })
    };
    match read_all() {
        Ok(signature) => Ok(signature),
        Err(Failure::Raised(class)) => Err(class),
        Err(_) => Ok(bare(undetermined(), env)),
    }
}
