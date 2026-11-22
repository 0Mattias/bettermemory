//! `project_file.py`, unit 1: the project file's vocabulary, `mint_id`
//! and `render` (`_toml_string`), and the checks `_from_table` makes of a
//! name (`_bad_name`, `_bad_char`) and of an id (`_ID_RE`). Nothing here
//! reads the machine. Unit 2, at the end of the file, is `parse` and its
//! reads, every effect through `Host`. The walk and `write` are unit 3.
//!
//! `_ID_RE` is matched by hand: `bmp_` and 26 members of `CROCKFORD`, as
//! `checkout_token::token_text` reads them.

use crate::checkout_token::{random_crockford, token_text, Raised as TokenRaised};
use crate::fsdata::{Flavour, Fs, Kind, OsError, PyErr, StatResult};
use crate::gitfile_rule::follow::fsencode;
use crate::gitfile_rule::hostpath::text::{
    abspath, basename, dirname, Process, Raised as HostRaised,
};
use crate::printable::quoted;
use crate::pyvalue::PyStr;
use crate::toml::{self, Value};

/// `PROJECT_FILE_NAME`.
pub const PROJECT_FILE_NAME: &str = "bettermemory.toml";
/// `ID_PREFIX`.
pub const ID_PREFIX: &str = "bmp_";
/// `ID_LENGTH`.
pub const ID_LENGTH: usize = 26;
/// `_MAX_BYTES`: a longer file is not read (by `_read_bounded`, unit 2).
pub const MAX_BYTES: usize = 64 * 1024;
/// `_QUOTE_LIMIT`: how much of a malformed id a message quotes.
pub const QUOTE_LIMIT: usize = 40;
/// `_SF_DATALESS`.
pub const SF_DATALESS: u32 = 0x4000_0000;
/// `_WINDOWS_RECALL`.
pub const WINDOWS_RECALL: u32 = 0x0004_0000 | 0x0040_0000;

/// `ErrorReason`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ErrorReason {
    Unreadable,
    Evicted,
    NotAFile,
    TooLarge,
    Toml,
    NoProjectTable,
    IdMissing,
    IdMalformed,
    NameMalformed,
    WrongType,
}

impl ErrorReason {
    /// The reason as the reference spells it.
    pub fn name(self) -> &'static str {
        match self {
            ErrorReason::Unreadable => "unreadable",
            ErrorReason::Evicted => "evicted",
            ErrorReason::NotAFile => "not_a_file",
            ErrorReason::TooLarge => "too_large",
            ErrorReason::Toml => "toml",
            ErrorReason::NoProjectTable => "no_project_table",
            ErrorReason::IdMissing => "id_missing",
            ErrorReason::IdMalformed => "id_malformed",
            ErrorReason::NameMalformed => "name_malformed",
            ErrorReason::WrongType => "wrong_type",
        }
    }
}

/// `ProjectFile`; `path` is the text of the reference's `Path`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProjectFile {
    pub id: PyStr,
    pub name: PyStr,
    pub boundary: bool,
    pub path: PyStr,
}

/// `ProjectFileError`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProjectFileError {
    pub path: PyStr,
    pub reason: ErrorReason,
    pub detail: PyStr,
}

impl ProjectFileError {
    /// `line`: the path quoted, then the detail.
    pub fn line(&self) -> PyStr {
        let mut out: PyStr = "bettermemory: ".chars().map(u32::from).collect();
        out.extend(quoted(&self.path));
        out.extend(" names no project: ".chars().map(u32::from));
        out.extend_from_slice(&self.detail);
        out
    }
}

/// `Found`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Found {
    File(ProjectFile),
    Error(ProjectFileError),
}

/// `Above`: `start` and the items, nearest first.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Above {
    pub start: Option<PyStr>,
    pub items: Vec<Found>,
}

/// `ProjectFile.directory` and `ProjectFileError.directory`: the
/// `Path`'s parent: `dirname` of the `Path`'s text, which is already
/// normalised, and `.` where that is empty (one relative component).
pub fn directory(flavour: Flavour, path: &[u32]) -> PyStr {
    let parent = dirname(flavour, path);
    if parent.is_empty() {
        vec![u32::from(b'.')]
    } else {
        parent
    }
}

/// `mint_id()`, the random bytes given.
pub fn mint_id(urandom: &[u8]) -> Result<String, TokenRaised> {
    Ok(format!(
        "{ID_PREFIX}{}",
        random_crockford(ID_LENGTH as i64, urandom)?
    ))
}

/// `_ID_RE.fullmatch(text) is not None`.
pub fn id_text(text: &[u32]) -> bool {
    let prefix: Vec<u32> = ID_PREFIX.chars().map(u32::from).collect();
    text.starts_with(&prefix) && token_text(&text[prefix.len()..])
}

/// `_bad_name(name)`.
pub fn bad_name(name: &[u32]) -> bool {
    name.iter().any(|&c| bad_char(c))
}

/// `_bad_char(char)`.
pub fn bad_char(c: u32) -> bool {
    c < 0x20 || (0x7F..0xA0).contains(&c) || c == 0x2028 || c == 0x2029
}

/// `_toml_string(value)`.
pub fn toml_string(value: &[u32]) -> PyStr {
    let mut out = vec![u32::from(b'"')];
    for &c in value {
        if c == u32::from(b'"') || c == u32::from(b'\\') {
            out.extend([u32::from(b'\\'), c]);
        } else if c < 0x20 || c == 0x7F {
            out.extend(format!("\\u{c:04X}").chars().map(u32::from));
        } else {
            out.push(c);
        }
    }
    out.push(u32::from(b'"'));
    out
}

/// `render(project)`; None where `.encode("utf-8")` raises (a lone
/// surrogate).
pub fn render(project: &ProjectFile) -> Option<Vec<u8>> {
    let mut lines: Vec<PyStr> = [
        "# bettermemory project file, written by `bettermemory project init`.",
        "# It names this folder's project; committed, every clone carries it.",
        "# The id is random: the name can be edited, the id should stay as it is.",
        "[project]",
    ]
    .iter()
    .map(|l| l.chars().map(u32::from).collect())
    .collect();
    for (key, value) in [("id = ", &project.id), ("name = ", &project.name)] {
        let mut line: PyStr = key.chars().map(u32::from).collect();
        line.extend(toml_string(value));
        lines.push(line);
    }
    if project.boundary {
        lines.push("boundary = true".chars().map(u32::from).collect());
    }
    let mut text = lines.join(&u32::from(b'\n'));
    text.push(u32::from(b'\n'));
    text.iter()
        .map(|&c| char::from_u32(c))
        .collect::<Option<String>>()
        .map(String::into_bytes)
}

// ---------------------------------------------------------------------------
// Unit 2: `parse` and its reads.
// ---------------------------------------------------------------------------

/// Why `parse` gives no `Found`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Fault {
    /// The reference raises: the exception's class name (`ValueError` for
    /// a NUL in the path or `int()`'s digit limit, an OSError subclass
    /// for a failed `os.close` or `os.getcwd`).
    Raised(&'static str),
    /// Not ported: an OS error the file system words no text for, a
    /// document nested to the reference's recursion limit (which follows
    /// the caller's stack), or a path the Windows flavour cannot spell.
    NotPorted,
}

/// What `parse` reads of the machine: the file system, the path flavour,
/// the process (`os.getcwd` for `abspath`) and `sys.platform == "darwin"`.
pub struct Host<'a, F: Fs + ?Sized> {
    pub fs: &'a F,
    pub flavour: Flavour,
    pub process: &'a dyn Process,
    pub darwin: bool,
}

fn text(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn error(path: &[u32], reason: ErrorReason, detail: PyStr) -> ProjectFileError {
    ProjectFileError {
        path: path.to_vec(),
        reason,
        detail,
    }
}

/// `parse(path)`.
pub fn parse<F: Fs + ?Sized>(host: &Host<'_, F>, path: &[u32]) -> Result<Found, Fault> {
    let absolute = absolute(host.flavour, path, host.process)?;
    let st = match host.fs.stat(&os_path(host.flavour, &absolute)?) {
        Ok(st) => st,
        Err(e) if e.class == PyErr::Value => return Err(Fault::Raised(e.class.name())),
        Err(e) => {
            let detail = os_text(host.fs, &e)?;
            return Ok(Found::Error(error(
                &absolute,
                ErrorReason::Unreadable,
                detail,
            )));
        }
    };
    parse_stat(host, &absolute, &st)
}

/// `_absolute(path)`: `Path(os.path.abspath(path))`. The text `abspath`
/// gives is already normal, so `Path` keeps it.
pub fn absolute(flavour: Flavour, path: &[u32], process: &dyn Process) -> Result<PyStr, Fault> {
    abspath(flavour, path, process).map_err(|raised| match raised {
        HostRaised::Value(_) => Fault::Raised(PyErr::Value.name()),
        HostRaised::Os(class) => Fault::Raised(class.name()),
    })
}

/// `os.fspath` of the path as the OS is handed it: `os.fsencode` under
/// POSIX (a code point it cannot encode raises UnicodeEncodeError, a
/// ValueError); UTF-8 under Windows, where a lone surrogate is not ported.
fn os_path(flavour: Flavour, path: &[u32]) -> Result<Vec<u8>, Fault> {
    match flavour {
        Flavour::Posix => fsencode(path).ok_or(Fault::Raised(PyErr::Value.name())),
        Flavour::Windows => path
            .iter()
            .map(|&c| char::from_u32(c))
            .collect::<Option<String>>()
            .map(String::into_bytes)
            .ok_or(Fault::NotPorted),
    }
}

/// `_evicted(st)`, `darwin` being `sys.platform == "darwin"`.
pub fn evicted(st: &StatResult, darwin: bool) -> bool {
    (darwin && st.flags & SF_DATALESS != 0) || st.file_attributes & WINDOWS_RECALL != 0
}

/// `_parse_stat(path, st)`.
pub fn parse_stat<F: Fs + ?Sized>(
    host: &Host<'_, F>,
    path: &[u32],
    st: &StatResult,
) -> Result<Found, Fault> {
    if st.kind != Kind::Regular {
        let detail = text("not a regular file");
        return Ok(Found::Error(error(path, ErrorReason::NotAFile, detail)));
    }
    if evicted(st, host.darwin) {
        let detail = text("not on this disk yet, evicted by the file provider");
        return Ok(Found::Error(error(path, ErrorReason::Evicted, detail)));
    }
    match read_bounded(host, path)? {
        Ok(raw) => parse_raw(host.flavour, path, &raw),
        Err(e) => Ok(Found::Error(e)),
    }
}

/// The tail of `_parse_stat` once the bytes are read: `tomllib.loads` of
/// their UTF-8 text, then `_from_table`.
pub fn parse_raw(flavour: Flavour, path: &[u32], raw: &[u8]) -> Result<Found, Fault> {
    let Ok(decoded) = std::str::from_utf8(raw) else {
        let detail = text("not UTF-8 text");
        return Ok(Found::Error(error(path, ErrorReason::Toml, detail)));
    };
    match toml::loads(decoded) {
        Ok(data) => Ok(from_table(flavour, path, &data)),
        Err(toml::Error::Decode(message)) => {
            let detail = text(&format!("not valid TOML ({message})"));
            Ok(Found::Error(error(path, ErrorReason::Toml, detail)))
        }
        Err(toml::Error::IntDigits(_)) => Err(Fault::Raised("ValueError")),
        Err(toml::Error::Recursion) => Err(Fault::NotPorted),
    }
}

/// `_read_bounded(path)`: the non-blocking open, `os.fstat`, reads of
/// what is still wanted up to a byte past [`MAX_BYTES`], and the close
/// of the `finally`, whose error raises over whatever the body answered.
pub fn read_bounded<F: Fs + ?Sized>(
    host: &Host<'_, F>,
    path: &[u32],
) -> Result<Result<Vec<u8>, ProjectFileError>, Fault> {
    let fs = host.fs;
    let mut file = match fs.open_result(&os_path(host.flavour, path)?) {
        Ok(file) => file,
        Err(e) if e.class == PyErr::Value => return Err(Fault::Raised(e.class.name())),
        Err(e) => {
            let detail = os_text(fs, &e)?;
            return Ok(Err(error(path, ErrorReason::Unreadable, detail)));
        }
    };
    let body = read_open(fs, &mut file);
    if let Err(e) = fs.close(file) {
        return Err(Fault::Raised(e.class.name()));
    }
    let data = match body {
        Ok(Ok(data)) => data,
        Ok(Err(reason)) => {
            let detail = text("not a regular file");
            return Ok(Err(error(path, reason, detail)));
        }
        Err(e) => {
            let detail = os_text(fs, &e)?;
            return Ok(Err(error(path, ErrorReason::Unreadable, detail)));
        }
    };
    if data.len() > MAX_BYTES {
        let detail = text(&format!("larger than {MAX_BYTES} bytes"));
        return Ok(Err(error(path, ErrorReason::TooLarge, detail)));
    }
    Ok(Ok(data))
}

/// The `try` of `_read_bounded` over the open file.
fn read_open<F: Fs + ?Sized>(
    fs: &F,
    file: &mut F::File,
) -> Result<Result<Vec<u8>, ErrorReason>, OsError> {
    if fs.fstat_result(file)?.kind != Kind::Regular {
        return Ok(Err(ErrorReason::NotAFile));
    }
    let mut data = Vec::new();
    while data.len() <= MAX_BYTES {
        let chunk = fs.read_once(file, MAX_BYTES + 1 - data.len())?;
        if chunk.is_empty() {
            break;
        }
        data.extend_from_slice(&chunk);
    }
    Ok(Ok(data))
}

/// `_from_table(path, data)`.
pub fn from_table(flavour: Flavour, path: &[u32], data: &toml::Table) -> Found {
    let wrong = |detail: &str| Found::Error(error(path, ErrorReason::WrongType, text(detail)));
    let table = match data.get("project") {
        None => {
            let detail = text("it has no [project] table");
            return Found::Error(error(path, ErrorReason::NoProjectTable, detail));
        }
        Some(Value::Table(table)) => table,
        Some(_) => return wrong("`project` is not a table"),
    };
    let id = match table.get("id") {
        None => {
            let detail = text("[project] has no id");
            return Found::Error(error(path, ErrorReason::IdMissing, detail));
        }
        Some(Value::String(id)) => text(id),
        Some(_) => return wrong("[project] id is not a string"),
    };
    if !id_text(&id) {
        let mut detail = text("[project] id ");
        detail.extend(quoted(&id[..id.len().min(QUOTE_LIMIT)]));
        detail.extend(text(&format!(
            " is not {ID_PREFIX} and {ID_LENGTH} Crockford base-32 characters"
        )));
        return Found::Error(error(path, ErrorReason::IdMalformed, detail));
    }
    let name = match table.get("name") {
        // No name: the directory's, display only (the id decides).
        None => basename(flavour, &dirname(flavour, path))
            .into_iter()
            .filter(|&c| !bad_char(c))
            .collect(),
        Some(Value::String(name)) => text(name),
        Some(_) => return wrong("[project] name is not a string"),
    };
    if bad_name(&name) {
        let detail = text("[project] name holds control characters or line breaks");
        return Found::Error(error(path, ErrorReason::NameMalformed, detail));
    }
    let boundary = match table.get("boundary") {
        None => false,
        Some(Value::Boolean(b)) => *b,
        Some(_) => return wrong("[project] boundary is not true or false"),
    };
    Found::File(ProjectFile {
        id,
        name,
        boundary,
        path: path.to_vec(),
    })
}

/// `_os_text(exc)`: `exc.strerror or str(exc)`. The file system's text
/// ([`Fs::strerror`]); where it gives none, or an empty one, `str(exc)`
/// would be worded from the errno's number, which is not kept: declined.
pub fn os_text<F: Fs + ?Sized>(fs: &F, e: &OsError) -> Result<PyStr, Fault> {
    match fs.strerror(e) {
        Some(s) if !s.is_empty() => Ok(text(&s)),
        _ => Err(Fault::NotPorted),
    }
}
