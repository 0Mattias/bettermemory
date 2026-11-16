//! The two [`RefFs`] implementations: [`StdFs`] over the machine's file
//! system, and [`MemFs`], a file system as data for tests and fixtures.

use std::collections::BTreeMap;
use std::io::{ErrorKind, Read};

use super::{FsError, RefFs};

/// How a file system answers the two calls where POSIX and Windows differ
/// for the reference: `O_NOFOLLOW` (POSIX refuses a link, Windows follows
/// it) and a file where a directory of the path should be (ENOTDIR on
/// POSIX, "not found" on Windows).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Flavour {
    Posix,
    Windows,
}

impl Flavour {
    /// The flavour of the machine this runs on.
    pub fn host() -> Flavour {
        if cfg!(windows) {
            Flavour::Windows
        } else {
            Flavour::Posix
        }
    }
}

/// The machine's file system, as `gitfile_rule._read` and `os.lstat` see it.
#[derive(Clone, Copy, Debug, Default)]
pub struct StdFs;

#[cfg(unix)]
fn native_path(path: &[u8]) -> Result<std::path::PathBuf, FsError> {
    use std::os::unix::ffi::OsStrExt;
    if path.contains(&0) {
        // Python raises ValueError, which no reader here takes as missing.
        return Err(FsError::Other);
    }
    Ok(std::ffi::OsStr::from_bytes(path).into())
}

#[cfg(not(unix))]
fn native_path(path: &[u8]) -> Result<std::path::PathBuf, FsError> {
    if path.contains(&0) {
        return Err(FsError::Other);
    }
    // Python decodes a bytes path as UTF-8 on Windows.
    let text = std::str::from_utf8(path).map_err(|_| FsError::Other)?;
    Ok(text.into())
}

fn error_of(error: &std::io::Error) -> FsError {
    match error.kind() {
        ErrorKind::NotFound => FsError::Missing,
        ErrorKind::NotADirectory => FsError::NotADirectory,
        ErrorKind::IsADirectory => FsError::IsADirectory,
        // CPython maps a name Windows refuses (`<>|"`, ERROR_INVALID_NAME)
        // to FileNotFoundError, which the reference reads as missing.
        ErrorKind::InvalidFilename if cfg!(windows) => FsError::Missing,
        _ => FsError::Other,
    }
}

fn is_link(path: &std::path::Path) -> bool {
    std::fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink())
}

impl RefFs for StdFs {
    fn read(&self, path: &[u8], limit: usize, follow: bool) -> Result<Vec<u8>, FsError> {
        let path = native_path(path)?;
        // POSIX opens with O_NOFOLLOW where `follow` is false; Windows has
        // no such flag and the reference follows the link there.
        let refuse_link = !follow && cfg!(unix);
        let meta = if refuse_link {
            std::fs::symlink_metadata(&path)
        } else {
            std::fs::metadata(&path)
        }
        .map_err(|e| error_of(&e))?;
        if meta.is_dir() {
            // Windows: `os.open` follows a link to a directory and fails
            // with PermissionError; `_is_directory(follow=False)` then sees
            // a link, not a directory, and the error rises (no
            // IsADirectoryError).
            if !follow && !refuse_link && is_link(&path) {
                return Err(FsError::Other);
            }
            return Err(FsError::IsADirectory);
        }
        // A link (ELOOP), a FIFO, a device: never opened, so never blocks.
        if !meta.is_file() {
            return Err(FsError::Other);
        }
        let file = std::fs::File::open(&path).map_err(|e| error_of(&e))?;
        // Sized once from the length the file reports, never past the
        // bound, so a large file is not copied while its buffer grows.
        let hint = usize::try_from(meta.len()).unwrap_or(usize::MAX).min(limit);
        let mut out = Vec::with_capacity(hint + 1);
        file.take(limit as u64 + 1)
            .read_to_end(&mut out)
            .map_err(|e| error_of(&e))?;
        Ok(out)
    }

    fn lstat_is_dir(&self, path: &[u8]) -> Result<bool, FsError> {
        let path = native_path(path)?;
        let meta = std::fs::symlink_metadata(path).map_err(|e| error_of(&e))?;
        Ok(meta.is_dir())
    }
}

/// One entry of a [`MemFs`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Node {
    File(Vec<u8>),
    Dir,
    /// A symbolic link to a path, relative to the link's directory.
    Link(Vec<u8>),
    /// A FIFO or any other file that is neither regular nor a directory.
    Special,
}

/// A file system as data: paths of `/`-separated components (no leading
/// slash) to [`Node`]s. A parent directory need not be listed.
#[derive(Clone, Debug)]
pub struct MemFs {
    pub flavour: Flavour,
    pub nodes: BTreeMap<Vec<u8>, Node>,
}

/// Links followed before a path fails, as an OS's ELOOP.
const LINK_HOPS: usize = 40;

impl MemFs {
    pub fn new(flavour: Flavour) -> MemFs {
        MemFs {
            flavour,
            nodes: BTreeMap::new(),
        }
    }

    pub fn insert(&mut self, path: &[u8], node: Node) {
        self.nodes.insert(path.to_vec(), node);
    }

    fn node(&self, path: &[u8]) -> Option<&Node> {
        if path.is_empty() {
            return Some(&Node::Dir);
        }
        if let Some(node) = self.nodes.get(path) {
            return Some(node);
        }
        // A directory implied by a deeper entry.
        let mut prefix = path.to_vec();
        prefix.push(b'/');
        self.nodes
            .range(prefix.clone()..)
            .next()
            .filter(|(k, _)| k.starts_with(&prefix))
            .map(|_| &Node::Dir)
    }

    fn missing_parent(&self) -> FsError {
        match self.flavour {
            Flavour::Posix => FsError::NotADirectory,
            Flavour::Windows => FsError::Missing,
        }
    }

    /// The path every link but a last one refused is followed through:
    /// the final node and its resolved path.
    fn walk(&self, path: &[u8], follow_last: bool) -> Result<(Vec<u8>, Node), FsError> {
        let mut pending: Vec<Vec<u8>> = path
            .split(|&b| b == b'/')
            .filter(|c| !c.is_empty())
            .map(<[u8]>::to_vec)
            .rev()
            .collect();
        let mut here: Vec<Vec<u8>> = Vec::new();
        let mut hops = 0;
        while let Some(part) = pending.pop() {
            if part == b"." {
                continue;
            }
            if part == b".." {
                here.pop();
                continue;
            }
            here.push(part);
            let joined = here.join(&b'/');
            let last = pending.is_empty();
            match self.node(&joined) {
                None => return Err(FsError::Missing),
                Some(Node::Link(target)) if !last || follow_last => {
                    hops += 1;
                    if hops > LINK_HOPS {
                        return Err(FsError::Other);
                    }
                    here.pop();
                    if target.starts_with(b"/") {
                        here.clear();
                    }
                    pending.extend(
                        target
                            .split(|&b| b == b'/')
                            .filter(|c| !c.is_empty())
                            .map(<[u8]>::to_vec)
                            .rev(),
                    );
                }
                Some(node) => {
                    if last {
                        return Ok((joined, node.clone()));
                    }
                    if !matches!(node, Node::Dir) {
                        return Err(self.missing_parent());
                    }
                }
            }
        }
        Ok((here.join(&b'/'), Node::Dir))
    }
}

impl RefFs for MemFs {
    fn read(&self, path: &[u8], limit: usize, follow: bool) -> Result<Vec<u8>, FsError> {
        if path.contains(&0) {
            return Err(FsError::Other);
        }
        let windows = self.flavour == Flavour::Windows;
        // Windows decodes a bytes path as UTF-8: ValueError, a decline.
        if windows && std::str::from_utf8(path).is_err() {
            return Err(FsError::Other);
        }
        let last_is_link =
            windows && !follow && matches!(self.walk(path, false), Ok((_, Node::Link(_))));
        match self.walk(path, follow || windows)?.1 {
            Node::File(bytes) => Ok(bytes[..bytes.len().min(limit + 1)].to_vec()),
            // A link to a directory on Windows: PermissionError rises.
            Node::Dir if last_is_link => Err(FsError::Other),
            Node::Dir => Err(FsError::IsADirectory),
            Node::Link(_) | Node::Special => Err(FsError::Other),
        }
    }

    fn lstat_is_dir(&self, path: &[u8]) -> Result<bool, FsError> {
        if path.contains(&0)
            || (self.flavour == Flavour::Windows && std::str::from_utf8(path).is_err())
        {
            return Err(FsError::Other);
        }
        Ok(matches!(self.walk(path, false)?.1, Node::Dir))
    }
}
