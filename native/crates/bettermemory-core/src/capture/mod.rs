//! A location capture's vocabulary: what the reference gathers about a
//! caller's directory before it resolves the project
//! (`location_git.Capture`), and its token, the JSON form a hook client
//! posts and the daemon reads (`Capture.to_token`, `Capture.from_token`,
//! `location_git.py` 1188-1451 at ececad8c).
//!
//! Data in, data out: nothing here reads a file, git, the clock or the
//! environment. The token is read from a [`PyValue`], the value
//! `json.loads` gives, and where the reference lets Python's own rules
//! through (the truth of any value, `int()`, `str()`) they are ported as
//! such. See native/README.md, "A location capture's vocabulary".

pub mod location;
pub mod paths;
pub mod remote;

use std::borrow::Cow;

use crate::pyvalue::{py_int, py_str, repr, PyError, PyStr, PyValue};
use crate::unicode;

/// `_location_probe.TOKEN_VERSION`.
pub const TOKEN_VERSION: i128 = 1;
/// `location_git.REF_TIPS_MAX`.
pub const REF_TIPS_MAX: usize = 64;
/// `location_git.MANIFEST_LIMIT`.
pub const MANIFEST_LIMIT: usize = 4096;
/// The other caps `from_token` reads under: remotes and aliases, stamps,
/// files, file errors.
pub const REMOTES_MAX: usize = 256;
pub const STAMPS_MAX: usize = 8;
pub const FILES_MAX: usize = 64;
pub const FILE_ERRORS_MAX: usize = 64;

/// The native limit on how deep `str()` of a key item may nest before it
/// is RecursionError, counting the value itself as 1 and every object
/// below it (a scalar at the bottom too): 9,999 for a top-level key
/// (``object`` and the like), 9,997 inside ``files``. This is the port's
/// own limit, not a parity claim: it equals what CPython 3.13 does on
/// Linux and macOS release builds, called on a fresh thread (its C limit
/// is 10,000 there; it is 3,000 on Windows, 1,000 on Windows ARM64, 500
/// in debug builds, and a caller deeper in C calls has less). The golden
/// file records only depths far from every platform's limit; the
/// boundary is tested natively (`capture_vocabulary_robustness`).
pub const STR_DEPTH_TOP: usize = 9999;
pub const STR_DEPTH_FILES: usize = 9997;

/// The keys of a token, in `to_token`'s order.
pub const TOKEN_KEYS: [&str; 29] = [
    "v",
    "cwd",
    "state",
    "root",
    "kind",
    "git_dir",
    "common_dir",
    "object",
    "common_object",
    "root_object",
    "cwd_object",
    "head",
    "branch",
    "ref_tips",
    "remotes",
    "aliases",
    "shallow",
    "token",
    "token_written",
    "token_status",
    "stamps",
    "ticks",
    "files",
    "file_errors",
    "file_tracked",
    "evicted",
    "detail",
    "manifest",
    "hostname",
];

/// A filesystem object as a capture keeps it: device, inode and birth
/// time, as text (`location_git.Key`).
pub type Key = (PyStr, PyStr, Option<PyStr>);

/// `state`: ``repo``, ``outside`` or ``indeterminate``.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Repo,
    Outside,
    Indeterminate,
}

impl State {
    pub const ALL: [State; 3] = [State::Repo, State::Outside, State::Indeterminate];

    pub fn as_str(self) -> &'static str {
        match self {
            State::Repo => "repo",
            State::Outside => "outside",
            State::Indeterminate => "indeterminate",
        }
    }
}

/// `kind`: ``primary``, ``linked``, ``bare`` or ``folder``.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Primary,
    Linked,
    Bare,
    Folder,
}

impl Kind {
    pub const ALL: [Kind; 4] = [Kind::Primary, Kind::Linked, Kind::Bare, Kind::Folder];

    pub fn as_str(self) -> &'static str {
        match self {
            Kind::Primary => "primary",
            Kind::Linked => "linked",
            Kind::Bare => "bare",
            Kind::Folder => "folder",
        }
    }
}

/// `location_git.FileSeen`: a project file at the cwd or above.
#[derive(Clone, Debug, PartialEq)]
pub struct FileSeen {
    pub id: PyStr,
    pub name: PyStr,
    pub dir: PyStr,
    pub path: PyStr,
    pub boundary: bool,
    pub key: Option<Key>,
}

/// One of `Capture.stamps`: a name and four Python ints (inode, mtime
/// and ctime in ns, size), each as canonical decimal text.
#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Stamp {
    pub name: PyStr,
    pub numbers: [String; 4],
}

/// `Capture.manifest`: a folder name and (path, size) entries, the size
/// as canonical decimal text.
#[derive(Clone, Debug, PartialEq)]
pub struct Manifest {
    pub name: PyStr,
    pub entries: Vec<(PyStr, String)>,
}

/// `location_git.Capture`. `kept_apart`, `tree_recorded` and
/// `config_sources` are not in the token; the reference's
/// `config_files` (stamped `gitconfig.ConfigFile`s, read from disk) is
/// the file-reading unit's and is not carried here.
#[derive(Clone, Debug, PartialEq)]
pub struct Capture {
    pub cwd: PyStr,
    pub state: State,
    pub root: Option<PyStr>,
    pub kind: Option<Kind>,
    pub git_dir: Option<PyStr>,
    pub common_dir: Option<PyStr>,
    pub object_key: Option<Key>,
    pub common_object_key: Option<Key>,
    pub root_object_key: Option<Key>,
    pub cwd_key: Option<Key>,
    pub head: Option<PyStr>,
    pub branch: Option<PyStr>,
    pub ref_tips: Vec<PyStr>,
    pub remote_keys: Vec<PyStr>,
    pub shallow: bool,
    pub token: Option<PyStr>,
    pub token_written: Option<PyStr>,
    pub token_status: PyStr,
    pub stamps: Vec<Stamp>,
    pub ticks: Option<bool>,
    pub files: Vec<FileSeen>,
    pub file_errors: Vec<PyStr>,
    pub file_tracked: Option<bool>,
    pub evicted: bool,
    pub detail: PyStr,
    pub manifest: Option<Manifest>,
    pub hostname: Option<PyStr>,
    pub alias_keys: Vec<PyStr>,
    pub kept_apart: bool,
    pub tree_recorded: bool,
    pub config_sources: Vec<PyStr>,
}

impl Capture {
    /// `Capture(cwd=cwd, state=state)`: every other field its default.
    pub fn new(cwd: PyStr, state: State) -> Capture {
        Capture {
            cwd,
            state,
            root: None,
            kind: None,
            git_dir: None,
            common_dir: None,
            object_key: None,
            common_object_key: None,
            root_object_key: None,
            cwd_key: None,
            head: None,
            branch: None,
            ref_tips: Vec::new(),
            remote_keys: Vec::new(),
            shallow: false,
            token: None,
            token_written: None,
            token_status: unicode::cps("n/a"),
            stamps: Vec::new(),
            ticks: None,
            files: Vec::new(),
            file_errors: Vec::new(),
            file_tracked: None,
            evicted: false,
            detail: Vec::new(),
            manifest: None,
            hostname: None,
            alias_keys: Vec::new(),
            kept_apart: false,
            tree_recorded: false,
            config_sources: Vec::new(),
        }
    }

    /// `Capture.born`: HEAD names a commit or another ref does.
    pub fn born(&self) -> bool {
        self.head.is_some() || !self.ref_tips.is_empty()
    }

    /// `Capture.to_token`: the token as the dict `json.dumps` writes, its
    /// keys in the reference's order.
    pub fn to_token(&self) -> PyValue {
        let s = |t: &PyStr| PyValue::Str(t.clone());
        let opt = |t: &Option<PyStr>| t.as_ref().map_or(PyValue::None, s);
        let strs = |v: &[PyStr]| PyValue::List(v.iter().map(s).collect());
        let opt_bool = |b: Option<bool>| b.map_or(PyValue::None, PyValue::Bool);
        let entry = |k: &str, v: PyValue| (PyValue::str(k), v);
        let items = vec![
            entry("v", PyValue::int(TOKEN_VERSION)),
            entry("cwd", s(&self.cwd)),
            entry("state", PyValue::str(self.state.as_str())),
            entry("root", opt(&self.root)),
            entry(
                "kind",
                self.kind
                    .map_or(PyValue::None, |k| PyValue::str(k.as_str())),
            ),
            entry("git_dir", opt(&self.git_dir)),
            entry("common_dir", opt(&self.common_dir)),
            entry("object", key_value(&self.object_key)),
            entry("common_object", key_value(&self.common_object_key)),
            entry("root_object", key_value(&self.root_object_key)),
            entry("cwd_object", key_value(&self.cwd_key)),
            entry("head", opt(&self.head)),
            entry("branch", opt(&self.branch)),
            entry("ref_tips", strs(&self.ref_tips)),
            entry("remotes", strs(&self.remote_keys)),
            entry("aliases", strs(&self.alias_keys)),
            entry("shallow", PyValue::Bool(self.shallow)),
            entry("token", opt(&self.token)),
            entry("token_written", opt(&self.token_written)),
            entry("token_status", s(&self.token_status)),
            entry(
                "stamps",
                PyValue::List(
                    self.stamps
                        .iter()
                        .map(|st| {
                            let mut row = vec![s(&st.name)];
                            row.extend(st.numbers.iter().map(|n| PyValue::Int(n.clone())));
                            PyValue::List(row)
                        })
                        .collect(),
                ),
            ),
            entry("ticks", opt_bool(self.ticks)),
            entry(
                "files",
                PyValue::List(
                    self.files
                        .iter()
                        .map(|f| {
                            PyValue::Dict(vec![
                                entry("id", s(&f.id)),
                                entry("name", s(&f.name)),
                                entry("dir", s(&f.dir)),
                                entry("path", s(&f.path)),
                                entry("boundary", PyValue::Bool(f.boundary)),
                                entry("object", key_value(&f.key)),
                            ])
                        })
                        .collect(),
                ),
            ),
            entry("file_errors", strs(&self.file_errors)),
            entry("file_tracked", opt_bool(self.file_tracked)),
            entry("evicted", PyValue::Bool(self.evicted)),
            entry("detail", s(&self.detail)),
            entry(
                "manifest",
                self.manifest.as_ref().map_or(PyValue::None, |m| {
                    PyValue::Dict(vec![
                        entry("name", s(&m.name)),
                        entry(
                            "entries",
                            PyValue::List(
                                m.entries
                                    .iter()
                                    .map(|(p, n)| {
                                        PyValue::List(vec![s(p), PyValue::Int(n.clone())])
                                    })
                                    .collect(),
                            ),
                        ),
                    ])
                }),
            ),
            entry("hostname", opt(&self.hostname)),
        ];
        PyValue::Dict(items)
    }

    /// `Capture.from_token`: the capture a token describes, `Ok(None)`
    /// where the reference refuses it (it catches KeyError, TypeError,
    /// ValueError and IndexError), or `Err` naming the exception it lets
    /// through: OverflowError from `int()` of an infinite stamp or
    /// manifest number, RecursionError from `str()` of a deeply nested key
    /// item (`location_git.py` 1361 and 1430, neither caught at 1384).
    pub fn from_token(raw: &PyValue) -> Result<Option<Capture>, PyError> {
        if !matches!(raw, PyValue::Dict(_)) || !is_version(raw.get("v")) {
            return Ok(None);
        }
        match read(raw) {
            Ok(cap) => Ok(Some(cap)),
            Err(Fail::Refused) => Ok(None),
            Err(Fail::Raised(e)) => Err(e),
        }
    }
}

fn key_value(key: &Option<Key>) -> PyValue {
    match key {
        None => PyValue::None,
        Some((d, i, b)) => PyValue::List(vec![
            PyValue::Str(d.clone()),
            PyValue::Str(i.clone()),
            b.as_ref()
                .map_or(PyValue::None, |b| PyValue::Str(b.clone())),
        ]),
    }
}

/// `raw.get("v") == TOKEN_VERSION` by Python's equality: 1, True, 1.0.
fn is_version(v: Option<&PyValue>) -> bool {
    match v {
        Some(PyValue::Int(t)) => t == "1",
        Some(PyValue::Bool(b)) => *b,
        Some(PyValue::Float(f)) => *f == 1.0,
        _ => false,
    }
}

/// Why a read stopped: a refusal the reference catches, or an exception
/// it lets through.
enum Fail {
    Refused,
    Raised(PyError),
}

impl From<PyError> for Fail {
    fn from(e: PyError) -> Fail {
        match e {
            PyError::OverflowError | PyError::RecursionError => Fail::Raised(e),
            _ => Fail::Refused,
        }
    }
}

type R<T> = Result<T, Fail>;

fn read(raw: &PyValue) -> R<Capture> {
    let get = |k: &str| raw.get(k);
    let cwd = text(get("cwd").ok_or(Fail::Refused)?)?;
    let state = match get("state").ok_or(Fail::Refused)? {
        PyValue::Str(s) => State::ALL
            .into_iter()
            .find(|st| unicode::cps(st.as_str()) == *s)
            .ok_or(Fail::Refused)?,
        _ => return Err(Fail::Refused),
    };
    let root = opt_text(get("root"))?;
    let kind = match get("kind") {
        None | Some(PyValue::None) => None,
        Some(PyValue::Str(s)) => Some(
            Kind::ALL
                .into_iter()
                .find(|k| unicode::cps(k.as_str()) == *s)
                .ok_or(Fail::Refused)?,
        ),
        Some(_) => return Err(Fail::Refused),
    };
    let git_dir = opt_text(get("git_dir"))?;
    let common_dir = opt_text(get("common_dir"))?;
    let object_key = key(get("object"), STR_DEPTH_TOP)?;
    let common_object_key = key(get("common_object"), STR_DEPTH_TOP)?;
    let root_object_key = key(get("root_object"), STR_DEPTH_TOP)?;
    let cwd_key = key(get("cwd_object"), STR_DEPTH_TOP)?;
    let head = match get("head") {
        None | Some(PyValue::None) => None,
        Some(v) => Some(hash(v)?),
    };
    let branch = opt_text(get("branch"))?;
    let ref_tips = bounded(get("ref_tips"), REF_TIPS_MAX)?
        .iter()
        .map(hash)
        .collect::<R<Vec<_>>>()?;
    let remote_keys = bounded(get("remotes"), REMOTES_MAX)?
        .iter()
        .map(text)
        .collect::<R<Vec<_>>>()?;
    let alias_keys = bounded(get("aliases"), REMOTES_MAX)?
        .iter()
        .map(text)
        .collect::<R<Vec<_>>>()?;
    let shallow = truth(get("shallow"));
    let token = opt_text(get("token"))?;
    let token_written = opt_text(get("token_written"))?;
    let token_status = match get("token_status") {
        None => unicode::cps("n/a"),
        Some(v) => text(v)?,
    };
    let mut stamps = Vec::new();
    for s in bounded(get("stamps"), STAMPS_MAX)? {
        let name = text(&*index(s, 0)?)?;
        let mut numbers: [String; 4] = Default::default();
        for (i, slot) in numbers.iter_mut().enumerate() {
            *slot = int_text(&*index(s, i + 1)?)?;
        }
        stamps.push(Stamp { name, numbers });
    }
    let ticks = match get("ticks") {
        None | Some(PyValue::None) => None,
        Some(PyValue::Bool(b)) => Some(*b),
        Some(_) => return Err(Fail::Refused),
    };
    let mut files = Vec::new();
    for f in bounded(get("files"), FILES_MAX)? {
        let field = |k: &str| -> R<PyStr> {
            match f {
                PyValue::Dict(_) => text(f.get(k).ok_or(Fail::Refused)?),
                _ => Err(Fail::Refused),
            }
        };
        let id = field("id")?;
        let name = field("name")?;
        let dir = field("dir")?;
        let path = field("path")?;
        files.push(FileSeen {
            id,
            name,
            dir,
            path,
            boundary: truth(f.get("boundary")),
            key: key(f.get("object"), STR_DEPTH_FILES)?,
        });
    }
    let file_errors = bounded(get("file_errors"), FILE_ERRORS_MAX)?
        .iter()
        .map(text)
        .collect::<R<Vec<_>>>()?;
    let file_tracked = match get("file_tracked") {
        None | Some(PyValue::None) => None,
        v => Some(truth(v)),
    };
    let evicted = truth(get("evicted"));
    let detail = match get("detail") {
        None => Vec::new(),
        Some(v) => text(v)?,
    };
    let manifest = match get("manifest") {
        None | Some(PyValue::None) => None,
        Some(m @ PyValue::Dict(_)) => {
            let entries = bounded(m.get("entries"), MANIFEST_LIMIT)?;
            let name = text(m.get("name").unwrap_or(&PyValue::None))?;
            let mut out = Vec::new();
            for e in entries {
                let path = text(&*index(e, 0)?)?;
                out.push((path, int_text(&*index(e, 1)?)?));
            }
            Some(Manifest { name, entries: out })
        }
        Some(_) => return Err(Fail::Refused),
    };
    let hostname = opt_text(get("hostname"))?;
    Ok(Capture {
        cwd,
        state,
        root,
        kind,
        git_dir,
        common_dir,
        object_key,
        common_object_key,
        root_object_key,
        cwd_key,
        head,
        branch,
        ref_tips,
        remote_keys,
        shallow,
        token,
        token_written,
        token_status,
        stamps,
        ticks,
        files,
        file_errors,
        file_tracked,
        evicted,
        detail,
        manifest,
        hostname,
        alias_keys,
        kept_apart: false,
        tree_recorded: false,
        config_sources: Vec::new(),
    })
}

/// `_text`: a `str`, else TypeError.
fn text(v: &PyValue) -> R<PyStr> {
    match v {
        PyValue::Str(s) => Ok(s.clone()),
        _ => Err(Fail::Refused),
    }
}

/// `_opt_text` of `raw.get(key)`.
fn opt_text(v: Option<&PyValue>) -> R<Option<PyStr>> {
    match v {
        None | Some(PyValue::None) => Ok(None),
        Some(v) => text(v).map(Some),
    }
}

/// `_hash`: a full hash, 40 or 64 hex digits, lowercased. `str.lower`
/// maps no other character to an ASCII hex digit, so any text that is
/// not ASCII fails the match whatever its case.
fn hash(v: &PyValue) -> R<PyStr> {
    let s = text(v)?;
    let hex = |&c: &u32| char::from_u32(c).is_some_and(|c| c.is_ascii_hexdigit());
    if (s.len() == 40 || s.len() == 64) && s.iter().all(hex) {
        Ok(s.iter().map(|&c| char_lower(c)).collect())
    } else {
        Err(Fail::Refused)
    }
}

fn char_lower(c: u32) -> u32 {
    if (u32::from(b'A')..=u32::from(b'Z')).contains(&c) {
        c + 32
    } else {
        c
    }
}

/// `_bounded`: None as empty, else a list of at most `limit` items.
fn bounded(v: Option<&PyValue>, limit: usize) -> R<&[PyValue]> {
    match v {
        None | Some(PyValue::None) => Ok(&[]),
        Some(PyValue::List(items)) if items.len() <= limit => Ok(items),
        Some(_) => Err(Fail::Refused),
    }
}

/// `bool(value)` of `raw.get(key)`.
pub fn truth(v: Option<&PyValue>) -> bool {
    match v {
        None | Some(PyValue::None) => false,
        Some(PyValue::Bool(b)) => *b,
        Some(PyValue::Int(t)) => t != "0",
        Some(PyValue::Float(f)) => *f != 0.0,
        Some(PyValue::Str(s)) | Some(PyValue::Path(s)) => !s.is_empty(),
        Some(PyValue::List(items)) | Some(PyValue::Tuple(items)) => !items.is_empty(),
        Some(PyValue::Dict(items)) => !items.is_empty(),
        Some(PyValue::DateTime(_)) => true,
    }
}

/// `int(value)` as canonical decimal text.
fn int_text(v: &PyValue) -> R<String> {
    match &py_int(v)? {
        PyValue::Int(t) => Ok(t.clone()),
        _ => Err(Fail::Refused),
    }
}

/// `value[i]` for an int index: an item of a list or tuple, a character
/// of a str, the entry of a dict whose key equals `i`; IndexError,
/// KeyError or TypeError otherwise.
fn index(v: &PyValue, i: usize) -> R<Cow<'_, PyValue>> {
    match v {
        PyValue::List(items) | PyValue::Tuple(items) => {
            items.get(i).map(Cow::Borrowed).ok_or(Fail::Refused)
        }
        PyValue::Str(s) => s
            .get(i)
            .map(|&c| Cow::Owned(PyValue::Str(vec![c])))
            .ok_or(Fail::Refused),
        PyValue::Dict(items) => items
            .iter()
            .rev()
            .find(|(k, _)| equals_index(k, i))
            .map(|(_, v)| Cow::Borrowed(v))
            .ok_or(Fail::Refused),
        _ => Err(Fail::Refused),
    }
}

fn equals_index(k: &PyValue, i: usize) -> bool {
    match k {
        PyValue::Int(t) => *t == i.to_string(),
        PyValue::Bool(b) => usize::from(*b) == i,
        PyValue::Float(f) => *f == i as f64,
        _ => false,
    }
}

/// `location_git._key(value)` as a caller outside a token reads it: a
/// value of the wrong shape is ValueError, as is an int past
/// [`INT_STR_DIGITS`]; RecursionError from `str()` of a deeply nested item
/// is the other raise.
pub fn object_key_of(v: Option<&PyValue>) -> Result<Option<Key>, PyError> {
    match key(v, STR_DEPTH_TOP) {
        Ok(k) => Ok(k),
        Err(Fail::Refused) => Err(PyError::ValueError),
        Err(Fail::Raised(e)) => Err(e),
    }
}

/// `_key`: None, or a three-item list or tuple as `str()` of each item
/// (the birth None kept). `depth` is the deepest object `str()` spells
/// there before RecursionError ([`STR_DEPTH_TOP`]).
fn key(v: Option<&PyValue>, depth: usize) -> R<Option<Key>> {
    let items = match v {
        None | Some(PyValue::None) => return Ok(None),
        Some(PyValue::List(items)) | Some(PyValue::Tuple(items)) if items.len() == 3 => items,
        Some(_) => return Err(Fail::Refused),
    };
    let device = str_of(&items[0], depth)?;
    let inode = str_of(&items[1], depth)?;
    let birth = match &items[2] {
        PyValue::None => None,
        b => Some(str_of(b, depth)?),
    };
    Ok(Some((device, inode, birth)))
}

/// The most digits `str()` writes of an int
/// (`sys.int_info.default_max_str_digits`); past it, ValueError.
pub const INT_STR_DIGITS: usize = 4300;

/// What `str()` of `value` meets first, in the order `repr` walks it
/// (depth first, left to right): an object deeper than `limit` levels
/// (RecursionError) or an int past [`INT_STR_DIGITS`] (ValueError).
fn str_failure(value: &PyValue, limit: usize) -> Option<PyError> {
    let mut work = vec![(value, 1usize)];
    while let Some((v, level)) = work.pop() {
        if level > limit {
            return Some(PyError::RecursionError);
        }
        match v {
            PyValue::Int(t) if t.trim_start_matches('-').len() > INT_STR_DIGITS => {
                return Some(PyError::ValueError);
            }
            PyValue::List(items) | PyValue::Tuple(items) => {
                work.extend(items.iter().rev().map(|i| (i, level + 1)));
            }
            PyValue::Dict(items) => {
                for (k, i) in items.iter().rev() {
                    work.push((i, level + 1));
                    work.push((k, level + 1));
                }
            }
            _ => {}
        }
    }
    None
}

/// `str(value)` where at most `depth` levels may be spelled, and no int
/// past [`INT_STR_DIGITS`] digits (`pyvalue::py_str` and `repr` apply no
/// such limit).
pub fn str_of(value: &PyValue, depth: usize) -> Result<PyStr, PyError> {
    if let Some(e) = str_failure(value, depth) {
        return Err(e);
    }
    let container = matches!(
        value,
        PyValue::List(_) | PyValue::Tuple(_) | PyValue::Dict(_)
    );
    if !container {
        return py_str(value);
    }
    // `str` of a container is its `repr`: each item's repr, one level in.
    let mut out = Vec::new();
    let push = |out: &mut Vec<u32>, t: &str| out.extend(t.chars().map(u32::from));
    match value {
        PyValue::List(items) | PyValue::Tuple(items) => {
            let list = matches!(value, PyValue::List(_));
            push(&mut out, if list { "[" } else { "(" });
            for (n, item) in items.iter().enumerate() {
                if n > 0 {
                    push(&mut out, ", ");
                }
                out.extend(repr(item)?);
            }
            if !list && items.len() == 1 {
                push(&mut out, ",");
            }
            push(&mut out, if list { "]" } else { ")" });
        }
        PyValue::Dict(items) => {
            push(&mut out, "{");
            for (n, (k, v)) in items.iter().enumerate() {
                if n > 0 {
                    push(&mut out, ", ");
                }
                out.extend(repr(k)?);
                push(&mut out, ": ");
                out.extend(repr(v)?);
            }
            push(&mut out, "}");
        }
        _ => {}
    }
    Ok(out)
}
