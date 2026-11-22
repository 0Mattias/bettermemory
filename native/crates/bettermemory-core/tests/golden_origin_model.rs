//! `origin_model.jsonl`: a fresh `Origin`'s private capture state is
//! empty, False and None, and its dump holds none of the three.

mod common;

use bettermemory_core::origin_model::CapturedOrigin;
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::records::Origin;
use common::{assert_count, jsonl};

fn s(t: &str) -> Option<Vec<u32>> {
    Some(t.chars().map(u32::from).collect())
}

#[test]
fn origin_model_every_case() {
    let lines = jsonl("origin_model.jsonl");
    let total = lines[0]["cases"].as_u64().unwrap() as usize;
    let case = &lines[1];
    let captured = CapturedOrigin::new(Origin {
        cwd: s("/w"),
        repo: s("u"),
        branch: s("b"),
        worktree_root: s("/w"),
        source: s("env"),
    });
    assert_eq!(
        case["alternates"].as_array().unwrap().len(),
        captured.repo_url_alternates.len()
    );
    assert_eq!(
        case["indeterminate"].as_bool().unwrap(),
        captured.git_indeterminate()
    );
    assert!(case["refusal"].is_null() && captured.git_refusal().is_none());
    let dumped = captured.origin.dump(false);
    let PyValue::Dict(dump) = &dumped else {
        panic!("a dict");
    };
    let mut keys: Vec<String> = dump
        .iter()
        .map(|(k, _)| match k {
            PyValue::Str(t) => t.iter().map(|&u| char::from_u32(u).unwrap()).collect(),
            other => panic!("key {other:?}"),
        })
        .collect();
    keys.sort();
    let mut want: Vec<String> = case["dump"].as_object().unwrap().keys().cloned().collect();
    want.sort();
    assert_eq!(keys, want);
    assert_eq!(lines.len() - 1, total);
    assert_count("origin_model.jsonl", 1, total);
}
