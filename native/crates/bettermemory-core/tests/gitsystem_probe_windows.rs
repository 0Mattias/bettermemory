//! Under the Windows flavour, which is not ported, the probe's four
//! functions that make, write or remove (`ask_folder`, `probe_repository`,
//! `sweep_probes`, `clear_probe`) decline before touching anything: on
//! any host, a scratch folder holding what each would change comes out
//! unchanged. `probe_name` and `probe_folder` answer under any flavour.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, UNIX_EPOCH};

use bettermemory_core::fsdata::{Flavour, StdFs};
use bettermemory_core::gitconfig::legacy::NotPorted;
use bettermemory_core::gitsystem::probe::{probe_folder, Refusal};
use bettermemory_core::gitsystem::record::Place;

struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn scratch() -> Scratch {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("bm-gitsysprobe-win-{}-{n}", std::process::id()));
    fs::create_dir(&dir).expect("a new scratch directory");
    Scratch(dir)
}

/// Every path under `dir` with its kind, size and modification time.
fn listing(dir: &Path) -> Vec<(PathBuf, bool, u64, std::time::SystemTime)> {
    let mut out = Vec::new();
    let mut stack = vec![dir.to_path_buf()];
    while let Some(d) = stack.pop() {
        for e in fs::read_dir(&d).unwrap() {
            let p = e.unwrap().path();
            let m = fs::symlink_metadata(&p).unwrap();
            if m.is_dir() {
                stack.push(p.clone());
            }
            out.push((p, m.is_dir(), m.len(), m.modified().unwrap()));
        }
    }
    out.sort();
    out
}

#[test]
fn the_windows_flavour_declines_before_touching_anything() {
    let base = scratch();
    let state = base.0.join("state");
    let probe = state.join(probe_folder());
    fs::create_dir_all(probe.join("remotes")).unwrap();
    fs::write(probe.join("extra"), b"x").unwrap();
    fs::write(probe.join("remotes").join("q"), b"x").unwrap();
    // An old probe of another name, which a sweep would remove.
    let old = state.join("git-probe-0123456789abcdef");
    fs::write(&old, b"x").unwrap();
    let at = UNIX_EPOCH + Duration::from_secs(1_000_000_000);
    fs::OpenOptions::new()
        .write(true)
        .open(&old)
        .unwrap()
        .set_times(fs::FileTimes::new().set_modified(at))
        .unwrap();
    let before = listing(&base.0);
    let text = state.to_str().unwrap().to_owned();
    let place = Place {
        fs: &StdFs,
        flavour: Flavour::Windows,
        state_dir: text.as_bytes(),
        cwd: b"C:\\",
    };
    let now = 2_000_000_000.0;
    assert_eq!(place.ask_folder(), Err(NotPorted));
    assert_eq!(place.probe_repository(now), Err(NotPorted));
    assert_eq!(place.sweep_probes(now), Err(Refusal::NotPorted));
    assert_eq!(
        place.clear_probe(probe.to_str().unwrap().as_bytes()),
        Err(Refusal::NotPorted)
    );
    assert_eq!(listing(&base.0), before, "the scratch folder is unchanged");
    assert!(probe_folder().starts_with("git-probe-"));
}
