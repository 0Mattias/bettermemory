//! `flock_excl` under hostile input and across processes: a lock held by
//! a second process excludes this one (and on Windows a lock of byte 0
//! taken as `msvcrt.locking` takes it excludes the native lock both
//! ways), a long retry chain stays linear, and very long names and
//! suffixes are answered without a panic.

mod timing;

use std::cell::Cell;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::AtomicBool;

use bettermemory_core::fsutil::{flock_excl, flock_windows_by, lock_path, FlockError, Msvcrt};

const CHILD: &str = "BM_FLOCK_ROBUSTNESS_CHILD";

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("bm-flock-rb-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// The child: takes the lock named by the variable the way it says
/// (`native:<path>` through `flock_excl`, `byte0:<lock file>` as
/// `msvcrt.locking` does), says `held`, and holds it until its input ends.
#[test]
fn child_holder() {
    let Ok(spec) = std::env::var(CHILD) else {
        return;
    };
    let (how, path) = spec.split_once(':').unwrap();
    let _hold: Box<dyn std::any::Any> = match how {
        "native" => Box::new(flock_excl(Path::new(path), ".lock").expect("the child's lock")),
        #[cfg(windows)]
        "byte0" => Box::new(win::lock_byte0(Path::new(path)).expect("the child's byte")),
        other => panic!("unknown hold {other}"),
    };
    println!("held");
    std::io::stdout().flush().unwrap();
    let mut line = String::new();
    let _ = std::io::stdin().read_line(&mut line);
}

/// A holder process; its output is read to the end (so its later libtest
/// lines never meet a closed pipe) and its exit status checked on drop.
struct Child(std::process::Child, Option<std::thread::JoinHandle<()>>);

impl Child {
    fn spawn(spec: String) -> Child {
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "child_holder",
                "--nocapture",
                "--test-threads",
                "1",
            ])
            .env(CHILD, spec)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .expect("the child starts");
        let mut out = BufReader::new(child.stdout.take().unwrap());
        let mut line = String::new();
        loop {
            line.clear();
            assert!(
                out.read_line(&mut line).unwrap() > 0,
                "the child ended before holding"
            );
            // libtest may print the test's name on the same line.
            if line.trim_end().ends_with("held") {
                break;
            }
        }
        let drain = std::thread::spawn(move || {
            let _ = std::io::copy(&mut out, &mut std::io::sink());
        });
        Child(child, Some(drain))
    }
}

impl Drop for Child {
    fn drop(&mut self) {
        drop(self.0.stdin.take());
        let status = self.0.wait();
        if status.is_err() {
            let _ = self.0.kill();
        }
        if let Some(drain) = self.1.take() {
            let _ = drain.join();
        }
        if !std::thread::panicking() {
            assert!(
                status.is_ok_and(|s| s.success()),
                "the holder process failed after holding"
            );
        }
    }
}

#[test]
fn a_second_process_holding_the_lock_excludes_this_one() {
    let dir = scratch("two");
    let target = dir.join("x.json");
    let child = Child::spawn(format!("native:{}", target.display()));
    let lock = dir.join("x.json.lock");
    let file = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&lock)
        .unwrap();
    assert!(
        file.try_lock().is_err(),
        "the child's lock excludes this process"
    );
    drop(child);
    // On POSIX the reference's acquire waits with no deadline, so the
    // native one is taken here only once the child has gone.
    let guard = flock_excl(&target, ".lock").expect("free after the child");
    drop(guard);
    drop(file);
    std::fs::remove_dir_all(&dir).unwrap();
}

/// A lock of byte 0 as `msvcrt.locking(fd, LK_NBLCK, 1)` takes it at a
/// fresh file's position 0: `LockFile(handle, 0, 0, 1, 0)`.
#[cfg(windows)]
mod win {
    use std::os::windows::io::AsRawHandle;
    use std::path::Path;

    #[link(name = "kernel32")]
    extern "system" {
        fn LockFile(h: *mut core::ffi::c_void, lo: u32, hi: u32, nlo: u32, nhi: u32) -> i32;
    }

    pub fn lock_byte0(path: &Path) -> std::io::Result<std::fs::File> {
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path)?;
        // SAFETY: the handle is the open file's, valid while `file` lives.
        let ok = unsafe { LockFile(file.as_raw_handle(), 0, 0, 1, 0) };
        if ok == 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(file)
    }
}

#[cfg(windows)]
#[test]
fn a_byte0_lock_and_the_native_lock_exclude_each_other() {
    use bettermemory_core::fsutil::StdMsvcrt;
    let dir = scratch("byte0");
    let target = dir.join("x.json");
    let lock = dir.join("x.json.lock");
    // The reference's lock held by another process refuses the native one.
    let child = Child::spawn(format!("byte0:{}", lock.display()));
    let mut m = StdMsvcrt;
    let file = m.open(&lock).expect("open");
    assert!(!m.lock(&file), "the byte-0 lock excludes the native lock");
    assert!(m.close(file));
    drop(child);
    // The native lock held by another process: byte 0 cannot be taken.
    let child = Child::spawn(format!("native:{}", target.display()));
    assert!(
        win::lock_byte0(&lock).is_err(),
        "the native lock excludes a byte-0 lock"
    );
    drop(child);
    assert!(
        win::lock_byte0(&lock).is_ok(),
        "free once the holder has gone"
    );
    std::fs::remove_dir_all(&dir).unwrap();
}

struct Forever {
    tries: Cell<u64>,
    now: f64,
}

impl Msvcrt for Forever {
    type Fd = ();
    fn open(&mut self, _p: &Path) -> Option<()> {
        Some(())
    }
    fn lock(&mut self, _fd: &()) -> bool {
        self.tries.set(self.tries.get() + 1);
        false
    }
    fn unlock(&mut self, _fd: &()) -> bool {
        true
    }
    fn close(&mut self, _fd: ()) -> bool {
        true
    }
    fn monotonic(&mut self) -> f64 {
        self.now += 1.0;
        self.now
    }
    fn sleep(&mut self, s: f64) {
        assert!((0.005..=0.1).contains(&s));
    }
}

fn retries(n: f64) -> u64 {
    let mut m = Forever {
        tries: Cell::new(0),
        now: 0.0,
    };
    let got = flock_windows_by(
        Path::new("x.lock"),
        Some(&mut m),
        n,
        &AtomicBool::new(false),
        &mut |_| {},
    );
    let failed = got.is_err();
    drop(got);
    assert!(failed);
    m.tries.get()
}

#[test]
fn a_long_retry_chain_is_linear_and_bounded() {
    assert_eq!(retries(100_000.0), 100_000);
    assert_eq!(retries(f64::NEG_INFINITY), 1);
    assert_eq!(retries(-0.0), 1);
    timing::grows_within(
        "flock_windows_by",
        30,
        &mut || timing::once(|| retries(20_000.0)),
        &mut || timing::once(|| retries(200_000.0)),
    );
}

#[test]
fn an_infinite_timeout_message_reads_as_python_writes_it() {
    // A clock at infinity would make the deadline NaN, which never
    // passes: the reference loops for ever there too.
    let mut m = Forever {
        tries: Cell::new(0),
        now: 0.0,
    };
    let got = flock_windows_by(
        Path::new("x.lock"),
        Some(&mut m),
        f64::NEG_INFINITY,
        &AtomicBool::new(false),
        &mut |_| {},
    );
    assert_eq!(
        got.err().unwrap().0,
        "flock_excl: could not acquire x.lock within -infs"
    );
}

#[test]
fn very_long_names_and_suffixes_are_answered() {
    let suffix = format!(".{}", "s".repeat(1 << 20));
    let long = "n".repeat(1 << 20);
    let got = lock_path(Path::new(&long), &suffix, false).unwrap();
    assert_eq!(got.as_os_str().len(), (2 << 20) + 1);
    let deep: PathBuf = std::iter::repeat_n("d", 100_000).collect();
    assert!(lock_path(&deep, ".lock", false).is_ok());
    assert!(lock_path(Path::new("x"), &"/".repeat(1 << 20), false).is_err());
    assert!(lock_path(Path::new(""), "\u{0}", false).is_err());
    // A name the system refuses is the system's error, not a panic.
    let dir = scratch("long");
    match flock_excl(&dir.join(&long), ".lock") {
        Err(FlockError::Io(_)) => {}
        other => panic!("expected the system's refusal, got {}", other.is_ok()),
    }
    std::fs::remove_dir_all(&dir).unwrap();
}
