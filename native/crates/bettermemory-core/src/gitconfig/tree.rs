//! A config file and its plain includes, as the reference reads them:
//! `config_tree` (`gitconfig.py` 255-289), `_read_config` (358-386),
//! `_parse_config` (408-444) and `_include` for `include.path` (463-525),
//! with `_include_target` (594-609). An `includeIf` that the reference
//! would act on ([`acts_on`]) stops the read as
//! [`ConfigValues::not_ported`]: its conditions are the next unit's. One
//! whose condition git does not know is skipped, as the reference skips
//! it.
//!
//! Bounds: 64 files in all ([`INCLUDE_FILES`], each file counted before
//! it is opened, a missing one too) and git's depth of 10
//! ([`INCLUDE_DEPTH`], checked only once the file is found), and the
//! 2 MiB of each file ([`super::file::config_bytes`]). A file included in
//! itself is found by its identity ([`crate::fsdata::FileId`]).

use std::collections::BTreeMap;

use super::file::{config_bytes, ConfigBytes};
use super::paths::{self, Machine};
use super::{git_entries, remote_rules, spaced, Name};
use crate::fsdata::{FileId, Fs};

/// git's bound on nested includes (`_INCLUDE_DEPTH`, line 66).
pub const INCLUDE_DEPTH: usize = 10;
/// Files one read takes in all (`_INCLUDE_FILES`, line 75).
pub const INCLUDE_FILES: usize = 64;

/// What the read is asked: the reference's `config_tree` keywords this
/// piece takes, and its `IncludeContext` as far as this piece reads it.
#[derive(Clone, Debug, Default)]
pub struct Ask {
    /// Name `path` itself among [`ConfigValues::files`].
    pub stamp_self: bool,
    /// Skip `path` where it cannot be read for want of permission.
    pub gentle: bool,
    /// A context: git's reader of remotes then refuses what it refuses
    /// (`_remote_rule`), and `%(prefix)/` reads `prefix`.
    pub context: Option<Context>,
}

/// The parts of `IncludeContext` this piece reads.
#[derive(Clone, Debug, Default)]
pub struct Context {
    /// `IncludeContext.prefix`: the directory, [`paths::AS_WRITTEN`], or
    /// None where it is not known.
    pub prefix: Option<Vec<u8>>,
}

/// `config_tree`'s answer (`ConfigValues`, line 78), less what the next
/// unit fills (`undecided`, `maybe_dies`, `asks_remote_urls`,
/// `forbidden`) and the stamps (only the paths are kept).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ConfigValues {
    /// Each name with its values (a value cut at its first NUL, None for
    /// a key with no `=`), in the order names were first set.
    pub values: Vec<(Name, Vec<Option<Vec<u8>>>)>,
    pub complete: bool,
    /// Why git dies reading the tree.
    pub dies: Option<Vec<u8>>,
    /// Each file the read opened or looked for, in order (`files`' paths).
    pub files: Vec<Vec<u8>>,
    /// The `includeIf` subsection the read stopped at, its condition not
    /// evaluated here.
    pub not_ported: Option<Vec<u8>>,
}

enum Stop {
    Overflow,
    Dies(Vec<u8>),
    NotPorted(Vec<u8>),
}

struct Reading<'a, F: Fs> {
    fs: &'a F,
    machine: &'a Machine<'a>,
    ask: &'a Ask,
    files: usize,
    chain: Vec<FileId>,
    out: ConfigValues,
    index: BTreeMap<(String, Option<Vec<u8>>, String), usize>,
}

fn words(parts: &[&[u8]]) -> Vec<u8> {
    parts.concat()
}

/// `config_tree(path, stamped=True, stamp_self=..., gentle=..., context=...)`.
pub fn config_tree<F: Fs>(fs: &F, machine: &Machine, path: &[u8], ask: &Ask) -> ConfigValues {
    let mut reading = Reading {
        fs,
        machine,
        ask,
        files: 0,
        chain: Vec::new(),
        out: ConfigValues {
            complete: true,
            ..ConfigValues::default()
        },
        index: BTreeMap::new(),
    };
    match reading.read(path, 0) {
        Ok(()) => {}
        Err(Stop::Overflow) => reading.out.complete = false,
        Err(Stop::Dies(why)) => reading.out.dies = Some(why),
        Err(Stop::NotPorted(sub)) => reading.out.not_ported = Some(sub),
    }
    reading.out
}

impl<F: Fs> Reading<'_, F> {
    fn read(&mut self, path: &[u8], depth: usize) -> Result<(), Stop> {
        self.files += 1;
        if self.files > INCLUDE_FILES {
            return Err(Stop::Overflow);
        }
        if depth > 0 || self.ask.stamp_self {
            self.out.files.push(path.to_vec());
        }
        let gentle = self.ask.gentle && depth == 0;
        let (data, identity) = match config_bytes(self.fs, path, gentle) {
            ConfigBytes::Skipped => return Ok(()),
            ConfigBytes::Dies(dies) => return Err(Stop::Dies(dies.why())),
            ConfigBytes::Overflow { .. } => return Err(Stop::Overflow),
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
        let parsed = self.parse(path, &data, depth);
        if pushed {
            self.chain.pop();
        }
        parsed
    }

    fn parse(&mut self, path: &[u8], data: &[u8], depth: usize) -> Result<(), Stop> {
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
            let key = (
                name.section.clone(),
                name.subsection.clone(),
                name.key.clone(),
            );
            match self.index.get(&key) {
                Some(&i) => self.out.values[i].1.push(value.clone()),
                None => {
                    self.index.insert(key, self.out.values.len());
                    self.out.values.push((name.clone(), vec![value.clone()]));
                }
            }
            if self.ask.context.is_some() {
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
                self.include(path, value.as_deref(), depth)?;
            } else if name.section == "includeif" {
                if let Some(sub) = &name.subsection {
                    if acts_on(sub, &name.key, self.ask.context.is_some()) {
                        return Err(Stop::NotPorted(sub.clone()));
                    }
                }
            }
        }
        Ok(())
    }

    fn include(
        &mut self,
        including: &[u8],
        value: Option<&[u8]>,
        depth: usize,
    ) -> Result<(), Stop> {
        let Some(value) = value else {
            return Err(Stop::Dies(words(&[
                including,
                b" sets include.path with no value",
            ])));
        };
        if spaced(value) {
            return Err(Stop::Overflow);
        }
        let target = self.target(value, including)?;
        self.read(&target, depth + 1)
    }
    fn target(&self, value: &[u8], including: &[u8]) -> Result<Vec<u8>, Stop> {
        let prefix = self.ask.context.as_ref().and_then(|c| c.prefix.as_deref());
        include_target(self.machine, value, including, prefix).map_err(|e| match e {
            Unexpanded::Dies(why) => Stop::Dies(why),
            Unexpanded::Overflow => Stop::Overflow,
        })
    }
}

/// Whether the reference acts on an `includeIf.<sub>.<key>` (lines
/// 436-444 and `_condition`, 1095-1110): at `path`, a condition starting
/// (case-sensitively) `gitdir:`, `gitdir/i:`, `onbranch:` or
/// `hasconfig:remote.*.url:` is evaluated and may read a file; any other
/// condition is False and nothing is read, the value not even looked at.
/// At another key only a `hasconfig:remote.*.url:` one is evaluated, and
/// with no context that evaluation does nothing.
pub fn acts_on(sub: &[u8], key: &str, context: bool) -> bool {
    const REMOTE_URL: &[u8] = b"hasconfig:remote.*.url:";
    if key == "path" {
        return [&b"gitdir:"[..], b"gitdir/i:", b"onbranch:", REMOTE_URL]
            .iter()
            .any(|p| sub.starts_with(p));
    }
    context && sub.starts_with(REMOTE_URL)
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
