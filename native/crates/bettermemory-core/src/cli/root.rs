//! The CLI root (`cli/__init__.py` 73-298): `_build_parser`,
//! `_add_hook_subparser` and `main`'s routing and dispatch. The command
//! modules are trait objects ([`Command`]); the hook client, the shim and
//! the version are arguments.

use super::{Exit, UsageError};
use crate::argparse::{
    Arg, ArgumentParser, HelpText, Kind, Namespace, NotPorted, Outcome, Parsed, SpecError,
};
use crate::entry::route_hook_word;
use crate::pyvalue::{repr_str, PyStr, PyValue};
use crate::unicode::cps;

/// The root parser's `description=` (95-99).
pub const DESCRIPTION: &str = "MCP memory for coding agents, checked against your files and \
git history on every read. Run with no arguments to start the MCP server over stdio.";

/// The `help=` of `bettermemory hook` (161-165).
pub const HOOK_HELP: &str = "Run a harness hook through the daemon: session-start, stop or \
prompt. Reads the hook's stdin JSON, prints the daemon's answer, always exits 0.";

mod registry;
pub use registry::{build_product_parser, COMMANDS};

/// The formatter's width when `COLUMNS` is unset and stdout is no
/// terminal: `shutil.get_terminal_size`'s fallback of 80, less 2.
pub const DEFAULT_WIDTH: i64 = 78;

/// One command module: its subparser builder and its handler.
pub trait Command {
    fn name(&self) -> &str;
    /// `add_subparser(sub)`: adds this command's parser to `root`'s
    /// subparsers.
    fn add_subparser(&self, root: &mut ArgumentParser, width: i64) -> Result<(), SpecError>;
    /// `run(args, root_parser=, sub_parser=)` with the parsers its arm
    /// passes; a return is the reference's return (exit 0).
    fn run(
        &self,
        args: &Namespace,
        root: Option<&Formatted<'_>>,
        sub: Option<&Formatted<'_>>,
    ) -> Exit;
}

/// The parsers `main`'s arm for `cmd` passes: (root, sub), or None where
/// no arm matches (`hook` and any other name).
pub fn arm_parsers(cmd: &str) -> Option<(bool, bool)> {
    Some(match cmd {
        "migrate" | "tombstones" => (true, true),
        "rename-scope" | "completion" => (true, false),
        "export" | "episodes" | "log" | "index" | "project" | "memory" | "membership" | "eval"
        | "review" | "capture" | "recall" | "backup" | "service" | "security" | "stats"
        | "config" | "key" => (false, true),
        "health" | "init" | "rollback" | "try" | "doctor" | "up" | "down" | "status" | "serve" => {
            (false, false)
        }
        _ => return None,
    })
}

/// `_add_hook_subparser(sub)` (152-177).
pub fn add_hook_subparser(root: &mut ArgumentParser) -> Result<(), SpecError> {
    let p = root.add_parser("hook", Some(HOOK_HELP), None)?;
    let choices = ["session-start", "stop", "prompt"]
        .map(String::from)
        .to_vec();
    p.add_argument(
        &["event"],
        Arg {
            choices: Some(choices),
            ..Arg::default()
        },
    )?;
    for flag in ["--quiet", "--dry-run"] {
        p.add_argument(
            &[flag],
            Arg {
                kind: Some(Kind::StoreTrue),
                ..Arg::default()
            },
        )?;
    }
    for opt in ["--transcript-path", "--session-id", "--prompt"] {
        p.add_argument(
            &[opt],
            Arg {
                default: Some(PyValue::None),
                ..Arg::default()
            },
        )?;
    }
    Ok(())
}

/// The first command registered under `name`.
fn registered<'a>(commands: &[&'a dyn Command], name: &str) -> Option<&'a dyn Command> {
    commands.iter().copied().find(|c| c.name() == name)
}

fn base_parser(version: &str, width: i64) -> Result<ArgumentParser, SpecError> {
    let mut parser = ArgumentParser::new("bettermemory", Some(DESCRIPTION), true);
    let version = Kind::Version(cps(&format!("bettermemory {version}")));
    parser.add_argument(
        &["--version"],
        Arg {
            kind: Some(version),
            ..Arg::default()
        },
    )?;
    parser.add_subparsers(Some("cmd"), width)?;
    Ok(parser)
}

/// `_build_parser()` (73-150) with `__version__` given. A name of
/// [`COMMANDS`] with no [`Command`] registered is skipped; `hook` is
/// always `_add_hook_subparser`'s.
pub fn build_parser(
    version: &str,
    commands: &[&dyn Command],
    width: i64,
) -> Result<ArgumentParser, SpecError> {
    let mut parser = base_parser(version, width)?;
    for name in COMMANDS {
        if name == "hook" {
            add_hook_subparser(&mut parser)?;
        } else if let Some(c) = registered(commands, name) {
            c.add_subparser(&mut parser, width)?;
        }
    }
    Ok(parser)
}

/// `subparsers[name]`: the parser `add_parser` made for `name`.
pub fn subparser<'a>(root: &'a ArgumentParser, name: &[u32]) -> Option<&'a ArgumentParser> {
    let sub = root.subparsers.as_ref()?;
    sub.parsers.iter().find(|(n, _)| n == name).map(|(_, p)| p)
}

/// How `main` ended.
#[derive(Debug, PartialEq)]
pub enum Ended {
    /// `route_hook_word` exited with `hook_main`'s code.
    Hook(i32),
    /// The parser exited: help, version, a parse error or the unknown
    /// subcommand error.
    Parser(Outcome),
    /// No subcommand: the shim ran.
    Shim(Exit),
    /// The arm of `cmd` ran its command.
    Ran { cmd: PyStr, exit: Exit },
}

/// What the reference raises before any command runs.
#[derive(Debug, PartialEq)]
pub enum Fault {
    /// `add_argument`, `add_subparsers` or `add_parser` raised.
    Spec(SpecError),
    NotPorted,
}

impl From<SpecError> for Fault {
    fn from(e: SpecError) -> Self {
        Fault::Spec(e)
    }
}

impl From<NotPorted> for Fault {
    fn from(_: NotPorted) -> Self {
        Fault::NotPorted
    }
}

/// `main()` (178-298) over `argv` (`sys.argv[1:]`).
pub fn main(
    argv: &[PyStr],
    version: &str,
    commands: &[&dyn Command],
    width: i64,
    help: &dyn HelpText,
    hook_main: impl FnOnce(&[PyStr]) -> i32,
    shim: impl FnOnce() -> Exit,
) -> Result<Ended, Fault> {
    if let Some(code) = route_hook_word(argv, hook_main) {
        return Ok(Ended::Hook(code));
    }
    let parser = build_parser(version, commands, width)?;
    let args = match parser.parse_args(argv, width, help)? {
        Parsed::Namespace(ns) => ns,
        Parsed::Exit(o) => return Ok(Ended::Parser(o)),
    };
    let cmd = match args.iter().find(|(k, _)| *k == cps("cmd")).map(|(_, v)| v) {
        Some(PyValue::None) => return Ok(Ended::Shim(shim())),
        Some(PyValue::Str(c)) => c.clone(),
        _ => return Err(Fault::NotPorted),
    };
    let arm = COMMANDS.iter().find(|n| cps(n) == cmd).and_then(|&n| {
        let (r, s) = arm_parsers(n)?;
        Some((registered(commands, n)?, r, s))
    });
    if let Some((command, r, s)) = arm {
        let sub = if s { subparser(&parser, &cmd) } else { None };
        let root = Formatted {
            parser: &parser,
            width,
        };
        let sub = sub.map(|parser| Formatted { parser, width });
        let exit = command.run(&args, r.then_some(&root), sub.as_ref());
        return Ok(Ended::Ran { cmd, exit });
    }
    let mut message = cps("unknown subcommand: ");
    message.extend(repr_str(&cmd));
    Ok(Ended::Parser(parser.error(&message, width)))
}

/// A parser as `main` hands it to a command: its formatter at the width
/// `main` was given, which is what the reference's `parser.error` reads
/// from `COLUMNS` when the command refuses.
#[derive(Clone, Copy)]
pub struct Formatted<'a> {
    pub parser: &'a ArgumentParser,
    pub width: i64,
}

impl UsageError for Formatted<'_> {
    fn prog(&self) -> &PyStr {
        &self.parser.prog
    }
    fn error(&self, message: &PyStr) -> Exit {
        let o = ArgumentParser::error(self.parser, message, self.width);
        Exit {
            code: o.code,
            stderr: o.stderr,
        }
    }
}

/// `_refuse`'s view of a parser, its formatter at [`DEFAULT_WIDTH`].
impl UsageError for ArgumentParser {
    fn prog(&self) -> &PyStr {
        &self.prog
    }
    fn error(&self, message: &PyStr) -> Exit {
        let o = ArgumentParser::error(self, message, DEFAULT_WIDTH);
        Exit {
            code: o.code,
            stderr: o.stderr,
        }
    }
}
