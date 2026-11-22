//! The stores a hook refuses before it starts a daemon
//! (`_store_refusal.py`, the whole module): a store inside a backup bundle
//! (`bundle_refusal`), a store a restore holds or did not finish
//! (`restore_state`, `restore_refusal`), and the one line a hook writes
//! for either (`store_refusal`).
//!
//! The reference keeps this module apart from `backup` and `restore_lock`
//! for the hook's import cost and reads the same files its own way: the
//! restore lock is tried once with no wait and no "this thread holds it"
//! pass, and a POSIX directory `flock` cannot lock falls through to the
//! record file for that call alone. So this is its own port, not
//! [`crate::store::restore_lock::restore_state`].
//!
//! Paths are spelled as `pathlib` spells them (`str(Path)`, `Path.name`,
//! `Path.parent`, `with_name`), worked on as text: on POSIX each byte of
//! the path is one character, so a byte that is no UTF-8 survives and is
//! shown as the lone surrogate `os.fsdecode` gives it. `with_name` on a
//! path with no name (`/`, `.`) is the reference's ValueError.
//!
//! `Path.resolve()` (strict=False) is Python 3.13's `posixpath.realpath`
//! step for step: each component `lstat`ed and each link read, every
//! error of a step leaving the component as spelled, a link met again
//! before it resolved (a loop) left as reached.
//!
//! Windows: the restore lock is the record file alone, locked with
//! `msvcrt.locking` on one byte at 1 MiB in the reference; std locks the
//! whole file, which overlaps that byte, so the port takes `try_lock` and
//! lets go at once, as `store::restore_lock`'s Windows reader does. A
//! file's link count is `nNumberOfLinks`, as `os.stat` reads it there.
//! `Path.resolve` (`ntpath.realpath`) is not ported for the Windows
//! flavour (ENG-124): `bundle_refusal` answers from the path as spelled
//! when that finds a manifest and otherwise declines
//! ([`Failure::NotPorted`]), and a path that is no Unicode text (a lone
//! UTF-16 surrogate, which `str(Path)` keeps) is declined by every
//! function here. All of it is untested here.

use std::fs::{File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

use crate::config::{pure_path, Flavour};
use crate::json;
use crate::printable::printable;
use crate::pyvalue::{PyError, PyStr, PyValue};
use crate::store::restore_lock::LOCK_SUFFIX;
use crate::unicode::cps;

/// `BUNDLE_MANIFEST_NAME` (`backup.MANIFEST_NAME`).
pub const BUNDLE_MANIFEST_NAME: &str = "manifest.json";
/// `BUNDLE_FORMAT` (`backup.BUNDLE_FORMAT`).
pub const BUNDLE_FORMAT: &str = "bettermemory.backup";
/// `BUNDLE_MANIFEST_CAP`: the most of a manifest read.
pub const BUNDLE_MANIFEST_CAP: u64 = 1 << 20;
/// `RESTORE_LOCK_SUFFIX` (`restore_lock.LOCK_SUFFIX`).
pub const RESTORE_LOCK_SUFFIX: &str = LOCK_SUFFIX;
/// `RESTORE_MARKER_CAP`: the most of a restore's record read.
pub const RESTORE_MARKER_CAP: usize = 1 << 16;
/// `RESTORE_LOCK_BYTE`: the byte a Windows restore locks.
pub const RESTORE_LOCK_BYTE: u64 = 1 << 20;

/// `os.O_NONBLOCK` and `os.O_DIRECTORY`, the C library's values on each
/// target.
#[cfg(unix)]
use bettermemory_sys::{O_DIRECTORY, O_NONBLOCK};

#[cfg(windows)]
const SEP: char = '\\';
#[cfg(not(windows))]
const SEP: char = '/';
#[cfg(windows)]
const FLAVOUR: Flavour = Flavour::Windows;
#[cfg(not(windows))]
const FLAVOUR: Flavour = Flavour::Posix;

/// A restore's record as `json.loads` gave it, keys in document order.
pub type Marker = Vec<(PyValue, PyValue)>;

/// What a function here gives in place of an answer.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Failure {
    /// The reference raises this.
    Raised(PyError),
    /// The reference asks what is not ported for the host (Windows:
    /// `Path.resolve`, a path that is no Unicode text); a decline.
    NotPorted,
}

impl From<PyError> for Failure {
    fn from(e: PyError) -> Self {
        Failure::Raised(e)
    }
}

/// What `restore_state` answers.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Running,
    Unfinished,
    Clear,
}

impl State {
    /// The reference's spelling.
    pub fn name(self) -> &'static str {
        match self {
            State::Running => "running",
            State::Unfinished => "unfinished",
            State::Clear => "clear",
        }
    }
}

/// A path as text, one character per byte on POSIX.
#[cfg(unix)]
fn spell(path: &Path) -> String {
    use std::os::unix::ffi::OsStrExt;
    path.as_os_str()
        .as_bytes()
        .iter()
        .map(|&b| char::from(b))
        .collect()
}
#[cfg(not(unix))]
fn spell(path: &Path) -> String {
    path.to_str().unwrap_or_default().to_owned()
}

/// [`spell`], declining a path `spell` cannot hold (on Windows, one with
/// a lone UTF-16 surrogate).
fn spelled(path: &Path) -> Result<String, Failure> {
    #[cfg(not(unix))]
    if path.to_str().is_none() {
        return Err(Failure::NotPorted);
    }
    Ok(spell(path))
}

#[cfg(unix)]
fn unspell(text: &str) -> PathBuf {
    use std::os::unix::ffi::OsStringExt;
    let bytes = text.chars().map(|c| u32::from(c) as u8).collect();
    PathBuf::from(std::ffi::OsString::from_vec(bytes))
}
#[cfg(not(unix))]
fn unspell(text: &str) -> PathBuf {
    PathBuf::from(text)
}

/// `str(path)` for a spelled path.
#[cfg(unix)]
fn shown(text: &str) -> PyStr {
    let bytes: Vec<u8> = text.chars().map(|c| u32::from(c) as u8).collect();
    crate::gitsystem::record::surrogateescape(&bytes)
}
#[cfg(not(unix))]
fn shown(text: &str) -> PyStr {
    cps(text)
}

/// `PurePath.name` of a pure spelling.
fn name_of(pure: &str) -> &str {
    let last = pure.rsplit(SEP).next().unwrap_or("");
    #[cfg(windows)]
    let last = match last.as_bytes() {
        [_, b':', ..] if !pure.contains(SEP) => &last[2..],
        _ => last,
    };
    if last == "." {
        ""
    } else {
        last
    }
}

/// `PurePath.parent` of a pure spelling.
fn parent_of(pure: &str) -> String {
    let head = &pure[..pure.len() - name_of(pure).len()];
    if head.is_empty() {
        return ".".to_owned();
    }
    let trimmed = head.strip_suffix(SEP).unwrap_or(head);
    if trimmed.is_empty() || trimmed.chars().all(|c| c == SEP) || trimmed.ends_with(':') {
        head.to_owned()
    } else {
        trimmed.to_owned()
    }
}

/// `parent / name` for a pure parent.
fn join(parent: &str, name: &str) -> String {
    if parent == "." {
        name.to_owned()
    } else if parent.ends_with(SEP) || parent.ends_with(':') {
        format!("{parent}{name}")
    } else {
        format!("{parent}{SEP}{name}")
    }
}

/// `path.with_name(path.name + RESTORE_LOCK_SUFFIX)`, ValueError for a
/// path with no name.
fn record_of(pure: &str) -> Result<String, PyError> {
    let name = name_of(pure);
    if name.is_empty() {
        return Err(PyError::ValueError);
    }
    let head = &pure[..pure.len() - name.len()];
    Ok(format!("{head}{name}{RESTORE_LOCK_SUFFIX}"))
}

/// `Path.resolve()` with strict=False: Python 3.13's `posixpath.realpath`,
/// every OSError of a step ignored and a symbolic link loop left as
/// reached; None where `os.getcwd` raises.
#[cfg(unix)]
fn resolved(pure: &str) -> Option<String> {
    use std::collections::HashMap;
    let mut rest: Vec<Option<String>> = pure.split('/').rev().map(|p| Some(p.to_owned())).collect();
    let mut part_count = rest.len();
    let mut path = if pure.starts_with('/') {
        "/".to_owned()
    } else {
        spell(&std::env::current_dir().ok()?)
    };
    let mut seen: HashMap<String, Option<String>> = HashMap::new();
    while part_count > 0 {
        let Some(name) = rest.pop()? else {
            let link = rest.pop()??;
            seen.insert(link, Some(path.clone()));
            continue;
        };
        part_count -= 1;
        if name.is_empty() || name == "." {
            continue;
        }
        if name == ".." {
            path.truncate(path.rfind('/').unwrap_or(0));
            if path.is_empty() {
                path.push('/');
            }
            continue;
        }
        let newpath = if path == "/" {
            format!("/{name}")
        } else {
            format!("{path}/{name}")
        };
        let at = unspell(&newpath);
        let target = match std::fs::symlink_metadata(&at) {
            Ok(m) if !m.file_type().is_symlink() => {
                path = newpath;
                continue;
            }
            Ok(_) => match seen.get(&newpath) {
                Some(Some(done)) => {
                    path = done.clone();
                    continue;
                }
                Some(None) => {
                    path = newpath;
                    continue;
                }
                None => std::fs::read_link(&at).ok(),
            },
            Err(_) => None,
        };
        let Some(target) = target else {
            path = newpath;
            continue;
        };
        let target = spell(&target);
        if target.starts_with('/') {
            path = "/".to_owned();
        }
        seen.insert(newpath.clone(), None);
        rest.push(Some(newpath));
        rest.push(None);
        let parts: Vec<&str> = target.split('/').collect();
        part_count += parts.len();
        rest.extend(parts.into_iter().rev().map(|p| Some(p.to_owned())));
    }
    Some(path)
}

fn get<'a>(document: &'a Marker, key: &str) -> Option<&'a PyValue> {
    let key = PyValue::str(key);
    document
        .iter()
        .rev()
        .find(|(k, _)| *k == key)
        .map(|(_, v)| v)
}

/// `_may_be_bundle_manifest`: anything but a JSON document that is not
/// an object naming [`BUNDLE_FORMAT`] counts.
pub fn may_be_bundle_manifest(manifest: &Path) -> bool {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::custom_flags(&mut options, O_NONBLOCK);
    let Ok(file) = options.open(manifest) else {
        return true;
    };
    let Ok(found) = file.metadata() else {
        return true;
    };
    if !found.is_file() || found.len() > BUNDLE_MANIFEST_CAP {
        return true;
    }
    let mut data = Vec::new();
    if file
        .take(BUNDLE_MANIFEST_CAP)
        .read_to_end(&mut data)
        .is_err()
    {
        return true;
    }
    match &json::loads_bytes(&data) {
        Ok(PyValue::Dict(items)) => get(items, "format") == Some(&PyValue::str(BUNDLE_FORMAT)),
        Ok(_) => false,
        Err(_) => true,
    }
}

/// `path.stat().st_nlink`, 0 where it raises.
fn link_count(path: &Path) -> u64 {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        std::fs::metadata(path).map_or(0, |m| m.nlink())
    }
    #[cfg(not(unix))]
    {
        File::open(path)
            .and_then(|f| bettermemory_sys::file_links(&f))
            .map_or(0, u64::from)
    }
}

/// The resolved candidate: None where `Path.resolve` raises; declined
/// on Windows, where it is not ported.
fn resolve_candidate(pure: &str) -> Result<Option<String>, Failure> {
    #[cfg(unix)]
    return Ok(resolved(pure));
    #[cfg(not(unix))]
    {
        let _ = pure;
        Err(Failure::NotPorted)
    }
}

/// `bundle_refusal(path)`: a manifest that may be a bundle's beside the
/// file, as spelled or resolved, or a file with other names.
/// On Windows the resolved candidate is not ported: a path whose spelled
/// candidate finds no manifest is declined.
pub fn bundle_refusal(path: &Path) -> Result<Option<PyStr>, Failure> {
    let pure = pure_path(&spelled(path)?, FLAVOUR);
    let shown_path = shown(&pure);
    let name = shown(name_of(&pure));
    for spelled_first in [true, false] {
        let candidate = if spelled_first {
            pure.clone()
        } else {
            match resolve_candidate(&pure)? {
                Some(candidate) => candidate,
                None => continue,
            }
        };
        let manifest = join(&parent_of(&candidate), BUNDLE_MANIFEST_NAME);
        let at = unspell(&manifest);
        if std::fs::metadata(&at).is_ok_and(|m| m.is_file()) && may_be_bundle_manifest(&at) {
            return Ok(Some(
                [
                    &shown_path[..],
                    &cps(" is in a backup bundle ("),
                    &shown(&manifest),
                    &cps(
                        " is beside it), and opening it would change it and fail the \
                         bundle's hash: copy ",
                    ),
                    &name,
                    &cps(
                        " out of the bundle to a directory of its own first and point the \
                         command at the copy (`bettermemory backup verify` checks a bundle \
                         where it is)",
                    ),
                ]
                .concat(),
            ));
        }
    }
    if link_count(&unspell(&pure)) <= 1 {
        return Ok(None);
    }
    Ok(Some(
        [
            &shown_path[..],
            &cps(
                " has other names (hard links), and opening it would change the file \
                 under each of them, a backup bundle's snapshot among them: restore it \
                 in place from a bundle (`bettermemory backup restore BUNDLE --yes`, \
                 which gives ",
            ),
            &name,
            &cps(" a file of its own), or copy "),
            &name,
            &cps(
                " to a directory of its own first, point BETTERMEMORY_DIR or \
                 `[storage] directory` at that directory and run `bettermemory key \
                 adopt --yes` there (until then the key ring stays bound to this path, \
                 and the copy opens for reading only)",
            ),
        ]
        .concat(),
    ))
}

/// `str(OSError)`: `[Errno n] text`.
fn os_error_text(e: &io::Error) -> String {
    let text = e.to_string();
    match e.raw_os_error() {
        Some(n) => {
            let suffix = format!(" (os error {n})");
            format!(
                "[Errno {n}] {}",
                text.strip_suffix(&suffix).unwrap_or(&text)
            )
        }
        None => text,
    }
}

/// `_restore_marker`: None for a blank record, the record when it reads
/// as an object, an empty one for anything else; RecursionError passes.
fn restore_marker(file: &mut File) -> Result<Option<Marker>, PyError> {
    let mut buf = vec![0u8; RESTORE_MARKER_CAP];
    let n = match file
        .seek(SeekFrom::Start(0))
        .and_then(|_| file.read(&mut buf))
    {
        Ok(n) => n,
        Err(e) => {
            let text = os_error_text(&e);
            return Ok(Some(vec![(
                PyValue::str("unreadable"),
                PyValue::str(&text),
            )]));
        }
    };
    let data = &buf[..n];
    if data
        .iter()
        .all(|b| matches!(b, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c))
    {
        return Ok(None);
    }
    match &json::loads_bytes(data) {
        Ok(PyValue::Dict(items)) => Ok(Some(items.clone())),
        Err(json::LoadError::Recursion) => Err(PyError::RecursionError),
        _ => Ok(Some(Vec::new())),
    }
}

/// `_restore_marker_at`: the record opened read-only, None when it is not.
#[cfg(not(windows))]
fn restore_marker_at(record: &Path) -> Result<Option<Marker>, PyError> {
    match File::open(record) {
        Ok(mut file) => restore_marker(&mut file),
        Err(_) => Ok(None),
    }
}

/// `_record_lock_held`: a shared `flock` tried and given back.
#[cfg(not(windows))]
fn record_lock_held(file: &File) -> bool {
    let tried = file.try_lock_shared();
    if tried.is_ok() {
        let _ = file.unlock();
    }
    try_lock_held(&tried)
}

/// Whether a tried shared `flock` counts as held: a granted lock is not,
/// `EWOULDBLOCK` is, and any other error is judged by `lock_error_held`.
/// Public so a test reaches every arm with a result it builds itself.
#[cfg(unix)]
pub fn try_lock_held(tried: &Result<(), std::fs::TryLockError>) -> bool {
    match tried {
        Ok(()) => false,
        Err(std::fs::TryLockError::WouldBlock) => true,
        Err(std::fs::TryLockError::Error(e)) => lock_error_held(e),
    }
}

/// Whether a failed `flock` other than `EWOULDBLOCK` counts as held:
/// the reference counts `EWOULDBLOCK`, `EAGAIN` and `EINTR`, and any
/// other errno (`ENOLCK`, `EOPNOTSUPP`, `EBADF`) as not held. Public so
/// a test reaches errnos no local file system produces.
#[cfg(unix)]
pub fn lock_error_held(e: &io::Error) -> bool {
    matches!(
        e.kind(),
        io::ErrorKind::Interrupted | io::ErrorKind::WouldBlock
    )
}

/// `_record_lock_held` on Windows: every failure is held.
#[cfg(windows)]
fn record_lock_held(file: &File) -> bool {
    match file.try_lock() {
        Ok(()) => {
            let _ = file.unlock();
            false
        }
        Err(_) => true,
    }
}

fn settled(marker: Option<Marker>) -> (State, Option<Marker>) {
    match marker {
        None => (State::Clear, None),
        Some(m) => (State::Unfinished, Some(m)),
    }
}

/// The record branch: the record file opened read-write (the CRT's
/// share mode on Windows), its lock tried, its marker read.
fn record_state(record: &Path) -> Result<(State, Option<Marker>), PyError> {
    #[cfg_attr(not(windows), allow(unused_mut))]
    let mut options = OpenOptions::new();
    #[cfg(windows)]
    std::os::windows::fs::OpenOptionsExt::share_mode(&mut options, 0x1 | 0x2);
    let Ok(mut file) = options.read(true).write(true).open(record) else {
        return Ok((State::Clear, None));
    };
    let held = record_lock_held(&file);
    let marker = restore_marker(&mut file)?;
    if held {
        return Ok((State::Running, marker));
    }
    Ok(settled(marker))
}

/// `restore_state(store_path)`: `running` while a restore holds the
/// lock, `unfinished` when none does and a record is left, else `clear`.
pub fn restore_state(store_path: &Path) -> Result<(State, Option<Marker>), Failure> {
    let pure = pure_path(&spelled(store_path)?, FLAVOUR);
    let record = unspell(&record_of(&pure)?);
    #[cfg(unix)]
    {
        let mut options = OpenOptions::new();
        options.read(true);
        std::os::unix::fs::OpenOptionsExt::custom_flags(&mut options, O_DIRECTORY);
        let Ok(dir) = options.open(unspell(&parent_of(&pure))) else {
            return Ok((State::Clear, None));
        };
        match dir.try_lock_shared() {
            Ok(()) => {
                let _ = dir.unlock();
                return Ok(settled(restore_marker_at(&record)?));
            }
            Err(std::fs::TryLockError::WouldBlock) => {
                return Ok((State::Running, restore_marker_at(&record)?));
            }
            Err(std::fs::TryLockError::Error(e)) if e.kind() == io::ErrorKind::Interrupted => {
                return Ok((State::Running, restore_marker_at(&record)?));
            }
            Err(std::fs::TryLockError::Error(_)) => drop(dir),
        }
    }
    Ok(record_state(&record)?)
}

/// The record branch alone, as the reference takes it on Windows and
/// where `flock` cannot lock the store's directory.
pub fn restore_state_record_branch(store_path: &Path) -> Result<(State, Option<Marker>), Failure> {
    let pure = pure_path(&spelled(store_path)?, FLAVOUR);
    Ok(record_state(&unspell(&record_of(&pure)?))?)
}

fn text_of(marker: &Marker, key: &str) -> Option<PyStr> {
    match get(marker, key) {
        Some(PyValue::Str(s)) => Some(s.clone()),
        _ => None,
    }
}

/// `_describe_restore`: what a restore's record says, through
/// `printable`.
pub fn describe_restore(marker: &Marker) -> PyStr {
    let mut parts: Vec<PyStr> = Vec::new();
    if let Some(v) = text_of(marker, "started_at") {
        parts.push([cps("started "), v].concat());
    }
    if let Some(PyValue::Int(n)) = get(marker, "pid") {
        parts.push(cps(&format!("by process {n}")));
    }
    if let Some(v) = text_of(marker, "bundle") {
        parts.push([cps("from "), v].concat());
    }
    if let Some(v) = text_of(marker, "stage") {
        parts.push([cps("stopped at "), v].concat());
    }
    if parts.is_empty() {
        printable(&cps("no record of it reads"))
    } else {
        printable(&parts.join(&cps(", ")[..]))
    }
}

/// `restore_refusal(store_path)`: the refusal a writing open gets from
/// the restore lock, read without waiting, or None.
pub fn restore_refusal(store_path: &Path) -> Result<Option<PyStr>, Failure> {
    let (state, marker) = restore_state(store_path)?;
    if state == State::Clear {
        return Ok(None);
    }
    let pure = pure_path(&spelled(store_path)?, FLAVOUR);
    let record = shown(&record_of(&pure)?);
    let why = if state == State::Running {
        [
            &cps("a restore of it is running (its record is ")[..],
            &record,
            &cps("); open it again when the restore has finished"),
        ]
        .concat()
    } else {
        let marker = marker.unwrap_or_default();
        let again = match text_of(&marker, "bundle") {
            Some(b) => [cps("`bettermemory backup restore "), b, cps(" --yes`")].concat(),
            None => cps("`bettermemory backup restore BUNDLE --yes`"),
        };
        [
            &cps("a restore of it did not finish (")[..],
            &describe_restore(&marker),
            &cps("; "),
            &record,
            &cps(" keeps its record), and the store may be part restored. Run the restore again to finish it ("),
            &printable(&again),
            &cps("), or open the store as it stands after `bettermemory backup unlock --yes`"),
        ]
        .concat()
    };
    Ok(Some([shown(&pure), cps(" is not opened: "), why].concat()))
}

/// `store_refusal(store_path)`: why a hook must not start a daemon for
/// the store, or None.
pub fn store_refusal(store_path: &Path) -> Result<Option<PyStr>, Failure> {
    match bundle_refusal(store_path)? {
        Some(why) => Ok(Some(why)),
        None => restore_refusal(store_path),
    }
}
