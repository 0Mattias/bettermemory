//! Cases a plausible wrong `flock_excl` or `_flock_windows` fails: the
//! deadline reached exactly, the pause's cap, the one warning shared by
//! both fallbacks, the release and close on each path, the lock path's
//! appended name, and the machine's lock held and released.

use std::cell::RefCell;
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;

use bettermemory_core::fsutil::{
    flock_excl, flock_warning, flock_windows_by, lock_path, FlockWarning, Msvcrt,
};

struct Script<'a> {
    log: &'a RefCell<Vec<String>>,
    open: bool,
    fails: i64,
    clock: Vec<f64>,
    unlock: bool,
}

impl Msvcrt for Script<'_> {
    type Fd = u8;

    fn open(&mut self, _p: &Path) -> Option<u8> {
        self.log.borrow_mut().push("open".into());
        self.open.then_some(7)
    }
    fn lock(&mut self, fd: &u8) -> bool {
        assert_eq!(*fd, 7);
        self.log.borrow_mut().push("lock".into());
        if self.fails < 0 {
            return false;
        }
        self.fails -= 1;
        self.fails < 0
    }
    fn unlock(&mut self, _fd: &u8) -> bool {
        self.log.borrow_mut().push("unlock".into());
        self.unlock
    }
    fn close(&mut self, _fd: u8) -> bool {
        self.log.borrow_mut().push("close".into());
        false
    }
    fn monotonic(&mut self) -> f64 {
        self.clock.remove(0)
    }
    fn sleep(&mut self, s: f64) {
        self.log.borrow_mut().push(format!("sleep {s}"));
    }
}

fn script<'a>(log: &'a RefCell<Vec<String>>, fails: i64, clock: &[f64]) -> Script<'a> {
    Script {
        log,
        open: true,
        fails,
        clock: clock.to_vec(),
        unlock: true,
    }
}

#[test]
fn the_deadline_reached_exactly_times_out() {
    let log = RefCell::new(Vec::new());
    let mut m = script(&log, -1, &[0.0, 0.5, 1.0]);
    let got = flock_windows_by(
        Path::new("x.lock"),
        Some(&mut m),
        1.0,
        &AtomicBool::new(false),
        &mut |_| {},
    );
    let err = got.err().expect("a timeout");
    assert_eq!(err.0, "flock_excl: could not acquire x.lock within 1.0s");
    // Closed, never unlocked.
    assert_eq!(
        log.borrow().as_slice(),
        ["open", "lock", "sleep 0.005", "lock", "close"]
    );
}

#[test]
fn the_pause_doubles_to_a_cap_of_a_tenth() {
    let log = RefCell::new(Vec::new());
    let mut m = script(&log, 8, &[0.0; 9]);
    let hold = flock_windows_by(
        Path::new("x.lock"),
        Some(&mut m),
        30.0,
        &AtomicBool::new(false),
        &mut |_| {},
    )
    .expect("held");
    assert!(hold.locked());
    drop(hold);
    let sleeps: Vec<String> = log
        .borrow()
        .iter()
        .filter(|e| e.starts_with("sleep"))
        .cloned()
        .collect();
    assert_eq!(
        sleeps,
        [
            "sleep 0.005",
            "sleep 0.01",
            "sleep 0.02",
            "sleep 0.04",
            "sleep 0.08",
            "sleep 0.1",
            "sleep 0.1",
            "sleep 0.1"
        ]
    );
    assert_eq!(log.borrow().last().unwrap(), "close");
}

#[test]
fn a_failed_release_still_closes() {
    let log = RefCell::new(Vec::new());
    let mut m = script(&log, 0, &[0.0]);
    m.unlock = false;
    drop(flock_windows_by(
        Path::new("x.lock"),
        Some(&mut m),
        30.0,
        &AtomicBool::new(false),
        &mut |_| {},
    ));
    assert_eq!(log.borrow().as_slice(), ["open", "lock", "unlock", "close"]);
}

#[test]
fn one_warning_for_both_fallbacks() {
    let warned = AtomicBool::new(false);
    let mut seen = Vec::new();
    let log = RefCell::new(Vec::new());
    let mut m = script(&log, 0, &[]);
    m.open = false;
    let hold = flock_windows_by(
        Path::new("d/x.lock"),
        Some(&mut m),
        30.0,
        &warned,
        &mut |w| seen.push(w),
    )
    .expect("the fallback");
    assert!(!hold.locked());
    drop(hold);
    let none: Option<&mut Script<'_>> = None;
    drop(flock_windows_by(
        Path::new("x.lock"),
        none,
        30.0,
        &warned,
        &mut |w| seen.push(w),
    ));
    let shown = Path::new("d/x.lock").display().to_string();
    assert_eq!(seen, [FlockWarning::CannotOpen(shown)]);
    // The fallback opened and closed nothing more.
    assert_eq!(log.borrow().as_slice(), ["open"]);
}

#[test]
fn the_lock_name_is_appended_not_replaced() {
    let posix = |p: &str, s: &str| {
        lock_path(Path::new(p), s, false).map(|p| p.to_string_lossy().into_owned())
    };
    #[cfg(not(windows))]
    assert_eq!(posix("d/x.tar.gz", ".lock").unwrap(), "d/x.tar.gz.lock");
    // The path is joined with the host's separator: the reference's `Path`
    // is a `WindowsPath` there, whose text is `d\x.tar.gz.lock`.
    #[cfg(windows)]
    assert_eq!(posix("d/x.tar.gz", ".lock").unwrap(), "d\\x.tar.gz.lock");
    assert_eq!(posix("a.", ".lock").unwrap(), "a..lock");
    assert_eq!(posix("x.txt", "lock").unwrap(), "x.txtlock");
    assert_eq!(
        posix(".hidden", "lock").unwrap_err().0,
        "Invalid suffix 'lock'"
    );
    assert_eq!(posix("x", ".").unwrap_err().0, "Invalid suffix '.'");
    #[cfg(not(windows))]
    assert_eq!(posix("d/..", ".lock").unwrap(), "d/...lock");
    #[cfg(windows)]
    assert_eq!(posix("d/..", ".lock").unwrap(), "d\\...lock");
    assert_eq!(posix("x.txt", "a\\b").unwrap(), "x.txta\\b");
    assert_eq!(
        lock_path(Path::new("x.txt"), "a\\b", true).unwrap_err().0,
        "Invalid name 'x.txta\\\\b'"
    );
}

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("bm-flock-kills-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    dir
}

#[test]
fn the_machine_lock_is_held_then_released_and_kept() {
    let dir = scratch("held");
    let target = dir.join("a/b/x.json");
    let guard = flock_excl(&target, ".lock").expect("the lock");
    let lock = dir.join("a/b/x.json.lock");
    let other = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&lock)
        .expect("open");
    assert!(other.try_lock().is_err(), "held while the guard lives");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = std::fs::metadata(&lock).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode & 0o077, 0, "never readable by others: {mode:o}");
    }
    drop(guard);
    assert!(other.try_lock().is_ok(), "released by the drop");
    drop(other);
    assert!(lock.exists(), "the lock file is kept");
    assert!(!target.exists());
    assert_eq!(flock_warning(), None);
    std::fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn a_refused_name_still_makes_the_parent() {
    let dir = scratch("refused");
    let err = flock_excl(&dir.join("p/x"), "lock").err().expect("refused");
    assert!(
        matches!(err, bettermemory_core::fsutil::FlockError::Path(ref e) if e.0 == "Invalid suffix 'lock'")
    );
    assert!(dir.join("p").is_dir());
    std::fs::remove_dir_all(&dir).unwrap();
}

/// pathlib's spelling: a leading `//` (exactly two) kept, every `.`
/// component dropped, an empty path shown as `.`, and a name outside
/// UTF-8 shown with `surrogateescape`'s `\udcXX` (CPython 3.13:
/// `PosixPath('//').with_suffix('.lock')` raises
/// `PosixPath('//') has an empty name`).
#[cfg(unix)]
#[test]
fn the_lock_path_is_spelled_as_pathlib_spells_it() {
    use std::ffi::OsStr;
    use std::os::unix::ffi::OsStrExt;
    let posix = |p: &Path, s: &str| lock_path(p, s, false).map(|p| p.into_os_string());
    let ok = |p: &str| posix(Path::new(p), ".lock").unwrap();
    let err = |p: &Path, s: &str| posix(p, s).unwrap_err().0;
    assert_eq!(ok("//x"), "//x.lock");
    assert_eq!(ok("///x"), "/x.lock");
    assert_eq!(ok("./b"), "b.lock");
    assert_eq!(ok("//a/./b"), "//a/b.lock");
    assert_eq!(
        err(Path::new("//"), ".lock"),
        "PosixPath('//') has an empty name"
    );
    assert_eq!(
        err(Path::new("///"), ".lock"),
        "PosixPath('/') has an empty name"
    );
    assert_eq!(
        err(Path::new(""), ".lock"),
        "PosixPath('.') has an empty name"
    );
    assert_eq!(
        err(Path::new("./"), ".lock"),
        "PosixPath('.') has an empty name"
    );
    assert_eq!(
        err(Path::new(OsStr::from_bytes(b"\xff.t")), ".a/b"),
        "Invalid name '\\udcff.t.a/b'"
    );
    assert_eq!(
        posix(Path::new(OsStr::from_bytes(b"\xff")), ".lock").unwrap(),
        OsStr::from_bytes(b"\xff.lock")
    );
}

#[test]
fn eintr_is_retried_and_other_errors_are_not() {
    use bettermemory_core::fsutil::lock_retrying_eintr;
    use std::io::{Error, ErrorKind};
    let mut calls = 0;
    let got = lock_retrying_eintr(|| {
        calls += 1;
        if calls < 3 {
            Err(Error::from(ErrorKind::Interrupted))
        } else {
            Ok(())
        }
    });
    assert!(got.is_ok());
    assert_eq!(calls, 3);
    let mut calls = 0;
    let got = lock_retrying_eintr(|| {
        calls += 1;
        Err(Error::from(ErrorKind::PermissionDenied))
    });
    assert_eq!(got.unwrap_err().kind(), ErrorKind::PermissionDenied);
    assert_eq!(calls, 1);
}
