//! Hostile input for `_read_prefix`, `relative_to_root` and
//! `_spelled_as_git`: very long and very deep paths, separators and NULs
//! by the megabyte, lengths at the integer bounds. No panic, and the
//! walk over a candidate's parents grows linearly with its depth where
//! each stat is constant time.

use std::time::{Duration, Instant};

use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind, MemFile, MemFs, Node, OsError, PyErr, Refused, Stat, StatResult,
};
use bettermemory_core::origin_gitfiles::machine::{
    read_prefix, relative_to_root, spelled_as_git, NotRelative,
};

/// `MemFs` whose `stat` refuses every path but the root's in constant
/// time, so the walk's own cost is what is measured.
struct Refusing {
    inner: MemFs,
}

impl Fs for Refusing {
    type File = MemFile;
    fn open(&self, path: &[u8]) -> Result<MemFile, PyErr> {
        self.inner.open(path)
    }
    fn fstat(&self, file: &mut MemFile) -> Result<Stat, PyErr> {
        self.inner.fstat(file)
    }
    fn read(&self, file: &mut MemFile, max: usize) -> Result<Vec<u8>, PyErr> {
        self.inner.read(file, max)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.inner.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<MemFile, OsError> {
        self.inner.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.inner.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.inner.readlink(path)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        if path == b"/r" {
            return self.inner.stat(path);
        }
        Err(OsError::new(PyErr::Permission, "EACCES"))
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.inner.lstat_result(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.inner.listdir(path)
    }
}

fn fastest_of_three(mut run: impl FnMut()) -> Duration {
    (0..3)
        .map(|_| {
            let start = Instant::now();
            run();
            start.elapsed()
        })
        .min()
        .unwrap()
}

fn deep(depth: usize) -> Vec<u8> {
    let mut out = Vec::with_capacity(depth * 2);
    for _ in 0..depth {
        out.extend_from_slice(b"/a");
    }
    out
}

#[test]
fn the_parent_walk_grows_linearly_with_depth() {
    let mut inner = MemFs::new(Flavour::Posix);
    inner.insert(b"/r", Node::Dir);
    let fs = Refusing { inner };
    let n = 200_000;
    let small = deep(n);
    let large = deep(2 * n);
    let check = |c: &[u8]| {
        let got = relative_to_root(&fs, c, b"/r");
        assert!(matches!(got, Err(NotRelative::ValueError(_))));
    };
    let t1 = fastest_of_three(|| check(&small));
    let t2 = fastest_of_three(|| check(&large));
    assert!(
        t2 < t1 * 4 + Duration::from_millis(50),
        "2n took {t2:?} against n's {t1:?}: not linear"
    );
}

#[test]
fn a_megabyte_of_separators_and_dots_is_one_path() {
    let fs = MemFs::new(Flavour::Posix);
    let mut c = vec![b'/'; 1 << 20];
    c.extend_from_slice(b"x");
    for _ in 0..100_000 {
        c.extend_from_slice(b"/./");
    }
    c.extend_from_slice(b"y");
    assert_eq!(relative_to_root(&fs, &c, b"/x"), Ok(b"y".to_vec()));
    assert_eq!(spelled_as_git(&fs, &c, false), Ok(Some(b"/x/y".to_vec())));
}

#[test]
fn nuls_by_the_thousand_raise_without_a_panic() {
    let fs = MemFs::new(Flavour::Posix);
    let c: Vec<u8> = std::iter::repeat_n(*b"/\0", 50_000).flatten().collect();
    assert!(matches!(
        relative_to_root(&fs, &c, b"/elsewhere/x"),
        Err(NotRelative::ValueError(_))
    ));
    assert_eq!(
        spelled_as_git(&fs, &c, true),
        Err(Refused::Raised(PyErr::Value))
    );
}

#[test]
fn a_path_past_path_max_is_none_on_macos() {
    let fs = MemFs::new(Flavour::Posix);
    let c = deep(100_000);
    assert_eq!(spelled_as_git(&fs, &c, true), Ok(None));
    assert!(matches!(
        relative_to_root(&fs, &c, b"/b"),
        Err(NotRelative::ValueError(_))
    ));
}

#[test]
fn lengths_at_the_integer_bounds() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/f", Node::File(vec![7; 3 << 20]));
    let mut f = fs.open(b"/f").unwrap();
    assert_eq!(read_prefix(&fs, &mut f, usize::MAX).unwrap().len(), 3 << 20);
    assert_eq!(
        read_prefix(&fs, &mut f, usize::MAX - 1).unwrap().len(),
        3 << 20
    );
    assert_eq!(read_prefix(&fs, &mut f, 0).unwrap(), b"");
    let mut d = fs.open(b"/").unwrap();
    assert_eq!(
        read_prefix(&fs, &mut d, usize::MAX),
        Err(Refused::Raised(PyErr::IsADirectory))
    );
}

#[test]
fn a_large_prefix_reads_in_linear_time() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/f", Node::File(vec![1; 16 << 20]));
    let run = |n: usize| {
        fastest_of_three(|| {
            let mut f = fs.open(b"/f").unwrap();
            assert_eq!(read_prefix(&fs, &mut f, n).unwrap().len(), n);
        })
    };
    let t1 = run(4 << 20);
    let t2 = run(8 << 20);
    assert!(
        t2 < t1 * 4 + Duration::from_millis(50),
        "2n took {t2:?} against n's {t1:?}"
    );
}

/// A FIFO named as the root. On macOS the reference opens the root
/// (`os.open(root, O_RDONLY | O_CLOEXEC)`), so the answer waits for a
/// writer and comes once one opens. Elsewhere the reference answers the
/// root itself and opens nothing, so the answer comes at once.
#[cfg(unix)]
#[test]
fn a_fifo_root_waits_for_a_writer_on_macos_alone() {
    use bettermemory_core::fsdata::StdFs;
    use std::os::unix::ffi::OsStrExt;
    let dir = std::env::temp_dir().join(format!("bm-ogf-fifo-{}", std::process::id()));
    std::fs::create_dir(&dir).unwrap();
    let fifo = dir.join("pipe");
    let made = std::process::Command::new("mkfifo")
        .arg(&fifo)
        .status()
        .is_ok_and(|s| s.success());
    if !made {
        std::fs::remove_dir_all(&dir).unwrap();
        eprintln!("SKIPPED: mkfifo is not on PATH");
        return;
    }
    let path = fifo.as_os_str().as_bytes().to_vec();
    let (sent, got) = std::sync::mpsc::channel();
    let reader = std::thread::spawn(move || {
        let _ = sent.send(spelled_as_git(&StdFs, &path, cfg!(target_os = "macos")));
    });
    let early = got.recv_timeout(Duration::from_millis(300));
    // Non-blocking, so that with no reader waiting the open fails (ENXIO)
    // in place of waiting itself.
    let writer = {
        use std::os::unix::fs::OpenOptionsExt;
        std::fs::OpenOptions::new()
            .write(true)
            .custom_flags(bettermemory_sys::O_NONBLOCK)
            .open(&fifo)
    };
    // An early answer is the reader's one message, so nothing more is
    // waited for after it.
    let late = if early.is_ok() {
        None
    } else {
        Some(got.recv_timeout(Duration::from_secs(60)))
    };
    drop(writer);
    reader.join().unwrap();
    std::fs::remove_dir_all(&dir).unwrap();
    if cfg!(target_os = "macos") {
        assert!(early.is_err(), "answered before a writer opened: {early:?}");
        let answer = late
            .expect("no early answer on macOS")
            .expect("an answer once a writer opened");
        assert!(matches!(answer, Ok(Some(_))), "{answer:?}");
    } else {
        let answer = early.expect("an answer at once: nothing is opened off macOS");
        assert_eq!(answer, Ok(Some(fifo.as_os_str().as_bytes().to_vec())));
    }
}
