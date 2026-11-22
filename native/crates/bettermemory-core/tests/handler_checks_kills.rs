//! Cases a plausible wrong port of the write-payload checks fails.

use bettermemory_core::handler_checks::{
    token_count, utf8_len, validate_content_size, validate_scope, validate_write_payload,
    CheckError, WriteArgs,
};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::records::Source;
use bettermemory_core::unicode::{cps, to_string};

fn msg(e: &CheckError) -> String {
    to_string(e.message()).unwrap()
}

fn args<'a>(content: &'a [u32], scopes: &'a [PyStr]) -> WriteArgs<'a> {
    WriteArgs {
        content,
        scopes,
        confidence: &[0x68, 0x69, 0x67, 0x68],
        source: &[0x69, 0x6e, 0x66, 0x65, 0x72, 0x72, 0x65, 0x64],
        allowed_scopes: &[],
        category: &[0x66, 0x61, 0x63, 0x74],
        max_content_bytes: 0,
        min_content_tokens: 0,
        max_scopes_per_write: 0,
        capture: false,
    }
}

#[test]
fn a_surrogate_run_is_reported_by_its_first_and_last_position() {
    let text = [0x61, 0x62, 0xD800, 0xDC00, 0xDFFF, 0x63, 0xD801];
    let e = utf8_len(&text).unwrap_err();
    assert_eq!(
        msg(&e),
        "'utf-8' codec can't encode characters in position 2-4: surrogates not allowed"
    );
    let e = utf8_len(&[0x78, 0xDABC]).unwrap_err();
    assert_eq!(
        msg(&e),
        "'utf-8' codec can't encode character '\\udabc' in position 1: surrogates not allowed"
    );
}

#[test]
fn the_size_is_in_utf8_bytes_and_the_cap_is_inclusive() {
    let e = cps("\u{e9}\u{e9}\u{e9}");
    assert!(validate_content_size(&e, 6, &cps("content")).is_ok());
    let err = validate_content_size(&e, 5, &cps("takeaway")).unwrap_err();
    assert_eq!(
        msg(&err),
        "takeaway is too long: 6 bytes of UTF-8, more than the limit of 5 bytes. Shorten the takeaway."
    );
    assert_eq!(utf8_len(&cps("\u{1F600}")).unwrap(), 4);
}

#[test]
fn the_floor_runs_before_the_size() {
    let content = [0x61, 0xD800];
    let scopes = vec![cps("a")];
    let mut a = args(&content, &scopes);
    a.min_content_tokens = 2;
    a.max_content_bytes = 1;
    let e = validate_write_payload(&a).unwrap_err();
    assert!(matches!(e, CheckError::Value(_)), "{e:?}");
    a.min_content_tokens = 1;
    assert!(matches!(
        validate_write_payload(&a).unwrap_err(),
        CheckError::Encode(_)
    ));
}

#[test]
fn whitespace_is_unicode_whitespace() {
    assert_eq!(token_count(&cps("a\u{3000}b\u{85}c\u{1c}d")), 4);
    assert_eq!(token_count(&cps("a\u{200b}b")), 1);
    let scopes = vec![cps("a")];
    let blank = cps("\u{3000}\u{2028}");
    assert!(validate_write_payload(&args(&blank, &scopes)).is_err());
    let zw = cps("\u{200b}");
    assert!(validate_write_payload(&args(&zw, &scopes)).is_ok());
}

#[test]
fn a_scope_ends_at_the_text_end_not_before_a_newline() {
    assert!(validate_scope(&cps("projects:foo")).is_ok());
    for bad in [
        "projects:foo\n",
        "a--b",
        "a:-b",
        "-a",
        "a-",
        "",
        "A",
        "\u{ff41}",
        "\u{661}",
    ] {
        assert!(validate_scope(&cps(bad)).is_err(), "{bad:?}");
    }
}

#[test]
fn a_capture_tag_is_inferred_only_while_capturing() {
    let content = cps("x");
    let scopes = vec![cps("a")];
    let src = cps("capture:");
    let mut a = args(&content, &scopes);
    a.source = &src;
    assert!(validate_write_payload(&a).is_err());
    a.capture = true;
    assert_eq!(validate_write_payload(&a).unwrap().source, Source::Inferred);
}

#[test]
fn the_allowed_list_is_sorted_and_the_unknown_list_is_not() {
    let content = cps("x");
    let scopes = vec![cps("q"), cps("b"), cps("q")];
    let allowed = vec![cps("z"), cps("a"), cps("B")];
    let mut a = args(&content, &scopes);
    a.allowed_scopes = &allowed;
    assert_eq!(
        msg(&validate_write_payload(&a).unwrap_err()),
        "scope(s) not in allowed list: ['q', 'b', 'q']. Allowed: ['B', 'a', 'z']"
    );
}
