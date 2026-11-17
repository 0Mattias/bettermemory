//! A socket where HEAD should be, read by `gitfile_rule::gitdir::read`:
//! OSError with the errno the OS gives (EOPNOTSUPP on macOS, ENXIO on
//! Linux), never a guessed one. Names differ by platform, so no socket is
//! in a POSIX fixture shared between them; each platform holds its own
//! here, and the reference Python is asked the same live where
//! `BM_REFERENCE_PYTHON` is set (NOT RUN where it is not). `MemFs` models
//! the open as Linux answers it.

#![cfg(any(target_os = "macos", target_os = "linux"))]

mod common;

use bettermemory_core::fsdata::{Flavour, MemFs, Node, OsError, PyErr, StdFs};
use bettermemory_core::gitfile_rule::gitdir::{read, Ctx, Failure};

#[cfg(target_os = "macos")]
const SOCKET_ERRNO: &str = "EOPNOTSUPP";
#[cfg(target_os = "linux")]
const SOCKET_ERRNO: &str = "ENXIO";

#[test]
fn a_socket_keeps_the_os_errno() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!(
        "bm-gsock-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir(&dir).expect("a new scratch directory");
    let dir = std::fs::canonicalize(&dir).unwrap();
    let head = dir.join("HEAD");
    let listener = std::os::unix::net::UnixListener::bind(&head).expect("a socket");
    let root = dir.to_str().unwrap().as_bytes().to_vec();
    let ctx = Ctx {
        fs: &StdFs,
        flavour: Flavour::Posix,
        cwd: &root,
    };
    let got = read(&ctx, b"HEAD", 255, true);
    let theirs = common::created::reference_python().map(|python| {
        let out = std::process::Command::new(python)
            .env_clear()
            .arg("-I")
            .arg("-c")
            .arg(
                "import errno, os, sys\n\
                 from bettermemory import gitfile_rule as g\n\
                 os.chdir(sys.argv[1])\n\
                 try:\n\
                 \x20   g._read('HEAD', 255)\n\
                 except OSError as e:\n\
                 \x20   print(type(e).__name__, errno.errorcode.get(e.errno))\n",
            )
            .arg(&dir)
            .output()
            .expect("the reference runs");
        String::from_utf8_lossy(&out.stdout).trim().to_owned()
    });
    drop(listener);
    let _ = std::fs::remove_dir_all(&dir);
    assert_eq!(got, Err(Failure::Os(OsError::new(PyErr::Os, SOCKET_ERRNO))));
    match theirs {
        Ok(line) => assert_eq!(line, format!("OSError {SOCKET_ERRNO}")),
        Err(why) => {
            let test = "gitdir_socket_reference";
            eprintln!("NOT RUN {test}: the live reference not asked: {why}");
            common::parity::record(
                test,
                "reference Python",
                "i",
                0,
                &[test.to_owned()],
                std::slice::from_ref(&why),
            );
        }
    }
}

/// `MemFs` answers a socket's open as Linux does (ENXIO), as its walk
/// follows Linux elsewhere.
#[test]
fn memfs_answers_a_socket_as_linux() {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/HEAD", Node::Socket);
    let ctx = Ctx {
        fs: &fs,
        flavour: Flavour::Posix,
        cwd: b"/",
    };
    assert_eq!(
        read(&ctx, b"/HEAD", 255, true),
        Err(Failure::Os(OsError::new(PyErr::Os, "ENXIO")))
    );
}
