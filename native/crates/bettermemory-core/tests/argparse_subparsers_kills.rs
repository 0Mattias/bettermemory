//! Inputs a plausible wrong port of `argparse`'s subparsers misreads, each
//! answer the reference's (3.13.13).

use bettermemory_core::argparse::{Arg, ArgumentParser, Kind, Parsed, Unfilled};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;

fn argv(words: &[&str]) -> Vec<PyStr> {
    words.iter().map(|w| cps(w)).collect()
}

/// `tree base {add,rm} ... tail`, the subparsers without a dest.
fn tree() -> ArgumentParser {
    let mut p = ArgumentParser::new("tree", None, true);
    p.add_argument(&["base"], Arg::default()).unwrap();
    let flag = Arg {
        kind: Some(Kind::StoreTrue),
        ..Arg::default()
    };
    p.add_argument(&["-v"], flag.clone()).unwrap();
    p.add_subparsers(None, 78).unwrap();
    let add = p.add_parser("add", Some("add one"), None).unwrap();
    add.add_argument(&["-n"], flag).unwrap();
    let child_v = Arg {
        default: Some(PyValue::str("child")),
        ..Arg::default()
    };
    add.add_argument(&["-v"], child_v).unwrap();
    p.add_parser("rm", None, None).unwrap();
    p.add_argument(&["tail"], Arg::default()).unwrap();
    p
}

fn ns(p: &ArgumentParser, words: &[&str]) -> Vec<(String, PyValue)> {
    match p.parse_args(&argv(words), 78, &Unfilled).unwrap() {
        Parsed::Namespace(ns) => ns
            .into_iter()
            .map(|(k, v)| (k.iter().map(|&c| char::from_u32(c).unwrap()).collect(), v))
            .collect(),
        Parsed::Exit(o) => panic!("exit {o:?}"),
    }
}

fn err(p: &ArgumentParser, words: &[&str]) -> String {
    match p.parse_args(&argv(words), 78, &Unfilled).unwrap() {
        Parsed::Exit(o) => o
            .stderr
            .iter()
            .map(|&c| char::from_u32(c).unwrap())
            .collect(),
        Parsed::Namespace(ns) => panic!("namespace {ns:?}"),
    }
}

#[test]
fn the_parser_pattern_gives_back_what_a_later_positional_needs() {
    // A greedy `[-AO]*` that never gives back hands `t` to the child.
    let got = ns(&tree(), &["b", "add", "t"]);
    assert_eq!(got[2], ("tail".into(), PyValue::str("t")));
}

#[test]
fn the_child_namespace_overwrites_in_place_and_appends() {
    let got = ns(&tree(), &["b", "add", "t"]);
    let keys: Vec<&str> = got.iter().map(|(k, _)| k.as_str()).collect();
    assert_eq!(keys, ["base", "v", "tail", "n"]);
    assert_eq!(got[1].1, PyValue::str("child"));
}

#[test]
fn the_parents_leftovers_come_before_the_childs() {
    let e = err(&tree(), &["b", "add", "-x", "t", "-y"]);
    assert!(
        e.ends_with("tree: error: unrecognized arguments: -y -x\n"),
        "{e}"
    );
}

#[test]
fn an_unnamed_subparsers_action_is_named_by_its_choices() {
    let e = err(&tree(), &["b", "mv", "t"]);
    assert!(
        e.ends_with("argument {add,rm}: invalid choice: 'mv' (choose from add, rm)\n"),
        "{e}"
    );
    assert!(
        e.starts_with("usage: tree [-h] [-v] base {add,rm} ... tail\n"),
        "{e}"
    );
}

#[test]
fn only_a_leading_double_dash_is_removed_for_the_parser() {
    // `--` after the name reaches the child, which removes it itself.
    let got = ns(&tree(), &["b", "--", "add", "t"]);
    assert_eq!(got[2], ("tail".into(), PyValue::str("t")));
    let e = err(&tree(), &["b", "add", "--", "t"]);
    assert!(e.ends_with("unrecognized arguments: --\n"), "{e}");
}

#[test]
fn the_child_prog_carries_the_parents_positionals() {
    let mut p = ArgumentParser::new("tree", None, true);
    p.add_argument(&["base"], Arg::default()).unwrap();
    p.add_subparsers(Some("cmd"), 78).unwrap();
    let rm = p.add_parser("rm", None, None).unwrap();
    rm.add_argument(&["name"], Arg::default()).unwrap();
    let e = err(&p, &["b", "rm"]);
    assert_eq!(
        e,
        "usage: tree base rm [-h] name\ntree base rm: error: the following arguments are required: name\n"
    );
}

#[test]
fn no_parsers_lists_no_choices() {
    let mut p = ArgumentParser::new("none", None, false);
    p.add_subparsers(Some("c"), 78).unwrap();
    assert_eq!(
        err(&p, &["x"]),
        "usage: none {} ...\nnone: error: argument c: invalid choice: 'x' (choose from )\n"
    );
    assert_eq!(ns(&p, &[]), [("c".to_string(), PyValue::None)]);
}

#[test]
fn an_unnamed_action_with_one_parser_is_named_by_it() {
    let mut p = ArgumentParser::new("one", None, true);
    p.add_subparsers(None, 78).unwrap();
    p.add_parser("go", None, None).unwrap();
    assert_eq!(
        err(&p, &["stay"]),
        "usage: one [-h] {go} ...\none: error: argument {go}: invalid choice: 'stay' (choose from go)\n"
    );
}

#[test]
fn an_empty_dest_names_the_argument_empty() {
    let mut p = ArgumentParser::new("e", None, true);
    p.add_subparsers(Some(""), 78).unwrap();
    p.add_parser("a", None, None).unwrap();
    assert_eq!(
        err(&p, &["b"]),
        "usage: e [-h] {a} ...\ne: error: argument : invalid choice: 'b' (choose from a)\n"
    );
}

#[test]
fn a_dest_named_unrecognized_args_declines() {
    // 3.13.13 reads the attribute as its own leftovers and answers
    // "unrecognized arguments: v"; that reading is not ported.
    let mut p = ArgumentParser::new("u", None, true);
    let a = Arg {
        dest: Some("_unrecognized_args".into()),
        ..Arg::default()
    };
    p.add_argument(&["--x"], a).unwrap();
    assert!(p.parse_args(&argv(&["--x", "v"]), 78, &Unfilled).is_err());
}
