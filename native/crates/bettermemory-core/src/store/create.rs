//! A new store (`Store.create`): the file, its schema and pragmas, meta,
//! the first log row (`store_created`), the empty length digest, and the
//! key ring beside it (key, head checkpoint, origin anchor, binding, their
//! lock files), every byte the reference writes under the same random
//! values and clock readings ([`Provider`]).
//!
//! What differs is the order, so a create killed anywhere leaves either no
//! store at the path (create can run again) or a whole one that opens and
//! verifies `ok` on both builds. The file is built under a temporary name
//! beside the store, held by a locked lock file; the key, the head and the
//! origin anchor are written before the file is closed and linked to its
//! name (never over a file that appeared meanwhile); the binding, which
//! names the final path, after. See `native/README.md`, "Creating a
//! store", for each kill boundary against the reference's.
//!
//! Not ported: `~` expansion and the default keys directory (every path is
//! the caller's), the reference's log lines, a marker an unfinished restore
//! left (only a running restore is refused here, as a read refuses it).

use std::ffi::OsStr;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use rusqlite::{Connection, OpenFlags};

use super::digest;
use super::restore_lock;
use crate::datetime::DateTime;
use crate::json::{self, DumpOptions};
use crate::lexicon;
use crate::log::{self, fingerprint, head_file_text, Key, LogRow};
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode;

/// `config.STORE_FILENAME`.
pub const STORE_FILENAME: &str = "memory.sqlite";
/// `store_schema.PAGE_SIZE`.
pub const PAGE_SIZE: i64 = 8192;
/// `store_schema.MIN_READER_REVISION`, `MIN_WRITER_REVISION`.
pub const MIN_READER_REVISION: i64 = 1;
pub const MIN_WRITER_REVISION: i64 = 5;
/// `log.KEY_BYTES`.
pub const KEY_BYTES: usize = 32;
/// `store_schema.CREATED_AFTER_ORPHANS`, `MEMBERSHIP_STORE_ID`.
pub const CREATED_AFTER_ORPHANS: &str = "created_after_orphans";
pub const MEMBERSHIP_STORE_ID: &str = "membership_store_id";

/// The temporary file a create builds the store in, beside it:
/// `.memory.sqlite.creating-<tag>`, and its lock `<that>.lock`.
const TEMP_PREFIX: &str = ".memory.sqlite.creating-";
const LOCK_SUFFIX: &str = ".lock";
/// A temporary file with no lock file beside it (a leftover: its lock file
/// went first, or was removed by hand), and a key ring writer's `.tmp`
/// file, are removed only when older than this. A live create always
/// holds its lock file ([`take_temp`] retries until the file it locked is
/// the one at the name).
const UNLOCKED_GRACE: std::time::Duration = std::time::Duration::from_secs(3600);

static COUNTER: AtomicU64 = AtomicU64::new(0);

/// Everything `Store.create` mints at random or reads from the clock, and
/// the version it records. Called in the reference's order: `store_id`,
/// `key`, `membership_store_id`, then `now` three times (meta's
/// `created`, the first row's `ts`, the head file's `ts`).
pub trait Provider {
    /// `secrets.token_hex(16)`: 32 lowercase hex digits.
    fn store_id(&mut self) -> io::Result<String>;
    /// `secrets.token_bytes(32)`, straight into the wiping key type.
    fn key(&mut self) -> io::Result<Key>;
    /// `secrets.token_hex(16)`.
    fn membership_store_id(&mut self) -> io::Result<String>;
    /// `datetime.now(timezone.utc)`: the log's clock (`log._utcnow_iso`,
    /// a row's and the head's `ts`) and `created`.
    fn now(&mut self) -> io::Result<DateTime>;
    /// `datetime.now(timezone.utc)` as `store_returned` reads it (a
    /// `returned` mark's stamp): the wall clock, apart from the log's in
    /// the reference's modules, so a test can tell the two apart. One clock
    /// unless a provider says otherwise.
    fn wall_now(&mut self) -> io::Result<DateTime> {
        self.now()
    }
    /// `bettermemory.__version__`.
    fn engine_version(&self) -> String;
    /// `models.generate_ulid()`: a record id's text, as `store_records`
    /// and `store_journal` mint it (`write_episode`, `write_floor`, and the
    /// record writers that mint an id). The reference reads `time.time()`
    /// for it apart from both clocks, plus ten random bytes, so an id's
    /// milliseconds need not match the stamp beside it. By default
    /// [`system_ulid`]: the system clock's seconds through
    /// [`super::mint::millis`] and ten bytes from the OS through
    /// [`super::mint::ulid`]. `utcnow` (`models.py:879`) is the wall clock
    /// ([`Provider::wall_now`]), the same read: no method of its own.
    fn ulid(&mut self) -> io::Result<String> {
        system_ulid()
    }
    /// `time.monotonic()` as `Store._in_chunks` reads it (in seconds, a
    /// float): when a chunk has held the write lock long enough. By
    /// default [`system_monotonic`].
    fn monotonic(&mut self) -> io::Result<f64> {
        Ok(system_monotonic())
    }
    /// `time.sleep(seconds)` as `Store._yield_between_chunks` sleeps, the
    /// write lock free. By default the thread sleeps.
    fn pause(&mut self, seconds: f64) {
        if let Ok(d) = std::time::Duration::try_from_secs_f64(seconds) {
            std::thread::sleep(d);
        }
    }
}

/// A monotonic clock in seconds since its first reading in this process.
pub fn system_monotonic() -> f64 {
    static BASE: std::sync::OnceLock<std::time::Instant> = std::sync::OnceLock::new();
    BASE.get_or_init(std::time::Instant::now)
        .elapsed()
        .as_secs_f64()
}

/// `generate_ulid` as the reference mints it: `time.time()` and
/// `secrets.token_bytes(10)`.
pub fn system_ulid() -> io::Result<String> {
    let seconds = match std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH) {
        Ok(d) => d.as_secs_f64(),
        Err(e) => -e.duration().as_secs_f64(),
    };
    let ms = ulid_millis(seconds)?;
    let mut random = [0u8; 10];
    System::fill(&mut random)?;
    Ok(super::mint::ulid(ms, &random))
}

/// The pure part of [`system_ulid`]: `int(seconds * 1000) & (2**48 - 1)`
/// as `generate_ulid` computes it. A finite time wraps at 48 bits (past
/// the year 10889, and before 1970, as the reference does); an infinite
/// product is the reference's OverflowError and a NaN its ValueError,
/// returned as an error whose text names that class.
pub fn ulid_millis(seconds: f64) -> io::Result<u64> {
    super::mint::millis(seconds).map_err(|e| io::Error::other(format!("the clock: {}", e.name())))
}

/// Fixed values, drawn in order: what a test passes. Not for products: a
/// fixed store id is refused where its key ring files already exist.
#[doc(hidden)]
#[derive(Clone, Debug)]
pub struct Fixed {
    pub store_id: String,
    pub membership_store_id: String,
    pub key: Key,
    /// The clock's readings in order; the last repeats when run out.
    pub clock: Vec<DateTime>,
    pub engine_version: String,
}

impl Provider for Fixed {
    fn store_id(&mut self) -> io::Result<String> {
        Ok(self.store_id.clone())
    }
    fn key(&mut self) -> io::Result<Key> {
        Ok(self.key.clone())
    }
    fn membership_store_id(&mut self) -> io::Result<String> {
        Ok(self.membership_store_id.clone())
    }
    fn now(&mut self) -> io::Result<DateTime> {
        match self.clock.len() {
            0 => Err(io::Error::other("the fixed clock holds no reading")),
            1 => Ok(self.clock[0]),
            _ => Ok(self.clock.remove(0)),
        }
    }
    fn engine_version(&self) -> String {
        self.engine_version.clone()
    }
    /// Deterministic, and never called by the create and open fixtures:
    /// always the id of millisecond 0 and ten zero bytes,
    /// `00000000000000000000000000`. A test that mints ids replays them
    /// with a provider of its own.
    fn ulid(&mut self) -> io::Result<String> {
        Ok(super::mint::ulid(0, &[0; 10]))
    }
}

/// A provider whose wall clock ([`Provider::wall_now`]) is its own:
/// `wall`'s readings in order, the last repeating; everything else is
/// `inner`'s. For tests that set the two clocks apart.
#[doc(hidden)]
#[derive(Clone, Debug)]
pub struct Clocks<P> {
    pub inner: P,
    pub wall: Vec<DateTime>,
}

impl<P: Provider> Provider for Clocks<P> {
    fn store_id(&mut self) -> io::Result<String> {
        self.inner.store_id()
    }
    fn key(&mut self) -> io::Result<Key> {
        self.inner.key()
    }
    fn membership_store_id(&mut self) -> io::Result<String> {
        self.inner.membership_store_id()
    }
    fn now(&mut self) -> io::Result<DateTime> {
        self.inner.now()
    }
    fn wall_now(&mut self) -> io::Result<DateTime> {
        match self.wall.len() {
            0 => Err(io::Error::other("the fixed wall clock holds no reading")),
            1 => Ok(self.wall[0]),
            _ => Ok(self.wall.remove(0)),
        }
    }
    fn engine_version(&self) -> String {
        self.inner.engine_version()
    }
    fn ulid(&mut self) -> io::Result<String> {
        self.inner.ulid()
    }
    fn monotonic(&mut self) -> io::Result<f64> {
        self.inner.monotonic()
    }
    fn pause(&mut self, seconds: f64) {
        self.inner.pause(seconds);
    }
}

/// The operating system's random source (the `getrandom` crate:
/// `getentropy` on macOS, the `getrandom` system call on Linux, falling
/// back to `/dev/urandom` only on a kernel without it, `ProcessPrng` on
/// Windows) and clock.
#[derive(Debug)]
pub struct System {
    pub engine_version: String,
}

impl System {
    fn fill(buf: &mut [u8]) -> io::Result<()> {
        getrandom::fill(buf).map_err(|e| io::Error::other(format!("the random source: {e}")))
    }

    fn hex16() -> io::Result<String> {
        let mut b = [0u8; 16];
        Self::fill(&mut b)?;
        Ok(b.iter().map(|x| format!("{x:02x}")).collect())
    }
}

impl Provider for System {
    fn store_id(&mut self) -> io::Result<String> {
        Self::hex16()
    }
    fn key(&mut self) -> io::Result<Key> {
        // Wiped if the draw fails; on success the buffer itself moves into
        // the key: the bytes are never copied.
        let mut bytes = zeroize::Zeroizing::new(vec![0u8; KEY_BYTES]);
        Self::fill(&mut bytes)?;
        Ok(Key::new(std::mem::take(&mut *bytes)))
    }
    fn membership_store_id(&mut self) -> io::Result<String> {
        Self::hex16()
    }
    fn now(&mut self) -> io::Result<DateTime> {
        Ok(system_now())
    }
    fn engine_version(&self) -> String {
        self.engine_version.clone()
    }
}

/// The system clock, in UTC to the microsecond.
pub fn system_now() -> DateTime {
    let us = match std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH) {
        Ok(d) => i64::try_from(d.as_micros()).unwrap_or(i64::MAX),
        Err(e) => -i64::try_from(e.duration().as_micros()).unwrap_or(i64::MAX),
    };
    DateTime::from_unix_micros_utc(us).unwrap_or(DateTime {
        year: 1970,
        month: 1,
        day: 1,
        hour: 0,
        minute: 0,
        second: 0,
        microsecond: 0,
        offset_us: Some(0),
    })
}

/// Why a create refused or failed. The store path is left as it was in
/// every case: nothing is at it that was not there before.
#[derive(Debug)]
pub enum CreateError {
    /// `FileExistsError("a store already exists at <path>")`: something is
    /// at the store file's path (a file, a directory, a link).
    Exists(PathBuf),
    /// `store_keys.KeysDirUnreadableError`: the keys directory, or a
    /// directory on the way to it, is a symbolic link to nothing.
    KeysDirUnreadable {
        keys_dir: PathBuf,
        link: PathBuf,
        reason: String,
    },
    /// The reference's `OSError` (its subclass by [`CreateError::class`]):
    /// which step, and the error.
    Io {
        step: &'static str,
        error: io::Error,
    },
    /// The reference's `sqlite3` error.
    Sqlite(rusqlite::Error),
    /// `restore_lock.RestoreLockedError`: a restore holds the store's
    /// directory.
    RestoreRunning,
    /// A key ring file for the drawn store id is already there (`<id>.key`,
    /// `.head`, `.origin`, `.path`): never written over. The reference's
    /// `create_key` replaces a key file; with a random id this never meets
    /// one.
    KeyRingExists(PathBuf),
    /// The store file's name is a symbolic link to nothing. The reference
    /// follows it and creates the store at the link's target; this build
    /// refuses.
    DanglingStoreLink { path: PathBuf, target: String },
    /// `restore_lock.RestoreLockedError` for a marker an unfinished restore
    /// left beside the store (`<store>.restore-lock` not blank): the
    /// reference's whole message.
    RestoreUnfinished(String),
    /// The provider gave a store id of another shape (`KeyRing` refuses it
    /// with ValueError), or a key of another length.
    Provider(&'static str),
}

impl CreateError {
    /// The class the reference raises for the same condition; for
    /// `DanglingStoreLink`, where the reference raises nothing (it creates
    /// the store through the link), the name of this build's refusal. That
    /// refusal comes before the store's directory is made or its mode
    /// tightened.
    pub fn class(&self) -> &'static str {
        match self {
            CreateError::Exists(_) | CreateError::KeyRingExists(_) => "FileExistsError",
            CreateError::DanglingStoreLink { .. } => "DanglingStoreLink",
            CreateError::KeysDirUnreadable { .. } => "KeysDirUnreadableError",
            CreateError::Io { error, .. } => match error.kind() {
                io::ErrorKind::AlreadyExists => "FileExistsError",
                io::ErrorKind::NotFound => "FileNotFoundError",
                io::ErrorKind::PermissionDenied => "PermissionError",
                io::ErrorKind::NotADirectory => "NotADirectoryError",
                io::ErrorKind::IsADirectory => "IsADirectoryError",
                _ => "OSError",
            },
            CreateError::Sqlite(_) => "OperationalError",
            CreateError::RestoreRunning | CreateError::RestoreUnfinished(_) => "RestoreLockedError",
            CreateError::Provider(_) => "ValueError",
        }
    }
}

impl std::fmt::Display for CreateError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CreateError::Exists(p) => write!(f, "a store already exists at {}", p.display()),
            CreateError::KeysDirUnreadable {
                keys_dir, reason, ..
            } => write!(
                f,
                "the keys directory {} cannot be used ({reason}); no store is created until \
                 it can keep the new store's key",
                keys_dir.display()
            ),
            CreateError::Io { step, error } => write!(f, "{step}: {error}"),
            CreateError::Sqlite(e) => write!(f, "{e}"),
            CreateError::RestoreRunning => {
                write!(f, "{}", restore_lock::RESTORE_RUNNING_WHY)
            }
            CreateError::RestoreUnfinished(m) => write!(f, "{m}"),
            CreateError::KeyRingExists(p) => write!(
                f,
                "{} is already there: a new store's key ring is never written over another's",
                p.display()
            ),
            CreateError::DanglingStoreLink { path, target } => write!(
                f,
                "{} is a symbolic link to {target}, which is not there; no store is created \
                 through it: remove the link, or create the store at {target}",
                path.display()
            ),
            CreateError::Provider(why) => write!(f, "{why}"),
        }
    }
}

impl std::error::Error for CreateError {}

impl From<rusqlite::Error> for CreateError {
    fn from(e: rusqlite::Error) -> Self {
        CreateError::Sqlite(e)
    }
}

fn io_step<T>(step: &'static str, r: io::Result<T>) -> Result<T, CreateError> {
    r.map_err(|error| CreateError::Io { step, error })
}

/// A store made: its file and its first row.
#[derive(Debug)]
pub struct Created {
    pub path: PathBuf,
    pub store_id: String,
    pub first: LogRow,
    /// `PRAGMA synchronous` read back on the connection that committed
    /// the first row, just before it closed (2, FULL).
    pub synchronous: i64,
}

/// The boundaries between create's steps, in order: what a test's hook is
/// shown just after each (and kills the process at).
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Boundary {
    /// The store's directory made.
    DirMade,
    /// The temporary file's lock taken.
    TempLocked,
    /// The temporary file opened by SQLite, its pragmas set.
    Connected,
    /// The five schema scripts run.
    Schema,
    /// The key file written.
    KeyWritten,
    /// Meta, the first row and the digest committed.
    Committed,
    /// The head file written.
    HeadWritten,
    /// The origin anchor written.
    OriginWritten,
    /// The connection closed (no `-wal` or `-shm` left).
    Closed,
    /// Stale `-wal`, `-shm` and `-journal` at the store's name removed.
    SidecarsCleared,
    /// The store file's name linked to the temporary file.
    Linked,
    /// The temporary name removed.
    TempRemoved,
    /// The binding (`.path`) written.
    Bound,
    /// The temporary lock removed: done.
    Done,
}

impl Boundary {
    pub const ALL: [Boundary; 14] = [
        Boundary::DirMade,
        Boundary::TempLocked,
        Boundary::Connected,
        Boundary::Schema,
        Boundary::KeyWritten,
        Boundary::Committed,
        Boundary::HeadWritten,
        Boundary::OriginWritten,
        Boundary::Closed,
        Boundary::SidecarsCleared,
        Boundary::Linked,
        Boundary::TempRemoved,
        Boundary::Bound,
        Boundary::Done,
    ];
}

/// `store_schema.store_path`: the store file for a path that names it or
/// the directory it lives in.
pub fn store_path(path: &Path) -> PathBuf {
    if path.file_name() == Some(OsStr::new(STORE_FILENAME)) {
        path.to_path_buf()
    } else {
        path.join(STORE_FILENAME)
    }
}

/// `Store.create(path, keys_dir=keys_dir)` with `provider`'s values.
pub fn create(
    path: &Path,
    keys_dir: &Path,
    provider: &mut dyn Provider,
) -> Result<Created, CreateError> {
    create_with(path, keys_dir, provider, &mut |_| {})
}

/// [`create`] with `hook` shown each [`Boundary`] as it is passed (the
/// kill test exits the process there).
#[doc(hidden)]
pub fn create_with(
    path: &Path,
    keys_dir: &Path,
    provider: &mut dyn Provider,
    hook: &mut dyn FnMut(Boundary),
) -> Result<Created, CreateError> {
    let file = store_path(path);
    // A dead create's temporary names go first, before a refusal: one
    // killed after its link left a second name of the live store.
    sweep_temps(&parent_of(&file));
    if let Ok(meta) = fs::symlink_metadata(&file) {
        if meta.file_type().is_symlink() && fs::metadata(&file).is_err() {
            let target = link_text(&fs::read_link(&file).unwrap_or_default());
            return Err(CreateError::DanglingStoreLink { path: file, target });
        }
        return Err(CreateError::Exists(file));
    }
    refuse_dangling_keys_dir(keys_dir)?;
    let dir = parent_of(&file);
    io_step("make the store's directory", owner_only_dir(&dir))?;
    hook(Boundary::DirMade);
    let orphans = orphans_beside(&file);
    let _held = match restore_lock::reading(&file, restore_lock::OPEN_WAIT) {
        Ok(held) => held,
        Err(restore_lock::Refused::Running) => return Err(CreateError::RestoreRunning),
    };
    if let Some(message) = unfinished_restore(&file) {
        return Err(CreateError::RestoreUnfinished(message));
    }
    sweep_temps(&dir);
    let temp = io_step("create the temporary store", take_temp(&dir))?;
    hook(Boundary::TempLocked);
    let mut written = Vec::new();
    let made =
        build(&temp.path, keys_dir, provider, &orphans, &mut written, hook).and_then(|made| {
            clear_sidecars(&file)?;
            hook(Boundary::SidecarsCleared);
            // Again, with nothing between this and the link but the link:
            // what a third party placed there after the first pass goes.
            clear_sidecars(&file)?;
            link_into_place(&temp.path, &file)?;
            Ok(made)
        });
    let (store_id, first, synchronous) = match made {
        Ok(made) => made,
        Err(e) => {
            temp.remove();
            // Every key ring file this create made carries its own fresh
            // id: nothing else can own them.
            for path in written.iter().rev() {
                let _ = fs::remove_file(path);
            }
            return Err(e);
        }
    };
    hook(Boundary::Linked);
    let _ = fs::remove_file(&temp.path);
    sync_dir(&dir);
    hook(Boundary::TempRemoved);
    // `_claim_key_ring`: a binding that cannot be written is logged by the
    // reference and the store stands; here it is not an error either.
    let _ = write_binding(keys_dir, &store_id, &file, &mut Vec::new());
    hook(Boundary::Bound);
    temp.remove();
    hook(Boundary::Done);
    Ok(Created {
        path: file,
        store_id,
        first,
        synchronous,
    })
}

/// `restore_lock.admit`'s marker check: a `<store>.restore-lock` that
/// holds anything but whitespace (its first 64 KiB) is a restore that did
/// not finish, refused with the reference's message (`describe`, through
/// `printable`). A marker that is not UTF-8 JSON holding an object reads
/// as no record, as the reference reads one that does not parse.
pub(super) fn unfinished_restore(file: &Path) -> Option<String> {
    use std::io::Read;
    let lock = restore_lock::lock_path(file);
    let mut data = Vec::new();
    let read = fs::File::open(&lock)
        .ok()?
        .take(1 << 16)
        .read_to_end(&mut data);
    let marker: Vec<(PyValue, PyValue)> = match read {
        Err(_) => Vec::new(),
        Ok(_) if data.iter().all(|b| b.is_ascii_whitespace()) => return None,
        Ok(_) => {
            let parsed = std::str::from_utf8(&data)
                .ok()
                .and_then(|t| json::loads(&unicode::cps(t)).ok());
            match &parsed {
                Some(PyValue::Dict(items)) => items.clone(),
                _ => Vec::new(),
            }
        }
    };
    let get = |k: &str| {
        marker
            .iter()
            .find(|(key, _)| *key == PyValue::str(k))
            .map(|(_, v)| v)
    };
    let text = |k: &str| match get(k) {
        Some(PyValue::Str(s)) => Some(s.clone()),
        _ => None,
    };
    let mut parts: Vec<PyStr> = Vec::new();
    if let Some(v) = text("started_at") {
        parts.push([unicode::cps("started "), v].concat());
    }
    if let Some(PyValue::Int(n)) = get("pid") {
        parts.push(unicode::cps(&format!("by process {n}")));
    }
    if let Some(v) = text("bundle") {
        parts.push([unicode::cps("from "), v].concat());
    }
    if let Some(v) = text("stage") {
        parts.push([unicode::cps("stopped at "), v].concat());
    }
    let described = if parts.is_empty() {
        unicode::cps("no record of it reads")
    } else {
        parts.join(&unicode::cps(", ")[..])
    };
    let again = match text("bundle") {
        Some(b) => [
            unicode::cps("`bettermemory backup restore "),
            b,
            unicode::cps(" --yes`"),
        ]
        .concat(),
        None => unicode::cps("`bettermemory backup restore BUNDLE --yes`"),
    };
    Some(format!(
        "{} is not opened: a restore of it did not finish ({}; {} keeps its record), and \
         the store may be part restored. Run the restore again to finish it ({}), or open \
         the store as it stands after `bettermemory backup unlock --yes`",
        file.display(),
        printable(&described),
        lock.display(),
        printable(&again)
    ))
}

/// `_printable.printable`: control and format characters, line and
/// paragraph separators and lone surrogates as their Python escapes; a
/// tab kept.
fn printable(text: &[u32]) -> String {
    let mut out = String::new();
    for &cp in text {
        let hide = cp != 9 && matches!(unicode::category(cp), "Cc" | "Cf" | "Zl" | "Zp" | "Cs");
        match (hide, cp) {
            (false, _) => out.push(char::from_u32(cp).unwrap_or('\u{fffd}')),
            (true, 10) => out.push_str("\\n"),
            (true, 13) => out.push_str("\\r"),
            (true, c) if c < 0x100 => out.push_str(&format!("\\x{c:02x}")),
            (true, c) if c < 0x10000 => out.push_str(&format!("\\u{c:04x}")),
            (true, c) => out.push_str(&format!("\\U{c:08x}")),
        }
    }
    out
}

pub(super) fn parent_of(file: &Path) -> PathBuf {
    match file.parent() {
        Some(p) if !p.as_os_str().is_empty() => p.to_path_buf(),
        _ => PathBuf::from("."),
    }
}

/// `prepare_store_dir`'s check: the nearest entry on the way to the keys
/// directory that is there must not be a symbolic link to nothing.
fn refuse_dangling_keys_dir(keys: &Path) -> Result<(), CreateError> {
    for entry in keys.ancestors() {
        if entry.as_os_str().is_empty() || fs::symlink_metadata(entry).is_err() {
            continue;
        }
        let dangling = fs::symlink_metadata(entry).is_ok_and(|m| m.file_type().is_symlink())
            && fs::metadata(entry).is_err();
        if dangling {
            let target = fs::read_link(entry).unwrap_or_default();
            let target = link_text(&target);
            let mut reason = format!("it is a symbolic link to {target}, which is not there");
            if entry != keys {
                reason = format!(
                    "{}, on the way to it, {}",
                    entry.display(),
                    reason.trim_start_matches("it ")
                );
            }
            return Err(CreateError::KeysDirUnreadable {
                keys_dir: keys.to_path_buf(),
                link: entry.to_path_buf(),
                reason,
            });
        }
        break;
    }
    Ok(())
}

/// `log._link_target`: a link's text without Windows' verbatim prefix.
fn link_text(target: &Path) -> String {
    let text = target.display().to_string();
    if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        return format!(r"\\{rest}");
    }
    text.strip_prefix(r"\\?\").unwrap_or(&text).to_owned()
}

/// `_fsutil.ensure_owner_only_dir(path, parents=True)`: each directory it
/// makes is 0700, and the leaf, made or not, loses group and other bits.
pub(super) fn owner_only_dir(path: &Path) -> io::Result<()> {
    let mut missing: Vec<&Path> = path
        .ancestors()
        .skip(1)
        .filter(|p| !p.as_os_str().is_empty() && !p.exists())
        .collect();
    missing.reverse();
    for ancestor in missing {
        make_dir(ancestor)?;
        tighten(ancestor);
    }
    make_dir(path)?;
    tighten(path);
    Ok(())
}

/// `Path.mkdir(mode=0o700, exist_ok=True)`: an existing directory is
/// fine, anything else there is FileExistsError.
fn make_dir(path: &Path) -> io::Result<()> {
    #[cfg(unix)]
    let builder = {
        use std::os::unix::fs::DirBuilderExt;
        let mut builder = fs::DirBuilder::new();
        builder.mode(0o700);
        builder
    };
    // No mode bits to ask for (a `mut` unused here failed the Windows
    // runner's clippy).
    #[cfg(not(unix))]
    let builder = fs::DirBuilder::new();
    match builder.create(path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == io::ErrorKind::AlreadyExists && path.is_dir() => Ok(()),
        Err(e) => Err(e),
    }
}

#[cfg(unix)]
fn tighten(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    if let Ok(meta) = fs::metadata(path) {
        let mode = meta.permissions().mode() & 0o7777;
        if mode & 0o077 != 0 {
            let _ = fs::set_permissions(path, fs::Permissions::from_mode(mode & 0o700));
        }
    }
}

#[cfg(not(unix))]
fn tighten(_path: &Path) {}

/// A new file, owner-only from its first byte, failing when the name is
/// taken.
pub(super) fn new_private_file(path: &Path) -> io::Result<fs::File> {
    let mut options = fs::OpenOptions::new();
    options.read(true).write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options.open(path)
}

#[cfg(unix)]
pub(super) fn sync_dir(dir: &Path) {
    if let Ok(d) = fs::File::open(dir) {
        let _ = bettermemory_sys::fsync(&d);
    }
}

#[cfg(not(unix))]
pub(super) fn sync_dir(_dir: &Path) {}

pub(super) fn with_suffix(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(suffix);
    path.with_file_name(name)
}

/// The temporary file and its held lock.
struct Temp {
    path: PathBuf,
    lock_path: PathBuf,
    lock: fs::File,
}

impl Temp {
    /// Remove the temporary file, its SQLite siblings, then the lock.
    fn remove(self) {
        for suffix in ["", "-wal", "-shm", "-journal"] {
            let _ = fs::remove_file(with_suffix(&self.path, suffix));
        }
        drop(self.lock);
        let _ = fs::remove_file(&self.lock_path);
    }
}

/// A lock file made under a fresh name and locked, then checked to be the
/// file at that name still (a sweep may have removed it in the instant
/// before the lock was taken: then another name is tried).
fn take_temp(dir: &Path) -> io::Result<Temp> {
    for _ in 0..100 {
        let n = COUNTER.fetch_add(1, Ordering::Relaxed);
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.subsec_nanos());
        let path = dir.join(format!("{TEMP_PREFIX}{}-{nanos:x}-{n}", std::process::id()));
        let lock_path = with_suffix(&path, LOCK_SUFFIX);
        let lock = match new_private_file(&lock_path) {
            Ok(f) => f,
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(e),
        };
        lock.lock()?;
        if !same_file(&lock, &lock_path) {
            continue;
        }
        return Ok(Temp {
            path,
            lock_path,
            lock,
        });
    }
    Err(io::Error::new(
        io::ErrorKind::AlreadyExists,
        "no temporary name",
    ))
}

#[cfg(unix)]
fn same_file(file: &fs::File, path: &Path) -> bool {
    use std::os::unix::fs::MetadataExt;
    match (file.metadata(), fs::metadata(path)) {
        (Ok(a), Ok(b)) => a.ino() == b.ino() && a.dev() == b.dev(),
        _ => false,
    }
}

/// Windows removes no open file, so the name cannot have gone.
#[cfg(not(unix))]
fn same_file(_file: &fs::File, path: &Path) -> bool {
    path.exists()
}

/// Remove the temporary files an earlier create left in `dir` that no
/// live create holds: those whose lock file (a regular file, never a link
/// or a FIFO planted under the name) can be locked and is still the file
/// at that name (its creator exited or was killed), and those with no lock
/// file over an hour old. The lock file is removed while it is still
/// held, so a create that opened it in between finds, once it locks, that
/// the file at the name is no longer its own, and takes another
/// ([`take_temp`]). Only temporary names are removed: a store linked into
/// place from one keeps its own name. Returns how many temporary files went.
///
/// Liveness rests on an `flock` (std's `File::try_lock`) held for the
/// create's whole life. Where locks are emulated per process (NFS on
/// Linux), two creates in one process do not exclude each other.
pub fn sweep_temps(dir: &Path) -> usize {
    let Ok(entries) = fs::read_dir(dir) else {
        return 0;
    };
    let mut removed = 0;
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            continue;
        };
        if !name.starts_with(TEMP_PREFIX) {
            continue;
        }
        let path = entry.path();
        if let Some(base) = name.strip_suffix(LOCK_SUFFIX) {
            if !fs::symlink_metadata(&path).is_ok_and(|m| m.file_type().is_file()) {
                continue;
            }
            let Ok(lock) = fs::OpenOptions::new().write(true).open(&path) else {
                continue;
            };
            if lock.try_lock().is_err() || !same_file(&lock, &path) {
                continue;
            }
            let temp = dir.join(base);
            for suffix in ["", "-wal", "-shm", "-journal"] {
                if fs::remove_file(with_suffix(&temp, suffix)).is_ok() && suffix.is_empty() {
                    removed += 1;
                }
            }
            // Removed while held, then let go.
            let _ = fs::remove_file(&path);
            drop(lock);
            continue;
        }
        let base = name
            .strip_suffix("-wal")
            .or_else(|| name.strip_suffix("-shm"))
            .or_else(|| name.strip_suffix("-journal"))
            .unwrap_or(name);
        if fs::symlink_metadata(dir.join(format!("{base}{LOCK_SUFFIX}"))).is_ok() {
            continue;
        }
        let old = fs::symlink_metadata(&path)
            .ok()
            .and_then(|m| m.modified().ok())
            .and_then(|m| m.elapsed().ok())
            .is_some_and(|age| age > UNLOCKED_GRACE);
        if old && fs::remove_file(&path).is_ok() && base == name {
            removed += 1;
        }
    }
    removed
}

/// `store_schema.orphans_beside`: the saved orphans beside the store file,
/// `memory.sqlite.orphaned-*.sqlite` regular files, oldest first
/// (`orphan_order`); none when the directory cannot be listed.
fn orphans_beside(file: &Path) -> Vec<PyStr> {
    let dir = parent_of(file);
    let Ok(entries) = fs::read_dir(&dir) else {
        return Vec::new();
    };
    let prefix: Vec<u32> = unicode::cps(&format!("{STORE_FILENAME}.orphaned-"));
    let suffix: Vec<u32> = unicode::cps(".sqlite");
    let mut found: Vec<((PyStr, u128), PyStr)> = Vec::new();
    for entry in entries.flatten() {
        let name = os_text(&entry.file_name());
        if name.len() < prefix.len() + suffix.len()
            || !name.starts_with(&prefix)
            || !name.ends_with(&suffix)
            || !entry.path().is_file()
        {
            continue;
        }
        found.push((orphan_order(&name), name));
    }
    found.sort();
    found.into_iter().map(|(_, n)| n).collect()
}

/// `orphan_order`: the regex `\.orphaned-(?P<stamp>[^.]*?)(?:-(?P<n>\d+))?\.sqlite\Z`
/// searched in the name: (stamp, n or 1), else (name, 1).
fn orphan_order(name: &[u32]) -> (PyStr, u128) {
    let marker = unicode::cps(".orphaned-");
    let end = name.len() - 7; // before ".sqlite"
    let mut at = 0;
    while at + marker.len() <= end {
        if name[at..].starts_with(&marker) {
            let rest = &name[at + marker.len()..end];
            if !rest.contains(&('.' as u32)) {
                // The lazy stamp: the shortest prefix the rest of which is
                // nothing or `-` and digits.
                for cut in 0..=rest.len() {
                    let tail = &rest[cut..];
                    if tail.is_empty() {
                        return (rest[..cut].to_vec(), 1);
                    }
                    if tail.len() > 1 && tail[0] == '-' as u32 {
                        let digits: Option<String> = tail[1..]
                            .iter()
                            .map(|&c| char::from_u32(c).and_then(|c| c.to_digit(10)))
                            .map(|d| d.and_then(|d| char::from_digit(d, 10)))
                            .collect();
                        if let Some(n) = digits.and_then(|d| d.parse::<u128>().ok()) {
                            return (rest[..cut].to_vec(), n);
                        }
                    }
                }
            }
        }
        at += 1;
    }
    (name.to_vec(), 1)
}

/// A file name or path as Python's `str` holds it: UTF-8 with undecodable
/// bytes as lone surrogates (`surrogateescape`) on unix; UTF-16 with lone
/// surrogates kept on Windows.
fn os_text(text: &OsStr) -> PyStr {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        let bytes = text.as_bytes();
        let mut out = Vec::with_capacity(bytes.len());
        for chunk in bytes.utf8_chunks() {
            out.extend(chunk.valid().chars().map(|c| c as u32));
            out.extend(chunk.invalid().iter().map(|&b| 0xDC00 + u32::from(b)));
        }
        out
    }
    #[cfg(not(unix))]
    {
        use std::os::windows::ffi::OsStrExt;
        char::decode_utf16(text.encode_wide())
            .map(|r| match r {
                Ok(c) => c as u32,
                Err(e) => u32::from(e.unpaired_surrogate()),
            })
            .collect()
    }
}

/// Everything up to the closed connection, in the temporary file: the
/// store id, the first row and the connection's `PRAGMA synchronous`.
fn build(
    temp: &Path,
    keys_dir: &Path,
    provider: &mut dyn Provider,
    orphans: &[PyStr],
    written: &mut Vec<PathBuf>,
    hook: &mut dyn FnMut(Boundary),
) -> Result<(String, LogRow, i64), CreateError> {
    io_step("create the temporary store", new_private_file(temp))?;
    let conn = Connection::open_with_flags(
        temp,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    conn.busy_timeout(super::BUSY_TIMEOUT)?;
    // `_connect`'s pragmas, in its order.
    conn.execute_batch(&format!("PRAGMA page_size = {PAGE_SIZE}"))?;
    conn.execute_batch("PRAGMA foreign_keys = ON")?;
    conn.query_row("PRAGMA journal_mode = WAL", [], |_| Ok(()))?;
    conn.query_row("PRAGMA schema_version", [], |_| Ok(()))?;
    // FULL, not NORMAL: in WAL mode NORMAL does not sync the WAL at a
    // COMMIT, and the head written after it is synced (ENG-328).
    conn.execute_batch("PRAGMA synchronous = FULL")?;
    hook(Boundary::Connected);
    for text in super::admission::schema_texts() {
        conn.execute_batch(text)?;
    }
    hook(Boundary::Schema);
    let store_id = checked_store_id(provider.store_id())?;
    let key = io_step("draw the key", provider.key())?;
    if key.bytes().len() != KEY_BYTES {
        return Err(CreateError::Provider("a key is 32 bytes"));
    }
    io_step("make the keys directory", owner_only_dir(keys_dir))?;
    sweep_ring_temps(keys_dir);
    for suffix in [".key", ".head", ".origin", ".path"] {
        let there = keys_dir.join(format!("{store_id}{suffix}"));
        if fs::symlink_metadata(&there).is_ok() {
            return Err(CreateError::KeyRingExists(there));
        }
    }
    io_step(
        "write the key",
        write_new(
            &keys_dir.join(format!("{store_id}.key")),
            key.bytes(),
            written,
        ),
    )?;
    hook(Boundary::KeyWritten);
    let membership = io_step("draw the membership id", provider.membership_store_id())?;
    let created = io_step("read the clock", provider.now())?.isoformat_utc();
    let tokenizer = lexicon::python_tokenizer_fingerprint();
    let engine = provider.engine_version();
    let mut meta: Vec<(&str, String)> = vec![
        ("store_id", store_id.clone()),
        ("schema_version", super::SCHEMA_VERSION.to_string()),
        ("schema_revision", super::SCHEMA_REVISION.to_string()),
        ("min_reader_revision", MIN_READER_REVISION.to_string()),
        ("min_writer_revision", MIN_WRITER_REVISION.to_string()),
        ("tokenizer_fingerprint", tokenizer.clone()),
        ("engine_version", engine.clone()),
        ("key_fingerprint", fingerprint(key.bytes())),
        ("created", created.clone()),
        (MEMBERSHIP_STORE_ID, membership),
    ];
    if !orphans.is_empty() {
        let names = PyValue::List(orphans.iter().map(|n| PyValue::Str(n.clone())).collect());
        let text = json::dumps(&names, DumpOptions::DEFAULT)
            .map_err(|_| CreateError::Provider("an orphan's name"))?;
        meta.push((CREATED_AFTER_ORPHANS, json::utf8_text(&text)));
    }
    let payload = PyValue::Dict(vec![
        (PyValue::str("store_id"), PyValue::str(&store_id)),
        (
            PyValue::str("schema_version"),
            PyValue::int(i128::from(super::SCHEMA_VERSION)),
        ),
        (
            PyValue::str("tokenizer_fingerprint"),
            PyValue::str(&tokenizer),
        ),
        (PyValue::str("engine_version"), PyValue::str(&engine)),
        (PyValue::str("created"), PyValue::str(&created)),
    ]);
    let ts = io_step("read the clock", provider.now())?.isoformat_utc();
    let first = log::next_row(None, key.bytes(), "store_created", &payload, None, &ts)
        .map_err(|_| CreateError::Provider("the first row's payload"))?;
    conn.execute_batch("BEGIN IMMEDIATE")?;
    let committed = (|| -> rusqlite::Result<()> {
        {
            let mut insert = conn.prepare("INSERT INTO meta(key, value) VALUES (?, ?)")?;
            for (k, v) in &meta {
                insert.execute([k, &v.as_str()])?;
            }
        }
        conn.execute(
            "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) \
             VALUES (?, ?, ?, ?, ?, ?, ?)",
            rusqlite::params![
                first.seq,
                first.ts,
                first.session,
                first.kind,
                first.payload,
                first.prev_mac,
                first.mac
            ],
        )?;
        digest::build_empty(&conn)?;
        conn.execute_batch("COMMIT")
    })();
    if let Err(e) = committed {
        let _ = conn.execute_batch("ROLLBACK");
        return Err(e.into());
    }
    hook(Boundary::Committed);
    let head_ts = io_step("read the clock", provider.now())?.isoformat_utc();
    io_step(
        "write the head",
        locked_write(
            &keys_dir.join(format!("{store_id}.head")),
            head_file_text(first.seq, &first.mac, &head_ts).as_bytes(),
            written,
        ),
    )?;
    hook(Boundary::HeadWritten);
    let origin = PyValue::Dict(vec![
        (PyValue::str("seq"), PyValue::int(i128::from(first.seq))),
        (PyValue::str("mac"), PyValue::str(&first.mac)),
        (
            PyValue::str("key_fingerprint"),
            PyValue::str(&fingerprint(key.bytes())),
        ),
    ]);
    let origin = json::dumps(&origin, DumpOptions::DEFAULT)
        .map(|t| json::utf8_text(&t))
        .map_err(|_| CreateError::Provider("the origin anchor"))?;
    io_step(
        "write the origin anchor",
        locked_write(
            &keys_dir.join(format!("{store_id}.origin")),
            origin.as_bytes(),
            written,
        ),
    )?;
    hook(Boundary::OriginWritten);
    let synchronous: i64 = conn.query_row("PRAGMA synchronous", [], |r| r.get(0))?;
    conn.close().map_err(|(_, e)| CreateError::Sqlite(e))?;
    for suffix in ["-wal", "-shm"] {
        if with_suffix(temp, suffix).exists() {
            return Err(CreateError::Io {
                step: "close the temporary store",
                error: io::Error::other(format!("its {suffix} stayed")),
            });
        }
    }
    io_step(
        "sync the temporary store",
        fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(temp)
            .and_then(|f| bettermemory_sys::fsync(&f)),
    )?;
    hook(Boundary::Closed);
    Ok((store_id, first, synchronous))
}

/// The provider's store id, refused unless it is 32 lowercase hex digits
/// (`KeyRing` names its files after it).
fn checked_store_id(drawn: io::Result<String>) -> Result<String, CreateError> {
    let id = io_step("draw the store id", drawn)?;
    if !super::keys::is_store_id(&id) {
        return Err(CreateError::Provider(
            "not a store id: a store id is 32 lowercase hex digits",
        ));
    }
    Ok(id)
}

/// `_fsutil.atomic_write_bytes(path, data, mode_before_rename=0o600)`,
/// never replacing: a private temporary file beside it
/// (`<name>.<pid>x<n>.tmp`), synced, linked to the name (which fails when
/// anything is there), the temporary name removed, the directory synced.
/// Where there are no hard links the file is written in place with an
/// exclusive create. The path is pushed to `written` once it is there.
fn write_new(path: &Path, data: &[u8], written: &mut Vec<PathBuf>) -> io::Result<()> {
    let n = COUNTER.fetch_add(1, Ordering::Relaxed);
    let tmp = with_suffix(path, &format!(".{}x{n}.tmp", std::process::id()));
    let staged = (|| {
        let mut f = new_private_file(&tmp)?;
        f.write_all(data)?;
        bettermemory_sys::fsync(&f)
    })();
    if let Err(e) = staged {
        let _ = fs::remove_file(&tmp);
        return Err(e);
    }
    let linked = fs::hard_link(&tmp, path);
    let _ = fs::remove_file(&tmp);
    match linked {
        Ok(()) => written.push(path.to_path_buf()),
        Err(e) if e.kind() == io::ErrorKind::AlreadyExists => return Err(e),
        Err(_) => {
            let mut f = new_private_file(path)?;
            written.push(path.to_path_buf());
            f.write_all(data)?;
            bettermemory_sys::fsync(&f)?;
        }
    }
    sync_dir(&parent_of(path));
    Ok(())
}

/// Remove this unit's own staging files in the keys directory left by a
/// dead create (`<32 hex>.<key|head|origin|path>.<pid>x<n>.tmp`, exactly)
/// once older than the grace period. Never a key file itself.
pub(super) fn sweep_ring_temps(keys_dir: &Path) {
    let Ok(entries) = fs::read_dir(keys_dir) else {
        return;
    };
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            continue;
        };
        if !is_ring_temp(name) {
            continue;
        }
        let old = fs::symlink_metadata(entry.path())
            .ok()
            .filter(fs::Metadata::is_file)
            .and_then(|m| m.modified().ok())
            .and_then(|m| m.elapsed().ok())
            .is_some_and(|age| age > UNLOCKED_GRACE);
        if old {
            let _ = fs::remove_file(entry.path());
        }
    }
}

/// `<32 lowercase hex>.<key|head|origin|path>.<digits>x<digits>.tmp`.
fn is_ring_temp(name: &str) -> bool {
    let Some(rest) = name.strip_suffix(".tmp") else {
        return false;
    };
    let (Some(id), Some(rest)) = (rest.get(..32), rest.get(32..)) else {
        return false;
    };
    if !super::keys::is_store_id(id) {
        return false;
    }
    let mut parts = rest.splitn(3, '.');
    let (Some(""), Some(kind), Some(tag)) = (parts.next(), parts.next(), parts.next()) else {
        return false;
    };
    let digits = |t: &str| !t.is_empty() && t.bytes().all(|b| b.is_ascii_digit());
    matches!(kind, "key" | "head" | "origin" | "path")
        && tag
            .split_once('x')
            .is_some_and(|(a, b)| digits(a) && digits(b))
}

/// `write_new` under `_fsutil.flock_excl(path)`: `<path>.lock` (0600,
/// kept) locked for the write; a lock file this create made is pushed to
/// `written` too.
fn locked_write(path: &Path, data: &[u8], written: &mut Vec<PathBuf>) -> io::Result<()> {
    let lock_path = with_suffix(path, ".lock");
    let lock = match new_private_file(&lock_path) {
        Ok(f) => {
            written.push(lock_path.clone());
            f
        }
        Err(e) if e.kind() == io::ErrorKind::AlreadyExists => fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(&lock_path)?,
        Err(e) => return Err(e),
    };
    lock.lock()?;
    let out = write_new(path, data, written);
    drop(lock);
    out
}

/// Remove stale SQLite sidecars at the store's name before the link: with
/// no database file there they are orphans (another store's live WAL, a
/// hot journal, junk), which SQLite would replay onto the linked file at
/// its first open. The reference's create ends the same way: SQLite,
/// creating the file there, discards them. Each is removed right after
/// looking again that no file is at the store's name.
fn clear_sidecars(file: &Path) -> Result<(), CreateError> {
    for suffix in ["-wal", "-shm", "-journal"] {
        if fs::symlink_metadata(file).is_ok() {
            return Err(CreateError::Exists(file.to_path_buf()));
        }
        match fs::remove_file(with_suffix(file, suffix)) {
            Ok(()) => {}
            Err(e) if e.kind() == io::ErrorKind::NotFound => {}
            Err(e) => return io_step("remove a stale sidecar of the store", Err(e)),
        }
    }
    Ok(())
}

/// `KeyRing._write_binding`: `{"path": <the store file, resolved>}`.
fn write_binding(
    keys_dir: &Path,
    store_id: &str,
    file: &Path,
    written: &mut Vec<PathBuf>,
) -> io::Result<()> {
    let resolved = fs::canonicalize(file)?;
    let text = os_text(resolved.as_os_str());
    #[cfg(not(unix))]
    let text = {
        // `Path.resolve` spells no verbatim prefix.
        let s = |t: &str| unicode::cps(t);
        if text.starts_with(&s(r"\\?\UNC\")) {
            [s(r"\\"), text[8..].to_vec()].concat()
        } else if text.starts_with(&s(r"\\?\")) {
            text[4..].to_vec()
        } else {
            text
        }
    };
    let value = PyValue::Dict(vec![(PyValue::str("path"), PyValue::Str(text))]);
    let json = json::dumps(&value, DumpOptions::DEFAULT)
        .map(|t| json::utf8_text(&t))
        .map_err(|_| io::Error::other("the binding"))?;
    locked_write(
        &keys_dir.join(format!("{store_id}.path")),
        json.as_bytes(),
        written,
    )
}

/// Link the closed temporary file to the store's name, failing when a
/// file appeared there meanwhile (`link` never replaces).
///
/// Where the file system has no hard links (FAT, some network shares),
/// `std::fs::rename` is the only move, and it replaces an existing file
/// on every platform (`rename(2)`; on Windows a replacing move), so a
/// look-then-rename could overwrite a store another process created in
/// between. Instead the name is claimed first with an exclusive create of
/// an empty placeholder (fails when anything is there), and only that
/// placeholder is renamed over. What this leaves undecided: a kill
/// between the claim and the rename leaves an empty `memory.sqlite`,
/// which neither build opens (both refuse to create over it), as the
/// reference's own window does; it is the only boundary where native is
/// no better than the reference, and only on such file systems.
fn link_into_place(temp: &Path, file: &Path) -> Result<(), CreateError> {
    match fs::hard_link(temp, file) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {
            Err(CreateError::Exists(file.to_path_buf()))
        }
        Err(_) => {
            match new_private_file(file) {
                Ok(placeholder) => drop(placeholder),
                Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {
                    return Err(CreateError::Exists(file.to_path_buf()));
                }
                Err(e) => return io_step("claim the store's name", Err(e)),
            }
            let moved = fs::rename(temp, file);
            if moved.is_err() {
                let _ = fs::remove_file(file);
            }
            io_step("move the store into place", moved)
        }
    }
}
