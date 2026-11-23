//! The spelling kept by the Store and the point at which it needs OS bytes.
#![cfg(unix)]
mod common;
#[path = "common/read_only_path.rs"]
mod host;
use bettermemory_core::store::key_directory::{default_keys_dir, os_path, resolve_keys_dir};
use bettermemory_core::unicode::cps;
use std::env;
use std::os::unix::ffi::OsStrExt;

#[test]
fn explicit_keys_keep_distinct_spelling_even_for_identical_file_bytes() {
    host::isolated(
        "explicit_keys_keep_distinct_spelling_even_for_identical_file_bytes",
        |_| {
            env::set_var(
                "BETTERMEMORY_KEYS_DIR",
                "~bettermemory_invalid_reference_user_x/x",
            );
            let scalar = resolve_keys_dir(Some(&cps("./é//."))).unwrap();
            let escaped = resolve_keys_dir(Some(&[0xdcc3, 0xdca9])).unwrap();
            assert_ne!(scalar, escaped);
            assert_eq!(os_path(&escaped).unwrap(), os_path(&scalar).unwrap());
            assert_eq!(
                os_path(&scalar).unwrap().as_os_str().as_bytes(),
                "é".as_bytes()
            );
            for (raw, expected) in [
                ("", "."),
                ("~missing/key", "~missing/key"),
                ("//tier/.././keys/", "//tier/../keys"),
                ("///tier", "/tier"),
            ] {
                assert_eq!(resolve_keys_dir(Some(&cps(raw))).unwrap(), cps(expected));
            }
            assert_eq!(resolve_keys_dir(None).unwrap_err().class, "RuntimeError");
        },
    );
}

#[test]
fn nul_is_encodable_and_surrogate_bounds_follow_the_utf8_error_handler() {
    let nul = resolve_keys_dir(Some(&[0x61, 0, 0x62])).unwrap();
    assert_eq!(os_path(&nul).unwrap().as_os_str().as_bytes(), b"a\0b");
    let pts = [0x61, 0xdcfe, 0xd800, 0xdc80, 0x65, 0xda00];
    let selected = resolve_keys_dir(Some(&pts)).unwrap();
    let err = os_path(&selected).unwrap_err();
    assert!(std::ptr::eq(err.object, selected.as_slice()));
    assert_eq!((err.start, err.end), (2, 4));
    assert_eq!(
        err.to_string(),
        "'utf-8' codec can't encode characters in position 2-3: surrogates not allowed"
    );
}

#[test]
fn relative_home_expansion_and_the_explicit_default_have_separate_inputs() {
    host::isolated(
        "relative_home_expansion_and_the_explicit_default_have_separate_inputs",
        |_| {
            env::set_var("HOME", ".");
            env::set_var("XDG_CONFIG_HOME", " \u{2003}//xdg//./selected\u{2002}");
            env::set_var("BETTERMEMORY_KEYS_DIR", "~/./x");
            assert_eq!(resolve_keys_dir(None).unwrap(), cps("x"));
            assert_eq!(
                default_keys_dir().unwrap(),
                cps("//xdg/selected/bettermemory/keys")
            );
            env::set_var("BETTERMEMORY_KEYS_DIR", "");
            assert_eq!(resolve_keys_dir(None).unwrap(), default_keys_dir().unwrap());
        },
    );
}
