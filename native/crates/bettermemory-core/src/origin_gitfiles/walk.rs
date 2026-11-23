//! Unit 7 of `origin_gitfiles`, with unit 4's remainder: the origin
//! cache's signature and the files beside the history.
//!
//! - [`capture_files`] (reference lines 434-460): `githead.signature` and
//!   the repository discovery settles, or the owner-free signature and
//!   `locate`'s repository where discovery declines;
//! - [`walk_files_signature`] (1430-1518): the common `config`,
//!   `config.worktree`, the working tree's `.gitmodules`, the index's
//!   `.gitmodules` entry and the `shallow` file;
//! - [`located_head`] (1520-1526): the head `locate` reads at a root;
//! - [`files_held`] (1528-1553): whether every part a search keyed on
//!   still reads as it did.
//!
//! The environment is arguments (GIT_INDEX_FILE's presence, XDG_CONFIG_HOME
//! and HOME, the discovery [`Env`]), the file system the caller's
//! ([`Fs`]), and the memos values the caller holds.

use super::attrs::{
    attribute_files_signature, gitattributes_signature, gitdir_at_root, index_gitmodules,
    AttributeFiles, IndexFailure, IndexGitmodules, IndexMemo,
};
use super::facts::{directories_held, hold_directory, Held, HoldFault, NotPorted};
use super::root::{config_facts, worktree_config_facts, ConfigMemo};
use crate::fsdata::{Flavour, Fs};
use crate::gitconfig::legacy::Stamp;
use crate::gitfile_rule::bounded::Answers;
use crate::gitfile_rule::discovery::Rule;
use crate::gitfile_rule::follow::Records;
use crate::gitfile_rule::gitdir::{Ctx, Failure};
use crate::gitfile_rule::hostpath::join;
use crate::githead::discovery::{
    discovery_moved, find_gitdir, locate, pure, stamp, Env, Found, Host, Owners,
};
use crate::githead::head_sha;
use crate::githead::signature::{signature, undetermined, CtxRefs, First, Signature};

/// `capture_files(start)`: the signature and the repository the origin
/// cache keys a capture on. `Err` carries the class of an exception
/// `githead.signature` lets out.
pub fn capture_files<F: Fs, A: Answers + ?Sized, R: Records + ?Sized, O: Owners + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    host: &Host<'_, O>,
    env: &Env,
    start: &[u8],
) -> Result<(Signature, Option<Found>), &'static str> {
    let base = signature(rule, host, env, start, true)?;
    if !matches!(base.first, First::Undetermined(_)) {
        return Ok((base, find_gitdir(rule, host, env, start)));
    }
    let located = signature(rule, host, env, start, false)?;
    let First::GitDir(dir) = &located.first else {
        return Ok((located, None));
    };
    match locate(rule, env, start, true, false) {
        Some(found) if found.gd.gitdir == *dir => Ok((located, Some(found))),
        _ => Ok((
            Signature {
                first: undetermined(),
                ..located
            },
            None,
        )),
    }
}

/// What [`walk_files_signature`] keys on: the stamps of the common
/// `config`, of `config.worktree` where git reads it, of the working
/// tree's `.gitmodules`, the index's slot and the stamp of `shallow`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WalkFiles {
    pub config: Option<Stamp>,
    pub worktree_config: Option<Stamp>,
    pub gitmodules: Option<Stamp>,
    pub index: Option<IndexGitmodules>,
    pub shallow: Option<Stamp>,
}

/// `Path(root) / name` on POSIX: `Path(".") / name` is the name alone.
fn under(root: &[u8], name: &[u8]) -> Vec<u8> {
    if root == b"." {
        return name.to_vec();
    }
    join(Flavour::Posix, root, name)
}

/// `_stamp(path)` inside the reference's `try`: the outer `Err` declines,
/// the inner one is the caught OSError or ValueError.
fn stamped<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    path: &[u8],
) -> Result<Result<Option<Stamp>, ()>, NotPorted> {
    match stamp(ctx, path, true) {
        Ok(found) => Ok(Ok(found)),
        Err(Failure::Os(_) | Failure::Value) => Ok(Err(())),
        Err(_) => Err(NotPorted),
    }
}

fn hold<F: Fs + ?Sized>(
    ctx: &Ctx<'_, F>,
    path: &[u8],
    held: &mut Held,
) -> Result<Result<(), ()>, NotPorted> {
    match hold_directory(ctx.fs, ctx.flavour, path, held) {
        Ok(()) => Ok(Ok(())),
        Err(HoldFault::Raised(_)) => Ok(Err(())),
        Err(HoldFault::NotPorted) => Err(NotPorted),
    }
}

/// `walk_files_signature(root, directories=...)`. `index_file_set` is
/// whether GIT_INDEX_FILE is in the environment. `Ok(None)` where the
/// reference answers None; `Err(NotPorted)` where the file system gives no
/// full stamp, and where the Windows flavour would require discovery or
/// path spelling. `directories` may hold some stamps on a None answer, as the
/// reference's dict does.
#[allow(clippy::too_many_arguments)]
pub fn walk_files_signature<F: Fs, A: Answers + ?Sized, R: Records + ?Sized, O: Owners + ?Sized>(
    config: &mut ConfigMemo,
    index: &mut IndexMemo,
    rule: &Rule<'_, F, A, R>,
    host: &Host<'_, O>,
    env: &Env,
    index_file_set: bool,
    root: &[u8],
    directories: Option<&mut Held>,
) -> Result<Option<WalkFiles>, NotPorted> {
    let ctx = rule.ctx;
    if index_file_set || discovery_moved(env) {
        return Ok(None);
    }
    if ctx.flavour == Flavour::Windows {
        return Err(NotPorted);
    }
    let Some(Found { gd, .. }) = gitdir_at_root(rule, host, env, root) else {
        return Ok(None);
    };
    let root = pure(root);
    if let Some(held) = directories {
        if hold(ctx, &root, held)?.is_err() {
            return Ok(None);
        }
        if hold(ctx, &join(ctx.flavour, &gd.gitdir, b"index"), held)?.is_err() {
            return Ok(None);
        }
    }
    let config_path = join(ctx.flavour, &gd.commondir, b"config");
    let Some((config_stamp, facts)) = config_facts(config, ctx, &config_path, rule.git)? else {
        return Ok(None);
    };
    if facts.names.is_none() {
        return Ok(None);
    }
    let Some((worktree_stamp, _)) = worktree_config_facts(config, ctx, &gd, &facts, rule.git)?
    else {
        return Ok(None);
    };
    let Ok(gitmodules) = stamped(ctx, &under(&root, b".gitmodules"))? else {
        return Ok(None);
    };
    let Ok(shallow) = stamped(ctx, &join(ctx.flavour, &gd.commondir, b"shallow"))? else {
        return Ok(None);
    };
    let entry = match index_gitmodules(index, config, ctx, &gd, rule.git) {
        Ok(entry) => entry,
        Err(IndexFailure::NotPorted) => return Err(NotPorted),
        Err(_) => return Ok(None),
    };
    let slot = if entry == Some(IndexGitmodules::Unmerged) || gitmodules.is_none() {
        entry
    } else {
        None
    };
    Ok(Some(WalkFiles {
        config: config_stamp,
        worktree_config: worktree_stamp,
        gitmodules,
        index: slot,
        shallow,
    }))
}

/// `located_head(root)`: the commit `head_sha` reads in the repository
/// `locate` finds at `root` without its walk.
pub fn located_head<F: Fs, A: Answers + ?Sized, R: Records + ?Sized>(
    rule: &Rule<'_, F, A, R>,
    env: &Env,
    root: &[u8],
) -> Option<String> {
    let found = locate(rule, env, root, false, false)?;
    head_sha(&CtxRefs(rule.ctx), &found.gd)
}

/// What a search keyed its hits on, for [`files_held`]: `repository` from
/// `attribute_files_signature`, `chains` from `gitattributes_signature`
/// per set of pathspecs in the order first keyed, `walk_files` from
/// [`walk_files_signature`], and the directories those recorded.
#[derive(Clone, Debug, Default)]
pub struct Keyed {
    pub repository: Option<AttributeFiles>,
    #[allow(clippy::type_complexity)]
    pub chains: Vec<(Vec<Vec<u8>>, Option<Vec<Option<Stamp>>>)>,
    pub walk_files: Option<WalkFiles>,
    pub directories: Held,
}

/// The environment `files_held` reads through its signatures.
pub struct Machine<'a> {
    pub env: &'a Env,
    pub index_file_set: bool,
    pub xdg_config_home: Option<&'a [u8]>,
    pub home: Option<&'a [u8]>,
}

/// `files_held(cwd, root, repository, chains, walk_files, directories)`.
/// `Err(NotPorted)` where a signature read again declines.
#[allow(clippy::too_many_arguments)]
pub fn files_held<F: Fs, A: Answers + ?Sized, R: Records + ?Sized, O: Owners + ?Sized>(
    config: &mut ConfigMemo,
    index: &mut IndexMemo,
    rule: &Rule<'_, F, A, R>,
    host: &Host<'_, O>,
    machine: &Machine<'_>,
    cwd: &[u8],
    root: &[u8],
    keyed: &Keyed,
) -> Result<bool, NotPorted> {
    let env = machine.env;
    if let Some(repository) = &keyed.repository {
        let now = attribute_files_signature(
            config,
            rule,
            host,
            env,
            machine.xdg_config_home,
            machine.home,
            cwd,
            root,
            None,
        )?;
        if now.as_ref() != Some(repository) {
            return Ok(false);
        }
    }
    if let Some(walk) = &keyed.walk_files {
        let now = walk_files_signature(
            config,
            index,
            rule,
            host,
            env,
            machine.index_file_set,
            root,
            None,
        )?;
        if now.as_ref() != Some(walk) {
            return Ok(false);
        }
    }
    if !directories_held(rule.ctx.fs, &keyed.directories) {
        return Ok(false);
    }
    for (specs, chain) in &keyed.chains {
        let Some(chain) = chain else { continue };
        let specs: Vec<&[u8]> = specs.iter().map(Vec::as_slice).collect();
        if gitattributes_signature(rule.ctx, root, &specs, None)?.as_ref() != Some(chain) {
            return Ok(false);
        }
    }
    Ok(true)
}
