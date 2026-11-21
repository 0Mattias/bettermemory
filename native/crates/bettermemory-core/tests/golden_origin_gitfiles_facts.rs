//! Every case of `origin_gitfiles_facts.jsonl` (class i) against
//! `origin_gitfiles::facts`: `_rev_parse_dies` and `_machine_value_dies`
//! over every value of the header's list, `_facts_of` and
//! `_config_variable_names` over config texts and a seeded sweep
//! (digest), and `_machine_config_dies` over described reads.

mod common;

use bettermemory_core::gitconfig::remotes::NotPortedWhy;
use bettermemory_core::gitconfig::tree::{ConfigValues, Values};
use bettermemory_core::gitconfig::Name;
use bettermemory_core::gitfile_rule::format::GitFacts;
use bettermemory_core::origin_gitfiles::facts::{
    config_variable_names, facts_of, machine_config_dies, machine_value_dies, rev_parse_dies,
};
use common::parity::Ledger;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

type Answer<'a> = &'a dyn Fn(&[u8], Option<&[u8]>) -> bool;

const FILE: &str = "origin_gitfiles_facts.jsonl";

fn unspell(v: &Value) -> Option<Vec<u8>> {
    let text = v.as_str()?;
    Some(match text.strip_prefix('=') {
        Some(rest) => rest.as_bytes().to_vec(),
        None => common::unhex(text),
    })
}

/// `gitsystem.is_at_least` patched to answer `at` for every release, or
/// to compare the release `[major, minor]` that `at` names.
fn git_at(at: &Value) -> GitFacts {
    GitFacts {
        release: match at.as_bool() {
            _ if at.is_array() => Some((
                at[0].as_u64().expect("a major") as u32,
                at[1].as_u64().expect("a minor") as u32,
            )),
            Some(true) => Some((u32::MAX, u32::MAX)),
            Some(false) => Some((0, 0)),
            None => None,
        },
        reads_compat: None,
    }
}

fn facts_line(data: &[u8], git: &GitFacts) -> String {
    let facts = facts_of(data, git);
    assert_eq!(
        config_variable_names(data),
        facts.names,
        "names of {data:?}"
    );
    let names = match &facts.names {
        None => "-".to_owned(),
        Some(n) => n
            .iter()
            .map(|x| common::hex(x))
            .collect::<Vec<_>>()
            .join(","),
    };
    let flags = [
        facts.attributes_elsewhere,
        facts.bare_moves,
        facts.git_dies,
        facts.includes,
    ];
    let bits: String = flags.iter().map(|&f| if f { '1' } else { '0' }).collect();
    format!("{names}|{bits}")
}

/// The texts `_random_texts(seed, count)` draws.
fn random_texts(seed: u64, count: usize, tokens: &[Vec<u8>]) -> Vec<Vec<u8>> {
    let mut state = seed;
    let mut draw = |n: u64| {
        state = state
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        ((state >> 33) % n) as usize
    };
    (0..count)
        .map(|_| {
            let length = draw(40);
            (0..length)
                .flat_map(|_| tokens[draw(tokens.len() as u64)].clone())
                .collect()
        })
        .collect()
}

fn values_of(spec: Option<&Value>) -> Values {
    let mut out = Values::new();
    for item in spec.and_then(Value::as_array).into_iter().flatten() {
        let name = Name {
            section: item[0].as_str().expect("a section").to_owned(),
            subsection: if item[1].is_null() {
                None
            } else {
                Some(unspell(&item[1]).expect("a subsection"))
            },
            key: item[2].as_str().expect("a key").to_owned(),
        };
        let values = item[3]
            .as_array()
            .expect("values")
            .iter()
            .map(unspell)
            .collect();
        out.push((name, values));
    }
    out
}

fn config_values(spec: &Value) -> ConfigValues {
    ConfigValues {
        values: values_of(spec.get("v")),
        undecided: values_of(spec.get("u")),
        complete: spec.get("c").and_then(Value::as_bool).unwrap_or(true),
        dies: spec
            .get("d")
            .and_then(Value::as_str)
            .map(|s| s.as_bytes().to_vec()),
        maybe_dies: spec
            .get("m")
            .and_then(Value::as_str)
            .map(|s| s.as_bytes().to_vec()),
        asks_remote_urls: spec.get("ar").and_then(Value::as_bool).unwrap_or(false),
        forbidden: spec.get("fb").and_then(Value::as_bool).unwrap_or(false),
        ..ConfigValues::default()
    }
}

#[test]
fn origin_gitfiles_facts_every_case() {
    let lines = common::jsonl(FILE);
    let header = &lines[0];
    let values: Vec<Option<Vec<u8>>> = header["values"]
        .as_array()
        .expect("values")
        .iter()
        .map(unspell)
        .collect();
    let tokens: Vec<Vec<u8>> = header["tokens"]
        .as_array()
        .expect("tokens")
        .iter()
        .map(|t| unspell(t).expect("a token"))
        .collect();
    let ledger = Ledger::new("golden_origin_gitfiles_facts", FILE);
    let mut failures = Vec::new();
    let mut counts = std::collections::BTreeMap::<String, u64>::new();
    for (i, row) in lines[1..].iter().enumerate() {
        let row = row.as_array().expect("a row");
        let kind = row[0].as_str().expect("a kind");
        *counts.entry(kind.into()).or_default() += 1;
        let git = git_at(&row[2]);
        let bits = |f: Answer| -> Value {
            let name = unspell(&row[1]).expect("a name");
            let s: String = values
                .iter()
                .map(|v| if f(&name, v.as_deref()) { '1' } else { '0' })
                .collect();
            json!(s)
        };
        let got = match kind {
            "rpd" => bits(&|n, v| rev_parse_dies(n, v, &git)),
            "mvd" => bits(&|n, v| machine_value_dies(n, v, &git)),
            "fo" => json!(facts_line(&unspell(&row[1]).expect("a text"), &git)),
            "fod" => {
                let seed = row[1].as_u64().expect("a seed");
                let count = row[2].as_u64().expect("a count") as usize;
                let texts = random_texts(seed, count, &tokens);
                assert_eq!(texts.len(), count);
                let mut digest = Sha256::new();
                let none = git_at(&Value::Null);
                for text in &texts {
                    digest.update(facts_line(text, &none).as_bytes());
                    digest.update(b"\n");
                }
                json!(common::hex(&digest.finalize()))
            }
            "mcd" => {
                let spec = &row[1];
                let got = if spec.get("raise").is_some() {
                    // The reference's reads raise: the native reads'
                    // `Err(Raised)` is the one counterpart.
                    machine_config_dies(Err(&NotPortedWhy::Raised), &git)
                } else {
                    let reads: Vec<ConfigValues> = spec
                        .as_array()
                        .expect("reads")
                        .iter()
                        .map(config_values)
                        .collect();
                    machine_config_dies(Ok(&reads), &git)
                };
                json!(got.expect("ported"))
            }
            other => panic!("no kind {other}"),
        };
        let want = row.last().expect("an answer");
        if &got != want {
            failures.push(format!(
                "#{i} {kind} {}: native {got} reference {want}",
                row[1]
            ));
        }
    }
    let mut total = 0u64;
    for kind in ["rpd", "mvd", "fo", "fod", "mcd"] {
        let n = counts.get(kind).copied().unwrap_or(0);
        assert_eq!(header[kind].as_u64(), Some(n), "{kind} count");
        assert!(n > 0, "{kind}: no case");
        total += n;
    }
    assert_eq!(total, (lines.len() - 1) as u64);
    ledger.settle(total as usize, &failures);
}
