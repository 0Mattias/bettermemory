//! Hostile states for the owner's writing open of a store whose key is
//! missing or foreign: two owners at once, a writer holding the lock past
//! the busy timeout in the reclaim and in the rekey, 20,000 segments,
//! payloads nested 100,000 and 1,000,000 deep, a refusal of a later step
//! with nothing written, no key byte in a `Debug` or a warning, and an
//! error raised after a warning whose variant stays reachable.

#![cfg(unix)]

mod common;
mod timing;

use std::fs;
use std::path::{Path, PathBuf};

use bettermemory_core::log::{fingerprint, Key};
use bettermemory_core::store::create::{Fixed, Provider};
use bettermemory_core::store::open::{
    open_for_writing, open_for_writing_mut, NotPorted, OpenError, UpkeepArg, UpkeepWarning,
    WritableStore,
};
use common::opened;
use common::stores::{scratch, store_id};
use common::written::instant;
use rusqlite::Connection;
use serde_json::{json, Value};

const DRAWN: u8 = 0xc0;

fn provider(key: u8) -> Fixed {
    let mut p = opened::engine_provider();
    p.key = Key::new(vec![key; 32]);
    p.clock = vec![instant("2001-02-03T04:05:06.123456Z")];
    p
}

/// A base, bound, with `recipe` applied.
fn store(dir: &Path, base: &str, recipe: &[Value]) -> PathBuf {
    let c = dir.join("c");
    opened::made(base, &c);
    let mut all = recipe.to_vec();
    all.push(json!({"bind": "memory.sqlite"}));
    opened::setup(&all, &c, &store_id());
    c
}

fn open(c: &Path, key: u8) -> Result<WritableStore, OpenError> {
    open_for_writing_mut(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &mut provider(key),
    )
}

fn key_file(c: &Path) -> Option<Vec<u8>> {
    fs::read(c.join("keys").join(format!("{}.key", store_id()))).ok()
}

fn meta_key(c: &Path) -> String {
    Connection::open(c.join("memory.sqlite"))
        .expect("open")
        .query_row(
            "SELECT value FROM meta WHERE key = 'key_fingerprint'",
            [],
            |r| r.get(0),
        )
        .expect("meta")
}

fn rekey_rows(c: &Path) -> i64 {
    Connection::open(c.join("memory.sqlite"))
        .expect("open")
        .query_row("SELECT count(*) FROM log WHERE kind = 'rekey'", [], |r| {
            r.get(0)
        })
        .expect("count")
}

fn missing() -> Value {
    json!({"unlink": ".key"})
}

/// The reference takes no lock around a missing key's rekey: each owner
/// draws and writes a key file outside any lock, and the order of the
/// transactions decides meta and the rows. Both opens succeed; meta can
/// be left naming a key whose file the other owner's write replaced (the
/// reference's finding), and the next open then reclaims the store for
/// the key on disk, which signs its own segment. After it meta names the
/// current key, and an open after that writes nothing.
#[test]
fn two_owners_rekeying_at_once_settle_by_the_next_open() {
    for round in 0..8u8 {
        let dir = scratch("rekey-two");
        let c = store(&dir.0, "intact", &[missing()]);
        std::thread::scope(|scope| {
            let workers: Vec<_> = [0xa0 + round, 0xb0 + round]
                .into_iter()
                .map(|key| {
                    let c = &c;
                    scope.spawn(move || {
                        let store = open(c, key).expect("opened");
                        assert!(store.connection().is_autocommit());
                        store.close().expect("close");
                    })
                })
                .collect();
            for w in workers {
                w.join().expect("owner");
            }
        });
        let rows = rekey_rows(&c);
        assert!((1..=2).contains(&rows), "{rows} rekey rows");
        open(&c, 0xee).expect("settled").close().expect("close");
        let current = key_file(&c).expect("a current key");
        assert_eq!(meta_key(&c), fingerprint(&current), "round {round}");
        let before = (rekey_rows(&c), meta_key(&c));
        open(&c, 0xef).expect("again").close().expect("close");
        assert_eq!((rekey_rows(&c), meta_key(&c)), before);
    }
}

/// A writer that holds the lock past the busy timeout: the reclaim logs
/// that meta could not be set back and the open goes on with the key; the
/// rekey fails the open after the old key was retired and the new one
/// written (a reference finding, reproduced), and the next open retires the
/// unused key and rekeys again.
#[test]
fn a_writer_past_the_busy_timeout() {
    // The reclaim.
    let dir = scratch("rekey-busy-reclaim");
    let c = store(
        &dir.0,
        "intact",
        &[
            json!({"sql": format!("UPDATE meta SET value = '{}' WHERE key = 'key_fingerprint';", "b".repeat(64))}),
            json!({"sql": "UPDATE meta SET value = '9.0.0' WHERE key = 'engine_version';"}),
        ],
    );
    let holder = Connection::open(c.join("memory.sqlite")).expect("holder");
    holder.execute_batch("BEGIN IMMEDIATE").expect("lock");
    let got = bettermemory_core::store::open::open_for_writing_mut_with(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &mut provider(DRAWN),
        &mut |b| {
            if b == bettermemory_core::store::open::Boundary::Rekeyed {
                holder.execute_batch("ROLLBACK").expect("release");
            }
        },
    )
    .expect("the reclaim's open goes on");
    match got.upkeep() {
        [UpkeepWarning::MetaNotSetBack { cause, .. }] => {
            assert_eq!(cause.class, "OperationalError");
            assert_eq!(cause.sqlite, Some("SQLITE_BUSY"));
        }
        other => panic!("{other:?}"),
    }
    assert!(got.connection().is_autocommit());
    got.close().expect("close");
    drop(holder);
    assert_eq!(meta_key(&c), "b".repeat(64));

    // The rekey.
    let dir = scratch("rekey-busy");
    let c = store(
        &dir.0,
        "intact",
        &[
            missing(),
            json!({"sql": "UPDATE meta SET value = '9.0.0' WHERE key = 'engine_version';"}),
        ],
    );
    let old = meta_key(&c);
    let holder = Connection::open(c.join("memory.sqlite")).expect("holder");
    holder.execute_batch("BEGIN IMMEDIATE").expect("lock");
    let e = open(&c, DRAWN).expect_err("the rekey's transaction fails");
    holder.execute_batch("ROLLBACK").expect("release");
    drop(holder);
    assert_eq!(e.class(), "OperationalError");
    assert_eq!(e.to_string(), "database is locked");
    assert_eq!(key_file(&c), Some(vec![DRAWN; 32]));
    assert_eq!(meta_key(&c), old);
    assert_eq!(rekey_rows(&c), 0);
    // The next open: the unused key retired, another written.
    open(&c, 0xd0).expect("rekeyed").close().expect("close");
    let retired = c.join("keys").join(format!(
        "{}.{}.key",
        store_id(),
        &fingerprint(&[DRAWN; 32])[..16]
    ));
    assert_eq!(fs::read(retired).expect("retired"), vec![DRAWN; 32]);
    assert_eq!(meta_key(&c), fingerprint(&[0xd0; 32]));
}

/// A store with `rows` rekey rows naming the store's first key, its key
/// missing: the dispatch reads every one.
fn many(dir: &Path, rows: i64) -> PathBuf {
    let c = store(dir, "intact", &[missing()]);
    let own = meta_key(&c);
    let conn = Connection::open(c.join("memory.sqlite")).expect("open");
    let mac: String = conn
        .query_row("SELECT mac FROM log WHERE seq = 8", [], |r| r.get(0))
        .expect("mac");
    conn.execute_batch("BEGIN").expect("begin");
    let payload = json!({"new_fingerprint": own, "old_fingerprint": own}).to_string();
    for k in 0..rows {
        conn.execute(
            "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) VALUES \
             (?, '2001-02-03T04:05:06.000000Z', NULL, 'rekey', ?, ?, ?)",
            rusqlite::params![
                9 + k,
                payload,
                if k == 0 { mac.clone() } else { "c".repeat(64) },
                "c".repeat(64)
            ],
        )
        .expect("rekey row");
    }
    conn.execute_batch("COMMIT").expect("commit");
    c
}

#[test]
fn twenty_thousand_segments_rekey_in_linear_time() {
    let dir = scratch("rekey-many");
    let small = many(&dir.0.join("s"), 2_000);
    let large = many(&dir.0.join("l"), 20_000);
    let timed = |template: &Path| {
        let run = scratch("rekey-many-run");
        let c = run.0.join("c");
        common::written::keep_copy(template, &c, &store_id());
        let took = timing::wall(|| open(&c, DRAWN).expect("open").close().expect("close"));
        assert_eq!(meta_key(&c), fingerprint(&[DRAWN; 32]));
        took
    };
    timing::grows_within(
        "a rekeying open over 2,000 then 20,000 segments",
        timing::TENFOLD,
        &mut || timed(&small),
        &mut || timed(&large),
    );
}

/// A rekey payload nested past the JSON limit: RecursionError out of the
/// dispatch, before any key is retired or drawn; never a panic.
#[test]
fn nested_payloads_raise_recursion_before_any_key_moves() {
    for deep in [100_000, 1_000_000] {
        let dir = scratch("rekey-nested");
        let c = store(
            &dir.0,
            "intact",
            &[json!({"write": ".key", "hex": "ee".repeat(32)})],
        );
        Connection::open(c.join("memory.sqlite"))
            .expect("open")
            .execute(
                "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) VALUES \
                 (9, 't', NULL, 'rekey', ?, 'p', 'm')",
                [format!("{}{}", "[".repeat(deep), "]".repeat(deep))],
            )
            .expect("nested");
        let e = open(&c, DRAWN).expect_err("raises");
        assert_eq!(e.class(), "RecursionError");
        assert_eq!(key_file(&c), Some(vec![0xee; 32]));
        assert_eq!(rekey_rows(&c), 1);
    }
}

/// The entries that share the provider refuse a missing or a foreign key
/// with nothing written; a later step's refusal (an index another
/// tokenizer spelled) refuses the drawing open whole, before a key moves.
#[test]
fn refusals_write_nothing() {
    for (name, recipe) in [
        ("key_missing", vec![missing()]),
        (
            "key_foreign",
            vec![json!({"write": ".key", "hex": "ee".repeat(32)})],
        ),
    ] {
        let dir = scratch("rekey-shared");
        let c = store(&dir.0, "intact", &recipe);
        let before = opened::state(&c, &dir.0, &store_id(), &[]);
        let e = open_for_writing(&c.join("memory.sqlite"), &c.join("keys"), &provider(DRAWN))
            .expect_err("refused");
        match &e {
            OpenError::NotPorted(n) => assert_eq!(n.name(), name),
            other => panic!("{other:?}"),
        }
        assert!(!e.to_string().contains("open_for_writing"), "{e}");
        let after = opened::state(&c, &dir.0, &store_id(), &[]);
        assert!(opened::differing(&before, &after).is_empty());
    }
    let dir = scratch("rekey-drift");
    let c = store(
        &dir.0,
        "intact",
        &[
            missing(),
            json!({"sql": format!(
                "UPDATE log SET payload = json_set(payload, '$.tokenizer_fingerprint', '{}') \
                 WHERE seq = 1;",
                "0".repeat(64)
            )}),
        ],
    );
    let before = opened::state(&c, &dir.0, &store_id(), &[]);
    let e = open(&c, DRAWN).expect_err("refused");
    assert!(matches!(
        e,
        OpenError::NotPorted(NotPorted::TokenizerDrift { .. })
    ));
    let after = opened::state(&c, &dir.0, &store_id(), &[]);
    assert!(opened::differing(&before, &after).is_empty());
}

/// A provider that records whether its key was drawn.
struct Counting(Fixed, usize);

impl Provider for Counting {
    fn store_id(&mut self) -> std::io::Result<String> {
        self.0.store_id()
    }
    fn key(&mut self) -> std::io::Result<Key> {
        self.1 += 1;
        self.0.key()
    }
    fn membership_store_id(&mut self) -> std::io::Result<String> {
        self.0.membership_store_id()
    }
    fn now(&mut self) -> std::io::Result<bettermemory_core::datetime::DateTime> {
        self.0.now()
    }
    fn engine_version(&self) -> String {
        self.0.engine_version()
    }
}

/// The new key's bytes reach its file only: not the store's `Debug`, not
/// a warning's arguments, not an error's text; and a reclaim draws none.
#[test]
fn no_key_byte_leaves_its_file() {
    let secret = [0x5a_u8; 32];
    let shown = |text: &str| {
        let hex: String = secret.iter().map(|b| format!("{b:02x}")).collect();
        assert!(!text.contains(&hex), "{text}");
        assert!(!text.contains("90, 90, 90"), "{text}");
    };
    let dir = scratch("rekey-secret");
    let c = store(&dir.0, "intact", &[missing()]);
    let mut p = Counting(provider(0x5a), 0);
    let store =
        open_for_writing_mut(&c.join("memory.sqlite"), &c.join("keys"), &mut p).expect("opened");
    assert_eq!(p.1, 1);
    assert_eq!(key_file(&c), Some(secret.to_vec()));
    shown(&format!("{store:?}"));
    for w in store.upkeep() {
        shown(&format!("{w:?}"));
        for a in w.args() {
            if let UpkeepArg::Text(points) = a {
                shown(
                    &points
                        .iter()
                        .filter_map(|&p| char::from_u32(p))
                        .collect::<String>(),
                );
            }
        }
    }
    store.close().expect("close");
    // A reclaim draws no key.
    let dir = scratch("rekey-reclaim-draws");
    let c = store_with_meta(&dir.0);
    let mut p = Counting(provider(0x5a), 0);
    open_for_writing_mut(&c.join("memory.sqlite"), &c.join("keys"), &mut p)
        .expect("reclaimed")
        .close()
        .expect("close");
    assert_eq!(p.1, 0);
}

fn store_with_meta(dir: &Path) -> PathBuf {
    store(
        dir,
        "intact",
        &[json!({"sql": format!(
            "UPDATE meta SET value = '{}' WHERE key = 'key_fingerprint';",
            "b".repeat(64)
        )})],
    )
}

/// An error raised after the upkeep logged something comes back as
/// `OpenError::Logged`, with the error's class and text; `inner` is the
/// error itself, so a caller can still match its variant (`NotPorted` to
/// fall back).
#[test]
fn a_logged_error_keeps_its_variant_reachable() {
    let dir = scratch("logged");
    let c = store(
        &dir.0,
        "intact",
        &[json!({"sql": format!(
            "UPDATE meta SET value = '{}' WHERE key = 'key_fingerprint';",
            "b".repeat(64)
        )})],
    );
    let opened = open_for_writing_mut(
        &c.join("memory.sqlite"),
        &c.join("keys"),
        &mut provider(DRAWN),
    )
    .expect("the store is reclaimed");
    let logged = opened.upkeep().to_vec();
    opened.close().expect("close");
    assert_eq!(logged.len(), 1, "the reclaim's warning");
    let bare = OpenError::NotPorted(NotPorted::KeyMissing);
    let wrapped = OpenError::Logged {
        error: Box::new(OpenError::NotPorted(NotPorted::KeyMissing)),
        logged,
    };
    assert!(!matches!(wrapped, OpenError::NotPorted(_)));
    assert!(matches!(
        wrapped.inner(),
        OpenError::NotPorted(NotPorted::KeyMissing)
    ));
    assert!(matches!(
        bare.inner(),
        OpenError::NotPorted(NotPorted::KeyMissing)
    ));
    assert_eq!(wrapped.class(), bare.class());
    assert_eq!(wrapped.to_string(), bare.to_string());
    assert_eq!(wrapped.upkeep().len(), 1);
}
