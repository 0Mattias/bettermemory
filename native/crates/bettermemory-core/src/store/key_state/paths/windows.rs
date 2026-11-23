//! The host calls made by Windows pathlib's non-strict realpath.
use super::{fsdecode, PyStr, StateError, StoreError};
use crate::fsdata::realpath::{nt_realpath, FinalPath, NtRaise, NtRaised};
use std::ffi::{OsStr, OsString};
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::path::PathBuf;

fn units(path: &[u32]) -> Vec<u16> {
    let mut out = Vec::with_capacity(path.len());
    for &cp in path {
        if let Ok(unit) = u16::try_from(cp) {
            out.push(unit);
        } else {
            out.extend_from_slice(
                char::from_u32(cp)
                    .expect("Python codepoint")
                    .encode_utf16(&mut [0; 2]),
            );
        }
    }
    out
}

fn decode(wide: &[u16]) -> PyStr {
    fsdecode(&OsString::from_wide(wide))
}

pub(crate) fn normalize_spelling(text: &[u32]) -> PyStr {
    // The pinned nt._path_splitroot_ex parses wide units. Pathlib tests ASCII
    // syntax and preserves each non-ASCII unit, including any lone surrogate.
    let wide = units(text);
    let masked: String = wide
        .iter()
        .map(|&c| if c < 128 { c as u8 as char } else { '\u{e000}' })
        .collect();
    let mut originals = wide.iter().copied().filter(|&c| c >= 128);
    let normal: Vec<_> = crate::config::pure_path(&masked, crate::config::Flavour::Windows)
        .chars()
        .map(|c| {
            if c == '\u{e000}' {
                originals.next().expect("path unit retained")
            } else {
                c as u16
            }
        })
        .collect();
    decode(&normal)
}

// Keep the path primitives on UTF-16 unit indices, the domain of the native
// splitroot/normpath. Results from the OS are decoded only at the outer boundary.
pub(super) fn join_name(dir: &[u32], ascii: &[u32]) -> PyStr {
    let path: Vec<_> = units(dir).into_iter().map(u32::from).collect();
    let joined =
        crate::gitfile_rule::hostpath::text::join(crate::fsdata::Flavour::Windows, &path, &[ascii]);
    decode(
        &joined
            .into_iter()
            .map(|unit| unit as u16)
            .collect::<Vec<_>>(),
    )
}
fn as_units(s: &OsStr) -> PyStr {
    s.encode_wide().map(u32::from).collect()
}
fn wide(path: &[u32]) -> Result<Vec<u16>, NtRaise> {
    if path.len() > 32767 || path.contains(&0) {
        Err(NtRaise::Value)
    } else {
        Ok(path.iter().map(|&u| u as u16).collect())
    }
}
fn host(path: &[u32]) -> Result<PathBuf, NtRaise> {
    Ok(OsString::from_wide(&wide(path)?).into())
}
fn raised(e: std::io::Error) -> NtRaise {
    NtRaise::Os(e.raw_os_error().map(i64::from))
}

struct Host;
impl FinalPath for Host {
    fn final_path_name(&self, path: &[u32]) -> Result<PyStr, NtRaise> {
        bettermemory_sys::final_path_name(&wide(path)?)
            .map(|w| w.into_iter().map(u32::from).collect())
            .map_err(raised)
    }
    fn full_path_name(&self, path: &[u32]) -> Option<PyStr> {
        bettermemory_sys::full_path_name(&wide(path).ok()?)
            .ok()
            .map(|w| w.into_iter().map(u32::from).collect())
    }
    fn readlink(&self, path: &[u32]) -> Result<PyStr, NtRaise> {
        std::fs::read_link(host(path)?)
            .map(|p| as_units(p.as_os_str()))
            .map_err(raised)
    }
    fn is_link(&self, path: &[u32]) -> bool {
        host(path).is_ok_and(|p| std::fs::symlink_metadata(p).is_ok_and(|m| m.is_symlink()))
    }
    fn find_first_file(&self, path: &[u32]) -> Result<PyStr, NtRaise> {
        bettermemory_sys::find_first_name(&wide(path)?)
            .map(|w| w.into_iter().map(u32::from).collect())
            .map_err(raised)
    }
}

pub(crate) fn resolve_spelling(text: &[u32]) -> Result<PyStr, StateError> {
    let cwd = std::env::current_dir().map_err(StoreError::Io)?;
    let path = units(&normalize_spelling(text))
        .into_iter()
        .map(u32::from)
        .collect::<Vec<_>>();
    let out = nt_realpath(&Host, &path, Ok(&as_units(cwd.as_os_str()))).map_err(|e| match e {
        NtRaised::Os(code) => StateError::Read(StoreError::Io(std::io::Error::from_raw_os_error(code.unwrap_or(1) as i32))),
        NtRaised::Value => StateError::raised("ValueError", "embedded null character in path"),
        NtRaised::Unbound => StateError::raised("UnboundLocalError", "cannot access local variable 'initial_winerror' where it is not associated with a value"),
    })?;
    Ok(normalize_spelling(&decode(
        &out.into_iter().map(|u| u as u16).collect::<Vec<_>>(),
    )))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::unicode::cps;

    /// Leave `path` pending deletion and return the handle that keeps its
    /// directory entry. A handle opened to delete on close marks the file when
    /// it closes, and the file stays, marked, while another handle is open.
    /// Every open of a marked file fails with ERROR_ACCESS_DENIED, whatever the
    /// caller's rights: one of the codes that send the non-strict `realpath` to
    /// the directory-entry lookup. An exclusive open does not fail the call
    /// (with no access requested the share mode is never checked, CI run
    /// 45260976205), and neither did denying SYNCHRONIZE on the hosted runner
    /// (CI run 45284088119). Dropping the returned handle removes the file.
    fn pending_delete(path: &std::path::Path) -> std::fs::File {
        use std::os::windows::fs::OpenOptionsExt;
        const DELETE: u32 = 0x0001_0000;
        const DELETE_ON_CLOSE: u32 = 0x0400_0000;
        let keep = std::fs::OpenOptions::new()
            .read(true)
            .share_mode(7)
            .open(path)
            .unwrap();
        let marker = std::fs::OpenOptions::new()
            .access_mode(DELETE)
            .share_mode(7)
            .custom_flags(DELETE_ON_CLOSE)
            .open(path)
            .unwrap();
        drop(marker);
        keep
    }

    #[test]
    fn resolve_recovers_the_entry_spelling_when_final_path_is_denied() {
        let file =
            std::env::temp_dir().join(format!("BM-Guard-{:016x}.tmp", getrandom::u64().unwrap()));
        std::fs::write(&file, b"").unwrap();
        let name = file.file_name().unwrap();
        let lower = file.with_file_name(name.to_ascii_lowercase());
        let lookup = if std::fs::symlink_metadata(&lower).is_ok() {
            &lower
        } else {
            &file
        };
        let keep = pending_delete(&file);
        assert_eq!(
            bettermemory_sys::final_path_name(
                &lookup.as_os_str().encode_wide().collect::<Vec<_>>()
            )
            .unwrap_err()
            .raw_os_error(),
            Some(5)
        );
        let result = resolve_spelling(&fsdecode(lookup.as_os_str())).unwrap();
        let basename =
            crate::gitfile_rule::hostpath::text::basename(crate::fsdata::Flavour::Windows, &result);
        assert_eq!(basename, fsdecode(name));
        drop(keep);
        assert!(std::fs::symlink_metadata(&file).is_err());
    }

    #[test]
    fn windows_wide_lexical_and_lookup_boundaries() {
        assert_eq!(normalize_spelling(&[0xd83d, 0xde42]), [0x1f642]);
        assert_eq!(
            normalize_spelling(&[0xf0000, 0x100000, 0xd800]),
            [0xf0000, 0x100000, 0xd800]
        );
        assert_eq!(join_name(&cps("🙂:"), &cps("name")), cps(r"🙂:\name"));
        assert_eq!(join_name(&cps("D:"), &cps("name")), cps("D:name"));
        let too_long = [cps(r"C:\"), vec![u32::from(b'a'); 32768]].concat();
        assert_eq!(resolve_spelling(&too_long).unwrap(), too_long);
    }
}
