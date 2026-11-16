//! Hostile input to the capture vocabulary: tokens of every wrong type at
//! every key, 100,000-deep nesting read on a 512 KiB thread, 10 MB texts
//! and URLs. Every outcome is typed (a capture, a refusal, or the
//! exception class the reference lets through); nothing panics and
//! nothing recurses on the input's depth. No golden file: these answers
//! are the port's own, with the reference's where the golden test pins
//! them.

use bettermemory_core::capture::location::{to_location, ObjectKey};
use bettermemory_core::capture::paths;
use bettermemory_core::capture::remote::{remote_key, RemoteKey};
use bettermemory_core::capture::{Capture, State, TOKEN_KEYS};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::pyvalue::{PyError, PyValue};
use bettermemory_core::unicode;

const DEEP: usize = 100_000;
const BIG: usize = 10 * 1024 * 1024;

fn deep(leaf: PyValue) -> PyValue {
    let mut v = leaf;
    for _ in 0..DEEP {
        v = PyValue::List(vec![v]);
    }
    v
}

fn deep_dict() -> PyValue {
    let mut v = PyValue::None;
    for _ in 0..DEEP {
        v = PyValue::Dict(vec![(PyValue::str("a"), v)]);
    }
    v
}

fn on_small_stack(f: impl FnOnce() + Send + 'static) {
    std::thread::Builder::new()
        .stack_size(512 * 1024)
        .spawn(f)
        .expect("a thread")
        .join()
        .expect("no panic on a 512 KiB stack");
}

fn base() -> Vec<(PyValue, PyValue)> {
    let mut cap = Capture::new(unicode::cps("/work/alpha"), State::Repo);
    cap.object_key = Some((unicode::cps("1"), unicode::cps("2"), None));
    match &cap.to_token() {
        PyValue::Dict(items) => items.clone(),
        _ => unreachable!(),
    }
}

fn with(key: &str, value: PyValue) -> PyValue {
    let mut items = base();
    match items.iter_mut().find(|(k, _)| *k == PyValue::str(key)) {
        Some(slot) => slot.1 = value,
        None => items.push((PyValue::str(key), value)),
    }
    PyValue::Dict(items)
}

fn typed(outcome: Result<Option<Capture>, PyError>) -> &'static str {
    match outcome {
        Ok(Some(_)) => "capture",
        Ok(None) => "refused",
        Err(PyError::OverflowError) => "OverflowError",
        Err(PyError::RecursionError) => "RecursionError",
        Err(other) => panic!("an exception the reference catches leaked: {other:?}"),
    }
}

fn wrong_values() -> Vec<PyValue> {
    vec![
        PyValue::None,
        PyValue::Bool(true),
        PyValue::Bool(false),
        PyValue::int(0),
        PyValue::Int("9".repeat(4301)),
        PyValue::Float(f64::NAN),
        PyValue::Float(f64::INFINITY),
        PyValue::Float(-0.0),
        PyValue::str(""),
        PyValue::Str(vec![0xD800]),
        PyValue::Path(unicode::cps("/work")),
        PyValue::DateTime(
            DateTime::from_isoformat(&unicode::cps("2026-01-01T00:00:00")).expect("a datetime"),
        ),
        PyValue::Tuple(vec![PyValue::str("a"), PyValue::str("b"), PyValue::None]),
        PyValue::List(vec![PyValue::Float(f64::INFINITY); 5]),
        PyValue::List(vec![PyValue::List(vec![
            PyValue::str("s"),
            PyValue::Float(f64::INFINITY),
        ])]),
        PyValue::List(vec![PyValue::Dict(vec![(
            PyValue::int(0),
            PyValue::str("s"),
        )])]),
        PyValue::Dict(vec![(PyValue::int(1), PyValue::int(1))]),
        PyValue::Dict(vec![(
            PyValue::str("entries"),
            PyValue::List(vec![PyValue::str("p9")]),
        )]),
    ]
}

#[test]
fn every_key_with_every_wrong_type() {
    let mut seen = std::collections::BTreeMap::new();
    for key in TOKEN_KEYS.iter().chain(["extra"].iter()) {
        for value in wrong_values() {
            let token = with(key, value);
            *seen.entry(typed(Capture::from_token(&token))).or_insert(0) += 1;
        }
    }
    for raw in wrong_values() {
        assert_eq!(typed(Capture::from_token(&raw)), "refused");
    }
    assert!(seen.contains_key("capture") && seen.contains_key("refused"));
    assert!(seen.contains_key("OverflowError"));
}

#[test]
fn deep_nesting_on_a_small_stack() {
    on_small_stack(|| {
        for key in ["object", "common_object", "root_object", "cwd_object"] {
            let items = PyValue::List(vec![
                deep(PyValue::int(0)),
                PyValue::str("1"),
                PyValue::None,
            ]);
            assert_eq!(
                typed(Capture::from_token(&with(key, items))),
                "RecursionError"
            );
            let items = PyValue::List(vec![PyValue::str("1"), PyValue::str("1"), deep_dict()]);
            assert_eq!(
                typed(Capture::from_token(&with(key, items))),
                "RecursionError"
            );
        }
        let file = PyValue::Dict(vec![
            (PyValue::str("id"), PyValue::str("i")),
            (PyValue::str("name"), PyValue::str("n")),
            (PyValue::str("dir"), PyValue::str("d")),
            (PyValue::str("path"), PyValue::str("p")),
            (
                PyValue::str("object"),
                PyValue::List(vec![deep(PyValue::None), PyValue::str("1"), PyValue::None]),
            ),
        ]);
        let token = with("files", PyValue::List(vec![file]));
        assert_eq!(typed(Capture::from_token(&token)), "RecursionError");
        for key in TOKEN_KEYS {
            let outcome = typed(Capture::from_token(&with(key, deep(PyValue::str("x")))));
            assert!(matches!(outcome, "capture" | "refused"), "{key}: {outcome}");
            let outcome = typed(Capture::from_token(&with(key, deep_dict())));
            assert!(matches!(outcome, "capture" | "refused"), "{key}: {outcome}");
        }
        assert_eq!(typed(Capture::from_token(&deep(PyValue::None))), "refused");
        let entries = PyValue::Dict(vec![
            (PyValue::str("name"), PyValue::str("m")),
            (
                PyValue::str("entries"),
                PyValue::List(vec![PyValue::List(vec![
                    PyValue::str("p"),
                    deep(PyValue::int(1)),
                ])]),
            ),
        ]);
        assert_eq!(
            typed(Capture::from_token(&with("manifest", entries))),
            "refused"
        );
    });
}

#[test]
fn big_texts() {
    on_small_stack(|| {
        let big = |c: char| unicode::cps(&c.to_string().repeat(BIG));
        let token = with("cwd", PyValue::Str(big('a')));
        let cap = Capture::from_token(&token)
            .expect("no raise")
            .expect("a capture");
        assert_eq!(cap.cwd.len(), BIG);
        assert_eq!(cap.to_token().get("cwd"), token.get("cwd"));
        let loc = to_location(&cap, &big('m'), None);
        assert_eq!(loc.machine.len(), BIG);
        assert_eq!(
            typed(Capture::from_token(&with("head", PyValue::Str(big('a'))))),
            "refused"
        );
        let key = PyValue::List(vec![
            PyValue::Str(big('1')),
            PyValue::Str(big('2')),
            PyValue::None,
        ]);
        assert_eq!(typed(Capture::from_token(&with("object", key))), "capture");
        let stamp = PyValue::List(vec![PyValue::List(vec![
            PyValue::str("s"),
            PyValue::Str(big('7')),
            PyValue::int(0),
            PyValue::int(0),
            PyValue::int(0),
        ])]);
        assert_eq!(
            typed(Capture::from_token(&with("stamps", stamp))),
            "refused"
        );
        let k = ObjectKey {
            device: big('1'),
            inode: big('2'),
            birth: None,
        };
        assert_eq!(k.compare(Some(&k)).as_str(), "unproven");
    });
}

#[test]
fn big_and_odd_urls() {
    on_small_stack(|| {
        let cps = |s: &str| unicode::cps(s);
        let pad = "a".repeat(BIG);
        let at = "@".repeat(BIG);
        let colons = ":".repeat(BIG);
        let cases = [
            format!("ssh://{pad}/x"),
            format!("ssh://{at}h/x"),
            format!("ssh://h:{}/x", "1".repeat(BIG)),
            format!("{pad}:x"),
            format!("{at}:{at}"),
            format!("x{colons}"),
            format!("ssh://h/{}", "/".repeat(BIG)),
            format!("{}a:b{}", " ".repeat(BIG), "\u{3000}".repeat(BIG / 3)),
            format!("file://{pad}"),
            "\n".repeat(BIG),
            format!("ssh://h/{pad}\n"),
        ];
        for url in &cases {
            match remote_key(Some(&cps(url))) {
                None | Some(RemoteKey::Hosted(_)) | Some(RemoteKey::Local { .. }) => {}
            }
        }
        let hosted = remote_key(Some(&cps(&format!("ssh://h/{}.git", pad))));
        assert!(matches!(hosted, Some(RemoteKey::Hosted(k)) if k.len() == BIG + 2));
        let p = cps(&format!("/{}", "w/".repeat(BIG / 2)));
        assert!(paths::inside(&p, &cps("/w")));
        assert_eq!(paths::basename(&p), cps("w"));
        let slashes = cps(&"\\".repeat(BIG));
        assert_eq!(paths::norm(&slashes), slashes);
    });
}

fn nest(n: usize) -> PyValue {
    let mut v = PyValue::int(0);
    for _ in 0..n {
        v = PyValue::List(vec![v]);
    }
    v
}

/// The native `str()` depth limit at its boundary: `STR_DEPTH_TOP`
/// levels at a top-level key, `STR_DEPTH_FILES` inside `files` (the
/// values CPython 3.13 gives on Linux and macOS on a fresh thread; not a
/// parity claim, so not in the golden file). And the int digit limit:
/// 4,300 digits are spelled, 4,301 refused, either sign.
#[test]
fn str_limits_at_their_boundaries() {
    on_small_stack(|| {
        use bettermemory_core::capture::{STR_DEPTH_FILES, STR_DEPTH_TOP};
        let top = |n: usize| {
            let key = PyValue::List(vec![nest(n), PyValue::str("1"), PyValue::None]);
            typed(Capture::from_token(&with("object", key)))
        };
        assert_eq!(top(STR_DEPTH_TOP - 1), "capture");
        assert_eq!(top(STR_DEPTH_TOP), "RecursionError");
        let in_files = |n: usize| {
            let file = PyValue::Dict(vec![
                (PyValue::str("id"), PyValue::str("i")),
                (PyValue::str("name"), PyValue::str("n")),
                (PyValue::str("dir"), PyValue::str("d")),
                (PyValue::str("path"), PyValue::str("p")),
                (
                    PyValue::str("object"),
                    PyValue::List(vec![nest(n), PyValue::str("1"), PyValue::None]),
                ),
            ]);
            typed(Capture::from_token(&with(
                "files",
                PyValue::List(vec![file]),
            )))
        };
        assert_eq!(in_files(STR_DEPTH_FILES - 1), "capture");
        assert_eq!(in_files(STR_DEPTH_FILES), "RecursionError");
        for (digits, want) in [(4300, "capture"), (4301, "refused")] {
            for sign in ["", "-"] {
                let big = PyValue::Int(format!("{sign}{}", "9".repeat(digits)));
                let key = PyValue::List(vec![PyValue::str("1"), big, PyValue::None]);
                assert_eq!(typed(Capture::from_token(&with("object", key))), want);
            }
        }
    });
}
