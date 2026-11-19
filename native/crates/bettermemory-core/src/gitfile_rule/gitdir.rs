//! Unit 2, first part: git's own reading of a git directory
//! (`gitfile_rule.py` 1062-1287): the bounded read that may refuse a final
//! link ([`read`], `_read` 1083-1124, with `_is_directory` 1127-1134), the
//! placeholder test (`_placeholder` 1062-1070, [`StatResult::placeholder`]),
//! the strict resolutions ([`real`] and [`real_to_last`], 1137-1164), HEAD's
//! validation ([`valid_headref`], 1167-1193), the common directory
//! ([`git_common`], 1207-1232) and the test of a git directory
//! ([`git_found`], [`git_directory`], [`git_read`], [`git_reads`],
//! 1235-1287).
//!
//! Every path goes to the file system through one place, [`Ctx::at`]: a
//! relative path is taken from the working directory given as an input
//! (`cwd`), never the process's own, as the reference's `os.open`,
//! `os.lstat` and `os.access` take a relative path from `os.getcwd()`. On
//! the Windows flavour a relative path is declined ([`Failure::NotPorted`]):
//! `ntpath`'s drive-relative joins are not ported. The strict resolutions
//! are `posixpath`'s alone: on Windows they, and so a `commondir`, are
//! declined.
//!
//! [`StatResult::placeholder`]: crate::fsdata::StatResult::placeholder

use crate::fsdata::realpath::{realpath_strict, StrictUnresolved};
use crate::fsdata::{Access, Flavour, Fs, Kind, OsError, PyErr};

use super::hostpath;
use super::probe::{path_missing, Raised};
use super::{headref_content, HEADREF_READ, READ_CHUNK, REF_LIMIT};

/// What the reference raises out of these functions, as a typed outcome.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Failure {
    /// `_Unsettled`: git would die or block reading here.
    Unsettled,
    /// An OSError, with its class and errno's name.
    Os(OsError),
    /// ValueError (a NUL in a path).
    Value,
    /// Not ported (the Windows flavour's resolutions, or a relative path
    /// there): a caller declines.
    NotPorted,
    /// Another exception the reference does not catch, by class name
    /// (RecursionError from a pin record's JSON, unit 4).
    Raised(&'static str),
}

impl From<OsError> for Failure {
    fn from(error: OsError) -> Failure {
        if error.class == PyErr::Value {
            Failure::Value
        } else {
            Failure::Os(error)
        }
    }
}

/// The file system, its flavour and the working directory a relative
/// path is taken from.
pub struct Ctx<'a, F: Fs + ?Sized> {
    pub fs: &'a F,
    pub flavour: Flavour,
    /// What `os.getcwd()` returned: absolute and physical (free of
    /// links). This is the caller's contract, not checked here: a `cwd`
    /// with a link is taken as given (`tree_of(b".git")` answers its
    /// spelling, unresolved). A caller whose `getcwd` fails (macOS: a
    /// working directory it cannot read) builds no `Ctx` and declines
    /// whole, the safe direction: the reference's `_tree_of` answers None
    /// there, never a tree.
    pub cwd: &'a [u8],
}

impl<'a, F: Fs + ?Sized> Ctx<'a, F> {
    /// The path the OS would be asked for: `path` itself where absolute,
    /// else under the working directory. The one place a relative path is
    /// resolved.
    pub fn at(&self, path: &[u8]) -> Result<Vec<u8>, Failure> {
        if hostpath::isabs(self.flavour, path) {
            return Ok(path.to_vec());
        }
        match self.flavour {
            Flavour::Posix => {
                if path.is_empty() {
                    // `os.open("")` and `os.lstat("")`: ENOENT, whatever
                    // the working directory.
                    return Ok(Vec::new());
                }
                Ok(hostpath::join(Flavour::Posix, self.cwd, path))
            }
            Flavour::Windows => Err(Failure::NotPorted),
        }
    }

    fn join(&self, a: &[u8], b: &[u8]) -> Vec<u8> {
        hostpath::join(self.flavour, a, b)
    }
}

fn os(class: PyErr) -> OsError {
    OsError {
        class,
        errno: None,
        winerror: None,
    }
}

/// `_is_directory(path, follow=...)`: a folder by `stat` (or `lstat`);
/// False where it cannot be stated.
fn is_directory<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, path: &[u8], follow: bool) -> bool {
    let answer = if follow {
        ctx.fs.stat(path)
    } else {
        ctx.fs.lstat_result(path)
    };
    answer.is_ok_and(|st| st.kind == Kind::Directory)
}

/// `_read(path, limit, follow=...)`: up to `limit + 1` leading bytes of the
/// regular file `path`. A non-blocking open (a FIFO fails at the `fstat`,
/// never waits); without `follow` a final link is ELOOP. A PermissionError
/// at the open of a folder (Windows; POSIX for a folder that cannot be
/// read) is IsADirectoryError; a folder is IsADirectoryError, any other
/// kind but a regular file OSError(EINVAL). Read in chunks of
/// `_READ_CHUNK`.
pub fn read<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    path: &[u8],
    limit: usize,
    follow: bool,
) -> Result<Vec<u8>, Failure> {
    let path = ctx.at(path)?;
    let opened = if follow {
        ctx.fs.open_result(&path)
    } else {
        ctx.fs.open_no_follow(&path)
    };
    let mut file = match opened {
        Ok(file) => file,
        Err(e) if e.class == PyErr::Permission && is_directory(ctx, &path, follow) => {
            return Err(Failure::Os(OsError::new(PyErr::IsADirectory, "EISDIR")));
        }
        Err(e) => return Err(e.into()),
    };
    let kind = ctx.fs.fstat(&mut file).map_err(os)?.kind;
    if kind == Kind::Directory {
        return Err(Failure::Os(OsError::new(PyErr::IsADirectory, "EISDIR")));
    }
    if kind != Kind::Regular {
        return Err(Failure::Os(OsError::new(PyErr::Os, "EINVAL")));
    }
    let mut out = Vec::new();
    while out.len() <= limit {
        let room = READ_CHUNK.min(limit + 1 - out.len());
        let chunk = ctx.fs.read(&mut file, room).map_err(os)?;
        if chunk.is_empty() {
            break;
        }
        out.extend_from_slice(&chunk[..chunk.len().min(room)]);
    }
    Ok(out)
}

/// `_real(path)`: `realpath(path, strict=True)`; any OSError is
/// `_Unsettled`, a ValueError rises.
pub fn real<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, path: &[u8]) -> Result<Vec<u8>, Failure> {
    match realpath_strict(ctx.fs, ctx.flavour, path, ctx.cwd) {
        Ok(found) => Ok(found),
        Err(StrictUnresolved::NotPorted) => Err(Failure::NotPorted),
        Err(StrictUnresolved::Raised(e)) if e.class == PyErr::Value => Err(Failure::Value),
        Err(StrictUnresolved::Raised(_)) => Err(Failure::Unsettled),
    }
}

/// `os.path.split` (POSIX).
fn split(path: &[u8]) -> (Vec<u8>, Vec<u8>) {
    let i = path.iter().rposition(|&b| b == b'/').map_or(0, |i| i + 1);
    (hostpath::dirname(Flavour::Posix, path), path[i..].to_vec())
}

/// `_real_to_last(path)`: `path` resolved strictly; where only its last
/// component is missing (FileNotFoundError that `path_missing` reads as
/// nothing there), that name below its resolved folder; any other error
/// `_Unsettled`, as is a last name of `""`, `.` or `..`.
pub fn real_to_last<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, path: &[u8]) -> Result<Vec<u8>, Failure> {
    match realpath_strict(ctx.fs, ctx.flavour, path, ctx.cwd) {
        Ok(found) => return Ok(found),
        Err(StrictUnresolved::NotPorted) => return Err(Failure::NotPorted),
        Err(StrictUnresolved::Raised(e)) if e.class == PyErr::Value => return Err(Failure::Value),
        Err(StrictUnresolved::Raised(e)) => {
            if !(e.class == PyErr::FileNotFound && path_missing(Raised::from(e))) {
                return Err(Failure::Unsettled);
            }
        }
    }
    // `path.rstrip("/") or path` (`os.sep` is `/` here: Windows was
    // declined above).
    let end = path.iter().rposition(|&b| b != b'/').map_or(0, |i| i + 1);
    let trimmed = if end == 0 { path } else { &path[..end] };
    let (parent, last) = split(trimmed);
    if last.is_empty() || last == b"." || last == b".." {
        return Err(Failure::Unsettled);
    }
    let resolved = real(ctx, &parent)?;
    Ok(ctx.join(&resolved, &last))
}

/// `_valid_headref(path, fetch=..., read=...)`: git's `validate_headref`.
/// A link is valid when its target starts with `refs/`; a folder is not;
/// any kind but a regular file is `_Unsettled` (git would wait on a FIFO
/// or read a device). With `read` False a regular file passes unread; with
/// `fetch` False so does a placeholder. Any OSError is False.
pub fn valid_headref<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    path: &[u8],
    fetch: bool,
    read_it: bool,
) -> Result<bool, Failure> {
    let at = ctx.at(path)?;
    let st = match ctx.fs.lstat_result(&at) {
        Ok(st) => st,
        Err(e) if e.class == PyErr::Value => return Err(Failure::Value),
        Err(_) => return Ok(false),
    };
    match st.kind {
        Kind::Link => {
            return match ctx.fs.readlink_result(&at) {
                Ok(target) => Ok(target.starts_with(b"refs/")),
                Err(e) if e.class == PyErr::Value => Err(Failure::Value),
                Err(_) => Ok(false),
            }
        }
        Kind::Directory => return Ok(false),
        Kind::Regular => {}
        _ => return Err(Failure::Unsettled),
    }
    if !read_it || (!fetch && st.placeholder()) {
        return Ok(true);
    }
    match read(ctx, path, HEADREF_READ, true) {
        Ok(content) => Ok(headref_content(&content)),
        Err(Failure::Os(_)) => Ok(false),
        Err(other) => Err(other),
    }
}

/// `_git_common(gitdir)`: the directory a `commondir` file names (from
/// `gitdir` unless absolute), resolved by [`real_to_last`], and True; or
/// `gitdir` itself and False where there is no such file (`path_missing`).
/// `_Unsettled` for one that cannot be read, is empty, over
/// `_REF_LIMIT`, or holds a NUL once its line ends are cut.
pub fn git_common<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    gitdir: &[u8],
) -> Result<(Vec<u8>, bool), Failure> {
    let content = match read(ctx, &ctx.join(gitdir, b"commondir"), REF_LIMIT, true) {
        Ok(content) => content,
        Err(Failure::Os(e)) => {
            if path_missing(Raised::from(e)) {
                return Ok((gitdir.to_vec(), false));
            }
            return Err(Failure::Unsettled);
        }
        Err(other) => return Err(other),
    };
    let end = content
        .iter()
        .rposition(|&b| b != b'\r' && b != b'\n')
        .map_or(0, |i| i + 1);
    let target = &content[..end];
    if content.is_empty() || content.len() > REF_LIMIT || target.contains(&0) {
        return Err(Failure::Unsettled);
    }
    let name = if hostpath::isabs(ctx.flavour, target) {
        target.to_vec()
    } else {
        ctx.join(gitdir, target)
    };
    Ok((real_to_last(ctx, &name)?, true))
}

/// `_git_found(candidate, fetch=..., read=...)`: git's `is_git_directory`:
/// a valid HEAD, then the common directory ([`git_common`]) holding
/// `objects` and `refs` that can be searched (`os.access(X_OK)`); None
/// where it is no git directory.
pub fn git_found<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    candidate: &[u8],
    fetch: bool,
    read_it: bool,
) -> Result<Option<(Vec<u8>, bool)>, Failure> {
    if !valid_headref(ctx, &ctx.join(candidate, b"HEAD"), fetch, read_it)? {
        return Ok(None);
    }
    let common = git_common(ctx, candidate)?;
    for name in [&b"objects"[..], b"refs"] {
        let at = ctx.at(&ctx.join(&common.0, name))?;
        if !ctx
            .fs
            .access(&at, Access::EXECUTE)
            .map_err(|_| Failure::Value)?
        {
            return Ok(None);
        }
    }
    Ok(Some(common))
}

/// `_git_directory(candidate)`: the common directory of a git directory,
/// None where `candidate` is none.
pub fn git_directory<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    candidate: &[u8],
    fetch: bool,
    read_it: bool,
) -> Result<Option<Vec<u8>>, Failure> {
    Ok(git_found(ctx, candidate, fetch, read_it)?.map(|found| found.0))
}

/// What `_git_read` answers.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum GitRead {
    /// A git directory: its common directory, and whether a `commondir`
    /// named it.
    Found(Vec<u8>, bool),
    /// No git directory (None).
    NotGitDir,
    /// git would die or block reading it (False): `_Unsettled`, an
    /// OSError or a ValueError.
    Dies,
}

/// `_git_read(candidate, ...)`: [`git_found`] with every `_Unsettled`,
/// OSError and ValueError read as git dying there. Only a decline (not
/// ported) is an error.
pub fn git_read<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    candidate: &[u8],
    fetch: bool,
    read_it: bool,
) -> Result<GitRead, Failure> {
    match git_found(ctx, candidate, fetch, read_it) {
        Ok(Some((common, named))) => Ok(GitRead::Found(common, named)),
        Ok(None) => Ok(GitRead::NotGitDir),
        Err(Failure::NotPorted) => Err(Failure::NotPorted),
        Err(_) => Ok(GitRead::Dies),
    }
}

/// `_git_reads(candidate, ...)`: True for a git directory, False for
/// none, None where git would die or block there.
pub fn git_reads<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    candidate: &[u8],
    fetch: bool,
    read_it: bool,
) -> Result<Option<bool>, Failure> {
    Ok(match git_read(ctx, candidate, fetch, read_it)? {
        GitRead::Found(..) => Some(true),
        GitRead::NotGitDir => Some(false),
        GitRead::Dies => None,
    })
}
