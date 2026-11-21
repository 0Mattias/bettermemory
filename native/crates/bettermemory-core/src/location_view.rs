//! `location.py`'s view continuity (1020-1185): `_git_dir_at`,
//! `continuity`, `_head_at`, `view_state`, `_carries_project_file` and
//! `_same_number`.
//!
//! What the reference reaches through other modules not yet ported comes
//! in as arguments ([`Effects`]): `object_key(path)`, `_at_dotgit` (its
//! found git directory, or None where it finds none or raises `GitDies`),
//! `project_file.parse` (whether it answered a `ProjectFile`),
//! `RepositoryHistory(common).holds` and the peel of a HEAD outside
//! `refs/heads/`. Each of the first four answers a `Result`: where the
//! reference's callee raises what `view_state` lets propagate (an
//! OSError or a ValueError of `_at_dotgit`'s `judge` or `_common_of`,
//! for one), the effect answers `Err` ([`Declined::Raised`]) and
//! `view_state` declines with it. `continuity`'s `holds`, `confirms` and
//! `reminted` are closures called lazily in the reference's order, each
//! able to decline.
//!
//! An init stamp is compared as a Python tuple is ([`py_eq`]): `True`,
//! `1` and `1.0` are one value. A stamp holding a list or a dict is
//! unhashable, where the reference raises TypeError
//! ([`Declined::Unhashable`]). The two sides meet through a hash set of
//! each stamp's canonical bytes, as the reference's set intersection
//! does, in time linear in the stamps.

use crate::capture::{Key, Kind};
use crate::checkout_token::read::{read, Fault};
use crate::checkout_token::{lineage, succeeds, Raised};
use crate::config;
use crate::fsdata::{Flavour, Fs, StatResult};
use crate::gitfile_rule::gitdir::{Ctx, Failure};
use crate::gitfile_rule::hostpath;
use crate::gitfile_rule::trust::common_of;
use crate::githead::discovery::pure;
use crate::githead::RefFs;
use crate::location_git::{resolve_head, Git};
use crate::location_probe::{self, path_state, Host, PathState};
use crate::location_stamps::{self, init_stamps, Platform, StampError};
use crate::pyvalue::PyValue;

/// `project_file.PROJECT_FILE_NAME`.
pub const PROJECT_FILE_NAME: &[u8] = b"bettermemory.toml";

/// The witness that decided `continuity`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Witness {
    Remint,
    Token,
    LastHead,
    InitStamps,
    SameObject,
    NothingObservable,
}

impl Witness {
    /// The witness as the reference spells it.
    pub fn name(self) -> &'static str {
        match self {
            Witness::Remint => "remint",
            Witness::Token => "token",
            Witness::LastHead => "last_head",
            Witness::InitStamps => "init_stamps",
            Witness::SameObject => "same_object",
            Witness::NothingObservable => "nothing_observable",
        }
    }
}

/// Where the reference raises, or where the port gives no answer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Declined {
    /// TypeError: a stamp holding a list or a dict.
    Unhashable,
    /// `_path_state` declined (an errno whose text is not ported).
    PathState(location_probe::Declined),
    /// `_common_of` raised or is not ported (the Windows flavour).
    Common(Failure),
    /// `checkout_token.read` raised or is not ported.
    Token(Fault),
    /// `checkout_token.succeeds` raised.
    Succeeds(Raised),
    /// `_init_stamps` raised or is not ported.
    Stamps(StampError),
    /// A path that is not UTF-8 where the port spells it as text.
    NotText,
    /// `_is_link` of a non-link whose reparse tag is not ported (Windows).
    NotPorted,
    /// An effect raised what the reference lets propagate out of
    /// `view_state` (an OSError or a ValueError of `_at_dotgit`,
    /// `object_key`, `project_file.parse` or `RepositoryHistory.holds`).
    Raised(String),
}

/// One side of `continuity`: a token, a HEAD and init stamps.
#[derive(Clone, Copy, Debug)]
pub struct Side<'s> {
    pub token: Option<&'s str>,
    pub head: Option<&'s str>,
    pub stamps: &'s [Vec<PyValue>],
}

/// `continuity`'s callables. `holds` is given `[last_head]` and answers
/// whether what it returns is truthy, or declines where it raises.
pub struct Asks<'f> {
    pub holds: &'f mut dyn FnMut(&[&str]) -> Result<bool, Declined>,
    pub confirms: Option<&'f mut dyn FnMut() -> Result<bool, Declined>>,
    pub reminted: Option<&'f mut dyn FnMut() -> Result<bool, Declined>>,
}

/// Python's `==` of two values as a set of tuples compares them: numbers
/// across `bool`, `int` and `float` by value, tuples and lists by item.
/// It walks with its own stack, so no depth of nesting exhausts the
/// thread's.
pub fn py_eq(a: &PyValue, b: &PyValue) -> bool {
    let mut stack = vec![(a, b)];
    while let Some((a, b)) = stack.pop() {
        let same = match (num(a), num(b)) {
            (Some(x), Some(y)) => x == y,
            (None, None) => match (a, b) {
                (PyValue::Tuple(x), PyValue::Tuple(y)) | (PyValue::List(x), PyValue::List(y)) => {
                    stack.extend(x.iter().zip(y));
                    x.len() == y.len()
                }
                _ => a == b,
            },
            _ => false,
        };
        if !same {
            return false;
        }
    }
    true
}

/// A number as Python compares it: an `int` (or `bool`) by its decimal
/// text, a float that is a whole number by its exact decimal text.
enum Num<'v> {
    Int(std::borrow::Cow<'v, str>),
    Float(f64),
}

fn num(v: &PyValue) -> Option<Num<'_>> {
    match v {
        PyValue::Bool(b) => Some(Num::Int(if *b { "1" } else { "0" }.into())),
        PyValue::Int(text) => Some(Num::Int(text.as_str().into())),
        PyValue::Float(f) => Some(Num::Float(*f)),
        _ => None,
    }
}

impl PartialEq for Num<'_> {
    fn eq(&self, other: &Num<'_>) -> bool {
        match (self, other) {
            (Num::Int(x), Num::Int(y)) => x == y,
            (Num::Float(x), Num::Float(y)) => x == y,
            (Num::Int(x), Num::Float(f)) | (Num::Float(f), Num::Int(x)) => {
                f.is_finite() && f.fract() == 0.0 && whole(*f) == *x
            }
        }
    }
}

/// A whole finite float's exact decimal text, `-0.0` as `0`.
fn whole(f: f64) -> String {
    if f == 0.0 {
        "0".to_owned()
    } else {
        format!("{f:.0}")
    }
}

fn chunk(out: &mut Vec<u8>, tag: u8, body: &[u8]) {
    out.push(tag);
    out.extend_from_slice(&(body.len() as u64).to_le_bytes());
    out.extend_from_slice(body);
}

/// A stamp's canonical bytes: two stamps the reference's set holds as one
/// value give the same bytes, and no others do. A number is its integer
/// text where it is one (`True`, `1` and `1.0` alike), else its float's
/// bits; a NaN is given bytes of its own (`nan`, counted up), since a
/// NaN equals no other object. A list or a dict anywhere declines
/// ([`Declined::Unhashable`]). The walk keeps its own stack.
fn canonical(stamp: &[PyValue], nan: &mut u64) -> Result<Vec<u8>, Declined> {
    let mut out = Vec::new();
    out.push(b't');
    out.extend_from_slice(&(stamp.len() as u64).to_le_bytes());
    let mut stack: Vec<&PyValue> = stamp.iter().rev().collect();
    while let Some(v) = stack.pop() {
        match v {
            PyValue::List(_) | PyValue::Dict(_) => return Err(Declined::Unhashable),
            PyValue::Tuple(items) => {
                out.push(b't');
                out.extend_from_slice(&(items.len() as u64).to_le_bytes());
                stack.extend(items.iter().rev());
            }
            PyValue::Bool(b) => chunk(&mut out, b'i', if *b { b"1" } else { b"0" }),
            PyValue::Int(text) => chunk(&mut out, b'i', text.as_bytes()),
            PyValue::Float(f) if f.is_nan() => {
                *nan += 1;
                chunk(&mut out, b'n', &nan.to_le_bytes());
            }
            PyValue::Float(f) if f.is_finite() && f.fract() == 0.0 => {
                chunk(&mut out, b'i', whole(*f).as_bytes());
            }
            PyValue::Float(f) => chunk(&mut out, b'f', &f.to_bits().to_le_bytes()),
            other => chunk(&mut out, b'o', format!("{other:?}").as_bytes()),
        }
    }
    Ok(out)
}

/// `{tuple(s) for s in stamps}` as canonical bytes.
fn stamp_set(
    stamps: &[Vec<PyValue>],
    nan: &mut u64,
) -> Result<std::collections::HashSet<Vec<u8>>, Declined> {
    stamps.iter().map(|s| canonical(s, nan)).collect()
}

fn truthy(text: Option<&str>) -> bool {
    text.is_some_and(|t| !t.is_empty())
}

/// `continuity(token, last_head, stamps, token_here=..., head_here=...,
/// stamps_here=..., holds=..., confirms=..., superseded=...,
/// reminted=...)`: whether the object continues the recorded checkout,
/// and the witness that decided.
pub fn continuity(
    mine: Side<'_>,
    here: Side<'_>,
    superseded: bool,
    asks: Asks<'_>,
) -> Result<(bool, Witness), Declined> {
    let Asks {
        holds,
        confirms,
        reminted,
    } = asks;
    let token = mine.token;
    if truthy(token) && here.token.is_some() && here.token != token {
        if let Some(reminted) = reminted {
            if reminted()? {
                return Ok((true, Witness::Remint));
            }
        }
    }
    if truthy(token) && superseded {
        if !truthy(here.token) {
            return Ok((false, Witness::Token));
        }
    } else if truthy(token) {
        return Ok((here.token == token, Witness::Token));
    }
    if let Some(last) = mine.head {
        let held = Some(last) == here.head || holds(&[last])?;
        return Ok((held, Witness::LastHead));
    }
    let mut nan = 0;
    let set_mine = stamp_set(mine.stamps, &mut nan)?;
    let set_there = stamp_set(here.stamps, &mut nan)?;
    if !set_mine.is_empty() && !set_there.is_empty() {
        let met = set_mine.iter().any(|s| set_there.contains(s));
        return Ok((met, Witness::InitStamps));
    }
    if let Some(confirms) = confirms {
        if confirms()? {
            return Ok((true, Witness::SameObject));
        }
    }
    if truthy(token) && here.token == token {
        return Ok((true, Witness::Token));
    }
    Ok((false, Witness::NothingObservable))
}

/// `_same_number(a, b)`: one device and inode number, birth aside.
pub fn same_number(a: Option<&Key>, b: Option<&Key>) -> bool {
    match (a, b) {
        (Some(a), Some(b)) => a.0 == b.0 && a.1 == b.1,
        _ => false,
    }
}

/// The machine `view_state` reads.
pub struct Machine<'a, F: Fs, H: Host + ?Sized> {
    /// The file system, its flavour and the working directory.
    pub ctx: Ctx<'a, F>,
    /// The same file system as `githead` reads refs.
    pub refs: &'a dyn RefFs,
    pub host: &'a H,
    pub platform: Platform,
    pub mountinfo: &'a [u8],
    /// `GIT_COMMON_DIR` as the environment holds it.
    pub common_env: Option<&'a [u8]>,
}

/// `_at_dotgit(path, dotgit, st)`'s found git directory.
pub type AtDotgit<'f> =
    dyn FnMut(&[u8], &[u8], &StatResult) -> Result<Option<Vec<u8>>, Declined> + 'f;

/// `object_key(path)`; `Err` where it raises.
pub type ObjectKey<'f> = dyn FnMut(&[u8]) -> Result<Option<Key>, Declined> + 'f;

/// `RepositoryHistory(common).holds(commits)`; `Err` where it raises.
pub type Holds<'f> = dyn FnMut(&[u8], &[&str]) -> Result<bool, Declined> + 'f;

/// What the reference reaches through modules not yet ported.
pub struct Effects<'f> {
    /// `object_key(path)`; `Err` where it raises.
    pub object_key: &'f mut ObjectKey<'f>,
    /// `_at_dotgit(path, dotgit, st)`'s found git directory: `Ok(None)`
    /// where it finds none or raises `GitDies`, `Err` where it raises
    /// anything else (an OSError or a ValueError of `judge` or
    /// `_common_of`), which the reference lets propagate.
    pub at_dotgit: &'f mut AtDotgit<'f>,
    /// Whether `project_file.parse(path)` answers a `ProjectFile`; `Err`
    /// where it raises.
    pub parse_project: &'f mut dyn FnMut(&[u8]) -> Result<bool, Declined>,
    /// `RepositoryHistory(common).holds(commits)`: whether it holds any;
    /// `Err` where it raises.
    pub holds: &'f mut Holds<'f>,
    /// The peel of a HEAD's ref outside `refs/heads/` (`_resolve_head`).
    pub peel: &'f mut dyn FnMut(&str) -> Option<String>,
    /// The pause before `checkout_token.read` reads a cut file again.
    pub pause: &'f mut dyn FnMut(f64),
}

/// What the store recorded of the view's checkout.
#[derive(Clone, Copy, Debug)]
pub struct Recorded<'r> {
    pub key: Option<&'r Key>,
    pub token: Option<&'r str>,
    pub last_head: Option<&'r str>,
    pub stamps: &'r [Vec<PyValue>],
    pub common_key: Option<&'r Key>,
    pub superseded: bool,
}

/// `_git_dir_at(path, kind)`: the git directory at a view's path.
pub fn git_dir_at<F: Fs>(
    fs: &F,
    flavour: Flavour,
    path: &[u8],
    kind: Kind,
    at_dotgit: &mut AtDotgit<'_>,
) -> Result<Option<Vec<u8>>, Declined> {
    if kind == Kind::Bare {
        return Ok(Some(path.to_vec()));
    }
    let dotgit = hostpath::join(flavour, path, b".git");
    let Ok(st) = fs.lstat_result(&dotgit) else {
        return Ok(None);
    };
    if st.kind == crate::fsdata::Kind::Directory {
        match st.is_link() {
            Some(false) => return Ok(Some(dotgit)),
            Some(true) => {}
            None => return Err(Declined::NotPorted),
        }
    }
    at_dotgit(path, &dotgit, &st)
}

/// `_head_at(git_dir, common, root)`: the commit HEAD names there.
pub fn head_at(
    refs: &dyn RefFs,
    git_dir: &[u8],
    common: &[u8],
    root: Option<&[u8]>,
    peel: &mut dyn FnMut(&str) -> Option<String>,
) -> Option<String> {
    let git = Git {
        dotgit: None,
        git_dir: git_dir.to_vec(),
        common_dir: common.to_vec(),
        root: root.map(<[u8]>::to_vec),
        kind: Kind::Primary,
    };
    resolve_head(refs, &git, peel).0
}

/// `str(Path(folder) / PROJECT_FILE_NAME)` under `flavour`.
pub fn project_file_path(flavour: Flavour, folder: &[u8]) -> Result<Vec<u8>, Declined> {
    let joined = hostpath::join(flavour, folder, PROJECT_FILE_NAME);
    match flavour {
        Flavour::Posix => Ok(pure(&joined)),
        Flavour::Windows => {
            let text = std::str::from_utf8(&joined).map_err(|_| Declined::NotText)?;
            Ok(config::pure_path(text, config::Flavour::Windows).into_bytes())
        }
    }
}

/// `_carries_project_file(folder)`.
pub fn carries_project_file(
    flavour: Flavour,
    folder: &[u8],
    parse: &mut dyn FnMut(&[u8]) -> Result<bool, Declined>,
) -> Result<bool, Declined> {
    parse(&project_file_path(flavour, folder)?)
}

fn key_value(key: Option<&Key>) -> PyValue {
    match key {
        None => PyValue::None,
        Some((dev, ino, birth)) => PyValue::Tuple(vec![
            PyValue::Str(dev.clone()),
            PyValue::Str(ino.clone()),
            birth.clone().map_or(PyValue::None, PyValue::Str),
        ]),
    }
}

fn text_value(text: Option<&str>) -> PyValue {
    text.map_or(PyValue::None, |t| {
        PyValue::Str(t.chars().map(u32::from).collect())
    })
}

/// A stamp as the tuple `_init_stamps` gives.
pub fn stamp_row(s: &location_stamps::Stamp) -> Vec<PyValue> {
    vec![
        PyValue::Str(s.name.chars().map(u32::from).collect()),
        PyValue::Int(s.ino.to_string()),
        PyValue::Int(s.mtime_ns.to_string()),
        PyValue::Int(s.ctime_ns.to_string()),
        PyValue::Int(s.size.to_string()),
    ]
}

/// `view_state(path, kind, key, token, last_head=..., stamps=...,
/// common_key=..., superseded=...)`: ``live``, ``live_legacy``,
/// ``replaced``, ``gone`` or ``unreachable``.
pub fn view_state<F: Fs, H: Host + ?Sized>(
    m: &Machine<'_, F, H>,
    fx: Effects<'_>,
    path: &[u8],
    kind: Kind,
    rec: Recorded<'_>,
) -> Result<&'static str, Declined> {
    match path_state(m.ctx.fs, m.host, path).map_err(Declined::PathState)? {
        PathState::Gone(_) => return Ok("gone"),
        PathState::Unreachable(_) => return Ok("unreachable"),
        PathState::Present(_) => {}
    }
    let flavour = m.ctx.flavour;
    let (git_dir, here) = if kind == Kind::Folder {
        (None, (fx.object_key)(path)?)
    } else {
        let git_dir = git_dir_at(m.ctx.fs, flavour, path, kind, fx.at_dotgit)?;
        let here = match git_dir.as_deref() {
            Some(g) => (fx.object_key)(g)?,
            None => None,
        };
        (git_dir, here)
    };
    let Some(here) = here else {
        return Ok("replaced");
    };
    let Some(key) = rec.key else {
        return Ok(if git_dir.is_some() {
            "live_legacy"
        } else {
            "replaced"
        });
    };
    if key.0 != here.0 || key.1 != here.1 {
        return Ok("replaced");
    }
    if let (Some(a), Some(b)) = (&key.2, &here.2) {
        return Ok(if a == b { "live" } else { "replaced" });
    }
    let Some(git_dir) = git_dir else {
        let parse = fx.parse_project;
        let mut confirms = || carries_project_file(flavour, path, parse);
        let none = Side {
            token: None,
            head: None,
            stamps: &[],
        };
        let (ok, _) = continuity(
            none,
            none,
            false,
            Asks {
                holds: &mut |_| Ok(false),
                confirms: Some(&mut confirms),
                reminted: None,
            },
        )?;
        return Ok(if ok { "live" } else { "replaced" });
    };
    let (common, linked) = common_of(&m.ctx, &git_dir, m.common_env).map_err(Declined::Common)?;
    let git_text = std::str::from_utf8(&git_dir).map_err(|_| Declined::NotText)?;
    let found = read(&m.ctx, git_text, fx.pause)
        .map_err(Declined::Token)?
        .token;
    let recorded = lineage(&text_value(rec.token), &key_value(Some(key)));
    let root = if kind == Kind::Bare { None } else { Some(path) };
    let head_here = head_at(m.refs, &git_dir, &common, root, fx.peel);
    let machine = location_stamps::Machine {
        fs: m.ctx.fs,
        platform: m.platform,
        mountinfo: m.mountinfo,
    };
    let stamps_here: Vec<Vec<PyValue>> =
        init_stamps(&machine, flavour, &git_dir, kind == Kind::Linked)
            .map_err(Declined::Stamps)?
            .iter()
            .map(stamp_row)
            .collect();
    let holds = fx.holds;
    let object_key = fx.object_key;
    let mut held = |commits: &[&str]| holds(&common, commits);
    let mut confirms =
        || Ok(linked && same_number((object_key)(&common)?.as_ref(), rec.common_key));
    let found_value = text_value(found.as_deref());
    let mut reminted = || succeeds(&found_value, recorded.as_ref()).map_err(Declined::Succeeds);
    let (ok, _) = continuity(
        Side {
            token: rec.token,
            head: rec.last_head,
            stamps: rec.stamps,
        },
        Side {
            token: found.as_deref(),
            head: head_here.as_deref(),
            stamps: &stamps_here,
        },
        rec.superseded,
        Asks {
            holds: &mut held,
            confirms: Some(&mut confirms),
            reminted: Some(&mut reminted),
        },
    )?;
    Ok(if ok { "live" } else { "replaced" })
}
