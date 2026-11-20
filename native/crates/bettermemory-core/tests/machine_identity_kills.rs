//! Cases a plausible wrong port of `machine`'s identity record fails.

use bettermemory_core::machine::{self as m, MachineFacts};
use bettermemory_core::pyvalue::{PyError, PyValue};

fn s(text: &str) -> PyValue {
    PyValue::str(text)
}

fn cps(text: &str) -> Vec<u32> {
    text.chars().map(u32::from).collect()
}

fn dict(pairs: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(pairs.into_iter().map(|(k, v)| (s(k), v)).collect())
}

fn id_file(parts: Vec<PyValue>) -> PyValue {
    dict(vec![("id_file", PyValue::List(parts))])
}

fn facts(h: Option<&str>, n: Option<&str>, f: Option<(&str, &str, Option<&str>)>) -> MachineFacts {
    MachineFacts {
        hardware: h.map(cps),
        hostname: n.map(cps),
        id_file: f.map(|(d, i, b)| (cps(d), cps(i), b.map(cps))),
    }
}

#[test]
fn the_id_is_written_least_significant_first() {
    let mut low = [0u8; 17];
    low[16] = 1;
    assert_eq!(m::new_machine_id(&low), format!("1{}", "0".repeat(25)));
    // The top six bits of the first byte are never read.
    let mut high = [0u8; 17];
    high[0] = 0xfc;
    assert_eq!(m::new_machine_id(&high), "0".repeat(26));
}

#[test]
fn an_empty_hardware_id_does_not_decide() {
    let a = facts(Some(""), Some("h"), Some(("1", "2", None)));
    let b = facts(Some("B"), Some("h"), Some(("1", "3", None)));
    assert!(!m::is_clone(&a, &b));
    let c = facts(Some("A"), Some("h"), None);
    let d = facts(Some("B"), Some("h"), None);
    assert!(m::is_clone(&c, &d));
    // Without hardware both facts must differ.
    let e = facts(None, Some("h"), None);
    let f = facts(None, Some("g"), None);
    assert!(!m::is_clone(&e, &f));
}

#[test]
fn truthiness_is_pythons() {
    assert!(m::truthy(&PyValue::Float(f64::NAN)));
    assert!(!m::truthy(&PyValue::Float(-0.0)));
    assert!(m::truthy(&s("0")));
    assert!(!m::truthy(&PyValue::int(0)));
    assert!(m::truthy(&PyValue::List(vec![PyValue::int(0)])));
    let raw = dict(vec![
        ("id", s("A")),
        ("facts", dict(vec![])),
        ("minted", s("0")),
        ("cloned", PyValue::Float(f64::NAN)),
        ("facts_updated", PyValue::Float(-0.0)),
    ]);
    let got = m::identity_from(&raw).unwrap().unwrap();
    assert!(got.minted && got.cloned && !got.facts_updated);
}

#[test]
fn a_tuple_is_no_id_file_and_parts_go_through_str() {
    let raw = dict(vec![(
        "id_file",
        PyValue::Tuple(vec![s("1"), s("2"), PyValue::None]),
    )]);
    assert_eq!(
        MachineFacts::from_dict(&raw).unwrap().unwrap().id_file,
        None
    );
    let raw = id_file(vec![
        PyValue::Bool(true),
        PyValue::List(vec![PyValue::Float(1e16), PyValue::None, s("a'")]),
        PyValue::Tuple(vec![PyValue::int(1)]),
    ]);
    let got = MachineFacts::from_dict(&raw)
        .unwrap()
        .unwrap()
        .id_file
        .unwrap();
    assert_eq!(
        got,
        (cps("True"), cps("[1e+16, None, \"a'\"]"), Some(cps("(1,)")))
    );
    // None is kept only as the birth time.
    let raw = id_file(vec![PyValue::None, PyValue::None, PyValue::None]);
    let got = MachineFacts::from_dict(&raw)
        .unwrap()
        .unwrap()
        .id_file
        .unwrap();
    assert_eq!(got, (cps("None"), cps("None"), None));
}

#[test]
fn the_digit_limit_counts_no_sign() {
    let digits = |n: usize, neg: bool| {
        PyValue::Int(format!("{}{}", if neg { "-" } else { "" }, "9".repeat(n)))
    };
    let ok = id_file(vec![digits(4300, true), digits(4300, false), PyValue::None]);
    assert!(MachineFacts::from_dict(&ok).is_ok());
    let bad = id_file(vec![digits(4301, true), PyValue::int(1), PyValue::None]);
    assert_eq!(MachineFacts::from_dict(&bad), Err(PyError::ValueError));
}

#[test]
fn recorded_is_read_after_facts_and_only_with_them() {
    let big = id_file(vec![
        PyValue::Int("1".repeat(4301)),
        PyValue::int(1),
        PyValue::None,
    ]);
    let mut deep = PyValue::int(1);
    for _ in 0..10_000 {
        deep = PyValue::List(vec![deep]);
    }
    let deep = id_file(vec![deep, PyValue::int(1), PyValue::None]);
    let no_facts = dict(vec![
        ("id", s("A")),
        ("facts", s("x")),
        ("recorded", big.clone()),
    ]);
    assert_eq!(m::identity_from(&no_facts), Ok(None));
    let both = dict(vec![("id", s("A")), ("facts", deep), ("recorded", big)]);
    assert_eq!(m::identity_from(&both), Err(PyError::RecursionError));
}

#[test]
fn keys_keep_the_reference_order_and_the_last_duplicate() {
    let raw = PyValue::Dict(vec![
        (s("hostname"), s("first")),
        (s("hostname"), s("last")),
    ]);
    let f = MachineFacts::from_dict(&raw).unwrap().unwrap();
    assert_eq!(f.hostname, Some(cps("last")));
    let dict_of_f = f.to_dict();
    let PyValue::Dict(items) = &dict_of_f else {
        panic!()
    };
    let keys: Vec<_> = items.iter().map(|(k, _)| k.clone()).collect();
    assert_eq!(keys, vec![s("hardware"), s("hostname"), s("id_file")]);
    let ident = m::MachineIdentity {
        id: cps("A"),
        facts: f,
        predecessor: None,
        minted: true,
        cloned: false,
        facts_updated: false,
        recorded: None,
    };
    let json = m::identity_to_json(&ident);
    let PyValue::Dict(items) = &json else {
        panic!()
    };
    let keys: Vec<_> = items.iter().map(|(k, _)| k.clone()).collect();
    let want = [
        "id",
        "facts",
        "predecessor",
        "minted",
        "cloned",
        "facts_updated",
        "recorded",
    ];
    assert_eq!(keys, want.iter().map(|k| s(k)).collect::<Vec<_>>());
    assert_eq!(items[3].1, PyValue::Bool(true));
}
