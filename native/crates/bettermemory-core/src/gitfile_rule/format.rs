//! A repository's format from its config, as git 2.54's setup.c reads it
//! (`gitfile_rule.py` 235-288 and 398-597): [`judge_format`]
//! (`_judge_format`, with `_take`, `_dies` and `_extension_reads`) and
//! [`object_format`].
//!
//! The git on PATH is data ([`GitFacts`]): the release it names
//! (`_git_is_at_least`) and whether it reads a compatibility object
//! format (`_reads_compat`), both asked only where the config makes the
//! reference ask them.

use crate::gitconfig::{git_bool, git_entries, git_int, Entry, FORMAT_LIMIT};

const VERSION: &[u8] = b"core.repositoryformatversion";
const WORKTREE_CONFIG: &[u8] = b"extensions.worktreeconfig";
const OBJECT_FORMAT: &[u8] = b"extensions.objectformat";
const COMPAT_FORMAT: &[u8] = b"extensions.compatobjectformat";
const BARE: &[u8] = b"core.bare";
const WORKTREE: &[u8] = b"core.worktree";
const EXTENSIONS: &[u8] = b"extensions.";

/// What value an extension git knows reads (`_EXTENSIONS_V0`,
/// `_EXTENSIONS_V1`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Rule {
    Boolean,
    SomeValue,
    Any,
    OneOf(&'static [&'static [u8]]),
}

const HASHES: &[&[u8]] = &[b"sha1", b"sha256"];

/// `_EXTENSIONS_V0` and `_EXTENSIONS_V1`: the rule and whether the
/// extension is of format 1 alone.
fn extension_rule(ext: &[u8]) -> Option<(Rule, bool)> {
    Some(match ext {
        b"noop" => (Rule::Any, false),
        b"preciousobjects" => (Rule::Boolean, false),
        b"partialclone" => (Rule::SomeValue, false),
        b"worktreeconfig" => (Rule::Boolean, false),
        b"noop-v1" => (Rule::Any, true),
        b"objectformat" | b"compatobjectformat" => (Rule::OneOf(HASHES), true),
        b"refstorage" => (Rule::OneOf(&[b"files", b"reftable"]), true),
        b"relativeworktrees" | b"submodulepathconfig" => (Rule::Boolean, true),
        _ => return None,
    })
}

/// `_EXTENSIONS_SINCE`: the release that first reads an extension.
fn since(ext: &[u8]) -> Option<(u32, u32)> {
    match ext {
        b"refstorage" => Some((2, 44)),
        b"relativeworktrees" => Some((2, 48)),
        b"submodulepathconfig" => Some((2, 54)),
        _ => None,
    }
}

fn cut(value: &[u8]) -> &[u8] {
    value.split(|&b| b == 0).next().unwrap_or(value)
}

/// `_extension_reads(rule, value)`.
fn extension_reads(rule: Rule, value: Option<&[u8]>) -> bool {
    match rule {
        Rule::OneOf(names) => value.is_some_and(|v| names.contains(&cut(v))),
        Rule::Boolean => git_bool(value).is_some(),
        Rule::SomeValue => value.is_some(),
        Rule::Any => true,
    }
}

/// The git on PATH, as the format's reading asks it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct GitFacts {
    /// The release (major, minor) git names, None where it names none.
    pub release: Option<(u32, u32)>,
    /// `gitsystem.reads_compat()`.
    pub reads_compat: Option<bool>,
}

impl GitFacts {
    /// `_git_is_at_least(release)`.
    pub fn is_at_least(&self, release: (u32, u32)) -> Option<bool> {
        self.release.map(|have| have >= release)
    }
}

/// `_Format`: a repository's format as `_judge_format` reads it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Format {
    /// `core.repositoryformatversion`, -1 where it is not set.
    pub version: i64,
    /// `extensions.worktreeConfig`.
    pub worktree_config: bool,
    /// The `core.bare` taken (`_take`): its value, None for a key with no
    /// `=`.
    pub bare: Option<Option<Vec<u8>>>,
    /// The `core.worktree` taken.
    pub worktree: Option<Vec<u8>>,
    /// `extensions.objectFormat`, `sha1` where it is not set.
    pub object_format: Vec<u8>,
}

fn worded(parts: &[&[u8]]) -> Vec<u8> {
    parts.concat()
}

/// `_dies(path, name)`.
fn dies(path: &[u8], name: &[u8]) -> Vec<u8> {
    worded(&[path, b" sets ", name, b" to a value git dies on"])
}

/// `_take(name, value, taken)`: False where git dies on the value.
fn take(name: &[u8], value: Option<&[u8]>, format: &mut Format) -> bool {
    if name == BARE {
        if git_bool(value).is_none() {
            return false;
        }
        format.bare = Some(value.map(<[u8]>::to_vec));
    } else if name == WORKTREE {
        let Some(value) = value else {
            return false;
        };
        format.worktree = Some(value.to_vec());
    }
    true
}

/// `_judge_format(path, entries)`: the format the config file `path`,
/// whose entries are `entries`, gives a git directory, the last of each
/// name standing; or why git dies reading it, as the reference words it.
pub fn judge_format(path: &[u8], entries: &[Entry], git: &GitFacts) -> Result<Format, Vec<u8>> {
    let mut format = Format {
        version: -1,
        worktree_config: false,
        bare: None,
        worktree: None,
        object_format: b"sha1".to_vec(),
    };
    let (mut v1_only, mut unknown) = (false, false);
    let mut compat: Option<Vec<u8>> = None;
    let mut newer: Vec<&'static [u8]> = Vec::new();
    for entry in entries {
        let name = entry.name();
        let value = entry.value.as_deref();
        if name == VERSION {
            let Some(number) = git_int(value) else {
                return Err(dies(path, &name));
            };
            format.version = number;
        } else if let Some(ext) = name.strip_prefix(EXTENSIONS) {
            let rule = extension_rule(ext);
            if let Some((rule, _)) = rule {
                if !extension_reads(rule, value) {
                    return Err(dies(path, &name));
                }
            }
            if name == COMPAT_FORMAT {
                if compat.is_some() {
                    return Err(dies(path, &name));
                }
                compat = Some(value.map_or(Vec::new(), |v| cut(v).to_vec()));
            }
            v1_only |= rule.is_some_and(|(_, v1)| v1);
            unknown |= rule.is_none();
            for known in [
                &b"refstorage"[..],
                b"relativeworktrees",
                b"submodulepathconfig",
            ] {
                if ext == known && !newer.contains(&known) {
                    newer.push(known);
                }
            }
            if name == WORKTREE_CONFIG {
                format.worktree_config = git_bool(value) == Some(true);
            } else if name == OBJECT_FORMAT {
                if let Some(value) = value {
                    format.object_format = cut(value).to_vec();
                }
            }
        } else if !take(&name, value, &mut format) {
            return Err(dies(path, &name));
        }
    }
    let version = format.version;
    if version == 0 && v1_only {
        return Err(worded(&[
            path,
            b" sets an extension of format 1 in a repository of format 0",
        ]));
    }
    if version >= 1 && unknown {
        return Err(worded(&[
            path,
            b" sets a repository extension git does not know",
        ]));
    }
    if version >= 1 {
        for ext in newer {
            let Some(release) = since(ext) else { continue };
            match git.is_at_least(release) {
                None => {
                    return Err(worded(&[
                        path,
                        b" sets extensions.",
                        ext,
                        b", and this git has not named its version",
                    ]))
                }
                Some(false) => {
                    let text = format!(
                        ", which git before {}.{} does not know",
                        release.0, release.1
                    );
                    return Err(worded(&[path, b" sets extensions.", ext, text.as_bytes()]));
                }
                Some(true) => {}
            }
        }
    }
    if let Some(compat) = compat {
        if version != -1 && version <= 1 {
            if compat == format.object_format {
                return Err(worded(&[
                    path,
                    b" sets a compatibility object format the same as its object format, \
                      which no git from 2.45 reads",
                ]));
            }
            match git.reads_compat {
                None => {
                    return Err(worded(&[
                        path,
                        b" sets a compatibility object format git has not said it reads",
                    ]))
                }
                Some(false) => {
                    return Err(worded(&[
                        path,
                        b" sets a compatibility object format this git dies on under format 1",
                    ]))
                }
                Some(true) => {}
            }
        }
    }
    Ok(format)
}

/// `object_format(data)`: the object format git 2.54 reads a repository
/// in from the bytes of its common directory's `config`: `sha1` or
/// `sha256`, `sha1` where no version is set; None where git dies on the
/// format, cannot parse the file, or the file is over [`FORMAT_LIMIT`].
pub fn object_format(data: &[u8], git: &GitFacts) -> Option<Vec<u8>> {
    if data.len() > FORMAT_LIMIT {
        return None;
    }
    let entries = git_entries(data).ok()?;
    let read = judge_format(b"config", &entries, git).ok()?;
    if read.version > 1 {
        return None;
    }
    if read.version == -1 {
        return Some(b"sha1".to_vec());
    }
    Some(read.object_format)
}
