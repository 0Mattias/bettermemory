//! The cases of `location_git_readers.jsonl` and
//! `location_git_readers_posix.jsonl` as data: the generator's synthetic
//! histories (`_History`, `_Rng`, `_random`), its answer lines, and its
//! trees, built in memory for both file-system models.

#![allow(dead_code)]

use bettermemory_core::fsdata::{Flavour, MemFs, Node};
use bettermemory_core::githead;
use bettermemory_core::location_git::{Reach, Walked, EDGE_LIMIT};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashMap;

/// The generator's `_LONG`.
pub const LONG: usize = 600;

fn sha_hex(text: &str) -> String {
    Sha256::digest(text.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// `_id(i)`.
pub fn id(i: usize) -> String {
    sha_hex(&format!("c{i}"))[..40].to_owned()
}

/// `_hash(seed, i)`.
pub fn hash(seed: &str, i: usize) -> String {
    sha_hex(&format!("{seed}{i}"))[..40].to_owned()
}

/// `_Rng`: xorshift64*.
pub struct Rng(pub u64);

impl Rng {
    pub fn next(&mut self, bound: u64) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D) % bound
    }
}

/// `_History`.
pub struct Hist {
    pub ids: Vec<String>,
    pub index: HashMap<String, usize>,
    pub lines: Vec<Vec<usize>>,
    pub upper: bool,
}

impl Hist {
    pub fn new(lines: Vec<Vec<usize>>, upper: bool) -> Hist {
        let ids: Vec<String> = (0..lines.len()).map(id).collect();
        let index = ids
            .iter()
            .enumerate()
            .map(|(i, s)| (s.clone(), i))
            .collect();
        Hist {
            ids,
            index,
            lines,
            upper,
        }
    }
    pub fn of(case: &Value) -> Hist {
        let lines = case["p"]
            .as_array()
            .unwrap()
            .iter()
            .map(|l| {
                l.as_array()
                    .unwrap()
                    .iter()
                    .map(|p| p.as_u64().unwrap() as usize)
                    .collect()
            })
            .collect();
        Hist::new(lines, case.get("up").is_some())
    }
    pub fn at(&self, sha: &str) -> Option<usize> {
        self.index.get(&sha.to_lowercase()).copied()
    }
    pub fn parents(&self, sha: &str) -> Vec<String> {
        let Some(i) = self.at(sha) else {
            return Vec::new();
        };
        self.lines[i]
            .iter()
            .map(|&p| {
                if self.upper {
                    self.ids[p].to_uppercase()
                } else {
                    self.ids[p].clone()
                }
            })
            .collect()
    }
    pub fn fingerprint(&self, sha: &str) -> Option<String> {
        match self.at(sha) {
            Some(i) if i % 3 != 0 => Some(format!("f{i}")),
            _ => None,
        }
    }
    pub fn name(&self, sha: &str) -> String {
        self.at(sha).map_or(sha.to_owned(), |i| i.to_string())
    }
    /// A given commit: an index or a text as is.
    pub fn given(&self, v: &Value) -> String {
        match v.as_u64() {
            Some(i) => self.ids[i as usize].clone(),
            None => v.as_str().unwrap().to_owned(),
        }
    }
}

/// `_reach_line`.
pub fn reach_line(h: &Hist, reach: &Reach) -> String {
    let rel: Vec<String> = reach
        .relations
        .iter()
        .map(|(c, from)| {
            let names: Vec<String> = from.iter().map(|o| h.name(o)).collect();
            format!("{}:{}", h.name(c), names.join(","))
        })
        .collect();
    format!(
        "{}|{}|{}",
        rel.join(";"),
        u8::from(reach.bounded),
        reach.handled
    )
}

/// `_walk_line`.
pub fn walk_line(h: &Hist, out: &[Walked]) -> String {
    out.iter()
        .map(|(s, (fp, ps))| {
            let ps: Vec<String> = ps.iter().map(|p| h.name(p)).collect();
            format!(
                "{}/{}/{}",
                h.name(s),
                fp.as_deref().unwrap_or("-"),
                ps.join("+")
            )
        })
        .collect::<Vec<_>>()
        .join(",")
}

/// `_shown`.
pub fn shown(line: &str) -> Value {
    if line.chars().count() <= LONG {
        json!(line)
    } else {
        json!({"n": line.chars().count(), "h": sha_hex(line)})
    }
}

/// A row's reach answer over the native port.
pub fn reach_of(case: &Value) -> Value {
    let h = Hist::of(case);
    let given: Vec<String> = case["g"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| h.given(v))
        .collect();
    let times: Option<Vec<i128>> = case.get("t").map(|t| {
        t.as_array()
            .unwrap()
            .iter()
            .map(|v| i128::from(v.as_u64().unwrap()))
            .collect()
    });
    let limit = case["limit"].as_u64().unwrap() as usize;
    let edges = case["edges"].as_u64().unwrap();
    let mut parents = |s: &str| h.parents(s);
    let mut committed = |s: &str| times.as_ref().unwrap()[h.at(s).unwrap_or(0)];
    let reach = bettermemory_core::location_git::reachable_among(
        &mut parents,
        &given,
        limit,
        edges,
        if times.is_some() {
            Some(&mut committed)
        } else {
            None
        },
    );
    shown(&reach_line(&h, &reach))
}

/// A row's walk answer over the native port.
pub fn walk_of(case: &Value) -> Value {
    let h = Hist::of(case);
    let head = match &case["head"] {
        Value::Null => None,
        v => Some(h.given(v)),
    };
    let tips: Vec<String> = case["tips"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| h.given(v))
        .collect();
    let mut parents = |s: &str| h.parents(s);
    let mut fp = |s: &str| h.fingerprint(s);
    let out = bettermemory_core::location_git::walk(&mut parents, &mut fp, head.as_deref(), &tips);
    shown(&walk_line(&h, &out))
}

/// `_random(rng)`.
pub type Random = (Vec<Vec<usize>>, Option<Vec<i128>>, Vec<usize>, usize, u64);

pub fn random(rng: &mut Rng) -> Random {
    let n = 1 + rng.next(30) as usize;
    let mut parents = Vec::new();
    for i in 0..n {
        if i < n - 1 {
            let k = rng.next(4);
            parents.push(
                (0..k)
                    .map(|_| i + 1 + rng.next((n - i - 1) as u64) as usize)
                    .collect(),
            );
        } else {
            parents.push(Vec::new());
        }
    }
    let mut times: Vec<i128> = (0..n).map(|i| 1000 - 10 * i as i128).collect();
    for t in times.iter_mut() {
        if rng.next(3) == 0 {
            *t += rng.next(101) as i128 - 50;
        }
    }
    let times = if rng.next(5) == 0 { None } else { Some(times) };
    let count = 1 + rng.next(8);
    let mut given: Vec<usize> = (0..count).map(|_| rng.next(n as u64) as usize).collect();
    given.sort_unstable();
    given.dedup();
    let limit = 1 + rng.next(40) as usize;
    let edges = if rng.next(2) == 1 {
        1 + rng.next(60)
    } else {
        EDGE_LIMIT
    };
    (parents, times, given, limit, edges)
}

/// One sweep answer line: `_reach_line`, `#`, `_walk_line`.
pub fn sweep_line(case: &Random) -> String {
    let (parents, times, given, limit, edges) = case;
    let h = Hist::new(parents.clone(), false);
    let ids: Vec<String> = given.iter().map(|&g| h.ids[g].clone()).collect();
    let mut p = |s: &str| h.parents(s);
    let mut committed = |s: &str| times.as_ref().unwrap()[h.index[s]];
    let reach = bettermemory_core::location_git::reachable_among(
        &mut p,
        &ids,
        *limit,
        *edges,
        if times.is_some() {
            Some(&mut committed)
        } else {
            None
        },
    );
    let head = (given[0] % 2 == 1).then(|| h.ids[given[0]].clone());
    let tips: Vec<String> = h.ids.iter().take(2).cloned().collect();
    let mut p2 = |s: &str| h.parents(s);
    let mut fp = |s: &str| h.fingerprint(s);
    let out = bettermemory_core::location_git::walk(&mut p2, &mut fp, head.as_deref(), &tips);
    format!("{}#{}", reach_line(&h, &reach), walk_line(&h, &out))
}

/// The sweep's digest over `count` cases from `seed`.
pub fn sweep(seed: u64, count: usize) -> String {
    let mut rng = Rng(seed);
    let mut digest = Sha256::new();
    for _ in 0..count {
        let case = random(&mut rng);
        digest.update(sweep_line(&case).as_bytes());
        digest.update(b"\n");
    }
    digest
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

// -- trees ------------------------------------------------------------------

/// `_b`'s spelling read back.
pub fn unb(text: &str) -> Vec<u8> {
    match text.strip_prefix('=') {
        Some(rest) => rest.as_bytes().to_vec(),
        None => super::unhex(text),
    }
}

/// `_b`.
pub fn b(data: &[u8]) -> String {
    if data
        .iter()
        .all(|&c| (0x20..0x7f).contains(&c) && c != b'\\')
    {
        format!("={}", std::str::from_utf8(data).unwrap())
    } else {
        super::hex(data)
    }
}

/// `os.fsencode` of a str (UTF-8, U+DC80 to U+DCFF as their byte).
pub fn fsencode(text: &[u32]) -> Vec<u8> {
    let mut out = Vec::new();
    for &c in text {
        if (0xdc80..=0xdcff).contains(&c) {
            out.push((c - 0xdc00) as u8);
        } else {
            let ch = char::from_u32(c).expect("a scalar value");
            let mut buf = [0; 4];
            out.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
        }
    }
    out
}

/// One built entry: a file, a folder, a link, or mode 000.
#[derive(Clone, Debug)]
pub enum Entry {
    File(Vec<u8>, Vec<u8>),
    Dir(Vec<u8>),
    Link(Vec<u8>, Vec<u8>),
    Locked(Vec<u8>),
}

fn join(base: &[u8], rel: &[u8]) -> Vec<u8> {
    if rel == b"." {
        return base.to_vec();
    }
    if base == b"." {
        return rel.to_vec();
    }
    let mut out = base.to_vec();
    out.push(b'/');
    out.extend_from_slice(rel);
    out
}

/// `_build`'s items as entries, relative to the tree's folder.
pub fn entries(items: &Value) -> Vec<Entry> {
    let mut out = Vec::new();
    for item in items.as_array().unwrap() {
        let kind = item[0].as_str().unwrap();
        let rel = unb(item[1].as_str().unwrap());
        let s = |k: usize| item[k].as_str().unwrap();
        let n = |k: usize| item[k].as_u64().unwrap() as usize;
        match kind {
            "f" => out.push(Entry::File(rel, unb(s(2)))),
            "d" => out.push(Entry::Dir(rel)),
            "l" => out.push(Entry::Link(rel, unb(s(2)))),
            "m" => out.push(Entry::Locked(rel)),
            "n" => {
                out.push(Entry::Dir(rel.clone()));
                for i in 0..n(2) {
                    let name = join(&rel, format!("f{i:04}").as_bytes());
                    out.push(Entry::File(name, vec![b'x'; n(3)]));
                }
            }
            "r" => {
                out.push(Entry::Dir(rel.clone()));
                for i in 0..n(2) {
                    let name = join(&rel, format!("{i:03}").as_bytes());
                    out.push(Entry::File(
                        name,
                        format!("{}\n", hash(s(3), i)).into_bytes(),
                    ));
                }
            }
            "q" => {
                let mut data = b"# pack-refs with: peeled fully-peeled sorted \n".to_vec();
                for i in 0..n(2) {
                    data.extend(format!("{} refs/heads/p{i:03}\n", hash(s(3), i)).bytes());
                }
                out.push(Entry::File(rel, data));
            }
            other => panic!("an item kind {other}"),
        }
    }
    out
}

fn ancestors(path: &[u8]) -> Vec<Vec<u8>> {
    let mut out = Vec::new();
    for (i, &c) in path.iter().enumerate() {
        if c == b'/' && i > 0 {
            out.push(path[..i].to_vec());
        }
    }
    out
}

/// The tree in `fsdata`'s memory model, at `base` (absolute).
pub fn mem_tree(entries: &[Entry], base: &[u8]) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    let mut known = std::collections::HashSet::new();
    let mut dir = |fs: &mut MemFs, path: &[u8]| {
        for a in ancestors(path) {
            if known.insert(a.clone()) {
                fs.insert(&a, Node::Dir);
            }
        }
    };
    let mut locked = Vec::new();
    dir(&mut fs, &join(base, b"x"));
    fs.insert(base, Node::Dir);
    for e in entries {
        match e {
            Entry::File(rel, data) => {
                let p = join(base, rel);
                dir(&mut fs, &p);
                fs.insert(&p, Node::File(data.clone()));
            }
            Entry::Dir(rel) => {
                let p = join(base, rel);
                dir(&mut fs, &p);
                fs.insert(&p, Node::Dir);
            }
            Entry::Link(rel, target) => {
                let p = join(base, rel);
                dir(&mut fs, &p);
                fs.insert(&p, Node::Link(target.clone()));
            }
            Entry::Locked(rel) => locked.push(join(base, rel)),
        }
    }
    for p in locked {
        fs.lock(&p);
    }
    fs
}

/// The tree in githead's memory model, at `base` (relative).
pub fn ref_tree(entries: &[Entry], base: &[u8]) -> githead::MemFs {
    let mut fs = githead::MemFs::new(githead::Flavour::Posix);
    fs.insert(base, githead::fs::Node::Dir);
    for e in entries {
        match e {
            Entry::File(rel, data) => {
                fs.insert(&join(base, rel), githead::fs::Node::File(data.clone()))
            }
            Entry::Dir(rel) => fs.insert(&join(base, rel), githead::fs::Node::Dir),
            Entry::Link(rel, t) => fs.insert(&join(base, rel), githead::fs::Node::Link(t.clone())),
            Entry::Locked(_) => panic!("no mode in a head case"),
        }
    }
    fs
}

/// The tree on disk under `base`, as `_build` makes it.
#[cfg(unix)]
pub fn disk_tree(entries: &[Entry], base: &std::path::Path) {
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::PermissionsExt;
    let at = |rel: &[u8]| {
        if rel == b"." {
            base.to_path_buf()
        } else {
            base.join(std::ffi::OsStr::from_bytes(rel))
        }
    };
    std::fs::create_dir_all(base).unwrap();
    let mut locked = Vec::new();
    for e in entries {
        match e {
            Entry::File(rel, data) => {
                let p = at(rel);
                std::fs::create_dir_all(p.parent().unwrap()).unwrap();
                std::fs::write(&p, data).unwrap();
            }
            Entry::Dir(rel) => std::fs::create_dir_all(at(rel)).unwrap(),
            Entry::Link(rel, t) => {
                let p = at(rel);
                std::fs::create_dir_all(p.parent().unwrap()).unwrap();
                std::os::unix::fs::symlink(std::ffi::OsStr::from_bytes(t), &p).unwrap();
            }
            Entry::Locked(rel) => locked.push(at(rel)),
        }
    }
    for p in locked {
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o000)).unwrap();
    }
}

/// `_show_manifest` of a native manifest.
pub fn show_manifest(m: &bettermemory_core::capture::Manifest) -> Value {
    let rows: Vec<String> = m
        .entries
        .iter()
        .map(|(p, s)| format!("{} {s}", b(&fsencode(p))))
        .collect();
    json!({"name": b(&fsencode(&m.name)), "e": shown(&rows.join("\n"))})
}
