//! The OS calls the native build needs and the standard library does not
//! offer safely. This is the only crate of the native build that may hold
//! `unsafe`; `bettermemory-core` forbids it. Each function here is one
//! call, with its argument's validity guaranteed by a borrowed standard
//! library type, so nothing a caller passes can make it unsound.

#![deny(unsafe_op_in_unsafe_fn)]
#![deny(missing_docs)]

use std::fs::File;
use std::io;

/// The platform's `O_NONBLOCK` open flag, as its C library defines it
/// (it differs between Linux architectures and between the BSDs).
#[cfg(unix)]
pub const O_NONBLOCK: i32 = libc::O_NONBLOCK;

/// `os.fsync(fd)`, the reference's `_fsutil.fsync_file`: flush a file (or
/// a directory opened for reading) to the disk with `fsync(2)`, tried
/// again when a signal interrupts it, as Python does.
///
/// Why not `File::sync_all`: on macOS and iOS the standard library's
/// `sync_all` and `sync_data` are `fcntl(F_FULLFSYNC)`, which also makes
/// the drive empty its cache. The reference does not ask for that, SQLite
/// does not either for the store's own commits (it would take `PRAGMA
/// fullfsync`), and it costs milliseconds a call: with it, a native write
/// moved the head checkpoint 23 times slower than the reference (87
/// against 1,988 writes a second). A head file flushed harder than the
/// commit it names is also the wrong way round.
///
/// On Windows `os.fsync` is `_commit`, which is `FlushFileBuffers`, and so
/// is `File::sync_all`; it needs a handle opened for writing.
pub fn fsync(file: &File) -> io::Result<()> {
    #[cfg(unix)]
    {
        use std::os::fd::AsRawFd;
        loop {
            // SAFETY: `file` is borrowed, so its descriptor is open for the
            // length of the call, and `fsync` reads and writes no memory of
            // this process.
            let status = unsafe { libc::fsync(file.as_raw_fd()) };
            if status == 0 {
                return Ok(());
            }
            let error = io::Error::last_os_error();
            if error.kind() != io::ErrorKind::Interrupted {
                return Err(error);
            }
        }
    }
    #[cfg(not(unix))]
    {
        file.sync_all()
    }
}

/// The platform's `O_NOFOLLOW` open flag: a final symbolic link fails
/// with ELOOP (macOS and Linux alike) instead of being followed.
#[cfg(unix)]
pub const O_NOFOLLOW: i32 = libc::O_NOFOLLOW;

/// The modes `os.access` asks: read (`R_OK`), write (`W_OK`) and execute
/// or search (`X_OK`), any together.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct AccessMode {
    /// `R_OK`.
    pub read: bool,
    /// `W_OK`.
    pub write: bool,
    /// `X_OK`.
    pub execute: bool,
}

/// `os.access(path, mode)` with its defaults (`effective_ids=False`,
/// `follow_symlinks=True`, no `dir_fd`): POSIX `access(2)`, which checks
/// with the real user and group ids, follows links, and asks search
/// permission of every folder on the way. True where it answers 0; False
/// for any error, as CPython answers (it raises nothing but ValueError,
/// for a NUL in the path, which `CStr` cannot hold, so the caller refuses
/// one first).
///
/// Elsewhere there is no `access(2)`: None, and the caller answers from
/// the attributes as CPython's `os.access` does there.
#[cfg(unix)]
pub fn access(path: &std::ffi::CStr, mode: AccessMode) -> Option<bool> {
    let mut bits = libc::F_OK;
    if mode.read {
        bits |= libc::R_OK;
    }
    if mode.write {
        bits |= libc::W_OK;
    }
    if mode.execute {
        bits |= libc::X_OK;
    }
    // SAFETY: `path` is a borrowed `CStr`, so the pointer is valid and
    // NUL-terminated for the length of the call; `access` only reads it
    // and writes no memory of this process. Every `bits` value is a valid
    // mode (an OR of the platform's own constants).
    let status = unsafe { libc::access(path.as_ptr(), bits) };
    Some(status == 0)
}

/// Not POSIX: no `access(2)` here (see the unix version).
#[cfg(not(unix))]
pub fn access(_path: &std::ffi::CStr, _mode: AccessMode) -> Option<bool> {
    None
}

/// The name `errno.errorcode` gives the error number `code` (an
/// `io::Error`'s raw OS error), for the errnos the native readers tell
/// apart. Names, not numbers, are what the fixtures record: ELOOP is 62 on
/// macOS and 40 on Linux. Safe code: it compares constants only.
///
/// None is the answer for any code it does not know, never a guessed
/// name. Among those are EOVERFLOW, EMFILE, ENFILE, ENOTSUP/EOPNOTSUPP,
/// EAGAIN/EWOULDBLOCK, EDEADLK and ENODEV; a number that two names share
/// on a platform (EAGAIN and EWOULDBLOCK, ENOTSUP and EOPNOTSUPP on Linux)
/// is never answered, and the fixture generator refuses to record one. The
/// list is `ERRNO_NAMES` in `tests/contract/gen_fs_stat.py`, held equal by
/// a native test.
#[cfg(unix)]
pub fn errno_name(code: i32) -> Option<&'static str> {
    Some(match code {
        libc::ENOENT => "ENOENT",
        libc::ENOTDIR => "ENOTDIR",
        libc::EISDIR => "EISDIR",
        libc::EACCES => "EACCES",
        libc::EPERM => "EPERM",
        libc::EEXIST => "EEXIST",
        libc::ELOOP => "ELOOP",
        libc::ENAMETOOLONG => "ENAMETOOLONG",
        libc::EIO => "EIO",
        libc::ENXIO => "ENXIO",
        libc::EINVAL => "EINVAL",
        libc::ENOTCONN => "ENOTCONN",
        libc::ESTALE => "ESTALE",
        libc::ETIMEDOUT => "ETIMEDOUT",
        libc::EINTR => "EINTR",
        libc::EBADF => "EBADF",
        libc::EILSEQ => "EILSEQ",
        _ => return None,
    })
}

/// Elsewhere an `io::Error`'s raw code is the OS's own (a Windows error
/// code), which is no errno: None.
#[cfg(not(unix))]
pub fn errno_name(_code: i32) -> Option<&'static str> {
    None
}

#[cfg(test)]
mod tests {
    use super::fsync;

    /// `access` on this test's own file: the bits as set, a missing path
    /// false. Root reads and writes past the bits, so only the execute
    /// answer (root needs one x bit too) and missing paths are asserted
    /// for any user.
    #[cfg(unix)]
    #[test]
    fn access_answers_by_the_bits() {
        use super::{access, AccessMode};
        use std::os::unix::fs::PermissionsExt;
        let dir =
            std::env::temp_dir().join(format!("bettermemory-sys-access-{}", std::process::id()));
        std::fs::create_dir(&dir).expect("dir");
        let file = dir.join("f");
        std::fs::write(&file, b"x").expect("write");
        std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o600)).expect("chmod");
        let c = |p: &std::path::Path| {
            std::ffi::CString::new(p.as_os_str().as_encoded_bytes()).expect("no NUL")
        };
        let x = AccessMode {
            execute: true,
            ..AccessMode::default()
        };
        let r = AccessMode {
            read: true,
            ..AccessMode::default()
        };
        assert_eq!(access(&c(&file), x), Some(false));
        assert_eq!(access(&c(&file), AccessMode::default()), Some(true));
        assert_eq!(access(&c(&dir.join("missing")), r), Some(false));
        assert_eq!(access(&c(&dir), x), Some(true));
        std::fs::remove_dir_all(&dir).expect("remove");
    }
    use std::io::Write;

    #[test]
    fn a_written_file_and_its_directory_sync() {
        let dir = std::env::temp_dir().join(format!("bettermemory-sys-{}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("dir");
        let path = dir.join("f");
        let mut file = std::fs::File::create(&path).expect("create");
        file.write_all(b"x").expect("write");
        fsync(&file).expect("fsync a file");
        drop(file);
        // A handle opened for reading, and a directory: `fsync(2)` takes
        // both. `FlushFileBuffers` wants write access ("Access is denied"
        // on the Windows runner), and no caller syncs such a handle there.
        #[cfg(unix)]
        {
            let read_only = std::fs::File::open(&path).expect("open");
            fsync(&read_only).expect("fsync a file opened for reading");
            fsync(&std::fs::File::open(&dir).expect("open dir")).expect("fsync a directory");
        }
        std::fs::remove_dir_all(&dir).expect("remove");
    }
}
