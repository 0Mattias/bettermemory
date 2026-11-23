//! The Windows Shell's per-user Local AppData folder and its HRESULT errors.

/// CPython's PC/errmap.h first unwraps a Win32-facility HRESULT. The
/// original signed value is still passed to FormatMessageW and OSError.
fn errno_and_class(hresult: i32) -> (i32, &'static str) {
    let code = if hresult as u32 >> 16 == 0x8007 {
        hresult & 0xffff
    } else {
        hresult
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

#[cfg(windows)]
mod shell {
    use super::errno_and_class;
    use std::ffi::{c_void, OsString};
    use std::os::windows::ffi::OsStringExt;
    use std::{ptr, slice};

    /// The Windows exception produced by a failed ctypes HRESULT return.
    #[derive(Clone, Debug, Eq, PartialEq)]
    pub struct WindowsFolderError {
        /// Original signed HRESULT, also exposed as Python's winerror.
        pub hresult: i32,
        /// The errno selected by CPython's Windows error mapping.
        pub errno: i32,
        /// Python's concrete OSError subclass.
        pub class: &'static str,
        /// Full Python exception text in the system's default language.
        pub message: String,
    }

    impl WindowsFolderError {
        /// Convert a failed HRESULT. Successful statuses have no exception.
        pub fn from_hresult(hresult: i32) -> Option<Self> {
            if hresult >= 0 {
                return None;
            }
            let (errno, class) = errno_and_class(hresult);
            Some(Self {
                hresult,
                errno,
                class,
                message: format!("[WinError {hresult}] {}", system_message(hresult)),
            })
        }
    }

    #[repr(C)]
    struct Guid(u32, u16, u16, [u8; 8]);

    const LOCAL_APP_DATA: Guid = Guid(
        0xf1b32785,
        0x6fba,
        0x4fcf,
        [0x9d, 0x55, 0x7b, 0x8e, 0x7f, 0x15, 0x70, 0x91],
    );
    const DONT_VERIFY: u32 = 0x4000;

    #[link(name = "shell32")]
    unsafe extern "system" {
        fn SHGetKnownFolderPath(
            id: *const Guid,
            flags: u32,
            token: *mut c_void,
            path: *mut *mut u16,
        ) -> i32;
    }

    #[link(name = "ole32")]
    unsafe extern "system" {
        fn CoTaskMemFree(address: *mut c_void);
    }

    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn FormatMessageW(
            flags: u32,
            source: *const c_void,
            message_id: u32,
            language_id: u32,
            output: *mut u16,
            size: u32,
            arguments: *const c_void,
        ) -> u32;
        fn LocalFree(address: *mut c_void) -> *mut c_void;
    }

    fn system_message(code: i32) -> String {
        let mut buf: *mut u16 = ptr::null_mut();
        // SAFETY: ALLOCATE_BUFFER writes the system-allocated pointer to buf.
        // No source or substitution arguments are required with these flags.
        let len = unsafe {
            FormatMessageW(
                0x100 | 0x1000 | 0x200, // allocate, from system, ignore inserts
                ptr::null(),
                code as u32,
                0x400, // MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT)
                (&raw mut buf).cast(),
                0,
                ptr::null(),
            )
        };
        if len == 0 {
            return format!("Windows Error 0x{:x}", code as u32);
        }
        // SAFETY: FormatMessageW returned this many UTF-16 code units;
        // the buffer stays owned until after the text has been copied.
        let wide = unsafe { slice::from_raw_parts(buf, len as usize) };
        let end = wide
            .iter()
            .rposition(|&c| c > 32 && c != 46)
            .map_or(0, |i| i + 1);
        let text = String::from_utf16_lossy(&wide[..end]);
        // SAFETY: this is the buffer returned by FormatMessageW above.
        unsafe { LocalFree(buf.cast()) };
        text
    }

    type Release = unsafe extern "system" fn(*mut c_void);
    struct TaskPath {
        ptr: *mut u16,
        release: Release,
    }
    impl Drop for TaskPath {
        fn drop(&mut self) {
            // SAFETY: folder_with requires a matching allocator. It accepts
            // NULL, and the owned output has no other release site.
            unsafe { (self.release)(self.ptr.cast()) };
        }
    }

    /// Ask the Windows Shell for the current user's Local AppData directory
    /// without checking or creating it. A successful null pointer is distinct
    /// from an allocated empty string, as in platformdirs.
    pub fn local_app_data() -> Result<Option<OsString>, WindowsFolderError> {
        // SAFETY: the Shell returns a CoTaskMem-owned, nul-terminated wide
        // string on success. Both APIs accept the pointer forms below.
        unsafe {
            folder_with(
                |out| SHGetKnownFolderPath(&LOCAL_APP_DATA, DONT_VERIFY, ptr::null_mut(), out),
                CoTaskMemFree,
            )
        }
    }

    // On success call must return either NULL or a nul-terminated wide string.
    // On every path release must accept the output, including NULL.
    unsafe fn folder_with(
        call: impl FnOnce(*mut *mut u16) -> i32,
        release: Release,
    ) -> Result<Option<OsString>, WindowsFolderError> {
        let mut path = TaskPath {
            ptr: ptr::null_mut(),
            release,
        };
        let status = call(&raw mut path.ptr);
        if let Some(error) = WindowsFolderError::from_hresult(status) {
            return Err(error);
        }
        if path.ptr.is_null() {
            return Ok(None);
        }
        let mut len = 0;
        // SAFETY: the successful non-null output is nul-terminated and
        // TaskPath owns its storage through the copy into OsString.
        unsafe {
            while *path.ptr.add(len) != 0 {
                len += 1;
            }
            Ok(Some(OsString::from_wide(slice::from_raw_parts(
                path.ptr, len,
            ))))
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use std::cell::Cell;
        use std::os::windows::ffi::OsStrExt;

        thread_local! { static RELEASED: Cell<(usize, *mut c_void)> = const { Cell::new((0, ptr::null_mut())) }; }
        unsafe extern "system" fn record_release(p: *mut c_void) {
            RELEASED.with(|last| last.set((last.get().0 + 1, p)));
        }

        #[test]
        fn releases_every_output_and_preserves_wide_names() {
            let mut name = [0xd800, 120, 0];
            for status in [0, 1, 0x80070005u32 as i32] {
                for p in [ptr::null_mut(), name.as_mut_ptr(), name[2..].as_mut_ptr()] {
                    // SAFETY: these strings live through the call and the
                    // matching test release only records its argument.
                    let result = unsafe {
                        folder_with(
                            |out| {
                                *out = p;
                                status
                            },
                            record_release,
                        )
                    };
                    RELEASED
                        .with(|last| assert_eq!(last.replace((0, ptr::null_mut())), (1, p.cast())));
                    if status < 0 {
                        let err = result.unwrap_err();
                        assert_eq!(
                            (err.hresult, err.errno, err.class),
                            (status, 13, "PermissionError")
                        );
                        assert!(err.message.starts_with("[WinError -2147024891] "));
                    } else if p.is_null() {
                        assert_eq!(result.unwrap(), None);
                    } else {
                        let expected = if p == name.as_mut_ptr() {
                            &name[..2]
                        } else {
                            &[]
                        };
                        assert_eq!(
                            result.unwrap().unwrap().encode_wide().collect::<Vec<_>>(),
                            expected
                        );
                    }
                }
            }
        }
    }
}
#[cfg(windows)]
pub use shell::{local_app_data, WindowsFolderError};

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn win32_facility_is_unwrapped_only_for_errno() {
        for (win32, expected) in [
            (2, (2, "FileNotFoundError")),
            (5, (13, "PermissionError")),
            (267, (20, "NotADirectoryError")),
            (258, (138, "TimeoutError")),
            (10004, (4, "InterruptedError")),
            (10058, (10058, "BrokenPipeError")),
            (10061, (10061, "ConnectionRefusedError")),
        ] {
            assert_eq!(errno_and_class((0x80070000u32 | win32) as i32), expected);
        }
        assert_eq!(errno_and_class(0x80004005u32 as i32), (22, "OSError"));
    }
}
