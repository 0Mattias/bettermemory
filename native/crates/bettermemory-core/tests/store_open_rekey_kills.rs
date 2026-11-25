//! The owner's writing open of a store whose key is missing or foreign,
//! killed at every boundary it passes, and the cases a plausible wrong
//! port of it fails.
//!
//! Kills. Four starting states: no key and no anchor (`intact`), a foreign
//! key that signs nothing on a rekeyed store with no anchor (the key in
//! hand is retired; the retired key holds the leading segment), a store
//! whose meta names a foreign tail's key while this host's key signs the
//! middle segment (a reclaim with a `rekey` row), and no key with the
//! anchor kept. The test binary runs itself as the opening child
//! (`rekey_kill_child`, ignored when run alone), with a fixed clock and
//! key draw, ended at each boundary the state passes (and after the store
//! is closed) by `abort` and by `exit`. Each killed state is copied twice:
//! a further native writing open settles one, drawing another fixed key;
//! the reference's writing open (`BM_REFERENCE_PYTHON`, with the same
//! clock and key draw) settles the other; both must leave the same state
//! (which key files exist and their fingerprints, meta, the log, the head,
//! the anchor). Without the reference the native half still runs: the
//! settled store's meta names its current key, and a third open changes
//! nothing.
//!
//! What a kill leaves, read from the reference: after `KeyRetired` no
//! current key (the next open is `key_missing` and rekeys again); after
//! `KeyCreated` a current key meta does not name and that signs nothing
//! (the next open retires it and rekeys again); after `Rekeyed` with no
//! anchor, the next open's `_settle_origin` anchors the first held
//! segment, which on the rekeyed store is seq 1 under the retired key, not
//! the `rekey` row `_take_over_origin` would have anchored.

#![cfg(unix)]

mod common;

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use bettermemory_core::log::{fingerprint, Key};
use bettermemory_core::store::create::Fixed;
use bettermemory_core::store::open::{
    open_for_writing_mut, open_for_writing_mut_with, Boundary, UpkeepArg,
};
use common::created::{reference_python, run_reference};
use common::opened;
use common::parity::record;
use common::stores::{scratch, store_id};
use common::written::{instant, keep_copy};
use rusqlite::Connection;
use serde_json::{json, Value};

const ROOT_ENV: &str = "BM_TEST_REKEY_ROOT";
const AT_ENV: &str = "BM_TEST_REKEY_KILL_AT";
const HOW_ENV: &str = "BM_TEST_REKEY_KILL_HOW";

/// The killed open's key and the settling opens' key.
const KILLED_KEY: u8 = 0xc0;
const SETTLING_KEY: u8 = 0xd0;
const STAMP: &str = "2001-02-03T04:05:06.123456Z";

/// `gen_store_open_upkeep._names`.
fn names(new: &str, old: &str) -> String {
    format!("{{\"new_fingerprint\":\"{new}\",\"old_fingerprint\":\"{old}\"}}")
}

fn key_hex(base: &str) -> String {
    common::stores::bases()["bases"][base]["keys"][".key"]
        .as_str()
        .expect("key")
        .to_owned()
}

fn fp_of_hex(hex: &str) -> String {
    fingerprint(&common::unhex(hex))
}

/// The MAC of a base's row at `seq`.
fn mac_of(base: &str, seq: i64) -> String {
    let dir = scratch("rekey-mac");
    let c = dir.0.join("c");
    opened::made(base, &c);
    let conn = Connection::open(c.join("memory.sqlite")).expect("open");
    conn.query_row("SELECT mac FROM log WHERE seq = ?", [seq], |r| r.get(0))
        .expect("mac")
}

fn rekey_row(seq: i64, payload: &str, prev: &str, mac: &str) -> Value {
    json!({"sql": format!(
        "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) VALUES \
         ({seq}, '2001-02-03T04:05:06.000000Z', NULL, 'rekey', '{payload}', '{prev}', '{mac}');"
    )})
}

fn meta(value: &str) -> Value {
    json!({"sql": format!(
        "UPDATE meta SET value = '{value}' WHERE key = 'key_fingerprint';"
    )})
}

/// The states: a name, a base, a recipe.
fn states() -> Vec<(&'static str, &'static str, Vec<Value>)> {
    let bind = json!({"bind": "memory.sqlite"});
    let fp1 = fp_of_hex(&key_hex("rekeyed"));
    vec![
        (
            "missing_no_anchor",
            "intact",
            vec![
                json!({"unlink": ".key"}),
                json!({"unlink": ".origin"}),
                bind.clone(),
            ],
        ),
        (
            "foreign_retired_no_anchor",
            "rekeyed",
            vec![
                json!({"write": ".key", "hex": "ee".repeat(32)}),
                json!({"unlink": ".origin"}),
                bind.clone(),
            ],
        ),
        (
            "reclaim_middle",
            "rekeyed",
            vec![
                rekey_row(
                    11,
                    &names(&"f".repeat(64), &fp1),
                    &mac_of("rekeyed", 10),
                    &"c".repeat(64),
                ),
                meta(&"f".repeat(64)),
                bind.clone(),
            ],
        ),
        (
            "missing_anchor_kept",
            "intact",
            vec![json!({"unlink": ".key"}), bind],
        ),
    ]
}

fn index(b: Boundary) -> usize {
    Boundary::ALL
        .iter()
        .position(|x| *x == b)
        .expect("boundary")
}

fn provider(key: u8) -> Fixed {
    let mut p = opened::engine_provider();
    p.key = Key::new(vec![key; 32]);
    p.clock = vec![instant(STAMP)];
    p
}

/// The opening child: a writing open of `$ROOT/c`, ended at boundary
/// `$AT` by `$HOW`. Writes each boundary passed to `$ROOT/progress`.
#[test]
#[ignore = "run by the kill test as its child"]
fn rekey_kill_child() {
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
        &mut provider(KILLED_KEY),
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
    opened::setup(recipe, &c, &store_id());
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
        &mut provider(KILLED_KEY),
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

/// The native settling open on `root/c`, then a third open that must
/// change nothing; the settled state.
fn settled_natively(root: &Path) -> Value {
    let c = root.join("c");
    let open = || {
        open_for_writing_mut(
            &c.join("memory.sqlite"),
            &c.join("keys"),
            &mut provider(SETTLING_KEY),
        )
        .unwrap_or_else(|e| panic!("a further writing open: {} {e}", e.class()))
    };
    let store = open();
    assert!(store.connection().is_autocommit());
    store.close().expect("close");
    let id = store_id();
    let got = opened::state(&c, root, &id, &[]);
    // meta names the current key.
    let key = fs::read(c.join("keys").join(format!("{id}.key"))).expect("a current key");
    let named: String = Connection::open(c.join("memory.sqlite"))
        .expect("open")
        .query_row(
            "SELECT value FROM meta WHERE key = 'key_fingerprint'",
            [],
            |r| r.get(0),
        )
        .expect("meta");
    assert_eq!(named, fingerprint(&key), "meta names another key");
    open().close().expect("close");
    let again = opened::state(&c, root, &id, &[]);
    assert!(
        opened::differing(&got, &again).is_empty(),
        "a third open changed the settled store"
    );
    got
}

/// The reference's settling open of each `(store, keys)` pair, with the
/// clock and the key draw fixed as the native settling open's.
const REFERENCE: &str = r#"
import sys
from unittest.mock import patch
import bettermemory
from bettermemory import log
from bettermemory.store import Store

class Draw:
    @staticmethod
    def token_bytes(n):
        return bytes([0xd0]) * n

args = sys.argv[1:]
with patch.object(log, "secrets", Draw), patch.object(
    log, "_utcnow_iso", lambda: "2001-02-03T04:05:06.123456Z"
), patch.object(bettermemory, "__version__", "9.0.0"):
    for i in range(0, len(args), 2):
        Store.open(args[i], keys_dir=args[i + 1], allow_rekey=True).close()
print("ok")
"#;

struct Killed {
    label: String,
    native: PathBuf,
    reference: PathBuf,
    state: usize,
    at: usize,
}

#[test]
fn a_killed_rekey_is_settled_by_the_next_open_as_the_reference_settles_it() {
    let dir = scratch("rekey-kills");
    let id = store_id();
    let mut killed: Vec<Killed> = Vec::new();
    let mut table: BTreeMap<String, usize> = BTreeMap::new();
    let mut every = Vec::new();
    for state in 0..states().len() {
        let reached = reached_by(state, &dir.0);
        every.extend(reached.iter().copied());
        for at in reached {
            for how in ["abort", "exit"] {
                let root = dir.0.join(format!("k{state}-{at}-{how}"));
                fs::create_dir(&root).expect("root");
                set_up(&root, state);
                let status = Command::new(std::env::current_exe().expect("exe"))
                    .args([
                        "--exact",
                        "rekey_kill_child",
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
                    "{} {at:02} after {:?} by {how}",
                    states()[state].0,
                    Boundary::ALL[at]
                );
                let (native, reference) = (root.join("n"), root.join("r"));
                keep_copy(&root.join("c"), &native.join("c"), &id);
                keep_copy(&root.join("c"), &reference.join("c"), &id);
                *table.entry(label.clone()).or_default() += 1;
                killed.push(Killed {
                    label,
                    native,
                    reference,
                    state,
                    at,
                });
            }
        }
    }
    for b in [
        Boundary::KeyRetired,
        Boundary::KeyCreated,
        Boundary::Rekeyed,
        Boundary::OriginTaken,
        Boundary::OriginSettled,
    ] {
        assert!(every.contains(&index(b)), "no state passes {b:?}");
    }
    let natives: Vec<Value> = killed.iter().map(|k| settled_natively(&k.native)).collect();
    // Read from the reference: a kill after the rekey row's commit on the
    // rekeyed store leaves the anchor to `_settle_origin`, on seq 1.
    for (k, got) in killed.iter().zip(&natives) {
        if k.state == 1 && k.at == index(Boundary::Rekeyed) {
            let anchor = got["keys"]
                .as_array()
                .expect("keys")
                .iter()
                .find(|i| i["name"] == "<id>.origin")
                .expect("an anchor");
            assert!(
                anchor["text"]
                    .as_str()
                    .expect("text")
                    .starts_with("{\"seq\": 1,"),
                "{}: {anchor}",
                k.label
            );
        }
    }
    match reference_python() {
        Err(why) => {
            eprintln!("NOT RUN the reference's half: {why}");
            record(
                "a_killed_rekey_is_settled_by_the_next_open_as_the_reference_settles_it",
                "reference Python",
                "i",
                0,
                &["the reference's settling open of each killed state".to_owned()],
                &[why],
            );
        }
        Ok(python) => {
            let args: Vec<PathBuf> = killed
                .iter()
                .flat_map(|k| {
                    let c = k.reference.join("c");
                    [c.join("memory.sqlite"), c.join("keys")]
                })
                .collect();
            let refs: Vec<&Path> = args.iter().map(PathBuf::as_path).collect();
            let out = run_reference(&python, &dir.0.join("home"), REFERENCE, &refs);
            assert!(
                out.status.success(),
                "{}",
                String::from_utf8_lossy(&out.stderr)
            );
            for (k, got) in killed.iter().zip(&natives) {
                let want = opened::state(&k.reference.join("c"), &k.reference, &id, &[]);
                let parts = opened::differing(&want, got);
                assert!(
                    parts.is_empty(),
                    "{}: settled otherwise than the reference in {parts:?}\nwant {want}\ngot  {got}",
                    k.label
                );
            }
            record(
                "a_killed_rekey_is_settled_by_the_next_open_as_the_reference_settles_it",
                "reference Python",
                "i",
                killed.len(),
                &[],
                &[],
            );
        }
    }
    for (label, count) in &table {
        eprintln!("{label} x{count}");
    }
    eprintln!("{} kills", killed.len());
    assert!(killed.len() >= 40, "{} kills", killed.len());
}

// ---------------------------------------------------------------------------
// What a plausible wrong port does otherwise
// ---------------------------------------------------------------------------

/// A store set up from `base` and `recipe` (bound), opened once with the
/// key draw `key`; the rows it appended (seq, payload, mac) and its
/// warnings' formats.
fn opened_once(base: &str, recipe: &[Value], key: u8) -> (PathBuf, common::stores::Scratch) {
    let dir = scratch("rekey-wrong");
    let c = dir.0.join("c");
    opened::made(base, &c);
    let mut recipe = recipe.to_vec();
    recipe.push(json!({"bind": "memory.sqlite"}));
    opened::setup(&recipe, &c, &store_id());
    open_for_writing_mut(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &mut provider(key),
    )
    .expect("opened")
    .close()
    .expect("close");
    (c, dir)
}

fn rows_from(c: &Path, seq: i64) -> Vec<(i64, String, String)> {
    let conn = Connection::open(c.join("memory.sqlite")).expect("open");
    let mut statement = conn
        .prepare("SELECT seq, payload, mac FROM log WHERE seq >= ? ORDER BY seq")
        .expect("prepare");
    statement
        .query_map([seq], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
        .expect("rows")
        .collect::<Result<_, _>>()
        .expect("rows")
}

/// The row the rekey appends names the last segment's key as the log
/// names it, never meta's text.
#[test]
fn the_rekey_row_names_the_last_segments_key_not_metas() {
    let fp0 = fp_of_hex(&key_hex("intact"));
    let (c, _dir) = opened_once(
        "intact",
        &[
            rekey_row(
                9,
                &names(&fp0, &"a".repeat(64)),
                &mac_of("intact", 8),
                &"c".repeat(64),
            ),
            meta(&"a".repeat(64)),
            json!({"write": ".key", "hex": "ee".repeat(32)}),
        ],
        KILLED_KEY,
    );
    let rows = rows_from(&c, 10);
    assert_eq!(rows.len(), 1);
    let new = fingerprint(&[KILLED_KEY; 32]);
    assert_eq!(rows[0].1, names(&new, &fp0));
    // Signed with the new key, which meta names at the append.
    let conn = Connection::open(c.join("memory.sqlite")).expect("open");
    let row = bettermemory_core::store::provenance::ConnSource(&conn);
    let read = bettermemory_core::log::LogSource::at_seq(&row, 10)
        .expect("read")
        .expect("row");
    assert!(bettermemory_core::log::row_verifies(
        &[KILLED_KEY; 32],
        &read
    ));
    assert!(!bettermemory_core::log::row_verifies(&[0xee; 32], &read));
}

/// A segment named by the key in hand whose first row does not verify
/// under it does not make the open keep that key: the MAC decides.
#[test]
fn a_segment_named_for_the_key_is_not_reclaimed_without_its_mac() {
    let fp0 = fp_of_hex(&key_hex("intact"));
    let fpe = fingerprint(&[0xee; 32]);
    let (c, _dir) = opened_once(
        "intact",
        &[
            rekey_row(9, &names(&fpe, &fp0), &mac_of("intact", 8), &"c".repeat(64)),
            json!({"write": ".key", "hex": "ee".repeat(32)}),
        ],
        KILLED_KEY,
    );
    let id = store_id();
    let keys = c.join("keys");
    // Retired, and a new key written.
    assert!(keys.join(format!("{id}.{}.key", &fpe[..16])).is_file());
    assert_eq!(
        fs::read(keys.join(format!("{id}.key"))).expect("key"),
        vec![KILLED_KEY; 32]
    );
    let rows = rows_from(&c, 10);
    assert_eq!(rows[0].1, names(&fingerprint(&[KILLED_KEY; 32]), &fpe));
}

/// The reclaim reads the last rekey name again under its lock: a rekey row
/// a second owner appended between the dispatch and the lock closes the
/// tail, and this open appends nothing more.
#[test]
fn the_reclaim_reads_the_tail_again_under_its_lock() {
    let dir = scratch("rekey-race");
    let c = dir.0.join("c");
    opened::made("rekeyed", &c);
    let (_, _, recipe) = &states()[2];
    opened::setup(recipe, &c, &store_id());
    let (file, keys) = (c.join("memory.sqlite"), c.join("keys"));
    let mut raced = false;
    let store = open_for_writing_mut_with(&file, &keys, &mut provider(KILLED_KEY), &mut |b| {
        if b == Boundary::Bound && !raced {
            raced = true;
            open_for_writing_mut(&file, &keys, &mut provider(KILLED_KEY))
                .expect("the second owner")
                .close()
                .expect("close");
        }
    })
    .expect("opened");
    let warned: Vec<String> = store
        .upkeep()
        .iter()
        .filter_map(|w| match w.args().get(3) {
            Some(UpkeepArg::Text(points)) => points.iter().map(|&p| char::from_u32(p)).collect(),
            _ => None,
        })
        .collect();
    store.close().expect("close");
    let rows = rows_from(&c, 12);
    assert_eq!(rows.len(), 1, "{rows:?}");
    assert!(warned[0].contains("meta is set back to it"), "{warned:?}");
}
