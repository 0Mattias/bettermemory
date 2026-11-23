//! The pinned Python integer-conversion limit and repr precision for schema TEXT.
use bettermemory_core::store::{open_checks_at_points, OpenCheckError};
use bettermemory_core::unicode::cps;
use rusqlite::Connection;

fn schema(text: &str) -> Result<(), OpenCheckError> {
    let meta = vec![
        ("store_id".into(), "a".repeat(32)),
        ("key_fingerprint".into(), "f".into()),
        ("schema_version".into(), text.to_owned()),
    ];
    open_checks_at_points(&Connection::open_in_memory().unwrap(), &meta, &[], "1").map(|_| ())
}
fn reason(text: &str) -> Vec<u32> {
    let Err(OpenCheckError::ValueError(message)) = schema(text) else {
        panic!("expected schema ValueError")
    };
    message
}

#[test]
fn count_digits_before_removing_zeros_and_ignore_separators() {
    schema(&"٠".repeat(4300)).unwrap();
    let grouped = ["٠"; 4301].join("_");
    let expected = cps("Exceeds the limit (4300 digits) for integer string conversion: value has 4301 digits; use sys.set_int_max_str_digits() to increase the limit");
    assert_eq!(reason(&format!("\u{3000}-{grouped} ")), expected);
    schema(&format!("-{}", "9".repeat(4300))).unwrap();
    assert_eq!(
        reason(&format!("+{}", "9".repeat(4300))),
        cps(&format!(
            " is schema {}; this build reads up to 1",
            "9".repeat(4300)
        ))
    );
}

#[test]
fn truncate_the_repr_by_codepoints_after_escaping() {
    let bad = "\0".repeat(128);
    let repr = format!("'{}'", "\\x00".repeat(128));
    let expected: String = repr.chars().take(200).collect();
    assert_eq!(
        reason(&bad),
        cps(&format!(
            "invalid literal for int() with base 10: {expected}"
        ))
    );
    let prefix = "2".repeat(4450);
    // 3.13 detects the trailing character before reporting a digit-count error.
    assert_eq!(
        reason(&format!("{prefix}z")),
        cps(&format!(
            "invalid literal for int() with base 10: '{}",
            "2".repeat(199)
        ))
    );
}
