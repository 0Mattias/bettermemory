//! `_parse_known_args` with `_parse_optional`, `_get_option_tuples`,
//! `consume_optional`, `consume_positionals`, `_match_argument` and
//! `_match_arguments_partial` (`argparse.py` 1976-2445 of 3.13.13).

use std::collections::BTreeMap;

use super::{subparsers, usage, ArgumentParser, HelpText, Kind, Namespace, Outcome};
use crate::pyvalue::{repr_str, PyStr, PyValue};
use crate::unicode::cps;

const DASH: u32 = 0x2D;
const EQ: u32 = 0x3D;

/// How a parse stops before its end: an `ArgumentError`'s text, an exit,
/// or an answer this unit leaves to a later one.
pub(crate) enum Stop {
    Error(PyStr),
    Exit(Outcome),
    NotPorted,
}

/// `(action, option_string, sep, explicit_arg)`.
type Tuple = (Option<usize>, PyStr, Option<PyStr>, Option<PyStr>);

/// `nargs` as the matcher reads it.
enum Nargs {
    One,
    Zero,
    Parser,
}

fn nargs(kind: &Kind) -> Nargs {
    match kind {
        Kind::Store => Nargs::One,
        Kind::Parsers => Nargs::Parser,
        _ => Nargs::Zero,
    }
}

/// `_get_action_name`.
fn action_name(p: &ArgumentParser, i: usize) -> PyStr {
    let a = &p.actions[i];
    if !a.option_strings.is_empty() {
        return a.option_strings.join(&0x2F);
    }
    if let Some(m) = &a.metavar {
        return m.clone();
    }
    match (&a.dest, &a.choices) {
        (Some(d), _) => d.clone(),
        (None, Some(c)) if !c.is_empty() => {
            let mut out = vec![0x7B];
            out.extend(c.join(&0x2C));
            out.push(0x7D);
            out
        }
        _ => Vec::new(),
    }
}

/// `str(ArgumentError(action, message))`.
fn argument_error(p: &ArgumentParser, i: usize, message: &str, tail: &[u32]) -> Stop {
    let name = action_name(p, i);
    // An action without a name (`None`) makes the text the message alone.
    let mut m = if name.is_empty() && p.actions[i].dest.is_none() {
        Vec::new()
    } else {
        let mut m = cps("argument ");
        m.extend(name);
        m.extend(cps(": "));
        m
    };
    m.extend(cps(message));
    m.extend(tail);
    Stop::Error(m)
}

/// `str.partition('=')`.
fn partition(s: &[u32]) -> (PyStr, Option<PyStr>, Option<PyStr>) {
    match s.iter().position(|&c| c == EQ) {
        Some(i) => (s[..i].to_vec(), Some(vec![EQ]), Some(s[i + 1..].to_vec())),
        None => (s.to_vec(), None, None),
    }
}

impl ArgumentParser {
    /// `_parse_optional`: None for a positional.
    fn parse_optional(&self, arg: &[u32]) -> Option<Vec<Tuple>> {
        if arg.first() != Some(&DASH) {
            return None;
        }
        if let Some(i) = self.lookup(arg) {
            return Some(vec![(Some(i), arg.to_vec(), None, None)]);
        }
        if arg.len() == 1 {
            return None;
        }
        let (option, sep, explicit) = partition(arg);
        if sep.is_some() {
            if let Some(i) = self.lookup(&option) {
                return Some(vec![(Some(i), option, sep, explicit)]);
            }
        }
        let tuples = self.option_tuples(arg);
        if !tuples.is_empty() {
            return Some(tuples);
        }
        if super::looks_negative(arg) && !self.negative_optionals {
            return None;
        }
        if arg.contains(&0x20) {
            return None;
        }
        Some(vec![(None, arg.to_vec(), None, None)])
    }

    /// `_get_option_tuples` (with `allow_abbrev`), every option string in
    /// the order the actions were added.
    fn option_tuples(&self, arg: &[u32]) -> Vec<Tuple> {
        let (prefix, sep, explicit) = partition(arg);
        let mut out = Vec::new();
        for (i, a) in self.actions.iter().enumerate() {
            for o in &a.option_strings {
                if arg[1] != DASH && o[..] == arg[..2] {
                    out.push((
                        Some(i),
                        o.clone(),
                        Some(Vec::new()),
                        Some(arg[2..].to_vec()),
                    ));
                } else if o.starts_with(&prefix) {
                    out.push((Some(i), o.clone(), sep.clone(), explicit.clone()));
                }
            }
        }
        out
    }

    /// `_match_argument` of an optional over the pattern that follows it.
    fn match_argument(&self, i: usize, pattern: &[u8]) -> Result<usize, Stop> {
        match nargs(&self.actions[i].kind) {
            Nargs::Zero => Ok(0),
            Nargs::Parser => Err(Stop::NotPorted),
            Nargs::One if pattern.first() == Some(&b'A') => Ok(1),
            Nargs::One => Err(argument_error(self, i, "expected one argument", &[])),
        }
    }
}

/// One positional's `nargs` pattern matched at `at` and the rest after
/// it, in the order `re` tries them: `(-*A-*)` or `(-*A[-AO]*)`, each
/// trailing run greedy and given back one at a time.
fn match_from(
    kinds: &[Nargs],
    pattern: &[u8],
    at: usize,
    failed: &mut Vec<Vec<bool>>,
) -> Option<Vec<usize>> {
    let Some((first, rest)) = kinds.split_first() else {
        return Some(Vec::new());
    };
    if failed[kinds.len()][at] {
        return None;
    }
    let mut a = at;
    while pattern.get(a) == Some(&b'-') {
        a += 1;
    }
    if pattern.get(a) == Some(&b'A') {
        let longest = match first {
            Nargs::Parser => pattern.len() - a - 1,
            _ => pattern[a + 1..].iter().take_while(|&&c| c == b'-').count(),
        };
        for tail in (0..=longest).rev() {
            let end = a + 1 + tail;
            if let Some(mut more) = match_from(rest, pattern, end, failed) {
                more.insert(0, end - at);
                return Some(more);
            }
        }
    }
    failed[kinds.len()][at] = true;
    None
}

/// `_match_arguments_partial`: the most leading positionals whose
/// patterns match together, and each one's count.
fn match_partial(kinds: &[Nargs], pattern: &[u8]) -> Vec<usize> {
    for k in (1..=kinds.len()).rev() {
        let mut failed = vec![vec![false; pattern.len() + 1]; k + 1];
        if let Some(out) = match_from(&kinds[..k], pattern, 0, &mut failed) {
            return out;
        }
    }
    Vec::new()
}

struct Run<'a> {
    p: &'a ArgumentParser,
    args: &'a [PyStr],
    pattern: Vec<u8>,
    options: BTreeMap<usize, Vec<Tuple>>,
    ns: &'a mut Namespace,
    seen: Vec<bool>,
    extras: Vec<PyStr>,
    unrecognized: &'a mut Vec<PyStr>,
    positionals: Vec<usize>,
    width: i64,
    hook: &'a dyn HelpText,
}

impl Run<'_> {
    fn set(&mut self, dest: &[u32], value: PyValue) {
        subparsers::set(self.ns, dest, value);
    }

    /// `take_action` with `_get_values`, `_check_value` and the action's
    /// `__call__`.
    fn take_action(&mut self, i: usize, args: Vec<PyStr>) -> Result<(), Stop> {
        self.seen[i] = true;
        let a = &self.p.actions[i];
        match &a.kind {
            Kind::Store | Kind::Parsers => {
                if let Some(choices) = &a.choices {
                    let value = args.first().cloned().unwrap_or_default();
                    if !choices.contains(&value) {
                        let mut tail = repr_str(&value);
                        tail.extend(cps(" (choose from "));
                        tail.extend(choices.join(&[0x2C, 0x20][..]));
                        tail.push(0x29);
                        return Err(argument_error(self.p, i, "invalid choice: ", &tail));
                    }
                }
                if a.kind == Kind::Parsers {
                    let (w, h) = (self.width, self.hook);
                    return subparsers::call(self.p, &args, self.ns, self.unrecognized, w, h);
                }
                let value = args.into_iter().next().unwrap_or_default();
                let dest = a.dest.clone().unwrap_or_default();
                self.set(&dest, PyValue::Str(value));
            }
            Kind::StoreTrue => {
                let dest = a.dest.clone().unwrap_or_default();
                self.set(&dest, PyValue::Bool(true));
            }
            Kind::Help => {
                let text = self.hook.format_help(self.p, self.width);
                let stdout = text.ok_or(Stop::NotPorted)?;
                return Err(Stop::Exit(Outcome {
                    code: 0,
                    stdout,
                    stderr: Vec::new(),
                }));
            }
            Kind::Version(v) => {
                let stdout =
                    usage::format_version(v, self.width, self.hook).ok_or(Stop::NotPorted)?;
                return Err(Stop::Exit(Outcome {
                    code: 0,
                    stdout,
                    stderr: Vec::new(),
                }));
            }
        }
        Ok(())
    }

    fn consume_optional(&mut self, start: usize) -> Result<usize, Stop> {
        let tuples = self.options[&start].clone();
        if tuples.len() > 1 {
            let mut m = cps("ambiguous option: ");
            m.extend(&self.args[start]);
            m.extend(cps(" could match "));
            let names: Vec<PyStr> = tuples.iter().map(|t| t.1.clone()).collect();
            m.extend(names.join(&[0x2C, 0x20][..]));
            return Err(Stop::Error(m));
        }
        let (mut action, mut option, mut sep, mut explicit) = tuples[0].clone();
        let mut taken: Vec<(usize, Vec<PyStr>)> = Vec::new();
        let stop;
        loop {
            let Some(i) = action else {
                self.extras.push(self.args[start].clone());
                return Ok(start + 1);
            };
            let Some(exp) = explicit.clone() else {
                let from = start + 1;
                let count = self.p.match_argument(i, &self.pattern[from..])?;
                stop = from + count;
                taken.push((i, self.args[from..stop].to_vec()));
                break;
            };
            let count = self.p.match_argument(i, b"A")?;
            // An option string of one character raises IndexError here.
            let second = *option.get(1).ok_or(Stop::NotPorted)?;
            if count == 0 && second != DASH && !exp.is_empty() {
                if sep.as_ref().is_some_and(|s| !s.is_empty()) || exp[0] == DASH {
                    return Err(ignored(self.p, i, &exp));
                }
                taken.push((i, Vec::new()));
                option = vec![option[0], exp[0]];
                match self.p.lookup(&option) {
                    Some(j) => {
                        action = Some(j);
                        let rest = exp[1..].to_vec();
                        (sep, explicit) = match rest.first() {
                            None => (None, None),
                            Some(&EQ) => (Some(vec![EQ]), Some(rest[1..].to_vec())),
                            Some(_) => (Some(Vec::new()), Some(rest)),
                        };
                    }
                    None => {
                        let mut extra = vec![option[0]];
                        extra.extend(&exp);
                        self.extras.push(extra);
                        stop = start + 1;
                        break;
                    }
                }
            } else if count == 1 {
                stop = start + 1;
                taken.push((i, vec![exp]));
                break;
            } else {
                return Err(ignored(self.p, i, &exp));
            }
        }
        for (i, args) in taken {
            self.take_action(i, args)?;
        }
        Ok(stop)
    }

    fn consume_positionals(&mut self, mut start: usize) -> Result<usize, Stop> {
        let p = self.p;
        let mut kinds = Vec::new();
        for &i in &self.positionals {
            match nargs(&p.actions[i].kind) {
                Nargs::Zero => return Err(Stop::NotPorted),
                Nargs::Parser if p.subparsers.is_none() => return Err(Stop::NotPorted),
                n => kinds.push(n),
            }
        }
        let counts = match_partial(&kinds, &self.pattern[start..]);
        for (n, &count) in counts.iter().enumerate() {
            let mut args = self.args[start..start + count].to_vec();
            if matches!(kinds[n], Nargs::Parser) {
                if self.pattern[start] == b'-' {
                    args.remove(0);
                }
            } else if self.pattern[start..start + count].contains(&b'-') {
                let at = args.iter().position(|a| a[..] == [DASH, DASH]);
                args.remove(at.expect("the '--' the pattern marks"));
            }
            start += count;
            self.take_action(self.positionals[n], args)?;
        }
        self.positionals.drain(..counts.len());
        Ok(start)
    }
}

fn ignored(p: &ArgumentParser, i: usize, explicit: &[u32]) -> Stop {
    argument_error(p, i, "ignored explicit argument ", &repr_str(explicit))
}

/// `_parse_known_args` (not intermixed): the extras, or why it stopped.
pub(crate) fn parse_known(
    p: &ArgumentParser,
    args: &[PyStr],
    ns: &mut Namespace,
    unrecognized: &mut Vec<PyStr>,
    width: i64,
    hook: &dyn HelpText,
) -> Result<Vec<PyStr>, Stop> {
    let mut pattern = Vec::with_capacity(args.len());
    let mut options = BTreeMap::new();
    for (i, arg) in args.iter().enumerate() {
        if arg[..] == [DASH, DASH] {
            pattern.push(b'-');
            pattern.resize(args.len(), b'A');
            break;
        }
        match p.parse_optional(arg) {
            None => pattern.push(b'A'),
            Some(t) => {
                options.insert(i, t);
                pattern.push(b'O');
            }
        }
    }
    let positionals = (0..p.actions.len())
        .filter(|&i| p.actions[i].option_strings.is_empty())
        .collect();
    let mut run = Run {
        p,
        args,
        pattern,
        options,
        ns,
        seen: vec![false; p.actions.len()],
        extras: Vec::new(),
        unrecognized,
        positionals,
        width,
        hook,
    };
    let last = run.options.keys().next_back().copied();
    let mut start = 0;
    while last.is_some_and(|m| start <= m) {
        let m = last.unwrap_or(0);
        let mut next = start;
        while next <= m && !run.options.contains_key(&next) {
            next += 1;
        }
        if start != next {
            let end = run.consume_positionals(start)?;
            if end > start {
                start = end;
                continue;
            }
            start = end;
        }
        if !run.options.contains_key(&start) {
            run.extras.extend_from_slice(&args[start..next]);
            start = next;
        }
        start = run.consume_optional(start)?;
    }
    let stop = run.consume_positionals(start)?;
    run.extras.extend_from_slice(&args[stop..]);
    let required: Vec<PyStr> = (0..p.actions.len())
        .filter(|&i| !run.seen[i] && p.actions[i].required)
        .map(|i| action_name(p, i))
        .collect();
    if !required.is_empty() {
        let mut m = cps("the following arguments are required: ");
        m.extend(required.join(&[0x2C, 0x20][..]));
        return Err(Stop::Error(m));
    }
    Ok(run.extras)
}
