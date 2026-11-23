//! The signing-key directory selected by a POSIX Store after its version checks.
use crate::config::{posix, ConfigError, KEYS_DIR_ENV};
use crate::fsdata::Flavour;
use crate::gitfile_rule::follow::fsencode;
use crate::gitsystem::record::surrogateescape;
use crate::pyvalue::PyStr;
use crate::verify_anchoring::attest::pure_text;
use std::env;
use std::ffi::OsString;
use std::fmt;
use std::os::unix::ffi::OsStringExt;
use std::path::{Path, PathBuf};

/// `log.default_keys_dir`: the application user configuration directory's `keys` child.
pub fn default_keys_dir() -> Result<PyStr, ConfigError> {
    let path = posix::user_config_dir()?.join("keys");
    Ok(surrogateescape(posix::bytes(&path)))
}

/// Keep the spelling of the Python Path, including codepoints that share an OS byte spelling.
/// An explicit argument only constructs a Path. The environment override expands a home.
pub fn resolve_keys_dir(explicit: Option<&[u32]>) -> Result<PyStr, ConfigError> {
    if let Some(text) = explicit {
        return Ok(pure_text(Flavour::Posix, text).expect("POSIX lexical paths are infallible"));
    }
    match env::var_os(KEYS_DIR_ENV).filter(|v| !v.is_empty()) {
        Some(raw) => {
            let path = posix::expanded(Path::new(&raw))?;
            Ok(surrogateescape(posix::bytes(&path)))
        }
        None => default_keys_dir(),
    }
}

/// The source fields of `os.fsencode(path)`'s `UnicodeEncodeError`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FsEncodeError<'a> {
    pub encoding: &'static str,
    pub object: &'a [u32],
    pub start: usize,
    pub end: usize,
    pub reason: &'static str,
}
impl fmt::Display for FsEncodeError<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "'{}' codec can't encode ", self.encoding)?;
        if self.end == self.start + 1 {
            write!(
                f,
                "character '\\u{:04x}' in position {}",
                self.object[self.start], self.start
            )?;
        } else {
            write!(f, "characters in position {}-{}", self.start, self.end - 1)?;
        }
        write!(f, ": {}", self.reason)
    }
}
impl std::error::Error for FsEncodeError<'_> {}

/// Project the selected spelling only when an OS operation needs its bytes.
/// An embedded NUL is encodable. The filesystem operation can reject it later.
pub fn os_path(selected: &[u32]) -> Result<PathBuf, FsEncodeError<'_>> {
    if let Some(bytes) = fsencode(selected) {
        return Ok(OsString::from_vec(bytes).into());
    }
    let start = selected
        .iter()
        .position(|&cp| char::from_u32(cp).is_none() && !(0xdc80..=0xdcff).contains(&cp))
        .expect("fsencode rejected a codepoint");
    // After surrogateescape consumes any encodable prefix, the UTF-8 encoder
    // reports the entire remaining contiguous surrogate run, even U+DC80..DCFF.
    let end = start
        + 1
        + selected[start + 1..]
            .iter()
            .take_while(|&&p| (0xd800..=0xdfff).contains(&p))
            .count();
    Err(FsEncodeError {
        encoding: "utf-8",
        object: selected,
        start,
        end,
        reason: "surrogates not allowed",
    })
}
