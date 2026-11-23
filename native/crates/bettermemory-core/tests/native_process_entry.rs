//! Run the installed-product target across its actual process boundary.

mod common;

use std::ffi::OsString;
use std::fs;
use std::path::Path;
use std::process::Command;

use common::parity::Ledger;
use common::provenance::scratch;

fn process(dir: &Path) -> Command {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_bettermemory"));
    cmd.current_dir(dir)
        .env("PATH", dir)
        .env("HOME", dir)
        .env("USERPROFILE", dir)
        .env("XDG_CONFIG_HOME", dir)
        .env("APPDATA", dir)
        .env("LOCALAPPDATA", dir)
        .env("BETTERMEMORY_DIR", dir.join("store"))
        .env("BETTERMEMORY_STATE_DIR", dir.join("state"))
        .env("BETTERMEMORY_KEYS_DIR", dir.join("keys"))
        .env("NO_COLOR", "1")
        .env_remove("COLUMNS");
    cmd
}

fn stream_bytes(text: &str) -> Vec<u8> {
    if cfg!(windows) {
        text.replace('\n', "\r\n").into_bytes()
    } else {
        text.as_bytes().to_vec()
    }
}

#[test]
fn native_process_entry_every_case() {
    let name = "native_process_entry.jsonl";
    let rows = common::jsonl(name);
    let dir = scratch("native-process-entry");
    let mut ledger = Ledger::new("native_process_entry_every_case", name);
    ledger.case("header", || {
        assert_eq!(rows[0]["cases"].as_u64(), Some(rows.len() as u64 - 1));
        assert!(rows.len() > 1);
    });
    for row in &rows[1..] {
        ledger.case(format!("{} at {}", row["argv"], row["columns"]), || {
            let mut cmd = process(&dir.0);
            if let Some(width) = row["columns"].as_str() {
                cmd.env("COLUMNS", width);
            }
            let args = row["argv"]
                .as_array()
                .unwrap()
                .iter()
                .map(|arg| arg.as_str().unwrap());
            let output = cmd.args(args).output().unwrap();
            assert_eq!(output.status.code().map(i64::from), row["code"].as_i64());
            assert_eq!(output.stdout, stream_bytes(row["out"].as_str().unwrap()));
            assert_eq!(output.stderr, stream_bytes(row["err"].as_str().unwrap()));
        });
    }
    assert_eq!(fs::read_dir(&dir.0).unwrap().count(), 0);
    ledger.finish(rows.len());
}

#[cfg(any(unix, windows))]
#[test]
fn raw_argument_and_first_action_precedence() {
    #[cfg(unix)]
    let raw = {
        use std::os::unix::ffi::OsStringExt;
        OsString::from_vec(vec![0xff])
    };
    #[cfg(windows)]
    let raw = {
        use std::os::windows::ffi::OsStringExt;
        OsString::from_wide(&[0xdcff])
    };
    let dir = scratch("native-process-raw");
    let expected = process(&dir.0).arg("--version").output().unwrap();
    let after = process(&dir.0).arg("--version").arg(&raw).output().unwrap();
    assert!(after.status.success());
    assert_eq!(after.stdout, expected.stdout);
    assert!(after.stderr.is_empty());
    let before = process(&dir.0).arg(&raw).arg("--version").output().unwrap();
    assert_eq!(before.status.code(), Some(2));
    assert!(before.stdout.is_empty());
    assert!(String::from_utf8(before.stderr)
        .unwrap()
        .contains("\\udcff"));
}

#[test]
fn reports_unavailable_process_routes() {
    let dir = scratch("native-process-routes");
    for (argv, message) in [
        (vec!["hook", "--version"], "hook client"),
        (vec![], "stdio shim"),
        (vec!["--help", "--version"], "help text"),
    ] {
        let out = process(&dir.0).args(argv).output().unwrap();
        assert_eq!(out.status.code(), Some(2));
        assert!(out.stdout.is_empty());
        assert!(String::from_utf8(out.stderr).unwrap().contains(message));
    }
    assert_eq!(fs::read_dir(&dir.0).unwrap().count(), 0);
}
