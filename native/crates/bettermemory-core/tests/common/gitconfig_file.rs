//! A `gitconfig_file.jsonl` case as data, and the native answers to it in
//! the fixture's spelling, over any [`Fs`].

use bettermemory_core::fsdata::{Flavour, Fs, Kind, MemFs, Node};
use bettermemory_core::gitconfig::file::{config_bytes, read_git_config, ConfigBytes};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// The recorded root of every case's paths.
pub const ROOT: &str = "<scratch>";

fn spelled(data: &[u8]) -> String {
    if data
        .iter()
        .all(|&b| (0x20..0x7f).contains(&b) && b != b'\\')
    {
        let mut out = String::from("=");
        out.push_str(std::str::from_utf8(data).expect("ASCII"));
        return out;
    }
    data.iter().map(|b| format!("{b:02x}")).collect()
}

fn unspelled(text: &str) -> Vec<u8> {
    match text.strip_prefix('=') {
        Some(rest) => rest.as_bytes().to_vec(),
        None => super::unhex(text),
    }
}

/// A file's content: bytes as spelled, or a recipe of pieces and counts.
pub fn content(value: &Value) -> Vec<u8> {
    if let Some(text) = value.as_str() {
        return unspelled(text);
    }
    let mut out = Vec::new();
    for pair in value.as_array().expect("a recipe") {
        let piece = unspelled(pair[0].as_str().expect("a piece"));
        let count = pair[1].as_u64().expect("a count") as usize;
        for _ in 0..count {
            out.extend_from_slice(&piece);
        }
    }
    out
}

fn strings(case: &Value, key: &str) -> Vec<String> {
    case.get(key)
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .map(|v| v.as_str().expect("a path").to_owned())
                .collect()
        })
        .unwrap_or_default()
}

/// The case's modes, path to owner bits, shallowest first.
pub fn modes(case: &Value) -> Vec<(String, u32)> {
    let mut out: Vec<(String, u32)> = case
        .get("m")
        .and_then(Value::as_object)
        .map(|m| {
            m.iter()
                .map(|(k, v)| {
                    let bits = u32::from_str_radix(v.as_str().unwrap(), 8).expect("octal");
                    (k.clone(), bits)
                })
                .collect()
        })
        .unwrap_or_default();
    out.sort_by_key(|(k, _)| k.matches('/').count());
    out
}

/// The case's tree in memory, rooted at [`ROOT`].
pub fn world(case: &Value, flavour: Flavour) -> MemFs {
    let mut fs = MemFs::new(flavour);
    let at = |rel: &str| format!("{ROOT}/{rel}").into_bytes();
    fs.insert(ROOT.as_bytes(), Node::Dir);
    for (rel, data) in case["f"].as_object().expect("files") {
        fs.insert(&at(rel), Node::File(content(data)));
    }
    for rel in strings(case, "d") {
        fs.insert(&at(&rel), Node::Dir);
    }
    if let Some(links) = case.get("l").and_then(Value::as_object) {
        for (rel, target) in links {
            fs.insert(
                &at(rel),
                Node::Link(target.as_str().unwrap().as_bytes().to_vec()),
            );
        }
    }
    for rel in strings(case, "p") {
        fs.insert(&at(&rel), Node::Fifo);
    }
    if let Some(hard) = case.get("h").and_then(Value::as_object) {
        for (rel, existing) in hard {
            fs.insert(&at(rel), Node::Hard(at(existing.as_str().unwrap())));
        }
    }
    for (rel, mode) in modes(case) {
        fs.set_mode(&at(&rel), mode);
    }
    fs.insert(b"/dev/null", Node::Device);
    fs.insert(b"/dev/zero", Node::Endless);
    fs
}

/// The second path whose identity the case compares, under `root`.
pub fn same(case: &Value, root: &str) -> Option<Vec<u8>> {
    case.get("s")
        .and_then(Value::as_str)
        .map(|s| format!("{root}/{s}").into_bytes())
}

/// The path the case reads, under `root`.
pub fn target(case: &Value, root: &str) -> Vec<u8> {
    if case.get("e").is_some() {
        // The empty path itself.
        return Vec::new();
    }
    format!("{root}/{}", case["t"].as_str().expect("a target")).into_bytes()
}

fn shown(data: &[u8]) -> Value {
    if data.len() > 256 {
        let digest: String = Sha256::digest(data)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        return json!({"n": data.len(), "sha256": digest});
    }
    json!(spelled(data))
}

fn kind(kind: Kind) -> &'static str {
    match kind {
        Kind::Regular => "reg",
        Kind::Directory => "dir",
        Kind::Fifo => "fifo",
        Kind::Socket => "sock",
        Kind::Link => "link",
        Kind::Other => "other",
    }
}

/// The native answers, in the fixture's spelling; `label` turns a
/// reason's bytes into the recorded text (the root relabelled).
pub fn answers<F: Fs>(
    fs: &F,
    path: &[u8],
    same: Option<&[u8]>,
    label: &dyn Fn(&[u8]) -> String,
) -> Value {
    let entries = |names: Option<&[Vec<u8>]>| {
        let read = read_git_config(fs, path, names);
        match read.why_text() {
            Some(why) => json!({"why": label(&why)}),
            None => Value::Array(
                read.entries
                    .iter()
                    .map(|e| json!([spelled(&e.name()), e.value.as_deref().map(spelled)]))
                    .collect(),
            ),
        }
    };
    let names = [
        b"core.repositoryformatversion".to_vec(),
        b"extensions.".to_vec(),
    ];
    let mut out = serde_json::Map::new();
    let rg = entries(None);
    let rn = entries(Some(&names));
    if rn != rg {
        out.insert("rn".into(), rn);
    }
    out.insert("rg".into(), rg);
    let bytes = |gentle| match config_bytes(fs, path, gentle) {
        ConfigBytes::Skipped => Value::Null,
        ConfigBytes::Read { data, identity } => {
            json!({"b": shown(&data), "id": identity.is_some()})
        }
        ConfigBytes::Dies(dies) => json!({"dies": label(&dies.why())}),
        ConfigBytes::Overflow { path } => json!({"over": label(&path)}),
    };
    if let Some(other) = same {
        let identity = |at: &[u8]| match config_bytes(fs, at, false) {
            ConfigBytes::Read { identity, .. } => identity,
            _ => None,
        };
        let si = match (identity(path), identity(other)) {
            (Some(a), Some(b)) => json!(a == b),
            _ => Value::Null,
        };
        out.insert("si".into(), si);
    }
    let cb = bytes(false);
    let cg = bytes(true);
    if cg != cb {
        out.insert("cg".into(), cg);
    }
    out.insert("cb".into(), cb);
    let fk = match fs.open(path) {
        Err(e) => e.name().to_owned(),
        Ok(mut file) => match fs.fstat(&mut file) {
            Ok(stat) => kind(stat.kind).to_owned(),
            Err(e) => format!("fstat {}", e.name()),
        },
    };
    out.insert("fk".into(), json!(fk));
    out.insert("ac".into(), json!(fs.access_read(path)));
    let ls = match fs.lstat(path) {
        Ok(k) => kind(k).to_owned(),
        Err(e) => e.name().to_owned(),
    };
    out.insert("ls".into(), json!(ls));
    Value::Object(out)
}

/// A reason's bytes as text, the case's root written `<scratch>`.
pub fn relabel(root: &str) -> impl Fn(&[u8]) -> String + '_ {
    move |bytes: &[u8]| {
        let text = String::from_utf8(bytes.to_vec()).expect("a UTF-8 reason");
        // Every occurrence, as the generator relabels.
        text.replace(root, ROOT)
    }
}
