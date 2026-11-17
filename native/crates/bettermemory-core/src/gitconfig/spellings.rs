//! A checkout's remote URLs, as written and as git rewrites them, and the
//! keys a capture takes from them: `location_git.py`'s `_url_text`
//! (506-509), the rest of `_rewrites` after its call of `_spellings`
//! (606-617), `_spellings` (645-715), `_best_rule` (718-728),
//! `_undecided_matters` (731-769), `remote_urls` (772-781) and
//! `read_remotes` (798-831), over [`super::remotes::gather`].
//!
//! What the git on PATH does (`Sources.legacy`, `resets`, `version`) is an
//! input; where it is not known and matters, the answer declines with the
//! reference's `_Unknown` text. The key of a URL is the caller's function,
//! as `read_remotes` takes it: a hosted URL's key is
//! `capture::remote::remote_key`'s, a local one needs the file system.
//! Only `url.<base>.insteadOf` rewrites a URL here; `pushInsteadOf` and
//! `pushurl` are not read, as the reference reads neither.

use super::legacy::{default_branch, legacy_remote, Against, Files, Halt, Legacy};
use super::remotes::{gather, Inputs, NotPortedWhy, Outcome};
use super::tree::ConfigValues;
use super::{spaced, KEEPS_SPACES_SINCE};
use crate::fsdata::Fs;
use crate::githead::RefFs;
use crate::pyvalue::PyStr;
use std::collections::{HashMap, HashSet};

/// `Spelling`: a URL as written, the URL git rewrites it to (None where no
/// rule matches), and whether the rule is the machine's.
pub type Spelling = (PyStr, Option<PyStr>, bool);

/// `_Remotes`: the URLs keyed, the files read, the machine's files named,
/// and why the files do not settle them.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Remotes {
    pub spellings: Vec<Spelling>,
    pub files: Files,
    pub named: Vec<Vec<u8>>,
    pub declined: Option<Vec<u8>>,
}

/// `RemoteKeys`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RemoteKeys {
    pub keys: Vec<PyStr>,
    pub aliases: Vec<PyStr>,
    pub files: Files,
    pub sources: Vec<Vec<u8>>,
    pub declined: Option<Vec<u8>>,
}

/// `_Rewrites` (and, of the same shape, `_spellings`' remotes: each
/// name with its URLs and whether each is the own config's): each insteadOf base in the order git met it, with its
/// prefixes, each with whether the machine's config gives it.
pub type Rewrites = Vec<(Vec<u8>, Vec<(Vec<u8>, bool)>)>;

/// A rule `_best_rule` finds: its base, its prefix, whether the machine's.
pub type Rule = (Vec<u8>, Vec<u8>, bool);

const RESETS_UNKNOWN: &[u8] = b"whether this git empties a remote's URLs at an empty one";
const LEGACY_UNKNOWN: &[u8] = b"whether this git reads legacy remote files";

/// `_url_text`: the bytes as UTF-8, a byte that is none replaced.
pub fn url_text(raw: &[u8]) -> PyStr {
    String::from_utf8_lossy(raw)
        .chars()
        .map(u32::from)
        .collect()
}

/// `_best_rule(url, rewrites)`: the longest prefix that `url` starts
/// with, of equal ones the first met.
pub fn best_rule(url: &[u8], rewrites: &Rewrites) -> Option<Rule> {
    let mut best: Option<(&[u8], &[u8], bool)> = None;
    for (base, prefixes) in rewrites {
        for (prefix, by_machine) in prefixes {
            if url.starts_with(prefix) && best.is_none_or(|b| b.1.len() < prefix.len()) {
                best = Some((base, prefix, *by_machine));
            }
        }
    }
    best.map(|(b, p, m)| (b.to_vec(), p.to_vec(), m))
}

fn remote_url_name(section: &str, sub: Option<&[u8]>, key: &str) -> bool {
    section == "remote" && key == "url" && sub.is_some_and(|s| !s.starts_with(b"/"))
}

/// `_undecided_matters(reads, best, legacy, rewrites)`.
pub fn undecided_matters(
    reads: &[&ConfigValues],
    best: &[(Vec<u8>, Option<Rule>)],
    legacy: bool,
    rewrites: &Rewrites,
) -> Option<Vec<u8>> {
    type Index<'r> = HashMap<&'r [u8], &'r [(Vec<u8>, bool)]>;
    let mut by_base: Option<Index> = None;
    for read in reads {
        if let Some(dies) = &read.maybe_dies {
            return Some(
                [
                    &b"an include under an unsettled condition may make git die: "[..],
                    dies,
                ]
                .concat(),
            );
        }
        for (name, found) in &read.undecided {
            let sub = name.subsection.as_deref();
            if remote_url_name(&name.section, sub, &name.key) {
                return Some(b"an include sets a remote URL under an unsettled condition".to_vec());
            }
            if let (true, Some(sub)) = (name.section == "url" && name.key == "insteadof", sub) {
                let matches = found
                    .iter()
                    .flatten()
                    .any(|p| best.iter().any(|(u, _)| u.starts_with(p)));
                if matches {
                    return Some(b"an include sets an insteadOf rule matching a remote URL under an unsettled condition".to_vec());
                }
                // `rewrites.get(sub)` and its prefixes as a set, as the
                // reference takes them: the bases indexed once.
                let index = by_base.get_or_insert_with(|| {
                    rewrites
                        .iter()
                        .map(|(b, ps)| (b.as_slice(), ps.as_slice()))
                        .collect()
                });
                let tied: HashSet<&[u8]> = index
                    .get(sub)
                    .map(|ps| ps.iter().map(|(p, _)| p.as_slice()).collect())
                    .unwrap_or_default();
                let is_tied = best.iter().any(|(_, r)| {
                    r.as_ref()
                        .is_some_and(|(b, p, _)| b.as_slice() != sub && tied.contains(p.as_slice()))
                });
                if is_tied {
                    return Some(b"an include names the base of an insteadOf rule tied with the one rewriting a remote URL under an unsettled condition".to_vec());
                }
            }
            if legacy && name.section == "init" && sub.is_none() && name.key == "defaultbranch" {
                return Some(
                    b"an include sets the default branch under an unsettled condition".to_vec(),
                );
            }
        }
    }
    None
}

/// What `_spellings` answers past its own declines.
enum Spelled {
    Out(Vec<Spelling>),
    Declined(Vec<u8>),
}

/// `_spellings(reads, legacy, legacy_remote, sources)`: `reads` each with
/// whether it is the repository's own; `legacy_remote` reads one legacy
/// remote against what this git does.
fn spellings(
    reads: &[(&ConfigValues, bool)],
    legacy: &[Vec<u8>],
    inputs: &Inputs,
    mut read_legacy: impl FnMut(&[u8], &Against) -> Result<Legacy, Halt>,
) -> Result<Spelled, NotPortedWhy> {
    let sources = &inputs.sources;
    let declined = |why: &[u8]| Ok(Spelled::Declined(why.to_vec()));
    let mut remotes: Rewrites = Vec::new();
    let mut rewrites: Rewrites = Vec::new();
    // Where each name and base sits in its list, so a long config is not
    // searched once per entry.
    let mut remote_at: HashMap<Vec<u8>, usize> = HashMap::new();
    let mut base_at: HashMap<Vec<u8>, usize> = HashMap::new();
    for (read, mine) in reads {
        for (name, found) in &read.values {
            let Some(sub) = name.subsection.as_deref() else {
                continue;
            };
            let section = name.section.as_str();
            let key = name.key.as_str();
            let checked = matches!((section, key), ("remote", "url") | ("url", "insteadof"));
            if checked && found.iter().flatten().any(|v| !v.is_empty() && spaced(v)) {
                let old = sources
                    .version
                    .is_none_or(|(a, b, _)| (a, b) < KEEPS_SPACES_SINCE);
                if old {
                    return declined(
                        format!(
                            "a {section}.{key} holds blank space git before 2.45 reads as a space"
                        )
                        .as_bytes(),
                    );
                }
            }
            if remote_url_name(section, Some(sub), key) {
                let at = *remote_at.entry(sub.to_vec()).or_insert_with(|| {
                    remotes.push((sub.to_vec(), Vec::new()));
                    remotes.len() - 1
                });
                let entries = &mut remotes[at].1;
                for value in found {
                    match value.as_deref() {
                        Some(v) if !v.is_empty() => entries.push((v.to_vec(), *mine)),
                        _ => match sources.resets {
                            None => return declined(RESETS_UNKNOWN),
                            Some(true) => entries.clear(),
                            Some(false) => entries.push((Vec::new(), *mine)),
                        },
                    }
                }
            } else if section == "url" && key == "insteadof" {
                let at = *base_at.entry(sub.to_vec()).or_insert_with(|| {
                    rewrites.push((sub.to_vec(), Vec::new()));
                    rewrites.len() - 1
                });
                rewrites[at]
                    .1
                    .extend(found.iter().flatten().map(|p| (p.clone(), !mine)));
            }
        }
    }
    let pending: Vec<&Vec<u8>> = legacy
        .iter()
        .filter(|name| {
            remote_at
                .get(*name)
                .is_none_or(|&at| remotes[at].1.is_empty())
        })
        .collect();
    if !pending.is_empty() {
        match sources.legacy {
            None => return declined(LEGACY_UNKNOWN),
            Some(false) => {}
            Some(true) => {
                let against = Against {
                    resets: sources.resets,
                    default: default_branch(
                        inputs.env.git_test_default_initial_branch_name.as_deref(),
                        reads.iter().map(|(r, _)| *r),
                    ),
                };
                for name in &pending {
                    let urls = match read_legacy(name, &against) {
                        Ok(Legacy::Urls(urls)) => Some(urls),
                        Ok(Legacy::Dies) => None,
                        Ok(Legacy::Unknown) => return declined(RESETS_UNKNOWN),
                        Err(Halt::Declines(why)) => return declined(&why),
                        Err(Halt::NotPorted) => return Err(NotPortedWhy::Stamp),
                    };
                    if let Some(urls) = urls {
                        let urls: Vec<(Vec<u8>, bool)> =
                            urls.into_iter().map(|u| (u, true)).collect();
                        match remote_at.get(*name) {
                            Some(&at) => remotes[at].1 = urls,
                            None => {
                                remote_at.insert((*name).clone(), remotes.len());
                                remotes.push(((*name).clone(), urls));
                            }
                        }
                    }
                }
            }
        }
    }
    let mut best: Vec<(Vec<u8>, Option<Rule>)> = Vec::new();
    let mut seen: HashSet<&[u8]> = HashSet::new();
    for (_, found) in &remotes {
        for (url, mine) in found {
            if *mine && !url.is_empty() && seen.insert(url) {
                best.push((url.clone(), best_rule(url, &rewrites)));
            }
        }
    }
    let plain: Vec<&ConfigValues> = reads.iter().map(|(r, _)| *r).collect();
    if let Some(why) = undecided_matters(&plain, &best, !pending.is_empty(), &rewrites) {
        return declined(&why);
    }
    Ok(Spelled::Out(
        best.into_iter()
            .map(|(url, rule)| match rule {
                None => (url_text(&url), None, false),
                Some((base, prefix, by_machine)) => {
                    let rewritten = [&base[..], &url[prefix.len()..]].concat();
                    (url_text(&url), Some(url_text(&rewritten)), by_machine)
                }
            })
            .collect(),
    ))
}

/// `_rewrites(common_dir, git_dir)`.
pub fn rewrites<F: Fs>(
    fs: &F,
    refs: &dyn RefFs,
    common_dir: &[u8],
    git_dir: &[u8],
    inputs: &Inputs,
) -> Result<Remotes, NotPortedWhy> {
    let gathered = gather(fs, refs, common_dir, git_dir, inputs)?;
    let mut files = gathered.files;
    let named = gathered.named;
    let (machine, own, legacy) = match gathered.outcome {
        Outcome::Declined(why) => {
            return Ok(Remotes {
                spellings: Vec::new(),
                files,
                named,
                declined: Some(why),
            })
        }
        Outcome::NoRemotes => {
            return Ok(Remotes {
                spellings: Vec::new(),
                files,
                named,
                declined: None,
            })
        }
        Outcome::Reads {
            machine,
            own,
            legacy,
        } => (machine, own, legacy),
    };
    let reads: Vec<(&ConfigValues, bool)> = machine
        .iter()
        .map(|r| (r, false))
        .chain(own.iter().map(|r| (r, true)))
        .collect();
    let flavour = inputs.machine.flavour;
    let spelled = spellings(&reads, &legacy, inputs, |name, against| {
        legacy_remote(fs, flavour, common_dir, name, &mut files, against)
    })?;
    let (spellings, declined) = match spelled {
        Spelled::Out(out) => (out, None),
        Spelled::Declined(why) => (Vec::new(), Some(why)),
    };
    Ok(Remotes {
        spellings,
        files,
        named,
        declined,
    })
}

/// `remote_urls(common_dir, git_dir)`: each URL as written, then each
/// rewritten one.
pub fn remote_urls(found: &Remotes) -> Vec<PyStr> {
    found
        .spellings
        .iter()
        .map(|(u, _, _)| u.clone())
        .chain(found.spellings.iter().filter_map(|(_, r, _)| r.clone()))
        .collect()
}

/// `read_remotes(common_dir, git_dir, key)` over what `rewrites` found;
/// `key` is the caller's (`location.remote_key(u, base)`), None or empty
/// for no key.
pub fn read_remotes(found: Remotes, key: &dyn Fn(&[u32]) -> Option<PyStr>) -> RemoteKeys {
    let some = |k: Option<PyStr>| k.filter(|k| !k.is_empty());
    let mut keys: Vec<PyStr> = Vec::new();
    let mut fetched: HashSet<PyStr> = HashSet::new();
    let mut aliased: Vec<PyStr> = Vec::new();
    for (url, rewritten, machine) in &found.spellings {
        let written = some(key(url));
        let as_fetched = match rewritten {
            None => written.clone(),
            Some(r) => some(key(r)),
        };
        keys.extend(written.iter().chain(as_fetched.iter()).cloned());
        fetched.extend(as_fetched.iter().cloned());
        if let (Some(w), Some(f)) = (&written, &as_fetched) {
            if *machine && f != w {
                aliased.push(w.clone());
            }
        }
    }
    keys.sort();
    keys.dedup();
    // `sorted(aliased - fetched)`: the set difference has no order of its
    // own, and the reference sorts it (code points), as here.
    let mut aliases: Vec<PyStr> = aliased
        .into_iter()
        .filter(|a| !fetched.contains(a))
        .collect();
    aliases.sort();
    aliases.dedup();
    RemoteKeys {
        keys,
        aliases,
        files: found.files,
        sources: found.named,
        declined: found.declined,
    }
}
