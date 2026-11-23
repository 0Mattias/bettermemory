//! Native file errors converted as CPython's PC/errmap.h (Win32 calls)
//! and the Universal CRT's errno.cpp (read/open/write) convert them.
//! The operating system supplies localized words at the point of diagnosis.

/// Python's errno and exception subclass for a Win32 error.
pub fn win32(code: i32) -> (i32, &'static str) {
    let code = if code as u32 >> 16 == 0x8007 {
        code & 0xffff
    } else {
        code
    };
    let errno = match code {
        2 | 3 | 15 | 18 | 53 | 67 | 161 | 206 => 2, // ENOENT
        10 => 7,                                    // E2BIG
        11 | 188..=202 => 8,                        // ENOEXEC
        6 | 114 | 130 => 9,                         // EBADF
        128 | 129 => 10,                            // ECHILD
        89 | 164 | 215 => 11,                       // EAGAIN
        7..=9 | 1816 => 12,                         // ENOMEM
        5 | 16 | 19..=36 | 65 | 82 | 83 | 108 | 132 | 158 | 167 => 13, // EACCES
        80 | 183 => 17,                             // EEXIST
        17 => 18,                                   // EXDEV
        267 => 20,                                  // ENOTDIR
        4 => 24,                                    // EMFILE
        112 => 28,                                  // ENOSPC
        109 | 232 => 32,                            // EPIPE
        145 => 41,                                  // ENOTEMPTY
        1113 => 42,                                 // EILSEQ
        258 => 138,                                 // ETIMEDOUT (UCRT)
        10004 | 10009 | 10013 | 10014 | 10022 | 10024 => code - 10000,
        10000..12000 => code,
        _ => 22, // EINVAL
    };
    let class = match errno {
        2 => "FileNotFoundError",
        4 => "InterruptedError",
        10 => "ChildProcessError",
        11 | 10035..=10037 => "BlockingIOError",
        13 => "PermissionError",
        17 => "FileExistsError",
        20 => "NotADirectoryError",
        32 | 10058 => "BrokenPipeError",
        138 | 10060 => "TimeoutError",
        10053 => "ConnectionAbortedError",
        10054 => "ConnectionResetError",
        10061 => "ConnectionRefusedError",
        _ => "OSError",
    };
    (errno, class)
}

/// FileIO uses UCRT's table, which predates the Win32-specific additions in CPython.
pub fn crt(code: i32) -> (i32, &'static str) {
    match code {
        232 | 258 | 267 | 10000..=11999 => (22, "OSError"),
        _ => win32(code),
    }
}

/// As os.strerror uses the C runtime, obtain its words separately from a
/// Win32 FormatMessageW result. No static error-string buffer is borrowed.
pub fn crt_words(errno: i32) -> String {
    #[link(name = "ucrt")]
    unsafe extern "C" {
        fn _wcserror_s(out: *mut u16, size: usize, errno: i32) -> i32;
    }
    let mut out = [0u16; 256];
    // SAFETY: out is a writable array of exactly the supplied number of wide
    // elements. errno is an unrestricted integer input to the UCRT function.
    let status = unsafe { _wcserror_s(out.as_mut_ptr(), out.len(), errno) };
    if status != 0 {
        return format!("Unknown error {errno}");
    }
    let len = out.iter().position(|&u| u == 0).unwrap_or(out.len());
    String::from_utf16_lossy(&out[..len])
}
