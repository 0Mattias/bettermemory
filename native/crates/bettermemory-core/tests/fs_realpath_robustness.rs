//! `realpath` and `abspath` on hostile paths, on a 512 KiB thread: typed
//! outcomes, no panic, no stack growing with the input.

mod common;
mod timing;

use bettermemory_core::fsdata::realpath::{abspath, realpath, Unresolved};
use bettermemory_core::fsdata::{Flavour, Fs, MemFs, Node, PyErr};

/// Runs `f` on a thread with a 512 KiB stack and returns what it gives.
fn small_stack<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> T {
    std::thread::Builder::new()
        .stack_size(512 << 10)
        .spawn(f)
        .expect("a thread")
        .join()
        .expect("no panic")
}

#[test]
fn a_megabyte_path_resolves_to_a_bounded_answer() {
    let got = small_stack(|| {
        let fs = MemFs::new(Flavour::Posix);
        // 1,024 components of 1 KiB. Each `lstat` reads the whole path so
        // far, as the reference's does (its NUL check and encoding), so
        // the cost is in the count of components, held below.
        let mut piece = b"/".to_vec();
        piece.extend_from_slice(&[b'a'; 1023]);
        let path = piece.repeat(1 << 10);
        realpath(&fs, Flavour::Posix, &path, b"/")
    });
    assert_eq!(got.map(|p| p.len()), Ok(1 << 20));
}

#[test]
fn a_hundred_thousand_components_and_dot_dots() {
    let got = small_stack(|| {
        let mut fs = MemFs::new(Flavour::Posix);
        fs.insert(b"/d/f", Node::File(b"x".to_vec()));
        let mut path = b"/d".to_vec();
        for _ in 0..100_000 {
            path.extend_from_slice(b"/.");
        }
        for _ in 0..100_000 {
            path.extend_from_slice(b"/..");
        }
        path.extend_from_slice(b"/d/f");
        let deep = realpath(&fs, Flavour::Posix, &path, b"/");
        let mut many = Vec::new();
        for _ in 0..100_000 {
            many.extend_from_slice(b"x/");
        }
        let relative = realpath(&fs, Flavour::Posix, &many, b"/d");
        let abs = abspath(Flavour::Posix, &path, b"/");
        (deep, relative.map(|p| p.len()), abs)
    });
    assert_eq!(got.0, Ok(b"/d/f".to_vec()));
    assert_eq!(got.1, Ok(2 + 100_000 * 2 - 1 + 1));
    assert_eq!(got.2, Ok(b"/d/f".to_vec()));
}

#[test]
fn a_chain_of_ten_thousand_links_does_not_recurse() {
    let got = small_stack(|| {
        let mut fs = MemFs::new(Flavour::Posix);
        for i in 0..10_000 {
            fs.insert(
                format!("/l{i}").as_bytes(),
                Node::Link(format!("/l{}", i + 1).into_bytes()),
            );
        }
        fs.insert(b"/l10000", Node::Link(b"end".to_vec()));
        fs.insert(b"/end", Node::Dir);
        let chain = realpath(&fs, Flavour::Posix, b"/l0/x", b"/");
        // And a loop of the same length.
        fs.insert(b"/l10000", Node::Link(b"/l0".to_vec()));
        let looped = realpath(&fs, Flavour::Posix, b"/l0/x", b"/");
        (chain, looped)
    });
    assert_eq!(got.0, Ok(b"/end/x".to_vec()));
    assert_eq!(got.1, Ok(b"/l0/x".to_vec()));
}

/// A file system on which every call fails.
struct Failing(PyErr);

impl Fs for Failing {
    type File = ();
    fn open(&self, _: &[u8]) -> Result<(), PyErr> {
        Err(self.0)
    }
    fn fstat(&self, _: &mut ()) -> Result<bettermemory_core::fsdata::Stat, PyErr> {
        Err(self.0)
    }
    fn read(&self, _: &mut (), _: usize) -> Result<Vec<u8>, PyErr> {
        Err(self.0)
    }
    fn access_read(&self, _: &[u8]) -> bool {
        false
    }
    fn access(&self, path: &[u8], _: bettermemory_core::fsdata::Access) -> Result<bool, PyErr> {
        Ok(self.access_read(path))
    }
    fn open_no_follow(
        &self,
        path: &[u8],
    ) -> Result<Self::File, bettermemory_core::fsdata::OsError> {
        self.open(path)
            .map_err(|class| bettermemory_core::fsdata::OsError {
                class,
                errno: None,
                winerror: None,
            })
    }
    fn lstat(&self, _: &[u8]) -> Result<bettermemory_core::fsdata::Kind, PyErr> {
        Err(self.0)
    }
    fn readlink(&self, _: &[u8]) -> Result<Vec<u8>, PyErr> {
        Err(self.0)
    }
    fn stat(
        &self,
        _: &[u8],
    ) -> Result<bettermemory_core::fsdata::StatResult, bettermemory_core::fsdata::OsError> {
        Err(bettermemory_core::fsdata::OsError {
            class: self.0,
            errno: None,
            winerror: None,
        })
    }
    fn lstat_result(
        &self,
        path: &[u8],
    ) -> Result<bettermemory_core::fsdata::StatResult, bettermemory_core::fsdata::OsError> {
        self.stat(path)
    }
    fn listdir(&self, _: &[u8]) -> Result<Vec<Vec<u8>>, bettermemory_core::fsdata::OsError> {
        Err(bettermemory_core::fsdata::OsError {
            class: self.0,
            errno: None,
            winerror: None,
        })
    }
}

#[test]
fn errors_are_ignored_or_raised_as_the_reference_does() {
    for error in [PyErr::FileNotFound, PyErr::Permission, PyErr::Os] {
        assert_eq!(
            realpath(&Failing(error), Flavour::Posix, b"/a/../b/c", b"/"),
            Ok(b"/b/c".to_vec())
        );
    }
    assert_eq!(
        realpath(&Failing(PyErr::Value), Flavour::Posix, b"/a", b"/"),
        Err(Unresolved::Raised(PyErr::Value))
    );
    let fs = MemFs::new(Flavour::Posix);
    assert_eq!(
        realpath(&fs, Flavour::Posix, b"/a\0b", b"/"),
        Err(Unresolved::Raised(PyErr::Value))
    );
    assert_eq!(
        realpath(&fs, Flavour::Posix, b"/\xff\xfe/x", b"/"),
        Ok(b"/\xff\xfe/x".to_vec())
    );
    // The Windows flavour is refused, never guessed.
    let win = MemFs::new(Flavour::Windows);
    assert_eq!(
        realpath(&win, Flavour::Windows, b"C:\\x", b"C:\\"),
        Err(Unresolved::NotPorted)
    );
    assert_eq!(
        abspath(Flavour::Windows, b"x", b"C:\\"),
        Err(Unresolved::NotPorted)
    );
}

/// A FIFO on the path is stated, never opened: the walk ends whether or
/// not anything writes to it. The wait is only a bound for a failure; a
/// pass does not depend on it.
#[cfg(unix)]
#[test]
fn a_real_fifo_on_the_path_does_not_block() {
    use bettermemory_core::fsdata::StdFs;
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!(
        "bm-fsreal-fifo-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir(&dir).expect("a new scratch directory");
    let dir = std::fs::canonicalize(&dir).expect("resolved");
    let fifo = dir.join("fifo");
    let made = std::process::Command::new("mkfifo")
        .arg(&fifo)
        .status()
        .is_ok_and(|s| s.success());
    let link = dir.join("l");
    let linked = std::os::unix::fs::symlink(&fifo, &link).is_ok();
    let (tx, rx) = std::sync::mpsc::channel();
    let queries: Vec<Vec<u8>> = [fifo.join("x"), link.clone(), fifo.clone()]
        .iter()
        .map(|p| p.to_str().unwrap().as_bytes().to_vec())
        .collect();
    std::thread::spawn(move || {
        let answers: Vec<_> = queries
            .iter()
            .map(|q| realpath(&StdFs, Flavour::Posix, q, b"/"))
            .collect();
        let _ = tx.send(answers);
    });
    let got = rx.recv_timeout(std::time::Duration::from_secs(120));
    let _ = std::fs::remove_dir_all(&dir);
    assert!(made && linked);
    let got = got.expect("the walk ended");
    let fifo_bytes = fifo.to_str().unwrap().as_bytes().to_vec();
    let mut under = fifo_bytes.clone();
    under.extend_from_slice(b"/x");
    assert_eq!(got, vec![Ok(under), Ok(fifo_bytes.clone()), Ok(fifo_bytes)]);
}

/// At a fixed depth, `MemFs`'s walk is linear in the components: a
/// directory 511 deep, then links to `.` a component each. Two sizes are
/// compared; the bound catches a walk whose cost per component grows with
/// the components (the review measured 44.5 s for 10,000 such components
/// before the walk kept its directory as one key). It cannot see the cost
/// of depth, which is held by the test after it.
#[test]
fn the_memfs_walk_is_linear_in_the_path() {
    fn tree() -> (MemFs, Vec<u8>) {
        let mut fs = MemFs::new(Flavour::Posix);
        fs.path_max = usize::MAX;
        fs.link_hops = usize::MAX;
        let mut deep = Vec::new();
        for _ in 0..511 {
            deep.extend_from_slice(b"/d");
        }
        fs.insert(&deep, Node::Dir);
        let mut link = deep.clone();
        link.extend_from_slice(b"/l");
        fs.insert(&link, Node::Link(b".".to_vec()));
        (fs, deep)
    }
    let (fs, deep) = tree();
    let path = |n: usize| {
        let mut p = deep.clone();
        for _ in 0..n {
            p.extend_from_slice(b"/l");
        }
        p.extend_from_slice(b"/");
        p
    };
    let (small, large) = (path(10_000), path(100_000));
    let time = |p: &[u8]| {
        timing::once(|| assert_eq!(fs.lstat(p), Ok(bettermemory_core::fsdata::Kind::Directory)))
    };
    timing::grows_within(
        "10,000 and 100,000 components",
        timing::TENFOLD,
        &mut || time(&small),
        &mut || time(&large),
    );
    // Within the default limits the same tree answers at once.
    let mut bounded = fs.clone();
    bounded.path_max = bettermemory_core::fsdata::PATH_MAX;
    bounded.link_hops = bettermemory_core::fsdata::LINK_HOPS;
    assert_eq!(bounded.lstat(&path(10_000)), Err(PyErr::Os));
}

/// The walk is quadratic in depth, as its documentation says: each
/// component looks up a key as long as the path so far. Doubling the
/// depth may at most quadruple the time; the bound is eight times plus
/// 50 ms on the least times seen (`timing`), so it fails on cubic growth
/// and not on noise.
/// Within `PATH_MAX` the depth is at most 512, which bounds the cost.
#[test]
fn the_memfs_walk_is_at_most_quadratic_in_depth() {
    let deep = |n: usize| {
        let mut p = Vec::new();
        for _ in 0..n {
            p.extend_from_slice(b"/d");
        }
        p
    };
    let tree = |n: usize| {
        let p = deep(n);
        let mut fs = MemFs::new(Flavour::Posix);
        fs.path_max = usize::MAX;
        fs.insert(&p, Node::Dir);
        (fs, p)
    };
    let (small, large) = (tree(4_000), tree(8_000));
    let time = |(fs, p): &(MemFs, Vec<u8>)| {
        timing::once(|| assert_eq!(fs.lstat(p), Ok(bettermemory_core::fsdata::Kind::Directory)))
    };
    timing::grows_within(
        "depth 4,000 and 8,000",
        8,
        &mut || time(&small),
        &mut || time(&large),
    );
}
