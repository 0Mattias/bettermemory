//! `gitconfig_tree.jsonl` and `gitconfig_tree_rules.jsonl` cases: the
//! machine they name and the native answers in the fixtures' spelling.

use std::collections::BTreeMap;

use bettermemory_core::fsdata::{Flavour, Fs};
use bettermemory_core::gitconfig::paths::{self, Machine};
use bettermemory_core::gitconfig::tree::{
    config_tree, include_target, Ask, Branch, Context, Unexpanded, Values,
};
use serde_json::{json, Value};

pub fn spelled(data: &[u8]) -> String {
    if data
        .iter()
        .all(|&b| (0x20..0x7f).contains(&b) && b != b'\\')
    {
        return format!("={}", std::str::from_utf8(data).expect("ASCII"));
    }
    data.iter().map(|b| format!("{b:02x}")).collect()
}

/// A text as the fixtures record it: UTF-8, else hex.
pub fn text(data: &[u8]) -> Value {
    match std::str::from_utf8(data) {
        Ok(s) => json!({"s": s}),
        Err(_) => json!({"x": data.iter().map(|b| format!("{b:02x}")).collect::<String>()}),
    }
}

fn under(root: &str, rel: &str) -> Vec<u8> {
    format!("{root}/{rel}").into_bytes()
}

/// The accounts a case names, home directories under `root`.
pub fn users(case: &Value, root: &str) -> BTreeMap<Vec<u8>, Vec<u8>> {
    case.get("u")
        .and_then(Value::as_object)
        .map(|u| {
            u.iter()
                .map(|(k, v)| (k.as_bytes().to_vec(), under(root, v.as_str().unwrap())))
                .collect()
        })
        .unwrap_or_default()
}

/// The native answer to a tree case read from `root`.
pub fn tree_answer<F: Fs>(fs: &F, case: &Value, root: &str) -> Value {
    let accounts = users(case, root);
    let lookup = |user: &[u8]| accounts.get(user).cloned();
    let machine = Machine {
        flavour: Flavour::Posix,
        home: case["hm"].as_str().map(|h| under(root, h)),
        profile: None,
        user_home: &lookup,
    };
    let k = &case["k"];
    let ask = Ask {
        stamp_self: k["s"].as_bool().unwrap(),
        gentle: k["g"].as_bool().unwrap(),
        git_dir: k.get("gd").and_then(Value::as_str).map(|g| under(root, g)),
        cwd: root.as_bytes().to_vec(),
        context: k["c"].as_bool().unwrap().then(|| Context {
            branch: match k.get("b") {
                None => Branch::Unsettled,
                Some(Value::Null) => Branch::Elsewhere,
                Some(b) => Branch::Name(b.as_str().unwrap().as_bytes().to_vec()),
            },
            prefix: k["p"].as_str().map(|p| {
                if p == "%(prefix)" {
                    paths::AS_WRITTEN.to_vec()
                } else {
                    under(root, p)
                }
            }),
        }),
    };
    let target = under(root, case["t"].as_str().unwrap());
    let read = config_tree(fs, &machine, &target, &ask);
    let label = |bytes: &[u8]| -> Vec<u8> {
        let mut out = Vec::new();
        let mut rest = bytes;
        while let Some(at) = rest.windows(root.len()).position(|w| w == root.as_bytes()) {
            out.extend_from_slice(&rest[..at]);
            out.extend_from_slice(b"<scratch>");
            rest = &rest[at + root.len()..];
        }
        out.extend_from_slice(rest);
        out
    };
    assert_eq!(read.raised, None, "no case raises");
    let mut answer = json!({
        "v": values(&read.values),
        "c": read.complete,
        "d": read.dies.as_deref().map(|d| text(&label(d))),
        "fl": read.files.iter().map(|f| text(&label(f))).collect::<Vec<_>>(),
        "np": read.not_ported.as_deref().map(spelled),
    });
    if !read.undecided.is_empty() {
        answer["ud"] = json!(values(&read.undecided));
    }
    if let Some(md) = &read.maybe_dies {
        answer["md"] = text(&label(md));
    }
    answer
}

fn values(names: &Values) -> Vec<Value> {
    names
        .iter()
        .map(|(name, vals)| {
            json!([
                name.section,
                name.subsection.as_deref().map(spelled),
                name.key,
                vals.iter()
                    .map(|v| v.as_deref().map(spelled))
                    .collect::<Vec<_>>()
            ])
        })
        .collect()
}

fn unspelled(text: &str) -> Vec<u8> {
    match text.strip_prefix('=') {
        Some(rest) => rest.as_bytes().to_vec(),
        None => super::unhex(text),
    }
}

/// The native answer to a rules case.
pub fn rule_answer(case: &Value) -> Value {
    let flavour = if case["w"] == 1 {
        Flavour::Windows
    } else {
        Flavour::Posix
    };
    let accounts: BTreeMap<Vec<u8>, Vec<u8>> = case
        .get("u")
        .and_then(Value::as_object)
        .map(|u| {
            u.iter()
                .map(|(k, v)| {
                    (
                        k.as_bytes().to_vec(),
                        v.as_str().unwrap().as_bytes().to_vec(),
                    )
                })
                .collect()
        })
        .unwrap_or_default();
    let lookup = |user: &[u8]| accounts.get(user).cloned();
    let bytes = |key: &str| case[key].as_str().map(|s| s.as_bytes().to_vec());
    let machine = Machine {
        flavour,
        home: bytes("h"),
        profile: bytes("pf"),
        user_home: &lookup,
    };
    let value = unspelled(case["x"].as_str().unwrap());
    let including = case["i"].as_str().unwrap().as_bytes();
    // `p`: 0 for no prefix callable, else its answer (null: not known).
    let prefix = case["p"].as_str().map(str::as_bytes);
    let target = match include_target(&machine, &value, including, prefix) {
        Ok(t) => json!(spelled(&t)),
        Err(Unexpanded::Dies(why)) => json!({"d": spelled(&why)}),
        Err(Unexpanded::Overflow) => json!("over"),
    };
    json!({
        "a": paths::git_absolute(flavour, &value),
        "n": spelled(&paths::native(flavour, &value)),
        "t": target,
    })
}
