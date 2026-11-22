//! Cases a plausible wrong port of `_store_refusal` fails: a manifest
//! that parses to something other than an object (the docstring counts
//! it, the code does not), the last of two `format` keys, the 1 MiB cap
//! on either side, `bytes.strip()` whitespace with 0x0b and without NUL,
//! the 64 KiB marker cap, a `bool` pid, a path with no name or named
//! `..`, the record branch's own lock, and a symbolic link loop.

mod common;
mod store_refusal_case;

use std::fs;
use std::path::Path;

use bettermemory_core::pyvalue::PyError;
use bettermemory_core::store_refusal::{
    may_be_bundle_manifest, restore_refusal, restore_state, restore_state_record_branch, Failure,
    State,
};

fn text(t: &[u32]) -> String {
    t.iter().filter_map(|&c| char::from_u32(c)).collect()
}

fn manifest(dir: &Path, data: &[u8]) -> bool {
    let m = dir.join("manifest.json");
    fs::write(&m, data).unwrap();
    may_be_bundle_manifest(&m)
}

#[test]
fn a_manifest_that_is_no_object_is_not_a_bundle() {
    let d = store_refusal_case::base("k-manifest");
    assert!(!manifest(&d, b"[\"bettermemory.backup\"]"));
    assert!(!manifest(&d, b"null"));
    assert!(manifest(
        &d,
        b"{\"format\": \"x\", \"format\": \"bettermemory.backup\"}"
    ));
    assert!(!manifest(
        &d,
        b"{\"format\": \"bettermemory.backup\", \"format\": \"x\"}"
    ));
    assert!(manifest(&d, b""));
    assert!(manifest(
        &d,
        b"\xef\xbb\xbf{\"format\": \"bettermemory.backup\"}"
    ));
    let mut at_cap = b"{\"format\": \"x\"".to_vec();
    at_cap.resize((1 << 20) - 1, b' ');
    at_cap.push(b'}');
    assert!(!manifest(&d, &at_cap), "exactly 1 MiB is read and parsed");
    at_cap.insert(1, b' ');
    assert!(manifest(&d, &at_cap), "one byte past the cap counts");
    let _ = fs::remove_dir_all(&d);
}

#[test]
fn a_record_of_whitespace_is_clear_and_any_other_byte_is_not() {
    let d = store_refusal_case::base("k-strip");
    let store = d.join("s.db");
    let record = d.join("s.db.restore-lock");
    for (data, state) in [
        (&b"\x0b\x0c \t\r\n"[..], State::Clear),
        (b"\x00", State::Unfinished),
        (b"\x1c", State::Unfinished),
    ] {
        fs::write(&record, data).unwrap();
        assert_eq!(restore_state(&store).unwrap().0, state, "{data:?}");
        assert_eq!(
            restore_state_record_branch(&store).unwrap().0,
            state,
            "{data:?}"
        );
    }
    let mut late = vec![b' '; 1 << 16];
    late.extend_from_slice(b"{}");
    fs::write(&record, &late).unwrap();
    assert_eq!(restore_state(&store).unwrap(), (State::Clear, None));
    let _ = fs::remove_dir_all(&d);
}

#[test]
fn a_bool_pid_is_not_described_and_the_bundle_names_the_command() {
    let d = store_refusal_case::base("k-describe");
    let store = d.join("s.db");
    fs::write(
        d.join("s.db.restore-lock"),
        br#"{"pid": true, "bundle": "b x"}"#,
    )
    .unwrap();
    let got = text(&restore_refusal(&store).unwrap().unwrap());
    assert!(got.contains("did not finish (from b x; "), "{got}");
    assert!(
        got.contains("(`bettermemory backup restore b x --yes`)"),
        "{got}"
    );
    assert!(!got.contains("by process"), "{got}");
    let _ = fs::remove_dir_all(&d);
}

#[test]
fn a_path_with_no_name_is_the_references_value_error() {
    assert!(matches!(
        restore_state(Path::new("/")),
        Err(Failure::Raised(PyError::ValueError))
    ));
    assert!(matches!(
        restore_state(Path::new(".")),
        Err(Failure::Raised(PyError::ValueError))
    ));
    assert!(matches!(
        restore_refusal(Path::new("")),
        Err(Failure::Raised(PyError::ValueError))
    ));
}

#[test]
fn a_store_named_dotdot_keeps_its_name() {
    let d = store_refusal_case::base("k-dotdot");
    fs::create_dir(d.join("e")).unwrap();
    fs::write(d.join("e").join("...restore-lock"), b"{}").unwrap();
    let store = d.join("e").join("..");
    let (state, marker) = restore_state(&store).unwrap();
    assert_eq!(
        (state, marker.map(|m| m.len())),
        (State::Unfinished, Some(0))
    );
    let _ = fs::remove_dir_all(&d);
}

#[cfg(unix)]
#[test]
fn the_record_branch_reads_the_record_files_lock_and_a_loop_ends() {
    let d = store_refusal_case::base("k-unix");
    let store = d.join("s.db");
    let record = d.join("s.db.restore-lock");
    fs::write(&record, b"").unwrap();
    let held = fs::File::open(&record).unwrap();
    held.try_lock().unwrap();
    assert_eq!(
        restore_state_record_branch(&store).unwrap(),
        (State::Running, None)
    );
    assert_eq!(restore_state(&store).unwrap(), (State::Clear, None));
    drop(held);
    assert_eq!(
        restore_state_record_branch(&store).unwrap(),
        (State::Clear, None)
    );
    std::os::unix::fs::symlink(d.join("b"), d.join("a")).unwrap();
    std::os::unix::fs::symlink(d.join("a"), d.join("b")).unwrap();
    assert!(
        bettermemory_core::store_refusal::bundle_refusal(&d.join("a").join("s.db"))
            .unwrap()
            .is_none()
    );
    let _ = fs::remove_dir_all(&d);
}

/// A relative store: its parent is `.`, so the manifest is `manifest.json`
/// and the restore lock is taken on `.`. One test changes the working
/// directory, and every other test here passes absolute paths.
#[test]
fn a_relative_store_and_a_root_dotdot_read_the_right_directory() {
    let d = store_refusal_case::base("k-relative");
    fs::write(
        d.join("manifest.json"),
        br#"{"format": "bettermemory.backup"}"#,
    )
    .unwrap();
    fs::write(d.join("s.db.restore-lock"), b"{}").unwrap();
    let before = std::env::current_dir().unwrap();
    std::env::set_current_dir(&d).unwrap();
    let got = bettermemory_core::store_refusal::bundle_refusal(Path::new("s.db"));
    let state = restore_state(Path::new("s.db"));
    #[cfg(unix)]
    let root = (
        Path::new("/manifest.json").exists(),
        bettermemory_core::store_refusal::bundle_refusal(Path::new("/..")),
    );
    std::env::set_current_dir(before).unwrap();
    let got = text(&got.unwrap().unwrap());
    assert!(
        got.starts_with("s.db is in a backup bundle (manifest.json is beside it)"),
        "{got}"
    );
    assert_eq!(state.unwrap().0, State::Unfinished);
    // `..` at the root stays at `/`: the resolved candidate's manifest is
    // `/manifest.json`, never the working directory's.
    #[cfg(unix)]
    if !root.0 {
        let got = root.1.unwrap().map(|t| text(&t)).unwrap_or_default();
        assert!(!got.contains("is in a backup bundle"), "{got}");
    }
    let _ = fs::remove_dir_all(&d);
}

/// `_record_lock_held` counts only `EWOULDBLOCK`, `EAGAIN` and `EINTR`
/// as held; `ENOLCK` (an NFS mount without lockd) and `EOPNOTSUPP` are
/// not held.
#[cfg(unix)]
#[test]
fn only_wouldblock_again_and_intr_count_as_held() {
    use bettermemory_core::store_refusal::lock_error_held;
    use std::io::Error;
    for errno in [
        libc_errno::EWOULDBLOCK,
        libc_errno::EAGAIN,
        libc_errno::EINTR,
    ] {
        assert!(lock_error_held(&Error::from_raw_os_error(errno)), "{errno}");
    }
    for errno in [
        libc_errno::ENOLCK,
        libc_errno::EOPNOTSUPP,
        libc_errno::EBADF,
    ] {
        assert!(
            !lock_error_held(&Error::from_raw_os_error(errno)),
            "{errno}"
        );
    }
}

#[cfg(unix)]
#[test]
fn a_tried_lock_goes_through_the_errno_class() {
    use bettermemory_core::store_refusal::try_lock_held;
    use std::fs::TryLockError;
    use std::io::Error;
    assert!(!try_lock_held(&Ok(())));
    assert!(try_lock_held(&Err(TryLockError::WouldBlock)));
    assert!(try_lock_held(&Err(TryLockError::Error(
        Error::from_raw_os_error(libc_errno::EINTR)
    ))));
    for errno in [
        libc_errno::ENOLCK,
        libc_errno::EOPNOTSUPP,
        libc_errno::EBADF,
    ] {
        assert!(
            !try_lock_held(&Err(TryLockError::Error(Error::from_raw_os_error(errno)))),
            "{errno}"
        );
    }
}

#[cfg(unix)]
mod libc_errno {
    pub const EBADF: i32 = 9;
    pub const EINTR: i32 = 4;
    #[cfg(target_os = "linux")]
    pub const EAGAIN: i32 = 11;
    #[cfg(not(target_os = "linux"))]
    pub const EAGAIN: i32 = 35;
    pub const EWOULDBLOCK: i32 = EAGAIN;
    #[cfg(target_os = "linux")]
    pub const ENOLCK: i32 = 37;
    #[cfg(not(target_os = "linux"))]
    pub const ENOLCK: i32 = 77;
    #[cfg(target_os = "linux")]
    pub const EOPNOTSUPP: i32 = 95;
    #[cfg(not(target_os = "linux"))]
    pub const EOPNOTSUPP: i32 = 102;
}
