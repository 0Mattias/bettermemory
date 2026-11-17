//! The stamped reads stay bounded and typed on a 512 KiB thread: 100,000
//! files read through one cache (256 kept, the oldest dropped first),
//! hostile paths, and a file that changes between its stamp and its read
//! (the stamp is taken first, so the read kept is one the next call sees
//! as changed and reads again).
//!
//! [`Swap`] is a test-local wrapper around `MemFs` that delegates every
//! method of `Fs` and makes the change at the file's first open. Once the
//! trait gains `lstat_result` (required) and `readlink_result`
//! (defaulted), it must delegate both too.

use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind, MemFs, Node, OsError, PyErr, Stat, StatResult,
};
use bettermemory_core::gitconfig::paths::Machine;
use bettermemory_core::gitconfig::remotes::{Env, Inputs, Sources};
use bettermemory_core::gitconfig::stamped::{inside, stamped_read, StampedReads, STAMPED_KEPT};
use bettermemory_core::gitfile_rule::format::GitFacts;
use std::cell::{Cell, RefCell};

fn on_small_stack(f: impl FnOnce() + Send + 'static) {
    std::thread::Builder::new()
        .stack_size(512 << 10)
        .spawn(f)
        .expect("a thread")
        .join()
        .expect("no panic");
}

fn with_inputs<R>(f: impl FnOnce(&Inputs) -> R) -> R {
    let none = |_: &[u8]| None;
    let machine = Machine {
        flavour: Flavour::Posix,
        home: Some(b"/h".to_vec()),
        profile: None,
        user_home: &none,
    };
    let inputs = Inputs {
        machine: &machine,
        env: Env::default(),
        sources: Sources::default(),
        git: GitFacts {
            release: Some((2, 54)),
            reads_compat: None,
        },
        cwd: b"/".to_vec(),
        asked_branch: None,
    };
    f(&inputs)
}

#[test]
fn a_hundred_thousand_files_keep_the_newest_256() {
    on_small_stack(|| {
        let mut fs = MemFs::new(Flavour::Posix);
        fs.insert(b"/c/0", Node::File(b"[a]\n\tb = 0\n".to_vec()));
        with_inputs(|inputs| {
            let mut cache = StampedReads::new();
            for i in 0..100_000 {
                let path = format!("/c/{i}");
                stamped_read(&mut cache, &fs, inputs, path.as_bytes(), b"/g").expect("ported");
            }
            // `_STAMPED_KEPT`.
            assert_eq!(STAMPED_KEPT, 256);
            assert_eq!(cache.len(), 256);
            let kept = cache.kept();
            assert_eq!(
                kept[0].0 .0,
                format!("/c/{}", 100_000 - STAMPED_KEPT).into_bytes()
            );
            assert_eq!(kept[STAMPED_KEPT - 1].0 .0, b"/c/99999".to_vec());
            // Read again, the newest stays where it is; an evicted one
            // comes back last.
            let before = kept[STAMPED_KEPT - 1].1;
            stamped_read(&mut cache, &fs, inputs, b"/c/99999", b"/g").expect("ported");
            assert_eq!(cache.kept()[STAMPED_KEPT - 1].1, before);
            stamped_read(&mut cache, &fs, inputs, b"/c/0", b"/g").expect("ported");
            assert_eq!(cache.kept()[STAMPED_KEPT - 1].0 .0, b"/c/0".to_vec());
            assert_eq!(cache.len(), STAMPED_KEPT);
        });
    });
}

#[test]
fn hostile_paths() {
    on_small_stack(|| {
        let fs = MemFs::new(Flavour::Posix);
        with_inputs(|inputs| {
            let mut cache = StampedReads::new();
            let long = vec![b'a'; 1 << 16];
            for path in [&b"/x\xff\xfe"[..], b"/a/../../b", b"", b"rel/x", &long] {
                let _ = stamped_read(&mut cache, &fs, inputs, path, b"/g\xff");
            }
            assert!(cache.len() <= 5);
        });
        assert_eq!(inside(Flavour::Posix, b"/a/b", b"/a"), Some(true));
        assert_eq!(inside(Flavour::Posix, b"/ab", b"/a"), Some(false));
        assert_eq!(inside(Flavour::Posix, b"/a/./b/..", b"/a/"), Some(true));
        assert_eq!(inside(Flavour::Posix, b"//a/b", b"/a"), Some(false));
        assert_eq!(inside(Flavour::Posix, b"/x", b"/"), Some(true));
        assert_eq!(inside(Flavour::Posix, b"a/../../b", b".."), Some(true));
        assert_eq!(inside(Flavour::Windows, b"C:\\a", b"C:\\"), None);
    });
}

/// `MemFs` whose file `at` is rewritten (`then`, and a new time) at its
/// first open: after the reader stamped it and before it reads it.
struct Swap {
    inner: RefCell<MemFs>,
    at: Vec<u8>,
    then: Vec<u8>,
    swapped: Cell<bool>,
}

impl Fs for Swap {
    type File = <MemFs as Fs>::File;
    fn open(&self, path: &[u8]) -> Result<Self::File, PyErr> {
        if path == self.at && !self.swapped.get() {
            self.swapped.set(true);
            self.inner
                .borrow_mut()
                .insert(path, Node::File(self.then.clone()));
        }
        self.inner.borrow().open(path)
    }
    fn fstat(&self, file: &mut Self::File) -> Result<Stat, PyErr> {
        self.inner.borrow().fstat(file)
    }
    fn read(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, PyErr> {
        self.inner.borrow().read(file, max)
    }
    fn access_read(&self, path: &[u8]) -> bool {
        self.inner.borrow().access_read(path)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.inner.borrow().access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<Self::File, OsError> {
        self.inner.borrow().open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.inner.borrow().lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.inner.borrow().readlink(path)
    }
    fn readlink_result(&self, path: &[u8]) -> Result<Vec<u8>, OsError> {
        self.inner.borrow().readlink_result(path)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.inner.borrow().lstat_result(path)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        let mut st = self.inner.borrow().stat(path)?;
        if path == self.at && self.swapped.get() {
            st.mtime_ns = 7;
        }
        Ok(st)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.inner.borrow().listdir(path)
    }
}

#[test]
fn a_file_changed_between_its_stamp_and_its_read_is_read_again() {
    on_small_stack(|| {
        let mut fs = MemFs::new(Flavour::Posix);
        fs.insert(b"/h/.gitconfig", Node::File(b"[a]\n\tb = 1\n".to_vec()));
        let fs = Swap {
            inner: RefCell::new(fs),
            at: b"/h/.gitconfig".to_vec(),
            then: b"[a]\n\tb = 2\n".to_vec(),
            swapped: Cell::new(false),
        };
        with_inputs(|inputs| {
            let mut cache = StampedReads::new();
            let first = stamped_read(&mut cache, &fs, inputs, b"/h/.gitconfig", b"/g").unwrap();
            // Stamped before the change, read after it.
            assert_eq!(first.values[0].1, vec![Some(b"2".to_vec())]);
            let mark = cache.kept()[0].1;
            let second = stamped_read(&mut cache, &fs, inputs, b"/h/.gitconfig", b"/g").unwrap();
            assert_ne!(cache.kept()[0].1, mark, "read again: its stamp moved");
            assert_ne!(first.stamps, second.stamps);
            let mark = cache.kept()[0].1;
            stamped_read(&mut cache, &fs, inputs, b"/h/.gitconfig", b"/g").unwrap();
            assert_eq!(cache.kept()[0].1, mark, "steady: kept");
        });
    });
}

#[test]
fn marks_go_on_across_a_clear() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/c", Node::File(b"[a]\n\tb = 0\n".to_vec()));
    with_inputs(|inputs| {
        let mut cache = StampedReads::new();
        stamped_read(&mut cache, &fs, inputs, b"/c", b"/g").expect("ported");
        let before = cache.kept()[0].1;
        cache.clear();
        assert!(cache.is_empty());
        stamped_read(&mut cache, &fs, inputs, b"/c", b"/g").expect("ported");
        assert_ne!(cache.kept()[0].1, before, "a new read, a new mark");
    });
}
