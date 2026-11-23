//! Append cases that distinguish namespace order, defaults, choice checks and replacement.

mod common;

use bettermemory_core::argparse::{Arg, ArgumentParser, Kind, Parsed, Unfilled};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;
use common::argparse::outcome;
use serde_json::json;

const WIDTH: i64 = 510;

fn parse(p: &ArgumentParser, argv: &[&str]) -> Parsed {
    p.parse_args(
        &argv.iter().map(|v| cps(v)).collect::<Vec<_>>(),
        WIDTH,
        &Unfilled,
    )
    .unwrap()
}

fn append(default: Option<PyValue>) -> Arg {
    Arg {
        kind: Some(Kind::Append),
        dest: Some("paths".into()),
        default,
        ..Arg::default()
    }
}

#[test]
fn append_uses_the_active_default_and_preserves_previous_results() {
    let mut p = ArgumentParser::new("sample", None, false);
    let flag = Arg {
        kind: Some(Kind::StoreTrue),
        ..Arg::default()
    };
    p.add_argument(&["--first"], flag.clone()).unwrap();
    let seed = PyValue::List(vec![PyValue::Bool(false), PyValue::str("seed")]);
    p.add_argument(&["-a", "--anchor"], append(Some(seed.clone())))
        .unwrap();
    p.add_argument(&["--other"], append(Some(PyValue::List(vec![]))))
        .unwrap();
    p.add_argument(&["--last"], flag).unwrap();
    let first = parse(&p, &["--other=x", "-ax", "--last"]);
    let empty = parse(&p, &[]);
    assert_eq!(
        outcome(&empty),
        json!({"ns":[["first",false],["paths",[false,"seed"]],["last",false]]})
    );
    assert_eq!(
        outcome(&first),
        json!({"ns":[["first",false],["paths",[false,"seed","x","x"]],["last",true]]})
    );
    assert_eq!(p.actions[1].default, Some(seed));
}

#[test]
fn subparser_overwrites_the_parent_destination_at_its_original_position() {
    let mut p = ArgumentParser::new("sample", None, false);
    p.add_argument(&["--outer"], append(None)).unwrap();
    p.add_subparsers(Some("command"), WIDTH).unwrap();
    let child = p.add_parser("child", None, None).unwrap();
    child
        .add_argument(&["--inner"], append(Some(PyValue::List(vec![]))))
        .unwrap();
    for (argv, values) in [
        (vec!["--outer=x", "child"], json!([])),
        (
            vec!["--outer=x", "child", "--inner=y", "--inner=y"],
            json!(["y", "y"]),
        ),
    ] {
        assert_eq!(
            outcome(&parse(&p, &argv)),
            json!({"ns":[["paths",values],["command","child"]]})
        );
    }
}

#[test]
fn positional_append_checks_choices_and_declines_a_non_list_destination() {
    let mut p = ArgumentParser::new("pos", None, false);
    p.add_argument(
        &["one"],
        Arg {
            kind: Some(Kind::Append),
            choices: Some(vec!["ok".into()]),
            ..Arg::default()
        },
    )
    .unwrap();
    assert_eq!(outcome(&parse(&p, &["ok"])), json!({"ns":[["one",["ok"]]]}));
    assert_eq!(
        outcome(&parse(&p, &["bad"]))["err"],
        "usage: pos {ok}\npos: error: argument one: invalid choice: 'bad' (choose from ok)\n"
    );

    let mut p = ArgumentParser::new("sample", None, false);
    p.add_argument(
        &["--replace"],
        Arg {
            dest: Some("paths".into()),
            ..Arg::default()
        },
    )
    .unwrap();
    p.add_argument(&["--append"], append(None)).unwrap();
    let argv: Vec<PyStr> = ["--replace=s", "--append=x"].into_iter().map(cps).collect();
    assert!(p.parse_args(&argv, WIDTH, &Unfilled).is_err());
    assert_eq!(
        outcome(&parse(&p, &["--append=x", "--replace=s"])),
        json!({"ns":[["paths","s"]]})
    );
}

#[test]
fn earlier_help_exits_before_a_later_missing_anchor() {
    struct ShowProgram;
    impl bettermemory_core::argparse::HelpText for ShowProgram {
        fn format_help(&self, parser: &ArgumentParser, _: i64) -> Option<PyStr> {
            Some(parser.prog.clone())
        }
    }
    let mut log = ArgumentParser::new("bettermemory log", None, true);
    log.add_subparsers(Some("log_cmd"), WIDTH).unwrap();
    bettermemory_core::cli::log_args::add_verify_parser(&mut log).unwrap();
    let args = ["verify", "--help", "--anchor"].map(cps);
    let result = log.parse_args(&args, WIDTH, &ShowProgram).unwrap();
    assert_eq!(
        outcome(&result),
        json!({"code":0,"out":"bettermemory log verify","err":""})
    );
}
