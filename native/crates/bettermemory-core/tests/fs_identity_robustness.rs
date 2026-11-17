//! `realpath_strict`, `lstat_result`, `samefile` and the stat bits: hostile
//! paths on a 512 KiB thread (debug build), typed answers and no panic;
//! devices and inode numbers apart in `MemFs`; a placeholder by its flag;
//! and (macOS) the file flags read from `st_flags` itself.

use bettermemory_core::fsdata::realpath::{realpath, realpath_strict, StrictUnresolved};
use bettermemory_core::fsdata::{
    samefile, samestat, Flavour, Fs, Kind, MemFs, Node, PyErr, PATH_MAX, SF_DATALESS,
};

fn on_small_stack(work: impl FnOnce() + Send + 'static) {
    std::thread::Builder::new()
        .stack_size(512 * 1024)
        .spawn(work)
        .expect("a thread")
        .join()
        .expect("no panic on a 512 KiB stack");
}

fn tree() -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/f", Node::File(b"x".to_vec()));
    fs.insert(b"/h", Node::Hard(b"/f".to_vec()));
    fs.insert(&b"/d".repeat(PATH_MAX / 2 - 1), Node::Dir);
    fs.insert(b"/loop", Node::Link(b"/loop".to_vec()));
    for i in 0..2000 {
        fs.insert(
            format!("/c{i}").as_bytes(),
            Node::Link(format!("c{}", i + 1).into_bytes()),
        );
    }
    fs.insert(b"/c2000", Node::Link(b"/f".to_vec()));
    fs
}

#[test]
fn hostile_paths_answer_typed_on_a_small_stack() {
    on_small_stack(|| {
        let fs = tree();
        let mut paths: Vec<Vec<u8>> = vec![
            Vec::new(),
            b"/".to_vec(),
            b"\0".to_vec(),
            b"/loop".to_vec(),
            b"/loop/x".to_vec(),
            b"/c0".to_vec(),
            b"/f/".to_vec(),
            b"/f/..".to_vec(),
            b"/f/x".to_vec(),
            b"/missing/x".to_vec(),
            vec![0xff, b'/', 0xfe],
        ];
        paths.push(b"/..".repeat(PATH_MAX));
        paths.push(b"/d".repeat(PATH_MAX / 2 - 1));
        paths.push(b"/d".repeat(PATH_MAX * 4));
        paths.push(b"/".repeat(PATH_MAX * 64));
        for path in &paths {
            let _ = realpath(&fs, Flavour::Posix, path, b"/");
            match realpath_strict(&fs, Flavour::Posix, path, b"/") {
                Ok(_) | Err(StrictUnresolved::Raised(_)) => {}
                Err(StrictUnresolved::NotPorted) => panic!("POSIX is ported"),
            }
            if let Ok(st) = fs.lstat_result(path) {
                assert!(st.is_link().is_some());
            }
            let _ = samefile(&fs, path, b"/f");
        }
        // A loop is ELOOP from its `stat`; a missing middle ENOENT; a
        // file used as a folder ENOTDIR, before the OS is asked.
        let errno = |p: &[u8]| match realpath_strict(&fs, Flavour::Posix, p, b"/") {
            Err(StrictUnresolved::Raised(e)) => e.errno,
            other => panic!("{other:?}"),
        };
        assert_eq!(errno(b"/loop"), Some("ELOOP"));
        assert_eq!(errno(b"/missing/x"), Some("ENOENT"));
        assert_eq!(errno(b"/f/.."), Some("ENOTDIR"));
        assert_eq!(
            realpath_strict(&fs, Flavour::Posix, b"/c0", b"/"),
            Ok(b"/f".to_vec())
        );
        assert_eq!(
            realpath_strict(&fs, Flavour::Posix, b"/a\0b", b"/"),
            Err(StrictUnresolved::Raised(
                bettermemory_core::fsdata::OsError::value()
            ))
        );
        assert_eq!(
            realpath_strict(&fs, Flavour::Windows, b"/f", b"/"),
            Err(StrictUnresolved::NotPorted)
        );
    });
}

/// Two devices can hold one inode number: the same path below two mounts
/// is not the same file (device compared); two files on one device are
/// not either (inode compared); a hard link is.
#[test]
fn samestat_compares_device_and_inode() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/m1/f", Node::File(b"x".to_vec()));
    fs.insert(b"/m2/f", Node::File(b"x".to_vec()));
    fs.insert(b"/m1/g", Node::File(b"x".to_vec()));
    fs.insert(b"/m1/h", Node::Hard(b"/m1/f".to_vec()));
    fs.mount(b"/m1", 7);
    fs.mount(b"/m2", 8);
    let st = |p: &[u8]| fs.stat(p).expect("a file");
    let (a, b) = (st(b"/m1/f"), st(b"/m2/f"));
    assert_eq!(a.ino, b.ino, "the same inode number on two devices");
    assert_ne!(a.dev, b.dev);
    assert_eq!(samestat(&a, &b), Some(false));
    assert_eq!(samestat(&a, &st(b"/m1/g")), Some(false));
    assert_eq!(samestat(&a, &st(b"/m1/h")), Some(true));
    assert_eq!(samefile(&fs, b"/m1/f", b"/m1/h"), Ok(Some(true)));
    assert_eq!(
        samefile(&fs, b"/m1/f", b"/m1/missing").map_err(|e| e.class),
        Err(PyErr::FileNotFound)
    );
    // Windows: no device or inode is ported, so no answer is guessed.
    let mut win = MemFs::new(Flavour::Windows);
    win.insert(b"/f", Node::File(b"x".to_vec()));
    assert_eq!(samefile(&win, b"/f", b"/f"), Ok(None));
}

/// `_placeholder` reads the dataless bit of `st_flags`, of a `stat` and of
/// an `lstat`; `lstat` describes a link itself, `stat` its target.
#[test]
fn a_placeholder_by_its_flag_and_lstat_not_following() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/p", Node::File(Vec::new()));
    fs.insert(b"/l", Node::Link(b"p".to_vec()));
    fs.set_flags(b"/p", SF_DATALESS);
    assert!(fs.stat(b"/p").unwrap().placeholder());
    assert!(fs.lstat_result(b"/p").unwrap().placeholder());
    assert!(fs.stat(b"/l").unwrap().placeholder());
    let link = fs.lstat_result(b"/l").unwrap();
    assert_eq!(link.kind, Kind::Link);
    assert!(!link.placeholder());
    assert_eq!(link.is_link(), Some(true));
    assert_eq!(fs.stat(b"/l").unwrap().is_link(), Some(false));
    fs.set_flags(b"/p", 0x8000);
    assert!(!fs.stat(b"/p").unwrap().placeholder());
}

/// macOS: `flags` is `st_flags` itself. A user can set `UF_HIDDEN`
/// (`chflags hidden`) though never `SF_DATALESS`; the hidden bit is read
/// on the file and not on a link to it under `lstat`.
#[cfg(target_os = "macos")]
#[test]
fn the_flags_are_st_flags() {
    use bettermemory_core::fsdata::StdFs;
    use std::sync::atomic::{AtomicUsize, Ordering};
    const UF_HIDDEN: u32 = 0x8000;
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!(
        "bm-fsflags-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir(&dir).expect("a new scratch directory");
    let file = dir.join("f");
    std::fs::write(&file, b"x").unwrap();
    let link = dir.join("l");
    std::os::unix::fs::symlink(&file, &link).unwrap();
    let set = std::process::Command::new("chflags")
        .arg("hidden")
        .arg(&file)
        .status()
        .is_ok_and(|s| s.success());
    let bytes = |p: &std::path::Path| p.to_str().unwrap().as_bytes().to_vec();
    let st = StdFs.stat(&bytes(&file)).unwrap();
    let through = StdFs.stat(&bytes(&link)).unwrap();
    let itself = StdFs.lstat_result(&bytes(&link)).unwrap();
    let _ = std::fs::remove_dir_all(&dir);
    assert!(set, "chflags hidden");
    assert_ne!(st.flags & UF_HIDDEN, 0, "{:#x}", st.flags);
    assert_ne!(through.flags & UF_HIDDEN, 0);
    assert_eq!(itself.flags & UF_HIDDEN, 0);
    assert!(!st.placeholder());
    assert_eq!(itself.is_link(), Some(true));
}

/// A mount at the root covers every path below it; a deeper mount still
/// wins below its own folder.
#[test]
fn a_mount_at_the_root_covers_everything() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/f", Node::File(b"x".to_vec()));
    fs.insert(b"/m/g", Node::File(b"y".to_vec()));
    fs.mount(b"/", 5);
    fs.mount(b"/m", 6);
    assert_eq!(fs.stat(b"/f").unwrap().dev, Some(5));
    assert_eq!(fs.stat(b"/").unwrap().dev, Some(5));
    assert_eq!(fs.stat(b"/m/g").unwrap().dev, Some(6));
}

/// An entry that stops being a link between the walk's `lstat` and its
/// `readlink` (a race): strict raises what `readlink` raised, with its
/// errno, as Python does; non-strict lets the path stand.
#[test]
fn a_readlink_failing_after_lstat_keeps_its_errno() {
    use bettermemory_core::fsdata::{OsError, StatResult};
    struct Raced(MemFs);
    impl Fs for Raced {
        type File = <MemFs as Fs>::File;
        fn open(&self, path: &[u8]) -> Result<Self::File, PyErr> {
            self.0.open(path)
        }
        fn fstat(&self, file: &mut Self::File) -> Result<bettermemory_core::fsdata::Stat, PyErr> {
            self.0.fstat(file)
        }
        fn read(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, PyErr> {
            self.0.read(file, max)
        }
        fn access(
            &self,
            path: &[u8],
            mode: bettermemory_core::fsdata::Access,
        ) -> Result<bool, PyErr> {
            self.0.access(path, mode)
        }
        fn open_no_follow(&self, path: &[u8]) -> Result<Self::File, OsError> {
            self.0.open_no_follow(path)
        }
        fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
            self.0.lstat(path)
        }
        fn readlink(&self, _: &[u8]) -> Result<Vec<u8>, PyErr> {
            Err(PyErr::FileNotFound)
        }
        fn readlink_result(&self, _: &[u8]) -> Result<Vec<u8>, OsError> {
            Err(OsError::new(PyErr::FileNotFound, "ENOENT"))
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
    mem.insert(b"/f", Node::File(b"x".to_vec()));
    mem.insert(b"/l", Node::Link(b"f".to_vec()));
    let fs = Raced(mem);
    assert_eq!(
        realpath_strict(&fs, Flavour::Posix, b"/l", b"/"),
        Err(StrictUnresolved::Raised(OsError::new(
            PyErr::FileNotFound,
            "ENOENT"
        )))
    );
    assert_eq!(
        realpath(&fs, Flavour::Posix, b"/l", b"/"),
        Ok(b"/l".to_vec())
    );
    // `MemFs` itself: a path that is no link is EINVAL.
    let mut plain = MemFs::new(Flavour::Posix);
    plain.insert(b"/f", Node::File(b"x".to_vec()));
    assert_eq!(
        plain.readlink_result(b"/f").map_err(|e| e.errno),
        Err(Some("EINVAL"))
    );
}
