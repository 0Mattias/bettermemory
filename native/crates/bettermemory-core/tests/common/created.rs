//! What the store-create tests share: the fixed provider of
//! `store_create.json`, a created store read back as the generator reads
//! the reference's, and the reference Python run in a child process.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use bettermemory_core::datetime::DateTime;
use bettermemory_core::log::Key;
use bettermemory_core::store::create::Fixed;
use bettermemory_core::unicode;
use rusqlite::types::ValueRef;
use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Map, Value};

use super::golden_path;

pub fn fixture() -> Value {
    let text = fs::read_to_string(golden_path("store_create.json")).expect("store_create.json");
    serde_json::from_str(&text).expect("JSON")
}

fn instant(text: &str) -> DateTime {
    DateTime::from_isoformat(&unicode::cps(&text.replace('Z', "+00:00"))).expect("a time")
}

/// The fixture's fixed values as a provider: the log fixture's key
/// (bytes 0..31).
pub fn fixed(doc: &Value) -> Fixed {
    let f = &doc["fixed"];
    let s = |k: &str| f[k].as_str().expect(k).to_owned();
    Fixed {
        store_id: s("store_id"),
        membership_store_id: s("membership_store_id"),
        key: Key::new((0u8..32).collect()),
        clock: vec![
            instant(&s("created")),
            instant(&s("log_ts")),
            instant(&s("head_ts")),
        ],
        engine_version: s("engine_version"),
    }
}

fn cell(v: ValueRef<'_>) -> Value {
    match v {
        ValueRef::Null => Value::Null,
        ValueRef::Integer(i) => json!(i),
        ValueRef::Real(f) => json!({ "real": bettermemory_core::float_repr::repr(f) }),
        ValueRef::Text(t) => json!(String::from_utf8(t.to_vec()).expect("UTF-8")),
        ValueRef::Blob(b) => json!({ "blob": super::hex(b) }),
    }
}

/// `sqlite_master`, the pragmas and every table of the store file, as
/// `gen_store_create._created` reads them.
pub fn read_store(file: &Path, pragmas: &[&str]) -> (Value, Value, Value) {
    let conn = Connection::open_with_flags(file, OpenFlags::SQLITE_OPEN_READ_ONLY).expect("open");
    let master: Vec<Value> = conn
        .prepare("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")
        .expect("master")
        .query_map([], |r| {
            Ok(Value::Array(
                (0..4).map(|i| cell(r.get_ref(i).unwrap())).collect(),
            ))
        })
        .expect("rows")
        .collect::<Result<_, _>>()
        .expect("rows");
    let mut prag = Map::new();
    for p in pragmas {
        let v = conn
            .query_row(&format!("PRAGMA {p}"), [], |r| Ok(cell(r.get_ref(0)?)))
            .expect("pragma");
        prag.insert((*p).to_owned(), v);
    }
    let names: Vec<String> = conn
        .prepare("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        .expect("names")
        .query_map([], |r| r.get(0))
        .expect("names")
        .collect::<Result<_, _>>()
        .expect("names");
    let mut tables = Map::new();
    for name in names {
        let (mut stmt, rowid) =
            match conn.prepare(&format!("SELECT rowid, * FROM \"{name}\" ORDER BY rowid")) {
                Ok(s) => (s, true),
                Err(_) => (
                    conn.prepare(&format!("SELECT * FROM \"{name}\" ORDER BY 1"))
                        .expect("select"),
                    false,
                ),
            };
        let columns: Vec<String> = stmt.column_names().map(str::to_owned).collect();
        let n = columns.len();
        let rows: Vec<Value> = stmt
            .query_map([], |r| {
                Ok(Value::Array(
                    (0..n).map(|i| cell(r.get_ref(i).unwrap())).collect(),
                ))
            })
            .expect("rows")
            .collect::<Result<_, _>>()
            .expect("rows");
        tables.insert(
            name,
            json!({ "columns": columns, "rowid": rowid, "rows": rows }),
        );
    }
    (
        Value::Array(master),
        Value::Object(prag),
        Value::Object(tables),
    )
}

/// A mode as the fixture writes it: the octal mode on unix; elsewhere
/// the one asked for (no mode bits to read).
pub fn mode_of(path: &Path, asked: &str) -> String {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = asked;
        format!(
            "{:04o}",
            fs::symlink_metadata(path)
                .expect("meta")
                .permissions()
                .mode()
                & 0o777
        )
    }
    #[cfg(not(unix))]
    {
        let _ = path;
        asked.to_owned()
    }
}

/// `text` with the scratch directory `root` (as given or resolved, plain or
/// JSON-escaped) spelled `<scratch>`, and the separators after it `/`.
pub fn scrub(text: &str, root: &Path) -> String {
    let mut spellings = vec![root.display().to_string()];
    if let Ok(c) = fs::canonicalize(root) {
        let c = c.display().to_string();
        spellings.push(c.strip_prefix(r"\\?\").unwrap_or(&c).to_owned());
    }
    let mut all: Vec<String> = spellings
        .iter()
        .flat_map(|s| [s.clone(), s.replace('\\', "\\\\")])
        .collect();
    all.sort_by_key(|s| std::cmp::Reverse(s.len()));
    let mut out = text.to_owned();
    for s in all {
        out = out.replace(&s, "<scratch>");
    }
    scratch_tails(&out)
}

/// The reference scrubber's rule (`_sandbox.Scrubber.paths`): after each
/// `<scratch>`, a run of separator-and-name pairs (`\\`, `\` or `/`, then
/// `[A-Za-z0-9._-]+`) is spelled with `/`. Nothing else in the text is
/// touched: a message may hold a backslash of its own (an escaped control
/// character; on the Windows runner a blanket replace turned `b\x1b1`
/// into `b/x1b1`, CI run on 76fc726f).
fn scratch_tails(text: &str) -> String {
    const MARK: &str = "<scratch>";
    let name = |c: char| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-');
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(at) = rest.find(MARK) {
        out.push_str(&rest[..at + MARK.len()]);
        rest = &rest[at + MARK.len()..];
        loop {
            let sep = if rest.starts_with("\\\\") {
                2
            } else if rest.starts_with('\\') || rest.starts_with('/') {
                1
            } else {
                break;
            };
            let after = &rest[sep..];
            let len = after.find(|c: char| !name(c)).unwrap_or(after.len());
            if len == 0 {
                break;
            }
            out.push('/');
            out.push_str(&after[..len]);
            rest = &after[len..];
        }
    }
    out.push_str(rest);
    out
}

/// The keys directory's files as the fixture lists them.
pub fn key_ring(keys: &Path, root: &Path) -> Value {
    let mut names: Vec<PathBuf> = fs::read_dir(keys)
        .expect("keys")
        .map(|e| e.expect("entry").path())
        .collect();
    names.sort();
    Value::Array(
        names
            .iter()
            .map(|p| {
                let name = p.file_name().unwrap().to_string_lossy().into_owned();
                let mut item = json!({ "name": name, "mode": mode_of(p, "0600") });
                let ext = p.extension().map(|e| e.to_string_lossy().into_owned());
                match ext.as_deref() {
                    Some("head" | "origin" | "path") => {
                        item["text"] = json!(scrub(&fs::read_to_string(p).expect("text"), root));
                    }
                    Some("key") => {
                        use sha2::Digest;
                        item["sha256"] =
                            json!(super::hex(&sha2::Sha256::digest(fs::read(p).expect("key"))));
                    }
                    _ => item["size"] = json!(fs::metadata(p).expect("meta").len()),
                }
                item
            })
            .collect(),
    )
}

/// The reference Python (`BM_REFERENCE_PYTHON`) when it imports
/// `bettermemory`; else why not.
pub fn reference_python() -> Result<PathBuf, String> {
    let Some(python) = std::env::var_os("BM_REFERENCE_PYTHON").map(PathBuf::from) else {
        return Err("BM_REFERENCE_PYTHON is not set".to_owned());
    };
    let probe = Command::new(&python)
        .args(["-c", "import bettermemory.store"])
        .output();
    match probe {
        Ok(o) if o.status.success() => Ok(python),
        Ok(_) => Err(format!(
            "{} cannot import bettermemory (a bare interpreter)",
            python.display()
        )),
        Err(e) => Err(format!("{} does not run: {e}", python.display())),
    }
}

/// Start a reference command with its own home and application paths, inheriting
/// only the executable search path and the OS variables needed to start Python.
pub fn reference_command(python: &Path, home: &Path) -> Command {
    fs::create_dir_all(home).expect("home");
    let mut cmd = Command::new(python);
    cmd.env_clear();
    for keep in ["PATH", "SYSTEMROOT", "SystemRoot", "TEMP", "TMP", "TMPDIR"] {
        if let Some(v) = std::env::var_os(keep) {
            cmd.env(keep, v);
        }
    }
    for (name, rel) in [
        ("HOME", ""),
        ("USERPROFILE", ""),
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_STATE_HOME", ".local/state"),
        ("XDG_DATA_HOME", ".local/share"),
        ("XDG_CACHE_HOME", ".cache"),
        ("APPDATA", "AppData/Roaming"),
        ("LOCALAPPDATA", "AppData/Local"),
        ("BETTERMEMORY_DIR", "mem"),
        ("BETTERMEMORY_KEYS_DIR", "keys-default"),
        ("BETTERMEMORY_STATE_DIR", "state"),
    ] {
        cmd.env(name, home.join(rel));
    }
    cmd.env("PYTHONDONTWRITEBYTECODE", "1");
    cmd
}

/// Run `script` with `args` in the isolated reference environment.
pub fn run_reference(python: &Path, home: &Path, script: &str, args: &[&Path]) -> Output {
    let mut cmd = reference_command(python, home);
    cmd.arg("-c").arg(script);
    for a in args {
        cmd.arg(a);
    }
    cmd.output().expect("the reference runs")
}
