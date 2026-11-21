//! Unit 3 of `origin_gitfiles` (the README's unit 5 with the text facts
//! of its unit 3): what git dies on in a repository's or the machine's
//! config, what a config text says, and the directories a check holds.
//!
//! - [`facts_of`] (`_facts_of`, reference lines 77-94) with
//!   [`ConfigFacts`] (`_ConfigFacts`, 50-65), [`no_config`] (`_NO_CONFIG`,
//!   68-70) and `_BARE_FALSE` (46) matched by hand;
//! - [`rev_parse_dies`] (`_rev_parse_dies`, 876-920) and its tables
//!   (722-873), [`merge_word_dies`] (923-939), `_git_is_at_least`
//!   (942-948) as [`GitFacts::is_at_least`];
//! - [`machine_config_dies`] (951-989), [`machine_value_dies`] (992-999);
//! - [`config_entries`] (1002-1014), [`config_variable_names`]
//!   (1017-1028);
//! - [`hold_directory`] (653-686), [`directories_held`] (689-699),
//!   [`global_attributes_path`] (702-720).
//!
//! The git on PATH is data ([`GitFacts`]), the environment is two
//! arguments, and the file system is the caller's ([`Fs`]).

use std::collections::{BTreeMap, BTreeSet};

use crate::fsdata::{Flavour, Fs, OsError, PyErr};
use crate::gitconfig::remotes::NotPortedWhy;
use crate::gitconfig::tree::ConfigValues;
use crate::gitconfig::{git_bool, git_entries, git_int, Entry};
use crate::gitfile_rule::format::GitFacts;
use crate::gitfile_rule::hostpath::{dirname, isabs, join};
use crate::gitfile_rule::probe::{path_missing, Raised};

/// `_ConfigFacts`: what the callers read from one configuration file.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ConfigFacts {
    /// The variables set, as git reads them; None where git refuses the
    /// file or it holds a NUL byte.
    pub names: Option<BTreeSet<Vec<u8>>>,
    pub attributes_elsewhere: bool,
    pub bare_moves: bool,
    pub git_dies: bool,
    pub includes: bool,
}

/// `_NO_CONFIG`: no names, every fact false.
pub fn no_config() -> ConfigFacts {
    ConfigFacts {
        names: Some(BTreeSet::new()),
        ..ConfigFacts::default()
    }
}

const INCLUDE_SECTIONS: [&[u8]; 2] = [b"include.", b"includeif."];

fn contains(hay: &[u8], needle: &[u8]) -> bool {
    hay.windows(needle.len()).any(|w| w == needle)
}

fn blanks(line: &[u8], mut at: usize) -> usize {
    while matches!(line.get(at), Some(b' ' | b'\t')) {
        at += 1;
    }
    at
}

/// `_BARE_FALSE.fullmatch(line)` is not None, for
/// `[ \t]*bare[ \t]*=[ \t]*(?:false|no|off|0)[ \t]*\r?`. Each blank run is
/// followed by a byte it cannot hold and no word is a prefix of another
/// followed by what the rest takes, so the greedy reading is the only
/// match (rule 3 of the pyre decision).
fn bare_false(line: &[u8]) -> bool {
    let at = blanks(line, 0);
    let Some(rest) = line[at..].strip_prefix(b"bare") else {
        return false;
    };
    let rest = &rest[blanks(rest, 0)..];
    let Some(rest) = rest.strip_prefix(b"=") else {
        return false;
    };
    let rest = &rest[blanks(rest, 0)..];
    let Some(rest) = [&b"false"[..], b"no", b"off", b"0"]
        .iter()
        .find_map(|w| rest.strip_prefix(*w))
    else {
        return false;
    };
    let rest = &rest[blanks(rest, 0)..];
    rest.is_empty() || rest == b"\r"
}

fn cut(value: &[u8]) -> &[u8] {
    value.split(|&b| b == 0).next().unwrap_or(value)
}

/// `_facts_of(data)`.
pub fn facts_of(data: &[u8], git: &GitFacts) -> ConfigFacts {
    let entries = config_entries(data);
    let lowered = data.to_ascii_lowercase();
    let named = |entries: &[Entry]| -> Vec<(Vec<u8>, Option<Vec<u8>>)> {
        entries
            .iter()
            .map(|e| (e.name(), e.value.clone()))
            .collect()
    };
    let pairs = entries.as_deref().map(named);
    ConfigFacts {
        names: pairs
            .as_ref()
            .map(|p| p.iter().map(|(n, _)| n.clone()).collect()),
        attributes_elsewhere: contains(&lowered, b"attributesfile") || contains(&lowered, b"[attr"),
        bare_moves: lowered
            .split(|&b| b == b'\n')
            .any(|line| contains(line, b"bare") && !bare_false(line)),
        git_dies: pairs.as_ref().is_some_and(|p| {
            p.iter()
                .any(|(n, v)| rev_parse_dies(n, v.as_deref().map(cut), git))
        }),
        includes: pairs.as_ref().is_some_and(|p| {
            p.iter()
                .any(|(n, _)| INCLUDE_SECTIONS.iter().any(|s| n.starts_with(s)))
        }),
    }
}

const DIES_UNLESS_BOOL: [&[u8]; 31] = [
    b"core.filemode",
    b"core.ignorecase",
    b"core.precomposeunicode",
    b"core.symlinks",
    b"core.sparsecheckout",
    b"core.sparsecheckoutcone",
    b"core.trustctime",
    b"core.quotepath",
    b"core.ignorestat",
    b"core.protecthfs",
    b"core.protectntfs",
    b"core.warnambiguousrefs",
    b"core.multipackindex",
    b"core.commitgraph",
    b"user.useconfigonly",
    b"color.pager",
    b"pager.color",
    b"sparse.expectfilesoutsideofpatterns",
    b"extensions.preciousobjects",
    b"extensions.worktreeconfig",
    b"feature.manyfiles",
    b"pack.usesparse",
    b"pack.usepathwalk",
    b"pack.readreverseindex",
    b"pack.usebitmapboundarytraversal",
    b"commitgraph.readchangedpaths",
    b"gc.writecommitgraph",
    b"fetch.writecommitgraph",
    b"core.fscache",
    b"core.longpaths",
    b"core.preloadindex",
];

const DIES_UNSET: [&[u8]; 22] = [
    b"core.excludesfile",
    b"core.attributesfile",
    b"core.editor",
    b"core.askpass",
    b"core.whitespace",
    b"core.fsync",
    b"core.fsyncmethod",
    b"core.checkroundtripencoding",
    b"core.untrackedcache",
    b"user.name",
    b"user.email",
    b"author.name",
    b"author.email",
    b"committer.name",
    b"committer.email",
    b"i18n.commitencoding",
    b"i18n.logoutputencoding",
    b"attr.tree",
    b"extensions.partialclone",
    b"core.notesref",
    b"core.hookspath",
    b"mailmap.file",
];

/// `_OLDER_DIES`: documentation in the reference (each is also in one of
/// the tables read), kept for the inventory.
pub const OLDER_DIES: [&[u8]; 5] = [
    b"core.notesref",
    b"core.hookspath",
    b"core.bigfilethreshold",
    b"core.preloadindex",
    b"mailmap.file",
];

const DIES_UNLESS_SIZE: &[u8] = b"core.bigfilethreshold";

/// `_MERGE_WORDS_SINCE`.
pub const MERGE_WORDS_SINCE: [(&[u8], (u32, u32)); 3] = [
    (b"always", (0, 0)),
    (b"inherit", (2, 35)),
    (b"simple", (2, 37)),
];
const MERGE_EXACT_SINCE: (u32, u32) = (2, 35);

/// `_DIES_UNLESS_WORD`: the word and whether no value dies.
const DIES_UNLESS_WORD: [(&[u8], &[u8], bool); 3] = [
    (b"core.autocrlf", b"input", false),
    (b"core.safecrlf", b"warn", false),
    (b"core.logallrefupdates", b"always", true),
];

const DIES_UNLESS_NAMED: [(&[u8], &[&[u8]]); 2] = [
    (
        b"push.default",
        &[
            b"nothing",
            b"matching",
            b"simple",
            b"upstream",
            b"tracking",
            b"current",
        ],
    ),
    (
        b"branch.autosetuprebase",
        &[b"never", b"local", b"remote", b"always"],
    ),
];

const READ_ANY: [&[u8]; 10] = [
    b"core.bare",
    b"core.worktree",
    b"core.pager",
    b"core.sshcommand",
    b"core.eol",
    b"core.fsmonitor",
    b"core.splitindex",
    b"core.alternaterefscommand",
    b"core.alternaterefsprefixes",
    b"extensions.noop",
];
const READ_WHOLE: [&[u8]; 5] = [
    b"core.",
    b"extensions.",
    b"feature.",
    b"index.",
    b"color.advice",
];
const NEGOTIATION_ALGORITHMS: [&[u8]; 4] = [b"skipping", b"noop", b"consecutive", b"default"];
const READ_ELSEWHERE: [&[u8]; 2] = [b"pack.packsizelimit", b"pack.compression"];

fn among(name: &[u8], table: &[&[u8]]) -> bool {
    table.contains(&name)
}

/// `_rev_parse_dies(name, value)`: whether `git rev-parse --show-toplevel
/// HEAD` may die on the repository's config setting `name` to `value`.
pub fn rev_parse_dies(name: &[u8], value: Option<&[u8]>, git: &GitFacts) -> bool {
    if among(name, &DIES_UNLESS_BOOL) || name.starts_with(b"advice.") {
        return git_bool(value).is_none();
    }
    if among(name, &DIES_UNSET) {
        return value.is_none();
    }
    if name == DIES_UNLESS_SIZE {
        return match value {
            None => true,
            Some(v) => v.contains(&b'-') || git_int(Some(v)).is_none(),
        };
    }
    if let Some(&(_, word, unset_dies)) = DIES_UNLESS_WORD.iter().find(|(n, _, _)| *n == name) {
        let Some(v) = value else {
            return unset_dies;
        };
        return cut(v).to_ascii_lowercase() != word && git_bool(value).is_none();
    }
    if let Some((_, words)) = DIES_UNLESS_NAMED.iter().find(|(n, _)| *n == name) {
        return value.is_none_or(|v| !words.contains(&cut(v)));
    }
    if name == b"branch.autosetupmerge" {
        return merge_word_dies(value, git);
    }
    if name == b"core.repositoryformatversion" {
        return git_int(value).is_none_or(|n| n > 1);
    }
    if name == b"fetch.negotiationalgorithm" {
        return value.is_none_or(|v| {
            !NEGOTIATION_ALGORITHMS.contains(&cut(v).to_ascii_lowercase().as_slice())
        });
    }
    if name == b"commitgraph.generationversion" || name == b"commitgraph.changedpathsversion" {
        return git_int(value).is_none();
    }
    if name == b"include.path" || (name.starts_with(b"includeif.") && name.ends_with(b".path")) {
        return true;
    }
    if among(name, &READ_ANY) {
        return false;
    }
    among(name, &READ_ELSEWHERE) || READ_WHOLE.iter().any(|p| name.starts_with(p))
}

/// `_merge_word_dies(value, _MERGE_WORDS_SINCE)`.
pub fn merge_word_dies(value: Option<&[u8]>, git: &GitFacts) -> bool {
    if let Some(v) = value {
        let word = cut(v);
        if let Some(&(_, since)) = MERGE_WORDS_SINCE.iter().find(|(w, _)| *w == word) {
            return since != (0, 0) && git.is_at_least(since) != Some(true);
        }
        let lower = word.to_ascii_lowercase();
        if let Some(&(_, since)) = MERGE_WORDS_SINCE.iter().find(|(w, _)| *w == lower) {
            if since == (0, 0) {
                return git.is_at_least(MERGE_EXACT_SINCE) != Some(false);
            }
        }
    }
    git_bool(value).is_none()
}

/// The machine's config could not be judged here: a native state the
/// reference never has (a read that stopped at a condition not ported,
/// or one that had to ask the branch, or `machine_config_reads`'
/// `NotPortedWhy` other than `Raised`). A caller declines.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NotPorted;

/// `_machine_config_dies(reads, git_dir)` over the reads' answer
/// (`gitconfig::stamped::machine_config_reads`). `Err(Raised)`, where the
/// reference's read raises ValueError, is its `except` and answers true.
pub fn machine_config_dies(
    found: Result<&[ConfigValues], &NotPortedWhy>,
    git: &GitFacts,
) -> Result<bool, NotPorted> {
    let found = match found {
        Ok(found) => found,
        Err(NotPortedWhy::Raised) => return Ok(true),
        Err(_) => return Err(NotPorted),
    };
    for read in found {
        if read.raised.is_some() {
            return Ok(true);
        }
        if read.not_ported.is_some() || read.asks_branch {
            return Err(NotPorted);
        }
        if read.dies.is_some() || read.maybe_dies.is_some() || !read.complete {
            return Ok(true);
        }
        for values in [&read.values, &read.undecided] {
            for (name, held) in values {
                if name.section == "include" || name.section == "includeif" {
                    continue;
                }
                let mut full = name.section.as_bytes().to_vec();
                if let Some(sub) = &name.subsection {
                    full.push(b'.');
                    full.extend_from_slice(sub);
                }
                full.push(b'.');
                full.extend_from_slice(name.key.as_bytes());
                if held
                    .iter()
                    .any(|v| machine_value_dies(&full, v.as_deref(), git))
                {
                    return Ok(true);
                }
            }
        }
    }
    Ok(false)
}

/// `_machine_value_dies(name, value)`.
pub fn machine_value_dies(name: &[u8], value: Option<&[u8]>, git: &GitFacts) -> bool {
    if name == b"core.bare" {
        return git_bool(value).is_none();
    }
    rev_parse_dies(name, value, git)
}

/// `_config_entries(data)`: None for a text holding a NUL byte or one git
/// refuses.
pub fn config_entries(data: &[u8]) -> Option<Vec<Entry>> {
    if data.contains(&0) {
        return None;
    }
    git_entries(data).ok()
}

/// `_config_variable_names(data)`.
pub fn config_variable_names(data: &[u8]) -> Option<BTreeSet<Vec<u8>>> {
    config_entries(data).map(|e| e.iter().map(Entry::name).collect())
}

/// What `held` records of a path: `(st_mtime_ns, st_ctime_ns)`.
pub type Held = BTreeMap<Vec<u8>, (i128, i128)>;

/// Why [`hold_directory`] gave no answer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum HoldFault {
    /// The exception `os.stat` raised and the reference lets out
    /// (`OSError` of the class, or `ValueError` for a NUL).
    Raised(OsError),
    /// The file system gives no change time (`StdFs` on Windows): the
    /// reference's pair cannot be had.
    NotPorted,
}

/// `_hold_directory(path, held)`: `stat` follows links; a missing path
/// holds its nearest existing ancestor.
pub fn hold_directory<F: Fs + ?Sized>(
    fs: &F,
    flavour: Flavour,
    path: &[u8],
    held: &mut Held,
) -> Result<(), HoldFault> {
    let mut current = path.to_vec();
    while !held.contains_key(&current) {
        match fs.stat(&current) {
            Ok(status) => {
                let ctime = status.ctime_ns.ok_or(HoldFault::NotPorted)?;
                held.insert(current.clone(), (status.mtime_ns, ctime));
            }
            Err(error) if matches!(error.class, PyErr::FileNotFound | PyErr::NotADirectory) => {
                let parent = dirname(flavour, &current);
                if parent == current || !path_missing(Raised::from(error)) {
                    return Err(HoldFault::Raised(error));
                }
                current = parent;
            }
            Err(error) => return Err(HoldFault::Raised(error)),
        }
    }
    Ok(())
}

/// `directories_held(held)`: false where any stamp moved or a stat fails.
pub fn directories_held<F: Fs + ?Sized>(fs: &F, held: &Held) -> bool {
    held.iter().all(|(path, &(mtime, ctime))| {
        fs.stat(path)
            .is_ok_and(|s| s.mtime_ns == mtime && s.ctime_ns == Some(ctime))
    })
}

/// `_global_attributes_path(root)`: `xdg_config_home` and `home` are the
/// environment's XDG_CONFIG_HOME and HOME (None where unset).
pub fn global_attributes_path(
    flavour: Flavour,
    xdg_config_home: Option<&[u8]>,
    home: Option<&[u8]>,
    root: &[u8],
) -> Option<Vec<u8>> {
    let candidate = match xdg_config_home {
        Some(xdg) if !xdg.is_empty() => [xdg, b"/git/attributes"].concat(),
        _ => [home?, b"/.config/git/attributes"].concat(),
    };
    if isabs(flavour, &candidate) {
        return Some(candidate);
    }
    Some(join(flavour, root, &candidate))
}
