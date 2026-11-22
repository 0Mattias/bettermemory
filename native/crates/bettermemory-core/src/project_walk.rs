//! `project_file.py`, unit 3: the upward walk (`Walk` and its
//! properties, `above_start`, `find`, `_walk`, `_look`) and `write`.
//!
//! Effects are arguments. The walk reads the file system through [`Fs`]
//! (`os.stat`, links followed, as `_look` calls it) and makes paths
//! absolute through a [`Process`] (`_absolute` is `os.path.abspath`). The
//! per-file parse (`_parse_stat`, unit 2) is a closure the caller hands
//! in. `write` reaches the disk through [`WriteEffects`] and takes the
//! random bytes `mint_id` reads.
//!
//! A `Path` is its text. `Path.parent` and `Path.is_relative_to` are
//! lexical, as pathlib has them; two paths are equal as `PurePath`
//! compares them: the text itself under POSIX, `str.lower` of it under
//! Windows.

use crate::fsdata::{Flavour, Fs, OsError, PyErr, StatResult};
use crate::gitfile_rule::follow::fsencode;
use crate::gitfile_rule::hostpath::splitroot;
use crate::gitfile_rule::hostpath::text::{abspath, join, Process, Raised};
use crate::gitfile_rule::probe::path_missing;
use crate::project_file::{
    bad_name, directory, mint_id, render, Above, ErrorReason, Found, ProjectFile, ProjectFileError,
    PROJECT_FILE_NAME,
};
use crate::pyvalue::PyStr;
use crate::unicode;

/// `_FILE_MODE`.
pub const FILE_MODE: u32 = 0o644;

/// What the reference raises, or where the port declines.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Fault {
    /// `ValueError` with its message.
    Value(PyStr),
    /// `write`'s own `FileNotFoundError`, with its message.
    NoDirectory(PyStr),
    /// An OSError (or a ValueError of class [`PyErr::Value`]) a call
    /// raised.
    Os(OsError),
    /// What `os.getcwd()` raised inside `os.path.abspath`.
    Cwd(PyErr),
    /// `UnicodeEncodeError`: a path or a name that cannot be encoded.
    Encode,
    /// Not the reference's: the port has no answer (an error without a
    /// strerror text, a parse that declines, random bytes of the wrong
    /// count).
    NotPorted,
}

/// The per-file parse, `_parse_stat(path, st)`.
pub type LookFile<'a> = dyn FnMut(&[u32], &StatResult) -> Result<Found, Fault> + 'a;

/// `Walk`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Walk {
    pub flavour: Flavour,
    pub cwd: PyStr,
    pub repository_root: Option<PyStr>,
    pub inner: Vec<Found>,
    pub above: Above,
}

/// `Placement`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Placement {
    Root,
    Nested,
    Enclosing,
    Folder,
}

impl Placement {
    /// The placement as the reference spells it.
    pub fn name(self) -> &'static str {
        match self {
            Placement::Root => "root",
            Placement::Nested => "nested",
            Placement::Enclosing => "enclosing",
            Placement::Folder => "folder",
        }
    }
}

fn file_of(item: &Found) -> Option<&ProjectFile> {
    match item {
        Found::File(file) => Some(file),
        Found::Error(_) => None,
    }
}

impl Walk {
    fn same(&self, a: &[u32], b: &[u32]) -> bool {
        path_eq(self.flavour, a, b)
    }

    fn file_eq(&self, a: &ProjectFile, b: &ProjectFile) -> bool {
        a.id == b.id && a.name == b.name && a.boundary == b.boundary && self.same(&a.path, &b.path)
    }

    fn at_root(&self, file: &ProjectFile) -> bool {
        self.repository_root
            .as_deref()
            .is_some_and(|root| self.same(&directory(self.flavour, &file.path), root))
    }

    /// `items`: `inner` then `above.items`.
    pub fn items(&self) -> Vec<&Found> {
        self.inner.iter().chain(self.above.items.iter()).collect()
    }

    /// `files`.
    pub fn files(&self) -> Vec<&ProjectFile> {
        self.items().into_iter().filter_map(file_of).collect()
    }

    /// `errors`.
    pub fn errors(&self) -> Vec<&ProjectFileError> {
        self.items()
            .into_iter()
            .filter_map(|item| match item {
                Found::Error(error) => Some(error),
                Found::File(_) => None,
            })
            .collect()
    }

    /// `root`: the first file of `inner` whose directory is the root.
    pub fn root(&self) -> Option<&ProjectFile> {
        self.inner
            .iter()
            .filter_map(file_of)
            .find(|file| self.at_root(file))
    }

    /// `nested`: the files of `inner` whose directory is not the root.
    pub fn nested(&self) -> Vec<&ProjectFile> {
        self.inner
            .iter()
            .filter_map(file_of)
            .filter(|file| !self.at_root(file))
            .collect()
    }

    /// `folder`.
    pub fn folder(&self) -> Option<&ProjectFile> {
        if self.repository_root.is_some() {
            return None;
        }
        self.files().first().copied()
    }

    /// `enclosing`: the files of `above`, less the first without a root.
    pub fn enclosing(&self) -> Vec<&ProjectFile> {
        let files: Vec<&ProjectFile> = self.above.items.iter().filter_map(file_of).collect();
        if self.repository_root.is_some() {
            files
        } else {
            files.into_iter().skip(1).collect()
        }
    }

    /// `nearest`.
    pub fn nearest(&self) -> Option<&ProjectFile> {
        if self.repository_root.is_none() {
            return self.folder();
        }
        self.nested().first().copied().or_else(|| self.root())
    }

    /// `boundaries`.
    pub fn boundaries(&self) -> Vec<&ProjectFile> {
        self.files()
            .into_iter()
            .filter(|file| file.boundary)
            .collect()
    }

    /// `placement(file)`; a file not on the walk raises ValueError.
    pub fn placement(&self, file: &ProjectFile) -> Result<Placement, Fault> {
        if !self.files().iter().any(|other| self.file_eq(other, file)) {
            let mut message = file.path.clone();
            message.extend(unicode::cps(" is not on this walk"));
            return Err(Fault::Value(message));
        }
        if self.repository_root.is_none() {
            let folder = self.folder().is_some_and(|f| self.file_eq(f, file));
            return Ok(if folder {
                Placement::Folder
            } else {
                Placement::Enclosing
            });
        }
        let inner = self
            .inner
            .iter()
            .filter_map(file_of)
            .any(|other| self.file_eq(other, file));
        if inner {
            return Ok(if self.at_root(file) {
                Placement::Root
            } else {
                Placement::Nested
            });
        }
        Ok(Placement::Enclosing)
    }
}

/// `str(PurePath(text))` of a text `ntpath` has already normalised: under
/// Windows, a drive-only UNC path (`\\srv\sh`, `\\?\UNC\srv\sh`)
/// gains the root separator `ntpath.abspath` and `ntpath.dirname` leave
/// off (`_parse_path` of 3.13; the separate `splitroot` of 3.11 answers
/// the same). Every other text is its own `Path` text.
pub fn pure_text(flavour: Flavour, text: &[u32]) -> PyStr {
    if flavour == Flavour::Windows {
        let sep = u32::from(b'\\');
        let (drive, root, _) = splitroot(text);
        if root.is_empty() && drive.first() == Some(&sep) && drive.last() != Some(&sep) {
            let parts: Vec<&[u32]> = drive.split(|&c| c == sep).collect();
            let odd = |part: &[u32]| {
                part.is_empty()
                    || part == [u32::from(b'?')]
                    || part == [u32::from(b'.')]
                    || part == [u32::from(b'?'), u32::from(b'.')]
            };
            if (parts.len() == 4 && !odd(parts[2])) || parts.len() == 6 {
                let mut out = text.to_vec();
                out.push(sep);
                return out;
            }
        }
    }
    text.to_vec()
}

/// `PurePath.__eq__`: the text under POSIX, `str.lower` under Windows,
/// each side first read as `Path` text.
pub fn path_eq(flavour: Flavour, a: &[u32], b: &[u32]) -> bool {
    match flavour {
        Flavour::Posix => a == b,
        Flavour::Windows => {
            let (a, b) = (pure_text(flavour, a), pure_text(flavour, b));
            a == b || unicode::lower(&a) == unicode::lower(&b)
        }
    }
}

/// `PurePath.is_relative_to(other)` (3.13): equal, or one of its
/// lexical parents is.
pub fn is_relative_to(flavour: Flavour, path: &[u32], other: &[u32]) -> bool {
    let mut current = pure_text(flavour, path);
    loop {
        if path_eq(flavour, &current, other) {
            return true;
        }
        let parent = directory(flavour, &current);
        if parent == current {
            return false;
        }
        current = parent;
    }
}

/// `_absolute(path)`: `Path(os.path.abspath(path))`, as `Path` text.
pub fn absolute(flavour: Flavour, path: &[u32], process: &dyn Process) -> Result<PyStr, Fault> {
    abspath(flavour, path, process)
        .map(|text| pure_text(flavour, &text))
        .map_err(|raised| match raised {
            Raised::Os(class) => Fault::Cwd(class),
            Raised::Value(message) => Fault::Value(message),
        })
}

/// `above_start(cwd, repository_root)`.
pub fn above_start(
    flavour: Flavour,
    process: &dyn Process,
    cwd: &[u32],
    repository_root: Option<&[u32]>,
) -> Result<Option<PyStr>, Fault> {
    let Some(root) = repository_root else {
        return absolute(flavour, cwd, process).map(Some);
    };
    let root = absolute(flavour, root, process)?;
    let parent = directory(flavour, &root);
    Ok(if path_eq(flavour, &parent, &root) {
        None
    } else {
        Some(parent)
    })
}

fn none_or(path: Option<&[u32]>) -> PyStr {
    path.map_or_else(|| unicode::cps("None"), <[u32]>::to_vec)
}

/// `find(cwd, repository_root, above=above)`.
#[allow(clippy::too_many_arguments)]
pub fn find<F: Fs + ?Sized>(
    fs: &F,
    flavour: Flavour,
    process: &dyn Process,
    cwd: &[u32],
    repository_root: Option<&[u32]>,
    above: Option<Above>,
    look_file: &mut LookFile<'_>,
) -> Result<Walk, Fault> {
    let here = absolute(flavour, cwd, process)?;
    let start = above_start(flavour, process, &here, repository_root)?;
    if let Some(cached) = &above {
        let same = match (&cached.start, &start) {
            (Some(a), Some(b)) => path_eq(flavour, a, b),
            (None, None) => true,
            _ => false,
        };
        if !same {
            let mut message = unicode::cps("the cached walk starts at ");
            message.extend(none_or(cached.start.as_deref()));
            message.extend(unicode::cps(", this one at "));
            message.extend(none_or(start.as_deref()));
            return Err(Fault::Value(message));
        }
    }
    let (root, inner) = match repository_root {
        None => (None, Vec::new()),
        Some(given) => {
            let root = absolute(flavour, given, process)?;
            let from = if is_relative_to(flavour, &here, &root) {
                here.clone()
            } else {
                root.clone()
            };
            let inner = walk(fs, flavour, &from, Some(&root), look_file)?;
            (Some(root), inner)
        }
    };
    let above = match above {
        Some(cached) => cached,
        None => {
            let items = match &start {
                None => Vec::new(),
                Some(start) => walk(fs, flavour, start, None, look_file)?,
            };
            Above { start, items }
        }
    };
    Ok(Walk {
        flavour,
        cwd: here,
        repository_root: root,
        inner,
        above,
    })
}

/// `_walk(start, stop=stop)`: the files at `start` and each ancestor,
/// nearest first, up to `stop` (included) or the filesystem root.
pub fn walk<F: Fs + ?Sized>(
    fs: &F,
    flavour: Flavour,
    start: &[u32],
    stop: Option<&[u32]>,
    look_file: &mut LookFile<'_>,
) -> Result<Vec<Found>, Fault> {
    let mut found = Vec::new();
    let mut current = start.to_vec();
    let name = unicode::cps(PROJECT_FILE_NAME);
    loop {
        if let Some(item) = look(fs, &join(flavour, &current, &[&name]), look_file)? {
            found.push(item);
        }
        let parent = directory(flavour, &current);
        if stop.is_some_and(|stop| path_eq(flavour, &current, stop))
            || path_eq(flavour, &parent, &current)
        {
            return Ok(found);
        }
        current = parent;
    }
}

/// `_look(path)`: None where nothing is there, an `unreadable` error
/// where the stat fails otherwise (its strerror the detail), else the
/// parse. A ValueError of the stat (a NUL) is raised, as the reference's
/// `except OSError` lets it through.
pub fn look<F: Fs + ?Sized>(
    fs: &F,
    path: &[u32],
    look_file: &mut LookFile<'_>,
) -> Result<Option<Found>, Fault> {
    let bytes = fsencode(path).ok_or(Fault::Encode)?;
    match fs.stat(&bytes) {
        Ok(st) => look_file(path, &st).map(Some),
        Err(error) if error.class == PyErr::Value => Err(Fault::Os(error)),
        Err(error) if path_missing(error.into()) => Ok(None),
        Err(error) => {
            let text = fs
                .strerror(&error)
                .filter(|text| !text.is_empty())
                .ok_or(Fault::NotPorted)?;
            Ok(Some(Found::Error(ProjectFileError {
                path: path.to_vec(),
                reason: ErrorReason::Unreadable,
                detail: unicode::cps(&text),
            })))
        }
    }
}

/// The calls `write` makes of the disk. Paths are the text of the
/// reference's `Path`.
pub trait WriteEffects {
    /// A file [`WriteEffects::create_excl`] opened.
    type File;
    /// `Path.is_dir()`: false where the stat raises.
    fn is_dir(&self, path: &[u32]) -> bool;
    /// `os.open(path, O_WRONLY | O_CREAT | O_EXCL | O_BINARY, mode)`.
    fn create_excl(&self, path: &[u32], mode: u32) -> Result<Self::File, OsError>;
    /// One `os.write(fd, data)`: the count written.
    fn write_once(&self, file: &mut Self::File, data: &[u8]) -> Result<usize, OsError>;
    /// `fsync_file(fd)`, which swallows its error.
    fn fsync_file(&self, file: &mut Self::File);
    /// `os.close(fd)`.
    fn close(&self, file: Self::File) -> Result<(), OsError>;
    /// `os.unlink(path)`, its error swallowed.
    fn unlink_quietly(&self, path: &[u32]);
    /// `atomic_write_bytes(path, data, mode=mode)`.
    fn atomic_write_bytes(&self, path: &[u32], data: &[u8], mode: u32) -> Result<(), Fault>;
    /// `fsync_dir(path)`, every error swallowed.
    fn fsync_dir(&self, path: &[u32]);
}

/// `write(directory, name=name, boundary=boundary, rebind=rebind)`, with
/// the random bytes `mint_id` reads.
#[allow(clippy::too_many_arguments)]
pub fn write<W: WriteEffects + ?Sized>(
    effects: &W,
    flavour: Flavour,
    process: &dyn Process,
    folder: &[u32],
    name: &[u32],
    boundary: bool,
    rebind: bool,
    urandom: &[u8],
) -> Result<ProjectFile, Fault> {
    if bad_name(name) {
        return Err(Fault::Value(unicode::cps(
            "a project name cannot hold control characters or line breaks",
        )));
    }
    let folder = absolute(flavour, folder, process)?;
    if !effects.is_dir(&folder) {
        let mut message = unicode::cps("no such directory: ");
        message.extend_from_slice(&folder);
        return Err(Fault::NoDirectory(message));
    }
    let id = mint_id(urandom).map_err(|_| Fault::NotPorted)?;
    let project = ProjectFile {
        id: unicode::cps(&id),
        name: name.to_vec(),
        boundary,
        path: join(flavour, &folder, &[&unicode::cps(PROJECT_FILE_NAME)]),
    };
    let body = render(&project).ok_or(Fault::Encode)?;
    if rebind {
        effects.atomic_write_bytes(&project.path, &body, FILE_MODE)?;
        return Ok(project);
    }
    let mut file = effects
        .create_excl(&project.path, FILE_MODE)
        .map_err(Fault::Os)?;
    let written = (|| {
        let mut view: &[u8] = &body;
        while !view.is_empty() {
            let count = effects.write_once(&mut file, view)?;
            view = &view[count.min(view.len())..];
        }
        effects.fsync_file(&mut file);
        Ok::<(), OsError>(())
    })();
    let closed = effects.close(file);
    if let Some(error) = closed.err().or(written.err()) {
        effects.unlink_quietly(&project.path);
        return Err(Fault::Os(error));
    }
    effects.fsync_dir(&folder);
    Ok(project)
}
