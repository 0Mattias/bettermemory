//! Hostile input for `machine::open`: a facts file nested a million deep,
//! a very long hostname and predecessor, `ioreg` output of millions of
//! line breaks. No panic, and time grows linearly.

#![cfg(unix)]

mod timing;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::fsdata::StdFs;
use bettermemory_core::machine::facts::StdIdFileStat;
use bettermemory_core::machine::open::{ioreg_text, open_machine, record_bytes, OpenError};
use bettermemory_core::machine::MachineFacts;
use bettermemory_core::pyvalue::PyError;

fn at() -> DateTime {
    DateTime::new(2026, 3, 4, 5, 6, 7, 0, Some(0)).expect("a time")
}

#[test]
fn a_facts_file_a_million_deep_raises_recursion_error() {
    let dir = std::env::temp_dir().join(format!("bm-machine-open-r-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("dir");
    let deep = format!(
        "{{\"a\": {}{}}}",
        "[".repeat(1_000_000),
        "]".repeat(1_000_000)
    );
    std::fs::write(dir.join("machine.json"), deep).expect("facts");
    let got = open_machine(
        &dir,
        &StdFs,
        &StdIdFileStat,
        || Ok(None),
        || None,
        &at(),
        || [0; 17],
    );
    assert!(
        matches!(got, Err(OpenError::Raised(PyError::RecursionError))),
        "{got:?}"
    );
    assert!(!dir.join("machine-id").exists());
    std::fs::remove_dir_all(&dir).ok();
}

fn facts(n: usize) -> (MachineFacts, Vec<u32>) {
    let host: Vec<u32> = [0xDCFFu32, 0x41, 0x2603]
        .iter()
        .copied()
        .cycle()
        .take(n)
        .collect();
    let fact = MachineFacts {
        hardware: Some(host.clone()),
        hostname: Some(host.clone()),
        id_file: None,
    };
    (fact, host)
}

#[test]
fn a_long_record_is_written_in_linear_time() {
    let (small, sp) = facts(100_000);
    let (large, lp) = facts(1_000_000);
    timing::grows_within(
        "record_bytes over long texts",
        timing::TENFOLD,
        &mut || timing::once(|| record_bytes("A", &small, Some(&sp), &at())),
        &mut || timing::once(|| record_bytes("A", &large, Some(&lp), &at())),
    );
}

#[test]
fn ioreg_output_of_many_line_breaks_reads_in_linear_time() {
    let small = "\r\r\n\n".repeat(100_000).into_bytes();
    let large = "\r\r\n\n".repeat(1_000_000).into_bytes();
    assert_eq!(ioreg_text(&large).expect("text").len(), 3_000_000);
    timing::grows_within(
        "ioreg_text over line breaks",
        timing::TENFOLD,
        &mut || timing::once(|| ioreg_text(&small)),
        &mut || timing::once(|| ioreg_text(&large)),
    );
}
