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

/// `fcntl.fcntl(fd, F_GETFL)`: the status flags of an open file, tried
/// again when a signal interrupts it, as Python does.
#[cfg(unix)]
pub fn status_flags(file: &File) -> io::Result<i32> {
    use std::os::fd::AsRawFd;
    loop {
        // SAFETY: `file` is borrowed, so its descriptor is open for the
        // length of the call; `F_GETFL` takes no argument and reads and
        // writes no memory of this process.
        let flags = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_GETFL) };
        if flags != -1 {
            return Ok(flags);
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}

/// `fcntl.fcntl(fd, F_SETFL, flags)`: set the status flags of an open
/// file, tried again when a signal interrupts it, as Python does. The
/// reads of the checkout token clear `O_NONBLOCK` with it.
#[cfg(unix)]
pub fn set_status_flags(file: &File, flags: i32) -> io::Result<()> {
    use std::os::fd::AsRawFd;
    loop {
        // SAFETY: `file` is borrowed, so its descriptor is open for the
        // length of the call; `F_SETFL` takes its argument by value (the
        // kernel ignores the bits it does not know) and reads and writes
        // no memory of this process.
        let status = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_SETFL, flags) };
        if status != -1 {
            return Ok(());
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}

/// `os.close(fd)`: close a file and answer what `close(2)` answers, which
/// dropping a `File` hides. Not tried again on EINTR, as Python's is not
/// (the descriptor is gone either way).
#[cfg(unix)]
pub fn close(file: File) -> io::Result<()> {
    use std::os::fd::IntoRawFd;
    let fd = file.into_raw_fd();
    // SAFETY: `into_raw_fd` gave up the one owner of an open descriptor,
    // so nothing else closes or uses it; it is closed exactly once here,
    // and `close` reads and writes no memory of this process.
    let status = unsafe { libc::close(fd) };
    if status == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

/// Elsewhere the standard library closes the handle: its answer is not
/// read.
#[cfg(not(unix))]
pub fn close(file: File) -> io::Result<()> {
    drop(file);
    Ok(())
}

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

/// `os.utime(path)` with no times on POSIX: `utimensat(AT_FDCWD, path,
/// NULL, 0)`, which sets both times to the file system's own "now"
/// (`UTIME_NOW`: on NFS the server's clock, not this machine's). It
/// opens nothing, so it needs no read access, only ownership or write
/// access, as the reference does. A signal interruption is retried.
#[cfg(unix)]
pub fn utime_now(path: &std::ffi::CStr) -> io::Result<()> {
    loop {
        // SAFETY: `path` is a borrowed `CStr`, valid and NUL-terminated
        // for the call; a null `times` pointer is the documented request
        // for the current time, and the call writes no memory of this
        // process.
        let status = unsafe { libc::utimensat(libc::AT_FDCWD, path.as_ptr(), std::ptr::null(), 0) };
        if status == 0 {
            return Ok(());
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
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
/// name. On macOS and Linux it also names every errno both share that
/// CPython raises as plain `OSError` (EMFILE, ENFILE, ENOMEM, EBUSY,
/// EOVERFLOW, EHOSTDOWN and the rest), so a read that fails with one is
/// worded as the reference words it. Left out are the errnos CPython
/// raises as a subclass `PyErr` has no class for (ESRCH, ECHILD,
/// EPIPE, the connection errors, EINPROGRESS, EALREADY), a number that
/// two names share on either platform (EAGAIN and EWOULDBLOCK, EDEADLK
/// and EDEADLOCK on Linux, ENOTSUP and EOPNOTSUPP on Linux), and the
/// names one platform alone has; the fixture generator refuses to record
/// a shared number. The shared list is `ERRNO_NAMES` in
/// `tests/contract/gen_fs_stat.py`, held equal by a native test.
///
/// macOS alone adds EOPNOTSUPP (102 there, ENOTSUP 45: no shared number),
/// what opening a socket answers; Linux answers ENXIO for that open,
/// which is in the shared list. On Linux an EOPNOTSUPP (= ENOTSUP, 95)
/// stays None: a caller sees an OSError with no errno name, the class
/// alone. A fixture shared with Linux records no socket.
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
        #[cfg(target_os = "macos")]
        libc::EOPNOTSUPP => "EOPNOTSUPP",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::E2BIG => "E2BIG",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENOEXEC => "ENOEXEC",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENOMEM => "ENOMEM",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EFAULT => "EFAULT",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENOTBLK => "ENOTBLK",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EBUSY => "EBUSY",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EXDEV => "EXDEV",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENODEV => "ENODEV",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENFILE => "ENFILE",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EMFILE => "EMFILE",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENOTTY => "ENOTTY",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ETXTBSY => "ETXTBSY",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EFBIG => "EFBIG",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENOSPC => "ENOSPC",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ESPIPE => "ESPIPE",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EROFS => "EROFS",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EMLINK => "EMLINK",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EDOM => "EDOM",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ERANGE => "ERANGE",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENOTSOCK => "ENOTSOCK",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EDESTADDRREQ => "EDESTADDRREQ",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EMSGSIZE => "EMSGSIZE",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EPROTOTYPE => "EPROTOTYPE",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENOPROTOOPT => "ENOPROTOOPT",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EPROTONOSUPPORT => "EPROTONOSUPPORT",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ESOCKTNOSUPPORT => "ESOCKTNOSUPPORT",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EPFNOSUPPORT => "EPFNOSUPPORT",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EAFNOSUPPORT => "EAFNOSUPPORT",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EADDRINUSE => "EADDRINUSE",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EADDRNOTAVAIL => "EADDRNOTAVAIL",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENETDOWN => "ENETDOWN",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENETUNREACH => "ENETUNREACH",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENETRESET => "ENETRESET",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENOBUFS => "ENOBUFS",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EISCONN => "EISCONN",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ETOOMANYREFS => "ETOOMANYREFS",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EHOSTDOWN => "EHOSTDOWN",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EHOSTUNREACH => "EHOSTUNREACH",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENOTEMPTY => "ENOTEMPTY",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EUSERS => "EUSERS",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EDQUOT => "EDQUOT",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EREMOTE => "EREMOTE",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENOLCK => "ENOLCK",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENOSYS => "ENOSYS",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EOVERFLOW => "EOVERFLOW",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ECANCELED => "ECANCELED",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EIDRM => "EIDRM",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENOMSG => "ENOMSG",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EBADMSG => "EBADMSG",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EMULTIHOP => "EMULTIHOP",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENODATA => "ENODATA",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENOLINK => "ENOLINK",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENOSR => "ENOSR",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENOSTR => "ENOSTR",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EPROTO => "EPROTO",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ETIME => "ETIME",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::ENOTRECOVERABLE => "ENOTRECOVERABLE",
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        libc::EOWNERDEAD => "EOWNERDEAD",
        _ => return None,
    })
}

/// Elsewhere an `io::Error`'s raw code is the OS's own (a Windows error
/// code), which is no errno: None.
#[cfg(not(unix))]
pub fn errno_name(_code: i32) -> Option<&'static str> {
    None
}

/// The processor time the calling thread has used so far, in user and
/// kernel mode alike: `clock_gettime(CLOCK_THREAD_CPUTIME_ID)`. Only
/// differences between two readings on the same thread mean anything.
///
/// The test suite's growth claims read it (`tests/timing` of
/// `bettermemory-core`): time the thread waits (on the scheduler while
/// other processes run, on a sleep, on another thread or process, on a
/// device) is not counted, so on a loaded machine it measures the work
/// itself where the wall clock measures the load too.
///
/// None where the clock is refused (the call fails, or answers a value
/// no `Duration` holds).
#[cfg(unix)]
pub fn thread_cpu_time() -> Option<std::time::Duration> {
    // SAFETY: `timespec` holds integers only (on some targets also an
    // integer padding field), for which all-zero bytes are a valid value.
    let mut now: libc::timespec = unsafe { std::mem::zeroed() };
    // SAFETY: `now` is a live `timespec` on this stack, borrowed mutably for
    // the length of the call, and `clock_gettime` writes that struct and no
    // other memory of this process.
    let status = unsafe { libc::clock_gettime(libc::CLOCK_THREAD_CPUTIME_ID, &mut now) };
    if status != 0 {
        return None;
    }
    let seconds = u64::try_from(now.tv_sec).ok()?;
    let nanos = u32::try_from(now.tv_nsec)
        .ok()
        .filter(|n| *n < 1_000_000_000)?;
    Some(std::time::Duration::new(seconds, nanos))
}

/// Not POSIX: no thread clock here (Windows has `GetThreadTimes`, which
/// the standard library does not reach), so None, and a caller times by
/// the wall clock.
#[cfg(not(unix))]
pub fn thread_cpu_time() -> Option<std::time::Duration> {
    None
}

/// The size of the buffer the reference hands `F_GETPATH`
/// (`origin_gitfiles._spelled_as_git`: `bytes(1024)`), which is macOS's
/// `MAXPATHLEN`: the kernel writes a path of at most 1,023 bytes and its
/// NUL, and fails with ENOSPC for a longer one.
pub const KERNEL_PATH_BUFFER: usize = 1024;

/// `fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))` up to its first NUL:
/// the path the kernel holds for an open file, each component in the case
/// and Unicode form the directory stores it in. Tried again when a signal
/// interrupts it, as Python's `fcntl` does.
///
/// macOS only: no other platform has `F_GETPATH`, and the reference calls
/// it nowhere else.
#[cfg(target_os = "macos")]
pub fn kernel_path(file: &File) -> io::Result<Vec<u8>> {
    use std::os::fd::AsRawFd;
    let mut buffer = [0u8; KERNEL_PATH_BUFFER];
    loop {
        // SAFETY: `file` is borrowed, so its descriptor is open for the
        // length of the call. `F_GETPATH` writes at most `MAXPATHLEN`
        // (1,024) bytes through the pointer, and `buffer` is 1,024 bytes
        // this frame owns for the length of the call; the call reads no
        // other memory of this process.
        let status = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_GETPATH, buffer.as_mut_ptr()) };
        if status != -1 {
            let end = buffer.iter().position(|&b| b == 0).unwrap_or(buffer.len());
            return Ok(buffer[..end].to_vec());
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}

/// `os.strerror(code)` on POSIX: the C library's text for an error
/// number, read with the thread-safe `strerror_r` (CPython calls
/// `strerror`, which gives the same text). Every number has a text:
/// macOS, glibc and musl each write their own for a number they define
/// no error for (`Unknown error: 1000`, `Unknown error 1000`, `No error
/// information`), answering EINVAL beside it. None where the text does
/// not fit the buffer or is no UTF-8, never a cut or guessed one. The
/// texts are the C locale's in both processes: CPython takes `LC_CTYPE`
/// alone from the environment, and a Rust process sets no locale.
#[cfg(unix)]
pub fn strerror(code: i32) -> Option<String> {
    let mut buf = [0u8; 1024];
    // SAFETY: `buf` is a live, writable buffer of the length passed; the
    // XSI `strerror_r` (glibc's `__xpg_strerror_r`, which `libc` links
    // there) writes at most that many bytes into it, its NUL included, and
    // touches no other memory of this process. Any `code` is valid.
    let status = unsafe { libc::strerror_r(code, buf.as_mut_ptr().cast(), buf.len()) };
    if status == libc::ERANGE {
        return None;
    }
    let end = buf.iter().position(|&b| b == 0)?;
    if end == 0 {
        return None;
    }
    String::from_utf8(buf[..end].to_vec()).ok()
}

/// Elsewhere `os.strerror` is the C runtime's table, which is not
/// ported: None.
#[cfg(not(unix))]
pub fn strerror(_code: i32) -> Option<String> {
    None
}

/// `socket.gethostname()` on POSIX: `gethostname(2)` into a buffer of
/// 1,024 bytes, as CPython's `socket_gethostname` sizes it, the answer
/// cut at its first NUL. `Err` where the call fails (the reference's
/// OSError).
#[cfg(unix)]
pub fn hostname() -> io::Result<Vec<u8>> {
    let mut buf = [0u8; 1024];
    // SAFETY: `buf` is a live, writable buffer of the length passed less
    // one, so the call writes inside it and the last byte stays NUL.
    let status = unsafe { libc::gethostname(buf.as_mut_ptr().cast(), buf.len() - 1) };
    if status < 0 {
        return Err(io::Error::last_os_error());
    }
    let end = buf.iter().position(|&b| b == 0).unwrap_or(buf.len());
    Ok(buf[..end].to_vec())
}

/// Not POSIX: the Windows call is not ported; the error is Unsupported.
#[cfg(not(unix))]
pub fn hostname() -> io::Result<Vec<u8>> {
    Err(io::Error::from(io::ErrorKind::Unsupported))
}

#[cfg(test)]
mod tests {
    use super::fsync;

    /// The thread clock moves with work done on this thread and not while
    /// the thread sleeps or waits on another thread's work.
    #[cfg(unix)]
    #[test]
    fn the_thread_clock_counts_this_threads_work_only() {
        use super::thread_cpu_time;
        use std::hint::black_box;
        use std::time::{Duration, Instant};
        let spin = |d: Duration| {
            let until = Instant::now() + d;
            let mut n = 0u64;
            while Instant::now() < until {
                n = black_box(n.wrapping_add(1));
            }
            n
        };
        // Spun for 100 ms of the wall clock: the thread runs for some of it
        // however loaded the machine is, and for no more than all of it.
        let wall = Instant::now();
        let start = thread_cpu_time().expect("a thread clock on unix");
        black_box(spin(Duration::from_millis(100)));
        let worked = thread_cpu_time().expect("a thread clock") - start;
        let elapsed = wall.elapsed();
        assert!(
            worked > Duration::ZERO && worked <= elapsed + Duration::from_millis(1),
            "{worked:?} of the thread's time in {elapsed:?}"
        );
        let before = thread_cpu_time().expect("a thread clock");
        std::thread::sleep(Duration::from_millis(300));
        std::thread::spawn(move || spin(Duration::from_millis(300)))
            .join()
            .expect("the other thread");
        let waited = thread_cpu_time().expect("a thread clock") - before;
        assert!(
            waited < Duration::from_millis(100),
            "300 ms asleep and 300 ms waiting on another thread moved it {waited:?}"
        );
    }

    #[cfg(not(unix))]
    #[test]
    fn there_is_no_thread_clock_elsewhere() {
        assert_eq!(super::thread_cpu_time(), None);
    }

    /// The kernel's path of an open directory is where it was made, and
    /// a closed-over missing path is an error at the open, not here.
    #[cfg(target_os = "macos")]
    #[test]
    fn the_kernel_names_an_open_directory() {
        use super::kernel_path;
        let made =
            std::env::temp_dir().join(format!("bettermemory-sys-path-{}", std::process::id()));
        std::fs::create_dir(&made).expect("dir");
        let real = std::fs::canonicalize(&made).expect("resolved");
        let dir = std::fs::File::open(&made).expect("open");
        let path = kernel_path(&dir).expect("F_GETPATH");
        assert_eq!(path, real.as_os_str().as_encoded_bytes());
        let root = std::fs::File::open("/").expect("open the root");
        assert_eq!(kernel_path(&root).expect("F_GETPATH"), b"/");
        std::fs::remove_dir(&made).expect("remove");
    }

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

    /// A file opened non-blocking for appending carries both flags;
    /// `O_NONBLOCK` cleared, the append flag stays; `close` answers 0
    /// for an open file.
    #[cfg(unix)]
    #[test]
    fn status_flags_are_read_and_set_and_a_file_closes() {
        use super::{close, set_status_flags, status_flags, O_NONBLOCK};
        use std::os::unix::fs::OpenOptionsExt;
        let dir =
            std::env::temp_dir().join(format!("bettermemory-sys-flags-{}", std::process::id()));
        std::fs::create_dir(&dir).expect("dir");
        let file = std::fs::OpenOptions::new()
            .append(true)
            .create(true)
            .custom_flags(O_NONBLOCK)
            .open(dir.join("f"))
            .expect("open");
        let flags = status_flags(&file).expect("F_GETFL");
        assert_eq!(flags & O_NONBLOCK, O_NONBLOCK);
        assert_eq!(flags & libc::O_APPEND, libc::O_APPEND);
        set_status_flags(&file, flags & !O_NONBLOCK).expect("F_SETFL");
        assert_eq!(status_flags(&file).expect("F_GETFL"), flags & !O_NONBLOCK);
        close(file).expect("close");
        std::fs::remove_dir_all(&dir).expect("remove");
    }
    use std::io::Write;

    /// `strerror` against CPython 3.13's `os.strerror` of the same
    /// numbers: a named errno, errno 0, a hole in the list where the
    /// platform has one, and numbers past the list, negative ones too
    /// (macOS with CPython 3.13.13; glibc 2.36 and 2.39; musl).
    #[cfg(unix)]
    #[test]
    fn strerror_spells_every_number_as_the_c_library() {
        use super::strerror;
        assert_eq!(
            strerror(libc::ENOENT).as_deref(),
            Some("No such file or directory")
        );
        assert_eq!(strerror(libc::EHOSTDOWN).as_deref(), Some("Host is down"));
        let texts: &[(i32, &str)] = if cfg!(target_os = "macos") {
            &[
                (0, "Undefined error: 0"),
                (1000, "Unknown error: 1000"),
                (-1, "Unknown error: -1"),
                (i32::MAX, "Unknown error: 2147483647"),
            ]
        } else if cfg!(all(target_os = "linux", target_env = "gnu")) {
            &[
                (0, "Success"),
                (41, "Unknown error 41"),
                (1000, "Unknown error 1000"),
                (-1, "Unknown error -1"),
                (i32::MAX, "Unknown error 2147483647"),
            ]
        } else if cfg!(all(target_os = "linux", target_env = "musl")) {
            &[(0, "No error information"), (1000, "No error information")]
        } else {
            &[]
        };
        for &(code, text) in texts {
            assert_eq!(strerror(code).as_deref(), Some(text), "{code}");
        }
    }

    /// Off POSIX nothing is ported: no text for any number.
    #[cfg(not(unix))]
    #[test]
    fn strerror_is_not_ported_elsewhere() {
        assert_eq!(super::strerror(2), None);
    }

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
