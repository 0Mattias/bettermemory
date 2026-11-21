//! `location.py`'s capture signature and capture cache: `_evicted`
//! (321-326), `_stamp` (355-359), `signature` (758-851), `_masked`
//! (854-865), `_Entry` (868-873), the cache and its clear (875-882),
//! `forget` (885-895), `_fresh` (898-902), `capture` (905-951),
//! `_capture_cached` (954-1002) and `capture_token` (1005-1017).
//!
//! Effects are arguments: the file system through [`Fs`] (and githead's
//! `stamp`), git's discovery walk, the uncached capture, the working
//! tree's record, the above-cache's clear, `config_unchanged` and both
//! clocks through closures, the discovery variables as a bool. The cache
//! is a value the caller holds behind its lock. See native/README.md,
//! "The capture signature and the capture cache".

use crate::capture::{Capture, State};
use crate::checkout_token::token_path;
use crate::fsdata::{Fs, OsError, PyErr, StatResult, SF_DATALESS, WINDOWS_RECALL};
use crate::gitconfig::stamped::inside;
use crate::gitfile_rule::gitdir::{Ctx, Failure};
use crate::gitfile_rule::headref_content;
use crate::gitfile_rule::hostpath::{dirname, join, normpath};
use crate::gitfile_rule::probe::{path_missing, Raised};
use crate::githead::discovery::stamp as githead_stamp;
use crate::location_git::{Git, PROJECT_FILE};
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode;

/// `CACHE_SECONDS` (`location_git.py` 141): how long a capture answers
/// for its directory, on both clocks.
pub const CACHE_SECONDS: f64 = 600.0;
/// `CACHE_CAP` (`location_git.py` 148): captures kept, oldest evicted.
pub const CACHE_CAP: usize = 256;
/// `FETCH_BUDGET` (`location_git.py` 127): `capture`'s default budget.
pub const FETCH_BUDGET: f64 = 0.3;
/// `_HEAD_LIMIT` (`location_git.py` 238): the bytes of HEAD read.
pub const HEAD_LIMIT: usize = 1 << 16;
/// `_DISCOVERY_ENV` (`location_git.py` 241): any of these set and not
/// empty, and no signature is kept (the caller's bool).
pub const DISCOVERY_ENV: [&str; 3] = ["GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"];
/// `_TOKEN_SLOT` (`location_git.py` 243).
const TOKEN_SLOT: &[u8] = b"token";

/// `githead.stamp`'s tuple.
pub type Stamp = [i128; 5];
/// `_stamp`'s answer where the stat raised: `(-1, -1, -1, -1, -1)`.
pub const UNSTAMPED: Stamp = [-1; 5];

/// Why a call has no answer: the reference answers where this port does
/// not (the Windows flavour's stat fields and relative paths, a git
/// directory that is not UTF-8), or the reference raises ValueError (a
/// NUL in a path discovery handed over, which `signature` lets through).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Declined {
    NotPorted,
    ValueError,
}

/// What `_discover` raises that `signature` catches (OSError, GitDies),
/// and a discovery the port does not answer.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DiscoverFailed {
    Os,
    GitDies,
    NotPorted,
}

/// `_discover(cwd, read_head=...)`.
pub type Discover<'a> = dyn FnMut(&[u8], bool) -> Result<Option<Git>, DiscoverFailed> + 'a;
/// `signature(cwd)` with `read_head` False.
pub type SignatureOf<'a> = dyn FnMut(&[u8]) -> Result<Option<Signature>, Declined> + 'a;
/// `_capture_uncached(cwd, write_token, budget, manifest)`.
pub type Uncached<'a> = dyn FnMut(&[u8], bool, f64, bool) -> Capture + 'a;

/// `(cwd, st_dev, st_ino)`; `id` is None only where `_masked` blanked a
/// cwd spelled ``token`` (the part reads `("token", None)` there).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CwdPart {
    pub path: Vec<u8>,
    pub id: Option<(u64, u64)>,
}

/// The repository arm of `signature`'s tuple, field by field.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RepoSignature {
    pub cwd: CwdPart,
    pub dotgit: Option<Vec<u8>>,
    pub dotgit_stamp: Option<Stamp>,
    pub git_dir: Vec<u8>,
    pub common_dir: Vec<u8>,
    pub head: Vec<u8>,
    /// HEAD's `(st_mtime_ns, st_ctime_ns, st_size, st_ino)`.
    pub head_stat: [i128; 4],
    pub ref_part: Option<Stamp>,
    pub packed: Option<Stamp>,
    pub config: Option<[i128; 4]>,
    pub shallow: Option<Stamp>,
    /// The token slot's stamp; `_masked` sets it None.
    pub token: Option<Stamp>,
    pub inner: Vec<Option<Stamp>>,
    pub refs: Option<(Option<Stamp>, Option<Stamp>)>,
}

/// `signature`'s tuple: `(OUTSIDE, cwd_part, project stamp)` or the
/// repository's. The derived equality is the tuples'.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Signature {
    Outside {
        cwd: CwdPart,
        project: Option<Stamp>,
    },
    Repo(Box<RepoSignature>),
}

/// `_evicted(st)`: SF_DATALESS in `st_flags` (macOS), or a recall
/// attribute in `st_file_attributes` (Windows).
pub fn evicted(st: &StatResult) -> bool {
    if st.flags & SF_DATALESS != 0 {
        return true;
    }
    st.file_attributes & WINDOWS_RECALL != 0
}

/// `_stamp(path)`: githead's stamp, `UNSTAMPED` where it raises.
pub fn stamp<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, path: &[u8]) -> Result<Option<Stamp>, Declined> {
    match githead_stamp(ctx, path, true) {
        Ok(found) => Ok(found),
        Err(Failure::NotPorted) => Err(Declined::NotPorted),
        Err(_) => Ok(Some(UNSTAMPED)),
    }
}

/// `os.stat(path)`: the OSError as answered, ValueError declined.
fn os_stat<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    path: &[u8],
) -> Result<Result<StatResult, OsError>, Declined> {
    let at = ctx.at(path).map_err(|_| Declined::NotPorted)?;
    match ctx.fs.stat(&at) {
        Err(e) if e.class == PyErr::Value => Err(Declined::ValueError),
        other => Ok(other),
    }
}

/// `open(path, "rb").read(_HEAD_LIMIT)`; None for an OSError.
fn read_head<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, path: &[u8]) -> Result<Option<Vec<u8>>, Declined> {
    let at = ctx.at(path).map_err(|_| Declined::NotPorted)?;
    let mut file = match ctx.fs.open_waiting(&at) {
        Ok(file) => file,
        Err(PyErr::Value) => return Err(Declined::ValueError),
        Err(_) => return Ok(None),
    };
    let mut out = Vec::new();
    while out.len() < HEAD_LIMIT {
        match ctx.fs.read(&mut file, HEAD_LIMIT - out.len()) {
            Ok(chunk) if chunk.is_empty() => break,
            Ok(chunk) => out.extend_from_slice(&chunk),
            Err(_) => return Ok(None),
        }
    }
    Ok(Some(out))
}

/// `str.strip()` over code points.
fn strip(t: &[u32]) -> &[u32] {
    let Some(start) = t.iter().position(|&c| !unicode::is_space(c)) else {
        return &[];
    };
    let end = t
        .iter()
        .rposition(|&c| !unicode::is_space(c))
        .unwrap_or(start);
    &t[start..=end]
}

fn encode(t: &[u32]) -> Vec<u8> {
    t.iter()
        .filter_map(|&c| char::from_u32(c))
        .collect::<String>()
        .into_bytes()
}

fn four(st: &StatResult) -> Result<[i128; 4], Declined> {
    match (st.ctime_ns, st.ino) {
        (Some(ctime), Some(ino)) => Ok([st.mtime_ns, ctime, i128::from(st.size), i128::from(ino)]),
        _ => Err(Declined::NotPorted),
    }
}

/// `signature(cwd, read_head=read_head)`. `moved` is whether a variable
/// of `_DISCOVERY_ENV` is set; `discover` is `_discover(cwd,
/// read_head=...)`. `Ok(None)` where the reference answers None.
pub fn signature<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    cwd: &[u8],
    read_head_bytes: bool,
    moved: bool,
    discover: &mut Discover<'_>,
) -> Result<Option<Signature>, Declined> {
    if moved {
        return Ok(None);
    }
    let fl = ctx.flavour;
    let at = ctx.at(cwd).map_err(|_| Declined::NotPorted)?;
    let Ok(st) = ctx.fs.stat(&at) else {
        return Ok(None);
    };
    let (Some(dev), Some(ino)) = (st.dev, st.ino) else {
        return Err(Declined::NotPorted);
    };
    let cwd_part = CwdPart {
        path: cwd.to_vec(),
        id: Some((dev, ino)),
    };
    let git = match discover(cwd, read_head_bytes) {
        Ok(Some(git)) => git,
        Ok(None) => {
            let project = stamp(ctx, &join(fl, cwd, PROJECT_FILE))?;
            return Ok(Some(Signature::Outside {
                cwd: cwd_part,
                project,
            }));
        }
        Err(DiscoverFailed::NotPorted) => return Err(Declined::NotPorted),
        Err(_) => return Ok(None),
    };
    let head_path = join(fl, &git.git_dir, b"HEAD");
    let Ok(hst) = os_stat(ctx, &head_path)? else {
        return Ok(None);
    };
    if evicted(&hst) {
        return Ok(None);
    }
    let cst = match os_stat(ctx, &join(fl, &git.common_dir, b"config"))? {
        Ok(cst) => Some(cst),
        Err(e) if e.class == PyErr::FileNotFound => {
            if !path_missing(Raised::from(e)) {
                return Ok(None); // a share that does not answer (ENG-217)
            }
            None
        }
        Err(_) => return Ok(None),
    };
    if cst.as_ref().is_some_and(evicted) {
        return Ok(None);
    }
    let Some(head) = read_head(ctx, &head_path)? else {
        return Ok(None);
    };
    if !read_head_bytes && !headref_content(&head) {
        return signature(ctx, cwd, true, moved, discover);
    }
    let (mut ref_part, mut refs) = (None, None);
    let decoded: Vec<u32> = String::from_utf8_lossy(&head)
        .chars()
        .map(u32::from)
        .collect();
    let text = strip(&decoded);
    let prefix: Vec<u32> = "ref:".chars().map(u32::from).collect();
    if let Some(rest) = text.strip_prefix(prefix.as_slice()) {
        let name = encode(strip(rest));
        let per_worktree = [&b"refs/bisect/"[..], b"refs/rewritten/", b"refs/worktree/"]
            .iter()
            .any(|p| name.starts_with(p));
        let mut path = if per_worktree {
            git.git_dir.clone()
        } else {
            git.common_dir.clone()
        };
        for part in name.split(|&b| b == b'/') {
            path = join(fl, &path, part);
        }
        ref_part = stamp(ctx, &path)?;
        if ref_part.is_none() {
            let refs_dir = join(fl, &git.common_dir, b"refs");
            refs = Some((
                stamp(ctx, &join(fl, &refs_dir, b"heads"))?,
                stamp(ctx, &join(fl, &refs_dir, b"remotes"))?,
            ));
        }
    }
    let mut inner = Vec::new();
    let root = git.root.clone().unwrap_or_else(|| git.git_dir.clone());
    let within = inside(fl, cwd, &root).ok_or(Declined::NotPorted)?;
    let mut d = if within { cwd.to_vec() } else { root.clone() };
    loop {
        inner.push(stamp(ctx, &join(fl, &d, PROJECT_FILE))?);
        if normpath(fl, &d) == normpath(fl, &root) {
            break;
        }
        let parent = dirname(fl, &d);
        if parent == d {
            break;
        }
        d = parent;
    }
    let git_dir_text = std::str::from_utf8(&git.git_dir).map_err(|_| Declined::NotPorted)?;
    let token = stamp(ctx, token_path(git_dir_text, fl).as_bytes())?;
    let dotgit_stamp = match &git.dotgit {
        Some(dotgit) => stamp(ctx, dotgit)?,
        None => None,
    };
    Ok(Some(Signature::Repo(Box::new(RepoSignature {
        cwd: cwd_part,
        dotgit_stamp,
        head_stat: four(&hst)?,
        ref_part,
        packed: stamp(ctx, &join(fl, &git.common_dir, b"packed-refs"))?,
        config: cst.as_ref().map(four).transpose()?,
        shallow: stamp(ctx, &join(fl, &git.common_dir, b"shallow"))?,
        token,
        inner,
        refs,
        dotgit: git.dotgit,
        git_dir: git.git_dir,
        common_dir: git.common_dir,
        head,
    }))))
}

/// `_masked(sig)`: every part that is a tuple whose first item is
/// `_TOKEN_SLOT` reads `(_TOKEN_SLOT, None)`: the token's slot, and the
/// cwd's part where the cwd is spelled ``token`` (a reference finding).
pub fn masked(sig: &Signature) -> Signature {
    let mut out = sig.clone();
    let cwd = match &mut out {
        Signature::Outside { cwd, .. } => cwd,
        Signature::Repo(repo) => {
            repo.token = None;
            &mut repo.cwd
        }
    };
    if cwd.path == TOKEN_SLOT {
        cwd.id = None;
    }
    out
}

/// `_Entry`.
#[derive(Clone, Debug, PartialEq)]
pub struct Entry {
    pub capture: Capture,
    pub signature: Signature,
    pub monotonic: f64,
    pub wall: f64,
}

/// `_CACHE`'s key: (cwd, write_token, manifest).
pub type CacheKey = (Vec<u8>, bool, bool);

/// `_CACHE`: insertion ordered, the oldest first. `_clear_cache` is
/// [`Cache::clear`].
#[derive(Clone, Debug, Default)]
pub struct Cache {
    entries: Vec<(CacheKey, Entry)>,
}

impl Cache {
    pub fn entries(&self) -> &[(CacheKey, Entry)] {
        &self.entries
    }

    pub fn get(&self, key: &CacheKey) -> Option<&Entry> {
        self.entries.iter().find(|(k, _)| k == key).map(|(_, e)| e)
    }

    pub fn clear(&mut self) {
        self.entries.clear();
    }

    /// `pop(key)`, then the entry set last, then the oldest dropped while
    /// more than `CACHE_CAP` are kept.
    pub fn put(&mut self, key: CacheKey, entry: Entry) {
        self.entries.retain(|(k, _)| *k != key);
        self.entries.push((key, entry));
        while self.entries.len() > CACHE_CAP {
            self.entries.remove(0);
        }
    }

    fn drop_cwd(&mut self, cwd: &[u8]) {
        self.entries.retain(|(k, _)| k.0 != cwd);
    }
}

/// `forget(cwd)`: every entry of `cwd`, or all of them and the project
/// files above (`clear_above`) where `cwd` is None.
pub fn forget(cache: &mut Cache, cwd: Option<&[u8]>, clear_above: &mut dyn FnMut()) {
    match cwd {
        Some(cwd) => cache.drop_cwd(cwd),
        None => {
            cache.clear();
            clear_above();
        }
    }
}

/// `_fresh(monotonic, wall, now, now_wall)`.
pub fn fresh(monotonic: f64, wall: f64, now: f64, now_wall: f64) -> bool {
    let (a, b) = (now - monotonic, now_wall - wall);
    (0.0..CACHE_SECONDS).contains(&a) && (0.0..CACHE_SECONDS).contains(&b)
}

/// What `capture` reaches outside itself.
pub struct Effects<'a> {
    /// `signature(cwd)`, read_head False.
    pub signature: &'a mut SignatureOf<'a>,
    /// `(time.monotonic(), time.time())`.
    pub clocks: &'a mut dyn FnMut() -> (f64, f64),
    /// `config_unchanged(capture)`.
    pub config_unchanged: &'a mut dyn FnMut(&Capture) -> Result<bool, Declined>,
    /// `_capture_uncached(cwd, write_token, budget, manifest)`.
    pub uncached: &'a mut Uncached<'a>,
    /// `record_working_tree(git_dir, tree)`.
    pub record_working_tree: &'a mut dyn FnMut(&PyStr, &PyStr) -> String,
}

/// The switches of `capture` (`budget` defaults to `FETCH_BUDGET`, the
/// others to True).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Asked {
    pub write_token: bool,
    pub budget: f64,
    pub manifest: bool,
    pub use_cache: bool,
    pub record_tree: bool,
}

impl Asked {
    pub fn new(write_token: bool) -> Asked {
        Asked {
            write_token,
            budget: FETCH_BUDGET,
            manifest: true,
            use_cache: true,
            record_tree: true,
        }
    }
}

/// `capture(cwd, ...)`.
pub fn capture(
    cache: &mut Cache,
    fx: &mut Effects<'_>,
    cwd: &[u8],
    asked: Asked,
) -> Result<Capture, Declined> {
    let mut got = capture_cached(cache, fx, cwd, asked)?;
    let (Some(git_dir), Some(root)) = (got.git_dir.clone(), got.root.clone()) else {
        return Ok(got);
    };
    if !asked.record_tree || !got.kept_apart || git_dir.is_empty() || root.is_empty() {
        return Ok(got);
    }
    match (fx.record_working_tree)(&git_dir, &root).as_str() {
        "first" => {
            got.tree_recorded = true;
            Ok(got)
        }
        "elsewhere" => {
            cache.drop_cwd(cwd);
            let again = Asked {
                use_cache: false,
                ..asked
            };
            capture_cached(cache, fx, cwd, again)
        }
        _ => Ok(got),
    }
}

/// `_capture_cached(cwd, ...)`: `capture` without the tree's record.
pub fn capture_cached(
    cache: &mut Cache,
    fx: &mut Effects<'_>,
    cwd: &[u8],
    asked: Asked,
) -> Result<Capture, Declined> {
    let key = (cwd.to_vec(), asked.write_token, asked.manifest);
    let before = if asked.use_cache {
        (fx.signature)(cwd)?
    } else {
        None
    };
    let (now, wall) = (fx.clocks)();
    if let (Some(before), Some(entry)) = (&before, cache.get(&key)) {
        if entry.signature == *before
            && fresh(entry.monotonic, entry.wall, now, wall)
            && (fx.config_unchanged)(&entry.capture)?
        {
            return Ok(entry.capture.clone());
        }
    }
    let got = (fx.uncached)(cwd, asked.write_token, asked.budget, asked.manifest);
    let Some(before) = before else {
        return Ok(got);
    };
    if got.state == State::Indeterminate || got.evicted {
        return Ok(got);
    }
    let Some(after) = (fx.signature)(cwd)? else {
        return Ok(got);
    };
    let same = if got.token_written.is_none() {
        after == before
    } else {
        masked(&after) == masked(&before)
    };
    if !same {
        return Ok(got);
    }
    let mut kept = got.clone();
    if let Some(written) = &got.token_written {
        kept.token = Some(written.clone());
        kept.token_written = None;
        kept.token_status = unicode::cps("present");
    }
    cache.put(
        key,
        Entry {
            capture: kept,
            signature: after,
            monotonic: now,
            wall,
        },
    );
    Ok(got)
}

/// `capture_token(cwd, ...)`: `capture(...).to_token()`.
pub fn capture_token(
    cache: &mut Cache,
    fx: &mut Effects<'_>,
    cwd: &[u8],
    write_token: bool,
    budget: f64,
    manifest: bool,
) -> Result<PyValue, Declined> {
    let asked = Asked {
        budget,
        manifest,
        ..Asked::new(write_token)
    };
    Ok(capture(cache, fx, cwd, asked)?.to_token())
}
