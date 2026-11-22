//! `config.load_config(path, create=True)`: the first-run write.
//!
//! The reference makes the file's parent directories
//! (`parent.mkdir(parents=True, exist_ok=True)`), writes [`DEFAULT_CONFIG`]
//! with `_fsutil.atomic_write_bytes` when `Path.exists` says there is no
//! file, prints [`created_notice`] on stderr, then reads the file as
//! `load_config(create=False)` does, except that a file gone by then is
//! FileNotFoundError rather than the defaults. The three effects reach the
//! logic through [`Create`]; [`SystemCreate`] is the real one. The notice is
//! the caller's to print: [`load_config_creating`] says whether it wrote.

use std::io;
use std::path::Path;

use super::{overlay_managed, read_config_file, Config, Ctx, FileError, OsFailure, Raised, World};
use crate::toml::Table;

/// `config.DEFAULT_CONFIG`, byte for byte (`settings_create.jsonl` holds
/// its length and sha256).
pub const DEFAULT_CONFIG: &str = include_str!("default_config.toml");

/// The effects of the first-run write, each as the reference's call answers.
pub trait Create {
    /// `Path(path).parent.mkdir(parents=True, exist_ok=True)`.
    fn mkdir_parent(&self, path: &str) -> Result<(), Raised>;
    /// `Path(path).exists()` as CPython 3.13 answers: false for a missing
    /// entry, ENOTDIR, ELOOP, EBADF or a NUL; any other stat error (EINVAL
    /// among them) raises (3.14 answers false there too, and its write then
    /// fails).
    fn exists(&self, path: &str) -> Result<bool, Raised>;
    /// `_fsutil.atomic_write_bytes(path, data)` with neither mode: a new
    /// file is 0o600 on POSIX.
    fn write_atomic(&self, path: &str, data: &[u8]) -> Result<(), Raised>;
}

/// The line the reference prints on stderr after the first-run write.
pub fn created_notice(path: &str) -> String {
    format!("[bettermemory] created default config at {path}\n")
}

/// `config.load_config(path, create=True)`: whether the default file was
/// written (the caller then prints [`created_notice`]), and the load.
pub fn load_config_creating(
    world: &World,
    create: &dyn Create,
    path: &str,
    ctx: &mut Ctx,
) -> (bool, Result<Config, Raised>) {
    #[cfg(unix)]
    debug_assert!(is_posix_spelled(path), "path not spelled as Path spells it");
    if let Err(raised) = create.mkdir_parent(path) {
        return (false, Err(raised));
    }
    let made = match create.exists(path) {
        Err(raised) => return (false, Err(raised)),
        Ok(true) => false,
        Ok(false) => {
            if let Err(raised) = create.write_atomic(path, DEFAULT_CONFIG.as_bytes()) {
                return (false, Err(raised));
            }
            true
        }
    };
    let data: Table = match read_config_file(world.fs, path) {
        Ok(data) => data,
        Err(FileError::NotFound) => {
            let gone = OsFailure {
                class: "FileNotFoundError",
                errno: 2,
                strerror: "No such file or directory".to_owned(),
            };
            return (made, Err(Raised::os(&gone, path)));
        }
        Err(FileError::Os(failure)) => return (made, Err(Raised::os(&failure, path))),
        Err(FileError::Raised(raised)) => return (made, Err(raised)),
    };
    (made, overlay_managed(world, data, path, ctx))
}

/// The real file system. A relative path is read against the process's
/// working directory.
pub struct SystemCreate;

/// The `OSError` subclass Python raises for an errno.
fn failure(error: &io::Error) -> OsFailure {
    use io::ErrorKind::*;
    let class = match error.kind() {
        PermissionDenied => "PermissionError",
        AlreadyExists => "FileExistsError",
        IsADirectory => "IsADirectoryError",
        NotADirectory => "NotADirectoryError",
        NotFound => "FileNotFoundError",
        _ => "OSError",
    };
    let text = error.to_string();
    let strerror = match text.rfind(" (os error ") {
        Some(i) => text[..i].to_owned(),
        None => text,
    };
    OsFailure {
        class,
        errno: error.raw_os_error().unwrap_or(0),
        strerror,
    }
}

/// The exception for an I/O error on `path`. On Windows Python's `str()`
/// names the Windows error code: `[WinError N] text: 'path'`.
fn raised(error: &io::Error, path: &Path) -> Raised {
    let failure = failure(error);
    if cfg!(windows) {
        return Raised {
            class: failure.class,
            message: format!(
                "[WinError {}] {}: {}",
                failure.errno,
                winerror_text(&failure.strerror),
                crate::toml::py_repr_str(&path.to_string_lossy())
            ),
        };
    }
    Raised::os(&failure, &path.to_string_lossy())
}

/// `PyErr_SetExcFromWindowsErr`'s text: `FormatMessageW`'s message with
/// every trailing character at or below a space, and every trailing dot,
/// removed. Rust's `io::Error` trims only the line break, so its text
/// keeps the final period.
pub fn winerror_text(text: &str) -> &str {
    text.trim_end_matches(|c: char| c <= ' ' || c == '.')
}

/// `Path.mkdir(exist_ok=True)`, with `parents` as given: a missing parent
/// raises FileNotFoundError unless `parents` is set.
pub fn mkdir(dir: &Path, parents: bool) -> Result<(), Raised> {
    match std::fs::create_dir(dir) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            let up = match dir.parent() {
                Some(p) if p.as_os_str().is_empty() => Path::new("."),
                Some(p) => p,
                None => return Err(raised(&e, dir)),
            };
            if !parents || up == dir {
                return Err(raised(&e, dir));
            }
            mkdir(up, true)?;
            mkdir(dir, false)
        }
        Err(_) if dir.is_dir() => Ok(()),
        Err(e) => Err(raised(&e, dir)),
    }
}

/// `pathlib._abc._ignore_error`: the errors `exists` reads as no file,
/// by the code the system gave. POSIX: ENOENT, ENOTDIR, EBADF, ELOOP.
/// Windows: the codes CPython maps to ENOENT or EBADF (2, 3, 6) and its
/// own list (21, 123, 1921); ERROR_DIRECTORY (267), which std calls
/// NotADirectory, is not among them. EINVAL raises.
pub fn ignored_code(code: i32) -> bool {
    #[cfg(target_os = "macos")]
    let codes = [2, 20, 9, 62];
    #[cfg(target_os = "linux")]
    let codes = [2, 20, 9, 40];
    #[cfg(windows)]
    let codes = [2, 3, 6, 21, 123, 1921];
    #[cfg(not(any(target_os = "macos", target_os = "linux", windows)))]
    let codes = [2, 20, 9];
    codes.contains(&code)
}

fn ignored(error: &io::Error) -> bool {
    error.raw_os_error().is_some_and(ignored_code)
}

/// Whether `path` is spelled as `str(PurePosixPath(path))` spells it: not
/// empty, no empty or `.` part (a leading `//` aside), no trailing `/`
/// (the root aside). The first-run write assumes it; `Path` would
/// otherwise make `config.toml/` a file and print `./x` as `x`.
pub fn is_posix_spelled(path: &str) -> bool {
    if path == "." || path == "/" || path == "//" {
        return true;
    }
    let body = path
        .strip_prefix("//")
        .filter(|rest| !rest.starts_with('/'))
        .or_else(|| path.strip_prefix('/'))
        .unwrap_or(path);
    !body.is_empty() && body.split('/').all(|part| !part.is_empty() && part != ".")
}

impl Create for SystemCreate {
    fn mkdir_parent(&self, path: &str) -> Result<(), Raised> {
        let parent = match Path::new(path).parent() {
            Some(p) if !p.as_os_str().is_empty() => p,
            _ => Path::new("."),
        };
        // Only the parent's text reaches mkdir: a NUL in the final name
        // raises later, from the write's open.
        if parent.to_string_lossy().contains('\0') {
            return Err(super::nul_in_path("mkdir"));
        }
        mkdir(parent, true)
    }

    fn exists(&self, path: &str) -> Result<bool, Raised> {
        if path.contains('\0') {
            return Ok(false);
        }
        match std::fs::metadata(path) {
            Ok(_) => Ok(true),
            Err(e) if ignored(&e) => Ok(false),
            Err(e) => Err(raised(&e, Path::new(path))),
        }
    }

    fn write_atomic(&self, path: &str, data: &[u8]) -> Result<(), Raised> {
        // tempfile's os.open of `<dir>/<name>XXXX` meets the NUL first.
        if path.contains('\0') {
            return Err(super::nul_in_path("open"));
        }
        crate::store::write::replace_retrying(Path::new(path), data)
            .map_err(|e| raised(&e, Path::new(path)))
    }
}

#[cfg(test)]
mod embedded_text_tests {
    use super::DEFAULT_CONFIG;

    /// The compiled-in text has LF line ends only. `.gitattributes` marks
    /// `default_config.toml` -text, but a checkout made with CRLF before
    /// that rule keeps CRLF until the file is checked out again, and
    /// `include_str!` embeds the bytes as they are on disk.
    #[test]
    fn the_default_config_holds_no_carriage_return() {
        assert!(!DEFAULT_CONFIG.contains('\r'));
        assert!(DEFAULT_CONFIG.starts_with("# bettermemory config\n"));
    }
}
