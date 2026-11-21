//! Inputs a plausible wrong `argparse` port answers otherwise than
//! CPython 3.13.13 (each expectation read from the reference).

use bettermemory_core::argparse::{
    Arg, ArgumentParser, Kind, Outcome, Parsed, SpecError, Unfilled,
};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;

fn parser(args: &[(&[&str], Arg)]) -> ArgumentParser {
    let mut p = ArgumentParser::new("k", None, true);
    for (names, arg) in args {
        p.add_argument(names, arg.clone()).unwrap();
    }
    p
}

fn store_true() -> Arg {
    Arg {
        kind: Some(Kind::StoreTrue),
        ..Arg::default()
    }
}

fn run(p: &ArgumentParser, argv: &[&str], width: i64) -> Parsed {
    let argv: Vec<PyStr> = argv.iter().map(|a| cps(a)).collect();
    p.parse_args(&argv, width, &Unfilled).unwrap()
}

fn ns(pairs: &[(&str, PyValue)]) -> Parsed {
    Parsed::Namespace(pairs.iter().map(|(k, v)| (cps(k), v.clone())).collect())
}

fn err(text: &str) -> Parsed {
    Parsed::Exit(Outcome {
        code: 2,
        stdout: Vec::new(),
        stderr: cps(text),
    })
}

/// `$` matches before a final newline: `-1\n` is a negative number too,
/// and so a positional; `-1x` is an unknown option.
#[test]
fn negative_numbers_are_positionals_and_dollar_takes_a_newline() {
    let p = parser(&[(&["n"], Arg::default()), (&["--foo-bar"], Arg::default())]);
    for a in ["-1", "-1\n"] {
        assert_eq!(
            run(&p, &[a], 78),
            ns(&[("n", PyValue::str(a)), ("foo_bar", PyValue::None)])
        );
    }
    assert_eq!(
        run(&p, &["-1x"], 78),
        err("usage: k [-h] [--foo-bar FOO_BAR] n\nk: error: the following arguments are required: n\n")
    );
}

/// A cluster of single-dash flags ends in an option taking the rest; an
/// `=` after a flag of the cluster is refused against that flag.
#[test]
fn short_clusters() {
    let p = parser(&[
        (&["-x", "--long-name"], store_true()),
        (&["-y"], store_true()),
        (&["-z"], Arg::default()),
    ]);
    assert_eq!(
        run(&p, &["-xyzval"], 78),
        ns(&[
            ("long_name", PyValue::Bool(true)),
            ("y", PyValue::Bool(true)),
            ("z", PyValue::str("val"))
        ])
    );
    let usage = "usage: k [-h] [-x] [-y] [-z Z]\n";
    assert_eq!(
        run(&p, &["-xy=1"], 78),
        err(&format!(
            "{usage}k: error: argument -y: ignored explicit argument '1'\n"
        ))
    );
    assert_eq!(
        run(&p, &["-yx=1"], 78),
        err(&format!(
            "{usage}k: error: argument -x/--long-name: ignored explicit argument '1'\n"
        ))
    );
}

/// Every prefix match is named, the whole argument (with its `=`) quoted.
#[test]
fn ambiguous_prefixes() {
    let p = parser(&[
        (&["--alpha"], Arg::default()),
        (&["--alps"], Arg::default()),
    ]);
    let usage = "usage: k [-h] [--alpha ALPHA] [--alps ALPS]\n";
    assert_eq!(
        run(&p, &["--alp=v"], 78),
        err(&format!(
            "{usage}k: error: ambiguous option: --alp=v could match --alpha, --alps\n"
        ))
    );
}

/// Only the first `--` is the marker; a second is an argument.
#[test]
fn only_the_first_double_dash_is_removed() {
    let p = parser(&[(&["a"], Arg::default()), (&["b"], Arg::default())]);
    assert_eq!(
        run(&p, &["--", "--", "x"], 78),
        ns(&[("a", PyValue::str("--")), ("b", PyValue::str("x"))])
    );
    assert_eq!(
        run(&p, &[], 78),
        err("usage: k [-h] a b\nk: error: the following arguments are required: a, b\n")
    );
}

/// The usage line wraps only past the width, not at it.
#[test]
fn usage_wraps_past_the_width() {
    let p = parser(&[(&["--abc"], Arg::default())]);
    assert_eq!(
        run(&p, &["--q"], 25),
        err("usage: k [-h] [--abc ABC]\nk: error: unrecognized arguments: --q\n")
    );
    assert_eq!(
        run(&p, &["--q"], 24),
        err("usage: k [-h]\n         [--abc ABC]\nk: error: unrecognized arguments: --q\n")
    );
}

/// Two actions of one dest: the first one's default is the namespace's.
#[test]
fn a_shared_dest_keeps_the_first_default() {
    let t = Arg {
        dest: Some("d".into()),
        default: Some(PyValue::str("t")),
        ..Arg::default()
    };
    let p = parser(&[
        (
            &["-s"],
            Arg {
                dest: Some("d".into()),
                ..Arg::default()
            },
        ),
        (&["-t"], t),
    ]);
    assert_eq!(run(&p, &[], 78), ns(&[("d", PyValue::None)]));
}

/// The version text's whitespace runs are one space, the ends stripped.
#[test]
fn version_text_is_collapsed() {
    let v = Arg {
        kind: Some(Kind::Version(cps("  a \t b\n"))),
        ..Arg::default()
    };
    let p = parser(&[(&["-V"], v)]);
    assert_eq!(
        run(&p, &["-V"], 78),
        Parsed::Exit(Outcome {
            code: 0,
            stdout: cps("a b\n"),
            stderr: Vec::new()
        })
    );
}

/// Specs the reference refuses in `add_argument`: an empty name
/// (`IndexError`), `dest=` beside a positional name, `required=` on a
/// positional, `help` and `version` as positionals (their classes take no
/// `required`), and the bare `--` as the long option (an empty dest).
#[test]
fn refused_specs() {
    let add =
        |names: &[&str], arg: Arg| ArgumentParser::new("k", None, true).add_argument(names, arg);
    assert_eq!(add(&[""], Arg::default()), Err(SpecError::EmptyName));
    assert_eq!(add(&["-x", ""], Arg::default()), Err(SpecError::EmptyName));
    let dest = Arg {
        dest: Some("y".into()),
        ..Arg::default()
    };
    assert_eq!(add(&["x"], dest.clone()), Err(SpecError::DestTwice));
    assert_eq!(add(&[], dest), Ok(()));
    for r in [true, false] {
        let req = Arg {
            required: Some(r),
            ..Arg::default()
        };
        assert_eq!(add(&["x"], req), Err(SpecError::PositionalRequired));
    }
    for kind in [Kind::Help, Kind::Version(cps("1"))] {
        let a = Arg {
            kind: Some(kind),
            ..Arg::default()
        };
        assert_eq!(add(&["v"], a), Err(SpecError::UnexpectedRequired));
    }
    assert_eq!(add(&["-x", "--"], Arg::default()), Err(SpecError::Dest));
}
