//! File spellings, lazy origin verification and real logger cutoffs.
mod common;
mod key_read_support;
use bettermemory_core::{
    fsutil::fsdecode,
    log::fingerprint,
    store::{
        key_read::{key_state_on, ring_fit, LogLevel, ReadError},
        StoreFile,
    },
    unicode::{cps, to_string},
};
use common::stores::scratch;
use key_read_support::Signed;
use std::fs;

#[cfg(unix)]
#[test]
fn physical_filename_failures_match_reference() {
    use bettermemory_core::{
        pyvalue::PyValue,
        store::key_read::{FilePathError, ReaderRing},
    };
    use {
        common::parity::Ledger, key_read_support::selects, serde_json::json,
        std::sync::atomic::Ordering,
    };
    let root = scratch("reader-unusable-key-location");
    let owner = Signed::new(&root.0);
    let mut conn = rusqlite::Connection::open_with_flags(
        &owner.path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .unwrap();
    let count = selects(&mut conn);
    let name = "store_key_read_paths_posix.jsonl";
    let rows = common::jsonl(name);
    let mut ledger = Ledger::new("physical_filename_failures_match_reference", name);
    for (i, row) in rows.iter().enumerate() {
        ledger.case(i, || {
            count.store(0, Ordering::Relaxed);
            let directory: Vec<u32> = serde_json::from_value(row["directory"].clone()).unwrap();
            let id = owner.ring.store_id();
            let ring = ReaderRing::new(&directory, &PyValue::str(id)).unwrap();
            let actual = if row["op"] == "fit" {
                let fit = ring.ring_fit(&conn, None).unwrap();
                json!([
                    fit.origin.status,
                    fit.head_status.name(),
                    fit.log_end.unwrap().seq
                ])
            } else {
                let error = ring
                    .key_state_at(
                        &conn,
                        &owner.path,
                        &fsdecode(owner.path.as_os_str()),
                        &cps(&owner.named),
                        true,
                        |_| -> Result<(), ()> { panic!("unexpected log") },
                    )
                    .unwrap_err();
                let ReadError::Path(error) = error else {
                    panic!("unexpected error {error:?}")
                };
                let message = to_string(&error.message()).unwrap();
                assert_eq!(
                    error.codec(),
                    if matches!(error, FilePathError::Nul) {
                        None
                    } else {
                        Some(("utf-8", "surrogates not allowed"))
                    }
                );
                match &error {
                    FilePathError::Nul => json!([error.class(), message]),
                    FilePathError::Encoding { object, start, end } => {
                        let index = object.windows(id.len()).position(|q| q == cps(id)).unwrap();
                        let mut stable = object.clone();
                        stable.splice(index..index + id.len(), cps("ID"));
                        json!([error.class(), message, stable, start, end])
                    }
                }
            };
            assert_eq!(actual, row["result"]);
            assert_eq!(
                count.load(Ordering::Relaxed) as u64,
                row["sql"].as_u64().unwrap()
            );
        });
    }
    ledger.finish(rows.len());
}

#[test]
fn read_fault_preserves_the_pending_file_and_the_callback_error() {
    let root = scratch("reader-pending-file");
    let owner = Signed::new(&root.0);
    let conn = StoreFile::open(&owner.path).unwrap();
    let pending = owner.ring.current_path().with_extension("key.pending");
    fs::create_dir(&pending).unwrap();
    conn.connection().execute_batch("BEGIN").unwrap();
    let marker = std::sync::Arc::new("caller handler");
    let error = key_state_on(
        conn.connection(),
        &owner.ring,
        &owner.path,
        &cps(&owner.named),
        false,
        |log| {
            assert_eq!(log.level, LogLevel::Warning);
            assert_eq!(
                log.format,
                "store %s: the pending key %s cannot be read: %s"
            );
            let e = log.cause.unwrap();
            assert_eq!(
                e.filename.as_deref(),
                Some(fsdecode(pending.as_os_str()).as_slice())
            );
            #[cfg(unix)]
            {
                assert_eq!(e.class, "IsADirectoryError");
                assert_eq!(e.errno, Some(21));
            }
            #[cfg(windows)]
            {
                assert_eq!(e.class, "PermissionError");
                assert_eq!(e.errno, Some(13));
            }
            Err(marker.clone())
        },
    )
    .unwrap_err();
    assert!(matches!(error,ReadError::Logger(e) if std::sync::Arc::ptr_eq(&marker,&e)));
    assert!(pending.is_dir());
    assert!(!conn.connection().is_autocommit());
    assert_eq!(
        fingerprint(owner.ring.load_current().unwrap().unwrap().bytes()),
        owner.named
    );
}

#[cfg(unix)]
#[test]
fn raw_dangling_key_target_keeps_every_codepoint() {
    use std::os::unix::{ffi::OsStrExt, fs::symlink};
    let root = scratch("reader-dangling-key");
    let owner = Signed::new(&root.0);
    fs::remove_file(owner.ring.current_path()).unwrap();
    let target = std::ffi::OsStr::from_bytes(b"parked-\xff-key");
    symlink(target, owner.ring.current_path()).unwrap();
    let conn = StoreFile::open(&owner.path).unwrap();
    let mut messages = Vec::new();
    let got = key_state_on(
        conn.connection(),
        &owner.ring,
        &owner.path,
        &cps(&owner.named),
        true,
        |line| {
            messages.push(line.message());
            Ok::<_, ()>(())
        },
    )
    .unwrap();
    assert_eq!(got.state.state.name(), "unreadable");
    let reason = &got.state.error.unwrap().reason;
    let expected = [
        cps("it is a symbolic link to "),
        fsdecode(target),
        cps(", which is not there"),
    ]
    .concat();
    assert_eq!(reason, &expected);
    assert!(messages[0].windows(expected.len()).any(|v| v == expected));
}

#[test]
fn current_and_retired_keys_are_read_only_when_needed() {
    let root = scratch("reader-lazy-origin");
    let owner = Signed::new(&root.0);
    let store = StoreFile::open(&owner.path).unwrap();
    fs::remove_file(owner.ring.current_path()).unwrap();
    for n in 0..160 {
        fs::write(
            owner
                .ring
                .keys_dir()
                .join(format!("{}.x{n:03}.key", owner.ring.store_id())),
            format!("decoy{n}"),
        )
        .unwrap();
    }
    let old = ring_fit(store.connection(), &owner.ring, Some(&owner.current)).unwrap();
    assert_eq!(old.origin.signer.as_deref(), Some(owner.named.as_str()));
    assert_eq!(
        ring_fit(store.connection(), &owner.ring, None)
            .unwrap()
            .origin
            .status,
        "unverifiable"
    );
    let retired = owner
        .ring
        .keys_dir()
        .join(format!("{}..key", owner.ring.store_id()));
    fs::write(&retired, owner.current.bytes()).unwrap();
    assert_eq!(
        ring_fit(store.connection(), &owner.ring, None)
            .unwrap()
            .origin
            .signer
            .as_deref(),
        Some(owner.named.as_str())
    );
    fs::rename(&retired, retired.with_extension("txt")).unwrap();
    assert_eq!(
        ring_fit(store.connection(), &owner.ring, None)
            .unwrap()
            .origin
            .status,
        "unverifiable"
    );
}

#[cfg(windows)]
#[test]
fn operating_system_and_crt_read_errors_match_windows_python() {
    use common::{created, parity};
    use serde_json::{json, Value};
    use std::{os::windows::fs::OpenOptionsExt, path::Path};
    const NAME: &str = "operating_system_and_crt_read_errors_match_windows_python";
    let root = scratch("reader-windows-io");
    let owner = Signed::new(&root.0.join("owner"));
    let reader = StoreFile::open(&owner.path).unwrap();
    let pending = owner.ring.current_path().with_extension("key.pending");
    let base = &owner.ring;
    let locked = fs::OpenOptions::new()
        .read(true)
        .share_mode(0)
        .open(base.current_path())
        .unwrap();
    fs::create_dir(&pending).unwrap();
    let mut actual = Vec::new();
    let snapshot = key_state_on(
        reader.connection(),
        base,
        &owner.path,
        &cps(&owner.named),
        true,
        |log| {
            let e = log.cause.unwrap();
            let show = |v: Option<Vec<u32>>| v.map(|s| to_string(&s).unwrap());
            actual.push(json!([
                e.class,
                to_string(&e.message).unwrap(),
                e.errno,
                e.winerror,
                show(e.filename.clone()),
                show(e.filename2.clone()),
                show(e.path.clone()),
                show(e.reason.clone())
            ]));
            Ok::<_, ()>(())
        },
    )
    .unwrap();
    assert_eq!(snapshot.state.state.name(), "unreadable");
    assert_eq!(actual.len(), 2);
    let script = r#"
from pathlib import Path
from bettermemory.log import KeyRing
from bettermemory import store_keys
import json,sqlite3,sys,logging
path,keys=map(Path,sys.argv[1:3]); ring=KeyRing(keys,sys.argv[3])
class Capture(logging.Handler):
    def emit(self,r):
        e=r.args[-1]
        observed.append([type(e).__name__,str(e),e.errno,getattr(e,'winerror',None),e.filename,e.filename2,str(e.path) if hasattr(e,'path') else None,getattr(e,'reason',None)])
observed=[]
store_keys.log.addHandler(Capture())
conn=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
try:
    st=store_keys.key_state(ring,path,sys.argv[4],conn=conn)
    assert st.state=='unreadable'
finally:
    conn.close()
print(json.dumps(observed))
"#;
    match created::reference_python() {
        Ok(python) => {
            let result = created::run_reference(
                &python,
                &root.0.join("home"),
                script,
                &[
                    &owner.path,
                    base.keys_dir(),
                    Path::new(base.store_id()),
                    Path::new(&owner.named),
                ],
            );
            assert!(
                result.status.success(),
                "{}",
                String::from_utf8_lossy(&result.stderr)
            );
            let observed: Value = serde_json::from_slice(&result.stdout).unwrap();
            assert_eq!(json!(actual), observed);
            parity::record(NAME, "reference Python", "i", 2, &[], &[]);
        }
        Err(why) => parity::record(
            NAME,
            "reference Python",
            "i",
            0,
            &["Windows physical calls".to_owned()],
            &[why],
        ),
    }
    drop(locked);
    fs::remove_dir(pending).unwrap();
    assert_eq!(
        base.load_current().unwrap().unwrap().bytes(),
        owner.current.bytes()
    );
}

#[cfg(windows)]
#[test]
fn a_committed_rotation_reports_the_original_windows_rename_error() {
    use common::{created, parity};
    use serde_json::{json, Value};
    use std::{os::windows::fs::OpenOptionsExt, path::Path};
    const NAME: &str = "a_committed_rotation_reports_the_original_windows_rename_error";
    let python = match created::reference_python() {
        Ok(p) => p,
        Err(why) => {
            parity::record(
                NAME,
                "reference Python",
                "i",
                0,
                &["signed Windows rotation".to_owned()],
                &[why],
            );
            return;
        }
    };
    let root = scratch("reader-windows-rotation");
    let owner = Signed::new(&root.0.join("owner"));
    // Existing readers can read the current key. Windows withholds delete permission
    // while this ordinary file handle is open, so the committed rename stays pending.
    let file = fs::OpenOptions::new()
        .read(true)
        .share_mode(1)
        .open(owner.ring.current_path())
        .unwrap();
    let script = r#"
from pathlib import Path
from bettermemory.log import KeyRing
from bettermemory import store_keys
from bettermemory.key_lifecycle import rotate_key
import json,sys,logging,sqlite3
path,keys,tracker=map(Path,sys.argv[1:4])
result=rotate_key(path,keys_dir=keys,state_dir=tracker,apply=True)
assert result["status"]=="done", result.get("status")
ring=KeyRing(keys,sys.argv[4]); expected=result["new_fingerprint"]
assert store_keys.pending_key_path(ring).is_file()
observed=[]
class Capture(logging.Handler):
    def emit(self,r):
        e=r.args[-1]
        observed.append([r.levelname,r.msg,type(e).__name__,str(e),e.errno,e.winerror,e.filename,e.filename2])
store_keys.log.addHandler(Capture())
conn=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
try:
    state=store_keys.key_state(ring,path,expected,conn=conn,warn=False)
finally:
    conn.close()
print(json.dumps([expected,observed,state.state]))
"#;
    let status = created::run_reference(
        &python,
        &root.0.join("home"),
        script,
        &[
            &owner.path,
            owner.ring.keys_dir(),
            &root.0.join("tracker"),
            Path::new(owner.ring.store_id()),
        ],
    );
    assert!(
        status.status.success(),
        "{}",
        String::from_utf8_lossy(&status.stderr)
    );
    let reference: Value = serde_json::from_slice(&status.stdout).unwrap();
    let named = cps(reference[0].as_str().unwrap());
    let store = StoreFile::open(&owner.path).unwrap();
    let mut logs = Vec::new();
    let failed = key_state_on(
        store.connection(),
        &owner.ring,
        &owner.path,
        &named,
        false,
        |notice| {
            assert_eq!(notice.level, LogLevel::Warning);
            let e = notice.cause.unwrap();
            let show = |s: Option<Vec<u32>>| s.map(|t| to_string(&t).unwrap());
            logs.push(json!([
                "WARNING",
                notice.format,
                e.class,
                to_string(&e.message).unwrap(),
                e.errno,
                e.winerror,
                show(e.filename.clone()),
                show(e.filename2.clone())
            ]));
            Ok::<_, ()>(())
        },
    )
    .unwrap();
    assert_eq!(logs.len(), 1);
    assert_eq!(json!(logs), reference[1]);
    assert_eq!(failed.state.state.name(), reference[2]);
    drop(file);
    let completed = key_state_on(
        store.connection(),
        &owner.ring,
        &owner.path,
        &named,
        false,
        |entry| {
            assert_eq!(entry.level, LogLevel::Info);
            Ok::<_, ()>(())
        },
    )
    .unwrap();
    assert_eq!(completed.state.state.name(), "present");
    parity::record(NAME, "reference Python", "i", 2, &[], &[]);
}

#[cfg(unix)]
#[test]
fn physical_copy_warnings_distinguish_both_original_path_spellings() {
    use bettermemory_core::{
        json::{self, DumpOptions},
        log::ring::binding_path::BindingPath,
        pyvalue::PyValue,
        store::key_read::ReaderRing,
    };
    let root = scratch("reader-spelled-copies");
    let owner = Signed::new(&root.0.join("owner-é"));
    let ring = ReaderRing::from_files(&owner.ring);
    let source = owner.ring.read_binding_exact().unwrap().unwrap();
    let escaped = |input: &[u32]| {
        let mut out = Vec::new();
        for &c in input {
            if c == u32::from('é') {
                out.extend([0xdcc3, 0xdca9]);
            } else {
                out.push(c);
            }
        }
        assert_ne!(out, input);
        out
    };
    let escaped_source = escaped(source.text());
    for varied_target in [true, false] {
        let target = root.0.join(if varied_target {
            "target-a-é.sqlite"
        } else {
            "target-b-é.sqlite"
        });
        fs::copy(&owner.path, &target).unwrap();
        let store = StoreFile::open(&target).unwrap();
        let scalar = fsdecode(target.as_os_str());
        let raw = escaped(&scalar);
        for (alias, level) in [
            (false, LogLevel::Warning),
            (true, LogLevel::Warning),
            (false, LogLevel::Debug),
            (true, LogLevel::Debug),
        ] {
            let current = if varied_target && alias {
                &raw
            } else {
                &scalar
            };
            let bound = if !varied_target && alias {
                escaped_source.as_slice()
            } else {
                source.text()
            };
            let document =
                PyValue::Dict(vec![(PyValue::str("path"), PyValue::Str(bound.to_vec()))]);
            let bytes = json::utf8_text(&json::dumps(&document, DumpOptions::DEFAULT).unwrap());
            fs::write(owner.ring.binding_path(), bytes.as_bytes()).unwrap();
            let before = fs::read(owner.ring.binding_path()).unwrap();
            let mut events = 0;
            let read = ring
                .key_state_at(
                    store.connection(),
                    &target,
                    current,
                    &cps(&owner.named),
                    true,
                    |entry| {
                        events += 1;
                        assert_eq!(entry.level, level);
                        assert_eq!(&entry.args[0], current);
                        assert!(entry.args[1]
                            .windows(bound.len())
                            .any(|slice| slice == bound));
                        Ok::<_, ()>(())
                    },
                )
                .unwrap();
            assert_eq!(events, 1);
            assert_eq!(read.state.state.name(), "bound_elsewhere");
            assert_eq!(read.binding.as_ref().map(BindingPath::text), Some(bound));
            assert!(read
                .view()
                .summary()
                .unwrap()
                .windows(bound.len())
                .any(|slice| slice == bound));
            assert_eq!(fs::read(owner.ring.binding_path()).unwrap(), before);
        }
    }
}

#[test]
fn unreadable_state_and_callback_share_the_exception_and_its_os_cause() {
    let root = scratch("reader-error-identity");
    let owner = Signed::new(&root.0.join("owner"));
    let ring = owner.copy_ring(&root.0.join("test-ring"));
    fs::remove_file(ring.current_path()).unwrap();
    fs::create_dir(ring.current_path()).unwrap();
    let store = StoreFile::open(&owner.path).unwrap();
    let mut warning = None;
    let read = key_state_on(
        store.connection(),
        &ring,
        &owner.path,
        &cps(&owner.named),
        true,
        |record| {
            warning = record.cause;
            Ok::<_, ()>(())
        },
    )
    .unwrap();
    let error = read.error.unwrap();
    assert!(std::sync::Arc::ptr_eq(&error, &warning.unwrap()));
    assert!(error.filename.is_none());
    let filename = fsdecode(ring.current_path().as_os_str());
    assert_eq!(error.path.as_deref(), Some(filename.as_slice()));
    let original = error.source.as_ref().unwrap();
    assert_eq!(original.filename.as_deref(), Some(filename.as_slice()));
    assert_eq!(original.errno, error.errno);
    assert!(original
        .os_error
        .as_ref()
        .is_some_and(|e| e.raw_os_error().is_some()));
    assert!(original.source.is_none());
    #[cfg(unix)]
    assert_eq!(original.class, "IsADirectoryError");
    #[cfg(windows)]
    assert_eq!(original.class, "PermissionError");
}
