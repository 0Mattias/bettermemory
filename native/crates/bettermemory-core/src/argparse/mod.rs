//! The flat-parser subset of CPython 3.13.13's `argparse` that the two hook
//! mains (`hook.main`, `hook.prompt_main`) parse through: an
//! `ArgumentParser` with optionals (`store`, `store_true`, `help`,
//! `version`) and positionals of one argument, `parse_args` without a
//! process exit, and the usage line its errors print.
//!
//! Single-value `append` also serves repeated CLI options such as `--anchor`.
//!
//! Effects are arguments: the help formatter's width is passed in (the
//! reference reads it from `shutil.get_terminal_size`), and the help text
//! and the filling of a long version text come through [`HelpText`], implemented by
//! [`help::DefaultHelp`] for the standard listing and wrapped version. Subparsers (`add_subparsers`,
//! `add_parser` and the PARSER `nargs`) are argparse-1b's, in
//! [`subparsers`].

pub mod help;
pub mod parse;
pub mod subparsers;
pub mod usage;

use crate::pyvalue::{PyStr, PyValue};
use crate::unicode;

/// What an action does when it is taken (the reference's action classes).
#[derive(Clone, Debug, PartialEq)]
pub enum Kind {
    /// `_StoreAction`, `nargs` None.
    Store,
    /// `_AppendAction` with one string value.
    Append,
    /// `_StoreTrueAction`.
    StoreTrue,
    /// `_HelpAction`: prints `format_help` and exits 0.
    Help,
    /// `_VersionAction` with its `version` text.
    Version(PyStr),
    /// `_SubParsersAction` (`nargs` PARSER): argparse-1b.
    Parsers,
}

impl Kind {
    /// Whether the action consumes a single value and displays its metavar.
    pub(crate) fn takes_value(&self) -> bool {
        matches!(self, Self::Store | Self::Append)
    }
}

/// One action as `add_argument` made it.
#[derive(Clone, Debug)]
pub struct Action {
    pub option_strings: Vec<PyStr>,
    /// None is `SUPPRESS`.
    pub dest: Option<PyStr>,
    pub kind: Kind,
    /// None is `SUPPRESS`.
    pub default: Option<PyValue>,
    pub choices: Option<Vec<PyStr>>,
    pub required: bool,
    pub metavar: Option<PyStr>,
    pub help: Option<PyStr>,
}

/// The keyword arguments of `add_argument` this unit takes.
#[derive(Clone, Debug, Default)]
pub struct Arg {
    /// None is `action="store"`.
    pub kind: Option<Kind>,
    pub dest: Option<String>,
    pub default: Option<PyValue>,
    pub choices: Option<Vec<String>>,
    pub required: Option<bool>,
    pub metavar: Option<String>,
    pub help: Option<String>,
}

/// The `ValueError` and `ArgumentError` `add_argument` raises.
#[derive(Clone, Debug, PartialEq)]
pub enum SpecError {
    /// "invalid option string ...: must start with a character '-'".
    Prefix,
    /// "dest= is required for options like ...".
    Dest,
    /// "conflicting option string".
    Conflict,
    /// The `IndexError` of `args[0][0]` on an empty first name.
    EmptyName,
    /// "dest supplied twice for positional argument".
    DestTwice,
    /// The `TypeError` "'required' is an invalid argument for positionals".
    PositionalRequired,
    /// The `TypeError` of passing `required` to an action class that does
    /// not take it (`help` and `version` as positionals).
    UnexpectedRequired,
    /// "cannot have multiple subparser arguments".
    MultipleSubparsers,
    /// "conflicting subparser: NAME".
    ConflictingSubparser,
    /// The `AttributeError` of `add_parser` on a parser without
    /// subparsers (the caller holds no `_SubParsersAction`).
    NoSubparsers,
}

/// An answer the reference gives that this unit does not reproduce yet.
#[derive(Clone, Debug, PartialEq)]
pub struct NotPorted;

/// What `parse_args` printed and the status it exited with.
#[derive(Clone, Debug, PartialEq)]
pub struct Outcome {
    pub code: i32,
    pub stdout: PyStr,
    pub stderr: PyStr,
}

/// The namespace, attribute by attribute in the order they were set.
pub type Namespace = Vec<(PyStr, PyValue)>;

#[derive(Clone, Debug, PartialEq)]
pub enum Parsed {
    Namespace(Namespace),
    Exit(Outcome),
}

/// Optional formatting operations supplied to the parser.
pub trait HelpText {
    /// `parser.format_help()` at `width`.
    fn format_help(&self, _parser: &ArgumentParser, _width: i64) -> Option<PyStr> {
        None
    }
    /// `textwrap.fill(text, width)` of a text longer than `width`.
    fn fill(&self, _text: &[u32], _width: i64) -> Option<PyStr> {
        None
    }
}

/// The hook that declines every text.
pub struct Unfilled;
impl HelpText for Unfilled {}

pub struct ArgumentParser {
    pub prog: PyStr,
    pub description: Option<PyStr>,
    pub actions: Vec<Action>,
    /// `_has_negative_number_optionals`.
    pub negative_optionals: bool,
    /// The `_SubParsersAction` `add_subparsers` made, if any.
    pub subparsers: Option<subparsers::Subparsers>,
}

fn cps(s: &str) -> PyStr {
    unicode::cps(s)
}

/// `_negative_number_matcher.match`: `^-\d+$|^-\d*\.\d+$`, where `$` also
/// matches before a final newline and `\d` is `str.isdecimal`.
pub fn looks_negative(s: &[u32]) -> bool {
    let whole = |b: &[u32]| {
        let digits = |t: &[u32]| t.iter().all(|&c| unicode::is_decimal(c));
        let n = b.len();
        (n > 0 && digits(b))
            || b.iter()
                .position(|&c| c == '.' as u32)
                .is_some_and(|d| d + 1 < n && digits(&b[..d]) && digits(&b[d + 1..]))
    };
    match s.split_first() {
        Some((&0x2D, body)) => {
            whole(body) || (body.last() == Some(&0x0A) && whole(&body[..body.len() - 1]))
        }
        _ => false,
    }
}

impl ArgumentParser {
    /// `ArgumentParser(prog=, description=, add_help=)`.
    pub fn new(prog: &str, description: Option<&str>, add_help: bool) -> Self {
        let mut p = ArgumentParser {
            prog: cps(prog),
            description: description.map(cps),
            actions: Vec::new(),
            negative_optionals: false,
            subparsers: None,
        };
        if add_help {
            let help = Arg {
                kind: Some(Kind::Help),
                help: Some("show this help message and exit".into()),
                ..Arg::default()
            };
            p.add_argument(&["-h", "--help"], help)
                .expect("the help option is valid");
        }
        p
    }

    /// `add_argument(*names, **arg)`.
    pub fn add_argument(&mut self, names: &[&str], arg: Arg) -> Result<(), SpecError> {
        let kind = arg.kind.unwrap_or(Kind::Store);
        let names: Vec<PyStr> = names.iter().map(|n| cps(n)).collect();
        if names.iter().any(|n| n.is_empty()) {
            return Err(SpecError::EmptyName);
        }
        let positional = names.is_empty() || (names.len() == 1 && names[0].first() != Some(&0x2D));
        let (option_strings, dest, required) = if positional {
            let dest = match (names.first(), &arg.dest) {
                (Some(_), Some(_)) => return Err(SpecError::DestTwice),
                (Some(n), None) => n.clone(),
                (None, Some(d)) => cps(d),
                (None, None) => return Err(SpecError::Dest),
            };
            if arg.required.is_some() {
                return Err(SpecError::PositionalRequired);
            }
            // No nargs is given, so `_get_positional_kwargs` passes
            // required=True to every action class.
            if matches!(kind, Kind::Help | Kind::Version(_)) {
                return Err(SpecError::UnexpectedRequired);
            }
            (Vec::new(), Some(dest), true)
        } else {
            if names.iter().any(|n| n.first() != Some(&0x2D)) {
                return Err(SpecError::Prefix);
            }
            let dest = match &arg.dest {
                Some(d) => cps(d),
                None => {
                    let long = names.iter().find(|n| n.len() > 1 && n[1] == 0x2D);
                    let from = long.unwrap_or(&names[0]);
                    let d: PyStr = from.iter().copied().skip_while(|&c| c == 0x2D).collect();
                    if d.is_empty() {
                        return Err(SpecError::Dest);
                    }
                    d.iter()
                        .map(|&c| if c == 0x2D { 0x5F } else { c })
                        .collect()
                }
            };
            (names, Some(dest), arg.required.unwrap_or(false))
        };
        if option_strings
            .iter()
            .any(|o| self.actions.iter().any(|a| a.option_strings.contains(o)))
        {
            return Err(SpecError::Conflict);
        }
        if option_strings.iter().any(|o| looks_negative(o)) {
            self.negative_optionals = true;
        }
        let suppress = matches!(kind, Kind::Help | Kind::Version(_));
        let default = match (&kind, arg.default) {
            _ if suppress => None,
            (_, Some(v)) => Some(v),
            (Kind::StoreTrue, None) => Some(PyValue::Bool(false)),
            (_, None) => Some(PyValue::None),
        };
        let help = match (&kind, arg.help) {
            (Kind::Version(_), None) => Some(cps("show program's version number and exit")),
            (_, h) => h.map(|h| cps(&h)),
        };
        self.actions.push(Action {
            option_strings,
            dest: if suppress { None } else { dest },
            kind,
            default,
            choices: arg.choices.map(|c| c.iter().map(|s| cps(s)).collect()),
            required,
            metavar: arg.metavar.map(|m| cps(&m)),
            help,
        });
        Ok(())
    }

    /// The action an exact option string names (`_option_string_actions`).
    pub(crate) fn lookup(&self, option: &[u32]) -> Option<usize> {
        self.actions
            .iter()
            .position(|a| a.option_strings.iter().any(|o| o == option))
    }

    /// `parse_args(argv)` with the formatter's width; `SystemExit` becomes
    /// an [`Outcome`].
    pub fn parse_args(
        &self,
        argv: &[PyStr],
        width: i64,
        hook: &dyn HelpText,
    ) -> Result<Parsed, NotPorted> {
        let (ns, extras) = match subparsers::parse_known_args(self, argv, width, hook) {
            Ok(known) => known,
            Err(parse::Stop::Exit(o)) => return Ok(Parsed::Exit(o)),
            Err(_) => return Err(NotPorted),
        };
        if extras.is_empty() {
            return Ok(Parsed::Namespace(ns));
        }
        let mut message = cps("unrecognized arguments: ");
        message.extend(extras.join(&0x20));
        Ok(Parsed::Exit(self.error(&message, width)))
    }

    /// `error(message)`: the usage, then `prog: error: message`, status 2.
    pub fn error(&self, message: &[u32], width: i64) -> Outcome {
        let mut stderr = usage::format_usage(self, width);
        stderr.extend(&self.prog);
        stderr.extend(cps(": error: "));
        stderr.extend(message);
        stderr.push(0x0A);
        Outcome {
            code: 2,
            stdout: Vec::new(),
            stderr,
        }
    }
}
