//! Unit 4: the pin records as data, and the follow of a `.git` entry
//! (`gitfile_rule.py` 147-219 and 702-905; `gitdir_pin.py`'s `recorded`
//! and `_read` after the file is found): [`gitfile_refusal`] and
//! [`refusal`] (`_refusal`), the recorded reading ([`recorded_reading`],
//! [`spells`], `_read_recorded`), [`earliest_naming`] (with `_view`),
//! [`names_git_dir`] and [`entry_names`].
//!
//! Nothing ambient: the record store is data ([`Records`], looked up by
//! the git directory's stat as `gitdir_pin._name` names a file by its
//! device and inode), whether a recorded path answers within the budget is
//! data ([`Answers`], unit 1), `GIT_COMMON_DIR` is an argument, the git on
//! PATH is [`GitFacts`]. Every path goes through [`Ctx::at`]; the working
//! directory is the caller's contract (unit 2b).
//!
//! A record's paths are Python `str`s ([`PinPaths`]): JSON may hold any
//! code point, a lone surrogate included, and the reference encodes them
//! with `surrogateescape` only where it asks the OS ([`fsencode`]). One it
//! cannot encode fails that call with ValueError, which the reference
//! catches there, so it reads as nothing found. Where the reference
//! compares a record's `str` with a decoded path (`_spells`), the path is
//! decoded and the two compared as code points.
//!
//! The Windows flavour is declined whole ([`Failure::NotPorted`]):
//! `normcase`, `realpath`, `samestat` and unit 3's rules are not ported
//! there. A record's own text is read under either ([`pin_text_under`]).

use crate::fsdata::realpath::{realpath, Unresolved};
use crate::fsdata::{samestat, Flavour, Fs, Kind, OsError, StatResult};
use crate::gitsystem::record::{surrogateescape, surrogatepass};
use crate::json::{loads_detailed, LoadError};
use crate::pyvalue::{PyStr, PyValue};
use crate::records::rows::decode_json_bytes;

use super::bounded::{bounded_read, bounded_reads, Answers, Job, Read};
use super::format::GitFacts;
use super::gitdir::{Ctx, Failure};
use super::hostpath;
use super::identity::{is_link, names_folder, names_here, same_folder, working_tree_of};
use super::probe::{path_gone, Raised};
use super::trust::{common_of, commondir, not_registered, tree_format, tree_refusal, Refusal};
use super::{parse_gitfile, small_text, GITFILE_LIMIT};

/// `UNREAD_TREE`: why a recorded tree refuses a follow it cannot tell.
pub const UNREAD_TREE: [&[u8]; 2] = [b"whose .git could not be read", b"which did not answer"];
/// `gitdir_pin.PIN_FORMAT`.
pub const PIN_FORMAT: i64 = 1;
/// `gitdir_pin._MAX_BYTES`: a record file past this is not read.
pub const PIN_MAX_BYTES: usize = 1 << 16;

/// The record store, as data: the bytes of the record file for the git
/// directory whose `stat` is `git_dir` (`pins_dir() / _name(stat)`), as
/// `open(path, "rb").read(_MAX_BYTES + 1)` reads them, or None where that
/// raises an OSError.
pub trait Records {
    fn record(&self, git_dir: &StatResult) -> Option<Vec<u8>>;
}

/// No record anywhere.
pub struct NoRecords;

impl Records for NoRecords {
    fn record(&self, _: &StatResult) -> Option<Vec<u8>> {
        None
    }
}

/// A record's two paths, as the `str`s JSON gave.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PinPaths {
    pub git_dir: PyStr,
    pub tree: PyStr,
}

/// What a record file's bytes are (`gitdir_pin._read` after its read).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PinText {
    Pin(PinPaths),
    /// Not a record: over the bound, not JSON, another format, a path
    /// that is not a `str` or not absolute.
    NotAPin,
    /// An exception `_read` does not catch (RecursionError).
    Raised(&'static str),
}

fn is_one(value: Option<&PyValue>) -> bool {
    match value {
        Some(PyValue::Int(text)) => text == "1",
        Some(PyValue::Float(f)) => *f == 1.0,
        Some(PyValue::Bool(b)) => *b,
        _ => false,
    }
}

fn absolute_str(value: Option<&PyValue>, flavour: Flavour) -> Option<PyStr> {
    match value {
        Some(PyValue::Str(s)) if hostpath::isabs_str(flavour, s) => Some(s.clone()),
        _ => None,
    }
}

/// `gitdir_pin._read`'s judgement of the bytes read where `os.path` is
/// `posixpath`: [`pin_text_under`] with [`Flavour::Posix`], the reading
/// [`recorded`] makes (it declines Windows before).
pub fn pin_text(data: &[u8]) -> PinText {
    pin_text_under(data, Flavour::Posix)
}

/// `gitdir_pin._read`'s judgement of the bytes read, with `os.path.isabs`
/// the flavour's ([`hostpath::isabs_str`]). On Windows a record whose
/// path is rooted without a drive (`/g`) is not a record: `ntpath.isabs`
/// reads such a path as relative from CPython 3.13, as absolute in 3.11
/// and 3.12. This is 3.13's reading.
pub fn pin_text_under(data: &[u8], flavour: Flavour) -> PinText {
    if data.len() > PIN_MAX_BYTES {
        return PinText::NotAPin;
    }
    let Ok(text) = decode_json_bytes(data) else {
        return PinText::NotAPin;
    };
    let body = match loads_detailed(&text) {
        Ok(body) => body,
        Err(LoadError::Recursion) => return PinText::Raised("RecursionError"),
        Err(_) => return PinText::NotAPin,
    };
    // Without the dict test the answer is the same: `get` of a non-dict is None.
    if !matches!(body, PyValue::Dict(_)) || !is_one(body.get("format")) {
        return PinText::NotAPin;
    }
    let (Some(git_dir), Some(tree)) = (
        absolute_str(body.get("git_dir"), flavour),
        absolute_str(body.get("tree"), flavour),
    ) else {
        return PinText::NotAPin;
    };
    PinText::Pin(PinPaths { git_dir, tree })
}

/// `os.fsencode` on POSIX (UTF-8, `surrogateescape`): None where a code
/// point cannot be encoded (a surrogate outside U+DC80-U+DCFF), where the
/// reference's call raises UnicodeEncodeError, a ValueError.
pub fn fsencode(text: &[u32]) -> Option<Vec<u8>> {
    let mut out = Vec::with_capacity(text.len());
    for &cp in text {
        if (0xDC80..=0xDCFF).contains(&cp) {
            out.push((cp - 0xDC00) as u8);
        } else {
            let c = char::from_u32(cp)?;
            let mut buf = [0; 4];
            out.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
        }
    }
    Some(out)
}

/// A path for a reason's text where the reference's f-string holds it:
/// [`fsencode`], or, where that fails, each code point it can encode so
/// and each other surrogate encoded as itself (`surrogatepass`), so the
/// surrogate the `str` holds is kept.
pub fn shown(text: &[u32]) -> Vec<u8> {
    fsencode(text).unwrap_or_else(|| {
        let mut out = Vec::with_capacity(text.len());
        for &cp in text {
            match fsencode(&[cp]) {
                Some(bytes) => out.extend(bytes),
                None => out.extend(surrogatepass(&[cp])),
            }
        }
        out
    })
}

fn posix<F: Fs + ?Sized>(ctx: &Ctx<'_, F>) -> Result<(), Failure> {
    match ctx.flavour {
        Flavour::Posix => Ok(()),
        Flavour::Windows => Err(Failure::NotPorted),
    }
}

fn join(a: &[u8], b: &[u8]) -> Vec<u8> {
    hostpath::join(Flavour::Posix, a, b)
}

fn stat<F: Fs + ?Sized>(ctx: &Ctx<'_, F>, path: &[u8]) -> Result<Option<StatResult>, Failure> {
    let at = ctx.at(path)?;
    Ok(ctx.fs.stat(&at).ok())
}

/// `gitdir_pin.recorded(git_dir)`: the record for `git_dir`, or None:
/// nothing recorded, `git_dir` gone, the record not one, or one whose git
/// directory is no longer `git_dir`'s object.
pub fn recorded<F: Fs + ?Sized, R: Records + ?Sized>(
    ctx: &Ctx<'_, F>,
    records: &R,
    git_dir: &[u8],
) -> Result<Option<PinPaths>, Failure> {
    posix(ctx)?;
    let Some(here) = stat(ctx, git_dir)? else {
        return Ok(None);
    };
    let Some(data) = records.record(&here) else {
        return Ok(None);
    };
    let pin = match pin_text(&data) {
        PinText::Pin(pin) => pin,
        PinText::NotAPin => return Ok(None),
        PinText::Raised(name) => return Err(Failure::Raised(name)),
    };
    let Some(theirs) = fsencode(&pin.git_dir) else {
        return Ok(None);
    };
    let Some(there) = stat(ctx, &theirs)? else {
        return Ok(None);
    };
    match samestat(&there, &here) {
        Some(true) => Ok(Some(pin)),
        Some(false) => Ok(None),
        None => Err(Failure::NotPorted),
    }
}

/// What a `.git` entry names against a git directory (`_entry_names`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Names {
    Names,
    Other,
    Gone,
    Unknown,
}

impl Names {
    pub fn word(self) -> &'static str {
        match self {
            Names::Names => "names",
            Names::Other => "other",
            Names::Gone => "gone",
            Names::Unknown => "unknown",
        }
    }
}

fn gone_or_unknown(exc: Raised) -> Names {
    if path_gone(exc) {
        Names::Gone
    } else {
        Names::Unknown
    }
}

/// `_entry_names(entry, git_dir)`.
pub fn entry_names<F: Fs>(
    ctx: &Ctx<'_, F>,
    entry: &[u8],
    git_dir: &[u8],
) -> Result<Names, Failure> {
    posix(ctx)?;
    let at = ctx.at(entry)?;
    let lst = match ctx.fs.lstat_result(&at) {
        Ok(lst) => lst,
        Err(e) => return Ok(gone_or_unknown(Raised::from(e))),
    };
    if is_link(&lst)? {
        if let Some(owner) = working_tree_of(ctx, entry)? {
            let folder = hostpath::dirname(Flavour::Posix, entry);
            if same_folder(ctx, &owner, &folder)? != Some(true) {
                return Ok(Names::Other);
            }
        }
    }
    let st = match ctx.fs.stat(&at) {
        Ok(st) => st,
        Err(e) => return Ok(gone_or_unknown(Raised::from(e))),
    };
    let want = match ctx.fs.stat(&ctx.at(git_dir)?) {
        Ok(want) => want,
        Err(e) => return Ok(gone_or_unknown(Raised::from(e))),
    };
    if st.kind == Kind::Directory {
        return match samestat(&st, &want) {
            Some(true) => Ok(Names::Names),
            Some(false) => Ok(Names::Other),
            None => Err(Failure::NotPorted),
        };
    }
    if st.kind != Kind::Regular {
        return Ok(Names::Other);
    }
    if st.placeholder() {
        return Ok(Names::Unknown);
    }
    let mut file = match ctx.fs.open_result(&at) {
        Ok(file) => file,
        Err(e) => return Ok(gone_or_unknown(Raised::from(e))),
    };
    let mut data = Vec::new();
    while data.len() <= GITFILE_LIMIT {
        let room = GITFILE_LIMIT + 1 - data.len();
        match ctx.fs.read(&mut file, room) {
            Ok(chunk) if chunk.is_empty() => break,
            Ok(chunk) => data.extend_from_slice(&chunk[..chunk.len().min(room)]),
            Err(e) => return Ok(gone_or_unknown(Raised::from(e))),
        }
    }
    let Some(named) = parse_gitfile(&data, entry, Flavour::Posix) else {
        return Ok(Names::Other);
    };
    Ok(
        if names_here(ctx, &named, git_dir, git_dir)? == Some(true) {
            Names::Names
        } else {
            Names::Other
        },
    )
}

/// `names_git_dir(entry, git_dir)`.
pub fn names_git_dir<F: Fs>(
    ctx: &Ctx<'_, F>,
    entry: &[u8],
    git_dir: &[u8],
) -> Result<bool, Failure> {
    Ok(entry_names(ctx, entry, git_dir)? == Names::Names)
}

/// `_spells(recorded, directory)`: the record spells the folder as given
/// or as resolved through its links.
pub fn spells<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    recorded: &[u32],
    directory: &[u8],
) -> Result<bool, Failure> {
    posix(ctx)?;
    // The reference compares `str`s: `directory` and its `realpath` are
    // `os.fsdecode`d paths, so they are compared as code points. A record
    // holding escaped surrogates that encode to a real UTF-8 name (U+DCC3
    // U+DCA9 for "\u{e9}") spells no decoded path, though its bytes match.
    if recorded == surrogateescape(directory).as_slice() {
        return Ok(true);
    }
    match realpath(ctx.fs, ctx.flavour, directory, ctx.cwd) {
        Ok(real) => Ok(recorded == surrogateescape(&real).as_slice()),
        Err(Unresolved::NotPorted) => Err(Failure::NotPorted),
        Err(Unresolved::Raised(_)) => Ok(false),
    }
}

fn why(parts: &[&[u8]]) -> Option<Refusal> {
    Some(Refusal {
        why: parts.concat(),
        format_unknown: false,
    })
}

/// `_read_recorded(pin, target, directory)`.
fn read_recorded<F: Fs>(
    ctx: &Ctx<'_, F>,
    pin: &PinPaths,
    target: &[u8],
    directory: &[u8],
) -> Result<(Option<Refusal>, bool), Failure> {
    // A tree it cannot encode: every call raises ValueError, which
    // `_same_folder` reads as False and `_entry_names` as gone.
    let Some(tree) = fsencode(&pin.tree) else {
        return Ok((None, false));
    };
    if same_folder(ctx, &tree, directory)? == Some(true) {
        return Ok((None, true));
    }
    Ok(match entry_names(ctx, &join(&tree, b".git"), target)? {
        Names::Names => (
            why(&[target, b" is the git directory of the working tree ", &tree]),
            false,
        ),
        Names::Unknown => (
            why(&[
                target,
                b" is recorded as the git directory of the working tree ",
                &tree,
                b", ",
                UNREAD_TREE[0],
            ]),
            false,
        ),
        Names::Other | Names::Gone => (None, false),
    })
}

/// `_recorded_reading(pin, target, directory)`: why the git directory
/// kept apart `target` is not followed from `directory` by `pin`, and
/// whether `pin` names `directory`.
pub fn recorded_reading<F: Fs, A: Answers + ?Sized>(
    ctx: &Ctx<'_, F>,
    answers: &A,
    pin: Option<&PinPaths>,
    target: &[u8],
    directory: &[u8],
) -> Result<(Option<Refusal>, bool), Failure> {
    posix(ctx)?;
    let Some(pin) = pin else {
        return Ok((None, false));
    };
    if spells(ctx, &pin.tree, directory)? {
        return Ok((None, true));
    }
    let tree = shown(&pin.tree);
    match bounded_read(answers, &tree, || {
        read_recorded(ctx, pin, target, directory)
    })? {
        Read::Value(answer) => Ok(answer),
        Read::Unanswered | Read::NotRead => Ok((
            why(&[
                target,
                b" is recorded as the git directory of the working tree ",
                &tree,
                b", ",
                UNREAD_TREE[1],
            ]),
            false,
        )),
    }
}

/// `_refusal(directory, target, common)`: why the `.git` entry in
/// `directory` naming the git directory `target` is not followed, and
/// whether a follow is provisional. `env` is `GIT_COMMON_DIR`.
#[allow(clippy::too_many_arguments)]
pub fn refusal<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    ctx: &Ctx<'_, F>,
    answers: &A,
    records: &R,
    directory: &[u8],
    target: &[u8],
    common: Option<(Vec<u8>, bool)>,
    env: Option<&[u8]>,
    git: &GitFacts,
) -> Result<(Option<Refusal>, bool), Failure> {
    posix(ctx)?;
    let (_, linked) = common_of(ctx, target, env)?;
    let named = if linked {
        None
    } else {
        commondir(ctx, target)?
    };
    if let Some(named) = named {
        return Ok((why(&[&not_registered(target, &named)]), false));
    }
    let found = tree_format(ctx, target, common, git)?;
    if linked {
        let back = small_text(ctx, &join(target, b"gitdir"))?.unwrap_or_default();
        if back.is_empty() {
            return Ok((
                why(&[
                    target,
                    b" is a worktree's git directory that names no worktree",
                ]),
                false,
            ));
        }
        if names_folder(ctx, back.as_bytes(), target, directory)? == Some(false) {
            return Ok((
                why(&[
                    target,
                    b" is the git directory of the worktree at ",
                    back.as_bytes(),
                ]),
                false,
            ));
        }
        return Ok((tree_refusal(ctx, target, &found, directory, false)?, false));
    }
    let refused = tree_refusal(ctx, target, &found, directory, true)?;
    if refused.is_some() || found.worktree.is_some() {
        return Ok((refused, false));
    }
    if let Some(tree) = working_tree_of(ctx, target)? {
        if same_folder(ctx, &tree, directory)? != Some(true) {
            return Ok((
                why(&[target, b" is the git directory of the working tree ", &tree]),
                false,
            ));
        }
    }
    let pin = recorded(ctx, records, target)?;
    let (refused, here) = recorded_reading(ctx, answers, pin.as_ref(), target, directory)?;
    let provisional = refused.is_none() && !here;
    Ok((refused, provisional))
}

/// `gitfile_refusal(directory, dotgit, target)`.
pub fn gitfile_refusal<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    ctx: &Ctx<'_, F>,
    answers: &A,
    records: &R,
    directory: &[u8],
    target: &[u8],
    env: Option<&[u8]>,
    git: &GitFacts,
) -> Result<Option<Refusal>, Failure> {
    Ok(refusal(ctx, answers, records, directory, target, None, env, git)?.0)
}

/// `_view(view, tree, git_dir)`: `here`, or what the view's `.git` names
/// with the folder resolved where it names the git directory.
fn view<F: Fs>(
    ctx: &Ctx<'_, F>,
    view: &[u8],
    tree: Option<&[u8]>,
    git_dir: &[u8],
) -> Result<(Option<Names>, Vec<u8>), Failure> {
    if let Some(tree) = tree {
        if same_folder(ctx, view, tree)? == Some(true) {
            return Ok((None, view.to_vec()));
        }
    }
    let seen = entry_names(ctx, &join(view, b".git"), git_dir)?;
    if seen != Names::Names {
        return Ok((Some(seen), view.to_vec()));
    }
    match realpath(ctx.fs, ctx.flavour, view, ctx.cwd) {
        Ok(real) => Ok((Some(seen), real)),
        Err(Unresolved::NotPorted) => Err(Failure::NotPorted),
        // Non-strict `realpath` raises ValueError alone (a NUL); any other
        // class it reports rises as that class.
        Err(Unresolved::Raised(class)) => Err(Failure::from(OsError {
            class,
            errno: None,
            winerror: None,
        })),
    }
}

/// `earliest_naming(views, tree, git_dir)`: the folder to record in place
/// of `tree` from the store's views, earliest first; None where none
/// names `git_dir`, or where one before them is `tree` itself.
pub fn earliest_naming<F: Fs, A: Answers + ?Sized>(
    ctx: &Ctx<'_, F>,
    answers: &A,
    views: &[Vec<u8>],
    tree: Option<&[u8]>,
    git_dir: &[u8],
) -> Result<Option<Vec<u8>>, Failure> {
    posix(ctx)?;
    let jobs: Vec<Job<'_, _, Failure>> = views
        .iter()
        .map(|v| {
            let read: Box<dyn FnOnce() -> Result<_, Failure> + '_> =
                Box::new(move || view(ctx, v, tree, git_dir));
            (v.clone(), read)
        })
        .collect();
    let seen = bounded_reads(answers, jobs, false)?;
    for (v, seen) in views.iter().zip(seen) {
        match seen {
            Read::Unanswered | Read::NotRead => return Ok(Some(v.clone())),
            Read::Value((None, _)) => return Ok(None),
            Read::Value((Some(Names::Unknown), _)) => return Ok(Some(v.clone())),
            Read::Value((Some(Names::Names), path)) => return Ok(Some(path)),
            Read::Value((Some(_), _)) => {}
        }
    }
    Ok(None)
}
