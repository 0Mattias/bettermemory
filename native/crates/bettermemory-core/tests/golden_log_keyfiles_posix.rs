//! Every case of `log_keyfiles_posix.jsonl` (POSIX only): `KeyFiles` over
//! a real keys directory in a scratch folder, with links, mode-000 files,
//! folders where files are expected and anchors nested past the JSON
//! limit; the answer or the error, then every entry under the folder.
//! And every case of `log_keyfiles_darwin.jsonl` (platform gated): the
//! message of a `KeyUnreadableError` whose reason is `strerror`'s.

#![cfg(unix)]

mod common;

use std::fs;
use std::os::unix::fs::{symlink, PermissionsExt};
use std::path::{Path, PathBuf};

use bettermemory_core::log::ring::{KeyFiles, RingError};
use bettermemory_core::log::HistoryOrigin;
use bettermemory_core::pyvalue::PyValue;
use common::parity::Ledger;
use common::{assert_count, jsonl, unhex};
use serde_json::{json, Value};

fn sub(text: &str, root: &Path) -> String {
    text.replace(root.to_str().expect("UTF-8 scratch"), "<root>")
}

fn unsub(text: &str, root: &Path) -> String {
    text.replace("<root>", root.to_str().expect("UTF-8 scratch"))
}

fn chmod(path: &Path, mode: u32) {
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).expect("chmod");
}

fn build(root: &Path, setup: &[Value]) {
    for step in setup {
        let name = step[1].as_str().expect("name");
        let path = root.join(name);
        match step[0].as_str().expect("kind") {
            "file" => {
                let bytes = unhex(step[2].as_str().expect("hex"));
                let text: String = bytes.iter().map(|&b| char::from(b)).collect();
                let back: Vec<u8> = unsub(&text, root).chars().map(|c| c as u8).collect();
                fs::write(&path, back).expect("file");
            }
            "deep" => {
                let n = step[2].as_u64().expect("depth") as usize;
                fs::write(&path, format!("{}{}", "[".repeat(n), "]".repeat(n))).expect("deep");
            }
            "dir" => fs::create_dir(&path).expect("dir"),
            "link" => symlink(unsub(step[2].as_str().expect("target"), root), &path).expect("link"),
            other => panic!("unknown step {other}"),
        }
    }
    for step in setup.iter().rev() {
        if step[0] != "link" {
            let mode = step
                .as_array()
                .expect("step")
                .last()
                .unwrap()
                .as_u64()
                .unwrap();
            chmod(&root.join(step[1].as_str().unwrap()), mode as u32);
        }
    }
}

fn snapshot(root: &Path) -> Vec<Value> {
    fn walk(root: &Path, folder: &Path, out: &mut Vec<Value>) {
        let Ok(entries) = fs::read_dir(folder) else {
            return;
        };
        let mut names: Vec<String> = entries
            .map(|e| e.expect("entry").file_name().into_string().expect("UTF-8"))
            .collect();
        names.sort();
        for name in names {
            let path = folder.join(&name);
            let rel = path
                .strip_prefix(root)
                .unwrap()
                .to_str()
                .unwrap()
                .to_owned();
            let meta = fs::symlink_metadata(&path).expect("lstat");
            let mode = meta.permissions().mode() & 0o7777;
            if meta.file_type().is_symlink() {
                let target = fs::read_link(&path).unwrap();
                out.push(json!([rel, "link", sub(target.to_str().unwrap(), root)]));
            } else if meta.is_dir() {
                out.push(json!([rel, "dir", mode]));
                walk(root, &path, out);
            } else {
                chmod(&path, mode | 0o400);
                let data = fs::read(&path).expect("read");
                chmod(&path, mode);
                let shown = if data.len() > 256 {
                    json!({"len": data.len()})
                } else if data.is_ascii() && data.iter().all(|&c| c >= 0x20 || c == 10 || c == 13) {
                    json!({"text": sub(std::str::from_utf8(&data).unwrap(), root)})
                } else {
                    json!({"hex": common::hex(&data)})
                };
                out.push(json!([rel, "file", shown, mode]));
            }
        }
    }
    let mut out = Vec::new();
    walk(root, root, &mut out);
    out
}

fn unlock(path: &Path) {
    let Ok(meta) = fs::symlink_metadata(path) else {
        return;
    };
    if meta.file_type().is_symlink() {
        return;
    }
    chmod(path, 0o700);
    if meta.is_dir() {
        for entry in fs::read_dir(path).expect("list").flatten() {
            unlock(&entry.path());
        }
    }
}

fn origin_json(o: &HistoryOrigin) -> Value {
    let mut v = json!({"seq": o.seq, "mac": o.mac});
    if let Some(named) = &o.key_fingerprint {
        v["key_fingerprint"] = json!(named);
    }
    v
}

fn origin_of(v: &Value) -> HistoryOrigin {
    HistoryOrigin {
        seq: v["seq"].as_i64().expect("seq"),
        mac: v["mac"].as_str().expect("mac").to_owned(),
        key_fingerprint: v
            .get("key_fingerprint")
            .and_then(Value::as_str)
            .map(str::to_owned),
    }
}

fn path_json(p: Option<PathBuf>, root: &Path) -> Value {
    p.map_or(
        Value::Null,
        |p| json!({"path": sub(p.to_str().unwrap(), root)}),
    )
}

/// The class CPython gives an OSError of this errno.
fn os_class(name: Option<&str>) -> &'static str {
    match name {
        Some("EACCES" | "EPERM") => "PermissionError",
        Some("EISDIR") => "IsADirectoryError",
        Some("ENOTDIR") => "NotADirectoryError",
        Some("ENOENT") => "FileNotFoundError",
        Some("EEXIST") => "FileExistsError",
        _ => "OSError",
    }
}

/// The answer as the fixture spells it, and a `KeyUnreadableError`'s
/// message when its reason is `strerror`'s.
fn run(files: &KeyFiles, op: &[Value], root: &Path) -> (Value, Option<String>) {
    let errno = |code: Option<i32>| code.and_then(bettermemory_sys::errno_name);
    let result: Result<Value, RingError> = match op[0].as_str().expect("op") {
        "load_current" => files
            .load_current()
            .map(|k| k.map_or(Value::Null, |k| json!({"hex": common::hex(k.bytes())})))
            .map_err(RingError::KeyUnreadable),
        "retire_current" => files.retire_current().map(|f| json!(f)),
        "read_origin" => files
            .read_origin()
            .map(|o| o.as_ref().map_or(Value::Null, origin_json))
            .map_err(RingError::Py),
        "write_origin" => files
            .write_origin(
                op[1].as_i64().unwrap(),
                op[2].as_str().unwrap(),
                op[3].as_str(),
            )
            .map(|b| json!(b)),
        "name_origin_key" => {
            let origin = HistoryOrigin {
                seq: op[1].as_i64().unwrap(),
                mac: op[2].as_str().unwrap().to_owned(),
                key_fingerprint: None,
            };
            files
                .name_origin_key(&origin, op[3].as_str().unwrap())
                .map(|b| json!(b))
        }
        "replace_origin" => {
            let origin = (!op[1].is_null()).then(|| origin_of(&op[1]));
            files
                .replace_origin(origin.as_ref())
                .map(|o| o.as_ref().map_or(Value::Null, origin_json))
        }
        "read_binding" => files.read_binding().map(|p| path_json(p, root)),
        "rebind_store" => {
            let target = unsub(op[1].as_str().unwrap(), root);
            files
                .rebind_store(Path::new(&target), root.to_str().unwrap().as_bytes())
                .map(|p| path_json(p, root))
        }
        other => panic!("unknown op {other}"),
    };
    match result {
        Ok(v) => (json!({"ok": v}), None),
        Err(RingError::KeyUnreadable(k)) => {
            let message = sub(&k.to_string(), root);
            let mut raised = json!({"class": "KeyUnreadableError", "errno": errno(k.errno)});
            if k.reason.starts_with("it is a symbolic link") {
                raised["message"] = json!(message);
                (json!({"raised": raised}), None)
            } else {
                (json!({"raised": raised}), Some(message))
            }
        }
        Err(RingError::Os(e)) => {
            let name = errno(e.raw_os_error());
            (
                json!({"raised": {"class": os_class(name), "errno": name}}),
                None,
            )
        }
        Err(RingError::Py(e)) => (json!({"raised": {"class": e.name()}}), None),
    }
}

#[test]
fn log_keyfiles_posix_every_case() {
    let lines = jsonl("log_keyfiles_posix.jsonl");
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(rows.len() as u64));
    let id = PyValue::str(lines[0]["store_id"].as_str().expect("id"));
    let darwin = jsonl("log_keyfiles_darwin.jsonl");
    let mut want_messages = darwin[1..].iter();
    let mut ledger = Ledger::new("golden_log_keyfiles_posix", "log_keyfiles_darwin.jsonl");
    let base = fs::canonicalize(std::env::temp_dir())
        .expect("temp")
        .join(format!("bm-keyfiles-{}", std::process::id()));
    let mut failures = Vec::new();
    let mut checked = 0;
    for (i, row) in rows.iter().enumerate() {
        let name = row["case"].as_str().expect("case");
        let scratch = base.join(i.to_string());
        let root = scratch.join("r");
        fs::create_dir_all(&root).expect("root");
        build(&root, row["setup"].as_array().expect("setup"));
        let files = KeyFiles::new(&root.join("keys"), &id).expect("an id");
        let (out, message) = run(&files, row["op"].as_array().expect("op"), &root);
        let after = snapshot(&root);
        unlock(&scratch);
        fs::remove_dir_all(&scratch).expect("cleanup");
        let got = json!({"out": out, "after": after});
        let want = json!({"out": row["out"], "after": row["after"]});
        if got != want {
            failures.push(format!("{name}:\n  expected {want}\n  got      {got}"));
        }
        if let Some(message) = message {
            let w = want_messages.next().expect("a darwin row");
            assert_eq!(w["case"], name, "darwin rows in order");
            let same = w["message"] == json!(message);
            if ledger.differs_fatally(name, same) {
                failures.push(format!("{name}: expected {} got {message}", w["message"]));
            }
        }
        checked += 1;
    }
    let _ = fs::remove_dir_all(&base);
    assert!(want_messages.next().is_none(), "every darwin row met");
    assert!(
        failures.is_empty(),
        "{} failures:\n{}",
        failures.len(),
        failures.join("\n")
    );
    assert_count("log_keyfiles_posix.jsonl", checked, rows.len());
    ledger.finish(darwin.len() - 1);
}
