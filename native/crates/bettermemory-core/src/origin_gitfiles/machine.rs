//! Unit 2 of `origin_gitfiles`: the leaves that read the machine.
//!
//! - [`read_prefix`] (`_read_prefix`, reference lines 1331-1343): the first
//!   bytes of an open file, after a seek to its start;
//! - [`relative_to_root`] (335-361): a path relative to a working tree's
//!   root, by spelling and then by `os.path.samestat` of each parent;
//! - [`spelled_as_git`] (`_spelled_as_git`, 262-294): a root spelled as the
//!   kernel holds it, through `F_GETPATH` on macOS.
//!
//! Paths are the bytes of a POSIX path (`os.fsencode` of the `str`), read
//! as `PurePosixPath` reads them. The file system is the caller's
//! ([`Fs`]): `rewind`, `kernel_path` and `stat` are the only calls made.

use crate::fsdata::{samestat, Fs, PyErr, Refused};

/// `_read_prefix(fd, length)`: `os.lseek(fd, 0, SEEK_SET)`, then reads of
/// at most `length - size` bytes until `length` bytes are held or a read
/// gives none. The seek's and each read's exception propagate, by class:
/// a FIFO or socket raises at the seek (ESPIPE), a folder at the first
/// read (EISDIR, a length of 0 reads nothing and answers empty).
pub fn read_prefix<F: Fs + ?Sized>(
    fs: &F,
    file: &mut F::File,
    length: usize,
) -> Result<Vec<u8>, Refused> {
    fs.rewind(file)?;
    let mut out = Vec::new();
    while out.len() < length {
        let chunk = fs.read(file, length - out.len()).map_err(Refused::Raised)?;
        if chunk.is_empty() {
            break;
        }
        out.extend_from_slice(&chunk);
    }
    Ok(out)
}

/// A `PurePosixPath` as `pathlib` parses it: the root (empty, `/` or
/// exactly two slashes `//`) and the components, with empty and `.`
/// components dropped (`..` kept).
#[derive(Clone, Debug, PartialEq, Eq)]
struct Pure {
    root: Vec<u8>,
    tail: Vec<Vec<u8>>,
}

impl Pure {
    /// `PurePosixPath(os.fsdecode(path))` (`posixpath.splitroot`).
    fn parse(path: &[u8]) -> Pure {
        let lead = path.iter().take_while(|&&b| b == b'/').count();
        let root: &[u8] = match lead {
            0 => b"",
            2 => b"//",
            _ => b"/",
        };
        let tail = path[lead..]
            .split(|&b| b == b'/')
            .filter(|c| !c.is_empty() && *c != b".")
            .map(<[u8]>::to_vec)
            .collect();
        Pure {
            root: root.to_vec(),
            tail,
        }
    }

    /// `str()`: the root and the components joined by `/`, `.` where both
    /// are empty.
    fn text(root: &[u8], tail: &[Vec<u8>]) -> Vec<u8> {
        let mut out = root.to_vec();
        out.extend_from_slice(&tail.join(&b'/'));
        if out.is_empty() {
            out.push(b'.');
        }
        out
    }

    /// `self.relative_to(other)` (3.13, `walk_up` false): `other` equal to
    /// `self` or one of its `parents`, that is the same root and a prefix
    /// of the components; the rest of the components, `.` where none.
    fn relative_to(&self, other: &Pure) -> Option<Vec<u8>> {
        (self.root == other.root && self.tail.starts_with(&other.tail))
            .then(|| Pure::text(b"", &self.tail[other.tail.len()..]))
    }
}

/// `relative_to_root`'s ValueError, by its message (`str(exc)`, as bytes:
/// the paths in it are `str(Path)`, which `os.fsencode` gives back).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum NotRelative {
    /// The `ValueError` raised, by its message: `{root} cannot be read`,
    /// `{candidate} is not in {root}`, or `os.stat`'s own
    /// `stat: embedded null character in path`, which the reference does
    /// not catch.
    ValueError(Vec<u8>),
    /// `os.path.samestat` is not ported on this file system (`Fs::stat`
    /// gives no device or inode: Windows); a caller declines.
    NotPorted,
}

/// `os.stat`'s ValueError for a path holding a NUL.
const STAT_NUL: &[u8] = b"stat: embedded null character in path";

/// `relative_to_root(Path(candidate), Path(root))`: `candidate` relative
/// to `root` by spelling; else the nearest parent of `candidate` that is
/// the root's directory on disk (`samestat` of each `Path.stat`, an
/// OSError passed over) anchors it.
pub fn relative_to_root<F: Fs + ?Sized>(
    fs: &F,
    candidate: &[u8],
    root: &[u8],
) -> Result<Vec<u8>, NotRelative> {
    let cand = Pure::parse(candidate);
    let base = Pure::parse(root);
    if let Some(rel) = cand.relative_to(&base) {
        return Ok(rel);
    }
    let root_text = Pure::text(&base.root, &base.tail);
    let held = match fs.stat(&root_text) {
        Ok(held) => held,
        Err(e) if e.class == PyErr::Value => return Err(NotRelative::ValueError(STAT_NUL.into())),
        Err(_) => {
            let mut message = root_text;
            message.extend_from_slice(b" cannot be read");
            return Err(NotRelative::ValueError(message));
        }
    };
    // `candidate.parents`, nearest first, down to the root (or `.`): each
    // a prefix of the candidate's text, so none is built anew.
    let text = Pure::text(&cand.root, &cand.tail);
    let mut ends = Vec::with_capacity(cand.tail.len());
    let mut end = cand.root.len();
    for part in &cand.tail {
        ends.push(end);
        end += part.len() + 1;
    }
    for depth in (0..cand.tail.len()).rev() {
        // The separator before component `depth` is not part of its parent.
        let end = match depth {
            0 => cand.root.len(),
            _ => ends[depth] - 1,
        };
        let parent: &[u8] = if end == 0 { b"." } else { &text[..end] };
        let st = match fs.stat(parent) {
            Ok(st) => st,
            Err(e) if e.class == PyErr::Value => {
                return Err(NotRelative::ValueError(STAT_NUL.into()))
            }
            Err(_) => continue,
        };
        match samestat(&st, &held) {
            Some(true) => return Ok(Pure::text(b"", &cand.tail[depth..])),
            Some(false) => {}
            None => return Err(NotRelative::NotPorted),
        }
    }
    let mut message = text;
    message.extend_from_slice(b" is not in ");
    message.extend_from_slice(&root_text);
    Err(NotRelative::ValueError(message))
}

/// `_spelled_as_git(Path(root))` where `darwin` is `sys.platform ==
/// "darwin"`. Elsewhere (Linux, Windows) the reference answers `root`
/// itself and reads nothing: `str(Path(root))`. On macOS: `os.open` of
/// the root, waiting on a FIFO as `O_NONBLOCK` absent does (an OSError answers None; a NUL raises ValueError, which the
/// reference does not catch), then `F_GETPATH` (an OSError, a path of
/// 1,024 bytes or more among them, answers None), read up to its first
/// NUL and spelled as a `Path`. `Refused::NotOffered` where `fs` offers
/// no `kernel_path`.
pub fn spelled_as_git<F: Fs + ?Sized>(
    fs: &F,
    root: &[u8],
    darwin: bool,
) -> Result<Option<Vec<u8>>, Refused> {
    let given = Pure::parse(root);
    let text = Pure::text(&given.root, &given.tail);
    if !darwin {
        return Ok(Some(text));
    }
    let mut file = match fs.open_waiting(&text) {
        Ok(file) => file,
        Err(PyErr::Value) => return Err(Refused::Raised(PyErr::Value)),
        Err(_) => return Ok(None),
    };
    match fs.kernel_path(&mut file) {
        Ok(raw) => {
            let end = raw.iter().position(|&b| b == 0).unwrap_or(raw.len());
            let spelled = Pure::parse(&raw[..end]);
            Ok(Some(Pure::text(&spelled.root, &spelled.tail)))
        }
        Err(Refused::Raised(_)) => Ok(None),
        Err(Refused::NotOffered) => Err(Refused::NotOffered),
    }
}
