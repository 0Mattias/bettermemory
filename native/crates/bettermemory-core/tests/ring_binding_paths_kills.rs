//! Boundary cases for the binding reader and its compatibility projection.
mod common;

use bettermemory_core::fsdata::Flavour;
use bettermemory_core::log::ring::binding_path::{BindingError, BindingPath};
use bettermemory_core::log::ring::{KeyFiles, RingError};
use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::unicode::cps;
use std::fs;

#[test]
fn lexical_values_do_not_collapse_to_a_host_spelling() {
    let path = |t: &[u32]| BindingPath::parse(Flavour::Posix, t).unwrap();
    let mut name = cps("//a/./b/../c/");
    name.push(0xfffd);
    assert_eq!(path(&name).text(), cps("//a/b/../c/�"));
    assert!(BindingPath::parse(Flavour::Posix, &[47, 0x11_0000]).is_none());
    assert!(BindingPath::parse(Flavour::Posix, &[47, 0]).is_none());
    for cp in [0xd800, 0xdc00, 0xdc7f, 0xdd00, 0xdfff] {
        assert_eq!(path(&[47, cp]).posix_bytes(), None);
    }
    for cp in [0xdc80, 0xdc81, 0xdcfe, 0xdcff] {
        assert_eq!(
            path(&[47, cp]).posix_bytes(),
            Some(vec![b'/', (cp - 0xdc00) as u8])
        );
    }
}

#[test]
fn recursion_is_neither_a_malformed_document_nor_an_io_error() {
    let dir = common::stores::scratch("binding-depth");
    let files = KeyFiles::new(&dir.0, &PyValue::str(&"b".repeat(32))).unwrap();
    for (opening, context) in [("[", "array"), (r#"{"x":"#, "object")] {
        let original = opening.repeat(bettermemory_core::json::MAX_DEPTH + 2);
        fs::write(files.binding_path(), &original).unwrap();
        let error = files.read_binding_exact().unwrap_err();
        assert!(matches!(error, BindingError::Recursion(message) if message.contains(context)));
        assert!(matches!(
            files.read_binding(),
            Err(RingError::Py(PyError::RecursionError))
        ));
        assert!(matches!(
            files.rebind_store_exact(&dir.0.join("ordinary"), b"/"),
            Err(BindingError::Recursion(_))
        ));
        assert_eq!(fs::read_to_string(files.binding_path()).unwrap(), original);
    }
}

#[cfg(unix)]
#[test]
fn exact_previous_and_unrepresentable_compatibility_result() {
    use std::io::ErrorKind;
    use std::os::unix::ffi::OsStrExt;

    let dir = common::stores::scratch("binding-previous");
    let cwd = dir.0.as_os_str().as_bytes();
    let target = dir.0.join("ordinary");
    let files = KeyFiles::new(&dir.0, &PyValue::str(&"b".repeat(32))).unwrap();
    for previous in ["/unicode/\\ud800", "/unicode/\\udcc3\\udca9"] {
        let raw = format!(r#"{{"path":"{previous}"}}"#);
        fs::write(files.binding_path(), &raw).unwrap();
        let exact = files.read_binding_exact().unwrap().unwrap();
        assert!(
            matches!(files.read_binding(), Err(RingError::Os(e)) if e.kind() == ErrorKind::InvalidData)
        );
        assert!(
            matches!(files.rebind_store(&target, cwd), Err(RingError::Os(e)) if e.kind() == ErrorKind::InvalidData)
        );
        assert_eq!(fs::read_to_string(files.binding_path()).unwrap(), raw);
        assert_eq!(files.rebind_store_exact(&target, cwd).unwrap(), Some(exact));
        assert_eq!(
            files.read_binding().unwrap().as_deref(),
            Some(target.as_path())
        );
    }
}
