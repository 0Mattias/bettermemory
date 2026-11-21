//! Cases a plausible wrong `restore_lock.admit` fails: blank by
//! `u8::is_ascii_whitespace` (no VT), a marker read as UTF-8 only, an
//! unreadable marker taken for none, the lock kept past an unfinished
//! refusal, a shared hold let go early, a `pid` that is a bool printed,
//! the whole marker read past 64 KiB, and the writing open refused with
//! the reader's `RestoreRunningError` instead of the running text. POSIX
//! only, as the fixture.

#![cfg(unix)]

mod common;

use std::fs::{self, File};
use std::path::PathBuf;
use std::time::Duration;

use bettermemory_core::store::open::open_for_writing;
use bettermemory_core::store::restore_lock::{
    admit, admit_in_record_mode, lock_path, record_file_taken, restore_state, Admission,
    AdmitError, State, Try,
};
use common::opened;
use common::stores::{scratch, Scratch};

const WAIT: Duration = Duration::from_millis(100);

fn store_with(marker: Option<&[u8]>) -> (Scratch, PathBuf) {
    let root = scratch("admit-kills");
    let store = root.0.join("memory.sqlite");
    fs::write(&store, b"").expect("store");
    if let Some(m) = marker {
        fs::write(lock_path(&store), m).expect("marker");
    }
    (root, store)
}

fn refused_text(got: Result<Admission, AdmitError>) -> String {
    match got {
        Err(AdmitError::Locked(l)) => l.to_string(),
        Err(AdmitError::Recursion) => "RecursionError".to_owned(),
        Ok(_) => "admitted".to_owned(),
    }
}

#[test]
fn vertical_tab_is_blank() {
    let (_root, store) = store_with(Some(b"\x0b\x0c \t\r\n"));
    assert!(matches!(
        admit(&store, WAIT, |_| false),
        Ok(Admission::Shared(_))
    ));
}

#[test]
fn a_utf16_marker_is_read() {
    let mut bytes = vec![0xff, 0xfe];
    for u in "{\"stage\": \"swap\"}".encode_utf16() {
        bytes.extend_from_slice(&u.to_le_bytes());
    }
    let (_root, store) = store_with(Some(&bytes));
    let text = refused_text(admit(&store, WAIT, |_| false));
    assert!(text.contains("(stopped at swap;"), "{text}");
}

#[test]
fn an_unreadable_marker_refuses() {
    let (_root, store) = store_with(None);
    fs::create_dir(lock_path(&store)).expect("dir");
    match admit(&store, WAIT, |_| false) {
        Err(AdmitError::Locked(l)) => {
            assert!(!l.running);
            let m = l.marker.as_ref().expect("marker");
            assert_eq!(m.len(), 1);
            assert!(l.to_string().contains("(no record of it reads;"));
        }
        other => panic!("{other:?}"),
    }
}

#[test]
fn the_lock_is_given_back_before_an_unfinished_refusal() {
    let (root, store) = store_with(Some(b"{}"));
    let got = admit(&store, WAIT, |_| false);
    // Held until the refusal drops, then free.
    let helper = File::open(&root.0).expect("dir");
    assert!(helper.try_lock().is_ok(), "the lock was kept");
    drop(got);
}

#[test]
fn a_shared_hold_keeps_a_restore_off_until_it_drops() {
    let (root, store) = store_with(None);
    let hold = admit(&store, WAIT, |_| false).expect("admitted");
    let helper = File::open(&root.0).expect("dir");
    assert!(matches!(
        helper.try_lock(),
        Err(fs::TryLockError::WouldBlock)
    ));
    hold.confirm().expect("confirm");
    drop(hold);
    assert!(helper.try_lock().is_ok());
}

#[test]
fn a_bool_pid_is_left_out() {
    let (_root, store) = store_with(Some(b"{\"pid\": true, \"stage\": \"x\"}"));
    let text = refused_text(admit(&store, WAIT, |_| false));
    assert!(text.contains("(stopped at x;"), "{text}");
}

#[test]
fn only_the_first_64_kib_is_read() {
    let mut bytes = vec![b' '; 1 << 16];
    bytes.extend_from_slice(b"{\"pid\": 1}");
    let (_root, store) = store_with(Some(&bytes));
    assert!(matches!(
        admit(&store, WAIT, |_| false),
        Ok(Admission::Shared(_))
    ));
    assert_eq!(
        restore_state(&store, WAIT, |_| false).expect("state").0,
        State::Clear
    );
}

#[test]
fn a_held_lock_with_a_marker_is_running_not_unfinished() {
    let (root, store) = store_with(Some(b"{\"stage\": \"x\"}"));
    let helper = File::open(&root.0).expect("dir");
    helper.try_lock().expect("helper");
    let text = refused_text(admit(&store, WAIT, |_| false));
    assert!(
        text.contains("a restore of it is running (its record is "),
        "{text}"
    );
    let (state, marker) = restore_state(&store, WAIT, |_| false).expect("state");
    assert_eq!(state, State::Running);
    assert!(marker.is_some());
}

#[test]
fn record_mode_refuses_a_swapped_store_file() {
    let (root, store) = store_with(None);
    let hold = admit_in_record_mode(&store, WAIT, |_| false).expect("admitted");
    assert!(matches!(hold, Admission::Record(_)));
    let fresh = root.0.join("fresh");
    fs::write(&fresh, b"").expect("fresh");
    fs::rename(&fresh, &store).expect("swap");
    match hold.confirm() {
        Err(AdmitError::Locked(l)) => {
            assert!(l.replaced && !l.running);
            assert!(l.to_string().contains("a restore replaced the store file"));
        }
        other => panic!("{other:?}"),
    }
}

#[test]
fn mine_admits_with_nothing_held() {
    let (root, store) = store_with(Some(b"{}"));
    let helper = File::open(&root.0).expect("dir");
    helper.try_lock().expect("helper");
    assert!(matches!(admit(&store, WAIT, |_| true), Ok(Admission::None)));
}

#[test]
fn a_writing_open_during_a_restore_is_restore_locked() {
    let dir = scratch("admit-open");
    let c = dir.0.join("c");
    opened::made("created", &c);
    let file = c.join("memory.sqlite");
    let helper = File::open(&c).expect("dir");
    helper.try_lock().expect("helper");
    let provider = opened::engine_provider();
    let err = open_for_writing(&file, &c.join("keys"), &provider).expect_err("refused");
    assert_eq!(err.class(), "RestoreLockedError");
    assert_eq!(
        err.to_string(),
        format!(
            "{} is not opened: a restore of it is running (its record is {}); open it \
             again when the restore has finished",
            file.display(),
            lock_path(&file).display()
        )
    );
}

/// A record file whose lock the file system refuses counts as taken, as
/// the reference's `_take` sets `taken` on `_Unlockable`; no local file
/// system refuses flock, so the rule is tested directly.
#[test]
fn unlockable_record_file_counts_as_taken() {
    assert!(record_file_taken(Try::Unlockable));
    assert!(record_file_taken(Try::Taken));
    assert!(!record_file_taken(Try::Held));
}
