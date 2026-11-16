//! The file system as the reference's readers of config files ask it, as
//! a trait ([`Fs`]): a non-blocking open that follows links, the kind,
//! identity and size of what was opened (`os.fstat`), a read of at most a
//! given count (`os.read`), `os.access(path, R_OK)` and `os.lstat`'s kind.
//! [`StdFs`] is the machine's; [`MemFs`] is one as data, of either
//! [`Flavour`]. Every error carries the Python exception the reference's
//! call raises ([`PyErr`]), since its texts name it.
//!
//! `githead`'s `RefFs` is not built on this trait: its read takes a
//! `follow` flag (`O_NOFOLLOW`) and answers in one call, and moving it
//! here would change its error mapping; see the README.

use std::collections::BTreeMap;
use std::io::{ErrorKind, Read};

pub use crate::githead::Flavour;

/// The Python exception an OS call raises, by its class name.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PyErr {
    FileNotFound,
    NotADirectory,
    IsADirectory,
    Permission,
    BlockingIO,
    Interrupted,
    /// `OSError` itself: an errno CPython maps to no subclass (ELOOP,
    /// ENXIO, ENAMETOOLONG, EIO).
    Os,
    /// `ValueError`: a path holding a NUL byte (or, on Windows, one that
    /// is not UTF-8).
    Value,
}

impl PyErr {
    /// The class name, as `type(exc).__name__` spells it.
    pub fn name(self) -> &'static str {
        match self {
            PyErr::FileNotFound => "FileNotFoundError",
            PyErr::NotADirectory => "NotADirectoryError",
            PyErr::IsADirectory => "IsADirectoryError",
            PyErr::Permission => "PermissionError",
            PyErr::BlockingIO => "BlockingIOError",
            PyErr::Interrupted => "InterruptedError",
            PyErr::Os => "OSError",
            PyErr::Value => "ValueError",
        }
    }

    /// `path_missing`: the OS's answer that nothing is there (ENOENT or
    /// ENOTDIR).
    pub fn missing(self) -> bool {
        matches!(self, PyErr::FileNotFound | PyErr::NotADirectory)
    }
}

/// The kind of a file, from its mode.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Regular,
    Directory,
    Fifo,
    Socket,
    /// A symbolic link (only `lstat` answers it).
    Link,
    /// A device or anything else.
    Other,
}

/// A file's identity, for telling one file from another reached by
/// another path: device and inode on POSIX, the resolved path in memory.
#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct FileId(pub Vec<u8>);

/// What `os.fstat` answers of an open file.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Stat {
    pub kind: Kind,
    /// None where the OS gives no identity (`st_ino` 0).
    pub identity: Option<FileId>,
    pub size: u64,
}

/// The file system the readers of config files take as data.
pub trait Fs {
    /// An open file.
    type File;
    /// `os.open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_BINARY)`:
    /// follows links, never waits on a FIFO.
    fn open(&self, path: &[u8]) -> Result<Self::File, PyErr>;
    /// `os.fstat` of an open file.
    fn fstat(&self, file: &mut Self::File) -> Result<Stat, PyErr>;
    /// `os.read(fd, max)`: at most `max` bytes, empty at the end.
    fn read(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, PyErr>;
    /// `os.access(path, os.R_OK)`, False where it raises ValueError.
    fn access_read(&self, path: &[u8]) -> bool;
    /// The kind `os.lstat` answers (the link itself, not its target).
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr>;
}

// ---------------------------------------------------------------------------
// The machine's file system
// ---------------------------------------------------------------------------

/// The machine's file system.
#[derive(Clone, Copy, Debug, Default)]
pub struct StdFs;

#[cfg(unix)]
fn native_path(path: &[u8]) -> Result<std::path::PathBuf, PyErr> {
    use std::os::unix::ffi::OsStrExt;
    if path.contains(&0) {
        return Err(PyErr::Value);
    }
    Ok(std::ffi::OsStr::from_bytes(path).into())
}

#[cfg(not(unix))]
fn native_path(path: &[u8]) -> Result<std::path::PathBuf, PyErr> {
    if path.contains(&0) {
        return Err(PyErr::Value);
    }
    // The reference's paths are text; a path here is its UTF-8.
    let text = std::str::from_utf8(path).map_err(|_| PyErr::Value)?;
    Ok(text.into())
}

/// CPython's errno-to-class map, through the kinds Rust names.
fn py_err(error: &std::io::Error) -> PyErr {
    match error.kind() {
        ErrorKind::NotFound => PyErr::FileNotFound,
        ErrorKind::NotADirectory => PyErr::NotADirectory,
        ErrorKind::IsADirectory => PyErr::IsADirectory,
        ErrorKind::PermissionDenied => PyErr::Permission,
        ErrorKind::WouldBlock => PyErr::BlockingIO,
        ErrorKind::Interrupted => PyErr::Interrupted,
        // CPython maps ERROR_INVALID_NAME to ENOENT on Windows.
        ErrorKind::InvalidFilename if cfg!(windows) => PyErr::FileNotFound,
        _ => PyErr::Os,
    }
}

fn kind_of(file_type: std::fs::FileType) -> Kind {
    if file_type.is_symlink() {
        return Kind::Link;
    }
    if file_type.is_dir() {
        return Kind::Directory;
    }
    if file_type.is_file() {
        return Kind::Regular;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::FileTypeExt;
        if file_type.is_fifo() {
            return Kind::Fifo;
        }
        if file_type.is_socket() {
            return Kind::Socket;
        }
    }
    Kind::Other
}

#[cfg(unix)]
fn open_options() -> std::fs::OpenOptions {
    use std::os::unix::fs::OpenOptionsExt;
    let mut options = std::fs::OpenOptions::new();
    options
        .read(true)
        .custom_flags(bettermemory_sys::O_NONBLOCK);
    options
}

#[cfg(not(unix))]
fn open_options() -> std::fs::OpenOptions {
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    options
}

#[cfg(unix)]
fn identity_of(meta: &std::fs::Metadata, _: &std::fs::File) -> Option<FileId> {
    use std::os::unix::fs::MetadataExt;
    if meta.ino() == 0 {
        return None;
    }
    let mut id = meta.dev().to_le_bytes().to_vec();
    id.extend_from_slice(&meta.ino().to_le_bytes());
    Some(FileId(id))
}

/// Windows gives no stable file index to Rust without `unsafe`; a file is
/// named by the path the open resolved, which the next unit's cycle check
/// takes as its identity (the reference takes `st_ino`, the file index).
#[cfg(not(unix))]
fn identity_of(_: &std::fs::Metadata, _: &std::fs::File) -> Option<FileId> {
    None
}

/// An open file of [`StdFs`].
pub struct StdFile {
    file: std::fs::File,
    path: std::path::PathBuf,
}

impl Fs for StdFs {
    type File = StdFile;

    fn open(&self, path: &[u8]) -> Result<StdFile, PyErr> {
        let path = native_path(path)?;
        let file = open_options().open(&path).map_err(|e| py_err(&e))?;
        Ok(StdFile { file, path })
    }

    fn fstat(&self, file: &mut StdFile) -> Result<Stat, PyErr> {
        let meta = file.file.metadata().map_err(|e| py_err(&e))?;
        let mut identity = identity_of(&meta, &file.file);
        if identity.is_none() && cfg!(windows) {
            identity = std::fs::canonicalize(&file.path)
                .ok()
                .map(|p| FileId(p.to_string_lossy().as_bytes().to_vec()));
        }
        Ok(Stat {
            kind: kind_of(meta.file_type()),
            identity,
            size: meta.len(),
        })
    }

    fn read(&self, file: &mut StdFile, max: usize) -> Result<Vec<u8>, PyErr> {
        let mut out = Vec::new();
        (&mut file.file)
            .take(max as u64)
            .read_to_end(&mut out)
            .map_err(|e| py_err(&e))?;
        Ok(out)
    }

    fn access_read(&self, path: &[u8]) -> bool {
        let Ok(path) = native_path(path) else {
            return false;
        };
        // Windows: CPython's `os.access` is `GetFileAttributesW`, which
        // describes a link itself (a dangling one answers), and R_OK holds
        // wherever the attributes are found (the read-only attribute
        // bears on W_OK only).
        if cfg!(windows) {
            return std::fs::symlink_metadata(&path).is_ok();
        }
        // POSIX `access(R_OK)`: an open for reading that is closed at once
        // answers it for a file, a directory and a FIFO (not for a socket,
        // which the readers never ask about), without waiting.
        open_options().open(&path).is_ok()
    }

    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        let path = native_path(path)?;
        let meta = std::fs::symlink_metadata(path).map_err(|e| py_err(&e))?;
        Ok(kind_of(meta.file_type()))
    }
}

// ---------------------------------------------------------------------------
// A file system as data
// ---------------------------------------------------------------------------

/// One entry of a [`MemFs`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Node {
    File(Vec<u8>),
    Dir,
    /// A symbolic link; a target starting with a separator is absolute,
    /// and one ending with a separator names a directory.
    Link(Vec<u8>),
    /// A hard link: another name of the file at this (normalized) path,
    /// sharing its bytes, its mode and its identity.
    Hard(Vec<u8>),
    Fifo,
    Socket,
    /// A character device that reads empty (the null device).
    Device,
    /// A character device that reads without end (the zero device).
    Endless,
}

/// The owner's permission bits, as the OS checks them for the owner.
const READ: u32 = 0o400;
const SEARCH: u32 = 0o100;

/// The hops macOS follows before ELOOP (Linux follows 40); the default,
/// so a fixture made on either agrees wherever a chain is shorter.
pub const LINK_HOPS: usize = 32;

/// A file system as data: paths of components to nodes. A component is
/// split at `/` (and at `\` too under [`Flavour::Windows`]); a leading
/// separator changes nothing, so `<scratch>/a` and `/<scratch>/a` are one
/// path. A parent directory need not be listed. `modes` holds the owner's
/// permission bits of a path where they are not the default (0o644 for a
/// file, 0o755 for a directory); they are checked under
/// [`Flavour::Posix`] only. `link_hops` is how many symbolic links a path
/// may pass through before ELOOP.
#[derive(Clone, Debug)]
pub struct MemFs {
    pub flavour: Flavour,
    pub nodes: BTreeMap<Vec<u8>, Node>,
    pub modes: BTreeMap<Vec<u8>, u32>,
    pub link_hops: usize,
}

/// An open file of [`MemFs`].
pub struct MemFile {
    node: Node,
    at: usize,
    identity: Vec<u8>,
}

/// A path's components.
type Parts = Vec<Vec<u8>>;

impl MemFs {
    pub fn new(flavour: Flavour) -> MemFs {
        MemFs {
            flavour,
            nodes: BTreeMap::new(),
            modes: BTreeMap::new(),
            link_hops: LINK_HOPS,
        }
    }

    fn key(&self, path: &[u8]) -> Vec<u8> {
        self.split(path).join(&b'/')
    }

    pub fn insert(&mut self, path: &[u8], node: Node) {
        let key = self.key(path);
        let node = match node {
            Node::Hard(to) => Node::Hard(self.key(&to)),
            other => other,
        };
        self.nodes.insert(key, node);
    }

    /// Set the owner's permission bits of `path` (0o000 to 0o777).
    pub fn set_mode(&mut self, path: &[u8], mode: u32) {
        let key = self.key(path);
        self.modes.insert(key, mode);
    }

    /// Mode 000.
    pub fn lock(&mut self, path: &[u8]) {
        self.set_mode(path, 0);
    }

    fn separator(&self, b: u8) -> bool {
        b == b'/' || (b == b'\\' && self.flavour == Flavour::Windows)
    }

    fn split(&self, path: &[u8]) -> Parts {
        path.split(|&b| self.separator(b))
            .filter(|c| !c.is_empty())
            .map(<[u8]>::to_vec)
            .collect()
    }

    fn trailing_separator(&self, path: &[u8]) -> bool {
        path.last().is_some_and(|&b| self.separator(b))
    }

    /// The node at a resolved key, a hard link taken to its file.
    fn node(&self, key: &[u8]) -> Option<(Vec<u8>, Node)> {
        if key.is_empty() {
            return Some((Vec::new(), Node::Dir));
        }
        if let Some(node) = self.nodes.get(key) {
            if let Node::Hard(to) = node {
                // A directory, a missing node or another hard link reads
                // as nothing there. Windows names a file by its canonical
                // path (`StdFs`), so there each name is its own identity.
                let identity = match self.flavour {
                    Flavour::Posix => to.clone(),
                    Flavour::Windows => key.to_vec(),
                };
                return match self.nodes.get(to) {
                    Some(target) if !matches!(target, Node::Hard(_) | Node::Dir) => {
                        Some((identity, target.clone()))
                    }
                    _ => None,
                };
            }
            return Some((key.to_vec(), node.clone()));
        }
        let mut prefix = key.to_vec();
        prefix.push(b'/');
        self.nodes
            .range(prefix.clone()..)
            .next()
            .filter(|(k, _)| k.starts_with(&prefix))
            .map(|_| (key.to_vec(), Node::Dir))
    }

    /// The owner's bits of the file whose identity is `id`.
    fn mode(&self, id: &[u8], node: &Node) -> u32 {
        if self.flavour == Flavour::Windows {
            return 0o777;
        }
        self.modes.get(id).copied().unwrap_or(match node {
            Node::Dir => 0o755,
            _ => 0o644,
        })
    }

    fn not_a_directory(&self) -> PyErr {
        match self.flavour {
            Flavour::Posix => PyErr::NotADirectory,
            Flavour::Windows => PyErr::FileNotFound,
        }
    }

    /// The node `path` names, with its identity: each component looked up
    /// in the directory reached so far (search permission asked of it),
    /// `..` included, links followed as met (a last one only where
    /// `follow_last`).
    fn walk(&self, path: &[u8], follow_last: bool) -> Result<(Vec<u8>, Node), PyErr> {
        if path.contains(&0) {
            return Err(PyErr::Value);
        }
        if self.flavour == Flavour::Windows && std::str::from_utf8(path).is_err() {
            return Err(PyErr::Value);
        }
        // An empty path names nothing (ENOENT; `_wopen("")` too).
        if path.is_empty() {
            return Err(PyErr::FileNotFound);
        }
        // A trailing separator asks only that the last component be a
        // directory, a final link followed; no `.` is looked up, so it
        // needs no search permission.
        let mut want_dir = self.trailing_separator(path);
        let mut pending = self.split(path);
        pending.reverse();
        // The directories reached, as resolved keys.
        let mut here: Vec<Vec<u8>> = Vec::new();
        let mut hops = 0;
        let mut found: (Vec<u8>, Node) = (Vec::new(), Node::Dir);
        while let Some(part) = pending.pop() {
            let last = pending.is_empty();
            // Looking a name up in a directory needs search permission.
            let dir = here.join(&b'/');
            if self.mode(&dir, &Node::Dir) & SEARCH == 0 {
                return Err(PyErr::Permission);
            }
            if part == b"." {
                found = (dir, Node::Dir);
                continue;
            }
            if part == b".." {
                here.pop();
                found = (here.join(&b'/'), Node::Dir);
                continue;
            }
            let mut key = dir.clone();
            if !key.is_empty() {
                key.push(b'/');
            }
            key.extend_from_slice(&part);
            let Some((id, node)) = self.node(&key) else {
                return Err(PyErr::FileNotFound);
            };
            match node {
                Node::Link(target) if !last || follow_last || want_dir => {
                    hops += 1;
                    if hops > self.link_hops {
                        return Err(PyErr::Os);
                    }
                    // A link to an empty target names nothing (macOS;
                    // Linux cannot make one).
                    if target.is_empty() {
                        return Err(PyErr::FileNotFound);
                    }
                    if target.first().is_some_and(|&b| self.separator(b)) {
                        here.clear();
                    }
                    if last {
                        want_dir |= self.trailing_separator(&target);
                    }
                    let mut more = self.split(&target);
                    if more.is_empty() {
                        // A link to the root.
                        found = (Vec::new(), Node::Dir);
                    }
                    more.reverse();
                    pending.extend(more);
                }
                Node::Dir => {
                    here.push(part);
                    found = (id, Node::Dir);
                }
                other => {
                    if !last {
                        return Err(self.not_a_directory());
                    }
                    found = (id, other);
                }
            }
        }
        if want_dir && found.1 != Node::Dir {
            return Err(self.not_a_directory());
        }
        Ok(found)
    }
}

fn kind_of_node(node: &Node) -> Kind {
    match node {
        Node::File(_) | Node::Hard(_) => Kind::Regular,
        Node::Dir => Kind::Directory,
        Node::Link(_) => Kind::Link,
        Node::Fifo => Kind::Fifo,
        Node::Socket => Kind::Socket,
        Node::Device | Node::Endless => Kind::Other,
    }
}

impl Fs for MemFs {
    type File = MemFile;

    fn open(&self, path: &[u8]) -> Result<MemFile, PyErr> {
        if self.flavour == Flavour::Windows && self.trailing_separator(path) {
            // `_wopen` of a path ending in a separator finds nothing.
            self.walk(path, true)?;
            return Err(PyErr::FileNotFound);
        }
        let (id, node) = self.walk(path, true)?;
        if self.flavour == Flavour::Windows {
            // `_wopen` of a directory is refused (EACCES).
            if node == Node::Dir {
                return Err(PyErr::Permission);
            }
        } else {
            if self.mode(&id, &node) & READ == 0 {
                return Err(PyErr::Permission);
            }
            // A socket cannot be opened (ENXIO on Linux, EOPNOTSUPP on
            // macOS: OSError either way).
            if node == Node::Socket {
                return Err(PyErr::Os);
            }
        }
        Ok(MemFile {
            node,
            at: 0,
            identity: id,
        })
    }

    fn fstat(&self, file: &mut MemFile) -> Result<Stat, PyErr> {
        let size = match &file.node {
            Node::File(bytes) => bytes.len() as u64,
            _ => 0,
        };
        Ok(Stat {
            kind: kind_of_node(&file.node),
            identity: Some(FileId(file.identity.clone())),
            size,
        })
    }

    fn read(&self, file: &mut MemFile, max: usize) -> Result<Vec<u8>, PyErr> {
        match &file.node {
            Node::File(bytes) => {
                let start = file.at.min(bytes.len());
                let end = start.saturating_add(max).min(bytes.len());
                file.at = end;
                Ok(bytes[start..end].to_vec())
            }
            Node::Dir => Err(PyErr::IsADirectory),
            Node::Fifo => Err(PyErr::BlockingIO),
            Node::Socket | Node::Link(_) | Node::Hard(_) => Err(PyErr::Os),
            Node::Device => Ok(Vec::new()),
            Node::Endless => Ok(vec![0; max]),
        }
    }

    fn access_read(&self, path: &[u8]) -> bool {
        if self.flavour == Flavour::Windows {
            // `GetFileAttributesW`: the link itself, found or not.
            return self.walk(path, false).is_ok();
        }
        match self.walk(path, true) {
            Ok((id, node)) => self.mode(&id, &node) & READ != 0,
            Err(_) => false,
        }
    }

    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        Ok(kind_of_node(&self.walk(path, false)?.1))
    }
}
