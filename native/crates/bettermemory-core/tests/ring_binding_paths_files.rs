//! Ordinary scratch documents and file identities. The files have no store data.
mod common;

use bettermemory_core::fsdata::Flavour;
use bettermemory_core::fsutil::fsdecode;
use bettermemory_core::log::ring::binding_path::{self, BindingError, BindingPath};
use bettermemory_core::log::ring::KeyFiles;
use bettermemory_core::pyvalue::PyValue;
use std::{fs, path::Path};

fn ring(dir: &Path) -> KeyFiles {
    KeyFiles::new(dir, &PyValue::str(&"a".repeat(32))).unwrap()
}

#[test]
fn missing_directory_and_fresh_document() {
    let dir = common::stores::scratch("binding-reads");
    let file = dir.0.join("document.path");
    assert_eq!(binding_path::read(&file, Flavour::Posix).unwrap(), None);
    fs::create_dir(&file).unwrap();
    assert!(matches!(
        binding_path::read(&file, Flavour::Posix),
        Err(BindingError::Os(_))
    ));
    fs::remove_dir(&file).unwrap();
    fs::write(&file, br#"{"path":"/fresh"}"#).unwrap();
    let path = binding_path::read(&file, Flavour::Posix).unwrap().unwrap();
    assert_eq!(path.text(), bettermemory_core::unicode::cps("/fresh"));
    fs::write(&file, [0xff]).unwrap();
    assert_eq!(binding_path::read(&file, Flavour::Posix).unwrap(), None);
}

#[test]
fn native_path_round_trip() {
    #[cfg(windows)]
    let flavour = Flavour::Windows;
    #[cfg(unix)]
    let flavour = Flavour::Posix;
    let dir = common::stores::scratch("binding-native");
    let path = dir.0.join("map-🌿");
    fs::write(&path, b"ordinary file").unwrap();
    let lexical = BindingPath::parse(flavour, &fsdecode(path.as_os_str())).unwrap();
    let files = ring(&dir.0);
    fs::write(files.binding_path(), lexical.json().unwrap()).unwrap();
    assert_eq!(files.read_binding_exact().unwrap().as_ref(), Some(&lexical));
    let host = files.read_binding().unwrap().unwrap();
    assert_eq!(fs::read(host).unwrap(), b"ordinary file");
}

#[cfg(unix)]
#[test]
fn lexical_then_physical_identity() {
    use bettermemory_core::store::rotation::bound_here_or_nowhere;
    use std::os::unix::{ffi::OsStrExt, fs::symlink};
    let dir = common::stores::scratch("binding-identity");
    let files = ring(&dir.0);
    let cwd = dir.0.as_os_str().as_bytes();
    let file = dir.0.join("é");
    let scalar = BindingPath::parse(Flavour::Posix, &fsdecode(file.as_os_str())).unwrap();
    let mut points = fsdecode(dir.0.as_os_str());
    points.extend([u32::from(b'/'), 0xdcc3, 0xdca9]);
    let escaped = BindingPath::parse(Flavour::Posix, &points).unwrap();
    assert!(!scalar.lexical_eq(&escaped, Flavour::Posix));
    assert_eq!(scalar.host_path(), escaped.host_path());
    fs::write(files.binding_path(), scalar.json().unwrap()).unwrap();
    assert!(bound_here_or_nowhere(&files, Path::new("é"), cwd).unwrap());
    fs::write(files.binding_path(), escaped.json().unwrap()).unwrap();
    assert!(!bound_here_or_nowhere(&files, Path::new("é"), cwd).unwrap());
    fs::write(&file, b"ordinary file").unwrap();
    symlink(&file, dir.0.join("soft")).unwrap();
    fs::hard_link(&file, dir.0.join("hard")).unwrap();
    for path in ["é", "hard", "soft", "nonexistent"] {
        assert_eq!(
            bound_here_or_nowhere(&files, Path::new(path), cwd).unwrap(),
            path != "nonexistent"
        );
    }
}

#[cfg(target_os = "linux")]
#[test]
fn ordinary_non_utf8_filename() {
    use std::os::unix::ffi::OsStrExt;
    let dir = common::stores::scratch("binding-bytes");
    let raw = dir.0.join(std::ffi::OsStr::from_bytes(b"raw-\xff"));
    fs::write(&raw, b"ordinary file").unwrap();
    let files = ring(&dir.0);
    let path = BindingPath::parse(Flavour::Posix, &fsdecode(raw.as_os_str())).unwrap();
    fs::write(files.binding_path(), path.json().unwrap()).unwrap();
    assert_eq!(
        files.read_binding().unwrap().as_deref(),
        Some(raw.as_path())
    );
    assert_eq!(files.read_binding_exact().unwrap(), Some(path));
}

#[cfg(windows)]
#[test]
fn windows_wide_units_and_present_value() {
    use bettermemory_core::store::rotation::{bound_here_or_nowhere, Declined};
    use std::os::windows::ffi::OsStrExt;
    let dir = common::stores::scratch("binding-wide");
    let files = ring(&dir.0);
    fs::write(files.binding_path(), br#"{"path":"C:/x/\ud800"}"#).unwrap();
    let exact = files.read_binding_exact().unwrap().unwrap();
    let path = files.read_binding().unwrap().unwrap();
    assert_eq!(
        path.as_os_str().encode_wide().collect::<Vec<_>>(),
        exact.windows_units()
    );
    assert!(matches!(
        bound_here_or_nowhere(&files, &dir.0.join("ordinary"), b""),
        Err(Declined::NotPorted)
    ));
}
