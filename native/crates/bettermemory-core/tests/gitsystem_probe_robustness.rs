//! The probe over trees no fixture names, and its cost.
//!
//! - Random trees in and around the probe (folders, files, links into
//!   and out of the state directory, dangling links, FIFOs, modes) under
//!   every function: none panics, waits or removes anything outside the
//!   state directory, and the sweep and the clear remove only what the
//!   module says they can.
//! - A probe file that is a FIFO: the reference waits on it for a writer;
//!   the port reads it without waiting and replaces it.
//! - The clear, the holds, the sweep and the build over many entries are
//!   linear: ten times the entries cost under sixty times the time.
//!
//! Every tree is made in a fresh scratch folder.

#![cfg(unix)]

mod timing;

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::gitsystem::probe::{
    is_probe_name, probe_folder, PROBE_FILES, PROBE_FOLDERS, PROBE_LISTED,
};
use bettermemory_core::gitsystem::record::Place;

const NOW: f64 = 2_000_000_000.0;

struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        open_up(&self.0);
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn open_up(dir: &Path) {
    let _ = fs::set_permissions(dir, fs::Permissions::from_mode(0o700));
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if fs::symlink_metadata(&path).is_ok_and(|m| m.is_dir()) {
            open_up(&path);
        }
    }
}

fn scratch() -> Scratch {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("bm-gitsysprobe-rob-{}-{n}", std::process::id()));
    fs::create_dir(&dir).expect("a new scratch directory");
    Scratch(fs::canonicalize(&dir).unwrap())
}

fn place(state: &str) -> Place<'_, StdFs> {
    Place {
        fs: &StdFs,
        flavour: Flavour::Posix,
        state_dir: state.as_bytes(),
        cwd: b"/",
    }
}

fn mkfifo(path: &Path) {
    let made = std::process::Command::new("mkfifo")
        .arg(path)
        .status()
        .is_ok_and(|s| s.success());
    assert!(made, "mkfifo could not run");
}

/// xorshift64*.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_f491_4f6c_dd1d)
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }
}

/// Every entry under `dir` (links not followed) and each file's bytes.
fn listing(dir: &Path) -> Vec<(PathBuf, Option<Vec<u8>>)> {
    let mut out = Vec::new();
    let mut stack = vec![dir.to_path_buf()];
    while let Some(d) = stack.pop() {
        let Ok(entries) = fs::read_dir(&d) else {
            continue;
        };
        for e in entries {
            let p = e.unwrap().path();
            let meta = fs::symlink_metadata(&p).unwrap();
            if meta.is_dir() {
                stack.push(p.clone());
                out.push((p, None));
            } else if meta.is_file() {
                out.push((p.clone(), fs::read(&p).ok()));
            } else {
                out.push((p, None));
            }
        }
    }
    out.sort();
    out
}

/// A random tree: the probe (whole, or partly) and entries in and around
/// it, and the folder `out` beside the state directory.
fn random_tree(rng: &mut Rng, root: &Path) {
    let state = root.join("state");
    let out = root.join("out");
    fs::create_dir(&state).unwrap();
    fs::create_dir(&out).unwrap();
    fs::write(out.join("f"), b"outside").unwrap();
    fs::create_dir(out.join("d")).unwrap();
    fs::write(out.join("d/g"), b"inside").unwrap();
    let probe = state.join(probe_folder());
    let mut places: Vec<PathBuf> = vec![state.clone()];
    if rng.below(4) > 0 {
        fs::create_dir(&probe).unwrap();
        places.push(probe.clone());
        for name in PROBE_FOLDERS {
            if rng.below(6) > 0 && fs::create_dir(probe.join(name)).is_ok() {
                places.push(probe.join(name));
            }
        }
        for (name, data) in PROBE_FILES {
            if rng.below(6) > 0 {
                let _ = fs::write(probe.join(name), data);
            }
        }
    }
    let names = [
        "x",
        "HEAD",
        "config",
        "objects",
        "remotes",
        "p",
        "compat-differ",
        "git-probe",
        "git-probe-0123456789abcdef",
        "git-ask",
    ];
    for _ in 0..rng.below(12) {
        let at = places[rng.below(places.len())].join(names[rng.below(names.len())]);
        if fs::symlink_metadata(&at).is_ok() {
            continue;
        }
        match rng.below(7) {
            0 => {
                if fs::create_dir(&at).is_ok() {
                    places.push(at);
                }
            }
            1 => fs::write(&at, b"x").unwrap(),
            2 => std::os::unix::fs::symlink(&out, &at).unwrap(),
            3 => std::os::unix::fs::symlink(out.join("f"), &at).unwrap(),
            4 => std::os::unix::fs::symlink("nothing", &at).unwrap(),
            5 => mkfifo(&at),
            _ => std::os::unix::fs::symlink(&probe, &at).unwrap(),
        }
    }
    // Old times on the probe-like names, so the sweep takes them.
    for name in ["git-probe", "git-probe-0123456789abcdef"] {
        let p = state.join(name);
        if fs::metadata(&p).is_ok_and(|m| m.is_dir() || m.is_file()) {
            let at = std::time::UNIX_EPOCH + Duration::from_secs(1_000_000_000);
            if let Ok(f) = fs::File::open(&p) {
                let _ = f.set_times(fs::FileTimes::new().set_modified(at));
            }
        }
    }
    for _ in 0..rng.below(3) {
        let at = &places[rng.below(places.len())];
        if at != &state {
            let _ = fs::set_permissions(at, fs::Permissions::from_mode(0o500));
        }
    }
}

/// What the sweep may remove: entries directly in `state` of a probe's
/// other name, and what they hold.
fn sweepable(state: &Path, path: &Path) -> bool {
    let Ok(rest) = path.strip_prefix(state) else {
        return false;
    };
    let first = rest
        .components()
        .next()
        .unwrap()
        .as_os_str()
        .as_encoded_bytes();
    is_probe_name(first) && first != probe_folder().as_bytes()
}

/// What the clear may remove: entries in the probe's listed folders, and
/// what they hold.
fn clearable(probe: &Path, path: &Path) -> bool {
    PROBE_LISTED.iter().any(|f| {
        let folder = if f.is_empty() {
            probe.to_path_buf()
        } else {
            probe.join(f)
        };
        path.strip_prefix(&folder)
            .is_ok_and(|r| r.components().count() >= 1)
    })
}

#[test]
fn random_trees_stay_inside_the_state_directory() {
    let base = scratch();
    let mut rng = Rng(0x9e37_79b9_7f4a_7c15);
    for i in 0..300 {
        for op in 0..5 {
            let root = base.0.join(format!("{i}-{op}"));
            fs::create_dir(&root).unwrap();
            let seed = rng.next();
            random_tree(&mut Rng(seed | 1), &root);
            let state = root.join("state");
            let probe = state.join(probe_folder());
            let text = state.to_str().unwrap().to_owned();
            let p = place(&text);
            let outside = listing(&root.join("out"));
            let before = listing(&state);
            match op {
                0 => {
                    let _ = p.sweep_probes(NOW);
                }
                1 => {
                    let _ = p.clear_probe(probe.to_str().unwrap().as_bytes());
                }
                2 => {
                    let _ = p.probe_holds(probe.to_str().unwrap().as_bytes());
                }
                3 => {
                    let _ = p.ask_folder();
                }
                _ => {
                    let _ = p.probe_repository(NOW);
                }
            }
            let after = listing(&state);
            let gone: Vec<&PathBuf> = before
                .iter()
                .map(|e| &e.0)
                .filter(|q| !after.iter().any(|a| &a.0 == *q))
                .collect();
            for q in &gone {
                let allowed = match op {
                    0 => sweepable(&state, q),
                    1 => clearable(&probe, q),
                    4 => sweepable(&state, q) || clearable(&probe, q),
                    _ => false,
                };
                assert!(allowed, "seed {seed} op {op}: removed {}", q.display());
            }
            if op != 4 && op != 3 {
                // Only the build and the ask folder write; neither of
                // them removes outside, and neither do the others write.
                assert_eq!(listing(&root.join("out")), outside, "seed {seed} op {op}");
            } else {
                // Nothing outside the state directory is removed.
                let now = listing(&root.join("out"));
                for (q, _) in &outside {
                    assert!(
                        now.iter().any(|n| &n.0 == q),
                        "seed {seed} op {op}: removed {} outside",
                        q.display()
                    );
                }
            }
        }
    }
}

#[test]
fn a_probe_file_that_is_a_fifo_is_replaced_without_waiting() {
    let base = scratch();
    for name in ["HEAD", "remotes/p", "compat-differ/config"] {
        let root = base.0.join(name.replace('/', "_"));
        let state = root.join("state");
        let probe = state.join(probe_folder());
        fs::create_dir_all(&probe).unwrap();
        for folder in PROBE_FOLDERS {
            fs::create_dir(probe.join(folder)).unwrap();
        }
        for (n, data) in PROBE_FILES {
            if n != name {
                fs::write(probe.join(n), data).unwrap();
            }
        }
        mkfifo(&probe.join(name));
        let text = state.to_str().unwrap().to_owned();
        let p = place(&text);
        assert_eq!(p.probe_holds(probe.to_str().unwrap().as_bytes()), Ok(false));
        let started = Instant::now();
        let (real, _) = p.probe_repository(NOW).unwrap().expect("built");
        assert!(started.elapsed() < Duration::from_secs(5));
        assert_eq!(p.probe_holds(&real), Ok(true));
        assert!(fs::symlink_metadata(probe.join(name)).unwrap().is_file());
    }
}

/// The time of one `run` over a tree `make` builds with `n` entries, the
/// tree made again for each run and its making not timed. By the wall
/// clock (`timing::wall`): each run makes, lists and removes files on
/// disk, and time spent waiting on the file system is no time of the
/// thread's own.
fn timed(
    n: usize,
    make: &dyn Fn(&Path, usize),
    run: &dyn Fn(&Place<'_, StdFs>, &Path),
) -> Duration {
    let base = scratch();
    let state = base.0.join("state");
    make(&state, n);
    let text = state.to_str().unwrap().to_owned();
    let p = place(&text);
    timing::wall(|| run(&p, &state))
}

fn built(state: &Path) -> PathBuf {
    let probe = state.join(probe_folder());
    fs::create_dir_all(&probe).unwrap();
    for folder in PROBE_FOLDERS {
        fs::create_dir(probe.join(folder)).unwrap();
    }
    for (n, data) in PROBE_FILES {
        fs::write(probe.join(n), data).unwrap();
    }
    probe
}

fn linear(name: &str, make: &dyn Fn(&Path, usize), run: &dyn Fn(&Place<'_, StdFs>, &Path)) {
    timing::grows_within(
        &format!("{name}: 1,000 and 10,000 entries"),
        timing::TENFOLD,
        &mut || timed(1_000, make, run),
        &mut || timed(10_000, make, run),
    );
}

#[test]
fn every_walk_is_linear() {
    let extras = |state: &Path, n: usize| {
        let probe = built(state);
        for i in 0..n {
            fs::write(probe.join(format!("e{i}")), b"").unwrap();
        }
    };
    linear("clear", &extras, &|p, state| {
        let probe = state.join(probe_folder());
        p.clear_probe(probe.to_str().unwrap().as_bytes()).unwrap();
    });
    linear("holds", &extras, &|p, state| {
        let probe = state.join(probe_folder());
        assert_eq!(p.probe_holds(probe.to_str().unwrap().as_bytes()), Ok(false));
    });
    linear("build", &extras, &|p, _| {
        assert!(p.probe_repository(NOW).unwrap().is_some());
    });
    linear(
        "sweep",
        &|state: &Path, n: usize| {
            fs::create_dir_all(state).unwrap();
            for i in 0..n {
                fs::write(state.join(format!("git-probe-{i:016x}")), b"").unwrap();
            }
        },
        &|p, _| p.sweep_probes(NOW).unwrap(),
    );
}

/// A removal that fails partway (a folder in it that cannot be emptied)
/// leaves everything outside the folder being removed as it was: which
/// of that folder's own entries remain is not pinned (the README).
#[test]
fn a_removal_failing_partway_stays_inside_its_folder() {
    let base = scratch();
    let state = base.0.join("state");
    let out = base.0.join("out");
    fs::create_dir_all(&state).unwrap();
    fs::create_dir_all(out.join("d")).unwrap();
    fs::write(out.join("f"), b"outside").unwrap();
    fs::write(out.join("d/g"), b"inside").unwrap();
    // An old probe of another name holding several entries, one a folder
    // that cannot be emptied, and links out.
    let old = state.join("git-probe-0123456789abcdef");
    for name in ["a", "m", "z"] {
        fs::create_dir_all(old.join(name).join("sub")).unwrap();
        fs::write(old.join(name).join("sub/f"), b"x").unwrap();
        std::os::unix::fs::symlink(&out, old.join(name).join("out")).unwrap();
        std::os::unix::fs::symlink(out.join("f"), old.join(name).join("f")).unwrap();
    }
    fs::set_permissions(old.join("m/sub"), fs::Permissions::from_mode(0o500)).unwrap();
    // The probe and its siblings, which must stay.
    let probe = state.join(probe_folder());
    fs::create_dir_all(probe.join("remotes")).unwrap();
    fs::write(probe.join("remotes/x"), b"x").unwrap();
    fs::write(state.join("git-ask"), b"x").unwrap();
    let at = std::time::UNIX_EPOCH + Duration::from_secs(1_000_000_000);
    fs::File::open(&old)
        .unwrap()
        .set_times(fs::FileTimes::new().set_modified(at))
        .unwrap();
    let outside_old = |l: Vec<(PathBuf, Option<Vec<u8>>)>| {
        l.into_iter()
            .filter(|e| !e.0.starts_with(&old))
            .collect::<Vec<_>>()
    };
    let before = outside_old(listing(&base.0));
    let text = state.to_str().unwrap().to_owned();
    place(&text).sweep_probes(NOW).unwrap();
    assert!(old.exists(), "the folder that could not be emptied stays");
    assert_eq!(outside_old(listing(&base.0)), before);
    // The same under the clear: an extra folder in the probe.
    let extra = probe.join("x");
    for name in ["a", "m", "z"] {
        fs::create_dir_all(extra.join(name).join("sub")).unwrap();
        fs::write(extra.join(name).join("sub/f"), b"x").unwrap();
        std::os::unix::fs::symlink(&out, extra.join(name).join("out")).unwrap();
    }
    fs::set_permissions(extra.join("m/sub"), fs::Permissions::from_mode(0o500)).unwrap();
    let outside_extra = |l: Vec<(PathBuf, Option<Vec<u8>>)>| {
        l.into_iter()
            .filter(|e| !e.0.starts_with(&extra))
            .collect::<Vec<_>>()
    };
    let before = outside_extra(listing(&base.0));
    let probe_text = probe.to_str().unwrap().to_owned();
    assert!(place(&text).clear_probe(probe_text.as_bytes()).is_err());
    assert!(extra.exists());
    let after = outside_extra(listing(&base.0));
    // The clear may have removed the probe's other extras listed before
    // `x`; nothing outside the probe changes.
    let outside_probe = |l: &[(PathBuf, Option<Vec<u8>>)]| {
        l.iter()
            .filter(|e| !e.0.starts_with(&probe))
            .cloned()
            .collect::<Vec<_>>()
    };
    assert_eq!(outside_probe(&after), outside_probe(&before));
}

#[test]
fn fifos_in_and_around_the_probe_are_never_waited_on() {
    let base = scratch();
    let state = base.0.join("state");
    let out = base.0.join("out");
    fs::create_dir_all(&out).unwrap();
    let probe = state.join(probe_folder());
    fs::create_dir_all(&probe).unwrap();
    for folder in PROBE_FOLDERS {
        fs::create_dir(probe.join(folder)).unwrap();
    }
    for (n, data) in PROBE_FILES {
        if n != "HEAD" && n != "config" {
            fs::write(probe.join(n), data).unwrap();
        }
    }
    // A FIFO at HEAD, a link from config to a FIFO outside, a FIFO in
    // remotes.
    mkfifo(&probe.join("HEAD"));
    mkfifo(&out.join("fifo"));
    std::os::unix::fs::symlink(out.join("fifo"), probe.join("config")).unwrap();
    mkfifo(&probe.join("remotes/q"));
    let text = state.to_str().unwrap().to_owned();
    let started = Instant::now();
    let (real, _) = place(&text).probe_repository(NOW).unwrap().expect("built");
    assert!(started.elapsed() < Duration::from_secs(5));
    assert_eq!(place(&text).probe_holds(&real), Ok(true));
    for name in ["HEAD", "config"] {
        assert!(fs::symlink_metadata(probe.join(name)).unwrap().is_file());
    }
    assert!(fs::symlink_metadata(probe.join("remotes/q")).is_err());
    use std::os::unix::fs::FileTypeExt;
    let fifo = fs::symlink_metadata(out.join("fifo")).unwrap();
    assert!(fifo.file_type().is_fifo(), "the FIFO outside is untouched");
}
