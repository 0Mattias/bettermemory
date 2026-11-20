//! Unit 5: one `.git` entry, one folder, and the walk (`gitfile_rule.py`
//! 1295-1537): what a `.git` entry names and whether it is followed
//! ([`Entry`], [`judge`]), what one folder gives git's discovery
//! ([`Reading`], [`reading_at`], `_entry_reading`), and the walk from a
//! folder up to the file system's root ([`discovery_reading`],
//! [`discovery_refusal`], [`toplevel_agrees`]).
//!
//! Nothing ambient ([`Rule`]): the file system, its flavour and the
//! working directory ([`Ctx`]), whether a recorded path answers within the
//! budget ([`Answers`], unit 1), the pin records ([`Records`], unit 4),
//! `GIT_COMMON_DIR` and the git on PATH ([`GitFacts`]) are all data. The
//! `lstat` of the entry [`judge`] takes is an argument, as the reference's
//! is. Every path goes through [`Ctx::at`]; the working directory is the
//! caller's contract (unit 2b).
//!
//! What the reference lets rise, rises: a ValueError ([`Failure::Value`])
//! from `realpath` of an entry whose path holds a NUL or from the open of
//! such a `.git` file, and the RecursionError of a pin record nested too
//! deep ([`Failure::Raised`], unit 4).
//!
//! Two answers differ from the reference's on purpose (the README's unit 5
//! section says why each is kept). A FIFO where the reference's blocking
//! open would wait (a linked worktree's `gitdir`) is read as no text, so
//! the follow is refused where the reference's walk is held until a
//! writer comes. A relative path is joined to `cwd` before the OS is asked
//! ([`Ctx::at`]), so a relative path under the OS's path limit whose join
//! is over it is ENAMETOOLONG here where the reference reads it; no caller
//! of the reference passes a relative path, and the walk resolves its
//! start first.
//!
//! The Windows flavour is declined whole ([`Failure::NotPorted`]), as
//! units 2 to 4 are: the walk stands on `realpath`, `samestat` and the
//! reparse tag, none ported there.

use crate::fsdata::realpath::{realpath, Unresolved};
use crate::fsdata::{Flavour, Fs, Kind, OsError, StatResult};

use super::bounded::Answers;
use super::follow::{refusal, Records};
use super::format::GitFacts;
use super::gitdir::{git_read, git_reads, Ctx, Failure, GitRead};
use super::gitfile_target;
use super::hostpath;
use super::identity::{is_link, same_folder, working_tree_of};
use super::trust::{foreign_commondir, inside_reading, not_registered, own_refusal, Refusal};

/// What the rule reads besides the paths it is asked about, each as data.
pub struct Rule<'a, F: Fs, A: Answers + ?Sized, R: Records + ?Sized> {
    pub ctx: &'a Ctx<'a, F>,
    /// Whether a recorded tree answers within `_RECORDED_BUDGET`.
    pub answers: &'a A,
    /// The pin records (`gitdir_pin`).
    pub records: &'a R,
    /// `GIT_COMMON_DIR` as the process holds it (an empty one is unset).
    pub env: Option<&'a [u8]>,
    /// The git on PATH (`_git_is_at_least`, `_reads_compat`).
    pub git: &'a GitFacts,
}

/// `Entry`: what a `.git` entry names, as membership reads it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    /// The git directory it reaches: the entry itself where `own`.
    pub git_dir: Vec<u8>,
    /// Why it is not followed; None where it is.
    pub why: Option<Refusal>,
    /// A folder's own `.git` directory.
    pub own: bool,
    /// A follow of a git directory kept apart that no record names this
    /// folder for.
    pub provisional: bool,
}

/// `Reading`: what one folder gives git's discovery, by membership's rule.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Reading {
    pub why: Option<Refusal>,
    pub provisional: bool,
    /// The folder membership roots the repository at; None where it is
    /// refused or bare.
    pub root: Option<Vec<u8>>,
}

impl Reading {
    fn refused(parts: &[&[u8]]) -> Reading {
        Reading {
            why: plain(parts),
            ..Reading::default()
        }
    }
}

fn plain(parts: &[&[u8]]) -> Option<Refusal> {
    Some(Refusal {
        why: parts.concat(),
        format_unknown: false,
    })
}

fn posix<F: Fs>(ctx: &Ctx<'_, F>) -> Result<(), Failure> {
    match ctx.flavour {
        Flavour::Posix => Ok(()),
        Flavour::Windows => Err(Failure::NotPorted),
    }
}

fn join(a: &[u8], b: &[u8]) -> Vec<u8> {
    hostpath::join(Flavour::Posix, a, b)
}

/// `os.path.realpath(path)` where the reference does not catch what it
/// raises: a ValueError (a NUL) rises.
fn real<F: Fs>(ctx: &Ctx<'_, F>, path: &[u8]) -> Result<Vec<u8>, Failure> {
    match realpath(ctx.fs, ctx.flavour, path, ctx.cwd) {
        Ok(real) => Ok(real),
        Err(Unresolved::NotPorted) => Err(Failure::NotPorted),
        Err(Unresolved::Raised(class)) => Err(Failure::from(OsError {
            class,
            errno: None,
            winerror: None,
        })),
    }
}

/// `os.stat(path)`, None where it raises OSError or ValueError.
fn stat<F: Fs>(ctx: &Ctx<'_, F>, path: &[u8]) -> Result<Option<StatResult>, Failure> {
    Ok(ctx.fs.stat(&ctx.at(path)?).ok())
}

/// `os.lstat(path)`, None where it raises OSError or ValueError
/// (`os.path.lexists` is whether it answers).
fn lstat<F: Fs>(ctx: &Ctx<'_, F>, path: &[u8]) -> Result<Option<StatResult>, Failure> {
    Ok(ctx.fs.lstat_result(&ctx.at(path)?).ok())
}

/// A refused entry that reaches `realpath(dotgit)`.
fn unfollowed<F: Fs>(
    ctx: &Ctx<'_, F>,
    dotgit: &[u8],
    why: &[&[u8]],
) -> Result<Option<Entry>, Failure> {
    Ok(Some(Entry {
        git_dir: real(ctx, dotgit)?,
        why: plain(why),
        own: false,
        provisional: false,
    }))
}

/// `judge(directory, dotgit, st, fetch=..., read=...)`: the `.git` entry
/// `dotgit` in `directory`, `st` its `lstat`: None where it names no git
/// directory, else the git directory it names and whether it is followed.
pub fn judge<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    directory: &[u8],
    dotgit: &[u8],
    st: &StatResult,
    fetch: bool,
    read: bool,
) -> Result<Option<Entry>, Failure> {
    let ctx = rule.ctx;
    posix(ctx)?;
    let linked = is_link(st)?;
    let through;
    let st = if linked {
        match stat(ctx, dotgit)? {
            Some(reached) => {
                through = reached;
                &through
            }
            None => return Ok(None),
        }
    } else {
        st
    };
    let mut common = None;
    if st.kind == Kind::Directory {
        match git_read(ctx, dotgit, fetch, read)? {
            GitRead::Dies => {
                let why: [&[u8]; 3] = [b"git cannot read ", dotgit, b" as a git directory"];
                return unfollowed(ctx, dotgit, &why);
            }
            GitRead::NotGitDir => return Ok(None),
            GitRead::Found(dir, named) => common = Some((dir, named)),
        }
    }
    if linked {
        // A link reaching another folder's own `.git` is that folder's,
        // whatever it holds.
        if let Some(owner) = working_tree_of(ctx, dotgit)? {
            if same_folder(ctx, &owner, directory)? != Some(true) {
                let why: [&[u8]; 3] = [dotgit, b" links to the .git of ", &owner];
                return unfollowed(ctx, dotgit, &why);
            }
        }
    }
    let target = match st.kind {
        Kind::Directory if !linked => {
            return Ok(Some(Entry {
                git_dir: dotgit.to_vec(),
                why: own_refusal(ctx, dotgit, directory, common, rule.git)?,
                own: true,
                provisional: false,
            }));
        }
        // A link to a git directory is a pointer to it.
        Kind::Directory => real(ctx, dotgit)?,
        Kind::Regular => {
            let Some(named) = gitfile_target(ctx, dotgit)? else {
                return Ok(None);
            };
            // `not common`: no git directory, and one git dies on, alike.
            match git_read(ctx, &named, fetch, read)? {
                GitRead::Found(dir, was_named) => common = Some((dir, was_named)),
                GitRead::NotGitDir | GitRead::Dies => return Ok(None),
            }
            named
        }
        _ => return Ok(None),
    };
    let (why, provisional) = refusal(
        ctx,
        rule.answers,
        rule.records,
        directory,
        &target,
        common,
        rule.env,
        rule.git,
    )?;
    Ok(Some(Entry {
        git_dir: target,
        why,
        own: false,
        provisional,
    }))
}

/// `_entry_reading(directory, dotgit, st)`: [`reading_at`] of the `.git`
/// entry; None where git goes on to the folder itself.
pub fn entry_reading<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    directory: &[u8],
    dotgit: &[u8],
    st: &StatResult,
) -> Result<Option<Reading>, Failure> {
    let ctx = rule.ctx;
    let Some(seen) = judge(rule, directory, dotgit, st, true, true)? else {
        // Git follows a `.git` file or dies on it, and passes over
        // anything else `judge` reads as no git directory.
        return Ok(match stat(ctx, dotgit)? {
            Some(through) if through.kind == Kind::Regular => Some(Reading::refused(&[
                dotgit,
                b" names no git directory membership follows",
            ])),
            _ => None,
        });
    };
    if seen.why.is_none() && seen.own {
        if let Some(named) = foreign_commondir(ctx, dotgit)? {
            return Ok(Some(Reading::refused(&[&not_registered(dotgit, &named)])));
        }
    }
    // `None if seen.why else directory`: a reason is never the empty
    // text, so its truth is whether there is one.
    let root = seen.why.is_none().then(|| directory.to_vec());
    Ok(Some(Reading {
        why: seen.why,
        provisional: seen.provisional,
        root,
    }))
}

/// `reading_at(directory)`: what git's discovery meets in the folder
/// `directory`, by membership's rule; None where git goes on to the
/// folder above.
pub fn reading_at<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    directory: &[u8],
) -> Result<Option<Reading>, Failure> {
    let ctx = rule.ctx;
    posix(ctx)?;
    let dotgit = join(directory, b".git");
    if let Some(st) = lstat(ctx, &dotgit)? {
        if let Some(found) = entry_reading(rule, directory, &dotgit, &st)? {
            return Ok(Some(found));
        }
    }
    if lstat(ctx, &join(directory, b"HEAD"))?.is_none() {
        return Ok(None);
    }
    match git_reads(ctx, directory, true, true)? {
        None => {
            return Ok(Some(Reading::refused(&[
                b"git cannot read ",
                directory,
                b" as a git directory",
            ])))
        }
        Some(false) => return Ok(None),
        Some(true) => {}
    }
    if let Some(named) = foreign_commondir(ctx, directory)? {
        return Ok(Some(Reading::refused(&[&not_registered(
            directory, &named,
        )])));
    }
    let (why, root) = inside_reading(ctx, directory, rule.git)?;
    Ok(Some(Reading {
        why,
        provisional: false,
        root,
    }))
}

/// `discovery_reading(start)`: the reading of the first folder from
/// `realpath(start)` up to the file system's root where git's walk stops,
/// or None where it stops at none (or `realpath` raises).
pub fn discovery_reading<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    start: &[u8],
) -> Result<Option<Reading>, Failure> {
    let ctx = rule.ctx;
    // `realpath` declines the Windows flavour too; it is said here first.
    posix(ctx)?;
    let mut here = match realpath(ctx.fs, ctx.flavour, start, ctx.cwd) {
        Ok(real) => real,
        Err(Unresolved::NotPorted) => return Err(Failure::NotPorted),
        Err(Unresolved::Raised(_)) => return Ok(None),
    };
    loop {
        if let Some(found) = reading_at(rule, &here)? {
            return Ok(Some(found));
        }
        // `dirname` shortens a path or leaves it as it is, so this ends.
        let parent = hostpath::dirname(Flavour::Posix, &here);
        if parent == here {
            return Ok(None);
        }
        here = parent;
    }
}

/// `discovery_refusal(start)`: why git, run in `start`, would read a
/// repository membership does not follow, or die or block in its walk.
pub fn discovery_refusal<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    start: &[u8],
) -> Result<Option<Refusal>, Failure> {
    Ok(discovery_reading(rule, start)?.and_then(|found| found.why))
}

/// `toplevel_agrees(start, toplevel)`: whether `toplevel` is the folder
/// membership's rule roots the repository git's walk reaches at, compared
/// as files. False where the walk reaches none it follows, or a bare one,
/// or either path cannot be read.
pub fn toplevel_agrees<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    start: &[u8],
    toplevel: &[u8],
) -> Result<bool, Failure> {
    // The test of `why` changes no answer (a reading with a reason has no
    // root), nor does the order of `_same_folder`'s two paths (the root was
    // read a moment ago, so only the toplevel can reach nothing).
    let Some(Reading {
        why: None,
        root: Some(root),
        ..
    }) = discovery_reading(rule, start)?
    else {
        return Ok(false);
    };
    Ok(same_folder(rule.ctx, toplevel, &root)? == Some(true))
}
