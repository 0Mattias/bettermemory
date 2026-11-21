//! The repository git finds from a directory, as `location_git.py`
//! (1015-1180) reads it: `GitDies` and `DiscoveryDies` ([`Dies`]),
//! `_from_env` ([`from_env`]), `_discover` ([`discover`]) and `_at_dotgit`
//! ([`at_dotgit`]).
//!
//! Nothing ambient: the file system, its flavour, the working directory,
//! `GIT_COMMON_DIR`, the git on PATH and the pin records come in the
//! gitfile rule's [`Rule`]; `GIT_DIR` and `GIT_WORK_TREE` in [`Env`];
//! `githead.across_filesystems()` and `githead.probed` as arguments
//! (`across`, `probed`), the walk taken lazily as the reference's
//! generator is.
//!
//! The Windows flavour declines ([`Failure::NotPorted`]): the reference's
//! readers stand on `realpath`, which fsdata does not port there. The
//! decline comes after the `GIT_DIR` test and, in [`discover`], after
//! `DiscoveryDies`, where the reference answers without the file system.

use crate::capture::Kind;
use crate::fsdata::{Flavour, Fs, Kind as FileKind, StatResult};
use crate::gitfile_rule::bounded::Answers;
use crate::gitfile_rule::discovery::{judge, Rule};
use crate::gitfile_rule::follow::Records;
use crate::gitfile_rule::gitdir::{git_reads, Failure};
use crate::gitfile_rule::hostpath::{join, normpath};
use crate::gitfile_rule::trust::{
    common_of, foreign_commondir, inside_reading, is_git_directory, not_registered, tree_format,
    Refusal,
};
use crate::location_git::Git;

/// `_DISCOVERY_ENV`: the variables that move git's discovery.
pub const DISCOVERY_ENV: [&str; 3] = ["GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"];

/// `DiscoveryDies`'s text.
pub const DISCOVERY_DIES: &[u8] =
    b"GIT_DISCOVERY_ACROSS_FILESYSTEM holds no boolean git reads: git dies on it";

/// What git dies on: `GitDies` with its text, or `DiscoveryDies` (a
/// `GitDies` too in the reference) with [`DISCOVERY_DIES`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Dies {
    Git(Vec<u8>),
    Discovery,
}

impl Dies {
    /// `str(exception)`.
    pub fn text(&self) -> &[u8] {
        match self {
            Dies::Git(why) => why,
            Dies::Discovery => DISCOVERY_DIES,
        }
    }
}

/// What these functions raise: git dies, or a failure of the readers
/// passed through as the reference lets it rise.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Raised {
    Dies(Dies),
    Failure(Failure),
}

impl From<Failure> for Raised {
    fn from(failure: Failure) -> Raised {
        Raised::Failure(failure)
    }
}

/// One entry of `refused`: (the path, the git directory it names, why).
/// `why.format_unknown` is whether the reference's text is a
/// `FormatUnknown`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Refused {
    pub path: Vec<u8>,
    pub git_dir: Vec<u8>,
    pub why: Refusal,
}

/// `GIT_DIR` and `GIT_WORK_TREE` as the process holds them (None where
/// unset). `GIT_COMMON_DIR` is the rule's `env`.
#[derive(Clone, Copy, Debug, Default)]
pub struct Env<'a> {
    pub git_dir: Option<&'a [u8]>,
    pub work_tree: Option<&'a [u8]>,
}

fn truthy(value: Option<&[u8]>) -> Option<&[u8]> {
    value.filter(|v| !v.is_empty())
}

fn posix(flavour: Flavour) -> Result<(), Failure> {
    match flavour {
        Flavour::Posix => Ok(()),
        Flavour::Windows => Err(Failure::NotPorted),
    }
}

fn pjoin(a: &[u8], b: &[u8]) -> Vec<u8> {
    join(Flavour::Posix, a, b)
}

fn pnorm(path: &[u8]) -> Vec<u8> {
    normpath(Flavour::Posix, path)
}

fn plain(why: Vec<u8>) -> Refusal {
    Refusal {
        why,
        format_unknown: false,
    }
}

fn push(refused: &mut Option<&mut Vec<Refused>>, path: &[u8], git_dir: &[u8], why: &Refusal) {
    if let Some(list) = refused.as_deref_mut() {
        list.push(Refused {
            path: path.to_vec(),
            git_dir: git_dir.to_vec(),
            why: why.clone(),
        });
    }
}

fn kind_of(linked: bool) -> Kind {
    if linked {
        Kind::Linked
    } else {
        Kind::Primary
    }
}

/// `_from_env(start, refused)`: the repository `GIT_DIR` names, None
/// where it is unset or empty.
pub fn from_env<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    env: &Env<'_>,
    start: &[u8],
    mut refused: Option<&mut Vec<Refused>>,
) -> Result<Option<Git>, Raised> {
    let Some(raw) = truthy(env.git_dir) else {
        return Ok(None);
    };
    posix(rule.ctx.flavour)?;
    let git_dir = pnorm(&pjoin(start, raw));
    let (common, linked) = common_of(rule.ctx, &git_dir, rule.env)?;
    let found = tree_format(rule.ctx, &git_dir, None, rule.git)?;
    let work = truthy(env.work_tree);
    let why = if let Some(text) = &found.why {
        Some(
            [
                &git_dir[..],
                b"'s config cannot be read as git reads it: ",
                text,
            ]
            .concat(),
        )
    } else if work.is_none() && !found.bare && found.worktree.as_deref() == Some(b"") {
        Some(
            [
                &git_dir[..],
                b"'s config names no working tree (an empty core.worktree)",
            ]
            .concat(),
        )
    } else {
        None
    };
    if let Some(why) = why {
        push(&mut refused, &git_dir, &git_dir, &plain(why.clone()));
        return Err(Raised::Dies(Dies::Git(why)));
    }
    let root = if let Some(work) = work {
        Some(pnorm(&pjoin(start, work)))
    } else if found.bare {
        None
    } else if let Some(worktree) = &found.worktree {
        Some(pnorm(&pjoin(&git_dir, worktree)))
    } else {
        Some(start.to_vec())
    };
    let kind = if root.is_none() {
        Kind::Bare
    } else {
        kind_of(linked)
    };
    Ok(Some(Git {
        dotgit: None,
        git_dir,
        common_dir: common,
        root,
        kind,
    }))
}

/// `_discover(start, refused, read_head=...)`: the repository git would
/// find from `start`. `across` is `githead.across_filesystems()`, read
/// only where `GIT_DIR` is unset or empty; `probed` is `githead.probed`,
/// given the normalized start and its device (None where `across`).
#[allow(clippy::too_many_arguments)]
pub fn discover<F, A, R, P, I>(
    rule: &Rule<'_, F, A, R>,
    env: &Env<'_>,
    start: &[u8],
    across: Option<bool>,
    probed: P,
    mut refused: Option<&mut Vec<Refused>>,
    read_head: bool,
) -> Result<Option<Git>, Raised>
where
    F: Fs,
    A: Answers + ?Sized,
    R: Records + ?Sized,
    P: FnOnce(&[u8], Option<u64>) -> Result<I, Failure>,
    I: Iterator<Item = Result<Vec<u8>, Failure>>,
{
    if truthy(env.git_dir).is_some() {
        return from_env(rule, env, start, refused);
    }
    let Some(across) = across else {
        return Err(Raised::Dies(Dies::Discovery));
    };
    posix(rule.ctx.flavour)?;
    let ctx = rule.ctx;
    let here = pnorm(start);
    let device = if across {
        None
    } else {
        let st = ctx.fs.stat(&ctx.at(&here)?).map_err(Failure::from)?;
        Some(st.dev.ok_or(Failure::NotPorted)?)
    };
    for d in probed(&here, device)? {
        let d = d?;
        let dotgit = pjoin(&d, b".git");
        let st = ctx.fs.lstat_result(&ctx.at(&dotgit)?).ok();
        if let Some(st) = st {
            if let Some(found) =
                at_dotgit(rule, &d, &dotgit, &st, refused.as_deref_mut(), read_head)?
            {
                return Ok(Some(found));
            }
        }
        if git_reads(ctx, &d, false, read_head)? == Some(true)
            && is_git_directory(ctx, &d, rule.env)?
        {
            let (why, root) = inside_reading(ctx, &d, rule.git)?;
            if let Some(why) = why {
                push(&mut refused, &d, &d, &why);
                if why.format_unknown {
                    return Err(Raised::Dies(Dies::Git(why.why)));
                }
                return Ok(None);
            }
            let (common, linked) = common_of(ctx, &d, rule.env)?;
            return Ok(Some(match root {
                Some(root) => Git {
                    dotgit: Some(d.clone()),
                    git_dir: d,
                    common_dir: common,
                    root: Some(root),
                    kind: kind_of(linked),
                },
                None => Git {
                    dotgit: None,
                    git_dir: d,
                    common_dir: common,
                    root: None,
                    kind: Kind::Bare,
                },
            }));
        }
        if refused.is_some() && isfile(rule, &pjoin(&d, b"HEAD"))? {
            if let Some(named) = foreign_commondir(ctx, &d)? {
                let why = plain(not_registered(&d, &named));
                push(&mut refused, &d, &named, &why);
            }
        }
    }
    Ok(None)
}

/// `os.path.isfile(path)`.
fn isfile<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    path: &[u8],
) -> Result<bool, Failure> {
    let at = rule.ctx.at(path)?;
    Ok(rule
        .ctx
        .fs
        .stat(&at)
        .is_ok_and(|st| st.kind == FileKind::Regular))
}

/// `_at_dotgit(directory, dotgit, st, refused, read_head=...)`: the
/// repository the `.git` entry `dotgit` in `directory` gives, `st` its
/// `lstat`.
pub fn at_dotgit<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    directory: &[u8],
    dotgit: &[u8],
    st: &StatResult,
    mut refused: Option<&mut Vec<Refused>>,
    read_head: bool,
) -> Result<Option<Git>, Raised> {
    let Some(seen) = judge(rule, directory, dotgit, st, false, read_head)? else {
        return Ok(None);
    };
    if let Some(why) = seen.why {
        push(&mut refused, dotgit, &seen.git_dir, &why);
        if why.format_unknown {
            return Err(Raised::Dies(Dies::Git(why.why)));
        }
        return Ok(None);
    }
    if seen.own {
        return Ok(Some(Git {
            dotgit: Some(dotgit.to_vec()),
            git_dir: dotgit.to_vec(),
            common_dir: dotgit.to_vec(),
            root: Some(directory.to_vec()),
            kind: Kind::Primary,
        }));
    }
    let (common, linked) = common_of(rule.ctx, &seen.git_dir, rule.env)?;
    Ok(Some(Git {
        dotgit: Some(dotgit.to_vec()),
        git_dir: seen.git_dir,
        common_dir: common,
        root: Some(directory.to_vec()),
        kind: kind_of(linked),
    }))
}
