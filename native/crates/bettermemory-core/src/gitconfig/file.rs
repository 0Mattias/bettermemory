//! One config file read from a file system ([`Fs`]), as the reference
//! reads it: [`config_bytes`] (`_config_bytes` and `_accessible`,
//! `gitconfig.py` 292-356), the bytes an include or a tree's own file
//! gives, and [`read_git_config`] (1564-1672), a file read whole with no
//! include, its entries or the reason they are not known.
//!
//! The 2 MiB bound ([`FORMAT_LIMIT`]) is applied while reading, before
//! any parse: at most the limit plus one byte is ever read. The
//! reference's `_FORMAT_MEMO` is left out: it is keyed by a digest of the
//! bytes read and only spares a parse, so it answers what a parse would.

use super::{git_entries, Entry, FORMAT_LIMIT};
use crate::fsdata::{FileId, Fs, Kind, PyErr};

/// Why git dies reading a file (`_Dies` at lines 317, 323 and 335).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Dies {
    /// `git cannot read {path} ({Exc})`: the open, the stat or a read
    /// raised.
    CannotRead { path: Vec<u8>, error: PyErr },
    /// `{path} is a folder, which git cannot read as a config`.
    Folder { path: Vec<u8> },
}

impl Dies {
    /// The reason's text, as UTF-8 bytes around the path's own bytes.
    pub fn why(&self) -> Vec<u8> {
        match self {
            Dies::CannotRead { path, error } => worded(
                b"git cannot read ",
                path,
                format!(" ({})", error.name()).as_bytes(),
            ),
            Dies::Folder { path } => worded(
                b"",
                path,
                b" is a folder, which git cannot read as a config",
            ),
        }
    }
}

fn worded(head: &[u8], path: &[u8], tail: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(head.len() + path.len() + tail.len());
    out.extend_from_slice(head);
    out.extend_from_slice(path);
    out.extend_from_slice(tail);
    out
}

/// `_config_bytes`' answer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ConfigBytes {
    /// None: nothing there (or, gentle, no permission); git skips it.
    Skipped,
    /// The bytes, with the file's identity where the OS gives one.
    Read {
        data: Vec<u8>,
        identity: Option<FileId>,
    },
    /// git dies at it.
    Dies(Dies),
    /// `_Overflow(path)`: git reads what is not read here (a FIFO, a
    /// socket, a file over the bound); the read is incomplete.
    Overflow { path: Vec<u8> },
}

/// `_config_bytes(path, identity, gentle=...)`: the bytes of a config
/// file read whole within [`FORMAT_LIMIT`].
pub fn config_bytes<F: Fs>(fs: &F, path: &[u8], gentle: bool) -> ConfigBytes {
    let mut file = match fs.open(path) {
        Ok(file) => file,
        Err(error) => {
            if (error.missing() && !fs.access_read(path)) || (gentle && error == PyErr::Permission)
            {
                return ConfigBytes::Skipped;
            }
            return ConfigBytes::Dies(Dies::CannotRead {
                path: path.to_vec(),
                error,
            });
        }
    };
    let cannot = |error| {
        ConfigBytes::Dies(Dies::CannotRead {
            path: path.to_vec(),
            error,
        })
    };
    let stat = match fs.fstat(&mut file) {
        Ok(stat) => stat,
        Err(error) => return cannot(error),
    };
    match stat.kind {
        Kind::Directory => {
            return ConfigBytes::Dies(Dies::Folder {
                path: path.to_vec(),
            })
        }
        Kind::Fifo | Kind::Socket => {
            return ConfigBytes::Overflow {
                path: path.to_vec(),
            }
        }
        _ => {}
    }
    let data = match read_bounded(fs, &mut file) {
        Ok(data) => data,
        Err(error) => return cannot(error),
    };
    if data.len() > FORMAT_LIMIT {
        return ConfigBytes::Overflow {
            path: path.to_vec(),
        };
    }
    ConfigBytes::Read {
        data,
        identity: stat.identity,
    }
}

/// The reference's read loop: reads of what is left to the limit plus
/// one byte, until one comes back empty.
fn read_bounded<F: Fs>(fs: &F, file: &mut F::File) -> Result<Vec<u8>, PyErr> {
    let mut out: Vec<u8> = Vec::new();
    while out.len() <= FORMAT_LIMIT {
        let chunk = fs.read(file, FORMAT_LIMIT + 1 - out.len())?;
        if chunk.is_empty() {
            break;
        }
        // A file system that hands back more than asked is cut to it.
        let room = FORMAT_LIMIT + 1 - out.len();
        out.extend_from_slice(&chunk[..chunk.len().min(room)]);
    }
    Ok(out)
}

/// Why `read_git_config` knows no entries (lines 1625-1661).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Unread {
    /// `{path} could not be read ({Exc})`.
    CouldNotRead(PyErr),
    /// `{path} is not a regular file`.
    NotRegular,
    /// `{path} is larger than 2 MiB`, by the stat's size or the bytes read.
    TooLarge,
    /// `{path} is a config git cannot parse`.
    Unparsed,
}

/// `read_git_config`'s answer (`GitConfigRead`, line 1564).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GitConfigRead {
    pub entries: Vec<Entry>,
    /// The reason with the path it names, None where the entries stand.
    pub why: Option<(Vec<u8>, Unread)>,
}

impl GitConfigRead {
    fn refused(path: &[u8], why: Unread) -> GitConfigRead {
        GitConfigRead {
            entries: Vec::new(),
            why: Some((path.to_vec(), why)),
        }
    }

    /// The reason's text, as the reference words it.
    pub fn why_text(&self) -> Option<Vec<u8>> {
        let (path, why) = self.why.as_ref()?;
        let tail = match why {
            Unread::CouldNotRead(error) => format!(" could not be read ({})", error.name()),
            Unread::NotRegular => " is not a regular file".to_owned(),
            Unread::TooLarge => format!(" is larger than {} MiB", FORMAT_LIMIT >> 20),
            Unread::Unparsed => " is a config git cannot parse".to_owned(),
        };
        Some(worded(b"", path, tail.as_bytes()))
    }
}

/// `read_git_config(path, names)`: the entries of one file, no include
/// followed; with `names`, only those named (a name ending in `.` keeps
/// every one it starts).
pub fn read_git_config<F: Fs>(fs: &F, path: &[u8], names: Option<&[Vec<u8>]>) -> GitConfigRead {
    let mut file = match fs.open(path) {
        Ok(file) => file,
        Err(error) if error.missing() => {
            return GitConfigRead {
                entries: Vec::new(),
                why: None,
            }
        }
        Err(error) => return GitConfigRead::refused(path, Unread::CouldNotRead(error)),
    };
    let stat = match fs.fstat(&mut file) {
        Ok(stat) => stat,
        Err(error) => return GitConfigRead::refused(path, Unread::CouldNotRead(error)),
    };
    if stat.kind != Kind::Regular {
        return GitConfigRead::refused(path, Unread::NotRegular);
    }
    if stat.size > FORMAT_LIMIT as u64 {
        return GitConfigRead::refused(path, Unread::TooLarge);
    }
    let data = match read_bounded(fs, &mut file) {
        Ok(data) => data,
        Err(error) => return GitConfigRead::refused(path, Unread::CouldNotRead(error)),
    };
    if data.len() > FORMAT_LIMIT {
        return GitConfigRead::refused(path, Unread::TooLarge);
    }
    let Ok(entries) = git_entries(&data) else {
        return GitConfigRead::refused(path, Unread::Unparsed);
    };
    let entries = match names {
        None => entries,
        Some(names) => {
            let starts: Vec<&Vec<u8>> = names.iter().filter(|n| n.ends_with(b".")).collect();
            entries
                .into_iter()
                .filter(|e| {
                    let name = e.name();
                    names.contains(&name) || starts.iter().any(|s| name.starts_with(s))
                })
                .collect()
        }
    };
    GitConfigRead { entries, why: None }
}
