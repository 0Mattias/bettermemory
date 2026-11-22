//! The scripted world of `tests/contract/gen_location_capture.py`: each
//! call `_capture_uncached` makes outside the unit answered from a JSON
//! spec and noted in a log as the generator notes it.

#![allow(dead_code)]

use std::cell::RefCell;

use bettermemory_core::capture::{Capture, Key, Kind, Manifest, Stamp};
use bettermemory_core::location::capture::{
    capture_uncached, AboveCache, Declined, Discovery, GitPaths, ObjectReads, Options, PathState,
    ProjectFile, ProjectFiles, Remotes, Steps, TokenRead, TokenWrite, Walk,
};
use bettermemory_core::pyvalue::PyStr;
use serde_json::{json, Value};

pub fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}

pub fn st(t: &[u32]) -> String {
    t.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

fn defaults() -> Value {
    json!({
        "path_state": ["present", "cwd", ""], "realpath": "/w/r", "keys": {}, "host": "h",
        "discover": null, "kept": false, "real_git": null, "stat_each": ["c", "g", "t"],
        "stats": {}, "evicted": [], "fetched": [], "token": ["absent", null],
        "write": ["written", "TOK"], "head": [null, null], "tips": [], "remotes": {},
        "manifest": null, "stamps": [], "ticks": null, "files": [],
        "clock": [[100.0, 1000.0]], "above_start": null, "walk": {}, "commit": null,
        "tree": null
    })
}

pub struct World {
    pub spec: RefCell<Value>,
    pub log: RefCell<Vec<String>>,
    pub clocks: RefCell<Vec<Value>>,
}

fn num(v: &Value) -> String {
    v.to_string()
}

fn opt(v: &Value) -> Option<PyStr> {
    v.as_str().map(cps)
}

fn key_of(v: &Value) -> Option<Key> {
    let a = v.as_array()?;
    Some((
        cps(a[0].as_str().unwrap()),
        cps(a[1].as_str().unwrap()),
        a[2].as_str().map(cps),
    ))
}

pub fn key_repr(k: Option<&Key>) -> String {
    let Some((a, b, c)) = k else {
        return "-".into();
    };
    let c = c.as_ref().map_or("None".into(), |c| format!("'{}'", st(c)));
    format!("('{}', '{}', {c})", st(a), st(b))
}

fn kind(s: &str) -> Kind {
    match s {
        "primary" => Kind::Primary,
        "linked" => Kind::Linked,
        "bare" => Kind::Bare,
        _ => Kind::Folder,
    }
}

fn git(v: &Value) -> GitPaths {
    GitPaths {
        dotgit: opt(&v["dotgit"]),
        git_dir: cps(v["git_dir"].as_str().unwrap()),
        common_dir: cps(v["common_dir"].as_str().unwrap()),
        root: opt(&v["root"]),
        kind: kind(v["kind"].as_str().unwrap()),
    }
}

impl World {
    pub fn new(spec: &Value) -> World {
        let mut full = defaults();
        for (k, v) in spec.as_object().unwrap() {
            full[k] = v.clone();
        }
        let clocks = full["clock"].as_array().unwrap().clone();
        World {
            spec: RefCell::new(full),
            log: RefCell::new(Vec::new()),
            clocks: RefCell::new(clocks),
        }
    }

    pub fn s(&self, k: &str) -> Value {
        self.spec.borrow()[k].clone()
    }

    pub fn note(&self, parts: &[&str]) {
        self.log.borrow_mut().push(parts.join(" "));
    }

    fn has(&self, k: &str, item: &str) -> bool {
        self.s(k).as_array().unwrap().iter().any(|v| v == item)
    }

    pub fn clock(&self) -> (f64, f64) {
        let mut clocks = self.clocks.borrow_mut();
        let pair = if clocks.len() == 1 {
            clocks[0].clone()
        } else {
            clocks.remove(0)
        };
        self.note(&["clock", &num(&pair[0]), &num(&pair[1])]);
        (pair[0].as_f64().unwrap(), pair[1].as_f64().unwrap())
    }
}

pub struct S<'a>(pub &'a World);
pub struct P<'a>(pub &'a World);
pub struct R<'a>(pub &'a World);

type Res<T> = Result<T, Declined>;

impl Steps for S<'_> {
    type Stat = String;

    fn path_state(&mut self, cwd: &[u32]) -> Res<PathState<String>> {
        self.0.note(&["path_state", &st(cwd)]);
        let p = self.0.s("path_state");
        let state = p[0].as_str().unwrap().to_string();
        Ok((
            state,
            p[1].as_str().map(str::to_string),
            cps(p[2].as_str().unwrap()),
        ))
    }
    fn view_path(&mut self, cwd: &[u32]) -> Res<PyStr> {
        self.0.note(&["view_path", &st(cwd)]);
        Ok(cps(&format!("v:{}", st(cwd))))
    }
    fn realpath(&mut self, cwd: &[u32]) -> Res<Result<PyStr, PyStr>> {
        self.0.note(&["realpath", &st(cwd)]);
        let r = self.0.s("realpath");
        Ok(match r.as_str() {
            Some(t) => Ok(cps(t)),
            None => Err(cps(r["raise"].as_str().unwrap())),
        })
    }
    fn object_key(&mut self, path: &[u32], s: Option<&String>) -> Res<Option<Key>> {
        let p = st(path);
        self.0.note(&["object_key", &p, s.map_or("-", |s| s)]);
        Ok(key_of(&self.0.s("keys")[&p]))
    }
    fn hostname(&mut self) -> Option<PyStr> {
        self.0.note(&["hostname"]);
        opt(&self.0.s("host"))
    }
    fn discover(&mut self, real: &[u32]) -> Res<Discovery> {
        self.0.note(&["discover", &st(real)]);
        let d = self.0.s("discover");
        if d.is_null() {
            return Ok(Discovery::Found(None));
        }
        Ok(match d["raise"].as_str() {
            None => Discovery::Found(Some(git(&d))),
            Some(k) => {
                let t = cps(d["text"].as_str().unwrap());
                match k {
                    "os" => Discovery::Unreadable(t),
                    "dies" => Discovery::Dies(t),
                    _ => Discovery::Format(t),
                }
            }
        })
    }
    fn kept_apart(&mut self, g: &GitPaths) -> Res<bool> {
        self.0.note(&["kept_apart", &st(&g.git_dir)]);
        Ok(self.0.s("kept").as_bool().unwrap())
    }
    fn real_git(&mut self, g: &GitPaths) -> Res<GitPaths> {
        self.0.note(&["real_git", &st(&g.git_dir)]);
        let r = self.0.s("real_git");
        Ok(if r.is_null() { g.clone() } else { git(&r) })
    }
    fn stat_each(&mut self, paths: [Option<&[u32]>; 3]) -> Vec<Option<String>> {
        let names: Vec<String> = paths.iter().map(|p| p.map_or("-".into(), st)).collect();
        let mut parts = vec!["stat_each"];
        parts.extend(names.iter().map(String::as_str));
        self.0.note(&parts);
        let each = self.0.s("stat_each");
        each.as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().map(str::to_string))
            .collect()
    }
    fn stat(&mut self, path: &[u32]) -> Res<Option<String>> {
        let p = st(path);
        self.0.note(&["stat", &p]);
        Ok(self.0.s("stats")[&p].as_str().map(str::to_string))
    }
    fn join(&self, a: &[u32], b: &str) -> PyStr {
        let mut out = a.to_vec();
        if !out.is_empty() && out.last() != Some(&u32::from('/')) {
            out.push(u32::from('/'));
        }
        out.extend(cps(b));
        out
    }
    fn evicted(&mut self, s: &String) -> bool {
        self.0.note(&["evicted", s]);
        self.0.has("evicted", s)
    }
    fn fetch(&mut self, path: &[u32]) -> Res<bool> {
        self.0.note(&["fetch", &st(path)]);
        Ok(self.0.has("fetched", &st(path)))
    }
    fn token_path(&mut self, gd: &[u32]) -> PyStr {
        self.0.note(&["token_path", &st(gd)]);
        cps(&format!("{}/token", st(gd)))
    }
    fn token_read(&mut self, gd: &[u32]) -> Res<TokenRead> {
        self.0.note(&["token_read", &st(gd)]);
        let t = self.0.s("token");
        Ok((t[0].as_str().unwrap().into(), opt(&t[1])))
    }
    fn token_write(&mut self, gd: &[u32], bare: bool) -> Res<TokenWrite> {
        let b = if bare { "True" } else { "False" };
        self.0.note(&["token_write", &st(gd), "True", b]);
        let t = self.0.s("write");
        Ok((t[0].as_str().unwrap().into(), opt(&t[1])))
    }
    fn resolve_head(&mut self, g: &GitPaths) -> Res<(Option<PyStr>, Option<PyStr>)> {
        self.0.note(&["resolve_head", &st(&g.git_dir)]);
        let h = self.0.s("head");
        Ok((opt(&h[0]), opt(&h[1])))
    }
    fn ref_tips(&mut self, common: &[u32]) -> Vec<PyStr> {
        self.0.note(&["ref_tips", &st(common)]);
        strs(&self.0.s("tips"))
    }
    fn read_remotes(&mut self, common: &[u32], gd: &[u32], base: &[u32]) -> Res<Remotes> {
        self.0.note(&["read_remotes", &st(common), &st(gd)]);
        self.0.note(&["remote_key", "u", &st(base)]);
        let r = self.0.s("remotes");
        Ok(Remotes {
            keys: strs(&r["keys"]),
            aliases: strs(&r["aliases"]),
            sources: strs(&r["sources"]),
            declined: opt(&r["declined"]),
        })
    }
    fn manifest(&mut self, root: &[u32]) -> Res<Option<Manifest>> {
        self.0.note(&["manifest", &st(root)]);
        let m = self.0.s("manifest");
        if m.is_null() {
            return Ok(None);
        }
        let entries = m[1].as_array().unwrap().iter();
        Ok(Some(Manifest {
            name: cps(m[0].as_str().unwrap()),
            entries: entries
                .map(|e| (cps(e[0].as_str().unwrap()), e[1].to_string()))
                .collect(),
        }))
    }
    fn init_stamps(&mut self, gd: &[u32], linked: bool) -> Res<Vec<Stamp>> {
        let l = if linked { "True" } else { "False" };
        self.0.note(&["init_stamps", &st(gd), l]);
        let s = self.0.s("stamps");
        Ok(s.as_array()
            .unwrap()
            .iter()
            .map(|e| Stamp {
                name: cps(e[0].as_str().unwrap()),
                numbers: [1, 2, 3, 4].map(|i| e[i].to_string()),
            })
            .collect())
    }
    fn capture_ticks(
        &mut self,
        own: Option<&String>,
        own_key: Option<&Key>,
        read: &[Option<String>],
        stamps: &[Stamp],
    ) -> Res<Option<bool>> {
        let names: Vec<&str> = read.iter().map(|r| r.as_deref().unwrap_or("-")).collect();
        let n = stamps.len().to_string();
        let k = key_repr(own_key);
        let k = if own_key.is_none() {
            "-".to_string()
        } else {
            k
        };
        self.0.note(&[
            "capture_ticks",
            own.map_or("-", |s| s),
            &k,
            &names.join(","),
            &n,
        ]);
        Ok(self.0.s("ticks").as_bool())
    }
    fn isfile(&mut self, path: &[u32]) -> bool {
        self.0.note(&["isfile", &st(path)]);
        self.0.has("files", &st(path))
    }
    fn clock(&mut self) -> (f64, f64) {
        self.0.clock()
    }
}

pub fn strs(v: &Value) -> Vec<PyStr> {
    v.as_array()
        .map(|a| a.iter().map(|s| cps(s.as_str().unwrap())).collect())
        .unwrap_or_default()
}

impl ProjectFiles for P<'_> {
    type Above = String;

    fn above_start(&mut self, cwd: &[u32], root: &[u32]) -> Option<PyStr> {
        self.0.note(&["above_start", &st(cwd), &st(root)]);
        opt(&self.0.s("above_start"))
    }

    fn find(&mut self, cwd: &[u32], root: Option<&[u32]>, above: Option<&String>) -> Walk<String> {
        let r = root.map_or("-".into(), st);
        self.0
            .note(&["find", &st(cwd), &r, above.map_or("-", |a| a)]);
        let w = self.0.s("walk");
        let made: Vec<ProjectFile> = w["files"]
            .as_array()
            .map(|a| a.to_vec())
            .unwrap_or_default()
            .iter()
            .map(|f| {
                let dir = f[2].as_str().unwrap();
                ProjectFile {
                    id: cps(f[0].as_str().unwrap()),
                    name: cps(f[1].as_str().unwrap()),
                    directory: cps(dir),
                    path: cps(&format!("{dir}/bettermemory.toml")),
                    boundary: f[3].as_bool().unwrap(),
                }
            })
            .collect();
        // A dict by directory: the last file of a directory wins.
        let pick = |k: &Value| {
            let k = cps(k.as_str()?);
            made.iter().rev().find(|f| f.directory == k).cloned()
        };
        Walk {
            files: made.clone(),
            errors: strs(&w["errors"]),
            above: above
                .cloned()
                .unwrap_or_else(|| format!("above:{}", st(cwd))),
            folder: pick(&w["folder"]),
            root: pick(&w["root"]),
        }
    }
}

impl ObjectReads for R<'_> {
    fn commit(&mut self, common: &[u32], sha: &[u32]) -> Option<PyStr> {
        self.0.note(&["commit", &st(common), &st(sha)]);
        opt(&self.0.s("commit"))
    }
    fn tree(&mut self, common: &[u32], sha: &[u32]) -> Option<Vec<(Vec<u8>, u32)>> {
        self.0.note(&["tree", &st(common), &st(sha)]);
        let t = self.0.s("tree");
        let entries = t.as_array()?;
        Some(
            entries
                .iter()
                .map(|e| {
                    let name = e[0].as_str().unwrap().as_bytes().to_vec();
                    (name, e[1].as_u64().unwrap() as u32)
                })
                .collect(),
        )
    }
}

fn sv(t: &Option<PyStr>) -> Value {
    t.as_ref().map_or(Value::Null, |t| json!(st(t)))
}

pub fn kv(k: &Option<Key>) -> Value {
    k.as_ref()
        .map_or(Value::Null, |(a, b, c)| json!([st(a), st(b), sv(c)]))
}

fn list(v: &[PyStr]) -> Value {
    json!(v.iter().map(|s| st(s)).collect::<Vec<_>>())
}

/// The generator's `_capture`.
pub fn capture_json(c: &Capture) -> Value {
    let int = |s: &String| -> Value { serde_json::from_str(s).unwrap() };
    json!({
        "cwd": st(&c.cwd), "state": c.state.as_str(), "root": sv(&c.root),
        "kind": c.kind.map(|k| k.as_str()), "git_dir": sv(&c.git_dir),
        "common_dir": sv(&c.common_dir), "object_key": kv(&c.object_key),
        "common_object_key": kv(&c.common_object_key),
        "root_object_key": kv(&c.root_object_key), "cwd_key": kv(&c.cwd_key),
        "head": sv(&c.head), "branch": sv(&c.branch), "ref_tips": list(&c.ref_tips),
        "remote_keys": list(&c.remote_keys), "alias_keys": list(&c.alias_keys),
        "config_sources": list(&c.config_sources), "shallow": c.shallow,
        "token": sv(&c.token), "token_written": sv(&c.token_written),
        "token_status": st(&c.token_status),
        "stamps": c.stamps.iter().map(|s| {
            let mut row = vec![json!(st(&s.name))];
            row.extend(s.numbers.iter().map(int));
            Value::Array(row)
        }).collect::<Vec<_>>(),
        "ticks": c.ticks,
        "files": c.files.iter().map(|f| json!([st(&f.id), st(&f.name), st(&f.dir),
            st(&f.path), f.boundary, kv(&f.key)])).collect::<Vec<_>>(),
        "file_errors": list(&c.file_errors), "file_tracked": c.file_tracked,
        "evicted": c.evicted, "detail": st(&c.detail),
        "manifest": c.manifest.as_ref().map(|m| json!([st(&m.name),
            m.entries.iter().map(|(p, n)| json!([st(p), int(n)])).collect::<Vec<_>>()])),
        "hostname": sv(&c.hostname), "kept_apart": c.kept_apart,
    })
}

/// One capture row run here: (log, out).
pub fn run_capture(row: &Value, mode_bits: u32) -> (Vec<String>, Value) {
    let world = World::new(&row["world"]);
    let cache = AboveCache::new(bettermemory_core::location::capture::CACHE_CAP);
    let opts = Options {
        write_token: row["write_token"].as_bool().unwrap(),
        manifest: row["manifest"].as_bool().unwrap(),
        mode_bits,
    };
    let got = capture_uncached(
        &mut S(&world),
        &mut P(&world),
        &cache,
        &mut R(&world),
        &opts,
        &cps("/w/c"),
    );
    let out = match got {
        Ok(c) => capture_json(&c),
        Err(Declined(text)) => json!({ "raise": text }),
    };
    let log = world.log.borrow().clone();
    (log, out)
}
