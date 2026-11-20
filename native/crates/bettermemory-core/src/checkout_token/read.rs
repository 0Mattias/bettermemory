//! The checkout token's reads (`checkout_token.py` 353-363 and 753-848,
//! the module's second unit): `read`, `_read_at`, `_read_text`,
//! `_open_plain`, `_read_all` and `_clear_nonblock`, with the `_os_text`
//! an unreadable file's detail is worded by.
//!
//! Every OS call is the file system's ([`Fs`]): the non-blocking open
//! (`O_RDONLY | O_NONBLOCK`, links followed), `os.fstat`, one `os.read` at
//! a time, `os.close`, and for the retries `os.stat`, the blocking open
//! and `fcntl`'s two calls. A relative path is taken from the working
//! directory given ([`Ctx::at`]). The pause before a file found unfinished
//! is read again is an argument.
//!
//! An OS error's text is the file system's too ([`Fs::strerror`]): where
//! it gives none, or the error has no errno name, the read declines
//! ([`Fault::NotPorted`]) and never words a guess.

use super::{cut_short, malformed_detail, parse, token_path};
use crate::fsdata::{Fs, Kind, OsError, PyErr};
use crate::gitfile_rule::gitdir::Ctx;
use crate::gitfile_rule::probe::{path_missing, Raised};
use crate::pyvalue::PyStr;

/// `_MAX_BYTES`: a file longer than this is not a token file and is not
/// read further.
pub const MAX_BYTES: usize = 4096;
/// `_REREAD_DELAY_SECONDS`: the pause before a file found empty or cut
/// short is read again.
pub const REREAD_DELAY_SECONDS: f64 = 0.01;

/// `ReadState`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReadState {
    Present,
    Absent,
    Malformed,
    Unreadable,
}

impl ReadState {
    /// The state as the reference spells it.
    pub fn name(self) -> &'static str {
        match self {
            ReadState::Present => "present",
            ReadState::Absent => "absent",
            ReadState::Malformed => "malformed",
            ReadState::Unreadable => "unreadable",
        }
    }
}

/// `TokenRead`: what a read found at the token's name.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TokenRead {
    pub state: ReadState,
    pub token: Option<String>,
    pub detail: String,
}

impl TokenRead {
    fn of(state: ReadState, detail: impl Into<String>) -> TokenRead {
        TokenRead {
            state,
            token: None,
            detail: detail.into(),
        }
    }
}

/// Why a read gives no `TokenRead`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Fault {
    /// The reference raises: a ValueError (a NUL in the path, class
    /// [`PyErr::Value`]), which no `except OSError` catches, or the
    /// OSError of an `os.close` that failed in `_read_text`'s `finally`.
    Raised(OsError),
    /// Not ported: an OS error whose errno name or text the file system
    /// does not give, or a relative path under the Windows flavour.
    NotPorted,
}

/// What `_read_text` returns: the file's text (ASCII, as code points), or
/// the `TokenRead` that ends the read.
pub type Text = Result<PyStr, TokenRead>;

/// `read(git_dir)`: the token of the checkout whose git directory is
/// `git_dir`, read at `token_path(git_dir)`.
pub fn read<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    git_dir: &str,
    pause: &mut dyn FnMut(f64),
) -> Result<TokenRead, Fault> {
    read_at(ctx, token_path(git_dir, ctx.flavour).as_bytes(), pause)
}

/// `_read_at(path)`, `path` being what `os.fspath` gives of the
/// reference's `Path`. A text that is no token and is the start of one is
/// read once more after `pause(REREAD_DELAY_SECONDS)`; what the second
/// read finds is the answer.
pub fn read_at<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    path: &[u8],
    pause: &mut dyn FnMut(f64),
) -> Result<TokenRead, Fault> {
    let mut text = read_text(ctx, path)?;
    if text
        .as_ref()
        .is_ok_and(|t| parse(t).is_none() && cut_short(t))
    {
        pause(REREAD_DELAY_SECONDS);
        text = read_text(ctx, path)?;
    }
    let text = match text {
        Ok(text) => text,
        Err(ends) => return Ok(ends),
    };
    Ok(match parse(&text) {
        // A token is 26 ASCII characters.
        Some(value) => TokenRead {
            state: ReadState::Present,
            token: Some(value.iter().map(|&c| char::from(c as u8)).collect()),
            detail: String::new(),
        },
        None => TokenRead::of(ReadState::Malformed, malformed_detail(&text)),
    })
}

/// `_read_text(path)`: the file's text, or the `TokenRead` that ends the
/// read (absent, unreadable, or malformed for what is not a small ASCII
/// regular file).
pub fn read_text<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, path: &[u8]) -> Result<Text, Fault> {
    let fs = ctx.fs;
    let path = ctx.at(path).map_err(|_| Fault::NotPorted)?;
    let mut file = match fs.open_result(&path) {
        Ok(file) => file,
        Err(error) if error.class == PyErr::Value => return Err(Fault::Raised(error)),
        Err(error) if missing(&error) => return Ok(Err(TokenRead::of(ReadState::Absent, ""))),
        Err(error) if !retried(&error) => return Ok(Err(unreadable(fs, "open", &error)?)),
        Err(_) => match open_plain(fs, &path)? {
            Ok(file) => file,
            Err(ends) => return Ok(Err(ends)),
        },
    };
    let body = read_open(fs, &mut file);
    // `finally: os.close(fd)`: a close that fails raises out of the read,
    // whatever the body returned or raised.
    fs.close(file).map_err(Fault::Raised)?;
    let data = match body? {
        Ok(data) => data,
        Err(ends) => return Ok(Err(ends)),
    };
    if data.len() > MAX_BYTES {
        let detail = "larger than a token file";
        return Ok(Err(TokenRead::of(ReadState::Malformed, detail)));
    }
    if !data.is_ascii() {
        return Ok(Err(TokenRead::of(ReadState::Malformed, "not ASCII text")));
    }
    Ok(Ok(data.iter().map(|&b| u32::from(b)).collect()))
}

/// The `try` of `_read_text` over the open file: `os.fstat`'s kind, then
/// `_read_all`. A failure of either is worded `read:`.
fn read_open<F: Fs + ?Sized>(
    fs: &F,
    file: &mut F::File,
) -> Result<Result<Vec<u8>, TokenRead>, Fault> {
    let read = match fs.fstat_result(file) {
        Ok(stat) if stat.kind != Kind::Regular => {
            let detail = "not a regular file";
            return Ok(Err(TokenRead::of(ReadState::Malformed, detail)));
        }
        Ok(_) => read_all(fs, file),
        Err(error) => Err(error),
    };
    match read {
        Ok(data) => Ok(Ok(data)),
        Err(error) => Ok(Err(unreadable(fs, "read", &error)?)),
    }
}

/// `_open_plain(path)`: a blocking open for the retry of a non-blocking
/// one the file system refused, made only once a stat says a regular file
/// is there. An error of the stat or of the open is read alike: absent
/// where the path is missing, else unreadable, worded `open:`.
fn open_plain<F: Fs + ?Sized>(fs: &F, path: &[u8]) -> Result<Result<F::File, TokenRead>, Fault> {
    let opened = match fs.stat(path) {
        Ok(stat) if stat.kind != Kind::Regular => {
            let detail = "not a regular file";
            return Ok(Err(TokenRead::of(ReadState::Malformed, detail)));
        }
        Ok(_) => fs.open_blocking(path),
        Err(error) => Err(error),
    };
    match opened {
        Ok(file) => Ok(Ok(file)),
        Err(error) if error.class == PyErr::Value => Err(Fault::Raised(error)),
        Err(error) if missing(&error) => Ok(Err(TokenRead::of(ReadState::Absent, ""))),
        Err(error) => Ok(Err(unreadable(fs, "open", &error)?)),
    }
}

/// `_read_all(fd)`: up to a byte more than a token file may hold, one
/// `os.read` of what is still wanted at a time, until one answers empty.
/// A read refused with EINVAL or EIO is made once more with `O_NONBLOCK`
/// cleared; the second read's error, or the first's where the flag could
/// not be cleared, is raised.
fn read_all<F: Fs + ?Sized>(fs: &F, file: &mut F::File) -> Result<Vec<u8>, OsError> {
    let mut data = Vec::new();
    while data.len() <= MAX_BYTES {
        let want = MAX_BYTES + 1 - data.len();
        let chunk = match fs.read_once(file, want) {
            Ok(chunk) => chunk,
            Err(error) => {
                if !retried(&error) || !clear_nonblock(fs, file) {
                    return Err(error);
                }
                fs.read_once(file, want)?
            }
        };
        if chunk.is_empty() {
            break;
        }
        data.extend_from_slice(&chunk);
    }
    Ok(data)
}

/// `_clear_nonblock(fd)`: False where the platform has no `fcntl` or
/// either call fails; the flags are set to what `F_GETFL` gave without
/// `O_NONBLOCK`.
fn clear_nonblock<F: Fs + ?Sized>(fs: &F, file: &mut F::File) -> bool {
    let Some(nonblock) = fs.nonblock_flag() else {
        return false;
    };
    let Ok(flags) = fs.status_flags(file) else {
        return false;
    };
    fs.set_status_flags(file, flags & !nonblock).is_ok()
}

/// `path_missing(exc)` of an OSError.
fn missing(error: &OsError) -> bool {
    path_missing(Raised::from(*error))
}

/// `exc.errno in _RETRY_ERRNOS`: EINVAL or EIO, of an OSError.
fn retried(error: &OsError) -> bool {
    error.class != PyErr::Value && matches!(error.errno, Some("EINVAL" | "EIO"))
}

/// `TokenRead("unreadable", detail=f"{call}: {_os_text(exc)}")`. A
/// ValueError is no OSError: it raises.
fn unreadable<F: Fs + ?Sized>(fs: &F, call: &str, error: &OsError) -> Result<TokenRead, Fault> {
    if error.class == PyErr::Value {
        return Err(Fault::Raised(*error));
    }
    let text = os_text(fs, error).ok_or(Fault::NotPorted)?;
    Ok(TokenRead::of(
        ReadState::Unreadable,
        format!("{call}: {text}"),
    ))
}

/// `_os_text(exc)` of an error the OS raised: `exc.strerror`, a space and
/// the errno's name in brackets. None where the file system has no text
/// for the error or the error no errno name (the reference would word
/// both from the errno's number, which is not kept).
pub fn os_text<F: Fs + ?Sized>(fs: &F, error: &OsError) -> Option<String> {
    let name = error.errno?;
    let text = fs.strerror(error).filter(|text| !text.is_empty())?;
    Some(format!("{text} ({name})"))
}
