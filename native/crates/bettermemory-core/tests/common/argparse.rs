//! The parsers of `argparse.jsonl`'s header built through the port, and
//! outcomes spelled as the generator spells them.

use bettermemory_core::argparse::{Arg, ArgumentParser, Kind, Parsed};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;
use serde_json::{json, Map, Value};

/// A fixture text: a string, or the list of its code points.
pub fn text(v: &Value) -> PyStr {
    match v {
        Value::String(s) => cps(s),
        Value::Array(a) => a.iter().map(|c| c.as_u64().unwrap() as u32).collect(),
        _ => panic!("not a text: {v}"),
    }
}

/// The generator's `_t`.
pub fn spell(t: &[u32]) -> Value {
    if t.iter().any(|c| (0xD800..0xE000).contains(c)) {
        return json!(t);
    }
    json!(t
        .iter()
        .map(|&c| char::from_u32(c).unwrap())
        .collect::<String>())
}

fn value(v: &PyValue) -> Value {
    match v {
        PyValue::None => Value::Null,
        PyValue::Bool(b) => json!(b),
        PyValue::Str(s) => spell(s),
        PyValue::List(items) => items.iter().map(value).collect(),
        other => panic!("no such value: {other:?}"),
    }
}

/// One parser of the header.
pub fn build(spec: &Value) -> ArgumentParser {
    let mut p = ArgumentParser::new(
        spec["prog"].as_str().unwrap(),
        spec["desc"].as_str(),
        spec["help"].as_bool().unwrap(),
    );
    add_args(&mut p, &spec["args"]);
    p
}

/// One tree of `argparse_subparsers.jsonl`'s header, built at `width`.
#[allow(dead_code)]
pub fn build_tree(spec: &Value, width: i64) -> ArgumentParser {
    let mut p = build(spec);
    add_sub(&mut p, &spec["sub"], width);
    add_args(&mut p, &spec["after"]);
    p
}

fn add_sub(p: &mut ArgumentParser, sub: &Value, width: i64) {
    p.add_subparsers(sub["dest"].as_str(), width).unwrap();
    for child in sub["parsers"].as_array().unwrap() {
        let c = p
            .add_parser(
                child["name"].as_str().unwrap(),
                child["help"].as_str(),
                child["desc"].as_str(),
            )
            .unwrap();
        add_args(c, &child["args"]);
        if let Some(s) = child.get("sub") {
            add_sub(c, s, width);
        }
    }
}

fn add_args(p: &mut ArgumentParser, args: &Value) {
    for call in args.as_array().unwrap() {
        let names: Vec<&str> = call[0]
            .as_array()
            .unwrap()
            .iter()
            .map(|n| n.as_str().unwrap())
            .collect();
        let kw = &call[1];
        let s = |k: &str| kw.get(k).and_then(Value::as_str).map(String::from);
        let kind = match kw.get("action").and_then(Value::as_str) {
            None | Some("store") => Kind::Store,
            Some("store_true") => Kind::StoreTrue,
            Some("version") => Kind::Version(cps(kw["version"].as_str().unwrap())),
            Some(other) => panic!("no such action {other}"),
        };
        let arg = Arg {
            kind: Some(kind),
            dest: s("dest"),
            default: kw.get("default").map(|d| match d {
                Value::Null => PyValue::None,
                Value::String(t) => PyValue::str(t),
                _ => panic!("default {d}"),
            }),
            choices: kw.get("choices").map(|c| {
                c.as_array()
                    .unwrap()
                    .iter()
                    .map(|x| x.as_str().unwrap().to_owned())
                    .collect()
            }),
            required: kw.get("required").and_then(Value::as_bool),
            metavar: s("metavar"),
            help: s("help"),
        };
        p.add_argument(&names, arg).unwrap();
    }
}

/// An outcome as a case row holds it (`ns`, or `code`, `out`, `err`).
pub fn outcome(parsed: &Parsed) -> Value {
    let mut m = Map::new();
    match parsed {
        Parsed::Namespace(ns) => {
            let pairs: Vec<Value> = ns
                .iter()
                .map(|(k, v)| json!([spell(k), value(v)]))
                .collect();
            m.insert("ns".into(), json!(pairs));
        }
        Parsed::Exit(o) => {
            m.insert("code".into(), json!(o.code));
            m.insert("err".into(), spell(&o.stderr));
            m.insert("out".into(), spell(&o.stdout));
        }
    }
    Value::Object(m)
}
