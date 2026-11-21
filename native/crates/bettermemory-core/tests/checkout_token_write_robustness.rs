//! Hostile input for `write`: very long paths, a folder of many entries,
//! clocks at the ends of the doubles, and two processes writing one git
//! directory at once. No panic in debug or release; time grows with the
//! input in proportion.

mod ct_write_script;
mod timing;

use bettermemory_core::checkout_token::read::Fault;
#[cfg(unix)]
use bettermemory_core::checkout_token::write::Machine;
use bettermemory_core::checkout_token::write::{sweep, write, TokenWrite, WriteStatus};
use bettermemory_core::fsdata::Flavour;
use bettermemory_core::gitfile_rule::gitdir::Ctx;
use ct_write_script::{WMachine, WScript, CWD};
use serde_json::{json, Value};

fn run_on(script: &WScript, git: &str) -> Result<TokenWrite, Fault> {
    let ctx = Ctx {
        fs: script,
        flavour: Flavour::Posix,
        cwd: CWD.as_bytes(),
    };
    let mut machine = WMachine { script };
    write(&ctx, &mut machine, git, true, false)
}

fn absent() -> Value {
    json!({"open": "ENOENT"})
}

#[test]
fn a_very_long_git_dir_is_written_in_linear_time() {
    let run = |n: usize| {
        let git = format!("/{}", "a/".repeat(n));
        let script = WScript::new(&json!({"git": git, "reads": [absent()]}));
        let answer = run_on(&script, &git).expect("an answer");
        assert_eq!(answer.status, WriteStatus::Written);
    };
    run(100_000);
    timing::grows_within(
        "long git dir",
        timing::TENFOLD,
        &mut || timing::once(|| run(5_000)),
        &mut || timing::once(|| run(50_000)),
    );
}

#[test]
fn a_folder_of_many_entries_is_swept_in_linear_time() {
    let row = |n: usize| {
        let scan: Vec<Value> = (0..n)
            .map(|i| {
                if i % 2 == 0 {
                    json!({"name": format!("bettermemory.tmp-{i}"), "m": i as f64})
                } else {
                    json!({"name": format!("other-{i}"), "m": 0.0})
                }
            })
            .collect();
        json!({"scan": scan})
    };
    let sweep_of = |row: &Value| {
        let script = WScript::new(row);
        sweep(&script, Flavour::Posix, "/r/.git/info", Some(1e12));
        let log = script.read.log.borrow();
        assert_eq!(log.iter().filter(|l| l.starts_with("unlink")).count(), {
            let n = row["scan"].as_array().map_or(0, Vec::len);
            n.div_ceil(2)
        });
    };
    let (small, large) = (row(2_000), row(20_000));
    timing::grows_within(
        "sweep",
        timing::TENFOLD,
        &mut || timing::once(|| sweep_of(&small)),
        &mut || timing::once(|| sweep_of(&large)),
    );
}

#[test]
fn odd_times_neither_panic_nor_sweep_what_the_reference_keeps() {
    let scan = json!([{"name": "bettermemory.tmp-A", "m": 1.0}]);
    for (now, removed) in [
        (f64::NAN, false),
        (f64::INFINITY, true),
        (f64::NEG_INFINITY, false),
        (f64::MAX, true),
        (f64::MIN, false),
        (3601.0, false),
        (3601.0000000000005, true),
    ] {
        let script = WScript::new(&json!({"scan": scan}));
        sweep(&script, Flavour::Posix, "/r/.git/info", Some(now));
        let log = script.read.log.borrow();
        assert_eq!(
            log.iter().any(|l| l.starts_with("unlink")),
            removed,
            "{now}"
        );
    }
}

#[test]
fn a_clock_at_the_end_of_the_doubles_reads_once() {
    for start in [f64::MAX, 1e300, 1e17] {
        let script = WScript::new(&json!({"reads": vec![absent(); 60], "link": "EEXIST"}));
        script.clock.set(start);
        let answer = run_on(&script, "/r/.git");
        // `deadline - now` is zero where adding 1.5 does not move the
        // clock, so one read ends the race: the reference's `left <= 0`.
        let reads = script
            .read
            .log
            .borrow()
            .iter()
            .filter(|l| l.starts_with("open"))
            .count();
        if start + 1.5 == start {
            assert_eq!(reads, 2, "{start}");
            assert_eq!(answer.expect("an answer").status, WriteStatus::FailedIo);
        }
    }
}

#[test]
fn every_scripted_failure_point_answers_without_panic() {
    for errno in ["EACCES", "EIO", "ENOSPC", "EROFS", "EEXIST", "ENOENT"] {
        for key in ["mkdir", "link", "utime", "owner", "mtime"] {
            let script = WScript::new(&json!({"reads": vec![absent(); 60], key: errno}));
            let _ = run_on(&script, "/r/.git");
        }
        for create in [
            json!({"open": errno}),
            json!({"writes": [errno]}),
            json!({"close": errno}),
        ] {
            let script = WScript::new(&json!({"reads": [absent()], "creates": [create]}));
            let _ = run_on(&script, "/r/.git");
        }
    }
}

/// This process as a [`Machine`]: the real clock and random bytes.
#[cfg(unix)]
struct Real {
    euid: u32,
    start: std::time::Instant,
}

#[cfg(unix)]
impl Machine for Real {
    fn euid(&mut self) -> u32 {
        self.euid
    }
    fn win32(&self) -> bool {
        false
    }
    fn urandom(&mut self, count: usize) -> Vec<u8> {
        let mut out = vec![0u8; count];
        getrandom::fill(&mut out).expect("random bytes");
        out
    }
    fn monotonic(&mut self) -> f64 {
        self.start.elapsed().as_secs_f64()
    }
    fn sleep(&mut self, seconds: f64) {
        std::thread::sleep(std::time::Duration::from_secs_f64(seconds));
    }
}

#[cfg(unix)]
const CHILD: &str = "BM_CT_WRITE_CHILD_GIT_DIR";

/// One writer of the two-process test, run as its own process.
#[cfg(unix)]
#[test]
#[ignore = "run by two_processes_writing_one_git_dir_leave_one_token"]
fn child_writer() {
    use bettermemory_core::fsdata::StdFs;
    use std::os::unix::fs::MetadataExt;
    let Ok(git) = std::env::var(CHILD) else {
        return;
    };
    let euid = std::fs::metadata(&git).expect("git dir").uid();
    let mut machine = Real {
        euid,
        start: std::time::Instant::now(),
    };
    let ctx = Ctx {
        fs: &StdFs,
        flavour: Flavour::Posix,
        cwd: b"/",
    };
    let got = write(&ctx, &mut machine, &git, true, false).expect("an answer");
    println!(
        "ANSWER {} {}",
        got.status.name(),
        got.token.unwrap_or_default()
    );
}

/// Two processes write one fresh git directory at once, rounds over: each
/// reports one token, the same, and at most one wrote it; no temporary
/// file is left. The reference skips the owner check and the directory
/// fsync on Windows, where this test does not run; it was seen on macOS.
#[cfg(unix)]
#[test]
fn two_processes_writing_one_git_dir_leave_one_token() {
    let exe = std::env::current_exe().expect("the test binary");
    let root = std::env::temp_dir().join(format!("bm-ct-write2-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    for round in 0..8 {
        let git = root.join(format!("r{round}/.git"));
        std::fs::create_dir_all(&git).expect("git dir");
        let children: Vec<_> = (0..2)
            .map(|_| {
                std::process::Command::new(&exe)
                    .args([
                        "--exact",
                        "child_writer",
                        "--ignored",
                        "--nocapture",
                        "--test-threads=1",
                    ])
                    .env(CHILD, &git)
                    .stdout(std::process::Stdio::piped())
                    .spawn()
                    .expect("a child")
            })
            .collect();
        let mut answers = Vec::new();
        for child in children {
            let out = child.wait_with_output().expect("the child ends");
            assert!(out.status.success(), "child failed");
            let text = String::from_utf8_lossy(&out.stdout).into_owned();
            let line = text
                .lines()
                .find_map(|l| l.split_once("ANSWER ").map(|(_, a)| a))
                .unwrap_or_else(|| panic!("an answer line: {text}"))
                .to_owned();
            answers.push(line);
        }
        let tokens: Vec<&str> = answers
            .iter()
            .map(|a| a.split(' ').nth(1).unwrap_or(""))
            .collect();
        assert_eq!(tokens[0], tokens[1], "{answers:?}");
        assert_eq!(tokens[0].len(), 26, "{answers:?}");
        let written = answers.iter().filter(|a| a.starts_with("written ")).count();
        assert!(
            written <= 1
                && answers
                    .iter()
                    .all(|a| a.starts_with("written ") || a.starts_with("exists ")),
            "{answers:?}"
        );
        let left: Vec<String> = std::fs::read_dir(git.join("info"))
            .expect("info")
            .map(|e| e.expect("entry").file_name().to_string_lossy().into_owned())
            .collect();
        assert_eq!(left, ["bettermemory"], "{answers:?}");
    }
    let _ = std::fs::remove_dir_all(&root);
}

/// `os.utime(path)` opens nothing, so a temp its owner may not read (a
/// umask of 0o477 leaves mode 0o200) still gets the file system's "now"
/// and the sweep runs. The reference behaves so on macOS and Linux (its
/// POSIX branch is `utimensat` with no times); seen here on macOS.
/// Windows opens for FILE_WRITE_ATTRIBUTES only, so it needs no read
/// access either; this test does not run there.
#[cfg(unix)]
#[test]
fn utime_now_needs_no_read_access() {
    use bettermemory_core::checkout_token::write::TokenFs;
    use bettermemory_core::fsdata::StdFs;
    use std::os::unix::fs::PermissionsExt;
    let dir = std::env::temp_dir().join(format!("ct-utime-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let file = dir.join("bettermemory.tmp-OLD");
    std::fs::write(&file, b"x").unwrap();
    let old = std::time::SystemTime::now() - std::time::Duration::from_secs(7200);
    std::fs::File::options()
        .write(true)
        .open(&file)
        .unwrap()
        .set_modified(old)
        .unwrap();
    std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o200)).unwrap();
    let answer = StdFs.utime_now(file.to_str().unwrap().as_bytes());
    let mtime = std::fs::metadata(&file).unwrap().modified().unwrap();
    std::fs::remove_dir_all(&dir).unwrap();
    assert!(answer.is_ok(), "{answer:?}");
    assert!(mtime > old + std::time::Duration::from_secs(3600));
}
