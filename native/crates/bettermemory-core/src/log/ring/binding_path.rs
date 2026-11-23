//! A key-ring binding records the spelling of a Python Path. The OS encoding
//! is needed only for a filesystem operation and can lose spelling information.

use crate::fsdata::Flavour;
use crate::fsutil::fsdecode;
use crate::gitfile_rule::hostpath::{splitroot, text::isabs};
use crate::json;
use crate::project_walk::{path_eq, pure_text};
use crate::pyvalue::{PyError, PyStr, PyValue};
use crate::response_write::{fsencode_as, FsEncoding};
use crate::unicode::cps;
use std::{
    fs, io,
    path::{Path, PathBuf},
};

#[cfg(unix)]
pub(crate) const HOST: Flavour = Flavour::Posix;
#[cfg(windows)]
pub(crate) const HOST: Flavour = Flavour::Windows;

/// The normalized, absolute Python path stored in a binding document.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BindingPath(PyStr);

#[derive(Debug)]
pub enum BindingError {
    Os(io::Error),
    Recursion(&'static str),
    Value,
}

impl From<io::Error> for BindingError {
    fn from(err: io::Error) -> Self {
        Self::Os(err)
    }
}

impl From<BindingError> for super::RingError {
    fn from(err: BindingError) -> Self {
        match err {
            BindingError::Os(err) => Self::Os(err),
            BindingError::Recursion(_) => Self::Py(PyError::RecursionError),
            BindingError::Value => Self::Py(PyError::ValueError),
        }
    }
}

impl BindingPath {
    pub fn parse(flavour: Flavour, text: &[u32]) -> Option<Self> {
        if text.is_empty() || text.iter().any(|&c| c == 0 || c > 0x10ffff) || !isabs(flavour, text)
        {
            return None;
        }
        let (sep, mut root, tail) = match flavour {
            Flavour::Posix => {
                let sep = u32::from(b'/');
                let n = text.iter().take_while(|&&c| c == sep).count();
                (
                    sep,
                    vec![sep; if n == 2 { 2 } else { 1 }],
                    text[n..].to_vec(),
                )
            }
            Flavour::Windows => {
                let sep = u32::from(b'\\');
                let swapped: PyStr = text
                    .iter()
                    .map(|&c| if c == u32::from(b'/') { sep } else { c })
                    .collect();
                let (drive, root, tail) = splitroot(&swapped);
                let prefix = if root.is_empty() {
                    pure_text(flavour, &drive)
                } else {
                    [drive, root].concat()
                };
                (sep, prefix, tail)
            }
        };
        for part in tail
            .split(|&c| c == sep)
            .filter(|p| !p.is_empty() && *p != [u32::from(b'.')])
        {
            if root.last() != Some(&sep) {
                root.push(sep);
            }
            root.extend_from_slice(part);
        }
        Some(Self(root))
    }

    pub fn text(&self) -> &[u32] {
        &self.0
    }

    pub fn lexical_eq(&self, other: &Self, flavour: Flavour) -> bool {
        path_eq(flavour, self.text(), other.text())
    }

    pub fn posix_bytes(&self) -> Option<Vec<u8>> {
        fsencode_as(self.text(), FsEncoding::SurrogateEscape)
    }

    pub fn windows_units(&self) -> Vec<u16> {
        let mut units = Vec::new();
        for &cp in self.text() {
            if let Ok(unit) = u16::try_from(cp) {
                units.push(unit);
            } else if let Some(ch) = char::from_u32(cp) {
                units.extend_from_slice(ch.encode_utf16(&mut [0; 2]));
            }
        }
        units
    }

    /// The OS projection for actual stat and open calls. Distinct spellings
    /// can project to the same bytes or UTF-16 units.
    pub fn host_path(&self) -> Option<PathBuf> {
        #[cfg(unix)]
        {
            use std::os::unix::ffi::OsStringExt;
            self.posix_bytes()
                .map(|b| std::ffi::OsString::from_vec(b).into())
        }
        #[cfg(windows)]
        {
            use std::os::windows::ffi::OsStringExt;
            Some(std::ffi::OsString::from_wide(&self.windows_units()).into())
        }
    }

    pub(crate) fn exact_host(&self) -> Result<PathBuf, BindingError> {
        self.host_path()
            .filter(|path| fsdecode(path.as_os_str()) == self.0)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "the recorded Python path has no lossless host path",
                )
                .into()
            })
    }

    /// A resolved path from the OS. Path.resolve removes the Windows
    /// verbatim prefix; a hand-authored binding document keeps it.
    pub(crate) fn resolved(path: &Path) -> Self {
        let text = fsdecode(path.as_os_str());
        #[cfg(windows)]
        let text = if text.starts_with(&cps(r"\\?\UNC\")) {
            [cps(r"\\"), text[8..].to_vec()].concat()
        } else if text.starts_with(&cps(r"\\?\")) {
            text[4..].to_vec()
        } else {
            text
        };
        Self(text)
    }

    pub fn json(&self) -> io::Result<String> {
        let value = PyValue::Dict(vec![(PyValue::str("path"), PyValue::Str(self.0.clone()))]);
        json::dumps(&value, json::DumpOptions::DEFAULT)
            .map(|s| json::utf8_text(&s))
            .map_err(|_| io::Error::other("the binding"))
    }

    pub(crate) fn same_file(&self, other: &Self) -> bool {
        if self.lexical_eq(other, HOST) {
            return true;
        }
        let (Some(a), Some(b)) = (self.host_path(), other.host_path()) else {
            return false;
        };
        same_identity(&a, &b)
    }
}

/// Compare two existing filesystem names, following symbolic links.
pub(crate) fn same_identity(a: &Path, b: &Path) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        fs::metadata(a)
            .is_ok_and(|x| fs::metadata(b).is_ok_and(|y| x.dev() == y.dev() && x.ino() == y.ino()))
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        let identity = |path| {
            // A stat-style handle also works for ordinary directories.
            let file = fs::OpenOptions::new()
                .access_mode(0x80)
                .custom_flags(0x0200_0000)
                .open(path)?;
            bettermemory_sys::file_index(&file)
        };
        identity(a).is_ok_and(|x| identity(b).is_ok_and(|y| x == y))
    }
}

/// Read the binding's contents. Only a missing file or a ValueError from
/// decoding is absent; other OS errors and the decoder's recursion error
/// retain their separate meanings for each caller.
pub fn read(path: &Path, flavour: Flavour) -> Result<Option<BindingPath>, BindingError> {
    let bytes = match fs::read(path) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        result => result?,
    };
    let Ok(s) = std::str::from_utf8(&bytes) else {
        return Ok(None);
    };
    let text = cps(&s.replace("\r\n", "\n").replace('\r', "\n"));
    match json::loads(&text) {
        Ok(v) => Ok(match v.get("path") {
            Some(PyValue::Str(s)) => BindingPath::parse(flavour, s),
            _ => None,
        }),
        Err(PyError::RecursionError) => Err(BindingError::Recursion(
            json::reading_recursion_message(&text),
        )),
        Err(_) => Ok(None),
    }
}
