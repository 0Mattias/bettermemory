//! Hostile store files: each must give an error or a report through every
//! read the store module offers, never a panic, and nothing a file can
//! make the native build allocate without bound (a value is capped at
//! `store::MAX_LENGTH`; the log is walked a row at a time). No reference
//! Python is needed: the files are written here with SQLite.

use std::fs;
use std::path::{Path, PathBuf};

use bettermemory_core::log::{self, Key, KeyRing, LogRow, GENESIS_MAC};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::keys::load_key_ring;
use bettermemory_core::store::{open_checks, StoreFile};
use rusqlite::Connection;

const STORE_ID: &str = "0123456789abcdef0123456789abcdef";

fn scratch(name: &str) -> PathBuf {
    let dir = PathBuf::from(env!("CARGO_TARGET_TMPDIR"))
        .join(format!("robust-{}-{name}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(dir.join("keys")).expect("dir");
    dir
}

fn key() -> Vec<u8> {
    (0u8..32).collect()
}

/// A small store: meta, log (signed rows), memories, tombstones,
/// episodes, the FTS table; the key in `keys/`.
fn base(dir: &Path, rows: usize) -> Connection {
    let conn = Connection::open(dir.join("memory.sqlite")).expect("create");
    // The reference's own schema, so the file is admitted.
    let schema: serde_json::Value =
        serde_json::from_str(bettermemory_core::store::admission::reference_schema_json())
            .expect("schema JSON");
    for text in schema["texts"].as_array().expect("texts") {
        conn.execute_batch(text.as_str().expect("text"))
            .expect("schema");
    }
    let fp = log::fingerprint(&key());
    for (k, v) in [
        ("store_id", STORE_ID),
        ("key_fingerprint", fp.as_str()),
        ("schema_version", "1"),
        ("schema_revision", "6"),
        ("min_reader_revision", "1"),
        ("min_writer_revision", "5"),
    ] {
        conn.execute("INSERT INTO meta VALUES (?, ?)", [k, v])
            .expect("meta");
    }
    let mut previous: Option<LogRow> = None;
    for i in 0..rows {
        let payload = PyValue::Dict(vec![(PyValue::str("n"), PyValue::int(i as i128))]);
        let row = log::next_row(
            previous.as_ref(),
            &key(),
            "memory_put",
            &payload,
            None,
            "2026-01-01T00:00:00+00:00",
        )
        .expect("row");
        conn.execute(
            "INSERT INTO log VALUES (?, ?, ?, ?, ?, ?, ?)",
            rusqlite::params![
                row.seq,
                row.ts,
                row.session,
                row.kind,
                row.payload,
                row.prev_mac,
                row.mac
            ],
        )
        .expect("insert");
        previous = Some(row);
    }
    conn.execute(
        "INSERT INTO memories(id, created, updated, confidence, body, body_fts, scopes_text, \
         scopes_json, provenance, source) VALUES ('01J0', '2026-01-01T00:00:00Z', \
         '2026-01-01T00:00:00Z', 'high', 'body', 'body', ' a ', '[\"a\"]', 'local', 'inferred')",
        [],
    )
    .expect("memory");
    fs::write(dir.join("keys").join(format!("{STORE_ID}.key")), key()).expect("key");
    conn
}

/// Every read, on whatever the file is; returns what happened, never
/// panics.
fn exercise(dir: &Path) -> String {
    let store = match StoreFile::open(&dir.join("memory.sqlite")) {
        Ok(s) => s,
        Err(e) => return format!("open: error ({e})"),
    };
    let mut seen = Vec::new();
    match store.meta() {
        Err(e) => seen.push(format!("meta error ({e})")),
        Ok(meta) => match open_checks(&meta, "store", "9.0.0") {
            Err(e) => seen.push(format!("refused ({e})")),
            Ok(o) => {
                seen.push("opened".into());
                let ring = load_key_ring(&dir.join("keys"), &o.store_id).expect("id checked");
                match store.verify_chain(&ring, &o.key_fingerprint) {
                    Ok(r) => seen.push(format!("chain {} ({} rows)", r.status, r.rows)),
                    Err(e) => seen.push(format!("chain error ({e:?})")),
                }
            }
        },
    }
    if let Ok(names) = store.table_names() {
        for name in names {
            if let Err(e) = store.each_row(&name, &mut |_| {}) {
                seen.push(format!("{name}: error ({e})"));
            }
        }
    }
    for (what, r) in [
        ("memories", store.each_memory(&mut |_| {})),
        ("tombstones", store.each_tombstone(&mut |_| {})),
        ("episodes", store.each_episode(&mut |_| {})),
    ] {
        if let Err(e) = r {
            seen.push(format!("{what}: error ({e})"));
        }
    }
    if let Err(e) = store.query_candidates("body", &Default::default()) {
        seen.push(format!("fts: error ({e})"));
    }
    seen.join("; ")
}

fn case(name: &str, make: impl FnOnce(&Path)) -> String {
    let dir = scratch(name);
    make(&dir);
    let outcome = exercise(&dir);
    eprintln!("{name}: {outcome}");
    let _ = fs::remove_dir_all(dir);
    outcome
}

fn edit(dir: &Path, sql: &str) {
    let conn = base(dir, 3);
    conn.execute_batch(sql).expect(sql);
}

#[test]
fn hostile_store_files() {
    let ok = case("baseline", |d| drop(base(d, 3)));
    assert!(ok.contains("chain unverifiable (3 rows)"), "{ok}");

    let t = case("truncated_half", |d| {
        drop(base(d, 50));
        let f = d.join("memory.sqlite");
        let bytes = fs::read(&f).expect("read");
        fs::write(&f, &bytes[..bytes.len() / 2]).expect("truncate");
    });
    assert!(t.contains("error") || t.contains("chain"), "{t}");

    case("truncated_header", |d| {
        drop(base(d, 3));
        let f = d.join("memory.sqlite");
        let bytes = fs::read(&f).expect("read");
        fs::write(&f, &bytes[..50]).expect("truncate");
    });

    let r = case("random_bytes", |d| {
        let mut state: u64 = 0x9e37_79b9_7f4a_7c15;
        let bytes: Vec<u8> = (0..65536)
            .map(|_| {
                state ^= state << 13;
                state ^= state >> 7;
                state ^= state << 17;
                state as u8
            })
            .collect();
        fs::write(d.join("memory.sqlite"), bytes).expect("write");
    });
    assert!(r.contains("error"), "{r}");

    case("empty_file", |d| {
        fs::write(d.join("memory.sqlite"), b"").expect("write");
    });

    let m = case("log_table_missing", |d| edit(d, "DROP TABLE log;"));
    assert!(m.contains("chain error"), "{m}");
    let m = case("meta_table_missing", |d| edit(d, "DROP TABLE meta;"));
    assert!(m.contains("meta error"), "{m}");
    case("memories_missing", |d| {
        edit(d, "DROP TABLE memories_fts; DROP TABLE memories;")
    });

    case("payload_blob", |d| {
        edit(d, "UPDATE log SET payload = x'00ff10' WHERE seq = 2;")
    });
    case("ts_integer", |d| {
        edit(d, "UPDATE log SET ts = 12345 WHERE seq = 2;")
    });
    case("kind_real", |d| {
        edit(d, "UPDATE log SET kind = 1.5 WHERE seq = 2;")
    });
    case("session_blob", |d| {
        edit(d, "UPDATE log SET session = x'' WHERE seq = 1;")
    });
    case("created_integer", |d| {
        edit(d, "UPDATE memories SET created = 7;")
    });
    case("created_garbage", |d| {
        edit(d, "UPDATE memories SET created = 'yesterday';")
    });
    case("scopes_not_json", |d| {
        edit(d, "UPDATE memories SET scopes_json = '[[[';")
    });
    case("scopes_deep_json", |d| {
        let deep = "[".repeat(20000) + &"]".repeat(20000);
        edit(d, &format!("UPDATE memories SET scopes_json = '{deep}';"));
    });
    case("corroborations_text", |d| {
        edit(d, "UPDATE memories SET corroborations = 'x';")
    });
    case("log_with_wrong_columns", |d| {
        edit(
            d,
            "DROP TABLE log; CREATE TABLE log (seq INTEGER PRIMARY KEY, ts TEXT);",
        )
    });

    for (k, v) in [
        ("schema_revision", "99999999999"),
        ("min_reader_revision", "-1"),
        ("min_reader_revision", "\u{0661}"),
        ("min_writer_revision", "999999999"),
        (
            "schema_version",
            "99999999999999999999999999999999999999999",
        ),
        ("schema_version", " 1_0 "),
        ("schema_version", "one"),
        ("store_id", "../../../../etc/passwd"),
        ("store_id", ""),
        ("key_fingerprint", "\u{0}"),
    ] {
        let o = case(&format!("meta_{k}"), |d| {
            let conn = base(d, 2);
            conn.execute("UPDATE meta SET value = ? WHERE key = ?", [v, k])
                .expect("meta");
        });
        assert!(!o.is_empty());
    }
    case("meta_huge_value", |d| {
        let conn = base(d, 2);
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'engine_version'",
            ["x".repeat(10 << 20)],
        )
        .expect("meta");
        conn.execute(
            "INSERT INTO meta VALUES ('engine_version', ?)",
            ["x".repeat(10 << 20)],
        )
        .expect("meta");
    });

    for seq in [i64::MAX, i64::MIN, 0, -1] {
        let o = case(&format!("seq_{seq}"), |d| {
            let conn = base(d, 3);
            conn.execute(
                "INSERT INTO log VALUES (?, 't', NULL, 'memory_put', '{}', ?, ?)",
                rusqlite::params![seq, GENESIS_MAC, GENESIS_MAC],
            )
            .expect("row");
        });
        assert!(o.contains("chain"), "{o}");
    }

    let n = case("nul_bytes", |d| {
        edit(
            d,
            "UPDATE log SET payload = 'a' || char(0) || 'b', kind = 'k' || char(0) WHERE seq = 2; \
             UPDATE memories SET body = 'x' || char(0) || 'y';",
        )
    });
    assert!(n.contains("chain"), "{n}");
    let s = case("lone_surrogate_bytes", |d| {
        edit(
            d,
            "UPDATE log SET payload = CAST(x'eda080' AS TEXT) WHERE seq = 2; \
             UPDATE memories SET body = CAST(x'eda080' AS TEXT);",
        )
    });
    assert!(s.contains("error"), "{s}");
    let big = case("payload_8_mib", |d| {
        let conn = base(d, 3);
        conn.execute(
            "UPDATE log SET payload = ? WHERE seq = 2",
            ["p".repeat(8 << 20)],
        )
        .expect("big");
    });
    assert!(big.contains("chain tampered"), "{big}");
}

/// Key ring files the reference reads as absent or unreadable.
#[test]
fn hostile_key_rings() {
    let dir = scratch("ring");
    let keys = dir.join("keys");
    // No files.
    let ring = load_key_ring(&keys, STORE_ID).expect("ring");
    assert!(ring.current.is_none() && ring.head_file.is_none() && ring.retired.is_empty());
    // A directory as the key, a dangling link as the head, a huge origin,
    // retired names of odd shapes.
    fs::create_dir(keys.join(format!("{STORE_ID}.key"))).expect("dir");
    #[cfg(unix)]
    std::os::unix::fs::symlink(dir.join("nowhere"), keys.join(format!("{STORE_ID}.head")))
        .expect("link");
    fs::write(keys.join(format!("{STORE_ID}.origin")), vec![b' '; 2 << 20]).expect("big");
    fs::write(keys.join(format!("{STORE_ID}..key")), key()).expect("empty part");
    fs::write(keys.join(format!("{STORE_ID}.a.b.key")), key()).expect("dotted part");
    fs::write(keys.join("../outside.key"), key()).expect("outside");
    let ring = load_key_ring(&keys, STORE_ID).expect("ring");
    assert!(ring.current.is_none());
    assert!(ring.head_file.is_none());
    assert!(ring.origin_file.is_none(), "over the cap: unreadable");
    assert_eq!(
        ring.retired.keys().cloned().collect::<Vec<_>>(),
        ["", "a.b"]
    );
    // Only a fingerprint names a key.
    assert!(ring.key_for_fingerprint("../outside").is_none());
    let fp = log::fingerprint(&key());
    assert_eq!(ring.held_keys(), vec![Key::new(key())]);
    assert!(
        ring.key_for_fingerprint(&fp).is_none(),
        "no file is named by its prefix"
    );
    // Not a store id: refused before any file is named.
    assert!(load_key_ring(&keys, "../../etc").is_err());
    let _ = KeyRing::default();
    let _ = fs::remove_dir_all(dir);
}

/// L3: retired keys are held in the order of the part between the id and
/// `.key`, where the reference sorts whole file names; `ab` and `ab.c`
/// order differently in the two. Which order is used cannot change a
/// report: `held_keys` is read only to find the one key a row verifies
/// under (`leading_key`, `check_origin`, which visits every key when none
/// verifies), and two distinct keys never verify the same MAC. Shown here:
/// the report with the signer under `ab.c` and another key under `ab`
/// equals the report with the signer alone, and with the names swapped.
#[test]
fn retired_key_order_cannot_change_a_report() {
    let signer: Vec<u8> = (100u8..132).collect();
    let other: Vec<u8> = (200u8..232).collect();
    let current: Vec<u8> = (0u8..32).collect();
    let mut rows: Vec<LogRow> = Vec::new();
    for i in 0..4 {
        let payload = PyValue::Dict(vec![(PyValue::str("n"), PyValue::int(i))]);
        let row =
            log::next_row(rows.last(), &signer, "memory_put", &payload, None, "t").expect("row");
        rows.push(row);
    }
    let fp = log::fingerprint(&current);
    let report_for = |files: &[(&str, &Vec<u8>)]| {
        let dir = scratch("order");
        let keys = dir.join("keys");
        fs::write(keys.join(format!("{STORE_ID}.key")), &current).expect("key");
        for (part, key) in files {
            fs::write(keys.join(format!("{STORE_ID}.{part}.key")), key).expect("retired");
        }
        let ring = load_key_ring(&keys, STORE_ID).expect("ring");
        let report = log::verify_chain(&rows, &ring, &fp).expect("report");
        let _ = fs::remove_dir_all(dir);
        report
    };
    let alone = report_for(&[("ab.c", &signer)]);
    let both = report_for(&[("ab", &other), ("ab.c", &signer)]);
    let swapped = report_for(&[("ab", &signer), ("ab.c", &other)]);
    assert_eq!(alone, both);
    assert_eq!(both, swapped);
    assert_eq!(alone.segments.len(), 1);
}

/// Each read of the store in `BM_STORE_DIR`, timed: meta, every memory
/// through `each_memory`, the chain, and `query_candidates` for
/// `BM_STORE_QUERY`. Prints one line per read; an error is printed, not
/// raised (a worst case may be refused by design). For measuring stores
/// no fixture holds (a few hundred thousand rows, rows at the length
/// limit, deep JSON).
#[test]
#[ignore = "times the reads of the store BM_STORE_DIR names"]
fn time_reads_of_env_store() {
    use std::time::Instant;
    let dir = PathBuf::from(std::env::var("BM_STORE_DIR").expect("BM_STORE_DIR"));
    let query = std::env::var("BM_STORE_QUERY").unwrap_or_else(|_| "record".into());
    let t = Instant::now();
    let store = match StoreFile::open(&dir.join("memory.sqlite")) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("open: {e} after {:?}", t.elapsed());
            return;
        }
    };
    eprintln!("open+admit: {:?}", t.elapsed());
    let t = Instant::now();
    let meta = store.meta().expect("meta");
    eprintln!("meta: {:?}", t.elapsed());
    let get = |k: &str| {
        meta.iter()
            .find(|(m, _)| m == k)
            .map(|(_, v)| v.clone())
            .unwrap_or_default()
    };
    let t = Instant::now();
    let mut n = 0usize;
    let read = store.each_memory(&mut |_| n += 1);
    eprintln!(
        "each_memory: {n} rows, {:?} in {:?}",
        read.err().map(|e| e.to_string()),
        t.elapsed()
    );
    let t = Instant::now();
    let ring = load_key_ring(&dir.join("keys"), &get("store_id")).expect("ring");
    match store.verify_chain(&ring, &get("key_fingerprint")) {
        Ok(r) => eprintln!(
            "verify_chain: {} rows {} in {:?}",
            r.rows,
            r.status,
            t.elapsed()
        ),
        Err(e) => eprintln!("verify_chain: error {e:?} in {:?}", t.elapsed()),
    }
    let t = Instant::now();
    let hits = store.query_candidates(&query, &Default::default());
    eprintln!(
        "query_candidates({query:?}): {:?} in {:?}",
        hits.map(|h| h.len()).map_err(|e| e.to_string()),
        t.elapsed()
    );
}
