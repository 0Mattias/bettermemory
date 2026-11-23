//! Ordinary home-directory lookups, environment changes and filesystem side effects.
#![cfg(unix)]
mod common;
#[path = "common/read_only_path.rs"]
mod host;
use bettermemory_core::store::key_directory::{default_keys_dir, os_path, resolve_keys_dir};
use bettermemory_core::unicode::cps;
use std::{
    env,
    ffi::OsString,
    fs,
    os::unix::ffi::{OsStrExt, OsStringExt},
    path::Path,
};

#[test]
fn actual_account_and_relative_xdg_fallback() {
    host::isolated("actual_account_and_relative_xdg_fallback", |root| {
        let original = host::snapshot(root);
        let home = bettermemory_sys::account_home(None).unwrap().unwrap();
        let suffix = if cfg!(target_os = "macos") {
            "Library/Application Support"
        } else {
            ".config"
        };
        for empty in [true, false] {
            if empty {
                env::set_var("HOME", "");
            } else {
                env::remove_var("HOME");
            }
            env::set_var("XDG_CONFIG_HOME", "relative-config");
            env::remove_var("BETTERMEMORY_KEYS_DIR");
            assert_eq!(
                os_path(&default_keys_dir().unwrap()).unwrap(),
                home.join(suffix).join("bettermemory/keys")
            );
            env::set_var("BETTERMEMORY_KEYS_DIR", "~/x");
            let expect = if empty {
                Path::new("/x").to_owned()
            } else {
                home.join("x")
            };
            assert_eq!(os_path(&resolve_keys_dir(None).unwrap()).unwrap(), expect);
        }
        assert_eq!(original, host::snapshot(root));
    });
}

#[test]
fn changing_raw_environment_selects_again_without_creating_or_resolving() {
    host::isolated(
        "changing_raw_environment_selects_again_without_creating_or_resolving",
        |root| {
            fs::create_dir(root.join("target")).unwrap();
            std::os::unix::fs::symlink("target", root.join("alias")).unwrap();
            let before = host::snapshot(root);
            for (raw, expected) in [
                (b"alias/../absent".as_slice(), b"alias/../absent".as_slice()),
                (b"relative-\xff//./keys", b"relative-\xff/keys"),
                (b" ", b" "),
            ] {
                env::set_var("BETTERMEMORY_KEYS_DIR", OsString::from_vec(raw.to_vec()));
                let text = resolve_keys_dir(None).unwrap();
                assert_eq!(os_path(&text).unwrap().as_os_str().as_bytes(), expected);
            }
            env::set_var(
                "XDG_CONFIG_HOME",
                OsString::from_vec(b"/unused-\xff".to_vec()),
            );
            env::remove_var("BETTERMEMORY_KEYS_DIR");
            let result = resolve_keys_dir(None).unwrap();
            assert!(result.contains(&0xdcff));
            assert_eq!(
                os_path(&result).unwrap().as_os_str().as_bytes(),
                b"/unused-\xff/bettermemory/keys"
            );
            let long = "a/./".repeat(5000) + "..";
            assert_eq!(
                resolve_keys_dir(Some(&cps(&long))).unwrap(),
                cps(&("a/".repeat(5000) + ".."))
            );
            assert_eq!(before, host::snapshot(root));
        },
    );
}
