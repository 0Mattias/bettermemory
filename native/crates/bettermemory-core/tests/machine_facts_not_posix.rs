//! `_file_object` on Windows answers the id file's device, inode and
//! `st_birthtime_ns`. The Windows flavour does not port the stat
//! (`StdIdFileStat` answers `NotPorted`), so `file_object` declines there:
//! the crate's stated Windows gap (ENG-124). Here the decline is checked
//! and the gap recorded. `golden_machine_facts.rs`,
//! `machine_facts_kills.rs` and `machine_facts_robustness.rs` run
//! everywhere.

#![cfg(not(unix))]

mod common;

use bettermemory_core::machine::facts as f;

#[test]
fn the_id_file_stat_declines_here() {
    let dir = std::env::temp_dir().join(format!("bm-machine-facts-np-{}", std::process::id()));
    std::fs::create_dir_all(&dir).expect("a folder");
    let path = dir.join("machine-id");
    std::fs::write(&path, b"x").expect("a file");
    let raw = path.to_string_lossy().into_owned().into_bytes();
    let got = f::file_object(&f::StdIdFileStat, &raw);
    std::fs::remove_dir_all(&dir).ok();
    assert!(
        matches!(got, Err(f::FileObjectError::Declined(_))),
        "{got:?}"
    );
    common::parity::record(
        "machine_facts_not_posix",
        "machine_facts_platform.jsonl",
        "os",
        0,
        &["_file_object over a live file".to_owned()],
        &["the Windows flavour does not port os.stat (ENG-124); \
           the reference answers (dev, ino, st_birthtime_ns)"
            .to_owned()],
    );
}
