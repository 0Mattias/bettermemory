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

use std::fs::File;
use std::io;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

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

enum Try {
    Taken,
    Held,
    Unlockable,
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
