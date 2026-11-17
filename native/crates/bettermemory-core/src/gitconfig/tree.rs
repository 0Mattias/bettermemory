//! A config file and its includes, as the reference reads them:
//! `config_tree` (`gitconfig.py` 255-289), `_read_config` (358-386),
//! `_parse_config` (408-444), `_include` (463-525) with
//! `_include_target` (594-609), and the `includeIf` conditions
//! `gitdir:`, `gitdir/i:`, `onbranch:` and `hasconfig:remote.*.url:`
//! (`_condition` 1081-1110, `_branch_condition` 1129-1147,
//! `_remote_url_condition` 1150-1168, `_gitdir_matches` 1177-1212), with
//! the pass that collects the remote URLs (`IncludeContext.collect`,
//! 168-172) and its forbidding reads (`_forbidding` 683-700,
//! `_remote_url_names` 703-706). A `gitdir:` condition under the Windows
//! flavour stops the read as [`ConfigValues::not_ported`]: its `realpath`
//! is not ported there. One whose condition git does not know is
//! skipped, as the reference skips it.
//!
//! Where a condition is not settled here (no git directory given, the
//! branch not known, a pattern the read's wildmatch steps do not settle,
//! a `%(prefix)/` not known), what the include sets goes to
//! [`ConfigValues::undecided`] and, where git would die reading it or
//! read more, [`ConfigValues::maybe_dies`] says why (lines 481-525).
//!
//! Bounds: 64 files in all ([`INCLUDE_FILES`], each file counted before
//! it is opened, a missing one too) and git's depth of 10
//! ([`INCLUDE_DEPTH`], checked only once the file is found), the 2 MiB
//! of each file ([`super::file::config_bytes`]), and the wildmatch steps
//! of one read ([`Steps`]). A file included in itself is found by its
//! identity ([`crate::fsdata::FileId`]).

use std::collections::BTreeMap;

use super::file::{config_bytes, ConfigBytes, Dies};
use super::legacy::{file_stamp, NotPorted, Stamp};
use super::paths::{self, Machine};
use super::wildmatch::{any_match, literal_head, Steps};
use super::{git_entries, remote_rules, spaced, Name};
use crate::fsdata::realpath::{abspath, realpath, Unresolved};
use crate::fsdata::{FileId, Flavour, Fs, PyErr};

/// git's bound on nested includes (`_INCLUDE_DEPTH`, line 66).
pub const INCLUDE_DEPTH: usize = 10;
/// Files one read takes in all (`_INCLUDE_FILES`, line 75).
pub const INCLUDE_FILES: usize = 64;

/// What the read is asked: the reference's `config_tree` keywords, and
/// its `IncludeContext` as far as this piece reads it.
#[derive(Clone, Debug, Default)]
pub struct Ask {
    /// Name `path` itself among [`ConfigValues::files`].
    pub stamp_self: bool,
    /// Skip `path` where it cannot be read for want of permission.
    pub gentle: bool,
    /// The git directory `gitdir:` matches against; None where not given
    /// (the condition is then not settled).
    pub git_dir: Option<Vec<u8>>,
    /// The working directory `realpath` and `abspath` read
    /// (`os.getcwd()`): absolute and free of links.
    pub cwd: Vec<u8>,
    /// A context: git's reader of remotes then refuses what it refuses
    /// (`_remote_rule`), `%(prefix)/` reads `prefix` and `onbranch:` reads
    /// `branch`.
    pub context: Option<Context>,
}

/// The branch HEAD names, as `IncludeContext.branch` answers it.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub enum Branch {
    /// `UNSETTLED`: not known; `onbranch:` is not settled.
    #[default]
    Unsettled,
    /// None: HEAD detached or naming another ref; `onbranch:` never holds.
    Elsewhere,
    /// A branch name under `refs/heads/`.
    Name(Vec<u8>),
    /// Not asked yet: the first `onbranch:` condition stops the read
    /// ([`ConfigValues::asks_branch`]), so the caller can ask (and stamp
    /// what the asking reads) where the reference's `IncludeContext.branch`
    /// is first called, and read again with the answer.
    Unasked,
}

/// `IncludeContext` (lines 126-178).
#[derive(Clone, Debug, Default)]
pub struct Context {
    /// `IncludeContext.prefix`: the directory, [`paths::AS_WRITTEN`], or
    /// None where it is not known.
    pub prefix: Option<Vec<u8>>,
    /// `IncludeContext.branch`.
    pub branch: Branch,
    /// `IncludeContext.hasconfig`: whether the git on PATH takes a
    /// `hasconfig:` condition; None where it is not known.
    pub hasconfig: Option<bool>,
    /// `IncludeContext.remote_urls`: every remote URL collected, in any
    /// order (read as a set, sorted); None until they are collected.
    pub remote_urls: Option<Vec<Vec<u8>>>,
    /// `IncludeContext.collecting`: the read that collects them.
    pub collecting: bool,
}

impl Context {
    /// `IncludeContext.collect()`: this context for the collecting read.
    #[must_use]
    pub fn collect(&self) -> Context {
        Context {
            remote_urls: None,
            collecting: true,
            ..self.clone()
        }
    }

    /// `IncludeContext.with_urls(urls)`: this context with the URLs.
    #[must_use]
    pub fn with_urls(&self, urls: Vec<Vec<u8>>) -> Context {
        Context {
            remote_urls: Some(urls),
            collecting: false,
            ..self.clone()
        }
    }
}

/// Names with their values, in the order names were first set.
pub type Values = Vec<(Name, Vec<Option<Vec<u8>>>)>;

/// `config_tree`'s answer (`ConfigValues`, line 78), less the stamps
/// (only the paths are kept).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ConfigValues {
    /// Each name with its values (a value cut at its first NUL, None for
    /// a key with no `=`), in the order names were first set.
    pub values: Values,
    pub complete: bool,
    /// Why git dies reading the tree.
    pub dies: Option<Vec<u8>>,
    /// What includes under conditions not settled here set.
    pub undecided: Values,
    /// Why git may die reading those includes, or read more.
    pub maybe_dies: Option<Vec<u8>>,
    /// Each file the read opened or looked for, in order (`files`' paths).
    pub files: Vec<Vec<u8>>,
    /// Each of [`ConfigValues::files`]' stamps, taken before the file is
    /// opened (`file_stamp`); None where nothing is there, or where the
    /// stamp is not held ([`ConfigValues::stamps_held`] false).
    pub stamps: Vec<Option<Stamp>>,
    /// Whether every stamp is the reference's (false: a file system that
    /// gives no inode, mode or change time, as on Windows).
    pub stamps_held: bool,
    /// The read stopped at an `onbranch:` condition under
    /// [`Branch::Unasked`]: the rest of the answer is not the reference's.
    pub asks_branch: bool,
    /// The `includeIf` subsection the read stopped at, its condition not
    /// evaluated here (a `gitdir:` one under the Windows flavour).
    pub not_ported: Option<Vec<u8>>,
    /// A `hasconfig:remote.*.url:` condition met before the remote URLs
    /// were collected.
    pub asks_remote_urls: bool,
    /// The collecting read met a remote URL in a file an `includeIf`
    /// reads, which git refuses.
    pub forbidden: bool,
    /// The exception the reference raises out of `config_tree` (a NUL in
    /// a path `realpath` states: `ValueError`).
    pub raised: Option<PyErr>,
}

enum Stop {
    /// `_Overflow`, with its text.
    Overflow(Vec<u8>),
    Dies(Vec<u8>),
    NotPorted(Vec<u8>),
    Raised(PyErr),
    AskBranch,
}

type Key = (String, Option<Vec<u8>>, String);

/// An ordered map of names to values.
#[derive(Default)]
struct Map {
    list: Values,
    index: BTreeMap<Key, usize>,
}

impl Map {
    fn push_all(&mut self, name: &Name, values: &[Option<Vec<u8>>]) {
        let key = (
            name.section.clone(),
            name.subsection.clone(),
            name.key.clone(),
        );
        match self.index.get(&key) {
            Some(&i) => self.list[i].1.extend_from_slice(values),
            None => {
                self.index.insert(key, self.list.len());
                self.list.push((name.clone(), values.to_vec()));
            }
        }
    }

    fn merge(&mut self, other: &Map) {
        for (name, values) in &other.list {
            self.push_all(name, values);
        }
    }
}

/// One `ConfigValues` being filled (`out`, or an undecided include's).
#[derive(Default)]
struct Out {
    values: Map,
    undecided: Map,
    maybe_dies: Option<Vec<u8>>,
    files: Vec<(Vec<u8>, Option<Stamp>)>,
}

struct Reading<'a, F: Fs> {
    fs: &'a F,
    machine: &'a Machine<'a>,
    ask: &'a Ask,
    files: usize,
    chain: Vec<FileId>,
    steps: Steps,
    /// The context's remote URLs, sorted, each once.
    urls: Option<Vec<Vec<u8>>>,
    forbidding: bool,
    asks_remote_urls: bool,
    forbidden: bool,
    stamps_held: bool,
}

fn words(parts: &[&[u8]]) -> Vec<u8> {
    parts.concat()
}

/// `config_tree(path, git_dir=..., stamped=True, stamp_self=...,
/// gentle=..., context=...)`.
pub fn config_tree<F: Fs>(fs: &F, machine: &Machine, path: &[u8], ask: &Ask) -> ConfigValues {
    let mut reading = Reading {
        fs,
        machine,
        ask,
        files: 0,
        chain: Vec::new(),
        steps: Steps::default(),
        urls: ask
            .context
            .as_ref()
            .and_then(|c| c.remote_urls.clone())
            .map(|mut urls| {
                urls.sort();
                urls.dedup();
                urls
            }),
        forbidding: false,
        asks_remote_urls: false,
        forbidden: false,
        stamps_held: true,
    };
    let mut out = Out::default();
    let stop = reading.read(path, &mut out, 0);
    let mut answer = ConfigValues {
        values: out.values.list,
        complete: true,
        dies: None,
        undecided: out.undecided.list,
        maybe_dies: out.maybe_dies,
        files: out.files.iter().map(|(p, _)| p.clone()).collect(),
        stamps: out.files.iter().map(|(_, s)| *s).collect(),
        stamps_held: reading.stamps_held,
        asks_branch: false,
        not_ported: None,
        raised: None,
        asks_remote_urls: reading.asks_remote_urls,
        forbidden: reading.forbidden,
    };
    match stop {
        Ok(()) => {}
        Err(Stop::Overflow(_)) => answer.complete = false,
        Err(Stop::Dies(why)) => answer.dies = Some(why),
        Err(Stop::NotPorted(sub)) => answer.not_ported = Some(sub),
        Err(Stop::Raised(error)) => answer.raised = Some(error),
        Err(Stop::AskBranch) => answer.asks_branch = true,
    }
    answer
}

fn unresolved(error: Unresolved, sub: &[u8]) -> Stop {
    match error {
        Unresolved::Raised(e) => Stop::Raised(e),
        Unresolved::NotPorted => Stop::NotPorted(sub.to_vec()),
    }
}

/// A refusal of the bytes opened, worded with the name as written.
fn named(dies: Dies, path: &[u8]) -> Dies {
    match dies {
        Dies::CannotRead { error, .. } => Dies::CannotRead {
            path: path.to_vec(),
            error,
        },
        Dies::Folder { .. } => Dies::Folder {
            path: path.to_vec(),
        },
    }
}

impl<F: Fs> Reading<'_, F> {
    fn read(&mut self, path: &[u8], out: &mut Out, depth: usize) -> Result<(), Stop> {
        self.files += 1;
        if self.files > INCLUDE_FILES {
            return Err(Stop::Overflow(path.to_vec()));
        }
        // What the OS opens: a relative name taken from the working
        // directory the caller gave, never the process's own.
        let at = paths::opened(self.machine.flavour, path, &self.ask.cwd);
        if depth > 0 || self.ask.stamp_self {
            let stamp = match file_stamp(self.fs, &at) {
                Ok(stamp) => stamp,
                Err(NotPorted) => {
                    self.stamps_held = false;
                    None
                }
            };
            out.files.push((path.to_vec(), stamp));
        }
        let gentle = self.ask.gentle && depth == 0;
        let (data, identity) = match config_bytes(self.fs, &at, gentle) {
            ConfigBytes::Skipped => return Ok(()),
            ConfigBytes::Dies(dies) => return Err(Stop::Dies(named(dies, path).why())),
            ConfigBytes::Overflow { .. } => return Err(Stop::Overflow(path.to_vec())),
            ConfigBytes::Read { data, identity } => (data, identity),
        };
        if depth > INCLUDE_DEPTH {
            return Err(Stop::Dies(words(&[
                path,
                format!(" is included past git's depth of {INCLUDE_DEPTH}").as_bytes(),
            ])));
        }
        if let Some(id) = &identity {
            if self.chain.contains(id) {
                return Err(Stop::Dies(words(&[
                    path,
                    b" is included in itself, which git follows past its depth",
                ])));
            }
        }
        let pushed = identity.is_some();
        if let Some(id) = identity {
            self.chain.push(id);
        }
        let parsed = self.parse(path, &data, out, depth);
        if pushed {
            self.chain.pop();
        }
        parsed
    }

    fn parse(&mut self, path: &[u8], data: &[u8], out: &mut Out, depth: usize) -> Result<(), Stop> {
        let Ok(entries) = git_entries(data) else {
            return Err(Stop::Dies(words(&[
                path,
                b" holds a line git's parser refuses",
            ])));
        };
        for entry in entries {
            let raw = entry.name();
            let name = entry.spelled();
            let value = entry.value_cut().map(<[u8]>::to_vec);
            out.values.push_all(&name, std::slice::from_ref(&value));
            if self.remote_rules() && !self.forbidding {
                if let Some(why) = remote_rules::remote_rule(&name, value.as_deref()) {
                    return Err(Stop::Dies(words(&[
                        path,
                        b" sets ",
                        String::from_utf8_lossy(&raw).as_bytes(),
                        b": ",
                        why.as_bytes(),
                    ])));
                }
            }
            if name.section == "include" && name.subsection.is_none() && name.key == "path" {
                self.include(path, None, value.as_deref(), out, depth)?;
            } else if name.section == "includeif" {
                if let Some(sub) = &name.subsection {
                    if name.key == "path" {
                        self.include(path, Some(sub), value.as_deref(), out, depth)?;
                    } else if sub.starts_with(REMOTE_URL) {
                        // Evaluated at each key: it may ask for the URLs.
                        self.condition(sub, path)?;
                    }
                }
            }
        }
        Ok(())
    }

    /// `_include`: under `condition` only where it holds; where it is not
    /// settled, into an undecided `ConfigValues` merged into `out`'s.
    fn include(
        &mut self,
        including: &[u8],
        condition: Option<&Vec<u8>>,
        value: Option<&[u8]>,
        out: &mut Out,
        depth: usize,
    ) -> Result<(), Stop> {
        let holds = match condition {
            None => Some(true),
            Some(sub) => self.condition(sub, including)?,
        };
        if holds == Some(false) {
            return Ok(());
        }
        if holds == Some(true) {
            let forbid = condition.is_some()
                && !self.forbidding
                && self.ask.context.as_ref().is_some_and(|c| c.collecting);
            return self.include_into(including, condition.is_none(), forbid, value, out, depth);
        }
        let mut maybe = Out::default();
        let read = self.include_into(
            including,
            condition.is_none(),
            false,
            value,
            &mut maybe,
            depth,
        );
        match read {
            Ok(()) => {}
            Err(Stop::Dies(why)) => maybe.maybe_dies = Some(why),
            Err(Stop::Overflow(what)) => {
                maybe.maybe_dies = Some(words(&[&what, b" is not read whole"]));
            }
            Err(other) => return Err(other),
        }
        out.undecided.merge(&maybe.values);
        out.undecided.merge(&maybe.undecided);
        if out.maybe_dies.is_none() {
            out.maybe_dies = maybe.maybe_dies;
        }
        out.files.append(&mut maybe.files);
        Ok(())
    }

    fn include_into(
        &mut self,
        including: &[u8],
        plain: bool,
        forbid: bool,
        value: Option<&[u8]>,
        out: &mut Out,
        depth: usize,
    ) -> Result<(), Stop> {
        let Some(value) = value else {
            let what: &[u8] = if plain {
                b"include.path"
            } else {
                b"an includeIf's path"
            };
            return Err(Stop::Dies(words(&[
                including,
                b" sets ",
                what,
                b" with no value",
            ])));
        };
        if spaced(value) {
            return Err(Stop::Overflow(value.to_vec()));
        }
        let target = self.target(value, including)?;
        if forbid {
            return self.forbidding_read(&target, out, depth);
        }
        self.read(&target, out, depth + 1)
    }

    /// `_forbidding` (683-700): `target` read for the collecting pass,
    /// its values dropped; a remote URL it or its includes set, taken or
    /// undecided, marks the read forbidden.
    fn forbidding_read(&mut self, target: &[u8], out: &mut Out, depth: usize) -> Result<(), Stop> {
        let mut sink = Out::default();
        self.forbidding = true;
        let read = self.read(target, &mut sink, depth + 1);
        self.forbidding = false;
        out.undecided.merge(&sink.undecided);
        if out.maybe_dies.is_none() {
            out.maybe_dies = sink.maybe_dies;
        }
        out.files.append(&mut sink.files);
        if sets_remote_url(&sink.values.list) || sets_remote_url(&sink.undecided.list) {
            self.forbidden = true;
        }
        read
    }

    /// `_Reading.remote_rules`: a context that is not collecting.
    fn remote_rules(&self) -> bool {
        self.ask.context.as_ref().is_some_and(|c| !c.collecting)
    }

    fn target(&self, value: &[u8], including: &[u8]) -> Result<Vec<u8>, Stop> {
        let prefix = self.ask.context.as_ref().and_then(|c| c.prefix.as_deref());
        include_target(self.machine, value, including, prefix).map_err(|e| match e {
            Unexpanded::Dies(why) => Stop::Dies(why),
            Unexpanded::Overflow => Stop::Overflow(value.to_vec()),
        })
    }

    /// `_condition` (1081-1110).
    fn condition(&mut self, sub: &[u8], including: &[u8]) -> Result<Option<bool>, Stop> {
        for (prefix, icase) in [(&b"gitdir:"[..], false), (b"gitdir/i:", true)] {
            if let Some(pattern) = sub.strip_prefix(prefix) {
                let Some(git_dir) = self.ask.git_dir.clone() else {
                    return Ok(None);
                };
                return self.gitdir_matches(sub, pattern, including, &git_dir, icase);
            }
        }
        if let Some(pattern) = sub.strip_prefix(b"onbranch:") {
            return self.branch_condition(pattern);
        }
        if let Some(pattern) = sub.strip_prefix(REMOTE_URL) {
            if let Some(context) = &self.ask.context {
                if context.hasconfig != Some(true) {
                    return Ok(context.hasconfig);
                }
            }
            return Ok(self.remote_url_condition(pattern));
        }
        Ok(Some(false))
    }

    /// `_remote_url_condition` (1150-1168): the pattern as written (no
    /// `**` added) against each URL, sorted.
    fn remote_url_condition(&mut self, pattern: &[u8]) -> Option<bool> {
        let context = self.ask.context.as_ref()?;
        if context.collecting {
            return Some(true);
        }
        let Some(urls) = &self.urls else {
            self.asks_remote_urls = true;
            return None;
        };
        any_match(
            pattern,
            urls.iter().map(Vec::as_slice),
            &mut self.steps,
            false,
        )
    }

    /// `_branch_condition` (1129-1147).
    fn branch_condition(&mut self, pattern: &[u8]) -> Result<Option<bool>, Stop> {
        let Some(context) = self.ask.context.as_ref() else {
            return Ok(None);
        };
        let branch = match &context.branch {
            Branch::Unasked => return Err(Stop::AskBranch),
            Branch::Unsettled => return Ok(None),
            Branch::Elsewhere => return Ok(Some(false)),
            Branch::Name(name) => name,
        };
        let mut spelled = pattern.to_vec();
        let sep = match self.machine.flavour {
            Flavour::Posix => b'/',
            Flavour::Windows => b'\\',
        };
        if matches!(spelled.last(), Some(&b) if b == b'/' || b == sep) {
            spelled.extend_from_slice(b"**");
        }
        Ok(any_match(
            &spelled,
            [branch.as_slice()],
            &mut self.steps,
            false,
        ))
    }

    /// `_gitdir_matches` (1177-1212). Under the Windows flavour every
    /// such condition with a git directory given is declined: it resolves
    /// the git directory with `realpath`, which is not ported there.
    fn gitdir_matches(
        &mut self,
        sub: &[u8],
        pattern: &[u8],
        including: &[u8],
        git_dir: &[u8],
        icase: bool,
    ) -> Result<Option<bool>, Stop> {
        let flavour = self.machine.flavour;
        if flavour == Flavour::Windows {
            return Err(Stop::NotPorted(sub.to_vec()));
        }
        let cwd = self.ask.cwd.clone();
        let prefix = self.ask.context.as_ref().and_then(|c| c.prefix.as_deref());
        // `_interpolate(real_home=True)`: HOME resolved where `~/` reads it.
        let real_home;
        let mut machine = self.machine;
        let user_empty =
            pattern.first() == Some(&b'~') && pattern.get(1).is_none_or(|&b| b == b'/');
        let resolved_machine;
        if user_empty {
            if let Some(home) = paths::home(self.machine) {
                real_home =
                    realpath(self.fs, flavour, &home, &cwd).map_err(|e| unresolved(e, sub))?;
                resolved_machine = Machine {
                    flavour,
                    home: Some(real_home),
                    profile: None,
                    user_home: self.machine.user_home,
                };
                machine = &resolved_machine;
            }
        }
        let expanded = match paths::interpolate(machine, pattern, prefix) {
            Err(_) => return Ok(None),
            Ok(found) => found,
        };
        let mut pattern = expanded.unwrap_or_else(|| pattern.to_vec());
        let mut literal = 0;
        if pattern.first() == Some(&b'.') && pattern.get(1) == Some(&b'/') {
            let real =
                realpath(self.fs, flavour, including, &cwd).map_err(|e| unresolved(e, sub))?;
            let base = paths::dirname(flavour, &real);
            literal = base.len() + 1;
            let mut joined = base;
            joined.extend_from_slice(&pattern[1..]);
            pattern = joined;
        } else if !paths::git_absolute(flavour, &pattern) {
            let mut joined = b"**/".to_vec();
            joined.extend_from_slice(&pattern);
            pattern = joined;
        }
        if pattern.last() == Some(&b'/') {
            pattern.extend_from_slice(b"**");
        }
        let real = realpath(self.fs, flavour, git_dir, &cwd).map_err(|e| unresolved(e, sub))?;
        let absolute = abspath(flavour, git_dir, &cwd).map_err(|e| unresolved(e, sub))?;
        let mut named = vec![real];
        if absolute != named[0] {
            named.push(absolute);
        }
        let rests: Vec<&[u8]> = named
            .iter()
            .filter(|t| literal_head(&pattern, t, literal, icase))
            .map(|t| &t[literal..])
            .collect();
        let tail = &pattern[literal.min(pattern.len())..];
        Ok(any_match(tail, rests, &mut self.steps, icase))
    }
}

/// The one `hasconfig:` condition git knows (`_HAS_REMOTE_URL`).
const REMOTE_URL: &[u8] = b"hasconfig:remote.*.url:";

/// `_remote_url_names` (703-706): whether any name is a
/// `remote.<name>.url` (a `remote.url` with no name is none).
fn sets_remote_url(names: &Values) -> bool {
    names
        .iter()
        .any(|(n, _)| n.section == "remote" && n.subsection.is_some() && n.key == "url")
}

/// The remote URLs the caller collects from the collecting reads
/// (`location_git.remote_config` 608-616): every value of each
/// `remote.<name>.url` they take, sorted, each once.
#[must_use]
pub fn collected_urls<'a>(reads: impl IntoIterator<Item = &'a ConfigValues>) -> Vec<Vec<u8>> {
    let mut urls: Vec<Vec<u8>> = reads
        .into_iter()
        .flat_map(|read| read.values.iter())
        .filter(|(n, _)| n.section == "remote" && n.subsection.is_some() && n.key == "url")
        .flat_map(|(_, values)| values.iter().flatten().cloned())
        .collect();
    urls.sort();
    urls.dedup();
    urls
}

/// Why an include's value names no file.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Unexpanded {
    /// `{including} includes {value!r}, which git cannot expand`.
    Dies(Vec<u8>),
    /// A `%(prefix)/` whose directory is not known: the read is
    /// incomplete.
    Overflow,
}

/// `_include_target(value, including, prefix)`: the file an include
/// names, expanded, taken from the including file's directory where it is
/// relative, and spelled with the flavour's separator.
pub fn include_target(
    machine: &Machine,
    value: &[u8],
    including: &[u8],
    prefix: Option<&[u8]>,
) -> Result<Vec<u8>, Unexpanded> {
    let flavour = machine.flavour;
    let expanded = paths::interpolate(machine, value, prefix).map_err(|_| Unexpanded::Overflow)?;
    let Some(mut target) = expanded else {
        return Err(Unexpanded::Dies(words(&[
            including,
            b" includes ",
            &paths::repr_text(value),
            b", which git cannot expand",
        ])));
    };
    if !paths::git_absolute(flavour, &target) {
        target = paths::join(flavour, &paths::dirname(flavour, including), &target);
    }
    Ok(paths::native(flavour, &target))
}
