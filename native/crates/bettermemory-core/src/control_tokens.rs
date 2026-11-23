//! `control_tokens` (529 lines at 465bc600): the chat-template delimiters
//! a stored text may not carry, read as the reference reads them.
//!
//! Texts are code points ([`PyStr`]), so an offset is a Python `str`
//! index and a lone surrogate is a character like any other. The pattern
//! `_CONTROL_TOKEN_RE` is matched by hand ([`next_match`]): every
//! alternative's name class excludes its closing delimiter, so the greedy
//! bounded runs never backtrack into a different answer, and the whole
//! pattern reads as a fixed decision per start position. `\s` is
//! `str.isspace` ([`unicode::is_space`]), as `re` reads it on a `str`
//! pattern.

use crate::printable;
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode;
use std::collections::HashMap;

/// `_CONTROL_TOKENS`: each family and its tokens, in the reference's order.
pub const CONTROL_TOKENS: &[(&str, &[&str])] = &[
    ("chatml", &["<|im_start|>", "<|im_end|>", "<|im_sep|>"]),
    (
        "openai",
        &[
            "<|endoftext|>",
            "<|startoftext|>",
            "<|endofprompt|>",
            "<|fim_prefix|>",
            "<|fim_middle|>",
            "<|fim_suffix|>",
        ],
    ),
    (
        "harmony",
        &[
            "<|start|>",
            "<|end|>",
            "<|message|>",
            "<|channel|>",
            "<|constrain|>",
            "<|return|>",
            "<|call|>",
        ],
    ),
    (
        "llama3",
        &[
            "<|begin_of_text|>",
            "<|end_of_text|>",
            "<|start_header_id|>",
            "<|end_header_id|>",
            "<|eot_id|>",
            "<|eom_id|>",
            "<|python_tag|>",
            "<|finetune_right_pad_id|>",
        ],
    ),
    ("llama4", &["<|header_start|>", "<|header_end|>", "<|eot|>"]),
    ("llama2", &["[INST]", "[/INST]", "<<SYS>>", "<</SYS>>"]),
    ("phi", &["<|system|>", "<|user|>", "<|assistant|>"]),
    ("gemma", &["<start_of_turn>", "<end_of_turn>"]),
    (
        "command-r",
        &[
            "<|START_OF_TURN_TOKEN|>",
            "<|END_OF_TURN_TOKEN|>",
            "<|SYSTEM_TOKEN|>",
            "<|USER_TOKEN|>",
            "<|CHATBOT_TOKEN|>",
        ],
    ),
    (
        "granite",
        &["<|start_of_role|>", "<|end_of_role|>", "<|tool_call|>"],
    ),
    (
        "deepseek",
        &[
            "<\u{ff5c}begin\u{2581}of\u{2581}sentence\u{ff5c}>",
            "<\u{ff5c}end\u{2581}of\u{2581}sentence\u{ff5c}>",
            "<\u{ff5c}User\u{ff5c}>",
            "<\u{ff5c}Assistant\u{ff5c}>",
            "<\u{ff5c}tool\u{2581}calls\u{2581}begin\u{ff5c}>",
            "<\u{ff5c}tool\u{2581}calls\u{2581}end\u{ff5c}>",
            "<\u{ff5c}tool\u{2581}call\u{2581}begin\u{ff5c}>",
            "<\u{ff5c}tool\u{2581}call\u{2581}end\u{ff5c}>",
            "<\u{ff5c}tool\u{2581}sep\u{ff5c}>",
            "<\u{ff5c}tool\u{2581}outputs\u{2581}begin\u{ff5c}>",
            "<\u{ff5c}tool\u{2581}outputs\u{2581}end\u{ff5c}>",
            "<\u{ff5c}tool\u{2581}output\u{2581}begin\u{ff5c}>",
            "<\u{ff5c}tool\u{2581}output\u{2581}end\u{ff5c}>",
        ],
    ),
];

/// `_TURN_MARKER_KIND`.
const TURN_MARKER_KIND: &str = "anthropic";
const BAR: u32 = 0xFF5C;

/// `_CONTROL_TOKEN_KIND.get(token)`.
fn kind_of(token: &[u32]) -> Option<&'static str> {
    CONTROL_TOKENS
        .iter()
        .find(|(_, ts)| {
            ts.iter()
                .any(|t| t.chars().map(u32::from).eq(token.iter().copied()))
        })
        .map(|&(k, _)| k)
}

/// One match of `_CONTROL_TOKEN_RE`: the whole span, the named group's
/// span, and whether that group is `turn`.
#[derive(Clone, Copy, Debug)]
struct Match {
    start: usize,
    end: usize,
    gstart: usize,
    gend: usize,
    turn: bool,
}

fn at(text: &[u32], i: usize) -> Option<u32> {
    text.get(i).copied()
}

fn is_char(text: &[u32], i: usize, c: char) -> bool {
    at(text, i) == Some(u32::from(c))
}

/// The end of a `{1,32}` run of `class` from `from`, when the run is 1 to
/// 32 long and is not followed by a 33rd member (each class excludes its
/// closing delimiter, so a longer run can match no shorter one).
fn run(text: &[u32], from: usize, class: impl Fn(u32) -> bool) -> Option<usize> {
    let mut k = 0;
    while k < 33 && at(text, from + k).is_some_and(&class) {
        k += 1;
    }
    (1..=32).contains(&k).then_some(from + k)
}

fn upper(c: u32) -> bool {
    (u32::from(b'A')..=u32::from(b'Z')).contains(&c)
}

fn pipe_class(c: u32) -> bool {
    upper(c) || (u32::from(b'a')..=u32::from(b'z')).contains(&c) || c == u32::from(b'_')
}

fn angle_class(c: u32) -> bool {
    (u32::from(b'a')..=u32::from(b'z')).contains(&c) || c == u32::from(b'_')
}

fn wide_class(c: u32) -> bool {
    c != BAR && !"<>[]".chars().any(|d| u32::from(d) == c) && !unicode::is_space(c)
}

/// `(Human|Assistant):` at `j`: the name's end.
fn speaker_at(text: &[u32], j: usize) -> Option<usize> {
    for name in ["Human", "Assistant"] {
        let n = name.len();
        if text.len() > j + n
            && name
                .bytes()
                .map(u32::from)
                .eq(text[j..j + n].iter().copied())
            && text[j + n] == u32::from(b':')
        {
            return Some(j + n);
        }
    }
    None
}

/// A delimited shape opened at `i` by `open` (1 or 2 code points), with
/// an optional `/`, a run of `class` and the closer `close`.
fn shape(
    text: &[u32],
    i: usize,
    open: usize,
    slash: bool,
    class: impl Fn(u32) -> bool,
    close: &[u32],
) -> Option<Match> {
    let gstart = i + open;
    let mut j = gstart;
    if slash && is_char(text, j, '/') {
        j += 1;
    }
    let gend = run(text, j, class)?;
    let end = gend + close.len();
    (text.len() >= end && text[gend..end] == *close).then_some(Match {
        start: i,
        end,
        gstart,
        gend,
        turn: false,
    })
}

/// `_CONTROL_TOKEN_RE.match(text, i)`, its alternatives in order.
fn match_at(text: &[u32], i: usize) -> Option<Match> {
    let c = at(text, i)?;
    let lt = u32::from(b'<');
    let found = if c == lt {
        match at(text, i + 1) {
            Some(0x7C) => shape(text, i, 2, false, pipe_class, &[0x7C, 0x3E]),
            Some(BAR) => shape(text, i, 2, false, wide_class, &[BAR, 0x3E]),
            Some(0x3C) => shape(text, i, 2, true, upper, &[0x3E, 0x3E]),
            _ => shape(text, i, 1, false, angle_class, &[0x3E]),
        }
    } else if c == u32::from(b'[') {
        shape(text, i, 1, true, upper, &[0x5D])
    } else {
        None
    };
    if found.is_some() {
        return found;
    }
    let turn = |j: usize| {
        speaker_at(text, j).map(|gend| Match {
            start: i,
            end: gend + 1,
            gstart: j,
            gend,
            turn: true,
        })
    };
    if i == 0 {
        let mut j = 0;
        while at(text, j).is_some_and(unicode::is_space) {
            j += 1;
        }
        if let Some(m) = turn(j) {
            return Some(m);
        }
    }
    if c == 0x0A {
        let mut j = i + 1;
        while matches!(at(text, j), Some(0x20 | 0x09 | 0x0D)) {
            j += 1;
        }
        if is_char(text, j, '\n') {
            return turn(j + 1);
        }
    }
    None
}

/// The next match at or after `from`, as `finditer` and `sub` scan.
fn next_match(text: &[u32], from: usize) -> Option<Match> {
    (from..text.len()).find_map(|i| match_at(text, i))
}

/// Every match in order, non-overlapping, as `finditer` gives them.
fn matches(text: &[u32]) -> impl Iterator<Item = Match> + '_ {
    let mut pos = 0;
    std::iter::from_fn(move || {
        let m = next_match(text, pos)?;
        pos = m.end;
        Some(m)
    })
}

/// `_matches(text)`: (offset, family, bare name) of every listed token.
fn listed(text: &[u32]) -> impl Iterator<Item = (usize, &'static str, &[u32])> + '_ {
    matches(text).filter_map(move |m| {
        let name = &text[m.gstart..m.gend];
        if m.turn {
            Some((m.gstart, TURN_MARKER_KIND, name))
        } else {
            kind_of(&text[m.start..m.end]).map(|k| (m.start, k, name))
        }
    })
}

/// `ControlTokenHit`: a token's family and its bare name.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Hit {
    pub kind: &'static str,
    pub token: PyStr,
}

/// `find_control_tokens(text)`: each listed token once, first-seen order.
pub fn find_control_tokens(text: &[u32]) -> Vec<Hit> {
    let mut hits: Vec<Hit> = Vec::new();
    for (_, kind, bare) in listed(text) {
        if !hits.iter().any(|h| h.kind == kind && h.token == bare) {
            hits.push(Hit {
                kind,
                token: bare.to_vec(),
            });
        }
    }
    hits
}

/// `control_token_offsets(text)`: (family, first offset, count), the
/// families in the order the text first carries them.
pub fn control_token_offsets(text: &[u32]) -> Vec<(&'static str, usize, usize)> {
    let mut out: Vec<(&'static str, usize, usize)> = Vec::new();
    for (start, kind, _) in listed(text) {
        match out.iter_mut().find(|e| e.0 == kind) {
            Some(e) => e.2 += 1,
            None => out.push((kind, start, 1)),
        }
    }
    out
}

/// `_touches(ch, apart=..., after=...)`.
fn touches(ch: u32, apart: bool, after: bool) -> bool {
    if apart {
        return !unicode::is_space(ch);
    }
    unicode::is_alnum(ch)
        || ch == u32::from(b'_')
        || (after && (ch == u32::from(b'<') || ch == u32::from(b'[')))
}

/// `_named(match, apart=...)`, appended to `out`.
fn named_into(text: &[u32], m: Match, apart: bool, out: &mut PyStr) {
    let whole = &text[m.start..m.end];
    if !m.turn && kind_of(whole).is_none() {
        out.extend_from_slice(whole);
        return;
    }
    let name = &text[m.gstart..m.gend];
    let speaker = name == unicode::cps("Human") || name == unicode::cps("Assistant");
    let after = m.end < text.len() && touches(text[m.end], apart || speaker, true);
    if m.turn {
        out.extend_from_slice(&text[m.start..m.gend]);
    } else {
        if m.start > 0 && touches(text[m.start - 1], apart, false) {
            out.push(0x20);
        }
        out.extend_from_slice(name);
    }
    if after {
        out.push(0x20);
    }
}

/// `_CONTROL_TOKEN_RE.sub(lambda m: _named(m, apart=apart), text)`.
fn sub_named(text: &[u32], apart: bool) -> PyStr {
    let mut out = Vec::with_capacity(text.len());
    let mut last = 0;
    for m in matches(text) {
        out.extend_from_slice(&text[last..m.start]);
        named_into(text, m, apart, &mut out);
        last = m.end;
    }
    out.extend_from_slice(&text[last..]);
    out
}

fn carries_any(text: &[u32]) -> bool {
    listed(text).next().is_some()
}

/// `defang_control_tokens(text)`: `None` where the reference returns
/// `text` itself (it carries no listed token).
fn defanged(text: &[u32]) -> Option<PyStr> {
    if !carries_any(text) {
        return None;
    }
    let near = sub_named(text, false);
    if !carries_any(&near) {
        return Some(near);
    }
    Some(sub_named(text, true))
}

/// `defang_control_tokens(text)`.
pub fn defang_control_tokens(text: &[u32]) -> PyStr {
    defanged(text).unwrap_or_else(|| text.to_vec())
}

/// `" ".join(text.split())`.
fn fold_space(text: &[u32]) -> PyStr {
    let mut out = Vec::with_capacity(text.len());
    for word in text
        .split(|&c| unicode::is_space(c))
        .filter(|w| !w.is_empty())
    {
        if !out.is_empty() {
            out.push(0x20);
        }
        out.extend_from_slice(word);
    }
    out
}

/// `folded_quote(text)`.
pub fn folded_quote(text: &[u32]) -> PyStr {
    fold_space(&defang_control_tokens(&fold_space(text)))
}

/// A dict key in a form two keys share exactly when Python's `==` holds
/// between them: strings, None, numbers across `bool`, `int` and `float`
/// (an integral number by its decimal text, `-0.0` as `0`), paths,
/// datetimes and tuples element by element. `None` for a key holding a
/// NaN, which equals nothing (two keys are never one object here), or a
/// list or dict, which this port never finds equal to another key.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
enum Key {
    None,
    Integral(String),
    Fraction(u64),
    Str(PyStr),
    Path(PyStr),
    DateTime(String),
    Tuple(Vec<Key>),
}

fn key_of(v: &PyValue) -> Option<Key> {
    Some(match v {
        PyValue::None => Key::None,
        PyValue::Bool(b) => Key::Integral(if *b { "1" } else { "0" }.to_string()),
        PyValue::Int(t) => Key::Integral(t.clone()),
        PyValue::Float(f) if f.is_nan() => return None,
        PyValue::Float(f) if f.is_finite() && f.fract() == 0.0 => {
            let t = format!("{f:.0}");
            Key::Integral(if t == "-0" { "0".to_string() } else { t })
        }
        PyValue::Float(f) => Key::Fraction(f.to_bits()),
        PyValue::Str(s) => Key::Str(s.clone()),
        PyValue::Path(s) => Key::Path(s.clone()),
        PyValue::DateTime(d) => Key::DateTime(format!("{d:?}")),
        PyValue::Tuple(items) => Key::Tuple(items.iter().map(key_of).collect::<Option<_>>()?),
        PyValue::List(_) | PyValue::Dict(_) => return None,
    })
}

/// `dict(zip(keys, values))`: the first key kept, the last value.
fn dict_of(pairs: Vec<PyValue>) -> PyValue {
    let mut out: Vec<(PyValue, PyValue)> = Vec::with_capacity(pairs.len() / 2);
    let mut index: HashMap<Key, usize> = HashMap::new();
    let mut it = pairs.into_iter();
    while let (Some(k), Some(v)) = (it.next(), it.next()) {
        let key = key_of(&k);
        match key.as_ref().and_then(|key| index.get(key)).copied() {
            Some(i) => out[i].1 = v,
            None => {
                if let Some(key) = key {
                    index.insert(key, out.len());
                }
                out.push((k, v));
            }
        }
    }
    PyValue::Dict(out)
}

/// A container's `i`-th child in `_children`'s order, if any.
fn child(node: &PyValue, i: usize) -> Option<&PyValue> {
    match node {
        PyValue::Dict(items) => items
            .get(i / 2)
            .map(|(k, v)| if i.is_multiple_of(2) { k } else { v }),
        PyValue::List(items) | PyValue::Tuple(items) => items.get(i),
        _ => None,
    }
}

fn is_container(v: &PyValue) -> bool {
    matches!(v, PyValue::Dict(_) | PyValue::List(_) | PyValue::Tuple(_))
}

/// One container on the walk's stack: its next child, and each child read
/// so far (`None` where the child came back as it was).
struct Visit<'a> {
    node: &'a PyValue,
    next: usize,
    done: Vec<Option<PyValue>>,
}

/// `_rebuilt(node, parts)`: `None` where every part is the child it was.
fn rebuilt(visit: Visit<'_>) -> Option<PyValue> {
    if visit.done.iter().all(Option::is_none) {
        return None;
    }
    let parts: Vec<PyValue> = visit
        .done
        .into_iter()
        .enumerate()
        .map(|(i, p)| p.unwrap_or_else(|| child(visit.node, i).cloned().unwrap_or(PyValue::None)))
        .collect();
    Some(match visit.node {
        PyValue::Dict(_) => dict_of(parts),
        PyValue::Tuple(_) => PyValue::Tuple(parts),
        _ => PyValue::List(parts),
    })
}

/// `named_tokens(value)`: every string in it, dict keys included and at
/// any depth, written by `defang_control_tokens`; a container rebuilt only
/// where a string in it changed. The walk keeps its own stack.
pub fn named_tokens(value: &PyValue) -> PyValue {
    changed_named_tokens(value).unwrap_or_else(|| value.clone())
}

pub(crate) fn changed_named_tokens(value: &PyValue) -> Option<PyValue> {
    match value {
        PyValue::Str(s) => return defanged(s).map(PyValue::Str),
        v if !is_container(v) => return None,
        _ => {}
    }
    let mut stack = vec![Visit {
        node: value,
        next: 0,
        done: Vec::new(),
    }];
    loop {
        let top = stack.last_mut().expect("a container");
        let Some(c) = child(top.node, top.next) else {
            let visit = stack.pop().expect("a container");
            let made = rebuilt(visit);
            match stack.last_mut() {
                Some(parent) => parent.done.push(made),
                None => return made,
            }
            continue;
        };
        top.next += 1;
        if is_container(c) {
            stack.push(Visit {
                node: c,
                next: 0,
                done: Vec::new(),
            });
        } else {
            let made = match c {
                PyValue::Str(s) => defanged(s).map(PyValue::Str),
                _ => None,
            };
            top.done.push(made);
        }
    }
}

/// `_FIELD_WHY`.
pub const FIELD_WHY: &str = "The store keeps this text, and a later read can put it in front of a \
model, where the token can forge a turn boundary, so no flag passes this refusal.";

/// `BARE_NAME_REMEDY`.
pub const BARE_NAME_REMEDY: &str = "Write the token's bare name without its delimiters \
(`im_start`, `INST`) or describe it in words; a token of kind `anthropic` is `Human:` or \
`Assistant:` opening the text or a line after a blank line: quote the line (`> Human: ...`) \
or name the speaker another way.";

/// `ControlTokenError`: the refusal's text.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ControlTokenError {
    pub message: PyStr,
}

/// `refuse_control_tokens(field, text, remedy=remedy, why=why)`: `text`
/// `None` is the reference's None.
pub fn refuse_control_tokens(
    field: &[u32],
    text: Option<&[u32]>,
    remedy: &[u32],
    why: &[u32],
) -> Result<(), ControlTokenError> {
    let Some(text) = text.filter(|t| !t.is_empty()) else {
        return Ok(());
    };
    let hits = find_control_tokens(text);
    if hits.is_empty() {
        return Ok(());
    }
    let s = unicode::cps;
    let mut m = s("`");
    m.extend_from_slice(field);
    m.extend(s(
        "` carries a chat-template control token, named here by template family and bare name: ",
    ));
    for (i, hit) in hits.iter().enumerate() {
        if i > 0 {
            m.extend(s(", "));
        }
        m.extend(s(hit.kind));
        m.push(0x20);
        printable::quoted_into(&hit.token, &mut m);
    }
    m.extend(s(". "));
    m.extend_from_slice(why);
    m.push(0x20);
    m.extend_from_slice(remedy);
    m.extend(s(" Then send the call again."));
    Err(ControlTokenError { message: m })
}
