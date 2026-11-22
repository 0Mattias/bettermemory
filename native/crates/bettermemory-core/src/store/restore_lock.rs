//! The reader's side of the restore lock (`restore_lock.reading`): a read
//! of a store holds every restore of it off from before it connects until
//! it closes, and a read that meets a running restore is refused
//! (`RestoreRunningError`, exit 75 from the reference's read-only
//! commands). Nothing is created: no lock file, no marker.
//!
//! - POSIX: the store file's directory, opened read-only and locked shared
//!   with `flock` (std's `File::try_lock_shared`), tried every 50 ms for
//!   `OPEN_WAIT_SECONDS` (2 s); not had by then, the read is refused. The
//!   hold is the open directory, kept until the [`super::StoreFile`]
//!   drops.
//! - A POSIX directory `flock` cannot lock (an error other than "held
//!   elsewhere"): record mode, as the reference puts that directory in for
//!   the process. The record file `<store>.restore-lock` is locked shared
//!   if it is there and let go at once; held past the wait, the read is
//!   refused; absent, or unlockable too, nothing is held (the reference
//!   holds nothing there either: `_store_handle` is Windows-only).
//! - Windows: the record file, locked exclusively (the reference's
//!   `msvcrt.locking` on one byte at 1 MiB; std locks the whole file,
//!   which overlaps that byte, so each sees the other) and let go at once;
//!   then the store file is held open without delete sharing, so no
//!   restore's swap can rename it, and the record is looked at again
//!   behind the handle while it stays held, up to the wait. Untested here.
//!
//! Not ported: the reference's "this thread holds it" pass (a restore's
//! own read of its store), since no native restore exists; the reference's
//! one-time log line for a directory in record mode; the process-wide
//! memory of such directories (each native open finds it again).
//!
//! The writing open's side (`restore_lock.admit`, [`admit`]): the same
//! lock taken shared within the wait and kept as [`Admission::Shared`]
//! until the open returns; held past the wait, the open is refused as
//! running ([`RestoreLocked`], `RestoreLockedError`); had, but a marker
//! is beside the store (`read_marker`: anything but `bytes.strip()`
//! whitespace in its first 64 KiB), it is let go and the open is refused
//! as unfinished. Where the lock is the record file (record mode, and
//! Windows), the open is admitted by a [`RecordCheck`], which its
//! [`Admission::confirm`] repeats once the connection has read the store
//! and which refuses a store file swapped in meanwhile (`replaced`).
//! [`restore_state`] is the same look without a hold. The reference's
//! `_this_thread_holds` is the `mine` argument: no native restore exists,
//! so every caller here passes one that names nothing. As for the reader,
//! record mode is found again on each call (no process-wide memory) and
//! no log line is written.

use std::collections::HashMap;
use std::fs::File;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::thread::ThreadId;
use std::time::{Duration, Instant};

use crate::json;
use crate::printable::printable;
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode;

/// `restore_lock.OPEN_WAIT_SECONDS`.
pub const OPEN_WAIT: Duration = Duration::from_secs(2);
/// `restore_lock._POLL_SECONDS`.
const POLL: Duration = Duration::from_millis(50);
/// `restore_lock.LOCK_SUFFIX`.
pub const LOCK_SUFFIX: &str = ".restore-lock";
/// `restore_lock.RESTORE_RUNNING_STATUS`: EX_TEMPFAIL.
pub const RESTORE_RUNNING_STATUS: i32 = 75;
/// `RestoreRunningError`'s reason, after `<path> is not opened: `.
pub const RESTORE_RUNNING_WHY: &str = "a restore is running; try again when it has finished";

/// `lock_path`: `<store file>.restore-lock` beside the store file.
pub fn lock_path(store_file: &Path) -> PathBuf {
    let mut name = store_file.file_name().unwrap_or_default().to_os_string();
    name.push(LOCK_SUFFIX);
    store_file.with_file_name(name)
}

/// What a read holds while it lasts: dropped after the connection.
#[derive(Debug)]
pub struct ReadHold {
    _file: File,
}

/// Why `reading` refused or failed.
#[derive(Debug)]
pub enum Refused {
    /// `RestoreRunningError`.
    Running,
}

/// One try at the lock: taken, held by another (past the wait), or a
/// file system that refuses the lock.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Try {
    Taken,
    Held,
    Unlockable,
}

/// `_take` on the record file: an Unlockable record file counts as taken,
/// so only a marker refuses; only a lock held by another leaves it not taken.
pub fn record_file_taken(attempt: Try) -> bool {
    !matches!(attempt, Try::Held)
}

/// `restore_lock._WINDOWS_LOCK_OFFSET`: the byte `msvcrt.locking` locks,
/// past any marker, so a marker reads through any handle while it is held.
#[cfg(windows)]
const WINDOWS_LOCK_OFFSET: u64 = 1 << 20;

#[cfg(not(windows))]
fn try_lock(file: &File, shared: bool) -> Try {
    let got = if shared {
        file.try_lock_shared()
    } else {
        file.try_lock()
    };
    match got {
        Ok(()) => Try::Taken,
        Err(std::fs::TryLockError::WouldBlock) => Try::Held,
        Err(std::fs::TryLockError::Error(e)) if e.kind() == io::ErrorKind::Interrupted => Try::Held,
        Err(std::fs::TryLockError::Error(_)) => Try::Unlockable,
    }
}

/// `_try_lock` on Windows: `msvcrt.locking(fd, LK_NBLCK, 1)` at
/// `_WINDOWS_LOCK_OFFSET`, exclusive whatever `shared` says; any error is
/// "held" (the reference returns False on every OSError there).
#[cfg(windows)]
fn try_lock(file: &File, shared: bool) -> Try {
    let _ = shared;
    match bettermemory_sys::lock_byte(file, WINDOWS_LOCK_OFFSET) {
        Ok(()) => Try::Taken,
        Err(_) => Try::Held,
    }
}

/// `_unlock`: `flock(LOCK_UN)`, or on Windows `LK_UNLCK` of the byte
/// [`try_lock`] locked; errors ignored as the reference suppresses them.
fn unlock_file(file: &File) {
    #[cfg(not(windows))]
    let _ = file.unlock();
    #[cfg(windows)]
    let _ = bettermemory_sys::unlock_byte(file, WINDOWS_LOCK_OFFSET);
}

/// `_lock_within`: Taken, Held (past `wait`) or Unlockable.
fn lock_within(file: &File, shared: bool, wait: Duration) -> Try {
    let deadline = Instant::now() + wait;
    loop {
        match try_lock(file, shared) {
            Try::Held if Instant::now() < deadline => std::thread::sleep(POLL),
            other => return other,
        }
    }
}

#[cfg(not(windows))]
fn parent_of(store_file: &Path) -> PathBuf {
    match store_file.parent() {
        Some(p) if !p.as_os_str().is_empty() => p.to_path_buf(),
        _ => PathBuf::from("."),
    }
}

/// The record file, opened without creating it; None when absent or not
/// openable (the reference's `_open_record` returns None only when absent
/// and raises otherwise, which `reading` turns into "nothing to hold").
fn open_record(store_file: &Path) -> Option<File> {
    record_options()
        .read(true)
        .write(true)
        .open(lock_path(store_file))
        .ok()
}

/// Options for every open of the record file. On Windows the share mode is
/// that of the reference's `os.open` (the CRT's `_SH_DENYNO`):
/// FILE_SHARE_READ | FILE_SHARE_WRITE without FILE_SHARE_DELETE, so a
/// release's unlink fails while any handle is open and leaves the empty
/// file, where std's default share mode would let it go delete-pending and
/// refuse the next open of the name.
fn record_options() -> std::fs::OpenOptions {
    #[cfg_attr(not(windows), allow(unused_mut))]
    let mut options = std::fs::OpenOptions::new();
    #[cfg(windows)]
    std::os::windows::fs::OpenOptionsExt::share_mode(&mut options, 0x1 | 0x2);
    options
}

/// `restore_lock.reading(store_file)` with `wait` for the reference's
/// `OPEN_WAIT_SECONDS` (a parameter for tests).
pub fn reading(store_file: &Path, wait: Duration) -> Result<Option<ReadHold>, Refused> {
    #[cfg(not(windows))]
    {
        let Ok(dir) = File::open(parent_of(store_file)) else {
            return Ok(None);
        };
        match lock_within(&dir, true, wait) {
            Try::Taken => Ok(Some(ReadHold { _file: dir })),
            Try::Held => Err(Refused::Running),
            Try::Unlockable => {
                drop(dir);
                record_mode_reading(store_file, wait)
            }
        }
    }
    #[cfg(windows)]
    {
        windows_reading(store_file, wait)
    }
}

/// `reading` on a POSIX directory `flock` cannot lock (record mode): the
/// record file locked shared within `wait` and let go; refused when held
/// past it; nothing held either way (the reference holds the store file
/// only on Windows). Public so a test can run it on a directory that
/// does lock (no local file system here refuses `flock`).
#[doc(hidden)]
pub fn record_mode_reading(store_file: &Path, wait: Duration) -> Result<Option<ReadHold>, Refused> {
    let Some(record) = open_record(store_file) else {
        return Ok(None);
    };
    match lock_within(&record, true, wait) {
        Try::Held => Err(Refused::Running),
        Try::Taken | Try::Unlockable => {
            unlock_file(&record);
            Ok(None)
        }
    }
}

#[cfg(windows)]
fn record_running(store_file: &Path, wait: Duration) -> bool {
    let Some(record) = open_record(store_file) else {
        return false;
    };
    match lock_within(&record, false, wait) {
        Try::Held => true,
        Try::Taken | Try::Unlockable => {
            unlock_file(&record);
            false
        }
    }
}

#[cfg(windows)]
fn store_handle(store_file: &Path) -> Option<File> {
    use std::os::windows::fs::OpenOptionsExt;
    // FILE_SHARE_READ | FILE_SHARE_WRITE, not FILE_SHARE_DELETE.
    std::fs::OpenOptions::new()
        .read(true)
        .share_mode(0x1 | 0x2)
        .open(store_file)
        .ok()
}

#[cfg(windows)]
fn windows_reading(store_file: &Path, wait: Duration) -> Result<Option<ReadHold>, Refused> {
    if record_running(store_file, wait) {
        return Err(Refused::Running);
    }
    let handle = match store_handle(store_file) {
        Some(h) => h,
        None => {
            // The swap's gap: look again, then try the handle once more.
            if record_running(store_file, wait) {
                return Err(Refused::Running);
            }
            match store_handle(store_file) {
                Some(h) => h,
                None => return Ok(None),
            }
        }
    };
    if record_running(store_file, wait) {
        return Err(Refused::Running);
    }
    Ok(Some(ReadHold { _file: handle }))
}

/// `restore_lock._MARKER_CAP_BYTES`: the most of a marker read.
const MARKER_CAP: usize = 1 << 16;

/// `_file_key`: the device and inode of an open file (`_HELD`'s key).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct FileKey {
    pub dev: u64,
    pub ino: u64,
}

/// What a caller's own hold covers (`_this_thread_holds`).
pub type Mine = fn(FileKey) -> bool;

/// `fstat` of an open file: on Windows the volume serial number and the
/// file index (`GetFileInformationByHandle`), as `os.fstat` fills them.
fn file_key(file: &File) -> io::Result<Option<FileKey>> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let m = file.metadata()?;
        Ok(Some(FileKey {
            dev: m.dev(),
            ino: m.ino(),
        }))
    }
    #[cfg(windows)]
    {
        let (dev, ino) = bettermemory_sys::file_index(file)?;
        Ok(Some(FileKey { dev, ino }))
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = file;
        Ok(None)
    }
}

/// `_Target`.
struct Target {
    file: File,
    key: Option<FileKey>,
    record: bool,
    taken: bool,
    mine: bool,
}

/// The record file opened read-write, made (owner-only) when `create`.
fn open_lock_file(lock: &Path, create: bool) -> io::Result<File> {
    let mut options = record_options();
    options.read(true).write(true).create(create);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
    options.open(lock)
}

/// `_open_record`: None when absent and not `create`, an error otherwise.
fn record_target(store_file: &Path, create: bool) -> io::Result<Option<Target>> {
    let file = match open_lock_file(&lock_path(store_file), create) {
        Ok(f) => f,
        Err(e) if e.kind() == io::ErrorKind::NotFound && !create => return Ok(None),
        Err(e) => return Err(e),
    };
    let key = file_key(&file)?;
    Ok(Some(Target {
        file,
        key,
        record: true,
        taken: false,
        mine: false,
    }))
}

/// `_open_target`; `record_mode` stands for the directory's key in
/// `_RECORD_MODE_DIRS` (the directory is still opened first).
fn open_target(store_file: &Path, record_mode: bool, create: bool) -> io::Result<Option<Target>> {
    #[cfg(not(windows))]
    {
        let file = File::open(parent_of(store_file))?;
        let key = file_key(&file)?;
        if !record_mode {
            return Ok(Some(Target {
                file,
                key,
                record: false,
                taken: false,
                mine: false,
            }));
        }
    }
    let _ = record_mode;
    record_target(store_file, create)
}

/// `_take`. Windows locks exclusively whatever `shared` says (the
/// reference's `msvcrt.locking` has no shared lock).
fn take(
    store_file: &Path,
    create: bool,
    shared: bool,
    wait: Duration,
    mine: Mine,
    record_mode: bool,
) -> io::Result<Option<Target>> {
    let shared = shared && cfg!(not(windows));
    let mut target = open_target(store_file, record_mode, create)?;
    if let Some(t) = target.as_mut().filter(|t| !t.record) {
        if t.key.is_some_and(mine) {
            t.mine = true;
            return Ok(target);
        }
        match lock_within(&t.file, shared, wait) {
            Try::Taken => t.taken = true,
            Try::Held => {}
            Try::Unlockable => {
                target = record_target(store_file, create)?;
            }
        }
        if target.as_ref().is_some_and(|t| !t.record) {
            return Ok(target);
        }
    }
    let Some(mut t) = target else {
        return Ok(None);
    };
    if t.key.is_some_and(mine) {
        t.mine = true;
        return Ok(Some(t));
    }
    // A record file that cannot be locked either counts as taken: only a
    // marker refuses.
    t.taken = record_file_taken(lock_within(&t.file, shared, wait));
    Ok(Some(t))
}

/// The text of the RecursionError a deeply nested marker raises (the
/// reference's own text names the decoder and is not pinned).
pub const RECURSION_TEXT: &str = "maximum recursion depth exceeded";

/// A marker as the reference's dict: the parsed object's items, or
/// `{"unreadable": str(exc)}` when the read failed.
pub type Marker = Vec<(PyValue, PyValue)>;

/// What a writing open or a look at the lock raised.
#[derive(Debug)]
pub enum AdmitError {
    /// `RestoreLockedError`.
    Locked(RestoreLocked),
    /// RecursionError out of `json.loads` of a deeply nested marker: the
    /// reference lets it through `admit` and `restore_state`.
    Recursion,
}

/// `json.loads(data)` for bytes: `json.detect_encoding` and a
/// `surrogatepass` decode, a decode failure a ValueError.
fn loads_bytes(data: &[u8]) -> Result<PyValue, json::LoadError> {
    let text = super::provenance::decode_json_bytes(data).ok_or(json::LoadError::Decode)?;
    json::loads_detailed(&text)
}

/// `_read_marker` on an open file: one read of at most 64 KiB.
fn marker_of(file: &mut File) -> Result<Option<Marker>, AdmitError> {
    use std::io::Read;
    let mut buf = vec![0u8; MARKER_CAP];
    let n = match file.read(&mut buf) {
        Ok(n) => n,
        Err(e) => {
            return Ok(Some(vec![(
                PyValue::str("unreadable"),
                PyValue::str(&os_error_text(&e)),
            )]))
        }
    };
    let data = &buf[..n];
    if data
        .iter()
        .all(|b| matches!(b, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c))
    {
        return Ok(None);
    }
    match &loads_bytes(data) {
        Ok(PyValue::Dict(items)) => Ok(Some(items.clone())),
        Ok(_) => Ok(Some(Vec::new())),
        Err(json::LoadError::Recursion) => Err(AdmitError::Recursion),
        Err(_) => Ok(Some(Vec::new())),
    }
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

/// `read_marker`: the marker beside the store, None when there is none,
/// it cannot be opened, or it is blank.
pub fn read_marker(store_file: &Path) -> Result<Option<Marker>, AdmitError> {
    match record_options().read(true).open(lock_path(store_file)) {
        Ok(mut f) => marker_of(&mut f),
        Err(_) => Ok(None),
    }
}

fn get<'a>(marker: &'a Marker, key: &str) -> Option<&'a PyValue> {
    let key = PyValue::str(key);
    marker.iter().rev().find(|(k, _)| *k == key).map(|(_, v)| v)
}

fn text_of(marker: &Marker, key: &str) -> Option<PyStr> {
    match get(marker, key) {
        Some(PyValue::Str(s)) => Some(s.clone()),
        _ => None,
    }
}

/// `describe`: what a marker says, through `printable`.
pub fn describe(marker: &Marker) -> PyStr {
    let mut parts: Vec<PyStr> = Vec::new();
    if let Some(v) = text_of(marker, "started_at") {
        parts.push([unicode::cps("started "), v].concat());
    }
    if let Some(PyValue::Int(n)) = get(marker, "pid") {
        parts.push(unicode::cps(&format!("by process {n}")));
    }
    if let Some(v) = text_of(marker, "bundle") {
        parts.push([unicode::cps("from "), v].concat());
    }
    if let Some(v) = text_of(marker, "stage") {
        parts.push([unicode::cps("stopped at "), v].concat());
    }
    if parts.is_empty() {
        printable(&unicode::cps("no record of it reads"))
    } else {
        printable(&parts.join(&unicode::cps(", ")[..]))
    }
}

fn text(cps: &[u32]) -> String {
    cps.iter().filter_map(|&c| char::from_u32(c)).collect()
}

/// `RestoreLockedError`: `str(exc)` is its [`std::fmt::Display`].
#[derive(Debug)]
pub struct RestoreLocked {
    pub store_file: PathBuf,
    pub lock: PathBuf,
    pub marker: Option<Marker>,
    pub running: bool,
    pub replaced: bool,
}

impl RestoreLocked {
    fn new(store_file: &Path, marker: Option<Marker>, running: bool) -> Self {
        RestoreLocked {
            store_file: store_file.to_path_buf(),
            lock: lock_path(store_file),
            marker,
            running,
            replaced: false,
        }
    }
}

impl std::fmt::Display for RestoreLocked {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let lock = self.lock.display();
        write!(f, "{} is not opened: ", self.store_file.display())?;
        if self.replaced {
            return write!(
                f,
                "a restore replaced the store file while it was being opened (its record is \
                 {lock}); open it again"
            );
        }
        if self.running {
            return write!(
                f,
                "a restore of it is running (its record is {lock}); open it again when the \
                 restore has finished"
            );
        }
        let empty = Vec::new();
        let marker = self.marker.as_ref().unwrap_or(&empty);
        let again = match text_of(marker, "bundle") {
            Some(b) => [
                unicode::cps("`bettermemory backup restore "),
                b,
                unicode::cps(" --yes`"),
            ]
            .concat(),
            None => unicode::cps("`bettermemory backup restore BUNDLE --yes`"),
        };
        write!(
            f,
            "a restore of it did not finish ({}; {lock} keeps its record), and the store may be \
             part restored. Run the restore again to finish it ({}), or open the store as it \
             stands after `bettermemory backup unlock --yes`",
            text(&describe(marker)),
            text(&printable(&again))
        )
    }
}

impl std::error::Error for RestoreLocked {}

/// `restore_lock.CLEAR`, `RUNNING`, `UNFINISHED`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Clear,
    Running,
    Unfinished,
}

impl State {
    /// The reference's string.
    pub fn name(self) -> &'static str {
        match self {
            State::Clear => "clear",
            State::Running => "running",
            State::Unfinished => "unfinished",
        }
    }
}

/// `restore_state(store_file, wait=wait)`, with `mine` for
/// `_this_thread_holds`.
pub fn restore_state(
    store_file: &Path,
    wait: Duration,
    mine: Mine,
) -> Result<(State, Option<Marker>), AdmitError> {
    state_in(store_file, wait, mine, false)
}

/// [`restore_state`] with the directory in record mode, as the reference
/// puts one `flock` cannot lock (a test's way in: no local file system
/// here refuses `flock`).
#[doc(hidden)]
pub fn restore_state_in_record_mode(
    store_file: &Path,
    wait: Duration,
    mine: Mine,
) -> Result<(State, Option<Marker>), AdmitError> {
    state_in(store_file, wait, mine, true)
}

fn state_in(
    store_file: &Path,
    wait: Duration,
    mine: Mine,
    record_mode: bool,
) -> Result<(State, Option<Marker>), AdmitError> {
    let target = match take(store_file, false, true, wait, mine, record_mode) {
        Ok(Some(t)) => t,
        Ok(None) | Err(_) => return Ok((State::Clear, None)),
    };
    if target.mine {
        return Ok((State::Clear, None));
    }
    if !target.taken {
        return Ok((State::Running, read_marker(store_file)?));
    }
    unlock_file(&target.file);
    drop(target);
    Ok(match read_marker(store_file)? {
        None => (State::Clear, None),
        Some(m) => (State::Unfinished, Some(m)),
    })
}

/// `_identity`: the file at `path` by device and inode, None when there
/// is none or its inode is 0. Windows: None (no file index in stable std),
/// so the replaced check is off there.
fn identity(path: &Path) -> Option<FileKey> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let m = std::fs::metadata(path).ok()?;
        (m.ino() != 0).then(|| FileKey {
            dev: m.dev(),
            ino: m.ino(),
        })
    }
    #[cfg(not(unix))]
    {
        let _ = path;
        None
    }
}

/// `_RecordCheck`: a writing open admitted where the lock is the record
/// file.
#[derive(Debug)]
pub struct RecordCheck {
    store_file: PathBuf,
    /// Windows: the store file held open (`_store_handle`).
    handle: Option<File>,
    before: Option<FileKey>,
    wait: Duration,
    mine: Mine,
}

/// What `admit` returns: dropping it closes it (`close`).
#[derive(Debug)]
pub enum Admission {
    /// `_SharedHold`: the store's directory locked shared.
    Shared(File),
    /// `_RecordCheck`.
    Record(RecordCheck),
    /// Nothing to keep off.
    None,
}

impl Admission {
    /// `confirm`, once the connection has read the store.
    pub fn confirm(&self) -> Result<(), AdmitError> {
        let Admission::Record(check) = self else {
            return Ok(());
        };
        let file = &check.store_file;
        if check.handle.is_some() {
            #[cfg(windows)]
            {
                let (state, record) = behind_handle(file, check.wait, check.mine)?;
                if state != State::Clear {
                    return Err(locked(file, state, record));
                }
            }
            return Ok(());
        }
        let (state, record) = restore_state(file, check.wait, check.mine)?;
        if state != State::Clear {
            return Err(locked(file, state, record));
        }
        let after = identity(file);
        if let (Some(before), Some(after)) = (check.before, after) {
            if before != after {
                let mut refusal = RestoreLocked::new(file, None, false);
                refusal.replaced = true;
                return Err(AdmitError::Locked(refusal));
            }
        }
        Ok(())
    }
}

fn locked(store_file: &Path, state: State, record: Option<Marker>) -> AdmitError {
    AdmitError::Locked(RestoreLocked::new(
        store_file,
        record,
        state == State::Running,
    ))
}

#[cfg(windows)]
fn behind_handle(
    store_file: &Path,
    wait: Duration,
    mine: Mine,
) -> Result<(State, Option<Marker>), AdmitError> {
    let deadline = Instant::now() + wait;
    loop {
        let (state, record) = restore_state(store_file, Duration::ZERO, mine)?;
        if state != State::Running || Instant::now() >= deadline {
            return Ok((state, record));
        }
        std::thread::sleep(POLL);
    }
}

/// `_record_admission`. Windows: `_open_store_handle` and the check again
/// behind the handle; elsewhere no handle, and the store file's identity
/// taken for `confirm`.
fn record_admission(
    store_file: &Path,
    wait: Duration,
    mine: Mine,
) -> Result<Admission, AdmitError> {
    #[cfg(windows)]
    let handle = match store_handle(store_file) {
        Some(h) => Some(h),
        None => {
            let (state, record) = behind_handle(store_file, wait, mine)?;
            if state != State::Clear {
                return Err(locked(store_file, state, record));
            }
            store_handle(store_file)
        }
    };
    #[cfg(not(windows))]
    let handle: Option<File> = None;
    let before = if handle.is_none() {
        identity(store_file)
    } else {
        None
    };
    #[cfg(windows)]
    if handle.is_some() {
        let (state, record) = behind_handle(store_file, wait, mine)?;
        if state != State::Clear {
            return Err(locked(store_file, state, record));
        }
    }
    Ok(Admission::Record(RecordCheck {
        store_file: store_file.to_path_buf(),
        handle,
        before,
        wait,
        mine,
    }))
}

/// `admit(store_file)` with `wait` for `OPEN_WAIT_SECONDS` and `mine` for
/// `_this_thread_holds`.
pub fn admit(store_file: &Path, wait: Duration, mine: Mine) -> Result<Admission, AdmitError> {
    admit_in(store_file, wait, mine, false)
}

/// [`admit`] with the directory in record mode (see
/// [`restore_state_in_record_mode`]).
#[doc(hidden)]
pub fn admit_in_record_mode(
    store_file: &Path,
    wait: Duration,
    mine: Mine,
) -> Result<Admission, AdmitError> {
    admit_in(store_file, wait, mine, true)
}

fn admit_in(
    store_file: &Path,
    wait: Duration,
    mine: Mine,
    record_mode: bool,
) -> Result<Admission, AdmitError> {
    let target = match take(store_file, false, true, wait, mine, record_mode) {
        Ok(t) => t,
        Err(_) => return Ok(Admission::None),
    };
    let Some(target) = target else {
        return record_admission(store_file, wait, mine);
    };
    if target.mine {
        return Ok(Admission::None);
    }
    if !target.taken {
        return Err(locked(store_file, State::Running, read_marker(store_file)?));
    }
    if let Some(marker) = read_marker(store_file)? {
        unlock_file(&target.file);
        return Err(locked(store_file, State::Unfinished, Some(marker)));
    }
    if target.record {
        unlock_file(&target.file);
        drop(target);
        return record_admission(store_file, wait, mine);
    }
    Ok(Admission::Shared(target.file))
}

// ---------------------------------------------------------------------------
// The restore's own hold (`RestoreLock`, `acquire`) and what reads it.
// ---------------------------------------------------------------------------

/// `restore_lock.MARKER_FORMAT`.
pub const MARKER_FORMAT: &str = "bettermemory.restore_lock";
/// `restore_lock.RESTORE_WAIT_SECONDS`.
pub const RESTORE_WAIT: Duration = Duration::from_secs(10);
/// `_HELD_ELSEWHERE_CODES`: SQLITE_BUSY and SQLITE_LOCKED.
const HELD_ELSEWHERE_CODES: [i32; 2] = [5, 6];

/// `_HELD`: the restore locks this process holds, by the locked file's
/// key, with the thread that took each.
fn registry() -> std::sync::MutexGuard<'static, HashMap<FileKey, ThreadId>> {
    static HELD: OnceLock<Mutex<HashMap<FileKey, ThreadId>>> = OnceLock::new();
    HELD.get_or_init(Default::default)
        .lock()
        .unwrap_or_else(|e| e.into_inner())
}

/// `_this_thread_holds`: this thread holds the restore lock `key` names,
/// so the restore's own opens of its store go through.
pub fn this_thread_holds(key: FileKey) -> bool {
    registry().get(&key) == Some(&std::thread::current().id())
}

/// `acquire`'s `mine`: `key in _HELD`, by any thread.
fn held_anywhere(key: FileKey) -> bool {
    registry().contains_key(&key)
}

/// `str(Path(path))`: the path as `pathlib` spells it (repeated
/// separators and `.` segments gone), by the host's flavour.
/// On POSIX a byte that is no UTF-8 is the lone surrogate `os.fsdecode`
/// gives it (`surrogateescape`): `pure_path` reads only `/` and `.`, so
/// it runs over the bytes spelled one character each and back.
fn path_text(path: &Path) -> PyStr {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        let bytes = path.as_os_str().as_bytes();
        let spelled: String = bytes.iter().map(|&b| char::from(b)).collect();
        let pure = crate::config::pure_path(&spelled, crate::config::Flavour::Posix);
        let back: Vec<u8> = pure.chars().map(|c| u32::from(c) as u8).collect();
        crate::gitsystem::record::surrogateescape(&back)
    }
    #[cfg(not(unix))]
    {
        #[cfg(windows)]
        let flavour = crate::config::Flavour::Windows;
        #[cfg(not(windows))]
        let flavour = crate::config::Flavour::Posix;
        unicode::cps(&crate::config::pure_path(&path.to_string_lossy(), flavour))
    }
}

/// What `acquire`, `unlock` and `mark` raise.
#[derive(Debug)]
pub enum HoldError {
    /// OSError: the lock or the marker cannot be opened or written.
    Os(io::Error),
    /// RecursionError out of `json.loads` of a deeply nested marker, or
    /// out of `json.dumps` of a deeply nested field.
    Recursion,
    /// What else `json.dumps` of a field raises (TypeError for a value
    /// JSON has no spelling for, ValueError, OverflowError).
    Type(crate::pyvalue::PyError),
    /// `RestoreLockedError` out of a look at the marker.
    Locked(RestoreLocked),
    /// The clock's time does not format (`strftime`).
    Clock(crate::datetime::DateError),
}

impl From<io::Error> for HoldError {
    fn from(e: io::Error) -> Self {
        HoldError::Os(e)
    }
}

fn from_admit(e: AdmitError) -> HoldError {
    match e {
        AdmitError::Recursion => HoldError::Recursion,
        AdmitError::Locked(l) => HoldError::Locked(l),
    }
}

/// `RestoreLock`: a restore's exclusive hold and the marker file it keeps.
/// Dropped without [`RestoreLock::release`] (a panic), it stays held as
/// the reference's does, whose file descriptors and `_HELD` entry outlive
/// the object: the files are left open until the process ends.
#[derive(Debug)]
pub struct RestoreLock {
    /// The marker's path (`lock_path`).
    pub path: PathBuf,
    pub store_file: PathBuf,
    /// The marker a restore that did not finish left.
    pub previous: Option<Marker>,
    /// The lock is the record file itself.
    pub record: bool,
    /// The thread that took it.
    pub thread: ThreadId,
    marker_file: Option<File>,
    /// The locked directory; None when the lock is the marker file.
    lock_file: Option<File>,
    key: Option<FileKey>,
    marker: Marker,
}

impl RestoreLock {
    /// `mark(**fields)`: the marker, with `fields` over what it held,
    /// written whole and synced. `now` is the clock's UTC time, read for
    /// `started_at` on the first mark only.
    pub fn mark(
        &mut self,
        fields: &[(&str, PyValue)],
        now: &crate::datetime::DateTime,
    ) -> Result<(), HoldError> {
        use std::io::{Seek, Write};
        if self.marker.is_empty() {
            let started = now
                .strftime("%Y-%m-%dT%H:%M:%SZ")
                .map_err(HoldError::Clock)?;
            self.marker = vec![
                (PyValue::str("format"), PyValue::str(MARKER_FORMAT)),
                (
                    PyValue::str("pid"),
                    PyValue::Int(std::process::id().to_string()),
                ),
                (PyValue::str("started_at"), PyValue::str(&started)),
                (
                    PyValue::str("store"),
                    PyValue::Str(path_text(&self.store_file)),
                ),
            ];
        }
        for (name, value) in fields {
            let key = PyValue::str(name);
            match self.marker.iter_mut().find(|(k, _)| *k == key) {
                Some(slot) => slot.1 = value.clone(),
                None => self.marker.push((key, value.clone())),
            }
        }
        let doc = PyValue::Dict(self.marker.clone());
        let mut data = text(
            &json::dumps(&doc, json::DumpOptions::SORTED_ASCII_SPACED).map_err(|e| match e {
                crate::pyvalue::PyError::RecursionError => HoldError::Recursion,
                other => HoldError::Type(other),
            })?,
        );
        data.push('\n');
        let Some(file) = self.marker_file.as_mut() else {
            return Err(HoldError::Os(io::ErrorKind::NotFound.into()));
        };
        file.set_len(0)?;
        file.seek(io::SeekFrom::Start(0))?;
        file.write_all(data.as_bytes())?;
        let _ = bettermemory_sys::fsync(file);
        Ok(())
    }

    /// `release(clear=clear)`: the lock given back; with `clear`, the
    /// marker emptied and removed first (on Windows removed after the
    /// close).
    pub fn release(mut self, clear: bool) {
        self.let_go(clear);
    }

    fn let_go(&mut self, clear: bool) {
        let Some(marker) = self.marker_file.take() else {
            return;
        };
        if clear {
            let _ = marker.set_len(0);
            #[cfg(not(windows))]
            let _ = std::fs::remove_file(&self.path);
        }
        if let Some(key) = self.key {
            registry().remove(&key);
        }
        unlock_file(self.lock_file.as_ref().unwrap_or(&marker));
        drop(marker);
        self.lock_file = None;
        #[cfg(windows)]
        if clear {
            let _ = std::fs::remove_file(&self.path);
        }
    }
}

impl Drop for RestoreLock {
    fn drop(&mut self) {
        // Not released: keep the lock and the registry entry, as the
        // reference's descriptors stay open once its object is gone.
        if let Some(marker) = self.marker_file.take() {
            std::mem::forget(marker);
        }
        if let Some(lock) = self.lock_file.take() {
            std::mem::forget(lock);
        }
    }
}

/// `held_here`: whether this thread already holds the store's lock.
pub fn held_here(store_file: &Path) -> io::Result<bool> {
    held_here_in(store_file, false)
}

/// [`held_here`] with the directory in record mode.
#[doc(hidden)]
pub fn held_here_in_record_mode(store_file: &Path) -> io::Result<bool> {
    held_here_in(store_file, true)
}

fn held_here_in(store_file: &Path, record_mode: bool) -> io::Result<bool> {
    let target = take(
        store_file,
        false,
        true,
        Duration::ZERO,
        this_thread_holds,
        record_mode,
    )?;
    Ok(target.is_some_and(|t| t.mine))
}

/// `acquire(store_file, wait=wait)`: the lock held exclusively and the
/// marker file made (owner-only) when missing; None when another holds
/// it past `wait` or a restore in this process holds it.
pub fn acquire(store_file: &Path, wait: Duration) -> Result<Option<RestoreLock>, HoldError> {
    acquire_in(store_file, wait, false)
}

/// [`acquire`] with the directory in record mode.
#[doc(hidden)]
pub fn acquire_in_record_mode(
    store_file: &Path,
    wait: Duration,
) -> Result<Option<RestoreLock>, HoldError> {
    acquire_in(store_file, wait, true)
}

fn acquire_in(
    store_file: &Path,
    wait: Duration,
    record_mode: bool,
) -> Result<Option<RestoreLock>, HoldError> {
    let lock = lock_path(store_file);
    let Some(target) = take(store_file, true, false, wait, held_anywhere, record_mode)? else {
        let why = format!("{} could not be made", text(&path_text(&lock)));
        return Err(HoldError::Os(io::Error::new(io::ErrorKind::NotFound, why)));
    };
    if target.mine || !target.taken {
        return Ok(None);
    }
    let (mut marker_file, lock_file) = if target.record {
        (target.file, None)
    } else {
        match open_lock_file(&lock, true) {
            Ok(f) => (f, Some(target.file)),
            Err(e) => {
                unlock_file(&target.file);
                return Err(HoldError::Os(e));
            }
        }
    };
    let previous = {
        use std::io::Seek;
        match marker_file.seek(io::SeekFrom::Start(0)) {
            Ok(_) => marker_of(&mut marker_file).map_err(from_admit)?,
            Err(e) => Some(vec![(
                PyValue::str("unreadable"),
                PyValue::str(&os_error_text(&e)),
            )]),
        }
    };
    if let Some(key) = target.key {
        registry().insert(key, std::thread::current().id());
    }
    Ok(Some(RestoreLock {
        path: lock,
        store_file: store_file.to_path_buf(),
        previous,
        record: target.record,
        thread: std::thread::current().id(),
        marker_file: Some(marker_file),
        lock_file,
        key: target.key,
        marker: Vec::new(),
    }))
}

/// One statement stepped once, as `Connection.execute` does.
fn step_once(conn: &rusqlite::Connection, sql: &str) -> rusqlite::Result<()> {
    let mut statement = conn.prepare(sql)?;
    let mut rows = statement.query([])?;
    rows.next()?;
    Ok(())
}

/// `exc.sqlite_errorcode & 0xFF in _HELD_ELSEWHERE_CODES`: an extended
/// code whose primary code is SQLITE_BUSY or SQLITE_LOCKED.
#[doc(hidden)]
pub fn held_elsewhere(extended_code: i32) -> bool {
    HELD_ELSEWHERE_CODES.contains(&(extended_code & 0xFF))
}

/// `probe`: why a restore must not replace the store file now (a SQLite
/// connection anywhere has it open), or None.
pub fn probe(store_file: &Path) -> Option<PyStr> {
    use rusqlite::OpenFlags;
    if !store_file.is_file() {
        return None;
    }
    let resolved = std::fs::canonicalize(store_file).ok()?;
    let uri = format!("{}?mode=rw", super::file_uri(&resolved));
    let flags = OpenFlags::SQLITE_OPEN_READ_WRITE
        | OpenFlags::SQLITE_OPEN_URI
        | OpenFlags::SQLITE_OPEN_NO_MUTEX;
    let conn = rusqlite::Connection::open_with_flags(&uri, flags).ok()?;
    // `timeout=0`: rusqlite's own open sets five seconds.
    let read = conn.busy_timeout(Duration::ZERO).and_then(|()| {
        step_once(&conn, "PRAGMA trusted_schema = OFF")?;
        step_once(&conn, "PRAGMA locking_mode = EXCLUSIVE")?;
        step_once(&conn, "PRAGMA schema_version")
    });
    let busy = match read {
        Err(rusqlite::Error::SqliteFailure(e, _)) => held_elsewhere(e.extended_code),
        _ => false,
    };
    drop(conn);
    busy.then(|| {
        [
            unicode::cps("a process has "),
            path_text(store_file),
            unicode::cps(
                " open (`bettermemory serve`, the shim's in-process server, a hook, \
                 a CLI command, `backup create`, or a daemon this state directory does not name); \
                 close every session using bettermemory and run the restore again",
            ),
        ]
        .concat()
    })
}

/// `unlock`'s outcomes (`CLEAR`, `CLEARED`, `HELD`, `PLANNED`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum UnlockStatus {
    Clear,
    Cleared,
    Held,
    Planned,
}

impl UnlockStatus {
    /// The reference's string.
    pub fn name(self) -> &'static str {
        match self {
            UnlockStatus::Clear => "clear",
            UnlockStatus::Cleared => "cleared",
            UnlockStatus::Held => "held",
            UnlockStatus::Planned => "planned",
        }
    }

    /// `UNLOCK_EXIT`.
    pub fn exit(self) -> i32 {
        match self {
            UnlockStatus::Clear | UnlockStatus::Cleared => 0,
            UnlockStatus::Held => 1,
            UnlockStatus::Planned => 3,
        }
    }
}

/// `HOLDER_RESTORE`, `HOLDER_READER`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Holder {
    Restore,
    Reader,
}

impl Holder {
    /// The reference's string.
    pub fn name(self) -> &'static str {
        match self {
            Holder::Restore => "restore",
            Holder::Reader => "reader",
        }
    }
}

/// `_holder`: readers when the lock can be had shared now, else a restore.
fn holder(store_file: &Path, record_mode: bool) -> Holder {
    match take(
        store_file,
        false,
        true,
        Duration::ZERO,
        this_thread_holds,
        record_mode,
    ) {
        Ok(Some(t)) if !t.record && !t.mine && t.taken => {
            unlock_file(&t.file);
            Holder::Reader
        }
        _ => Holder::Restore,
    }
}

/// `unlock`'s report: `{status, lock, marker, holder}`.
#[derive(Debug)]
pub struct UnlockReport {
    pub status: UnlockStatus,
    pub lock: PyStr,
    pub marker: Option<Marker>,
    pub holder: Option<Holder>,
}

/// `unlock(store_file, apply=apply)` with `wait` for `OPEN_WAIT_SECONDS`.
pub fn unlock(store_file: &Path, apply: bool, wait: Duration) -> Result<UnlockReport, HoldError> {
    unlock_in(store_file, apply, wait, false)
}

/// [`unlock`] with the directory in record mode.
#[doc(hidden)]
pub fn unlock_in_record_mode(
    store_file: &Path,
    apply: bool,
    wait: Duration,
) -> Result<UnlockReport, HoldError> {
    unlock_in(store_file, apply, wait, true)
}

fn unlock_in(
    store_file: &Path,
    apply: bool,
    wait: Duration,
    record_mode: bool,
) -> Result<UnlockReport, HoldError> {
    let lock = lock_path(store_file);
    let mut report = UnlockReport {
        status: UnlockStatus::Clear,
        lock: path_text(&lock),
        marker: None,
        holder: None,
    };
    if !lock.exists() {
        return Ok(report);
    }
    let Some(held) = acquire_in(store_file, wait, record_mode)? else {
        report.status = UnlockStatus::Held;
        report.marker = read_marker(store_file).map_err(from_admit)?;
        report.holder = Some(holder(store_file, record_mode));
        return Ok(report);
    };
    report.marker = held.previous.clone();
    let keep = held.previous.is_some() && !apply;
    if held.previous.is_some() {
        report.status = if apply {
            UnlockStatus::Cleared
        } else {
            UnlockStatus::Planned
        };
    }
    held.release(!keep);
    Ok(report)
}

/// `state_finding`: `(state, summary, remedy, details)`.
#[derive(Debug)]
pub struct Finding {
    pub state: State,
    pub summary: PyStr,
    pub remedy: Option<PyStr>,
    /// `{state, record, restore?}` in the reference's order.
    pub details: Marker,
}

/// `state_finding(store_file)`: `restore_state` in `doctor`'s terms.
pub fn state_finding(store_file: &Path) -> Result<Finding, AdmitError> {
    finding_in(store_file, false)
}

/// [`state_finding`] with the directory in record mode.
#[doc(hidden)]
pub fn state_finding_in_record_mode(store_file: &Path) -> Result<Finding, AdmitError> {
    finding_in(store_file, true)
}

fn finding_in(store_file: &Path, record_mode: bool) -> Result<Finding, AdmitError> {
    let (state, record) = state_in(store_file, Duration::ZERO, this_thread_holds, record_mode)?;
    let mut details = vec![
        (PyValue::str("state"), PyValue::str(state.name())),
        (
            PyValue::str("record"),
            PyValue::Str(path_text(&lock_path(store_file))),
        ),
    ];
    let restore = record.clone().map_or(PyValue::None, PyValue::Dict);
    let cps = unicode::cps;
    match state {
        State::Unfinished => {
            details.push((PyValue::str("restore"), restore));
            let marker = record.unwrap_or_default();
            let again = match get(&marker, "bundle") {
                Some(PyValue::Str(b)) => [
                    cps("bettermemory backup restore "),
                    b.clone(),
                    cps(" --yes"),
                ]
                .concat(),
                _ => cps("bettermemory backup restore BUNDLE --yes"),
            };
            Ok(Finding {
                state,
                summary: [
                    cps("writes are refused: a restore did not finish ("),
                    describe(&marker),
                    cps(")"),
                ]
                .concat(),
                remedy: Some(
                    [
                        cps("run `bettermemory backup unlock --yes` after checking the store, or re-run the restore (`"),
                        printable(&again),
                        cps("`)"),
                    ]
                    .concat(),
                ),
                details,
            })
        }
        State::Running => {
            details.push((PyValue::str("restore"), restore));
            Ok(Finding {
                state,
                summary: cps("writes are refused while a restore runs"),
                remedy: Some(cps(
                    "run `bettermemory doctor` again when the restore has finished",
                )),
                details,
            })
        }
        State::Clear => Ok(Finding {
            state,
            summary: cps("no restore is running or left unfinished"),
            remedy: None,
            details,
        }),
    }
}

#[cfg(test)]
mod lock_tests {
    use super::{open_lock_file, try_lock, unlock_file, Try};
    use std::path::PathBuf;

    fn scratch(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("bm-restore-unlock-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("scratch directory");
        dir.join("memory.sqlite.restore-lock")
    }

    /// `unlock_file` gives back the lock `try_lock` took while the holding
    /// handle stays open: flock's LOCK_UN on POSIX, and on Windows the
    /// UnlockFile of the same byte LockFile took (closing the handle would
    /// free either, so only an open handle shows the unlock itself).
    #[test]
    fn an_unlock_frees_the_lock_while_the_handle_stays_open() {
        let lock = scratch("open");
        let holder = open_lock_file(&lock, true).expect("holder");
        let other = open_lock_file(&lock, false).expect("other");
        assert_eq!(try_lock(&holder, false), Try::Taken);
        assert_eq!(try_lock(&other, false), Try::Held);
        unlock_file(&holder);
        assert_eq!(try_lock(&other, false), Try::Taken);
        unlock_file(&other);
        drop((holder, other));
        let _ = std::fs::remove_dir_all(lock.parent().unwrap());
    }

    /// On Windows the record file opens without FILE_SHARE_DELETE, as the
    /// reference's `os.open` does, so an unlink meeting an open handle
    /// fails and leaves the file in place. The failure is
    /// ERROR_SHARING_VIOLATION (32), which std maps to no named ErrorKind;
    /// ERROR_ACCESS_DENIED (5) would mean another cause, such as a
    /// read-only file or a delete-pending one.
    #[cfg(windows)]
    #[test]
    fn an_open_record_file_is_not_removed() {
        const ERROR_SHARING_VIOLATION: i32 = 32;
        let lock = scratch("share");
        let open = open_lock_file(&lock, true).expect("open");
        let removed = std::fs::remove_file(&lock);
        assert_eq!(
            removed.map_err(|e| e.raw_os_error()),
            Err(Some(ERROR_SHARING_VIOLATION))
        );
        let marker = super::read_marker(&lock.with_file_name("memory.sqlite"));
        assert!(matches!(marker, Ok(None)), "{marker:?}");
        assert!(std::fs::remove_file(&lock).is_err());
        drop(open);
        std::fs::remove_file(&lock).expect("removed once closed");
        let _ = std::fs::remove_dir_all(lock.parent().unwrap());
    }
}
