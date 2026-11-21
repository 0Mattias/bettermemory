//! `origin_history.py`, its second unit: the repository a history is read
//! from and the files that change it without moving its head.
//!
//! - [`located_key`]: the root and head `githead.locate` reads;
//! - [`history_guard`]: the stamps of the files that change which commits
//!   a head reaches, and the variables of `_HISTORY_ENV`;
//! - [`cuts`], [`shallow_file`] and [`cuts_in`]: the commits a `shallow`
//!   file lists.
//!
//! Nothing ambient. Discovery reads through the membership rule's
//! [`Rule`] (its file system, flavour and working directory), the owner
//! check through [`Host`], the discovery variables through [`Env`]; HEAD
//! is read through a [`RefFs`]; the four variables of `_HISTORY_ENV` are
//! an argument ([`HistoryEnv`]).

use crate::fsdata::{Flavour, Fs, PyErr};
use crate::gitconfig::legacy::Stamp;
use crate::gitfile_rule::bounded::Answers;
use crate::gitfile_rule::discovery::Rule;
use crate::gitfile_rule::follow::Records;
use crate::gitfile_rule::hostpath;
use crate::gitfile_rule::trust::object_format_at;
use crate::githead::discovery::{
    discovery_moved, gitdir_at, locate, pure, stamp, Env, Found, Host, Owners,
};
use crate::githead::{head_sha, RefFs};
use crate::gitsystem::record::surrogateescape;
use crate::pyvalue::PyStr;

use super::{Cut, Key, CUTS_MAX};

/// `os.environ.get(name)` for each name of `_HISTORY_ENV`, in its order.
pub type HistoryEnv = [Option<PyStr>; 4];

/// What `history_guard` returns: the five stamps (`shallow`,
/// `info/grafts`, `refs/replace`, `packed-refs`, `config`) and the four
/// variables. Two guards are equal as the reference's tuples are.
pub type Guard = ([Option<Stamp>; 5], HistoryEnv);

/// `ValueError` from `open` (a NUL in the path), which `cuts_in` does not
/// catch: the reference raises it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ValueError;

/// `located_key(cwd)`: None under a variable that moves discovery, where
/// `locate(cwd)` finds no repository, or where HEAD names no commit.
pub fn located_key<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    env: &Env,
    refs: &dyn RefFs,
    cwd: &[u8],
) -> Option<Key> {
    if discovery_moved(env) {
        return None;
    }
    let found = locate(rule, env, cwd, true, false)?;
    let head = head_sha(refs, &found.gd)?;
    Some((
        surrogateescape(&found.worktree_root),
        head.chars().map(u32::from).collect(),
    ))
}

/// `githead.gitdir_at(root) or githead.locate(root, walk=walk)`, in
/// Python's order: `locate` is asked only where `gitdir_at` declines.
fn either<F: Fs, A: Answers + ?Sized, R: Records + ?Sized, O: Owners + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    host: &Host<'_, O>,
    env: &Env,
    root: &[u8],
    walk: bool,
) -> Option<Found> {
    gitdir_at(rule, host, env, root).or_else(|| locate(rule, env, root, walk, false))
}

fn join(dir: &[u8], name: &[u8]) -> Vec<u8> {
    hostpath::join(Flavour::Posix, dir, name)
}

/// `history_guard(root)`: None where no repository is read at `root`, or
/// where a stamp raises (any OSError but absence, a ValueError, or a stat
/// the flavour does not port).
pub fn history_guard<F: Fs, A: Answers + ?Sized, R: Records + ?Sized, O: Owners + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    host: &Host<'_, O>,
    env: &Env,
    history: &HistoryEnv,
    root: &[u8],
) -> Option<Guard> {
    let found = either(rule, host, env, root, false)?;
    let common = &found.gd.commondir;
    let paths = [
        join(common, b"shallow"),
        join(&join(common, b"info"), b"grafts"),
        join(&join(common, b"refs"), b"replace"),
        join(common, b"packed-refs"),
        join(common, b"config"),
    ];
    let mut stamps: [Option<Stamp>; 5] = [None; 5];
    for (slot, path) in stamps.iter_mut().zip(paths.iter()) {
        *slot = stamp(rule.ctx, path, true).ok()?;
    }
    Some((stamps, history.clone()))
}

/// `shallow_file(start, walk=walk)`: `<commondir>/shallow` of the
/// repository found as [`history_guard`] finds it.
pub fn shallow_file<F: Fs, A: Answers + ?Sized, R: Records + ?Sized, O: Owners + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    host: &Host<'_, O>,
    env: &Env,
    start: &[u8],
    walk: bool,
) -> Option<Vec<u8>> {
    either(rule, host, env, start, walk).map(|f| join(&f.gd.commondir, b"shallow"))
}

/// `cuts(start, walk=walk)`: [`cuts_in`] of [`shallow_file`].
pub fn cuts<F: Fs, A: Answers + ?Sized, R: Records + ?Sized, O: Owners + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    host: &Host<'_, O>,
    env: &Env,
    start: &[u8],
    walk: bool,
) -> Result<Option<Cut>, ValueError> {
    match shallow_file(rule, host, env, start, walk) {
        None => Ok(None),
        Some(path) => cuts_in(rule, &path),
    }
}

/// `Path(path).parent`, as a string: the parts but the last, `.` where
/// none is left, the root itself for a root.
fn parent(path: &[u8]) -> Vec<u8> {
    let whole = pure(path);
    match whole.iter().rposition(|&c| c == b'/') {
        None => b".".to_vec(),
        Some(at) => {
            let head = &whole[..at];
            if head.is_empty() || head == b"/" {
                whole[..=at].to_vec()
            } else {
                head.to_vec()
            }
        }
    }
}

/// `cuts_in(path)`: the lowercase object names the `shallow` file at
/// `path` lists. Empty where open raises FileNotFoundError or the file is
/// empty; None where another OSError rises, the file is longer than
/// `_CUTS_MAX` bytes, the common config's object format is not known, or
/// a record's first `width` bytes are short or not hex. The open waits
/// as Python's `open` does (a FIFO blocks). Under the Windows flavour it
/// declines for every path but one holding a NUL, which raises.
pub fn cuts_in<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    path: &[u8],
) -> Result<Option<Cut>, ValueError> {
    let ctx = rule.ctx;
    if ctx.flavour == Flavour::Windows {
        // `Path` and `open` on Windows read drives and backslashes, which
        // neither `pure` nor `parent` ports: a NUL still raises there, and
        // every other path declines (the crate's stated Windows gap).
        if path.contains(&0) {
            return Err(ValueError);
        }
        return Ok(None);
    }
    // `open(Path(...))` opens the path as `Path` spells it: no trailing
    // slash, no empty or `.` part. Under the POSIX flavour `at` cannot
    // fail; a failure would be a flavour not ported, so it declines.
    let Ok(at) = ctx.at(&pure(path)) else {
        return Ok(None);
    };
    let raw = match ctx.fs.open_waiting(&at) {
        Err(PyErr::FileNotFound) => return Ok(Some(Cut::new())),
        Err(PyErr::Value) => return Err(ValueError),
        Err(_) => return Ok(None),
        Ok(mut file) => match ctx.fs.read(&mut file, CUTS_MAX + 1) {
            Ok(raw) => raw,
            Err(_) => return Ok(None),
        },
    };
    if raw.len() > CUTS_MAX {
        return Ok(None);
    }
    if raw.is_empty() {
        return Ok(Some(Cut::new()));
    }
    let Ok(Some(name)) = object_format_at(ctx, &parent(path), rule.git) else {
        return Ok(None);
    };
    let width = if name == b"sha256" { 64 } else { 40 };
    let mut records: Vec<&[u8]> = raw.split(|&c| c == b'\n').collect();
    if records.last() == Some(&&b""[..]) {
        records.pop();
    }
    let mut out = Cut::new();
    for record in records {
        let oid = record[..width.min(record.len())].to_ascii_lowercase();
        if oid.len() != width || !oid.iter().all(|c| b"0123456789abcdef".contains(c)) {
            return Ok(None);
        }
        out.insert(oid.into_iter().map(u32::from).collect());
    }
    Ok(Some(out))
}
