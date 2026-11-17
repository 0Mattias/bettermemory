//! A native ask killed anywhere (making the ask folder, sweeping and
//! building the probe, between git's answers) leaves a state directory
//! the next ask answers from in full: the same answer and the same
//! commands as an ask in a fresh state directory, and nothing outside
//! the state directory changed.
//!
//! The test binary runs itself as the asking child (`ask_kill_child`,
//! ignored when run alone), which asks again and again, breaking the
//! probe between asks, until it is killed from outside at fixed delays
//! timed from a marker it writes as it enters its loop.

#![cfg(unix)]

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command as Process;
use std::time::{Duration, Instant};

use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::gitsystem::ask::{Asker, Budget, Clock, Command, Env, Git, Ran};
use bettermemory_core::gitsystem::probe::probe_folder;
use bettermemory_core::gitsystem::record::{Answer, Place};

const ROOT_ENV: &str = "BM_TEST_GITSYS_ASK_KILL_ROOT";
const LOOPING: &str = "looping";

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

/// The default answers of the generator's script, and every command.
struct Scripted {
    asked: Vec<Command>,
}

impl Git for Scripted {
    fn run(&mut self, c: &Command) -> Ran {
        self.asked.push(c.clone());
        let q: Vec<String> = c.argv[9..]
            .iter()
            .map(|a| a.iter().map(|&x| char::from_u32(x).unwrap()).collect())
            .collect();
        let q: Vec<&str> = q.iter().map(String::as_str).collect();
        let stdout: &[u8] = match q.last().copied() {
            Some("GIT_CONFIG_SYSTEM") => b"/etc/gitconfig\n",
            Some("-z") => b"system\0file:/etc/gitconfig\0a\nb\0",
            Some("version") => b"git version 2.54.0\n",
            Some("p") => b"bettermemory-probe-b\n",
            Some(r"^bettermemory\.") => {
                b"bettermemory.prefix\n/usr/z\0bettermemory.hasconfig\nyes\0"
            }
            _ => b".\n",
        };
        Ran::Exited {
            status: 0,
            stdout: stdout.to_vec(),
        }
    }
}

struct Steady(f64);

impl Clock for Steady {
    fn monotonic(&mut self) -> f64 {
        self.0 += 0.001;
        self.0
    }
    fn wall(&mut self) -> f64 {
        2e9
    }
}

fn ask(root: &Path) -> (Answer, Vec<Command>) {
    let state = format!("{}/state", root.display());
    let cwd = root.to_str().unwrap().to_owned();
    let env: Env = vec![(cps("PATH"), cps(&format!("{}/bin", root.display())))];
    let place = Place {
        fs: &StdFs,
        flavour: Flavour::Posix,
        state_dir: state.as_bytes(),
        cwd: cwd.as_bytes(),
    };
    let mut git = Scripted { asked: Vec::new() };
    let got = Asker {
        place: &place,
        git: &mut git,
        clock: &mut Steady(100.0),
        env: &env,
        cs_path: None,
        budget: &mut Budget::Inactive,
    }
    .ask("k", 1.0)
    .unwrap();
    (got, git.asked)
}

fn setup(root: &Path) {
    fs::create_dir_all(root.join("bin")).unwrap();
    fs::write(root.join("bin/git"), b"#!").unwrap();
    fs::set_permissions(root.join("bin/git"), fs::Permissions::from_mode(0o755)).unwrap();
    fs::create_dir_all(root.join("out")).unwrap();
    fs::write(root.join("out/f"), b"outside").unwrap();
}

#[test]
#[ignore = "run by the kill test as its child"]
fn ask_kill_child() {
    let Some(root) = std::env::var_os(ROOT_ENV).map(PathBuf::from) else {
        return;
    };
    let probe = root.join("state").join(probe_folder());
    fs::write(root.join(LOOPING), b"").unwrap();
    for j in 0u64.. {
        let (got, _) = ask(&root);
        assert!(got.ok);
        match j % 3 {
            0 => fs::write(probe.join("extra"), b"x").unwrap(),
            1 => fs::write(probe.join("config"), b"[core]\n").unwrap(),
            _ => fs::remove_dir_all(root.join("state")).unwrap(),
        }
    }
}

struct Guard(PathBuf);

impl Drop for Guard {
    fn drop(&mut self) {
        if std::thread::panicking() {
            eprintln!(
                "gitsystem_ask_kills: removing {} after the failure",
                self.0.display()
            );
        }
        let _ = fs::remove_dir_all(&self.0);
    }
}

/// A command's argv and environment, the scratch path as `@`.
type Labelled = (Vec<Vec<u32>>, Env);

fn label(root: &Path, commands: &[Command]) -> Vec<Labelled> {
    let r = cps(root.to_str().unwrap());
    let lab = |t: &Vec<u32>| -> Vec<u32> {
        let mut out = Vec::new();
        let mut i = 0;
        while i < t.len() {
            if t[i..].starts_with(&r) {
                out.push(u32::from(b'@'));
                i += r.len();
            } else {
                out.push(t[i]);
                i += 1;
            }
        }
        out
    };
    commands
        .iter()
        .map(|c| {
            (
                c.argv.iter().map(lab).collect(),
                c.env.iter().map(|(k, v)| (k.clone(), lab(v))).collect(),
            )
        })
        .collect()
}

#[test]
fn a_killed_ask_is_answered_whole_next_time() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("bm-gitsysaskkill-{}-{n}", std::process::id()));
    fs::create_dir(&dir).unwrap();
    let guard = Guard(fs::canonicalize(&dir).unwrap());
    let base = guard.0.clone();
    let fresh = base.join("fresh");
    setup(&fresh);
    let (want, want_commands) = ask(&fresh);
    let want_commands = label(&fresh, &want_commands);
    let delays: Vec<u64> = (0..40).map(|i| (i * 7919) % 40_000 + 200).collect();
    let mut landed = 0;
    for (i, us) in delays.iter().enumerate() {
        let root = base.join(format!("k{i}"));
        setup(&root);
        let mut child = Process::new(std::env::current_exe().unwrap())
            .args(["--exact", "ask_kill_child", "--ignored", "--test-threads=1"])
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
        let (got, commands) = ask(&root);
        let shown = format!("kill {i} at {us} us");
        assert!(got.ok, "{shown}");
        assert_eq!(got.sources, want.sources, "{shown}");
        assert_eq!(label(&root, &commands), want_commands, "{shown}");
        assert_eq!(fs::read(root.join("out/f")).unwrap(), b"outside", "{shown}");
        assert_eq!(
            fs::read_dir(root.join("out")).unwrap().count(),
            1,
            "{shown}"
        );
    }
    eprintln!(
        "gitsystem_ask_kills: {} kills, {landed} while the loop ran",
        delays.len()
    );
    assert!(
        landed * 2 >= delays.len(),
        "only {landed} kills landed in the loop"
    );
    drop(guard);
}
