//! `StdFs` in a scratch directory of the test's own, on every platform:
//! a file, a missing one, a directory and the bounded read, each read as
//! the reference reads it on this platform.

use std::path::PathBuf;

use bettermemory_core::fsdata::{Fs, Kind, PyErr, StdFs};
use bettermemory_core::gitconfig::file::{config_bytes, read_git_config, ConfigBytes, Unread};
use bettermemory_core::gitconfig::FORMAT_LIMIT;

mod common;

struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// A new directory of this test's own: the process id, a process-wide
/// counter and the clock in its name, made with `create_dir`, which
/// fails where the name is taken.
fn scratch() -> Scratch {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("a clock")
        .as_nanos();
    let dir = std::env::temp_dir().join(format!("bm-fsdata-{}-{n}-{nanos}", std::process::id()));
    std::fs::create_dir(&dir).expect("a new scratch directory");
    Scratch(dir)
}

fn bytes(path: &std::path::Path) -> Vec<u8> {
    path.to_str()
        .expect("a UTF-8 scratch path")
        .as_bytes()
        .to_vec()
}

#[test]
fn the_machines_file_system_answers_as_the_reference_reads_it() {
    let s = scratch();
    std::fs::write(s.0.join("c"), b"[core]\n\tbare = true\n").unwrap();
    std::fs::write(s.0.join("big"), vec![b'#'; FORMAT_LIMIT + 10]).unwrap();
    std::fs::create_dir(s.0.join("d")).unwrap();
    let fs = StdFs;

    let c = bytes(&s.0.join("c"));
    let mut file = fs.open(&c).unwrap();
    let stat = fs.fstat(&mut file).unwrap();
    assert_eq!(stat.kind, Kind::Regular);
    assert_eq!(stat.size, 20);
    assert!(stat.identity.is_some());
    assert_eq!(fs.read(&mut file, 6).unwrap(), b"[core]");
    assert_eq!(fs.read(&mut file, 100).unwrap().len(), 14);
    assert!(fs.read(&mut file, 100).unwrap().is_empty());
    assert!(fs.access_read(&c));
    assert_eq!(fs.lstat(&c), Ok(Kind::Regular));
    let read = read_git_config(&fs, &c, None);
    assert_eq!(read.why, None);
    assert_eq!(read.entries.len(), 1);

    let missing = bytes(&s.0.join("missing"));
    assert_eq!(fs.open(&missing).err(), Some(PyErr::FileNotFound));
    assert!(!fs.access_read(&missing));
    assert_eq!(config_bytes(&fs, &missing, false), ConfigBytes::Skipped);
    assert_eq!(read_git_config(&fs, &missing, None).why, None);

    let big = bytes(&s.0.join("big"));
    assert!(matches!(
        config_bytes(&fs, &big, false),
        ConfigBytes::Overflow { .. }
    ));
    assert_eq!(
        read_git_config(&fs, &big, None).why.map(|w| w.1),
        Some(Unread::TooLarge)
    );

    let nul = b"a\0b".to_vec();
    assert_eq!(fs.open(&nul).err(), Some(PyErr::Value));
    assert!(!fs.access_read(&nul));

    let d = bytes(&s.0.join("d"));
    assert_eq!(fs.lstat(&d), Ok(Kind::Directory));
    assert!(fs.access_read(&d));
    #[cfg(unix)]
    {
        assert_eq!(
            read_git_config(&fs, &d, None).why.map(|w| w.1),
            Some(Unread::NotRegular)
        );
        assert_eq!(
            config_bytes(&fs, &d, false),
            ConfigBytes::Dies(bettermemory_core::gitconfig::file::Dies::Folder { path: d.clone() })
        );
    }
    // Windows: `_wopen` of a directory is refused (EACCES), so the
    // reference's readers answer PermissionError (CPython read, not run).
    #[cfg(windows)]
    {
        assert_eq!(fs.open(&d).err(), Some(PyErr::Permission));
        assert_eq!(config_bytes(&fs, &d, true), ConfigBytes::Skipped);
        assert_eq!(
            read_git_config(&fs, &d, None).why.map(|w| w.1),
            Some(Unread::CouldNotRead(PyErr::Permission))
        );
    }
}

/// Windows: a dangling symbolic link. CPython's `os.access` asks
/// `GetFileAttributesW`, which finds the link itself, so `_accessible` is
/// true where the open finds nothing, and git dies. Creating the link can
/// need a privilege; a runner that refuses it records the gap.
#[cfg(windows)]
#[test]
fn a_dangling_link_dies_on_windows() {
    let s = scratch();
    let link = s.0.join("l");
    if let Err(e) = std::os::windows::fs::symlink_file(s.0.join("missing"), &link) {
        common::parity::record(
            "fsdata_stdfs",
            "none",
            "os",
            0,
            &["a dangling link".to_owned()],
            &[format!("the runner refused to create a symbolic link: {e}")],
        );
        return;
    }
    let fs = StdFs;
    let path = bytes(&link);
    assert!(fs.access_read(&path));
    assert_eq!(
        config_bytes(&fs, &path, false),
        ConfigBytes::Dies(bettermemory_core::gitconfig::file::Dies::CannotRead {
            path: path.clone(),
            error: PyErr::FileNotFound,
        })
    );
}
