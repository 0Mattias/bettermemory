//! Cases a plausible wrong `attribution` fails, each answer the
//! reference's (checked against `attribution.py` at 465bc600).

use bettermemory_core::attribution::{
    attribute_uses, candidate_sentences, content_token_set, normalize, sentence_matches,
};
use bettermemory_core::unicode::cps;

fn matches(sentence: &str, reply: &str) -> bool {
    let r = cps(reply);
    sentence_matches(&cps(sentence), &normalize(&r), &content_token_set(&r))
}

#[test]
fn the_stopword_ratio_bound_is_inclusive() {
    // 8 of 10 tokens are stopwords: dropped; 7 of 10: kept.
    assert!(candidate_sentences(&cps("the and of to in on at by zebrafish yakherds")).is_empty());
    assert_eq!(
        candidate_sentences(&cps("the and of to in on at zebrafish yakherds quokka")).len(),
        1
    );
    // Punctuation around a stopword does not hide it.
    assert!(
        candidate_sentences(&cps("(the) \"and\" of, to; in: on! at? zebrafish yakherds"))
            .is_empty()
    );
}

#[test]
fn the_containment_ratio_and_floor() {
    let ten = "alpha bravo charlie delta echo foxtrot golf hotel india juliet";
    assert!(matches(ten, "juliet india hotel golf foxtrot echo"));
    assert!(!matches(ten, "juliet india hotel golf foxtrot"));
    assert!(matches(
        "alpha bravo charlie delta",
        "delta charlie bravo alpha"
    ));
    assert!(!matches("alpha bravo charlie", "charlie bravo alpha"));
}

#[test]
fn sentence_splitting() {
    // A dot not followed by a space does not split; a newline does.
    let one = candidate_sentences(&cps("version v2.6.0 shipped from the release branch today"));
    assert_eq!(one.len(), 1);
    let two = candidate_sentences(&cps(
        "first line has enough words in it to count\nsecond line also has enough words in it",
    ));
    assert_eq!(two.len(), 2);
    // Terminal marks are stripped, then the length is checked again.
    let cut = candidate_sentences(&cps("yyyyyyyyyyyyyyyyyyyy a b c d e.."));
    assert_eq!(cut, vec![cps("yyyyyyyyyyyyyyyyyyyy a b c d e")]);
    assert!(candidate_sentences(&cps("yyyyyyyyyyyyyyyyyyy a b c d e..")).is_empty());
}

#[test]
fn the_excerpt_is_capped_and_its_prefix_matches() {
    let body = format!("start {} end", "alpha ".repeat(120));
    let got = candidate_sentences(&cps(&body));
    assert_eq!(got[0].len(), 500);
    // The reply holds only the first 500 characters, and it matches.
    let reply = String::from_utf8(body.as_bytes()[..500].to_vec()).unwrap();
    let m = attribute_uses(&[(cps("m"), cps(&body))], &cps(&reply));
    assert_eq!(m.len(), 1);
}

#[test]
fn one_match_per_memory_in_order() {
    let s1 = "The auth middleware lives in src/auth/middleware.py today.";
    let s2 = "Deploys run from the release branch through the staging pipeline.";
    let body = format!("{s2} {s1}");
    let reply = format!("{s1} {s2}");
    let got = attribute_uses(
        &[
            (cps("b"), cps(&body)),
            (cps("a"), cps(s1)),
            (cps("e"), vec![]),
        ],
        &cps(&reply),
    );
    assert_eq!(got.len(), 2);
    assert_eq!(got[0].memory_id, cps("b"));
    assert_eq!(got[0].claim_excerpt, cps(&s2[..s2.len() - 1]));
    assert_eq!(got[1].memory_id, cps("a"));
}

#[test]
fn normalization() {
    assert_eq!(normalize(&cps("  A\t\nB\u{0b}C  ")), cps("a b c"));
    assert_eq!(normalize(&cps("Cafe\u{301}")), cps("caf\u{e9}"));
    // The precomposed and decomposed spellings give the same tokens.
    assert_eq!(
        content_token_set(&cps("caf\u{e9}")),
        content_token_set(&cps("cafe\u{301}"))
    );
    let toks: Vec<Vec<u8>> = content_token_set(&cps("The SRC/foo.py is_ok"))
        .into_iter()
        .collect();
    assert_eq!(
        toks,
        vec![
            b"foo".to_vec(),
            b"is_ok".to_vec(),
            b"py".to_vec(),
            b"src".to_vec()
        ]
    );
    // Unit separators are whitespace to `str.isspace`.
    assert_eq!(normalize(&cps("a\u{1f}b")), cps("a b"));
}

#[test]
fn should_is_a_stopword() {
    // "should" is the one six-letter stopword.
    let set = content_token_set(&cps("should alpha bravo"));
    let got: Vec<&[u8]> = set.iter().map(|t| t.as_slice()).collect();
    assert_eq!(got, vec![&b"alpha"[..], &b"bravo"[..]]);
    // 8 of 10 tokens are stopwords: dropped.
    assert!(candidate_sentences(&cps(
        "should should should should should should should should alpha bravo"
    ))
    .is_empty());
}
