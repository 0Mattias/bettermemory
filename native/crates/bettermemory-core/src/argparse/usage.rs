//! `HelpFormatter._format_usage` of 3.13.13 (`argparse.py` 298-497) as
//! `format_usage` returns it, with its wrapping at the formatter's width,
//! and the version text `_VersionAction` prints (`_format_text`,
//! `_fill_text`), both through `format_help`'s last pass.

use super::{ArgumentParser, HelpText, Kind};
use crate::pyvalue::PyStr;
use crate::unicode::{cps, is_space, upper};

const NL: u32 = 0x0A;

/// `HelpFormatter.format_help` of a root section holding one item: the
/// item between newlines, runs of three or more newlines made two
/// (`_long_break_matcher`), newlines stripped from both ends, one added.
fn finish(item: &[u32]) -> PyStr {
    let mut text = vec![NL];
    text.extend(item);
    text.push(NL);
    let mut out: PyStr = Vec::with_capacity(text.len() + 1);
    let mut i = 0;
    while i < text.len() {
        let run = text[i..].iter().take_while(|&&c| c == NL).count();
        if run == 0 {
            out.push(text[i]);
            i += 1;
        } else {
            out.extend(std::iter::repeat_n(NL, if run >= 3 { 2 } else { run }));
            i += run;
        }
    }
    let start = out.iter().position(|&c| c != NL).unwrap_or(out.len());
    let end = out.iter().rposition(|&c| c != NL).map_or(start, |e| e + 1);
    let mut out = out[start..end].to_vec();
    out.push(NL);
    out
}

fn bracket(part: PyStr) -> PyStr {
    let mut out = vec![0x5B];
    out.extend(part);
    out.push(0x5D);
    out
}

/// `_metavar_formatter`'s one metavar: the metavar, `{choices}` or the
/// default one.
fn metavar(a: &super::Action, default: PyStr) -> PyStr {
    if let Some(m) = &a.metavar {
        return m.clone();
    }
    match &a.choices {
        Some(c) => {
            let mut out = vec![0x7B];
            out.extend(c.join(&0x2C));
            out.push(0x7D);
            out
        }
        None => default,
    }
}

/// `get_lines` of `_format_usage`.
fn get_lines(parts: &[PyStr], indent: usize, prefix: Option<usize>, width: i64) -> Vec<PyStr> {
    let mut lines: Vec<PyStr> = Vec::new();
    let mut line: Vec<&PyStr> = Vec::new();
    let mut len = prefix.unwrap_or(indent) as i64 - 1;
    let emit = |line: &[&PyStr]| {
        let mut l = vec![0x20; indent];
        let words: Vec<PyStr> = line.iter().map(|w| (*w).clone()).collect();
        l.extend(words.join(&0x20));
        l
    };
    for part in parts {
        if len + 1 + part.len() as i64 > width && !line.is_empty() {
            lines.push(emit(&line));
            line.clear();
            len = indent as i64 - 1;
        }
        line.push(part);
        len += part.len() as i64 + 1;
    }
    if !line.is_empty() {
        lines.push(emit(&line));
    }
    if prefix.is_some() {
        let cut = indent.min(lines[0].len());
        lines[0].drain(..cut);
    }
    lines
}

/// `parser.format_usage()` with the formatter at `width`.
pub fn format_usage(p: &ArgumentParser, width: i64) -> PyStr {
    let prefix = cps("usage: ");
    let prog = p.prog.clone();
    let (mut opt, mut pos) = (Vec::new(), Vec::new());
    for a in &p.actions {
        if a.option_strings.is_empty() {
            pos.push(metavar(a, a.dest.clone().unwrap_or_default()));
            continue;
        }
        let mut part = a.option_strings[0].clone();
        if a.kind == Kind::Store {
            part.push(0x20);
            part.extend(metavar(a, upper(&a.dest.clone().unwrap_or_default())));
        }
        opt.push(if a.required { part } else { bracket(part) });
    }
    let mut usage = prog.clone();
    if !p.actions.is_empty() {
        let all: Vec<PyStr> = opt.iter().chain(&pos).cloned().collect();
        // `' '.join([s for s in [prog, action_usage] if s])`
        let joined = all.join(&0x20);
        usage = [prog.clone(), joined]
            .into_iter()
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join(&0x20);
        if (prefix.len() + usage.len()) as i64 > width {
            let lines = if 4 * (prefix.len() + prog.len()) as i128 <= 3 * i128::from(width) {
                let indent = prefix.len() + prog.len() + 1;
                let with_prog = |parts: &[PyStr]| {
                    let mut v = vec![prog.clone()];
                    v.extend_from_slice(parts);
                    v
                };
                if !opt.is_empty() {
                    let mut l = get_lines(&with_prog(&opt), indent, Some(prefix.len()), width);
                    l.extend(get_lines(&pos, indent, None, width));
                    l
                } else if !pos.is_empty() {
                    get_lines(&with_prog(&pos), indent, Some(prefix.len()), width)
                } else {
                    vec![prog.clone()]
                }
            } else {
                let indent = prefix.len();
                let all: Vec<PyStr> = opt.iter().chain(&pos).cloned().collect();
                let mut l = get_lines(&all, indent, None, width);
                if l.len() > 1 {
                    l = get_lines(&opt, indent, None, width);
                    l.extend(get_lines(&pos, indent, None, width));
                }
                let mut v = vec![prog.clone()];
                v.extend(l);
                v
            };
            usage = lines.join(&NL);
        }
    }
    let mut item = prefix;
    item.extend(usage);
    item.extend([NL, NL]);
    finish(&item)
}

/// What `_VersionAction` prints: the text with ASCII whitespace runs made
/// one space and stripped, filled at `max(width, 11)`. A text that fits
/// is its own fill; a longer one, or one naming `%(prog)`, is the hook's.
pub fn format_version(text: &[u32], width: i64, hook: &dyn HelpText) -> Option<PyStr> {
    let marker = cps("%(prog)");
    if text.windows(marker.len()).any(|w| w == marker) {
        return None;
    }
    let mut collapsed: PyStr = Vec::with_capacity(text.len());
    for &c in text {
        let ws = matches!(c, 0x09..=0x0D | 0x20);
        if !(ws && collapsed.last() == Some(&0x20)) {
            collapsed.push(if ws { 0x20 } else { c });
        }
    }
    let start = collapsed
        .iter()
        .position(|&c| !is_space(c))
        .unwrap_or(collapsed.len());
    let end = collapsed
        .iter()
        .rposition(|&c| !is_space(c))
        .map_or(start, |e| e + 1);
    let text = &collapsed[start..end];
    let fill_width = width.max(11);
    let mut item = if text.len() as i64 <= fill_width {
        text.to_vec()
    } else {
        hook.fill(text, fill_width)?
    };
    item.extend([NL, NL]);
    Some(finish(&item))
}
