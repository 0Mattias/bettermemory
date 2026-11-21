//! Discovery: where the repository whose working tree holds a folder
//! keeps its files, found as git's `setup.c` walks (`githead.py` 138-553
//! and 669-684 at v9): [`find_gitdir`], [`gitdir_at`], [`locate`],
//! [`probed`], [`across_filesystems`], [`discovery_moved`], [`located`]
//! and [`stamp`].
//!
//! Nothing ambient. The file system, its flavour and the working
//! directory come in the membership rule's [`Rule`] (one file system for
//! every stat, realpath, lstat, access and read); the environment
//! variables the reference reads are an [`Env`]; the effective user and
//! each path's owner are [`Host`] (`os.geteuid`, `os.lstat(...).st_uid`).
//!
//! Every function declines under the Windows flavour: the reference's
//! `_check_environment` declines off POSIX, and `realpath` and
//! `reading_at` are not ported there. Every decline of the reference
//! (`_Unsettled`, OSError, ValueError) is None in [`find_gitdir`],
//! [`gitdir_at`] and [`locate`], as theirs is.

use crate::fsdata::realpath::{realpath, realpath_strict, StrictUnresolved, Unresolved};
use crate::fsdata::{Access, Flavour, Fs, Kind, OsError, PyErr, StdFs};
use crate::gitconfig::git_bool;
use crate::gitconfig::legacy::Stamp;
use crate::gitfile_rule::bounded::Answers;
use crate::gitfile_rule::discovery::{reading_at, Rule};
use crate::gitfile_rule::follow::Records;
use crate::gitfile_rule::gitdir::{git_directory, read, real, Ctx, Failure};
use crate::gitfile_rule::hostpath;
use crate::gitfile_rule::probe::{path_missing, Raised};

use super::GitDir;

/// `_GITFILE_LIMIT`: git refuses a `.git` file over 1 MiB.
pub const GITFILE_LIMIT: usize = 1 << 20;

/// The environment variables the reference reads here, as the process
/// holds them (None: unset). The first five are `_DISCOVERY_ENV`.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Env {
    pub git_dir: Option<Vec<u8>>,
    pub git_work_tree: Option<Vec<u8>>,
    pub git_common_dir: Option<Vec<u8>>,
    pub across_filesystem: Option<Vec<u8>>,
    pub assume_different_owner: Option<Vec<u8>>,
    pub ceilings: Option<Vec<u8>>,
    pub sudo_uid: Option<Vec<u8>>,
}

/// The owner of a path, as `os.lstat(path).st_uid` (or `os.stat` with
/// `follow`) reads it.
pub trait Owners {
    fn owner_uid(&self, path: &[u8], follow: bool) -> Result<u32, OsError>;
}

impl Owners for StdFs {
    #[cfg(unix)]
    fn owner_uid(&self, path: &[u8], follow: bool) -> Result<u32, OsError> {
        use std::os::unix::ffi::OsStrExt;
        use std::os::unix::fs::MetadataExt;
        if path.contains(&0) {
            return Err(OsError::value());
        }
        let path = std::path::Path::new(std::ffi::OsStr::from_bytes(path));
        let meta = if follow {
            std::fs::metadata(path)
        } else {
            std::fs::symlink_metadata(path)
        };
        meta.map(|m| m.uid())
            .map_err(|e| crate::fsdata::Numbered::of_io(&e).error)
    }

    /// Off POSIX the reference's ownership check reads security
    /// descriptors, not ported: every path declines.
    #[cfg(not(unix))]
    fn owner_uid(&self, _path: &[u8], _follow: bool) -> Result<u32, OsError> {
        Err(OsError::of_class(PyErr::Os))
    }
}

/// The machine facts `_check_owner` reads: the effective user and each
/// path's owner.
pub struct Host<'a, O: Owners + ?Sized> {
    pub euid: u32,
    pub owners: &'a O,
}

/// `githead.GitDir`: the git directory and common directory, and the
/// folder that holds the `.git` entry.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Found {
    pub gd: GitDir,
    pub worktree_root: Vec<u8>,
}

/// `PurePosixPath(path).parts`: empty and `.` segments dropped, `..`
/// kept, exactly two leading slashes their own root. `config::pure_path`
/// is the same rule over `str`; a path here is bytes.
pub fn parts(path: &[u8]) -> Vec<&[u8]> {
    let mut out: Vec<&[u8]> = Vec::new();
    if path.starts_with(b"//") && !path.starts_with(b"///") {
        out.push(b"//");
    } else if path.starts_with(b"/") {
        out.push(b"/");
    }
    out.extend(
        path.split(|&b| b == b'/')
            .filter(|p| !p.is_empty() && *p != b"."),
    );
    out
}

/// `os.fspath(Path(path))`: the parts joined (`.` where none).
pub fn pure(path: &[u8]) -> Vec<u8> {
    let all = parts(path);
    let (root, rest): (&[u8], &[&[u8]]) = match all.first() {
        Some(&r) if r.starts_with(b"/") => (r, &all[1..]),
        _ => (b"", &all[..]),
    };
    let mut out = root.to_vec();
    out.extend_from_slice(&rest.join(&b'/'));
    if out.is_empty() {
        out.push(b'.');
    }
    out
}

/// `_below(path, root)`: `path` strictly under `root`, part by part
/// (`os.path.normcase` is the identity on POSIX).
pub fn below(path: &[u8], root: &[u8]) -> bool {
    let (inner, outer) = (parts(path), parts(root));
    inner.len() > outer.len() && inner[..outer.len()] == outer[..]
}

fn posix<F: Fs + ?Sized>(ctx: &Ctx<'_, F>) -> Result<(), Failure> {
    match ctx.flavour {
        Flavour::Posix => Ok(()),
        Flavour::Windows => Err(Failure::NotPorted),
    }
}

fn join(a: &[u8], b: &[u8]) -> Vec<u8> {
    hostpath::join(Flavour::Posix, a, b)
}

fn dirname(path: &[u8]) -> Vec<u8> {
    hostpath::dirname(Flavour::Posix, path)
}

fn isabs(path: &[u8]) -> bool {
    path.starts_with(b"/")
}

/// `os.stat(path)`; an OSError or ValueError rises.
fn stat_of<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    path: &[u8],
) -> Result<crate::fsdata::StatResult, Failure> {
    Ok(ctx.fs.stat(&ctx.at(path)?)?)
}

/// `os.path.realpath(path)` (not strict): a ValueError rises.
fn resolved<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, path: &[u8]) -> Result<Vec<u8>, Failure> {
    match realpath(ctx.fs, ctx.flavour, path, ctx.cwd) {
        Ok(found) => Ok(found),
        Err(Unresolved::NotPorted) => Err(Failure::NotPorted),
        Err(Unresolved::Raised(class)) => Err(Failure::from(OsError::of_class(class))),
    }
}

fn device(st: &crate::fsdata::StatResult) -> Result<u64, Failure> {
    st.dev.ok_or(Failure::NotPorted)
}

/// `stamp(path, follow=...)`: `(st_mtime_ns, st_ctime_ns, st_size,
/// st_ino, st_mode)`, None where nothing is there (`path_missing`); any
/// other OSError, and a ValueError, rises.
pub fn stamp<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    path: &[u8],
    follow: bool,
) -> Result<Option<Stamp>, Failure> {
    let at = ctx.at(path)?;
    let answer = if follow {
        ctx.fs.stat(&at)
    } else {
        ctx.fs.lstat_result(&at)
    };
    let st = match answer {
        Ok(st) => st,
        Err(e) if e.class.missing() && path_missing(Raised::from(e)) => return Ok(None),
        Err(e) => return Err(e.into()),
    };
    let (Some(ctime), Some(ino), Some(mode)) = (st.ctime_ns, st.ino, st.mode) else {
        return Err(Failure::NotPorted);
    };
    Ok(Some([
        st.mtime_ns,
        ctime,
        i128::from(st.size),
        i128::from(ino),
        i128::from(mode),
    ]))
}

/// `discovery_moved()`: a variable of `_DISCOVERY_ENV` is set.
pub fn discovery_moved(env: &Env) -> bool {
    env.git_dir.is_some()
        || env.git_work_tree.is_some()
        || env.git_common_dir.is_some()
        || env.across_filesystem.is_some()
        || env.assume_different_owner.is_some()
}

/// `across_filesystems()`: False unset, else `git_bool` of the value;
/// None where git dies on it.
pub fn across_filesystems(env: &Env) -> Option<bool> {
    match &env.across_filesystem {
        None => Some(false),
        Some(raw) => git_bool(Some(raw)),
    }
}

/// `_across_filesystems()`: True where git dies on the value.
pub fn across_filesystems_or_true(env: &Env) -> bool {
    across_filesystems(env).unwrap_or(true)
}

/// `_check_environment()`.
fn check_environment<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, env: &Env) -> Result<(), Failure> {
    posix(ctx)?;
    if discovery_moved(env) {
        return Err(Failure::Unsettled);
    }
    Ok(())
}

/// `_read_gitfile(entry, directory)`: the resolved git directory a `.git`
/// file names, and its common directory.
fn read_gitfile<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    entry: &[u8],
    directory: &[u8],
) -> Result<(Vec<u8>, Vec<u8>), Failure> {
    let content = read(ctx, entry, GITFILE_LIMIT, true)?;
    if content.len() > GITFILE_LIMIT || !content.starts_with(b"gitdir: ") {
        return Err(Failure::Unsettled);
    }
    let end = content
        .iter()
        .rposition(|&b| b != b'\r' && b != b'\n')
        .map_or(0, |i| i + 1);
    let body = &content[..end];
    if body.len() < 9 || body.contains(&0) {
        return Err(Failure::Unsettled);
    }
    let named = &body[8..];
    let target = if isabs(named) {
        named.to_vec()
    } else {
        join(directory, named)
    };
    let gitdir = real(ctx, &target)?;
    let common = git_directory(ctx, &gitdir, true, true)?.ok_or(Failure::Unsettled)?;
    Ok((gitdir, common))
}

/// `_check_owner(*paths)`: each path, not followed, is the effective
/// user's; root under `SUDO_UID` declines.
fn check_owner<O: Owners + ?Sized>(
    host: &Host<'_, O>,
    env: &Env,
    paths: &[&[u8]],
) -> Result<(), Failure> {
    if host.euid == 0 && env.sudo_uid.is_some() {
        return Err(Failure::Unsettled);
    }
    for path in paths {
        if host.owners.owner_uid(path, false)? != host.euid {
            return Err(Failure::Unsettled);
        }
    }
    Ok(())
}

fn found(gitdir: &[u8], common: &[u8], directory: &[u8]) -> Found {
    Found {
        gd: GitDir {
            gitdir: pure(gitdir),
            commondir: pure(common),
        },
        worktree_root: pure(directory),
    }
}

/// How `_probe` is asked: with the owner's check (`Some`), as git
/// (`as_git`), and whether a provisional follow declines (`stable`).
struct Probe<'a, O: Owners + ?Sized> {
    owner: Option<(&'a Host<'a, O>, &'a Env)>,
    as_git: bool,
    stable: bool,
}

/// `_probe(directory, ...)`.
fn probe<F: Fs, A: Answers + ?Sized, R: Records + ?Sized, O: Owners + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    how: &Probe<'_, O>,
    directory: &[u8],
) -> Result<Option<Found>, Failure> {
    let ctx = rule.ctx;
    if !how.as_git {
        if let Some(reading) = reading_at(rule, directory)? {
            if reading.why.is_some() || (how.stable && reading.provisional) {
                return Err(Failure::Unsettled);
            }
        }
    }
    let entry = join(directory, b".git");
    let kind = match ctx.fs.stat(&ctx.at(&entry)?) {
        Ok(st) => st.kind,
        Err(e) if e.class.missing() => {
            if !path_missing(Raised::from(e)) {
                return Err(e.into());
            }
            if git_directory(ctx, directory, true, true)?.is_some() {
                return Err(Failure::Unsettled);
            }
            return Ok(None);
        }
        Err(e) => return Err(e.into()),
    };
    if kind == Kind::Regular {
        let (gitdir, common) = read_gitfile(ctx, &entry, directory)?;
        if let Some((host, env)) = how.owner {
            check_owner(host, env, &[&entry, directory, &gitdir])?;
        }
        return Ok(Some(found(&gitdir, &common, directory)));
    }
    let Some(common) = git_directory(ctx, &entry, true, true)? else {
        return Err(Failure::Unsettled);
    };
    if let Some((host, env)) = how.owner {
        check_owner(host, env, &[directory, &entry])?;
    }
    Ok(Some(found(&entry, &common, directory)))
}

/// `_ceiling_offset(path)`: git's `longest_ancestor_length` over the
/// ceilings, each resolved strictly (one that does not resolve declines
/// the whole discovery); -1 where none is a strict ancestor.
fn ceiling_offset<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    env: &Env,
    path: &[u8],
) -> Result<i64, Failure> {
    let raw = match &env.ceilings {
        Some(raw) if !raw.is_empty() => raw,
        _ => return Ok(-1),
    };
    if path == b"/" {
        return Ok(-1);
    }
    let mut best = -1;
    let mut verbatim = false;
    for entry in raw.split(|&b| b == b':') {
        if entry.is_empty() {
            verbatim = true;
            continue;
        }
        if !isabs(entry) {
            continue;
        }
        let ceiling = if verbatim {
            entry.to_vec()
        } else {
            real(ctx, entry)?
        };
        let length = if ceiling.ends_with(b"/") {
            ceiling.len() - 1
        } else {
            ceiling.len()
        };
        if path.len() > length + 1 && path[length] == b'/' && path.starts_with(&ceiling[..length]) {
            best = best.max(i64::try_from(length).unwrap_or(i64::MAX));
        }
    }
    Ok(best)
}

/// `_discover(start, stable=...)`.
fn discover<F: Fs, A: Answers + ?Sized, R: Records + ?Sized, O: Owners + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    host: &Host<'_, O>,
    env: &Env,
    start: &[u8],
) -> Result<Option<Found>, Failure> {
    let ctx = rule.ctx;
    check_environment(ctx, env)?;
    let mut here = resolved(ctx, &pure(start))?;
    let status = stat_of(ctx, &here)?;
    if status.kind != Kind::Directory
        || !ctx
            .fs
            .access(&here, Access::EXECUTE)
            .map_err(|e| Failure::from(OsError::of_class(e)))?
    {
        return Err(Failure::Unsettled);
    }
    let dev = device(&status)?;
    let ceiling = ceiling_offset(ctx, env, &here)?;
    let how = Probe {
        owner: Some((host, env)),
        as_git: false,
        stable: false,
    };
    loop {
        if let Some(found) = probe(rule, &how, &here)? {
            return Ok(Some(found));
        }
        let parent = dirname(&here);
        if parent == here {
            return Ok(None);
        }
        let length = if parent == b"/" { 0 } else { parent.len() };
        if i64::try_from(length).unwrap_or(i64::MAX) <= ceiling {
            return Ok(None);
        }
        if device(&stat_of(ctx, &parent)?)? != dev {
            return Ok(None);
        }
        here = parent;
    }
}

/// `find_gitdir(start)`: the repository git discovers from `start`; None
/// outside any and wherever the files do not settle it.
pub fn find_gitdir<F: Fs, A: Answers + ?Sized, R: Records + ?Sized, O: Owners + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    host: &Host<'_, O>,
    env: &Env,
    start: &[u8],
) -> Option<Found> {
    discover(rule, host, env, start).ok().flatten()
}

/// `gitdir_at(root)`: [`find_gitdir`] without the walk.
pub fn gitdir_at<F: Fs, A: Answers + ?Sized, R: Records + ?Sized, O: Owners + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    host: &Host<'_, O>,
    env: &Env,
    root: &[u8],
) -> Option<Found> {
    let at = || -> Result<Option<Found>, Failure> {
        let ctx = rule.ctx;
        check_environment(ctx, env)?;
        let directory = resolved(ctx, &pure(root))?;
        if stat_of(ctx, &directory)?.kind != Kind::Directory {
            return Ok(None);
        }
        let how = Probe {
            owner: Some((host, env)),
            as_git: false,
            stable: false,
        };
        probe(rule, &how, &directory)
    };
    at().ok().flatten()
}

/// `_ceilings(here)`: the ceilings that are strict ancestors of `here`; a
/// relative entry and one that does not resolve are dropped, and an empty
/// entry leaves those after it as written. A ValueError rises.
fn ceilings<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    env: &Env,
    here: &[u8],
) -> Result<Vec<Vec<u8>>, Failure> {
    let Some(raw) = env.ceilings.as_ref().filter(|raw| !raw.is_empty()) else {
        return Ok(Vec::new());
    };
    let mut out = Vec::new();
    let mut verbatim = false;
    for entry in raw.split(|&b| b == b':') {
        if entry.is_empty() {
            verbatim = true;
            continue;
        }
        if !isabs(entry) {
            continue;
        }
        let ceiling = if verbatim {
            entry.to_vec()
        } else {
            match realpath_strict(ctx.fs, ctx.flavour, entry, ctx.cwd) {
                Ok(found) => found,
                Err(StrictUnresolved::NotPorted) => return Err(Failure::NotPorted),
                Err(StrictUnresolved::Raised(e)) if e.class == PyErr::Value => {
                    return Err(Failure::Value)
                }
                Err(StrictUnresolved::Raised(_)) => continue,
            }
        };
        if below(here, &ceiling) {
            out.push(ceiling);
        }
    }
    Ok(out)
}

/// `probed(here, device)`: the folders git's discovery probes from the
/// resolved folder `here`, in order, as an iterator; a stat that fails is
/// its last item.
pub struct Probed<'a, F: Fs + ?Sized> {
    ctx: &'a Ctx<'a, F>,
    ceilings: Vec<Vec<u8>>,
    device: Option<u64>,
    next: Option<Vec<u8>>,
    started: bool,
}

/// `probed(here, device)`; the ceilings are read first (a ValueError
/// rises).
pub fn probed<'a, F: Fs + ?Sized>(
    ctx: &'a Ctx<'a, F>,
    env: &Env,
    here: &[u8],
    device: Option<u64>,
) -> Result<Probed<'a, F>, Failure> {
    Ok(Probed {
        ctx,
        ceilings: ceilings(ctx, env, here)?,
        device,
        next: Some(here.to_vec()),
        started: false,
    })
}

impl<F: Fs + ?Sized> Probed<'_, F> {
    fn step(&self, here: &[u8]) -> Result<Option<Vec<u8>>, Failure> {
        let parent = dirname(here);
        if parent == here || self.ceilings.iter().any(|c| !below(&parent, c)) {
            return Ok(None);
        }
        if let Some(dev) = self.device {
            if device(&stat_of(self.ctx, &parent)?)? != dev {
                return Ok(None);
            }
        }
        Ok(Some(parent))
    }
}

impl<F: Fs + ?Sized> Iterator for Probed<'_, F> {
    type Item = Result<Vec<u8>, Failure>;

    fn next(&mut self) -> Option<Self::Item> {
        let here = self.next.take()?;
        if !self.started {
            self.started = true;
            self.next = Some(here.clone());
            return Some(Ok(here));
        }
        match self.step(&here) {
            Ok(Some(parent)) => {
                self.next = Some(parent.clone());
                Some(Ok(parent))
            }
            Ok(None) => None,
            Err(e) => Some(Err(e)),
        }
    }
}

/// `_locate(start, walk=..., as_git=..., stable=...)`.
fn locate_in<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    env: &Env,
    start: &[u8],
    walk: bool,
    as_git: bool,
    stable: bool,
) -> Option<Found> {
    let ctx = rule.ctx;
    let run = || -> Result<Option<Found>, Failure> {
        let here = real(ctx, &pure(start))?;
        let status = stat_of(ctx, &here)?;
        if status.kind != Kind::Directory {
            return Ok(None);
        }
        let how: Probe<'_, StdFs> = Probe {
            owner: None,
            as_git,
            stable,
        };
        for directory in probed(ctx, env, &here, Some(device(&status)?))? {
            let found = probe(rule, &how, &directory?)?;
            if found.is_some() || !walk {
                return Ok(found);
            }
        }
        Ok(None)
    };
    run().ok().flatten()
}

/// `locate(start, walk=..., as_git=...)`: the files of the repository
/// whose working tree holds `start`, for a reader that runs nothing it
/// configures; no owner's check, every platform the rule reads.
pub fn locate<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    env: &Env,
    start: &[u8],
    walk: bool,
    as_git: bool,
) -> Option<Found> {
    locate_in(rule, env, start, walk, as_git, false)
}

/// `_entry_above(start)`: whether `realpath(start)` or a folder above it
/// holds a `.git` or a `HEAD` entry; an lstat that fails but for absence
/// rises.
pub fn any_entry_above<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, start: &[u8]) -> Result<bool, Failure> {
    let mut here = resolved(ctx, &pure(start))?;
    loop {
        for name in [&b".git"[..], b"HEAD"] {
            match ctx.fs.lstat_result(&ctx.at(&join(&here, name))?) {
                Ok(_) => return Ok(true),
                Err(e) if e.class.missing() && path_missing(Raised::from(e)) => {}
                Err(e) => return Err(e.into()),
            }
        }
        let parent = dirname(&here);
        if parent == here {
            return Ok(false);
        }
        here = parent;
    }
}

/// `_located(start)`: [`locate`] with a provisional follow declined, for
/// `signature`; `Unsettled` under a variable that moves discovery, and
/// where none is found while a `.git` or `HEAD` entry stands above.
pub fn located<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    env: &Env,
    start: &[u8],
) -> Result<Option<Found>, Failure> {
    if discovery_moved(env) {
        return Err(Failure::Unsettled);
    }
    let found = locate_in(rule, env, start, true, false, true);
    if found.is_none() && any_entry_above(rule.ctx, start)? {
        return Err(Failure::Unsettled);
    }
    Ok(found)
}
