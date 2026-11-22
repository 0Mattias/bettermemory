//! `open_machine` on Windows makes the state directory, reads both facts
//! and writes the two files. The Windows flavour does not port the paths
//! or the id file's stat, so `open_machine` declines (`NotPorted`) before
//! touching anything: the crate's stated Windows gap (ENG-124), recorded
//! here as class os. `machine_open.jsonl` and `machine_open_platform.jsonl`
//! are POSIX only, and so are the tests that read them.

#![cfg(not(unix))]

mod common;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::fsdata::StdFs;
use bettermemory_core::location_probe::Declined;
use bettermemory_core::machine::facts::StdIdFileStat;
use bettermemory_core::machine::open::{open_machine, OpenError};

#[test]
fn open_machine_declines_here() {
    let dir = std::env::temp_dir().join(format!("bm-machine-open-np-{}", std::process::id()));
    let now = DateTime::new(2026, 1, 1, 0, 0, 0, 0, Some(0)).expect("a time");
    let got = open_machine(
        &dir,
        &StdFs,
        &StdIdFileStat,
        || panic!("no hardware read"),
        || panic!("no hostname read"),
        &now,
        || panic!("no draw"),
    );
    assert!(
        matches!(got, Err(OpenError::Declined(Declined::NotPorted))),
        "{got:?}"
    );
    assert!(!dir.exists());
    common::parity::record(
        "machine_open_not_posix",
        "machine_open.jsonl",
        "os",
        0,
        &["open_machine over a state directory".to_owned()],
        &[
            "the Windows flavour does not port the paths or os.stat (ENG-124); \
           the reference opens and writes the state directory"
                .to_owned(),
        ],
    );
}
