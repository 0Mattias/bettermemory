//! Cases a plausible wrong literal reader fails: set and dict equality
//! across types, the spelling of the empty set and of a one-element tuple,
//! signed zeros in complex values, and the forms `literal_eval` refuses.

use bettermemory_core::pysyntax::literal::{canonical_repr, literal_eval, Literal, LiteralError};

fn cps(s: &str) -> Vec<u32> {
    s.chars().map(u32::from).collect()
}

fn canon(s: &str) -> Result<String, LiteralError> {
    let r = canonical_repr(&literal_eval(&cps(s))?)?;
    Ok(r.iter().map(|&c| char::from_u32(c).unwrap()).collect())
}

fn refused(s: &str) {
    assert!(
        matches!(literal_eval(&cps(s)), Err(LiteralError::Invalid(_))),
        "accepted {s:?}"
    );
}

#[test]
fn a_set_keeps_the_first_of_equal_elements() {
    assert_eq!(canon("{1, 1.0, True}").unwrap(), "{1}");
    assert_eq!(canon("{True, 1, 1.0}").unwrap(), "{True}");
    assert_eq!(canon("{1.0, 1}").unwrap(), "{1.0}");
    assert_eq!(canon("{0.0, -0.0, 0j}").unwrap(), "{0.0}");
    assert_eq!(canon("{1+0j, 1}").unwrap(), "{(1+0j)}");
}

#[test]
fn a_complex_key_reads_a_negative_zero_real_part_as_zero() {
    assert_eq!(canon("{-1j, 0.0-1j}").unwrap(), "{(-0-1j)}");
    assert_eq!(canon("{0.0-1j: 1, -1j: 2}").unwrap(), "{-1j: 2}");
}

#[test]
fn an_integral_float_past_i64_keeps_its_exact_value() {
    assert_eq!(
        canon("{9223372036854775807, 9.5e18}").unwrap(),
        "{9.5e+18, 9223372036854775807}"
    );
    assert_eq!(
        canon("{9223372036854775807, 9.223372036854775807e18}").unwrap(),
        "{9.223372036854776e+18, 9223372036854775807}"
    );
    assert_eq!(
        canon("{9223372036854775808, 9.223372036854775807e18}").unwrap(),
        "{9223372036854775808}"
    );
}

#[test]
fn a_dict_keeps_the_first_key_and_the_last_value() {
    assert_eq!(canon("{1: 'a', 1.0: 'b', True: 'c'}").unwrap(), "{1: 'c'}");
    assert_eq!(canon("{'a': 1, 'a': 2}").unwrap(), "{'a': 2}");
}

#[test]
fn one_equals_true_and_str_never_equals_bytes() {
    assert_eq!(canon("{(1,), (True,)}").unwrap(), "{(1,)}");
    assert_eq!(canon("{'a', b'a'}").unwrap(), "{'a', b'a'}");
    assert_eq!(canon("{1, '1'}").unwrap(), "{'1', 1}");
}

#[test]
fn big_ints_equal_floats_only_exactly() {
    assert_eq!(
        canon("{9007199254740993, 9007199254740992.0}").unwrap(),
        "{9007199254740992.0, 9007199254740993}"
    );
    assert_eq!(
        canon("{1000000000000000019884624838656, 1e30}").unwrap(),
        "{1000000000000000019884624838656}"
    );
    assert_eq!(
        canon("{1000000000000000000000000000000, 1e30}").unwrap(),
        "{1000000000000000000000000000000, 1e+30}"
    );
}

#[test]
fn the_empty_set_and_one_element_tuples() {
    assert_eq!(canon("set()").unwrap(), "set()");
    assert_eq!(canon("(set)()").unwrap(), "set()");
    assert_eq!(canon("{}").unwrap(), "{}");
    assert_eq!(canon("(1,)").unwrap(), "(1,)");
    assert_eq!(canon("1,").unwrap(), "(1,)");
    assert_eq!(canon("(1)").unwrap(), "1");
    refused("set");
    refused("set(())");
    refused("set()()");
}

#[test]
fn complex_signs() {
    assert_eq!(canon("-0j").unwrap(), "(-0-0j)");
    assert_eq!(canon("0j").unwrap(), "0j");
    assert_eq!(canon("-0.0+0j").unwrap(), "0j");
    assert_eq!(canon("0.0-0j").unwrap(), "0j");
    assert_eq!(canon("-0.0-0j").unwrap(), "(-0+0j)");
    assert_eq!(canon("1-2j").unwrap(), "(1-2j)");
    assert_eq!(canon("1e16+1j").unwrap(), "(1e+16+1j)");
    assert_eq!(canon("-1e309j").unwrap(), "(-0-infj)");
}

#[test]
fn signs_apply_to_numbers_once() {
    refused("-True");
    refused("--1");
    refused("-(-1)");
    refused("-(1+2j)");
    refused("1+2j+3j");
    refused("1j+1j");
    refused("1+-2j");
    assert_eq!(canon("-(1)").unwrap(), "-1");
    assert_eq!(canon("-0").unwrap(), "0");
    assert_eq!(canon("-0.0").unwrap(), "-0.0");
}

#[test]
fn str_and_bytes_do_not_join() {
    refused("'a' b'b'");
    refused("b'a' 'b'");
    assert_eq!(canon("'a' 'b'").unwrap(), "'ab'");
    assert_eq!(canon("b'a' b'b'").unwrap(), "b'ab'");
}

#[test]
fn unhashable_members_are_refused() {
    refused("{[1]}");
    refused("{(1, [2])}");
    refused("{{}: 1}");
    refused("{set()}");
}

#[test]
fn bytes_repr_picks_its_quote() {
    assert_eq!(canon("b'\\''").unwrap(), "b\"'\"");
    assert_eq!(canon("b'\\'\"'").unwrap(), "b'\\'\"'");
    assert_eq!(
        canon("b'\\t\\x00\\x7f\\xff\\\\'").unwrap(),
        "b'\\t\\x00\\x7f\\xff\\\\'"
    );
}

#[test]
fn ints_past_4300_digits_have_no_repr() {
    let ok = "9".repeat(4300);
    assert_eq!(canon(&ok).unwrap(), ok);
    let big = Literal::Int("9".repeat(4301));
    assert!(canonical_repr(&big).is_err());
    let neg = Literal::Int(format!("-{}", "9".repeat(4300)));
    assert!(canonical_repr(&neg).is_ok());
}

#[test]
fn helpers() {
    let v = literal_eval(&cps("'x'")).unwrap();
    assert_eq!(v.as_str(), Some(&cps("x")));
    assert!(!v.is_container());
    assert!(literal_eval(&cps("()")).unwrap().is_container());
    assert!(literal_eval(&cps("set()")).unwrap().is_container());
}
