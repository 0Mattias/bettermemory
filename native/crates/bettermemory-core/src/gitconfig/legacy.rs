//! The stamps a cached answer is kept against, and the legacy remote
//! files git still reads: the rest of `src/bettermemory/gitconfig.py`.
//!
//! - [`file_stamp`] and [`unchanged`] (`file_stamp` 620-637, `unchanged`
//!   640-651);
//! - [`legacy_names`] (`_legacy_names` 947-968) over [`LEGACY_FOLDERS`]
//!   (`_LEGACY_FOLDERS`, `_LEGACY_LIMIT`, `_LEGACY_NAMES`, 893-899);
//! - [`legacy_remote`] (`_legacy_remote` 982-1030) with its bounded read
//!   of one file (`_legacy_file` 905-944) and `_add_url` (1033-1042);
//! - [`default_branch`] (`_default_branch` 1045-1059) and
//!   [`legacy_remote_stamps`] (1062-1078).
//!
//! A stamp is `(st_mtime_ns, st_ctime_ns, st_size, st_ino, st_mode)` of
//! `os.stat`. Where the file system gives no inode, mode or change time
//! (the Windows side of [`Fs::stat`]), a stamp of a file that is there
//! cannot be held to the reference's and the call answers [`NotPorted`].

use super::paths::{self, code_points};
use super::remote_rules::{refname_valid, refspec_valid};
use super::tree::ConfigValues;
use crate::fsdata::{Flavour, Fs, Kind, PyErr};
use crate::gitfile_rule::probe::{path_missing, Raised};

/// `Stamp`: `(st_mtime_ns, st_ctime_ns, st_size, st_ino, st_mode)`.
pub type Stamp = [i128; 5];
/// `_UNSTATED`: a path that cannot be stat-ed.
pub const UNSTATED: Stamp = [-1; 5];
/// `ConfigFile`: a path and its stamp (None: nothing there).
pub type Files = Vec<(Vec<u8>, Option<Stamp>)>;

/// `_LEGACY_FOLDERS`, in the order git reads them for a remote.
pub const LEGACY_FOLDERS: [&[u8]; 2] = [b"remotes", b"branches"];
/// `_LEGACY_LIMIT`: the bytes read of one legacy file.
pub const LEGACY_LIMIT: usize = 1 << 16;
/// `_LEGACY_NAMES`: the remotes the folders may name.
pub const LEGACY_NAMES: usize = 256;
/// `_GIT_SPACE`: what git's `strbuf_trim` trims.
const GIT_SPACE: &[u8] = b" \t\n\r";

/// A stamp the file system cannot give as the reference's (no inode,
/// mode or change time).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NotPorted;

/// `file_stamp(path)`.
pub fn file_stamp<F: Fs>(fs: &F, path: &[u8]) -> Result<Option<Stamp>, NotPorted> {
    let st = match fs.stat(path) {
        Ok(st) => st,
        Err(e) => {
            return Ok(if path_missing(Raised::from(e)) {
                None
            } else {
                Some(UNSTATED)
            })
        }
    };
    let (Some(ino), Some(mode)) = (st.ino, st.mode) else {
        return Err(NotPorted);
    };
    let (ino, mode) = (i128::from(ino), i128::from(mode));
    Ok(Some(match st.kind {
        Kind::Directory => [st.mtime_ns, st.ctime_ns.ok_or(NotPorted)?, 0, ino, mode],
        Kind::Regular => [
            st.mtime_ns,
            st.ctime_ns.ok_or(NotPorted)?,
            i128::from(st.size),
            ino,
            mode,
        ],
        _ => [0, 0, 0, ino, mode],
    }))
}

/// `unchanged(files)`: each path stamped once, compared in order.
pub fn unchanged<F: Fs>(fs: &F, files: &[(Vec<u8>, Option<Stamp>)]) -> Result<bool, NotPorted> {
    let mut seen: std::collections::HashMap<&[u8], Option<Stamp>> =
        std::collections::HashMap::new();
    for (path, was) in files {
        let now = match seen.get(path.as_slice()) {
            Some(now) => *now,
            None => {
                let now = file_stamp(fs, path)?;
                seen.insert(path, now);
                now
            }
        };
        if now != *was {
            return Ok(false);
        }
    }
    Ok(true)
}

/// Why a legacy read stops.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Halt {
    /// The capture declines, with the reference's text.
    Declines(Vec<u8>),
    /// A stamp not held to the reference's.
    NotPorted,
}

impl From<NotPorted> for Halt {
    fn from(_: NotPorted) -> Halt {
        Halt::NotPorted
    }
}

fn words(parts: &[&[u8]]) -> Vec<u8> {
    parts.concat()
}

fn class_text(class: PyErr) -> &'static [u8] {
    class.name().as_bytes()
}

/// `_legacy_names(common_dir, files)`: the names, sorted as the
/// reference sorts text (by code point, a byte that is not UTF-8 its
/// surrogate), each once across both folders.
pub fn legacy_names<F: Fs>(
    fs: &F,
    flavour: Flavour,
    common_dir: &[u8],
    files: &mut Files,
) -> Result<Vec<Vec<u8>>, Halt> {
    let mut names: Vec<Vec<u8>> = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for folder in LEGACY_FOLDERS {
        let path = paths::join(flavour, common_dir, folder);
        files.push((path.clone(), file_stamp(fs, &path)?));
        let entries = match fs.listdir(&path) {
            Ok(entries) => entries,
            Err(e) => {
                if path_missing(Raised::from(e)) {
                    continue;
                }
                return Err(Halt::Declines(words(&[
                    &path,
                    b" cannot be listed (",
                    class_text(e.class),
                    b")",
                ])));
            }
        };
        let mut keyed: Vec<(Vec<u32>, Vec<u8>)> =
            entries.into_iter().map(|n| (code_points(&n), n)).collect();
        keyed.sort();
        for (_, name) in keyed {
            if seen.insert(name.clone()) {
                names.push(name);
            }
        }
        if names.len() > LEGACY_NAMES {
            return Err(Halt::Declines(
                format!("the legacy remote folders name more than {LEGACY_NAMES} remotes")
                    .into_bytes(),
            ));
        }
    }
    Ok(names)
}

/// `_legacy_file(path, files)`: the bytes (None: git reads nothing).
fn legacy_file<F: Fs>(fs: &F, path: &[u8], files: &mut Files) -> Result<Option<Vec<u8>>, Halt> {
    files.push((path.to_vec(), file_stamp(fs, path)?));
    let cannot = |class: PyErr| {
        Halt::Declines(words(&[
            path,
            b" cannot be read (",
            class_text(class),
            b")",
        ]))
    };
    let mut file = match fs.open(path) {
        Ok(file) => file,
        Err(class) => {
            if class.missing() || matches!(class, PyErr::Permission | PyErr::IsADirectory) {
                return Ok(None);
            }
            return Err(cannot(class));
        }
    };
    let st = fs.fstat(&mut file).map_err(cannot)?;
    if st.kind == Kind::Directory {
        return Ok(None);
    }
    if st.kind != Kind::Regular {
        return Err(Halt::Declines(words(&[path, b" is not a regular file"])));
    }
    let mut data = Vec::new();
    while data.len() <= LEGACY_LIMIT {
        let chunk = fs
            .read(&mut file, LEGACY_LIMIT + 1 - data.len())
            .map_err(cannot)?;
        if chunk.is_empty() {
            break;
        }
        data.extend_from_slice(&chunk);
    }
    if data.len() > LEGACY_LIMIT {
        return Err(Halt::Declines(words(&[
            path,
            format!(" is larger than {} KiB", LEGACY_LIMIT >> 10).as_bytes(),
        ])));
    }
    Ok(Some(data))
}

/// `_Legacy`: whether this git empties a remote's URLs at an empty one
/// (None: not known, which `_add_url` raises as `_Unknown` only where it
/// asks), and the branch [`default_branch`] names (None: git dies).
#[derive(Clone, Debug, Default)]
pub struct Against {
    pub resets: Option<bool>,
    pub default: Option<Vec<u8>>,
}

/// What `_legacy_remote` answers.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Legacy {
    /// The URLs, in order (empty: none).
    Urls(Vec<Vec<u8>>),
    /// None: git dies reading this remote.
    Dies,
    /// `resets` was asked and is not known (`_Unknown`).
    Unknown,
}

fn rstrip(mut s: &[u8]) -> &[u8] {
    while let Some((last, rest)) = s.split_last() {
        if !GIT_SPACE.contains(last) {
            break;
        }
        s = rest;
    }
    s
}

fn lstrip(mut s: &[u8]) -> &[u8] {
    while let Some((first, rest)) = s.split_first() {
        if !GIT_SPACE.contains(first) {
            break;
        }
        s = rest;
    }
    s
}

fn cut(s: &[u8]) -> &[u8] {
    s.split(|&b| b == 0).next().unwrap_or(s)
}

/// `_add_url`; false where `resets` is asked and not known.
fn add_url(urls: &mut Vec<Vec<u8>>, url: &[u8], resets: Option<bool>) -> bool {
    if !url.is_empty() {
        urls.push(url.to_vec());
        return true;
    }
    match resets {
        None => false,
        Some(true) => {
            urls.clear();
            true
        }
        Some(false) => {
            urls.push(Vec::new());
            true
        }
    }
}

/// `_legacy_remote(common_dir, name, files, against)`.
pub fn legacy_remote<F: Fs>(
    fs: &F,
    flavour: Flavour,
    common_dir: &[u8],
    name: &[u8],
    files: &mut Files,
    against: &Against,
) -> Result<Legacy, Halt> {
    let remotes = paths::join(flavour, &paths::join(flavour, common_dir, b"remotes"), name);
    let found = legacy_file(fs, &remotes, files)?.unwrap_or_default();
    let mut urls = Vec::new();
    for line in found.split(|&b| b == b'\n') {
        let line = cut(rstrip(line));
        for (prefix, fetch) in [(&b"Push:"[..], false), (b"Pull:", true)] {
            if let Some(spec) = line.strip_prefix(prefix) {
                match refspec_valid(lstrip(spec), fetch) {
                    None => {
                        return Err(Halt::Declines(words(&[
                            name,
                            b"'s legacy refspec turns on the object format",
                        ])))
                    }
                    Some(false) => return Ok(Legacy::Dies),
                    Some(true) => {}
                }
            }
        }
        if let Some(url) = line.strip_prefix(b"URL:") {
            if !add_url(&mut urls, lstrip(url), against.resets) {
                return Ok(Legacy::Unknown);
            }
        }
    }
    if !urls.is_empty() {
        return Ok(Legacy::Urls(urls));
    }
    let branches = paths::join(
        flavour,
        &paths::join(flavour, common_dir, b"branches"),
        name,
    );
    let found = legacy_file(fs, &branches, files)?.unwrap_or_default();
    let first = found.split(|&b| b == b'\n').next().unwrap_or_default();
    let line = lstrip(rstrip(first));
    if line.is_empty() {
        return Ok(Legacy::Urls(Vec::new()));
    }
    let line = cut(line);
    let (url, branch) = match line.iter().position(|&b| b == b'#') {
        Some(at) => (&line[..at], line[at + 1..].to_vec()),
        None => match &against.default {
            None => return Ok(Legacy::Dies),
            Some(default) => (line, default.clone()),
        },
    };
    let fetch = words(&[b"refs/heads/", &branch, b":refs/heads/", name]);
    if refspec_valid(&fetch, true) != Some(true) {
        return Ok(Legacy::Dies);
    }
    if refspec_valid(&words(&[b"HEAD:refs/heads/", &branch]), false) != Some(true) {
        return Ok(Legacy::Dies);
    }
    if !add_url(&mut urls, url, against.resets) {
        return Ok(Legacy::Unknown);
    }
    Ok(Legacy::Urls(urls))
}

/// `_default_branch(reads)`: `test_name` is
/// GIT_TEST_DEFAULT_INITIAL_BRANCH_NAME (None or empty: unset).
pub fn default_branch<'a>(
    test_name: Option<&[u8]>,
    reads: impl IntoIterator<Item = &'a ConfigValues>,
) -> Option<Vec<u8>> {
    let mut found: Option<Vec<u8>> = Some(b"master".to_vec());
    match test_name {
        Some(named) if !named.is_empty() => found = Some(named.to_vec()),
        _ => {
            for read in reads {
                for (name, values) in &read.values {
                    if name.section == "init"
                        && name.subsection.is_none()
                        && name.key == "defaultbranch"
                    {
                        if let Some(last) = values.last() {
                            found.clone_from(last);
                        }
                    }
                }
            }
        }
    }
    let found = found?;
    refname_valid(&words(&[b"refs/heads/", &found]), false, false).then_some(found)
}

/// `legacy_remote_stamps(common_dir, name)`.
pub fn legacy_remote_stamps<F: Fs>(
    fs: &F,
    flavour: Flavour,
    common_dir: &[u8],
    name: &[u8],
) -> Result<[Option<Stamp>; 2], NotPorted> {
    let stamp = |folder: &[u8]| {
        file_stamp(
            fs,
            &paths::join(flavour, &paths::join(flavour, common_dir, folder), name),
        )
    };
    Ok([stamp(LEGACY_FOLDERS[0])?, stamp(LEGACY_FOLDERS[1])?])
}
