//! The file reads, the object format and the alternates walk of
//! `src/bettermemory/gitobjects.py`: unit 3 of its port (see the README's
//! "Git objects read from a repository's files").
//!
//! [`ObjectReader`] is the loaded state of the reference's `ObjectReader`
//! (`__init__`, `_load`, `_object_format`, `_object_dirs`,
//! `_link_alternates`, `_list_new`, `_rescan`, `_open`, `_read_whole`)
//! and the questions it answers from that state alone (`object_dirs`,
//! `complete`, `evicted`). The machine reaches it through `fsdata`'s
//! [`Fs`], the working directory `os.path.realpath` would read is an
//! argument, and the git on PATH the object format asks is a
//! [`GitFacts`] given at construction. [`FsHandle`] is an open file of
//! [`Fs`] behind unit 2's [`Handle`], for the packed reads of unit 4.

use std::collections::HashSet;

use super::stream::Handle;
use super::{alternate_entries, parse_index, PackIndex};
use crate::fsdata::realpath::{realpath, Unresolved};
use crate::fsdata::{Flavour, Fs, Kind, PyErr, Refused, StatResult};
use crate::gitfile_rule::format::{object_format, GitFacts};
use crate::gitfile_rule::hostpath::{isabs, join, normpath};

/// `_ALT_DEPTH`: the deepest nesting at which an alternates file is read.
pub const ALT_DEPTH: usize = 5;
/// `_SF_DATALESS`: `st_flags` of a file a provider fetches on first read.
pub const SF_DATALESS: u32 = 0x4000_0000;
/// `_RECALL_ON_DATA_ACCESS`: Windows' attribute of the same.
pub const RECALL_ON_DATA_ACCESS: u32 = 0x0040_0000;

/// Bytes asked of each read of `readall`'s loop.
const READ_PIECE: usize = 1 << 16;

/// Why a question of the reader gave no answer.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stop {
    /// The reference raises this out of the question: `ValueError` from
    /// `os.path.realpath` of an alternates entry holding a NUL.
    Raised(PyErr),
    /// The Windows flavour's `realpath`, which is not ported: a caller
    /// declines.
    NotPorted,
}

/// `_NAME_BYTES[name]`: an object name's length in bytes by its format.
fn name_bytes(name: &[u8]) -> Option<usize> {
    match name {
        b"sha1" => Some(20),
        b"sha256" => Some(32),
        _ => None,
    }
}

/// `_is_evicted(st)`.
pub fn is_evicted(st: &StatResult) -> bool {
    st.flags & SF_DATALESS != 0 || st.file_attributes & RECALL_ON_DATA_ACCESS != 0
}

/// One repository's object files as the reference's `ObjectReader` loads
/// them.
pub struct ObjectReader<'a, F: Fs + ?Sized> {
    fs: &'a F,
    flavour: Flavour,
    cwd: Vec<u8>,
    git: GitFacts,
    git_dir: Vec<u8>,
    loaded: bool,
    /// `_hash_len`.
    pub hash_len: Option<usize>,
    dirs: Vec<Vec<u8>>,
    /// `_packs`: the pack indexes loaded, in the order listed.
    pub packs: Vec<PackIndex>,
    listed: HashSet<Vec<u8>>,
    /// `_unreadable`.
    pub unreadable: bool,
    evicted: Vec<Vec<u8>>,
    /// The paths of `evicted`, for membership without a scan (the
    /// reference's dict).
    evicted_set: HashSet<Vec<u8>>,
}

impl<'a, F: Fs + ?Sized> ObjectReader<'a, F> {
    /// `ObjectReader(git_dir)`: nothing is read until a question is asked.
    pub fn new(
        fs: &'a F,
        flavour: Flavour,
        cwd: &[u8],
        git: GitFacts,
        git_dir: &[u8],
    ) -> ObjectReader<'a, F> {
        ObjectReader {
            fs,
            flavour,
            cwd: cwd.to_vec(),
            git,
            git_dir: git_dir.to_vec(),
            loaded: false,
            hash_len: None,
            dirs: Vec::new(),
            packs: Vec::new(),
            listed: HashSet::new(),
            unreadable: false,
            evicted: Vec::new(),
            evicted_set: HashSet::new(),
        }
    }

    /// `object_dirs`.
    pub fn object_dirs(&mut self) -> Result<Vec<Vec<u8>>, Stop> {
        self.load()?;
        Ok(self.dirs.clone())
    }

    /// `complete`.
    pub fn complete(&mut self) -> Result<bool, Stop> {
        self.load()?;
        Ok(self.hash_len.is_some()
            && !self.dirs.is_empty()
            && !self.unreadable
            && self.evicted.is_empty())
    }

    /// `evicted`: the files passed over, in the order met. It loads
    /// nothing.
    pub fn evicted(&self) -> Vec<Vec<u8>> {
        self.evicted.clone()
    }

    /// The pack index paths listed so far (`_listed`), sorted.
    pub fn listed(&self) -> Vec<Vec<u8>> {
        let mut out: Vec<Vec<u8>> = self.listed.iter().cloned().collect();
        out.sort();
        out
    }

    /// `_load`: `_loaded` is set first, so a question after a raise
    /// answers from the state the raise left.
    pub fn load(&mut self) -> Result<(), Stop> {
        if self.loaded {
            return Ok(());
        }
        self.loaded = true;
        self.hash_len = self.object_format()?;
        self.dirs = self.object_dirs_walk()?;
        if self.hash_len.is_some() {
            self.packs = self.list_new()?;
        }
        Ok(())
    }

    /// `_object_format`.
    fn object_format(&mut self) -> Result<Option<usize>, Stop> {
        let path = join(self.flavour, &self.git_dir, b"config");
        Ok(match self.read_whole(&path)? {
            None if self.evicted_set.contains(&path) => None,
            None => Some(20),
            Some(text) => object_format(&text, &self.git).and_then(|n| name_bytes(&n)),
        })
    }

    /// `os.path.isdir(path)`.
    fn isdir(&self, path: &[u8]) -> bool {
        matches!(self.fs.stat(path), Ok(st) if st.kind == Kind::Directory)
    }

    /// `os.path.isfile(path)`.
    fn isfile(&self, path: &[u8]) -> bool {
        matches!(self.fs.stat(path), Ok(st) if st.kind == Kind::Regular)
    }

    /// `os.path.realpath(path)`, its ValueError raised.
    fn realpath(&self, path: &[u8]) -> Result<Vec<u8>, Stop> {
        match realpath(self.fs, self.flavour, path, &self.cwd) {
            Ok(real) => Ok(real),
            Err(Unresolved::NotPorted) => Err(Stop::NotPorted),
            Err(Unresolved::Raised(class)) => Err(Stop::Raised(class)),
        }
    }

    /// `_dir_key(path)`: normcase is the identity on POSIX; an OSError
    /// of `realpath` keys the path itself (non-strict `realpath` raises
    /// none), its ValueError goes through.
    fn dir_key(&self, path: &[u8]) -> Result<Vec<u8>, Stop> {
        match self.realpath(path) {
            Err(Stop::Raised(class)) if class != PyErr::Value => Ok(path.to_vec()),
            other => other,
        }
    }

    /// `_object_dirs`.
    fn object_dirs_walk(&mut self) -> Result<Vec<Vec<u8>>, Stop> {
        let main = join(self.flavour, &self.git_dir, b"objects");
        if !self.isdir(&main) {
            return Ok(Vec::new());
        }
        let mut seen = HashSet::new();
        seen.insert(self.dir_key(&main)?);
        let mut out = vec![main.clone()];
        self.link_alternates(&main, 0, &mut out, &mut seen)?;
        Ok(out)
    }

    /// `_link_alternates`, recursing as the reference does: at most
    /// `ALT_DEPTH + 1` frames deep.
    fn link_alternates(
        &mut self,
        objdir: &[u8],
        depth: usize,
        out: &mut Vec<Vec<u8>>,
        seen: &mut HashSet<Vec<u8>>,
    ) -> Result<(), Stop> {
        if depth > ALT_DEPTH {
            return Ok(());
        }
        let info = join(self.flavour, objdir, b"info");
        let Some(text) = self.read_whole(&join(self.flavour, &info, b"alternates"))? else {
            return Ok(());
        };
        let base = match self.realpath(objdir) {
            Err(Stop::Raised(class)) if class != PyErr::Value => return Ok(()),
            other => other?,
        };
        for entry in alternate_entries(&text) {
            let path = if isabs(self.flavour, &entry) {
                normpath(self.flavour, &entry)
            } else {
                normpath(self.flavour, &join(self.flavour, &base, &entry))
            };
            let key = self.dir_key(&path)?;
            if seen.contains(&key) || !self.isdir(&path) {
                continue;
            }
            seen.insert(key);
            out.push(path.clone());
            self.link_alternates(&path, depth + 1, out, seen)?;
        }
        Ok(())
    }

    /// `_list_new`.
    pub fn list_new(&mut self) -> Result<Vec<PackIndex>, Stop> {
        let Some(hash_len) = self.hash_len else {
            return Ok(Vec::new());
        };
        let mut new = Vec::new();
        for objdir in self.dirs.clone() {
            let pack_dir = join(self.flavour, &objdir, b"pack");
            let Ok(mut names) = self.fs.listdir(&pack_dir) else {
                continue;
            };
            names.sort();
            for name in names {
                if !name.ends_with(b".idx") {
                    continue;
                }
                let idx_path = join(self.flavour, &pack_dir, &name);
                if !self.listed.insert(idx_path.clone()) {
                    continue;
                }
                let mut pack_path = idx_path[..idx_path.len() - 4].to_vec();
                pack_path.extend_from_slice(b".pack");
                if !self.isfile(&pack_path) {
                    continue;
                }
                let index = self
                    .read_whole(&idx_path)?
                    .and_then(|data| parse_index(data, pack_path, hash_len));
                match index {
                    Some(index) => new.push(index),
                    None => self.unreadable = true,
                }
            }
        }
        Ok(new)
    }

    /// `_rescan`.
    pub fn rescan(&mut self) -> Result<usize, Stop> {
        let new = self.list_new()?;
        let count = new.len();
        self.packs.extend(new);
        Ok(count)
    }

    /// `_open(path)`: the regular file at `path`, opened without waiting
    /// on a FIFO, or None; an evicted file is recorded, not opened. The
    /// ValueError of `os.stat` (a NUL in the path) goes through.
    pub fn open(&mut self, path: &[u8]) -> Result<Option<F::File>, Stop> {
        let before = match self.fs.stat(path) {
            Ok(before) => before,
            Err(e) if e.class == PyErr::Value => return Err(Stop::Raised(PyErr::Value)),
            Err(_) => return Ok(None),
        };
        if before.kind != Kind::Regular {
            return Ok(None);
        }
        if is_evicted(&before) {
            if self.evicted_set.insert(path.to_vec()) {
                self.evicted.push(path.to_vec());
            }
            return Ok(None);
        }
        let Ok(mut file) = self.fs.open(path) else {
            return Ok(None);
        };
        let regular = matches!(self.fs.fstat(&mut file), Ok(st) if st.kind == Kind::Regular);
        if !regular {
            let _ = self.fs.close(file);
            return Ok(None);
        }
        Ok(Some(file))
    }

    /// `_read_whole(path)`: `readall` of what `_open` gives, None where a
    /// read raises.
    pub fn read_whole(&mut self, path: &[u8]) -> Result<Option<Vec<u8>>, Stop> {
        let Some(mut file) = self.open(path)? else {
            return Ok(None);
        };
        let mut out = Vec::new();
        let read = loop {
            match self.fs.read_once(&mut file, READ_PIECE) {
                Ok(piece) if piece.is_empty() => break true,
                Ok(piece) => out.extend_from_slice(&piece),
                // `readall` of a non-blocking read with nothing yet
                // answers what it has, None where that is nothing.
                Err(e) if e.class == PyErr::BlockingIO => break !out.is_empty(),
                Err(_) => break false,
            }
        };
        let _ = self.fs.close(file);
        Ok(read.then_some(out))
    }
}

/// An open file of [`Fs`] as a [`Handle`]: `seek` is [`Fs::seek`], `read`
/// one [`Fs::read_once`], a non-blocking read with nothing answering
/// empty as `FileIO.read`'s None does.
pub struct FsHandle<'a, F: Fs + ?Sized> {
    pub fs: &'a F,
    pub file: F::File,
}

impl<F: Fs + ?Sized> Handle for FsHandle<'_, F> {
    fn seek(&mut self, offset: u64) -> Result<(), PyErr> {
        self.fs.seek(&mut self.file, offset).map_err(|e| match e {
            Refused::Raised(class) => class,
            Refused::NotOffered => PyErr::Os,
        })
    }

    fn read(&mut self, n: usize) -> Result<Vec<u8>, PyErr> {
        match self.fs.read_once(&mut self.file, n) {
            Ok(piece) => Ok(piece),
            Err(e) if e.class == PyErr::BlockingIO => Ok(Vec::new()),
            Err(e) => Err(e.class),
        }
    }
}
