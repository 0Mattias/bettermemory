//! A native `_record` killed anywhere leaves the record it replaced or the
//! one it wrote, never a torn one: both builds read every record a kill
//! leaves as a whole answer, and a further native record succeeds.
//!
//! The test binary runs itself as the writing child
//! (`record_kill_child`, ignored when run alone), which records answers
//! for one PATH, asked at 1, 2, 3 and on, over a record of the answer
//! asked at 0, until it is killed from outside at fixed delays (no
//! destructors, no `finally`). After each kill the record must read, by
//! the native build, as the answer asked at some `j`, and its bytes must
//! be exactly those a record of that answer alone writes; it is kept as a
//! copy. The copies are then read by the reference's `_recorded` in one
//! child process, when `BM_REFERENCE_PYTHON` imports `bettermemory` (else
//! set aside, said in a PARITY line), which must give the same `j`. A
//! temporary file the kill left beside the record is counted, as the
//! reference leaves one too (`atomic_write_bytes`' `finally` does not run
//! under SIGKILL).

#![cfg(unix)]

mod common;

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::gitsystem::record::{Answer, Natural, Place, Recorded, Sources, RECORD};
use common::created::{reference_python, run_reference};

const ROOT_ENV: &str = "BM_TEST_GITSYS_KILL_ROOT";
const KEY: &str = "0123456789abcdef0123456789abcdef";

fn answer(asked: f64) -> Answer {
    Answer {
        key: KEY.into(),
        git: Some("/usr/bin/git".chars().map(u32::from).collect()),
        stamp: Some(["1", "2", "3", "4", "33261"].map(String::from)),
        asked,
        ok: true,
        sources: Sources {
            own: vec!["/etc/a".chars().map(u32::from).collect()],
            system: Some("/etc/gitconfig".chars().map(u32::from).collect()),
            legacy: Some(true),
            resets: Some(true),
            prefix: Some("/usr".chars().map(u32::from).collect()),
            compat_differ: Some(false),
            hasconfig: Some(true),
            version: Some([2, 54, 0].map(Natural::from)),
        },
        tried: None,
    }
}

fn place(state: &str) -> Place<'_, StdFs> {
    Place {
        fs: &StdFs,
        flavour: Flavour::Posix,
        state_dir: state.as_bytes(),
        cwd: b"/",
    }
}

/// The writing child: records over `$ROOT/state` until killed.
#[test]
#[ignore = "run by the kill test as its child"]
fn record_kill_child() {
    let Some(root) = std::env::var_os(ROOT_ENV).map(PathBuf::from) else {
        return;
    };
    let state = root.join("state");
    let state = state.to_str().unwrap();
    for j in 1.. {
        let done = place(state).record(&answer(f64::from(j))).unwrap();
        assert!(matches!(done, Recorded::Written(_)));
    }
}

/// The bytes a record of the answer asked at `j` alone holds.
fn whole(j: u32, scratch: &Path) -> Vec<u8> {
    let dir = scratch.join(format!("whole-{j}"));
    fs::create_dir(&dir).unwrap();
    let state = dir.join("state");
    let Recorded::Written(data) = place(state.to_str().unwrap())
        .record(&answer(f64::from(j)))
        .unwrap()
    else {
        panic!("not written");
    };
    data
}

fn scratch() -> PathBuf {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("bm-gitsyskill-{}-{n}", std::process::id()));
    fs::create_dir(&dir).expect("a new scratch directory");
    fs::canonicalize(&dir).unwrap()
}

#[test]
fn a_killed_record_is_the_old_one_or_the_new_one() {
    let base = scratch();
    // Fixed delays, in microseconds: within the child's start, and well
    // into its loop of writes.
    let delays: Vec<u64> = (0..40).map(|i| (i * 7919) % 40_000 + 200).collect();
    let mut kept: Vec<(PathBuf, u32)> = Vec::new();
    let mut temps = 0;
    for (i, us) in delays.iter().enumerate() {
        let root = base.join(format!("k{i}"));
        let state = root.join("state");
        fs::create_dir_all(&root).unwrap();
        let state_text = state.to_str().unwrap().to_owned();
        place(&state_text).record(&answer(0.0)).unwrap();
        let mut child = Command::new(std::env::current_exe().expect("exe"))
            .args([
                "--exact",
                "record_kill_child",
                "--ignored",
                "--nocapture",
                "--test-threads=1",
            ])
            .env(ROOT_ENV, &root)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .expect("child");
        std::thread::sleep(Duration::from_micros(*us));
        let _ = child.kill();
        let _ = child.wait();
        let got = place(&state_text)
            .recorded(KEY)
            .unwrap()
            .expect("a whole answer after the kill");
        assert!(got.ok && got.asked.fract() == 0.0 && got.asked >= 0.0);
        let j = got.asked as u32;
        let data = fs::read(state.join(RECORD)).unwrap();
        assert_eq!(data, whole(j, &root), "kill {i}: the record of {j}, whole");
        temps += fs::read_dir(&state).unwrap().count() - 1;
        // A further record succeeds over what the kill left.
        assert!(matches!(
            place(&state_text)
                .record(&answer(f64::from(j) + 0.5))
                .unwrap(),
            Recorded::Written(_)
        ));
        let copy = base.join(format!("copy{i}"));
        fs::create_dir(&copy).unwrap();
        fs::write(copy.join(RECORD), &data).unwrap();
        kept.push((copy, j));
    }
    eprintln!(
        "gitsystem_record_kills: {} kills, records left asked at {:?}, {temps} temporary files left",
        kept.len(),
        kept.iter().map(|k| k.1).collect::<Vec<_>>()
    );
    match reference_python() {
        Err(why) => eprintln!("PARITY gitsystem_record_kills set aside: {why}"),
        Ok(python) => {
            let script = r#"
import sys
from pathlib import Path
from unittest import mock
from bettermemory import gitsystem
for copy in sys.argv[1:]:
    with mock.patch.object(gitsystem, "_record_path", lambda: Path(copy) / gitsystem.RECORD):
        got = gitsystem._recorded("0123456789abcdef0123456789abcdef")
    print("none" if got is None else f"{got.ok} {got.asked!r}")
"#;
            let args: Vec<&Path> = kept.iter().map(|k| k.0.as_path()).collect();
            let out = run_reference(&python, &base.join("home"), script, &args);
            assert!(
                out.status.success(),
                "{}",
                String::from_utf8_lossy(&out.stderr)
            );
            let lines: Vec<String> = String::from_utf8(out.stdout)
                .unwrap()
                .lines()
                .map(str::to_owned)
                .collect();
            let want: Vec<String> = kept.iter().map(|k| format!("True {}.0", k.1)).collect();
            assert_eq!(lines, want, "the reference reads each record left");
            eprintln!(
                "PARITY gitsystem_record_kills: the reference read {} records",
                lines.len()
            );
        }
    }
    let _ = fs::remove_dir_all(&base);
}
