//! The disk-reading half of the sparse-checkout probe
//! (`verify_anchoring.py` 609-651 and 790-890): [`sparse_enabled`],
//! [`parts_below`], [`skip_index`] with its [`SkipMemo`], and
//! [`absent_by_sparsity`].
//!
//! Nothing ambient: the file system, its flavour and the working
//! directory come in the membership rule's [`Rule`], the environment
//! variables discovery reads in an [`Env`], and `os.path.expanduser` is a
//! closure. The memo is the caller's, with no lock and no registration
//! with `_caches`.
//!
//! The Windows flavour declines ([`Failure::NotPorted`]) wherever the
//! reference would resolve a path or stamp a file there: `realpath` and
//! the stamp's inode are not ported for it.

use std::sync::Arc;

use crate::fsdata::realpath::{realpath, Unresolved};
use crate::fsdata::{Flavour, Fs, Kind, OsError};
use crate::gitconfig::legacy::Stamp;
use crate::gitfile_rule::bounded::Answers;
use crate::gitfile_rule::discovery::Rule;
use crate::gitfile_rule::follow::Records;
use crate::gitfile_rule::format::object_format;
use crate::gitfile_rule::gitdir::{Ctx, Failure};
use crate::githead::discovery::{locate, parts, pure, stamp, Env, Found};
use crate::githead::GitDir;
use crate::gitsystem::answers::text;

use super::sparse::{config_switch, index_hash_len, parse_skip_index, SkipIndex};

/// `_SKIP_MEMO_CAP`: the memo holds the indexes of this many paths.
pub const SKIP_MEMO_CAP: usize = 4;

/// The stamps `_skip_index` compares: the index's, then the common
/// directory's `config`.
pub type Stamps = [Option<Stamp>; 2];

/// `_SKIP_MEMO`: index path to (stamps, what was read), least recently
/// used first, as `OrderedDict.move_to_end` keeps it.
#[derive(Clone, Debug, Default)]
pub struct SkipMemo {
    held: Vec<(Vec<u8>, Stamps, Option<Arc<SkipIndex>>)>,
}

impl SkipMemo {
    pub fn new() -> SkipMemo {
        SkipMemo::default()
    }

    /// `_clear_skip_memo()`.
    pub fn clear(&mut self) {
        self.held.clear();
    }

    pub fn len(&self) -> usize {
        self.held.len()
    }

    pub fn is_empty(&self) -> bool {
        self.held.is_empty()
    }

    /// The keys, least recently used first.
    pub fn keys(&self) -> Vec<&[u8]> {
        self.held.iter().map(|(k, _, _)| k.as_slice()).collect()
    }
}

/// `Path(a) / b` of two bytes paths, as `pathlib` spells the join.
fn joined(dir: &[u8], name: &[u8]) -> Vec<u8> {
    let mut out = dir.to_vec();
    out.push(b'/');
    out.extend_from_slice(name);
    pure(&out)
}

/// `Path.read_bytes()`: the whole file.
fn read_bytes<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, path: &[u8]) -> Result<Vec<u8>, Failure> {
    let at = ctx.at(path)?;
    let mut file = ctx.fs.open_result(&at)?;
    let mut out = Vec::new();
    loop {
        let chunk = ctx
            .fs
            .read_once(&mut file, 1 << 16)
            .map_err(Failure::from)?;
        if chunk.is_empty() {
            return Ok(out);
        }
        out.extend_from_slice(&chunk);
    }
}

/// `Path.read_text(encoding="utf-8", errors="replace")`: decoded, then
/// universal newlines (`\r\n` and a lone `\r` read as `\n`).
fn read_text<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, path: &[u8]) -> Result<Vec<u32>, Failure> {
    let decoded = text(&read_bytes(ctx, path)?);
    let mut out = Vec::with_capacity(decoded.len());
    let mut i = 0;
    while i < decoded.len() {
        if decoded[i] == 0x0D {
            out.push(0x0A);
            if decoded.get(i + 1) == Some(&0x0A) {
                i += 1;
            }
        } else {
            out.push(decoded[i]);
        }
        i += 1;
    }
    Ok(out)
}

/// `_sparse_enabled(found)`: `core.sparseCheckout` in the common
/// directory's `config`, then in the git directory's `config.worktree`,
/// the last file that sets it winning; a file not read is passed over.
pub fn sparse_enabled<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, found: &GitDir) -> bool {
    let section: Vec<u32> = "core".chars().map(u32::from).collect();
    let key: Vec<u32> = "sparsecheckout".chars().map(u32::from).collect();
    let mut enabled = false;
    for config in [
        joined(&found.commondir, b"config"),
        joined(&found.gitdir, b"config.worktree"),
    ] {
        let Ok(text) = read_text(ctx, &config) else {
            continue;
        };
        // The text is decoded with `replace` and the name is ASCII, so
        // the encode never fails.
        if let Ok(Some(value)) = config_switch(&text, &section, &key) {
            enabled = value;
        }
    }
    enabled
}

fn resolved<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, path: &[u8]) -> Result<Vec<u8>, Failure> {
    match realpath(ctx.fs, ctx.flavour, path, ctx.cwd) {
        Ok(found) => Ok(found),
        Err(Unresolved::NotPorted) => Err(Failure::NotPorted),
        Err(Unresolved::Raised(class)) => Err(Failure::from(OsError::of_class(class))),
    }
}

/// `_parts_below(path, root)`: the components of `realpath(path)` below
/// `realpath(root)`, None where it is neither that folder nor below it.
/// Names compare through `normcase`, which is the name itself under
/// POSIX; the Windows flavour declines at `realpath`.
pub fn parts_below<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    path: &[u8],
    root: &[u8],
) -> Result<Option<Vec<Vec<u8>>>, Failure> {
    let inner_path = resolved(ctx, path)?;
    let outer_path = resolved(ctx, root)?;
    let inner = parts(&inner_path);
    let outer = parts(&outer_path);
    if inner.len() < outer.len() || inner[..outer.len()] != outer[..] {
        return Ok(None);
    }
    Ok(Some(
        inner[outer.len()..].iter().map(|p| p.to_vec()).collect(),
    ))
}

/// `githead.stamp(path)` as `_skip_index` takes it: a stamp that cannot
/// be taken ends the read (None), but the Windows flavour declines.
fn stamped<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, path: &[u8]) -> Result<Option<Stamp>, Failure> {
    stamp(ctx, path, true)
}

/// `_skip_index(found)`: the [`SkipIndex`] of the git directory's
/// `index`, read whole and memoised in `memo` on the stamps of the index
/// and the common directory's `config`; None where there is no index,
/// where a read fails, and where the index or its object format is not
/// read exactly. A stamp that moved during the read answers what was read
/// and leaves the memo alone. Err only for the Windows flavour.
pub fn skip_index<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    found: &GitDir,
    memo: &mut SkipMemo,
) -> Result<Option<Arc<SkipIndex>>, Failure> {
    let path = joined(&found.gitdir, b"index");
    let config = joined(&found.commondir, b"config");
    skip_index_at(rule, &path, &config, memo)
}

fn skip_index_at<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    path: &[u8],
    config: &[u8],
    memo: &mut SkipMemo,
) -> Result<Option<Arc<SkipIndex>>, Failure> {
    let ctx = rule.ctx;
    let both = || -> Result<Stamps, Failure> { Ok([stamped(ctx, path)?, stamped(ctx, config)?]) };
    let declined = |e: Failure| match e {
        Failure::NotPorted => Err(Failure::NotPorted),
        _ => Ok(None),
    };
    let current = match both() {
        Ok(c) => c,
        Err(e) => return declined(e),
    };
    if current[0].is_none() {
        return Ok(None);
    }
    if let Some(at) = memo
        .held
        .iter()
        .position(|(k, s, _)| k == path && *s == current)
    {
        let entry = memo.held.remove(at);
        let answer = entry.2.clone();
        memo.held.push(entry);
        return Ok(answer);
    }
    let read = || -> Result<(Vec<u8>, Vec<u8>, bool), Failure> {
        let text = if current[1].is_some() {
            read_bytes(ctx, config)?
        } else {
            Vec::new()
        };
        let data = read_bytes(ctx, path)?;
        Ok((text, data, both()? == current))
    };
    let (text, data, settled) = match read() {
        Ok(got) => got,
        Err(e) => return declined(e),
    };
    let hash_len = object_format(&text, rule.git).and_then(|name| index_hash_len(&name));
    let index = hash_len
        .and_then(|h| parse_skip_index(&data, h).ok())
        .map(Arc::new);
    if !settled {
        return Ok(index);
    }
    if let Some(at) = memo.held.iter().position(|(k, _, _)| k == path) {
        memo.held.remove(at);
    }
    memo.held.push((path.to_vec(), current, index.clone()));
    while memo.held.len() > SKIP_MEMO_CAP {
        memo.held.remove(0);
    }
    Ok(index)
}

/// `os.path.isdir(path)`: a `stat` that answers a folder; any failure is
/// False.
fn isdir<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, path: &[u8]) -> bool {
    if path.is_empty() || path.contains(&0) {
        return false;
    }
    match ctx.at(path) {
        Ok(at) => ctx.fs.stat(&at).is_ok_and(|st| st.kind == Kind::Directory),
        Err(_) => false,
    }
}

/// `posixpath.split(path)`.
fn split(path: &[u8]) -> (Vec<u8>, Vec<u8>) {
    let i = path.iter().rposition(|&b| b == b'/').map_or(0, |i| i + 1);
    let (head, tail) = path.split_at(i);
    let head = if !head.is_empty() && head.iter().any(|&b| b != b'/') {
        let end = head.iter().rposition(|&b| b != b'/').map_or(0, |i| i + 1);
        &head[..end]
    } else {
        head
    };
    (head.to_vec(), tail.to_vec())
}

/// `_absent_by_sparsity(path)`: whether `path`, absent from the disk, is
/// one the sparse checkout of its working tree leaves out: the nearest
/// existing folder at or above it is found, its repository located
/// (`locate`, walking), the switch read, and the path's components below
/// the working tree asked of the index; an index not read exactly answers
/// True. The Windows flavour declines.
pub fn absent_by_sparsity<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    env: &Env,
    expanduser: &dyn Fn(&[u8]) -> Vec<u8>,
    memo: &mut SkipMemo,
    path: &[u8],
) -> Result<bool, Failure> {
    let ctx = rule.ctx;
    if ctx.flavour == Flavour::Windows {
        return Err(Failure::NotPorted);
    }
    let expanded = expanduser(path);
    let mut tail: Vec<Vec<u8>> = Vec::new();
    let mut start = expanded;
    while !isdir(ctx, &start) {
        let (parent, name) = split(&start);
        if name.is_empty() || parent == start {
            return Ok(false);
        }
        tail.push(name);
        start = parent;
    }
    let Some(Found { gd, worktree_root }) = locate(rule, env, &start, true, false) else {
        return Ok(false);
    };
    if !sparse_enabled(ctx, &gd) {
        return Ok(false);
    }
    let Some(base) = parts_below(ctx, &start, &worktree_root)? else {
        return Ok(false);
    };
    let mut all = base;
    all.extend(tail.into_iter().rev());
    if all.is_empty() {
        return Ok(false);
    }
    let index = skip_index(rule, &gd, memo)?;
    Ok(index.is_none_or(|i| i.skips(&all)))
}

#[cfg(test)]
mod tests {
    use super::split;

    #[test]
    fn split_is_posixpath_split() {
        assert_eq!(split(b"/a/b"), (b"/a".to_vec(), b"b".to_vec()));
        assert_eq!(split(b"//a"), (b"//".to_vec(), b"a".to_vec()));
        assert_eq!(split(b"a//b"), (b"a".to_vec(), b"b".to_vec()));
        assert_eq!(split(b"a/"), (b"a".to_vec(), Vec::new()));
        assert_eq!(split(b"b"), (Vec::new(), b"b".to_vec()));
    }
}
