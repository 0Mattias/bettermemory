//! `pysyntax::literal` over hostile input: long lists, large sets, deep
//! nesting, long sign chains. No panic in debug or release, and time
//! linear (or n log n for the sort of a set) where the reference's is.

mod timing;

use bettermemory_core::pysyntax::literal::{canonical_repr, literal_eval, Literal, LiteralError};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn list(n: usize) -> Vec<u32> {
    cps(&format!("[{}]", "1, 'a', 2.5, ".repeat(n / 3)))
}

#[test]
fn a_100000_element_list_is_linear() {
    let (small, large) = (list(10_000), list(100_000));
    match literal_eval(&large).unwrap() {
        Literal::List(items) => assert_eq!(items.len(), 99_999),
        other => panic!("{other:?}"),
    }
    timing::grows_within(
        "list: 10,000 and 100,000 elements",
        timing::TENFOLD,
        &mut || timing::once(|| canonical_repr(&literal_eval(&small).unwrap())),
        &mut || timing::once(|| canonical_repr(&literal_eval(&large).unwrap())),
    );
}

fn set(n: usize) -> Vec<u32> {
    let items: Vec<String> = (0..n).map(|i| format!("{}", (i * 7919) % n)).collect();
    cps(&format!("{{{}, 0.0, True}}", items.join(", ")))
}

#[test]
fn a_set_of_50000_members_sorts_in_n_log_n() {
    let (small, large) = (set(5_000), set(50_000));
    match literal_eval(&large).unwrap() {
        Literal::Set(items) => assert_eq!(items.len(), 50_000),
        other => panic!("{other:?}"),
    }
    timing::grows_within(
        "set: 5,000 and 50,000 members",
        timing::TENFOLD,
        &mut || timing::once(|| canonical_repr(&literal_eval(&small).unwrap())),
        &mut || timing::once(|| canonical_repr(&literal_eval(&large).unwrap())),
    );
}

#[test]
fn a_dict_of_50000_equal_keys_is_one_pair() {
    let t = cps(&format!("{{{}}}", "1: 2, 1.0: 3, True: 4, ".repeat(16_667)));
    match literal_eval(&t).unwrap() {
        Literal::Dict(pairs) => assert_eq!(pairs.len(), 1),
        other => panic!("{other:?}"),
    }
}

#[test]
fn nesting_to_the_bracket_limit() {
    for open in ["[", "(", "{1: "] {
        let close = if open == "[" {
            "]"
        } else if open == "(" {
            ")"
        } else {
            "}"
        };
        let t = cps(&format!("{}1{}", open.repeat(200), close.repeat(200)));
        assert!(canonical_repr(&literal_eval(&t).unwrap()).is_ok(), "{open}");
        let t = cps(&format!("{}1{}", open.repeat(201), close.repeat(201)));
        assert!(literal_eval(&t).is_err());
    }
    let t = cps(&format!("{}1{}", "{".repeat(200), "}".repeat(200)));
    assert!(matches!(literal_eval(&t), Err(LiteralError::Invalid(_))));
}

#[test]
fn long_sign_and_sum_chains_are_refused_at_once() {
    for t in [
        format!("{}1", "-".repeat(1_000_000)),
        format!("{}1j", "1+".repeat(300_000)),
        format!("{}1", "-(".repeat(200)),
    ] {
        assert!(literal_eval(&cps(&t)).is_err());
    }
}

#[test]
fn integer_and_float_boundaries() {
    let max = cps(&format!("{}+0j", (1u128 << 127)));
    assert!(literal_eval(&max).is_ok());
    let over = cps(&format!("1{}+1j", "0".repeat(309)));
    assert!(literal_eval(&over).is_err());
    let huge = cps(&format!("0x{}", "f".repeat(100_000)));
    let v = literal_eval(&huge).unwrap();
    assert!(canonical_repr(&v).is_err());
    let floats = cps("{1.7976931348623157e308, 5e-324, 1e309, -1e309, 0.0}");
    assert!(canonical_repr(&literal_eval(&floats).unwrap()).is_ok());
}
