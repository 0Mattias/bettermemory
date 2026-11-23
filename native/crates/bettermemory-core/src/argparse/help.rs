//! The default, non-coloured English help listing of CPython 3.13.
//! The parser supplies its ordered actions, child choices and explicit width.
use super::{usage, Action, ArgumentParser, HelpText, Kind};
use crate::pyvalue::PyStr;
use crate::unicode::{cps, is_re_digit, is_space, is_word, py_strip, upper};
use std::collections::VecDeque;

const SPACE: u32 = 32;
const NL: u32 = 10;
const DASH: u32 = 45;

/// Default help and version text formatting for the native parser model.
pub struct DefaultHelp;

pub(super) fn normalized(text: &[u32]) -> PyStr {
    let mut out = Vec::with_capacity(text.len());
    for &c in py_strip(text) {
        if matches!(c, 9..=13 | SPACE) {
            if out.last() != Some(&SPACE) {
                out.push(SPACE);
            }
        } else {
            out.push(c);
        }
    }
    out
}

// textwrap expands tabs before translating its six ASCII whitespace characters.
// A carriage return or newline resets the tab column before translation.
fn expand_whitespace(text: &[u32]) -> PyStr {
    let mut out = Vec::with_capacity(text.len());
    let mut column = 0;
    for &c in text {
        match c {
            9 => {
                out.extend(std::iter::repeat_n(SPACE, 8 - column));
                column = 0;
            }
            NL | 13 => {
                out.push(SPACE);
                column = 0;
            }
            _ => {
                out.push(if matches!(c, 11 | 12) { SPACE } else { c });
                column = (column + 1) % 8;
            }
        }
    }
    out
}

// textwrap's "letter" class is a regex word character other than a
// decimal digit. Its hyphen break needs letters on both sides.
fn hyphen_break(text: &[u32], at: usize) -> bool {
    let letter = |i: usize| text.get(i).is_some_and(|&c| is_word(c) && !is_re_digit(c));
    let left = at >= 2
        && letter(at - 1)
        && (letter(at - 2) || (at >= 3 && text[at - 2] == DASH && letter(at - 3)));
    left && letter(at + 1)
        && (letter(at + 2) || (text.get(at + 2) == Some(&DASH) && letter(at + 3)))
}

fn chunks(text: &[u32]) -> VecDeque<&[u32]> {
    let mut out = VecDeque::new();
    let (mut start, mut i) = (0, 0);
    while i < text.len() {
        let c = text[i];
        let word_punct = |c| {
            is_word(c)
                || matches!(
                    char::from_u32(c),
                    Some('!' | '"' | '\'' | '&' | '.' | ',' | '?')
                )
        };
        let mut end = i;
        if c == SPACE {
            end += text[i..].iter().take_while(|&&v| v == SPACE).count();
        } else if c == DASH && i > 0 && word_punct(text[i - 1]) && text.get(i + 1) == Some(&DASH) {
            let next = i + text[i..].iter().take_while(|&&v| v == DASH).count();
            if text.get(next).is_some_and(|&v| is_word(v)) {
                // An ASCII dash run between words is a separate chunk.
                end = next;
            }
        }
        if end > i {
            if start < i {
                out.push_back(&text[start..i]);
            }
            out.push_back(&text[i..end]);
            start = end;
            i = end;
        } else {
            if c == DASH && i > start && hyphen_break(text, i) {
                out.push_back(&text[start..=i]);
                start = i + 1;
            }
            i += 1;
        }
    }
    if start < text.len() {
        out.push_back(&text[start..]);
    }
    out
}

fn wrap(text: &[u32], width: usize) -> Vec<PyStr> {
    debug_assert!(width > 0);
    let mut chunks = chunks(text);
    let mut lines = Vec::new();
    let blank = |chunk: &&[u32]| chunk.iter().all(|&c| is_space(c));
    while !chunks.is_empty() {
        if !lines.is_empty() && chunks.front().is_some_and(blank) {
            chunks.pop_front();
        }
        let mut parts = Vec::new();
        let mut room = width;
        while let Some(&chunk) = chunks.front() {
            if chunk.len() <= room {
                chunks.pop_front();
                parts.push(chunk);
                room -= chunk.len();
            } else {
                if chunk.len() > width && room > 0 {
                    let cut = chunk[..room]
                        .iter()
                        .rposition(|&c| c == DASH)
                        .filter(|&idx| chunk[..idx].iter().any(|&c| c != DASH))
                        .map_or(room, |idx| idx + 1);
                    parts.push(&chunk[..cut]);
                    *chunks.front_mut().expect("the current chunk") = &chunk[cut..];
                }
                break;
            }
        }
        if parts.last().is_some_and(blank) {
            parts.pop();
        }
        if !parts.is_empty() {
            lines.push(parts.concat());
        }
    }
    lines
}

// The shipped pages need literal text. The two simple %-forms also occur
// in ordinary callers. A formatter that needs other mapping conversions
// can still be supplied through HelpText.
fn expand(text: &[u32], prog: &[u32]) -> Option<PyStr> {
    let marker = cps("%(prog)s");
    let mut out = Vec::with_capacity(text.len());
    let mut rest = text;
    while let Some(pos) = rest.iter().position(|&c| c == 37) {
        out.extend_from_slice(&rest[..pos]);
        rest = &rest[pos..];
        if rest.starts_with(&[37, 37]) {
            out.push(37);
            rest = &rest[2..];
        } else if rest.starts_with(&marker) {
            out.extend_from_slice(prog);
            rest = &rest[marker.len()..];
        } else {
            return None;
        }
    }
    out.extend_from_slice(rest);
    Some(out)
}

struct Entry<'a> {
    indent: usize,
    invocation: PyStr,
    help: Option<&'a [u32]>,
}

fn invocation(a: &Action) -> PyStr {
    let dest = a.dest.as_deref().unwrap_or_default();
    if a.option_strings.is_empty() {
        return usage::metavar(a, dest.to_vec());
    }
    let mut result = Vec::new();
    for name in &a.option_strings {
        if !result.is_empty() {
            result.extend([44, SPACE]);
        }
        result.extend(name);
    }
    if a.kind.takes_value() {
        result.push(SPACE);
        result.extend(usage::metavar(a, upper(dest)));
    }
    result
}

fn render_entry(
    out: &mut PyStr,
    entry: &Entry<'_>,
    column: usize,
    width: usize,
    prog: &[u32],
) -> Option<()> {
    out.extend(std::iter::repeat_n(SPACE, entry.indent));
    out.extend(&entry.invocation);
    let Some(help) = entry.help.filter(|v| !v.is_empty()) else {
        out.push(NL);
        return Some(());
    };
    let same_line = entry.indent + entry.invocation.len() + 2 <= column;
    if same_line {
        out.extend(std::iter::repeat_n(
            SPACE,
            column - entry.indent - entry.invocation.len(),
        ));
    } else {
        out.push(NL);
    }
    if py_strip(help).is_empty() {
        if same_line {
            out.push(NL);
        }
        return Some(());
    }
    let expanded = expand(help, prog)?;
    // Python indexes the first wrapped line of a nonempty expansion.
    // A whitespace-only program name can make that list empty.
    if !expanded.is_empty() && py_strip(&expanded).is_empty() {
        return None;
    }
    for (i, line) in wrap(&normalized(&expanded), width).iter().enumerate() {
        if !same_line || i != 0 {
            out.extend(std::iter::repeat_n(SPACE, column));
        }
        out.extend(line);
        out.push(NL);
    }
    Some(())
}

impl HelpText for DefaultHelp {
    fn format_help(&self, parser: &ArgumentParser, width: i64) -> Option<PyStr> {
        let mut groups = [Vec::new(), Vec::new()];
        for (index, action) in parser.actions.iter().enumerate() {
            let group = &mut groups[usize::from(!action.option_strings.is_empty())];
            group.push(Entry {
                indent: 2,
                invocation: invocation(action),
                help: action.help.as_deref(),
            });
            if action.kind == Kind::Parsers {
                let children = parser
                    .subparsers
                    .as_ref()
                    .filter(|sub| sub.action == index)?;
                for (name, help) in &children.choices_help {
                    group.push(Entry {
                        indent: 4,
                        invocation: name.clone(),
                        help: Some(help),
                    });
                }
            }
        }
        let longest = groups
            .iter()
            .flatten()
            .map(|entry| entry.indent + entry.invocation.len())
            .max()
            .unwrap_or(0);
        let column = (longest + 2).min(width.saturating_sub(20).clamp(4, 24) as usize);
        let content_width = |margin: usize| {
            usize::try_from(width.saturating_sub(margin as i64).max(11)).unwrap_or(usize::MAX)
        };
        let mut out = usage::format_usage(parser, width);
        out.push(NL);
        if let Some(description) = &parser.description {
            let expanded;
            let marker = cps("%(prog)");
            let text = if description.windows(marker.len()).any(|part| part == marker) {
                expanded = expand(description, &parser.prog)?;
                &expanded
            } else {
                description
            };
            out.extend(wrap(&normalized(text), content_width(0)).join(&NL));
            out.extend([NL, NL]);
        }
        for (heading, group) in ["positional arguments:", "options:"]
            .into_iter()
            .zip(groups)
        {
            if group.is_empty() {
                continue;
            }
            out.extend(cps(heading));
            out.push(NL);
            for entry in group {
                render_entry(
                    &mut out,
                    &entry,
                    column,
                    content_width(column),
                    &parser.prog,
                )?;
            }
            out.push(NL);
        }
        Some(usage::finish(&out))
    }

    fn fill(&self, text: &[u32], width: i64) -> Option<PyStr> {
        if width <= 0 {
            return None;
        }
        let size = usize::try_from(width).unwrap_or(usize::MAX);
        Some(wrap(&expand_whitespace(text), size).join(&NL))
    }
}
