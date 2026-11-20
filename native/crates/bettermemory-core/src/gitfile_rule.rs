//! The rule a `.git` entry is followed by (`gitfile_rule.py`), unit 1:
//! the leaves every later unit reads through (native/README.md, "The rule
//! a .git entry is followed by").
//!
//! - the bounds (`_GITFILE_LIMIT`, `_REF_LIMIT`, `_READ_CHUNK`,
//!   `_HEADREF_READ`, `_SPACE`) and `_HEX_PREFIX` ([`headref_content`]);
//! - what a `gitdir:` file's bytes name ([`parse_gitfile`], lines
//!   100-114), read through [`Fs`] ([`gitfile_target`], 90-97), and a small
//!   text file's content ([`small_text`], 117-127);
//! - a repository's format from its config's bytes ([`format`]:
//!   `object_format` 398-419, `_judge_format` 422-515 with `_take`,
//!   `_dies` and `_extension_reads`), the git on PATH taken as data;
//! - the probe's reading of an error as nothing at a path ([`probe`],
//!   `_location_probe.py` 60-106);
//! - the bounded reads ([`bounded`], `location_reads.py`), the time bound
//!   arriving as data;
//! - the host's `os.path` for the parse, as data ([`hostpath`]).
//!
//! No clock, timer or thread decides an answer here, and every file is
//! read through [`Fs`].
//!
//! The later units are modules of this one: git's reading of a git
//! directory ([`gitdir`]) and the identity helpers ([`identity`]), the
//! format from files and the trust of a `commondir` ([`trust`]), the pin
//! records and the follow ([`follow`]), the judge and discovery
//! ([`discovery`]). With them the module is whole on POSIX.

pub mod bounded;
pub mod discovery;
pub mod follow;
pub mod format;
pub mod gitdir;
pub mod hostpath;
pub mod identity;
pub mod probe;
pub mod trust;

use crate::fsdata::{Flavour, Fs, Kind, PyErr};

use gitdir::{Ctx, Failure};

/// `_GITFILE_LIMIT`: git refuses a `.git` file over 1 MiB.
pub const GITFILE_LIMIT: usize = 1 << 20;
/// `_REF_LIMIT`: a ref file or a `commondir` file's bound.
pub const REF_LIMIT: usize = 1 << 16;
/// `_READ_CHUNK`: the size of each read of a bounded file.
pub const READ_CHUNK: usize = 1 << 16;
/// `_HEADREF_READ`: git's `validate_headref` reads 255 bytes of HEAD.
pub const HEADREF_READ: usize = 255;
/// `_SPACE`: git's `isspace` (space, tab, LF, CR).
pub const SPACE: &[u8] = b" \t\n\r";

/// `_headref_content`: git's `validate_headref` of a HEAD whose leading
/// bytes are `content` (255 of them read): a `ref:` line naming something
/// under `refs/`, or 40 hex digits of either case (`_HEX_PREFIX`).
pub fn headref_content(content: &[u8]) -> bool {
    let content = &content[..content.len().min(HEADREF_READ)];
    if let Some(rest) = content.strip_prefix(b"ref:") {
        let start = rest
            .iter()
            .position(|b| !SPACE.contains(b))
            .unwrap_or(rest.len());
        if rest[start..].starts_with(b"refs/") {
            return true;
        }
    }
    content.len() >= 40 && content[..40].iter().all(u8::is_ascii_hexdigit)
}

/// `str.strip()` with no argument: Python's whitespace (`str.isspace`) off
/// both ends.
fn py_strip(text: &str) -> &str {
    text.trim_matches(|c: char| crate::unicode::is_space(c as u32))
}

/// `data.decode("utf-8", errors="replace").strip()`.
pub(crate) fn stripped_text(data: &[u8]) -> String {
    py_strip(&String::from_utf8_lossy(data)).to_owned()
}

/// `_parse_gitfile(data, dotgit)`: the directory the bytes `data` of the
/// `.git` file `dotgit` name after `gitdir:`, taken from the file's folder
/// when relative and normalized by the host's `os.path` (`flavour`); None
/// where they are not such a file. The answer is the path's bytes (the
/// reference's `str` encoded as the caller's path was, UTF-8 with any
/// byte it did not decode kept).
pub fn parse_gitfile(data: &[u8], dotgit: &[u8], flavour: Flavour) -> Option<Vec<u8>> {
    if data.len() > GITFILE_LIMIT {
        return None;
    }
    let text = stripped_text(data);
    let rest = text.strip_prefix("gitdir:")?;
    let target = py_strip(rest);
    if target.is_empty() {
        return None;
    }
    let target = target.as_bytes();
    let joined = if hostpath::isabs(flavour, target) {
        target.to_vec()
    } else {
        hostpath::join(flavour, &hostpath::dirname(flavour, dotgit), target)
    };
    Some(hostpath::normpath(flavour, &joined))
}

/// The bytes of the file at `path` read as the reference's
/// `open(path, "rb").read(GITFILE_LIMIT + 1)` reads them, or None where
/// that raises an OSError (a ValueError rises: [`Failure::Value`]); also
/// None where the file is longer than
/// [`GITFILE_LIMIT`].
///
/// The reference's `open` blocks on a FIFO until a writer comes; this
/// open does not block ([`Fs::open`]), and a FIFO or a socket reads as
/// None, as one that cannot be read (native/README.md). A directory is
/// None, as its `open` raises IsADirectoryError (PermissionError on
/// Windows). At most [`GITFILE_LIMIT`] + 1 bytes are read.
///
/// The path goes through [`Ctx::at`]: a relative one is taken from the
/// context's working directory, never the process's own (declined on the
/// Windows flavour).
fn read_small<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, path: &[u8]) -> Result<Option<Vec<u8>>, Failure> {
    let path = ctx.at(path)?;
    match ctx.fs.open(&path) {
        // `open` raises ValueError (a NUL in the path), which neither
        // `_gitfile_target` nor `_small_text` catches: it rises.
        Err(PyErr::Value) => Err(Failure::Value),
        Err(_) => Ok(None),
        Ok(file) => Ok(read_open(ctx.fs, file)),
    }
}

fn read_open<F: Fs + ?Sized>(fs: &F, mut file: F::File) -> Option<Vec<u8>> {
    let stat = fs.fstat(&mut file).ok()?;
    if matches!(stat.kind, Kind::Directory | Kind::Fifo | Kind::Socket) {
        return None;
    }
    let mut out: Vec<u8> = Vec::new();
    while out.len() <= GITFILE_LIMIT {
        let room = GITFILE_LIMIT + 1 - out.len();
        let chunk = fs.read(&mut file, room).ok()?;
        if chunk.is_empty() {
            break;
        }
        out.extend_from_slice(&chunk[..chunk.len().min(room)]);
    }
    if out.len() > GITFILE_LIMIT {
        return None;
    }
    Some(out)
}

/// `_gitfile_target(dotgit)`: the directory the `gitdir:` file `dotgit`
/// names ([`parse_gitfile`]), or None where it cannot be read or names
/// none.
pub fn gitfile_target<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    dotgit: &[u8],
) -> Result<Option<Vec<u8>>, Failure> {
    Ok(read_small(ctx, dotgit)?.and_then(|data| parse_gitfile(&data, dotgit, ctx.flavour)))
}

/// `_small_text(path)`: a small text file's content, decoded with
/// `replace` and stripped; None where it cannot be read or holds more than
/// [`GITFILE_LIMIT`] bytes.
pub fn small_text<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    path: &[u8],
) -> Result<Option<String>, Failure> {
    Ok(read_small(ctx, path)?.map(|data| stripped_text(&data)))
}
