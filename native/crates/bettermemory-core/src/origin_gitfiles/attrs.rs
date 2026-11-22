//! Unit 6 of `origin_gitfiles`: the attribute file signatures and the
//! index's `.gitmodules` entry.
//!
//! - [`attribute_files_signature`] (reference lines 1031-1127): the
//!   repository's config, `config.worktree`, `info/attributes` and the
//!   global attributes file, each by its stamp;
//! - [`gitattributes_signature`] (1129-1192): the working tree's
//!   `.gitattributes` chain of each pathspec;
//! - [`IndexMemo`] (`_INDEX_MEMO`, `_INDEX_MEMO_CAP`, 1195-1237) and
//!   [`index_gitmodules`] (`_index_gitmodules`, 1346-1414);
//! - [`gitdir_at_root`] (`_gitdir_at_root`, 1417-1427).
//!
//! The environment is arguments (XDG_CONFIG_HOME and HOME, the discovery
//! [`Env`]), the file system is the caller's ([`Fs`]), and both memos are
//! values the caller holds.

use std::collections::HashSet;

use super::facts::{global_attributes_path, hold_directory, Held, HoldFault, NotPorted};
use super::machine::read_prefix;
use super::root::{config_facts, fd_stamp, worktree_config_facts, ConfigMemo};
use super::{gitmodules_entries, IndexFault};
use crate::fsdata::{Flavour, Fs, PyErr, Refused};
use crate::gitconfig::legacy::Stamp;
use crate::gitfile_rule::bounded::Answers;
use crate::gitfile_rule::discovery::Rule;
use crate::gitfile_rule::follow::Records;
use crate::gitfile_rule::format::GitFacts;
use crate::gitfile_rule::gitdir::{Ctx, Failure};
use crate::gitfile_rule::hostpath::{dirname, join};
use crate::gitfile_rule::probe::{path_missing, Raised};
use crate::githead::discovery::{
    discovery_moved, find_gitdir, gitdir_at, locate, pure, stamp, Env, Found, Host, Owners,
};
use crate::githead::GitDir;

/// What [`attribute_files_signature`] keys on: the stamps of the common
/// `config`, of `config.worktree` where read, of `info/attributes`, and
/// the global attributes file with its stamp. A stamp is None while its
/// file does not exist.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AttributeFiles {
    pub config: Option<Stamp>,
    pub worktree_config: Option<Stamp>,
    pub info_attributes: Option<Stamp>,
    pub global_file: Option<Vec<u8>>,
    pub global_attributes: Option<Stamp>,
}

/// A stamp read where the reference catches OSError and ValueError: the
/// outer `Err` is a decline (not ported), the inner one the reference's
/// None.
fn caught<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    path: &[u8],
    follow: bool,
) -> Result<Result<Option<Stamp>, ()>, NotPorted> {
    match stamp(ctx, path, follow) {
        Ok(found) => Ok(Ok(found)),
        Err(Failure::Os(_) | Failure::Value) => Ok(Err(())),
        Err(_) => Err(NotPorted),
    }
}

/// `_hold_directory` where the reference catches OSError and ValueError.
fn held<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    path: &[u8],
    directories: &mut Held,
) -> Result<Result<(), ()>, NotPorted> {
    match hold_directory(ctx.fs, ctx.flavour, path, directories) {
        Ok(()) => Ok(Ok(())),
        Err(HoldFault::Raised(_)) => Ok(Err(())),
        Err(HoldFault::NotPorted) => Err(NotPorted),
    }
}

/// `attribute_files_signature(cwd, root, directories=...)`.
/// `xdg_config_home` and `home` are the environment's XDG_CONFIG_HOME and
/// HOME (None where unset), `env` the discovery variables, `host` the
/// effective user and the owners `githead.find_gitdir` checks. `Ok(None)`
/// where the reference answers None; `Err(NotPorted)` where the file
/// system gives no full stamp, a decline. `directories` may hold some
/// stamps on a None answer, as the reference's dict does.
#[allow(clippy::too_many_arguments)]
pub fn attribute_files_signature<
    F: Fs,
    A: Answers + ?Sized,
    R: Records + ?Sized,
    O: Owners + ?Sized,
>(
    memo: &mut ConfigMemo,
    rule: &Rule<'_, F, A, R>,
    host: &Host<'_, O>,
    env: &Env,
    xdg_config_home: Option<&[u8]>,
    home: Option<&[u8]>,
    cwd: &[u8],
    root: &[u8],
    directories: Option<&mut Held>,
) -> Result<Option<AttributeFiles>, NotPorted> {
    let ctx = rule.ctx;
    let mut gd = find_gitdir(rule, host, env, cwd);
    if gd.is_none() && !discovery_moved(env) {
        gd = locate(rule, env, root, false, false);
        if xdg_config_home.is_none_or(<[u8]>::is_empty) && home.is_none() {
            return Ok(None);
        }
    }
    let Some(Found { gd, .. }) = gd else {
        return Ok(None);
    };
    let config_path = join(ctx.flavour, &gd.commondir, b"config");
    let Some((config_stamp, facts)) = config_facts(memo, ctx, &config_path, rule.git)? else {
        return Ok(None);
    };
    if facts.names.is_none() {
        return Ok(None);
    }
    let Some((worktree_stamp, worktree_facts)) =
        worktree_config_facts(memo, ctx, &gd, &facts, rule.git)?
    else {
        return Ok(None);
    };
    for each in [&facts, &worktree_facts] {
        let follows = each
            .names
            .as_ref()
            .is_none_or(|names| names.contains(&b"log.follow"[..]));
        if follows || each.attributes_elsewhere || each.includes {
            return Ok(None);
        }
    }
    let global_file = global_attributes_path(
        ctx.flavour,
        xdg_config_home,
        home,
        &pure_root(ctx.flavour, root),
    );
    let info = join(ctx.flavour, &gd.commondir, b"info");
    if let Some(directories) = directories {
        if held(ctx, &info, directories)?.is_err() {
            return Ok(None);
        }
        if let Some(global) = &global_file {
            if held(ctx, &dirname(ctx.flavour, global), directories)?.is_err() {
                return Ok(None);
            }
        }
    }
    let Ok(info_attributes) = caught(ctx, &join(ctx.flavour, &info, b"attributes"), true)? else {
        return Ok(None);
    };
    let global_attributes = match &global_file {
        None => None,
        Some(global) => match caught(ctx, global, true)? {
            Ok(found) => found,
            Err(()) => return Ok(None),
        },
    };
    Ok(Some(AttributeFiles {
        config: config_stamp,
        worktree_config: worktree_stamp,
        info_attributes,
        global_file,
        global_attributes,
    }))
}

/// `os.fspath(Path(root))` on POSIX; the Windows flavour's `Path` is not
/// spelled here, so `root` is taken as given there.
fn pure_root(flavour: Flavour, root: &[u8]) -> Vec<u8> {
    match flavour {
        Flavour::Posix => pure(root),
        Flavour::Windows => root.to_vec(),
    }
}

/// `_PATHSPEC_WILDCARDS`, with the leading colon of pathspec magic.
const PATHSPEC_WILDCARDS: &[u8] = b"*?[\\";

/// `Path(directory) / part`: `.` gives the part alone.
fn child(directory: &[u8], part: &[u8]) -> Vec<u8> {
    if directory == b"." {
        return part.to_vec();
    }
    join(Flavour::Posix, directory, part)
}

/// `gitattributes_signature(root, pathspecs, directories=...)`: the
/// stamps (links not followed) of `.gitattributes` in each directory of
/// the pathspecs' chains, in the order first named. `Ok(None)` where the
/// reference answers None. The Windows flavour declines whole: its `Path`
/// spelling is not ported here.
pub fn gitattributes_signature<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    root: &[u8],
    pathspecs: &[&[u8]],
    directories: Option<&mut Held>,
) -> Result<Option<Vec<Option<Stamp>>>, NotPorted> {
    if ctx.flavour == Flavour::Windows {
        return Err(NotPorted);
    }
    let root = pure(root);
    let mut chain: Vec<Vec<u8>> = Vec::new();
    let mut seen: HashSet<Vec<u8>> = HashSet::new();
    for spec in pathspecs {
        let parts: Vec<&[u8]> = spec.split(|&b| b == b'/').collect();
        if spec.starts_with(b":")
            || spec.iter().any(|b| PATHSPEC_WILDCARDS.contains(b))
            || parts.iter().any(|p| matches!(*p, b"" | b"." | b".."))
        {
            return Ok(None);
        }
        let mut whole = root.clone();
        for part in &parts {
            whole = child(&whole, part);
        }
        let at = match ctx.at(&whole) {
            Ok(at) => at,
            // The reference's `except (OSError, ValueError)`.
            Err(Failure::Os(_) | Failure::Value) => return Ok(None),
            Err(_) => return Err(NotPorted),
        };
        match ctx.fs.stat(&at) {
            Ok(st) if st.kind == crate::fsdata::Kind::Directory => return Ok(None),
            Ok(_) => {}
            Err(e)
                if matches!(e.class, PyErr::FileNotFound | PyErr::NotADirectory)
                    && path_missing(Raised::from(e)) => {}
            Err(_) => return Ok(None),
        }
        let mut directory = root.clone();
        if seen.insert(directory.clone()) {
            chain.push(directory.clone());
        }
        for part in &parts[..parts.len() - 1] {
            directory = child(&directory, part);
            if seen.insert(directory.clone()) {
                chain.push(directory.clone());
            }
        }
    }
    if let Some(directories) = directories {
        for directory in &chain {
            if held(ctx, directory, directories)?.is_err() {
                return Ok(None);
            }
        }
    }
    let mut out = Vec::with_capacity(chain.len());
    for directory in &chain {
        match caught(ctx, &child(directory, b".gitattributes"), false)? {
            Ok(found) => out.push(found),
            Err(()) => return Ok(None),
        }
    }
    Ok(Some(out))
}

/// `_INDEX_MEMO_CAP`.
pub const INDEX_MEMO_CAP: usize = 64;
/// `_INDEX_FIRST_READ`.
pub const INDEX_FIRST_READ: u64 = 1 << 16;
/// `_INDEX_READ_GROWTH`.
pub const INDEX_READ_GROWTH: u64 = 4;

/// What the index holds for `.gitmodules` where it holds an entry.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum IndexGitmodules {
    /// `_INDEX_UNMERGED`: only stages 1 to 3.
    Unmerged,
    /// The stage-0 entry's mode and object id in hex.
    Entry(u32, String),
}

/// Why [`index_gitmodules`] gives no answer.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum IndexFailure {
    /// `_IndexUnsettled`.
    Unsettled,
    /// The exception the reference lets out (OSError by class, or
    /// ValueError for a NUL in the path).
    Raised(PyErr),
    /// The file system gives no full stamp or no rewind: a caller
    /// declines.
    NotPorted,
}

impl From<NotPorted> for IndexFailure {
    fn from(_: NotPorted) -> IndexFailure {
        IndexFailure::NotPorted
    }
}

type Kept = Option<IndexGitmodules>;

/// `_INDEX_MEMO`: the entry each index held, keyed on the index's path,
/// oldest first, at most [`INDEX_MEMO_CAP`].
#[derive(Clone, Debug, Default)]
pub struct IndexMemo {
    entries: Vec<(Vec<u8>, Stamp, Kept)>,
}

impl IndexMemo {
    pub fn new() -> IndexMemo {
        IndexMemo::default()
    }

    /// `_clear_index_memo`.
    pub fn clear(&mut self) {
        self.entries.clear();
    }

    /// The keys held, oldest first.
    pub fn keys(&self) -> Vec<Vec<u8>> {
        self.entries.iter().map(|e| e.0.clone()).collect()
    }

    fn hit(&mut self, key: &[u8], current: &Stamp) -> Option<Kept> {
        let at = self.entries.iter().position(|e| e.0 == key)?;
        if self.entries[at].1 != *current {
            return None;
        }
        let entry = self.entries.remove(at);
        let kept = entry.2.clone();
        self.entries.push(entry);
        Some(kept)
    }

    fn keep(&mut self, key: Vec<u8>, stamp: Stamp, kept: Kept) {
        self.entries.retain(|e| e.0 != key);
        self.entries.push((key, stamp, kept));
        while self.entries.len() > INDEX_MEMO_CAP {
            self.entries.remove(0);
        }
    }
}

const S_IFMT: i128 = 0o170_000;
const S_IFREG: i128 = 0o100_000;

/// `_index_gitmodules(gd)`: `Ok(None)` where no index or no entry. The
/// reference closes the file in a `finally` whose OSError would replace
/// the answer; the port reads a failed close as nothing.
pub fn index_gitmodules<F: Fs + ?Sized>(
    memo: &mut IndexMemo,
    config: &mut ConfigMemo,
    ctx: &Ctx<'_, F>,
    gd: &GitDir,
    git: &GitFacts,
) -> Result<Option<IndexGitmodules>, IndexFailure> {
    let config_path = join(ctx.flavour, &gd.commondir, b"config");
    match config_facts(config, ctx, &config_path, git)? {
        Some((_, facts))
            if facts
                .names
                .as_ref()
                .is_some_and(|n| !n.contains(&b"extensions.objectformat"[..])) => {}
        _ => return Err(IndexFailure::Unsettled),
    }
    let key = join(ctx.flavour, &gd.gitdir, b"index");
    let at = match ctx.at(&key) {
        Ok(at) => at,
        Err(Failure::Value) => return Err(IndexFailure::Raised(PyErr::Value)),
        Err(_) => return Err(IndexFailure::NotPorted),
    };
    let mut file = match ctx.fs.open_result(&at) {
        Ok(file) => file,
        Err(e) if e.class == PyErr::FileNotFound && path_missing(Raised::from(e)) => {
            return Ok(None)
        }
        Err(e) => return Err(IndexFailure::Raised(e.class)),
    };
    let read = read_open(memo, ctx, gd, &key, &mut file);
    let _ = ctx.fs.close(file);
    let (before, entries) = match read? {
        Ok(hit) => return Ok(hit),
        Err(read) => read,
    };
    let mut entry: Kept = None;
    for (stage, mode, oid) in &entries {
        if *stage == 0 {
            entry = Some(IndexGitmodules::Entry(*mode, hex(oid)));
        }
    }
    if entry.is_none() && !entries.is_empty() {
        entry = Some(IndexGitmodules::Unmerged);
    }
    memo.keep(key, before, entry.clone());
    Ok(entry)
}

fn hex(data: &[u8]) -> String {
    data.iter().map(|b| format!("{b:02x}")).collect()
}

fn full_stamp<F: Fs + ?Sized>(fs: &F, file: &mut F::File) -> Result<Stamp, IndexFailure> {
    match fs.fstat_full(file).ok_or(IndexFailure::NotPorted)? {
        Ok(st) => Ok(fd_stamp(&st)?),
        Err(e) => Err(IndexFailure::Raised(e.class)),
    }
}

/// The open file's part of `_index_gitmodules`: `Ok(hit)` from the memo,
/// else `Err` with the stamp and the entries read.
#[allow(clippy::type_complexity)]
fn read_open<F: Fs + ?Sized>(
    memo: &mut IndexMemo,
    ctx: &Ctx<'_, F>,
    gd: &GitDir,
    key: &[u8],
    file: &mut F::File,
) -> Result<Result<Kept, (Stamp, Vec<super::GitmodulesEntry>)>, IndexFailure> {
    let before = full_stamp(ctx.fs, file)?;
    if before[4] & S_IFMT != S_IFREG {
        return Err(IndexFailure::Unsettled);
    }
    if let Some(kept) = memo.hit(key, &before) {
        return Ok(Ok(kept));
    }
    let names = ctx
        .fs
        .listdir(&gd.gitdir)
        .map_err(|e| IndexFailure::Raised(e.class))?;
    if names.iter().any(|n| n.starts_with(b"sharedindex.")) {
        return Err(IndexFailure::Unsettled);
    }
    let size = u64::try_from(before[2]).map_err(|_| IndexFailure::NotPorted)?;
    let mut want = INDEX_FIRST_READ;
    let entries = loop {
        let length = usize::try_from(want.min(size)).map_err(|_| IndexFailure::NotPorted)?;
        let data = match read_prefix(ctx.fs, file, length) {
            Ok(data) => data,
            Err(Refused::Raised(class)) => return Err(IndexFailure::Raised(class)),
            Err(Refused::NotOffered) => return Err(IndexFailure::NotPorted),
        };
        match gitmodules_entries(&data, data.len() as u64 >= size) {
            Ok(entries) => break entries,
            Err(IndexFault::Short) if want < size => want = want.saturating_mul(INDEX_READ_GROWTH),
            Err(_) => return Err(IndexFailure::Unsettled),
        }
    };
    if full_stamp(ctx.fs, file)? != before {
        return Err(IndexFailure::Unsettled);
    }
    Ok(Err((before, entries)))
}

/// `_gitdir_at_root(root)`: `githead.gitdir_at`'s repository and, where it
/// declines, `githead.locate`'s without its walk; None under a variable
/// that moves git's discovery.
pub fn gitdir_at_root<F: Fs, A: Answers + ?Sized, R: Records + ?Sized, O: Owners + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    host: &Host<'_, O>,
    env: &Env,
    root: &[u8],
) -> Option<Found> {
    let found = gitdir_at(rule, host, env, root);
    if found.is_none() && !discovery_moved(env) {
        return locate(rule, env, root, false, false);
    }
    found
}
