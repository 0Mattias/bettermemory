//! Hostile input for `related_text` and `_write_sentences`: very long
//! bodies, runs of breaks with nothing between them, a shared token only
//! at the end, and a growth claim at ten times the input.

mod timing;

use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::review::*;
use bettermemory_core::unicode;

fn long_body(sentences: usize) -> PyStr {
    let mut b = unicode::cps(&"alpha beta. ".repeat(sentences));
    b.extend(unicode::cps("ruff"));
    b
}

#[test]
fn many_breaks_and_a_token_at_the_end() {
    let got = related_text(&unicode::cps("ruff"), &long_body(50_000));
    assert_eq!(got, unicode::cps("...ruff"));
}

#[test]
fn only_breaks() {
    let body: PyStr = unicode::cps(&".\n! ?\t".repeat(100_000));
    assert!(related_text(&unicode::cps("x"), &body).len() <= RELATED_TEXT_CHARS);
    assert!(write_sentences(&body).is_empty());
    let lines: PyStr = std::iter::repeat_n(u32::from(b'\n'), 200_000).collect();
    assert!(related_text(&unicode::cps("x"), &lines).is_empty());
}

#[test]
fn one_huge_sentence_and_odd_code_points() {
    let mut body: PyStr = std::iter::repeat_n(0x10FFFF, 300_000).collect();
    body.extend([0xD800, u32::from(b'.'), 0x3000, 0xDFFF]);
    let got = related_text(&unicode::cps("x"), &body);
    assert!(got.len() <= RELATED_TEXT_CHARS);
    assert!(write_sentences(&body).is_empty());
}

#[test]
fn related_text_grows_linearly() {
    let small = long_body(5_000);
    let large = long_body(50_000);
    let content = unicode::cps("ruff");
    timing::grows_within(
        "related_text: 5,000 and 50,000 sentences",
        timing::TENFOLD,
        &mut || timing::once(|| related_text(&content, &small)),
        &mut || timing::once(|| related_text(&content, &large)),
    );
}

#[test]
fn write_sentences_grows_linearly() {
    let small = long_body(5_000);
    let large = long_body(50_000);
    timing::grows_within(
        "write_sentences: 5,000 and 50,000 sentences",
        timing::TENFOLD,
        &mut || timing::once(|| write_sentences(&small)),
        &mut || timing::once(|| write_sentences(&large)),
    );
}
