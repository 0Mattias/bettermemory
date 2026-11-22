//! The uncached capture (`location.py` 463-691): the project-file walk
//! with its cache of the part above a repository (`_walk_files`,
//! `_ABOVE`), the tracked-file read (`_file_tracked`), the token fields
//! (`_token_fields`), the indeterminate capture (`_indeterminate`) and
//! `_capture_uncached` itself.
//!
//! Effects are arguments: the project-file walk is [`ProjectFiles`], the
//! object reads are [`ObjectReads`], and every other call the reference
//! makes (the path's state, `realpath`, discovery, the stats, the token's
//! read and write, the head, the remotes, the stamps) is a method of
//! [`Steps`], answered in the reference's own vocabulary. The steps are
//! called in the reference's order, so a step's side effect (the token's
//! write) happens where the reference's does.

use std::sync::Mutex;

use crate::capture::{Capture, FileSeen, Key, Kind, Manifest, Stamp, State};
use crate::location_git::PROJECT_FILE;
use crate::pyvalue::PyStr;

/// `CACHE_SECONDS`: the life of a cached walk above a root.
pub const CACHE_SECONDS: f64 = 600.0;
/// `CACHE_CAP`: the most walks the cache keeps.
pub const CACHE_CAP: usize = 256;

fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

fn cat(parts: &[&[u32]]) -> PyStr {
    parts.concat()
}

/// A step that cannot answer as the reference does (a branch not ported,
/// or an exception the reference lets rise).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Declined(pub String);

/// `project_file.ProjectFile`, the fields a capture reads.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProjectFile {
    pub id: PyStr,
    pub name: PyStr,
    pub directory: PyStr,
    pub path: PyStr,
    pub boundary: bool,
}

/// `project_file.Walk`, as a capture reads it: the well-formed files and
/// the malformed ones' lines (nearest first), the part above the root,
/// the folder's file (no repository) and the root's file.
#[derive(Clone, Debug, PartialEq)]
pub struct Walk<A> {
    pub files: Vec<ProjectFile>,
    pub errors: Vec<PyStr>,
    pub above: A,
    pub folder: Option<ProjectFile>,
    pub root: Option<ProjectFile>,
}

/// `project_file.find` and `project_file.above_start` (project_file.py
/// 140-233 and the walk).
pub trait ProjectFiles {
    /// `project_file.Above`.
    type Above: Clone;
    /// `str(above_start(cwd, root))`, None where nothing lies above.
    fn above_start(&mut self, cwd: &[u32], root: &[u32]) -> Option<PyStr>;
    /// `find(cwd, root, above=above)`.
    fn find(
        &mut self,
        cwd: &[u32],
        root: Option<&[u32]>,
        above: Option<&Self::Above>,
    ) -> Walk<Self::Above>;
}

/// `_fresh`: younger than [`CACHE_SECONDS`] on both clocks, never from
/// the future.
pub fn fresh(monotonic: f64, wall: f64, now: f64, now_wall: f64) -> bool {
    let young = |d: f64| (0.0..CACHE_SECONDS).contains(&d);
    young(now - monotonic) && young(now_wall - wall)
}

/// `_ABOVE`: the walks above a root by their start, in insertion order
/// (a key set again keeps its place, as a dict's does), the oldest
/// evicted past the cap. The process keeps one.
pub struct AboveCache<A> {
    cap: usize,
    entries: Mutex<Vec<(PyStr, f64, f64, A)>>,
}

impl<A: Clone> AboveCache<A> {
    pub fn new(cap: usize) -> AboveCache<A> {
        AboveCache {
            cap,
            entries: Mutex::new(Vec::new()),
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Vec<(PyStr, f64, f64, A)>> {
        self.entries.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn get(&self, key: &[u32]) -> Option<(f64, f64, A)> {
        let held = self.lock();
        let hit = held.iter().find(|e| e.0 == key)?;
        Some((hit.1, hit.2, hit.3.clone()))
    }

    fn put(&self, key: PyStr, now: f64, wall: f64, above: A) {
        let mut held = self.lock();
        match held.iter_mut().find(|e| e.0 == key) {
            Some(e) => (e.1, e.2, e.3) = (now, wall, above),
            None => held.push((key, now, wall, above)),
        }
        while held.len() > self.cap {
            held.remove(0);
        }
    }

    /// The keys held, oldest first.
    pub fn keys(&self) -> Vec<PyStr> {
        self.lock().iter().map(|e| e.0.clone()).collect()
    }

    pub fn clear(&self) {
        self.lock().clear();
    }
}

/// `object_key(directory)` as `_walk_files` calls it.
pub type KeyOf<'a> = dyn FnMut(&[u32]) -> Result<Option<Key>, Declined> + 'a;
/// `_walk_files`'s answer: the walk, its files, the malformed lines.
pub type Walked<A> = (Walk<A>, Vec<FileSeen>, Vec<PyStr>);

/// `_walk_files(cwd, root)`: the walk, its files as `FileSeen` (each
/// directory keyed through `key`), and the malformed files' lines.
/// `clock` is `(time.monotonic(), time.time())`, read after the start.
pub fn walk_files<P: ProjectFiles + ?Sized>(
    files: &mut P,
    cache: &AboveCache<P::Above>,
    clock: &mut dyn FnMut() -> (f64, f64),
    key: &mut KeyOf<'_>,
    cwd: &[u32],
    root: Option<&[u32]>,
) -> Result<Walked<P::Above>, Declined> {
    let start = root.and_then(|r| files.above_start(cwd, r));
    let (now, wall) = clock();
    let mut above = None;
    if let Some(start) = &start {
        if let Some((m, w, a)) = cache.get(start) {
            if fresh(m, w, now, wall) {
                above = Some(a);
            }
        }
    }
    let walk = files.find(cwd, root, above.as_ref());
    if let (Some(start), None) = (start, &above) {
        cache.put(start, now, wall, walk.above.clone());
    }
    let mut seen = Vec::new();
    for f in &walk.files {
        seen.push(FileSeen {
            id: f.id.clone(),
            name: f.name.clone(),
            dir: f.directory.clone(),
            path: f.path.clone(),
            boundary: f.boundary,
            key: key(&f.directory)?,
        });
    }
    let errors = walk.errors.clone();
    Ok((walk, seen, errors))
}

/// The object reads of `gitobjects.ObjectReader(common_dir)`.
pub trait ObjectReads {
    /// `reader.commit(sha)`'s tree, None where the objects do not say.
    fn commit(&mut self, common_dir: &[u32], sha: &[u32]) -> Option<PyStr>;
    /// `reader.tree(sha)`'s entries as (name, mode), None likewise.
    fn tree(&mut self, common_dir: &[u32], sha: &[u32]) -> Option<Vec<(Vec<u8>, u32)>>;
}

/// The width of the C `mode_t` that `stat.S_ISDIR` converts its argument
/// to: 16 bits on macOS and Windows, 32 on Linux and the rest.
pub const MODE_T_BITS: u32 = if cfg!(any(target_os = "macos", windows)) {
    16
} else {
    32
};

/// `stat.S_ISDIR(mode)` with a `mode_t` of `bits`: OverflowError ("mode
/// out of range") for a mode wider than that.
pub fn s_isdir(mode: u32, bits: u32) -> Result<bool, Declined> {
    if bits < 32 && mode >> bits != 0 {
        return Err(Declined("OverflowError: mode out of range".into()));
    }
    Ok(mode & 0o170000 == 0o040000)
}

/// `_file_tracked(common_dir, head)`: whether HEAD's tree holds the
/// project file at its root as anything but a directory; None where the
/// objects do not say. `bits` is [`MODE_T_BITS`] on the host.
pub fn file_tracked(
    reads: &mut dyn ObjectReads,
    bits: u32,
    common_dir: &[u32],
    head: &[u32],
) -> Result<Option<bool>, Declined> {
    let Some(tree) = reads.commit(common_dir, head) else {
        return Ok(None);
    };
    let Some(entries) = reads.tree(common_dir, &tree) else {
        return Ok(None);
    };
    for (name, mode) in entries {
        if name == PROJECT_FILE && !s_isdir(mode, bits)? {
            return Ok(Some(true));
        }
    }
    Ok(Some(false))
}

/// `checkout_token.read`'s answer: the state's name and the token.
pub type TokenRead = (String, Option<PyStr>);
/// `checkout_token.write`'s answer: the status's name and the token.
pub type TokenWrite = (String, Option<PyStr>);

/// `_token_fields(git, write_token=...)`: (token found, token written,
/// status). `write` is `checkout_token.write(git_dir, policy=True,
/// bare=...)`, called only where the token is absent and the policy
/// allows.
pub fn token_fields(
    found: TokenRead,
    write_token: bool,
    write: &mut dyn FnMut() -> Result<TokenWrite, Declined>,
) -> Result<(Option<PyStr>, Option<PyStr>, PyStr), Declined> {
    match found.0.as_str() {
        "present" => return Ok((found.1, None, cps("present"))),
        "absent" => {}
        state => return Ok((None, None, cps(&format!("failed:{state}")))),
    }
    if !write_token {
        return Ok((None, None, cps("skipped:policy")));
    }
    let (status, token) = write()?;
    Ok(match status.as_str() {
        "written" => (None, token, cps("written")),
        "exists" => (token, None, cps("present")),
        "malformed" => (None, None, cps("failed:malformed")),
        _ => (None, None, cps(&status)),
    })
}

/// `_indeterminate(cwd, detail, key)`.
pub fn indeterminate(cwd: PyStr, detail: PyStr, key: Option<Key>, host: Option<PyStr>) -> Capture {
    let mut c = Capture::new(cwd, State::Indeterminate);
    c.detail = detail;
    c.cwd_key = key;
    c.hostname = host;
    c
}

/// `_Git`, its paths as the reference's strings.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GitPaths {
    pub dotgit: Option<PyStr>,
    pub git_dir: PyStr,
    pub common_dir: PyStr,
    pub root: Option<PyStr>,
    pub kind: Kind,
}

/// What `_discover` raises, by the clause that catches it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Discovery {
    Found(Option<GitPaths>),
    /// OSError, its `str`.
    Unreadable(PyStr),
    /// `DiscoveryDies`.
    Dies(PyStr),
    /// `GitDies`.
    Format(PyStr),
}

/// `_path_state(cwd)`: the state's name, the stat, the detail.
pub type PathState<S> = (String, Option<S>, PyStr);

/// `read_remotes(...)`'s fields a capture keeps.
#[derive(Clone, Debug, PartialEq, Eq, Default)]
pub struct Remotes {
    pub keys: Vec<PyStr>,
    pub aliases: Vec<PyStr>,
    pub sources: Vec<PyStr>,
    pub declined: Option<PyStr>,
}

/// The calls `_capture_uncached` makes, each answering as the reference's
/// does. `Stat` is an `os.stat_result`.
pub trait Steps {
    type Stat: Clone;
    fn path_state(&mut self, cwd: &[u32]) -> Result<PathState<Self::Stat>, Declined>;
    fn view_path(&mut self, cwd: &[u32]) -> Result<PyStr, Declined>;
    /// `os.path.realpath(cwd)`; Err(Ok(text)) for an OSError or a
    /// ValueError, its `str`.
    fn realpath(&mut self, cwd: &[u32]) -> Result<Result<PyStr, PyStr>, Declined>;
    fn object_key(
        &mut self,
        path: &[u32],
        st: Option<&Self::Stat>,
    ) -> Result<Option<Key>, Declined>;
    fn hostname(&mut self) -> Option<PyStr>;
    fn discover(&mut self, real: &[u32]) -> Result<Discovery, Declined>;
    fn kept_apart(&mut self, git: &GitPaths) -> Result<bool, Declined>;
    fn real_git(&mut self, git: &GitPaths) -> Result<GitPaths, Declined>;
    fn stat_each(&mut self, paths: [Option<&[u32]>; 3]) -> Vec<Option<Self::Stat>>;
    /// `os.stat(path)`, None for an OSError.
    fn stat(&mut self, path: &[u32]) -> Result<Option<Self::Stat>, Declined>;
    /// `os.path.join(a, b)`.
    fn join(&self, a: &[u32], b: &str) -> PyStr;
    fn evicted(&mut self, st: &Self::Stat) -> bool;
    fn fetch(&mut self, path: &[u32]) -> Result<bool, Declined>;
    fn token_path(&mut self, git_dir: &[u32]) -> PyStr;
    fn token_read(&mut self, git_dir: &[u32]) -> Result<TokenRead, Declined>;
    fn token_write(&mut self, git_dir: &[u32], bare: bool) -> Result<TokenWrite, Declined>;
    fn resolve_head(&mut self, git: &GitPaths) -> Result<(Option<PyStr>, Option<PyStr>), Declined>;
    fn ref_tips(&mut self, common_dir: &[u32]) -> Vec<PyStr>;
    /// `read_remotes(common_dir, git_dir, lambda u: remote_key(u, base))`.
    fn read_remotes(
        &mut self,
        common: &[u32],
        git_dir: &[u32],
        base: &[u32],
    ) -> Result<Remotes, Declined>;
    fn manifest(&mut self, root: &[u32]) -> Result<Option<Manifest>, Declined>;
    fn init_stamps(&mut self, git_dir: &[u32], linked: bool) -> Result<Vec<Stamp>, Declined>;
    fn capture_ticks(
        &mut self,
        own: Option<&Self::Stat>,
        own_key: Option<&Key>,
        read: &[Option<Self::Stat>],
        stamps: &[Stamp],
    ) -> Result<Option<bool>, Declined>;
    fn isfile(&mut self, path: &[u32]) -> bool;
    /// `(time.monotonic(), time.time())`.
    fn clock(&mut self) -> (f64, f64);
}

/// What `_capture_uncached` is given besides its steps.
pub struct Options {
    pub write_token: bool,
    pub manifest: bool,
    /// [`MODE_T_BITS`] on the host.
    pub mode_bits: u32,
}

fn walk_in<S: Steps, P: ProjectFiles>(
    steps: &mut S,
    files: &mut P,
    cache: &AboveCache<P::Above>,
    cwd: &[u32],
    root: Option<&[u32]>,
) -> Result<Walked<P::Above>, Declined> {
    let s = std::cell::RefCell::new(steps);
    walk_files(
        files,
        cache,
        &mut || s.borrow_mut().clock(),
        &mut |p| s.borrow_mut().object_key(p, None),
        cwd,
        root,
    )
}

/// `_capture_uncached(cwd, write_token=..., budget=..., manifest=...)`;
/// the budget is the steps' `fetch`'s.
pub fn capture_uncached<S: Steps, P: ProjectFiles>(
    steps: &mut S,
    files: &mut P,
    cache: &AboveCache<P::Above>,
    reads: &mut dyn ObjectReads,
    opts: &Options,
    cwd: &[u32],
) -> Result<Capture, Declined> {
    let (state, st, why) = steps.path_state(cwd)?;
    let st = match (state.as_str(), st) {
        ("present", Some(st)) => st,
        _ => {
            let detail = if why.is_empty() {
                cps(&format!("cwd {state}"))
            } else {
                cat(&[&cps(&format!("cwd {state}: ")), &why])
            };
            let at = steps.view_path(cwd)?;
            return Ok(indeterminate(at, detail, None, steps.hostname()));
        }
    };
    let real = match steps.realpath(cwd)? {
        Ok(real) => real,
        Err(text) => {
            let at = steps.view_path(cwd)?;
            let detail = cat(&[&cps("cwd unreadable: "), &text]);
            return Ok(indeterminate(at, detail, None, steps.hostname()));
        }
    };
    let cwd_key = steps.object_key(&real, Some(&st))?;
    let host = steps.hostname();
    let (prefix, text) = match steps.discover(&real)? {
        Discovery::Found(found) => (None, found),
        Discovery::Unreadable(t) => (Some(("unreadable: ", t)), None),
        Discovery::Dies(t) => (Some(("discovery: ", t)), None),
        Discovery::Format(t) => (Some(("repository format: ", t)), None),
    };
    if let Some((p, t)) = prefix {
        let h = steps.hostname();
        return Ok(indeterminate(real, cat(&[&cps(p), &t]), cwd_key, h));
    }
    let Some(found) = text else {
        let (walk, seen, errors) = walk_in(steps, files, cache, &real, None)?;
        let mut c = Capture::new(real, State::Outside);
        (c.files, c.file_errors, c.cwd_key, c.hostname) = (seen, errors, cwd_key, host);
        if let Some(folder) = walk.folder {
            let root = folder.directory;
            c.kind = Some(Kind::Folder);
            c.object_key = steps.object_key(&root, None)?;
            if opts.manifest {
                c.manifest = steps.manifest(&root)?;
            }
            c.root = Some(root);
        }
        return Ok(c);
    };
    let kept = steps.kept_apart(&found)?;
    let git = steps.real_git(&found)?;
    let each = steps.stat_each([
        Some(&git.common_dir),
        Some(&git.git_dir),
        git.root.as_deref(),
    ]);
    let pick = |i: usize| each.get(i).cloned().flatten();
    let (common_st, own_st, root_st) = (pick(0), pick(1), pick(2));
    let common_key = match &common_st {
        Some(s) => steps.object_key(&git.common_dir, Some(s))?,
        None => None,
    };
    let own_key = match &own_st {
        Some(s) => steps.object_key(&git.git_dir, Some(s))?,
        None => None,
    };
    let root_key = match (&git.root, &root_st) {
        (Some(r), Some(s)) if !r.is_empty() => steps.object_key(r, Some(s))?,
        _ => None,
    };
    let top = match &git.root {
        Some(r) if !r.is_empty() => r.clone(),
        _ => git.git_dir.clone(),
    };
    let mut c = Capture::new(real.clone(), State::Repo);
    c.root = Some(top.clone());
    c.kind = Some(git.kind);
    c.git_dir = Some(git.git_dir.clone());
    c.common_dir = Some(git.common_dir.clone());
    (c.object_key, c.common_object_key) = (own_key.clone(), common_key);
    (c.root_object_key, c.cwd_key) = (root_key, cwd_key.clone());
    let mut read = vec![Some(st), common_st, own_st.clone(), root_st];
    for (name, dir) in [("HEAD", &git.git_dir), ("config", &git.common_dir)] {
        let path = steps.join(dir, name);
        let Some(fst) = steps.stat(&path)? else {
            continue;
        };
        read.push(Some(fst.clone()));
        if steps.evicted(&fst) && !steps.fetch(&path)? {
            let mut status = cps("not_read:evicted");
            let tpath = steps.token_path(&git.git_dir);
            if let Some(tst) = steps.stat(&tpath)? {
                if !steps.evicted(&tst) {
                    c.token = steps.token_read(&git.git_dir)?.1;
                    if c.token.as_ref().is_some_and(|t| !t.is_empty()) {
                        status = cps("present");
                    }
                }
            }
            (c.token_status, c.evicted, c.detail, c.hostname) = (status, true, path, host);
            return Ok(c);
        }
    }
    let (head, branch) = steps.resolve_head(&git)?;
    let tips = steps.ref_tips(&git.common_dir);
    let remotes = steps.read_remotes(&git.common_dir, &git.git_dir, &top)?;
    if let Some(why) = remotes.declined {
        let h = steps.hostname();
        let detail = cat(&[&cps("remotes not settled: "), &why]);
        return Ok(indeterminate(real, detail, cwd_key, h));
    }
    let found = steps.token_read(&git.git_dir)?;
    let bare = git.kind == Kind::Bare;
    let gd = git.git_dir.clone();
    let (token, written, status) = token_fields(found, opts.write_token, &mut || {
        steps.token_write(&gd, bare)
    })?;
    let (walk, seen, errors) = walk_in(steps, files, cache, &real, Some(&top))?;
    if walk.root.is_some() {
        if let Some(h) = &head {
            c.file_tracked = file_tracked(reads, opts.mode_bits, &git.common_dir, h)?;
        }
    }
    let root_set = git.root.as_ref().filter(|r| !r.is_empty());
    let keep = opts.manifest && head.is_none() && tips.is_empty() && git.root.is_some();
    let stamps = steps.init_stamps(&git.git_dir, git.kind == Kind::Linked)?;
    let shallow = steps.join(&git.common_dir, "shallow");
    c.shallow = steps.isfile(&shallow);
    c.ticks = steps.capture_ticks(own_st.as_ref(), own_key.as_ref(), &read, &stamps)?;
    (c.head, c.branch, c.ref_tips) = (head, branch, tips);
    (c.remote_keys, c.alias_keys) = (remotes.keys, remotes.aliases);
    c.config_sources = remotes.sources;
    (c.token, c.token_written, c.token_status) = (token, written, status);
    (c.stamps, c.files, c.file_errors) = (stamps, seen, errors);
    if keep {
        if let Some(r) = root_set {
            c.manifest = steps.manifest(r)?;
        }
    }
    (c.hostname, c.kept_apart) = (host, kept);
    Ok(c)
}
