//! Unit 3: a git directory's format from its files, and the trust of a
//! `commondir` (`gitfile_rule.py` 222-232, 291-395, 550-573, 599-699,
//! 985-1059): where git puts the working tree by the config
//! ([`tree_format`]), the format of a common directory
//! ([`common_format`], [`object_format_at`], [`reads_worktree_config`]),
//! the refusals that follow from it ([`tree_refusal`], [`own_refusal`],
//! [`inside_reading`]), and the common directory membership trusts
//! ([`common_of`], [`commondir`], [`foreign_commondir`], [`registered`],
//! [`is_git_directory`]).
//!
//! Config is read through [`read_git_config`] and judged by unit 1's
//! [`judge_format`]; the git on PATH is data ([`GitFacts`]);
//! `GIT_COMMON_DIR` is an argument, never read from the environment.
//! Every path reaches the file system through [`Ctx::at`]; the working
//! directory is the caller's contract (unit 2b). A reason is the
//! reference's text, as bytes (its `str` encoded as the paths were).
//!
//! The Windows flavour is declined whole ([`Failure::NotPorted`]): the
//! unit leans on `ntpath`'s joins and `basename`, on `realpath` and
//! `samefile`, none of them ported.

use crate::fsdata::realpath::{realpath, Unresolved};
use crate::fsdata::{samefile, Flavour, Fs, Kind};
use crate::gitconfig::file::read_git_config;
use crate::gitconfig::{git_bool, spaced, Entry, KEEPS_SPACES_SINCE};

use super::format::{judge_format, Format, GitFacts};
use super::gitdir::{git_common, Ctx, Failure};
use super::hostpath;
use super::identity::names_here;
use super::{stripped_text, GITFILE_LIMIT};

const BARE: &[u8] = b"core.bare";
const WORKTREE: &[u8] = b"core.worktree";

/// `_FORMAT_NAMES`: the names a format's reading keeps.
fn format_names() -> Vec<Vec<u8>> {
    [
        &b"core.repositoryformatversion"[..],
        b"extensions.worktreeconfig",
        BARE,
        WORKTREE,
        b"extensions.",
    ]
    .iter()
    .map(|n| n.to_vec())
    .collect()
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

fn words(parts: &[&[u8]]) -> Vec<u8> {
    parts.concat()
}

/// `TreeFormat`: where a git directory's own config puts its working tree.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct TreeFormat {
    /// Why git's read of it is not known.
    pub why: Option<Vec<u8>>,
    /// The `core.worktree` git takes, cut at a NUL.
    pub worktree: Option<Vec<u8>>,
    /// Whether the `core.bare` git takes is true.
    pub bare: bool,
}

impl TreeFormat {
    fn why(why: Vec<u8>) -> TreeFormat {
        TreeFormat {
            why: Some(why),
            ..TreeFormat::default()
        }
    }
}

/// `read_git_config(path, _FORMAT_NAMES)`, the path opened through
/// [`Ctx::at`] and named in a reason as given.
fn read_format_config<F: Fs>(
    ctx: &Ctx<'_, F>,
    path: &[u8],
) -> Result<Result<Vec<Entry>, Vec<u8>>, Failure> {
    let at = ctx.at(path)?;
    let mut read = read_git_config(ctx.fs, &at, Some(&format_names()));
    if let Some((named, _)) = read.why.as_mut() {
        *named = path.to_vec();
    }
    Ok(match read.why_text() {
        Some(why) => Err(why),
        None => Ok(read.entries),
    })
}

/// `_common_format(common_dir)`: the format the config in `common_dir`
/// gives, or why it is not known.
pub fn common_format<F: Fs>(
    ctx: &Ctx<'_, F>,
    common_dir: &[u8],
    git: &GitFacts,
) -> Result<Result<Format, Vec<u8>>, Failure> {
    posix(ctx)?;
    let path = join(common_dir, b"config");
    Ok(match read_format_config(ctx, &path)? {
        Err(why) => Err(why),
        Ok(entries) => judge_format(&path, &entries, git),
    })
}

/// `object_format_at(common_dir)`.
pub fn object_format_at<F: Fs>(
    ctx: &Ctx<'_, F>,
    common_dir: &[u8],
    git: &GitFacts,
) -> Result<Option<Vec<u8>>, Failure> {
    Ok(match common_format(ctx, common_dir, git)? {
        Err(_) => None,
        Ok(read) if read.version > 1 => None,
        Ok(read) if read.version == -1 => Some(b"sha1".to_vec()),
        Ok(read) => Some(read.object_format),
    })
}

/// `reads_worktree_config(common_dir)`.
pub fn reads_worktree_config<F: Fs>(
    ctx: &Ctx<'_, F>,
    common_dir: &[u8],
    git: &GitFacts,
) -> Result<Option<bool>, Failure> {
    Ok(match common_format(ctx, common_dir, git)? {
        Err(_) => None,
        Ok(read) if read.version > 1 => None,
        Ok(read) if read.worktree_config && read.version < -1 => git.is_at_least((2, 42)),
        Ok(read) => Some(read.worktree_config && read.version != -1),
    })
}

/// `tree_format(git_dir, common)`: where git puts the working tree of
/// `git_dir` by its own config; `common` is `_git_common`'s answer where
/// the caller has it.
pub fn tree_format<F: Fs>(
    ctx: &Ctx<'_, F>,
    git_dir: &[u8],
    common: Option<(Vec<u8>, bool)>,
    git: &GitFacts,
) -> Result<TreeFormat, Failure> {
    posix(ctx)?;
    let (where_, has_common) = match common {
        Some(common) => common,
        None => match git_common(ctx, git_dir) {
            Ok(common) => common,
            Err(Failure::Unsettled) => {
                let path = join(git_dir, b"commondir");
                return Ok(TreeFormat::why(words(&[b"git cannot read ", &path])));
            }
            Err(other) => return Err(other),
        },
    };
    let read = match common_format(ctx, &where_, git)? {
        Err(why) => return Ok(TreeFormat::why(why)),
        Ok(read) => read,
    };
    if read.version < 0 {
        return Ok(TreeFormat::default());
    }
    if read.version > 1 {
        let path = join(&where_, b"config");
        return Ok(TreeFormat::why(words(&[
            &path,
            b" sets a repository format version git does not read",
        ])));
    }
    let (mut bare, mut worktree) = (read.bare, read.worktree);
    if read.worktree_config {
        let path = join(git_dir, b"config.worktree");
        let entries = match read_format_config(ctx, &path)? {
            Err(why) => return Ok(TreeFormat::why(why)),
            Ok(entries) => entries,
        };
        for entry in &entries {
            let name = entry.name();
            let value = entry.value.as_deref();
            if name == BARE {
                if git_bool(value).is_none() {
                    return Ok(TreeFormat::why(dies(&path, &name)));
                }
                bare = Some(value.map(<[u8]>::to_vec));
            } else if name == WORKTREE {
                let Some(value) = value else {
                    return Ok(TreeFormat::why(dies(&path, &name)));
                };
                worktree = Some(value.to_vec());
            }
        }
    } else if has_common {
        return Ok(TreeFormat::default());
    }
    if let Some(worktree) = &worktree {
        if spaced(worktree) && git.is_at_least(KEEPS_SPACES_SINCE) != Some(true) {
            let path = join(&where_, b"config");
            return Ok(TreeFormat::why(words(&[
                &path,
                b" names a working tree holding blank space git before 2.45 reads as a space",
            ])));
        }
    }
    Ok(TreeFormat {
        why: None,
        worktree: worktree.map(|w| w.split(|&b| b == 0).next().unwrap_or(&[]).to_vec()),
        // Equivalent to `!= Some(false)`: a `core.bare` git_bool reads as None is refused when taken.
        bare: bare.is_some_and(|value| git_bool(value.as_deref()) == Some(true)),
    })
}

/// `_dies(path, name)`.
fn dies(path: &[u8], name: &[u8]) -> Vec<u8> {
    words(&[path, b" sets ", name, b" to a value git dies on"])
}

/// A refusal: its text, and whether it is a `FormatUnknown` (git dies,
/// or the format is not known).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Refusal {
    pub why: Vec<u8>,
    pub format_unknown: bool,
}

fn plain(why: Vec<u8>) -> Option<Refusal> {
    Some(Refusal {
        why,
        format_unknown: false,
    })
}

fn unknown(why: Vec<u8>) -> Option<Refusal> {
    Some(Refusal {
        why,
        format_unknown: true,
    })
}

/// `_tree_refusal(git_dir, found, directory, moved=...)`.
pub fn tree_refusal<F: Fs>(
    ctx: &Ctx<'_, F>,
    git_dir: &[u8],
    found: &TreeFormat,
    directory: &[u8],
    moved: bool,
) -> Result<Option<Refusal>, Failure> {
    posix(ctx)?;
    if let Some(why) = &found.why {
        return Ok(unknown(words(&[
            git_dir,
            b"'s config cannot be read as git reads it: ",
            why,
        ])));
    }
    if found.bare {
        return Ok(plain(words(&[
            git_dir,
            b"'s config names no working tree (core.bare)",
        ])));
    }
    let Some(worktree) = &found.worktree else {
        return Ok(None);
    };
    if worktree.is_empty() {
        return Ok(unknown(words(&[
            git_dir,
            b"'s config names no working tree (an empty core.worktree, on which git dies)",
        ])));
    }
    let here = names_here(ctx, worktree, git_dir, directory)?;
    if here == Some(true) || (here.is_none() && moved) {
        return Ok(None);
    }
    Ok(plain(named_tree(git_dir, worktree)))
}

fn named_tree(git_dir: &[u8], worktree: &[u8]) -> Vec<u8> {
    words(&[
        git_dir,
        b"'s config names the working tree ",
        worktree,
        b" (core.worktree)",
    ])
}

/// `own_refusal(dotgit, directory, common)`.
pub fn own_refusal<F: Fs>(
    ctx: &Ctx<'_, F>,
    dotgit: &[u8],
    directory: &[u8],
    common: Option<(Vec<u8>, bool)>,
    git: &GitFacts,
) -> Result<Option<Refusal>, Failure> {
    let found = tree_format(ctx, dotgit, common, git)?;
    tree_refusal(ctx, dotgit, &found, directory, false)
}

/// `inside_reading(git_dir)`: `(why, root)`.
pub fn inside_reading<F: Fs>(
    ctx: &Ctx<'_, F>,
    git_dir: &[u8],
    git: &GitFacts,
) -> Result<(Option<Refusal>, Option<Vec<u8>>), Failure> {
    let found = tree_format(ctx, git_dir, None, git)?;
    let parent = hostpath::dirname(Flavour::Posix, git_dir);
    let base = git_dir.rsplit(|&b| b == b'/').next().unwrap_or(git_dir);
    let dotgit = base == b".git";
    if found.why.is_none() && found.worktree.is_none() {
        let root = (dotgit && !found.bare).then_some(parent);
        return Ok((None, root));
    }
    let mut why = tree_refusal(ctx, git_dir, &found, &parent, false)?;
    if why.is_none() && !dotgit {
        why = plain(named_tree(
            git_dir,
            found.worktree.as_deref().unwrap_or_default(),
        ));
    }
    let root = why.is_none().then_some(parent);
    Ok((why, root))
}

/// The leading bytes of a file as `open(path, "rb").read(limit)` reads
/// them, or None where that raises an OSError; a ValueError (a NUL)
/// rises. The open does not block.
fn read_head<F: Fs>(
    ctx: &Ctx<'_, F>,
    path: &[u8],
    limit: usize,
) -> Result<Option<Vec<u8>>, Failure> {
    let at = ctx.at(path)?;
    let mut file = match ctx.fs.open(&at) {
        Ok(file) => file,
        Err(crate::fsdata::PyErr::Value) => return Err(Failure::Value),
        Err(_) => return Ok(None),
    };
    let Ok(stat) = ctx.fs.fstat(&mut file) else {
        return Ok(None);
    };
    // A folder's open raises IsADirectoryError; a FIFO's waits and a
    // socket's raises (the port answers None for both, native/README.md).
    // Without `Directory` the answer is the same: a folder's `read` fails (EISDIR), which is None.
    if matches!(stat.kind, Kind::Directory | Kind::Fifo | Kind::Socket) {
        return Ok(None);
    }
    let mut out = Vec::new();
    while out.len() < limit {
        let Ok(chunk) = ctx.fs.read(&mut file, limit - out.len()) else {
            return Ok(None);
        };
        if chunk.is_empty() {
            break;
        }
        let room = limit - out.len();
        out.extend_from_slice(&chunk[..chunk.len().min(room)]);
    }
    Ok(Some(out))
}

/// `_commondir(git_dir)`: the common directory `git_dir`'s `commondir`
/// file names, or None. The first `_GITFILE_LIMIT` bytes are used, a
/// longer file cut there, as the reference reads it.
pub fn commondir<F: Fs>(ctx: &Ctx<'_, F>, git_dir: &[u8]) -> Result<Option<Vec<u8>>, Failure> {
    posix(ctx)?;
    let Some(data) = read_head(ctx, &join(git_dir, b"commondir"), GITFILE_LIMIT)? else {
        return Ok(None);
    };
    let rel = stripped_text(&data);
    if rel.is_empty() {
        return Ok(None);
    }
    Ok(Some(hostpath::normpath(
        Flavour::Posix,
        &join(git_dir, rel.as_bytes()),
    )))
}

/// `_registered(git_dir, common)`: whether `realpath(git_dir)`'s folder
/// is `common/worktrees`, compared as files.
pub fn registered<F: Fs>(ctx: &Ctx<'_, F>, git_dir: &[u8], common: &[u8]) -> Result<bool, Failure> {
    posix(ctx)?;
    let real = match realpath(ctx.fs, ctx.flavour, git_dir, ctx.cwd) {
        Ok(real) => real,
        Err(Unresolved::NotPorted) => return Err(Failure::NotPorted),
        Err(Unresolved::Raised(_)) => return Ok(false),
    };
    let parent = hostpath::dirname(Flavour::Posix, &real);
    let worktrees = ctx.at(&join(common, b"worktrees"))?;
    match samefile(ctx.fs, &ctx.at(&parent)?, &worktrees) {
        Ok(Some(same)) => Ok(same),
        Ok(None) => Err(Failure::NotPorted),
        Err(_) => Ok(false),
    }
}

/// `_common_of(git_dir)` with `GIT_COMMON_DIR` as `env` (an empty one is
/// unset): `(common, named)`.
pub fn common_of<F: Fs>(
    ctx: &Ctx<'_, F>,
    git_dir: &[u8],
    env: Option<&[u8]>,
) -> Result<(Vec<u8>, bool), Failure> {
    posix(ctx)?;
    if let Some(env) = env.filter(|e| !e.is_empty()) {
        return Ok((
            hostpath::normpath(Flavour::Posix, &join(git_dir, env)),
            true,
        ));
    }
    match commondir(ctx, git_dir)? {
        Some(common) if registered(ctx, git_dir, &common)? => Ok((common, true)),
        _ => Ok((git_dir.to_vec(), false)),
    }
}

/// `os.path.isdir(path)`.
fn isdir<F: Fs>(ctx: &Ctx<'_, F>, path: &[u8]) -> Result<bool, Failure> {
    let at = ctx.at(path)?;
    Ok(ctx.fs.stat(&at).is_ok_and(|st| st.kind == Kind::Directory))
}

/// `_not_registered(git_dir, common)`.
pub fn not_registered(git_dir: &[u8], common: &[u8]) -> Vec<u8> {
    words(&[
        git_dir,
        b" names the common directory ",
        common,
        b" but is not one of its worktrees",
    ])
}

/// `_foreign_commondir(git_dir)`.
pub fn foreign_commondir<F: Fs>(
    ctx: &Ctx<'_, F>,
    git_dir: &[u8],
) -> Result<Option<Vec<u8>>, Failure> {
    let Some(named) = commondir(ctx, git_dir)? else {
        return Ok(None);
    };
    if registered(ctx, git_dir, &named)? {
        return Ok(None);
    }
    if isdir(ctx, &join(&named, b"objects"))? && isdir(ctx, &join(&named, b"refs"))? {
        return Ok(Some(named));
    }
    Ok(None)
}

/// `_is_git_directory(path)` with `GIT_COMMON_DIR` as `env`.
pub fn is_git_directory<F: Fs>(
    ctx: &Ctx<'_, F>,
    path: &[u8],
    env: Option<&[u8]>,
) -> Result<bool, Failure> {
    posix(ctx)?;
    let head = ctx.at(&join(path, b"HEAD"))?;
    if ctx.fs.lstat_result(&head).is_err() {
        return Ok(false);
    }
    let (common, _) = common_of(ctx, path, env)?;
    Ok(isdir(ctx, &join(&common, b"objects"))? && isdir(ctx, &join(&common, b"refs"))?)
}
