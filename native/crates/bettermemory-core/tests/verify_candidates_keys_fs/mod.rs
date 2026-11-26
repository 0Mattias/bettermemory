//! What the tests of `verify_candidates::keys` share: a file system that
//! records the `lstat`, `stat` and `readlink` calls made of it
//! ([`Traced`]), the scratch trees of the fixtures built on the disk or in
//! memory, and the texts the fixtures spell with `@` for the tree.
//!
//! A test file takes this with `mod verify_candidates_keys_fs;`.

#![allow(dead_code)]

use std::cell::RefCell;

use bettermemory_core::fsdata::{
    Access, Flavour, Fs, Kind, MemFs, Node, OsError, PyErr, Stat, StatResult,
};
use bettermemory_core::verify_candidates::scan::{getpwnam_refuses, UserHome};
use serde_json::Value;

/// An [`Fs`] that records each `lstat`, `stat` and `readlink` asked of
/// it, as `(name, path)`, and answers as the one it wraps.
pub struct Traced<'a, F: Fs + ?Sized> {
    pub inner: &'a F,
    pub calls: RefCell<Vec<(&'static str, Vec<u8>)>>,
}

impl<'a, F: Fs + ?Sized> Traced<'a, F> {
    pub fn new(inner: &'a F) -> Self {
        Traced {
            inner,
            calls: RefCell::new(Vec::new()),
        }
    }

    pub fn take(&self) -> Vec<(&'static str, Vec<u8>)> {
        std::mem::take(&mut *self.calls.borrow_mut())
    }

    fn note(&self, name: &'static str, path: &[u8]) {
        self.calls.borrow_mut().push((name, path.to_vec()));
    }
}

impl<F: Fs + ?Sized> Fs for Traced<'_, F> {
    type File = F::File;

    fn open(&self, path: &[u8]) -> Result<Self::File, PyErr> {
        self.inner.open(path)
    }
    fn fstat(&self, file: &mut Self::File) -> Result<Stat, PyErr> {
        self.inner.fstat(file)
    }
    fn read(&self, file: &mut Self::File, max: usize) -> Result<Vec<u8>, PyErr> {
        self.inner.read(file, max)
    }
    fn access(&self, path: &[u8], mode: Access) -> Result<bool, PyErr> {
        self.inner.access(path, mode)
    }
    fn open_no_follow(&self, path: &[u8]) -> Result<Self::File, OsError> {
        self.inner.open_no_follow(path)
    }
    fn lstat(&self, path: &[u8]) -> Result<Kind, PyErr> {
        self.note("lstat", path);
        self.inner.lstat(path)
    }
    fn readlink(&self, path: &[u8]) -> Result<Vec<u8>, PyErr> {
        self.note("readlink", path);
        self.inner.readlink(path)
    }
    fn stat(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.note("stat", path);
        self.inner.stat(path)
    }
    fn lstat_result(&self, path: &[u8]) -> Result<StatResult, OsError> {
        self.inner.lstat_result(path)
    }
    fn listdir(&self, path: &[u8]) -> Result<Vec<Vec<u8>>, OsError> {
        self.inner.listdir(path)
    }
}

pub fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

/// A fixture's text: a string, or `{"cps": [...]}`.
pub fn text(v: &Value) -> Vec<u32> {
    match v {
        Value::String(s) => cps(s),
        _ => v["cps"]
            .as_array()
            .expect("a text")
            .iter()
            .map(|c| c.as_u64().expect("a code point") as u32)
            .collect(),
    }
}

/// `t` with each `@` read as `root`.
pub fn at(t: &[u32], root: &[u32]) -> Vec<u32> {
    let mut out = Vec::with_capacity(t.len());
    for &c in t {
        if c == u32::from('@') {
            out.extend_from_slice(root);
        } else {
            out.push(c);
        }
    }
    out
}

/// A path as the fixture spells it: `@` for `root`, `^` for `flipped`.
pub fn spell(path: &[u32], root: &[u32], flipped: &[u32]) -> Value {
    let mut spelled = path.to_vec();
    for (base, mark) in [(root, '@'), (flipped, '^')] {
        if path == base || (path.starts_with(base) && path.get(base.len()) == Some(&u32::from('/')))
        {
            spelled = std::iter::once(u32::from(mark))
                .chain(path[base.len()..].iter().copied())
                .collect();
            break;
        }
    }
    let wide = spelled.windows(2).any(|w| w == [0x5C, 0x5C]);
    match spelled
        .iter()
        .map(|&c| char::from_u32(c))
        .collect::<Option<String>>()
    {
        Some(s) if !wide => Value::String(s),
        _ => serde_json::json!({ "cps": spelled }),
    }
}

/// The bytes a fixture's call names: its `@` or `^` read as the root or
/// its case-swapped spelling, encoded as `os.fsencode` encodes.
pub fn call_bytes(v: &Value, root: &[u32], flipped: &[u32]) -> Vec<u8> {
    let t = text(v);
    let path = match t.first() {
        Some(&c) if c == u32::from('@') => [root, &t[1..]].concat(),
        Some(&c) if c == u32::from('^') => [flipped, &t[1..]].concat(),
        _ => t,
    };
    bettermemory_core::gitfile_rule::follow::fsencode(&path).expect("an encodable call")
}

/// The calls a fixture lists: those not on a folder above `root`.
pub fn kept_calls(
    calls: Vec<(&'static str, Vec<u8>)>,
    root: &[u8],
) -> Vec<(&'static str, Vec<u8>)> {
    calls
        .into_iter()
        .filter(|(_, p)| {
            let trimmed: &[u8] = {
                let end = p.iter().rposition(|&b| b != b'/').map_or(0, |i| i + 1);
                &p[..end]
            };
            !(p == b"/" || (root.starts_with(trimmed) && root.get(trimmed.len()) == Some(&b'/')))
        })
        .collect()
}

/// ASCII `str.swapcase` of a scratch root (temporary folders are ASCII).
pub fn ascii_swapcase(t: &[u32]) -> Vec<u32> {
    t.iter()
        .map(|&c| {
            assert!(c < 0x80, "a scratch root past ASCII");
            let b = c as u8;
            u32::from(if b.is_ascii_uppercase() {
                b.to_ascii_lowercase()
            } else {
                b.to_ascii_uppercase()
            })
        })
        .collect()
}

/// The fixture's `pwd.getpwnam`: refused as CPython refuses, else the
/// table with `@` read as `root`.
pub fn lookup<'a>(users: &'a Value, root: &'a [u32]) -> impl FnMut(&[u32]) -> UserHome + 'a {
    move |name: &[u32]| {
        if getpwnam_refuses(name) {
            return UserHome::Refused;
        }
        let Some(name) = name
            .iter()
            .map(|&c| char::from_u32(c))
            .collect::<Option<String>>()
        else {
            return UserHome::Unknown;
        };
        match users.get(&name).and_then(Value::as_str) {
            Some(home) => UserHome::Found(at(&cps(home), root)),
            None => UserHome::Unknown,
        }
    }
}

/// A fixture tree on the disk under `root`.
#[cfg(unix)]
pub fn build_on_disk(root: &std::path::Path, tree: &Value) {
    use std::os::unix::ffi::OsStrExt;
    let r = root.as_os_str().as_bytes().to_vec();
    for entry in tree.as_array().expect("a tree") {
        let kind = entry[0].as_str().unwrap();
        let rel = entry[1].as_str().unwrap();
        let path = root.join(rel);
        match kind {
            "d" => std::fs::create_dir_all(&path).unwrap(),
            "f" => {
                std::fs::create_dir_all(path.parent().unwrap()).unwrap();
                std::fs::write(&path, b"").unwrap();
            }
            _ => {
                std::fs::create_dir_all(path.parent().unwrap()).unwrap();
                let target: Vec<u8> = entry[2]
                    .as_str()
                    .unwrap()
                    .bytes()
                    .flat_map(|b| if b == b'@' { r.clone() } else { vec![b] })
                    .collect();
                std::os::unix::fs::symlink(std::ffi::OsStr::from_bytes(&target), &path).unwrap();
            }
        }
    }
}

/// A fixture tree in memory under `root` (bytes, absolute).
pub fn build_in_memory(root: &[u8], tree: &Value) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(root, Node::Dir);
    for entry in tree.as_array().expect("a tree") {
        let rel = entry[1].as_str().unwrap().as_bytes();
        let path = [root, b"/", rel].concat();
        let node = match entry[0].as_str().unwrap() {
            "d" => Node::Dir,
            "f" => Node::File(Vec::new()),
            _ => Node::Link(
                entry[2]
                    .as_str()
                    .unwrap()
                    .bytes()
                    .flat_map(|b| if b == b'@' { root.to_vec() } else { vec![b] })
                    .collect(),
            ),
        };
        fs.insert(&path, node);
    }
    fs
}
