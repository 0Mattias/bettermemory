//! A store file read without writing to it (`store_schema.connect_read_only`,
//! the opening checks of `store_open._open_connected` and
//! `store_versions.check_versions`, `store_rows`' readers, the log as
//! `log.iter_rows` reads it, and `store_index.query_candidates`).
//!
//! How a file is opened, as the reference opens it for its read-only
//! commands:
//!
//! - a file in rollback-journal mode with no `-wal` beside it is read in
//!   place with `mode=ro` (SQLite creates nothing beside such a file);
//! - a file whose `-wal` and `-shm` are both there (a daemon has it open,
//!   or one crashed with it open) is read in place with `mode=ro`, through
//!   the `-shm` it shares with the writer: the frames the `-wal` holds are
//!   read, committed ones only, and nothing is checkpointed. SQLite takes
//!   its read locks in that `-shm`, as any reader does, and when the
//!   `-shm` is stale (its writer died) it rebuilds the wal-index there: the
//!   one change a read can make, the reference's `mode=ro` read making the
//!   same on the same file; the store file and the `-wal` are unchanged;
//! - a WAL-mode file without both (a closed store), or a `-wal` without its
//!   `-shm`, is copied first into a private directory (owner-only, under
//!   `std::env::temp_dir()`), file and `-wal` together, taken again when
//!   either changed while it was copied (inode, size, mtime), at most three
//!   times; the copy is read `mode=ro&immutable=1`, or `mode=ro` when a
//!   `-wal` came with it. On POSIX the copy's names are removed once the
//!   admitted connection holds the files open; the directory, with the
//!   `.lock` its reader locks for its life, is removed when the
//!   [`StoreFile`] drops, and a dead reader's is swept at the next open
//!   ([`sweep_private_copies`]).
//!
//! Every file is admitted (`admission`, `store_admission.admit_store`)
//! before any table is read: a view, a trigger, a virtual table, a computed
//! column or default, a check or foreign key, an unexpected index, or one
//! of the store's own objects with another text or shape is refused, the
//! schema read under a 2 s deadline with no value over 16 KiB, meta under
//! the same deadline. After that SQLite's own limits are the reference's
//! (its defaults): an admitted file's tables compute nothing, so each read
//! costs what its rows hold.

pub mod admission;
pub mod keys;

use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use rusqlite::types::ValueRef;
use rusqlite::{Connection, OpenFlags};

use crate::datetime::{self, DateTime};
use crate::json;
use crate::lexicon;
use crate::log::{KeyRing, LogRow, LogSource, VerifyError};
use crate::pyvalue::{repr_str, PyError, PyStr, PyValue};
use crate::search::Record;
use crate::{float_repr, unicode};

/// `store_schema.SCHEMA_VERSION`, `SCHEMA_REVISION`.
pub const SCHEMA_VERSION: i64 = 1;
pub const SCHEMA_REVISION: i64 = 6;
const SNAPSHOT_TRIES: usize = 3;

/// Why a store could not be opened or read.
#[derive(Debug)]
pub enum StoreError {
    Io(io::Error),
    Sqlite(rusqlite::Error),
    /// The file changed every time it was copied to be read.
    Changed,
    /// A text column that is not UTF-8 (Python's sqlite3 raises
    /// OperationalError "Could not decode to UTF-8").
    NotUtf8 {
        table: &'static str,
        column: &'static str,
    },
    /// A column of a type the reader cannot take (`int()` or `str()`
    /// would raise, or a NULL in a column the reference reads as text).
    Type {
        table: &'static str,
        column: &'static str,
        found: &'static str,
    },
    /// `ValueError` from `_open_connected`: the message.
    Refused(String),
    /// `StoreRefusedError` / `LaterStoreError` (`store_admission`).
    Admission(admission::Refusal),
    /// `store_versions.StoreVersionError`.
    Version(StoreVersionError),
    /// The reference raises (a JSON column, a timestamp): which.
    Py(PyError),
}

impl From<io::Error> for StoreError {
    fn from(e: io::Error) -> Self {
        StoreError::Io(e)
    }
}

impl From<rusqlite::Error> for StoreError {
    fn from(e: rusqlite::Error) -> Self {
        StoreError::Sqlite(e)
    }
}

impl From<PyError> for StoreError {
    fn from(e: PyError) -> Self {
        StoreError::Py(e)
    }
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::Io(e) => write!(f, "{e}"),
            StoreError::Sqlite(e) => write!(f, "{e}"),
            StoreError::Changed => write!(f, "changed every time it was copied to be read"),
            StoreError::NotUtf8 { table, column } => {
                write!(f, "{table}.{column}: Could not decode to UTF-8")
            }
            StoreError::Type {
                table,
                column,
                found,
            } => {
                write!(f, "{table}.{column}: a {found} value")
            }
            StoreError::Refused(m) => write!(f, "{m}"),
            StoreError::Admission(r) => write!(f, "is not opened: {}", r.why),
            StoreError::Version(e) => write!(f, "{}", e.message),
            StoreError::Py(e) => write!(f, "{}", e.name()),
        }
    }
}

/// `store_versions.StoreVersionError`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoreVersionError {
    pub message: String,
    pub key: Option<&'static str>,
    pub needed: Option<i64>,
}

/// `store_versions.VersionVerdict` of a reader's open (`writing` False).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Verdict {
    /// The store's minimum writer revision is above this build's: the
    /// reference sets `query_only`; `reason` is its message.
    pub read_only: Option<String>,
    pub revision: i64,
    pub min_reader: i64,
    pub min_writer: i64,
}

/// A value as SQLite returned it.
#[derive(Clone, Debug, PartialEq)]
pub enum SqlValue {
    Null,
    Integer(i64),
    Real(f64),
    Text(String),
    Blob(Vec<u8>),
}

impl SqlValue {
    fn type_name(&self) -> &'static str {
        match self {
            SqlValue::Null => "NULL",
            SqlValue::Integer(_) => "INTEGER",
            SqlValue::Real(_) => "REAL",
            SqlValue::Text(_) => "TEXT",
            SqlValue::Blob(_) => "BLOB",
        }
    }

    /// Python's `str()` of the value sqlite3 gives.
    pub fn py_str(&self) -> String {
        match self {
            SqlValue::Null => "None".to_owned(),
            SqlValue::Integer(i) => i.to_string(),
            SqlValue::Real(f) => float_repr::repr(*f),
            SqlValue::Text(t) => t.clone(),
            SqlValue::Blob(b) => bytes_repr(b),
        }
    }
}

/// `repr(bytes)`.
fn bytes_repr(b: &[u8]) -> String {
    let quote = if b.contains(&b'\'') && !b.contains(&b'"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::from("b");
    out.push(quote);
    for &c in b {
        match c {
            b'\\' => out.push_str("\\\\"),
            b'\t' => out.push_str("\\t"),
            b'\n' => out.push_str("\\n"),
            b'\r' => out.push_str("\\r"),
            _ if c as char == quote => {
                out.push('\\');
                out.push(quote);
            }
            0x20..=0x7e => out.push(c as char),
            _ => out.push_str(&format!("\\x{c:02x}")),
        }
    }
    out.push(quote);
    out
}

fn value_of(
    v: ValueRef<'_>,
    table: &'static str,
    column: &'static str,
) -> Result<SqlValue, StoreError> {
    Ok(match v {
        ValueRef::Null => SqlValue::Null,
        ValueRef::Integer(i) => SqlValue::Integer(i),
        ValueRef::Real(f) => SqlValue::Real(f),
        ValueRef::Text(t) => SqlValue::Text(
            std::str::from_utf8(t)
                .map_err(|_| StoreError::NotUtf8 { table, column })?
                .to_owned(),
        ),
        ValueRef::Blob(b) => SqlValue::Blob(b.to_vec()),
    })
}

/// A value of any column, for a generic dump.
pub fn sql_value(v: ValueRef<'_>) -> Result<SqlValue, StoreError> {
    value_of(v, "?", "?")
}

static SCRATCH_COUNTER: AtomicU64 = AtomicU64::new(0);

fn beside(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path
        .file_name()
        .map(|n| n.to_os_string())
        .unwrap_or_default();
    name.push(suffix);
    path.with_file_name(name)
}

/// `_says_wal`: the header's file-format bytes (18, 19) say WAL.
fn says_wal(path: &Path) -> io::Result<bool> {
    use std::io::Read;
    let mut header = [0u8; 20];
    let mut file = fs::File::open(path)?;
    let mut read = 0;
    while read < 20 {
        let n = file.read(&mut header[read..])?;
        if n == 0 {
            return Ok(false);
        }
        read += n;
    }
    Ok(header[18] == 2 || header[19] == 2)
}

type Stamp = Option<(u64, u64, i128)>;

fn stamp(path: &Path) -> io::Result<Stamp> {
    match fs::metadata(path) {
        Ok(m) => {
            #[cfg(unix)]
            {
                use std::os::unix::fs::MetadataExt;
                Ok(Some((
                    m.ino(),
                    m.size(),
                    i128::from(m.mtime()) * 1_000_000_000 + i128::from(m.mtime_nsec()),
                )))
            }
            #[cfg(not(unix))]
            {
                let mtime = m
                    .modified()
                    .ok()
                    .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                    .map_or(0, |d| d.as_nanos() as i128);
                Ok(Some((0, m.len(), mtime)))
            }
        }
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e),
    }
}

/// The prefix of a private copy's directory under the temp dir; the
/// reference's own (`bettermemory-read-`, `tempfile.mkdtemp`) is never
/// swept here.
pub const SCRATCH_PREFIX: &str = "bettermemory-native-read-";
/// The lock file in a private directory, locked by its reader for the
/// reader's life: a directory whose lock can be taken has no live reader.
const SCRATCH_LOCK: &str = ".lock";
/// A directory with no lock file is swept only when older than this (one
/// whose reader has not locked it yet is younger).
const UNLOCKED_GRACE: std::time::Duration = std::time::Duration::from_secs(3600);

/// The prefix a private directory is made under before it is locked and
/// renamed to [`SCRATCH_PREFIX`]: a sweeper never sees a directory whose
/// lock is not held yet. One left by a reader killed in that instant holds
/// nothing but an empty `.lock` and is swept after [`UNLOCKED_GRACE`].
const UNLOCKED_PREFIX: &str = "bettermemory-native-new-";

/// A private directory and the lock that says its reader is alive.
struct Scratch {
    dir: PathBuf,
    _lock: fs::File,
    /// The copy when its name is already gone: SQLite reads it through
    /// `/dev/fd/N`, which this descriptor keeps open (macOS and FreeBSD
    /// only, [`copy_unnamed`]).
    #[cfg(any(target_vendor = "apple", target_os = "freebsd"))]
    _held: Option<fs::File>,
}

/// An I/O error with the step that failed and what it touched named, never
/// a host path: a failure on a platform no one runs here says which call.
fn step<T>(what: &str, result: io::Result<T>) -> io::Result<T> {
    result.map_err(|e| io::Error::new(e.kind(), format!("{what}: {e}")))
}

/// A new 0700 directory under the temp dir named with `prefix`, made with
/// `mkdir`, which fails when the name exists: never a directory another
/// user made first.
fn exclusive_dir(prefix: &str) -> io::Result<PathBuf> {
    let base = std::env::temp_dir();
    for _ in 0..100 {
        let n = SCRATCH_COUNTER.fetch_add(1, Ordering::Relaxed);
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.subsec_nanos());
        let dir = base.join(format!("{prefix}{}-{nanos:x}-{n}", std::process::id()));
        #[cfg(unix)]
        let created = {
            use std::os::unix::fs::DirBuilderExt;
            fs::DirBuilder::new().mode(0o700).create(&dir)
        };
        #[cfg(not(unix))]
        let created = fs::DirBuilder::new().create(&dir);
        match created {
            Ok(()) => return Ok(dir),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(e) => return step("make the private directory", Err(e)),
        }
    }
    Err(io::Error::new(
        io::ErrorKind::AlreadyExists,
        "no private directory",
    ))
}

fn owner_only() -> fs::OpenOptions {
    let mut options = fs::OpenOptions::new();
    options.read(true).write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options
}

/// A private directory, locked before it carries the name a sweeper looks
/// at: made under [`UNLOCKED_PREFIX`], its `.lock` (0600) created and
/// locked, then renamed.
///
/// On Windows a directory that holds an open file cannot be renamed (the
/// first CI run's "Access is denied"), so there the directory is made under
/// its final name and locked in place: a sweeper that meets it in the
/// instant before its lock is taken finds an empty `.lock` it can lock and
/// tries to remove the directory, which Windows refuses while the reader
/// holds `.lock` open.
#[cfg(not(unix))]
fn private_dir() -> io::Result<Scratch> {
    let dir = exclusive_dir(SCRATCH_PREFIX)?;
    let locked = step(
        "create the private directory's lock file",
        owner_only().open(dir.join(SCRATCH_LOCK)),
    )
    .and_then(|f| step("lock the private directory", f.lock().map(|()| f)));
    match locked {
        Ok(lock) => Ok(Scratch { dir, _lock: lock }),
        Err(e) => {
            let _ = fs::remove_dir_all(&dir);
            Err(e)
        }
    }
}

#[cfg(unix)]
fn private_dir() -> io::Result<Scratch> {
    let made = exclusive_dir(UNLOCKED_PREFIX)?;
    let locked = step(
        "create the private directory's lock file",
        owner_only().open(made.join(SCRATCH_LOCK)),
    )
    .and_then(|f| step("lock the private directory", f.lock().map(|()| f)));
    let lock = match locked {
        Ok(lock) => lock,
        Err(e) => {
            let _ = fs::remove_dir_all(&made);
            return Err(e);
        }
    };
    let name = made
        .file_name()
        .and_then(|n| n.to_str())
        .and_then(|n| n.strip_prefix(UNLOCKED_PREFIX))
        .map(|rest| format!("{SCRATCH_PREFIX}{rest}"))
        .expect("made name");
    let dir = made.with_file_name(name);
    // rename(2) fails on a non-empty directory of that name; an empty one
    // another user made would be replaced only if ours could, which the
    // sticky temp dir forbids.
    if let Err(e) = fs::rename(&made, &dir) {
        let _ = fs::remove_dir_all(&made);
        return step("rename the locked private directory", Err(e));
    }
    Ok(Scratch {
        dir,
        _lock: lock,
        #[cfg(any(target_vendor = "apple", target_os = "freebsd"))]
        _held: None,
    })
}

/// Remove the private directories under the temp dir that no live reader
/// holds: this user's, named with [`SCRATCH_PREFIX`], whose lock file can
/// be locked (its reader exited, or was killed, without removing it), or
/// that have no lock file and are over an hour old; and this user's
/// [`UNLOCKED_PREFIX`] directories over an hour old. Returns how many.
pub fn sweep_private_copies() -> usize {
    let base = std::env::temp_dir();
    let Ok(entries) = fs::read_dir(&base) else {
        return 0;
    };
    #[cfg(unix)]
    let me = {
        use std::os::unix::fs::MetadataExt;
        // This process's uid, from a directory it just made exclusively.
        exclusive_dir(UNLOCKED_PREFIX).ok().and_then(|probe| {
            let uid = fs::symlink_metadata(&probe).map(|m| m.uid()).ok();
            let _ = fs::remove_dir(&probe);
            uid
        })
    };
    let older = |meta: &fs::Metadata, secs: u64| {
        meta.modified()
            .ok()
            .and_then(|m| m.elapsed().ok())
            .is_some_and(|age| age > std::time::Duration::from_secs(secs))
    };
    let old = |meta: &fs::Metadata| older(meta, UNLOCKED_GRACE.as_secs());
    let mut removed = 0;
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            continue;
        };
        let unlocked = name.starts_with(UNLOCKED_PREFIX);
        if !name.starts_with(SCRATCH_PREFIX) && !unlocked {
            continue;
        }
        let Ok(meta) = fs::symlink_metadata(entry.path()) else {
            continue;
        };
        if !meta.is_dir() {
            continue;
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            if me != Some(meta.uid()) {
                continue;
            }
        }
        let dead = if unlocked {
            old(&meta)
        } else {
            match fs::OpenOptions::new()
                .write(true)
                .open(entry.path().join(SCRATCH_LOCK))
            {
                // Windows makes the directory under its swept name and locks
                // it after ([`private_dir`]): there a lock that can be taken
                // means a dead reader only once its lock file is a minute
                // old (the file is written once, when the directory is made).
                Ok(lock) => {
                    lock.try_lock().is_ok()
                        && (cfg!(unix) || lock.metadata().is_ok_and(|m| older(&m, 60)))
                }
                Err(e) if e.kind() == io::ErrorKind::NotFound => old(&meta),
                Err(_) => false,
            }
        };
        if dead && fs::remove_dir_all(entry.path()).is_ok() {
            removed += 1;
        }
    }
    removed
}

fn copy_owner_only(what: &str, source: &Path, target: &Path) -> io::Result<()> {
    let mut out = step(
        &format!("create the {what} copy"),
        owner_only().open(target),
    )?;
    let mut input = step(&format!("open the {what} to copy"), fs::File::open(source))?;
    step(&format!("copy the {what}"), io::copy(&mut input, &mut out))?;
    Ok(())
}

/// The copy of a store with no `-wal` on POSIX: its file made, opened and
/// its name removed before a byte of the store is written to it, so no
/// signal can leave the store's bytes under a name; SQLite opens it as
/// `/dev/fd/N` (immutable, so it takes no lock and wants no `-shm`).
///
/// macOS and FreeBSD only, where `/dev/fd/N` is the descriptor itself. On
/// Linux it is a symlink to `/proc/self/fd/N`, whose link text is the
/// removed name with " (deleted)" after it; SQLite's unix VFS resolves the
/// link before it opens and fails ("unable to open database file", the
/// first CI run). Linux reads the named copy instead ([`snapshot`]).
#[cfg(any(target_vendor = "apple", target_os = "freebsd"))]
fn copy_unnamed(source: &Path, dir: &Path) -> io::Result<(PathBuf, fs::File)> {
    use std::os::fd::AsRawFd;
    let named = dir.join("copy");
    let mut out = step("create the unnamed copy", owner_only().open(&named))?;
    step("remove the unnamed copy's name", fs::remove_file(&named))?;
    let mut input = step("open the store to copy", fs::File::open(source))?;
    step("copy the store", io::copy(&mut input, &mut out))?;
    let path = PathBuf::from(format!("/dev/fd/{}", out.as_raw_fd()));
    Ok((path, out))
}

/// `_snapshot`: (dir, copy, whether a -wal came, whether the copy has a
/// name), or None when a file changed while it was copied or vanished.
fn snapshot(path: &Path) -> Result<Option<(Scratch, PathBuf, bool, bool)>, StoreError> {
    let wal = beside(path, "-wal");
    let before = (stamp(path)?, stamp(&wal)?);
    let scratch = private_dir()?;
    let dir = scratch.dir.clone();
    // (the copy, whether it has a name, the descriptor of an unnamed one)
    let result = (|| -> io::Result<(PathBuf, bool, Option<fs::File>)> {
        #[cfg(any(target_vendor = "apple", target_os = "freebsd"))]
        if before.1.is_none() {
            let (copy, held) = copy_unnamed(path, &dir)?;
            return Ok((copy, false, Some(held)));
        }
        let copy = dir.join(path.file_name().unwrap_or_default());
        copy_owner_only("store", path, &copy)?;
        if before.1.is_some() {
            copy_owner_only("store's -wal", &wal, &beside(&copy, "-wal"))?;
        }
        Ok((copy, true, None))
    })();
    let (copy, named, held) = match result {
        Ok(done) => done,
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            let _ = fs::remove_dir_all(&dir);
            return Ok(None);
        }
        Err(e) => {
            let _ = fs::remove_dir_all(&dir);
            return Err(e.into());
        }
    };
    #[cfg(any(target_vendor = "apple", target_os = "freebsd"))]
    let scratch = Scratch {
        _held: held,
        ..scratch
    };
    #[cfg(not(any(target_vendor = "apple", target_os = "freebsd")))]
    debug_assert!(held.is_none());
    if (stamp(path)?, stamp(&wal)?) != before {
        let _ = fs::remove_dir_all(&dir);
        return Ok(None);
    }
    Ok(Some((scratch, copy, before.1.is_some(), named)))
}

/// `sqlite_file_uri`: `file:` and the absolute path, percent-encoded.
fn file_uri(path: &Path) -> String {
    // The path's own bytes, percent-encoded: never a lossy string, which
    // would name another file.
    #[cfg(unix)]
    let bytes = std::os::unix::ffi::OsStrExt::as_bytes(path.as_os_str()).to_vec();
    #[cfg(unix)]
    let keep: &[u8] = b"/-._~";
    // Windows: `canonicalize` gives a verbatim path (`\\?\C:\x`, or
    // `\\?\UNC\server\share\x`); SQLite wants `file:///C:/x` or
    // `file:////server/share/x`, with `/` separators and the colon kept.
    #[cfg(not(unix))]
    let bytes = {
        let text = path.to_string_lossy().into_owned();
        let text = if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
            format!(r"\\{rest}")
        } else {
            text.strip_prefix(r"\\?\").unwrap_or(&text).to_owned()
        };
        let text = text.replace('\\', "/");
        let text = if text.starts_with("//") {
            format!("//{text}")
        } else {
            format!("///{text}")
        };
        text.into_bytes()
    };
    #[cfg(not(unix))]
    let keep: &[u8] = b"/-._~:";
    let mut out = String::from("file:");
    for &b in &bytes {
        if b.is_ascii_alphanumeric() || keep.contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// One store file open for reading only.
pub struct StoreFile {
    conn: Option<Connection>,
    deadline: std::sync::Arc<admission::Deadline>,
    scratch: Option<Scratch>,
    path: PathBuf,
}

impl Drop for StoreFile {
    fn drop(&mut self) {
        // The connection closes before its copy is removed.
        drop(self.conn.take());
        if let Some(scratch) = self.scratch.take() {
            // The lock (and an unnamed copy) closes first: Windows removes
            // no file that is open.
            let dir = scratch.dir.clone();
            drop(scratch);
            let _ = fs::remove_dir_all(&dir);
        }
    }
}

impl std::fmt::Debug for StoreFile {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("StoreFile")
            .field("path", &self.path)
            .field("copy", &self.scratch.is_some())
            .finish()
    }
}

/// `store.CLI_BUSY_TIMEOUT_SECONDS`: how long a statement waits for a
/// writer's lock.
pub const BUSY_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(5);

impl StoreFile {
    /// `connect_read_only`: open the store file at `path` writing nothing
    /// to it or beside it (the module's notes), and admit it
    /// (`admission::admit`); a refused file is [`StoreError::Admission`].
    pub fn open(path: &Path) -> Result<StoreFile, StoreError> {
        sweep_private_copies();
        let resolved = step("resolve the store's path", fs::canonicalize(path))?;
        if !resolved.is_file() {
            return Err(io::Error::new(io::ErrorKind::NotFound, "no store file").into());
        }
        for _ in 0..SNAPSHOT_TRIES {
            let wal = beside(&resolved, "-wal");
            let shm = beside(&resolved, "-shm");
            let live = wal.exists() && shm.exists();
            let mut scratch = None;
            let mut target = resolved.clone();
            let mut query = "mode=ro";
            let mut has_wal = false;
            let mut named = false;
            if !live && (wal.exists() || says_wal(&resolved)?) {
                let Some((taken, copy, with_wal, with_name)) = snapshot(&resolved)? else {
                    continue;
                };
                named = with_name;
                scratch = Some(taken);
                target = copy;
                has_wal = with_wal;
                query = if has_wal {
                    "mode=ro"
                } else {
                    "mode=ro&immutable=1"
                };
            }
            let uri = format!("{}?{query}", file_uri(&target));
            let flags = OpenFlags::SQLITE_OPEN_READ_ONLY
                | OpenFlags::SQLITE_OPEN_URI
                | OpenFlags::SQLITE_OPEN_NO_MUTEX;
            // From here a failure drops `store`, which removes the copy.
            let mut store = StoreFile {
                conn: None,
                deadline: Default::default(),
                scratch,
                path: path.to_path_buf(),
            };
            let mut conn = Connection::open_with_flags(&uri, flags)?;
            conn.busy_timeout(BUSY_TIMEOUT)?;
            store.deadline = admission::install(&mut conn)?;
            store.conn = Some(conn);
            match admission::admit(store.conn(), &store.deadline) {
                Ok(()) => {}
                Err(admission::AdmitError::Refused(r)) => return Err(StoreError::Admission(r)),
                Err(admission::AdmitError::Sqlite(e)) => return Err(e.into()),
                Err(admission::AdmitError::Store(e)) => return Err(*e),
            }
            if store.scratch.is_some() && named {
                store.unlink_copy(&target, has_wal)?;
            }
            return Ok(store);
        }
        Err(StoreError::Changed)
    }

    /// On POSIX, remove the copy's names once SQLite holds them open, so a
    /// reader killed before it drops leaves no copy of the store behind:
    /// the bytes live on in the open descriptors until the connection
    /// closes. A read first, so every file SQLite keeps open is open (the
    /// `-wal` is opened at the first read). `golden_store_files` reads
    /// every table and verifies the chain after this, `-wal` case
    /// included. Elsewhere (Windows cannot remove an open file) the copy
    /// stays until the reader drops.
    fn unlink_copy(&self, copy: &Path, has_wal: bool) -> Result<(), StoreError> {
        #[cfg(unix)]
        {
            self.conn()
                .query_row("SELECT count(*) FROM sqlite_master", [], |_| Ok(()))?;
            fs::remove_file(copy)?;
            if has_wal {
                fs::remove_file(beside(copy, "-wal"))?;
            }
        }
        #[cfg(not(unix))]
        let _ = (copy, has_wal);
        Ok(())
    }

    /// Run `body` under the admission deadline (`_deadline`, no length
    /// limit), as the reference runs its admission and meta reads.
    pub fn with_admission_deadline<T>(
        &self,
        what: &str,
        body: impl FnOnce(&Connection) -> Result<T, admission::AdmitError>,
    ) -> Result<T, admission::AdmitError> {
        admission::with_deadline(self.conn(), &self.deadline, what, None, body)
    }

    /// The private directory a copy is read from, if any (for tests).
    pub fn private_dir(&self) -> Option<&Path> {
        self.scratch.as_ref().map(|s| s.dir.as_path())
    }

    fn conn(&self) -> &Connection {
        // Set from open to drop.
        self.conn.as_ref().expect("open connection")
    }

    /// The path the store was opened by.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Whether the store is read from a private copy.
    pub fn reads_a_copy(&self) -> bool {
        self.scratch.is_some()
    }

    /// The connection, for reads this module does not name.
    pub fn connection(&self) -> &Connection {
        self.conn()
    }

    /// `PRAGMA <name>`'s first value.
    pub fn pragma(&self, name: &str) -> Result<SqlValue, StoreError> {
        if !name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_') {
            return Err(StoreError::Refused(format!("not a pragma: {name}")));
        }
        let sql = format!("PRAGMA {name}");
        let mut statement = self.conn().prepare(&sql)?;
        let mut rows = statement.query([])?;
        match rows.next()? {
            Some(row) => sql_value(row.get_ref(0)?),
            None => Ok(SqlValue::Null),
        }
    }

    /// `read_store_meta`: `{str(key): str(value)}`, a later row's value
    /// replacing an earlier one's under the same key, read under the
    /// admission deadline (no length limit), as the reference reads it.
    pub fn meta(&self) -> Result<Vec<(String, String)>, StoreError> {
        let result =
            admission::with_deadline(self.conn(), &self.deadline, "meta table", None, |conn| {
                let mut statement = conn.prepare("SELECT key, value FROM meta")?;
                let mut rows = statement.query([])?;
                let mut out: Vec<(String, String)> = Vec::new();
                let mut at: std::collections::HashMap<String, usize> = Default::default();
                while let Some(row) = rows.next()? {
                    let key =
                        sql_value(row.get_ref(0)?).map_err(admission::AdmitError::from_store)?;
                    let value =
                        sql_value(row.get_ref(1)?).map_err(admission::AdmitError::from_store)?;
                    let (key, value) = (key.py_str(), value.py_str());
                    match at.get(&key) {
                        Some(&i) => out[i].1 = value,
                        None => {
                            at.insert(key.clone(), out.len());
                            out.push((key, value));
                        }
                    }
                }
                Ok(out)
            });
        match result {
            Ok(out) => Ok(out),
            Err(admission::AdmitError::Refused(r)) => Err(StoreError::Admission(r)),
            Err(admission::AdmitError::Sqlite(e)) => Err(e.into()),
            Err(admission::AdmitError::Store(e)) => Err(*e),
        }
    }

    /// The table names `sqlite_master` lists, in name order.
    pub fn table_names(&self) -> Result<Vec<String>, StoreError> {
        let mut statement = self
            .conn()
            .prepare("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")?;
        let mut rows = statement.query([])?;
        let mut out = Vec::new();
        while let Some(row) = rows.next()? {
            out.push(sql_value(row.get_ref(0)?)?.py_str());
        }
        Ok(out)
    }

    /// `sqlite_master`'s (type, name, tbl_name, sql) in (type, name) order.
    pub fn master(&self) -> Result<Vec<[SqlValue; 4]>, StoreError> {
        let mut statement = self
            .conn()
            .prepare("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")?;
        let mut rows = statement.query([])?;
        let mut out = Vec::new();
        while let Some(row) = rows.next()? {
            out.push([
                sql_value(row.get_ref(0)?)?,
                sql_value(row.get_ref(1)?)?,
                sql_value(row.get_ref(2)?)?,
                sql_value(row.get_ref(3)?)?,
            ]);
        }
        Ok(out)
    }

    /// Every row of `table`, ordered by each column in turn, visited one at
    /// a time. `table` is quoted; the reference never reads a table by a
    /// name the file gives, nor does any caller here but a dump.
    pub fn each_row(
        &self,
        table: &str,
        visit: &mut dyn FnMut(Vec<SqlValue>),
    ) -> Result<(), StoreError> {
        let quoted = format!("\"{}\"", table.replace('"', "\"\""));
        let columns = {
            let mut statement = self
                .conn()
                .prepare(&format!("PRAGMA table_info({quoted})"))?;
            let n = statement.query_map([], |_| Ok(()))?.count();
            n
        };
        let order: Vec<String> = (1..=columns).map(|i| i.to_string()).collect();
        let sql = if order.is_empty() {
            format!("SELECT * FROM {quoted}")
        } else {
            format!("SELECT * FROM {quoted} ORDER BY {}", order.join(", "))
        };
        let mut statement = self.conn().prepare(&sql)?;
        let width = statement.column_count();
        let mut rows = statement.query([])?;
        while let Some(row) = rows.next()? {
            let mut values = Vec::with_capacity(width);
            for i in 0..width {
                values.push(sql_value(row.get_ref(i)?)?);
            }
            visit(values);
        }
        Ok(())
    }

    /// `SELECT count(*)` of a table.
    pub fn count(&self, table: &str) -> Result<i64, StoreError> {
        let quoted = format!("\"{}\"", table.replace('"', "\"\""));
        Ok(self
            .conn()
            .query_row(&format!("SELECT count(*) FROM {quoted}"), [], |r| r.get(0))?)
    }
}

// ---------------------------------------------------------------------------
// The opening checks and the version contract
// ---------------------------------------------------------------------------

/// What the open found: the store's id and meta's key fingerprint, and
/// the version verdict.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Opened {
    pub store_id: String,
    pub key_fingerprint: String,
    pub verdict: Verdict,
}

fn meta_get<'a>(meta: &'a [(String, String)], key: &str) -> Option<&'a str> {
    meta.iter().find(|(k, _)| k == key).map(|(_, v)| v.as_str())
}

/// `store_versions._shown`.
fn shown(text: &str) -> String {
    let r = repr_str(&unicode::cps(text));
    let inner = &r[1..r.len() - 1];
    let cut: PyStr = if inner.len() <= 64 {
        inner.to_vec()
    } else {
        let mut c = inner[..61].to_vec();
        c.extend(unicode::cps("..."));
        c
    };
    json::utf8_text(&cut)
}

/// `store_versions._stored_revision`.
fn stored_revision(
    meta: &[(String, String)],
    key: &'static str,
    path: &str,
) -> Result<i64, StoreVersionError> {
    let Some(raw) = meta_get(meta, key) else {
        return Ok(0);
    };
    let whole = (1..=9).contains(&raw.len()) && raw.bytes().all(|b| b.is_ascii_digit());
    if !whole {
        return Err(StoreVersionError {
            message: format!(
                "{path} carries a {key} that is not a whole number ('{}'); the store is not \
                 opened: set meta's {key} back to a whole number, or restore the store from a \
                 backup taken before the edit",
                shown(raw)
            ),
            key: Some(key),
            needed: None,
        });
    }
    Ok(raw.parse::<i64>().unwrap_or(0))
}

/// `store_versions.check_versions` for a reader's open (`writing` False).
/// `path` is the path as the reference spells it in a message, `version`
/// the build's version (`bettermemory.__version__`).
pub fn check_versions(
    meta: &[(String, String)],
    path: &str,
    version: &str,
) -> Result<Verdict, StoreVersionError> {
    let revision = stored_revision(meta, "schema_revision", path)?;
    let min_reader = stored_revision(meta, "min_reader_revision", path)?;
    let min_writer = stored_revision(meta, "min_writer_revision", path)?;
    let newest = match meta_get(meta, "engine_version") {
        Some(engine) if !engine.is_empty() => {
            format!(" (its newest writer was bettermemory {})", shown(engine))
        }
        _ => String::new(),
    };
    if min_reader > SCHEMA_REVISION {
        return Err(StoreVersionError {
            message: format!(
                "{path} needs a reader at schema revision {min_reader} or later (meta's \
                 min_reader_revision){newest}; this build, bettermemory {version}, reads \
                 revision {SCHEMA_REVISION}: upgrade bettermemory to open it"
            ),
            key: Some("min_reader_revision"),
            needed: Some(min_reader),
        });
    }
    let read_only = (min_writer > SCHEMA_REVISION).then(|| {
        format!(
            "{path} is open for reading only: it needs a writer at schema revision \
             {min_writer} or later{newest}, and this build, bettermemory {version}, writes \
             revision {SCHEMA_REVISION}: upgrade bettermemory to write to it"
        )
    });
    Ok(Verdict {
        read_only,
        revision,
        min_reader,
        min_writer,
    })
}

/// Python's `int(text)` of meta's `schema_version` (surrounding
/// whitespace, a sign, underscores between digits, any Unicode decimal
/// digits), as canonical decimal text.
fn python_int(text: &str) -> Option<String> {
    match &crate::pyvalue::int_from_str(&unicode::cps(text)) {
        Ok(PyValue::Int(decimal)) => Some(decimal.clone()),
        _ => None,
    }
}

/// Whether canonical decimal text names an int above `bound`.
fn decimal_above(decimal: &str, bound: i64) -> bool {
    match decimal.parse::<i64>() {
        Ok(v) => v > bound,
        Err(_) => !decimal.starts_with('-'),
    }
}

/// The checks `_open_connected` makes before the key ring, then
/// [`check_versions`]: the store id's shape, the schema version.
pub fn open_checks(
    meta: &[(String, String)],
    path: &str,
    version: &str,
) -> Result<Opened, StoreError> {
    let store_id = meta_get(meta, "store_id").unwrap_or("");
    let expected = meta_get(meta, "key_fingerprint").unwrap_or("");
    if store_id.is_empty() || expected.is_empty() {
        return Err(StoreError::Refused(format!(
            "{path} carries no store id or key fingerprint"
        )));
    }
    if !keys::is_store_id(store_id) {
        let head: String = store_id.chars().take(40).collect();
        return Err(StoreError::Refused(format!(
            "{path} carries a store id this build never writes ({}); the key directory is \
             searched by it, so the store is not opened",
            json::utf8_text(&repr_str(&unicode::cps(&head)))
        )));
    }
    let schema_text = meta_get(meta, "schema_version").unwrap_or("0");
    let Some(schema) = python_int(schema_text) else {
        return Err(StoreError::Refused(format!(
            "invalid literal for int() with base 10: {}",
            json::utf8_text(&repr_str(&unicode::cps(schema_text)))
        )));
    };
    if decimal_above(&schema, SCHEMA_VERSION) {
        return Err(StoreError::Refused(format!(
            "{path} is schema {schema}; this build reads up to {SCHEMA_VERSION}"
        )));
    }
    let verdict = check_versions(meta, path, version).map_err(StoreError::Version)?;
    Ok(Opened {
        store_id: store_id.to_owned(),
        key_fingerprint: expected.to_owned(),
        verdict,
    })
}

// ---------------------------------------------------------------------------
// The log
// ---------------------------------------------------------------------------

const LOG_SELECT: &str = "SELECT seq, ts, session, kind, payload, prev_mac, mac FROM log";

fn log_row(row: &rusqlite::Row<'_>) -> Result<LogRow, StoreError> {
    let text = |i: usize, column: &'static str| -> Result<String, StoreError> {
        Ok(value_of(row.get_ref(i)?, "log", column)?.py_str())
    };
    // `int(raw[0])`: seq is the rowid alias, an INTEGER.
    let seq = match value_of(row.get_ref(0)?, "log", "seq")? {
        SqlValue::Integer(i) => i,
        other => {
            return Err(StoreError::Type {
                table: "log",
                column: "seq",
                found: other.type_name(),
            })
        }
    };
    let session = match value_of(row.get_ref(2)?, "log", "session")? {
        SqlValue::Null => None,
        other => Some(other.py_str()),
    };
    Ok(LogRow {
        seq,
        ts: text(1, "ts")?,
        session,
        kind: text(3, "kind")?,
        payload: text(4, "payload")?,
        prev_mac: text(5, "prev_mac")?,
        mac: text(6, "mac")?,
    })
}

impl StoreFile {
    fn log_query(
        &self,
        tail: &str,
        params: &[&dyn rusqlite::ToSql],
        visit: &mut dyn FnMut(LogRow) -> Result<bool, PyError>,
    ) -> Result<Result<(), PyError>, StoreError> {
        let mut statement = self.conn().prepare(&format!("{LOG_SELECT} {tail}"))?;
        let mut rows = statement.query(params)?;
        while let Some(row) = rows.next()? {
            match visit(log_row(row)?) {
                Ok(true) => {}
                Ok(false) => break,
                Err(e) => return Ok(Err(e)),
            }
        }
        Ok(Ok(()))
    }

    /// `log.iter_rows(conn)`: visit every row with seq above 0 in seq order.
    pub fn each_log_row(
        &self,
        visit: &mut dyn FnMut(&LogRow) -> Result<(), PyError>,
    ) -> Result<Result<(), PyError>, StoreError> {
        self.log_query("WHERE seq > ? ORDER BY seq", &[&0i64], &mut |row| {
            visit(&row).map(|()| true)
        })
    }
}

impl LogSource for StoreFile {
    type Error = StoreError;

    fn first(&self) -> Result<Option<LogRow>, StoreError> {
        let mut out = None;
        self.log_query("WHERE seq > ? ORDER BY seq LIMIT 1", &[&0i64], &mut |row| {
            out = Some(row);
            Ok(false)
        })?
        .map_err(StoreError::Py)?;
        Ok(out)
    }

    fn rekeys(&self) -> Result<Vec<LogRow>, StoreError> {
        let mut out = Vec::new();
        self.log_query(
            "WHERE seq > ? AND kind = 'rekey' ORDER BY seq",
            &[&0i64],
            &mut |row| {
                if row.kind == crate::log::REKEY {
                    out.push(row);
                }
                Ok(true)
            },
        )?
        .map_err(StoreError::Py)?;
        Ok(out)
    }

    fn at_seq(&self, seq: i64) -> Result<Option<LogRow>, StoreError> {
        // `iter_rows` reads seq > 0 only.
        if seq <= 0 {
            return Ok(None);
        }
        let mut out = None;
        self.log_query("WHERE seq = ? ORDER BY seq LIMIT 1", &[&seq], &mut |row| {
            out = Some(row);
            Ok(false)
        })?
        .map_err(StoreError::Py)?;
        Ok(out)
    }

    fn each(
        &self,
        visit: &mut dyn FnMut(&LogRow) -> Result<(), PyError>,
    ) -> Result<Result<(), PyError>, StoreError> {
        self.each_log_row(visit)
    }
}

impl StoreFile {
    /// `log.verify_chain(conn, keyring=ring, current_fingerprint=...)`:
    /// one pass over the log table, every statement of it in one read
    /// transaction, so the rows come from one snapshot of the log as the
    /// reference's single `SELECT` gives them (a writer's commits after it
    /// began are not seen). The head and origin files are read as the
    /// reference reads them, outside SQLite, after the rows.
    pub fn verify_chain(
        &self,
        ring: &KeyRing,
        current_fingerprint: &str,
    ) -> Result<crate::log::ChainReport, VerifyError<StoreError>> {
        self.read_transaction(|store| {
            crate::log::verify_chain_from(store, ring, current_fingerprint)
        })
        .map_err(VerifyError::Source)?
    }

    /// Run `body` in one read transaction (one snapshot of the store), or
    /// in the one already open.
    pub fn read_transaction<T>(&self, body: impl FnOnce(&Self) -> T) -> Result<T, StoreError> {
        let conn = self.conn();
        if !conn.is_autocommit() {
            return Ok(body(self));
        }
        conn.execute_batch("BEGIN")?;
        let out = body(self);
        // A read transaction: ending it changes nothing.
        let _ = conn.execute_batch("COMMIT");
        Ok(out)
    }
}

// ---------------------------------------------------------------------------
// Records
// ---------------------------------------------------------------------------

/// A memory or tombstone row as `store_rows._row_to_memory` and
/// `_row_to_tombstone` read its columns, before the model validates them
/// (pydantic's validators are not ported: the ULID check of `id`, the
/// scope list's rules, the 64-entry caps, enum membership of `confidence`
/// and `source`, `links`, `origin` and `actor`).
#[derive(Clone, Debug, PartialEq)]
pub struct MemoryRow {
    pub rowid: Option<i64>,
    pub id: String,
    pub created: DateTime,
    pub updated: DateTime,
    pub scopes: Vec<PyValue>,
    pub confidence: String,
    pub source: String,
    pub body: String,
    pub category: Option<String>,
    pub last_verified_at: Option<DateTime>,
    pub verified_paths: Vec<PyValue>,
    pub verified_commits: Vec<PyValue>,
    pub verified_versions: Vec<PyValue>,
    pub verified_absent_paths: Vec<PyValue>,
    pub claims: Vec<PyValue>,
    /// `_anchor_or_none`.
    pub verified_head: Option<String>,
    pub corroborations: Option<i64>,
    pub last_corroborated: Option<DateTime>,
    /// Tombstones only: `removed`, `removed_reason`, `removed_session`.
    pub removed: Option<(DateTime, String, Option<String>)>,
}

impl MemoryRow {
    /// The ranker's record (`search::Record`) for this memory.
    pub fn to_search_record(&self) -> Option<Record> {
        let scopes = self
            .scopes
            .iter()
            .map(|s| match s {
                PyValue::Str(t) => unicode::to_string(t),
                _ => None,
            })
            .collect::<Option<Vec<String>>>()?;
        Some(Record {
            id: self.id.clone(),
            created: self.created,
            updated: self.updated,
            scopes,
            body: unicode::cps(&self.body),
        })
    }
}

/// An episode row as `_row_to_episode` reads it (validation not ported).
#[derive(Clone, Debug, PartialEq)]
pub struct EpisodeRow {
    pub id: String,
    pub session_id: String,
    pub created: DateTime,
    pub body: String,
    pub scopes: Vec<PyValue>,
    pub takeaway: Option<String>,
    pub is_floor: bool,
    pub swarm_id: Option<String>,
}

struct Columns<'a> {
    row: &'a rusqlite::Row<'a>,
    table: &'static str,
}

impl Columns<'_> {
    fn value(&self, column: &'static str) -> Result<SqlValue, StoreError> {
        value_of(self.row.get_ref(column)?, self.table, column)
    }

    fn text(&self, column: &'static str) -> Result<String, StoreError> {
        match self.value(column)? {
            SqlValue::Text(t) => Ok(t),
            other => Err(StoreError::Type {
                table: self.table,
                column,
                found: other.type_name(),
            }),
        }
    }

    fn text_opt(&self, column: &'static str) -> Result<Option<String>, StoreError> {
        match self.value(column)? {
            SqlValue::Null => Ok(None),
            SqlValue::Text(t) => Ok(Some(t)),
            other => Err(StoreError::Type {
                table: self.table,
                column,
                found: other.type_name(),
            }),
        }
    }

    /// `_dt(row[column])`.
    fn dt(&self, column: &'static str) -> Result<DateTime, StoreError> {
        let text = self.text(column)?;
        datetime::parse_record_dt(&unicode::cps(&text)).map_err(|_| PyError::ValueError.into())
    }

    fn dt_opt(&self, column: &'static str) -> Result<Option<DateTime>, StoreError> {
        match self.text_opt(column)? {
            None => Ok(None),
            Some(text) => datetime::parse_record_dt(&unicode::cps(&text))
                .map(Some)
                .map_err(|_| PyError::ValueError.into()),
        }
    }

    /// `_load_json(row[column], [])` of a list column.
    fn list(&self, column: &'static str) -> Result<Vec<PyValue>, StoreError> {
        match self.text_opt(column)? {
            None => Ok(Vec::new()),
            Some(text) if text == "[]" => Ok(Vec::new()),
            Some(text) => match &json::loads(&unicode::cps(&text))? {
                PyValue::List(items) => Ok(items.clone()),
                _ => Err(StoreError::Py(PyError::TypeError)),
            },
        }
    }
}

fn anchor_or_none(value: Option<String>) -> Option<String> {
    let candidate = value?;
    let trimmed = candidate.trim_matches(|c: char| unicode::is_space(c as u32));
    let lowered = unicode::to_string(&unicode::lower(&unicode::cps(trimmed)))?;
    let hex = lowered
        .bytes()
        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b));
    (hex && (lowered.len() == 40 || lowered.len() == 64)).then_some(lowered)
}

fn memory_row(c: &Columns<'_>, tombstone: bool) -> Result<MemoryRow, StoreError> {
    let removed = if tombstone {
        Some((
            c.dt("removed")?,
            c.text("removed_reason")?,
            c.text_opt("removed_session")?,
        ))
    } else {
        None
    };
    Ok(MemoryRow {
        rowid: None,
        id: c.text("id")?,
        created: c.dt("created")?,
        updated: c.dt("updated")?,
        scopes: c.list("scopes_json")?,
        confidence: c.text("confidence")?,
        source: c.text("source")?,
        body: c.text("body")?,
        category: c.text_opt("category")?,
        last_verified_at: c.dt_opt("last_verified_at")?,
        verified_paths: c.list("verified_paths_json")?,
        verified_commits: c.list("verified_commits_json")?,
        verified_versions: c.list("verified_versions_json")?,
        verified_absent_paths: c.list("verified_absent_paths_json")?,
        claims: c.list("claims_json")?,
        verified_head: anchor_or_none(c.text_opt("verified_head")?),
        corroborations: if tombstone {
            None
        } else {
            match c.value("corroborations")? {
                SqlValue::Integer(i) => Some(i),
                other => {
                    return Err(StoreError::Type {
                        table: c.table,
                        column: "corroborations",
                        found: other.type_name(),
                    })
                }
            }
        },
        last_corroborated: if tombstone {
            None
        } else {
            c.dt_opt("last_corroborated")?
        },
        removed,
    })
}

impl StoreFile {
    /// `iter_memories`' rows (`SELECT * FROM memories ORDER BY rowid`),
    /// visited one at a time.
    pub fn each_memory(&self, visit: &mut dyn FnMut(MemoryRow)) -> Result<(), StoreError> {
        let mut statement = self
            .conn()
            .prepare("SELECT * FROM memories ORDER BY rowid")?;
        let mut rows = statement.query([])?;
        while let Some(row) = rows.next()? {
            let c = Columns {
                row,
                table: "memories",
            };
            let mut m = memory_row(&c, false)?;
            m.rowid = match c.value("rowid")? {
                SqlValue::Integer(i) => Some(i),
                _ => None,
            };
            visit(m);
        }
        Ok(())
    }

    /// The tombstones' rows, in rowid order.
    pub fn each_tombstone(&self, visit: &mut dyn FnMut(MemoryRow)) -> Result<(), StoreError> {
        let mut statement = self
            .conn()
            .prepare("SELECT * FROM tombstones ORDER BY rowid")?;
        let mut rows = statement.query([])?;
        while let Some(row) = rows.next()? {
            visit(memory_row(
                &Columns {
                    row,
                    table: "tombstones",
                },
                true,
            )?);
        }
        Ok(())
    }

    /// The episodes' rows, in rowid order.
    pub fn each_episode(&self, visit: &mut dyn FnMut(EpisodeRow)) -> Result<(), StoreError> {
        let mut statement = self
            .conn()
            .prepare("SELECT * FROM episodes ORDER BY rowid")?;
        let mut rows = statement.query([])?;
        while let Some(row) = rows.next()? {
            let c = Columns {
                row,
                table: "episodes",
            };
            let is_floor = match c.value("is_floor")? {
                SqlValue::Integer(i) => i != 0,
                SqlValue::Real(f) => f != 0.0,
                SqlValue::Null => false,
                SqlValue::Text(t) => !t.is_empty(),
                SqlValue::Blob(b) => !b.is_empty(),
            };
            visit(EpisodeRow {
                id: c.text("id")?,
                session_id: c.text("session_id")?,
                created: c.dt("created")?,
                body: c.text("body")?,
                scopes: c.list("scopes_json")?,
                takeaway: c.text_opt("takeaway")?,
                is_floor,
                swarm_id: c.text_opt("swarm_id")?,
            });
        }
        Ok(())
    }

    // -----------------------------------------------------------------------
    // The FTS prefilter
    // -----------------------------------------------------------------------

    /// `store_index.query_candidates`: `[(id, bm25)]` as its SQL orders
    /// them (`ORDER BY score ASC LIMIT ?`, no tie break, as the reference
    /// writes it), the scopes as an OR of `LIKE '% scope %'` over
    /// `scopes_text`, the client and model as exact equality a NULL never
    /// meets, the cap in SQL.
    pub fn query_candidates(
        &self,
        text: &str,
        filters: &CandidateFilters,
    ) -> Result<Vec<(String, f64)>, StoreError> {
        if text.chars().all(|c| unicode::is_space(c as u32)) {
            return Ok(Vec::new());
        }
        let match_query = lexicon::fts_match_query(&unicode::cps(text));
        if match_query.is_empty() {
            return Ok(Vec::new());
        }
        let mut sql = String::from(
            "SELECT m.id, bm25(memories_fts) AS score FROM memories_fts \
             JOIN memories m ON m.rowid = memories_fts.rowid \
             WHERE memories_fts MATCH ? ",
        );
        let mut params: Vec<rusqlite::types::Value> = vec![match_query.into()];
        if !filters.scopes.is_empty() {
            let likes = vec!["m.scopes_text LIKE ?"; filters.scopes.len()];
            sql.push_str(&format!("AND ({}) ", likes.join(" OR ")));
            params.extend(filters.scopes.iter().map(|s| format!("% {s} %").into()));
        }
        if let Some(client) = &filters.client {
            sql.push_str("AND m.actor_client IS NOT NULL AND m.actor_client = ? ");
            params.push(client.clone().into());
        }
        if let Some(model) = &filters.model {
            sql.push_str("AND m.actor_model IS NOT NULL AND m.actor_model = ? ");
            params.push(model.clone().into());
        }
        sql.push_str("ORDER BY score ASC LIMIT ?");
        params.push(filters.max_results.into());
        let mut statement = self.conn().prepare(&sql)?;
        let mut rows = statement.query(rusqlite::params_from_iter(params))?;
        let mut out = Vec::new();
        while let Some(row) = rows.next()? {
            let id = value_of(row.get_ref(0)?, "memories", "id")?.py_str();
            let score = match value_of(row.get_ref(1)?, "memories_fts", "bm25")? {
                SqlValue::Real(f) => f,
                SqlValue::Integer(i) => i as f64,
                other => {
                    return Err(StoreError::Type {
                        table: "memories_fts",
                        column: "bm25",
                        found: other.type_name(),
                    })
                }
            };
            out.push((id, score));
        }
        Ok(out)
    }

    /// `history.history_replaced`: every `store_restored` and
    /// `history_accepted` row past seq 0 in log order, as `{seq, ts, kind,
    /// action, bundle_created_at?, to_seq?, from_seq?}` (`replaced_detail`).
    /// RecursionError for a payload nested past [`json::MAX_DEPTH`], which
    /// the reference does not catch.
    pub fn history_replaced(&self) -> Result<Vec<Vec<(&'static str, PyValue)>>, StoreError> {
        let mut out = Vec::new();
        let mut failed: Option<PyError> = None;
        self.log_query(
            "WHERE kind IN ('history_accepted', 'store_restored') AND seq > ? ORDER BY seq",
            &[&0i64],
            &mut |row| {
                match replaced_entry(&row) {
                    Ok(entry) => out.push(entry),
                    Err(e) => {
                        failed = Some(e);
                        return Ok(false);
                    }
                }
                Ok(true)
            },
        )?
        .map_err(StoreError::Py)?;
        match failed {
            Some(e) => Err(StoreError::Py(e)),
            None => Ok(out),
        }
    }

    /// `store_index.ranked_rows(text)`: row numbers best first.
    pub fn ranked_rows(&self, text: &str) -> Result<Vec<i64>, StoreError> {
        if text.chars().all(|c| unicode::is_space(c as u32)) {
            return Ok(Vec::new());
        }
        let match_query = lexicon::fts_match_query(&unicode::cps(text));
        if match_query.is_empty() {
            return Ok(Vec::new());
        }
        let mut statement = self.conn().prepare(
            "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ? \
             ORDER BY bm25(memories_fts), rowid",
        )?;
        let mut rows = statement.query([match_query])?;
        let mut out = Vec::new();
        while let Some(row) = rows.next()? {
            match row.get_ref(0)? {
                ValueRef::Integer(i) => out.push(i),
                _ => {
                    return Err(StoreError::Type {
                        table: "memories_fts",
                        column: "rowid",
                        found: "non-integer",
                    })
                }
            }
        }
        Ok(out)
    }
}

/// The filters `query_candidates` takes besides the text.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CandidateFilters {
    pub scopes: Vec<String>,
    pub client: Option<String>,
    pub model: Option<String>,
    /// `max_results`, 100 by default.
    pub max_results: i64,
}

impl Default for CandidateFilters {
    fn default() -> Self {
        CandidateFilters {
            scopes: Vec::new(),
            client: None,
            model: None,
            max_results: 100,
        }
    }
}

/// Order two Python ints given as canonical decimal text.
fn cmp_int(a: &str, b: &str) -> std::cmp::Ordering {
    let neg = |t: &str| t.starts_with('-');
    match (neg(a), neg(b)) {
        (true, false) => std::cmp::Ordering::Less,
        (false, true) => std::cmp::Ordering::Greater,
        (n, _) => {
            let by = a.len().cmp(&b.len()).then_with(|| a.cmp(b));
            if n {
                by.reverse()
            } else {
                by
            }
        }
    }
}

/// One entry of `history_replaced` (`replaced_detail` of its payload).
fn replaced_entry(row: &LogRow) -> Result<Vec<(&'static str, PyValue)>, PyError> {
    let payload = match json::loads(&unicode::cps(&row.payload)) {
        Ok(p @ PyValue::Dict(_)) => Some(p),
        Ok(_) => None,
        Err(PyError::RecursionError) => return Err(PyError::RecursionError),
        Err(_) => None,
    };
    let empty = PyValue::Dict(Vec::new());
    let payload = payload.unwrap_or(empty);
    let mapping = |key: &str| match payload.get(key) {
        Some(v @ PyValue::Dict(_)) => Some(v.clone()),
        _ => None,
    };
    let mut after = mapping("head_after");
    if after.is_none() && row.kind != "store_restored" {
        after = mapping("log_end_before");
    }
    let to_seq = after.as_ref().and_then(|a| a.get("seq").cloned());
    let mut known: Vec<String> = Vec::new();
    for end in [mapping("head_before"), mapping("log_end_before")]
        .into_iter()
        .flatten()
    {
        if let Some(PyValue::Int(seq)) = end.get("seq") {
            known.push(seq.clone());
        }
    }
    let from_seq = known.into_iter().max_by(|a, b| cmp_int(a, b));
    let action = if row.kind == "store_restored" {
        "rewound"
    } else {
        "accepted"
    };
    let mut entry: Vec<(&'static str, PyValue)> = vec![
        ("seq", PyValue::int(i128::from(row.seq))),
        ("ts", PyValue::str(&row.ts)),
        ("kind", PyValue::str(&row.kind)),
        ("action", PyValue::str(action)),
    ];
    for (name, value) in [
        (
            "bundle_created_at",
            payload.get("bundle_created_at").cloned(),
        ),
        ("to_seq", to_seq),
        ("from_seq", from_seq.map(PyValue::Int)),
    ] {
        if let Some(v) = value {
            if !matches!(v, PyValue::None) {
                entry.push((name, v));
            }
        }
    }
    Ok(entry)
}

/// The bundled SQLite's version text.
pub fn sqlite_version() -> &'static str {
    rusqlite::version()
}
