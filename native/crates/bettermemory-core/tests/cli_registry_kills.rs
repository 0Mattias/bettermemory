//! Registration, dispatch boundaries and the owner of each parser exit.

use bettermemory_core::argparse::{ArgumentParser, HelpText, NotPorted, Parsed, SpecError};
use bettermemory_core::cli::root::{build_product_parser, subparser, COMMANDS};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::unicode::cps;

struct Page;

impl HelpText for Page {
    fn format_help(&self, parser: &ArgumentParser, _: i64) -> Option<PyStr> {
        Some(parser.prog.clone())
    }
}

fn parse(p: &ArgumentParser, argv: &[&str], width: i64) -> Result<Parsed, NotPorted> {
    p.parse_args(
        &argv.iter().map(|v| cps(v)).collect::<Vec<_>>(),
        width,
        &Page,
    )
}

#[test]
fn selection_stops_at_every_deferred_command() {
    let parser = build_product_parser("custom", 78).unwrap();
    let log = subparser(&parser, &cps("log")).unwrap();
    for name in COMMANDS
        .iter()
        .copied()
        .filter(|n| !matches!(*n, "log" | "hook"))
    {
        assert!(subparser(&parser, &cps(name)).is_none());
        for tail in [
            vec![],
            vec!["--help"],
            vec!["--version"],
            vec!["--", "--invalid"],
        ] {
            let mut argv = vec![name];
            argv.extend(tail);
            assert_eq!(parse(&parser, &argv, 78), Err(NotPorted), "{argv:?}");
        }
    }
    for name in ["export", "anchor", "accept-history"] {
        assert!(subparser(log, &cps(name)).is_none());
        for tail in [
            vec![],
            vec!["--help"],
            vec!["--unknown"],
            vec!["--", "--help"],
        ] {
            let mut argv = vec!["log", name];
            argv.extend(tail);
            assert_eq!(parse(&parser, &argv, 78), Err(NotPorted));
        }
    }
}

#[test]
fn first_exit_and_parser_ownership_follow_selection() {
    for width in [42, 78, 118] {
        let parser = build_product_parser("custom", width).unwrap();
        for (argv, page) in [
            (vec!["--help", "health"], "bettermemory"),
            (vec!["--version", "health"], "bettermemory custom\n"),
            (vec!["log", "--help", "export"], "bettermemory log"),
            (
                vec!["log", "verify", "-h", "--anchor"],
                "bettermemory log verify",
            ),
            (vec!["hook", "--help", "stray"], "bettermemory hook"),
        ] {
            let Ok(Parsed::Exit(out)) = parse(&parser, &argv, width) else {
                panic!("{argv:?}")
            };
            assert_eq!((out.code, out.stdout, out.stderr), (0, cps(page), vec![]));
        }
        // CPython 3.11 rejects a leading separator here; the pinned 3.13 accepts it.
        for (argv, plain) in [
            (vec!["--", "log", "verify"], vec!["log", "verify"]),
            (vec!["log", "--", "verify"], vec!["log", "verify"]),
            (vec!["--", "hook", "stop"], vec!["hook", "stop"]),
        ] {
            let pinned = parse(&parser, &plain, width);
            assert!(matches!(pinned, Ok(Parsed::Namespace(_))));
            assert_eq!(parse(&parser, &argv, width), pinned);
        }
        for (argv, owner) in [
            (
                vec!["bad"],
                "bettermemory: error: argument cmd: invalid choice:",
            ),
            (
                vec!["log", "bad"],
                "bettermemory log: error: argument log_cmd: invalid choice:",
            ),
            (
                vec!["log", "verify", "--anchor"],
                "bettermemory log verify: error: argument --anchor: expected one argument",
            ),
            (
                vec!["log", "verify", "--unknown"],
                "bettermemory: error: unrecognized arguments: --unknown",
            ),
        ] {
            let Ok(Parsed::Exit(out)) = parse(&parser, &argv, width) else {
                panic!("{argv:?}")
            };
            assert_eq!(out.code, 2);
            assert!(out.stdout.is_empty());
            let needle = cps(owner);
            assert!(
                out.stderr.windows(needle.len()).any(|v| v == needle),
                "{argv:?}"
            );
        }
    }
}

fn register(p: &mut ArgumentParser, real: bool, help: Option<&str>) -> Result<(), SpecError> {
    if real {
        p.add_parser("item", help, Some("original")).map(|_| ())
    } else {
        p.add_deferred_parser("item", help)
    }
}

#[test]
fn conflicting_registration_keeps_child_identity_choices_and_help() {
    for real in [false, true] {
        for new_real in [false, true] {
            for help in [None, Some(""), Some("first")] {
                let mut p = ArgumentParser::new("root", None, false);
                assert_eq!(register(&mut p, real, help), Err(SpecError::NoSubparsers));
                p.add_subparsers(Some("command"), 78).unwrap();
                register(&mut p, real, help).unwrap();
                let id = subparser(&p, &cps("item")).map(std::ptr::from_ref);
                assert_eq!(
                    register(&mut p, new_real, Some("second")),
                    Err(SpecError::ConflictingSubparser)
                );
                assert_eq!(subparser(&p, &cps("item")).map(std::ptr::from_ref), id);
                let sub = p.subparsers.as_ref().unwrap();
                assert_eq!(p.actions[sub.action].choices, Some(vec![cps("item")]));
                assert_eq!(
                    sub.choices_help,
                    help.map(|v| (cps("item"), cps(v)))
                        .into_iter()
                        .collect::<Vec<_>>()
                );
                let child = p.add_parser("other", None, None).unwrap();
                child.add_subparsers(Some("child"), 78).unwrap();
                register(child, new_real, help).unwrap();
            }
        }
    }
}

#[test]
fn duplicate_checks_consult_both_public_choice_views() {
    let mut p = ArgumentParser::new("root", None, false);
    p.add_subparsers(None, 78).unwrap();
    p.add_parser("actual", Some("original"), None).unwrap();
    let action = p.subparsers.as_ref().unwrap().action;
    p.actions[action].choices = Some(vec![cps("advertised")]);
    for name in ["advertised", "actual"] {
        assert_eq!(
            p.add_deferred_parser(name, Some("new")),
            Err(SpecError::ConflictingSubparser)
        );
        assert_eq!(
            p.add_parser(name, None, None).err(),
            Some(SpecError::ConflictingSubparser)
        );
    }
    assert_eq!(p.actions[action].choices, Some(vec![cps("advertised")]));
    assert_eq!(
        p.subparsers.as_ref().unwrap().choices_help,
        [(cps("actual"), cps("original"))]
    );
}
