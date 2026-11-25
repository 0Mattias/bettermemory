//! The owner's open settling a restore's rows, held to what a plausible
//! wrong open would get wrong:
//!
//! - killed at any boundary (by `abort` and by `exit`), the store a
//!   further writing open settles is the one an open never killed leaves:
//!   one `store_restored` row, named in the record, the anchor on its row,
//!   the digest current;
//! - the settle runs after the anchor's upkeep (`_settle_origin`), never
//!   before it, on each of the three paths;
//! - the entries that share the provider refuse a pending entry with
//!   nothing written, and settle nothing;
//! - a failure `settle` catches does not fail the open; one it lets
//!   through does, after the upkeep;
//! - the entries that share the provider read the record again at the
//!   settle: an entry written since the decision refuses, a record that
//!   does not read is said;
//! - a restore's warning reaches the open's caller with its own
//!   arguments (`UpkeepWarning::args`).
//!
//! The test binary runs itself as the opening child
//! (`restore_kill_child`, ignored when run alone).

#![cfg(unix)]

mod common;
mod restore_rows;

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use bettermemory_core::store::open::{
    open_for_writing, open_for_writing_mut, open_for_writing_mut_with, open_for_writing_with,
    Boundary, NotPorted, OpenError, UpkeepArg, UpkeepWarning,
};
use bettermemory_core::store::restore_rows::{Arg as RestoreArg, Warning};
use common::opened;
use common::stores::{scratch, store_id};
use serde_json::{json, Value};

const ROOT_ENV: &str = "BM_TEST_RESTORE_OPEN_ROOT";
const AT_ENV: &str = "BM_TEST_RESTORE_OPEN_KILL_AT";
const HOW_ENV: &str = "BM_TEST_RESTORE_OPEN_KILL_HOW";

fn index(b: Boundary) -> usize {
    Boundary::ALL
        .iter()
        .position(|x| *x == b)
        .expect("boundary")
}

fn mac_at(c: &Path, seq: i64) -> String {
    let conn = rusqlite::Connection::open(c.join("memory.sqlite")).expect("open");
    conn.query_row("SELECT mac FROM log WHERE seq = ?", [seq], |r| r.get(0))
        .expect("mac")
}

/// The `intact` base bound to its file, `extra` applied, and a restore
/// record whose one entry put back the head at seq 8 (`gen_store_open_restore`).
fn set_up(root: &Path, extra: &[Value], entry: Value) {
    let c = root.join("c");
    opened::made("intact", &c);
    let mut recipe = extra.to_vec();
    recipe.push(json!({"bind": "memory.sqlite"}));
    opened::setup(&recipe, &c, &store_id());
    let mac8 = mac_at(&c, 8);
    let mut entry = entry;
    if entry["head_after"].is_null() {
        entry["head_after"] = json!({"seq": 8, "mac": mac8});
    }
    let record = c
        .join("keys")
        .join(format!("{}.backup_restore.json", store_id()));
    restore_rows::put_record(&record, &restore_rows::record_of(&[entry]));
}

fn pending() -> Value {
    restore_rows::entry("", json!({"head_after": null}), &[])
}

/// The states: a name and its edits besides the record.
fn states() -> Vec<(&'static str, Vec<Value>)> {
    vec![
        ("present", vec![]),
        ("present_no_anchor", vec![json!({"unlink": ".origin"})]),
        ("rekey", vec![json!({"unlink": ".key"})]),
        (
            "reclaim",
            vec![
                json!({"sql": "UPDATE meta SET value = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' \
                                WHERE key = 'key_fingerprint';"}),
            ],
        ),
    ]
}

/// The opening child: a writing open of `$ROOT/c`, ended at boundary
/// `$AT` by `$HOW`. Writes each boundary passed to `$ROOT/progress`.
#[test]
#[ignore = "run by the kill test as its child"]
fn restore_kill_child() {
    let Some(root) = std::env::var_os(ROOT_ENV).map(PathBuf::from) else {
        return;
    };
    let at: Option<usize> = std::env::var(AT_ENV).ok().and_then(|v| v.parse().ok());
    let how = std::env::var(HOW_ENV).unwrap_or_default();
    let progress = root.join("progress");
    let mut hook = |b: Boundary| {
        let i = index(b);
        let _ = fs::write(&progress, i.to_string());
        if at == Some(i) {
            if how == "abort" {
                std::process::abort();
            }
            std::process::exit(9);
        }
    };
    let c = root.join("c");
    let store = open_for_writing_mut_with(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &mut restore_rows::clock(),
        &mut hook,
    )
    .expect("opened");
    store.close().expect("closed");
    hook(Boundary::Closed);
}

/// A further writing open, and what the store and keys directory hold.
fn settled(root: &Path) -> Value {
    let c = root.join("c");
    let store = open_for_writing_mut(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &mut restore_rows::clock(),
    )
    .unwrap_or_else(|e| panic!("a further writing open: {} {e}", e.class()));
    store.close().expect("close");
    opened::state(&c, root, &store_id(), &[])
}

fn boundaries(root: &Path) -> Vec<usize> {
    let c = root.join("c");
    let mut seen = Vec::new();
    open_for_writing_mut_with(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &mut restore_rows::clock(),
        &mut |b| {
            if !seen.contains(&index(b)) {
                seen.push(index(b));
            }
        },
    )
    .expect("dry open")
    .close()
    .expect("close");
    seen.push(index(Boundary::Closed));
    seen
}

/// How many `store_restored` rows the log holds, and whether the record
/// names a row.
fn restored(root: &Path) -> (i64, bool) {
    let c = root.join("c");
    let conn = rusqlite::Connection::open(c.join("memory.sqlite")).expect("open");
    let rows = conn
        .query_row(
            "SELECT count(*) FROM log WHERE kind = 'store_restored'",
            [],
            |r| r.get(0),
        )
        .expect("count");
    let record = c
        .join("keys")
        .join(format!("{}.backup_restore.json", store_id()));
    let named = !restore_rows::record_entries(&record)[0]["row"].is_null();
    (rows, named)
}

#[test]
fn a_killed_settle_is_settled_by_the_next_open_as_if_never_killed() {
    let dir = scratch("open-restore-kills");
    let mut table: BTreeMap<String, usize> = BTreeMap::new();
    let mut kills = 0;
    for (n, (name, extra)) in states().iter().enumerate() {
        let whole = dir.0.join(format!("whole{n}"));
        fs::create_dir(&whole).expect("whole");
        set_up(&whole, extra, pending());
        let want = settled(&whole);
        assert_eq!(restored(&whole), (1, true), "{name}: never killed");
        let dry = dir.0.join(format!("dry{n}"));
        fs::create_dir(&dry).expect("dry");
        set_up(&dry, extra, pending());
        // Before the anchor's step a missing or foreign key's kills are
        // `store_open_rekey_kills`' (a key drawn twice differs).
        let from = if *name == "present" || *name == "present_no_anchor" {
            0
        } else {
            index(Boundary::OriginTaken)
        };
        let reached: Vec<usize> = boundaries(&dry)
            .into_iter()
            .filter(|i| *i >= from)
            .collect();
        assert!(
            reached.contains(&index(Boundary::RestoreSettled)),
            "{name}: {reached:?}"
        );
        for at in reached {
            for how in ["abort", "exit"] {
                let root = dir.0.join(format!("k{n}-{at}-{how}"));
                fs::create_dir(&root).expect("root");
                set_up(&root, extra, pending());
                let status = Command::new(std::env::current_exe().expect("exe"))
                    .args([
                        "--exact",
                        "restore_kill_child",
                        "--ignored",
                        "--nocapture",
                        "--test-threads=1",
                    ])
                    .env(ROOT_ENV, &root)
                    .env(AT_ENV, at.to_string())
                    .env(HOW_ENV, how)
                    .stdout(std::process::Stdio::null())
                    .stderr(std::process::Stdio::null())
                    .status()
                    .expect("child");
                assert!(!status.success(), "the child was not stopped at {at}");
                let progress: usize = fs::read_to_string(root.join("progress"))
                    .expect("progress")
                    .parse()
                    .expect("index");
                assert_eq!(progress, at, "the child stopped elsewhere");
                let left = restored(&root);
                // Killed before the settle: no row yet; after it: the row
                // appended and named.
                let settled_by_then = at >= index(Boundary::RestoreSettled);
                assert_eq!(
                    left,
                    if settled_by_then {
                        (1, true)
                    } else {
                        (0, false)
                    },
                    "{name} killed after {:?}",
                    Boundary::ALL[at]
                );
                let got = settled(&root);
                let parts = opened::differing(&want, &got);
                assert!(
                    parts.is_empty(),
                    "{name} killed after {:?}, then opened, differs in {parts:?}",
                    Boundary::ALL[at]
                );
                assert_eq!(restored(&root), (1, true));
                *table
                    .entry(format!("{name} {:?}", Boundary::ALL[at]))
                    .or_default() += 1;
                kills += 1;
            }
        }
    }
    for (label, count) in &table {
        eprintln!("{label} x{count}");
    }
    eprintln!("{kills} kills");
    assert!(kills >= 40, "{kills} kills");
}

/// The settle follows the anchor's upkeep on each path: by the time it
/// runs the anchor is on disk, and the boundary before it passed.
#[test]
fn the_settle_follows_the_anchors_upkeep() {
    for (name, extra) in states() {
        let dir = scratch("open-restore-order");
        set_up(&dir.0, &extra, pending());
        let c = dir.0.join("c");
        let origin = c.join("keys").join(format!("{}.origin", store_id()));
        let mut passed = Vec::new();
        let mut anchored_before = None;
        let mut rows_before = None;
        let store = open_for_writing_mut_with(
            &c.join("memory.sqlite"),
            &c.join("keys"),
            &mut restore_rows::clock(),
            &mut |b| {
                if matches!(b, Boundary::OriginSettled | Boundary::OriginTaken) {
                    anchored_before = Some(origin.is_file());
                    let conn = rusqlite::Connection::open(c.join("memory.sqlite")).expect("open");
                    rows_before = Some(
                        conn.query_row(
                            "SELECT count(*) FROM log WHERE kind = 'store_restored'",
                            [],
                            |r| r.get::<_, i64>(0),
                        )
                        .expect("count"),
                    );
                }
                passed.push(b);
            },
        )
        .expect("open");
        let at = |b: Boundary| passed.iter().position(|x| *x == b);
        let upkept = at(Boundary::OriginSettled)
            .or(at(Boundary::OriginTaken))
            .expect("an anchor step");
        assert!(
            upkept < at(Boundary::RestoreSettled).expect("settled"),
            "{name}"
        );
        assert_eq!(
            anchored_before,
            Some(true),
            "{name}: no anchor before the settle"
        );
        assert_eq!(rows_before, Some(0), "{name}: a row before the anchor");
        // The restore's warning is the last the upkeep logged.
        assert!(
            matches!(
                store.upkeep().last(),
                Some(UpkeepWarning::Restore(Warning::Named { seq, .. })) if *seq > 8
            ),
            "{name}: {:?}",
            store.upkeep()
        );
        store.close().expect("close");
    }
}

/// The entries that share the provider (no log clock) refuse a pending
/// entry with nothing written, and settle nothing.
#[test]
fn a_shared_entry_refuses_a_pending_entry_and_writes_nothing() {
    let dir = scratch("open-restore-shared");
    set_up(&dir.0, &[], pending());
    let c = dir.0.join("c");
    let before = opened::state(&c, &dir.0, &store_id(), &[]);
    let got = open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    );
    assert!(
        matches!(got, Err(OpenError::NotPorted(NotPorted::RestoreRecord))),
        "{:?}",
        got.map(|_| ())
    );
    let after = opened::state(&c, &dir.0, &store_id(), &[]);
    assert!(opened::differing(&before, &after).is_empty());
    assert_eq!(restored(&dir.0), (0, false));
    // An entry of another kind, or another store's, is none of its: the
    // shared entry opens, as the reference's settle appends nothing.
    for entry in [
        restore_rows::entry(
            "",
            json!({"head_after": null, "kind": "history_accepted"}),
            &[],
        ),
        restore_rows::entry(
            "",
            json!({"head_after": null, "store_id": "f".repeat(32)}),
            &[],
        ),
    ] {
        let dir = scratch("open-restore-shared-other");
        set_up(&dir.0, &[], entry);
        let c = dir.0.join("c");
        let store = open_for_writing(
            &c.join("memory.sqlite"),
            &c.join("keys"),
            &opened::engine_provider(),
        )
        .expect("opens");
        assert!(store.upkeep().is_empty(), "{:?}", store.upkeep());
        store.close().expect("close");
    }
    // A record that does not read is said, as `pending` says it.
    let dir = scratch("open-restore-shared-unread");
    set_up(&dir.0, &[], pending());
    let c = dir.0.join("c");
    fs::write(
        c.join("keys")
            .join(format!("{}.backup_restore.json", store_id())),
        b"{",
    )
    .expect("record");
    let store = open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("opens");
    assert!(
        matches!(
            store.upkeep(),
            [UpkeepWarning::Restore(Warning::RecordUnread { .. })]
        ),
        "{:?}",
        store.upkeep()
    );
    store.close().expect("close");
}

/// What `settle` catches is logged and the store opens; what it lets
/// through (OverflowError: a head past an SQLite INTEGER) fails the open
/// after the upkeep wrote the anchor.
#[test]
fn a_caught_failure_opens_and_an_uncaught_one_raises() {
    let dir = scratch("open-restore-caught");
    set_up(&dir.0, &[], pending());
    let c = dir.0.join("c");
    fs::create_dir(
        c.join("keys")
            .join(format!("{}.backup_restore.json.lock", store_id())),
    )
    .expect("a directory for the lock");
    let store = open_for_writing_mut(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &mut restore_rows::clock(),
    )
    .expect("the open does not fail");
    match store.upkeep() {
        [UpkeepWarning::Restore(Warning::NotAppended(cause))] => {
            assert_eq!(cause.class, "IsADirectoryError")
        }
        other => panic!("{other:?}"),
    }
    store.close().expect("close");

    let dir = scratch("open-restore-uncaught");
    set_up(
        &dir.0,
        &[json!({"unlink": ".origin"})],
        restore_rows::entry("", json!({"head_after": {"seq": 777_777, "mac": "x"}}), &[]),
    );
    let c = dir.0.join("c");
    // A seq of 2**64, which no JSON value here holds.
    let record = c
        .join("keys")
        .join(format!("{}.backup_restore.json", store_id()));
    let text = fs::read_to_string(&record).expect("record");
    fs::write(&record, text.replace("777777", "18446744073709551616")).expect("record");
    let got = open_for_writing_mut(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &mut restore_rows::clock(),
    );
    match got {
        Err(e) => {
            assert_eq!(e.class(), "OverflowError");
            assert_eq!(
                e.to_string(),
                "Python int too large to convert to SQLite INTEGER"
            );
        }
        Ok(_) => panic!("opened"),
    }
    let origin = c.join("keys").join(format!("{}.origin", store_id()));
    assert!(origin.is_file(), "the anchor's upkeep ran first");
}

/// The entries that share the provider read the record again where the
/// settle runs: a pending entry another process wrote after the decision
/// refuses the open there, and a record nested past where `json.loads`
/// stops is said as `settle` says what it catches (`pending` raised
/// RecursionError), and the store opens.
#[test]
fn a_shared_entry_reads_the_record_again_at_the_settle() {
    let dir = scratch("open-restore-shared-since");
    let other = restore_rows::entry(
        "",
        json!({"head_after": null, "store_id": "f".repeat(32)}),
        &[],
    );
    set_up(&dir.0, &[], other);
    let c = dir.0.join("c");
    let record = c
        .join("keys")
        .join(format!("{}.backup_restore.json", store_id()));
    let mut entry = pending();
    entry["head_after"] = json!({"seq": 8, "mac": mac_at(&c, 8)});
    let got = open_for_writing_with(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
        &mut |b| {
            if b == Boundary::Checked {
                restore_rows::put_record(&record, &restore_rows::record_of(&[entry.clone()]));
            }
        },
    );
    assert!(
        matches!(
            got.as_ref().map_err(OpenError::inner),
            Err(OpenError::NotPorted(NotPorted::RestoreRecord))
        ),
        "{:?}",
        got.map(|_| ())
    );
    assert_eq!(restored(&dir.0), (0, false));

    let dir = scratch("open-restore-shared-nested");
    set_up(&dir.0, &[], pending());
    let c = dir.0.join("c");
    let deep = 100_000;
    fs::write(
        c.join("keys")
            .join(format!("{}.backup_restore.json", store_id())),
        format!("{{\"restores\": {}{}}}", "[".repeat(deep), "]".repeat(deep)),
    )
    .expect("record");
    let store = open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::engine_provider(),
    )
    .expect("opens");
    match store.upkeep() {
        [UpkeepWarning::Restore(Warning::NotAppended(cause))] => {
            assert_eq!(cause.class, "RecursionError")
        }
        other => panic!("{other:?}"),
    }
    store.close().expect("close");
}

/// A restore's warning, read through the open's own warning type, has the
/// arguments `restore_rows` gives it: the store, each value's text, the
/// row's seq.
#[test]
fn a_restore_warning_keeps_its_arguments() {
    let dir = scratch("open-restore-args");
    set_up(&dir.0, &[], pending());
    let c = dir.0.join("c");
    let store = open_for_writing_mut(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &mut restore_rows::clock(),
    )
    .expect("open");
    let Some(warning @ UpkeepWarning::Restore(inner @ Warning::Named { seq, .. })) =
        store.upkeep().last()
    else {
        panic!("{:?}", store.upkeep())
    };
    assert_eq!(warning.format(), inner.format());
    let outer = warning.args();
    let own = inner.args();
    assert_eq!(outer.len(), own.len());
    for (o, i) in outer.iter().zip(&own) {
        match (o, i) {
            (UpkeepArg::Path, RestoreArg::Store) => {}
            (UpkeepArg::Int(o), RestoreArg::Int(i)) => assert_eq!(o, i),
            (UpkeepArg::Text(o), RestoreArg::Value(v)) => {
                assert_eq!(Some(o.clone()), bettermemory_core::pyvalue::py_str(v).ok())
            }
            other => panic!("{other:?}"),
        }
    }
    assert!(outer.contains(&UpkeepArg::Int(*seq)), "{outer:?}");
    store.close().expect("close");
}
