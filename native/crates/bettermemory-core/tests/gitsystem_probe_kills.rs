//! A native `_probe_repository` killed anywhere leaves a state directory
//! the next build of the probe recovers from in one call: whatever a kill
//! left (a folder half made, a temporary file beside a probe file, a probe
//! half cleared), the next native call answers the probe, resolved, and
//! it holds; the reference's `_probe_holds` and `_probe_repository` agree
//! on each tree a kill left. Nothing outside the state directory changes.
//!
//! The test binary runs itself as the building child (`probe_kill_child`,
//! ignored when run alone), which builds the probe, then breaks it (an
//! extra file, a probe file of other bytes, the probe removed whole) and
//! builds it again, until it is killed from outside at fixed delays (no
//! destructors). Every tree is in a fresh scratch folder.

#![cfg(unix)]

mod common;

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::gitsystem::probe::probe_folder;
use bettermemory_core::gitsystem::record::Place;
use common::created::{reference_python, run_reference};

const ROOT_ENV: &str = "BM_TEST_GITSYS_PROBE_KILL_ROOT";
/// The marker the child writes as it enters its build-and-break loop.
const LOOPING: &str = "looping";
/// The clock the sweep is given: no probe here is of another name.
const NOW: f64 = 2_000_000_000.0;

fn place(state: &str) -> Place<'_, StdFs> {
    Place {
        fs: &StdFs,
        flavour: Flavour::Posix,
        state_dir: state.as_bytes(),
        cwd: b"/",
    }
}

/// The building child: builds and breaks `$ROOT/state`'s probe until
/// killed.
#[test]
#[ignore = "run by the kill test as its child"]
fn probe_kill_child() {
    let Some(root) = std::env::var_os(ROOT_ENV).map(PathBuf::from) else {
        return;
    };
    let state = root.join("state");
    let probe = state.join(probe_folder());
    let text = state.to_str().unwrap().to_owned();
    // The signal the parent times its kill from.
    fs::write(root.join(LOOPING), b"").unwrap();
    for j in 0u64.. {
        let got = place(&text).probe_repository(NOW).unwrap();
        assert!(got.is_some());
        match j % 3 {
            0 => fs::write(probe.join("extra"), b"x").unwrap(),
            1 => fs::write(probe.join("config"), b"[core]\n").unwrap(),
            _ => fs::remove_dir_all(&probe).unwrap(),
        }
    }
}

/// The scratch folder, removed however the test ends.
struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        if std::thread::panicking() {
            eprintln!(
                "gitsystem_probe_kills: removing {} after the failure",
                self.0.display()
            );
        }
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn scratch() -> PathBuf {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("bm-gitsysprobekill-{}-{n}", std::process::id()));
    fs::create_dir(&dir).expect("a new scratch directory");
    fs::canonicalize(&dir).unwrap()
}

/// Every path under `dir`, links not followed, with each file's bytes.
fn listing(dir: &Path) -> Vec<(PathBuf, Option<Vec<u8>>)> {
    let mut out = Vec::new();
    let mut stack = vec![dir.to_path_buf()];
    while let Some(d) = stack.pop() {
        for e in fs::read_dir(&d).unwrap() {
            let p = e.unwrap().path();
            let meta = fs::symlink_metadata(&p).unwrap();
            if meta.is_dir() {
                stack.push(p.clone());
                out.push((p, None));
            } else if meta.is_file() {
                let data = fs::read(&p).unwrap();
                out.push((p, Some(data)));
            } else {
                out.push((p, None));
            }
        }
    }
    out.sort();
    out
}

#[test]
fn a_killed_build_is_recovered_in_one_call() {
    let guard = Scratch(scratch());
    let base = guard.0.clone();
    let delays: Vec<u64> = (0..40).map(|i| (i * 7919) % 40_000 + 200).collect();
    let mut left: Vec<(PathBuf, bool)> = Vec::new();
    let mut odd = 0;
    let mut landed = 0;
    for (i, us) in delays.iter().enumerate() {
        let root = base.join(format!("k{i}"));
        let state = root.join("state");
        let out = root.join("out");
        fs::create_dir_all(&state).unwrap();
        fs::create_dir_all(&out).unwrap();
        fs::write(out.join("f"), b"outside").unwrap();
        std::os::unix::fs::symlink(&out, state.join("link")).unwrap();
        let outside = listing(&out);
        let mut child = Command::new(std::env::current_exe().expect("exe"))
            .args([
                "--exact",
                "probe_kill_child",
                "--ignored",
                "--nocapture",
                "--test-threads=1",
            ])
            .env(ROOT_ENV, &root)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .expect("child");
        // The delay runs from the child's entering its loop.
        let started = std::time::Instant::now();
        while !root.join(LOOPING).exists() {
            if child.try_wait().unwrap().is_some() {
                panic!("kill {i}: the child ended before its loop");
            }
            assert!(
                started.elapsed() < Duration::from_secs(60),
                "kill {i}: the child never entered its loop"
            );
            std::thread::sleep(Duration::from_millis(1));
        }
        std::thread::sleep(Duration::from_micros(*us));
        if child.try_wait().unwrap().is_none() {
            landed += 1;
        }
        let _ = child.kill();
        let _ = child.wait();
        let state_text = state.to_str().unwrap().to_owned();
        let probe = state.join(probe_folder());
        // A copy of what the kill left, for the reference.
        let copy = base.join(format!("copy{i}"));
        fs::create_dir(&copy).unwrap();
        if probe.exists() {
            let status = Command::new("cp")
                .arg("-R")
                .arg(&probe)
                .arg(copy.join(probe_folder()))
                .status()
                .unwrap();
            assert!(status.success());
        }
        let copied = copy.join(probe_folder());
        let held = place(copy.to_str().unwrap()).probe_holds(copied.to_str().unwrap().as_bytes())
            == Ok(true);
        if !held {
            odd += 1;
        }
        let p = place(&state_text);
        let (real, before) = p
            .probe_repository(NOW)
            .unwrap()
            .expect("the probe, built again over what the kill left");
        assert_eq!(
            real,
            probe.to_str().unwrap().as_bytes(),
            "kill {i} at {us} us"
        );
        assert_eq!(p.probe_holds(&real), Ok(true), "kill {i} at {us} us");
        assert_eq!(
            p.probe_stamps(&real).unwrap(),
            before,
            "kill {i} at {us} us"
        );
        assert_eq!(
            listing(&out),
            outside,
            "kill {i} at {us} us: outside the state directory"
        );
        assert!(state.join("link").is_symlink());
        left.push((copy, held));
    }
    eprintln!(
        "gitsystem_probe_kills: {} kills, {landed} while the loop ran, {odd} left a probe \
         that did not hold",
        left.len()
    );
    assert!(
        landed * 2 >= delays.len(),
        "only {landed} of {} kills landed while the child's loop ran",
        delays.len()
    );
    match reference_python() {
        Err(why) => eprintln!("PARITY gitsystem_probe_kills set aside: {why}"),
        Ok(python) => {
            // Each copy as the kill left it: the reference's holds, then
            // its build, which must answer.
            let script = r#"
import os, sys
from pathlib import Path
from unittest import mock
from bettermemory import _daemon_client, gitsystem
for copy in sys.argv[1:]:
    probe = Path(copy) / gitsystem.PROBE_FOLDER
    try:
        holds = gitsystem._probe_holds(str(probe))
    except OSError:
        holds = False
    with mock.patch.object(_daemon_client, "resolve_state_dir", lambda: Path(copy)):
        got = gitsystem._probe_repository()
    ok = got is not None and gitsystem._probe_holds(got[0])
    print(f"{holds} {ok}")
"#;
            let args: Vec<&Path> = left.iter().map(|l| l.0.as_path()).collect();
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
            assert_eq!(lines.len(), left.len());
            for (line, (copy, held)) in lines.iter().zip(&left) {
                let (holds, ok) = line.split_once(' ').unwrap();
                let shown = copy.display();
                assert_eq!(holds == "True", *held, "the reference's holds over {shown}");
                assert_eq!(ok, "True", "the reference builds over {shown}");
                let probe = copy.join(probe_folder());
                let after =
                    place(copy.to_str().unwrap()).probe_holds(probe.to_str().unwrap().as_bytes());
                assert_eq!(after, Ok(true), "{shown} holds after the reference's build");
            }
            eprintln!(
                "PARITY gitsystem_probe_kills: the reference rebuilt {} probes",
                lines.len()
            );
        }
    }
    drop(guard);
}
