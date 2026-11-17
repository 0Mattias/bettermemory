//! `gitsystem::record` on hostile input, each run on a 512 KiB thread (in
//! a debug build): a 100 MiB record (at most `MAX_BYTES + 1` bytes read),
//! one nested 100,000 deep (past the bound: not parsed) and 32,000 deep
//! (within it: the reference's RecursionError, without native
//! recursion), an entry nested 9,000 deep written back whole, a FIFO
//! where the record should be (read without waiting for a writer, where
//! the reference's blocking `open` waits), and hostile bytes; none
//! panics. Nothing here depends on machine speed.

use std::cell::Cell;

use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind, MemFs, Node, OsError, PyErr, Stat, StatResult, StdFs,
};
use bettermemory_core::gitsystem::record::{
    key, Answer, Place, Recorded, Refused, Sources, MAX_BYTES, RECORD,
};
use bettermemory_core::pyvalue::PyError;

const STACK: usize = 512 * 1024;

/// The host's path flavour, for the cases on disk.
fn host() -> Flavour {
    if cfg!(windows) {
        Flavour::Windows
    } else {
        Flavour::Posix
    }
}
const STATE: &str = "/s";

fn on_small_stack<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> T {
    std::thread::Builder::new()
        .stack_size(STACK)
        .spawn(f)
        .unwrap()
        .join()
        .unwrap()
}

/// `MemFs` counting the bytes read: delegates every method of `Fs`.
struct Counted {
    inner: MemFs,
    read: Cell<usize>,
}

impl Fs for Counted {
    type File = <MemFs as Fs>::File;
    fn open(&self, path: &[u8]) -> Result<Self::File, PyErr> {
        self.inner.open(path)
    }
    fn fstat(&self, file: &mut Self::File) -> Result<Stat, PyErr> {
        self.inner.fstat(file)
    }
    fn read(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, PyErr> {
        let got = self.inner.read(file, max)?;
        self.read.set(self.read.get() + got.len());
        Ok(got)
    }
    fn access_read(&self, path: &[u8]) -> bool {
        self.inner.access_read(path)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.inner.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<Self::File, OsError> {
        self.inner.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.inner.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.inner.readlink(path)
    }
    fn readlink_result(&self, path: &[u8]) -> Result<Vec<u8>, OsError> {
        self.inner.readlink_result(path)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.inner.lstat_result(path)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.inner.stat(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.inner.listdir(path)
    }
}

fn with_record(data: Vec<u8>) -> Counted {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(STATE.as_bytes(), Node::Dir);
    fs.insert(format!("{STATE}/{RECORD}").as_bytes(), Node::File(data));
    Counted {
        inner: fs,
        read: Cell::new(0),
    }
}

fn recorded(fs: &Counted, key: &str) -> Result<Option<Answer>, Refused> {
    Place {
        fs,
        flavour: Flavour::Posix,
        state_dir: STATE.as_bytes(),
        cwd: b"/",
    }
    .recorded(key)
}

fn answer(key: &str) -> Answer {
    Answer {
        key: key.into(),
        git: None,
        stamp: None,
        asked: 1.0,
        ok: false,
        sources: Sources::default(),
        tried: None,
    }
}

#[test]
fn a_record_of_100_mib_is_read_no_further_than_the_bound() {
    on_small_stack(|| {
        let mut data = b"{\"format\": 5, \"entries\": [".to_vec();
        data.resize(100 << 20, b' ');
        let fs = with_record(data);
        assert_eq!(recorded(&fs, "k"), Ok(None));
        assert!(fs.read.get() <= MAX_BYTES + 1, "read {}", fs.read.get());
    });
}

#[test]
fn a_record_of_100_mib_on_disk_is_not_read() {
    on_small_stack(|| {
        let dir = std::env::temp_dir().join(format!("bm-gitsysrob-{}-big", std::process::id()));
        std::fs::create_dir(&dir).unwrap();
        let file = std::fs::File::create(dir.join(RECORD)).unwrap();
        file.set_len(100 << 20).unwrap();
        drop(file);
        let state = dir.to_str().unwrap().to_owned();
        let place = Place {
            fs: &StdFs,
            flavour: host(),
            state_dir: state.as_bytes(),
            cwd: b"",
        };
        assert_eq!(place.recorded("k"), Ok(None));
        let _ = std::fs::remove_dir_all(&dir);
    });
}

#[test]
fn nesting_past_the_bound_and_within_it() {
    on_small_stack(|| {
        let deep = |n: usize| {
            let mut v = vec![b'['; n];
            v.extend(std::iter::repeat_n(b']', n));
            v
        };
        assert_eq!(recorded(&with_record(deep(100_000)), "k"), Ok(None));
        assert_eq!(
            recorded(&with_record(deep(32_000)), "k"),
            Err(Refused::Raises(PyError::RecursionError))
        );
        // An entry nested 9,000 deep is no answer, and is written back
        // whole beside a new one.
        let nested = format!(
            "{{\"format\": 5, \"entries\": [{{\"path\": \"o\", \"x\": {}{}}}]}}",
            "[".repeat(9000),
            "]".repeat(9000)
        );
        assert_eq!(
            recorded(&with_record(nested.clone().into_bytes()), "o"),
            Ok(None)
        );
        let dir = std::env::temp_dir().join(format!("bm-gitsysrob-{}-deep", std::process::id()));
        std::fs::create_dir(&dir).unwrap();
        std::fs::write(dir.join(RECORD), &nested).unwrap();
        let state = dir.to_str().unwrap().to_owned();
        let place = Place {
            fs: &StdFs,
            flavour: host(),
            state_dir: state.as_bytes(),
            cwd: b"",
        };
        let Ok(Recorded::Written(data)) = place.record(&answer("k")) else {
            panic!("not written");
        };
        let text = String::from_utf8(data).unwrap();
        assert!(text.contains(&format!("\"x\": {}{}", "[".repeat(9000), "]".repeat(9000))));
        let _ = std::fs::remove_dir_all(&dir);
    });
}

#[cfg(unix)]
#[test]
fn a_fifo_where_the_record_should_be_is_not_waited_on() {
    on_small_stack(|| {
        let dir = std::env::temp_dir().join(format!("bm-gitsysrob-{}-fifo", std::process::id()));
        std::fs::create_dir(&dir).unwrap();
        let made = std::process::Command::new("mkfifo")
            .arg(dir.join(RECORD))
            .status()
            .is_ok_and(|s| s.success());
        assert!(made, "mkfifo could not run");
        let state = dir.to_str().unwrap().to_owned();
        let place = Place {
            fs: &StdFs,
            flavour: host(),
            state_dir: state.as_bytes(),
            cwd: b"",
        };
        assert_eq!(place.recorded("k"), Ok(None));
        assert!(matches!(
            place.record(&answer("k")),
            Ok(Recorded::Written(_))
        ));
        assert!(std::fs::symlink_metadata(dir.join(RECORD))
            .unwrap()
            .is_file());
        let _ = std::fs::remove_dir_all(&dir);
    });
}

#[test]
fn hostile_bytes_never_panic() {
    on_small_stack(|| {
        let pieces: [&[u8]; 16] = [
            b"{\"format\": 5, \"entries\": [",
            b"{\"path\": \"k\", ",
            b"\"own\": [",
            b"\"stamp\": [",
            b"\"asked\": ",
            b"Infinity",
            b"NaN",
            b"1e999",
            b"\"\\ud800\"",
            b"]",
            b"}",
            b", ",
            b"null",
            b"\xff\xfe",
            b"\"x\": [[[[",
            b"9999999999999999999999999",
        ];
        let mut seed: u64 = 0x9E37_79B9_7F4A_7C15;
        let mut next = || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            seed
        };
        for _ in 0..3000 {
            let n = (next() % 40) as usize;
            let mut data = Vec::new();
            for _ in 0..n {
                let r = next();
                if r % 5 == 0 {
                    data.push((r >> 8) as u8);
                } else {
                    data.extend_from_slice(pieces[(r >> 8) as usize % pieces.len()]);
                }
            }
            let fs = with_record(data);
            let _ = recorded(&fs, "k");
        }
        let long: Vec<u32> = std::iter::repeat_n(0xD800, 1 << 20).collect();
        assert_eq!(key(Some(&long), &[]).len(), 32);
    });
}

#[cfg(unix)]
const UMASK_ROOT_ENV: &str = "BM_TEST_GITSYS_UMASK_ROOT";

/// The child: records into `$ROOT` under the umask its parent's shell
/// set.
#[cfg(unix)]
#[test]
#[ignore = "run by the umask test as its child"]
fn record_under_umask_child() {
    let Some(root) = std::env::var_os(UMASK_ROOT_ENV) else {
        return;
    };
    let state = root.to_str().unwrap().to_owned();
    let place = Place {
        fs: &StdFs,
        flavour: Flavour::Posix,
        state_dir: state.as_bytes(),
        cwd: b"",
    };
    assert!(matches!(
        place.record(&answer("k")),
        Ok(Recorded::Written(_))
    ));
}

/// The record is 0600 whatever the umask, as the reference's `fchmod`
/// makes it: a child under umask 277 (which would leave 0400) records.
/// The umask is the child's alone, set by the shell that starts it.
#[cfg(unix)]
#[test]
fn the_record_is_0600_under_any_umask() {
    use std::os::unix::fs::PermissionsExt;
    let dir = std::env::temp_dir().join(format!("bm-gitsysrob-{}-umask", std::process::id()));
    std::fs::create_dir(&dir).unwrap();
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
    let status = std::process::Command::new("/bin/sh")
        .args([
            "-c",
            "umask 277 && exec \"$0\" --exact record_under_umask_child --ignored --test-threads=1",
        ])
        .arg(std::env::current_exe().unwrap())
        .env(UMASK_ROOT_ENV, &dir)
        .stdout(std::process::Stdio::null())
        .status()
        .unwrap();
    assert!(status.success());
    let mode = std::fs::metadata(dir.join(RECORD))
        .unwrap()
        .permissions()
        .mode()
        & 0o777;
    let _ = std::fs::remove_dir_all(&dir);
    assert_eq!(mode, 0o600);
}
