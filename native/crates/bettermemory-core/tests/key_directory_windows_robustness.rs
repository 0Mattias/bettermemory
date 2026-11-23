//! Host lookup failure and long lexical paths through the actual Windows selector.
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::store::key_directory::windows::{
    resolve_with, wide_units, DirectoryError, Folders,
};
use bettermemory_core::unicode::cps;

struct Host {
    raw: Option<PyStr>,
    home: Option<PyStr>,
}
impl Folders for Host {
    fn environment(&mut self, key: &str) -> Option<PyStr> {
        match key {
            "BETTERMEMORY_KEYS_DIR" => self.raw.clone(),
            "USERPROFILE" => self.home.clone(),
            "USERNAME" => Some(cps("current")),
            _ => None,
        }
    }
    fn local_app_data(&mut self) -> Result<Option<PyStr>, DirectoryError> {
        Err(DirectoryError {
            class: "PermissionError",
            message: vec![0xdcff],
            errno: Some(13),
            winerror: Some(-2147024891),
        })
    }
}
#[test]
fn shell_error_keeps_its_windows_fields_and_original_codepoints() {
    let error = resolve_with(
        &mut Host {
            raw: None,
            home: None,
        },
        None,
    )
    .unwrap_err();
    assert_eq!(error.class, "PermissionError");
    assert_eq!(error.message, [0xdcff]);
    assert_eq!((error.errno, error.winerror), (Some(13), Some(-2147024891)));
}

#[test]
fn home_expansion_preserves_a_drive_relative_tail() {
    for (profile, expected) in [
        (".", r".\a:b"),
        ("D:", r"D:a:b"),
        ("D:/profiles/me", r"D:\profiles\me\a:b"),
    ] {
        let mut host = Host {
            raw: Some(cps(r"~\a:b")),
            home: Some(cps(profile)),
        };
        assert_eq!(resolve_with(&mut host, None).unwrap(), cps(expected));
    }
}

#[test]
fn long_non_scalar_components_keep_their_positions() {
    let mut raw = cps("D:/");
    let mut expected = cps(r"D:\");
    for _ in 0..1024 {
        let segment = [0xe000, 0xf0000, 0x100000, 0xd800, 0xdcff];
        raw.extend(segment);
        raw.extend(cps("/./"));
        expected.extend([0xe000, 0xf0000, 0x100000, 0x100ff]);
        expected.push(b'\\' as u32);
    }
    expected.pop();
    let mut host = Host {
        raw: None,
        home: None,
    };
    let actual = resolve_with(&mut host, Some(&raw)).unwrap();
    assert_eq!(actual, expected);
    assert_eq!(wide_units(&actual).len(), actual.len() + 3072);
}

#[test]
fn a_dot_disambiguates_tilde_colon_as_a_relative_account_name() {
    let mut host = Host {
        raw: Some(cps("./~:")),
        home: Some(cps("D:/current")),
    };
    assert_eq!(resolve_with(&mut host, None).unwrap(), cps(r"D:\:"));
    host.home = Some(vec![]);
    assert_eq!(
        resolve_with(&mut host, None).unwrap_err().class,
        "RuntimeError"
    );
    host.raw = Some(cps("~:"));
    assert_eq!(resolve_with(&mut host, None).unwrap(), cps("~:"));
    assert_eq!(
        resolve_with(&mut host, Some(&cps("./~:"))).unwrap(),
        cps(r".\~:")
    );
}
