//! A native create killed anywhere leaves no store at the path (and a
//! fresh create there succeeds and verifies ok) or a store that opens and
//! verifies ok, on both builds; never anything else.
//!
//! The test binary runs itself as the creating child (`kill_child`, ignored
//! when run alone): at each [`Boundary`] it ends with `abort` or with
//! `exit` (no destructors either way), twice each, each run with stale
//! junk `-wal`, `-shm` and `-journal` at the store's name, and on top a child
//! is killed (`Child::kill`) at pseudo-random times (a fresh create
//! follows the first such kill at each boundary) while a hook slows each
//! step by a millisecond. Every kill is checked natively at once; the
//! stores are then checked by the reference in one child process, when
//! `BM_REFERENCE_PYTHON` imports `bettermemory` (else that half is set
//! aside, said in a PARITY line). What each boundary left is printed.

mod common;

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use bettermemory_core::log::KeyRing;
use bettermemory_core::store::create::{self, Boundary, Fixed};
use bettermemory_core::store::keys::load_key_ring;
use bettermemory_core::store::{StoreError, StoreFile};
use common::created::{fixed, fixture, reference_python, run_reference};
use common::parity::record;
use common::stores::scratch;

const ROOT_ENV: &str = "BM_TEST_CREATE_ROOT";
const AT_ENV: &str = "BM_TEST_CREATE_KILL_AT";
const HOW_ENV: &str = "BM_TEST_CREATE_KILL_HOW";
const ID_ENV: &str = "BM_TEST_CREATE_STORE_ID";

fn provider(id: &str) -> Fixed {
    let mut p = fixed(&fixture());
    p.store_id = id.to_owned();
    p.clock = vec![create::system_now()];
    p
}

/// The creating child: `create` in `$ROOT/store` with keys in `$ROOT/keys`,
/// ended at boundary `$AT` by `$HOW` (abort, exit), or slowed (`slow`) for
/// a kill from outside. Writes each boundary passed to `$ROOT/progress`.
#[test]
#[ignore = "run by the kill test as its child"]
fn kill_child() {
    let Some(root) = std::env::var_os(ROOT_ENV).map(PathBuf::from) else {
        return;
    };
    let at: Option<usize> = std::env::var(AT_ENV).ok().and_then(|v| v.parse().ok());
    let how = std::env::var(HOW_ENV).unwrap_or_default();
    let id = std::env::var(ID_ENV).expect("id");
    let progress = root.join("progress");
    let mut hook = |b: Boundary| {
        let i = Boundary::ALL
            .iter()
            .position(|x| *x == b)
            .expect("boundary");
        let _ = fs::write(&progress, i.to_string());
        if how == "slow" {
            std::thread::sleep(Duration::from_millis(1));
        }
        if at == Some(i) {
            if how == "abort" {
                std::process::abort();
            }
            std::process::exit(9);
        }
    };
    let made = create::create_with(
        &root.join("store"),
        &root.join("keys"),
        &mut provider(&id),
        &mut hook,
    );
    made.expect("created");
}

fn loader(
    keys: PathBuf,
    id: String,
) -> impl FnMut(&StoreFile) -> Result<(KeyRing, String), StoreError> {
    move |s: &StoreFile| {
        let fp = s
            .meta()?
            .into_iter()
            .find(|(k, _)| k == "key_fingerprint")
            .map(|(_, v)| v)
            .unwrap_or_default();
        let ring = load_key_ring(&keys, &id).map_err(|e| StoreError::Refused(format!("{e:?}")))?;
        Ok((ring, fp))
    }
}

fn native_ok(store: &Path, keys: &Path, id: &str) {
    let file = StoreFile::open(store).expect("the store opens natively");
    let report = file
        .log_verify_loading(&mut loader(keys.to_path_buf(), id.to_owned()))
        .expect("native verify");
    assert_eq!(report.status, "ok", "{report:?}");
}

fn names(dir: &Path) -> Vec<String> {
    let mut out: Vec<String> = fs::read_dir(dir)
        .map(|r| {
            r.flatten()
                .map(|e| {
                    let n = e.file_name().to_string_lossy().into_owned();
                    // Names with a pid or a store id in them, by kind.
                    if n.starts_with(".memory.sqlite.creating-") {
                        if n.ends_with(".lock") {
                            "<temp>.lock".to_owned()
                        } else {
                            "<temp>".to_owned()
                        }
                    } else if n.len() > 32 && n.as_bytes()[32] == b'.' {
                        format!("<id>{}", &n[32..])
                    } else {
                        n
                    }
                })
                .collect()
        })
        .unwrap_or_default();
    out.sort();
    out
}

/// A kill to make: the boundary to end at, how, the delay before a kill
/// from outside.
type Job<'a> = (Option<usize>, &'a str, Option<u64>);

/// One kill: what it left, and whether a store stood at the path after.
struct Outcome {
    reached: Option<usize>,
    store: bool,
    left: String,
    /// The store files the reference must verify, with their keys and ids.
    check: Vec<(PathBuf, PathBuf, String)>,
}

/// The boundaries a random kill left no store at and a fresh create
/// already followed: later ones there only check that no store stands.
static FRESHENED: std::sync::Mutex<Vec<Option<usize>>> = std::sync::Mutex::new(Vec::new());

fn one_kill(root: &Path, at: Option<usize>, how: &str, n: usize, delay: Option<u64>) -> Outcome {
    let id = format!("{:032x}", 0xabc0_0000_u64 + n as u64);
    // Stale sidecars at the store's name in every run: a create must
    // remove them before its link, wherever it is killed.
    fs::create_dir_all(root.join("store")).expect("store dir");
    for suffix in ["-wal", "-shm", "-journal"] {
        fs::write(root.join(format!("store/memory.sqlite{suffix}")), b"junk").expect("junk");
    }
    let mut cmd = Command::new(std::env::current_exe().expect("exe"));
    cmd.args([
        "--exact",
        "kill_child",
        "--ignored",
        "--nocapture",
        "--test-threads=1",
    ])
    .env(ROOT_ENV, root)
    .env(HOW_ENV, how)
    .env(ID_ENV, &id)
    .stdout(std::process::Stdio::null())
    .stderr(std::process::Stdio::null());
    if let Some(at) = at {
        cmd.env(AT_ENV, at.to_string());
    }
    let mut child = cmd.spawn().expect("child");
    if let Some(ms) = delay {
        std::thread::sleep(Duration::from_micros(ms));
        let _ = child.kill();
    }
    let _ = child.wait();
    let reached = fs::read_to_string(root.join("progress"))
        .ok()
        .and_then(|t| t.parse().ok());
    let store = root.join("store");
    let keys = root.join("keys");
    let file = store.join("memory.sqlite");
    let left = format!("store: {:?}; keys: {:?}", names(&store), names(&keys));
    let mut check = Vec::new();
    let stood = fs::symlink_metadata(&file).is_ok();
    if stood {
        native_ok(&file, &keys, &id);
        check.push((file, keys, id));
    } else if at.is_none() && {
        let mut seen = FRESHENED.lock().expect("lock");
        let again = seen.contains(&reached);
        seen.push(reached);
        again
    } {
        // A random kill at a boundary a fresh create already followed.
    } else {
        // No store: a fresh create at the same paths succeeds, sweeps the
        // dead create's temporary files and verifies ok.
        let fresh = format!("{:032x}", 0xfee0_0000_u64 + n as u64);
        let made = create::create(&store, &keys, &mut provider(&fresh)).expect("a fresh create");
        native_ok(&made.path, &keys, &fresh);
        assert_eq!(
            names(&store),
            vec!["memory.sqlite".to_owned()],
            "the fresh create swept the dead one's files"
        );
        check.push((made.path, keys, fresh));
    }
    Outcome {
        reached,
        store: stood,
        left,
        check,
    }
}

const VERIFY: &str = r#"
import json, sys
from bettermemory.store import Store
out = []
args = sys.argv[1:]
for i in range(0, len(args), 2):
    s = Store.open(args[i], keys_dir=args[i + 1], read_only=True)
    try:
        out.append(s.log_verify()["status"])
    finally:
        s.close()
print(json.dumps(out))
"#;

#[test]
fn a_killed_create_leaves_no_store_or_a_whole_one() {
    let dir = scratch("kills");
    let mut n = 0;
    let mut table: BTreeMap<String, BTreeMap<String, usize>> = BTreeMap::new();
    let mut checks: Vec<(PathBuf, PathBuf, String)> = Vec::new();
    let mut per_boundary: BTreeMap<usize, usize> = BTreeMap::new();
    let mut tally = |o: Outcome, label: String| {
        let key = format!("{label}: {}", if o.store { "store" } else { "no store" });
        *table.entry(key).or_default().entry(o.left).or_default() += 1;
        if let Some(r) = o.reached {
            *per_boundary.entry(r).or_default() += 1;
        }
        checks.extend(o.check);
    };
    // Every job: (boundary to end at, how, delay before a kill from outside).
    let mut jobs: Vec<Job> = Vec::new();
    for i in 0..Boundary::ALL.len() {
        for how in ["abort", "exit"] {
            for _ in 0..2 {
                jobs.push((Some(i), how, None));
            }
        }
    }
    // Kills from outside at pseudo-random times (a fixed seed: the same
    // delays every run; where they land depends on the machine).
    let mut seed: u64 = 0x2545_f491_4f6c_dd1d;
    for _ in 0..48 {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        jobs.push((None, "slow", Some(seed % 60_000)));
    }
    let jobs: Vec<(usize, Job)> = jobs.into_iter().enumerate().collect();
    let outcomes: Vec<(usize, Option<usize>, Outcome)> = std::thread::scope(|scope| {
        let workers: Vec<_> = jobs
            .chunks(jobs.len().div_ceil(4))
            .map(|chunk| {
                let base = &dir.0;
                scope.spawn(move || {
                    chunk
                        .iter()
                        .map(|&(k, (at, how, delay))| {
                            let root = base.join(format!("k{k}"));
                            fs::create_dir(&root).expect("root");
                            (k, at, one_kill(&root, at, how, k, delay))
                        })
                        .collect::<Vec<_>>()
                })
            })
            .collect();
        workers
            .into_iter()
            .flat_map(|w| w.join().expect("worker"))
            .collect()
    });
    for (_, at, o) in outcomes {
        n += 1;
        let label = match (at, o.reached) {
            (Some(i), reached) => {
                assert_eq!(reached, Some(i), "the child stopped elsewhere than {i}");
                format!("{i:02} after {:?}", Boundary::ALL[i])
            }
            (None, Some(r)) => format!("{r:02} killed after {:?}", Boundary::ALL[r]),
            (None, None) => "killed before any boundary".to_owned(),
        };
        tally(o, label);
    }
    for (outcome, lefts) in &table {
        for (left, count) in lefts {
            eprintln!("{outcome} x{count}: {left}");
        }
    }
    eprintln!("{n} kills; boundaries reached by the killed children: {per_boundary:?}");
    assert!(n >= 100);
    match reference_python() {
        Err(why) => {
            eprintln!("NOT RUN the reference's half: {why}");
            record(
                "a_killed_create_leaves_no_store_or_a_whole_one",
                "reference Python",
                "i",
                0,
                &["the reference's verify".to_owned()],
                &[why],
            );
        }
        Ok(python) => {
            let args: Vec<&Path> = checks
                .iter()
                .flat_map(|(s, k, _)| [s.as_path(), k.as_path()])
                .collect();
            let out = run_reference(&python, &dir.0.join("home"), VERIFY, &args);
            assert!(
                out.status.success(),
                "{}",
                String::from_utf8_lossy(&out.stderr)
            );
            let said: Vec<String> = serde_json::from_slice(out.stdout.trim_ascii()).expect("JSON");
            assert_eq!(said.len(), checks.len());
            assert!(said.iter().all(|s| s == "ok"), "{said:?}");
            eprintln!("the reference verified {} stores ok", said.len());
            record(
                "a_killed_create_leaves_no_store_or_a_whole_one",
                "reference Python",
                "i",
                said.len(),
                &[],
                &[],
            );
        }
    }
}
