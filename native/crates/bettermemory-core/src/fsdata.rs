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

pub mod realpath;

use std::collections::BTreeMap;
use std::io::{ErrorKind, Read, Seek, SeekFrom};

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
    Timeout,
    FileExists,
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
            PyErr::Timeout => "TimeoutError",
            PyErr::FileExists => "FileExistsError",
            PyErr::Os => "OSError",
            PyErr::Value => "ValueError",
        }
    }

    /// `path_missing`: the OS's answer that nothing is there (ENOENT or
    /// ENOTDIR).
    pub fn missing(self) -> bool {
        matches!(self, PyErr::FileNotFound | PyErr::NotADirectory)
    }

    /// The class CPython raises for an errno name (its errno-to-subclass
    /// table, for the names `bettermemory_sys::errno_name` answers); a
    /// name it maps to no subclass is `OSError`.
    pub fn of_errno(name: &str) -> PyErr {
        match name {
            "ENOENT" => PyErr::FileNotFound,
            "ENOTDIR" => PyErr::NotADirectory,
            "EISDIR" => PyErr::IsADirectory,
            "EACCES" | "EPERM" => PyErr::Permission,
            "EEXIST" => PyErr::FileExists,
            "ETIMEDOUT" => PyErr::Timeout,
            "EINTR" => PyErr::Interrupted,
            "EAGAIN" => PyErr::BlockingIO,
            _ => PyErr::Os,
        }
    }
}

/// An error as the reference's exception carries it: its class and, for
/// an OSError, the errno's name (`errno.errorcode`), never its number,
/// which differs between platforms. A ValueError has no errno.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OsError {
    pub class: PyErr,
    pub errno: Option<&'static str>,
    /// The raw Windows error code (`OSError.winerror`), which the
    /// reference reads before the class; None on every other platform.
    pub winerror: Option<u32>,
}

impl OsError {
    pub fn new(class: PyErr, errno: &'static str) -> OsError {
        OsError {
            class,
            errno: Some(errno),
            winerror: None,
        }
    }

    /// `ValueError` (a NUL in the path).
    pub fn value() -> OsError {
        OsError {
            class: PyErr::Value,
            errno: None,
            winerror: None,
        }
    }

    /// An error known by its class alone: no errno's name, no Windows
    /// code. A caller that must name the errno declines on it.
    pub fn of_class(class: PyErr) -> OsError {
        OsError {
            class,
            errno: None,
            winerror: None,
        }
    }
}

impl From<OsError> for PyErr {
    fn from(error: OsError) -> PyErr {
        error.class
    }
}

/// Why a call on an open file that not every file system offers
/// ([`Fs::rewind`], [`Fs::kernel_path`]) gave no value.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Refused {
    /// The reference's call raises: the exception's class.
    Raised(PyErr),
    /// This file system does not offer the call; a caller declines.
    NotOffered,
}

/// What [`Fs::basic_info`] answers of a path.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BasicInfo {
    /// The call is not made here (the default).
    Declined,
    /// A call failed: no handle, or the query refused.
    Failed,
    /// FILE_BASIC_INFO's creation, last access, last write and change
    /// times, as FILETIMEs (`c_int64`).
    Times([i64; 4]),
}

/// An [`OsError`] with its errno as the number the OS gave
/// (`OSError.errno`): every number, where [`OsError::errno`] names only
/// those the readers tell apart. No fixture records a number, which
/// differs between platforms; `os.strerror(exc.errno)` and `str(exc)`
/// read it. `code` is None where the number is not known: an error made
/// in memory (named alone), a ValueError, and Windows, whose code is the
/// error's `winerror`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Numbered {
    pub error: OsError,
    pub code: Option<i32>,
}

impl From<OsError> for Numbered {
    fn from(error: OsError) -> Numbered {
        Numbered { error, code: None }
    }
}

impl Numbered {
    /// The error [`StdFs`] raises for an `io::Error`: its class and name
    /// as for every reader, and on POSIX its raw code, the errno.
    pub fn of_io(error: &std::io::Error) -> Numbered {
        Numbered {
            error: os_error(error),
            code: error.raw_os_error().filter(|_| cfg!(unix)),
        }
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

/// What `os.stat` (links followed) or `os.lstat` answers: the fields
/// `gitconfig.file_stamp` reads, and those `gitfile_rule` reads
/// (`samestat`'s device and inode, `_placeholder`'s flags and attributes,
/// `_is_link`'s reparse tag). `ino`, `dev`, `mode`, `ctime_ns` and
/// `reparse_tag` are None where they are not ported (Windows: `st_ino`
/// and `st_dev` are the file index and the volume serial, which the
/// standard library gives only on nightly; CPython makes `st_mode` and
/// `st_ctime` up from attributes; the reparse tag needs a call std does
/// not make); a caller declines there.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StatResult {
    pub kind: Kind,
    /// `st_mode`: the type bits and the permission bits.
    pub mode: Option<u32>,
    pub ino: Option<u64>,
    /// `st_dev`.
    pub dev: Option<u64>,
    pub size: u64,
    pub mtime_ns: i128,
    pub ctime_ns: Option<i128>,
    /// `getattr(st, "st_flags", 0)`: the BSD file flags on macOS, 0 where
    /// `os.stat_result` has no `st_flags` (Linux, Windows).
    pub flags: u32,
    /// `getattr(st, "st_file_attributes", 0)`: Windows' attributes, 0
    /// elsewhere.
    pub file_attributes: u32,
    /// `getattr(st, "st_reparse_tag", 0)`: 0 off Windows; None on Windows,
    /// where it is not ported.
    pub reparse_tag: Option<u32>,
}

/// `_SF_DATALESS` (`gitfile_rule.py` 75): a file provider holds the data.
pub const SF_DATALESS: u32 = 0x4000_0000;
/// `_WINDOWS_RECALL` (76): `FILE_ATTRIBUTE_RECALL_ON_OPEN` and
/// `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS`.
pub const WINDOWS_RECALL: u32 = 0x0004_0000 | 0x0040_0000;
/// `_JUNCTION_TAG` (940): `IO_REPARSE_TAG_MOUNT_POINT`.
pub const JUNCTION_TAG: u32 = 0xA000_0003;

impl StatResult {
    /// `_placeholder(st)` (1062-1070): the dataless flag or a recall
    /// attribute.
    pub fn placeholder(&self) -> bool {
        self.flags & SF_DATALESS != 0 || self.file_attributes & WINDOWS_RECALL != 0
    }

    /// `_is_link(st)` (943-948) of an `lstat` result: a symbolic link, or
    /// a junction by its reparse tag. None where a non-link's tag is not
    /// ported (Windows), so a junction cannot be told.
    pub fn is_link(&self) -> Option<bool> {
        if self.kind == Kind::Link {
            return Some(true);
        }
        self.reparse_tag.map(|tag| tag == JUNCTION_TAG)
    }
}

/// `os.path.samestat(a, b)`: the same device and inode. None where either
/// is not ported (Windows), never a guess.
pub fn samestat(a: &StatResult, b: &StatResult) -> Option<bool> {
    match (a.dev, a.ino, b.dev, b.ino) {
        (Some(ad), Some(ai), Some(bd), Some(bi)) => Some(ad == bd && ai == bi),
        _ => None,
    }
}

/// `os.path.samefile(a, b)`: both stated (links followed), the first
/// error raised as it is; `Ok(None)` where `samestat` is not ported.
pub fn samefile<F: Fs + ?Sized>(fs: &F, a: &[u8], b: &[u8]) -> Result<Option<bool>, OsError> {
    let first = fs.stat(a)?;
    let second = fs.stat(b)?;
    Ok(samestat(&first, &second))
}

/// The type bits of `st_mode` for a kind (`stat.S_IF*`).
pub fn type_bits(kind: Kind) -> u32 {
    match kind {
        Kind::Regular => 0o100000,
        Kind::Directory => 0o040000,
        Kind::Link => 0o120000,
        Kind::Fifo => 0o010000,
        Kind::Socket => 0o140000,
        Kind::Other => 0o020000,
    }
}

/// The modes `os.access` asks, any together; none is `F_OK`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Access {
    pub read: bool,
    pub write: bool,
    pub execute: bool,
}

impl Access {
    /// `R_OK`.
    pub const READ: Access = Access {
        read: true,
        write: false,
        execute: false,
    };
    /// `W_OK`.
    pub const WRITE: Access = Access {
        read: false,
        write: true,
        execute: false,
    };
    /// `X_OK`.
    pub const EXECUTE: Access = Access {
        read: false,
        write: false,
        execute: true,
    };

    /// The owner's permission bits it asks: `0o400`, `0o200` and `0o100`,
    /// ORed.
    fn bits(self) -> u32 {
        let mut bits = 0;
        if self.read {
            bits |= READ;
        }
        if self.write {
            bits |= WRITE;
        }
        if self.execute {
            bits |= SEARCH;
        }
        bits
    }
}

/// The file system the readers of config files take as data.
pub trait Fs {
    /// An open file.
    type File;
    /// `os.open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_BINARY)`:
    /// follows links, never waits on a FIFO.
    fn open(&self, path: &[u8]) -> Result<Self::File, PyErr>;
    /// `os.open(path, O_RDONLY | O_CLOEXEC)`: [`Fs::open`] without
    /// `O_NONBLOCK`, so a FIFO waits for a writer. By default
    /// [`Fs::open`]; `StdFs` drops the flag.
    fn open_waiting(&self, path: &[u8]) -> Result<Self::File, PyErr> {
        self.open(path)
    }
    /// [`Fs::open`] with the error's errno. By default the class alone;
    /// `StdFs` and `MemFs` carry the errno.
    fn open_result(&self, path: &[u8]) -> Result<Self::File, OsError> {
        self.open(path).map_err(|class| OsError {
            class,
            errno: None,
            winerror: None,
        })
    }
    /// `os.fstat` of an open file.
    fn fstat(&self, file: &mut Self::File) -> Result<Stat, PyErr>;
    /// `os.read(fd, max)`: at most `max` bytes, empty at the end.
    fn read(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, PyErr>;
    /// `os.access(path, os.R_OK)`, False where it raises ValueError.
    fn access_read(&self, path: &[u8]) -> bool {
        self.access(path, Access::READ).unwrap_or(false)
    }
    /// `os.access(path, mode)` with its defaults (`effective_ids=False`,
    /// `follow_symlinks=True`): POSIX `access(2)`, the real ids, links
    /// followed, search permission asked of each folder on the way. False
    /// for any OSError, as CPython answers; ValueError (a NUL in the path,
    /// or on Windows one that is not UTF-8) is `Err(PyErr::Value)`, which
    /// `gitfile_rule._git_found` lets through and `gitconfig._accessible`
    /// takes as False.
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr>;
    /// `os.open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_BINARY |
    /// O_NOFOLLOW)` (`gitfile_rule._read` with `follow=False`): a final
    /// symbolic link, dangling or not, is ELOOP (OSError) on macOS and
    /// Linux alike; links in the middle are followed; a FIFO opens without
    /// waiting. Windows' `os` has no `O_NOFOLLOW` (the reference's
    /// `getattr` gives 0), so there it is [`Fs::open`]: links followed.
    fn open_no_follow(&self, path: &[u8]) -> Result<Self::File, OsError>;
    /// The kind `os.lstat` answers (the link itself, not its target).
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr>;
    /// `os.readlink(path)`: a symbolic link's target as stored. A path
    /// that is no link raises OSError (EINVAL).
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr>;
    /// `os.readlink(path)` with the error's errno (EINVAL for a path that
    /// is no link). By default the class alone, from [`Fs::readlink`];
    /// `StdFs` and `MemFs` carry the errno.
    fn readlink_result(&self, path: &[u8]) -> Result<Vec<u8>, OsError> {
        self.readlink(path).map_err(|class| OsError {
            class,
            errno: None,
            winerror: None,
        })
    }
    /// `os.stat(path)`: links followed.
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError>;
    /// [`Fs::stat`] with the errno's number ([`Numbered`]), for a reader
    /// that spells any errno's text. By default none: the error as
    /// [`Fs::stat`] names it; `StdFs` carries the number on POSIX.
    fn stat_numbered(&self, path: &[u8]) -> Result<StatResult, Numbered> {
        self.stat(path).map_err(Numbered::from)
    }
    /// `os.lstat(path)` in full: a final link is described itself. The
    /// kind alone is [`Fs::lstat`].
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError>;
    /// `os.listdir(path)`: the names in the folder (links followed), `.`
    /// and `..` left out, in the OS's order (a caller sorts).
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError>;
    /// `os.lseek(fd, 0, os.SEEK_SET)`: the next [`Fs::read`] starts at the
    /// file's first byte. A FIFO or a socket raises OSError (ESPIPE); a
    /// folder and a device seek (macOS and Linux alike). By default not
    /// offered; `StdFs` and `MemFs` answer.
    fn rewind(&self, _file: &mut Self::File) -> Result<(), Refused> {
        Err(Refused::NotOffered)
    }
    /// `fh.seek(offset)` of an unbuffered file (`lseek(fd, offset,
    /// SEEK_SET)`): the next read starts at `offset`, past the end too. A
    /// FIFO or a socket raises OSError (ESPIPE), as [`Fs::rewind`]. By
    /// default not offered; `StdFs` and `MemFs` answer.
    fn seek(&self, _file: &mut Self::File, _offset: u64) -> Result<(), Refused> {
        Err(Refused::NotOffered)
    }
    /// `fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))` up to its first
    /// NUL: the path the kernel holds for the open file, each component
    /// in the case and Unicode form its folder stores. A path of 1,024
    /// bytes or more raises OSError (ENOSPC). macOS alone has the call:
    /// by default, and in `StdFs` on every other platform, not offered.
    fn kernel_path(&self, _file: &mut Self::File) -> Result<Vec<u8>, Refused> {
        Err(Refused::NotOffered)
    }
    /// `os.open(path, O_RDONLY | O_CLOEXEC | O_BINARY)`: a blocking open,
    /// links followed, which waits on a FIFO until a writer opens it
    /// (`checkout_token._open_plain` makes it only after a stat). Declined
    /// by default: an OSError with no errno.
    fn open_blocking(&self, _path: &[u8]) -> Result<Self::File, OsError> {
        Err(OsError::of_class(PyErr::Os))
    }
    /// [`Fs::fstat`] with the error's errno. By default the class alone.
    fn fstat_result(&self, file: &mut Self::File) -> Result<Stat, OsError> {
        self.fstat(file).map_err(OsError::of_class)
    }
    /// One `os.read(fd, max)`: what a single read answers, at most `max`
    /// bytes and empty at the end, with the error's errno. By default
    /// [`Fs::read`], which may read more than once, with the class alone.
    fn read_once(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, OsError> {
        self.read(file, max).map_err(OsError::of_class)
    }
    /// `os.close(fd)`, which can fail (EINTR, EIO). By default the file is
    /// dropped and nothing is answered.
    fn close(&self, file: Self::File) -> Result<(), OsError> {
        drop(file);
        Ok(())
    }
    /// `os.O_NONBLOCK` where the platform has `fcntl`; None, the default,
    /// where it has none (`sys.platform == "win32"`).
    fn nonblock_flag(&self) -> Option<i32> {
        None
    }
    /// `fcntl.fcntl(fd, F_GETFL)`: an open file's status flags. Declined
    /// by default.
    fn status_flags(&self, _file: &mut Self::File) -> Result<i32, OsError> {
        Err(OsError::of_class(PyErr::Os))
    }
    /// `fcntl.fcntl(fd, F_SETFL, flags)`. Declined by default.
    fn set_status_flags(&self, _file: &mut Self::File, _flags: i32) -> Result<(), OsError> {
        Err(OsError::of_class(PyErr::Os))
    }
    /// `exc.strerror` of an error this file system raised: the text the
    /// platform's C library has for its errno. None, the default, where
    /// it is not known: a caller that words the error declines.
    fn strerror(&self, _error: &OsError) -> Option<String> {
        None
    }
    /// FILE_BASIC_INFO of `path` (`location_stamps.basic_info_ns`): its
    /// handle opened for attributes alone, then queried. Windows only;
    /// declined by default, as `StdFs` does (the calls need `unsafe`).
    fn basic_info(&self, _path: &[u8]) -> BasicInfo {
        BasicInfo::Declined
    }

    /// Whether the folder `path` (links followed) holds an entry named
    /// other than `name`: `os.scandir(path)` under `all(entry.name ==
    /// name for entry in entries)`, which reads the entries only up to the
    /// first such one. An error opening the folder, or reading an entry
    /// before that one, is the OSError `scandir` raises. By default the
    /// folder is listed whole ([`Fs::listdir`]); `StdFs` and `MemFs` stop
    /// at the first other name, so their cost does not grow with the
    /// folder.
    fn holds_other_than(&self, path: &[u8], name: &[u8]) -> Result<bool, OsError> {
        Ok(self
            .listdir(path)?
            .iter()
            .any(|other| other.as_slice() != name))
    }
    /// What `location._birth` reads of `path`'s birth time
    /// ([`BirthFacts`]). Nothing by default, as a file system that
    /// reports no birth time.
    fn birth_facts(&self, _path: &[u8]) -> BirthFacts {
        BirthFacts::default()
    }
}

/// A number attribute of `os.stat_result` as `getattr` gives it.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub enum BirthAttr {
    /// The attribute is missing (or None).
    #[default]
    Absent,
    Int(i128),
    Float(f64),
    /// A `bool`, which `isinstance(_, int)` takes.
    Bool(bool),
}

/// The birth time facts `location._birth` reads: `st_birthtime_ns` and
/// `st_birthtime` of the stat result, `st_ctime_ns` (Windows before
/// 3.12), and what `_statx_birth` answers (Linux).
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct BirthFacts {
    pub birthtime_ns: BirthAttr,
    pub birthtime: BirthAttr,
    pub ctime_ns: Option<i128>,
    pub statx: Option<i128>,
}

/// `SystemTime` as nanoseconds from the epoch, negative before it.
fn epoch_ns(time: std::time::SystemTime) -> i128 {
    match time.duration_since(std::time::UNIX_EPOCH) {
        Ok(d) => d.as_nanos() as i128,
        Err(e) => -(e.duration().as_nanos() as i128),
    }
}

/// CPython's `st_birthtime` on macOS and the BSDs from nanoseconds since
/// the epoch: `bsec + 1e-9 * bnsec` with `bsec` the seconds cast to
/// `unsigned long` (a time before the epoch wraps, as there) and `bnsec`
/// the nanoseconds of the timespec (0 to 999,999,999).
pub fn birthtime_float(ns: i128) -> f64 {
    let (sec, nsec) = (ns.div_euclid(1_000_000_000), ns.rem_euclid(1_000_000_000));
    (sec as i64 as u64) as f64 + 1e-9 * (nsec as u64) as f64
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
        ErrorKind::TimedOut => PyErr::Timeout,
        ErrorKind::AlreadyExists => PyErr::FileExists,
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

/// The open of [`Fs::open_waiting`]: no `O_NONBLOCK`.
fn waiting_options() -> std::fs::OpenOptions {
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    options
}

#[cfg(not(unix))]
fn open_options() -> std::fs::OpenOptions {
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    options
}

/// The open of [`Fs::open_no_follow`]: `O_NOFOLLOW` beside `O_NONBLOCK`.
/// The standard library's `OpenOptions` always sets `O_CLOEXEC`, so no
/// test can catch its loss.
#[cfg(unix)]
fn no_follow_options() -> std::fs::OpenOptions {
    use std::os::unix::fs::OpenOptionsExt;
    let mut options = std::fs::OpenOptions::new();
    options
        .read(true)
        .custom_flags(bettermemory_sys::O_NONBLOCK | bettermemory_sys::O_NOFOLLOW);
    options
}

/// Windows: the reference's `getattr(os, "O_NOFOLLOW", 0)` is 0, so the
/// open follows links, as [`Fs::open`] does.
#[cfg(not(unix))]
fn no_follow_options() -> std::fs::OpenOptions {
    open_options()
}

/// POSIX `access(2)` through `bettermemory-sys` (the path holds no NUL:
/// `native_path` refused one).
#[cfg(unix)]
fn access_of(path: &std::path::Path, mode: Access) -> bool {
    use std::os::unix::ffi::OsStrExt;
    let Ok(c) = std::ffi::CString::new(path.as_os_str().as_bytes()) else {
        return false;
    };
    let mode = bettermemory_sys::AccessMode {
        read: mode.read,
        write: mode.write,
        execute: mode.execute,
    };
    bettermemory_sys::access(&c, mode).unwrap_or(false)
}

/// Windows: CPython's `os.access` is `GetFileAttributesW` (read, not run
/// here): False where no attributes are found; W_OK fails on a read-only
/// file that is not a directory; R_OK and X_OK hold wherever the
/// attributes are found. The attributes are the link's own (a dangling
/// link answers), as `symlink_metadata` reads them.
#[cfg(windows)]
fn access_of(path: &std::path::Path, mode: Access) -> bool {
    use std::os::windows::fs::MetadataExt;
    const FILE_ATTRIBUTE_READONLY: u32 = 0x1;
    const FILE_ATTRIBUTE_DIRECTORY: u32 = 0x10;
    let Ok(meta) = std::fs::symlink_metadata(path) else {
        return false;
    };
    let attributes = meta.file_attributes();
    !mode.write
        || attributes & FILE_ATTRIBUTE_READONLY == 0
        || attributes & FILE_ATTRIBUTE_DIRECTORY != 0
}

/// Neither POSIX nor Windows: not ported.
#[cfg(not(any(unix, windows)))]
fn access_of(_: &std::path::Path, _: Access) -> bool {
    false
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

    fn open_waiting(&self, path: &[u8]) -> Result<StdFile, PyErr> {
        let path = native_path(path)?;
        let file = waiting_options().open(&path).map_err(|e| py_err(&e))?;
        Ok(StdFile { file, path })
    }

    fn open_result(&self, path: &[u8]) -> Result<StdFile, OsError> {
        let path = native_path(path).map_err(|_| OsError::value())?;
        let file = open_options().open(&path).map_err(|e| os_error(&e))?;
        Ok(StdFile { file, path })
    }

    fn fstat(&self, file: &mut StdFile) -> Result<Stat, PyErr> {
        fstat_of(file).map_err(|e| py_err(&e))
    }

    fn read(&self, file: &mut StdFile, max: usize) -> Result<Vec<u8>, PyErr> {
        let mut out = Vec::new();
        (&mut file.file)
            .take(max as u64)
            .read_to_end(&mut out)
            .map_err(|e| py_err(&e))?;
        Ok(out)
    }

    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        let path = native_path(path)?;
        Ok(access_of(&path, mode))
    }

    fn open_no_follow(&self, path: &[u8]) -> Result<StdFile, OsError> {
        let path = native_path(path).map_err(|_| OsError::value())?;
        let file = no_follow_options().open(&path).map_err(|e| os_error(&e))?;
        Ok(StdFile { file, path })
    }

    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        let path = native_path(path)?;
        let meta = std::fs::symlink_metadata(path).map_err(|e| py_err(&e))?;
        Ok(kind_of(meta.file_type()))
    }

    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        let path = native_path(path)?;
        let target = std::fs::read_link(path).map_err(|e| py_err(&e))?;
        link_bytes(target)
    }

    fn readlink_result(&self, path: &[u8]) -> Result<Vec<u8>, OsError> {
        let path = native_path(path).map_err(|_| OsError::value())?;
        let target = std::fs::read_link(path).map_err(|e| os_error(&e))?;
        link_bytes(target).map_err(|class| OsError {
            class,
            errno: None,
            winerror: None,
        })
    }

    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.stat_numbered(path).map_err(|raised| raised.error)
    }

    fn stat_numbered(&self, path: &[u8]) -> Result<StatResult, Numbered> {
        let path = native_path(path).map_err(|_| Numbered::from(OsError::value()))?;
        let meta = std::fs::metadata(path).map_err(|e| Numbered::of_io(&e))?;
        Ok(stat_of(&meta))
    }

    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        let path = native_path(path).map_err(|_| OsError::value())?;
        let meta = std::fs::symlink_metadata(path).map_err(|e| os_error(&e))?;
        Ok(stat_of(&meta))
    }

    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        let path = native_path(path).map_err(|_| OsError::value())?;
        let mut names = Vec::new();
        for entry in std::fs::read_dir(path).map_err(|e| os_error(&e))? {
            let entry = entry.map_err(|e| os_error(&e))?;
            names.push(name_bytes(entry.file_name())?);
        }
        Ok(names)
    }

    fn holds_other_than(&self, path: &[u8], name: &[u8]) -> Result<bool, OsError> {
        let path = native_path(path).map_err(|_| OsError::value())?;
        for entry in std::fs::read_dir(path).map_err(|e| os_error(&e))? {
            let entry = entry.map_err(|e| os_error(&e))?;
            // A name with no spelling here (off unix, one that is not
            // Unicode) is another name all the same.
            if name_bytes(entry.file_name()).ok().as_deref() != Some(name) {
                return Ok(true);
            }
        }
        Ok(false)
    }

    fn rewind(&self, file: &mut StdFile) -> Result<(), Refused> {
        (&file.file)
            .seek(SeekFrom::Start(0))
            .map(|_| ())
            .map_err(|e| Refused::Raised(py_err(&e)))
    }

    fn seek(&self, file: &mut StdFile, offset: u64) -> Result<(), Refused> {
        (&file.file)
            .seek(SeekFrom::Start(offset))
            .map(|_| ())
            .map_err(|e| Refused::Raised(py_err(&e)))
    }

    fn kernel_path(&self, file: &mut StdFile) -> Result<Vec<u8>, Refused> {
        kernel_path_of(&file.file)
    }

    /// The standard library's read-only open: `O_RDONLY | O_CLOEXEC` on
    /// POSIX, tried again on EINTR as `os.open` is.
    fn open_blocking(&self, path: &[u8]) -> Result<StdFile, OsError> {
        let path = native_path(path).map_err(|_| OsError::value())?;
        let mut options = std::fs::OpenOptions::new();
        options.read(true);
        let file = options.open(&path).map_err(|e| os_error(&e))?;
        Ok(StdFile { file, path })
    }

    fn fstat_result(&self, file: &mut StdFile) -> Result<Stat, OsError> {
        fstat_of(file).map_err(|e| os_error(&e))
    }

    /// One `read(2)`, tried again on EINTR as `os.read` is. The buffer is
    /// at most [`READ_ONCE_CAP`] bytes, so a hostile count allocates no
    /// more.
    fn read_once(&self, file: &mut StdFile, max: usize) -> Result<Vec<u8>, OsError> {
        let mut out = vec![0; max.min(READ_ONCE_CAP)];
        loop {
            match file.file.read(&mut out) {
                Ok(n) => {
                    out.truncate(n);
                    return Ok(out);
                }
                Err(e) if e.kind() == ErrorKind::Interrupted => {}
                Err(e) => return Err(os_error(&e)),
            }
        }
    }

    fn close(&self, file: StdFile) -> Result<(), OsError> {
        bettermemory_sys::close(file.file).map_err(|e| os_error(&e))
    }

    /// What `os.stat_result` carries of the creation time on this host,
    /// as CPython 3.13 builds it: on Linux no attribute, so `_birth` asks
    /// `statx` with `STATX_BTIME` (None where the mask lacks it or the
    /// call is missing); on Windows `st_birthtime_ns`, an int; on macOS
    /// and the BSDs only the float `st_birthtime`, which CPython makes as
    /// `(double)(unsigned long)tv_sec + 1e-9 * tv_nsec` (posixmodule.c),
    /// so `_birth` loses the low digits through `int(seconds * 1e9)`.
    /// Android's `sys.platform` is `android` (PEP 738): no attribute and
    /// no `statx` there, so nothing.
    fn birth_facts(&self, path: &[u8]) -> BirthFacts {
        let mut facts = BirthFacts::default();
        if cfg!(target_os = "android") {
            return facts;
        }
        let created = native_path(path)
            .ok()
            .and_then(|p| std::fs::metadata(p).ok())
            .and_then(|m| m.created().ok())
            .map(epoch_ns);
        let Some(ns) = created else {
            return facts;
        };
        if cfg!(target_os = "linux") {
            facts.statx = Some(ns);
        } else if cfg!(windows) {
            facts.birthtime_ns = BirthAttr::Int(ns);
        } else {
            facts.birthtime = BirthAttr::Float(birthtime_float(ns));
        }
        facts
    }

    #[cfg(unix)]
    fn nonblock_flag(&self) -> Option<i32> {
        Some(bettermemory_sys::O_NONBLOCK)
    }

    #[cfg(unix)]
    fn status_flags(&self, file: &mut StdFile) -> Result<i32, OsError> {
        bettermemory_sys::status_flags(&file.file).map_err(|e| os_error(&e))
    }

    #[cfg(unix)]
    fn set_status_flags(&self, file: &mut StdFile, flags: i32) -> Result<(), OsError> {
        bettermemory_sys::set_status_flags(&file.file, flags).map_err(|e| os_error(&e))
    }

    /// The C library's text (`strerror_r`, as the standard library words
    /// an OS error) for the number this platform gives the errno's name:
    /// the reverse of `bettermemory_sys::errno_name`, whose numbers are
    /// below 4,096 on every platform. Windows keeps the default: the C
    /// runtime's texts are not ported.
    #[cfg(unix)]
    fn strerror(&self, error: &OsError) -> Option<String> {
        let name = error.errno?;
        let code = (1..4096).find(|&code| bettermemory_sys::errno_name(code) == Some(name))?;
        let text = std::io::Error::from_raw_os_error(code).to_string();
        let end = text.rfind(" (os error ")?;
        Some(text[..end].to_owned())
    }
}

/// macOS: `F_GETPATH` through `bettermemory-sys`.
#[cfg(target_os = "macos")]
fn kernel_path_of(file: &std::fs::File) -> Result<Vec<u8>, Refused> {
    bettermemory_sys::kernel_path(file).map_err(|e| Refused::Raised(py_err(&e)))
}

/// Every other platform: no `F_GETPATH`, and the reference asks none.
#[cfg(not(target_os = "macos"))]
fn kernel_path_of(_: &std::fs::File) -> Result<Vec<u8>, Refused> {
    Err(Refused::NotOffered)
}

/// The most [`Fs::read_once`] of [`StdFs`] reads in one call.
pub const READ_ONCE_CAP: usize = 1 << 20;

/// `os.fstat` of an open file of [`StdFs`].
fn fstat_of(file: &StdFile) -> std::io::Result<Stat> {
    let meta = file.file.metadata()?;
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

/// An `io::Error` as the reference's exception: the class CPython picks
/// and the errno's name.
fn os_error(error: &std::io::Error) -> OsError {
    let unix_name = match error.raw_os_error() {
        Some(code) if cfg!(unix) => bettermemory_sys::errno_name(code),
        _ => None,
    };
    // On POSIX the class follows the errno, as CPython picks it; a code
    // without a name, and Windows, fall back on the error's kind.
    let class = unix_name.map_or_else(|| py_err(error), PyErr::of_errno);
    let errno = match error.raw_os_error() {
        Some(_) if cfg!(unix) => unix_name,
        // Windows: CPython's errno for the codes that pick these classes
        // (read, not run here).
        _ => match class {
            PyErr::FileNotFound => Some("ENOENT"),
            PyErr::NotADirectory => Some("ENOTDIR"),
            PyErr::IsADirectory => Some("EISDIR"),
            PyErr::Permission => Some("EACCES"),
            _ => None,
        },
    };
    let winerror = match error.raw_os_error() {
        Some(code) if cfg!(windows) => u32::try_from(code).ok(),
        _ => None,
    };
    OsError {
        class,
        errno,
        winerror,
    }
}

#[cfg(unix)]
fn stat_of(meta: &std::fs::Metadata) -> StatResult {
    use std::os::unix::fs::MetadataExt;
    let ns = |s: i64, n: i64| i128::from(s) * 1_000_000_000 + i128::from(n);
    StatResult {
        kind: kind_of(meta.file_type()),
        mode: Some(meta.mode()),
        ino: Some(meta.ino()),
        dev: Some(meta.dev()),
        size: meta.size(),
        mtime_ns: ns(meta.mtime(), meta.mtime_nsec()),
        ctime_ns: Some(ns(meta.ctime(), meta.ctime_nsec())),
        flags: st_flags(meta),
        file_attributes: 0,
        reparse_tag: Some(0),
    }
}

/// macOS's `st_flags`.
#[cfg(target_os = "macos")]
fn st_flags(meta: &std::fs::Metadata) -> u32 {
    use std::os::macos::fs::MetadataExt;
    meta.st_flags()
}

/// Linux and other unix: CPython's `stat_result` has `st_flags` only on
/// the BSDs and macOS; Linux has none (0). The other BSDs are not ported
/// (0 here, where CPython would answer the flags).
#[cfg(all(unix, not(target_os = "macos")))]
fn st_flags(_: &std::fs::Metadata) -> u32 {
    0
}

/// Windows: the kind, size and modification time; `st_ino`, `st_mode` and
/// `st_ctime` are not ported (None).
#[cfg(not(unix))]
fn stat_of(meta: &std::fs::Metadata) -> StatResult {
    let mtime_ns = meta
        .modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map_or(0, |d| d.as_nanos() as i128);
    StatResult {
        kind: kind_of(meta.file_type()),
        mode: None,
        ino: None,
        dev: None,
        size: meta.len(),
        mtime_ns,
        ctime_ns: None,
        flags: 0,
        file_attributes: attributes_of(meta),
        reparse_tag: None,
    }
}

/// Windows' `st_file_attributes`, as `GetFileAttributesEx` gives them
/// (read, not run here).
#[cfg(windows)]
fn attributes_of(meta: &std::fs::Metadata) -> u32 {
    use std::os::windows::fs::MetadataExt;
    meta.file_attributes()
}

/// Neither unix nor Windows: not ported (0).
#[cfg(not(any(unix, windows)))]
fn attributes_of(_: &std::fs::Metadata) -> u32 {
    0
}

/// A listed name as bytes: as stored on POSIX.
#[cfg(unix)]
fn name_bytes(name: std::ffi::OsString) -> Result<Vec<u8>, OsError> {
    use std::os::unix::ffi::OsStringExt;
    Ok(name.into_vec())
}

/// A listed name as UTF-8 elsewhere; one that is not Unicode (CPython
/// gives a `str` with lone surrogates) is refused as an OSError.
#[cfg(not(unix))]
fn name_bytes(name: std::ffi::OsString) -> Result<Vec<u8>, OsError> {
    name.into_string()
        .map(String::into_bytes)
        .map_err(|_| OsError {
            class: PyErr::Os,
            errno: None,
            winerror: None,
        })
}

/// A link's target as bytes: as stored on POSIX.
#[cfg(unix)]
fn link_bytes(target: std::path::PathBuf) -> Result<Vec<u8>, PyErr> {
    use std::os::unix::ffi::OsStringExt;
    Ok(target.into_os_string().into_vec())
}

/// A link's target as UTF-8 elsewhere; one that is not Unicode is
/// refused as an OSError (the reference reads it as `str`).
#[cfg(not(unix))]
fn link_bytes(target: std::path::PathBuf) -> Result<Vec<u8>, PyErr> {
    target
        .into_os_string()
        .into_string()
        .map(String::into_bytes)
        .map_err(|_| PyErr::Os)
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
const WRITE: u32 = 0o200;
const SEARCH: u32 = 0o100;

/// The hops macOS follows before ELOOP (Linux follows 40); the default,
/// so a fixture made on either agrees wherever a chain is shorter.
pub const LINK_HOPS: usize = 32;

/// `NAME_MAX`: the longest name of one path component, in bytes.
pub const NAME_MAX: usize = 255;

/// macOS's `PATH_MAX` (Linux takes 4096): the default, so a fixture made
/// on either agrees wherever a path is shorter. A POSIX fixture may hold
/// no path between 1,025 and 4,096 bytes, which macOS refuses
/// (ENAMETOOLONG) and Linux reads; the generators refuse to write one.
pub const PATH_MAX: usize = 1024;

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
    /// `st_flags` of a path where not 0 (`set_flags`).
    pub flags: BTreeMap<Vec<u8>, u32>,
    /// Devices mounted at folders (`mount`); the rest is device 0.
    pub mounts: BTreeMap<Vec<u8>, u64>,
    pub link_hops: usize,
    /// The host's `PATH_MAX`, its terminating NUL included: a path of
    /// this many bytes or more is ENAMETOOLONG, as is a link's target
    /// with the bytes after the link (`walk`).
    pub path_max: usize,
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
            flags: BTreeMap::new(),
            mounts: BTreeMap::new(),
            link_hops: LINK_HOPS,
            path_max: PATH_MAX,
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

    /// The stat result of the node `node` whose identity is `id`.
    fn stat_result(&self, id: &[u8], node: &Node) -> StatResult {
        let kind = kind_of_node(node);
        let size = match node {
            Node::File(bytes) => bytes.len() as u64,
            Node::Link(target) => target.len() as u64,
            _ => 0,
        };
        let posix = self.flavour == Flavour::Posix;
        let (dev, rel) = self.device_of(id);
        let bits = match node {
            Node::Link(_) => 0o777,
            _ => self.mode(id, node),
        };
        StatResult {
            kind,
            mode: posix.then(|| type_bits(kind) | bits),
            // An inode number for each identity on its device, never 0.
            ino: posix.then(|| inode_of(rel)),
            dev: posix.then_some(dev),
            size,
            mtime_ns: 0,
            ctime_ns: posix.then_some(0),
            flags: self.flags.get(id).copied().unwrap_or(0),
            file_attributes: 0,
            // Windows' tag is not ported, as in `StdFs`.
            reparse_tag: posix.then_some(0),
        }
    }

    /// The device `id` is on (the longest mount above it, else 0) and its
    /// path below that mount, from which its inode number is made: two
    /// devices can hold the same inode number.
    fn device_of<'a>(&self, id: &'a [u8]) -> (u64, &'a [u8]) {
        let mut best: (u64, &[u8]) = (0, id);
        let mut best_len = 0;
        for (at, &dev) in &self.mounts {
            // The root's key is empty: a mount there covers every path.
            let under = at.is_empty()
                || id == at.as_slice()
                || (id.starts_with(at) && id.get(at.len()) == Some(&b'/'));
            if under && at.len() >= best_len {
                best_len = at.len();
                let rest = &id[at.len()..];
                best = (dev, rest);
            }
        }
        best
    }

    /// Set the file flags (`st_flags`) of `path`, for a placeholder.
    pub fn set_flags(&mut self, path: &[u8], flags: u32) {
        let key = self.key(path);
        self.flags.insert(key, flags);
    }

    /// Mount a device `dev` (not 0) at the folder `path`.
    pub fn mount(&mut self, path: &[u8], dev: u64) {
        let key = self.key(path);
        self.mounts.insert(key, dev);
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

    fn not_a_directory(&self) -> OsError {
        match self.flavour {
            Flavour::Posix => OsError::new(PyErr::NotADirectory, "ENOTDIR"),
            Flavour::Windows => OsError::new(PyErr::FileNotFound, "ENOENT"),
        }
    }

    /// The node `path` names, with its identity: each component looked up
    /// in the directory reached so far (search permission asked of it),
    /// `..` included, links followed as met (a last one only where
    /// `follow_last`).
    ///
    /// The path's bytes not yet read are kept as the kernel keeps them, so
    /// the length limit is checked where macOS checks it (measured on this
    /// machine): the path as given, and at each link followed, the link's
    /// target and the bytes after the link together, each ENAMETOOLONG at
    /// `path_max` bytes or more.
    ///
    /// Cost: each component's lookup compares the whole key of the folder
    /// reached, so a walk costs its components times the depth reached.
    /// That is quadratic in depth, bounded by `path_max` for each of at
    /// most `link_hops + 1` stretches of path.
    fn walk(&self, path: &[u8], follow_last: bool) -> Result<(Vec<u8>, Node), OsError> {
        if path.contains(&0) {
            return Err(OsError::value());
        }
        if self.flavour == Flavour::Windows && std::str::from_utf8(path).is_err() {
            return Err(OsError::value());
        }
        // ENAMETOOLONG at the host's path limit (OSError itself).
        if path.len() >= self.path_max {
            return Err(OsError::new(PyErr::Os, "ENAMETOOLONG"));
        }
        // An empty path names nothing (ENOENT; `_wopen("")` too).
        if path.is_empty() {
            return Err(OsError::new(PyErr::FileNotFound, "ENOENT"));
        }
        // A trailing separator asks only that the last component be a
        // directory, a final link followed; no `.` is looked up, so it
        // needs no search permission.
        let mut want_dir = self.trailing_separator(path);
        // The bytes not yet read: segments (the path, then each link's
        // target pushed on top) with how far each is read, and their count.
        let mut stack: Vec<(Vec<u8>, usize)> = vec![(path.to_vec(), 0)];
        let mut unread = path.len();
        // The directory reached, as a resolved key, and where each of its
        // components starts (for `..`).
        let mut dir: Vec<u8> = Vec::new();
        let mut starts: Vec<usize> = Vec::new();
        let mut hops = 0;
        let mut found: (Vec<u8>, Node) = (Vec::new(), Node::Dir);
        self.skip_separators(&mut stack, &mut unread);
        while let Some(part) = self.next_part(&mut stack, &mut unread) {
            // The bytes after this component, separators included.
            let after = unread;
            self.skip_separators(&mut stack, &mut unread);
            let last = stack.is_empty();
            // Looking a name up in a directory needs search permission.
            if self.mode(&dir, &Node::Dir) & SEARCH == 0 {
                return Err(OsError::new(PyErr::Permission, "EACCES"));
            }
            // ENAMETOOLONG for a name past NAME_MAX (255 bytes on macOS
            // and Linux alike).
            if part.len() > NAME_MAX {
                return Err(OsError::new(PyErr::Os, "ENAMETOOLONG"));
            }
            if part == b"." {
                if last {
                    found = (dir.clone(), Node::Dir);
                }
                continue;
            }
            if part == b".." {
                if let Some(start) = starts.pop() {
                    dir.truncate(start.saturating_sub(1));
                }
                if last {
                    found = (dir.clone(), Node::Dir);
                }
                continue;
            }
            let base = dir.len();
            if base > 0 {
                dir.push(b'/');
            }
            dir.extend_from_slice(&part);
            let looked = self.node(&dir);
            let Some((id, node)) = looked else {
                return Err(OsError::new(PyErr::FileNotFound, "ENOENT"));
            };
            match node {
                Node::Link(target) if !last || follow_last || want_dir => {
                    dir.truncate(base);
                    hops += 1;
                    if hops > self.link_hops {
                        return Err(OsError::new(PyErr::Os, "ELOOP"));
                    }
                    // A link to an empty target names nothing (macOS;
                    // Linux cannot make one).
                    if target.is_empty() {
                        return Err(OsError::new(PyErr::FileNotFound, "ENOENT"));
                    }
                    // The target and the bytes after the link, as the
                    // kernel splices them.
                    if target.len().saturating_add(after) >= self.path_max {
                        return Err(OsError::new(PyErr::Os, "ENAMETOOLONG"));
                    }
                    if target.first().is_some_and(|&b| self.separator(b)) {
                        dir.clear();
                        starts.clear();
                    }
                    if last {
                        want_dir |= self.trailing_separator(&target);
                    }
                    if !target.iter().any(|&b| !self.separator(b)) {
                        // A link to the root.
                        found = (Vec::new(), Node::Dir);
                    }
                    unread += target.len();
                    stack.push((target, 0));
                    self.skip_separators(&mut stack, &mut unread);
                }
                Node::Dir => {
                    starts.push(if base > 0 { base + 1 } else { 0 });
                    found = (id, Node::Dir);
                }
                other => {
                    dir.truncate(base);
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

    /// The non-blocking open, a final link followed where `follow` and
    /// ELOOP where not.
    fn open_at(&self, path: &[u8], follow: bool) -> Result<MemFile, OsError> {
        if self.flavour == Flavour::Windows && self.trailing_separator(path) {
            // `_wopen` of a path ending in a separator finds nothing.
            self.walk(path, true)?;
            return Err(OsError::new(PyErr::FileNotFound, "ENOENT"));
        }
        let (id, node) = self.walk(path, follow)?;
        if matches!(node, Node::Link(_)) {
            return Err(OsError::new(PyErr::Os, "ELOOP"));
        }
        if self.flavour == Flavour::Windows {
            // `_wopen` of a directory is refused (EACCES).
            if node == Node::Dir {
                return Err(OsError::new(PyErr::Permission, "EACCES"));
            }
        } else {
            if self.mode(&id, &node) & READ == 0 {
                return Err(OsError::new(PyErr::Permission, "EACCES"));
            }
            // A socket cannot be opened: ENXIO on Linux, EOPNOTSUPP on
            // macOS, OSError either way. The model answers as Linux, as
            // its walk does elsewhere (`..` after a non-directory).
            if node == Node::Socket {
                return Err(OsError::new(PyErr::Os, "ENXIO"));
            }
        }
        Ok(MemFile {
            node,
            at: 0,
            identity: id,
        })
    }

    /// Read past separators, dropping each segment read to its end.
    fn skip_separators(&self, stack: &mut Vec<(Vec<u8>, usize)>, unread: &mut usize) {
        while let Some((bytes, at)) = stack.last_mut() {
            while *at < bytes.len() && self.separator(bytes[*at]) {
                *at += 1;
                *unread -= 1;
            }
            if *at < bytes.len() {
                return;
            }
            stack.pop();
        }
    }

    /// The next component (separators before it already read).
    fn next_part(&self, stack: &mut Vec<(Vec<u8>, usize)>, unread: &mut usize) -> Option<Vec<u8>> {
        let (bytes, at) = stack.last_mut()?;
        let start = *at;
        while *at < bytes.len() && !self.separator(bytes[*at]) {
            *at += 1;
        }
        *unread -= *at - start;
        let part = bytes[start..*at].to_vec();
        if *at >= bytes.len() {
            stack.pop();
        }
        Some(part)
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
        self.open_at(path, true).map_err(PyErr::from)
    }

    fn open_result(&self, path: &[u8]) -> Result<MemFile, OsError> {
        self.open_at(path, true)
    }

    fn open_no_follow(&self, path: &[u8]) -> Result<MemFile, OsError> {
        // Windows has no `O_NOFOLLOW`: the open follows there.
        self.open_at(path, self.flavour == Flavour::Windows)
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

    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        let found = match self.flavour {
            // `GetFileAttributesW`: the link itself, found or not; no
            // read-only attribute is modelled, so W_OK holds too.
            Flavour::Windows => self.walk(path, false).map(|_| true),
            // `access(2)`: links followed, each folder searched on the
            // way (`walk`), then the owner's bits of what is found.
            Flavour::Posix => self
                .walk(path, true)
                .map(|(id, node)| self.mode(&id, &node) & mode.bits() == mode.bits()),
        };
        match found {
            Ok(answer) => Ok(answer),
            Err(e) if e.class == PyErr::Value => Err(PyErr::Value),
            Err(_) => Ok(false),
        }
    }

    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        Ok(kind_of_node(&self.walk(path, false)?.1))
    }

    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.readlink_result(path).map_err(PyErr::from)
    }

    fn readlink_result(&self, path: &[u8]) -> Result<Vec<u8>, OsError> {
        match self.walk(path, false)?.1 {
            Node::Link(target) => Ok(target),
            // EINVAL: no link (OSError itself).
            _ => Err(OsError::new(PyErr::Os, "EINVAL")),
        }
    }

    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        let (id, node) = self.walk(path, true)?;
        Ok(self.stat_result(&id, &node))
    }

    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        let (id, node) = self.walk(path, false)?;
        Ok(self.stat_result(&id, &node))
    }

    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        let (id, node) = self.walk(path, true)?;
        if node != Node::Dir {
            return Err(OsError::new(PyErr::NotADirectory, "ENOTDIR"));
        }
        if self.mode(&id, &node) & READ == 0 {
            return Err(OsError::new(PyErr::Permission, "EACCES"));
        }
        let mut prefix = id.clone();
        if !prefix.is_empty() {
            prefix.push(b'/');
        }
        let mut names: std::collections::BTreeSet<Vec<u8>> = std::collections::BTreeSet::new();
        for key in self.nodes.range(prefix.clone()..).map(|(k, _)| k) {
            let Some(rest) = key.strip_prefix(prefix.as_slice()) else {
                break;
            };
            let name = rest.split(|&b| b == b'/').next().unwrap_or(rest);
            names.insert(name.to_vec());
        }
        Ok(names.into_iter().collect())
    }

    fn rewind(&self, file: &mut MemFile) -> Result<(), Refused> {
        match file.node {
            // ESPIPE.
            Node::Fifo | Node::Socket => Err(Refused::Raised(PyErr::Os)),
            _ => {
                file.at = 0;
                Ok(())
            }
        }
    }

    fn seek(&self, file: &mut MemFile, offset: u64) -> Result<(), Refused> {
        match file.node {
            // ESPIPE.
            Node::Fifo | Node::Socket => Err(Refused::Raised(PyErr::Os)),
            _ => {
                file.at = usize::try_from(offset).unwrap_or(usize::MAX);
                Ok(())
            }
        }
    }

    fn kernel_path(&self, file: &mut MemFile) -> Result<Vec<u8>, Refused> {
        if self.flavour == Flavour::Windows {
            return Err(Refused::NotOffered);
        }
        // The path as resolved, from the root. With its NUL it must fit
        // the reference's buffer of 1,024 bytes (ENOSPC past it), whatever
        // the host's `PATH_MAX`.
        let mut path = vec![b'/'];
        path.extend_from_slice(&file.identity);
        if path.len() >= bettermemory_sys::KERNEL_PATH_BUFFER {
            return Err(Refused::Raised(PyErr::Os));
        }
        Ok(path)
    }

    /// The texts macOS's C library and glibc share, for the errnos this
    /// model raises and their neighbours; None for a name they word
    /// apart (ENXIO, ESTALE, ETIMEDOUT, ENOTCONN, EILSEQ) and under
    /// [`Flavour::Windows`], whose C runtime's texts are not ported.
    fn strerror(&self, error: &OsError) -> Option<String> {
        if self.flavour == Flavour::Windows {
            return None;
        }
        let text = match error.errno? {
            "ENOENT" => "No such file or directory",
            "ENOTDIR" => "Not a directory",
            "EISDIR" => "Is a directory",
            "EACCES" => "Permission denied",
            "EPERM" => "Operation not permitted",
            "EEXIST" => "File exists",
            "ELOOP" => "Too many levels of symbolic links",
            "ENAMETOOLONG" => "File name too long",
            "EIO" => "Input/output error",
            "EINVAL" => "Invalid argument",
            "EINTR" => "Interrupted system call",
            "EBADF" => "Bad file descriptor",
            _ => return None,
        };
        Some(text.to_owned())
    }

    fn holds_other_than(&self, path: &[u8], name: &[u8]) -> Result<bool, OsError> {
        use std::ops::Bound;
        let (id, node) = self.walk(path, true)?;
        if node != Node::Dir {
            return Err(OsError::new(PyErr::NotADirectory, "ENOTDIR"));
        }
        if self.mode(&id, &node) & READ == 0 {
            return Err(OsError::new(PyErr::Permission, "EACCES"));
        }
        let mut prefix = id;
        if !prefix.is_empty() {
            prefix.push(b'/');
        }
        // The keys of one name are the name's own and a run of keys below
        // it, so at most two steps pass over `name`: never a listing.
        let mut from = Bound::Included(prefix.clone());
        loop {
            let Some((key, _)) = self.nodes.range((from, Bound::Unbounded)).next() else {
                return Ok(false);
            };
            let Some(rest) = key.strip_prefix(prefix.as_slice()) else {
                return Ok(false);
            };
            let first = rest.split(|&b| b == b'/').next().unwrap_or(rest);
            if first != name {
                return Ok(true);
            }
            from = if first.len() == rest.len() {
                Bound::Excluded(key.clone())
            } else {
                // Past every key below `name/`: `0` follows `/`.
                let mut past = prefix.clone();
                past.extend_from_slice(name);
                past.push(b'0');
                Bound::Included(past)
            };
        }
    }
}

/// A stable inode number for an identity (FNV-1a), never 0.
fn inode_of(id: &[u8]) -> u64 {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in id {
        hash ^= u64::from(b);
        hash = hash.wrapping_mul(0x0100_0000_01b3);
    }
    hash | 1
}
