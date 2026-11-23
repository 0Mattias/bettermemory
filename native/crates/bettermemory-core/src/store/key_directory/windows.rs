//! Windows Path selection and the locked platformdirs LocalAppData lookup.
use crate::config::{pure_path, Flavour, KEYS_DIR_ENV};
use crate::fsdata::Flavour::Windows;
use crate::gitfile_rule::hostpath::{nt_join_by, splitroot, text};
use crate::pyvalue::PyStr;
use crate::unicode::{cps, lower, py_strip};

const SLASH: u32 = b'\\' as u32;
const TILDE: u32 = b'~' as u32;

/// The Python error raised by the Path or shell-folder lookup.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DirectoryError {
    pub class: &'static str,
    pub message: PyStr,
    pub errno: Option<i32>,
    pub winerror: Option<i32>,
}

impl DirectoryError {
    fn plain(class: &'static str, message: &str) -> Self {
        Self {
            class,
            message: cps(message),
            errno: None,
            winerror: None,
        }
    }
}

/// Inputs to the Windows directory selector. The shell result is read afresh
/// on each lookup. A successful NULL pointer is distinct from an empty path.
pub trait Folders {
    fn environment(&mut self, name: &str) -> Option<PyStr>;
    fn local_app_data(&mut self) -> Result<Option<PyStr>, DirectoryError>;
}

fn lexical(path: &[u32]) -> PyStr {
    // Path parsing touches ASCII syntax and retains all non-ASCII codepoints
    // once and in order. Mask every non-ASCII value, even a real U+E000.
    let spelling: String = path
        .iter()
        .map(|&cp| {
            if cp < 128 {
                cp as u8 as char
            } else {
                '\u{e000}'
            }
        })
        .collect();
    let mut original = path.iter().copied().filter(|&cp| cp >= 128);
    pure_path(&spelling, Flavour::Windows)
        .chars()
        .map(|c| {
            if c == '\u{e000}' {
                original.next().expect("non-ASCII component preserved")
            } else {
                c as u32
            }
        })
        .collect()
}

/// Windows UTF-16 units of a Python string, retaining unpaired surrogates.
pub fn wide_units(selected: &[u32]) -> Vec<u16> {
    let mut out = Vec::with_capacity(selected.len());
    for &cp in selected {
        if let Ok(unit) = u16::try_from(cp) {
            out.push(unit);
        } else {
            let scalar = char::from_u32(cp).expect("valid Python codepoint");
            out.extend_from_slice(scalar.encode_utf16(&mut [0; 2]));
        }
    }
    out
}

// CPython 3.13 nt._path_splitroot_ex operates on wchar_t units and returns
// Python strings through PyUnicode_FromWideChar. A valid surrogate pair
// becomes a scalar, and a drive colon is tested at unit index one.
fn path_units(text: &[u32]) -> PyStr {
    if text.iter().all(|&cp| cp <= u16::MAX.into()) {
        return text.to_vec();
    }
    wide_units(text).into_iter().map(u32::from).collect()
}

fn from_units(units: PyStr) -> PyStr {
    if !units.iter().any(|unit| (0xd800..=0xdbff).contains(unit)) {
        return units;
    }
    char::decode_utf16(units.into_iter().map(|unit| unit as u16))
        .map(|r| r.map_or_else(|e| u32::from(e.unpaired_surrogate()), u32::from))
        .collect()
}

fn env_units(folders: &mut impl Folders, name: &str) -> Option<PyStr> {
    folders.environment(name).map(|s| path_units(&s))
}

fn join(path: &[u32], parts: &[&[u32]]) -> PyStr {
    nt_join_by(path, parts, |left, right| {
        lower(&from_units(left.to_vec())) == lower(&from_units(right.to_vec()))
    })
}

fn no_home() -> DirectoryError {
    DirectoryError::plain("RuntimeError", "Could not determine home directory.")
}

fn expanduser(folders: &mut impl Folders, raw: &[u32]) -> Result<PyStr, DirectoryError> {
    let spelling = lexical(raw);
    // Path displays a protective dot before a relative drive-like component
    // (e.g. "./~:"). expanduser inspects that component and the original drive.
    let path = spelling
        .strip_prefix(&[b'.' as u32, SLASH])
        .unwrap_or(&spelling);
    if path.first() != Some(&TILDE) || !splitroot(raw).0.is_empty() {
        return Ok(spelling);
    }
    let first = path
        .iter()
        .position(|&cp| cp == SLASH)
        .unwrap_or(path.len());
    let mut home = match env_units(folders, "USERPROFILE") {
        Some(value) => value,
        None => {
            let part = env_units(folders, "HOMEPATH").ok_or_else(no_home)?;
            let drive = env_units(folders, "HOMEDRIVE").unwrap_or_default();
            join(&drive, &[&part])
        }
    };
    if first > 1 {
        let requested = &path[1..first];
        let user = env_units(folders, "USERNAME");
        if user.as_deref() != Some(requested) {
            if user.as_deref() != Some(text::basename(Windows, &home).as_slice()) {
                return Err(no_home());
            }
            home = join(&text::dirname(Windows, &home), &[requested]);
        }
    }
    if home.first() == Some(&TILDE) {
        return Err(no_home());
    }
    let mut result = lexical(&home);
    if first < path.len() {
        // Path.expanduser appends the original tail as components. In
        // particular a tail that starts with a drive is still a component.
        let (drive, root, rest) = splitroot(&result);
        let drive_only = !drive.is_empty() && root.is_empty() && rest.is_empty();
        if !drive_only && result.last() != Some(&SLASH) {
            result.push(SLASH);
        }
        result.extend_from_slice(&path[first + 1..]);
        result = lexical(&result);
    }
    Ok(result)
}

fn fully_qualified(path: &[u32]) -> bool {
    let (drive, root, _) = splitroot(path);
    drive
        .first()
        .is_some_and(|&c| c == SLASH || c == b'/' as u32)
        || (!drive.is_empty() && !root.is_empty())
}

/// `log.default_keys_dir` with the system inputs supplied by its caller.
pub fn default_with(folders: &mut impl Folders) -> Result<PyStr, DirectoryError> {
    let value = folders
        .environment("WIN_PD_OVERRIDE_LOCAL_APPDATA")
        .unwrap_or_default();
    let value = path_units(py_strip(&value));
    let candidate = value.as_slice();
    let base = if fully_qualified(candidate) {
        candidate.to_vec()
    } else {
        path_units(&folders.local_app_data()?.ok_or_else(|| {
            DirectoryError::plain(
                "ValueError",
                "SHGetKnownFolderPath returned NULL for CSIDL_LOCAL_APPDATA",
            )
        })?)
    };
    let app = cps("bettermemory");
    Ok(from_units(lexical(&join(
        &text::normpath(Windows, &base),
        &[&app, &app, &cps("keys")],
    ))))
}

/// `Store.open` with a nullable keys_dir, after its metadata checks.
pub fn resolve_with(
    folders: &mut impl Folders,
    explicit: Option<&[u32]>,
) -> Result<PyStr, DirectoryError> {
    if let Some(path) = explicit {
        return Ok(from_units(lexical(&path_units(path))));
    }
    match folders.environment(KEYS_DIR_ENV).filter(|v| !v.is_empty()) {
        Some(value) => expanduser(folders, &path_units(&value)).map(from_units),
        None => default_with(folders),
    }
}

#[cfg(windows)]
struct System;

#[cfg(windows)]
impl Folders for System {
    fn environment(&mut self, name: &str) -> Option<PyStr> {
        std::env::var_os(name).map(|v| crate::fsutil::fsdecode(&v))
    }
    fn local_app_data(&mut self) -> Result<Option<PyStr>, DirectoryError> {
        bettermemory_sys::local_app_data()
            .map(|path| path.map(|p| crate::fsutil::fsdecode(&p)))
            .map_err(|error| DirectoryError {
                class: error.class,
                message: cps(&error.message),
                errno: Some(error.errno),
                winerror: Some(error.hresult),
            })
    }
}

#[cfg(windows)]
pub fn default_keys_dir() -> Result<PyStr, DirectoryError> {
    default_with(&mut System)
}

#[cfg(windows)]
pub fn resolve_keys_dir(explicit: Option<&[u32]>) -> Result<PyStr, DirectoryError> {
    resolve_with(&mut System, explicit)
}

#[cfg(windows)]
pub fn os_path(selected: &[u32]) -> Result<std::path::PathBuf, std::convert::Infallible> {
    use std::os::windows::ffi::OsStringExt;
    Ok(std::ffi::OsString::from_wide(&wide_units(selected)).into())
}
