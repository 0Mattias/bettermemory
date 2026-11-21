//! `location_git.RepositoryHistory` and its git fallback `_git`
//! (`location_git.py` 1563-1817): a repository's objects read through an
//! object reader made on first use, with `git cat-file` asked only where
//! the reader reports itself incomplete, and `git merge-base
//! --independent` for the witnesses no other witness reaches.
//!
//! Effects are arguments: the object reader is the [`Objects`] trait,
//! made by a factory closure on first use, and git is the [`GitRun`]
//! trait (`_git.run_git` and `_git.log_nonzero_exit`). A reader's error
//! (`Objects::Raised`) propagates as the reference's exception does.

use std::cell::RefCell;
use std::collections::{BTreeSet, HashMap, HashSet};

use crate::githead::full_hash;
use crate::gitobjects::{
    commit_fingerprint, committed_at, parent_lines, Commit, TreeEntry, MIN_ABBREV,
};
use crate::location_git::{self, Reach, EDGE_LIMIT, PROJECT_FILE, WALK_LIMIT};
use crate::unicode;

/// Blob ids sampled per witness commit (`BLOBS_PER_WITNESS`).
pub const BLOBS_PER_WITNESS: usize = 8;
/// Their floor in bytes (`BLOB_MIN_SIZE`).
pub const BLOB_MIN_SIZE: u64 = 64;
/// Tree entries looked at for the blob sample (`BLOB_LOOK`).
pub const BLOB_LOOK: usize = 64;
/// How long `independent`'s git may take (`INDEPENDENT_SECONDS`).
pub const INDEPENDENT_SECONDS: f64 = 1.0;
/// The most commits `independent` hands git (`INDEPENDENT_MAX`).
pub const INDEPENDENT_MAX: usize = 256;
/// `_INDEPENDENT_ARGS`.
pub const INDEPENDENT_ARGS: [&str; 2] = ["merge-base", "--independent"];
/// `_UNGRAFTED`'s key; its value is the host's null device.
pub const UNGRAFTED_KEY: &str = "GIT_GRAFT_FILE";
/// `os.devnull` of the host.
pub const NULL_DEVICE: &str = if cfg!(windows) { "nul" } else { "/dev/null" };
/// `_GIT_TIMEOUT_SECONDS`.
pub const GIT_TIMEOUT_SECONDS: f64 = 20.0;

const S_IFMT: u32 = 0o170000;
const S_IFDIR: u32 = 0o040000;
const S_IFREG: u32 = 0o100000;

/// The object reader `RepositoryHistory` reads through
/// (`gitobjects.ObjectReader`'s methods it calls).
pub trait Objects {
    /// What a read raises.
    type Raised;
    /// `complete`.
    fn complete(&mut self) -> Result<bool, Self::Raised>;
    /// `name_length`: the object format's hex length, None when unknown.
    fn name_length(&mut self) -> Result<Option<usize>, Self::Raised>;
    /// `has_many(shas)`: the ids held.
    fn has_many(&mut self, shas: &[String]) -> Result<HashSet<String>, Self::Raised>;
    /// `resolve(prefix)`: the one full id an abbreviation names.
    fn resolve(&mut self, prefix: &str) -> Result<Option<String>, Self::Raised>;
    /// `read(sha)`: (type, body).
    fn read(&mut self, sha: &str) -> Result<Option<(String, Vec<u8>)>, Self::Raised>;
    /// `commit(sha)`.
    fn commit(&mut self, sha: &str) -> Result<Option<Commit>, Self::Raised>;
    /// `tree(sha)`.
    fn tree(&mut self, sha: &str) -> Result<Option<Vec<TreeEntry>>, Self::Raised>;
    /// `header(sha)`: (type, size).
    fn header(&mut self, sha: &str) -> Result<Option<(String, u64)>, Self::Raised>;
}

/// A completed git process: the exit code and the raw output (the
/// runner's `text=True` decode is done by the caller here).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Done {
    pub returncode: i64,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

/// `_git.run_git` and `_git.log_nonzero_exit`.
pub trait GitRun {
    /// `run_git(args, git_dir=, stdin=, timeout=, env=, text=)`: None
    /// when git could not run.
    fn run(
        &mut self,
        args: &[String],
        git_dir: &str,
        stdin: Option<&[u8]>,
        timeout: f64,
        env: &[(String, String)],
        text: bool,
    ) -> Option<Done>;
    /// `log_nonzero_exit(args, done, where)`.
    fn log_nonzero_exit(&mut self, args: &[String], done: &Done, at: &str);
}

/// `_git(common_dir, args, stdin)`: stdout, None where git cannot run or
/// exits non-zero.
pub fn git(
    run: &mut dyn GitRun,
    common_dir: &str,
    args: &[String],
    stdin: Option<&[u8]>,
) -> Option<Vec<u8>> {
    let done = run.run(args, common_dir, stdin, GIT_TIMEOUT_SECONDS, &[], false)?;
    if done.returncode != 0 {
        run.log_nonzero_exit(args, &done, common_dir);
        return None;
    }
    Some(done.stdout)
}

/// `str.splitlines()` (no ends kept).
pub fn splitlines(text: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut it = text.char_indices().peekable();
    while let Some((i, c)) = it.next() {
        let brk = matches!(
            c,
            '\n' | '\r'
                | '\x0b'
                | '\x0c'
                | '\x1c'
                | '\x1d'
                | '\x1e'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        );
        if brk {
            out.push(&text[start..i]);
            let mut end = i + c.len_utf8();
            if c == '\r' {
                if let Some(&(_, '\n')) = it.peek() {
                    it.next();
                    end += 1;
                }
            }
            start = end;
        }
    }
    if start < text.len() {
        out.push(&text[start..]);
    }
    out
}

fn space(c: char) -> bool {
    unicode::is_space(c as u32)
}

/// `str.split()`.
pub fn split(text: &str) -> Vec<&str> {
    text.split(space).filter(|s| !s.is_empty()).collect()
}

fn lower(s: &str) -> String {
    unicode::to_string(&unicode::lower(&unicode::cps(s))).expect("text lowers to text")
}

fn chars(s: &str) -> usize {
    s.chars().count()
}

/// `_askable(given)`.
pub fn askable(given: &str) -> bool {
    (MIN_ABBREV..=64).contains(&chars(given)) && given.bytes().all(|b| b.is_ascii_hexdigit())
}

/// `_FULL_HASH.fullmatch(c.lower())`.
fn full(c: &str) -> Option<String> {
    let low = lower(c);
    full_hash(low.as_bytes()).then_some(low)
}

/// One entry of `_expand`: (id, (full id, type where git answered)).
pub type Expanded = (String, (String, Option<String>));

/// A dict in insertion order, as the reference's `found`: a key popped
/// and set again goes to the end.
#[derive(Default)]
struct Found {
    slots: Vec<Option<Expanded>>,
    at: HashMap<String, usize>,
}

impl Found {
    fn put(&mut self, key: &str, value: (String, Option<String>)) {
        match self.at.get(key) {
            Some(&i) => self.slots[i] = Some((key.to_owned(), value)),
            None => {
                self.at.insert(key.to_owned(), self.slots.len());
                self.slots.push(Some((key.to_owned(), value)));
            }
        }
    }
    fn has(&self, key: &str) -> bool {
        self.at.contains_key(key)
    }
    fn pop(&mut self, key: &str) {
        if let Some(i) = self.at.remove(key) {
            self.slots[i] = None;
        }
    }
    fn items(self) -> Vec<Expanded> {
        self.slots.into_iter().flatten().collect()
    }
}

struct State<O, F, G> {
    make: F,
    reader: Option<O>,
    run: G,
    parents: HashMap<String, Vec<String>>,
    committed: HashMap<String, u64>,
}

/// `RepositoryHistory(common_dir)`.
pub struct RepositoryHistory<O, F, G> {
    common: String,
    s: RefCell<State<O, F, G>>,
}

impl<O, F, G> State<O, F, G>
where
    O: Objects,
    F: FnMut(&str) -> Result<O, O::Raised>,
    G: GitRun,
{
    fn reader(&mut self, common: &str) -> Result<&mut O, O::Raised> {
        if self.reader.is_none() {
            self.reader = Some((self.make)(common)?);
        }
        Ok(self.reader.as_mut().expect("made above"))
    }

    fn batch_check(
        &mut self,
        common: &str,
        ids: &[String],
    ) -> Option<HashMap<String, (String, String)>> {
        let mut input = ids.join("\n");
        input.push('\n');
        let args = vec!["cat-file".to_owned(), "--batch-check".to_owned()];
        let out = git(&mut self.run, common, &args, Some(input.as_bytes()))?;
        let text = String::from_utf8_lossy(&out);
        let lines = splitlines(&text);
        let paired: Vec<(&String, &str)> = if lines.len() == ids.len() {
            ids.iter().zip(lines.iter().copied()).collect()
        } else {
            let mut v = Vec::new();
            for ln in &lines {
                let first = split(ln);
                for i in ids {
                    if first.first().is_some_and(|f| *f == lower(i)) {
                        v.push((i, *ln));
                    }
                }
            }
            v
        };
        let mut found = HashMap::new();
        for (given, line) in paired {
            let parts = split(line);
            if parts.len() >= 2 && parts[1] != "missing" && parts[1] != "ambiguous" {
                found.insert(given.clone(), (lower(parts[0]), parts[1].to_owned()));
            }
        }
        Some(found)
    }

    fn expand(&mut self, common: &str, ids: &[String]) -> Result<Found, O::Raised> {
        let reader = self.reader(common)?;
        let length = reader.name_length()?;
        let is_whole = |i: &String| length == Some(chars(i));
        let whole: Vec<String> = ids.iter().filter(|i| is_whole(i)).cloned().collect();
        let short: Vec<String> = ids.iter().filter(|i| !is_whole(i)).cloned().collect();
        let mut found = Found::default();
        let lowered: Vec<String> = whole.iter().map(|i| lower(i)).collect();
        let held = reader.has_many(&lowered)?;
        for given in &whole {
            if held.contains(&lower(given)) {
                found.put(given, (lower(given), None));
            }
        }
        for given in &short {
            if let Some(f) = reader.resolve(given)? {
                found.put(given, (f, None));
            }
        }
        if reader.complete()? {
            return Ok(found);
        }
        let in_short: HashSet<&str> = short.iter().map(String::as_str).collect();
        let mut seen = HashSet::new();
        let ask: Vec<String> = whole
            .iter()
            .chain(short.iter())
            .filter(|i| !found.has(i) || in_short.contains(i.as_str()))
            .filter(|i| seen.insert((*i).clone()))
            .filter(|i| askable(i))
            .cloned()
            .collect();
        let told = if ask.is_empty() {
            None
        } else {
            self.batch_check(common, &ask)
        };
        let Some(told) = told else {
            return Ok(found);
        };
        for given in &short {
            match told.get(given) {
                Some((f, k)) => found.put(given, (f.clone(), Some(k.clone()))),
                None => found.pop(given),
            }
        }
        for given in &whole {
            if !found.has(given) {
                if let Some((f, k)) = told.get(given) {
                    found.put(given, (f.clone(), Some(k.clone())));
                }
            }
        }
        Ok(found)
    }

    fn commit_body(&mut self, common: &str, sha: &str) -> Result<Option<Vec<u8>>, O::Raised> {
        let reader = self.reader(common)?;
        if let Some((kind, body)) = reader.read(sha)? {
            return Ok((kind == "commit").then_some(body));
        }
        if reader.complete()? {
            return Ok(None);
        }
        let args = vec!["cat-file".to_owned(), "commit".to_owned(), sha.to_owned()];
        Ok(git(&mut self.run, common, &args, None))
    }

    fn parents(&mut self, common: &str, commit: &str) -> Result<Vec<String>, O::Raised> {
        let sha = lower(commit);
        if let Some(hit) = self.parents.get(&sha) {
            return Ok(hit.clone());
        }
        let body = self.commit_body(common, &sha)?;
        let mut found = Vec::new();
        let mut when = 0;
        if let Some(body) = body {
            let hexsz = if chars(&sha) == 64 { 64 } else { 40 };
            found = parent_lines(&body, hexsz).map(|h| h.0).unwrap_or_default();
            when = committed_at(&body, hexsz);
        }
        self.parents.insert(sha.clone(), found.clone());
        self.committed.insert(sha, when);
        Ok(found)
    }

    fn committed(&mut self, common: &str, commit: &str) -> Result<u64, O::Raised> {
        let sha = lower(commit);
        if !self.committed.contains_key(&sha) {
            self.parents(common, &sha)?;
        }
        Ok(self.committed[&sha])
    }
}

impl<O, F, G> RepositoryHistory<O, F, G>
where
    O: Objects,
    F: FnMut(&str) -> Result<O, O::Raised>,
    G: GitRun,
{
    /// `RepositoryHistory(common_dir)`, the reader made by `make` on
    /// first use and git run through `run`.
    pub fn new(common_dir: &str, make: F, run: G) -> Self {
        RepositoryHistory {
            common: common_dir.to_owned(),
            s: RefCell::new(State {
                make,
                reader: None,
                run,
                parents: HashMap::new(),
                committed: HashMap::new(),
            }),
        }
    }

    /// The git runner, for a caller to read back.
    pub fn with_run<T>(&self, f: impl FnOnce(&mut G) -> T) -> T {
        f(&mut self.s.borrow_mut().run)
    }

    /// Whether the reader has been made.
    pub fn reader_made(&self) -> bool {
        self.s.borrow().reader.is_some()
    }

    /// `_expand(ids)`: (id, (full id, type where git answered)) in the
    /// reference's dict order.
    pub fn expand_typed(&self, ids: &[String]) -> Result<Vec<Expanded>, O::Raised> {
        Ok(self.s.borrow_mut().expand(&self.common, ids)?.items())
    }

    /// `expand(ids)`, in dict order.
    pub fn expand(&self, ids: &[String]) -> Result<Vec<(String, String)>, O::Raised> {
        Ok(self
            .expand_typed(ids)?
            .into_iter()
            .map(|(g, (f, _))| (g, f))
            .collect())
    }

    /// `holds(commits)`.
    pub fn holds(&self, commits: &[String]) -> Result<BTreeSet<String>, O::Raised> {
        Ok(self
            .expand_typed(commits)?
            .into_iter()
            .filter(|(_, (_, k))| k.as_deref().is_none_or(|k| k == "commit"))
            .map(|(g, _)| lower(&g))
            .collect())
    }

    /// `holds_objects(ids)`.
    pub fn holds_objects(&self, ids: &[String]) -> Result<BTreeSet<String>, O::Raised> {
        Ok(self
            .expand_typed(ids)?
            .into_iter()
            .map(|(g, _)| lower(&g))
            .collect())
    }

    /// `fingerprint(commit)`.
    pub fn fingerprint(&self, commit: &str) -> Result<Option<String>, O::Raised> {
        let body = self
            .s
            .borrow_mut()
            .commit_body(&self.common, &lower(commit))?;
        Ok(body.and_then(|b| commit_fingerprint(&b)))
    }

    /// `parents(commit)`.
    pub fn parents(&self, commit: &str) -> Result<Vec<String>, O::Raised> {
        self.s.borrow_mut().parents(&self.common, commit)
    }

    /// `committed(commit)`.
    pub fn committed(&self, commit: &str) -> Result<u64, O::Raised> {
        self.s.borrow_mut().committed(&self.common, commit)
    }

    /// `reachable_among(commits)`: `location_git::reachable_among` over
    /// this history. A reader's error ends the reads (each later one
    /// answers nothing) and is raised once the traversal returns.
    pub fn reachable_among(&self, commits: &[String]) -> Result<Reach, O::Raised> {
        let failed: RefCell<Option<O::Raised>> = RefCell::new(None);
        let mut parents = |c: &str| -> Vec<String> {
            if failed.borrow().is_some() {
                return Vec::new();
            }
            self.parents(c).unwrap_or_else(|e| {
                *failed.borrow_mut() = Some(e);
                Vec::new()
            })
        };
        let mut committed = |c: &str| -> i128 {
            if failed.borrow().is_some() {
                return 0;
            }
            match self.committed(c) {
                Ok(t) => i128::from(t),
                Err(e) => {
                    *failed.borrow_mut() = Some(e);
                    0
                }
            }
        };
        let reach = location_git::reachable_among(
            &mut parents,
            commits,
            WALK_LIMIT,
            EDGE_LIMIT,
            Some(&mut committed),
        );
        match failed.into_inner() {
            Some(e) => Err(e),
            None => Ok(reach),
        }
    }

    /// `independent(commits)`: sorted, None where git does not decide.
    pub fn independent(&self, commits: &[String]) -> Option<Vec<String>> {
        let given: Vec<String> = commits
            .iter()
            .filter_map(|c| full(c))
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        if given.len() < 2 {
            return Some(given);
        }
        if given.len() > INDEPENDENT_MAX {
            return None;
        }
        let mut args: Vec<String> = ["--no-replace-objects", "-c", "core.commitGraph=false"]
            .iter()
            .chain(INDEPENDENT_ARGS.iter())
            .map(|s| (*s).to_owned())
            .collect();
        args.extend(given.iter().cloned());
        let env = [(UNGRAFTED_KEY.to_owned(), NULL_DEVICE.to_owned())];
        let mut s = self.s.borrow_mut();
        let done = s
            .run
            .run(&args, &self.common, None, INDEPENDENT_SECONDS, &env, true)?;
        if done.returncode != 0 {
            let short: Vec<String> = INDEPENDENT_ARGS.iter().map(|s| (*s).to_owned()).collect();
            s.run.log_nonzero_exit(&short, &done, &self.common);
            return None;
        }
        let text = String::from_utf8_lossy(&done.stdout);
        let top: BTreeSet<String> = splitlines(&text)
            .into_iter()
            .map(|l| lower(l.trim_matches(space)))
            .filter(|l| !l.is_empty())
            .collect();
        let held: HashSet<&String> = given.iter().collect();
        (!top.is_empty() && top.iter().all(|t| held.contains(t))).then(|| top.into_iter().collect())
    }

    /// `blobs(commit)`.
    pub fn blobs(&self, commit: &str) -> Result<Vec<String>, O::Raised> {
        let mut s = self.s.borrow_mut();
        let reader = s.reader(&self.common)?;
        let Some(found) = reader.commit(&lower(commit))? else {
            return Ok(Vec::new());
        };
        let mut out = Vec::new();
        let mut looked = 0;
        let mut todo = std::collections::VecDeque::from([(found.tree, 0)]);
        while looked < BLOB_LOOK {
            let Some((tree, depth)) = todo.pop_front() else {
                break;
            };
            let Some(entries) = reader.tree(&tree)? else {
                continue;
            };
            for entry in entries {
                if looked >= BLOB_LOOK {
                    break;
                }
                if entry.mode & S_IFMT == S_IFDIR {
                    if depth == 0 {
                        todo.push_back((entry.sha, 1));
                    }
                    continue;
                }
                if entry.name == PROJECT_FILE || entry.mode & S_IFMT != S_IFREG {
                    continue;
                }
                looked += 1;
                if let Some((_, size)) = reader.header(&entry.sha)? {
                    if size >= BLOB_MIN_SIZE {
                        out.push(entry.sha);
                    }
                }
            }
        }
        out.sort();
        out.truncate(BLOBS_PER_WITNESS);
        Ok(out)
    }
}
