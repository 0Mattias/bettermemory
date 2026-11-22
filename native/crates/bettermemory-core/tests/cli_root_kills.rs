//! Cases a plausible wrong `cli::root` fails: the arm table, the order of
//! the subparsers, a name with no command, a command named `hook`, the
//! unknown subcommand error and `UsageError`.

mod common;

use bettermemory_core::argparse::{ArgumentParser, Namespace, SpecError, Unfilled};
use bettermemory_core::cli::root::{
    arm_parsers, build_parser, main, subparser, Command, Ended, Formatted, COMMANDS, DEFAULT_WIDTH,
};
use bettermemory_core::cli::{Exit, UsageError};
use bettermemory_core::unicode::cps;
use common::cli_root::{run_main, StandIn};
use serde_json::json;
use std::cell::Cell;

fn has(p: &ArgumentParser, opt: &str) -> bool {
    p.actions
        .iter()
        .any(|a| a.option_strings.contains(&cps(opt)))
}

fn tail(err: &serde_json::Value) -> String {
    let s = err.as_str().unwrap();
    s.lines().last().unwrap().to_owned()
}

#[test]
fn every_arm_passes_the_reference_parsers() {
    let both = ["migrate", "tombstones"];
    let root = ["rename-scope", "completion"];
    let none = [
        "health", "init", "rollback", "try", "doctor", "up", "down", "status", "serve",
    ];
    for name in COMMANDS {
        let want = if name == "hook" {
            None
        } else if both.contains(&name) {
            Some((true, true))
        } else if root.contains(&name) {
            Some((true, false))
        } else if none.contains(&name) {
            Some((false, false))
        } else {
            Some((false, true))
        };
        assert_eq!(arm_parsers(name), want, "{name}");
    }
    assert_eq!(arm_parsers("Health"), None);
    assert_eq!(arm_parsers(""), None);
}

#[test]
fn subparsers_follow_the_reference_order() {
    let stand_ins: Vec<StandIn> = COMMANDS.iter().rev().map(|n| StandIn::new(n)).collect();
    let commands: Vec<&dyn Command> = stand_ins.iter().map(|c| c as &dyn Command).collect();
    let p = build_parser("1", &commands, 78).unwrap();
    let names: Vec<_> = p
        .subparsers
        .as_ref()
        .unwrap()
        .parsers
        .iter()
        .map(|(n, _)| n.clone())
        .collect();
    let want: Vec<_> = COMMANDS.iter().map(|n| cps(n)).collect();
    assert_eq!(names, want);
    // The hook parser is the root's own, not the registered stand-in's.
    let hook = subparser(&p, &cps("hook")).unwrap();
    assert!(has(hook, "--quiet"));
    assert!(!has(hook, "--opt"));
}

#[test]
fn an_unregistered_name_is_no_choice() {
    let only: &[&str] = &["health"];
    let got = run_main(&[cps("init")], "1", 78, &Unfilled, Some(only));
    assert_eq!(got["code"], 2);
    assert!(
        tail(&got["err"]).contains("invalid choice: 'init'"),
        "{got}"
    );
    let got = run_main(&[cps("health")], "1", 78, &Unfilled, Some(only));
    assert_eq!(got["arm"], "health");
}

#[test]
fn the_hook_parser_reached_through_a_double_dash_is_unknown() {
    let argv = [cps("--"), cps("hook"), cps("stop")];
    let got = run_main(&argv, "1", 78, &Unfilled, None);
    assert_eq!(got["code"], 2);
    assert_eq!(
        tail(&got["err"]),
        "bettermemory: error: unknown subcommand: 'hook'"
    );
}

#[test]
fn a_hook_word_goes_first_whatever_follows() {
    for w in ["hook", "session-start", "audit-turn", "prompt-recall"] {
        let got = run_main(&[cps(w), cps("--version")], "1", 78, &Unfilled, None);
        assert_eq!(got, json!({"hook": 7, "called": [w, "--version"]}));
    }
    let got = run_main(&[cps("--version"), cps("hook")], "2", 78, &Unfilled, None);
    assert_eq!(got["out"], "bettermemory 2\n");
}

#[test]
fn no_subcommand_runs_the_shim_once_and_nothing_else() {
    struct Never;
    impl Command for Never {
        fn name(&self) -> &str {
            "health"
        }
        fn add_subparser(&self, root: &mut ArgumentParser, _: i64) -> Result<(), SpecError> {
            root.add_parser("health", None, None).map(|_| ())
        }
        fn run(&self, _: &Namespace, _: Option<&Formatted<'_>>, _: Option<&Formatted<'_>>) -> Exit {
            panic!("ran");
        }
    }
    let ran = Cell::new(0);
    let shim = || {
        ran.set(ran.get() + 1);
        Exit {
            code: 3,
            stderr: cps("s"),
        }
    };
    let got = main(&[], "1", &[&Never], 78, &Unfilled, |_| panic!("hook"), shim).unwrap();
    assert_eq!(
        got,
        Ended::Shim(Exit {
            code: 3,
            stderr: cps("s")
        })
    );
    assert_eq!(ran.get(), 1);
}

#[test]
fn the_first_registered_command_wins() {
    let a = StandIn::new("health");
    let b = StandIn::new("health");
    let p = build_parser("1", &[&a, &b], 78);
    assert!(p.is_ok());
    let got = main(
        &[cps("health")],
        "1",
        &[&a, &b],
        78,
        &Unfilled,
        |_| 0,
        || unreachable!(),
    );
    assert!(matches!(got, Ok(Ended::Ran { .. })));
    assert!(a.seen.borrow().is_some());
    assert!(b.seen.borrow().is_none());
}

#[test]
fn usage_error_is_the_parser_at_78_columns() {
    let p = ArgumentParser::new("prog", None, true);
    assert_eq!(UsageError::prog(&p), &cps("prog"));
    let e = UsageError::error(&p, &cps("bad"));
    assert_eq!(e.code, 2);
    assert_eq!(e.stderr, cps("usage: prog [-h]\nprog: error: bad\n"));
}

/// The usage line `argparse` writes for `prog` with `-h` alone, `"usage:
/// " + prog + " [-h]"`, `prefix + prog + 5` columns: one line when that
/// fits the width, else the prog and `[-h]` under it (`parser.error` of
/// the reference with `COLUMNS` unset, read from CPython 3.13).
fn refused(prog: &str, wraps: bool) -> Vec<u32> {
    let usage = if wraps {
        format!("usage: {prog}\n       [-h]\n")
    } else {
        format!("usage: {prog} [-h]\n")
    };
    cps(&format!("{usage}{prog}: error: bad\n"))
}

#[test]
fn usage_error_wraps_at_78_columns_not_77_or_80() {
    assert_eq!(DEFAULT_WIDTH, 78);
    // 78 columns fit, 79 do not.
    for (n, wraps) in [(66, false), (67, true)] {
        let prog = "p".repeat(n);
        let p = ArgumentParser::new(&prog, None, true);
        let e = UsageError::error(&p, &cps("bad"));
        assert_eq!(e.code, 2);
        assert_eq!(e.stderr, refused(&prog, wraps), "{n}");
    }
}

#[test]
fn a_command_refuses_at_the_width_main_was_given() {
    struct Widths;
    impl Command for Widths {
        fn name(&self) -> &str {
            "migrate"
        }
        fn add_subparser(&self, root: &mut ArgumentParser, _: i64) -> Result<(), SpecError> {
            root.add_parser("migrate", None, None).map(|_| ())
        }
        fn run(
            &self,
            _: &Namespace,
            root: Option<&Formatted<'_>>,
            sub: Option<&Formatted<'_>>,
        ) -> Exit {
            let (root, sub) = (root.unwrap(), sub.unwrap());
            assert_eq!(root.width, sub.width);
            Exit {
                code: i32::try_from(root.width).unwrap(),
                stderr: Vec::new(),
            }
        }
    }
    for width in [78, 198] {
        let got = main(
            &[cps("migrate")],
            "1",
            &[&Widths],
            width,
            &Unfilled,
            |_| 0,
            || unreachable!(),
        );
        match got {
            Ok(Ended::Ran { exit, .. }) => assert_eq!(i64::from(exit.code), width),
            other => panic!("{other:?}"),
        }
    }
    // A refusal through a parser so handed wraps at that width.
    let prog = "p".repeat(67);
    let long = ArgumentParser::new(&prog, None, true);
    for (width, wraps) in [(78, true), (79, false), (198, false)] {
        let f = Formatted {
            parser: &long,
            width,
        };
        assert_eq!(UsageError::prog(&f), &cps(&prog));
        let e = UsageError::error(&f, &cps("bad"));
        assert_eq!((e.code, e.stderr), (2, refused(&prog, wraps)), "{width}");
    }
}
