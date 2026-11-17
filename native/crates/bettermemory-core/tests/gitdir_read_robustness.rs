//! `gitfile_rule::gitdir` on hostile trees, on a 512 KiB thread (debug
//! build): typed answers, no panic, bounded reads; a folder of 100,000
//! names costs what one of 10,000 does (no scan where the reference asks
//! one name); and a FIFO where `HEAD` or `commondir` should be never
//! waits.

use std::cell::Cell;

use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind, MemFs, Node, OsError, PyErr, Stat, StatResult,
};
use bettermemory_core::gitfile_rule::gitdir::{
    git_common, git_read, read, real_to_last, valid_headref, Ctx, Failure, GitRead,
};
use bettermemory_core::gitfile_rule::{HEADREF_READ, REF_LIMIT};

fn on_small_stack(work: impl FnOnce() + Send + 'static) {
    std::thread::Builder::new()
        .stack_size(512 * 1024)
        .spawn(work)
        .expect("a thread")
        .join()
        .expect("no panic on a 512 KiB stack");
}

/// Counts the bytes handed out by reads.
struct Counting {
    inner: MemFs,
    bytes: Cell<usize>,
}

impl Fs for Counting {
    type File = <MemFs as Fs>::File;
    fn open(&self, path: &[u8]) -> Result<Self::File, PyErr> {
        self.inner.open(path)
    }
    fn open_result(&self, path: &[u8]) -> Result<Self::File, OsError> {
        self.inner.open_result(path)
    }
    fn fstat(&self, file: &mut Self::File) -> Result<Stat, PyErr> {
        self.inner.fstat(file)
    }
    fn read(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, PyErr> {
        let got = self.inner.read(file, max)?;
        self.bytes.set(self.bytes.get() + got.len());
        Ok(got)
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
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.inner.stat(path)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.inner.lstat_result(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.inner.listdir(path)
    }
}

fn repo(fs: &mut MemFs, at: &str) {
    fs.insert(
        format!("{at}/HEAD").as_bytes(),
        Node::File(b"ref: refs/heads/main\n".to_vec()),
    );
    fs.insert(format!("{at}/objects").as_bytes(), Node::Dir);
    fs.insert(format!("{at}/refs").as_bytes(), Node::Dir);
}

#[test]
fn hostile_trees_answer_typed_and_bounded_on_a_small_stack() {
    on_small_stack(|| {
        let mut fs = MemFs::new(Flavour::Posix);
        repo(&mut fs, "/r");
        repo(&mut fs, "/big");
        // A `commondir` and a HEAD of 1 MiB.
        fs.insert(b"/big/commondir", Node::File(vec![b'a'; 1 << 20]));
        repo(&mut fs, "/fat");
        fs.insert(
            b"/fat/HEAD",
            Node::File([b"ref: refs/x\n".as_slice(), &vec![b' '; 1 << 20]].concat()),
        );
        // An endless file where `commondir` should be.
        repo(&mut fs, "/endless");
        fs.insert(b"/endless/commondir", Node::Endless);
        // A chain of 2,000 links to a git directory, and a loop.
        for i in 0..2000 {
            fs.insert(
                format!("/c{i}").as_bytes(),
                Node::Link(format!("c{}", i + 1).into_bytes()),
            );
        }
        fs.insert(b"/c2000", Node::Link(b"r".to_vec()));
        repo(&mut fs, "/looped");
        fs.insert(b"/looped/commondir", Node::Link(b"/loop".to_vec()));
        fs.insert(b"/loop", Node::Link(b"/loop".to_vec()));
        repo(&mut fs, "/toloop");
        fs.insert(b"/toloop/commondir", Node::File(b"/loop/x".to_vec()));
        let fs = Counting {
            inner: fs,
            bytes: Cell::new(0),
        };
        let ctx = Ctx {
            fs: &fs,
            flavour: Flavour::Posix,
            cwd: b"/",
        };
        let reads = |f: &dyn Fn()| {
            fs.bytes.set(0);
            f();
            fs.bytes.get()
        };
        let n = reads(&|| assert_eq!(git_read(&ctx, b"/big", true, true), Ok(GitRead::Dies)));
        assert!(n <= REF_LIMIT + 1 + HEADREF_READ + 1, "{n} bytes");
        let n = reads(&|| assert_eq!(git_read(&ctx, b"/endless", true, true), Ok(GitRead::Dies)));
        assert!(n <= REF_LIMIT + 1 + HEADREF_READ + 1, "{n} bytes");
        let n = reads(&|| {
            assert_eq!(valid_headref(&ctx, b"/fat/HEAD", true, true), Ok(true));
        });
        assert!(n <= HEADREF_READ + 1, "{n} bytes");
        // Past the OS's link limit the HEAD cannot be stated (ELOOP): no
        // git directory, as the kernel answers the reference; within it,
        // found.
        assert_eq!(git_read(&ctx, b"/c0", true, true), Ok(GitRead::NotGitDir));
        assert_eq!(
            git_read(&ctx, b"/c1980", true, true),
            Ok(GitRead::Found(b"/c1980".to_vec(), false))
        );
        assert_eq!(git_read(&ctx, b"/looped", true, true), Ok(GitRead::Dies));
        assert_eq!(git_common(&ctx, b"/toloop"), Err(Failure::Unsettled));
        for hostile in [
            &b""[..],
            b"\0",
            b"/r\0",
            &[0xff, 0xfe],
            b"/c0/../r",
            b"//r//",
        ] {
            let _ = git_read(&ctx, hostile, true, true);
            let _ = git_common(&ctx, hostile);
            let _ = real_to_last(&ctx, hostile);
            let _ = read(&ctx, hostile, 8, false);
        }
        // Windows: a relative path, and a `commondir` to resolve, decline.
        let win = Ctx {
            fs: &fs,
            flavour: Flavour::Windows,
            cwd: b"C:\\",
        };
        assert_eq!(read(&win, b"r\\HEAD", 8, true), Err(Failure::NotPorted));
    });
}

/// A git directory beside 10,000 and then 100,000 other names: the test
/// asks a few names, never lists, so the larger costs what the smaller
/// does (under 5 times plus 50 ms, best of five; a scan of the folder
/// would be 10 times).
#[test]
fn a_crowded_folder_is_not_scanned() {
    fn tree(n: usize) -> MemFs {
        let mut fs = MemFs::new(Flavour::Posix);
        repo(&mut fs, "/g");
        for i in 0..n {
            fs.insert(format!("/g/n{i}").as_bytes(), Node::File(Vec::new()));
        }
        fs.insert(b"/g/commondir", Node::File(b"/g".to_vec()));
        fs
    }
    let best = |fs: &MemFs| {
        let ctx = Ctx {
            fs,
            flavour: Flavour::Posix,
            cwd: b"/",
        };
        (0..5)
            .map(|_| {
                let started = std::time::Instant::now();
                for _ in 0..200 {
                    assert_eq!(
                        git_read(&ctx, b"/g", true, true),
                        Ok(GitRead::Found(b"/g".to_vec(), true))
                    );
                }
                started.elapsed()
            })
            .min()
            .unwrap()
    };
    let (small, large) = (tree(10_000), tree(100_000));
    let (s, l) = (best(&small), best(&large));
    assert!(
        l < s * 5 + std::time::Duration::from_millis(50),
        "10,000 names {s:?}, 100,000 names {l:?}"
    );
}

/// A FIFO where `HEAD` and where `commondir` should be: the answers come
/// at once (`_Unsettled` for HEAD, git dying for `commondir`). Should an
/// open wait, a helper opens each FIFO for writing after 60 s, which
/// releases it, and the test fails; passing never waits on the clock.
#[cfg(unix)]
#[test]
fn a_fifo_never_waits() {
    use bettermemory_core::fsdata::StdFs;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::mpsc;
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!(
        "bm-gitdir-fifo-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir(&dir).expect("a new scratch directory");
    let dir = std::fs::canonicalize(&dir).unwrap();
    for g in ["h", "c"] {
        std::fs::create_dir_all(dir.join(g).join("objects")).unwrap();
        std::fs::create_dir_all(dir.join(g).join("refs")).unwrap();
    }
    std::fs::write(dir.join("c/HEAD"), b"ref: refs/heads/main\n").unwrap();
    let fifos = [dir.join("h/HEAD"), dir.join("c/commondir")];
    for f in &fifos {
        let made = std::process::Command::new("mkfifo")
            .arg(f)
            .status()
            .is_ok_and(|s| s.success());
        assert!(made, "mkfifo");
    }
    let (done, finished) = mpsc::channel::<()>();
    let rescue_paths = fifos.clone();
    let rescue = std::thread::spawn(move || {
        if finished
            .recv_timeout(std::time::Duration::from_secs(60))
            .is_ok()
        {
            return false;
        }
        for f in &rescue_paths {
            let _ = std::fs::OpenOptions::new().write(true).open(f);
        }
        true
    });
    let root = dir.to_str().unwrap().as_bytes().to_vec();
    let ctx = Ctx {
        fs: &StdFs,
        flavour: Flavour::Posix,
        cwd: &root,
    };
    let head = git_read(&ctx, &[root.as_slice(), b"/h"].concat(), true, true);
    let common = git_read(&ctx, &[root.as_slice(), b"/c"].concat(), true, true);
    let _ = done.send(());
    let rescued = rescue.join().unwrap();
    let _ = std::fs::remove_dir_all(&dir);
    assert!(!rescued, "an open waited for a writer");
    assert_eq!(head, Ok(GitRead::Dies));
    assert_eq!(common, Ok(GitRead::Dies));
}

/// A file system whose reads hand out one byte at a time (a short read is
/// allowed of `os.read`): `_read` still returns `limit + 1` bytes of a
/// longer file and all of a file of exactly `limit`, by its loop alone.
#[test]
fn short_reads_still_give_limit_plus_one() {
    struct OneByte(Counting);
    impl Fs for OneByte {
        type File = <MemFs as Fs>::File;
        fn open(&self, path: &[u8]) -> Result<Self::File, PyErr> {
            self.0.open(path)
        }
        fn open_result(&self, path: &[u8]) -> Result<Self::File, OsError> {
            self.0.open_result(path)
        }
        fn fstat(&self, file: &mut Self::File) -> Result<Stat, PyErr> {
            self.0.fstat(file)
        }
        fn read(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, PyErr> {
            self.0.read(file, max.min(1))
        }
        fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
            self.0.access(path, mode)
        }
        fn open_no_follow(&self, path: &[u8]) -> Result<Self::File, OsError> {
            self.0.open_no_follow(path)
        }
        fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
            self.0.lstat(path)
        }
        fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
            self.0.readlink(path)
        }
        fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
            self.0.stat(path)
        }
        fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
            self.0.lstat_result(path)
        }
        fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
            self.0.listdir(path)
        }
    }
    let mut mem = MemFs::new(Flavour::Posix);
    mem.insert(b"/long", Node::File(b"abcdefghijkl".to_vec()));
    mem.insert(b"/exact", Node::File(b"abcdefgh".to_vec()));
    let fs = OneByte(Counting {
        inner: mem,
        bytes: Cell::new(0),
    });
    let ctx = Ctx {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/",
    };
    assert_eq!(read(&ctx, b"/long", 8, true), Ok(b"abcdefghi".to_vec()));
    assert_eq!(read(&ctx, b"/exact", 8, false), Ok(b"abcdefgh".to_vec()));
}
