//! A native writing open killed anywhere leaves a store both builds open
//! and verify ok, holding every record it held, and a further writing
//! open succeeds.
//!
//! Three starting states that make the open write: `intact` (the key
//! ring to bind, `engine_version` to record, the digest behind by every
//! record kind but a restore), `fts` with no digest (a build from 18
//! records), and `intact` in rollback-journal mode, bound (the switch to
//! WAL, then the digest). The test binary runs itself as the opening child
//! (`open_kill_child`, ignored when run alone), which ends at each
//! [`Boundary`] the state passes, by `abort` and by `exit` (no destructors
//! either way); on top, children slowed a millisecond per boundary are
//! killed from outside at fixed pseudo-random delays. After each kill: a
//! copy is kept for the reference, the native check verifies the store ok
//! and counts its records, and a native writing open of it succeeds and
//! verifies ok. The copies are then opened for writing, verified and
//! counted by the reference in one child process, when
//! `BM_REFERENCE_PYTHON` imports `bettermemory` (else set aside, said in a
//! PARITY line). What each boundary left is printed.

#![cfg(unix)]

mod common;

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use bettermemory_core::log::KeyRing;
use bettermemory_core::store::keys::load_key_ring;
use bettermemory_core::store::open::{open_for_writing, open_for_writing_with, Boundary};
use bettermemory_core::store::{StoreError, StoreFile};
use common::created::{reference_python, run_reference};
use common::opened;
use common::parity::record;
use common::stores::scratch;
use serde_json::{json, Value};

const ROOT_ENV: &str = "BM_TEST_OPEN_ROOT";
const AT_ENV: &str = "BM_TEST_OPEN_KILL_AT";
const HOW_ENV: &str = "BM_TEST_OPEN_KILL_HOW";

/// The states: a name, a base, a recipe.
fn states() -> Vec<(&'static str, &'static str, Vec<Value>)> {
    let bind = json!({"bind": "memory.sqlite"});
    vec![
        ("intact", "intact", vec![]),
        (
            "fts_no_digest",
            "fts",
            vec![
                json!({"sql": "DROP TABLE digest_records; DROP TABLE digest_tokens; \
                               DROP TABLE digest_state;"}),
                bind.clone(),
            ],
        ),
        (
            "rollback_journal",
            "intact",
            vec![json!({"sql": "PRAGMA journal_mode = DELETE;"}), bind],
        ),
    ]
}

fn index(b: Boundary) -> usize {
    assert_eq!(
        Boundary::ALL.len(),
        18,
        "a boundary added: name what a kill there leaves"
    );
    Boundary::ALL
        .iter()
        .position(|x| *x == b)
        .expect("boundary")
}

/// The opening child: a writing open of `$ROOT/c`, ended at boundary `$AT`
/// by `$HOW` (abort, exit), or slowed (`slow`) for a kill from outside.
/// Writes each boundary passed to `$ROOT/progress`.
#[test]
#[ignore = "run by the kill test as its child"]
fn open_kill_child() {
    let Some(root) = std::env::var_os(ROOT_ENV).map(PathBuf::from) else {
        return;
    };
    let at: Option<usize> = std::env::var(AT_ENV).ok().and_then(|v| v.parse().ok());
    let how = std::env::var(HOW_ENV).unwrap_or_default();
    let progress = root.join("progress");
    let mut hook = |b: Boundary| {
        let i = index(b);
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
    let (header, _) = opened::cases();
    let c = root.join("c");
    let store = open_for_writing_with(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::provider(&header),
        &mut hook,
    )
    .expect("opened");
    store.close().expect("closed");
    hook(Boundary::Closed);
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

/// The native check: verifies ok; the number of active records.
fn native_ok(c: &Path, id: &str) -> usize {
    let file = StoreFile::open(&c.join("memory.sqlite")).expect("the store opens natively");
    let report = file
        .log_verify_loading(&mut loader(c.join("keys"), id.to_owned()))
        .expect("native verify");
    assert_eq!(report.status, "ok", "{report:?}");
    let mut n = 0;
    file.each_memory(&mut |_| n += 1).expect("memories");
    n
}

fn set_up(root: &Path, base: &str, recipe: &[Value], id: &str) {
    let c = root.join("c");
    opened::made(base, &c);
    opened::setup(recipe, &c, id);
}

/// What a kill left: the store directory's names, the keys directory's,
/// the journal mode, and the digest's state keys.
fn left(c: &Path, id: &str) -> String {
    let names = |d: &Path| {
        let mut v: Vec<String> = fs::read_dir(d)
            .map(|r| {
                r.flatten()
                    .map(|e| e.file_name().to_string_lossy().replace(id, "<id>"))
                    .collect()
            })
            .unwrap_or_default();
        v.sort();
        v.join(" ")
    };
    let dir = names(c).replace("keys", "").trim().to_owned();
    let ring = names(&c.join("keys"));
    // Read-write, creating nothing: a kill inside a transaction on a
    // rollback-journal store leaves a hot journal, which the next
    // connection that may write rolls back, and a read-only one cannot
    // (SQLITE_READONLY_ROLLBACK; the macOS runner's kill landed there once,
    // CI run on 908635ec).
    let conn = rusqlite::Connection::open_with_flags(
        c.join("memory.sqlite"),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
    )
    .expect("open");
    let mode: String = conn
        .query_row("PRAGMA journal_mode", [], |r| r.get(0))
        .expect("mode");
    let digest: String = conn
        .query_row(
            "SELECT group_concat(key, ',') FROM (SELECT key FROM digest_state ORDER BY key)",
            [],
            |r| r.get::<_, Option<String>>(0),
        )
        .map(|v| v.unwrap_or_default())
        .unwrap_or_else(|_| "no digest".to_owned());
    format!("dir [{dir}]; keys [{ring}]; {mode}; digest_state [{digest}]")
}

/// A copy of the case for the reference, its binding pointed at the copy.
fn keep_copy(c: &Path, to: &Path, id: &str) {
    fs::create_dir_all(to.join("keys")).expect("copy dir");
    for entry in fs::read_dir(c).expect("case").flatten() {
        if entry.file_type().expect("type").is_file() {
            fs::copy(entry.path(), to.join(entry.file_name())).expect("copy");
        }
    }
    for entry in fs::read_dir(c.join("keys")).expect("keys").flatten() {
        fs::copy(entry.path(), to.join("keys").join(entry.file_name())).expect("copy key");
    }
    let binding = to.join("keys").join(format!("{id}.path"));
    if let Ok(text) = fs::read_to_string(&binding) {
        let from = fs::canonicalize(c).expect("c").display().to_string();
        let into = fs::canonicalize(to).expect("to").display().to_string();
        fs::write(&binding, text.replace(&from, &into)).expect("rebind");
    }
}

type Job = (usize, Option<usize>, &'static str, Option<u64>);

struct Outcome {
    state: usize,
    at: Option<usize>,
    reached: Option<usize>,
    left: String,
    copy: PathBuf,
    want: usize,
}

fn one_kill(root: &Path, job: Job, id: &str) -> Outcome {
    let (state, at, how, delay) = job;
    let (_, base, recipe) = &states()[state];
    set_up(root, base, recipe, id);
    let c = root.join("c");
    let want = native_ok(&c, id);
    let mut cmd = Command::new(std::env::current_exe().expect("exe"));
    cmd.args([
        "--exact",
        "open_kill_child",
        "--ignored",
        "--nocapture",
        "--test-threads=1",
    ])
    .env(ROOT_ENV, root)
    .env(HOW_ENV, how)
    .stdout(std::process::Stdio::null())
    .stderr(std::process::Stdio::null());
    if let Some(at) = at {
        cmd.env(AT_ENV, at.to_string());
    }
    let mut child = cmd.spawn().expect("child");
    if let Some(us) = delay {
        std::thread::sleep(Duration::from_micros(us));
        let _ = child.kill();
    }
    let _ = child.wait();
    let reached = fs::read_to_string(root.join("progress"))
        .ok()
        .and_then(|t| t.parse().ok());
    let left = left(&c, id);
    let copy = root.join("copy");
    keep_copy(&c, &copy, id);
    assert_eq!(native_ok(&c, id), want, "records after the kill");
    let (header, _) = opened::cases();
    let again = open_for_writing(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::provider(&header),
    )
    .unwrap_or_else(|e| panic!("a further writing open after {left}: {} {e}", e.class()));
    again.close().expect("close");
    assert_eq!(native_ok(&c, id), want, "records after a further open");
    Outcome {
        state,
        at,
        reached,
        left,
        copy,
        want,
    }
}

/// The boundaries an unkilled open of each state passes.
fn reached_by(state: usize, base: &Path, id: &str) -> Vec<usize> {
    let root = base.join(format!("dry{state}"));
    fs::create_dir(&root).expect("dry");
    let (_, b, recipe) = &states()[state];
    set_up(&root, b, recipe, id);
    let c = root.join("c");
    let (header, _) = opened::cases();
    let mut seen = Vec::new();
    open_for_writing_with(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &opened::provider(&header),
        &mut |b| {
            let i = index(b);
            if !seen.contains(&i) {
                seen.push(i);
            }
        },
    )
    .expect("dry open")
    .close()
    .expect("close");
    seen.push(index(Boundary::Closed));
    seen
}

const REFERENCE: &str = r#"
import json, sys
from bettermemory.store import Store
out = []
args = sys.argv[1:]
for i in range(0, len(args), 2):
    s = Store.open(args[i], keys_dir=args[i + 1], allow_rekey=True)
    try:
        out.append([s.log_verify()["status"], sum(1 for _ in s.iter_memories())])
    finally:
        s.close()
print(json.dumps(out))
"#;

#[test]
fn a_killed_writing_open_leaves_a_whole_store() {
    let dir = scratch("open-kills");
    let id = common::stores::store_id();
    let mut jobs: Vec<Job> = Vec::new();
    for state in 0..states().len() {
        for at in reached_by(state, &dir.0, &id) {
            for how in ["abort", "exit"] {
                jobs.push((state, Some(at), how, None));
            }
        }
    }
    let mut seed: u64 = 0x9e37_79b9_7f4a_7c15;
    for k in 0..60 {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        jobs.push((k % states().len(), None, "slow", Some(seed % 40_000)));
    }
    let jobs: Vec<(usize, Job)> = jobs.into_iter().enumerate().collect();
    let outcomes: Vec<Outcome> = std::thread::scope(|scope| {
        let workers: Vec<_> = jobs
            .chunks(jobs.len().div_ceil(4))
            .map(|chunk| {
                let base = &dir.0;
                let id = &id;
                scope.spawn(move || {
                    chunk
                        .iter()
                        .map(|&(k, job)| {
                            let root = base.join(format!("k{k}"));
                            fs::create_dir(&root).expect("root");
                            one_kill(&root, job, id)
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
    let mut table: BTreeMap<String, BTreeMap<String, usize>> = BTreeMap::new();
    for o in &outcomes {
        let name = states()[o.state].0;
        let label = match (o.at, o.reached) {
            (Some(i), reached) => {
                assert_eq!(reached, Some(i), "the child stopped elsewhere than {i}");
                format!("{name} {i:02} after {:?}", Boundary::ALL[i])
            }
            (None, Some(r)) => format!("{name} {r:02} killed after {:?}", Boundary::ALL[r]),
            (None, None) => format!("{name} killed before any boundary"),
        };
        *table
            .entry(label)
            .or_default()
            .entry(o.left.clone())
            .or_default() += 1;
    }
    for (label, lefts) in &table {
        for (left, count) in lefts {
            eprintln!("{label} x{count}: {left}");
        }
    }
    eprintln!("{} kills", outcomes.len());
    assert!(outcomes.len() >= 100);
    match reference_python() {
        Err(why) => {
            eprintln!("NOT RUN the reference's half: {why}");
            record(
                "a_killed_writing_open_leaves_a_whole_store",
                "reference Python",
                "i",
                0,
                &["the reference's writing open and verify".to_owned()],
                &[why],
            );
        }
        Ok(python) => {
            let args: Vec<PathBuf> = outcomes
                .iter()
                .flat_map(|o| [o.copy.join("memory.sqlite"), o.copy.join("keys")])
                .collect();
            let refs: Vec<&Path> = args.iter().map(PathBuf::as_path).collect();
            let out = run_reference(&python, &dir.0.join("home"), REFERENCE, &refs);
            assert!(
                out.status.success(),
                "{}",
                String::from_utf8_lossy(&out.stderr)
            );
            let said: Vec<(String, usize)> =
                serde_json::from_slice(out.stdout.trim_ascii()).expect("JSON");
            assert_eq!(said.len(), outcomes.len());
            for (o, (status, n)) in outcomes.iter().zip(&said) {
                assert_eq!(status, "ok", "{}", o.left);
                assert_eq!(*n, o.want, "{}", o.left);
            }
            eprintln!("the reference opened and verified {} stores ok", said.len());
            record(
                "a_killed_writing_open_leaves_a_whole_store",
                "reference Python",
                "i",
                said.len(),
                &[],
                &[],
            );
        }
    }
}

/// A refusal of meta sets `_connect`'s owner-only modes only where the
/// reference's do: on a WAL store, whose `PRAGMA journal_mode = WAL` is a
/// no-op the reference passes before its chmod. A rollback-journal store
/// that another connection is reading keeps its modes and its journal: the
/// reference's switch to WAL needs the exclusive lock and fails the open
/// ("database is locked") before the chmod.
#[test]
fn a_refusal_sets_the_modes_only_past_the_wal_switch() {
    use std::os::unix::fs::PermissionsExt;
    let id = common::stores::store_id();
    for (name, journal, want) in [("wal", "WAL", 0o600), ("delete", "DELETE", 0o644)] {
        let dir = scratch(&format!("open-refused-modes-{name}"));
        let c = dir.0.join("c");
        opened::made("intact", &c);
        opened::setup(
            &[
                json!({"bind": "memory.sqlite"}),
                json!({"sql": format!(
                    "PRAGMA journal_mode = {journal}; \
                     UPDATE meta SET value = 'NOT-AN-ID' WHERE key = 'store_id';"
                )}),
            ],
            &c,
            &id,
        );
        let file = c.join("memory.sqlite");
        fs::set_permissions(&file, fs::Permissions::from_mode(0o644)).expect("chmod");
        let reader = rusqlite::Connection::open(&file).expect("reader");
        reader
            .execute_batch("BEGIN; SELECT count(*) FROM meta;")
            .expect("a read lock");
        let got = open_for_writing(&file, &c.join("keys"), &opened::engine_provider());
        assert_eq!(
            got.as_ref().map(|_| ()).map_err(|e| e.class().to_owned()),
            Err("ValueError".to_owned()),
            "{name}"
        );
        drop(got);
        reader.execute_batch("ROLLBACK").expect("rollback");
        let mode = fs::metadata(&file).expect("stat").permissions().mode() & 0o777;
        assert_eq!(mode, want, "{name}: mode {mode:o}");
        let mode_now: String = reader
            .query_row("PRAGMA journal_mode", [], |r| r.get(0))
            .expect("journal");
        assert_eq!(mode_now.to_uppercase(), journal, "{name}");
    }
}
