//! Every case of `location_stamps_platform.jsonl` against
//! `location_stamps`: `_device_name` in each section, as the platform the
//! section names answers, and the table spellings read back. The rows of
//! `location_stamps.jsonl` and `location_stamps_python.jsonl`, which are
//! made on POSIX only, are compared in `golden_location_stamps_posix.rs`.

mod common;

use bettermemory_core::location_stamps::{self as ls, Platform};
use common::location_stamps::{answer, bytes_of, int};
use serde_json::Value;

/// The encoding of `os.major` a section of the platform file names.
fn encoding(name: &str) -> Platform {
    match name {
        "darwin" => Platform::Darwin,
        "linux-gnu" => Platform::Linux,
        "linux-musl" => Platform::LinuxMusl,
        "win32" => Platform::Windows,
        other => panic!("no device encoding for {other}"),
    }
}

#[test]
fn device_names_answer_as_each_platform_that_made_them() {
    let name = "location_stamps_platform.jsonl";
    let rows = common::jsonl(name);
    let listed: Vec<&str> = rows[0]["sections"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s.as_str().unwrap())
        .collect();
    // macOS's section and both Linux C libraries' are in every set: the
    // running platform's made there, the others carried as recorded.
    assert_eq!(&listed[..3], ["darwin", "linux-gnu", "linux-musl"]);
    let mut seen: Vec<&str> = Vec::new();
    let mut devices: Vec<Vec<&Value>> = Vec::new();
    let mut plat = None;
    let mut checked = 0;
    for row in &rows[1..] {
        if row[0] == "section" {
            seen.push(row[1].as_str().unwrap());
            devices.push(Vec::new());
            plat = Some(encoding(row[1].as_str().unwrap()));
            continue;
        }
        assert_eq!(row[0], "dev", "row {row}");
        let plat = plat.expect("a row before any section");
        let got = answer(ls::device_name(plat, int(&row[1])));
        assert_eq!(got, row[2], "{} row {row}", seen.last().unwrap());
        devices.last_mut().unwrap().push(&row[1]);
        checked += 1;
    }
    assert_eq!(seen, listed);
    // Every section names the same devices in the same order.
    assert!(devices.iter().all(|d| d == &devices[0]), "sections differ");
    common::assert_count(name, checked, rows.len() - 1 - seen.len());
}

#[test]
fn spellings_read_back() {
    assert_eq!(bytes_of("=a b"), b"a b");
    assert_eq!(bytes_of("5c00"), b"\\\0");
}
