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

use std::fs::File;
use std::io;
use std::path::{Path, PathBuf};
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
    std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(lock_path(store_file))
        .ok()
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
            let _ = record.unlock();
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
            let _ = record.unlock();
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

/// `fstat` of an open file. Windows: stable std reaches no file index, so
/// there is no key and the `mine` check sees nothing (no native restore
/// holds a lock there yet).
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
    #[cfg(not(unix))]
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

/// `_open_record(create=False)`: None when absent, an error otherwise.
fn record_target(store_file: &Path) -> io::Result<Option<Target>> {
    let file = match std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(lock_path(store_file))
    {
        Ok(f) => f,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
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

/// `_open_target(create=False)`; `record_mode` stands for the directory's
/// key in `_RECORD_MODE_DIRS` (the directory is still opened first).
fn open_target(store_file: &Path, record_mode: bool) -> io::Result<Option<Target>> {
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
    record_target(store_file)
}

/// `_take(create=False)`. Windows locks exclusively whatever `shared`
/// says (the reference's `msvcrt.locking` has no shared lock).
fn take(
    store_file: &Path,
    shared: bool,
    wait: Duration,
    mine: Mine,
    record_mode: bool,
) -> io::Result<Option<Target>> {
    let shared = shared && cfg!(not(windows));
    let mut target = open_target(store_file, record_mode)?;
    if let Some(t) = target.as_mut().filter(|t| !t.record) {
        if t.key.is_some_and(mine) {
            t.mine = true;
            return Ok(target);
        }
        match lock_within(&t.file, shared, wait) {
            Try::Taken => t.taken = true,
            Try::Held => {}
            Try::Unlockable => {
                target = record_target(store_file)?;
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
    match File::open(lock_path(store_file)) {
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
    let target = match take(store_file, true, wait, mine, record_mode) {
        Ok(Some(t)) => t,
        Ok(None) | Err(_) => return Ok((State::Clear, None)),
    };
    if target.mine {
        return Ok((State::Clear, None));
    }
    if !target.taken {
        return Ok((State::Running, read_marker(store_file)?));
    }
    let _ = target.file.unlock();
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
    let target = match take(store_file, true, wait, mine, record_mode) {
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
        let _ = target.file.unlock();
        return Err(locked(store_file, State::Unfinished, Some(marker)));
    }
    if target.record {
        let _ = target.file.unlock();
        drop(target);
        return record_admission(store_file, wait, mine);
    }
    Ok(Admission::Shared(target.file))
}
