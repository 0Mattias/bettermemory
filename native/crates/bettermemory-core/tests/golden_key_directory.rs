//! Exact path spellings and the deferred OS projection from the reference Store.
mod common;
#[cfg(unix)]
#[path = "common/read_only_path.rs"]
mod host;

#[cfg(unix)]
#[test]
fn selected_key_directories() {
    use bettermemory_core::gitfile_rule::follow::fsencode;
    use bettermemory_core::store::key_directory::{default_keys_dir, os_path, resolve_keys_dir};
    use serde_json::{json, Value};
    use std::{
        env,
        ffi::OsString,
        os::unix::ffi::{OsStrExt, OsStringExt},
    };

    fn env_value(name: &str, value: &Value) {
        if value.is_null() {
            env::remove_var(name);
        } else {
            let points: Vec<u32> = serde_json::from_value(value.clone()).unwrap();
            env::set_var(name, OsString::from_vec(fsencode(&points).unwrap()));
        }
    }
    host::isolated("selected_key_directories", |root| {
        let rows = common::jsonl("key_directory_posix.jsonl");
        assert_eq!(rows.len(), 57);
        let before = host::snapshot(root);
        for row in rows {
            let value = if let Some(explicit) = row.get("explicit") {
                env::set_var("BETTERMEMORY_KEYS_DIR", "~unavailable-profile/key");
                let path: Vec<u32> = serde_json::from_value(explicit.clone()).unwrap();
                resolve_keys_dir(Some(&path))
            } else if let Some(xdg) = row.get("default_xdg") {
                env_value("XDG_CONFIG_HOME", xdg);
                env::set_var("BETTERMEMORY_KEYS_DIR", "~unavailable-profile/key");
                default_keys_dir()
            } else {
                env_value("HOME", &row["home"]);
                env_value("XDG_CONFIG_HOME", &row["xdg"]);
                env_value("BETTERMEMORY_KEYS_DIR", &row["override"]);
                resolve_keys_dir(None)
            };
            let result = match value {
                Err(error) => json!({"raised":error.class,"message":error.message}),
                Ok(text) => match os_path(&text) {
                    Ok(path) => json!({"path":text,"bytes":path.as_os_str().as_bytes()}),
                    Err(error) => {
                        assert_eq!(error.object, text);
                        json!({"path":text,"encoding_error":[error.encoding,error.start,error.end,error.reason,error.to_string()]})
                    }
                },
            };
            assert_eq!(result, row["out"], "{row}");
        }
        assert_eq!(host::snapshot(root), before);
    });
}

#[cfg(not(unix))]
#[test]
fn posix_directory_selection() {
    common::parity::record(
        "key_directory_posix",
        "key_directory_posix.jsonl",
        "os",
        0,
        &["POSIX key directory selection".into()],
        &["host configuration and account lookup are POSIX".into()],
    );
}
