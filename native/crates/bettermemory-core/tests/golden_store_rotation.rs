//! Every case of `store_rotation.jsonl` and `store_rotation_python.jsonl`
//! (both POSIX only) against
//! `store::rotation`: `finish_rotation`, `_finish_for_signing`,
//! `write_key_once` and `pending_key_path` over a real keys directory in a
//! scratch folder (links, hard links, mode-000 files, read-only folders),
//! the answer, the log lines and every entry under the folder afterwards;
//! `_signs_a_segment` over segments; `KeysDirUnreadableError`'s reason and
//! message.

#![cfg(unix)]

mod common;

use std::fs;
use std::os::unix::fs::{symlink, PermissionsExt};
use std::path::Path;

use bettermemory_core::float_repr;
use bettermemory_core::log::ring::KeyFiles;
use bettermemory_core::log::LogRow;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::rotation::{
    finish_for_signing, finish_rotation, keys_dir_unreadable, pending_key_path, signs_a_segment,
    write_key_once, Declined, Finished, Level, SegmentStart,
};
use common::parity::Ledger;
use common::{hex, jsonl, unhex};
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
    let plain: Vec<&Value> = setup
        .iter()
        .filter(|s| s[0] != "hard" && s[0] != "chmod")
        .collect();
    for step in &plain {
        let path = root.join(step[1].as_str().expect("name"));
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
    for step in plain.iter().rev() {
        if step[0] != "link" {
            let mode = step.as_array().unwrap().last().unwrap().as_u64().unwrap();
            chmod(&root.join(step[1].as_str().unwrap()), mode as u32);
        }
    }
    for step in setup {
        match step[0].as_str() {
            Some("hard") => fs::hard_link(
                root.join(step[2].as_str().unwrap()),
                root.join(step[1].as_str().unwrap()),
            )
            .expect("hard link"),
            Some("chmod") => chmod(
                &root.join(step[1].as_str().unwrap()),
                step[2].as_u64().unwrap() as u32,
            ),
            _ => {}
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
                    json!({"hex": hex(&data)})
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

/// The message with the scratch folder and each shown errno's `strerror`
/// spelled as the generator spells them.
fn shown(text: &str, root: &Path, names: &[&str]) -> String {
    let mut text = sub(text, root);
    for code in 1..200 {
        if let Some(name) = bettermemory_sys::errno_name(code) {
            if names.contains(&name) {
                let words = bettermemory_sys::strerror(code).expect("strerror");
                text = text.replace(&words, &format!("<{name}>"));
            }
        }
    }
    text
}

fn class(name: Option<&str>) -> &'static str {
    match name {
        Some("EACCES" | "EPERM") => "PermissionError",
        Some("ENOTDIR") => "NotADirectoryError",
        Some("ENOENT") => "FileNotFoundError",
        _ => "OSError",
    }
}

fn finished(result: Result<Finished, Declined>, root: &Path, names: &[&str]) -> Value {
    match result {
        Ok(f) => {
            let log: Vec<Value> = f
                .log
                .iter()
                .map(|l| {
                    let level = match l.level {
                        Level::Info => "INFO",
                        Level::Warning => "WARNING",
                    };
                    json!([level, shown(&l.text, root, names)])
                })
                .collect();
            json!({"key": f.key.map(|k| hex(k.bytes())), "log": log})
        }
        Err(Declined::Py(e)) => json!({"raised": [e.name(), null], "log": []}),
        Err(Declined::NotPorted) => panic!("declined on POSIX"),
    }
}

fn run(files: &KeyFiles, op: &[Value], root: &Path, names: &[&str]) -> Value {
    let cwd = root.to_str().unwrap().as_bytes();
    match op[0].as_str().expect("op") {
        "pending" => json!({"ok": sub(pending_key_path(files).to_str().unwrap(), root)}),
        "once" => match write_key_once(
            &root.join(op[1].as_str().unwrap()),
            &unhex(op[2].as_str().unwrap()),
        ) {
            Ok(b) => json!({"ok": b}),
            Err(e) => {
                let name = e.raw_os_error().and_then(bettermemory_sys::errno_name);
                json!({"raised": [class(name), name]})
            }
        },
        "finish" => {
            let store = unsub(op[1].as_str().unwrap(), root);
            let got = finish_rotation(
                files,
                Path::new(&store),
                op[2].as_str().unwrap(),
                cwd,
                false,
                &mut |_| panic!("no pause off Windows"),
            );
            finished(got, root, names)
        }
        "signing" => {
            let store = unsub(op[1].as_str().unwrap(), root);
            let mut sleeps = Vec::new();
            let got = finish_for_signing(
                files,
                Path::new(&store),
                op[2].as_str().unwrap(),
                cwd,
                false,
                op[3].as_u64().unwrap() as u32,
                &mut |s| sleeps.push(float_repr::repr(s)),
            );
            let mut out = finished(got, root, names);
            out["sleeps"] = json!(sleeps);
            out
        }
        other => panic!("unknown op {other}"),
    }
}

fn row_of(v: &Value) -> LogRow {
    LogRow {
        seq: v["seq"].as_i64().unwrap(),
        ts: v["ts"].as_str().unwrap().to_owned(),
        session: v["session"].as_str().map(str::to_owned),
        kind: v["kind"].as_str().unwrap().to_owned(),
        payload: v["payload"].as_str().unwrap().to_owned(),
        prev_mac: v["prev_mac"].as_str().unwrap().to_owned(),
        mac: v["mac"].as_str().unwrap().to_owned(),
    }
}

fn signs(row: &Value) -> Value {
    let parts: Vec<(String, Option<LogRow>)> = row["segments"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| {
            let first = (!s[1].is_null()).then(|| row_of(&s[1]));
            (s[0].as_str().unwrap().to_owned(), first)
        })
        .collect();
    let segments: Vec<SegmentStart<'_>> = parts
        .iter()
        .map(|(fp, first)| SegmentStart {
            key_fingerprint: fp,
            first_row: first.as_ref(),
        })
        .collect();
    json!(signs_a_segment(
        &segments,
        &unhex(row["key"].as_str().unwrap())
    ))
}

fn keysdir(row: &Value) -> Value {
    let (reason, message) = keys_dir_unreadable(
        Path::new(row["keys_dir"].as_str().unwrap()),
        Path::new(row["link"].as_str().unwrap()),
        row["reason"].as_str().unwrap(),
    );
    json!({"reason": reason, "message": message, "str": message})
}

fn every_case(file: &str) {
    let lines = jsonl(file);
    let rows = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64(), Some(rows.len() as u64));
    let id = PyValue::str(lines[0]["store_id"].as_str().expect("id"));
    let names: Vec<&str> = lines[0]["shown"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    let mut ledger = Ledger::new("golden_store_rotation", file);
    let base = fs::canonicalize(std::env::temp_dir())
        .expect("temp")
        .join(format!(
            "bm-rotation-{}-{}",
            std::process::id(),
            lines[0]["fixture"].as_str().expect("fixture")
        ));
    for (i, row) in rows.iter().enumerate() {
        match row["part"].as_str() {
            Some("signs") => {
                ledger.case(format!("signs {i}"), || {
                    assert_eq!(signs(row), row["out"], "row {i}")
                });
                continue;
            }
            Some("keysdir") => {
                ledger.case(format!("keysdir {i}"), || {
                    assert_eq!(keysdir(row), row["out"], "row {i}")
                });
                continue;
            }
            _ => {}
        }
        let name = row["case"].as_str().expect("case");
        let scratch = base.join(i.to_string());
        let root = scratch.join("r");
        fs::create_dir_all(&root).expect("root");
        build(&root, row["setup"].as_array().expect("setup"));
        let files = KeyFiles::new(&root.join("keys"), &id).expect("an id");
        let out = run(&files, row["op"].as_array().expect("op"), &root, &names);
        let after = snapshot(&root);
        unlock(&scratch);
        fs::remove_dir_all(&scratch).expect("cleanup");
        let got = json!({"out": out, "after": after});
        let want = json!({"out": row["out"], "after": row["after"]});
        ledger.case(name, || assert_eq!(got, want, "{name}"));
    }
    let _ = fs::remove_dir_all(&base);
    ledger.finish(rows.len());
}

#[test]
fn store_rotation_every_case() {
    every_case("store_rotation.jsonl");
}

/// The Python-gated sibling: a binding nested past where `json.loads`
/// stops recursing.
#[test]
fn store_rotation_python_every_case() {
    every_case("store_rotation_python.jsonl");
}
