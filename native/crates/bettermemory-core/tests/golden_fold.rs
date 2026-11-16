//! Every case of `fold.jsonl`: its store rebuilt with SQLite (a base of
//! `store_bases.json`, a prelude from the fixture's header, its recipe; or
//! the committed `store_real.sqlite`), opened read-only, and the native
//! `refold` and `log_verify` reports compared field for field with the
//! reference's. Plus hostile payloads and tables, which must never panic
//! and must give a report or a typed error.

mod common;

use std::path::Path;
use std::time::{Duration, Instant};

use bettermemory_core::log::{ReportDict, ReportValue};
use bettermemory_core::store::fold::{
    Cell, FoldError, FoldReport, Label, LogVerify, Problem, TableFold,
};
use bettermemory_core::store::keys::load_key_ring;
use bettermemory_core::store::{StoreError, StoreFile};
use bettermemory_core::unicode;
use common::parity::Ledger;
use common::stores::{materialize, scratch};
use common::{golden_path, jsonl, unhex};
use rusqlite::Connection;
use serde_json::{json, Map, Value};

fn cell(c: &Cell) -> Value {
    match c {
        Cell::Null => Value::Null,
        Cell::Int(i) => json!(i),
        Cell::Real(f) => json!({ "float": format!("{:016x}", f.to_bits()) }),
        Cell::Text(t) => json!(t),
        Cell::Blob(b) => json!({ "blob": common::hex(b) }),
    }
}

fn label(l: &Label) -> Value {
    match l {
        Label::One(c) => cell(c),
        Label::Many(cells) => Value::Array(cells.iter().map(cell).collect()),
    }
}

fn tables(t: &[TableFold]) -> Value {
    let mut m = Map::new();
    for t in t {
        m.insert(
            t.table.to_owned(),
            // The fixture's form: a table with nothing unaccounted or
            // missing is its row count alone.
            if t.unaccounted.is_empty() && t.missing.is_empty() {
                json!(t.rows)
            } else {
                json!({
                    "unaccounted": t.unaccounted.iter().map(label).collect::<Vec<_>>(),
                    "missing": t.missing.iter().map(label).collect::<Vec<_>>(),
                    "rows": t.rows,
                })
            },
        );
    }
    Value::Object(m)
}

fn fold_json(r: &FoldReport) -> Value {
    json!({
        "status": r.status,
        "tables": tables(&r.tables),
        "membership": tables(&r.membership),
        "problems": r.problems.iter().map(|p| json!({
            "seq": p.seq, "kind": p.kind, "problem": "payload_invalid", "detail": p.detail,
        })).collect::<Vec<_>>(),
    })
}

fn report_value(v: &ReportValue) -> Value {
    match v {
        ReportValue::Null => Value::Null,
        ReportValue::Int(i) => json!(i),
        ReportValue::BigInt(text) => json!({ "int": text }),
        ReportValue::Str(s) => json!(unicode::to_string(s).expect("UTF-8 report text")),
    }
}

fn dict(d: &ReportDict) -> Value {
    Value::Object(
        d.iter()
            .map(|(k, v)| ((*k).to_owned(), report_value(v)))
            .collect(),
    )
}

fn verify_json(r: &LogVerify) -> Value {
    let fold = fold_json(&r.fold);
    json!({
        "status": r.status,
        "store_id": r.store_id,
        "key_fingerprint": r.key_fingerprint,
        "rows": r.chain.rows,
        "segments": r.chain.segments.iter().map(dict).collect::<Vec<_>>(),
        "head": dict(&r.chain.head),
        "origin": dict(&r.chain.origin),
        "problems": r.problems.iter().map(|p| match p {
            Problem::Chain(d) => dict(d),
            Problem::Payload(p) => json!({
                "seq": p.seq, "kind": p.kind, "problem": "payload_invalid", "detail": p.detail,
            }),
        }).collect::<Vec<_>>(),
        "fold": {
            "status": fold["status"],
            "tables": fold["tables"],
            "membership": fold["membership"],
        },
        "unaccounted_ids": r.unaccounted_ids().iter().map(label).collect::<Vec<_>>(),
    })
}

fn error_json(e: &FoldError) -> Value {
    match e {
        FoldError::Raised(class) => json!({ "raised": class }),
        FoldError::Store(e) => json!({ "store_error": e.to_string() }),
    }
}

/// Rebuild a case's store in `dir`.
fn build(case: &Value, header: &Value, dir: &Path) {
    let base = case["base"].as_str().expect("base");
    if base == "store_real" {
        std::fs::create_dir_all(dir.join("keys")).expect("keys");
        std::fs::copy(golden_path("store_real.sqlite"), dir.join("memory.sqlite")).expect("copy");
        let doc: Value = serde_json::from_str(
            &std::fs::read_to_string(golden_path("store_real.json")).expect("store_real.json"),
        )
        .expect("JSON");
        let id = doc["store_id"].as_str().expect("store_id");
        for (suffix, hex) in doc["keys"].as_object().expect("keys") {
            std::fs::write(
                dir.join("keys").join(format!("{id}{suffix}")),
                unhex(hex.as_str().expect("hex")),
            )
            .expect("key");
        }
        return;
    }
    let mut recipe = Vec::new();
    for name in case["prelude"].as_array().expect("preludes") {
        let name = name.as_str().expect("prelude name");
        recipe.extend(
            header["preludes"][name]
                .as_array()
                .expect("prelude")
                .iter()
                .cloned(),
        );
    }
    recipe.extend(case["recipe"].as_array().expect("recipe").iter().cloned());
    materialize(&json!({ "base": base, "recipe": recipe }), dir);
}

struct Outcome {
    got: Value,
    fold_time: Duration,
}

fn native(dir: &Path) -> Outcome {
    let store = match StoreFile::open(&dir.join("memory.sqlite")) {
        Ok(s) => s,
        Err(StoreError::Admission(r)) => {
            // `golden_store_files` pins which refusals the reference raises
            // as OperationalError (`reference_raises`).
            let class = if r.reference_raises.is_some() {
                "OperationalError"
            } else if r.later.is_some() {
                "LaterStoreError"
            } else {
                "StoreRefusedError"
            };
            return Outcome {
                got: json!({ "open": { "refused": class } }),
                fold_time: Duration::ZERO,
            };
        }
        Err(e) => {
            return Outcome {
                got: json!({ "open": { "refused_natively": e.to_string() } }),
                fold_time: Duration::ZERO,
            }
        }
    };
    let keys = dir.join("keys");
    let t = Instant::now();
    let refold = match store.refold() {
        Ok(r) => fold_json(&r),
        Err(e) => error_json(&e),
    };
    let fold_time = t.elapsed();
    let mut verified = match store.log_verify_loading(&mut loader(&keys)) {
        Ok(r) => verify_json(&r),
        Err(e) => error_json(&e),
    };
    if verified.get("fold").is_some() {
        let same = ["status", "tables", "membership"]
            .iter()
            .all(|k| verified["fold"][k] == refold[k]);
        if same {
            verified["fold"] = json!("=refold");
        }
    }
    Outcome {
        got: json!({ "refold": refold, "log_verify": verified }),
        fold_time,
    }
}

/// A key loader as `bm-verify`'s: meta's store id and fingerprint, the
/// ring from `keys`.
fn loader(
    keys: &Path,
) -> impl FnMut(&StoreFile) -> Result<(bettermemory_core::log::KeyRing, String), StoreError> + '_ {
    move |s: &StoreFile| {
        let meta = s.meta()?;
        let get = |k: &str| {
            meta.iter()
                .find(|(key, _)| key == k)
                .map(|(_, v)| v.clone())
                .unwrap_or_default()
        };
        let ring = load_key_ring(keys, &get("store_id"))
            .map_err(|e| StoreError::Refused(format!("{e:?}")))?;
        Ok((ring, get("key_fingerprint")))
    }
}

/// The refusal class the reference gives is the native one, except for
/// `blob_sql`, where the reference raises TypeError out of
/// `connect_read_only` and the crate refuses the file: a deliberate
/// difference `golden_store_files` pins (its `DELIBERATE`).
fn same_refusal(name: &str, want: &Value, got: &Value) -> bool {
    let w = &want["open"]["refused"];
    let g = &got["open"]["refused"];
    if name == "store_files:blob_sql" {
        return w == "TypeError" && g == "StoreRefusedError";
    }
    w == g
}

#[test]
fn fold_every_case() {
    let mut lines = jsonl("fold.jsonl");
    let header = lines.remove(0);
    let mut ledger = Ledger::new("golden_fold::fold_every_case", "fold.jsonl");
    let mut slowest = (Duration::ZERO, String::new());
    for (i, case) in lines.iter().enumerate() {
        let name = case["case"].as_str().expect("case").to_owned();
        // Named by index: a case name may hold a colon, which Windows
        // refuses in a path.
        let dir = scratch(&format!("fold-{i}"));
        build(case, &header, &dir.0);
        let out = native(&dir.0);
        if out.fold_time > slowest.0 {
            slowest = (out.fold_time, name.clone());
        }
        ledger.case(&name, || {
            if case.get("open").is_some() {
                assert!(
                    same_refusal(&name, case, &out.got),
                    "{name}: want {}, got {}",
                    case["open"],
                    out.got
                );
                return;
            }
            assert_eq!(out.got["refold"], case["refold"], "{name}: refold");
            assert_eq!(
                out.got["log_verify"], case["log_verify"],
                "{name}: log_verify"
            );
        });
    }
    eprintln!(
        "slowest native refold: {:.3} ms ({})",
        slowest.0.as_secs_f64() * 1000.0,
        slowest.1
    );
    ledger.finish(lines.len());
}

/// Hostile payloads (signed or not: `refold` does not read MACs) and
/// tables: each gives a report or a typed error, never a panic.
#[test]
fn hostile_inputs_never_panic() {
    let mut lines = jsonl("fold.jsonl");
    let header = lines.remove(0);
    let populated = lines
        .iter()
        .find(|c| c["case"] == "populated")
        .expect("populated case")
        .clone();
    let deep_object = "{\"a\":".repeat(12_000) + "1" + &"}".repeat(12_000);
    let payloads: Vec<(&str, String)> = vec![
        ("memory_put", "[".repeat(9_000) + &"]".repeat(9_000)),
        ("memory_put", deep_object),
        (
            "memory_put",
            "{\"memory\": ".to_owned() + &"[".repeat(5_000) + &"]".repeat(5_000) + "}",
        ),
        (
            "memory_put",
            format!("{{\"memory\": \"{}\"}}", "x".repeat(2_000_000)),
        ),
        (
            "memory_put",
            "{\"memory\": {\"id\": \"\\u0000\"}, \"provenance\": \"local\"}".into(),
        ),
        ("tombstone_put", "{\"tombstone\": 1e999}".into()),
        ("tombstone_put", "{\"corroborations\": -1e308}".into()),
        (
            "verification_put",
            "{\"memory_id\": [[[[1]]]], \"verified_at\": {}}".into(),
        ),
        (
            "verification_put",
            "{\"memory_id\": \"\\ud800\", \"verified_at\": \"x\"}".into(),
        ),
        (
            "conflict_put",
            "{\"conflict\": {\"id\": 1e400, \"similarity\": NaN}}".into(),
        ),
        (
            "import_put",
            format!(
                "{{\"source\": {}, \"content_hash\": 1, \"imported_at\": 2}}",
                "9".repeat(5000)
            ),
        ),
        (
            "quarantine_put",
            "{\"name\": null, \"reason\": true, \"at\": -0.0, \"size\": 1.5}".into(),
        ),
        (
            "checkout_put",
            "{\"checkout\": {\"id\": \"c\", \"stamps_json\": [\"\\udfff\"]}}".into(),
        ),
        (
            "standing_put",
            "{\"standing\": {\"record_id\": \"r\", \"probability\": Infinity}}".into(),
        ),
        ("homes_put", "\u{0}".into()),
        ("memory_delete", "{\"id\": \"a\\u0000b\"}".into()),
        (
            "episode_put",
            "{\"episode\": {\"id\": \"01ARZ3NDEKTSV4RRFFQ69G5FAV\", \"created\": \"\\u0000\"}}"
                .into(),
        ),
    ];
    let tables: Vec<&str> = vec![
        "UPDATE memories SET body = CAST(X'C0AF' AS TEXT)",
        "UPDATE memories SET body = X'00'",
        "UPDATE memories SET id = X'01' WHERE rowid = (SELECT min(rowid) FROM memories)",
        "UPDATE memories SET created = 1e308",
        "INSERT INTO memory_links VALUES (X'FF', 1, 2.5, NULL)",
        "UPDATE episodes SET body = replace(hex(zeroblob(500000)), '0', 'z')",
        "ALTER TABLE homes ADD COLUMN later TEXT",
        "DROP TABLE native_membership_rows",
        "ALTER TABLE checkouts DROP COLUMN unresolved_json",
        "DELETE FROM log",
        "UPDATE log SET payload = CAST(X'FFFE' AS TEXT) WHERE seq = 1",
        "UPDATE log SET payload = X'7B7D' WHERE kind = 'store_created'",
        "UPDATE log SET kind = 'memory_put' WHERE kind = 'store_created'",
    ];
    let mut outcomes = 0;
    for (i, (kind, payload)) in payloads.iter().enumerate() {
        let dir = scratch(&format!("fold-hostile-payload-{i}"));
        build(&populated, &header, &dir.0);
        let conn = Connection::open(dir.0.join("memory.sqlite")).expect("open");
        conn.execute(
            "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) \
             VALUES ((SELECT max(seq) + 1 FROM log), 't', NULL, ?1, ?2, '', '')",
            [kind, payload.as_str()],
        )
        .expect("append");
        drop(conn);
        let store = StoreFile::open(&dir.0.join("memory.sqlite")).expect("open");
        match store.refold() {
            Ok(r) => assert!(r.status == "ok" || r.status == "diverged"),
            Err(FoldError::Raised(class)) => assert!(!class.is_empty()),
            Err(FoldError::Store(e)) => panic!("payload {i}: untyped {e}"),
        }
        outcomes += 1;
    }
    for (i, sql) in tables.iter().enumerate() {
        let dir = scratch(&format!("fold-hostile-table-{i}"));
        build(&populated, &header, &dir.0);
        let conn = Connection::open(dir.0.join("memory.sqlite")).expect("open");
        conn.execute_batch(sql).expect(sql);
        drop(conn);
        // A table the admission refuses is a typed outcome too.
        let Ok(store) = StoreFile::open(&dir.0.join("memory.sqlite")) else {
            outcomes += 1;
            continue;
        };
        match store.refold() {
            Ok(r) => assert!(r.status == "ok" || r.status == "diverged"),
            Err(FoldError::Raised(class)) => assert!(!class.is_empty(), "{sql}"),
            Err(FoldError::Store(_)) => {}
        }
        outcomes += 1;
    }
    assert_eq!(outcomes, payloads.len() + tables.len());
}
