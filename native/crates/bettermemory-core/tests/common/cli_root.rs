//! The stand-in commands of `gen_cli_root` and `main`'s answer spelled as
//! the generator spells a row.

use super::argparse::{outcome, spell};
use bettermemory_core::argparse::{Arg, ArgumentParser, HelpText, Namespace, Parsed, SpecError};
use bettermemory_core::cli::root::{main, Command, Ended, Formatted, COMMANDS};
use bettermemory_core::cli::Exit;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use serde_json::{json, Value};
use std::cell::RefCell;

/// The help text the case printed, as argparse-2's formatter will give it.
pub struct Printed(pub PyStr);
impl HelpText for Printed {
    fn format_help(&self, _: &ArgumentParser, _: i64) -> Option<PyStr> {
        Some(self.0.clone())
    }
}

/// The generator's stand-in: `add_parser(name, help=, description=)`
/// with `--opt`, and a handler that records its arm and parsers.
pub struct StandIn {
    pub name: String,
    pub seen: RefCell<Option<Value>>,
}

impl StandIn {
    pub fn new(name: &str) -> Self {
        StandIn {
            name: name.into(),
            seen: RefCell::new(None),
        }
    }
}

impl Command for StandIn {
    fn name(&self) -> &str {
        &self.name
    }
    fn add_subparser(&self, root: &mut ArgumentParser, _width: i64) -> Result<(), SpecError> {
        let help = format!("{} help", self.name);
        let p = root.add_parser(&self.name, Some(&help), Some(&help))?;
        let opt = Arg {
            default: Some(PyValue::None),
            ..Arg::default()
        };
        p.add_argument(&["--opt"], opt)
    }
    fn run(
        &self,
        args: &Namespace,
        root: Option<&Formatted<'_>>,
        sub: Option<&Formatted<'_>>,
    ) -> Exit {
        let prog = |p: Option<&Formatted<'_>>| p.map_or(Value::Null, |p| spell(&p.parser.prog));
        let ns = outcome(&Parsed::Namespace(args.clone()))["ns"].clone();
        let seen = json!({"root": prog(root), "sub": prog(sub), "ns": ns});
        *self.seen.borrow_mut() = Some(seen);
        Exit {
            code: 0,
            stderr: Vec::new(),
        }
    }
}

/// `main` over `argv` with a stand-in for every name of `COMMANDS` but
/// `hook` (or those `only` names), spelled as a row without its `a`.
pub fn run_main(
    argv: &[PyStr],
    version: &str,
    width: i64,
    help: &dyn HelpText,
    only: Option<&[&str]>,
) -> Value {
    let stand_ins: Vec<StandIn> = COMMANDS
        .iter()
        .filter(|n| **n != "hook" && only.is_none_or(|o| o.contains(n)))
        .map(|n| StandIn::new(n))
        .collect();
    let commands: Vec<&dyn Command> = stand_ins.iter().map(|c| c as &dyn Command).collect();
    let called = RefCell::new(None);
    let hook_main = |a: &[PyStr]| {
        *called.borrow_mut() = Some(a.to_vec());
        7
    };
    let shim = || Exit {
        code: 0,
        stderr: Vec::new(),
    };
    let ended = match main(argv, version, &commands, width, help, hook_main, shim) {
        Ok(ended) => ended,
        Err(fault) => return json!({ "fault": format!("{fault:?}") }),
    };
    match ended {
        Ended::Hook(code) => {
            let a: Vec<Value> = called
                .borrow()
                .as_ref()
                .unwrap()
                .iter()
                .map(|t| spell(t))
                .collect();
            json!({"hook": code, "called": a})
        }
        Ended::Shim(_) => json!({"shim": true}),
        Ended::Parser(o) => outcome(&Parsed::Exit(o)),
        Ended::Ran { cmd, exit } => {
            assert_eq!(exit.code, 0);
            let name = String::from_iter(cmd.iter().map(|&c| char::from_u32(c).unwrap()));
            let stand_in = stand_ins.iter().find(|c| c.name == name).unwrap();
            let mut row = stand_in.seen.borrow().clone().unwrap();
            row["arm"] = json!(name);
            row
        }
    }
}
