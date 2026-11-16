//! `StdFs`, the machine's file system as the reader asks it, over a git
//! directory written in a scratch directory of the test's own.

use std::path::PathBuf;

mod common;

use bettermemory_core::githead::{self, FsError, GitDir, RefFs, StdFs, REF_LIMIT};

const H1: &str = "1111111111111111111111111111111111111111";

struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn scratch() -> Scratch {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("a clock")
        .as_nanos();
    let dir = std::env::temp_dir().join(format!("bm-githead-{}-{nanos}", std::process::id()));
    std::fs::create_dir_all(&dir).expect("a scratch directory");
    Scratch(dir)
}

fn bytes(path: &std::path::Path) -> Vec<u8> {
    path.to_str()
        .expect("a UTF-8 scratch path")
        .as_bytes()
        .to_vec()
}

#[test]
fn the_machines_file_system_reads_as_the_reference_does() {
    let s = scratch();
    let g = s.0.join("g");
    std::fs::create_dir_all(g.join("refs/heads/dir")).unwrap();
    std::fs::write(g.join("HEAD"), b"ref: refs/heads/main\n").unwrap();
    std::fs::write(g.join("refs/heads/main"), format!("{H1}\n")).unwrap();
    std::fs::write(g.join("big"), vec![b'x'; REF_LIMIT + 10]).unwrap();
    let fs = StdFs;
    let at = |rel: &str| bytes(&g.join(rel));

    assert_eq!(
        fs.read(&at("big"), REF_LIMIT, true).unwrap().len(),
        REF_LIMIT + 1
    );
    assert_eq!(fs.read(&at("missing"), 10, false), Err(FsError::Missing));
    assert_eq!(
        fs.read(&at("refs/heads/dir"), 10, false),
        Err(FsError::IsADirectory)
    );
    assert_eq!(fs.lstat_is_dir(&at("refs/heads/dir")), Ok(true));
    assert_eq!(fs.lstat_is_dir(&at("HEAD")), Ok(false));
    assert_eq!(fs.lstat_is_dir(&at("missing")), Err(FsError::Missing));
    #[cfg(unix)]
    let enotdir = FsError::NotADirectory;
    #[cfg(not(unix))]
    let enotdir = FsError::Missing;
    assert_eq!(fs.read(&at("HEAD/x"), 10, false), Err(enotdir));

    let gd = GitDir {
        gitdir: bytes(&g),
        commondir: bytes(&g),
    };
    assert_eq!(githead::head_sha(&fs, &gd).as_deref(), Some(H1));
    assert_eq!(
        githead::head_ref(&fs, &gd).as_deref(),
        Some("refs/heads/main")
    );
    assert_eq!(githead::head_unborn(&fs, &gd), Some(false));
    std::fs::remove_file(g.join("refs/heads/main")).unwrap();
    std::fs::write(g.join("packed-refs"), format!("{H1} refs/heads/main\n")).unwrap();
    assert_eq!(githead::head_sha(&fs, &gd).as_deref(), Some(H1));
    assert!(githead::packed_names(&fs, &gd, b"refs/heads/main"));
}

#[cfg(unix)]
#[test]
fn a_link_is_refused_where_the_reference_refuses_it() {
    let s = scratch();
    let g = s.0.join("g");
    std::fs::create_dir_all(&g).unwrap();
    std::fs::write(g.join("real"), b"ref: refs/heads/main\n").unwrap();
    std::os::unix::fs::symlink("real", g.join("HEAD")).unwrap();
    let fs = StdFs;
    let head = bytes(&g.join("HEAD"));
    assert_eq!(fs.read(&head, 100, false), Err(FsError::Other));
    assert_eq!(
        fs.read(&head, 100, true).unwrap(),
        b"ref: refs/heads/main\n"
    );
    assert_eq!(fs.lstat_is_dir(&head), Ok(false));
}

/// Windows: a loose ref that is a symbolic link to a directory. CPython's
/// `os.open` follows it and raises PermissionError, `_is_directory` (no
/// follow) sees a link, and the error rises: the reference declines and
/// does not read packed-refs. Creating the link can need a privilege; a
/// runner that refuses it records the gap instead of failing.
#[cfg(windows)]
#[test]
fn a_link_to_a_directory_declines_on_windows() {
    let s = scratch();
    let g = s.0.join("g");
    std::fs::create_dir_all(g.join("refs/heads/d")).unwrap();
    std::fs::write(g.join("HEAD"), b"ref: refs/heads/main\n").unwrap();
    std::fs::write(g.join("packed-refs"), format!("{H1} refs/heads/main\n")).unwrap();
    let link = g.join("refs/heads/main");
    if let Err(e) = std::os::windows::fs::symlink_dir(g.join("refs/heads/d"), &link) {
        common::parity::record(
            "githead_stdfs",
            "none",
            "os",
            0,
            &["a link to a directory".to_owned()],
            &[format!("the runner refused to create a symbolic link: {e}")],
        );
        return;
    }
    let fs = StdFs;
    assert_eq!(fs.read(&bytes(&link), 10, false), Err(FsError::Other));
    assert_eq!(fs.read(&bytes(&link), 10, true), Err(FsError::IsADirectory));
    let gd = GitDir {
        gitdir: bytes(&g),
        commondir: bytes(&g),
    };
    assert_eq!(githead::head_sha(&fs, &gd), None);
    // `_lookup` raised an OSError, not `_Unsettled`: no answer.
    assert_eq!(githead::head_unborn(&fs, &gd), None);
}

/// Windows: a name Windows refuses reads as missing (CPython raises
/// FileNotFoundError for ERROR_INVALID_NAME), so packed-refs answers.
#[cfg(windows)]
#[test]
fn a_name_windows_refuses_reads_as_missing() {
    let s = scratch();
    let g = s.0.join("g");
    std::fs::create_dir_all(g.join("refs/heads")).unwrap();
    std::fs::write(g.join("HEAD"), b"ref: refs/heads/a<b\n").unwrap();
    std::fs::write(g.join("packed-refs"), format!("{H1} refs/heads/a<b\n")).unwrap();
    let fs = StdFs;
    assert_eq!(
        fs.read(&bytes(&g.join("refs/heads/a<b")), 10, false),
        Err(FsError::Missing)
    );
    let gd = GitDir {
        gitdir: bytes(&g),
        commondir: bytes(&g),
    };
    assert_eq!(githead::head_sha(&fs, &gd).as_deref(), Some(H1));
}
