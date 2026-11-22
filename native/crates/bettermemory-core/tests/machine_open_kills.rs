//! Cases a plausible wrong `machine::open` fails: the record's bytes (key
//! order, indent, line feed, escapes, the time's spelling), `ioreg`'s text
//! (universal newlines, a strict decode), a kept id rewritten nowhere, the
//! predecessor of a mint, and the written files' mode.

#![cfg(unix)]

use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::PathBuf;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::fsdata::StdFs;
use bettermemory_core::machine::facts::{IdFileStat, IdStat, IdStatError};
use bettermemory_core::machine::open::{ioreg_text, open_machine, record_bytes, OpenError};
use bettermemory_core::machine::MachineFacts;
use bettermemory_core::pyvalue::PyError;

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn at(us: u32) -> DateTime {
    DateTime::new(2026, 3, 4, 5, 6, 7, us, Some(0)).expect("a time")
}

struct Fixed;

impl IdFileStat for Fixed {
    fn id_stat(&self, _path: &[u8]) -> Result<IdStat, IdStatError> {
        Ok(IdStat {
            dev: u64::MAX,
            ino: 0,
            birth: None,
        })
    }
}

fn scratch(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("bm-machine-open-k-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    dir
}

#[test]
fn the_record_is_sorted_indented_escaped_and_ends_in_a_line_feed() {
    let facts = MachineFacts {
        hardware: None,
        hostname: Some(vec![0xDCFF, 0xE9]),
        id_file: Some((cps("18446744073709551615"), cps("0"), None)),
    };
    let got = record_bytes("A", &facts, Some(&[0xD800]), &at(0)).expect("bytes");
    let want = "{\n  \"facts\": {\n    \"hardware\": null,\n    \"hostname\": \"\\udcff\\u00e9\",\n    \
                \"id_file\": [\n      \"18446744073709551615\",\n      \"0\",\n      null\n    ]\n  },\n  \
                \"id\": \"A\",\n  \"predecessor\": \"\\ud800\",\n  \"recorded\": \"2026-03-04T05:06:07Z\"\n}\n";
    assert_eq!(String::from_utf8(got).expect("ASCII"), want);
    let with_us = record_bytes("A", &facts, None, &at(5)).expect("bytes");
    let text = String::from_utf8(with_us).expect("ASCII");
    assert!(
        text.contains("\"recorded\": \"2026-03-04T05:06:07.000005Z\""),
        "{text}"
    );
    assert!(text.contains("\"predecessor\": null"), "{text}");
}

#[test]
fn ioreg_text_reads_universal_newlines_and_refuses_bad_utf8() {
    assert_eq!(
        ioreg_text(b"a\r\nb\rc\n\r").expect("text"),
        cps("a\nb\nc\n\n")
    );
    assert_eq!(ioreg_text(b"\xff"), Err(PyError::UnicodeDecodeError));
    assert_eq!(ioreg_text(b"\xc3\xa9").expect("text"), vec![0xE9]);
}

#[test]
fn a_kept_id_with_equal_facts_writes_nothing_and_a_change_writes_0600() {
    let dir = scratch("kept");
    let open = |host: &str| {
        open_machine(
            &dir,
            &StdFs,
            &Fixed,
            || Ok(None),
            || Some(cps(host)),
            &at(1),
            || [7u8; 17],
        )
        .expect("an open")
    };
    let first = open("h");
    assert!(first.minted && !first.cloned && first.predecessor.is_none());
    let facts = dir.join("machine.json");
    let ino = std::fs::metadata(&facts).expect("facts").ino();
    let again = open("h");
    assert!(!again.minted && !again.facts_updated);
    assert_eq!(std::fs::metadata(&facts).expect("facts").ino(), ino);
    std::fs::set_permissions(&facts, std::fs::Permissions::from_mode(0o644)).expect("0644");
    let renamed = open("h2");
    assert!(!renamed.minted && renamed.facts_updated && renamed.id == first.id);
    let md = std::fs::metadata(&facts).expect("facts");
    assert_ne!(md.ino(), ino);
    assert_eq!(md.permissions().mode() & 0o777, 0o600);
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn a_mint_names_the_id_files_id_not_the_records() {
    let dir = scratch("pred");
    std::fs::create_dir_all(&dir).expect("dir");
    std::fs::write(
        dir.join("machine-id"),
        "bettermemory machine id v1\n0123456789ABCDEFGHJKMNPQRS\n",
    )
    .expect("id");
    std::fs::write(
        dir.join("machine.json"),
        "{\"id\": \"ZZZZZZZZZZZZZZZZZZZZZZZZZZ\", \"facts\": {}, \"predecessor\": \"P\"}",
    )
    .expect("facts");
    let got = open_machine(
        &dir,
        &StdFs,
        &Fixed,
        || Ok(None),
        || None,
        &at(0),
        || [0u8; 17],
    )
    .expect("an open");
    assert!(got.minted && got.cloned);
    assert_eq!(got.predecessor, Some(cps("0123456789ABCDEFGHJKMNPQRS")));
    assert_eq!(
        got.recorded,
        Some(MachineFacts {
            hardware: None,
            hostname: None,
            id_file: None
        })
    );
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn a_nul_in_the_state_directory_is_value_error_before_any_read() {
    use std::os::unix::ffi::OsStrExt;
    let dir = std::path::Path::new(std::ffi::OsStr::from_bytes(b"/tmp/a\0b"));
    let got = open_machine(
        dir,
        &StdFs,
        &Fixed,
        || panic!("no hardware read"),
        || panic!("no hostname read"),
        &at(0),
        || panic!("no draw"),
    );
    assert!(
        matches!(got, Err(OpenError::Raised(PyError::ValueError))),
        "{got:?}"
    );
}

/// The `ioreg` spawn answers macOS's IOPlatformUUID, the value
/// `gethostuuid` gives; the reference spawns it only on macOS.
#[cfg(target_os = "macos")]
#[test]
fn ioreg_names_the_same_uuid_as_gethostuuid() {
    use bettermemory_core::machine::facts::{ioreg_uuid, uuid_text, HardwareSource};
    use bettermemory_core::machine::open::StdMachine;
    let out = StdMachine::default()
        .ioreg()
        .expect("decodes")
        .expect("spawned");
    let raw = bettermemory_sys::gethostuuid().expect("gethostuuid");
    assert_eq!(ioreg_uuid(&out), Some(cps(&uuid_text(&raw))));
}
