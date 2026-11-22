//! One case of `store_refusal.jsonl` or `store_refusal_posix.jsonl`: the
//! case directory built by its steps, then `bundle_refusal`,
//! `restore_state`, `restore_refusal` and `store_refusal` of its store
//! path compared with the row (see `gen_store_refusal`).

#![allow(dead_code)]

use std::fs::File;
use std::path::{Path, PathBuf};

use bettermemory_core::pyvalue::{PyError, PyStr, PyValue};
use bettermemory_core::store_refusal::{
    bundle_refusal, restore_refusal, restore_state, restore_state_record_branch, store_refusal,
    Failure, Marker, State,
};
use serde_json::Value;

use crate::common;

/// A fresh scratch directory for one test binary.
pub fn base(tag: &str) -> PathBuf {
    let base = std::env::temp_dir().join(format!("bm-srefusal-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&base);
    std::fs::create_dir_all(&base).expect("scratch");
    // Resolved, so no link above the case (macOS's /var) is met again
    // when a case's own link resolves: `link_met_twice` needs that.
    std::fs::canonicalize(&base).expect("scratch resolves")
}

pub fn pieces(value: &Value) -> Vec<u8> {
    let mut out = Vec::new();
    for piece in value.as_array().expect("pieces") {
        let bytes = common::unhex(piece[0].as_str().expect("hex"));
        for _ in 0..piece[1].as_u64().expect("count") {
            out.extend_from_slice(&bytes);
        }
    }
    out
}

fn cps(text: &str) -> PyStr {
    text.chars().map(u32::from).collect()
}

/// The generator's `_shown`: the case directory as `@` and, within each
/// path that starts at `@` (up to a space, `)`, `;` or `,`), the host's
/// separator as `/`; `printable`'s backslash escapes are left alone.
fn shown(root: &Path, text: &[u32]) -> PyStr {
    let mut out: PyStr = text.to_vec();
    let resolved = std::fs::canonicalize(root).expect("root resolves");
    for base in [resolved, root.to_path_buf()] {
        let needle = cps(&base.to_string_lossy());
        let mut next = Vec::new();
        let mut i = 0;
        while i < out.len() {
            if out[i..].starts_with(&needle) {
                next.push(u32::from('@'));
                i += needle.len();
            } else {
                next.push(out[i]);
                i += 1;
            }
        }
        out = next;
    }
    let sep = u32::from(std::path::MAIN_SEPARATOR);
    let mut in_path = false;
    out.iter()
        .map(|&c| {
            if c == u32::from('@') {
                in_path = true;
            } else if " );,".chars().any(|e| u32::from(e) == c) {
                in_path = false;
            }
            if in_path && c == sep {
                u32::from('/')
            } else {
                c
            }
        })
        .collect()
}

fn class(e: &Failure) -> &'static str {
    match e {
        Failure::Raised(PyError::ValueError) => "ValueError",
        Failure::Raised(PyError::RecursionError) => "RecursionError",
        other => panic!("unexpected {other:?}"),
    }
}

/// Compared, or (Windows only) declined as not ported (ENG-124).
enum Seen {
    Same,
    Differs,
    Declined,
}

fn seen(same: bool) -> Seen {
    if same {
        Seen::Same
    } else {
        Seen::Differs
    }
}

fn same_text(row: &Value, key: &str, root: &Path, got: Result<Option<PyStr>, Failure>) -> Seen {
    match (row.get(key), got) {
        (_, Err(Failure::NotPorted)) if cfg!(not(unix)) => Seen::Declined,
        (Some(Value::Null), Ok(None)) => Seen::Same,
        (Some(Value::Object(o)), Err(e)) => seen(o["raise"] == class(&e)),
        (_, Ok(Some(text))) => seen(common::text_field(row, key) == shown(root, &text)),
        _ => Seen::Differs,
    }
}

pub fn keys(marker: &Option<Marker>) -> Value {
    match marker {
        None => Value::Null,
        Some(m) => {
            let mut k: Vec<String> = m
                .iter()
                .map(|(k, _)| match k {
                    PyValue::Str(s) => s.iter().filter_map(|&c| char::from_u32(c)).collect(),
                    other => panic!("key {other:?}"),
                })
                .collect();
            k.sort();
            Value::from(k)
        }
    }
}

fn same_state(row: &Value, got: Result<(State, Option<Marker>), Failure>) -> bool {
    match got {
        Ok((state, marker)) => row["st"] == serde_json::json!([state.name(), keys(&marker)]),
        Err(e) => row["st"]["raise"] == class(&e),
    }
}

/// Builds the case under `base` and checks it; the failures it found and
/// the answers it declined (Windows: `Path.resolve`, ENG-124).
pub fn check(base: &Path, row: &Value) -> (Vec<String>, Vec<String>) {
    let name = row["n"].as_str().expect("a name");
    let root = base.join(name);
    std::fs::create_dir(&root).expect("case directory");
    let mut held: Vec<File> = Vec::new();
    let mut rec = false;
    for step in row["steps"].as_array().expect("steps") {
        let op = step[0].as_str().expect("op");
        if op == "rec" {
            rec = true;
            continue;
        }
        let at = root.join(step[1].as_str().expect("rel"));
        match op {
            "w" => std::fs::write(&at, pieces(&step[2])).expect("write"),
            "d" => std::fs::create_dir_all(&at).expect("mkdir"),
            "sparse" => File::create(&at)
                .and_then(|f| f.set_len(step[2].as_u64().expect("size")))
                .expect("sparse"),
            "ln" => {
                std::fs::hard_link(root.join(step[2].as_str().expect("to")), &at).expect("link")
            }
            #[cfg(unix)]
            "sym" => std::os::unix::fs::symlink(root.join(step[2].as_str().expect("to")), &at)
                .expect("symlink"),
            #[cfg(unix)]
            "fifo" => {
                let ok = std::process::Command::new("mkfifo")
                    .arg(&at)
                    .status()
                    .expect("mkfifo runs");
                assert!(ok.success(), "mkfifo");
            }
            "hold" => {
                let file = File::open(&at).expect("open to hold");
                file.try_lock().expect("hold");
                held.push(file);
            }
            other => panic!("{name}: step {other}"),
        }
    }
    let store = root.join(row.get("p").and_then(Value::as_str).unwrap_or("s.db"));
    let mut failures = Vec::new();
    let mut declined = Vec::new();
    if rec {
        if !same_state(row, restore_state_record_branch(&store)) {
            failures.push(format!("{name}: st"));
        }
        return (failures, declined);
    }
    let mut text = |key: &str, got: Result<Option<PyStr>, Failure>| {
        let shown = format!(
            "{name}: {key} {:?}",
            got.clone()
                .map(|t| t.map(|t| String::from_iter(t.iter().filter_map(|&c| char::from_u32(c)))))
        );
        match same_text(row, key, &root, got) {
            Seen::Same => {}
            Seen::Differs => failures.push(shown),
            Seen::Declined => declined.push(format!("{name}: {key}")),
        }
    };
    text("b", bundle_refusal(&store));
    text("r", restore_refusal(&store));
    text("sr", store_refusal(&store));
    if !same_state(row, restore_state(&store)) {
        failures.push(format!(
            "{name}: st {:?}",
            restore_state(&store).map(|(s, m)| (s, keys(&m)))
        ));
    }
    drop(held);
    (failures, declined)
}

/// Every case of `file`, counted.
pub fn run(file: &str, tag: &str) {
    let lines = common::jsonl(file);
    let rows = &lines[1..];
    assert_eq!(lines[0]["count"].as_u64(), Some(rows.len() as u64));
    let base = base(tag);
    let mut failures = Vec::new();
    let mut declined = Vec::new();
    let mut checked = 0;
    for row in rows {
        let (f, d) = check(&base, row);
        failures.extend(f);
        declined.extend(d);
        checked += 1;
    }
    let _ = std::fs::remove_dir_all(&base);
    assert!(failures.is_empty(), "{file}: {failures:#?}");
    common::assert_count(file, checked, rows.len());
    if !declined.is_empty() {
        common::parity::record(
            &format!("golden_store_refusal ({tag})"),
            file,
            "os",
            checked,
            &declined,
            &[
                "Path.resolve (ntpath.realpath) is not ported for the Windows flavour (ENG-124): \
               bundle_refusal declines where the spelled path finds no manifest"
                    .to_owned(),
            ],
        );
    }
}
