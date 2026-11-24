//! `cli/log_cmd.py::run`: the dispatch of `bettermemory log ...` after the
//! root parser selected it.

use super::root::Formatted;
use crate::argparse::{HelpText, Namespace, NotPorted, Outcome};
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode::cps;

fn attribute<'a>(args: &'a Namespace, name: &str) -> Option<&'a PyValue> {
    let name = cps(name);
    args.iter().find(|(k, _)| *k == name).map(|(_, v)| v)
}

/// The arguments `_cli_log_verify` takes from the namespace: `args.json`
/// and `args.anchor or ()`, in the caller's order, repeats included.
#[derive(Clone, Debug, PartialEq)]
pub struct VerifyArgs {
    pub json_out: bool,
    pub anchor_files: Vec<PyStr>,
}

/// `args.json` and `args.anchor or ()` of a parsed `log verify`.
pub fn verify_args(args: &Namespace) -> Result<VerifyArgs, NotPorted> {
    let json_out = match attribute(args, "json") {
        Some(PyValue::Bool(value)) => *value,
        _ => return Err(NotPorted),
    };
    let anchor_files = match attribute(args, "anchor") {
        Some(PyValue::None) => Vec::new(),
        Some(PyValue::List(items)) => items
            .iter()
            .map(|item| match item {
                PyValue::Str(name) => Ok(name.clone()),
                _ => Err(NotPorted),
            })
            .collect::<Result<_, _>>()?,
        _ => return Err(NotPorted),
    };
    Ok(VerifyArgs {
        json_out,
        anchor_files,
    })
}

/// `run(args, sub_parser=)` (247-270). `sub` is the `log` parser, the one
/// the reference hands every log command for its usage errors. No
/// subcommand prints that parser's help to stdout and exits 2, before any
/// configuration, store or key is looked at. `verify` is the command
/// itself; a sibling whose parser is not ported never parses, so any other
/// name is [`NotPorted`].
pub fn run(
    args: &Namespace,
    sub: &Formatted<'_>,
    help: &dyn HelpText,
    verify: impl FnOnce(VerifyArgs, &Formatted<'_>) -> Outcome,
) -> Result<Outcome, NotPorted> {
    match attribute(args, "log_cmd") {
        Some(PyValue::None) => Ok(Outcome {
            code: 2,
            stdout: help.format_help(sub.parser, sub.width).ok_or(NotPorted)?,
            stderr: Vec::new(),
        }),
        Some(PyValue::Str(name)) if *name == cps("verify") => Ok(verify(verify_args(args)?, sub)),
        _ => Err(NotPorted),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::argparse::help::DefaultHelp;
    use crate::argparse::Parsed;
    use crate::cli::root::{build_product_parser, subparser};

    const WIDTH: i64 = 78;

    fn parsed(argv: &[&str]) -> Namespace {
        let parser = build_product_parser("1", WIDTH).unwrap();
        let argv: Vec<PyStr> = argv.iter().map(|a| cps(a)).collect();
        match parser.parse_args(&argv, WIDTH, &DefaultHelp).unwrap() {
            Parsed::Namespace(ns) => ns,
            Parsed::Exit(o) => panic!("the parser exited: {o:?}"),
        }
    }

    fn ran(argv: &[&str]) -> (Result<Outcome, NotPorted>, Option<VerifyArgs>) {
        let root = build_product_parser("1", WIDTH).unwrap();
        let sub = Formatted {
            parser: subparser(&root, &cps("log")).unwrap(),
            width: WIDTH,
        };
        let mut seen = None;
        let outcome = run(&parsed(argv), &sub, &DefaultHelp, |args, parser| {
            assert_eq!(parser.parser.prog, cps("bettermemory log"));
            seen = Some(args);
            Outcome {
                code: 7,
                stdout: cps("ran"),
                stderr: Vec::new(),
            }
        });
        (outcome, seen)
    }

    #[test]
    fn bare_log_prints_the_log_help_to_stdout_and_exits_2_without_the_command() {
        let (outcome, seen) = ran(&["log"]);
        let outcome = outcome.unwrap();
        assert_eq!(outcome.code, 2);
        assert!(outcome.stderr.is_empty());
        let root = build_product_parser("1", WIDTH).unwrap();
        let help = DefaultHelp
            .format_help(subparser(&root, &cps("log")).unwrap(), WIDTH)
            .unwrap();
        assert_eq!(outcome.stdout, help);
        assert!(crate::unicode::to_string(&help)
            .unwrap()
            .starts_with("usage: bettermemory log [-h]"));
        assert_eq!(seen, None);
    }

    #[test]
    fn verify_takes_json_and_the_anchors_in_the_callers_order() {
        let (outcome, seen) = ran(&["log", "verify"]);
        assert_eq!(outcome.unwrap().code, 7);
        assert_eq!(
            seen,
            Some(VerifyArgs {
                json_out: false,
                anchor_files: Vec::new()
            })
        );
        let (outcome, seen) = ran(&[
            "log",
            "verify",
            "--anchor",
            "b",
            "--json",
            "--anchor=a",
            "--a",
            "b",
            "--anchor",
            "",
        ]);
        assert_eq!(outcome.unwrap().stdout, cps("ran"));
        assert_eq!(
            seen,
            Some(VerifyArgs {
                json_out: true,
                anchor_files: vec![cps("b"), cps("a"), cps("b"), Vec::new()]
            })
        );
    }

    #[test]
    fn a_namespace_the_log_parser_did_not_make_is_not_ported() {
        let root = build_product_parser("1", WIDTH).unwrap();
        let sub = Formatted {
            parser: subparser(&root, &cps("log")).unwrap(),
            width: WIDTH,
        };
        let never = |_: VerifyArgs, _: &Formatted<'_>| -> Outcome { panic!("not verify") };
        for args in [
            vec![],
            vec![(cps("log_cmd"), PyValue::str("export"))],
            vec![(cps("log_cmd"), PyValue::Bool(true))],
            vec![(cps("log_cmd"), PyValue::str("verify"))],
            vec![
                (cps("log_cmd"), PyValue::str("verify")),
                (cps("json"), PyValue::None),
                (cps("anchor"), PyValue::None),
            ],
            vec![
                (cps("log_cmd"), PyValue::str("verify")),
                (cps("json"), PyValue::Bool(false)),
                (cps("anchor"), PyValue::List(vec![PyValue::Bool(true)])),
            ],
            vec![
                (cps("log_cmd"), PyValue::str("verify")),
                (cps("json"), PyValue::Bool(false)),
                (cps("anchor"), PyValue::str("a")),
            ],
        ] {
            assert_eq!(run(&args, &sub, &DefaultHelp, never), Err(NotPorted));
        }
    }
}
