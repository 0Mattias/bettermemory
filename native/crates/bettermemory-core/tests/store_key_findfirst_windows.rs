//! Real Windows reference for the directory-entry lookup used by non-strict Path.resolve.
#![cfg(windows)]
mod common;

use bettermemory_core::log::ring::KeyFiles;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::key_state::{warn_bound_elsewhere, Level};
use bettermemory_core::unicode::cps;
use common::{created, parity, stores::scratch};
use serde_json::Value;
use std::{fs, os::windows::ffi::OsStrExt, path::Path};

const NAME: &str = "denied_key_state_path_matches_windows_python";
const REFERENCE: &str = r#"
import ctypes, json, nt, sys
from ctypes import wintypes
from pathlib import Path
from bettermemory.store_keys import _resolved_for_guard
path, missing = map(Path, sys.argv[1:3])
try:
    nt._getfinalpathname(path)
except OSError as exc:
    denied = exc.winerror
else:
    raise AssertionError('the file must be pending deletion')
try:
    nt._getfinalpathname(missing)
except OSError as exc:
    absent = exc.winerror
else:
    raise AssertionError('expected an absent file')
def words(s):
    data = s.encode('utf-16-le', 'surrogatepass')
    return [int.from_bytes(data[i:i+2], 'little') for i in range(0,len(data),2)]
api = ctypes.WinDLL('kernel32', use_last_error=True)
api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
api.OpenProcess.restype = wintypes.HANDLE
api.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
api.CloseHandle.argtypes = [wintypes.HANDLE]
parent = api.OpenProcess(0x1000, False, int(sys.argv[3]))
if not parent:
    raise ctypes.WinError(ctypes.get_last_error())
try:
    count = wintypes.DWORD()
    if not api.GetProcessHandleCount(parent, ctypes.byref(count)):
        raise ctypes.WinError(ctypes.get_last_error())
finally:
    api.CloseHandle(parent)
resolved = _resolved_for_guard(path)
if resolved != _resolved_for_guard(path.parent / 'child' / '..' / path.name):
    raise AssertionError('guard aliases differ')
print(json.dumps([denied, absent, words(resolved.name), count.value]))
"#;

/// Leave `path` pending deletion and return the handle that keeps its
/// directory entry. A handle opened to delete on close marks the file when
/// it closes, and the file stays, marked, while another handle is open.
/// Every open of a marked file fails with ERROR_ACCESS_DENIED, whatever the
/// caller's rights: one of the codes that send the non-strict `realpath` to
/// the directory-entry lookup. An exclusive open does not fail the call
/// (with no access requested the share mode is never checked, CI run
/// 45260976205), and neither did denying SYNCHRONIZE on the hosted runner
/// (CI run 45284088119). Dropping the returned handle removes the file.
fn pending_delete(path: &Path) -> fs::File {
    use std::os::windows::fs::OpenOptionsExt;
    const DELETE: u32 = 0x0001_0000;
    const DELETE_ON_CLOSE: u32 = 0x0400_0000;
    let keep = fs::OpenOptions::new()
        .read(true)
        .share_mode(7)
        .open(path)
        .unwrap();
    let marker = fs::OpenOptions::new()
        .access_mode(DELETE)
        .share_mode(7)
        .custom_flags(DELETE_ON_CLOSE)
        .open(path)
        .unwrap();
    drop(marker);
    keep
}

#[test]
fn denied_key_state_path_matches_windows_python() {
    let tmp = scratch("guard-fallback");
    let work = tmp.0.join("fixture");
    fs::create_dir(&work).unwrap();
    let stored = work.join("Mixed-É.name");
    let absent = work.join("absent");
    fs::write(&stored, b"data").unwrap();
    let keep = pending_delete(&stored);
    let wide = |p: &Path| p.as_os_str().encode_wide().collect::<Vec<_>>();
    let observed = bettermemory_sys::final_path_name(&wide(&stored))
        .unwrap_err()
        .raw_os_error();
    assert_eq!(observed, Some(5));
    let name = wide(Path::new("Mixed-É.name"));
    let id = std::process::id().to_string();
    let home = tmp.0.join("home");
    let python = created::reference_python();
    let observe = |python: &Path| -> Value {
        let o = created::run_reference(
            python,
            &home,
            REFERENCE,
            &[&stored, &absent, Path::new(&id)],
        );
        assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
        serde_json::from_slice(&o.stdout).unwrap()
    };
    let before = python.as_ref().ok().map(|p| observe(p));
    let ring = KeyFiles::new(&work, &PyValue::str("1234567890abcdef1234567890abcdef")).unwrap();
    let peer = work.join("other");
    for (path, wanted) in [
        (&stored, Level::Warning),
        (&work.join("child/../Mixed-É.name"), Level::Debug),
    ] {
        warn_bound_elsewhere(&ring, path, &cps(ring.store_id()), &peer, None, |w| {
            assert_eq!(w.level, wanted);
            Ok::<_, ()>(())
        })
        .unwrap();
    }
    let present_path = wide(&stored);
    let absent_path = wide(&absent);
    // A FindFirstFile lookup returns the stored component, even when supplied a pattern.
    assert_eq!(
        bettermemory_sys::find_first_name(&wide(&work.join("Mixed-*.name"))).unwrap(),
        name
    );
    for _ in 0..256 {
        assert_eq!(
            bettermemory_sys::find_first_name(&present_path).unwrap(),
            name
        );
        assert_eq!(
            bettermemory_sys::find_first_name(&absent_path)
                .unwrap_err()
                .raw_os_error(),
            Some(2)
        );
    }
    if let (Ok(python), Some(before)) = (&python, before) {
        let after = observe(python);
        for result in [&before, &after] {
            assert_eq!(result[0], 5);
            assert_eq!(result[1], 2);
            assert_eq!(result[2], serde_json::json!(name));
        }
        assert!(
            after[3].as_u64().unwrap() <= before[3].as_u64().unwrap() + 4,
            "search handles grew: {before}, {after}"
        );
    }
    drop(keep);
    assert!(fs::symlink_metadata(&stored).is_err());
    fs::remove_dir(work).unwrap();
    match python {
        Ok(_) => parity::record(NAME, "reference Python", "i", 4, &[], &[]),
        Err(reason) => parity::record(
            NAME,
            "reference Python",
            "i",
            0,
            &["Windows reference".into()],
            &[reason],
        ),
    }
}
