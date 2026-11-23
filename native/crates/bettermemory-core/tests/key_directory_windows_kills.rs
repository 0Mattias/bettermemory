//! Directory lookup is lazy and preserves the spelling before conversion to UTF-16.
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::store::key_directory::windows::{
    default_with, resolve_with, wide_units, DirectoryError, Folders,
};
use bettermemory_core::unicode::cps;

#[derive(Default)]
struct Calls {
    reads: Vec<String>,
    keys: Option<PyStr>,
    override_path: Option<PyStr>,
    folders: Vec<PyStr>,
}
impl Folders for Calls {
    fn environment(&mut self, name: &str) -> Option<PyStr> {
        self.reads.push(name.into());
        match name {
            "BETTERMEMORY_KEYS_DIR" => self.keys.clone(),
            "WIN_PD_OVERRIDE_LOCAL_APPDATA" => self.override_path.clone(),
            _ => None,
        }
    }
    fn local_app_data(&mut self) -> Result<Option<PyStr>, DirectoryError> {
        self.reads.push("shell".into());
        Ok(Some(
            self.folders.pop().expect("an unexpected folder lookup"),
        ))
    }
}
#[test]
fn empty_key_override_uses_the_shell_until_a_qualified_folder_is_supplied() {
    let mut calls = Calls {
        keys: Some(Vec::new()),
        folders: vec![cps("H:/second"), cps("R:/first")],
        ..Calls::default()
    };
    assert_eq!(
        resolve_with(&mut calls, None).unwrap(),
        cps(r"R:\first\bettermemory\bettermemory\keys")
    );
    calls.override_path = Some(cps("  D:/override   "));
    assert_eq!(
        default_with(&mut calls).unwrap(),
        cps(r"D:\override\bettermemory\bettermemory\keys")
    );
    calls.override_path = Some(cps("Q:relative"));
    assert_eq!(
        default_with(&mut calls).unwrap(),
        cps(r"H:\second\bettermemory\bettermemory\keys")
    );
    assert_eq!(
        calls.reads,
        [
            "BETTERMEMORY_KEYS_DIR",
            "WIN_PD_OVERRIDE_LOCAL_APPDATA",
            "shell",
            "WIN_PD_OVERRIDE_LOCAL_APPDATA",
            "WIN_PD_OVERRIDE_LOCAL_APPDATA",
            "shell"
        ]
    );
}

#[test]
fn windows_splitroot_returns_canonical_pairs_and_preserves_lone_units() {
    let mut calls = Calls::default();
    for raw in [
        vec![0x1f642],
        vec![0xf0000, 0x100000, 0xd800],
        vec![0x61, 0, 0x62],
    ] {
        let selected = resolve_with(&mut calls, Some(&raw)).unwrap();
        assert_eq!(selected, raw);
    }
    assert_eq!(
        resolve_with(&mut calls, Some(&[0xd83d, 0xde42])).unwrap(),
        [0x1f642]
    );
    assert_eq!(
        resolve_with(&mut calls, Some(&cps("./🙂:file"))).unwrap(),
        cps("🙂:file")
    );
    assert_eq!(wide_units(&[0x1f642]), wide_units(&[0xd83d, 0xde42]));
    assert_eq!(wide_units(&[0xd800, 0, 0xdcff]), [0xd800, 0, 0xdcff]);
    assert_eq!(resolve_with(&mut calls, Some(&[])).unwrap(), cps("."));
    assert!(calls.reads.is_empty());
}

#[test]
fn drive_colon_is_recognized_at_wide_unit_index_one() {
    // Windows' C splitroot treats the two units of this scalar as
    // separate for drive recognition. An astral colon path is relative.
    let mut calls = Calls {
        override_path: Some(cps("🙂:/qualified-looking")),
        folders: vec![cps("R:/actual")],
        ..Calls::default()
    };
    assert_eq!(
        default_with(&mut calls).unwrap(),
        cps(r"R:\actual\bettermemory\bettermemory\keys")
    );
    assert_eq!(calls.reads, ["WIN_PD_OVERRIDE_LOCAL_APPDATA", "shell"]);

    struct Profile(&'static str);
    impl Folders for Profile {
        fn environment(&mut self, name: &str) -> Option<PyStr> {
            match name {
                "BETTERMEMORY_KEYS_DIR" => Some(cps("~/sub")),
                "HOMEDRIVE" => Some(cps("🙂:")),
                "HOMEPATH" => Some(cps(self.0)),
                _ => None,
            }
        }
        fn local_app_data(&mut self) -> Result<Option<PyStr>, DirectoryError> {
            panic!("the key override bypasses the shell")
        }
    }
    assert_eq!(
        resolve_with(&mut Profile("/root"), None).unwrap(),
        cps(r"\root\sub")
    );
    assert_eq!(
        resolve_with(&mut Profile("relative"), None).unwrap(),
        cps(r"🙂:\relative\sub")
    );
}

#[test]
fn unc_home_drives_compare_lowercase_decoded_supplementary_letters() {
    struct Home(&'static str, &'static str);
    impl Folders for Home {
        fn environment(&mut self, name: &str) -> Option<PyStr> {
            match name {
                "BETTERMEMORY_KEYS_DIR" => Some(cps("~/sub")),
                "HOMEDRIVE" => Some(cps(self.0)),
                "HOMEPATH" => Some(cps(self.1)),
                _ => None,
            }
        }
        fn local_app_data(&mut self) -> Result<Option<PyStr>, DirectoryError> {
            panic!("override")
        }
    }
    for (drive, part, want) in [
        ("//𐐀/share/folder", "//𐐨/share", r"\\𐐨\share\folder\sub"),
        ("//𞤀/share/folder", "//𞤢/share", r"\\𞤢\share\folder\sub"),
        (
            "//𐐀/share/folder",
            "//different/share",
            r"\\different\share\sub",
        ),
    ] {
        assert_eq!(
            resolve_with(&mut Home(drive, part), None).unwrap(),
            cps(want)
        );
    }
}
