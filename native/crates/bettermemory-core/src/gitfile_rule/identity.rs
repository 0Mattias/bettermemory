//! Unit 2, second part: the identity helpers (`gitfile_rule.py`): whether a
//! path names an entry ([`names_here`], 130-144), whether a `.git` path
//! sits in a folder ([`names_folder`], 908-920), whether two paths reach
//! one folder ([`same_folder`], 923-934), whether an `lstat` result leads
//! elsewhere ([`is_link`], 940-948), and the folder whose own `.git` an
//! entry is ([`tree_of`], 969-982) where it is a working tree
//! ([`working_tree_of`], 951-966).
//!
//! None of them lets an error out: each catches what it asks the OS and
//! answers a value, so the only [`Failure`] is [`Failure::NotPorted`]. Every
//! path goes through [`Ctx::at`], as in [`super::gitdir`]. On the Windows
//! flavour [`names_here`], [`names_folder`], [`same_folder`], [`tree_of`]
//! and [`working_tree_of`] are declined whole (they need `samestat`, and
//! [`tree_of`] `realpath`, neither ported there), as is [`is_link`] of an
//! entry that is not a symbolic link (its reparse tag is not ported).

use crate::fsdata::realpath::{realpath, Unresolved};
use crate::fsdata::{samestat, Flavour, Fs, OsError, StatResult};

use super::gitdir::{Ctx, Failure};
use super::hostpath;
use super::probe::{path_missing, Raised};

fn posix<F: Fs + ?Sized>(ctx: &Ctx<'_, F>) -> Result<(), Failure> {
    match ctx.flavour {
        Flavour::Posix => Ok(()),
        Flavour::Windows => Err(Failure::NotPorted),
    }
}

/// `os.stat(path)` with the path taken from the working directory.
fn stat<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, path: &[u8]) -> Result<StatResult, OsError> {
    match ctx.at(path) {
        Ok(path) => ctx.fs.stat(&path),
        // Unreachable on POSIX: `at` fails only on Windows.
        Err(_) => Err(OsError::value()),
    }
}

/// `None if path_missing(exc) else False`.
fn missing_or_false(error: OsError) -> Option<bool> {
    if path_missing(Raised::from(error)) {
        None
    } else {
        Some(false)
    }
}

/// `samestat(theirs, os.stat(other))`, False where `other` cannot be
/// stated.
fn same_as<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    theirs: &StatResult,
    other: &[u8],
) -> Result<Option<bool>, Failure> {
    match stat(ctx, other) {
        Ok(ours) => samestat(theirs, &ours).map(Some).ok_or(Failure::NotPorted),
        Err(_) => Ok(Some(false)),
    }
}

/// `_names_here(raw, base, here)`: True where `raw` (from `base` when
/// relative) is the same file as `here`, False where another or where
/// either cannot be stated, None where nothing is at `raw`
/// (`path_missing`).
pub fn names_here<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    raw: &[u8],
    base: &[u8],
    here: &[u8],
) -> Result<Option<bool>, Failure> {
    posix(ctx)?;
    let path = if hostpath::isabs(ctx.flavour, raw) {
        raw.to_vec()
    } else {
        hostpath::join(ctx.flavour, base, raw)
    };
    match stat(ctx, &path) {
        Ok(theirs) => same_as(ctx, &theirs, here),
        Err(e) => Ok(missing_or_false(e)),
    }
}

/// `_same_folder(a, b)`: True where `a` and `b` reach one file, None where
/// `a` reaches nothing (`path_missing`), False otherwise.
pub fn same_folder<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    a: &[u8],
    b: &[u8],
) -> Result<Option<bool>, Failure> {
    posix(ctx)?;
    match stat(ctx, a) {
        Ok(theirs) => same_as(ctx, &theirs, b),
        Err(e) => Ok(missing_or_false(e)),
    }
}

/// `_names_folder(raw, base, directory)`: None where nothing is at the
/// normalised `raw` (`path_missing` of its `lstat`), True where its folder
/// is `directory` ([`same_folder`] of its `dirname`), False otherwise.
pub fn names_folder<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    raw: &[u8],
    base: &[u8],
    directory: &[u8],
) -> Result<Option<bool>, Failure> {
    posix(ctx)?;
    let joined = if hostpath::isabs(ctx.flavour, raw) {
        raw.to_vec()
    } else {
        hostpath::join(ctx.flavour, base, raw)
    };
    let path = hostpath::normpath(ctx.flavour, &joined);
    let at = ctx.at(&path)?;
    if let Err(e) = ctx.fs.lstat_result(&at) {
        return Ok(missing_or_false(e));
    }
    let folder = hostpath::dirname(ctx.flavour, &path);
    Ok(Some(same_folder(ctx, &folder, directory)? == Some(true)))
}

/// `_is_link(st)`: a symbolic link, or a junction by its reparse tag;
/// declined where the tag is not ported.
pub fn is_link(st: &StatResult) -> Result<bool, Failure> {
    st.is_link().ok_or(Failure::NotPorted)
}

/// `_tree_of(git_dir)`: the folder of `realpath(git_dir)` where that
/// folder's `.git` is the same file as it; None otherwise, or where
/// anything raises.
pub fn tree_of<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    git_dir: &[u8],
) -> Result<Option<Vec<u8>>, Failure> {
    posix(ctx)?;
    let real = match realpath(ctx.fs, ctx.flavour, git_dir, ctx.cwd) {
        Ok(real) => real,
        Err(Unresolved::NotPorted) => return Err(Failure::NotPorted),
        // Only a ValueError (a NUL) is raised here; `git_dir` would then fail its `stat` alike.
        Err(Unresolved::Raised(_)) => return Ok(None),
    };
    let parent = hostpath::dirname(ctx.flavour, &real);
    // `realpath` leaves a link in `real` only where it cannot follow it (a loop, an error), where
    // `lstat` would differ from `stat` but the `.git` beside it can never be that link's inode.
    let Ok(theirs) = stat(ctx, &real) else {
        return Ok(None);
    };
    let dotgit = hostpath::join(ctx.flavour, &parent, b".git");
    let Ok(ours) = stat(ctx, &dotgit) else {
        return Ok(None);
    };
    match samestat(&theirs, &ours) {
        Some(true) => Ok(Some(parent)),
        Some(false) => Ok(None),
        None => Err(Failure::NotPorted),
    }
}

/// `_working_tree_of(path)`: [`tree_of`], where that folder holds a name
/// other than `.git` or cannot be listed; None where it holds `.git`
/// alone.
pub fn working_tree_of<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    path: &[u8],
) -> Result<Option<Vec<u8>>, Failure> {
    let Some(owner) = tree_of(ctx, path)? else {
        return Ok(None);
    };
    if let Ok(names) = ctx.fs.listdir(&ctx.at(&owner)?) {
        if names.iter().all(|name| name.as_slice() == b".git") {
            return Ok(None);
        }
    }
    Ok(Some(owner))
}
