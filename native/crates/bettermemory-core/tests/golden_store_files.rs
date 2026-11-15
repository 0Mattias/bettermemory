//! Every case of `store_files.jsonl`: its base from `store_bases.json`
//! rebuilt with SQLite, its recipe applied, read by the native build and
//! compared field for field with the reference's committed answers: the
//! admission's refusal (each within a time bound), or the version
//! contract's outcome, the pragmas, `sqlite_master`, meta, every table's
//! rows, the memories, tombstones and episodes, the chain report,
//! `history_replaced`, and for the FTS case the prefilter per query and
//! filter. Cases pinned by digest are compared by the same digest. No
//! reference Python is needed.
//!
//! Also here: the crate's copy of the reference's schema texts against
//! `store_admission.json`.

mod common;

use std::sync::mpsc;
use std::time::{Duration, Instant};

use bettermemory_core::log::{ChainReport, ReportDict, ReportValue};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::keys::load_key_ring;
use bettermemory_core::store::{
    admission, open_checks, CandidateFilters, SqlValue, StoreError, StoreFile,
};
use bettermemory_core::unicode;
use common::parity::{self, Ledger};
use common::stores::{cases, materialize, scratch};
use common::{assert_count, committed_path, golden_path};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

/// What a message names the store's path as.
const STORE: &str = "<store>";
/// How long any one case's native read may take before the test fails.
const BOUND: Duration = Duration::from_secs(10);

pub fn value_json(v: &SqlValue) -> Value {
    match v {
        SqlValue::Null => Value::Null,
        SqlValue::Integer(i) => json!(i),
        SqlValue::Real(f) => json!({ "float": format!("{:016x}", f.to_bits()) }),
        SqlValue::Text(t) => json!(t),
        SqlValue::Blob(b) => json!({ "blob": common::hex(b) }),
    }
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
    let mut m = Map::new();
    for (k, v) in d {
        assert!(m.insert((*k).to_owned(), report_value(v)).is_none());
    }
    Value::Object(m)
}

fn report_json(r: &ChainReport) -> Value {
    json!({
        "status": r.status,
        "rows": r.rows,
        "segments": r.segments.iter().map(dict).collect::<Vec<_>>(),
        "problems": r.problems.iter().map(dict).collect::<Vec<_>>(),
        "head": dict(&r.head),
        "origin": dict(&r.origin),
    })
}

fn py_json(v: &PyValue) -> Value {
    match v {
        PyValue::None => Value::Null,
        PyValue::Bool(b) => json!(b),
        PyValue::Int(t) => serde_json::from_str(t).expect("int"),
        PyValue::Float(f) => json!(f),
        PyValue::Str(s) => json!(unicode::to_string(s).expect("UTF-8")),
        PyValue::List(items) | PyValue::Tuple(items) => {
            Value::Array(items.iter().map(py_json).collect())
        }
        PyValue::Dict(items) => Value::Object(
            items
                .iter()
                .map(|(k, v)| match k {
                    PyValue::Str(k) => (unicode::to_string(k).expect("key"), py_json(v)),
                    other => panic!("key {other:?}"),
                })
                .collect(),
        ),
        other => panic!("no JSON form for {other:?}"),
    }
}

fn list(items: &[PyValue]) -> Value {
    Value::Array(items.iter().map(py_json).collect())
}

/// `digest` of the generator: sha256 of compact JSON, keys sorted.
fn digest(v: &Value) -> String {
    common::hex(&Sha256::digest(
        serde_json::to_string(v).expect("JSON").as_bytes(),
    ))
}

const PRAGMAS: [&str; 6] = [
    "application_id",
    "user_version",
    "journal_mode",
    "page_size",
    "encoding",
    "auto_vacuum",
];

/// The cases where the reference raises a TypeError out of
/// `connect_read_only` (a bug in the reference, not ported) and the crate
/// refuses: the case and the crate's `why`. The fixture header flags them.
/// The cases in store_files.jsonl, those the reference refuses and those
/// it raises on.
const CASES: usize = 40;
const REFUSED: usize = 18;
const RAISED: usize = 4;

const DELIBERATE: [(&str, &str); 1] = [(
    "blob_sql",
    "it is not a store this build wrote: its schema's sql holds a Blob, not text",
)];

fn refusal_json(r: &admission::Refusal, version: &str) -> Value {
    if let Some(raised) = &r.reference_raises {
        // The reference raises this OperationalError where the crate refuses.
        return json!({"outcome": "raised", "type": "OperationalError", "message": raised});
    }
    let mut out = json!({
        "outcome": "refused_store",
        "type": if r.later.is_some() { "LaterStoreError" } else { "StoreRefusedError" },
        "why": r.why,
        "message": r.message(STORE, version),
        "timed_out": r.timed_out,
        "holds_meta": r.holds_meta,
    });
    if let Some(l) = &r.later {
        out["key"] = json!(l.key);
        out["needed"] = json!(l.needed);
    }
    out
}

fn filters_of(options: &Value) -> CandidateFilters {
    let mut f = CandidateFilters::default();
    if let Some(scopes) = options.get("scopes") {
        f.scopes = scopes
            .as_array()
            .expect("scopes")
            .iter()
            .map(|s| s.as_str().expect("scope").to_owned())
            .collect();
    }
    f.client = options
        .get("client")
        .map(|c| c.as_str().expect("client").to_owned());
    f.model = options
        .get("model")
        .map(|c| c.as_str().expect("model").to_owned());
    if let Some(n) = options.get("max_results") {
        f.max_results = n.as_i64().expect("max_results");
    }
    f
}

/// What the native build answers about one case, in the committed shape.
fn native_answers(dir: &std::path::Path, case: &Value, version: &str, full: bool) -> Value {
    let store = match StoreFile::open(&dir.join("memory.sqlite")) {
        Ok(s) => s,
        Err(StoreError::Admission(r)) => return json!({ "open": refusal_json(&r, version) }),
        Err(e) => panic!("open: {e}"),
    };
    let mut out = Map::new();
    out.insert(
        "pragmas".into(),
        Value::Object(
            PRAGMAS
                .iter()
                .map(|p| ((*p).to_owned(), value_json(&store.pragma(p).expect(p))))
                .collect(),
        ),
    );
    let master = Value::Array(
        store
            .master()
            .expect("master")
            .iter()
            .map(|row| Value::Array(row.iter().map(value_json).collect()))
            .collect(),
    );
    let meta = store.meta().expect("meta");
    let meta_json = Value::Object(meta.iter().map(|(k, v)| (k.clone(), json!(v))).collect());
    out.insert(
        "open".into(),
        match open_checks(&meta, STORE, version) {
            Ok(o) => json!({
                "outcome": if o.verdict.read_only.is_some() { "read_only" } else { "readable" },
                "reason": o.verdict.read_only,
                "revision": o.verdict.revision,
                "min_reader": o.verdict.min_reader,
                "min_writer": o.verdict.min_writer,
            }),
            Err(StoreError::Version(e)) => json!({
                "outcome": "refused",
                "message": e.message,
                "key": e.key,
                "needed": e.needed,
            }),
            Err(other) => panic!("open: {other}"),
        },
    );
    let mut tables = Map::new();
    let mut counts = Map::new();
    for name in store.table_names().expect("tables") {
        if name.starts_with("memories_fts") {
            continue;
        }
        let mut rows = Vec::new();
        store
            .each_row(&name, &mut |row| {
                rows.push(Value::Array(row.iter().map(value_json).collect()))
            })
            .expect("rows");
        counts.insert(name.clone(), json!(rows.len()));
        tables.insert(name, Value::Array(rows));
    }
    let record = |m: &bettermemory_core::store::MemoryRow| {
        let mut r = json!({
            "id": m.id,
            "created": m.created.isoformat(),
            "updated": m.updated.isoformat(),
            "scopes": list(&m.scopes),
            "confidence": m.confidence,
            "source": m.source,
            "body": m.body,
            "category": m.category,
            "last_verified_at": m.last_verified_at.as_ref().map(|d| d.isoformat()),
            "verified_paths": list(&m.verified_paths),
            "verified_commits": list(&m.verified_commits),
            "verified_versions": list(&m.verified_versions),
            "verified_absent_paths": list(&m.verified_absent_paths),
            "claims": list(&m.claims),
            "verified_head": m.verified_head,
        });
        if let Some((removed, reason, session)) = &m.removed {
            r["removed"] = json!(removed.isoformat());
            r["removed_reason"] = json!(reason);
            r["removed_session"] = json!(session);
        }
        r
    };
    let mut memories = Vec::new();
    store
        .each_memory(&mut |m| {
            assert!(m.to_search_record().is_some(), "a ranker record");
            memories.push(record(&m));
        })
        .expect("memories");
    let mut tombstones = Vec::new();
    store
        .each_tombstone(&mut |m| tombstones.push(record(&m)))
        .expect("tombstones");
    let mut episodes = Vec::new();
    store
        .each_episode(&mut |e| {
            episodes.push(json!({
                "id": e.id,
                "session_id": e.session_id,
                "created": e.created.isoformat(),
                "body": e.body,
                "scopes": list(&e.scopes),
                "takeaway": e.takeaway,
                "is_floor": e.is_floor,
                "swarm_id": e.swarm_id,
            }))
        })
        .expect("episodes");
    out.insert("row_counts".into(), Value::Object(counts));
    let records = json!({
        "memories": memories,
        "tombstones": tombstones,
        "episodes": episodes,
    });
    if full {
        out.insert("sqlite_master".into(), master);
        out.insert("meta".into(), meta_json);
        out.insert("tables".into(), Value::Object(tables));
        for k in ["memories", "tombstones", "episodes"] {
            out.insert(k.into(), records[k].clone());
        }
    } else {
        out.insert("sqlite_master_sha256".into(), json!(digest(&master)));
        out.insert("meta_sha256".into(), json!(digest(&meta_json)));
        out.insert(
            "tables_sha256".into(),
            json!(digest(&Value::Object(tables))),
        );
        out.insert("records_sha256".into(), json!(digest(&records)));
    }
    let get = |k: &str| meta.iter().find(|(key, _)| key == k).expect(k).1.clone();
    let ring = load_key_ring(&dir.join("keys"), &get("store_id")).expect("ring");
    let report = store
        .verify_chain(&ring, &get("key_fingerprint"))
        .expect("report");
    out.insert("chain".into(), report_json(&report));
    let replaced = store.history_replaced().expect("history_replaced");
    out.insert(
        "history_replaced".into(),
        Value::Array(
            replaced
                .iter()
                .map(|entry| {
                    Value::Object(
                        entry
                            .iter()
                            .map(|(k, v)| ((*k).to_owned(), py_json(v)))
                            .collect(),
                    )
                })
                .collect(),
        ),
    );
    if let Some(queries) = case.get("fts").and_then(Value::as_array) {
        let mut fts = Vec::new();
        for q in queries {
            let query = q["query"].as_str().expect("query");
            let options = &q["options"];
            let candidates = store
                .query_candidates(query, &filters_of(options))
                .expect("candidates");
            fts.push(json!({
                "query": query,
                "options": options,
                "match_query": bettermemory_core::lexicon::fts_match_query(&unicode::cps(query)),
                "candidates": candidates
                    .iter()
                    .map(|(id, s)| json!([id, format!("{:016x}", s.to_bits())]))
                    .collect::<Vec<_>>(),
                "ranked_rows": store.ranked_rows(query).expect("ranked"),
            }));
        }
        out.insert("fts".into(), Value::Array(fts));
    }
    Value::Object(out)
}

/// Compare two JSON values, counting the leaves compared; panics naming
/// the first path that differs.
fn compare(path: &str, want: &Value, got: &Value, leaves: &mut usize) {
    match (want, got) {
        (Value::Object(w), Value::Object(g)) => {
            let wk: Vec<_> = w.keys().collect();
            let gk: Vec<_> = g.keys().collect();
            assert_eq!(wk, gk, "{path}: keys");
            for (k, v) in w {
                compare(&format!("{path}.{k}"), v, &g[k], leaves);
            }
        }
        (Value::Array(w), Value::Array(g)) => {
            assert_eq!(w.len(), g.len(), "{path}: length");
            for (i, (a, b)) in w.iter().zip(g).enumerate() {
                compare(&format!("{path}[{i}]"), a, b, leaves);
            }
        }
        _ => {
            assert_eq!(want, got, "{path}");
            *leaves += 1;
        }
    }
}

#[test]
fn every_case_every_answer() {
    let (header, cases) = cases();
    let checked = run_cases(&header, &cases);
    // Pinned as literals: a line deleted from the file fails here.
    assert_eq!(checked, CASES, "store_files.jsonl cases");
    let outcomes = |o: &str| cases.iter().filter(|c| c["open"]["outcome"] == o).count();
    assert_eq!(
        (outcomes("refused_store"), outcomes("raised")),
        (REFUSED, RAISED),
        "refused and raised cases"
    );
}

/// The cases of another file in store_files.jsonl's form, named by
/// `BM_STORE_CASES` (a randomized tampering run writes one).
#[test]
#[ignore = "runs the cases a BM_STORE_CASES file names"]
fn cases_from_env() {
    let path = std::env::var("BM_STORE_CASES").expect("BM_STORE_CASES");
    let mut lines = common::jsonl(&path);
    let header = lines.remove(0);
    let checked = run_cases(&header, &lines);
    assert_count(&path, checked, lines.len());
}

fn run_cases(header: &Value, cases: &[Value]) -> usize {
    let version = header["reference_version"]
        .as_str()
        .expect("version")
        .to_owned();
    let full: Vec<String> = header["full"]
        .as_array()
        .expect("full")
        .iter()
        .map(|v| v.as_str().expect("name").to_owned())
        .collect();
    let mut checked = 0;
    let mut lines = Vec::new();
    // bm25 bits are one platform's (class iii): a differing case is set
    // aside, named, where the file was made elsewhere.
    let mut ledger = Ledger::new("every_case_every_answer", "store_files.jsonl");
    for case in cases {
        let name = case["case"].as_str().expect("case").to_owned();
        let dir = scratch(&name);
        materialize(case, &dir.0);
        let is_full = full.contains(&name);
        // The read runs on its own thread: one that hangs fails the test.
        let (tx, rx) = mpsc::channel();
        let (path, owned, v) = (dir.0.clone(), case.clone(), version.clone());
        let started = Instant::now();
        std::thread::spawn(move || {
            let _ = tx.send(native_answers(&path, &owned, &v, is_full));
        });
        let got = rx
            .recv_timeout(BOUND)
            .unwrap_or_else(|_| panic!("{name}: no answer within {BOUND:?}"));
        let took = started.elapsed();
        let mut want = case.as_object().expect("object").clone();
        for k in ["case", "what", "base", "recipe"] {
            want.remove(k);
        }
        if let Some((_, why)) = DELIBERATE.iter().find(|(c, _)| *c == name) {
            assert_eq!(
                want["open"]["type"], "TypeError",
                "{name}: the reference's outcome"
            );
            want = json!({"open": {
                "outcome": "refused_store",
                "type": "StoreRefusedError",
                "why": why,
                "message": format!("{STORE} is not opened: {why}"),
                "timed_out": false,
                "holds_meta": null,
            }})
            .as_object()
            .expect("object")
            .clone();
        }
        let mut leaves = 0;
        ledger.case(&name, || {
            compare(&name, &Value::Object(want), &got, &mut leaves)
        });
        lines.push(format!(
            "{name}: {} {leaves} values in {:.1} ms",
            got["open"]["outcome"].as_str().unwrap_or("?"),
            took.as_secs_f64() * 1000.0
        ));
        checked += 1;
    }
    eprintln!("{}", lines.join("\n"));
    ledger.finish(checked);
    checked
}

#[test]
fn the_crates_schema_texts_are_the_references() {
    // The crate compiles in `reference_schema.json`: pinned to the
    // committed file always.
    let b: Value = serde_json::from_str(admission::reference_schema_json()).expect("JSON");
    let committed =
        std::fs::read_to_string(committed_path("store_admission.json")).expect("golden");
    let a: Value = serde_json::from_str(&committed).expect("JSON");
    assert_eq!(a, b, "the compiled-in schema is the committed file's");
    // And the directory under test holds the same schema texts: they are
    // the reference's source strings, not the platform's. Set aside, named,
    // only where a gate of the file is open.
    let under_test = std::fs::read_to_string(golden_path("store_admission.json")).expect("golden");
    let a: Value = serde_json::from_str(&under_test).expect("JSON");
    let mut ledger = Ledger::new(
        "the_crates_schema_texts_are_the_references",
        "store_admission.json",
    );
    ledger.case("store_admission.json under test", || assert_eq!(a, b));
    ledger.finish(1);
}

/// One store file as the reference's SQLite (3.50.4) wrote it, not rebuilt:
/// the bundled SQLite reads its bytes and every answer is the reference's,
/// the FTS prefilter's rows and bm25 bits and the shadow tables' schema
/// rows included.
#[test]
fn the_reference_written_file_reads_the_same() {
    let mut ledger = Ledger::new(
        "the_reference_written_file_reads_the_same",
        "store_real.sqlite",
    );
    ledger.case("store_real.sqlite", reference_written_file);
    ledger.finish(1);
}

/// The committed file is SQLite 3.50.4's; an as-found set's is the SQLite
/// its Python links, which its manifest names.
fn reference_written_file() {
    let doc: Value = serde_json::from_str(
        &std::fs::read_to_string(golden_path("store_real.json")).expect("golden"),
    )
    .expect("JSON");
    let bytes = std::fs::read(golden_path("store_real.sqlite")).expect("store_real.sqlite");
    assert_eq!(
        doc["sqlite_version"],
        parity::provenance().sqlite.as_str(),
        "written by the reference's SQLite"
    );
    assert_eq!(
        common::hex(&Sha256::digest(&bytes)),
        doc["file_sha256"].as_str().expect("sha256"),
        "the committed file is the one its answers are about"
    );
    let dir = scratch("real");
    std::fs::create_dir_all(dir.0.join("keys")).expect("keys dir");
    std::fs::write(dir.0.join("memory.sqlite"), &bytes).expect("copy the bytes as they are");
    let store_id = doc["store_id"].as_str().expect("store id");
    for (suffix, text) in doc["keys"].as_object().expect("keys") {
        let raw: Vec<u8> = (0..text.as_str().expect("hex").len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&text.as_str().expect("hex")[i..i + 2], 16).expect("hex"))
            .collect();
        std::fs::write(dir.0.join("keys").join(format!("{store_id}{suffix}")), raw).expect("key");
    }
    let want = doc["answers"].clone();
    let version = doc["reference_version"].as_str().expect("version");
    let got = native_answers(&dir.0, &want, version, true);
    let mut leaves = 0;
    compare("store_real", &want, &got, &mut leaves);
    let shadows = want["sqlite_master"]
        .as_array()
        .expect("master")
        .iter()
        .filter(|r| {
            r[1].as_str()
                .is_some_and(|n| n.starts_with("memories_fts_"))
        })
        .count();
    assert_eq!(shadows, 4, "the FTS shadow tables' rows are compared");
    assert_eq!(want["fts"].as_array().expect("fts").len(), 32);
    assert_eq!(
        std::fs::read(dir.0.join("memory.sqlite")).expect("reread"),
        bytes,
        "the file is read without a write"
    );
    eprintln!(
        "store_real.sqlite: {} bytes, {leaves} values equal",
        bytes.len()
    );
}

/// The crate's answers about the store in `BM_STORE_DIR` (its
/// `memory.sqlite` and `keys`), written as JSON to `BM_STORE_OUT`: for
/// comparing with the reference on stores no fixture holds (a real restore
/// in a scratch directory).
#[test]
#[ignore = "answers about the store BM_STORE_DIR names"]
fn answers_of_env_store() {
    let dir = std::env::var("BM_STORE_DIR").expect("BM_STORE_DIR");
    let out = std::env::var("BM_STORE_OUT").expect("BM_STORE_OUT");
    let version = std::env::var("BM_REFERENCE_VERSION").unwrap_or_else(|_| "9.0.0".into());
    let got = native_answers(std::path::Path::new(&dir), &json!({}), &version, true);
    std::fs::write(out, serde_json::to_string(&got).expect("JSON")).expect("write");
}
