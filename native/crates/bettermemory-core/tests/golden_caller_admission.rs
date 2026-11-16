//! `caller_admission.jsonl`: what a caller is admitted to see, given how
//! it was resolved (`store::caller`). Each table case is rebuilt from its
//! rows in a store file of its own (the tables' definitions as the
//! reference made or the case altered them), its projects read natively,
//! and every recorded answer compared: `project_home` of every key, and
//! for every resolution `nothing` and, on every surface, `admits_project`
//! of every key, `admits_row` of every scope list and home, `admitted_projects`
//! and `admits_global`. `main_store_callers_compute_the_tabulated_admission`
//! computes the admission of `search_store.jsonl`'s three callers from
//! their recorded resolutions and the main store's projects rows, holds it
//! to the admission that file tabulates, and draws and ranks every run
//! line's pool through it.

mod common;

use std::collections::HashMap;
use std::path::Path;

use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::search::{CorpusStats, Hit, Mode};
use bettermemory_core::store::caller::{
    Admission, Membership, ModelError, Resolution, Surface, Val,
};
use bettermemory_core::store::pool::{self, Filters, PoolReader};
use bettermemory_core::store::StoreFile;
use bettermemory_core::{float_repr, unicode};
use common::parity::Ledger;
use common::{jsonl, stores, unhex};
use serde_json::{json, Value};

const FILE: &str = "caller_admission.jsonl";

/// A table's recorded projects asked about (None: no home row).
type Keys = Vec<Option<PyStr>>;
/// A table's `[scopes, recorded]` pairs.
type RowKeys = Vec<(Vec<PyStr>, Option<PyStr>)>;
/// Table cases, and the resolutions asked of each (four on every table:
/// none, global, indeterminate, deferred; then the table's own).
const TABLES: usize = 23;
const CASES: usize = 145;

fn to_rusqlite(cell: &Value) -> (rusqlite::types::Value, bool) {
    use rusqlite::types::Value as V;
    match cell {
        Value::Null => (V::Null, false),
        Value::String(s) => (V::Text(s.clone()), false),
        Value::Object(o) => {
            if let Some(i) = o.get("int") {
                (
                    V::Integer(i.as_str().expect("int").parse().expect("i64")),
                    false,
                )
            } else if let Some(r) = o.get("real") {
                let bits = u64::from_str_radix(r.as_str().expect("real"), 16).expect("hex");
                (V::Real(f64::from_bits(bits)), false)
            } else if let Some(b) = o.get("blob") {
                (V::Blob(unhex(b.as_str().expect("blob"))), false)
            } else if let Some(t) = o.get("text_hex") {
                (V::Blob(unhex(t.as_str().expect("text_hex"))), true)
            } else {
                panic!("a cell: {cell}")
            }
        }
        other => panic!("a cell: {other}"),
    }
}

/// The case's two tables, in a fresh store file under `dir`.
fn rebuild_tables(header: &Value, table: &Value, path: &Path) -> rusqlite::Connection {
    let conn = rusqlite::Connection::open(path).expect("create");
    conn.execute_batch("PRAGMA synchronous = OFF")
        .expect("sync");
    for name in ["projects", "native_membership_rows"] {
        let t = &table["tables"][name];
        if t.is_null() {
            continue;
        }
        let sql = t
            .get("sql")
            .unwrap_or(&header["sql"][name])
            .as_str()
            .expect("sql");
        conn.execute_batch(sql).expect("table");
        let columns: Vec<&str> = t["columns"]
            .as_array()
            .expect("columns")
            .iter()
            .map(|c| c.as_str().expect("column"))
            .collect();
        for row in t["rows"].as_array().expect("rows") {
            let row = row.as_array().expect("row");
            let mut values = vec![rusqlite::types::Value::Integer(
                row[0].as_i64().expect("rowid"),
            )];
            let mut marks = vec!["?".to_owned()];
            for cell in &row[1..] {
                let (v, as_text) = to_rusqlite(cell);
                values.push(v);
                marks.push(if as_text { "CAST(? AS TEXT)" } else { "?" }.to_owned());
            }
            let sql = format!(
                "INSERT INTO \"{name}\"(rowid, {}) VALUES ({})",
                columns
                    .iter()
                    .map(|c| format!("\"{c}\""))
                    .collect::<Vec<_>>()
                    .join(", "),
                marks.join(", ")
            );
            conn.execute(&sql, rusqlite::params_from_iter(values))
                .expect("row");
        }
    }
    conn
}

fn key_of(v: &Value) -> Option<PyStr> {
    match v {
        Value::Null => None,
        Value::String(s) => Some(unicode::cps(s)),
        Value::Object(o) if o.contains_key("cps") => Some(common::cps_of(&o["cps"])),
        other => panic!("a key: {other}"),
    }
}

fn text_json(t: &[u32]) -> Value {
    match unicode::to_string(t) {
        Some(s) => json!(s),
        None => json!({ "cps": t }),
    }
}

/// `gen_caller_admission._key` of a value.
fn val_json(v: &Val) -> Value {
    match v {
        Val::Null => Value::Null,
        Val::Bool(b) => json!({ "bool": b }),
        Val::Int(t) => json!({ "int": t }),
        Val::Real(f) => json!({ "real": format!("{:016x}", f.to_bits()) }),
        Val::Text(t) => text_json(t),
        Val::Blob(b) => json!({ "blob": common::hex(b) }),
    }
}

fn flag(r: Result<bool, ModelError>) -> char {
    match r {
        Ok(true) => '1',
        Ok(false) => '0',
        Err(_) => 'E',
    }
}

fn scopes_of(v: &Value) -> Vec<PyStr> {
    v.as_array()
        .expect("scopes")
        .iter()
        .map(|s| unicode::cps(s.as_str().expect("scope")))
        .collect()
}

/// `gen_caller_admission._answers`.
fn answers(
    a: &Admission<'_>,
    keys: &[Option<PyStr>],
    rows: &[(Vec<PyStr>, Option<PyStr>)],
) -> Value {
    let projects: String = keys
        .iter()
        .map(|k| flag(a.admits_project(k.as_deref())))
        .collect();
    let row_flags: String = rows
        .iter()
        .map(|(s, r)| flag(a.admits_row(s, r.as_deref())))
        .collect();
    let admitted = match a.admitted_projects() {
        Ok(None) => Value::Null,
        Ok(Some(ids)) => Value::Array(ids.iter().map(|i| text_json(i)).collect()),
        Err(_) => json!("E"),
    };
    json!({
        "projects": projects,
        "rows": row_flags,
        "admitted": admitted,
        "global": flag(a.admits_global()).to_string(),
    })
}

fn home_json(a: &Admission<'_>, key: &[u32]) -> Value {
    match a.project_home(key) {
        Ok(h) => json!([
            val_json(&h.project),
            h.chain.iter().map(|c| text_json(c)).collect::<Vec<_>>(),
            val_json(&h.tenant),
            h.unresolved
        ]),
        Err(_) => json!("E"),
    }
}

#[test]
fn every_case_answers_as_the_reference() {
    let lines = jsonl(FILE);
    let header = &lines[0];
    assert_eq!(header["fixture"], "caller_admission");
    let surfaces: Vec<Surface> = header["surfaces"]
        .as_array()
        .expect("surfaces")
        .iter()
        .map(|s| Surface::parse(s.as_str().expect("surface")).expect("a surface"))
        .collect();
    assert_eq!(surfaces, Surface::ALL);
    let dir = stores::scratch("caller-admission");
    let mut ledger = Ledger::new("every_case_answers_as_the_reference", FILE);
    let (mut tables, mut cases) = (0, 0);
    let mut current: Option<(Membership, Keys, RowKeys)> = None;
    for line in &lines[1..] {
        if line.get("table").is_some() {
            tables += 1;
            let label = line["table"].as_str().expect("table").to_owned();
            let path = dir.0.join(format!("t{tables}.sqlite"));
            let conn = rebuild_tables(header, line, &path);
            let model = Membership::read(&conn);
            let keys: Keys = line["keys"]
                .as_array()
                .expect("keys")
                .iter()
                .map(key_of)
                .collect();
            let rows: RowKeys = line["row_keys"]
                .as_array()
                .expect("row_keys")
                .iter()
                .map(|p| (scopes_of(&p[0]), key_of(&p[1])))
                .collect();
            ledger.case(&label, || {
                assert_eq!(
                    model.error().map(ModelError::name),
                    line["raises"].as_str(),
                    "{label}: raises"
                );
                let a = Admission::new(Surface::SessionBrief, None, &model);
                let homes: Vec<Value> = keys.iter().flatten().map(|k| home_json(&a, k)).collect();
                assert_eq!(Value::Array(homes), line["homes"], "{label}: homes");
            });
            current = Some((model, keys, rows));
            continue;
        }
        cases += 1;
        let (model, keys, rows) = current.as_ref().expect("a table first");
        let resolution = match &line["resolution"] {
            Value::Null => None,
            r => Some(Resolution::from_json(&r.to_string()).expect("a resolution")),
        };
        let label = format!(
            "{} {}",
            line["case"].as_str().expect("case"),
            line["resolution"]
        );
        ledger.case(&label, || {
            let first = Admission::new(Surface::SessionBrief, resolution.as_ref(), model);
            let nothing = first
                .nothing()
                .map(|n| text_json(&n))
                .unwrap_or(Value::Null);
            assert_eq!(nothing, line["nothing"], "{label}: nothing");
            let by: Vec<usize> = line["by_surface"]
                .as_str()
                .expect("by_surface")
                .chars()
                .map(|c| c.to_digit(10).expect("digit") as usize)
                .collect();
            for (i, surface) in Surface::ALL.into_iter().enumerate() {
                let a = Admission::new(surface, resolution.as_ref(), model);
                let got = answers(&a, keys, rows);
                assert_eq!(got, line["answers"][by[i]], "{label} on {}", surface.name());
            }
        });
    }
    assert_eq!(tables, TABLES);
    assert_eq!(cases, CASES);
    ledger.finish(tables + cases);
}

// -- the main store of search_store.jsonl -----------------------------------

const STORE_FILE: &str = "search_store.jsonl";

fn bits(x: f64) -> String {
    format!("{:016x}", x.to_bits())
}

fn hits_json(hits: &[Hit]) -> Value {
    Value::Array(
        hits.iter()
            .enumerate()
            .map(|(i, h)| {
                json!({
                    "rank": i + 1,
                    "id": h.id,
                    "score": float_repr::repr(h.score),
                    "bits": bits(h.score),
                    "raw_score": float_repr::repr(h.raw_score),
                    "raw_bits": bits(h.raw_score),
                    "relevance": h.relevance,
                    "match_terms": h.match_terms,
                })
            })
            .collect(),
    )
}

fn stats_json(terms: &[String], found: &Option<CorpusStats>) -> Value {
    let object = |m: &HashMap<String, i64>| {
        Value::Object(m.iter().map(|(k, v)| (k.clone(), json!(v))).collect())
    };
    let mut out = vec![json!(terms)];
    match found {
        None => out.push(Value::Null),
        Some(s) => {
            out.push(json!(s.size));
            out.push(object(&s.body_df));
            out.push(object(&s.scope_df));
            out.push(match s.avgdl {
                None => Value::Null,
                Some(a) => json!(bits(a)),
            });
        }
    }
    Value::Array(out)
}

fn plain_value(v: &Value) -> rusqlite::types::Value {
    match v {
        Value::Null => rusqlite::types::Value::Null,
        Value::Number(n) => rusqlite::types::Value::Integer(n.as_i64().expect("an integer")),
        Value::String(t) => rusqlite::types::Value::Text(t.clone()),
        Value::Object(_) => to_rusqlite(v).0,
        other => panic!("a column value: {other}"),
    }
}

fn insert(conn: &rusqlite::Connection, table: &str, columns: &[Value], rows: &[Value]) {
    let names: Vec<String> = columns
        .iter()
        .map(|c| format!("\"{}\"", c.as_str().expect("column")))
        .collect();
    let marks = vec!["?"; names.len()].join(", ");
    let sql = format!(
        "INSERT INTO \"{table}\"({}) VALUES ({marks})",
        names.join(", ")
    );
    for row in rows {
        let values: Vec<_> = row
            .as_array()
            .expect("row")
            .iter()
            .map(plain_value)
            .collect();
        conn.execute(&sql, rusqlite::params_from_iter(values))
            .expect("row");
    }
}

/// `gen_store_path.rebuild_store` of the main store, then the projects
/// rows `caller_admission.jsonl` records for it.
fn main_store(store_header: &Value, ours: &Value, path: &Path) {
    let schema = &store_header["schema"];
    let conn = rusqlite::Connection::open(path).expect("create");
    conn.execute_batch(&format!(
        "PRAGMA page_size = {}",
        schema["page_size"].as_i64().expect("page_size")
    ))
    .expect("page size");
    for sql in schema["tables"].as_array().expect("tables") {
        conn.execute_batch(sql.as_str().expect("sql"))
            .expect("table");
    }
    for (table, content) in store_header["store"]["tables"].as_object().expect("tables") {
        insert(
            &conn,
            table,
            content["columns"].as_array().expect("columns"),
            content["rows"].as_array().expect("rows"),
        );
    }
    let projects = &ours["main_store"]["projects"];
    insert(
        &conn,
        "projects",
        projects["columns"].as_array().expect("columns"),
        projects["rows"].as_array().expect("rows"),
    );
    for sql in schema["rest"].as_array().expect("rest") {
        conn.execute_batch(sql.as_str().expect("sql"))
            .expect("statement");
    }
    let mode = schema["journal_mode"].as_str().expect("journal_mode");
    conn.query_row(&format!("PRAGMA journal_mode = {mode}"), [], |_| Ok(()))
        .expect("journal mode");
}

fn tabulated(data: &Value) -> pool::Admission {
    pool::Admission {
        projects: data["projects"]
            .as_object()
            .expect("projects")
            .iter()
            .map(|(k, v)| (k.clone(), v.as_bool().expect("bool")))
            .collect(),
        global_rows: data["global_rows"]
            .as_array()
            .expect("global_rows")
            .iter()
            .map(|pair| {
                let scopes = pair[0]
                    .as_array()
                    .expect("scopes")
                    .iter()
                    .map(|s| s.as_str().expect("scope").to_owned())
                    .collect();
                (scopes, pair[1].as_bool().expect("bool"))
            })
            .collect(),
    }
}

#[test]
fn main_store_callers_compute_the_tabulated_admission() {
    let ours = jsonl(FILE).swap_remove(0);
    let lines = jsonl(STORE_FILE);
    let header = &lines[0];
    let dir = stores::scratch("caller-admission-main");
    let path = dir.0.join("memory.sqlite");
    main_store(header, &ours, &path);
    let store = StoreFile::open(&path).expect("the store opens");
    let model = Membership::read(store.connection());
    assert!(model.error().is_none());
    let resolutions: HashMap<String, Resolution> = ours["main_store"]["resolutions"]
        .as_object()
        .expect("resolutions")
        .iter()
        .map(|(k, v)| {
            (
                k.clone(),
                Resolution::from_json(&v.to_string()).expect("a resolution"),
            )
        })
        .collect();
    let surfaces = [
        ("auto_scope", Surface::SearchAutoScope),
        ("unscoped", Surface::SearchUnscoped),
    ];
    let mut computed: HashMap<String, pool::Admission> = HashMap::new();
    let mut ledger = Ledger::new(
        "main_store_callers_compute_the_tabulated_admission",
        STORE_FILE,
    );
    let mut in_file = 0;
    for (name, r) in &resolutions {
        for (label, surface) in surfaces {
            let who = format!("{name}/{label}");
            let got = Admission::new(surface, Some(r), &model)
                .pool_admission(store.connection())
                .expect("an admission");
            in_file += 1;
            ledger.case(format!("{who} admission"), || {
                assert_eq!(got, tabulated(&header["admissions"][&who]), "{who}");
            });
            computed.insert(who, got);
        }
    }
    assert_eq!(computed.len(), 6);
    let now =
        DateTime::from_isoformat(&unicode::cps(header["now"].as_str().expect("now"))).expect("now");
    let fingerprint = header["digest_fingerprint"].as_str().expect("fingerprint");
    let width = header["max_results"].as_i64().expect("max_results");
    let reader = PoolReader::new(&store);
    let mut runs = 0;
    for line in lines[1..]
        .iter()
        .filter(|l| l.get("store").is_none() && l.get("hostile").is_none())
    {
        runs += 1;
        let query = line["query"].as_str().expect("query");
        let mode = Mode::parse(line["mode"].as_str().expect("mode")).expect("mode");
        for (who, admission) in &computed {
            in_file += 1;
            let filters = Filters {
                admission: Some(admission),
                ..Filters::default()
            };
            let drawn = reader
                .resolve_search_pool(query, &filters, pool::POOL_SIZE, Some(fingerprint))
                .expect("a pool");
            let mut stats = Vec::new();
            let hits = drawn
                .rank(query, &now, mode, width, &mut |t, s| {
                    stats.push(stats_json(t, s))
                })
                .expect("a ranking");
            let got = json!({"pool": drawn.ids(), "hits": hits_json(&hits), "stats": stats});
            let label = format!("{query:?} [{}] {who}", line["mode"]);
            ledger.case(&label, || assert_eq!(got, line[who.as_str()], "{label}"));
        }
    }
    assert_eq!(runs, 81);
    ledger.finish(in_file);
}
