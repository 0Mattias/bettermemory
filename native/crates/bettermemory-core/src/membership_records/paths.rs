//! The path helpers of `membership_records.py` (1083-1195 at 3a26051a):
//! recorded paths compared, joined and rebased in either spelling.
//!
//! A recorded path crosses machines, so its spelling picks the path
//! module ([`flavour`]) and the host never does. Three functions read
//! the host all the same, and take it as an argument:
//!
//! - [`rebase`] calls `relpath`, which makes both paths absolute: a
//!   relative one is joined to the working directory, and `ntpath` on
//!   Windows asks `nt._getfullpathname` first (`hostpath::text::Process`);
//! - [`common_by_pointer`] splits with the host's `os.path`;
//! - [`dir_key`] and [`dir_below`] fold with `_DIR_PATHS`, the host's
//!   `os.path` unless a caller swaps it.

use crate::capture::paths as recorded;
use crate::fsdata::Flavour;
use crate::gitfile_rule::hostpath::text::{self, Process, Raised};
use crate::pyvalue::PyStr;

const BACKSLASH: u32 = b'\\' as u32;

fn is(text: &[u32], word: &str) -> bool {
    text.iter().copied().eq(word.chars().map(u32::from))
}

/// `_same_path`: equal after `_paths.norm`.
pub fn same_path(a: &[u32], b: &[u32]) -> bool {
    recorded::norm(a) == recorded::norm(b)
}

/// `_flavour`: `ntpath` where the path holds a backslash or starts with
/// a drive (`_DRIVE_PATH`, `^[A-Za-z]:`, matched by hand: one ASCII
/// letter and a colon at the start), `posixpath` otherwise.
pub fn flavour(path: &[u32]) -> Flavour {
    let letter = |c: u32| matches!(c, 0x41..=0x5A | 0x61..=0x7A);
    let drive = path.len() >= 2 && letter(path[0]) && path[1] == u32::from(b':');
    if drive || path.contains(&BACKSLASH) {
        Flavour::Windows
    } else {
        Flavour::Posix
    }
}

/// `_path_under(base, *parts)`: `parts` under `base`, joined in
/// `base`'s spelling.
pub fn path_under(base: &[u32], parts: &[&[u32]]) -> PyStr {
    text::join(flavour(base), base, parts)
}

/// `_rebase(path, root, target)`: the part of `path` below `root`, read
/// in `root`'s spelling, written in `target`'s under `target`, and the
/// result normalised. It raises what `relpath` raises: ValueError for an
/// empty `path` or (under `ntpath`) two drives, and the OSError of a
/// working directory that cannot be read when a relative path needs it.
pub fn rebase(
    path: &[u32],
    root: &[u32],
    target: &[u32],
    process: &dyn Process,
) -> Result<PyStr, Raised> {
    let (source, dest) = (flavour(root), flavour(target));
    let mut relative = text::relpath(source, path, root, process)?;
    if source != dest {
        let (from, to) = (text::sep(source), text::sep(dest));
        for c in relative.iter_mut().filter(|c| **c == from) {
            *c = to;
        }
    }
    Ok(text::normpath(
        dest,
        &text::join(dest, target, &[&relative]),
    ))
}

/// `_basename`: `_paths.basename`.
pub fn basename(path: &[u32]) -> PyStr {
    recorded::basename(path)
}

/// `_common_by_pointer(kind, git_dir)`: the common directory a git
/// directory names by where it sits. `os_path` is the host's `os.path`,
/// which splits a linked worktree's path; `kind` is compared as text.
pub fn common_by_pointer(
    os_path: Flavour,
    kind: Option<&[u32]>,
    git_dir: Option<&[u32]>,
) -> Option<PyStr> {
    let git_dir = git_dir.filter(|g| !g.is_empty())?;
    let kind = kind?;
    if is(kind, "linked") {
        let admin = text::dirname(os_path, &recorded::norm(git_dir));
        if !is(&text::basename(os_path, &admin), "worktrees") {
            return None;
        }
        return Some(text::dirname(os_path, &admin));
    }
    if is(kind, "primary") || is(kind, "bare") {
        return Some(recorded::norm(git_dir));
    }
    None
}

/// `_dir_key`: a directory normalised and case folded as `dir_paths`
/// (`_DIR_PATHS`) does.
pub fn dir_key(dir_paths: Flavour, path: &[u32]) -> PyStr {
    text::normcase(dir_paths, &text::normpath(dir_paths, path))
}

/// `_dir_below`: whether the key `here` starts with the key `root`, its
/// trailing separators trimmed, and one separator of `dir_paths`.
pub fn dir_below(dir_paths: Flavour, here: &[u32], root: &[u32]) -> bool {
    let sep = text::sep(dir_paths);
    let end = root.iter().rposition(|&c| c != sep).map_or(0, |i| i + 1);
    here.len() > end && here[..end] == root[..end] && here[end] == sep
}
