//! Class i: compare Store.open and the native signing-key directory selector in
//! real Windows child processes, including the Windows known-folder lookup.

#![cfg(windows)]
mod common;

use bettermemory_core::store::key_directory::{os_path, resolve_keys_dir};
use serde_json::{json, Value};
use std::{
    env, fs,
    os::windows::ffi::OsStrExt,
    path::{Path, PathBuf},
    process::Command,
};

const NAME: &str = "selected_signing_key_paths_match_native_windows_python";
const SCRIPT: &str = include_str!("common/key_directory_windows.py");

#[test]
#[ignore = "run in an isolated child of the Windows reference comparison"]
fn select_in_child_environment() {
    let Some(input) = env::var_os("BM_KEYDIR_PROBE_INPUT") else {
        return;
    };
    let output = env::var_os("BM_KEYDIR_PROBE_OUTPUT").expect("child output path");
    let requests: Vec<Option<Vec<u32>>> =
        serde_json::from_slice(&fs::read(input).unwrap()).unwrap();
    let actual: Vec<Value> = requests
        .iter()
        .map(|explicit| match resolve_keys_dir(explicit.as_deref()) {
            Ok(text) => {
                let path = os_path(&text).expect("a Windows UTF-16 path");
                let units: Vec<u16> = path.as_os_str().encode_wide().collect();
                json!([text, units])
            }
            Err(reason) => json!([reason.class, reason.message]),
        })
        .collect();
    fs::write(output, serde_json::to_vec(&actual).unwrap()).unwrap();
}

fn configure(
    cmd: &mut Command,
    root: &Path,
    platformdirs: Option<&Path>,
    key_override: Option<&str>,
) {
    cmd.env_clear();
    for key in ["PATH", "SYSTEMROOT", "COMSPEC"] {
        if let Some(value) = env::var_os(key) {
            cmd.env(key, value);
        }
    }
    cmd.current_dir(root)
        .env("TEMP", root)
        .env("TMP", root)
        .env("USERPROFILE", root.join("profile").join("account"))
        .env("USERNAME", "account")
        .env("HOME", root.join("ignored"))
        .env("APPDATA", root.join("ignored-roaming"))
        .env("LOCALAPPDATA", root.join("ignored-local"))
        .env("BETTERMEMORY_DIR", root.join("unused-store"))
        .env("BETTERMEMORY_STATE_DIR", root.join("state"))
        .env("BETTERMEMORY_KEYS_DIR", key_override.unwrap_or_default());
    if let Some(value) = platformdirs {
        cmd.env("WIN_PD_OVERRIDE_LOCAL_APPDATA", value);
    }
}

fn reference_python(name: &str, component: &str) -> Option<PathBuf> {
    match common::created::reference_python() {
        Ok(python) => Some(python),
        Err(reason) => {
            common::parity::record(
                name,
                "reference Python",
                "i",
                0,
                &[component.into()],
                &[reason],
            );
            None
        }
    }
}

fn projection(text: &str) -> Value {
    json!([
        text.chars().map(u32::from).collect::<Vec<_>>(),
        text.encode_utf16().collect::<Vec<_>>()
    ])
}

/// Earlier Windows pathlib releases parsed these paths in Python before
/// constructing their display spelling. Keep each side's exact result.
fn legacy_spellings(version: (u8, u8), input: Option<&[u32]>) -> Option<(Value, Value)> {
    match (version, input?) {
        ((3, 11 | 12), pair @ [0xd83d, 0xde42]) => Some((json!([pair, pair]), projection("🙂"))),
        ((3, 12), path) if path.iter().copied().eq("./🙂:part".chars().map(u32::from)) => {
            Some((projection(r".\🙂:part"), projection("🙂:part")))
        }
        _ => None,
    }
}

#[test]
fn selected_signing_key_paths_match_native_windows_python() {
    let Some(python) = reference_python(NAME, "Windows key-directory resolver") else {
        return;
    };
    let scratch = common::stores::scratch("key-directory-win");
    let root = &scratch.0;
    let input = root.join("input.json");
    let output = root.join("output.json");
    let nonexistent = root.join("never-made");
    let points = |s: &str| Some(s.chars().map(u32::from).collect::<Vec<_>>());
    let requests = [
        None,
        points(""),
        points("~/already/explicit"),
        points(r"C:relative/./tail"),
        points(r"//server/share/child/../leaf"),
        points(r"//?/UNC/server/share"),
        points("./🙂:part"),
        points("a\0b"),
        points("🙂"),
        Some(vec![0xd83d, 0xde42]),
        Some(vec![0xdcff]),
        Some(vec![0xf0000, 0x100000, 0xd800]),
    ];
    fs::write(&input, serde_json::to_vec(&requests).unwrap()).unwrap();
    let modes = [
        (None, None),
        (Some(nonexistent.as_path()), None),
        (Some(Path::new(r"C:relative")), None),
        // The display spelling keeps its dot prefix. Path.expanduser sees the logical "~:" component.
        (Some(nonexistent.as_path()), Some("./~:/sub")),
        (None, Some("relative/../ring")),
    ];
    let mut compared = 0;
    let mut legacy_cases = Vec::new();
    for (platformdirs, key_override) in modes {
        let mut reference = Command::new(&python);
        configure(&mut reference, root, platformdirs, key_override);
        let result = reference
            .args(["-I", "-c", SCRIPT])
            .arg(&input)
            .arg(root)
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        let (version, expected): ((u8, u8), Vec<Value>) =
            serde_json::from_slice(&result.stdout).unwrap();
        assert_eq!(expected.len(), requests.len());
        assert!(
            expected[0][0].is_array(),
            "default directory lookup failed: {:?}",
            expected[0]
        );
        // The two explicit inputs have distinct Python codepoints. The real Windows
        // pathlib implementation determines the spelling returned by Store.open.
        assert_ne!(requests[8], requests[9]);
        assert_eq!(expected[8][1], expected[9][1], "same UTF-16 projection");

        let mut child = Command::new(env::current_exe().unwrap());
        configure(&mut child, root, platformdirs, key_override);
        let result = child
            .args(["--ignored", "--exact", "select_in_child_environment"])
            .env("BM_KEYDIR_PROBE_INPUT", &input)
            .env("BM_KEYDIR_PROBE_OUTPUT", &output)
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        let actual: Vec<Value> = serde_json::from_slice(&fs::read(&output).unwrap()).unwrap();
        assert_eq!(actual.len(), requests.len());
        for ((explicit, got), wanted) in requests.iter().zip(&actual).zip(&expected) {
            if let Some((older, pinned)) = legacy_spellings(version, explicit.as_deref()) {
                assert_eq!(wanted, &older, "older Windows pathlib spelling");
                assert_eq!(got, &pinned, "native spelling for the pinned Python");
                legacy_cases.push(format!(
                    "Python {}.{}: {explicit:?}, known-folder override {platformdirs:?}, key override {key_override:?}",
                    version.0, version.1
                ));
            } else {
                assert_eq!(got, wanted, "explicit {explicit:?}, known-folder override {platformdirs:?}, key override {key_override:?}");
                compared += 1;
            }
        }
        fs::remove_file(&output).unwrap();
        assert!(!nonexistent.exists());
    }
    assert_eq!(compared + legacy_cases.len(), 60);
    common::parity::record(NAME, "reference Python", "i", compared, &[], &[]);
    if !legacy_cases.is_empty() {
        common::parity::record(
            NAME,
            "older Windows pathlib",
            "ii",
            0,
            &legacy_cases,
            &["Windows pathlib drive parsing and UTF-16 conversion before CPython 3.13".into()],
        );
    }
}

/// ctypes checks SHGetKnownFolderPath's HRESULT before reading the output pointer.
#[test]
fn known_folder_hresult_matches_ctypes_on_windows() {
    const CHECK: &str = "known_folder_hresult_matches_ctypes_on_windows";
    let Some(python) = reference_python(CHECK, "ctypes HRESULT") else {
        return;
    };
    let scratch = common::stores::scratch("known-folder-hresult");
    // S_OK, S_FALSE, wrapped Win32/WinSock errors, E_FAIL and an unmapped facility.
    let statuses = [
        0u32,
        1,
        0x8007_0002,
        0x8007_0003,
        0x8007_0005,
        0x8007_0050,
        0x8007_0102,
        0x8007_010b,
        0x8007_2714,
        0x8007_274d,
        0x8000_4005,
        0x8123_4567,
    ]
    .map(|status| status as i32);
    let mut reference = Command::new(python);
    configure(&mut reference, &scratch.0, None, None);
    let result = reference
        .args(["-I", "-c", SCRIPT, "hresults"])
        .arg(serde_json::to_string(&statuses).unwrap())
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let expected: Vec<Value> = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(expected.len(), statuses.len());
    for (status, wanted) in statuses.into_iter().zip(expected) {
        let got = bettermemory_sys::WindowsFolderError::from_hresult(status)
            .map(|e| json!([e.class, e.hresult, e.errno, e.message]));
        assert_eq!(json!(got), wanted, "HRESULT {status:#010x}");
    }
    common::parity::record(CHECK, "reference Python", "i", statuses.len(), &[], &[]);
}
