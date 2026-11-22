//! What the tests of `pyre::regex` share: a row's pattern built, and one
//! call's answer line as `gen_pyre_api._answer` writes it.
#![allow(dead_code)]

use bettermemory_core::pyre::regex::{Item, Match, Regex, RegexError, Subject};
use bettermemory_core::pyre::replace::ReplaceError;
use bettermemory_core::pyre::tree::BuildError;
use serde_json::Value;

pub fn codes(v: &Value) -> Vec<u32> {
    v.as_array()
        .expect("code points")
        .iter()
        .map(|c| c.as_u64().expect("a code point") as u32)
        .collect()
}

/// A pattern as `gen_pyre_tree.spell` writes it: `~` for a backslash, or
/// the list of its code points.
pub fn unspell(v: &Value) -> Vec<u32> {
    match v.as_str() {
        Some(s) => s
            .chars()
            .map(|c| if c == '~' { 0x5c } else { u32::from(c) })
            .collect(),
        None => codes(v),
    }
}

/// A pattern and whether it is bytes.
pub struct Built {
    pub re: Regex,
    pub bytes: bool,
}

fn flags(row: &Value) -> u32 {
    row.get("flags").and_then(Value::as_u64).unwrap_or(0) as u32
}

fn compiled(row: &Value) -> Result<Regex, RegexError> {
    let pattern = unspell(&row["pattern"]);
    if row["bytes"].as_bool().expect("bytes") {
        let b: Vec<u8> = pattern.iter().map(|&c| c as u8).collect();
        Regex::new_bytes(&b, flags(row))
    } else {
        Regex::new(&pattern, flags(row))
    }
}

pub fn build(row: &Value) -> Result<Built, RegexError> {
    Ok(Built {
        re: compiled(row)?,
        bytes: row["bytes"].as_bool().expect("bytes"),
    })
}

/// `gen_pyre_parse._failed_full` of what the build answers.
pub fn refusal_name(row: &Value) -> String {
    match compiled(row) {
        Ok(_) => "built".into(),
        Err(RegexError::Build(e)) => match e {
            BuildError::Invalid { at, why } => format!("!{why:?}@{at}"),
            BuildError::Unsupported { at, what } => format!("?{what:?}@{at}"),
            BuildError::Overflow => "%Overflow".into(),
            BuildError::DigitLimit => "%DigitLimit".into(),
            BuildError::Value(why) => format!("%{why:?}"),
        },
        Err(other) => format!("{other:?}"),
    }
}

/// `gen_pyre_api._spans`, and with `detail` `_detail`.
pub fn show(m: &Match, detail: bool) -> String {
    let mut parts: Vec<String> = m.regs.iter().map(|(a, b)| format!("{a},{b}")).collect();
    parts.push(m.lastindex.map_or(-1, |i| i as isize).to_string());
    if detail {
        parts.push(format!("{},{}", m.pos, m.endpos));
        parts.push(match &m.lastgroup {
            Some(name) => name.iter().filter_map(|&c| char::from_u32(c)).collect(),
            None => "None".into(),
        });
    }
    parts.join("|")
}

fn piece<C: Copy + Into<u32>>(p: &[C]) -> String {
    let codes: Vec<String> = p.iter().map(|&c| c.into().to_string()).collect();
    format!("'{}", codes.join("."))
}

fn items<C: Copy + Into<u32>>(found: &[Item<Vec<C>>]) -> String {
    let shown: Vec<String> = found
        .iter()
        .map(|item| match item {
            Item::One(p) => piece(p),
            Item::Tuple(ps) => {
                let parts: Vec<String> = ps.iter().map(|p| piece(p)).collect();
                format!("({})", parts.join("/"))
            }
        })
        .collect();
    format!("[{}]", shown.join(","))
}

/// One call's answer, on code points or on bytes of these code points.
pub fn line(
    b: &Built,
    text: &[u32],
    pos: isize,
    endpos: isize,
    method: &str,
    detail: bool,
) -> String {
    let re = &b.re;
    let bytes: Vec<u8> = text.iter().map(|&c| c as u8).collect();
    let one = |m: Option<Match>| m.map_or("-".to_string(), |m| show(&m, detail));
    let answer = match (method, b.bytes) {
        ("match", false) => re.r#match(text, pos, endpos).map(one),
        ("match", true) => re.r#match(bytes.as_slice(), pos, endpos).map(one),
        ("fullmatch", false) => re.fullmatch(text, pos, endpos).map(one),
        ("fullmatch", true) => re.fullmatch(bytes.as_slice(), pos, endpos).map(one),
        ("search", false) => re.search(text, pos, endpos).map(one),
        ("search", true) => re.search(bytes.as_slice(), pos, endpos).map(one),
        ("finditer", bytes_) => {
            let found = if bytes_ {
                re.finditer(bytes.as_slice(), pos, endpos)
            } else {
                re.finditer(text, pos, endpos)
            };
            found.map(|ms| {
                let shown: Vec<String> = ms.iter().map(|m| show(m, detail)).collect();
                format!("[{}]", shown.join(";"))
            })
        }
        ("findall", false) => re.findall(text, pos, endpos).map(|f| items(&f)),
        ("findall", true) => re.findall(bytes.as_slice(), pos, endpos).map(|f| items(&f)),
        (other, _) => panic!("a method: {other}"),
    };
    answer.unwrap_or_else(|e| panic!("{e}"))
}

/// `gen_pyre_exec._windows`.
pub fn windows(n: isize, short: isize) -> Vec<(isize, isize)> {
    if n <= short {
        (-1..n + 2)
            .flat_map(|p| (-1..n + 2).map(move |e| (p, e)))
            .collect()
    } else {
        vec![(0, n), (1, n), (0, n - 1), (1, n - 1)]
    }
}

/// `gen_pyre_exec._reversed`.
pub fn reversed(n: isize, pos: isize, endpos: isize) -> bool {
    pos.clamp(0, n) > endpos.clamp(0, n)
}

/// `gen_pyre_api._swap`: `None` for an empty match, else the groups from
/// the last to the first (empty where one took no part), or with no
/// group the match's text reversed.
pub fn swap<C: Copy>(text: &[C], m: &Match) -> Option<Vec<C>> {
    let (start, end) = m.regs[0];
    if start == end {
        return None;
    }
    let cut = |(a, b): (isize, isize)| text[a as usize..b as usize].to_vec();
    if m.regs.len() > 1 {
        Some(
            (1..m.regs.len())
                .rev()
                .flat_map(|g| m.group(g).map(cut).unwrap_or_default())
                .collect(),
        )
    } else {
        let mut whole = cut((start, end));
        whole.reverse();
        Some(whole)
    }
}

fn replaced_on<U>(
    re: &Regex,
    text: &[U],
    method: &str,
    repl: &[U],
    count: isize,
) -> Result<String, ReplaceError>
where
    U: Copy + From<u8> + PartialEq + Into<u32>,
    [U]: Subject<Piece = Vec<U>>,
{
    Ok(match method {
        "split" => {
            let items: Vec<String> = re
                .split(text, count)?
                .iter()
                .map(|x| x.as_ref().map_or("None".into(), |p| piece(p)))
                .collect();
            format!("[{}]", items.join(","))
        }
        "subn" => {
            let (out, n) = re.subn(repl, text, count)?;
            format!("{}|{n}", piece(&out))
        }
        "subn_fn" => {
            let (out, n) = re.subn_with(|m| swap(text, m), text, count)?;
            format!("{}|{n}", piece(&out))
        }
        other => panic!("a method: {other}"),
    })
}

/// One answer of `gen_pyre_api._replaced`.
pub fn replaced(
    b: &Built,
    text: &[u32],
    method: &str,
    repl: &[u32],
    count: isize,
) -> Result<String, ReplaceError> {
    if b.bytes {
        let t: Vec<u8> = text.iter().map(|&c| c as u8).collect();
        let r: Vec<u8> = repl.iter().map(|&c| c as u8).collect();
        replaced_on(&b.re, &t, method, &r, count)
    } else {
        replaced_on(&b.re, text, method, repl, count)
    }
}
