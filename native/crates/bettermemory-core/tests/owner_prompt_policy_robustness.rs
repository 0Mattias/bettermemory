//! Hostile input for `owner_prompt` and `policy`: very large texts and
//! block lists, nesting and every acknowledge combination, without a
//! panic and in time linear in the input.

mod timing;

use bettermemory_core::owner_prompt::{owner_prompt, owner_text, text_blocks};
use bettermemory_core::policy::check_overrides_with;
use bettermemory_core::pyvalue::{PyStr, PyValue};

fn id(text: &[u32], _cut: bool) -> PyStr {
    text.to_vec()
}

fn blocks(n: usize) -> PyValue {
    PyValue::List(
        (0..n)
            .map(|i| {
                PyValue::Dict(vec![
                    (
                        PyValue::str("type"),
                        PyValue::str(if i % 3 == 0 { "image" } else { "text" }),
                    ),
                    (PyValue::str("text"), PyValue::str("word")),
                ])
            })
            .collect(),
    )
}

#[test]
fn a_megabyte_of_whitespace_and_text() {
    let mut text = vec![0x3000u32; 1_000_000];
    assert_eq!(owner_text(&text, true, &id), None);
    text.push(u32::from('x'));
    assert_eq!(
        owner_text(&text, true, &id).map(|t| t.len()),
        Some(1_000_001)
    );
    let row = PyValue::Dict(vec![
        (PyValue::str("type"), PyValue::str("user")),
        (
            PyValue::str("message"),
            PyValue::Dict(vec![(PyValue::str("content"), blocks(200_000))]),
        ),
    ]);
    assert!(owner_prompt(&row, true, &id).is_some());
}

#[test]
fn nested_content_is_not_walked() {
    let mut v = PyValue::str("x");
    for _ in 0..2_000 {
        v = PyValue::List(vec![v]);
    }
    assert_eq!(text_blocks(&v), None);
}

#[test]
fn every_flag_and_odd_tool_names() {
    let fa: Vec<String> = [
        "credential",
        "transient",
        "scope_mismatch",
        "user_claim",
        "truncation",
    ]
    .iter()
    .map(|s| (*s).to_owned())
    .collect();
    let tool = "\u{0}t\u{10ffff}".repeat(10_000);
    let r = check_overrides_with(true, &fa, &|_| true, &tool, true, &|_| true).unwrap_err();
    assert_eq!(r.flags.len(), 6);
    assert!(r.to_string().contains(&tool));
}

#[test]
fn text_blocks_grows_linearly() {
    let small = blocks(20_000);
    let large = blocks(200_000);
    timing::grows_within(
        "text_blocks: 20,000 and 200,000 blocks",
        timing::TENFOLD,
        &mut || timing::once(|| text_blocks(&small)),
        &mut || timing::once(|| text_blocks(&large)),
    );
}
