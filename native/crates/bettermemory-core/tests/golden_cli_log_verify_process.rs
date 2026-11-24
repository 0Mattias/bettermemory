//! `bettermemory log verify` as a process against
//! `cli_log_verify_process_posix.jsonl`: for every case the same store (a
//! case of `store_files.jsonl` rebuilt under a fresh root), the same edits
//! and files, the same arguments and environment, and then the executable's
//! exit status, its stdout and its stderr byte for byte (the root spelled
//! `<root>`), and the names it left under the root.
//!
//! POSIX only, as the fixture is: the store is selected by the POSIX
//! configuration rules and the cases set file modes.

#![cfg(unix)]

mod common;

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;

use common::parity::Ledger;
use common::stores::{self, scratch};
use serde_json::Value;

const FIXTURE: &str = "cli_log_verify_process_posix.jsonl";
const ROOT: &str = "<root>";

fn placed(text: &str, root: &Path) -> String {
    text.replace(ROOT, root.to_str().expect("an ASCII scratch root"))
}

fn key_file(store: &Path, suffix: &Value) -> PathBuf {
    store.join("keys").join(format!(
        "{}{}",
        stores::store_id(),
        suffix.as_str().expect("suffix")
    ))
}

fn set_mode(path: &Path, mode: &Value) {
    let mode = u32::try_from(mode.as_u64().expect("mode")).expect("mode bits");
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).expect("chmod");
}

/// The fixture's edits after the store is rebuilt (`_apply`).
fn apply(ops: &[Value], root: &Path) {
    let mut store = root.join("store");
    for op in ops {
        if let Some(suffix) = op.get("chmod") {
            set_mode(&key_file(&store, suffix), &op["mode"]);
        } else if let Some(mode) = op.get("chmod_store") {
            set_mode(&store.join("memory.sqlite"), mode);
        } else if let Some(name) = op.get("chmod_path") {
            set_mode(&root.join(name.as_str().expect("name")), &op["mode"]);
        } else if op.get("hold_restore").is_some() {
            // Taken by `run`, around the executable.
        } else if let Some(suffix) = op.get("key_text") {
            let text = placed(op["text"].as_str().expect("text"), root);
            fs::write(key_file(&store, suffix), text).expect("key text");
        } else if let Some(name) = op.get("copy_store") {
            let target = root.join(name.as_str().expect("name"));
            fs::create_dir_all(&target).expect("copy dir");
            fs::copy(store.join("memory.sqlite"), target.join("memory.sqlite")).expect("copy");
        } else if let Some(name) = op.get("move_store") {
            let target = root.join(name.as_str().expect("name"));
            fs::create_dir_all(target.parent().expect("parent")).expect("parent dir");
            fs::rename(&store, &target).expect("move");
            store = target;
        } else {
            stores::apply(std::slice::from_ref(op), &store);
        }
    }
}

/// The names under `root` that are not a directory holding something
/// (`_left`), after every file is made readable again.
fn left(root: &Path) -> Vec<String> {
    fn walk(dir: &Path, root: &Path, out: &mut Vec<String>) {
        for entry in fs::read_dir(dir).expect("listing") {
            let path = entry.expect("entry").path();
            let name = path
                .strip_prefix(root)
                .expect("under the root")
                .to_str()
                .expect("an ASCII name")
                .to_owned();
            let meta = fs::symlink_metadata(&path).expect("lstat");
            if meta.is_dir() {
                if fs::read_dir(&path).expect("listing").next().is_none() {
                    out.push(name);
                } else {
                    walk(&path, root, out);
                }
            } else {
                out.push(name);
            }
        }
    }
    let mut out = Vec::new();
    walk(root, root, &mut out);
    out.sort();
    out
}

fn readable_again(dir: &Path) {
    for entry in fs::read_dir(dir).expect("listing") {
        let path = entry.expect("entry").path();
        let meta = fs::symlink_metadata(&path).expect("lstat");
        if meta.is_dir() {
            readable_again(&path);
        } else if meta.is_file() && meta.permissions().mode() & 0o400 == 0 {
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).expect("chmod");
        }
    }
}

fn run(row: &Value, store_cases: &[Value]) {
    let name = row["case"].as_str().expect("case");
    let dir = scratch(name);
    let root = dir.0.clone();
    for part in ["me", "xdg", "state"] {
        fs::create_dir_all(root.join(part)).expect("root part");
    }
    if let Some(store) = row["store"].as_str() {
        let case = store_cases
            .iter()
            .find(|c| c["case"] == store)
            .unwrap_or_else(|| panic!("no store case {store}"));
        stores::materialize(case, &root.join("store"));
    }
    if let Some(files) = row.get("files").and_then(Value::as_object) {
        for (name, text) in files {
            let path = root.join(name);
            fs::create_dir_all(path.parent().expect("parent")).expect("file dir");
            fs::write(&path, placed(text.as_str().expect("text"), &root)).expect("file");
        }
    }
    if let Some(ops) = row.get("ops").and_then(Value::as_array) {
        apply(ops, &root);
    }
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_bettermemory"));
    cmd.current_dir(root.join("me"))
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("HOME", root.join("me"))
        .env("XDG_CONFIG_HOME", root.join("xdg"))
        .env("BETTERMEMORY_DIR", root.join("store"))
        .env("BETTERMEMORY_KEYS_DIR", root.join("store").join("keys"))
        .env("BETTERMEMORY_STATE_DIR", root.join("state"))
        .env("NO_COLOR", "1")
        .env("COLUMNS", "80");
    if let Some(env) = row.get("env").and_then(Value::as_object) {
        for (name, value) in env {
            match value.as_str() {
                Some(value) => cmd.env(name, placed(value, &root)),
                None => cmd.env_remove(name),
            };
        }
    }
    let args = row["argv"]
        .as_array()
        .expect("argv")
        .iter()
        .map(|arg| arg.as_str().expect("argument"));
    // `restore_lock`: a restore holds the store's directory locked
    // exclusively while it runs.
    let held = row
        .get("ops")
        .and_then(Value::as_array)
        .is_some_and(|ops| ops.iter().any(|op| op.get("hold_restore").is_some()))
        .then(|| {
            let dir = fs::File::open(root.join("store")).expect("the store directory");
            dir.lock().expect("the restore lock");
            dir
        });
    let output = cmd.args(args).output().expect("the executable runs");
    drop(held);
    readable_again(&root);
    let scrubbed = |raw: &[u8]| {
        String::from_utf8(raw.to_vec())
            .expect("UTF-8 output")
            .replace(root.to_str().expect("an ASCII scratch root"), ROOT)
    };
    assert_eq!(
        scrubbed(&output.stderr),
        row["err"].as_str().expect("err"),
        "{name}: stderr"
    );
    assert_eq!(
        scrubbed(&output.stdout),
        row["out"].as_str().expect("out"),
        "{name}: stdout"
    );
    assert_eq!(
        output.status.code().map(i64::from),
        row["code"].as_i64(),
        "{name}: status"
    );
    let expected: Vec<String> = row["left"]
        .as_array()
        .expect("left")
        .iter()
        .map(|n| n.as_str().expect("name").to_owned())
        .collect();
    assert_eq!(left(&root), expected, "{name}: names left under the root");
}

#[test]
fn cli_log_verify_process_every_case() {
    let rows = common::jsonl(FIXTURE);
    let (_, store_cases) = stores::cases();
    let mut ledger = Ledger::new("cli_log_verify_process_every_case", FIXTURE);
    ledger.case("header", || {
        assert_eq!(rows[0]["cases"].as_u64(), Some(rows.len() as u64 - 1));
        assert!(rows.len() > 100, "the fixture lost its cases");
    });
    for row in &rows[1..] {
        ledger.case(row["case"].as_str().expect("case"), || {
            run(row, &store_cases)
        });
    }
    ledger.finish(rows.len());
}

/// The fixture reaches every verdict and every way the command ends, so a
/// regenerated file that lost one fails here and not silently.
#[test]
fn the_fixture_covers_every_ending() {
    let rows = common::jsonl(FIXTURE);
    let has = |code: i64, lead: &str, stream: &str| {
        rows[1..].iter().any(|row| {
            row["code"].as_i64() == Some(code)
                && row[stream].as_str().is_some_and(|text| text.contains(lead))
        })
    };
    assert!(has(0, "bettermemory log verify: ok\n", "out"));
    assert!(has(1, "bettermemory log verify: tampered\n", "out"));
    assert!(has(1, "bettermemory log verify: unverifiable\n", "out"));
    assert!(has(0, "\"status\": \"ok\"", "out"));
    assert!(has(1, "\"status\": \"tampered\"", "out"));
    assert!(has(1, "anchor_mismatch", "out"));
    assert!(has(0, "other_store", "out"));
    assert!(has(1, "cannot read the store at", "err"));
    assert!(has(1, "the config does not load", "err"));
    assert!(has(2, "no bettermemory 9 store at", "err"));
    assert!(has(2, "holds no anchor", "err"));
    assert!(has(1, "opens without its key", "err"));
    assert!(has(0, "is open for reading only", "err"));
    assert!(has(2, "usage: bettermemory log [-h]", "out"));
    assert!(has(75, "a restore is running", "err"));
    assert!(has(2, "which is under a system directory", "err"));
    assert!(has(1, "payload_invalid", "out"));
    assert!(has(1, "Could not decode to UTF-8 column", "err"));
}

/// The ending the reference leaves to a traceback: one line that names
/// what stopped the command, nothing on stdout, status 1. The fixture
/// holds no such row (a traceback's text is the interpreter's), so the
/// status and the line's lead are held here: an anchor file nested past
/// what the JSON reader recurses.
#[test]
fn an_ending_the_reference_leaves_to_a_traceback_is_one_line_and_status_1() {
    let (_, store_cases) = stores::cases();
    let dir = scratch("stopped");
    let root = dir.0.clone();
    for part in ["me", "xdg", "state"] {
        fs::create_dir_all(root.join(part)).expect("root part");
    }
    let intact = store_cases
        .iter()
        .find(|c| c["case"] == "intact")
        .expect("the intact case");
    stores::materialize(intact, &root.join("store"));
    fs::write(root.join("me").join("deep.json"), "[".repeat(100_000)).expect("anchor");
    let output = Command::new(env!("CARGO_BIN_EXE_bettermemory"))
        .current_dir(root.join("me"))
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("HOME", root.join("me"))
        .env("XDG_CONFIG_HOME", root.join("xdg"))
        .env("BETTERMEMORY_DIR", root.join("store"))
        .env("BETTERMEMORY_KEYS_DIR", root.join("store").join("keys"))
        .env("BETTERMEMORY_STATE_DIR", root.join("state"))
        .args(["log", "verify", "--anchor", "deep.json"])
        .output()
        .expect("the executable runs");
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    let stderr = String::from_utf8(output.stderr).expect("UTF-8");
    assert!(
        stderr.starts_with("bettermemory: log verify stopped: RecursionError"),
        "{stderr}"
    );
    assert_eq!(stderr.lines().count(), 1, "{stderr}");
}

/// What the command logged reaches its stream before the report reaches
/// stdout, as the reference writes a warning while it opens the store and
/// the report when it is done: with both streams on one pipe the warning
/// of a store a later writer owns comes first.
#[test]
fn a_logged_warning_comes_before_the_report_on_a_shared_stream() {
    let (_, store_cases) = stores::cases();
    let dir = scratch("order");
    let root = dir.0.clone();
    for part in ["me", "xdg", "state"] {
        fs::create_dir_all(root.join(part)).expect("root part");
    }
    let case = store_cases
        .iter()
        .find(|c| c["case"] == "writer_above")
        .expect("the writer_above case");
    stores::materialize(case, &root.join("store"));
    let output = Command::new("/bin/sh")
        .arg("-c")
        .arg("exec \"$BIN\" log verify 2>&1")
        .current_dir(root.join("me"))
        .env_clear()
        .env("BIN", env!("CARGO_BIN_EXE_bettermemory"))
        .env("PATH", "/usr/bin:/bin")
        .env("HOME", root.join("me"))
        .env("XDG_CONFIG_HOME", root.join("xdg"))
        .env("BETTERMEMORY_DIR", root.join("store"))
        .env("BETTERMEMORY_KEYS_DIR", root.join("store").join("keys"))
        .env("BETTERMEMORY_STATE_DIR", root.join("state"))
        .output()
        .expect("the shell runs");
    assert_eq!(output.status.code(), Some(0));
    assert!(output.stderr.is_empty());
    let both = String::from_utf8(output.stdout).expect("UTF-8");
    let warned = both.find("is open for reading only").expect("the warning");
    let reported = both
        .find("bettermemory log verify: ok")
        .expect("the report");
    assert!(warned < reported, "{both}");
}
