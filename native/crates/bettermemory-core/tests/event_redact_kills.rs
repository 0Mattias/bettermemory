//! Cases a plausible wrong port of `event_redact` fails. Each answer is
//! the reference's under CPython 3.13.13.

use bettermemory_core::datetime::DateTime;
use bettermemory_core::event_redact as er;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::store::rows;
use bettermemory_core::time_utils;
use bettermemory_core::unicode::cps;

fn none(_: &[u32]) -> Vec<(usize, usize, PyStr)> {
    Vec::new()
}

fn redact(text: &[u32]) -> (String, PyStr, String) {
    let PyValue::Dict(ref d) = er::redact_query(text, &mut none) else {
        panic!("a dict")
    };
    let (PyValue::Str(h), PyValue::Str(p), PyValue::Int(n)) = (&d[0].1, &d[1].1, &d[2].1) else {
        panic!("hash, preview, len")
    };
    (
        String::from_utf16_lossy(&h.iter().map(|&c| c as u16).collect::<Vec<_>>()),
        p.clone(),
        n.clone(),
    )
}

/// A lone surrogate is hashed as `?` (errors="replace"), not U+FFFD.
#[test]
fn surrogate_hashes_as_question_mark() {
    assert_eq!(redact(&[0xD800]).0, "8a8de823d5ed3e12");
    assert_eq!(redact(&[0xFFFD]).0, "83d544ccc223c057");
}

/// `\b` is Unicode: `é` is a word character, so no boundary follows the key.
#[test]
fn word_boundary_is_unicode() {
    let mut t = cps(&format!("sk-{}", "a".repeat(20)));
    t.push(0xE9);
    let (h, p, n) = redact(&t);
    assert_eq!(
        (h.as_str(), p, n.as_str()),
        ("d16f3dda10eff208", t.clone(), "24")
    );
}

/// The `sk-ant-` pattern runs first; the length is the original's.
#[test]
fn anthropic_before_generic() {
    let (h, p, n) = redact(&cps(&format!("sk-ant-{}", "a".repeat(20))));
    assert_eq!(h, "a85100222f139866");
    assert_eq!(p, cps("[REDACTED:anthropic-key]"));
    assert_eq!(n, "27");
}

/// The preview counts code points, not UTF-8 bytes.
#[test]
fn preview_in_code_points() {
    let (h, p, n) = redact(&cps(&"\u{e9}".repeat(40)));
    assert_eq!(
        (h.as_str(), p.len(), n.as_str()),
        ("84fe2e03d50dd3a1", 32, "40")
    );
}

/// The detector sees the five patterns' output and its spans are spliced
/// in order, once each.
#[test]
fn spans_read_the_first_pass() {
    let text = cps(&format!("AKIA{} x tok", "Z".repeat(16)));
    let mut seen = Vec::new();
    let out = er::strip_known_secrets(&text, &mut |t: &[u32]| {
        seen.push(t.to_vec());
        vec![(0, 3, cps("a")), (27, 30, cps("b"))]
    });
    assert_eq!(seen, vec![cps("[REDACTED:aws-access-key] x tok")]);
    assert_eq!(
        out,
        cps("[REDACTED:a]DACTED:aws-access-key] x[REDACTED:b]k")
    );
}

/// A named field the row lacks is not added; `imported_from` keeps its
/// place.
#[test]
fn absent_fields_and_imported_from() {
    let f = vec![(PyValue::str("x"), PyValue::None)];
    assert_eq!(er::named_event_fields(&f), f);
    let event = vec![
        (PyValue::str("imported_from"), PyValue::str("x")),
        (PyValue::str("kind"), PyValue::str("k")),
        (PyValue::str("z"), PyValue::int(1)),
    ];
    let (ts, s, k, p) = rows::event_import_payload(&event, &cps("v8"), &mut none).unwrap();
    assert_eq!((ts, s, k), (None, None, cps("k")));
    assert_eq!(
        p,
        vec![
            (PyValue::str("imported_from"), PyValue::str("v8")),
            (PyValue::str("z"), PyValue::int(1)),
        ]
    );
}

/// `ensure_utc` keeps an aware value's own offset.
#[test]
fn ensure_utc_keeps_offset() {
    let dt = DateTime::from_isoformat(&cps("2026-01-02T03:04:05+02:00")).unwrap();
    assert_eq!(
        time_utils::ensure_utc(Some(dt)).unwrap().isoformat(),
        "2026-01-02T03:04:05+02:00"
    );
    assert_eq!(time_utils::isoformat_utc_optional(None), None);
}
