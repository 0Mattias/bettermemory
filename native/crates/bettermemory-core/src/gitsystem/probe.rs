//! The probe repository git is asked in, and the folder it is asked in
//! otherwise (`_probe_name`, `PROBE_FOLDER`, `_where`, `_probe_repository`,
//! `_sweep_probes`, `_probe_stamps`, `_probe_children`, `_probe_holds`,
//! `_clear_probe`).
//!
//! Reads, stamps and listings go through [`Fs`]; folders are made, files
//! written and entries removed through `std::fs`, with the helpers the
//! record uses (`owner_only_dir`, `replace_retrying`). The wall clock of
//! the sweep is an input.
//!
//! What each removing function can remove (the README repeats it):
//!
//! - [`Place::sweep_probes`]: an entry directly in the state directory
//!   whose name is `git-probe` or `git-probe-` and 16 lowercase hex
//!   digits, other than [`probe_folder`], whose own modification time
//!   (not followed) is `REASK_SECONDS` or more before `now`: a folder with
//!   all it holds (links in it unlinked, never followed), anything else
//!   (a link too) unlinked.
//! - [`Place::clear_probe`]: an entry directly in the probe, its
//!   `remotes` or its `compat-differ` (each skipped where it is a link or
//!   no folder) that is not one the probe keeps there of the kind it keeps
//!   (by the listing's entry type, as `os.scandir` gives it: `d_type`,
//!   `lstat` only where that is unknown): a folder with all it holds,
//!   anything else unlinked.
//! - [`Place::probe_repository`]: what the sweep and the clear remove, and
//!   a probe file's entry it replaces by rename.
//!
//! After a removal of a folder that fails partway, which of that folder's
//! entries remain is not pinned: `remove_dir_all` walks forward, 3.13's
//! `shutil.rmtree` in reverse. The answer, and everything outside that
//! folder, are the same.
//!
//! Whether a path is a link or a folder (`Path.is_symlink`,
//! `Path.is_dir`) is read as Python 3.13 reads it: False where the check
//! raises FileNotFoundError, NotADirectoryError, ELOOP, EBADF or
//! ValueError, and any other error raised (EACCES through a folder that
//! cannot be searched). 3.14 reads every error as False.
//!
//! Under the Windows flavour, `ask_folder`, `probe_repository`,
//! `sweep_probes` and `clear_probe` decline (`NotPorted`) before touching
//! anything.
//!
//! One divergence: where a probe file is a FIFO the reference's
//! `read_bytes` waits for a writer; here [`Fs::open`] does not wait, the
//! FIFO reads empty, and it is replaced as any file of other bytes is.

use sha2::{Digest, Sha256};

use crate::fsdata::realpath::{realpath, Unresolved};
use crate::fsdata::{Flavour, Fs, Kind, PyErr};
use crate::gitconfig::legacy::{file_stamp, NotPorted, Stamp};
use crate::gitconfig::paths;
use crate::pyvalue::{repr_str, PyStr};
use crate::store::create::owner_only_dir;
use crate::store::write::replace_retrying;

use super::record::{os_path, Place, REASK_SECONDS};

/// `_where`'s answer: the ask folder, resolved, and the
/// `GIT_CEILING_DIRECTORIES` that keeps git in it.
pub type AskFolder = (Vec<u8>, Vec<u8>);
/// `_probe_repository`'s answer: the probe, resolved, and its stamps.
pub type Probe = (Vec<u8>, Vec<Option<Stamp>>);

/// `ASK_FOLDER`.
pub const ASK_FOLDER: &str = "git-ask";
/// `_COMPAT_PROBE`.
pub const COMPAT_PROBE: &str = "compat-differ";
/// `_HASCONFIG_FILE`.
pub const HASCONFIG_FILE: &str = "hasconfig.inc";

/// `_PROBE_FILES`, in the dict's order.
pub const PROBE_FILES: [(&str, &[u8]); 6] = [
    ("HEAD", b"ref: refs/heads/main\n"),
    (
        "config",
        b"[core]\n\trepositoryformatversion = 0\n\tbare = true\n\
          [remote \"h\"]\n\turl = bettermemory-probe-h\n\
          [includeIf \"hasconfig:remote.*.url:bettermemory-probe-h\"]\n\
          \tpath = hasconfig.inc\n\
          [bettermemory]\n\tprefix = %(prefix)/z\n",
    ),
    ("hasconfig.inc", b"[bettermemory]\n\thasconfig = yes\n"),
    (
        "remotes/p",
        b"URL: bettermemory-probe-a\nURL:\nURL: bettermemory-probe-b\n",
    ),
    ("compat-differ/HEAD", b"ref: refs/heads/main\n"),
    (
        "compat-differ/config",
        b"[core]\n\trepositoryformatversion = 1\n\
          \tbare = true\n[extensions]\n\tcompatObjectFormat = sha256\n",
    ),
];

/// `_PROBE_FOLDERS`.
pub const PROBE_FOLDERS: [&str; 6] = [
    "objects",
    "refs",
    "remotes",
    "compat-differ",
    "compat-differ/objects",
    "compat-differ/refs",
];

/// `_PROBE_LISTED`.
pub const PROBE_LISTED: [&str; 3] = ["", "remotes", "compat-differ"];

/// Where `_probe_name`'s `repr(...).encode("ascii")` fails: the text
/// names a printable character past ASCII, which `repr` keeps
/// (UnicodeEncodeError).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NotAscii;

/// `repr(b)` of bytes.
fn repr_bytes(b: &[u8], out: &mut Vec<u8>) {
    let quote = if b.contains(&b'\'') && !b.contains(&b'"') {
        b'"'
    } else {
        b'\''
    };
    out.push(b'b');
    out.push(quote);
    for &c in b {
        match c {
            b'\\' => out.extend_from_slice(b"\\\\"),
            b'\t' => out.extend_from_slice(b"\\t"),
            b'\n' => out.extend_from_slice(b"\\n"),
            b'\r' => out.extend_from_slice(b"\\r"),
            _ if c == quote => {
                out.push(b'\\');
                out.push(c);
            }
            0x20..=0x7e => out.push(c),
            _ => out.extend_from_slice(format!("\\x{c:02x}").as_bytes()),
        }
    }
    out.push(quote);
}

/// `repr(s)` of text, ASCII-encoded.
fn repr_text(s: &[u32], out: &mut Vec<u8>) -> Result<(), NotAscii> {
    for c in repr_str(s) {
        if c > 0x7f {
            return Err(NotAscii);
        }
        out.push(c as u8);
    }
    Ok(())
}

/// `_probe_name(files, folders)`: `git-probe-` and the first 16 hex
/// digits of the SHA-256 of `repr((sorted(files.items()), folders))`.
/// `files` is the dict's items, its keys distinct.
pub fn probe_name(files: &[(PyStr, Vec<u8>)], folders: &[PyStr]) -> Result<String, NotAscii> {
    let mut items: Vec<&(PyStr, Vec<u8>)> = files.iter().collect();
    items.sort_by(|a, b| a.0.cmp(&b.0).then_with(|| a.1.cmp(&b.1)));
    let mut held = b"([".to_vec();
    for (i, (name, data)) in items.iter().enumerate() {
        if i > 0 {
            held.extend_from_slice(b", ");
        }
        held.push(b'(');
        repr_text(name, &mut held)?;
        held.extend_from_slice(b", ");
        repr_bytes(data, &mut held);
        held.push(b')');
    }
    held.extend_from_slice(b"], (");
    for (i, name) in folders.iter().enumerate() {
        if i > 0 {
            held.extend_from_slice(b", ");
        }
        repr_text(name, &mut held)?;
    }
    if folders.len() == 1 {
        held.push(b',');
    }
    held.extend_from_slice(b"))");
    let digest = Sha256::digest(&held);
    let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    Ok(format!("git-probe-{}", &hex[..16]))
}

fn cps(s: &str) -> PyStr {
    s.chars().map(|c| c as u32).collect()
}

/// `PROBE_FOLDER`: [`probe_name`] of the probe's own files and folders.
pub fn probe_folder() -> String {
    let files: Vec<(PyStr, Vec<u8>)> = PROBE_FILES
        .iter()
        .map(|(n, d)| (cps(n), d.to_vec()))
        .collect();
    let folders: Vec<PyStr> = PROBE_FOLDERS.iter().map(|n| cps(n)).collect();
    probe_name(&files, &folders).expect("the probe's names are ASCII")
}

/// `_PROBE_NAMES.fullmatch(name)`: `git-probe`, or it, `-` and 16
/// lowercase hex digits.
pub fn is_probe_name(name: &[u8]) -> bool {
    match name.strip_prefix(b"git-probe") {
        Some([]) => true,
        Some([b'-', hex @ ..]) => {
            hex.len() == 16 && hex.iter().all(|c| matches!(c, b'0'..=b'9' | b'a'..=b'f'))
        }
        _ => false,
    }
}

/// `_PROBE_STAMPED`: the probe, its folders, its files.
pub fn probe_stamped() -> Vec<&'static str> {
    let mut out = vec![""];
    out.extend(PROBE_FOLDERS);
    out.extend(PROBE_FILES.iter().map(|(n, _)| *n));
    out
}

/// `_probe_children(folder)`: the folders and files the probe holds
/// directly in `folder` ("" the probe), by name, sorted.
pub fn probe_children(folder: &str) -> (Vec<&'static str>, Vec<&'static str>) {
    let prefix = if folder.is_empty() {
        String::new()
    } else {
        format!("{folder}/")
    };
    let within = |names: &mut dyn Iterator<Item = &'static str>| {
        let mut out: Vec<&'static str> = names
            .filter_map(|n| n.strip_prefix(prefix.as_str()))
            .filter(|rest| !rest.contains('/'))
            .collect();
        out.sort_unstable();
        out.dedup();
        out
    };
    (
        within(&mut PROBE_FOLDERS.iter().copied()),
        within(&mut PROBE_FILES.iter().map(|(n, _)| *n)),
    )
}

/// What a probe step meets: an error the reference catches (OSError,
/// ValueError) or a part not ported.
enum Stop {
    Caught(PyErr),
    NotPorted,
}

impl From<PyErr> for Stop {
    fn from(e: PyErr) -> Stop {
        Stop::Caught(e)
    }
}

impl From<crate::fsdata::OsError> for Stop {
    fn from(e: crate::fsdata::OsError) -> Stop {
        Stop::Caught(e.class)
    }
}

impl From<std::io::Error> for Stop {
    fn from(e: std::io::Error) -> Stop {
        Stop::Caught(py_err(&e))
    }
}

/// CPython's errno-to-class map, through the kinds Rust names (as
/// `fsdata`'s).
fn py_err(error: &std::io::Error) -> PyErr {
    use std::io::ErrorKind;
    match error.kind() {
        ErrorKind::NotFound => PyErr::FileNotFound,
        ErrorKind::NotADirectory => PyErr::NotADirectory,
        ErrorKind::IsADirectory => PyErr::IsADirectory,
        ErrorKind::PermissionDenied => PyErr::Permission,
        ErrorKind::WouldBlock => PyErr::BlockingIO,
        ErrorKind::Interrupted => PyErr::Interrupted,
        ErrorKind::TimedOut => PyErr::Timeout,
        ErrorKind::AlreadyExists => PyErr::FileExists,
        ErrorKind::InvalidFilename if cfg!(windows) => PyErr::FileNotFound,
        _ => PyErr::Os,
    }
}

impl From<NotPorted> for Stop {
    fn from(_: NotPorted) -> Stop {
        Stop::NotPorted
    }
}

impl From<Unresolved> for Stop {
    fn from(e: Unresolved) -> Stop {
        match e {
            Unresolved::Raised(e) => Stop::Caught(e),
            Unresolved::NotPorted => Stop::NotPorted,
        }
    }
}

/// Why [`Place::sweep_probes`] or [`Place::clear_probe`] did not finish:
/// the class the reference raises, or the Windows flavour, not ported.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Refusal {
    Raised(PyErr),
    NotPorted,
}

impl From<Stop> for Refusal {
    fn from(e: Stop) -> Refusal {
        match e {
            Stop::Caught(e) => Refusal::Raised(e),
            Stop::NotPorted => Refusal::NotPorted,
        }
    }
}

/// Whether `Path.is_symlink` and `Path.is_dir` of Python 3.13 read this
/// error as False (`pathlib._abc._ignore_error`, and ValueError).
fn ignored(e: &crate::fsdata::OsError) -> bool {
    matches!(
        e.class,
        PyErr::FileNotFound | PyErr::NotADirectory | PyErr::Value
    ) || matches!(e.errno, Some("ELOOP" | "EBADF"))
}

fn settled<T>(r: Result<T, Stop>) -> Result<Option<T>, NotPorted> {
    match r {
        Ok(v) => Ok(Some(v)),
        Err(Stop::Caught(_)) => Ok(None),
        Err(Stop::NotPorted) => Err(NotPorted),
    }
}

/// `st_mtime` as CPython builds it: `sec + nsec * 1e-9`.
fn st_mtime(ns: i128) -> f64 {
    let sec = ns.div_euclid(1_000_000_000);
    let nsec = ns.rem_euclid(1_000_000_000);
    sec as f64 + nsec as f64 * 1e-9
}

/// `ensure_owner_only_dir(path, parents=True)` on a path as bytes.
fn make_owner_only(path: &[u8]) -> Result<(), Stop> {
    let p = os_path(path).ok_or(Stop::Caught(PyErr::Value))?;
    owner_only_dir(&p)?;
    Ok(())
}

/// `shutil.rmtree(path)` (fd-based: a link in it is unlinked, never
/// followed) or `os.unlink(path)`.
fn remove(path: &[u8], folder: bool) -> Result<(), Stop> {
    let p = os_path(path).ok_or(Stop::Caught(PyErr::Value))?;
    if folder {
        std::fs::remove_dir_all(p)?;
    } else {
        std::fs::remove_file(p)?;
    }
    Ok(())
}

/// A listed name as the bytes the OS gave.
fn name_bytes(name: &std::ffi::OsStr) -> Vec<u8> {
    name.as_encoded_bytes().to_vec()
}

impl<F: Fs> Place<'_, F> {
    fn under(&self, root: &[u8], name: &str) -> Vec<u8> {
        paths::join(self.flavour, root, name.as_bytes())
    }

    fn os(&self, path: &[u8]) -> Vec<u8> {
        paths::opened(self.flavour, path, self.cwd)
    }

    /// `path.is_symlink()` as 3.13 answers it (see the module's note).
    fn is_link(&self, path: &[u8]) -> Result<bool, PyErr> {
        match self.fs.lstat_result(&self.os(path)) {
            Ok(st) => Ok(st.kind == Kind::Link),
            Err(e) if ignored(&e) => Ok(false),
            Err(e) => Err(e.class),
        }
    }

    /// `path.is_dir()`, links followed, as 3.13 answers it.
    fn is_dir(&self, path: &[u8]) -> Result<bool, PyErr> {
        match self.fs.stat(&self.os(path)) {
            Ok(st) => Ok(st.kind == Kind::Directory),
            Err(e) if ignored(&e) => Ok(false),
            Err(e) => Err(e.class),
        }
    }

    /// The Windows flavour, which these functions decline.
    fn windows(&self) -> bool {
        self.flavour == Flavour::Windows
    }

    /// At most `max` bytes of the file at `path`, as `f.read(max)` reads.
    fn read_upto(&self, path: &[u8], max: usize) -> Result<Vec<u8>, PyErr> {
        let mut file = self.fs.open(&self.os(path))?;
        let mut data = Vec::new();
        while data.len() < max {
            let got = self.fs.read(&mut file, max - data.len())?;
            if got.is_empty() {
                break;
            }
            data.extend_from_slice(&got);
        }
        Ok(data)
    }

    /// `_where()`: the folder `ASK_FOLDER` under the state directory,
    /// made owner-only, resolved, and the `GIT_CEILING_DIRECTORIES` that
    /// keeps git in it (its parent); None where it cannot be made or holds
    /// an entry.
    pub fn ask_folder(&self) -> Result<Option<AskFolder>, NotPorted> {
        if self.windows() {
            return Err(NotPorted);
        }
        settled((|| {
            let folder = self.under(self.state_dir, ASK_FOLDER);
            make_owner_only(&self.os(&folder))?;
            let real = realpath(self.fs, self.flavour, &folder, self.cwd)?;
            if !self.fs.listdir(&self.os(&real))?.is_empty() {
                return Err(Stop::Caught(PyErr::Os));
            }
            let ceiling = paths::dirname(self.flavour, &real);
            Ok((real, ceiling))
        })())
    }

    /// `_sweep_probes(state)` with `time.time()` as `now`: see the
    /// module's note for what it removes. Err where the state directory
    /// is a folder that cannot be listed (the reference's `scandir`
    /// raises).
    pub fn sweep_probes(&self, now: f64) -> Result<(), Refusal> {
        if self.windows() {
            return Err(Refusal::NotPorted);
        }
        self.sweep(&mut || now).map_err(Refusal::from)
    }

    /// The sweep, `time.time()` read through `wall` where the reference
    /// reads it: once, after the state directory is found a folder.
    fn sweep(&self, wall: &mut dyn FnMut() -> f64) -> Result<(), Stop> {
        let state = self.state_dir;
        if !self.is_dir(state)? {
            return Ok(());
        }
        let now = wall();
        let own = probe_folder();
        for name in self.fs.listdir(&self.os(state))? {
            if name == own.as_bytes() || !is_probe_name(&name) {
                continue;
            }
            let path = paths::join(self.flavour, state, &name);
            let Ok(st) = self.fs.lstat_result(&self.os(&path)) else {
                continue;
            };
            if now - st_mtime(st.mtime_ns) < REASK_SECONDS {
                continue;
            }
            let _ = remove(&self.os(&path), st.kind == Kind::Directory);
        }
        Ok(())
    }

    /// `_probe_stamps(root)`: the stamps of the probe at `root`, in
    /// [`probe_stamped`]'s order.
    pub fn probe_stamps(&self, root: &[u8]) -> Result<Vec<Option<Stamp>>, NotPorted> {
        probe_stamped()
            .into_iter()
            .map(|name| file_stamp(self.fs, &self.os(&self.under(root, name))))
            .collect()
    }

    /// `_probe_holds(root)`: whether the probe at `root` holds its
    /// folders and files, neither a link, the files with their bytes, and
    /// nothing else in a folder of [`PROBE_LISTED`]. Err where a read
    /// raises.
    pub fn probe_holds(&self, root: &[u8]) -> Result<bool, PyErr> {
        for folder in PROBE_LISTED {
            let (folders, files) = probe_children(folder);
            let mut want: Vec<&[u8]> = folders
                .iter()
                .chain(files.iter())
                .map(|n| n.as_bytes())
                .collect();
            want.sort_unstable();
            let mut got = self
                .fs
                .listdir(&self.os(&self.under(root, folder)))
                .map_err(|e| e.class)?;
            got.sort_unstable();
            got.dedup();
            if got.len() != want.len() || got.iter().zip(&want).any(|(g, w)| g.as_slice() != *w) {
                return Ok(false);
            }
        }
        for name in PROBE_FOLDERS {
            if self.fs.lstat(&self.os(&self.under(root, name)))? != Kind::Directory {
                return Ok(false);
            }
        }
        for (name, data) in PROBE_FILES {
            let path = self.under(root, name);
            if self.fs.lstat(&self.os(&path))? != Kind::Regular {
                return Ok(false);
            }
            if self.read_upto(&path, data.len() + 1)? != data {
                return Ok(false);
            }
        }
        Ok(true)
    }

    /// `_clear_probe(root)`: see the module's note for what it removes.
    /// Err where a listing or a removal raises (the reference stops
    /// there).
    pub fn clear_probe(&self, root: &[u8]) -> Result<(), Refusal> {
        if self.windows() {
            return Err(Refusal::NotPorted);
        }
        self.clear(root).map_err(Refusal::from)
    }

    fn clear(&self, root: &[u8]) -> Result<(), Stop> {
        for name in PROBE_LISTED {
            let folder = if name.is_empty() {
                root.to_vec()
            } else {
                self.under(root, name)
            };
            let (keep_folders, keep_files) = probe_children(name);
            if self.is_link(&folder)? || !self.is_dir(&folder)? {
                continue;
            }
            let listed = os_path(&self.os(&folder)).ok_or(Stop::Caught(PyErr::Value))?;
            for found in std::fs::read_dir(listed)? {
                let found = found?;
                let entry = name_bytes(&found.file_name());
                let path = paths::join(self.flavour, &folder, &entry);
                // `DirEntry.is_dir(follow_symlinks=False)`: the listing's
                // type, `lstat` only where it is unknown, False where that
                // finds nothing.
                let kind = match found.file_type() {
                    Ok(t) => Some(t),
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
                    Err(e) => return Err(e.into()),
                };
                let is_dir = kind.is_some_and(|t| t.is_dir());
                let is_file = kind.is_some_and(|t| t.is_file());
                let named = |set: &[&str]| set.iter().any(|n| n.as_bytes() == entry.as_slice());
                if (is_dir && named(&keep_folders)) || (is_file && named(&keep_files)) {
                    continue;
                }
                remove(&self.os(&path), is_dir)?;
            }
        }
        Ok(())
    }

    /// `_probe_repository()`: the probe, resolved, and its stamps taken
    /// before its content was checked; None where it cannot be built to
    /// hold what it should in two tries, or is a link.
    pub fn probe_repository(&self, now: f64) -> Result<Option<Probe>, NotPorted> {
        self.probe_repository_at(&mut || now)
    }

    /// [`Place::probe_repository`] with `time.time()` read through `wall`
    /// where the sweep reads it (none where the state directory is no
    /// folder).
    pub fn probe_repository_at(
        &self,
        wall: &mut dyn FnMut() -> f64,
    ) -> Result<Option<Probe>, NotPorted> {
        if self.windows() {
            return Err(NotPorted);
        }
        let built = (|| -> Result<Option<Probe>, Stop> {
            let root = self.under(self.state_dir, &probe_folder());
            self.sweep(wall)?;
            for _attempt in 0..2 {
                make_owner_only(&self.os(&root))?;
                if self.is_link(&root)? {
                    return Ok(None);
                }
                for name in PROBE_FOLDERS {
                    make_owner_only(&self.os(&self.under(&root, name)))?;
                }
                for (name, data) in PROBE_FILES {
                    let path = self.under(&root, name);
                    let same = match self.read_upto(&path, data.len() + 1) {
                        Ok(got) => got == data,
                        Err(PyErr::FileNotFound) => false,
                        Err(e) => return Err(e.into()),
                    };
                    if !same {
                        self.write(&path, data)?;
                    }
                }
                let real = realpath(self.fs, self.flavour, &root, self.cwd)?;
                let before = self.probe_stamps(&real)?;
                if self.probe_holds(&real)? {
                    return Ok(Some((real, before)));
                }
                self.clear(&root)?;
            }
            Ok(None)
        })();
        settled(built).map(Option::flatten)
    }

    /// `atomic_write_bytes(path, data, mode_before_rename=0o600)`.
    fn write(&self, path: &[u8], data: &[u8]) -> Result<(), Stop> {
        let p = os_path(&self.os(path)).ok_or(Stop::Caught(PyErr::Value))?;
        replace_retrying(&p, data)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o600));
        }
        Ok(())
    }
}
