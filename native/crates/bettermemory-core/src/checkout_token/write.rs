//! `write` of `checkout_token.py`: the guard, the temporary file in
//! `info/`, the stale-temp sweep, the link (or the exclusive create where
//! links are refused), the read of another writer's token after a lost
//! race, and the failure texts (the module's third unit).
//!
//! Effects are arguments: the file system a [`TokenFs`], the effective
//! uid, the platform, random bytes, clock and pause a [`Machine`]. A
//! [`Fault`] is a ValueError the reference raises (a NUL in a path), or an
//! error with no errno name or text to word the detail from (declined).

use super::read::{os_text, read_at, Fault, ReadState, REREAD_DELAY_SECONDS};
use super::{mint, random_bytes_needed, random_crockford, render, token_path, TOKEN_LENGTH};
use super::{CUT_SHORT, EMPTY};
use crate::config::{self, pure_path};
use crate::fsdata::{Flavour, Fs, Numbered, OsError, PyErr, StdFs};
use crate::fsutil::{unlink_error, unlink_retrying_by, Remover};
use crate::gitfile_rule::gitdir::Ctx;
use crate::gitfile_rule::hostpath;

/// `_RACE_READ_SECONDS`: how long a writer that lost the race reads again.
pub const RACE_READ_SECONDS: f64 = 1.5;
/// `_RACE_PAUSE_MAX_SECONDS`: the longest pause between two such reads.
pub const RACE_PAUSE_MAX_SECONDS: f64 = 0.2;
/// `_TEMP_SUFFIX_LENGTH`: characters of randomness in a temporary name.
pub const TEMP_SUFFIX_LENGTH: i64 = 12;
/// `_TEMP_PREFIX`: `f"{TOKEN_NAME}.tmp-"`.
pub const TEMP_PREFIX: &str = "bettermemory.tmp-";
/// `_STALE_TEMP_SECONDS`: a temporary file older was left by a killed writer.
pub const STALE_TEMP_SECONDS: f64 = 3600.0;
/// `_FILE_MODE`.
pub const FILE_MODE: u32 = 0o644;
/// `_READONLY_ERRNOS`, by name.
pub const READONLY_ERRNOS: [&str; 3] = ["EACCES", "EPERM", "EROFS"];

/// `WriteStatus`; the last two only a re-mint gives (`CONTENDED`,
/// `CLAIM_BLOCKED`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WriteStatus {
    Written,
    Exists,
    Malformed,
    SkippedBare,
    SkippedForeignOwner,
    SkippedPolicy,
    FailedReadonly,
    FailedIo,
    SkippedContended,
    FailedClaimBlocked,
}

impl WriteStatus {
    /// The status as the reference spells it.
    pub fn name(self) -> &'static str {
        match self {
            WriteStatus::Written => "written",
            WriteStatus::Exists => "exists",
            WriteStatus::Malformed => "malformed",
            WriteStatus::SkippedBare => "skipped:bare",
            WriteStatus::SkippedForeignOwner => "skipped:foreign_owner",
            WriteStatus::SkippedPolicy => "skipped:policy",
            WriteStatus::FailedReadonly => "failed:readonly",
            WriteStatus::FailedIo => "failed:io",
            WriteStatus::SkippedContended => "skipped:contended",
            WriteStatus::FailedClaimBlocked => "failed:claim_blocked",
        }
    }
}

/// `TokenWrite`: the outcome of `write`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TokenWrite {
    pub status: WriteStatus,
    pub token: Option<String>,
    pub detail: String,
}

impl TokenWrite {
    fn of(status: WriteStatus, detail: impl Into<String>) -> TokenWrite {
        let detail = detail.into();
        TokenWrite {
            status,
            token: None,
            detail,
        }
    }

    /// `ok`: the git directory now carries a token.
    pub fn ok(&self) -> bool {
        matches!(self.status, WriteStatus::Written | WriteStatus::Exists)
    }
}

/// The machine `write` runs on, besides its file system.
pub trait Machine {
    /// `os.geteuid()`.
    fn euid(&mut self) -> u32;
    /// `sys.platform == "win32"`.
    fn win32(&self) -> bool;
    /// `os.urandom(count)`, as `secrets.randbits` asks for it.
    fn urandom(&mut self, count: usize) -> Vec<u8>;
    /// `time.monotonic()`.
    fn monotonic(&mut self) -> f64;
    /// `time.sleep(seconds)`.
    fn sleep(&mut self, seconds: f64);
}

/// The write-side calls of `write`, over the reads of [`Fs`]. Paths are
/// what `os.fspath` gives of the reference's `Path`; an error is the
/// exception the reference's call raises (class [`PyErr::Value`] for a
/// path holding a NUL).
pub trait TokenFs: Fs {
    /// A file [`TokenFs::create_excl`] opened.
    type Created;
    /// `os.mkdir(path)`, mode 0o777.
    fn mkdir(&self, path: &[u8]) -> Result<(), OsError>;
    /// `os.open(path, O_WRONLY | O_CREAT | O_EXCL | O_BINARY, mode)`.
    fn create_excl(&self, path: &[u8], mode: u32) -> Result<Self::Created, OsError>;
    /// One `os.write(fd, data)`: the count written.
    fn write_once(&self, file: &mut Self::Created, data: &[u8]) -> Result<usize, OsError>;
    /// `os.fsync(fd)`; `fsync_file` swallows its error.
    fn fsync_file(&self, file: &mut Self::Created) -> Result<(), OsError>;
    /// `os.close(fd)`.
    fn close_created(&self, file: Self::Created) -> Result<(), OsError>;
    /// `os.link(src, dst)`.
    fn link(&self, src: &[u8], dst: &[u8]) -> Result<(), OsError>;
    /// `fsync_dir(path)` past its Windows check: an open with
    /// `O_DIRECTORY`, an fsync and a close, every error swallowed.
    fn fsync_dir(&self, path: &[u8]);
    /// The names `os.scandir(path)` yields, in its order, up to the first
    /// error of the iteration; an error of the open is the error.
    fn scandir_names(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError>;
    /// `os.unlink(path)`.
    fn unlink(&self, path: &[u8]) -> Result<(), OsError>;
    /// `os.utime(path)`: both times set to now, links followed.
    fn utime_now(&self, path: &[u8]) -> Result<(), OsError>;
    /// `os.stat(path).st_uid`; None where the machine has no uids.
    fn owner_uid(&self, path: &[u8]) -> Result<Option<u32>, OsError>;
    /// `DirEntry.stat(follow_symlinks=False).st_mtime`.
    fn lstat_mtime(&self, path: &[u8]) -> Result<f64, OsError> {
        self.lstat_result(path).map(|s| st_mtime(s.mtime_ns))
    }
    /// `os.stat(path).st_mtime`.
    fn stat_mtime(&self, path: &[u8]) -> Result<f64, OsError> {
        self.stat(path).map(|s| st_mtime(s.mtime_ns))
    }
    /// `os.replace(src, dst)`. The default declines: an error with no
    /// errno name, which `failed` answers as not ported.
    fn replace(&self, src: &[u8], dst: &[u8]) -> Result<(), OsError> {
        let _ = (src, dst);
        Err(OsError::of_class(PyErr::Os))
    }
}

/// `st_mtime` as CPython's `fill_time` makes it from the nanoseconds:
/// whole seconds plus `nsec * 1e-9`, in doubles.
pub fn st_mtime(ns: i128) -> f64 {
    let sec = ns.div_euclid(1_000_000_000);
    let nsec = ns.rem_euclid(1_000_000_000);
    sec as f64 + nsec as f64 * 1e-9
}

/// The OSError, or the ValueError the reference does not catch.
fn caught(error: OsError) -> Result<OsError, Fault> {
    if error.class == PyErr::Value {
        Err(Fault::Raised(error))
    } else {
        Ok(error)
    }
}

/// `_failed(exc)`: EACCES, EPERM and EROFS are failed:readonly, anything
/// else failed:io, the detail `_os_text`.
pub fn failed<F: Fs + ?Sized>(fs: &F, error: &OsError) -> Result<TokenWrite, Fault> {
    let text = os_text(fs, error).ok_or(Fault::NotPorted)?;
    let readonly = error.errno.is_some_and(|n| READONLY_ERRNOS.contains(&n));
    let status = if readonly {
        WriteStatus::FailedReadonly
    } else {
        WriteStatus::FailedIo
    };
    Ok(TokenWrite::of(status, text))
}

/// `str(Path(base) / name)` under `flavour`.
fn under(flavour: Flavour, base: &str, name: &str) -> String {
    let joined = hostpath::join(flavour, base.as_bytes(), name.as_bytes());
    let pure = match flavour {
        Flavour::Posix => config::Flavour::Posix,
        Flavour::Windows => config::Flavour::Windows,
    };
    pure_path(&String::from_utf8_lossy(&joined), pure)
}

/// `write(git_dir, policy=..., bare=...)`.
pub fn write<F: TokenFs + ?Sized>(
    ctx: &Ctx<'_, F>,
    machine: &mut dyn Machine,
    git_dir: &str,
    policy: bool,
    bare: bool,
) -> Result<TokenWrite, Fault> {
    if let Some(skipped) = guard(ctx.fs, machine, git_dir, policy, bare)? {
        return Ok(skipped);
    }
    let path = token_path(git_dir, ctx.flavour);
    if let Some(present) = existing(ctx, machine, path.as_bytes())? {
        return Ok(present);
    }
    let needed = random_bytes_needed(TOKEN_LENGTH as i64).unwrap_or(0);
    let value = mint(&machine.urandom(needed)).map_err(|_| Fault::NotPorted)?;
    let body =
        render(&value.chars().map(u32::from).collect::<Vec<_>>()).map_err(|_| Fault::NotPorted)?;
    let info = match info_dir(ctx.fs, ctx.flavour, git_dir)? {
        Ok(info) => info,
        Err(error) => return failed(ctx.fs, &error),
    };
    let temp = match write_temp(ctx, machine, &info, &body)? {
        Ok(temp) => temp,
        Err(error) => return failed(ctx.fs, &error),
    };
    let placed = place(ctx, machine, &temp, &path, &info, value, &body);
    unlink_quietly(ctx.fs, machine, temp.as_bytes())?;
    placed
}

/// `_guard`: policy off, a bare repository, then (off Windows) the git
/// directory's owner against the effective uid.
pub fn guard<F: TokenFs + ?Sized>(
    fs: &F,
    machine: &mut dyn Machine,
    git_dir: &str,
    policy: bool,
    bare: bool,
) -> Result<Option<TokenWrite>, Fault> {
    if !policy {
        return Ok(Some(TokenWrite::of(WriteStatus::SkippedPolicy, "")));
    }
    if bare {
        return Ok(Some(TokenWrite::of(WriteStatus::SkippedBare, "")));
    }
    if !machine.win32() {
        let owner = match fs.owner_uid(git_dir.as_bytes()) {
            Ok(owner) => owner.ok_or(Fault::NotPorted)?,
            Err(error) => return failed(fs, &caught(error)?).map(Some),
        };
        if owner != machine.euid() {
            return Ok(Some(TokenWrite::of(WriteStatus::SkippedForeignOwner, "")));
        }
    }
    Ok(None)
}

/// `_existing(path)`: None where no file is there.
pub fn existing<F: TokenFs + ?Sized>(
    ctx: &Ctx<'_, F>,
    machine: &mut dyn Machine,
    path: &[u8],
) -> Result<Option<TokenWrite>, Fault> {
    let found = read_at(ctx, path, &mut |s| machine.sleep(s))?;
    Ok(match found.state {
        ReadState::Absent => None,
        ReadState::Present => Some(TokenWrite {
            status: WriteStatus::Exists,
            token: found.token,
            detail: String::new(),
        }),
        ReadState::Malformed => Some(TokenWrite::of(WriteStatus::Malformed, found.detail)),
        ReadState::Unreadable => Some(TokenWrite::of(
            WriteStatus::FailedIo,
            format!("the existing token cannot be read: {}", found.detail),
        )),
    })
}

/// `_info_dir(git_dir)`: `<git_dir>/info`, made where missing; only a
/// FileExistsError is suppressed.
pub fn info_dir<F: TokenFs + ?Sized>(
    fs: &F,
    flavour: Flavour,
    git_dir: &str,
) -> Result<Result<String, OsError>, Fault> {
    let info = under(flavour, git_dir, "info");
    match fs.mkdir(info.as_bytes()) {
        Ok(()) => Ok(Ok(info)),
        Err(error) if error.class == PyErr::FileExists => Ok(Ok(info)),
        Err(error) => Ok(Err(caught(error)?)),
    }
}

/// `_create(path, body)`: an exclusive open, the writes and an fsync, then
/// the close. A file this call opened and could not finish is unlinked; an
/// open that fails unlinks nothing.
pub fn create<F: TokenFs + ?Sized>(
    fs: &F,
    machine: &mut dyn Machine,
    path: &[u8],
    body: &[u8],
) -> Result<Result<(), OsError>, Fault> {
    let mut file = match fs.create_excl(path, FILE_MODE) {
        Ok(file) => file,
        Err(error) => return Ok(Err(caught(error)?)),
    };
    let mut outcome = Ok(());
    let mut view = body;
    while !view.is_empty() {
        match fs.write_once(&mut file, view) {
            Ok(count) => view = view.get(count..).unwrap_or(&[]),
            Err(error) => {
                outcome = Err(error);
                break;
            }
        }
    }
    if outcome.is_ok() {
        let _ = fs.fsync_file(&mut file);
    }
    // The `finally` close: its error replaces the write's.
    if let Err(error) = fs.close_created(file) {
        outcome = Err(error);
    }
    match outcome {
        Ok(()) => Ok(Ok(())),
        Err(error) => {
            let error = caught(error)?;
            unlink_quietly(fs, machine, path)?;
            Ok(Err(error))
        }
    }
}

/// `_write_temp(info, body)`: `info/bettermemory.tmp-<12 random>`,
/// created, then the sweep by the filesystem's clock read on it.
pub fn write_temp<F: TokenFs + ?Sized>(
    ctx: &Ctx<'_, F>,
    machine: &mut dyn Machine,
    info: &str,
    body: &[u8],
) -> Result<Result<String, OsError>, Fault> {
    let needed = random_bytes_needed(TEMP_SUFFIX_LENGTH).unwrap_or(0);
    let suffix = random_crockford(TEMP_SUFFIX_LENGTH, &machine.urandom(needed))
        .map_err(|_| Fault::NotPorted)?;
    let temp = under(ctx.flavour, info, &format!("{TEMP_PREFIX}{suffix}"));
    if let Err(error) = create(ctx.fs, machine, temp.as_bytes(), body)? {
        return Ok(Err(error));
    }
    let now = fs_now(ctx.fs, temp.as_bytes());
    sweep(ctx.fs, ctx.flavour, info, now);
    Ok(Ok(temp))
}

/// `_fs_now(path)`: `os.utime(path)`, then `os.stat(path).st_mtime`;
/// None where either fails.
pub fn fs_now<F: TokenFs + ?Sized>(fs: &F, path: &[u8]) -> Option<f64> {
    fs.utime_now(path).ok()?;
    fs.stat_mtime(path).ok()
}

/// `_sweep(info, now)`: entries named `bettermemory.tmp-...` whose own
/// modification time is strictly below `now - 3600` are unlinked; None
/// removes nothing; every error is ignored.
pub fn sweep<F: TokenFs + ?Sized>(fs: &F, flavour: Flavour, info: &str, now: Option<f64>) {
    let Some(now) = now else { return };
    let cutoff = now - STALE_TEMP_SECONDS;
    let Ok(names) = fs.scandir_names(info.as_bytes()) else {
        return;
    };
    for name in names {
        if !name.starts_with(TEMP_PREFIX.as_bytes()) {
            continue;
        }
        let entry = hostpath::join(flavour, info.as_bytes(), &name);
        if fs.lstat_mtime(&entry).is_ok_and(|mtime| mtime < cutoff) {
            let _ = fs.unlink(&entry);
        }
    }
}

/// `_place(temp, path, value, body)`: the link; a refusal other than
/// EEXIST falls to the exclusive create; EEXIST of either reads the
/// winner's token; `fsync_dir` of `info` only after success.
pub fn place<F: TokenFs + ?Sized>(
    ctx: &Ctx<'_, F>,
    machine: &mut dyn Machine,
    temp: &str,
    path: &str,
    info: &str,
    value: String,
    body: &[u8],
) -> Result<TokenWrite, Fault> {
    let fs = ctx.fs;
    if let Err(error) = fs.link(temp.as_bytes(), path.as_bytes()) {
        if caught(error)?.class == PyErr::FileExists {
            return after_race(ctx, machine, path.as_bytes());
        }
        match create(fs, machine, path.as_bytes(), body)? {
            Ok(()) => {}
            Err(error) if error.class == PyErr::FileExists => {
                return after_race(ctx, machine, path.as_bytes());
            }
            Err(error) => return failed(fs, &error),
        }
    }
    if !machine.win32() {
        fs.fsync_dir(info.as_bytes());
    }
    Ok(TokenWrite {
        status: WriteStatus::Written,
        token: Some(value),
        detail: String::new(),
    })
}

/// `_after_race(path)`: the winner's token, read again while nothing,
/// an unreadable file, or one empty or cut short is found, in pauses from
/// 0.01 s doubling to 0.2 s, until 1.5 s by `time.monotonic` have passed.
pub fn after_race<F: TokenFs + ?Sized>(
    ctx: &Ctx<'_, F>,
    machine: &mut dyn Machine,
    path: &[u8],
) -> Result<TokenWrite, Fault> {
    let deadline = machine.monotonic() + RACE_READ_SECONDS;
    let mut pause = REREAD_DELAY_SECONDS;
    let found = loop {
        let found = existing(ctx, machine, path)?;
        let unfinished = match &found {
            None => true,
            Some(w) => {
                w.status == WriteStatus::FailedIo
                    || (w.status == WriteStatus::Malformed
                        && (w.detail == EMPTY || w.detail == CUT_SHORT))
            }
        };
        let left = deadline - machine.monotonic();
        if !unfinished || left <= 0.0 {
            break found;
        }
        machine.sleep(if left < pause { left } else { pause });
        pause = if RACE_PAUSE_MAX_SECONDS < 2.0 * pause {
            RACE_PAUSE_MAX_SECONDS
        } else {
            2.0 * pause
        };
    };
    Ok(found.unwrap_or_else(|| {
        TokenWrite::of(
            WriteStatus::FailedIo,
            "the token another writer placed was removed",
        )
    }))
}

struct Unlinker<'a, F: ?Sized> {
    fs: &'a F,
    machine: &'a mut dyn Machine,
    path: &'a [u8],
}

impl<F: TokenFs + ?Sized> Remover for Unlinker<'_, F> {
    type Error = OsError;
    fn unlink(&mut self) -> Result<(), OsError> {
        self.fs.unlink(self.path)
    }
    fn permission(&self, error: &OsError) -> bool {
        error.class == PyErr::Permission
    }
    fn sleep(&mut self, seconds: f64) {
        self.machine.sleep(seconds);
    }
}

/// `_unlink_quietly(path)` with no `still`: `unlink_retrying`, every
/// OSError swallowed (a ValueError is not).
pub fn unlink_quietly<F: TokenFs + ?Sized>(
    fs: &F,
    machine: &mut dyn Machine,
    path: &[u8],
) -> Result<(), Fault> {
    let win32 = machine.win32();
    let mut remover = Unlinker { fs, machine, path };
    match unlink_retrying_by(&mut remover, win32, None) {
        Err(error) => caught(error).map(|_| ()),
        Ok(()) => Ok(()),
    }
}

#[cfg(unix)]
fn host(path: &[u8]) -> Result<std::path::PathBuf, OsError> {
    use std::os::unix::ffi::OsStrExt;
    if path.contains(&0) {
        return Err(OsError::value());
    }
    Ok(std::ffi::OsStr::from_bytes(path).into())
}

#[cfg(windows)]
fn host(path: &[u8]) -> Result<std::path::PathBuf, OsError> {
    use std::os::windows::ffi::OsStringExt;
    match wtf8_to_wide(path) {
        Some(wide) if !wide.contains(&0) => Ok(std::ffi::OsString::from_wide(&wide).into()),
        _ => Err(OsError::value()),
    }
}

#[cfg(not(any(unix, windows)))]
fn host(path: &[u8]) -> Result<std::path::PathBuf, OsError> {
    match std::str::from_utf8(path) {
        Ok(text) if !text.contains('\0') => Ok(text.into()),
        _ => Err(OsError::value()),
    }
}

/// A Windows name (UTF-16 code units, perhaps with an unpaired
/// surrogate) as bytes: a pair becomes its code point, a lone surrogate
/// its own three-byte form (WTF-8), so a name `os.scandir` gives as a
/// `str` holding a surrogate keeps every code point and its prefix.
#[cfg_attr(not(windows), allow(dead_code))]
pub(crate) fn wide_to_wtf8(wide: &[u16]) -> Vec<u8> {
    let mut out = Vec::with_capacity(wide.len());
    let mut buffer = [0u8; 4];
    for unit in char::decode_utf16(wide.iter().copied()) {
        match unit {
            Ok(c) => out.extend_from_slice(c.encode_utf8(&mut buffer).as_bytes()),
            Err(lone) => {
                let code = u32::from(lone.unpaired_surrogate());
                out.push(0xE0 | (code >> 12) as u8);
                out.push(0x80 | ((code >> 6) & 0x3F) as u8);
                out.push(0x80 | (code & 0x3F) as u8);
            }
        }
    }
    out
}

/// The inverse of `wide_to_wtf8`: None for bytes that are not UTF-8 with
/// lone surrogates allowed (an encoded surrogate pair is refused, as
/// WTF-8 does, so every name has one spelling).
#[cfg_attr(not(windows), allow(dead_code))]
pub(crate) fn wtf8_to_wide(bytes: &[u8]) -> Option<Vec<u16>> {
    let mut out = Vec::with_capacity(bytes.len());
    let mut rest = bytes;
    let mut last_high = false;
    while !rest.is_empty() {
        match std::str::from_utf8(rest) {
            Ok(text) => {
                out.extend(text.encode_utf16());
                break;
            }
            Err(error) => {
                let good = error.valid_up_to();
                let text = std::str::from_utf8(&rest[..good]).ok()?;
                if good > 0 {
                    last_high = false;
                }
                out.extend(text.encode_utf16());
                let tail = &rest[good..];
                if tail.len() < 3 || tail[0] != 0xED || !(0xA0..=0xBF).contains(&tail[1]) {
                    return None;
                }
                if !(0x80..=0xBF).contains(&tail[2]) {
                    return None;
                }
                let code = 0xD000 | (u16::from(tail[1] & 0x3F) << 6) | u16::from(tail[2] & 0x3F);
                let high = code < 0xDC00;
                if last_high && !high {
                    return None;
                }
                out.push(code);
                last_high = high;
                rest = &tail[3..];
            }
        }
    }
    Some(out)
}

fn io(error: std::io::Error) -> OsError {
    Numbered::of_io(&error).error
}

impl TokenFs for StdFs {
    type Created = std::fs::File;

    fn mkdir(&self, path: &[u8]) -> Result<(), OsError> {
        std::fs::create_dir(host(path)?).map_err(io)
    }

    fn create_excl(&self, path: &[u8], mode: u32) -> Result<std::fs::File, OsError> {
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(mode);
        }
        #[cfg(not(unix))]
        let _ = mode;
        options.open(host(path)?).map_err(io)
    }

    fn write_once(&self, file: &mut std::fs::File, data: &[u8]) -> Result<usize, OsError> {
        std::io::Write::write(file, data).map_err(io)
    }

    fn fsync_file(&self, file: &mut std::fs::File) -> Result<(), OsError> {
        bettermemory_sys::fsync(file).map_err(io)
    }

    fn close_created(&self, file: std::fs::File) -> Result<(), OsError> {
        bettermemory_sys::close(file).map_err(io)
    }

    fn link(&self, src: &[u8], dst: &[u8]) -> Result<(), OsError> {
        std::fs::hard_link(host(src)?, host(dst)?).map_err(io)
    }

    fn replace(&self, src: &[u8], dst: &[u8]) -> Result<(), OsError> {
        std::fs::rename(host(src)?, host(dst)?).map_err(io)
    }

    #[cfg(unix)]
    fn fsync_dir(&self, path: &[u8]) {
        // No `O_DIRECTORY` without libc: a stat says a directory before
        // the read-only open, so no FIFO there is waited on.
        let Ok(path) = host(path) else { return };
        if !std::fs::metadata(&path).is_ok_and(|m| m.is_dir()) {
            return;
        }
        if let Ok(dir) = std::fs::File::open(path) {
            let _ = bettermemory_sys::fsync(&dir);
        }
    }

    #[cfg(not(unix))]
    fn fsync_dir(&self, _path: &[u8]) {}

    fn scandir_names(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        let mut names = Vec::new();
        for entry in std::fs::read_dir(host(path)?).map_err(io)? {
            let Ok(entry) = entry else { break };
            #[cfg(unix)]
            {
                use std::os::unix::ffi::OsStrExt;
                names.push(entry.file_name().as_bytes().to_vec());
            }
            #[cfg(windows)]
            {
                use std::os::windows::ffi::OsStrExt;
                let wide: Vec<u16> = entry.file_name().encode_wide().collect();
                names.push(wide_to_wtf8(&wide));
            }
            #[cfg(not(any(unix, windows)))]
            if let Ok(name) = entry.file_name().into_string() {
                names.push(name.into_bytes());
            }
        }
        Ok(names)
    }

    fn unlink(&self, path: &[u8]) -> Result<(), OsError> {
        std::fs::remove_file(host(path)?).map_err(|e| unlink_error(&e))
    }

    #[cfg(unix)]
    fn utime_now(&self, path: &[u8]) -> Result<(), OsError> {
        // `os.utime(path)` with no times is `utimensat(..., NULL)`: the
        // file system's own clock (the server's on NFS), and no open.
        use std::os::unix::ffi::OsStrExt;
        let name = std::ffi::CString::new(host(path)?.as_os_str().as_bytes())
            .map_err(|_| OsError::value())?;
        bettermemory_sys::utime_now(&name).map_err(io)
    }

    #[cfg(not(unix))]
    fn utime_now(&self, path: &[u8]) -> Result<(), OsError> {
        // `os.utime` with no times on Windows reads this machine's clock
        // (`GetSystemTimeAsFileTime`) and sets it with `SetFileTime` on a
        // handle opened for FILE_WRITE_ATTRIBUTES, as here.
        let mut options = std::fs::OpenOptions::new();
        #[cfg(windows)]
        {
            use std::os::windows::fs::OpenOptionsExt;
            options.access_mode(0x100);
        }
        #[cfg(not(windows))]
        options.write(true);
        let file = options.open(host(path)?).map_err(io)?;
        let now = std::time::SystemTime::now();
        let times = std::fs::FileTimes::new()
            .set_accessed(now)
            .set_modified(now);
        file.set_times(times).map_err(io)
    }

    #[cfg(unix)]
    fn owner_uid(&self, path: &[u8]) -> Result<Option<u32>, OsError> {
        use std::os::unix::fs::MetadataExt;
        Ok(Some(std::fs::metadata(host(path)?).map_err(io)?.uid()))
    }

    #[cfg(not(unix))]
    fn owner_uid(&self, path: &[u8]) -> Result<Option<u32>, OsError> {
        host(path)?;
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use super::{wide_to_wtf8, wtf8_to_wide};

    /// A Windows name with an unpaired surrogate keeps its prefix, so the
    /// sweep sees `bettermemory.tmp-` followed by U+D800 as Python's
    /// `os.scandir` does, and turns back into the same UTF-16 to unlink.
    #[test]
    fn a_lone_surrogate_name_keeps_its_prefix_and_round_trips() {
        let mut wide: Vec<u16> = "bettermemory.tmp-A".encode_utf16().collect();
        wide.push(0xD800);
        let bytes = wide_to_wtf8(&wide);
        assert!(bytes.starts_with(b"bettermemory.tmp-A"));
        assert_eq!(&bytes[18..], &[0xED, 0xA0, 0x80]);
        assert_eq!(wtf8_to_wide(&bytes), Some(wide));
        for name in [
            &[0xDC00u16, 0x41][..],
            &[0xD83D, 0xDE00, 0xDFFF],
            &[0x41],
            &[],
        ] {
            assert_eq!(wtf8_to_wide(&wide_to_wtf8(name)).as_deref(), Some(name));
        }
        // A surrogate pair spelled as two lone ones, and bytes that are not
        // UTF-8 at all, have no Windows name.
        assert_eq!(wtf8_to_wide(&[0xED, 0xA0, 0xBD, 0xED, 0xB8, 0x80]), None);
        assert_eq!(wtf8_to_wide(&[0xFF]), None);
        assert_eq!(wtf8_to_wide(&[0xED, 0xA0]), None);
    }
}
