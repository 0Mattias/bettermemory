//! The cases of `githead.jsonl` and `githead_posix.jsonl` as file systems
//! in memory, and the native answers in the fixture's spelling.

use bettermemory_core::githead::{self, fs::Node, Flavour, GitDir, MemFs, Stop};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

const SAFE: &[u8] =
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .,=_:-+*~%()[]#;!@'/{}^?&$\n\r\t";

/// A fixture byte string: `=` and itself, or hex.
pub fn bytes_of(text: &str) -> Vec<u8> {
    match text.strip_prefix('=') {
        Some(plain) => plain.as_bytes().to_vec(),
        None => super::unhex(text),
    }
}

/// `_b`: bytes as the fixture writes them.
pub fn written(bytes: &[u8]) -> String {
    if bytes.iter().all(|b| SAFE.contains(b)) {
        format!("={}", String::from_utf8(bytes.to_vec()).expect("ASCII"))
    } else {
        super::hex(bytes)
    }
}

fn content(value: &Value) -> Vec<u8> {
    if let Some(text) = value.as_str() {
        return bytes_of(text);
    }
    let mut out = Vec::new();
    for piece in value.as_array().expect("a recipe") {
        let bytes = bytes_of(piece[0].as_str().expect("a piece"));
        out.extend_from_slice(&bytes.repeat(piece[1].as_u64().expect("a count") as usize));
    }
    out
}

/// The case's file system under `flavour`, and its git directory.
pub fn world(case: &Value, flavour: Flavour) -> (MemFs, GitDir) {
    let mut fs = MemFs::new(flavour);
    fs.insert(b"g", Node::Dir);
    let paths = |key: &str| -> Vec<String> {
        case.get(key)
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .map(|p| p.as_str().expect("a path").to_owned())
                    .collect()
            })
            .unwrap_or_default()
    };
    for dir in paths("d") {
        fs.insert(dir.as_bytes(), Node::Dir);
    }
    for fifo in paths("p") {
        fs.insert(fifo.as_bytes(), Node::Special);
    }
    for (path, data) in case["f"].as_object().expect("files") {
        fs.insert(path.as_bytes(), Node::File(content(data)));
    }
    if let Some(links) = case.get("l").and_then(Value::as_object) {
        for (path, target) in links {
            let target = target.as_str().expect("a target").as_bytes().to_vec();
            fs.insert(path.as_bytes(), Node::Link(target));
        }
    }
    let gitdir: &[u8] = if case["w"] == 1 {
        b"g/worktrees/w"
    } else {
        b"g"
    };
    let gd = GitDir {
        gitdir: gitdir.to_vec(),
        commondir: b"g".to_vec(),
    };
    (fs, gd)
}

fn shown(data: &[u8]) -> Value {
    if data.len() > 256 {
        let digest: String = Sha256::digest(data)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        json!({"n": data.len(), "sha256": digest})
    } else {
        json!(written(data))
    }
}

/// The native answers of `case`, as the fixture's `r` spells them.
pub fn answers(case: &Value, flavour: Flavour) -> Value {
    let (fs, gd) = world(case, flavour);
    let mut out = Map::new();
    let head = githead::head_bytes(&fs, &gd);
    let written_head = case["f"]
        .get(std::str::from_utf8(&gd.gitdir).expect("ASCII").to_owned() + "/HEAD")
        .and_then(Value::as_str)
        .map(bytes_of);
    if head.is_none() || head != written_head {
        out.insert("b".into(), head.as_deref().map_or(Value::Null, shown));
    }
    let resolved = match githead::resolve(&fs, &gd) {
        Ok((name, sha)) => json!([name.as_deref().map(written), sha]),
        Err(Stop::Unsettled) => json!("x"),
        Err(Stop::Os) => json!("o"),
    };
    let sha = githead::head_sha(&fs, &gd);
    let name = githead::head_ref(&fs, &gd);
    if let Some(sha) = &sha {
        assert_eq!(resolved[1], json!(sha), "head_sha is _resolve's hash");
    }
    if let Some(name) = &name {
        assert_eq!(
            resolved[0],
            json!(written(name.as_bytes())),
            "head_ref is _resolve's name"
        );
    }
    out.insert("v".into(), resolved.clone());
    out.insert(
        "sn".into(),
        json!(format!(
            "{}{}",
            u8::from(sha.is_some()),
            u8::from(name.is_some())
        )),
    );
    out.insert("u".into(), json!(githead::head_unborn(&fs, &gd)));
    let queries: Vec<Vec<u8>> = match case.get("q").and_then(Value::as_array) {
        Some(q) => q
            .iter()
            .map(|n| bytes_of(n.as_str().expect("a name")))
            .collect(),
        None => vec![b"refs/heads/main".to_vec()],
    };
    let named: String = queries
        .iter()
        .map(|n| {
            if githead::packed_names(&fs, &gd, n) {
                '1'
            } else {
                '0'
            }
        })
        .collect();
    out.insert("p".into(), json!(named));
    // `head_commit` to its first object read, as the fixture's `c`: the
    // kind, its hash held to `_resolve`'s as the generator holds it.
    let (kind, hash) = match githead::head_commit(&fs, &gd) {
        githead::HeadCommit::Answer(h) => (json!("a"), Some(h)),
        githead::HeadCommit::Peel(h) => (json!("p"), Some(h)),
        githead::HeadCommit::Decline => (Value::Null, None),
    };
    if let Some(hash) = hash {
        assert_eq!(resolved[1], json!(hash), "head_commit's hash is _resolve's");
    }
    out.insert("c".into(), kind);
    Value::Object(out)
}
