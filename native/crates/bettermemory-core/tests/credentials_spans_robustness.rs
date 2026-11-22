//! Hostile input for `credentials::spans` and `builder`: texts dense in
//! the separators the generic rule's bounded prefix repetition reads,
//! keyword-free prose, many keywords and URIs, lone surrogates, a huge
//! snippet, and trees nested 100,000 deep or 100,000 wide. Nothing panics
//! and nothing recurses natively. Steps of `_GENERIC_KEYWORD_RE` grow
//! linearly (rule 6: steps at 2n within 2.2 times steps at n), and the
//! detectors' time at ten times the input is within thirty times the time
//! at the input.

mod timing;

use bettermemory_core::builder::{fold_nullable, strip_titles};
use bettermemory_core::credentials::spans::{
    find_credential_markers, redact_all, safe_snippet, secret_spans, GENERIC_KEYWORD_PATTERN,
    URI_USERINFO_PATTERN,
};
use bettermemory_core::pyre::regex::{CallError, Regex};
use bettermemory_core::pyre::tree::IGNORECASE;
use bettermemory_core::pyvalue::PyValue;

/// A secret-shaped value, written in pieces.
const S: &str = concat!("Xy7kQ9", "pLm2Zt");

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

/// The least per-run budget under which `finditer` over `text` answers:
/// the steps of its longest run. A budget error at the bound found is a
/// failure, never a silent miss.
fn steps(pattern: &str, text: &[u32]) -> u64 {
    let re = Regex::new_str(pattern, IGNORECASE).expect("the engine builds the pattern");
    let fits = |b: u64| match re.try_finditer(text, 0, isize::MAX, Some(b)) {
        Ok(_) => true,
        Err(CallError::Budget) => false,
        Err(e) => panic!("{e}"),
    };
    let mut hi = 1u64;
    while !fits(hi) {
        hi *= 2;
    }
    let mut lo = hi / 2;
    while lo + 1 < hi {
        let mid = (lo + hi) / 2;
        if fits(mid) {
            hi = mid;
        } else {
            lo = mid;
        }
    }
    let plain = re
        .finditer(text, 0, isize::MAX)
        .expect("no budget, no error");
    let budgeted = re
        .try_finditer(text, 0, isize::MAX, Some(hi))
        .expect("within the budget found");
    assert_eq!(plain, budgeted);
    hi
}

fn shapes() -> Vec<(&'static str, String)> {
    vec![
        ("underscores", "_".to_string()),
        ("dashes", "-".to_string()),
        ("a_", "a_".to_string()),
        ("-a", "-a".to_string()),
        ("abc1_", "abc1_".to_string()),
        ("long segments", format!("{}_", "a".repeat(29))),
        (
            "prose",
            "the quick brown fox jumps over the lazy dog ".to_string(),
        ),
        (
            "near keywords",
            "pass word api secretary bearers ".to_string(),
        ),
        ("keywords", "password is set to now ".to_string()),
        ("authorization", "Authorization: Bearer ".to_string()),
    ]
}

#[test]
fn generic_steps_grow_linearly() {
    for (name, unit) in shapes() {
        let n = 2000 / unit.len();
        let small = steps(GENERIC_KEYWORD_PATTERN, &cps(&unit.repeat(n)));
        let large = steps(GENERIC_KEYWORD_PATTERN, &cps(&unit.repeat(2 * n)));
        assert!(
            large * 10 <= small * 22,
            "{name}: {large} steps at 2n against {small} at n"
        );
    }
}

#[test]
fn uri_steps_grow_linearly() {
    for unit in ["postgres://", "a:", "ab://u:", "x"] {
        let n = 2000 / unit.len();
        let small = steps(URI_USERINFO_PATTERN, &cps(&unit.repeat(n)));
        let large = steps(URI_USERINFO_PATTERN, &cps(&unit.repeat(2 * n)));
        assert!(large * 10 <= small * 22, "{unit}: {large} against {small}");
    }
}

#[test]
fn the_detectors_are_linear_in_time() {
    for (name, unit) in shapes() {
        let small = cps(&unit.repeat(3000 / unit.len()));
        let large = cps(&unit.repeat(30000 / unit.len()));
        timing::grows_within(
            name,
            30,
            &mut || timing::once(|| find_credential_markers(&small)),
            &mut || timing::once(|| find_credential_markers(&large)),
        );
    }
}

#[test]
fn odd_texts_answer() {
    assert!(secret_spans(&[]).is_empty());
    assert!(find_credential_markers(&[0xD800]).is_empty());
    let spaces = vec![u32::from(' '); 100_000];
    assert!(find_credential_markers(&spaces).is_empty());
    let mut text = cps(&format!("password={S}"));
    text.push(0xDFFF);
    assert_eq!(
        secret_spans(&text),
        vec![(9, 22, "generic-secret-assignment")]
    );
    let many = cps(&format!("password={S} ").repeat(5000));
    assert_eq!(secret_spans(&many).len(), 5000);
    assert_eq!(redact_all(&many).len(), 5000 * 46);
    // Arguments past the text clamp as Python's slices do.
    assert!(safe_snippet(&many, 0, 0, "x").starts_with(&cps("password=[redacted:")));
    let len = many.len();
    let tail = safe_snippet(&many, len + 10, len + 20, "x");
    assert!(tail.is_empty() || tail.starts_with(&cps("...")));
}

fn nested(depth: usize, wrap: impl Fn(PyValue) -> PyValue) -> PyValue {
    let mut v = PyValue::str("leaf");
    for _ in 0..depth {
        v = wrap(v);
    }
    v
}

#[test]
fn deep_and_wide_trees() {
    let null = || PyValue::Dict(vec![(PyValue::str("type"), PyValue::str("null"))]);
    let mut deep = nested(100_000, |v| {
        PyValue::Dict(vec![
            (PyValue::str("title"), PyValue::str("t")),
            (
                PyValue::str("properties"),
                PyValue::Dict(vec![(
                    PyValue::str("p"),
                    PyValue::Dict(vec![(
                        PyValue::str("anyOf"),
                        PyValue::List(vec![v, null()]),
                    )]),
                )]),
            ),
        ])
    });
    strip_titles(&mut deep);
    fold_nullable(&mut deep);
    let mut lists = nested(100_000, |v| PyValue::List(vec![v]));
    strip_titles(&mut lists);
    fold_nullable(&mut lists);
    let mut wide = PyValue::List(
        (0..100_000)
            .map(|i| {
                PyValue::Dict(vec![
                    (PyValue::str("title"), PyValue::int(i)),
                    (PyValue::str("default"), PyValue::None),
                ])
            })
            .collect(),
    );
    strip_titles(&mut wide);
    assert_eq!(wide, PyValue::List(vec![PyValue::Dict(vec![]); 100_000]));
}
