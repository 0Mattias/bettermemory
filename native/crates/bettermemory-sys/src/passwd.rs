//! POSIX account home lookup for `~` and `~name` path expansion.
use std::ffi::{CStr, CString, OsString};
use std::io;
use std::mem::MaybeUninit;
use std::os::unix::ffi::OsStringExt;
use std::path::PathBuf;

/// Read the current user's home, or that of a supplied account. `None` is a
/// genuine missing account. Copy the returned bytes before the buffer drops.
pub fn account_home(name: Option<&[u8]>) -> io::Result<Option<PathBuf>> {
    let name = name.map(CString::new).transpose()?;
    let mut buffer = vec![0; 1024];
    loop {
        let mut record = MaybeUninit::<libc::passwd>::uninit();
        let mut found = std::ptr::null_mut();
        // SAFETY: both calls initialize `record` and `found` against the live
        // writable buffer. A null result is handled before reading pw_dir.
        let rc = unsafe {
            let buf = buffer.as_mut_ptr();
            let size = buffer.len();
            match &name {
                Some(n) => libc::getpwnam_r(n.as_ptr(), record.as_mut_ptr(), buf, size, &mut found),
                None => {
                    libc::getpwuid_r(libc::getuid(), record.as_mut_ptr(), buf, size, &mut found)
                }
            }
        };
        if rc == libc::ERANGE {
            buffer.resize(
                buffer
                    .len()
                    .checked_mul(2)
                    .ok_or(io::ErrorKind::OutOfMemory)?,
                0,
            );
            continue;
        }
        if rc != 0 {
            return Err(io::Error::from_raw_os_error(rc));
        }
        if found.is_null() {
            return Ok(None);
        }
        // SAFETY: a successful lookup returned this live record and a
        // NUL-terminated directory in `buffer`, which we copy here.
        let dir = unsafe { (*found).pw_dir };
        if dir.is_null() {
            return Ok(None);
        }
        // SAFETY: pw_dir is non-null and its backing buffer remains live.
        let bytes = unsafe { CStr::from_ptr(dir).to_bytes().to_owned() };
        return Ok(Some(PathBuf::from(OsString::from_vec(bytes))));
    }
}
