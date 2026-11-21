//! `add_subparsers`, `_SubParsersAction.add_parser` and `__call__`, and
//! `parse_known_args` (`_parse_known_args2`) as a subparser is run by its
//! parent (`argparse.py` 1185-1293, 1873-1903, 1945-1974 of 3.13.13).

use super::parse::{parse_known, Stop};
use super::{usage, Action, ArgumentParser, HelpText, Kind, Namespace, SpecError};
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode::{cps, is_space};

/// The `_SubParsersAction`: its place among the parser's actions, the
/// `prog` prefix of its parsers and the parsers by name, in the order they
/// were added.
pub struct Subparsers {
    pub action: usize,
    pub prog_prefix: PyStr,
    pub parsers: Vec<(PyStr, ArgumentParser)>,
    /// Each `_ChoicesPseudoAction`: a parser's name and its `help=`.
    pub choices_help: Vec<(PyStr, PyStr)>,
}

/// The attribute `_parse_known_args2` reads the subparsers' leftovers from.
const UNRECOGNIZED: &str = "_unrecognized_args";

/// The deepest chain of subparsers run. Each level costs the reference about
/// six Python frames, so 3.13.13 raises `RecursionError` from 166 levels on
/// and the bound follows the release; from this depth on the port declines,
/// well inside every release's answer and the thread's stack.
pub const MAX_DEPTH: usize = 100;

thread_local! {
    static DEPTH: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

/// One level of the chain, given back when dropped.
struct Level;

impl Level {
    fn enter() -> Result<Self, Stop> {
        DEPTH.with(|d| {
            if d.get() + 1 >= MAX_DEPTH {
                return Err(Stop::NotPorted);
            }
            d.set(d.get() + 1);
            Ok(Level)
        })
    }
}

impl Drop for Level {
    fn drop(&mut self) {
        DEPTH.with(|d| d.set(d.get() - 1));
    }
}

impl ArgumentParser {
    /// `add_subparsers(dest=dest)` with the formatter at `width`: `prog`
    /// is this parser's usage of its positionals, without a prefix,
    /// stripped.
    pub fn add_subparsers(&mut self, dest: Option<&str>, width: i64) -> Result<(), SpecError> {
        if self.subparsers.is_some() {
            return Err(SpecError::MultipleSubparsers);
        }
        let usage = usage::format_usage_with(self, width, &[], true);
        let start = usage.iter().position(|&c| !is_space(c)).unwrap_or(0);
        let end = usage
            .iter()
            .rposition(|&c| !is_space(c))
            .map_or(start, |e| e + 1);
        self.actions.push(Action {
            option_strings: Vec::new(),
            dest: dest.map(cps),
            kind: Kind::Parsers,
            default: Some(PyValue::None),
            choices: Some(Vec::new()),
            required: false,
            metavar: None,
            help: None,
        });
        self.subparsers = Some(Subparsers {
            action: self.actions.len() - 1,
            prog_prefix: usage[start..end].to_vec(),
            parsers: Vec::new(),
            choices_help: Vec::new(),
        });
        Ok(())
    }

    /// `add_parser(name, help=, description=)` on the parser's subparsers
    /// action: a parser whose `prog` is the prefix, a space and the name,
    /// with `add_help`.
    pub fn add_parser(
        &mut self,
        name: &str,
        help: Option<&str>,
        description: Option<&str>,
    ) -> Result<&mut ArgumentParser, SpecError> {
        let sub = self.subparsers.as_mut().ok_or(SpecError::NoSubparsers)?;
        let name = cps(name);
        if sub.parsers.iter().any(|(n, _)| *n == name) {
            return Err(SpecError::ConflictingSubparser);
        }
        let mut prog = sub.prog_prefix.clone();
        prog.push(0x20);
        prog.extend(&name);
        if let Some(h) = help {
            sub.choices_help.push((name.clone(), cps(h)));
        }
        let mut child = ArgumentParser::new("", description, true);
        child.prog = prog;
        if let Some(c) = self.actions[sub.action].choices.as_mut() {
            c.push(name.clone());
        }
        sub.parsers.push((name, child));
        Ok(&mut sub.parsers.last_mut().expect("just pushed").1)
    }
}

/// `parse_known_args(argv)` with a fresh namespace: the namespace and the
/// leftovers, the subparsers' after this parser's, or the exit its
/// `error` makes of an `ArgumentError`.
pub(crate) fn parse_known_args(
    p: &ArgumentParser,
    argv: &[PyStr],
    width: i64,
    hook: &dyn HelpText,
) -> Result<(Namespace, Vec<PyStr>), Stop> {
    let mut ns: Namespace = Vec::new();
    for a in &p.actions {
        if let (Some(d), Some(v)) = (&a.dest, &a.default) {
            if !ns.iter().any(|(k, _)| k == d) {
                ns.push((d.clone(), v.clone()));
            }
        }
    }
    let mut unrecognized = Vec::new();
    let mut extras = match parse_known(p, argv, &mut ns, &mut unrecognized, width, hook) {
        Ok(extras) => extras,
        Err(Stop::Error(m)) => return Err(Stop::Exit(p.error(&m, width))),
        Err(stop) => return Err(stop),
    };
    // A dest named `_unrecognized_args` meets the reference's own
    // attribute; that reading is not reproduced.
    if ns.iter().any(|(k, _)| *k == cps(UNRECOGNIZED)) {
        return Err(Stop::NotPorted);
    }
    extras.extend(unrecognized);
    Ok((ns, extras))
}

/// `_SubParsersAction.__call__` after `_get_values` checked the name:
/// the dest set to the name, then the child's namespace set attribute by
/// attribute in its order, its leftovers kept for the parent.
pub(crate) fn call(
    p: &ArgumentParser,
    values: &[PyStr],
    ns: &mut Namespace,
    unrecognized: &mut Vec<PyStr>,
    width: i64,
    hook: &dyn HelpText,
) -> Result<(), Stop> {
    let sub = p.subparsers.as_ref().ok_or(Stop::NotPorted)?;
    let _level = Level::enter()?;
    let name = &values[0];
    if let Some(d) = &p.actions[sub.action].dest {
        set(ns, d, PyValue::Str(name.clone()));
    }
    let child = &sub
        .parsers
        .iter()
        .find(|(n, _)| n == name)
        .ok_or(Stop::NotPorted)?
        .1;
    let (child_ns, rest) = parse_known_args(child, &values[1..], width, hook)?;
    for (k, v) in child_ns {
        set(ns, &k, v);
    }
    unrecognized.extend(rest);
    Ok(())
}

/// `setattr`: a new attribute goes last, an existing one keeps its place.
pub(crate) fn set(ns: &mut Namespace, dest: &[u32], value: PyValue) {
    match ns.iter_mut().find(|(k, _)| k == dest) {
        Some(slot) => slot.1 = value,
        None => ns.push((dest.to_vec(), value)),
    }
}
