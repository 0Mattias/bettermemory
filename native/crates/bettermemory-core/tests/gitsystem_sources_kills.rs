//! `sources` killed anywhere (asking, writing the record) leaves a state
//! directory a fresh cache answers from in full: the same answer as a
//! fresh state directory gives, a record that is either read or asked
//! over, a second call answered from the held cache without asking, and
//! nothing outside the state directory changed.
//!
//! The test binary runs itself as the child (`sources_kill_child`,
//! ignored when run alone), which calls `sources` again and again over
//! three PATHs with the clock moving past each answer's life, so it asks
//! and records at nearly every call, until it is killed from outside at
//! fixed delays timed from a marker it writes as it enters its loop.

#![cfg(unix)]

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command as Process;
use std::time::{Duration, Instant};

use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::gitsystem::ask::{Budget, Clock, Command, Env, Git, Ran};
use bettermemory_core::gitsystem::record::{Place, Sources, REASK_SECONDS};
use bettermemory_core::gitsystem::sources::{Caller, Held};

const ROOT_ENV: &str = "BM_TEST_GITSYS_SOURCES_KILL_ROOT";
const LOOPING: &str = "looping";

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

struct Scripted {
    asked: usize,
}

impl Git for Scripted {
    fn run(&mut self, c: &Command) -> Ran {
        self.asked += 1;
        let last: String = c
            .argv
            .last()
            .unwrap()
            .iter()
            .map(|&x| char::from_u32(x).unwrap())
            .collect();
        let stdout: &[u8] = match last.as_str() {
            "GIT_CONFIG_SYSTEM" => b"/etc/gitconfig\n",
            "-z" => b"system\0file:/etc/gitconfig\0a\nb\0",
            "version" => b"git version 2.54.0\n",
            "p" => b"bettermemory-probe-b\n",
            r"^bettermemory\." => b"bettermemory.prefix\n/usr/z\0bettermemory.hasconfig\nyes\0",
            _ => b".\n",
        };
        Ran::Exited {
            status: 0,
            stdout: stdout.to_vec(),
        }
    }
}

struct Steady {
    mono: f64,
    wall: f64,
}

impl Clock for Steady {
    fn monotonic(&mut self) -> f64 {
        self.mono += 0.001;
        self.mono
    }
    fn wall(&mut self) -> f64 {
        self.wall
    }
}

/// `sources()` for PATH entry `p` at `now`, and how many questions git
/// was asked.
fn call(root: &Path, held: &mut Held, p: usize, now: f64) -> (Sources, usize) {
    let r = root.to_str().unwrap().to_owned();
    let state = format!("{r}/state");
    let link = format!("{r}/xcode");
    let env: Env = vec![(cps("PATH"), cps(&format!("{r}/p{p}")))];
    let place = Place {
        fs: &StdFs,
        flavour: Flavour::Posix,
        state_dir: state.as_bytes(),
        cwd: r.as_bytes(),
    };
    let mut git = Scripted { asked: 0 };
    let got = Caller {
        place: &place,
        git: &mut git,
        clock: &mut Steady {
            mono: 100.0,
            wall: now,
        },
        env: &env,
        cs_path: None,
        budget: &mut Budget::Inactive,
        macos: false,
        link: link.as_bytes(),
    }
    .sources(held, true)
    .unwrap();
    (got, git.asked)
}

fn setup(root: &Path) {
    for p in 0..3 {
        let dir = root.join(format!("p{p}"));
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("git"), b"#!").unwrap();
        fs::set_permissions(dir.join("git"), fs::Permissions::from_mode(0o755)).unwrap();
    }
    fs::create_dir_all(root.join("out")).unwrap();
    fs::write(root.join("out/f"), b"outside").unwrap();
}

#[test]
#[ignore = "run by the kill test as its child"]
fn sources_kill_child() {
    let Some(root) = std::env::var_os(ROOT_ENV).map(PathBuf::from) else {
        return;
    };
    let mut held = Held::new();
    fs::write(root.join(LOOPING), b"").unwrap();
    for j in 0u64.. {
        let now = 1000.0 + (j / 3) as f64 * (REASK_SECONDS + 1.0);
        let (got, _) = call(&root, &mut held, (j % 3) as usize, now);
        assert_eq!(
            got.version.map(|v| v[1].as_str().to_owned()).as_deref(),
            Some("54")
        );
        if j % 7 == 0 {
            held.clear();
        }
    }
}

struct Guard(PathBuf);

impl Drop for Guard {
    fn drop(&mut self) {
        if std::thread::panicking() {
            eprintln!(
                "gitsystem_sources_kills: removing {} after the failure",
                self.0.display()
            );
        }
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn a_killed_sources_is_answered_whole_next_time() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("bm-gitsyssrckill-{}-{n}", std::process::id()));
    fs::create_dir(&dir).unwrap();
    let guard = Guard(fs::canonicalize(&dir).unwrap());
    let base = guard.0.clone();
    let fresh = base.join("fresh");
    setup(&fresh);
    let (want, _) = call(&fresh, &mut Held::new(), 0, 1000.0);
    let delays: Vec<u64> = (0..40).map(|i| (i * 7919) % 40_000 + 200).collect();
    let mut landed = 0;
    for (i, us) in delays.iter().enumerate() {
        let root = base.join(format!("k{i}"));
        setup(&root);
        let mut child = Process::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "sources_kill_child",
                "--ignored",
                "--test-threads=1",
            ])
            .env(ROOT_ENV, &root)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .unwrap();
        let started = Instant::now();
        while !root.join(LOOPING).exists() {
            assert!(
                child.try_wait().unwrap().is_none(),
                "kill {i}: the child ended"
            );
            assert!(
                started.elapsed() < Duration::from_secs(60),
                "kill {i}: no loop"
            );
            std::thread::sleep(Duration::from_millis(1));
        }
        std::thread::sleep(Duration::from_micros(*us));
        if child.try_wait().unwrap().is_none() {
            landed += 1;
        }
        let _ = child.kill();
        let _ = child.wait();
        let shown = format!("kill {i} at {us} us");
        for p in 0..3 {
            // A fresh cache, at a time the child may have recorded an
            // answer for (current) or not.
            let mut held = Held::new();
            let (got, _) = call(&root, &mut held, p, 1000.0 + REASK_SECONDS / 2.0);
            assert_eq!(got, want, "{shown}");
            let (again, asked) = call(&root, &mut held, p, 1000.0 + REASK_SECONDS / 2.0);
            assert_eq!(again, want, "{shown}");
            assert_eq!(asked, 0, "{shown}: held, yet asked");
        }
        assert_eq!(fs::read(root.join("out/f")).unwrap(), b"outside", "{shown}");
        assert_eq!(
            fs::read_dir(root.join("out")).unwrap().count(),
            1,
            "{shown}"
        );
    }
    eprintln!(
        "gitsystem_sources_kills: {} kills, {landed} while the loop ran",
        delays.len()
    );
    assert!(
        landed * 2 >= delays.len(),
        "only {landed} kills landed in the loop"
    );
    drop(guard);
}
