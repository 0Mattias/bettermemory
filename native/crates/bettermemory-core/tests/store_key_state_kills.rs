//! The cut points that determine what the user sees about a held key.
use bettermemory_core::log::ring::KeyFiles;
use bettermemory_core::log::{
    check_origin, compute_mac, fingerprint, Head, HistoryOrigin, Key, LogRow, OriginCheck,
};
use bettermemory_core::pyvalue::PyValue;
use bettermemory_core::store::key_state::{
    head_status, sentence, HeadStatus, KeyState, KeyStatus, RingFit, StateError, KEY_STATES,
};
use bettermemory_core::unicode::{cps, to_string};
use rusqlite::Connection;
use std::path::Path;

#[test]
fn first_successful_signer_is_evidence_separate_from_equality_and_report() {
    let signing = Key::new((0..32).collect());
    let other = Key::new(vec![253; 32]);
    let mac = compute_mac(signing.bytes(), 1, "now", None, "search", "{}", "");
    let row = LogRow {
        seq: 1,
        ts: "now".into(),
        session: None,
        kind: "search".into(),
        payload: "{}".into(),
        prev_mac: String::new(),
        mac: mac.clone(),
    };
    let anchor = HistoryOrigin {
        seq: 1,
        mac,
        key_fingerprint: Some(fingerprint(signing.bytes())),
    };
    let checked = check_origin(Some(&anchor), Some(&row), &[other.clone(), signing.clone()]);
    assert_eq!(checked.signer, Some(fingerprint(signing.bytes())));
    assert!(checked.holds());
    let different = OriginCheck {
        signer: None,
        ..checked.clone()
    };
    assert_eq!(checked, different);
    assert_eq!(
        checked.to_dict(),
        PyValue::Dict(vec![
            (PyValue::str("status"), PyValue::str("ok")),
            (PyValue::str("seq"), PyValue::int(1))
        ])
    );
    let missing = check_origin(Some(&anchor), None, &[signing]);
    assert!(missing.broken());
    assert_eq!(missing.reason, Some("missing"));
    assert!(missing.signer.is_none());
    assert_eq!(
        check_origin(Some(&anchor), Some(&row), &[other]).status,
        "unverifiable"
    );
}
#[test]
fn a_none_checkpoint_needs_no_schema_and_a_read_error_leaves_the_transaction_open() {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch("BEGIN").unwrap();
    assert_eq!(head_status(None, &conn).unwrap(), HeadStatus::Absent);
    let h = Head {
        seq: PyValue::int(1),
        mac: cps("x"),
    };
    let err = head_status(Some(&h), &conn).unwrap_err();
    assert!(matches!(err, StateError::Read(_)));
    assert!(err.to_string().contains("no such table"));
    assert!(!conn.is_autocommit());
    conn.execute_batch("ROLLBACK").unwrap();
}
#[test]
fn refusal_paths_and_unread_reasons_keep_codepoints() {
    let ring = KeyFiles::new(Path::new("."), &PyValue::str(&"a".repeat(32))).unwrap();
    let mut expected = cps(&"x".repeat(15));
    expected.extend([0x1f9a9, 0xdcff]);
    let mut s = KeyState::new(KeyStatus::Present, ring, "store".into(), expected.clone());
    let mut prefix = cps("fingerprint ");
    prefix.extend(&expected[..16]);
    assert!(s
        .summary()
        .unwrap()
        .windows(prefix.len())
        .any(|p| p == prefix));
    assert_eq!(
        s.details().unwrap().get("expected_fingerprint"),
        Some(&PyValue::Str(expected))
    );
    assert_eq!(s.remedy().unwrap(), None);
    let mut text = vec![0xdf, 0xdcff];
    assert_eq!(sentence(&text), vec![83, 83, 0xdcff]);
    text.clear();
    assert!(sentence(&text).is_empty());
    s.key = Some(Key::new(vec![173; 32]));
    assert!(!format!("{s:?}").contains("Key(32 bytes"));
    s.state = KeyStatus::BoundElsewhere;
    assert!(matches!(
        s.summary(),
        Err(StateError::Raised {
            class: "AssertionError",
            ..
        })
    ));
    assert_eq!(KEY_STATES.len(), 6);
}
#[test]
fn an_origin_reason_precedes_formatting_a_very_large_checkpoint() {
    let mut fit = RingFit {
        origin: OriginCheck {
            status: "mismatch",
            seq: Some(3),
            reason: Some("mac"),
            signer: None,
        },
        head: Some(Head {
            seq: PyValue::Int("1".repeat(4301)),
            mac: cps("mac"),
        }),
        head_status: HeadStatus::Past,
        log_end: None,
    };
    assert!(to_string(&fit.reason().unwrap().unwrap())
        .unwrap()
        .contains("another MAC than the key ring's origin anchor"));
    fit.origin.status = "ok";
    let err = fit.reason().unwrap_err();
    assert_eq!(
        err.to_string(),
        bettermemory_core::toml::INT_STR_LIMIT_MESSAGE
    );
    fit.head_status = HeadStatus::Stale;
    assert!(fit.fits());
    assert_eq!(fit.reason().unwrap(), None);
}
#[cfg(unix)]
#[test]
fn paths_preserve_exactly_two_initial_slashes_and_their_non_unicode_bytes() {
    use std::os::unix::ffi::OsStrExt;
    for (input, expected) in [
        ("//example/keys", "//example/keys"),
        ("///example/keys", "/example/keys"),
    ] {
        let files = KeyFiles::new(Path::new(input), &PyValue::str(&"c".repeat(32))).unwrap();
        let mut s = KeyState::new(
            KeyStatus::BoundElsewhere,
            files,
            "store".into(),
            cps("expected"),
        );
        s.bound_to = Some(Path::new(std::ffi::OsStr::from_bytes(b"//other/\xff")).to_owned());
        let mut other = cps("//other/");
        other.push(0xdcff);
        assert!(s.summary().unwrap().starts_with(&cps(&format!(
            "the key ring under {expected} is bound elsewhere"
        ))));
        assert!(s
            .refusal(Path::new(input))
            .unwrap()
            .starts_with(&cps(&format!("store {expected} is open for reading only"))));
        let d = s.details().unwrap();
        assert_eq!(d.get("bound_to"), Some(&PyValue::Str(other)));
        assert_eq!(
            d.get("key_path"),
            Some(&PyValue::str(&format!("{expected}/{}.key", "c".repeat(32))))
        );
    }
}
