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

#[cfg(test)]
mod tests {
    use super::fsync;
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
