//! The config files a checkout reads for its remotes, and their reads:
//! `location_git.py`'s `_env_true` (259-267), `_system_configs`
//! (286-305), `_global_configs` (308-325), `_machine_configs` (328-337),
//! `_repository_paths` (340-360, with `gitfile_rule.reads_worktree_config`
//! 550-573), `_read_configs` (363-391), `_failed` (394-403),
//! `_head_branch` (406-444), `_asked_branch` (447-476) and the passes of
//! `_rewrites` (512-620) up to the call of `_spellings`.
//!
//! Nothing is read ambiently: the environment ([`Env`]), what the git on
//! PATH says it reads ([`Sources`], [`GitFacts`]), the home directory
//! ([`Machine`]) and the answer of `git symbolic-ref -q HEAD` where the
//! files do not settle the branch ([`Inputs::asked_branch`]) are inputs.
//! No git is run.

use super::git_bool;
use super::legacy::{file_stamp, legacy_names, Files, Halt, NotPorted, Stamp};
use super::paths::{self, Machine};
use super::tree::{config_tree, Ask, Branch, ConfigValues, Context, Values};
use crate::fsdata::{Flavour, Fs};
use crate::gitfile_rule::format::{judge_format, GitFacts};
use crate::githead::{self, GitDir, RefFs};

/// The environment variables the reads take, as the process has them
/// (None: unset). HOME is [`Machine::home`].
#[derive(Clone, Debug, Default)]
pub struct Env {
    pub git_config_nosystem: Option<Vec<u8>>,
    pub git_config_system: Option<Vec<u8>>,
    pub git_config_global: Option<Vec<u8>>,
    pub xdg_config_home: Option<Vec<u8>>,
    /// GIT_TEST_DEFAULT_INITIAL_BRANCH_NAME, the branch a `branches` file
    /// names where it names none (`gitconfig._default_branch`).
    pub git_test_default_initial_branch_name: Option<Vec<u8>>,
}

/// `gitsystem.Sources`, as far as these reads take it.
#[derive(Clone, Debug, Default)]
pub struct Sources {
    /// The files the git on PATH reads of its own build, in order.
    pub own: Vec<Vec<u8>>,
    /// The system file it was built to read.
    pub system: Option<Vec<u8>>,
    /// What it expands `%(prefix)` to (`paths::AS_WRITTEN` for none).
    pub prefix: Option<Vec<u8>>,
    /// Whether it takes a `hasconfig:` condition.
    pub hasconfig: Option<bool>,
    /// Whether it reads the legacy `remotes/` and `branches/` files.
    pub legacy: Option<bool>,
    /// Whether an empty `remote.<name>.url` empties the remote's URLs.
    pub resets: Option<bool>,
    /// The version it names (`git version`).
    pub version: Option<(u32, u32, u32)>,
}

/// What the reads are given.
pub struct Inputs<'a> {
    pub machine: &'a Machine<'a>,
    pub env: Env,
    pub sources: Sources,
    pub git: GitFacts,
    /// The working directory `realpath` reads.
    pub cwd: Vec<u8>,
    /// What `git symbolic-ref -q HEAD` answers (`_asked_branch`'s
    /// answer: a branch, [`Branch::Elsewhere`], or [`Branch::Unsettled`]
    /// where git does not answer in time); None where it is not given,
    /// and the reads decline as not ported where they would ask it.
    pub asked_branch: Option<Branch>,
}

/// Why the native reads stop where the reference goes on.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum NotPortedWhy {
    /// A stamp the file system does not give as the reference's.
    Stamp,
    /// The branch had to be asked of git and no answer was given.
    AskedBranch,
    /// A condition the tree reader declines (its subsection).
    Condition(Vec<u8>),
    /// The reference raises out of the read (a NUL `realpath` states).
    Raised,
    /// A git or common directory that is not absolute. The reference's
    /// caller passes both through `os.path.realpath` (`location.py`
    /// 745-747), so this is never reached from it; native declines
    /// rather than read either from any working directory.
    RelativeGitDir,
}

/// What `_rewrites` reaches before `_spellings`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Outcome {
    /// Why the files do not settle what git reads (`_Remotes.declined`).
    Declined(Vec<u8>),
    /// No remote: no legacy name and no remote URL the own config sets.
    NoRemotes,
    /// The reads `_spellings` is given: the machine's and the
    /// repository's own, and the legacy names.
    Reads {
        machine: Vec<ConfigValues>,
        own: Vec<ConfigValues>,
        legacy: Vec<Vec<u8>>,
    },
}

/// The files read, stamped, the machine's config files named, and the
/// outcome.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Gathered {
    pub files: Files,
    pub named: Vec<Vec<u8>>,
    pub outcome: Outcome,
}

/// `_env_true(raw)`: git's boolean, a value that is none read as true.
pub fn env_true(raw: &[u8]) -> bool {
    git_bool(Some(raw)).unwrap_or(true)
}

/// `_system_configs`.
pub fn system_configs(env: &Env, sources: &Sources) -> Vec<Vec<u8>> {
    if let Some(raw) = &env.git_config_nosystem {
        if env_true(raw) {
            return Vec::new();
        }
    }
    let system = match &env.git_config_system {
        Some(named) => Some(named.clone()),
        None => sources.system.clone(),
    };
    let mut out = sources.own.clone();
    if let Some(system) = system.filter(|s| !s.is_empty()) {
        out.push(system);
    }
    out
}

/// `_global_configs`.
pub fn global_configs(machine: &Machine, env: &Env) -> Vec<Vec<u8>> {
    if let Some(named) = &env.git_config_global {
        return if named.is_empty() {
            Vec::new()
        } else {
            vec![named.clone()]
        };
    }
    let flavour = machine.flavour;
    let home = paths::home(machine);
    let xdg = env.xdg_config_home.clone().filter(|x| !x.is_empty());
    let mut out = Vec::new();
    if let Some(xdg) = &xdg {
        out.push(paths::join(
            flavour,
            &paths::join(flavour, xdg, b"git"),
            b"config",
        ));
    }
    if let Some(home) = home {
        if xdg.is_none() {
            out.push([&home[..], b"/.config/git/config"].concat());
        }
        out.push([&home[..], b"/.gitconfig"].concat());
    }
    out
}

/// `_machine_configs`: the system's, then the global ones, each spelled
/// with the flavour's separator.
pub fn machine_configs(machine: &Machine, env: &Env, sources: &Sources) -> Vec<Vec<u8>> {
    system_configs(env, sources)
        .into_iter()
        .chain(global_configs(machine, env))
        .map(|p| paths::native(machine.flavour, &p))
        .collect()
}

/// `_FORMAT_NAMES` (`gitfile_rule.py` 247).
fn format_names() -> Vec<Vec<u8>> {
    [
        &b"core.repositoryformatversion"[..],
        b"extensions.worktreeconfig",
        b"core.bare",
        b"core.worktree",
        b"extensions.",
    ]
    .iter()
    .map(|n| n.to_vec())
    .collect()
}

/// `gitfile_rule.reads_worktree_config(common_dir)`.
pub fn reads_worktree_config<F: Fs>(
    fs: &F,
    flavour: Flavour,
    common_dir: &[u8],
    git: &GitFacts,
) -> Option<bool> {
    let path = paths::join(flavour, common_dir, b"config");
    let read = super::file::read_git_config(fs, &path, Some(&format_names()));
    if read.why.is_some() {
        return None;
    }
    let format = judge_format(&path, &read.entries, git).ok()?;
    if format.version > 1 {
        return None;
    }
    if format.worktree_config && format.version < -1 {
        return git.is_at_least((2, 42));
    }
    Some(format.worktree_config && format.version != -1)
}

/// `_repository_paths(common_dir, git_dir)`.
pub fn repository_paths<F: Fs>(
    fs: &F,
    flavour: Flavour,
    common_dir: &[u8],
    git_dir: &[u8],
    git: &GitFacts,
) -> Result<Vec<Vec<u8>>, Vec<u8>> {
    let Some(worktree_config) = reads_worktree_config(fs, flavour, common_dir, git) else {
        return Err([
            &b"git dies reading the format of "[..],
            &paths::join(flavour, common_dir, b"config"),
            b", or this git has not said how it reads it",
        ]
        .concat());
    };
    let mut out = vec![paths::join(flavour, common_dir, b"config")];
    if worktree_config {
        out.push(paths::join(flavour, git_dir, b"config.worktree"));
    }
    Ok(out)
}

/// `_failed(reads)`.
pub fn failed(reads: &[ConfigValues]) -> Option<Vec<u8>> {
    for read in reads {
        if let Some(dies) = &read.dies {
            return Some([&b"git dies reading its config: "[..], dies].concat());
        }
        if !read.complete {
            return Some(b"a config file is not read whole here".to_vec());
        }
    }
    None
}

fn sets_remote_url(names: &Values) -> bool {
    names
        .iter()
        .any(|(n, _)| n.section == "remote" && n.subsection.is_some() && n.key == "url")
}

/// Whether a git or common directory is absolute in the flavour.
fn absolute(flavour: Flavour, dir: &[u8]) -> bool {
    !dir.is_empty() && paths::opened(flavour, dir, b"\0") == dir
}

/// The one place this unit stamps a path itself; every config read goes
/// through `config_tree`, which opens a relative name from `Ask::cwd`.
fn stamp<F: Fs>(fs: &F, path: &[u8]) -> Result<Option<Stamp>, NotPortedWhy> {
    file_stamp(fs, path).map_err(|NotPorted| NotPortedWhy::Stamp)
}

/// `githead._PER_WORKTREE`: refs under the git directory.
const PER_WORKTREE: [&[u8]; 3] = [b"refs/bisect/", b"refs/rewritten/", b"refs/worktree/"];

/// The reads of one capture: its files, and the branch, asked once.
struct Reads<'a, F: Fs> {
    fs: &'a F,
    refs: &'a dyn RefFs,
    inputs: &'a Inputs<'a>,
    common_dir: &'a [u8],
    git_dir: &'a [u8],
    files: Files,
    branch: Branch,
}

impl<F: Fs> Reads<'_, F> {
    /// `_read_configs(paths, git_dir, context, files, stamp_first,
    /// gentle)`: the branch asked where a read first needs it, its stamps
    /// before that read's files, as the reference's `_Once` takes them.
    fn read(
        &mut self,
        list: &[Vec<u8>],
        base: &Context,
        stamp_first: bool,
        gentle: &[Vec<u8>],
    ) -> Result<Vec<ConfigValues>, NotPortedWhy> {
        let mut out = Vec::new();
        for (i, path) in list.iter().enumerate() {
            let read = loop {
                let ask = Ask {
                    stamp_self: stamp_first || i > 0,
                    gentle: gentle.contains(path),
                    git_dir: Some(self.git_dir.to_vec()),
                    cwd: self.inputs.cwd.clone(),
                    context: Some(Context {
                        branch: self.branch.clone(),
                        ..base.clone()
                    }),
                };
                let read = config_tree(self.fs, self.inputs.machine, path, &ask);
                if read.asks_branch {
                    self.branch = self.head_branch()?;
                    continue;
                }
                break read;
            };
            if read.raised.is_some() {
                return Err(NotPortedWhy::Raised);
            }
            if let Some(sub) = &read.not_ported {
                return Err(NotPortedWhy::Condition(sub.clone()));
            }
            if !read.stamps_held {
                return Err(NotPortedWhy::Stamp);
            }
            self.files
                .extend(read.files.iter().cloned().zip(read.stamps.iter().copied()));
            out.push(read);
        }
        Ok(out)
    }

    fn push_stamp(&mut self, path: Vec<u8>) -> Result<(), NotPortedWhy> {
        let s = stamp(self.fs, &path)?;
        self.files.push((path, s));
        Ok(())
    }

    /// `_head_branch`.
    fn head_branch(&mut self) -> Result<Branch, NotPortedWhy> {
        let flavour = self.inputs.machine.flavour;
        let gd = GitDir {
            gitdir: self.git_dir.to_vec(),
            commondir: self.common_dir.to_vec(),
        };
        self.push_stamp(paths::join(flavour, self.git_dir, b"HEAD"))?;
        let Some(content) = githead::head_bytes(self.refs, &gd) else {
            return self.asked_branch();
        };
        let Ok((mut name, _)) = githead::parse(&content) else {
            return self.asked_branch();
        };
        let mut reads = 1;
        while let Some(current) = name.clone() {
            if reads == githead::MAX_READS {
                return self.asked_branch();
            }
            reads += 1;
            let base = if PER_WORKTREE.iter().any(|p| current.starts_with(p)) {
                self.git_dir
            } else {
                self.common_dir
            };
            self.push_stamp(paths::join(flavour, base, &current))?;
            match githead::lookup(self.refs, &gd, &current) {
                Err(_) => return self.asked_branch(),
                Ok((None, _)) => break,
                Ok((target, _)) => name = target,
            }
        }
        Ok(
            match name.as_deref().and_then(|n| n.strip_prefix(b"refs/heads/")) {
                Some(branch) => Branch::Name(branch.to_vec()),
                None => Branch::Elsewhere,
            },
        )
    }

    /// `_asked_branch`: the reftable lists and packed-refs stamped, then
    /// git's answer, which is an input.
    fn asked_branch(&mut self) -> Result<Branch, NotPortedWhy> {
        let flavour = self.inputs.machine.flavour;
        let mut lists: Vec<Vec<u8>> = Vec::new();
        for folder in [self.git_dir, self.common_dir] {
            let path = paths::join(
                flavour,
                &paths::join(flavour, folder, b"reftable"),
                b"tables.list",
            );
            if !lists.contains(&path) {
                lists.push(path);
            }
        }
        lists.push(paths::join(flavour, self.common_dir, b"packed-refs"));
        for path in lists {
            self.push_stamp(path)?;
        }
        self.inputs
            .asked_branch
            .clone()
            .ok_or(NotPortedWhy::AskedBranch)
    }
}

fn collected_urls(reads: &[ConfigValues]) -> Vec<Vec<u8>> {
    super::tree::collected_urls(reads)
}

/// `_rewrites(common_dir, git_dir)` up to its call of `_spellings`.
pub fn gather<F: Fs>(
    fs: &F,
    refs: &dyn RefFs,
    common_dir: &[u8],
    git_dir: &[u8],
    inputs: &Inputs,
) -> Result<Gathered, NotPortedWhy> {
    let flavour = inputs.machine.flavour;
    // The repository's own paths (its config, config.worktree, HEAD, the
    // refs, the legacy folders) all hang off these two, so with both
    // absolute nothing here is read from a working directory.
    if !absolute(flavour, common_dir) || !absolute(flavour, git_dir) {
        return Err(NotPortedWhy::RelativeGitDir);
    }
    let mut reads = Reads {
        fs,
        refs,
        inputs,
        common_dir,
        git_dir,
        files: Vec::new(),
        branch: Branch::Unasked,
    };
    let done = |reads: Reads<'_, F>, named: Vec<Vec<u8>>, outcome: Outcome| Gathered {
        files: reads.files,
        named,
        outcome,
    };
    let list = match repository_paths(fs, flavour, common_dir, git_dir, &inputs.git) {
        Ok(list) => list,
        Err(why) => return Ok(done(reads, Vec::new(), Outcome::Declined(why))),
    };
    let context = Context {
        prefix: inputs.sources.prefix.clone(),
        hasconfig: inputs.sources.hasconfig,
        ..Context::default()
    };
    let mut own = reads.read(&list, &context, false, &[])?;
    if let Some(why) = failed(&own) {
        return Ok(done(reads, Vec::new(), Outcome::Declined(why)));
    }
    let legacy = match legacy_names(fs, flavour, common_dir, &mut reads.files) {
        Ok(names) => names,
        Err(Halt::Declines(why)) => return Ok(done(reads, Vec::new(), Outcome::Declined(why))),
        Err(Halt::NotPorted) => return Err(NotPortedWhy::Stamp),
    };
    if legacy.is_empty()
        && !own
            .iter()
            .any(|r| sets_remote_url(&r.values) || sets_remote_url(&r.undecided))
    {
        return Ok(done(reads, Vec::new(), Outcome::NoRemotes));
    }
    let named = machine_configs(inputs.machine, &inputs.env, &inputs.sources);
    let gentle: Vec<Vec<u8>> = global_configs(inputs.machine, &inputs.env)
        .iter()
        .map(|p| paths::native(flavour, p))
        .collect();
    let mut machine = reads.read(&named, &context, true, &gentle)?;
    let mut why = failed(&machine);
    if why.is_none() && machine.iter().chain(&own).any(|r| r.asks_remote_urls) {
        let collect = context.collect();
        let mut collected = reads.read(&named, &collect, true, &gentle)?;
        collected.extend(reads.read(&list, &collect, false, &[])?);
        why = failed(&collected);
        if why.is_none() && collected.iter().any(|r| r.forbidden) {
            why = Some(b"git refuses a remote URL a file an includeIf reads sets".to_vec());
        }
        if why.is_none() && collected.iter().any(|r| sets_remote_url(&r.undecided)) {
            why = Some(
                b"the remote URLs a hasconfig include is matched against are unsettled".to_vec(),
            );
        }
        if why.is_none() {
            let settled = context.with_urls(collected_urls(&collected));
            machine = reads.read(&named, &settled, true, &gentle)?;
            own = reads.read(&list, &settled, false, &[])?;
            why = failed(&[machine.clone(), own.clone()].concat());
        }
    }
    if let Some(why) = why {
        return Ok(done(reads, named, Outcome::Declined(why)));
    }
    Ok(done(
        reads,
        named,
        Outcome::Reads {
            machine,
            own,
            legacy,
        },
    ))
}
