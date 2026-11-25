//! The owner's writing open killed in or around its upkeep leaves a store
//! that a further writing open settles to exactly what an open never
//! killed leaves: one `rekey` row closing the foreign tail, the anchor on
//! the first row of the earliest held segment naming its key, the digest
//! current.
//!
//! Four starting states that make the upkeep write: a foreign tail and
//! no anchor (`intact`), the same with no digest (so the digest is built
//! after the `rekey` row is committed), no anchor on a rekeyed store (the
//! anchor goes on seq 1 under the retired key), and an anchor that names
//! no key. Between them they pass every [`Boundary`]. The test binary
//! runs itself as the opening child (`upkeep_kill_child`, ignored when run
//! alone), ended at each boundary the state passes (and after the store
//! is closed), by `abort` and by `exit`. Both opens read the same fixed log clock, so
//! a `rekey` row the killed open committed and one the further open
//! appends carry the same stamp. What each boundary left is printed.
//!
//! Also here, the order the upkeep keeps: after the binding, so an open
//! that finds the ring bound elsewhere at the binding's locked re-read
//! closes no tail.

#![cfg(unix)]

mod common;

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use bettermemory_core::store::open::{
    open_for_writing_mut, open_for_writing_mut_with, Boundary, NotPorted, OpenError,
};
use common::opened;
use common::stores::scratch;
use common::written::{clock, instant};
use serde_json::{json, Value};

const ROOT_ENV: &str = "BM_TEST_UPKEEP_ROOT";
const AT_ENV: &str = "BM_TEST_UPKEEP_KILL_AT";
const HOW_ENV: &str = "BM_TEST_UPKEEP_KILL_HOW";

const TAIL: &str = "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) VALUES \
     (9, '2001-02-03T04:05:06.000000Z', NULL, 'rekey', \
     '{\"new_fingerprint\":\"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff\",\
     \"old_fingerprint\":\"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee\"}', \
     '27c628f1c20a3f30af2bdd631e3f6de27b0603c00e950a11c704336524f6a760', \
     'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc');";

/// The states: a name, a base, a recipe.
fn states() -> Vec<(&'static str, &'static str, Vec<Value>)> {
    let bind = json!({"bind": "memory.sqlite"});
    let no_origin = json!({"unlink": ".origin"});
    vec![
        (
            "tail_no_origin",
            "intact",
            vec![json!({ "sql": TAIL }), no_origin.clone(), bind.clone()],
        ),
        (
            "tail_no_origin_no_digest",
            "intact",
            vec![
                json!({ "sql": TAIL }),
                json!({"sql": "DROP TABLE digest_records; DROP TABLE digest_tokens; \
                               DROP TABLE digest_state;"}),
                no_origin.clone(),
                bind.clone(),
            ],
        ),
        (
            "rekeyed_no_origin",
            "rekeyed",
            vec![no_origin, bind.clone()],
        ),
        (
            "unnamed",
            "intact",
            vec![
                json!({"write": ".origin", "hex": hex_of(
                    "{\"mac\": \"8d24c4ba49a3560ef35efea300521482bc6540a9d9cec3d99e8e85c81fded089\", \"seq\": 1}"
                )}),
                bind,
            ],
        ),
    ]
}

fn hex_of(text: &str) -> String {
    common::hex(text.as_bytes())
}

fn index(b: Boundary) -> usize {
    Boundary::ALL
        .iter()
        .position(|x| *x == b)
        .expect("boundary")
}

fn provider() -> bettermemory_core::store::create::Fixed {
    clock(&[
        instant("2001-02-03T04:05:06.123456Z"),
        instant("2001-02-03T04:05:07.123456Z"),
    ])
}

/// The opening child: a writing open of `$ROOT/c`, ended at boundary
/// `$AT` by `$HOW`. Writes each boundary passed to `$ROOT/progress`.
#[test]
#[ignore = "run by the kill test as its child"]
fn upkeep_kill_child() {
    let Some(root) = std::env::var_os(ROOT_ENV).map(PathBuf::from) else {
        return;
    };
    let at: Option<usize> = std::env::var(AT_ENV).ok().and_then(|v| v.parse().ok());
    let how = std::env::var(HOW_ENV).unwrap_or_default();
    let progress = root.join("progress");
    let mut hook = |b: Boundary| {
        let i = index(b);
        let _ = fs::write(&progress, i.to_string());
        if at == Some(i) {
            if how == "abort" {
                std::process::abort();
            }
            std::process::exit(9);
        }
    };
    let c = root.join("c");
    let store = open_for_writing_mut_with(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &mut provider(),
        &mut hook,
    )
    .expect("opened");
    store.close().expect("closed");
    hook(Boundary::Closed);
}

fn set_up(root: &Path, state: usize) {
    let (_, base, recipe) = &states()[state];
    let c = root.join("c");
    opened::made(base, &c);
    opened::setup(recipe, &c, &common::stores::store_id());
}

fn settled(root: &Path) -> Value {
    let c = root.join("c");
    let store = open_for_writing_mut(&c.join("memory.sqlite"), &c.join("keys"), &mut provider())
        .unwrap_or_else(|e| panic!("a further writing open: {} {e}", e.class()));
    assert!(store.connection().is_autocommit());
    store.close().expect("close");
    opened::state(&c, root, &common::stores::store_id(), &[])
}

/// What a kill left: the log's last row's kind and the anchor's text.
fn left(root: &Path) -> String {
    let c = root.join("c");
    let conn = rusqlite::Connection::open(c.join("memory.sqlite")).expect("open");
    let (seq, kind): (i64, String) = conn
        .query_row(
            "SELECT seq, kind FROM log ORDER BY seq DESC LIMIT 1",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .expect("last row");
    let origin = fs::read_to_string(
        c.join("keys")
            .join(format!("{}.origin", common::stores::store_id())),
    )
    .map(|t| {
        if t.contains("key_fingerprint") {
            "anchor naming a key"
        } else {
            "anchor naming none"
        }
    })
    .unwrap_or("no anchor");
    format!("last row {seq} {kind}; {origin}")
}

fn reached_by(state: usize, base: &Path) -> Vec<usize> {
    let root = base.join(format!("dry{state}"));
    fs::create_dir(&root).expect("dry");
    set_up(&root, state);
    let c = root.join("c");
    let mut seen = Vec::new();
    open_for_writing_mut_with(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &mut provider(),
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
    seen
}

#[test]
fn a_killed_upkeep_is_settled_by_the_next_open_as_if_never_killed() {
    let dir = scratch("upkeep-kills");
    let mut table: BTreeMap<String, usize> = BTreeMap::new();
    let mut kills = 0;
    let mut every = Vec::new();
    for state in 0..states().len() {
        let whole = dir.0.join(format!("whole{state}"));
        fs::create_dir(&whole).expect("whole");
        set_up(&whole, state);
        let want = settled(&whole);
        let mut reached = reached_by(state, &dir.0);
        reached.push(index(Boundary::Closed));
        let passes: &[Boundary] = match state {
            0 => &[
                Boundary::TailNamed,
                Boundary::TailClosed,
                Boundary::OriginSettled,
            ],
            1 => &[
                Boundary::TailNamed,
                Boundary::BuildBegun,
                Boundary::BuildBatch,
                Boundary::BuildEnded,
            ],
            _ => &[],
        };
        for b in passes {
            assert!(reached.contains(&index(*b)), "{b:?} not passed");
        }
        every.extend(reached.iter().copied());
        for at in reached {
            for how in ["abort", "exit"] {
                let root = dir.0.join(format!("k{state}-{at}-{how}"));
                fs::create_dir(&root).expect("root");
                set_up(&root, state);
                let status = Command::new(std::env::current_exe().expect("exe"))
                    .args([
                        "--exact",
                        "upkeep_kill_child",
                        "--ignored",
                        "--nocapture",
                        "--test-threads=1",
                    ])
                    .env(ROOT_ENV, &root)
                    .env(AT_ENV, at.to_string())
                    .env(HOW_ENV, how)
                    .stdout(std::process::Stdio::null())
                    .stderr(std::process::Stdio::null())
                    .status()
                    .expect("child");
                assert!(!status.success(), "the child was not stopped at {at}");
                let progress: usize = fs::read_to_string(root.join("progress"))
                    .expect("progress")
                    .parse()
                    .expect("index");
                assert_eq!(progress, at, "the child stopped elsewhere");
                let label = format!(
                    "{} {at:02} after {:?}: {}",
                    states()[state].0,
                    Boundary::ALL[at],
                    left(&root)
                );
                let got = settled(&root);
                let parts = opened::differing(&want, &got);
                assert!(parts.is_empty(), "{label} then opened differs in {parts:?}");
                *table.entry(label).or_default() += 1;
                kills += 1;
            }
        }
    }
    for (label, count) in &table {
        eprintln!("{label} x{count}");
    }
    eprintln!("{kills} kills");
    // The four after the tail are a missing or foreign key's
    // (`store_open_rekey_kills`).
    assert_eq!(Boundary::ALL.len(), 17);
    let rekey = [
        Boundary::KeyRetired,
        Boundary::KeyCreated,
        Boundary::Rekeyed,
        Boundary::OriginTaken,
    ];
    for b in Boundary::ALL {
        assert!(
            rekey.contains(&b) || every.contains(&index(b)),
            "no state passes {b:?}"
        );
    }
    assert!(kills >= 40, "{kills} kills");
}

/// The upkeep runs after the binding: an open whose binding's locked
/// re-read finds the ring bound to a copy that holds the store (a second
/// opener bound it meanwhile) refuses there, as the reference's turns
/// into a reader's, and closes no tail.
#[test]
fn a_binding_lost_to_a_copy_closes_no_tail() {
    let id = common::stores::store_id();
    let dir = scratch("upkeep-race");
    let c = dir.0.join("c");
    opened::made("intact", &c);
    opened::setup(
        &[
            json!({ "sql": TAIL }),
            json!({"copy_store": "other/memory.sqlite"}),
        ],
        &c,
        &id,
    );
    let binding = c.join("keys").join(format!("{id}.path"));
    let other = fs::canonicalize(c.join("other/memory.sqlite")).expect("other");
    let won = format!(
        "{{\"path\": {}}}",
        serde_json::to_string(other.to_str().expect("UTF-8")).expect("json")
    );
    let mut passed = Vec::new();
    let got = open_for_writing_mut_with(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &mut provider(),
        &mut |b| {
            passed.push(b);
            if b == Boundary::EngineRecorded {
                fs::write(&binding, &won).expect("the second opener binds");
            }
        },
    );
    assert!(
        matches!(
            got,
            Err(OpenError::NotPorted(NotPorted::BoundElsewhere { .. }))
        ),
        "{:?}",
        got.map(|_| ())
    );
    assert!(!passed.contains(&Boundary::TailNamed), "{passed:?}");
    let conn = rusqlite::Connection::open(c.join("memory.sqlite")).expect("open");
    let last: i64 = conn
        .query_row("SELECT max(seq) FROM log", [], |r| r.get(0))
        .expect("max");
    assert_eq!(last, 9, "a rekey row was appended");
}

/// The order of the boundaries an open with a foreign tail passes: the
/// binding, the tail named, closed, the anchor settled, then the digest.
#[test]
fn the_upkeep_follows_the_binding_and_precedes_the_digest() {
    let dir = scratch("upkeep-order");
    let c = dir.0.join("c");
    opened::made("intact", &c);
    opened::setup(&[json!({ "sql": TAIL })], &c, &common::stores::store_id());
    let mut passed = Vec::new();
    open_for_writing_mut_with(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &mut provider(),
        &mut |b| passed.push(b),
    )
    .expect("open")
    .close()
    .expect("close");
    let at = |b: Boundary| passed.iter().position(|x| *x == b).expect("passed");
    assert!(at(Boundary::Bound) < at(Boundary::TailNamed));
    assert!(at(Boundary::TailNamed) < at(Boundary::TailClosed));
    assert!(at(Boundary::TailClosed) < at(Boundary::OriginSettled));
    let after = &passed[at(Boundary::OriginSettled) + 1..];
    assert!(!after.is_empty(), "no digest step: {passed:?}");
    assert!(
        after
            .iter()
            .all(|b| index(*b) > index(Boundary::OriginSettled)),
        "{passed:?}"
    );
}
