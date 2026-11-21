//! Cases a plausible wrong `fsutil` fails: a retry off Windows, a retry of
//! an error that is not `PermissionError`, `still` asked before the pause
//! or after the last attempt, a sixth attempt, a constant pause, the
//! Windows codes classified by `io::ErrorKind` alone, a read that stops at
//! the first short read or keeps the extra byte, and the machine's own
//! unlink.

use std::io::Read;

use bettermemory_core::fsdata::PyErr;
use bettermemory_core::fsutil::{
    bounded_stream_read, unlink_error, unlink_retrying, unlink_retrying_by, windows_unlink,
    winerror_is_eacces, Remover, StreamReadError, WindowsUnlink, REPLACE_ATTEMPTS,
    REPLACE_BACKOFF_S,
};

#[derive(Default)]
struct Log {
    fails: Vec<&'static str>,
    tries: usize,
    events: Vec<String>,
}

impl Remover for Log {
    type Error = usize;

    fn unlink(&mut self) -> Result<(), usize> {
        let n = self.tries;
        self.tries += 1;
        self.events.push(format!("unlink{n}"));
        match self.fails.get(n) {
            Some(&"P") => Err(n),
            Some(&"O") => Err(100 + n),
            _ => Ok(()),
        }
    }

    fn permission(&self, error: &usize) -> bool {
        *error < 100
    }

    fn sleep(&mut self, seconds: f64) {
        self.events.push(format!("sleep{seconds}"));
    }
}

#[test]
fn constants_are_the_reference_s() {
    assert_eq!(REPLACE_ATTEMPTS, 5);
    assert_eq!(REPLACE_BACKOFF_S, 0.01);
}

#[test]
fn posix_tries_once() {
    let mut r = Log {
        fails: vec!["P"],
        ..Log::default()
    };
    assert_eq!(unlink_retrying_by(&mut r, false, None), Err(0));
    assert_eq!(r.events, ["unlink0"]);
}

#[test]
fn windows_order_of_events() {
    let mut r = Log {
        fails: vec!["P", "P", "P", "P", "P", "P"],
        ..Log::default()
    };
    let mut asked = Vec::new();
    let mut still = || {
        asked.push(());
        true
    };
    assert_eq!(unlink_retrying_by(&mut r, true, Some(&mut still)), Err(4));
    assert_eq!(asked.len(), 4);
    assert_eq!(
        r.events,
        [
            "unlink0",
            "sleep0.01",
            "unlink1",
            "sleep0.02",
            "unlink2",
            "sleep0.04",
            "unlink3",
            "sleep0.08",
            "unlink4"
        ]
    );
}

#[test]
fn windows_other_error_stops_at_once() {
    let mut r = Log {
        fails: vec!["P", "O"],
        ..Log::default()
    };
    assert_eq!(unlink_retrying_by(&mut r, true, None), Err(101));
    assert_eq!(r.tries, 2);
}

#[test]
fn still_false_gives_up_quietly() {
    let mut r = Log {
        fails: vec!["P", "P"],
        ..Log::default()
    };
    let mut still = || false;
    assert_eq!(unlink_retrying_by(&mut r, true, Some(&mut still)), Ok(()));
    assert_eq!(r.events, ["unlink0", "sleep0.01"]);
}

#[test]
fn winerror_table() {
    for code in [5, 16, 19, 32, 33, 36, 65, 82, 83, 108, 132, 158, 167, 10013] {
        assert!(winerror_is_eacces(code), "{code}");
        assert!(
            winerror_is_eacces(0x8007_0000 | code & 0xFFFF),
            "{code} as HRESULT"
        );
    }
    for code in [
        0,
        2,
        3,
        4,
        17,
        18,
        37,
        80,
        145,
        183,
        1920,
        10004,
        0x8008_0005,
    ] {
        assert!(!winerror_is_eacces(code), "{code}");
    }
}

struct OneByte(Vec<u8>, usize);

impl Read for OneByte {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        if self.1 == self.0.len() || buf.is_empty() {
            return Ok(0);
        }
        buf[0] = self.0[self.1];
        self.1 += 1;
        Ok(1)
    }
}

#[test]
fn short_reads_are_joined() {
    let got = bounded_stream_read(&mut OneByte(b"abcdef".to_vec(), 0), 6).unwrap();
    assert_eq!(got, b"abcdef");
    let over = bounded_stream_read(&mut OneByte(b"abcdefg".to_vec(), 0), 6).unwrap_err();
    assert_eq!(over.message().unwrap(), "stream input exceeds cap 6 bytes");
}

#[test]
fn reads_no_further_than_one_past_the_cap() {
    let mut s = OneByte(vec![1; 100], 0);
    assert!(bounded_stream_read(&mut s, 10).is_err());
    assert_eq!(s.1, 11);
    let mut s = OneByte(vec![1; 100], 0);
    assert!(matches!(
        bounded_stream_read(&mut s, -1),
        Err(StreamReadError::OverCap(-1))
    ));
    assert_eq!(s.1, 0);
    let mut s = OneByte(vec![1; 100], 0);
    assert!(matches!(
        bounded_stream_read(&mut s, -3),
        Err(StreamReadError::BadLength)
    ));
    assert_eq!(s.1, 0);
}

struct Broken;

impl Read for Broken {
    fn read(&mut self, _: &mut [u8]) -> std::io::Result<usize> {
        Err(std::io::Error::other("broken"))
    }
}

#[test]
fn io_error_passes_through() {
    assert!(matches!(
        bounded_stream_read(&mut Broken, 5),
        Err(StreamReadError::Io(_))
    ));
}

/// The machine's unlink: a missing file is FileNotFoundError on every
/// platform (POSIX ENOENT; Windows ERROR_FILE_NOT_FOUND, which is not
/// retried); a present one goes.
#[test]
fn machine_unlink() {
    let dir = std::env::temp_dir().join(format!("fsutil-kills-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let file = dir.join("f");
    std::fs::write(&file, b"x").unwrap();
    assert_eq!(unlink_retrying(&file, None), Ok(()));
    assert!(!file.exists());
    let err = unlink_retrying(&file, None).unwrap_err();
    assert_eq!(err.class, PyErr::FileNotFound);
    std::fs::remove_dir(&dir).unwrap();
}

/// `Py_DeleteFileW`'s choice by attributes: a read-only file or a plain
/// directory is ERROR_ACCESS_DENIED, a directory link goes by
/// `RemoveDirectoryW`, anything else by `DeleteFileW`.
#[test]
fn windows_unlink_by_attributes() {
    assert_eq!(windows_unlink(0x20, false), WindowsUnlink::DeleteFile);
    assert_eq!(windows_unlink(0x80, false), WindowsUnlink::DeleteFile);
    assert_eq!(windows_unlink(0x400, true), WindowsUnlink::DeleteFile);
    assert_eq!(windows_unlink(0x21, false), WindowsUnlink::AccessDenied);
    assert_eq!(windows_unlink(0x401, true), WindowsUnlink::AccessDenied);
    assert_eq!(windows_unlink(0x10, false), WindowsUnlink::AccessDenied);
    assert_eq!(windows_unlink(0x410, true), WindowsUnlink::RemoveDirectory);
    assert_eq!(windows_unlink(0x411, true), WindowsUnlink::RemoveDirectory);
}

/// EACCES (13) is `PermissionError` on POSIX; on Windows the raw code 13
/// is ERROR_INVALID_DATA, which does not map to EACCES.
#[test]
fn unlink_error_of_raw_13() {
    let err = unlink_error(&std::io::Error::from_raw_os_error(13));
    if cfg!(windows) {
        assert_eq!(err.class, PyErr::Os);
    } else {
        assert_eq!(err.class, PyErr::Permission);
    }
}

/// A file in a directory without write permission: POSIX refuses with
/// EACCES, a `PermissionError` (macOS and Linux alike; only macOS was
/// seen). The machine's unlink tries once and asks nothing; the Windows
/// branch over the same remover retries it, and a no from `still` ends
/// the removal quietly after the first pause.
#[cfg(unix)]
#[test]
fn machine_permission_error() {
    use std::os::unix::fs::PermissionsExt;
    let dir = std::env::temp_dir().join(format!("fsutil-kills-ro-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let file = dir.join("f");
    std::fs::write(&file, b"x").unwrap();
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o500)).unwrap();
    let mut asked = 0;
    let mut still = || {
        asked += 1;
        true
    };
    let first = unlink_retrying(&file, Some(&mut still));
    let mut asked_w = 0;
    let mut still_w = || {
        asked_w += 1;
        false
    };
    let windows = unlink_retrying_by(
        &mut bettermemory_core::fsutil::StdRemover { path: &file },
        true,
        Some(&mut still_w),
    );
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
    let denied = file.exists();
    let _ = std::fs::remove_file(&file);
    std::fs::remove_dir(&dir).unwrap();
    if !denied {
        // Running as root: the mode does not refuse.
        return;
    }
    assert_eq!(first.unwrap_err().class, PyErr::Permission);
    assert_eq!(asked, 0);
    assert_eq!(windows, Ok(()));
    assert_eq!(asked_w, 1);
}
