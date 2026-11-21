//! Cases a plausible wrong port of the restore's hold fails: a registry
//! that ignores the thread, a release that keeps the registry entry, a
//! mark that reads the clock again or starts from the previous marker, a
//! probe that keeps rusqlite's five-second busy wait, and a writing open
//! that still passes a `mine` naming nothing. POSIX only (the lock is a
//! directory `flock`); `restore_lock_hold_not_posix.rs` records the gap.

#![cfg(unix)]

mod common;
mod timing;

use std::fs;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::restore_lock::{
    acquire, acquire_in_record_mode, admit, held_elsewhere, held_here, lock_path, probe,
    read_marker, restore_state, this_thread_holds, unlock, Admission, AdmitError, HoldError, State,
    UnlockStatus,
};
use common::stores::scratch;

const WAIT: Duration = Duration::from_millis(200);
/// Longer than a nested acquire may take.
const LONG: Duration = Duration::from_secs(10);

fn store() -> (common::stores::Scratch, PathBuf) {
    let root = scratch("hold-kills");
    let store = root.0.join("memory.sqlite");
    fs::write(&store, b"").expect("store");
    (root, store)
}

fn at(seconds: i64) -> DateTime {
    DateTime::from_unix_micros_utc(seconds * 1_000_000).expect("time")
}

/// `_this_thread_holds` names the thread: another thread of the process
/// is refused as any other opener is, the holding thread is admitted.
#[test]
fn only_the_holding_thread_is_admitted() {
    let (_root, store) = store();
    let held = acquire(&store, WAIT).expect("acquire").expect("held");
    assert!(matches!(
        admit(&store, WAIT, this_thread_holds),
        Ok(Admission::None)
    ));
    assert_eq!(
        restore_state(&store, WAIT, this_thread_holds)
            .expect("state")
            .0,
        State::Clear
    );
    let other = std::thread::scope(|t| {
        t.spawn(|| match admit(&store, WAIT, this_thread_holds) {
            Err(AdmitError::Locked(l)) => l.running,
            _ => false,
        })
        .join()
        .expect("thread")
    });
    assert!(other, "another thread meets a running restore");
    held.release(true);
    assert!(!held_here(&store).expect("held_here"));
    assert!(matches!(
        admit(&store, WAIT, this_thread_holds),
        Ok(Admission::Shared(_))
    ));
}

/// A hold dropped without release (before its first mark, so the marker
/// is empty) stays held, as the reference's descriptors and `_HELD`
/// entry outlive its object: another thread's writing open is refused,
/// and a second acquire answers None at once.
#[test]
fn a_dropped_hold_stays_held() {
    let (_root, store) = store();
    let held = acquire(&store, WAIT).expect("acquire").expect("held");
    drop(held);
    assert!(held_here(&store).expect("held_here"));
    let other = std::thread::scope(|t| {
        t.spawn(|| match admit(&store, WAIT, this_thread_holds) {
            Err(AdmitError::Locked(l)) => l.running,
            _ => false,
        })
        .join()
        .expect("thread")
    });
    assert!(other, "a dropped hold still refuses writers");
    let started = Instant::now();
    assert!(acquire(&store, WAIT).expect("acquire").is_none());
    assert!(started.elapsed() < WAIT);
}

/// A second acquire in the process answers None at once (`key in
/// _HELD`), not after waiting on its own lock for the whole `wait`.
#[test]
fn a_nested_acquire_answers_at_once() {
    let (_root, store) = store();
    let held = acquire(&store, LONG).expect("acquire").expect("held");
    let started = Instant::now();
    assert!(acquire(&store, LONG).expect("acquire").is_none());
    let other = std::thread::scope(|t| {
        t.spawn(|| acquire(&store, LONG).expect("acquire").is_none())
            .join()
            .expect("thread")
    });
    assert!(other);
    assert!(
        started.elapsed() < Duration::from_secs(2),
        "{:?}",
        started.elapsed()
    );
    held.release(true);
}

/// A shorter mark after a longer one leaves no tail of the old bytes.
#[test]
fn a_shorter_mark_replaces_a_longer_one_whole() {
    let (_root, store) = store();
    let mut held = acquire(&store, WAIT).expect("acquire").expect("held");
    held.mark(&[("bundle", PyValue::str(&"x".repeat(100)))], &at(0))
        .expect("mark");
    held.mark(&[("bundle", PyValue::str("y"))], &at(0))
        .expect("mark");
    let text = fs::read_to_string(lock_path(&store)).expect("marker");
    assert!(text.ends_with("}\n"), "{text}");
    assert!(!text.contains('x'), "{text}");
    held.release(false);
    let marker = read_marker(&store).expect("read").expect("marker");
    let bundle = marker
        .iter()
        .find(|(k, _)| *k == PyValue::str("bundle"))
        .map(|(_, v)| v.clone());
    assert_eq!(bundle, Some(PyValue::str("y")));
}

/// `release(clear=True)` empties the marker before it removes the name,
/// so another link to the file reads empty.
#[test]
fn a_cleared_release_empties_the_marker_first() {
    let (root, store) = store();
    let mut held = acquire(&store, WAIT).expect("acquire").expect("held");
    held.mark(&[("stage", PyValue::str("swap"))], &at(0))
        .expect("mark");
    let other = root.0.join("other-link");
    fs::hard_link(lock_path(&store), &other).expect("link");
    held.release(true);
    assert!(!lock_path(&store).exists());
    assert_eq!(fs::read(&other).expect("other").len(), 0);
}

/// In record mode a missing directory is the OS's own FileNotFoundError
/// out of opening the lock path, not a synthetic one.
#[test]
fn a_record_mode_acquire_in_a_missing_directory_is_the_os_error() {
    let (root, _store) = store();
    let store = root.0.join("missing").join("memory.sqlite");
    match acquire_in_record_mode(&store, WAIT) {
        Err(HoldError::Os(e)) => {
            assert_eq!(e.kind(), std::io::ErrorKind::NotFound);
            assert!(e.raw_os_error().is_some(), "{e}");
        }
        other => panic!("{other:?}"),
    }
}

/// The probe's held-elsewhere codes: SQLITE_BUSY and SQLITE_LOCKED with
/// any extended code over them, nothing else.
#[test]
fn the_held_elsewhere_codes_are_busy_and_locked_by_primary_code() {
    for code in [5, 6, 261, 517, 773, 1029, 262, 518] {
        assert!(held_elsewhere(code), "{code}");
    }
    for code in [0, 1, 7, 8, 10, 11, 14, 26, 266, 1034] {
        assert!(!held_elsewhere(code), "{code}");
    }
}

/// A path byte that is no UTF-8 is spelled as `os.fsdecode` spells it,
/// a lone surrogate, never U+FFFD.
#[test]
fn a_path_byte_that_is_no_utf8_keeps_its_surrogate() {
    use std::os::unix::ffi::OsStrExt;
    let (root, _store) = store();
    let mut name = root.0.as_os_str().as_bytes().to_vec();
    name.extend_from_slice(b"//./\xff.sqlite");
    let store = PathBuf::from(std::ffi::OsStr::from_bytes(&name));
    let report = unlock(&store, false, WAIT).expect("unlock");
    assert_eq!(report.status, UnlockStatus::Clear);
    assert!(report.lock.contains(&0xDCFF));
    assert!(!report.lock.contains(&0xFFFD));
    // `str(Path(...))` drops the repeated separator and the `.` segment.
    assert!(report.lock.windows(3).any(|w| w == [0x2F, 0xDCFF, 0x2E]));
    assert!(!report.lock.windows(2).any(|w| w == [0x2F, 0x2F]));
}

/// Linux takes any bytes in a name: the marker's `store` keeps the
/// surrogate, written `\udcff` under `ensure_ascii`.
#[cfg(target_os = "linux")]
#[test]
fn a_marker_of_a_store_whose_name_is_no_utf8_escapes_the_surrogate() {
    use std::os::unix::ffi::OsStrExt;
    let (root, _store) = store();
    let store = root.0.join(std::ffi::OsStr::from_bytes(b"\xff.sqlite"));
    fs::write(&store, b"").expect("store");
    let mut held = acquire(&store, WAIT).expect("acquire").expect("held");
    held.mark(&[], &at(0)).expect("mark");
    let text = fs::read_to_string(lock_path(&store)).expect("marker");
    assert!(text.contains("\\udcff.sqlite\""), "{text}");
    held.release(true);
}

/// `started_at` is read on the first mark only; later marks update the
/// fields over it, and a new hold does not start from the previous one.
#[test]
fn the_clock_is_read_once_and_fields_update() {
    let (_root, store) = store();
    let mut held = acquire(&store, WAIT).expect("acquire").expect("held");
    held.mark(&[("stage", PyValue::str("a"))], &at(0))
        .expect("mark");
    held.mark(&[("stage", PyValue::str("b"))], &at(86_400 * 400))
        .expect("mark");
    let text = fs::read_to_string(lock_path(&store)).expect("marker");
    assert!(
        text.contains("\"started_at\": \"1970-01-01T00:00:00Z\""),
        "{text}"
    );
    assert!(text.contains("\"stage\": \"b\""), "{text}");
    assert!(text.ends_with("}\n"));
    held.release(false);
    let mut next = acquire(&store, WAIT).expect("acquire").expect("held");
    next.mark(&[("other", PyValue::Int("1".into()))], &at(60))
        .expect("mark");
    let text = fs::read_to_string(lock_path(&store)).expect("marker");
    assert!(!text.contains("stage"), "{text}");
    assert!(text.contains("1970-01-01T00:01:00Z"), "{text}");
    next.release(true);
}

/// A field JSON has no spelling for is TypeError, as `json.dumps` raises.
#[test]
fn a_field_json_cannot_spell_is_refused() {
    let (_root, store) = store();
    let mut held = acquire(&store, WAIT).expect("acquire").expect("held");
    let when = PyValue::DateTime(at(0));
    assert!(held.mark(&[("when", when)], &at(0)).is_err());
    held.release(true);
}

/// `unlock` while this thread holds the lock: held, by a restore.
#[test]
fn unlock_under_its_own_hold_is_held_by_a_restore() {
    let (_root, store) = store();
    let held = acquire(&store, WAIT).expect("acquire").expect("held");
    let report = unlock(&store, true, WAIT).expect("unlock");
    assert_eq!(report.status, UnlockStatus::Held);
    assert_eq!(report.status.exit(), 1);
    assert_eq!(report.holder.map(|h| h.name()), Some("restore"));
    held.release(true);
    let report = unlock(&store, true, WAIT).expect("unlock");
    assert_eq!(report.status, UnlockStatus::Clear);
}

/// `timeout=0`: a held store answers at once, not after a busy wait.
#[test]
fn the_probe_does_not_wait_on_a_held_store() {
    let (_root, store) = store();
    let conn = rusqlite::Connection::open(&store).expect("open");
    conn.execute_batch("PRAGMA journal_mode = WAL; CREATE TABLE t (x);")
        .expect("wal");
    timing::ends_within("probe", Duration::from_secs(2), &mut || {
        timing::wall(|| assert!(probe(&store).is_some()))
    });
    drop(conn);
    assert!(probe(&store).is_none());
}
