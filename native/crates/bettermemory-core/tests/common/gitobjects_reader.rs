//! What the tests of `gitobjects::files::reader` share with
//! `tests/contract/gen_gitobjects_reader.py`: a case's tree built in memory
//! or on disk (file bytes as written, no `@`), and its questions asked of
//! the port and answered in the fixture's spelling.

#![allow(dead_code)]

use bettermemory_core::fsdata::{Flavour, MemFs, Node, PyErr};
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::gitobjects::files::reader::{Fault, MAX_OBJECT_BYTES};
use bettermemory_core::gitobjects::files::{ObjectReader, Stop};
use bettermemory_core::gitobjects::Raised;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::gitobjects_files::{abs, claims_of, spelled, Claims, Tree};

/// The case's tree in memory, at `root`.
pub fn mem_tree(case: &Value, root: &[u8]) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(root, Node::Dir);
    let mut modes = Vec::new();
    for entry in case["t"].as_array().unwrap() {
        let path = abs(root, entry[1].as_str().unwrap());
        match entry[0].as_str().unwrap() {
            "d" => fs.insert(&path, Node::Dir),
            "f" => fs.insert(&path, Node::File(spelled(&entry[2]))),
            "p" => fs.insert(&path, Node::Fifo),
            "m" => modes.push((path, entry[2].as_u64().unwrap() as u32)),
            other => panic!("a tree entry {other}"),
        }
    }
    for (path, mode) in modes {
        fs.set_mode(&path, mode);
    }
    fs
}

/// The case's tree on disk, at `root` (a real path).
#[cfg(unix)]
pub fn disk_tree(case: &Value, root: &[u8]) {
    use super::gitobjects_files::path_of;
    use std::os::unix::fs::PermissionsExt;
    let mut modes = Vec::new();
    for entry in case["t"].as_array().unwrap() {
        let kind = entry[0].as_str().unwrap();
        let path = path_of(&abs(root, entry[1].as_str().unwrap()));
        if kind != "m" {
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        }
        match kind {
            "d" => std::fs::create_dir_all(&path).unwrap(),
            "f" => std::fs::write(&path, spelled(&entry[2])).unwrap(),
            "p" => {
                let made = std::process::Command::new("mkfifo")
                    .arg(&path)
                    .status()
                    .unwrap();
                assert!(made.success());
            }
            "m" => modes.push((path, entry[2].as_u64().unwrap() as u32)),
            other => panic!("a tree entry {other}"),
        }
    }
    for (path, mode) in modes {
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode)).unwrap();
    }
}

fn hex(data: &[u8]) -> String {
    data.iter().map(|b| format!("{b:02x}")).collect()
}

fn content(data: &[u8]) -> Value {
    if data.len() <= 64 {
        json!(hex(data))
    } else {
        json!({"n": data.len(), "sha256": hex(&Sha256::digest(data))})
    }
}

/// A fault in the fixture's spelling.
pub fn fault(fault: Fault) -> Value {
    let class = match fault {
        Fault::Overflow => "OverflowError",
        Fault::Key(_) => "KeyError",
        Fault::Raised(Raised::ValueError) | Fault::Stop(Stop::Raised(PyErr::Value)) => "ValueError",
        other => panic!("a fault the fixture has no spelling for: {other:?}"),
    };
    json!({ "raise": class })
}

fn rel(root: &[u8], path: &[u8]) -> Value {
    let rest = &path[root.len() + 1..];
    json!(rest.iter().map(|&b| b as char).collect::<String>())
}

/// One question asked of `reader`.
pub fn answer<F: Tree>(
    reader: &mut ObjectReader<'_, Claims<F>>,
    fs: &Claims<F>,
    root: &[u8],
    q: &Value,
) -> Value {
    match q.as_str() {
        Some("n") => return reader.name_length().map_or_else(fault, |n| json!(n)),
        Some("c") => {
            return reader
                .complete()
                .map_or_else(|s| fault(Fault::Stop(s)), |c| json!(c))
        }
        Some("e") => return Value::Array(reader.evicted().iter().map(|p| rel(root, p)).collect()),
        _ => {}
    }
    let op = q[0].as_str().unwrap();
    if op == "w" {
        F::write(fs, &abs(root, q[1].as_str().unwrap()), &spelled(&q[2]));
        return Value::Null;
    }
    if op == "m" {
        let shas: Vec<&str> = q[1]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| s.as_str().unwrap())
            .collect();
        return reader
            .has_many(&shas)
            .map_or_else(fault, |found| json!(found.into_iter().collect::<Vec<_>>()));
    }
    let sha = q[1].as_str().unwrap();
    let cap = q.get(2).and_then(Value::as_u64).unwrap_or(MAX_OBJECT_BYTES);
    let answered = match op {
        "h" => reader.has(sha).map(|h| json!(h)),
        "v" => reader.resolve(sha).map(|v| json!(v)),
        "H" => reader
            .header(sha)
            .map(|h| h.map_or(Value::Null, |(kind, size)| json!([kind, size.hex()]))),
        "R" => reader
            .read(sha, cap)
            .map(|r| r.map_or(Value::Null, |(kind, data)| json!([kind, content(&data)]))),
        "C" => reader.commit(sha, cap).map(|c| {
            c.map_or(Value::Null, |c| {
                json!({
                    "tree": c.tree,
                    "parents": c.parents,
                    "author": hex(&c.author),
                    "committer": hex(&c.committer),
                    "message": hex(&c.message),
                })
            })
        }),
        "T" => reader.tree(sha, cap).map(|t| {
            t.map_or(Value::Null, |entries| {
                Value::Array(
                    entries
                        .iter()
                        .map(|e| json!([e.mode, hex(&e.name), e.sha]))
                        .collect(),
                )
            })
        }),
        "F" => reader.fingerprint(sha, cap).map(|f| json!(f)),
        other => panic!("a question {other}"),
    };
    answered.unwrap_or_else(fault)
}

/// The case's questions asked of the port over `fs` (the tree at `root`).
pub fn ask<F: Tree>(fs: &Claims<F>, root: &[u8], case: &Value) -> Value {
    let git_dir = abs(root, "g");
    let facts = GitFacts {
        release: Some((2, 54)),
        reads_compat: Some(true),
    };
    let mut reader = ObjectReader::new(fs, Flavour::Posix, b"/", facts, &git_dir);
    let answers = case["q"]
        .as_array()
        .unwrap()
        .iter()
        .map(|q| answer(&mut reader, fs, root, q))
        .collect();
    Value::Array(answers)
}

/// The case asked over its tree in memory at `/m`.
pub fn ask_in_memory(case: &Value) -> Value {
    let root = b"/m".to_vec();
    let mut fs = Claims::new(mem_tree(case, &root));
    fs.claims = claims_of(case, &root);
    ask(&fs, &root, case)
}

/// The case asked over its tree on disk in a scratch folder.
#[cfg(unix)]
pub fn ask_on_disk(case: &Value, tag: &str) -> Value {
    use bettermemory_core::fsdata::StdFs;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::PermissionsExt;
    let tmp = std::env::temp_dir().join(format!("bm-gobj-reader-{}-{tag}", std::process::id()));
    let _ = std::fs::remove_dir_all(&tmp);
    std::fs::create_dir_all(&tmp).unwrap();
    let real = std::fs::canonicalize(&tmp).unwrap();
    let root = real.as_os_str().as_bytes().to_vec();
    disk_tree(case, &root);
    let mut fs = Claims::new(StdFs);
    fs.claims = claims_of(case, &root);
    let answer = ask(&fs, &root, case);
    for entry in case["t"].as_array().unwrap() {
        if entry[0] == "m" {
            let path = super::gitobjects_files::path_of(&abs(&root, entry[1].as_str().unwrap()));
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
    }
    std::fs::remove_dir_all(&tmp).unwrap();
    answer
}

/// Every case of a fixture, in memory and, on unix, on disk.
pub fn every_case(test: &str, file: &str) {
    let lines = super::jsonl(file);
    let cases = &lines[1..];
    assert_eq!(lines[0]["cases"].as_u64().unwrap() as usize, cases.len());
    assert!(!cases.is_empty());
    let mut ledger = super::parity::Ledger::new(test, file);
    for (i, case) in cases.iter().enumerate() {
        let name = case["name"].as_str().unwrap();
        ledger.case(name, || {
            assert_eq!(ask_in_memory(case), case["a"], "{name} (in memory)");
            #[cfg(unix)]
            assert_eq!(
                ask_on_disk(case, &format!("{file}-{i}")),
                case["a"],
                "{name} (on disk)"
            );
            #[cfg(not(unix))]
            let _ = i;
        });
    }
    ledger.finish(cases.len());
}

/// A zlib stream of stored blocks of `data`, as the generator's
/// `_deflate`.
pub fn stored(data: &[u8]) -> Vec<u8> {
    let mut out = vec![0x78, 0x01];
    let pieces: Vec<&[u8]> = if data.is_empty() {
        vec![&[]]
    } else {
        data.chunks(65535).collect()
    };
    for (i, piece) in pieces.iter().enumerate() {
        out.push(u8::from(i == pieces.len() - 1));
        let n = piece.len() as u16;
        out.extend_from_slice(&n.to_le_bytes());
        out.extend_from_slice(&(!n).to_le_bytes());
        out.extend_from_slice(piece);
    }
    let (mut a, mut b) = (1u32, 0u32);
    for &c in data {
        a = (a + u32::from(c)) % 65521;
        b = (b + a) % 65521;
    }
    out.extend_from_slice(&((b << 16) | a).to_be_bytes());
    out
}

/// A loose object file's bytes.
pub fn loose_file(kind: &str, body: &[u8]) -> Vec<u8> {
    let mut data = format!("{kind} {}\0", body.len()).into_bytes();
    data.extend_from_slice(body);
    stored(&data)
}

/// A pack entry's type and size header.
pub fn entry_head(code: u8, mut size: usize) -> Vec<u8> {
    let mut out = Vec::new();
    let mut c = (code << 4) | (size & 15) as u8;
    size >>= 4;
    while size != 0 {
        out.push(c | 0x80);
        c = (size & 0x7F) as u8;
        size >>= 7;
    }
    out.push(c);
    out
}

/// A version 2 index of `(name, offset)` pairs; an offset of 2**31 or
/// more goes to the 64-bit table.
pub fn index_of(entries: &[(Vec<u8>, u64)], hash_len: usize) -> Vec<u8> {
    let mut entries = entries.to_vec();
    entries.sort();
    let mut out = b"\xfftOc\x00\x00\x00\x02".to_vec();
    for byte in 0..256usize {
        let count = entries
            .iter()
            .filter(|(n, _)| usize::from(n[0]) <= byte)
            .count() as u32;
        out.extend_from_slice(&count.to_be_bytes());
    }
    for (name, _) in &entries {
        out.extend_from_slice(name);
    }
    out.extend(std::iter::repeat_n(0, 4 * entries.len()));
    let mut table = Vec::new();
    for (_, at) in &entries {
        if *at >= 1 << 31 {
            out.extend_from_slice(&(0x8000_0000u32 | (table.len() / 8) as u32).to_be_bytes());
            table.extend_from_slice(&at.to_be_bytes());
        } else {
            out.extend_from_slice(&(*at as u32).to_be_bytes());
        }
    }
    out.extend(table);
    out.extend(std::iter::repeat_n(0, 2 * hash_len));
    out
}

/// A name of `hash_len` bytes from hex text padded with `fill`.
pub fn name_of(hex_text: &str, fill: char, hash_len: usize) -> String {
    let mut out = hex_text.to_owned();
    while out.len() < 2 * hash_len {
        out.push(fill);
    }
    out
}

/// The repository's loose path of `name`.
pub fn loose_path(objects: &str, name: &str) -> Vec<u8> {
    format!("{objects}/{}/{}", &name[..2], &name[2..]).into_bytes()
}

/// A reader of `/m/g` over `fs`.
pub fn reader_of<F: bettermemory_core::fsdata::Fs>(fs: &F) -> ObjectReader<'_, F> {
    let facts = GitFacts {
        release: Some((2, 54)),
        reads_compat: Some(true),
    };
    ObjectReader::new(fs, Flavour::Posix, b"/", facts, b"/m/g")
}

/// An empty repository in memory at `/m/g`.
pub fn empty_repo() -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(b"/m/g/objects/pack", Node::Dir);
    fs
}

/// Bytes of hex text.
pub fn unhex_text(text: &str) -> Vec<u8> {
    super::unhex(text)
}
