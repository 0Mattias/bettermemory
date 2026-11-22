//! The cases of `symbols.jsonl` and `symbols_platform.jsonl`
//! (`gen_symbols`): their trees, citations, roots and the names
//! `ast.parse` gave each file, replayed by the text's digest.

use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};

use bettermemory_core::fsdata::{Flavour, Fs, MemFs, Node};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::symbols::{
    check_symbol_citations, Anchor, Declined, Machine, ModuleNames, NameSets, SymbolDriftReport,
};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

/// A text: a string, or a list of code points where it holds a lone
/// surrogate.
pub fn text(value: &Value) -> PyStr {
    match value {
        Value::String(s) => s.chars().map(u32::from).collect(),
        Value::Array(list) => list.iter().map(|v| v.as_u64().unwrap() as u32).collect(),
        other => panic!("not a text: {other}"),
    }
}

/// A text as the fixture writes it.
pub fn spelled(text: &[u32]) -> Value {
    let chars: Option<String> = text.iter().map(|&c| char::from_u32(c)).collect();
    match chars {
        Some(s) => Value::String(s),
        None => json!(text),
    }
}

/// A file's bytes, from its spelling.
pub fn content(spec: &Value) -> Vec<u8> {
    match spec {
        Value::String(s) => s.as_bytes().to_vec(),
        Value::Object(map) if map.contains_key("x") => super::unhex(map["x"].as_str().unwrap()),
        Value::Object(map) => {
            let mut out = map["head"].as_str().unwrap_or("").as_bytes().to_vec();
            let rep = map["rep"].as_str().unwrap().as_bytes();
            for _ in 0..map["n"].as_u64().unwrap() {
                out.extend_from_slice(rep);
            }
            out
        }
        other => panic!("not a file: {other}"),
    }
}

/// `ast.parse` and the two name walks as the reference answered them,
/// looked up by the SHA-256 of the text in UTF-8. A text no file of the
/// case decodes to is a miss.
pub struct Replay {
    by_digest: BTreeMap<String, Option<NameSets>>,
    pub misses: RefCell<usize>,
    pub calls: RefCell<usize>,
}

fn names_of(value: &Value) -> Option<NameSets> {
    let list = value.as_array()?;
    let set = |v: &Value| -> BTreeSet<PyStr> { v.as_array().unwrap().iter().map(text).collect() };
    Some((set(&list[0]), set(&list[1])))
}

impl Replay {
    pub fn of(case: &Value) -> Replay {
        let mut by_digest = BTreeMap::new();
        if let Some(Value::Object(map)) = case.get("n") {
            for entry in map.values() {
                if entry.is_null() {
                    continue;
                }
                by_digest.insert(
                    entry["h"].as_str().unwrap().to_owned(),
                    names_of(&entry["v"]),
                );
            }
        }
        Replay {
            by_digest,
            misses: RefCell::new(0),
            calls: RefCell::new(0),
        }
    }
}

impl ModuleNames for Replay {
    fn names(&self, source: &[u32]) -> Option<NameSets> {
        *self.calls.borrow_mut() += 1;
        let utf8: String = source
            .iter()
            .map(|&c| char::from_u32(c).expect("decoded text holds no surrogate"))
            .collect();
        let digest = super::hex(&Sha256::digest(utf8.as_bytes()));
        match self.by_digest.get(&digest) {
            Some(names) => names.clone(),
            None => {
                *self.misses.borrow_mut() += 1;
                None
            }
        }
    }
}

/// A root spelled with `@` for the case's directory.
pub fn root_of(value: &Value, here: &str) -> PyStr {
    let spelled = text(value);
    if spelled.first() == Some(&u32::from('@')) {
        let mut out: PyStr = here.chars().map(u32::from).collect();
        out.extend_from_slice(&spelled[1..]);
        return out;
    }
    spelled
}

/// The case's tree in memory under `here`.
pub fn mem_tree(case: &Value, here: &str) -> MemFs {
    let mut fs = MemFs::new(Flavour::Posix);
    fs.insert(here.as_bytes(), Node::Dir);
    for (rel, spec) in case["f"].as_object().unwrap() {
        fs.insert(
            format!("{here}/{rel}").as_bytes(),
            Node::File(content(spec)),
        );
    }
    if let Some(Value::Array(dirs)) = case.get("d") {
        for d in dirs {
            fs.insert(
                format!("{here}/{}", d.as_str().unwrap()).as_bytes(),
                Node::Dir,
            );
        }
    }
    fs
}

/// The report as `to_dict()` spells it.
pub fn report_json(report: &SymbolDriftReport) -> Value {
    let mut out = Map::new();
    for (key, list) in report.to_dict() {
        out.insert(
            key.to_owned(),
            Value::Array(list.iter().map(|t| spelled(t)).collect()),
        );
    }
    Value::Object(out)
}

/// The case run over `fs` with `here` its directory and the working
/// directory.
pub fn run<F: Fs + ?Sized>(
    case: &Value,
    fs: &F,
    flavour: Flavour,
    here: &str,
    names: &Replay,
) -> Result<SymbolDriftReport, Declined> {
    let citations: Vec<(PyStr, PyStr)> = case["c"]
        .as_array()
        .unwrap()
        .iter()
        .map(|pair| (text(&pair[0]), text(&pair[1])))
        .collect();
    let worktree = case.get("w").map(|w| root_of(w, here));
    let anchor = case.get("a").map(|a| Anchor {
        root: if a[0].is_null() {
            None
        } else {
            Some(root_of(&a[0], here))
        },
        convicting: a[1].as_bool().unwrap(),
    });
    let machine = Machine {
        fs,
        flavour,
        cwd: here.as_bytes(),
    };
    check_symbol_citations(
        &citations,
        worktree.as_deref(),
        anchor.as_ref(),
        &machine,
        names,
    )
}
