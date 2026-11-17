//! Every case of `gitsystem_record.jsonl`, POSIX only: the record of the
//! git on PATH's answers read in every shape, whether an answer stands,
//! the answer kept after a failed ask, and the record written, against
//! `gitsystem::record`.
//!
//! On disk every case runs as the generator runs it, writes and changes
//! included (`StdFs` reads, `std::fs` writes). In memory (`MemFs`) every
//! case that neither writes the record nor changes the git binary runs
//! too; the rest are counted and left to the disk test.

#![cfg(unix)]

mod common;

use bettermemory_core::float_repr;
use bettermemory_core::fsdata::{Flavour, Fs, MemFs, Node, StdFs};
use bettermemory_core::gitconfig::legacy::{file_stamp, Stamp};
use bettermemory_core::gitsystem::record::{
    developer, key, surrogateescape, Answer, Natural, Place, RecordedStamp, Refused, Sources,
    RECORD,
};
use common::gitconfig_file::content;
use common::jsonl;
use common::parity::Ledger;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

const FILE: &str = "gitsystem_record.jsonl";
const PYTHON_FILE: &str = "gitsystem_record_python.jsonl";
const MEM_ROOT: &str = "/<scratch>";
/// The generator's `CHANGED_AT` (`gen_location_stamped`).
const CHANGED_AT: i128 = 1_000_000_000;
/// The generator's `SHOWN`.
const SHOWN: usize = 512;

struct Fixture {
    base: Vec<u8>,
    answer: Map<String, Value>,
    out: Map<String, Value>,
}

fn fixture(file: &str) -> (Fixture, Vec<Value>) {
    let mut lines = jsonl(file);
    let header = lines.remove(0);
    assert_eq!(header["cases"].as_u64(), Some(lines.len() as u64));
    let fx = Fixture {
        base: content(&header["base"]),
        answer: header["answer"].as_object().unwrap().clone(),
        out: header["out"].as_object().unwrap().clone(),
    };
    (fx, lines)
}

/// A file's bytes as the fixture spells them: `_b`, a recipe, or the
/// base record with its middle replaced (`{"d": [i, k, mid]}`).
fn bytes_of(fx: &Fixture, value: &Value) -> Vec<u8> {
    if let Some(d) = value.get("d") {
        let i = d[0].as_u64().unwrap() as usize;
        let k = d[1].as_u64().unwrap() as usize;
        let mut out = fx.base[..i].to_vec();
        out.extend(content(&d[2]));
        out.extend_from_slice(&fx.base[fx.base.len() - k..]);
        return out;
    }
    content(value)
}

fn b_spelled(data: &[u8]) -> String {
    if data
        .iter()
        .all(|&b| (0x20..0x7f).contains(&b) && b != b'\\')
    {
        return format!("={}", std::str::from_utf8(data).unwrap());
    }
    data.iter().map(|b| format!("{b:02x}")).collect()
}

/// The generator's `_sp`.
fn sp(data: &[u8]) -> Value {
    let mut runs: Vec<(Vec<u8>, usize)> = Vec::new();
    let mut plain = Vec::new();
    let mut i = 0;
    while i < data.len() {
        let mut j = i;
        while j < data.len() && data[j] == data[i] {
            j += 1;
        }
        if j - i >= 32 {
            if !plain.is_empty() {
                runs.push((std::mem::take(&mut plain), 1));
            }
            runs.push((vec![data[i]], j - i));
        } else {
            plain.extend_from_slice(&data[i..j]);
        }
        i = j;
    }
    if runs.is_empty() {
        return json!(b_spelled(data));
    }
    if !plain.is_empty() {
        runs.push((plain, 1));
    }
    json!(runs
        .iter()
        .map(|(p, n)| json!([b_spelled(p), n]))
        .collect::<Vec<_>>())
}

/// The length of `json.dumps(value)` for the fixture's values (ASCII
/// text, whose only escape here is a quote).
fn py_len(value: &Value) -> usize {
    match value {
        Value::Null => 4,
        Value::Bool(b) => {
            if *b {
                4
            } else {
                5
            }
        }
        Value::Number(n) => n.to_string().len(),
        Value::String(s) => 2 + s.len() + s.bytes().filter(|&b| b == b'"').count(),
        Value::Array(a) => 2 + a.iter().map(py_len).sum::<usize>() + 2 * a.len().saturating_sub(1),
        Value::Object(m) => {
            2 + m
                .iter()
                .map(|(k, v)| py_len(&json!(k)) + 2 + py_len(v))
                .sum::<usize>()
                + 2 * m.len().saturating_sub(1)
        }
    }
}

/// The generator's `_spelled`.
fn spelled(fx: &Fixture, data: &[u8]) -> Value {
    let base = &fx.base;
    let mut i = 0;
    while i < data.len().min(base.len()) && data[i] == base[i] {
        i += 1;
    }
    let mut k = 0;
    while k < data.len().min(base.len()) - i && data[data.len() - 1 - k] == base[base.len() - 1 - k]
    {
        k += 1;
    }
    let diff = json!({"d": [i, k, sp(&data[i..data.len() - k])]});
    let plain = sp(data);
    if py_len(&diff) < py_len(&plain) {
        diff
    } else {
        plain
    }
}

/// `bytes.decode("utf-8", "surrogatepass")`.
fn surrogatepass_decode(mut b: &[u8]) -> Vec<u32> {
    let mut out = Vec::new();
    loop {
        match std::str::from_utf8(b) {
            Ok(s) => {
                out.extend(s.chars().map(u32::from));
                return out;
            }
            Err(e) => {
                let (good, rest) = b.split_at(e.valid_up_to());
                out.extend(std::str::from_utf8(good).unwrap().chars().map(u32::from));
                let [0xed, x @ 0xa0..=0xbf, y @ 0x80..=0xbf, ..] = rest else {
                    panic!("not surrogatepass UTF-8");
                };
                out.push(0xd000 | (u32::from(x & 0x3f) << 6) | u32::from(y & 0x3f));
                b = &rest[3..];
            }
        }
    }
}

fn stamp_text(s: &Stamp) -> String {
    format!(
        "[{}]",
        s.iter().map(i128::to_string).collect::<Vec<_>>().join(", ")
    )
}

fn replace_all(data: &[u8], from: &[u8], to: &[u8]) -> Vec<u8> {
    if from.is_empty() {
        return data.to_vec();
    }
    let mut out = Vec::with_capacity(data.len());
    let mut i = 0;
    while i < data.len() {
        if data[i..].starts_with(from) {
            out.extend_from_slice(to);
            i += from.len();
        } else {
            out.push(data[i]);
            i += 1;
        }
    }
    out
}

fn float_of(v: &Value) -> f64 {
    v.as_str().unwrap().parse::<f64>().unwrap()
}

/// What `_record` left: what stands at the record (its kind, else its
/// bytes), its mode, the state directory's mode and names.
type Left = (
    Value,
    Option<Vec<u8>>,
    Option<u32>,
    u32,
    Option<Vec<String>>,
);

/// A case's tree and its changes.
trait World {
    type F: Fs;
    fn fs(&self) -> &Self::F;
    fn root(&self) -> String;
    fn change(&mut self, kind: &str, rel: &str, data: Option<Vec<u8>>, n: i128);
    fn put(&mut self, rel: &str, data: Vec<u8>);
    /// What `_record` left: (`w`, `md`, `sd`, `ls`) before labelling.
    fn written(&self) -> Left;
}

const ENCODINGS: [(&str, usize, bool); 4] = [
    ("u16le", 2, false),
    ("u16be", 2, true),
    ("u32le", 4, false),
    ("u32be", 4, true),
];

/// The record's bytes as the case puts them: filled, then encoded.
fn filled(case: &Value, rel: &str, data: Vec<u8>, root: &str, stamp0: &Option<Stamp>) -> Vec<u8> {
    let stamp = stamp0
        .as_ref()
        .map(stamp_text)
        .unwrap_or_else(|| "null".into());
    let data = replace_all(&data, b"@ROOT@", root.as_bytes());
    let data = replace_all(&data, b"@STAMP0@", stamp.as_bytes());
    let Some(enc) = case.get("e").filter(|_| rel == format!("state/{RECORD}")) else {
        return data;
    };
    let name = enc[0].as_str().unwrap();
    let bom = enc[1].as_bool().unwrap();
    let (_, width, big) = ENCODINGS.iter().find(|e| e.0 == name).unwrap();
    let mut out = Vec::new();
    if bom {
        out.extend(match (*width, *big) {
            (2, false) => vec![0xff, 0xfe],
            (2, true) => vec![0xfe, 0xff],
            (4, false) => vec![0xff, 0xfe, 0, 0],
            _ => vec![0, 0, 0xfe, 0xff],
        });
    }
    for &b in &data {
        let mut unit = vec![0u8; *width];
        if *big {
            unit[width - 1] = b;
        } else {
            unit[0] = b;
        }
        out.extend(unit);
    }
    out
}

fn files_of(case: &Value) -> Vec<(String, Value)> {
    let mut out = vec![("bin/git".to_owned(), json!("6769740a"))];
    if let Some(files) = case.get("f").and_then(Value::as_object) {
        for (rel, data) in files {
            if rel == "bin/git" {
                out[0].1 = data.clone();
            } else {
                out.push((rel.clone(), data.clone()));
            }
        }
    }
    out
}

fn strings(case: &Value, key: &str) -> Vec<String> {
    case.get(key)
        .and_then(Value::as_array)
        .map(|a| a.iter().map(|v| v.as_str().unwrap().to_owned()).collect())
        .unwrap_or_default()
}

fn modes(case: &Value) -> Vec<(String, u32)> {
    case.get("m")
        .and_then(Value::as_object)
        .map(|m| {
            m.iter()
                .map(|(k, v)| {
                    (
                        k.clone(),
                        u32::from_str_radix(v.as_str().unwrap(), 8).unwrap(),
                    )
                })
                .collect()
        })
        .unwrap_or_default()
}

struct Memory {
    fs: MemFs,
}

impl Memory {
    fn new(fx: &Fixture, case: &Value) -> Memory {
        let mut fs = MemFs::new(Flavour::Posix);
        let at = |rel: &str| format!("{MEM_ROOT}/{rel}").into_bytes();
        fs.insert(MEM_ROOT.as_bytes(), Node::Dir);
        fs.insert(&at("state"), Node::Dir);
        fs.insert(&at("bin"), Node::Dir);
        for rel in strings(case, "d") {
            fs.insert(&at(&rel), Node::Dir);
        }
        let files = files_of(case);
        fs.insert(&at("bin/git"), Node::File(bytes_of(fx, &files[0].1)));
        for (rel, m) in modes(case) {
            if rel == "bin/git" {
                fs.set_mode(&at(&rel), m);
            }
        }
        let stamp0 = file_stamp(&fs, &at("bin/git")).unwrap();
        for (rel, data) in &files[1..] {
            let data = filled(case, rel, bytes_of(fx, data), MEM_ROOT, &stamp0);
            fs.insert(&at(rel), Node::File(data));
        }
        if let Some(links) = case.get("l").and_then(Value::as_object) {
            for (rel, target) in links {
                let t = target
                    .as_str()
                    .unwrap()
                    .replacen("@/", &format!("{MEM_ROOT}/"), 1);
                fs.insert(&at(rel), Node::Link(t.into_bytes()));
            }
        }
        for (rel, m) in modes(case) {
            if rel != "bin/git" {
                fs.set_mode(&at(&rel), m);
            }
        }
        Memory { fs }
    }
}

impl World for Memory {
    type F = MemFs;
    fn fs(&self) -> &MemFs {
        &self.fs
    }
    fn root(&self) -> String {
        MEM_ROOT.to_owned()
    }
    fn change(&mut self, _: &str, _: &str, _: Option<Vec<u8>>, _: i128) {
        unreachable!("changes run on disk only")
    }
    fn put(&mut self, rel: &str, data: Vec<u8>) {
        self.fs
            .insert(format!("{MEM_ROOT}/{rel}").as_bytes(), Node::File(data));
    }
    fn written(&self) -> Left {
        unreachable!("writes run on disk only")
    }
}

struct Disk {
    root: std::path::PathBuf,
}

fn mkfifo(path: &std::path::Path) {
    let made = std::process::Command::new("mkfifo")
        .arg(path)
        .status()
        .is_ok_and(|s| s.success());
    assert!(made, "mkfifo could not run");
}

fn set_mode(p: &std::path::Path, m: u32) {
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(p, std::fs::Permissions::from_mode(m)).unwrap();
}

/// As `golden_location_stamped`'s: again until the change time moves.
fn until_ctime_moves(path: &std::path::Path, apply: impl Fn()) {
    use std::os::unix::fs::MetadataExt;
    let ctime = |p: &std::path::Path| {
        let m = std::fs::metadata(p).unwrap();
        (m.ctime(), m.ctime_nsec())
    };
    let before = ctime(path);
    let started = std::time::Instant::now();
    apply();
    while ctime(path) == before {
        assert!(
            started.elapsed() < std::time::Duration::from_secs(5),
            "the change time of {} did not move",
            path.display()
        );
        std::thread::sleep(std::time::Duration::from_millis(2));
        apply();
    }
}

/// As `golden_location_stamped`'s `set_time`.
fn set_time(path: &std::path::Path, n: i128) {
    use std::os::unix::fs::PermissionsExt;
    let at = std::time::UNIX_EPOCH + std::time::Duration::from_secs((CHANGED_AT + n) as u64);
    let times = std::fs::FileTimes::new().set_accessed(at).set_modified(at);
    let mode = std::fs::metadata(path).unwrap().permissions().mode() & 0o7777;
    let opened_up = mode & 0o200 == 0;
    if opened_up {
        set_mode(path, 0o600);
    }
    let file = std::fs::OpenOptions::new().write(true).open(path).unwrap();
    file.set_times(times).unwrap();
    drop(file);
    if opened_up {
        set_mode(path, mode);
    }
}

impl Disk {
    fn new(fx: &Fixture, case: &Value, root: std::path::PathBuf) -> Disk {
        let root_text = root.to_str().unwrap().to_owned();
        std::fs::create_dir_all(root.join("state")).unwrap();
        for rel in strings(case, "d") {
            std::fs::create_dir_all(root.join(rel)).unwrap();
        }
        let files = files_of(case);
        for (rel, _) in &files {
            let path = root.join(rel);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(&path, b"").unwrap();
        }
        if let Some(links) = case.get("l").and_then(Value::as_object) {
            for (rel, target) in links {
                let path = root.join(rel);
                std::fs::create_dir_all(path.parent().unwrap()).unwrap();
                let t = target
                    .as_str()
                    .unwrap()
                    .replacen("@/", &format!("{root_text}/"), 1);
                std::os::unix::fs::symlink(t, &path).unwrap();
            }
        }
        fn walk(p: &std::path::Path) {
            for entry in std::fs::read_dir(p).unwrap().flatten() {
                let path = entry.path();
                let kind = entry.file_type().unwrap();
                if kind.is_symlink() {
                    continue;
                }
                if kind.is_dir() {
                    walk(&path);
                    set_mode(&path, 0o755);
                } else {
                    set_mode(&path, 0o644);
                }
            }
        }
        walk(&root);
        let git = root.join("bin/git");
        std::fs::write(&git, bytes_of(fx, &files[0].1)).unwrap();
        for (rel, m) in modes(case) {
            if rel == "bin/git" {
                set_mode(&root.join(rel), m);
            }
        }
        let stamp0 = file_stamp(&StdFs, git.to_str().unwrap().as_bytes()).unwrap();
        for (rel, data) in &files[1..] {
            let data = filled(case, rel, bytes_of(fx, data), &root_text, &stamp0);
            std::fs::write(root.join(rel), data).unwrap();
        }
        for (rel, m) in modes(case) {
            if rel != "bin/git" {
                set_mode(&root.join(rel), m);
            }
        }
        Disk { root }
    }
}

impl World for Disk {
    type F = StdFs;
    fn fs(&self) -> &StdFs {
        &StdFs
    }
    fn root(&self) -> String {
        self.root.to_str().unwrap().to_owned()
    }
    fn change(&mut self, kind: &str, rel: &str, data: Option<Vec<u8>>, n: i128) {
        let path = self.root.join(rel);
        match kind {
            "write" => {
                std::fs::write(&path, data.unwrap()).unwrap();
                set_time(&path, n);
            }
            "mtime" => set_time(&path, n),
            "rename" => {
                let new = path.with_file_name(format!(
                    "{}.new",
                    path.file_name().unwrap().to_str().unwrap()
                ));
                std::fs::write(&new, data.unwrap()).unwrap();
                set_mode(&new, 0o644);
                set_time(&new, n);
                std::fs::rename(&new, &path).unwrap();
            }
            "mode" => {
                let m = std::str::from_utf8(&data.unwrap_or(b"600".to_vec()))
                    .ok()
                    .and_then(|m| u32::from_str_radix(m, 8).ok())
                    .unwrap();
                until_ctime_moves(&path, || set_mode(&path, m));
            }
            "remove" => std::fs::remove_file(&path).unwrap(),
            "dir" => {
                std::fs::remove_file(&path).unwrap();
                std::fs::create_dir(&path).unwrap();
                set_mode(&path, 0o755);
            }
            "fifo" => {
                std::fs::remove_file(&path).unwrap();
                mkfifo(&path);
                set_mode(&path, 0o644);
            }
            other => panic!("no change {other}"),
        }
    }
    fn put(&mut self, rel: &str, data: Vec<u8>) {
        std::fs::write(self.root.join(rel), data).unwrap();
    }
    fn written(&self) -> Left {
        use std::os::unix::fs::PermissionsExt;
        let state = self.root.join("state");
        let path = state.join(RECORD);
        let (w, data, md) = match std::fs::symlink_metadata(&path) {
            Err(_) => (Value::Null, None, None),
            Ok(m) => {
                let mode = Some(m.permissions().mode() & 0o7777);
                if m.file_type().is_symlink() {
                    (json!("link"), None, mode)
                } else if m.is_dir() {
                    (json!("dir"), None, mode)
                } else {
                    (Value::Null, Some(std::fs::read(&path).unwrap()), mode)
                }
            }
        };
        let sd = std::fs::symlink_metadata(&state)
            .unwrap()
            .permissions()
            .mode()
            & 0o7777;
        let ls = std::fs::read_dir(&state).ok().map(|d| {
            let mut names: Vec<String> = d
                .flatten()
                .map(|e| e.file_name().to_str().unwrap().to_owned())
                .collect();
            names.sort();
            names
        });
        (w, data, md, sd, ls)
    }
}

/// The steps of one case, answered in the fixture's spelling.
fn answer<W: World>(world: &mut W, fx: &Fixture, case: &Value) -> Value {
    let root = world.root();
    let git = format!("{root}/bin/git");
    let state = format!("{root}/state");
    let mut seen: Vec<Option<Stamp>> = vec![file_stamp(world.fs(), git.as_bytes()).unwrap()];
    let mut out = Vec::new();
    let mut changes = 0;
    for step in case["s"].as_array().unwrap() {
        let kind = step[0].as_str().unwrap();
        let txt = |v: &Value| -> Option<Vec<u32>> {
            if v.is_null() {
                return None;
            }
            let text = surrogatepass_decode(&content(v));
            Some(
                match text.strip_prefix(&[u32::from(b'@'), u32::from(b'/')][..]) {
                    Some(rest) => {
                        let mut t: Vec<u32> = format!("{root}/").chars().map(u32::from).collect();
                        t.extend_from_slice(rest);
                        t
                    }
                    None => text,
                },
            )
        };
        let lab = |t: &Option<Vec<u32>>| -> Value {
            let Some(t) = t else {
                return Value::Null;
            };
            let prefix: Vec<u32> = format!("{root}/").chars().map(u32::from).collect();
            let t = match t.strip_prefix(&prefix[..]) {
                Some(rest) => {
                    let mut out = vec![u32::from(b'@'), u32::from(b'/')];
                    out.extend_from_slice(rest);
                    out
                }
                None => t.clone(),
            };
            sp(&bettermemory_core::gitsystem::record::surrogatepass(&t))
        };
        let mut git_now = |fs: &W::F| -> Option<Stamp> {
            let st = file_stamp(fs, git.as_bytes()).unwrap();
            if !seen.contains(&st) {
                seen.push(st);
            }
            st
        };
        let mut answer_in = |over: &Value, fs: &W::F| -> Answer {
            let mut spec = fx.answer.clone();
            for (k, v) in over.as_object().unwrap() {
                spec.insert(k.clone(), v.clone());
            }
            let stamp: Option<RecordedStamp> = match &spec["st"] {
                Value::Null => None,
                Value::String(s) if s == "<git>" => git_now(fs).map(|s| s.map(|x| x.to_string())),
                Value::String(s) if s.starts_with("<git~") => {
                    // `bin/git`'s stamp now with one part moved by one.
                    let moved = usize::from(s.as_bytes()[5] - b'0');
                    let now = git_now(fs).expect("a git to stamp");
                    Some(std::array::from_fn(|i| {
                        (now[i] + i128::from(i == moved)).to_string()
                    }))
                }
                Value::Array(a) => Some(std::array::from_fn(|i| a[i].as_str().unwrap().to_owned())),
                other => panic!("a stamp {other}"),
            };
            let flag = |k: &str| spec[k].as_bool();
            Answer {
                key: spec["k"].as_str().unwrap().to_owned(),
                git: txt(&spec["g"]),
                stamp,
                asked: float_of(&spec["a"]),
                ok: spec["ok"].as_bool().unwrap(),
                sources: Sources {
                    own: spec["o"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|o| txt(o).unwrap())
                        .collect(),
                    system: txt(&spec["sy"]),
                    legacy: flag("lg"),
                    resets: flag("rs"),
                    prefix: txt(&spec["px"]),
                    compat_differ: flag("cd"),
                    hasconfig: flag("hc"),
                    version: spec["v"].as_array().map(|v| {
                        std::array::from_fn(|i| Natural::from(v[i].as_u64().unwrap() as u32))
                    }),
                },
                tried: spec["t"].as_str().map(|t| t.parse().unwrap()),
            }
        };
        let place = Place {
            fs: world.fs(),
            flavour: Flavour::Posix,
            state_dir: state.as_bytes(),
            cwd: root.as_bytes(),
        };
        let answer_out = |a: &Option<Answer>, seen: &[Option<Stamp>]| -> Value {
            let Some(a) = a else {
                return Value::Null;
            };
            let st = match &a.stamp {
                None => Value::Null,
                Some(s) => match seen.iter().position(|x| {
                    x.as_ref()
                        .is_some_and(|x| x.iter().zip(s).all(|(p, q)| p.to_string() == *q))
                }) {
                    Some(i) => json!(format!("<git:{i}>")),
                    None => json!(s),
                },
            };
            let src = &a.sources;
            let full = json!({
                "k": a.key,
                "g": lab(&a.git),
                "st": st,
                "a": float_repr::repr(a.asked),
                "ok": a.ok,
                "o": src.own.iter().map(|o| lab(&Some(o.clone()))).collect::<Vec<_>>(),
                "sy": lab(&src.system),
                "lg": src.legacy,
                "rs": src.resets,
                "px": lab(&src.prefix),
                "cd": src.compat_differ,
                "hc": src.hasconfig,
                "v": src.version.as_ref().map(|v| v.iter().map(|p| p.as_str().parse::<u64>().unwrap()).collect::<Vec<_>>()),
                "t": a.tried.map(float_repr::repr),
            });
            let mut diff = Map::new();
            for (k, v) in full.as_object().unwrap() {
                if fx.out.get(k) != Some(v) {
                    diff.insert(k.clone(), v.clone());
                }
            }
            Value::Object(diff)
        };
        let raised = |r: Refused| -> Value {
            match r {
                Refused::Raises(e) => json!({"x": e.name()}),
                Refused::NotPorted => panic!("a stamp not ported on POSIX"),
            }
        };
        let got = match kind {
            "recorded" => match place.recorded(step[1].as_str().unwrap()) {
                Ok(a) => json!({"a": answer_out(&a, &seen)}),
                Err(r) => raised(r),
            },
            "current" => {
                let a = answer_in(&step[1], world.fs());
                match place.current(&a, float_of(&step[2]), step[3].as_bool().unwrap()) {
                    Ok(c) => json!({"c": c}),
                    Err(r) => raised(r),
                }
            }
            "kept" => {
                let given: Vec<Option<Answer>> = step[3]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|s| (!s.is_null()).then(|| answer_in(s, world.fs())))
                    .collect();
                let refs: Vec<Option<&Answer>> = given.iter().map(Option::as_ref).collect();
                match place.kept(step[1].as_str().unwrap(), float_of(&step[2]), &refs) {
                    Ok(a) => json!({"a": answer_out(&a, &seen)}),
                    Err(r) => raised(r),
                }
            }
            "record" => {
                let a = answer_in(&step[1], world.fs());
                match place.record(&a) {
                    Err(r) => raised(r),
                    Ok(_) => {
                        let (w, data, md, sd, ls) = world.written();
                        let mut got = json!({"w": w, "sd": format!("{sd:03o}"), "ls": ls});
                        if let Some(md) = md {
                            got["md"] = json!(format!("{md:03o}"));
                        }
                        if let Some(data) = data {
                            let mut data = replace_all(&data, root.as_bytes(), b"@ROOT@");
                            for (i, s) in seen.iter().enumerate() {
                                if let Some(s) = s {
                                    data = replace_all(
                                        &data,
                                        stamp_text(s).as_bytes(),
                                        format!("@STAMP{i}@").as_bytes(),
                                    );
                                }
                            }
                            got["w"] = if data.len() > SHOWN {
                                let h: String = Sha256::digest(&data)
                                    .iter()
                                    .map(|b| format!("{b:02x}"))
                                    .collect();
                                json!({"n": data.len(), "sha256": h})
                            } else {
                                spelled(fx, &data)
                            };
                        }
                        got
                    }
                }
            }
            "key" => {
                let path = (!step[1].is_null()).then(|| surrogateescape(&content(&step[1])));
                let dev = (!step[3].is_null()).then(|| surrogateescape(&content(&step[3])));
                let link = format!("{root}/xcode");
                let d = developer(
                    world.fs(),
                    step[2].as_bool().unwrap(),
                    dev.as_deref(),
                    link.as_bytes(),
                );
                json!({"k": key(path.as_deref(), &d)})
            }
            "change" => {
                let data = (!step[3].is_null()).then(|| content(&step[3]));
                world.change(
                    step[1].as_str().unwrap(),
                    step[2].as_str().unwrap(),
                    data,
                    changes,
                );
                changes += 1;
                json!({})
            }
            "put" => {
                let rel = step[1].as_str().unwrap();
                let s0 = seen[0];
                let data = filled(case, "", bytes_of(fx, &step[2]), &root, &s0);
                world.put(rel, data);
                json!({})
            }
            other => panic!("no step {other}"),
        };
        out.push(got);
    }
    json!(out)
}

fn writes(case: &Value) -> bool {
    case["s"]
        .as_array()
        .unwrap()
        .iter()
        .any(|s| matches!(s[0].as_str(), Some("record" | "change")))
}

fn in_memory(test: &str, file: &str) {
    let (fx, cases) = fixture(file);
    let ledger = Ledger::new(test, file);
    let mut failures = Vec::new();
    let mut ran = 0;
    for (i, case) in cases.iter().enumerate() {
        if writes(case) {
            continue;
        }
        ran += 1;
        let mut world = Memory::new(&fx, case);
        let got = answer(&mut world, &fx, case);
        if got != case["r"] {
            failures.push(format!("#{i}: native {got} reference {}", case["r"]));
        }
    }
    // Failures first: a count that falls short must not hide them.
    ledger.settle(ran, &failures);
    assert!(
        ran > cases.len() / 2,
        "only {ran} of {} cases ran in memory",
        cases.len()
    );
}

#[test]
fn gitsystem_record_every_case_in_memory() {
    in_memory("golden_gitsystem_record", FILE);
}

#[test]
fn gitsystem_record_python_every_case_in_memory() {
    in_memory("golden_gitsystem_record_python", PYTHON_FILE);
}

struct Scratch(std::path::PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        use std::os::unix::fs::PermissionsExt;
        fn open_up(dir: &std::path::Path) {
            let Ok(entries) = std::fs::read_dir(dir) else {
                return;
            };
            for entry in entries.flatten() {
                let path = entry.path();
                let meta = std::fs::symlink_metadata(&path);
                if meta.as_ref().is_ok_and(|m| m.file_type().is_symlink()) {
                    continue;
                }
                let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700));
                if meta.is_ok_and(|m| m.is_dir()) {
                    open_up(&path);
                }
            }
        }
        open_up(&self.0);
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn fresh_dir() -> std::path::PathBuf {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("bm-gitsysrec-{}-{n}", std::process::id()));
    std::fs::create_dir(&dir).expect("a new scratch directory");
    std::fs::canonicalize(&dir).expect("a resolved scratch directory")
}

fn refuse_root(base: &std::path::Path) {
    let probe = base.join("probe");
    std::fs::create_dir(&probe).unwrap();
    set_mode(&probe, 0o000);
    let listed = std::fs::read_dir(&probe).is_ok();
    set_mode(&probe, 0o700);
    std::fs::remove_dir(&probe).unwrap();
    assert!(
        !listed,
        "a mode-000 folder can be listed here (running as root?)"
    );
}

fn on_disk(test: &str, file: &str) {
    let (fx, cases) = fixture(file);
    let ledger = Ledger::new(test, file);
    let base = Scratch(fresh_dir());
    refuse_root(&base.0);
    let mut failures = Vec::new();
    for (i, case) in cases.iter().enumerate() {
        let dir = base.0.join(i.to_string());
        std::fs::create_dir(&dir).unwrap();
        set_mode(&dir, 0o755);
        let mut world = Disk::new(&fx, case, dir);
        let got = answer(&mut world, &fx, case);
        if got != case["r"] {
            failures.push(format!("#{i}: native {got} reference {}", case["r"]));
        }
    }
    drop(base);
    ledger.settle(cases.len(), &failures);
}

#[test]
fn gitsystem_record_every_case_on_disk() {
    on_disk("golden_gitsystem_record_stdfs", FILE);
}

#[test]
fn gitsystem_record_python_every_case_on_disk() {
    on_disk("golden_gitsystem_record_python_stdfs", PYTHON_FILE);
}
