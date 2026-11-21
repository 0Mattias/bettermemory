//! Cases a plausible wrong `log::ring` fails: the id check taken
//! loosely, the retired name overwritten, an unreadable anchor or binding
//! read as none, the first of two `path` keys taken, RecursionError
//! swallowed, the lock file removed, the message's tail dropped.

use std::path::Path;

use bettermemory_core::log::ring::{BadId, KeyFiles};
use bettermemory_core::pyvalue::PyValue;

const ID: &str = "0123456789abcdef0123456789abcdef";

#[test]
fn the_id_is_32_lowercase_hex_digits_and_the_message_shows_40() {
    for bad in [
        &ID.to_uppercase(),
        &format!("{ID}\n"),
        &ID[1..],
        &format!("{ID}0"),
    ] {
        assert!(
            KeyFiles::new(Path::new("k"), &PyValue::str(bad)).is_err(),
            "{bad:?}"
        );
    }
    let long = "z".repeat(100);
    let Err(BadId::ValueError(text)) = KeyFiles::new(Path::new("k"), &PyValue::str(&long)) else {
        panic!("a ValueError");
    };
    assert!(
        text.starts_with(&format!("not a store id: '{}';", "z".repeat(40))),
        "{text}"
    );
    // `str(5)`, not a TypeError.
    let Err(BadId::ValueError(text)) = KeyFiles::new(Path::new("k"), &PyValue::int(5)) else {
        panic!("a ValueError");
    };
    assert!(text.starts_with("not a store id: '5';"), "{text}");
    let files = KeyFiles::new(Path::new("k"), &PyValue::str(ID)).expect("an id");
    let fp = "f".repeat(64);
    assert_eq!(
        files.retired_path(&fp),
        Path::new("k").join(format!("{ID}.ffffffffffffffff.key"))
    );
}

#[cfg(unix)]
mod files {
    use super::*;
    use std::fs;
    use std::os::unix::fs::PermissionsExt;

    use bettermemory_core::log::ring::RingError;
    use bettermemory_core::log::{self, HistoryOrigin};
    use bettermemory_core::pyvalue::PyError;

    fn scratch(tag: &str) -> std::path::PathBuf {
        let root = fs::canonicalize(std::env::temp_dir())
            .unwrap()
            .join(format!("bm-kf-kills-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("keys")).unwrap();
        root
    }

    fn ring(root: &Path) -> KeyFiles {
        KeyFiles::new(&root.join("keys"), &PyValue::str(ID)).unwrap()
    }

    #[test]
    fn a_taken_retired_name_keeps_its_file() {
        let root = scratch("retire");
        let r = ring(&root);
        fs::write(r.current_path(), [7u8; 32]).unwrap();
        let fp = log::fingerprint(&[7u8; 32]);
        fs::write(r.retired_path(&fp), b"old").unwrap();
        assert_eq!(r.retire_current().unwrap(), Some(fp.clone()));
        assert!(!r.current_path().exists());
        assert_eq!(fs::read(r.retired_path(&fp)).unwrap(), b"old");
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn an_unreadable_anchor_is_not_written_over() {
        let root = scratch("origin");
        let r = ring(&root);
        fs::write(r.origin_path(), b"junk").unwrap();
        fs::set_permissions(r.origin_path(), fs::Permissions::from_mode(0o000)).unwrap();
        let readable_anyway = fs::read(r.origin_path()).is_ok();
        let out = r.write_origin(1, &"a".repeat(64), None);
        fs::set_permissions(r.origin_path(), fs::Permissions::from_mode(0o600)).unwrap();
        if !readable_anyway {
            assert!(matches!(out, Err(RingError::Os(_))), "{out:?}");
            assert_eq!(fs::read(r.origin_path()).unwrap(), b"junk");
        }
        // The lock file stays, 0600.
        let lock = root.join("keys").join(format!("{ID}.origin.lock"));
        assert_eq!(
            fs::metadata(lock).unwrap().permissions().mode() & 0o777,
            0o600
        );
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn a_named_anchor_is_left_and_a_written_one_is_0600() {
        let root = scratch("name");
        let r = ring(&root);
        assert!(r.write_origin(3, &"a".repeat(64), None).unwrap());
        assert_eq!(
            fs::metadata(r.origin_path()).unwrap().permissions().mode() & 0o777,
            0o600
        );
        let at = HistoryOrigin {
            seq: 3,
            mac: "a".repeat(64),
            key_fingerprint: None,
        };
        assert!(r.name_origin_key(&at, &"c".repeat(64)).unwrap());
        assert!(!r.name_origin_key(&at, &"d".repeat(64)).unwrap());
        assert_eq!(
            r.read_origin().unwrap().unwrap().key_fingerprint,
            Some("c".repeat(64))
        );
        assert!(!r.write_origin(5, &"b".repeat(64), None).unwrap());
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn the_last_path_key_wins_and_recursion_is_raised() {
        let root = scratch("binding");
        let r = ring(&root);
        fs::write(r.binding_path(), br#"{"path": "/a", "path": "/b"}"#).unwrap();
        assert_eq!(r.read_binding().unwrap(), Some("/b".into()));
        let deep = format!("{}{}", "[".repeat(20_000), "]".repeat(20_000));
        fs::write(r.binding_path(), deep).unwrap();
        assert!(matches!(
            r.read_binding(),
            Err(RingError::Py(PyError::RecursionError))
        ));
        assert!(matches!(
            r.rebind_store(&root.join("s.db"), b"/"),
            Err(RingError::Py(PyError::RecursionError))
        ));
        fs::write(r.binding_path(), br#"{"path": "/elsewhere"}"#).unwrap();
        let previous = r.rebind_store(&root.join("s.db"), b"/").unwrap();
        assert_eq!(previous, Some("/elsewhere".into()));
        let text = fs::read_to_string(r.binding_path()).unwrap();
        assert_eq!(
            text,
            format!("{{\"path\": \"{}\"}}", root.join("s.db").display())
        );
        fs::remove_dir_all(&root).unwrap();
    }

    /// `Path("//r/s.db") != Path("/r/s.db")` in pathlib (a leading `//`
    /// is kept), so a binding spelled with it is rewritten to the
    /// resolved spelling, and the old spelling is returned.
    #[test]
    fn a_binding_with_a_leading_double_slash_is_rewritten() {
        let root = scratch("dslash");
        let r = ring(&root);
        let store = root.join("s.db");
        let doubled = format!("/{}", store.display());
        fs::write(r.binding_path(), format!("{{\"path\": \"{doubled}\"}}")).unwrap();
        let previous = r.rebind_store(&store, b"/").unwrap();
        assert_eq!(previous.as_deref(), Some(Path::new(&doubled)));
        assert_eq!(
            previous.unwrap().to_str().unwrap(),
            doubled,
            "the spelling read is returned"
        );
        assert_eq!(
            fs::read_to_string(r.binding_path()).unwrap(),
            format!("{{\"path\": \"{}\"}}", store.display())
        );
        fs::remove_dir_all(&root).unwrap();
    }

    const UMASK_ROOT_ENV: &str = "BM_TEST_KEYFILES_UMASK_ROOT";

    /// The child: writes the anchor and the binding into `$ROOT` under the
    /// umask its parent's shell set.
    #[test]
    #[ignore = "run by the umask test as its child"]
    fn write_under_umask_child() {
        let Some(root) = std::env::var_os(UMASK_ROOT_ENV) else {
            return;
        };
        let root = std::path::PathBuf::from(root);
        let r = ring(&root);
        assert!(r.write_origin(1, &"a".repeat(64), None).unwrap());
        assert_eq!(r.rebind_store(&root.join("s.db"), b"/").unwrap(), None);
    }

    /// The anchor and the binding are 0600 whatever the umask, as the
    /// reference's `fchmod` makes them: a child under umask 377 (which
    /// would leave 0400) writes both.
    #[test]
    fn the_anchor_and_binding_are_0600_under_any_umask() {
        let root = scratch("umask");
        fs::set_permissions(root.join("keys"), fs::Permissions::from_mode(0o700)).unwrap();
        let status = std::process::Command::new("/bin/sh")
            .args([
                "-c",
                "umask 377 && exec \"$0\" --exact files::write_under_umask_child --ignored --test-threads=1",
            ])
            .arg(std::env::current_exe().unwrap())
            .env(UMASK_ROOT_ENV, &root)
            .stdout(std::process::Stdio::null())
            .status()
            .unwrap();
        assert!(status.success());
        let r = ring(&root);
        for path in [r.origin_path(), r.binding_path()] {
            assert_eq!(
                fs::metadata(&path).unwrap().permissions().mode() & 0o777,
                0o600,
                "{}",
                path.display()
            );
        }
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn a_dangling_keys_directory_is_unreadable_not_missing() {
        let root = scratch("dangling");
        fs::remove_dir(root.join("keys")).unwrap();
        std::os::unix::fs::symlink("gone", root.join("keys")).unwrap();
        let err = ring(&root).load_current().unwrap_err();
        assert_eq!(
            err.reason,
            "it is a symbolic link to gone, which is not there"
        );
        assert!(
            err.to_string()
                .ends_with("; the store is not rekeyed over it"),
            "{err}"
        );
        fs::remove_dir_all(&root).unwrap();
    }
}
