//! A scripted [`DriftGit`] that answers as a `verify_weak.jsonl` script
//! says and logs each call as the generator logs it
//! (`tests/contract/gen_verify_weak.py`).

#![allow(dead_code)]

use std::cell::RefCell;
use std::collections::BTreeMap;

use bettermemory_core::claims::{Claim, Kind};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;
use bettermemory_core::verify::weak::{DriftGit, LiteralFloor, PatchWindow, Place, Set, Spelling};
use serde_json::{json, Value};

/// The first n hashes the generator squeezes to ``{"seq": n}``.
pub fn many(n: usize) -> Vec<String> {
    (0..n).map(|i| format!("{i:040x}")).collect()
}

/// A row spelled out as the generator's `_squeeze` squeezed it, the
/// named streams and the walk read from the header.
pub fn unsqueeze(v: &Value, header: &Value) -> Value {
    match v {
        Value::Object(m) if m.len() == 1 && m.contains_key("seq") => {
            json!(many(m["seq"].as_u64().expect("n") as usize))
        }
        Value::Object(m) => Value::Object(
            m.iter()
                .map(|(k, x)| (k.clone(), unsqueeze(x, header)))
                .collect(),
        ),
        Value::Array(a) => Value::Array(a.iter().map(|x| unsqueeze(x, header)).collect()),
        Value::String(t) if t == "WALK" => header["walk"].clone(),
        Value::String(t) if t.len() == 2 && t.starts_with('#') => json!(t[1..].repeat(40)),
        Value::String(t) if t.starts_with('$') => header["streams"][&t[1..]].clone(),
        other => other.clone(),
    }
}

pub fn text(v: &Value) -> PyStr {
    cps(v.as_str().expect("a text"))
}

pub fn texts(v: &Value) -> Vec<PyStr> {
    v.as_array().expect("texts").iter().map(text).collect()
}

pub fn set(v: &Value) -> Set {
    texts(v).into_iter().collect()
}

pub fn out(t: &[u32]) -> Value {
    json!(t
        .iter()
        .map(|&c| char::from_u32(c).expect("a char"))
        .collect::<String>())
}

pub fn outs(ts: &[PyStr]) -> Value {
    Value::Array(ts.iter().map(|t| out(t)).collect())
}

pub fn claims(v: &Value) -> Vec<Claim> {
    v.as_array()
        .expect("claims")
        .iter()
        .map(|c| Claim {
            kind: match c[0].as_str().expect("kind") {
                "path" => Kind::Path,
                "symbol" => Kind::Symbol,
                "literal" => Kind::Literal,
                _ => Kind::Absent,
            },
            rel_path: text(&c[1]),
            name: text(&c[2]),
            value: text(&c[3]),
        })
        .collect()
}

pub fn top(at: &Place) -> Value {
    at.toplevel.map_or(Value::Null, out)
}

/// The spellings the script chose.
pub struct Chosen {
    pub chosen: BTreeMap<PyStr, Vec<PyStr>>,
    pub declined: bool,
}

impl Chosen {
    pub fn of_script(s: &Value) -> Chosen {
        let chosen = s
            .get("chosen")
            .and_then(Value::as_object)
            .map(|m| m.iter().map(|(k, v)| (cps(k), texts(v))).collect())
            .unwrap_or_default();
        Chosen {
            chosen,
            declined: s.get("declined").and_then(Value::as_bool).unwrap_or(false),
        }
    }
}

impl Spelling for Chosen {
    fn of(&self, spec: &[u32]) -> Vec<PyStr> {
        self.chosen
            .get(spec)
            .cloned()
            .unwrap_or_else(|| vec![spec.to_vec()])
    }
    fn declined(&self) -> bool {
        self.declined
    }
}

/// The script and the calls made.
pub struct Script {
    pub s: Value,
    pub pairs: RefCell<Vec<Value>>,
    pub calls: RefCell<Vec<Value>>,
}

impl Script {
    pub fn new(s: &Value) -> Script {
        let pairs = s
            .get("pairs")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        Script {
            s: s.clone(),
            pairs: RefCell::new(pairs),
            calls: RefCell::new(Vec::new()),
        }
    }

    fn log(&self, v: Value) {
        self.calls.borrow_mut().push(v);
    }
}

impl DriftGit for Script {
    fn resolve_repo_pathspecs(&self, at: Place, paths: &[PyStr]) -> Option<Vec<PyStr>> {
        self.log(json!(["rp", top(&at), outs(paths)]));
        match self.s.get("rp") {
            None => Some(paths.to_vec()),
            Some(Value::Null) => None,
            Some(v) => Some(texts(v)),
        }
    }
    fn commit_author_sha_pairs_touching_pathspecs(
        &self,
        at: Place,
        pathspecs: &[PyStr],
    ) -> Option<Vec<(DateTime, PyStr)>> {
        self.log(json!(["pairs", top(&at), outs(pathspecs)]));
        let got = self.pairs.borrow_mut().remove(0);
        got.as_array().map(|a| {
            a.iter()
                .map(|p| {
                    (
                        DateTime::from_isoformat(&text(&p[0])).expect("a date"),
                        text(&p[1]),
                    )
                })
                .collect()
        })
    }
    fn paths_in_history(&self, at: Place, pathspecs: &[PyStr]) -> Option<bool> {
        self.log(json!(["hist", top(&at), outs(pathspecs)]));
        self.s.get("hist").and_then(Value::as_bool)
    }
    fn too_large(&self, at: Place, shas: &[PyStr], pathspecs: &[PyStr]) -> bool {
        self.log(json!(["big", top(&at), outs(shas), outs(pathspecs)]));
        self.s.get("big").and_then(Value::as_bool).unwrap_or(false)
    }
    fn commit_patch_stream(&self, at: Place, shas: &[PyStr], pathspecs: &[PyStr]) -> Option<PyStr> {
        self.log(json!(["stream", top(&at), outs(shas), outs(pathspecs)]));
        self.s.get("stream").and_then(Value::as_str).map(cps)
    }
    fn local_patch_window(
        &self,
        at: Place,
        shas: &[PyStr],
        pathspecs: &[PyStr],
    ) -> Option<PatchWindow> {
        self.log(json!(["split", top(&at), outs(shas), outs(pathspecs)]));
        let w = self.s.get("split").filter(|w| !w.is_null())?;
        Some(PatchWindow {
            stream: text(&w[0]),
            counted: set(&w[1]),
            touched: set(&w[2]),
            deleted: set(&w[3]),
        })
    }
    fn literal_floor(
        &self,
        at: Place,
        shas: &[PyStr],
        specs: &[PyStr],
        pathspecs: &[PyStr],
        _claims: &[Claim],
        _spell: &dyn Spelling,
    ) -> Option<LiteralFloor> {
        self.log(json!([
            "floor",
            top(&at),
            outs(shas),
            outs(specs),
            outs(pathspecs)
        ]));
        match self.s.get("floor") {
            None => Some(LiteralFloor::default()),
            Some(Value::Null) => None,
            Some(f) => Some(LiteralFloor {
                paths: set(&f[0]),
                commits: set(&f[1]),
                listed: set(&f[2]),
            }),
        }
    }
    fn spelled(
        &self,
        at: Place,
        specs: &[PyStr],
        anchor: Option<&[u32]>,
        since: Option<&DateTime>,
    ) -> Box<dyn Spelling> {
        let stamp = since.map_or(Value::Null, |d| json!(d.isoformat()));
        self.log(json!([
            "spelled",
            top(&at),
            outs(specs),
            anchor.map_or(Value::Null, out),
            stamp
        ]));
        Box::new(Chosen::of_script(&self.s))
    }
}

/// The generator's `_literal_pathspecs`.
pub fn literal(names: &[PyStr]) -> Vec<PyStr> {
    names
        .iter()
        .map(|n| {
            let mut out = cps(":(literal)");
            out.extend_from_slice(n);
            out
        })
        .collect()
}
