//! The key ring's files (`log.KeyRing` over a keys directory): the id
//! check, the paths, the current key read and retired, the origin anchor
//! written, named and replaced, and the binding read and moved.
//!
//! The in-memory [`KeyRing`] answers what the files say; this module
//! reads and writes them. A file the reference writes with
//! `atomic_write_bytes(..., mode_before_rename=0o600)` is written by
//! `store::write::replace_retrying` (a 0600 temporary file, synced, renamed
//! over the name). The keys directory's locks (`flock_excl` on
//! `<file>.lock`) go through [`RingLock`].
//!
//! The store writer and the ring share the decoder and path comparisons
//! in [`binding_path`]. The store writer still has its own current-key reader.

pub mod binding_path;
use binding_path::{BindingError, BindingPath, HOST};
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use super::{fingerprint, HistoryOrigin, Key, KeyRing, RETIRED_PREFIX_CHARS};
use crate::json::{self, DumpOptions};
use crate::pyvalue::{py_str, repr_str, PyError, PyValue};
use crate::store::create::owner_only_dir;
use crate::store::write::replace_retrying;
use crate::unicode;

/// `KeyUnreadableError`: the current key file is there and cannot be
/// read. `errno` is the cause's, `reason` what the read said (its
/// `strerror`) or what the caller knows better (a dangling link).
#[derive(Debug)]
pub struct KeyUnreadable {
    pub path: PathBuf,
    pub errno: Option<i32>,
    pub reason: String,
}

impl KeyUnreadable {
    fn new(path: PathBuf, cause: &io::Error, reason: Option<String>) -> Self {
        // `cause.strerror or str(cause) or type(cause).__name__`.
        let reason = reason.unwrap_or_else(|| {
            cause
                .raw_os_error()
                .and_then(bettermemory_sys::strerror)
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| cause.to_string())
        });
        KeyUnreadable {
            path,
            errno: cause.raw_os_error(),
            reason,
        }
    }
}

impl std::fmt::Display for KeyUnreadable {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "the key file {} is there and cannot be read ({}); \
             the store is not rekeyed over it",
            self.path.display(),
            self.reason
        )
    }
}

/// What a ring operation raised: `KeyUnreadableError`, another OSError,
/// or the reference's own exception (RecursionError from an anchor or a
/// binding nested past `json::MAX_DEPTH`, which no `except` catches).
#[derive(Debug)]
pub enum RingError {
    KeyUnreadable(KeyUnreadable),
    Os(io::Error),
    Py(PyError),
}

impl From<io::Error> for RingError {
    fn from(e: io::Error) -> Self {
        RingError::Os(e)
    }
}

impl From<PyError> for RingError {
    fn from(e: PyError) -> Self {
        RingError::Py(e)
    }
}

impl From<KeyUnreadable> for RingError {
    fn from(e: KeyUnreadable) -> Self {
        RingError::KeyUnreadable(e)
    }
}

/// Why `KeyRing(keys_dir, store_id)` refused the id: ValueError with its
/// text, or what `str(store_id)` raised.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BadId {
    ValueError(String),
    Py(PyError),
}

/// `flock_excl(path)`: an exclusive lock held on `<path>.lock` while the
/// guard lives.
pub trait RingLock {
    type Guard;
    fn lock(&self, lock_path: &Path) -> io::Result<Self::Guard>;
}

/// The lock `store::write`'s `open_lock` takes: the lock file created 0600
/// or opened, kept, and locked with `File::lock`. That is `flock_excl`'s
/// POSIX branch; on Windows the reference locks byte 0 with
/// `msvcrt.locking`, which this does not interoperate with.
#[derive(Clone, Copy, Debug, Default)]
pub struct FileLock;

impl RingLock for FileLock {
    type Guard = fs::File;
    fn lock(&self, lock_path: &Path) -> io::Result<fs::File> {
        let mut options = fs::OpenOptions::new();
        options.read(true).write(true).create(true).truncate(false);
        #[cfg(unix)]
        std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
        let file = options.open(lock_path)?;
        file.lock()?;
        Ok(file)
    }
}

/// `STORE_ID_PATTERN.fullmatch(id)`: `[0-9a-f]{32}`, by hand (no choice
/// in the pattern).
fn is_store_id(id: &[u32]) -> bool {
    id.len() == 32
        && id
            .iter()
            .all(|&c| (0x30..=0x39).contains(&c) || (0x61..=0x66).contains(&c))
}

fn with_suffix(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(suffix);
    PathBuf::from(name)
}

/// `os.replace(src, dst)` as `_fsutil.replace_atomic` runs it: on Windows
/// a PermissionError is tried again, five attempts in all, the pause
/// doubling from 10 ms.
fn rename_retrying(src: &Path, dst: &Path) -> io::Result<()> {
    #[cfg(windows)]
    {
        let mut delay = std::time::Duration::from_millis(10);
        for _ in 1..5 {
            match fs::rename(src, dst) {
                Err(e) if e.kind() == io::ErrorKind::PermissionDenied => {
                    std::thread::sleep(delay);
                    delay = delay.saturating_mul(2);
                }
                other => return other,
            }
        }
    }
    fs::rename(src, dst)
}

/// `Path.exists()` as 3.13 has it: an OSError whose errno is ENOENT,
/// ENOTDIR, EBADF or ELOOP (or whose winerror is 21, 123 or 1921) and a
/// ValueError read as absent; any other OSError raises.
fn path_exists(path: &Path) -> io::Result<bool> {
    match fs::metadata(path) {
        Ok(_) => Ok(true),
        Err(e) => {
            let code = e.raw_os_error();
            #[cfg(unix)]
            let ignored = matches!(code, Some(2 | 20 | 9)) || is_link_loop(code);
            #[cfg(not(unix))]
            let ignored =
                e.kind() == io::ErrorKind::NotFound || matches!(code, Some(3 | 21 | 123 | 1921));
            if ignored || (code.is_none() && e.kind() == io::ErrorKind::InvalidInput) {
                Ok(false)
            } else {
                Err(e)
            }
        }
    }
}

#[cfg(unix)]
fn is_link_loop(code: Option<i32>) -> bool {
    #[cfg(target_vendor = "apple")]
    let eloop = 62;
    #[cfg(not(target_vendor = "apple"))]
    let eloop = 40;
    code == Some(eloop)
}

/// `atomic_write_bytes(path, data, mode_before_rename=0o600)`: the file
/// is 0600 whatever the umask (the reference's fchmod and its post-rename
/// chmod), never wider at the visible path.
fn write_private(path: &Path, data: &[u8]) -> io::Result<()> {
    replace_retrying(path, data)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        // Best effort, as the reference's chmod is.
        let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

/// Read the link target as Python code points, dropping the verbatim prefix
/// used by Windows readlink. POSIX surrogateescaped bytes remain distinct.
fn dangling_reason(path: &Path) -> Option<Vec<u32>> {
    if !fs::symlink_metadata(path).is_ok_and(|m| m.is_symlink()) || fs::metadata(path).is_ok() {
        return None;
    }
    let target = crate::fsutil::fsdecode(fs::read_link(path).ok()?.as_os_str());
    let mut text = unicode::cps("it is a symbolic link to ");
    let unc = unicode::cps(r"\\?\UNC\");
    let verbatim = unicode::cps(r"\\?\");
    if let Some(tail) = target.strip_prefix(unc.as_slice()) {
        text.extend(unicode::cps(r"\\"));
        text.extend(tail);
    } else {
        text.extend(target.strip_prefix(verbatim.as_slice()).unwrap_or(&target));
    }
    text.extend(unicode::cps(", which is not there"));
    Some(text)
}

fn lossy_reason(text: &[u32]) -> String {
    text.iter()
        .map(|&cp| char::from_u32(cp).unwrap_or(char::REPLACEMENT_CHARACTER))
        .collect()
}

/// FileIO on Windows checks UCRT errno after open/read. In particular a
/// path exceeding the Windows filename limit is reported as ENOENT there.
pub(crate) fn key_not_found(error: &io::Error) -> bool {
    #[cfg(windows)]
    if let Some(code) = error.raw_os_error() {
        return bettermemory_sys::windows_file_error::crt(code).0 == 2;
    }
    error.kind() == io::ErrorKind::NotFound
}

/// The existing writer's string projection of a dangling link.
pub fn dangling_link(path: &Path) -> Option<String> {
    dangling_reason(path).map(|reason| lossy_reason(&reason))
}

#[derive(Debug)]
pub(crate) struct CurrentReadError {
    pub path: PathBuf,
    pub cause: io::Error,
    pub dangling: Option<Vec<u32>>,
}
impl CurrentReadError {
    pub(crate) fn legacy(&self) -> KeyUnreadable {
        KeyUnreadable::new(
            self.path.clone(),
            &self.cause,
            self.dangling.as_deref().map(lossy_reason),
        )
    }
}

/// `json.dumps(value)`, as bytes.
fn dumps(value: &PyValue) -> io::Result<Vec<u8>> {
    json::dumps(value, DumpOptions::DEFAULT)
        .map(|t| json::utf8_text(&t).into_bytes())
        .map_err(|_| io::Error::other("a value json.dumps refuses"))
}

/// `HistoryOrigin.to_dict()`.
fn origin_dict(origin: &HistoryOrigin) -> PyValue {
    let mut items = vec![
        (PyValue::str("seq"), PyValue::int(i128::from(origin.seq))),
        (PyValue::str("mac"), PyValue::str(&origin.mac)),
    ];
    if let Some(named) = &origin.key_fingerprint {
        items.push((PyValue::str("key_fingerprint"), PyValue::str(named)));
    }
    PyValue::Dict(items)
}

/// `Path(text)` in the host's flavour.
fn host_path(text: &str) -> PathBuf {
    #[cfg(unix)]
    let flavour = crate::config::Flavour::Posix;
    #[cfg(not(unix))]
    let flavour = crate::config::Flavour::Windows;
    PathBuf::from(crate::config::pure_path(text, flavour))
}

/// `Path(store_file).resolve()`, spelled as `str()` spells it: on POSIX
/// `os.path.realpath` (non-strict, 3.13) with `cwd` as `os.getcwd()`.
#[cfg(unix)]
pub(crate) fn resolve(store_file: &Path, cwd: &[u8]) -> Result<BindingPath, BindingError> {
    use std::os::unix::ffi::{OsStrExt, OsStringExt};
    // The non-strict POSIX walker ignores empty and dot components itself.
    let bytes = store_file.as_os_str().as_bytes();
    let real = crate::fsdata::realpath::realpath(&crate::fsdata::StdFs, HOST, bytes, cwd)
        .map_err(|_| BindingError::Value)?;
    Ok(BindingPath::resolved(Path::new(
        &std::ffi::OsString::from_vec(real),
    )))
}

/// On Windows `ntpath.realpath` (non-strict) as `Path.resolve` has it:
/// the path made absolute, its longest existing head canonicalized (the
/// verbatim prefix dropped, `Path.resolve` spells none) and the part that
/// is not there joined back as given.
#[cfg(not(unix))]
pub(crate) fn resolve(store_file: &Path, _cwd: &[u8]) -> Result<BindingPath, BindingError> {
    let absolute = std::path::absolute(store_file)?;
    let mut head = absolute.as_path();
    let mut tail: Vec<&std::ffi::OsStr> = Vec::new();
    let real = loop {
        match fs::canonicalize(head) {
            Ok(real) => break real,
            Err(e) => match (head.parent(), head.file_name()) {
                (Some(parent), Some(name)) => {
                    tail.push(name);
                    head = parent;
                }
                _ => return Err(e.into()),
            },
        }
    };
    let real = tail.iter().rev().fold(real, |acc, name| acc.join(name));
    Ok(BindingPath::resolved(&real))
}

pub(crate) fn validated_store_id(store_id: &PyValue) -> Result<String, BadId> {
    if let PyValue::Str(id) = store_id {
        if is_store_id(id) {
            return Ok(unicode::to_string(id).expect("hex store id"));
        }
    }
    let text = py_str(store_id).map_err(BadId::Py)?;
    let head: Vec<u32> = text.iter().copied().take(40).collect();
    let shown = unicode::to_string(&repr_str(&head)).unwrap_or_default();
    Err(BadId::ValueError(format!(
        "not a store id: {shown}; a store id is 32 lowercase hex digits, \
             and the key ring names its files after it"
    )))
}

/// One store's key ring on disk (`log.KeyRing`): the keys directory and
/// the store id its files are named after.
#[derive(Clone, Debug)]
pub struct KeyFiles<L: RingLock = FileLock> {
    keys_dir: PathBuf,
    store_id: String,
    lock: L,
}

impl KeyFiles<FileLock> {
    /// `KeyRing(keys_dir, store_id)`: ValueError for an id of any shape
    /// but 32 lowercase hex digits.
    pub fn new(keys_dir: &Path, store_id: &PyValue) -> Result<Self, BadId> {
        Self::with_lock(keys_dir, store_id, FileLock)
    }
}

impl<L: RingLock> KeyFiles<L> {
    pub fn with_lock(keys_dir: &Path, store_id: &PyValue, lock: L) -> Result<Self, BadId> {
        let id = validated_store_id(store_id)?;
        // Path(keys_dir) parses separators and dot components.
        let keys_dir = match keys_dir.to_str() {
            Some(text) => host_path(text),
            None => keys_dir.to_owned(),
        };
        Ok(Self {
            keys_dir,
            store_id: id,
            lock,
        })
    }

    pub fn keys_dir(&self) -> &Path {
        &self.keys_dir
    }

    pub fn store_id(&self) -> &str {
        &self.store_id
    }

    fn file(&self, suffix: &str) -> PathBuf {
        self.keys_dir.join(format!("{}.{suffix}", self.store_id))
    }

    pub fn current_path(&self) -> PathBuf {
        self.file("key")
    }

    pub fn head_path(&self) -> PathBuf {
        self.file("head")
    }

    pub fn origin_path(&self) -> PathBuf {
        self.file("origin")
    }

    pub fn binding_path(&self) -> PathBuf {
        self.file("path")
    }

    /// `<id>.<the fingerprint's first 16 characters>.key`.
    pub fn retired_path(&self, key_fingerprint: &str) -> PathBuf {
        let prefix: String = key_fingerprint.chars().take(RETIRED_PREFIX_CHARS).collect();
        self.file(&format!("{prefix}.key"))
    }

    /// `ensure_owner_only_dir(keys_dir, parents=True)`.
    pub fn ensure_dir(&self) -> io::Result<()> {
        owner_only_dir(&self.keys_dir)
    }

    fn hold(&self, path: &Path) -> io::Result<L::Guard> {
        self.lock.lock(&with_suffix(path, ".lock"))
    }

    /// `KeyRing.load_current`: the key, None when there is no key file;
    /// [`KeyUnreadable`] when it is there and cannot be read, or when the
    /// key file or the keys directory is a link to nothing.
    pub fn load_current(&self) -> Result<Option<Key>, KeyUnreadable> {
        self.load_current_exact().map_err(|e| e.legacy())
    }

    pub(crate) fn load_current_exact(&self) -> Result<Option<Key>, CurrentReadError> {
        let path = self.current_path();
        match fs::read(&path) {
            Ok(bytes) => Ok(Some(Key::new(bytes))),
            Err(cause) if key_not_found(&cause) => {
                match dangling_reason(&path).or_else(|| dangling_reason(&self.keys_dir)) {
                    Some(reason) => Err(CurrentReadError {
                        path,
                        cause,
                        dangling: Some(reason),
                    }),
                    None => Ok(None),
                }
            }
            Err(cause) => Err(CurrentReadError {
                path,
                cause,
                dangling: None,
            }),
        }
    }

    /// `KeyRing.retire_current`: the current key moved aside under its
    /// fingerprint (removed when that name is taken), its fingerprint
    /// returned; None when there is no current key.
    pub fn retire_current(&self) -> Result<Option<String>, RingError> {
        let Some(key) = self.load_current()? else {
            return Ok(None);
        };
        let key_fingerprint = fingerprint(key.bytes());
        let target = self.retired_path(&key_fingerprint);
        if path_exists(&target)? {
            fs::remove_file(self.current_path())?;
        } else {
            rename_retrying(&self.current_path(), &target)?;
        }
        Ok(Some(key_fingerprint))
    }

    /// `KeyRing.read_origin`: None when the file cannot be read or does
    /// not read as an anchor; Err(RecursionError) passes through.
    pub fn read_origin(&self) -> Result<Option<HistoryOrigin>, PyError> {
        let Ok(bytes) = fs::read(self.origin_path()) else {
            return Ok(None);
        };
        KeyRing {
            origin_file: Some(bytes),
            ..KeyRing::default()
        }
        .read_origin()
    }

    fn write_origin_file(&self, origin: &HistoryOrigin) -> io::Result<()> {
        write_private(&self.origin_path(), &dumps(&origin_dict(origin))?)
    }

    /// `KeyRing.write_origin`: the anchor written once, under its lock.
    /// False when one is there; a file that does not parse as one is
    /// replaced; one that cannot be read raises and stays.
    pub fn write_origin(
        &self,
        seq: i64,
        mac: &str,
        key_fingerprint: Option<&str>,
    ) -> Result<bool, RingError> {
        self.ensure_dir()?;
        let path = self.origin_path();
        let _guard = self.hold(&path)?;
        if self.read_origin()?.is_some() {
            return Ok(false);
        }
        match fs::read(&path) {
            Err(e) if e.kind() != io::ErrorKind::NotFound => return Err(e.into()),
            _ => {}
        }
        self.write_origin_file(&HistoryOrigin {
            seq,
            mac: mac.to_owned(),
            key_fingerprint: key_fingerprint.map(str::to_owned),
        })?;
        Ok(true)
    }

    /// `KeyRing.name_origin_key`: the key named on an anchor that is still
    /// `origin` (its seq and MAC) and names none; true when written.
    pub fn name_origin_key(
        &self,
        origin: &HistoryOrigin,
        key_fingerprint: &str,
    ) -> Result<bool, RingError> {
        self.ensure_dir()?;
        let _guard = self.hold(&self.origin_path())?;
        let Some(current) = self.read_origin()? else {
            return Ok(false);
        };
        if current.key_fingerprint.is_some()
            || (current.seq, &current.mac) != (origin.seq, &origin.mac)
        {
            return Ok(false);
        }
        self.write_origin_file(&HistoryOrigin {
            key_fingerprint: Some(key_fingerprint.to_owned()),
            ..current
        })?;
        Ok(true)
    }

    /// `KeyRing.replace_origin`: `origin` installed, or the anchor removed
    /// for None; the one it replaced returned.
    pub fn replace_origin(
        &self,
        origin: Option<&HistoryOrigin>,
    ) -> Result<Option<HistoryOrigin>, RingError> {
        self.ensure_dir()?;
        let path = self.origin_path();
        let _guard = self.hold(&path)?;
        let before = self.read_origin()?;
        match origin {
            None => match fs::remove_file(&path) {
                Err(e) if e.kind() != io::ErrorKind::NotFound => return Err(e.into()),
                _ => {}
            },
            Some(origin) => self.write_origin_file(origin)?,
        }
        Ok(before)
    }

    /// `KeyRing.read_binding`: the bound store file, None when there is
    /// none or it does not read as one (a non-empty absolute string `path`
    /// with no NUL, the last `path` key winning as in a dict); an OSError
    /// other than not-found, and RecursionError, raise.
    pub fn read_binding(&self) -> Result<Option<PathBuf>, RingError> {
        Ok(self
            .read_binding_exact()?
            .map(|path| path.exact_host())
            .transpose()?)
    }

    /// Read the original Python spelling, including values the OS cannot hold.
    pub fn read_binding_exact(&self) -> Result<Option<BindingPath>, BindingError> {
        binding_path::read(&self.binding_path(), HOST)
    }

    /// `KeyRing.rebind_store`: the ring bound to `store_file` resolved
    /// (`os.getcwd()` answering `cwd`) whatever it was bound to, under the
    /// binding's lock; the file it was bound to before returned. A binding
    /// that cannot be read reads as none.
    pub fn rebind_store(
        &self,
        store_file: &Path,
        cwd: &[u8],
    ) -> Result<Option<PathBuf>, RingError> {
        Ok(self.rebind_with(store_file, cwd, |previous| previous.exact_host())?)
    }

    /// Rebind while returning the exact previous spelling recorded by Python.
    pub fn rebind_store_exact(
        &self,
        store_file: &Path,
        cwd: &[u8],
    ) -> Result<Option<BindingPath>, BindingError> {
        self.rebind_with(store_file, cwd, Ok)
    }

    fn rebind_with<T>(
        &self,
        store_file: &Path,
        cwd: &[u8],
        previous_value: impl FnOnce(BindingPath) -> Result<T, BindingError>,
    ) -> Result<Option<T>, BindingError> {
        let resolved = resolve(store_file, cwd)?;
        self.ensure_dir()?;
        let path = self.binding_path();
        let _guard = self.hold(&path)?;
        let previous = match self.read_binding_exact() {
            Err(BindingError::Os(_)) => None,
            value => value?,
        };
        let unchanged = previous
            .as_ref()
            .is_some_and(|p| p.lexical_eq(&resolved, HOST));
        let returned = previous.map(previous_value).transpose()?;
        if !unchanged {
            write_private(&path, resolved.json()?.as_bytes())?;
        }
        Ok(returned)
    }
}
