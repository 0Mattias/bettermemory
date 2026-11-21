//! The capture's filesystem leaves (`location.py` 247-460 and 694-750):
//! birth time, the object key from a stat, eviction and fetch, the HEAD
//! stamp, the local remote key, the kept-apart rule, the working-tree
//! record and the resolved git paths.
//!
//! Effects are arguments: the file system ([`Fs`], whose
//! [`Fs::birth_facts`] carries what the OS says of a birth time), the
//! process's platform ([`Platform`]), the bounded reads' board
//! ([`Board`]) and the record store ([`PinStore`]). The Windows flavour
//! resolves no path (`realpath` is not ported there, ENG-124), so
//! [`local`] and [`real_git`] decline under it.

use crate::capture::paths::norm;
use crate::capture::remote::RemoteKey;
use crate::capture::{Key, Kind};
use crate::fsdata::realpath::{realpath, Unresolved};
use crate::fsdata::{BirthAttr, BirthFacts, Flavour, Fs, PyErr, StatResult};
use crate::gitconfig::legacy::Stamp;
use crate::gitfile_rule::bounded::Answers;
use crate::gitfile_rule::follow::{fsencode, recorded_reading, PinPaths};
use crate::gitfile_rule::format::GitFacts;
use crate::gitfile_rule::gitdir::{Ctx, Failure};
use crate::gitfile_rule::hostpath;
use crate::gitfile_rule::trust::tree_format;
use crate::githead::discovery::stamp as head_stamp;
use crate::gitsystem::record::surrogateescape;
use crate::location_git::Git;
use crate::location_reads::{bounded_read, Board, Read};
use crate::pyvalue::PyStr;

/// `STATX_BTIME`, the mask bit of a birth time.
pub const STATX_BTIME: u32 = 0x800;

/// `sys.platform` as `_birth` and `_statx_birth` read it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Platform {
    Linux,
    Windows,
    /// macOS, the BSDs, Android (`android` since 3.13, PEP 738) and the
    /// rest.
    Other,
}

/// This process's platform.
pub const HOST_PLATFORM: Platform = if cfg!(target_os = "linux") {
    Platform::Linux
} else if cfg!(windows) {
    Platform::Windows
} else {
    Platform::Other
};

/// What `_birth` raises: `int()` of a float that is not finite.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BirthError {
    /// OverflowError (an infinity).
    Overflow,
    /// ValueError (a NaN).
    Value,
    /// Windows' `st_ctime_ns`, which is not ported.
    NotPorted,
}

fn le(raw: &[u8], from: usize, to: usize) -> u64 {
    let part = raw
        .get(from.min(raw.len())..to.min(raw.len()))
        .unwrap_or(&[]);
    part.iter()
        .rev()
        .fold(0u64, |acc, &b| (acc << 8) | u64::from(b))
}

/// `_statx_birth(path)` given the call's answer: None off Linux, where
/// the call is missing (`answer` None), where it returned nonzero, or
/// where the mask lacks `STATX_BTIME`; else the seconds (signed, bytes
/// 80-88) times 10^9 plus the nanoseconds (bytes 88-92) of `raw`.
pub fn statx_birth(platform: Platform, answer: Option<(i32, &[u8])>) -> Option<i128> {
    if platform != Platform::Linux {
        return None;
    }
    let (rc, raw) = answer?;
    if rc != 0 || le(raw, 0, 4) as u32 & STATX_BTIME == 0 {
        return None;
    }
    let width = raw.len().clamp(80, 88) - 80;
    let mut seconds = i128::from(le(raw, 80, 88));
    if width > 0 && seconds >> (8 * width - 1) & 1 == 1 {
        seconds -= 1i128 << (8 * width);
    }
    Some(seconds * 1_000_000_000 + i128::from(le(raw, 88, 92)))
}

/// `int(f)` as decimal text: truncated toward zero, exact at any size.
pub fn int_of_float(f: f64) -> Result<String, BirthError> {
    if f.is_nan() {
        return Err(BirthError::Value);
    }
    if f.is_infinite() {
        return Err(BirthError::Overflow);
    }
    let t = f.trunc();
    if t.abs() < 1e38 {
        return Ok((t as i128).to_string());
    }
    let bits = t.abs().to_bits();
    let exp = ((bits >> 52) & 0x7ff) as i32 - 1075;
    let mant = (bits & ((1 << 52) - 1)) | (1 << 52);
    // Little-endian base 10^9 limbs of mant * 2^exp (exp > 0 here),
    // shifted up to 29 bits a pass: a limb below 2^30 stays below 2^59.
    let mut limbs = vec![mant % 1_000_000_000, mant / 1_000_000_000];
    let mut left = exp;
    while left > 0 {
        let shift = left.min(29);
        left -= shift;
        let mut carry = 0;
        for limb in &mut limbs {
            let v = (*limb << shift) + carry;
            *limb = v % 1_000_000_000;
            carry = v / 1_000_000_000;
        }
        if carry > 0 {
            limbs.push(carry);
        }
    }
    while limbs.len() > 1 && limbs.last() == Some(&0) {
        limbs.pop();
    }
    let mut out = if t < 0.0 {
        "-".to_owned()
    } else {
        String::new()
    };
    out += &limbs.last().unwrap_or(&0).to_string();
    for limb in limbs.iter().rev().skip(1) {
        out += &format!("{limb:09}");
    }
    Ok(out)
}

fn bool_text(b: bool) -> String {
    if b { "True" } else { "False" }.to_owned()
}

/// `str(_birth(path, st))`: None where `report` (`REPORT_BIRTH`) is off;
/// an int `st_birthtime_ns` as it is (a `bool` too: `str(True)`); else an
/// int or float `st_birthtime` times 10^9 through `int()`; else on
/// Windows `st_ctime_ns`; else what `_statx_birth` answered.
pub fn birth(
    report: bool,
    facts: &BirthFacts,
    platform: Platform,
) -> Result<Option<String>, BirthError> {
    if !report {
        return Ok(None);
    }
    match facts.birthtime_ns {
        BirthAttr::Int(n) => return Ok(Some(n.to_string())),
        BirthAttr::Bool(b) => return Ok(Some(bool_text(b))),
        BirthAttr::Absent | BirthAttr::Float(_) => {}
    }
    match facts.birthtime {
        BirthAttr::Int(0) | BirthAttr::Bool(false) => return Ok(Some("0".to_owned())),
        BirthAttr::Int(n) => return Ok(Some(format!("{n}000000000"))),
        BirthAttr::Bool(true) => return Ok(Some("1000000000".to_owned())),
        BirthAttr::Float(f) => return int_of_float(f * 1e9).map(Some),
        BirthAttr::Absent => {}
    }
    if platform == Platform::Windows {
        return facts
            .ctime_ns
            .map(|n| Some(n.to_string()))
            .ok_or(BirthError::NotPorted);
    }
    // `_statx_birth`'s answer, which is None off Linux ([`statx_birth`]).
    Ok(facts.statx.map(|n| n.to_string()))
}

fn text(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

/// `object_key(path, st)`: `(st_dev, st_ino, birth)` as text, None where
/// `path` cannot be stat'ed (`st` None: an OSError or a ValueError). The
/// birth facts are the file system's for `path`.
pub fn object_key<F: Fs + ?Sized>(
    fs: &F,
    path: &[u8],
    st: Option<StatResult>,
    report: bool,
    platform: Platform,
) -> Result<Option<Key>, BirthError> {
    let st = match st {
        Some(st) => st,
        None => match fs.stat(path) {
            Ok(st) => st,
            Err(_) => return Ok(None),
        },
    };
    let (Some(dev), Some(ino)) = (st.dev, st.ino) else {
        return Err(BirthError::NotPorted);
    };
    let born = birth(report, &fs.birth_facts(path), platform)?;
    Ok(Some((
        text(&dev.to_string()),
        text(&ino.to_string()),
        born.map(|b| text(&b)),
    )))
}

/// `_evicted(st)`: the dataless flag or a recall attribute.
pub fn evicted(st: &StatResult) -> bool {
    st.placeholder()
}

/// `_read_one(path)`: `open(path, "rb")` (a FIFO waits) and one byte read.
pub fn read_one<F: Fs + ?Sized>(fs: &F, path: &[u8]) -> Result<(), PyErr> {
    let mut file = fs.open_waiting(path)?;
    fs.read(&mut file, 1)?;
    Ok(())
}

/// `_fetch(path, budget)`: the file read on a worker thread through
/// `bounded_read` (its OSError dropped, a ValueError raised); false where
/// that did not answer in time; else whether `path` stats as not evicted
/// (false where the stat raises an OSError; a ValueError rises).
pub fn fetch<F: Fs + Clone + Send + 'static>(
    fs: &F,
    board: &Board<&'static str, (), PyErr>,
    path: &str,
    budget: f64,
) -> Result<bool, PyErr> {
    let (reader, at) = (fs.clone(), path.as_bytes().to_vec());
    let read = move || match read_one(&reader, &at) {
        Err(PyErr::Value) => Err(PyErr::Value),
        _ => Ok(()),
    };
    if bounded_read(board, path, "fetch", read, budget)? == Read::Unanswered {
        return Ok(false);
    }
    match fs.stat(path.as_bytes()) {
        Ok(st) => Ok(!evicted(&st)),
        Err(e) if e.class == PyErr::Value => Err(PyErr::Value),
        Err(_) => Ok(false),
    }
}

/// `_stamp(path)`: `githead.stamp(path)` (None where nothing is there),
/// `(-1, -1, -1, -1, -1)` where it raises an OSError or a ValueError.
pub fn stamp<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, path: &[u8]) -> Result<Option<Stamp>, Failure> {
    match head_stamp(ctx, path, true) {
        Err(Failure::Os(_) | Failure::Value) => Ok(Some([-1; 5])),
        other => other,
    }
}

fn is(c: u32, b: u8) -> bool {
    c == u32::from(b)
}

/// `posixpath.join(base, path)` of a relative `path`.
fn join(base: &[u32], path: &[u32]) -> PyStr {
    let mut out = base.to_vec();
    if !out.is_empty() && !out.last().is_some_and(|&c| is(c, b'/')) {
        out.push(u32::from(b'/'));
    }
    out.extend_from_slice(path);
    out
}

/// `_local(path, base)`: `path` joined onto `base` unless it is absolute
/// or `base` is None, through `realpath` (the working directory `cwd`),
/// then `_paths.norm`. A path no `os.fsencode` takes raises ValueError.
pub fn local<F: Fs + ?Sized>(
    fs: &F,
    flavour: Flavour,
    cwd: &[u8],
    path: &[u32],
    base: Option<&[u32]>,
) -> Result<PyStr, Unresolved> {
    if flavour == Flavour::Windows {
        return Err(Unresolved::NotPorted);
    }
    let joined = match base {
        Some(base) if !path.first().is_some_and(|&c| is(c, b'/')) => join(base, path),
        _ => path.to_vec(),
    };
    let bytes = fsencode(&joined).ok_or(Unresolved::Raised(PyErr::Value))?;
    let resolved = realpath(fs, flavour, &bytes, cwd)?;
    Ok(norm(&surrogateescape(&resolved)))
}

/// `remote_key(url, base)`'s local key: ``local:`` and [`local`] of the
/// text, or of [`file_url_path`] of a file URL. None for a hosted key.
pub fn local_key<F: Fs + ?Sized>(
    fs: &F,
    flavour: Flavour,
    cwd: &[u8],
    key: &RemoteKey,
    base: Option<&[u32]>,
) -> Result<Option<PyStr>, Unresolved> {
    let RemoteKey::Local {
        text: url,
        file_url,
    } = key
    else {
        return Ok(None);
    };
    let path = if *file_url {
        file_url_path(flavour, url)
    } else {
        url.clone()
    };
    let mut out = text("local:");
    out.extend(local(fs, flavour, cwd, &path, base)?);
    Ok(Some(out))
}

fn hex(c: u32) -> Option<u8> {
    char::from_u32(c)?.to_digit(16).map(|d| d as u8)
}

/// `urllib.parse.unquote(text)`: each run of ASCII percent-decoded and
/// read as UTF-8 with U+FFFD for what is not; the rest kept.
pub fn unquote(text: &[u32]) -> PyStr {
    if !text.iter().any(|&c| is(c, b'%')) {
        return text.to_vec();
    }
    let mut out = Vec::with_capacity(text.len());
    let mut i = 0;
    while i < text.len() {
        if text[i] >= 0x80 {
            out.push(text[i]);
            i += 1;
            continue;
        }
        let end = text[i..]
            .iter()
            .position(|&c| c >= 0x80)
            .map_or(text.len(), |n| i + n);
        let run = &text[i..end];
        let mut bytes = Vec::with_capacity(run.len());
        let mut j = 0;
        while j < run.len() {
            let pair = (
                run.get(j + 1).and_then(|&c| hex(c)),
                run.get(j + 2).and_then(|&c| hex(c)),
            );
            match pair {
                (Some(a), Some(b)) if is(run[j], b'%') => {
                    bytes.push(a * 16 + b);
                    j += 3;
                }
                _ => {
                    bytes.push(run[j] as u8);
                    j += 1;
                }
            }
        }
        out.extend(String::from_utf8_lossy(&bytes).chars().map(u32::from));
        i = end;
    }
    out
}

/// What `nturl2path.url2pathname` raises.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NtUrlError {
    /// OSError ("Bad URL").
    Os,
    /// IndexError (nothing before the drive's colon).
    Index,
}

fn replace(text: &[u32], from: u8, to: &[u32]) -> PyStr {
    let mut out = Vec::with_capacity(text.len());
    for &c in text {
        if is(c, from) {
            out.extend_from_slice(to);
        } else {
            out.push(c);
        }
    }
    out
}

/// `nturl2path.url2pathname(url)` as CPython 3.13.13 has it.
pub fn url2pathname_nt(url: &[u32]) -> Result<PyStr, NtUrlError> {
    let starts =
        |t: &[u32], p: &str| t.len() >= p.len() && t.iter().zip(p.bytes()).all(|(&c, b)| is(c, b));
    let mut url = url;
    if starts(url, "///") {
        url = &url[2..];
    } else if starts(url, "//localhost/") {
        url = &url[11..];
    }
    if starts(url, "///") {
        url = &url[1..];
    }
    let url = replace(url, b':', &[u32::from(b'|')]);
    let back = [u32::from(b'\\')];
    if !url.iter().any(|&c| is(c, b'|')) {
        return Ok(unquote(&replace(&url, b'/', &back)));
    }
    let comp: Vec<&[u32]> = url.split(|&c| is(c, b'|')).collect();
    if comp.len() != 2 {
        return Err(NtUrlError::Os);
    }
    let last = *comp[0].last().ok_or(NtUrlError::Index)?;
    let drive = char::from_u32(last)
        .filter(char::is_ascii_alphabetic)
        .ok_or(NtUrlError::Os)?;
    let mut out = vec![u32::from(drive.to_ascii_uppercase()), u32::from(b':')];
    out.extend(unquote(&replace(comp[1], b'/', &back)));
    Ok(out)
}

/// `_file_url_path(url)` under `flavour` (`os.path.sep`): the path after
/// the authority, percent-decoded, off Windows; on Windows a drive
/// authority or `localhost` read through [`url2pathname_nt`] (a `?` and a
/// `#` quoted first), any other host a UNC path.
pub fn file_url_path(flavour: Flavour, url: &[u32]) -> PyStr {
    let after = url.get(7..).unwrap_or(&[]);
    let cut = after
        .iter()
        .position(|&c| is(c, b'/'))
        .unwrap_or(after.len());
    let (mut host, mut path) = (after[..cut].to_vec(), after[cut..].to_vec());
    if flavour != Flavour::Windows {
        return unquote(&path);
    }
    if host.len() == 2
        && char::from_u32(host[0]).is_some_and(|c| c.is_ascii_alphabetic())
        && is(host[1], b':')
    {
        let mut joined = text("/");
        joined.extend_from_slice(&host);
        joined.extend_from_slice(&path);
        path = joined;
        host.clear();
    }
    let back = [u32::from(b'\\')];
    let lowered: PyStr = host
        .iter()
        .map(|&c| char::from_u32(c).map_or(c, |ch| u32::from(ch.to_ascii_lowercase())))
        .collect();
    if !host.is_empty() && lowered != text("localhost") {
        let mut out = vec![u32::from(b'\\'); 2];
        out.extend(host);
        out.extend(replace(&unquote(&path), b'/', &back));
        return out;
    }
    let shaped = replace(&replace(&path, b'?', &text("%3F")), b'#', &text("%23"));
    match url2pathname_nt(&shaped) {
        Ok(p) => p,
        Err(_) => replace(&unquote(&path), b'/', &back),
    }
}

/// `kept_apart(found)`: a primary checkout found from a `.git` entry that
/// is not its git directory, whose `root/.git` lstats as no directory of
/// its own (a file, a link, a junction), and whose config names no
/// `core.worktree` (`tree_format`). An lstat that raises is false.
pub fn kept_apart<F: Fs>(ctx: &Ctx<'_, F>, found: &Git, git: &GitFacts) -> Result<bool, Failure> {
    if found.kind != Kind::Primary || found.dotgit.as_ref().is_none_or(|d| *d == found.git_dir) {
        return Ok(false);
    }
    let Some(root) = &found.root else {
        return Ok(false);
    };
    let at = ctx.at(&hostpath::join(ctx.flavour, root, b".git"))?;
    let Ok(own) = ctx.fs.lstat_result(&at) else {
        return Ok(false);
    };
    if own.kind == crate::fsdata::Kind::Directory && !own.is_link().ok_or(Failure::NotPorted)? {
        return Ok(false);
    }
    Ok(tree_format(ctx, &found.git_dir, None, git)?
        .worktree
        .is_none())
}

/// `gitdir_pin.recorded` and `gitdir_pin.put`: the record store.
pub trait PinStore {
    /// `recorded(git_dir)`.
    fn recorded(&self, git_dir: &[u8]) -> Result<Option<PinPaths>, Failure>;
    /// `put(git_dir, tree, previous)`: whether this call wrote it.
    fn put(&self, git_dir: &[u8], tree: &[u8], previous: Option<&PinPaths>) -> bool;
}

/// What `record_working_tree` returns.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Recorded {
    First,
    Elsewhere,
    Kept,
}

impl Recorded {
    pub fn as_str(self) -> &'static str {
        match self {
            Recorded::First => "first",
            Recorded::Elsewhere => "elsewhere",
            Recorded::Kept => "kept",
        }
    }
}

/// `record_working_tree(git_dir, tree)`.
pub fn record_working_tree<F: Fs, A: Answers + ?Sized, P: PinStore + ?Sized>(
    ctx: &Ctx<'_, F>,
    answers: &A,
    pins: &P,
    git_dir: &[u8],
    tree: &[u8],
) -> Result<Recorded, Failure> {
    let pin = pins.recorded(git_dir)?;
    let (why, here) = recorded_reading(ctx, answers, pin.as_ref(), git_dir, tree)?;
    if here {
        return Ok(Recorded::Kept);
    }
    if why.is_some() {
        return Ok(Recorded::Elsewhere);
    }
    if pins.put(git_dir, tree, pin.as_ref()) {
        return Ok(if pin.is_none() {
            Recorded::First
        } else {
            Recorded::Kept
        });
    }
    let again = pins.recorded(git_dir)?;
    if recorded_reading(ctx, answers, again.as_ref(), git_dir, tree)?
        .0
        .is_some()
    {
        return Ok(Recorded::Elsewhere);
    }
    Ok(Recorded::Kept)
}

/// `_real_git(git)`: each path through `realpath`, in the reference's
/// order (the dot-git entry, the git directory, the common directory, the
/// root).
pub fn real_git<F: Fs + ?Sized>(
    fs: &F,
    flavour: Flavour,
    cwd: &[u8],
    git: &Git,
) -> Result<Git, Unresolved> {
    let real = |p: &[u8]| realpath(fs, flavour, p, cwd);
    Ok(Git {
        dotgit: git.dotgit.as_deref().map(real).transpose()?,
        git_dir: real(&git.git_dir)?,
        common_dir: real(&git.common_dir)?,
        root: git.root.as_deref().map(real).transpose()?,
        kind: git.kind,
    })
}
