//! What the writing-open tests share: `store_open.jsonl`'s cases set up
//! as `gen_store_open.setup` sets them up, a store's state read as
//! `gen_store_open.state` reads it, and the provider the open runs with.

use std::fs;
use std::path::Path;

use bettermemory_core::log::Key;
use bettermemory_core::store::create::{self, Fixed};
use rusqlite::types::ValueRef;
use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use super::{created, hex, jsonl, stores};

/// The header and the cases of `store_open.jsonl`.
pub fn cases() -> (Value, Vec<Value>) {
    let mut lines = jsonl("store_open.jsonl");
    let header = lines.remove(0);
    (header, lines)
}

/// `gen_store_open.ENGINE`: the engine version the fixture's opens run
/// as (the golden test holds the header to it). A constant, so the tests
/// that do not read the fixture run where it is not generated (Windows).
pub const ENGINE: &str = "9.0.0";

/// A provider whose engine version is the fixture's; the open draws
/// nothing else.
pub fn provider(header: &Value) -> Fixed {
    assert_eq!(
        header["engine_version"], ENGINE,
        "store_open.jsonl's engine"
    );
    engine_provider()
}

/// [`provider`] without the fixture.
pub fn engine_provider() -> Fixed {
    Fixed {
        store_id: String::new(),
        membership_store_id: String::new(),
        key: Key::new(vec![0; 32]),
        clock: Vec::new(),
        engine_version: ENGINE.to_owned(),
    }
}

/// `gen_store_open._made`: a base rebuilt, or the created store.
pub fn made(base: &str, case: &Path) {
    if base == "created" {
        let doc = created::fixture();
        create::create(case, &case.join("keys"), &mut created::fixed(&doc)).expect("create");
        return;
    }
    stores::rebuild(&stores::bases()["bases"][base], case);
}

fn bind_text(target: &Path) -> String {
    let resolved = match fs::canonicalize(target) {
        Ok(p) => p,
        Err(_) => {
            let parent = fs::canonicalize(target.parent().expect("parent"))
                .unwrap_or_else(|_| target.parent().unwrap().to_path_buf());
            parent.join(target.file_name().expect("name"))
        }
    };
    let text = resolved.to_str().expect("UTF-8");
    format!(
        "{{\"path\": {}}}",
        serde_json::to_string(text).expect("json")
    )
}

/// Leave `sql` committed only in a `-wal` beside its `-shm`.
fn live_wal(store: &Path, sql: &str) {
    let keep = store.parent().expect("dir").join(".live");
    fs::create_dir(&keep).expect("keep");
    let name = |suffix: &str| {
        let mut s = store.as_os_str().to_owned();
        s.push(suffix);
        std::path::PathBuf::from(s)
    };
    let conn = Connection::open(store).expect("open");
    conn.query_row("PRAGMA wal_autocheckpoint = 0", [], |_| Ok(()))
        .expect("autocheckpoint");
    conn.execute_batch(sql).expect("sql");
    for suffix in ["", "-wal", "-shm"] {
        fs::copy(name(suffix), keep.join(format!("f{suffix}"))).expect("copy");
    }
    drop(conn);
    for suffix in ["", "-wal", "-shm"] {
        fs::rename(keep.join(format!("f{suffix}")), name(suffix)).expect("put back");
    }
    fs::remove_dir(&keep).expect("rmdir");
}

/// `gen_store_open._hot_journal`: `sql` uncommitted, its rollback
/// journal beside the file.
fn hot_journal(store: &Path, sql: &str) {
    let keep = store.parent().expect("dir").join(".hot");
    fs::create_dir(&keep).expect("keep");
    let journal = {
        let mut s = store.as_os_str().to_owned();
        s.push("-journal");
        std::path::PathBuf::from(s)
    };
    let conn = Connection::open(store).expect("open");
    conn.execute_batch("BEGIN IMMEDIATE").expect("begin");
    conn.execute_batch(sql).expect("sql");
    fs::copy(store, keep.join("f")).expect("copy");
    fs::copy(&journal, keep.join("f-journal")).expect("copy journal");
    conn.execute_batch("ROLLBACK").expect("rollback");
    drop(conn);
    fs::rename(keep.join("f"), store).expect("put back");
    fs::rename(keep.join("f-journal"), &journal).expect("put back journal");
    fs::remove_dir(&keep).expect("rmdir");
}

/// `gen_store_open.setup`.
pub fn setup(recipe: &[Value], case: &Path, store_id: &str) {
    let rel = |op: &Value, key: &str| case.join(op[key].as_str().expect(key));
    for op in recipe {
        if let Some(rel) = op.get("bind") {
            let text = bind_text(&case.join(rel.as_str().expect("rel")));
            fs::write(case.join("keys").join(format!("{store_id}.path")), text).expect("bind");
        } else if let Some(raw) = op.get("bind_raw") {
            let spelled = format!(
                "{}/{}",
                case.to_str().expect("UTF-8"),
                raw.as_str().expect("raw")
            );
            let text = format!(
                "{{\"path\": {}}}",
                serde_json::to_string(&spelled).expect("json")
            );
            fs::write(case.join("keys").join(format!("{store_id}.path")), text).expect("bind");
        } else if let Some(rel) = op.get("copy_store") {
            let target = case.join(rel.as_str().expect("rel"));
            fs::create_dir_all(target.parent().expect("parent")).expect("dir");
            fs::copy(case.join("memory.sqlite"), target).expect("copy");
        } else if op.get("sql_at").is_some() {
            let conn = Connection::open(rel(op, "sql_at")).expect("open");
            conn.execute_batch(op["sql"].as_str().expect("sql"))
                .expect("sql");
        } else if op.get("file").is_some() {
            let target = rel(op, "file");
            fs::create_dir_all(target.parent().expect("parent")).expect("dir");
            let bytes = (0..op["hex"].as_str().expect("hex").len() / 2)
                .map(|i| {
                    u8::from_str_radix(&op["hex"].as_str().expect("hex")[2 * i..2 * i + 2], 16)
                        .expect("hex")
                })
                .collect::<Vec<u8>>();
            fs::write(target, bytes).expect("file");
        } else if op.get("mkdir").is_some() {
            fs::create_dir_all(rel(op, "mkdir")).expect("mkdir");
        } else if op.get("symlink").is_some() {
            #[cfg(unix)]
            std::os::unix::fs::symlink(op["to"].as_str().expect("to"), rel(op, "symlink"))
                .expect("symlink");
            #[cfg(not(unix))]
            panic!("a symbolic link case is POSIX only");
        } else if op.get("chmod").is_some() {
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let mode =
                    u32::from_str_radix(op["mode"].as_str().expect("mode"), 8).expect("mode");
                fs::set_permissions(rel(op, "chmod"), fs::Permissions::from_mode(mode))
                    .expect("chmod");
            }
            #[cfg(not(unix))]
            panic!("a mode case is POSIX only");
        } else if let Some(sql) = op.get("hot_journal") {
            hot_journal(&case.join("memory.sqlite"), sql.as_str().expect("sql"));
        } else if let Some(sql) = op.get("live_wal") {
            let at = op
                .get("at")
                .and_then(Value::as_str)
                .unwrap_or("memory.sqlite");
            live_wal(&case.join(at), sql.as_str().expect("sql"));
        } else if op.get("remove_store").is_some() {
            fs::remove_file(case.join("memory.sqlite")).expect("remove");
        } else {
            stores::apply(std::slice::from_ref(op), case);
        }
    }
}

/// `gen_store_open_upkeep.NOT_UTF8`: how that fixture's state spells a
/// text that is not UTF-8, before its bytes in hex and a closing `>`.
pub const NOT_UTF8: &str = "<not UTF-8: ";

fn cell(v: ValueRef<'_>, spell_not_utf8: bool) -> String {
    match v {
        ValueRef::Null => "N".to_owned(),
        ValueRef::Integer(i) => format!("i{i}"),
        ValueRef::Real(f) => format!("r{}", bettermemory_core::float_repr::repr(f)),
        ValueRef::Text(t) => {
            let t = match std::str::from_utf8(t) {
                Ok(t) => t.to_owned(),
                Err(_) if spell_not_utf8 => format!("{NOT_UTF8}{}>", hex(t)),
                Err(e) => panic!("a text that is not UTF-8: {e}"),
            };
            format!("t{}:{t}", t.len())
        }
        ValueRef::Blob(b) => format!("b{}", hex(b)),
    }
}

fn sha(text: &str) -> String {
    hex(&Sha256::digest(text.as_bytes()))
}

/// `gen_store_open._scrub`.
pub fn scrub(text: &str, root: &Path) -> String {
    let mut spellings = vec![root.display().to_string()];
    if let Ok(c) = fs::canonicalize(root) {
        spellings.push(c.display().to_string());
    }
    spellings.sort_by_key(|s| std::cmp::Reverse(s.len()));
    let mut out = text.to_owned();
    for s in spellings {
        out = out.replace(&s, "<scratch>");
    }
    out
}

fn mode(path: &Path) -> String {
    created::mode_of(path, "0600")
}

/// `gen_store_open.state`: a text that is not UTF-8 panics, as the
/// reference's reader raises on one.
pub fn state(case: &Path, root: &Path, store_id: &str, pragmas: &[Value]) -> Value {
    state_reading(case, root, store_id, pragmas, false)
}

/// [`state`] as `gen_store_open_upkeep.state` reads it: a text that is
/// not UTF-8 (a case there leaves one in the log) spelled exactly, as
/// [`NOT_UTF8`] and its bytes in hex.
pub fn state_not_utf8(case: &Path, root: &Path, store_id: &str, pragmas: &[Value]) -> Value {
    state_reading(case, root, store_id, pragmas, true)
}

fn state_reading(
    case: &Path,
    root: &Path,
    store_id: &str,
    pragmas: &[Value],
    spell: bool,
) -> Value {
    let mut out = Map::new();
    let mut names: Vec<String> = fs::read_dir(case)
        .expect("case")
        .map(|e| e.expect("entry").file_name().to_string_lossy().into_owned())
        .filter(|n| n != "keys")
        .collect();
    names.sort();
    out.insert("dir".into(), json!(names));
    let mut keys = Vec::new();
    let keys_dir = case.join("keys");
    if keys_dir.is_dir() {
        let mut paths: Vec<_> = fs::read_dir(&keys_dir)
            .expect("keys")
            .map(|e| e.expect("entry").path())
            .collect();
        paths.sort();
        for p in paths {
            let name = p.file_name().unwrap().to_string_lossy().into_owned();
            let mut item = json!({ "name": name.replace(store_id, "<id>"), "mode": mode(&p) });
            let ext = p.extension().map(|e| e.to_string_lossy().into_owned());
            if matches!(ext.as_deref(), Some("head" | "origin" | "path")) || name.ends_with(".json")
            {
                let text = String::from_utf8_lossy(&fs::read(&p).expect("text")).into_owned();
                item["text"] = json!(scrub(&text, root));
            } else if name.ends_with(".key") || name.ends_with(".pending") {
                item["sha256"] = json!(hex(&Sha256::digest(fs::read(&p).expect("key"))));
            } else {
                item["size"] = json!(fs::metadata(&p).expect("meta").len());
            }
            keys.push(item);
        }
    }
    out.insert("keys".into(), Value::Array(keys));
    let store = case.join("memory.sqlite");
    if !store.is_file() {
        return Value::Object(out);
    }
    out.insert("mode".into(), json!(mode(&store)));
    // By path, not by URI: a Windows path holds characters a URI reads
    // otherwise (the verbatim prefix has a `?`; CI run on 5d6161e4).
    let conn = Connection::open_with_flags(&store, OpenFlags::SQLITE_OPEN_READ_ONLY).expect("open");
    let master: Vec<[String; 4]> = match conn
        .prepare("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")
    {
        Ok(mut s) => s
            .query_map([], |r| {
                Ok([
                    cell(r.get_ref(0)?, spell),
                    cell(r.get_ref(1)?, spell),
                    cell(r.get_ref(2)?, spell),
                    cell(r.get_ref(3)?, spell),
                ])
            })
            .expect("master")
            .collect::<Result<_, _>>()
            .expect("master"),
        Err(_) => {
            out.insert("unreadable".into(), json!("DatabaseError"));
            return Value::Object(out);
        }
    };
    let canonical = |rows: &[Vec<String>]| {
        rows.iter()
            .map(|r| r.join("\u{1f}"))
            .collect::<Vec<_>>()
            .join("\u{1e}")
    };
    let master_rows: Vec<Vec<String>> = master.iter().map(|r| r.to_vec()).collect();
    out.insert("master".into(), json!(sha(&canonical(&master_rows))));
    let mut prag = Map::new();
    for p in pragmas {
        let p = p.as_str().expect("pragma");
        let v = conn
            .query_row(&format!("PRAGMA {p}"), [], |r| {
                Ok(match r.get_ref(0)? {
                    ValueRef::Integer(i) => json!(i),
                    ValueRef::Text(t) => json!(String::from_utf8_lossy(t)),
                    _ => Value::Null,
                })
            })
            .expect("pragma");
        prag.insert(p.to_owned(), v);
    }
    out.insert("pragmas".into(), Value::Object(prag));
    let raw: Vec<(String, String, String, Option<String>)> = conn
        .prepare("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")
        .expect("master")
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))
        .expect("master")
        .collect::<Result<_, _>>()
        .expect("master");
    let virtual_tables: Vec<String> = raw
        .iter()
        .filter(|(k, _, _, s)| k == "table" && s.as_deref().unwrap_or("").contains("VIRTUAL"))
        .map(|(_, n, _, _)| n.clone())
        .collect();
    let mut lines = String::new();
    for (kind, name, _, sql) in &raw {
        if kind != "table"
            || virtual_tables.contains(name)
            || name.starts_with("sqlite_")
            || virtual_tables
                .iter()
                .any(|v| name.starts_with(&format!("{v}_")))
        {
            continue;
        }
        let width = conn
            .prepare(&format!("PRAGMA table_info(\"{name}\")"))
            .expect("info")
            .query_map([], |_| Ok(()))
            .expect("info")
            .count();
        let rowid = !sql
            .as_deref()
            .unwrap_or("")
            .to_uppercase()
            .contains("WITHOUT ROWID");
        let select = if rowid { "rowid, *" } else { "*" };
        let n = width + usize::from(rowid);
        let order: Vec<String> = (1..=n).map(|i| i.to_string()).collect();
        let rows: Vec<Vec<String>> = conn
            .prepare(&format!(
                "SELECT {select} FROM \"{name}\" ORDER BY {}",
                order.join(", ")
            ))
            .expect("select")
            .query_map([], |r| {
                Ok((0..n).map(|i| cell(r.get_ref(i).unwrap(), spell)).collect())
            })
            .expect("rows")
            .collect::<Result<_, _>>()
            .expect("rows");
        let digest = sha(&canonical(&rows));
        lines.push_str(&format!("{name}\t{}\t{digest}\n", rows.len()));
        if name == "meta" || name == "digest_state" {
            out.insert(name.clone(), json!(rows));
        } else if ["log", "digest_records", "digest_tokens"].contains(&name.as_str()) {
            out.insert(
                name.clone(),
                json!({ "count": rows.len(), "sha256": digest }),
            );
        }
    }
    out.insert("tables".into(), json!(sha(&lines)));
    Value::Object(out)
}

/// The parts of `want` and `got` that differ, by name.
pub fn differing(want: &Value, got: &Value) -> Vec<String> {
    let (Some(w), Some(g)) = (want.as_object(), got.as_object()) else {
        return vec!["(whole)".to_owned()];
    };
    let mut keys: Vec<&String> = w.keys().chain(g.keys()).collect();
    keys.sort();
    keys.dedup();
    keys.into_iter()
        .filter(|k| w.get(*k) != g.get(*k))
        .map(|k| k.to_string())
        .collect()
}
