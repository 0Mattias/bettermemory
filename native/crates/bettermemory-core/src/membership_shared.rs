//! The pure rules of `membership_shared.py` (ENG-215): the re-mint read
//! from another machine's view of the same git directory, its stamp and
//! tick comparisons, and the predicates over a checkout's question rows
//! and log rows. Unit 1 of the module's cut.
//!
//! `self: Membership` is never read here. Where the reference calls
//! `_carriers` (one read of the checkouts' token index), the caller gives
//! a closure called with `sorted(set((k.token, found)))`, only where the
//! reference makes that read. `_asked_where` takes the proposal rows and
//! `_joined_silently_by_token` the log rows the store's reads return.
//!
//! A stamp's numbers are canonical decimal text (`capture::Stamp`), so
//! the reference's branch for a stamp that is not five long has no input
//! here. Text that is not canonical (the fields are public) is read as a
//! number that is not an `int`: `_same_file` and `_ticked` answer False. Equality of numbers is
//! equality of their text; arithmetic is in `i128` where every value fits
//! and over the decimal text, as Python's unbounded ints, where one does
//! not.

use std::collections::BTreeSet;

use crate::capture::location::Location;
use crate::capture::Stamp;
use crate::checkout_token::{self, Raised};
use crate::json;
use crate::location_stamps::in_ticks;
use crate::log::LogRow;
use crate::membership_records::records::{
    Checkout, Proposal, ANSWERED, DUPLICATE_CHECKOUT, FILE_CONTRADICTION, FIRST_SIGHT, IMPORT,
    LEGACY, SUBPROJECT_CONTRADICTION,
};
use crate::pyvalue::{py_str, PyError, PyStr, PyValue};
use crate::unicode;

/// `RESTAMPED_BY`: the witnesses after which a checkout's init stamps are
/// taken as its object reads them.
pub const RESTAMPED_BY: [&str; 3] = ["birth", "remint", "token"];
/// `_SECOND_NS`.
pub const SECOND_NS: i128 = 1_000_000_000;
/// `HEAL`, `ASK_WHICH`, `SETTLED`, `ASKED_HERE`.
pub const HEAL: &str = "heal";
pub const ASK_WHICH: &str = "ask";
pub const SETTLED: &str = "settled:";
pub const ASKED_HERE: &str = "sighting";
/// `_NAMING`: the log kinds `_joined_silently_by_token` reads.
pub const NAMING: [&str; 5] = [
    "checkout_registered",
    "project_absorbed",
    "project_file_bound",
    "resolution_joined",
    "resolution_new",
];
/// `_PICKING`, `_ABOUT_ELSE`.
pub const PICKING: [&str; 3] = [FIRST_SIGHT, FILE_CONTRADICTION, DUPLICATE_CHECKOUT];
pub const ABOUT_ELSE: [&str; 3] = [SUBPROJECT_CONTRADICTION, LEGACY, IMPORT];
/// `_SUBJECT`: the evidence key naming the project a question is about.
pub const SUBJECT: [(&str, &str); 2] =
    [(IMPORT, "imported_project"), (LEGACY, "unresolved_project")];

fn is(text: &[u32], w: &str) -> bool {
    text.iter().copied().eq(w.chars().map(u32::from))
}

fn opt_is(text: Option<&PyStr>, w: &[u32]) -> bool {
    text.is_some_and(|t| t.as_slice() == w)
}

// ---------------------------------------------------------------------------
// Integers as Python's: canonical decimal text.

/// Whether `x` is canonical decimal text: an optional `-`, then `0` or
/// digits without a leading zero, `-0` excluded. Text that is not reads
/// as the reference's number that is not an `int`: the rules answer
/// False for it, the same in every build.
fn canonical(x: &str) -> bool {
    let (neg, d) = parts(x);
    !d.is_empty()
        && d.bytes().all(|b| b.is_ascii_digit())
        && (d == "0" && !neg || !d.starts_with('0'))
}

/// The sign and digits of canonical decimal text.
fn parts(x: &str) -> (bool, &str) {
    match x.strip_prefix('-') {
        Some(d) => (true, d),
        None => (false, x),
    }
}

/// `x % 10**n == 0`: the last `n` digits are zeros (Python's floor modulo
/// is zero exactly where the remainder of the magnitude is).
fn divisible(x: &str, n: usize) -> bool {
    let (_, d) = parts(x);
    d == "0" || (d.len() > n && d.bytes().rev().take(n).all(|b| b == b'0'))
}

/// `|a - b|` of two magnitudes, `a >= b`, as digits without leading zeros.
fn sub_magnitude(a: &str, b: &str) -> String {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    let mut out = Vec::with_capacity(a.len());
    let mut borrow = 0i32;
    for i in 0..a.len() {
        let x = i32::from(a[a.len() - 1 - i] - b'0');
        let y = if i < b.len() {
            i32::from(b[b.len() - 1 - i] - b'0')
        } else {
            0
        };
        let mut d = x - y - borrow;
        borrow = i32::from(d < 0);
        if d < 0 {
            d += 10;
        }
        out.push(b'0' + d as u8);
    }
    while out.len() > 1 && out.last() == Some(&b'0') {
        out.pop();
    }
    out.reverse();
    String::from_utf8(out).unwrap_or_default()
}

fn magnitude_ge(a: &str, b: &str) -> bool {
    (a.len(), a) >= (b.len(), b)
}

/// `abs(a - b) < _SECOND_NS`, over unbounded integers.
fn under_a_second(a: &str, b: &str) -> bool {
    if let (Ok(x), Ok(y)) = (a.parse::<i128>(), b.parse::<i128>()) {
        if let Some(d) = x.checked_sub(y) {
            return d.unsigned_abs() < SECOND_NS.unsigned_abs();
        }
    }
    let ((na, ma), (nb, mb)) = (parts(a), parts(b));
    let gap = if na != nb {
        // |a| + |b|: under a second only where both are.
        if ma.len() > 9 || mb.len() > 9 {
            return false;
        }
        let sum = ma.parse::<u64>().unwrap_or(u64::MAX) + mb.parse::<u64>().unwrap_or(u64::MAX);
        return sum < 1_000_000_000;
    } else if magnitude_ge(ma, mb) {
        sub_magnitude(ma, mb)
    } else {
        sub_magnitude(mb, ma)
    };
    gap.len() <= 9
}

/// `in_ticks(times)` over decimal text: `location_stamps::in_ticks` where
/// every time fits `i128`, else the same rule read off the digits.
fn in_ticks_text(times: &[&str]) -> bool {
    let parsed: Option<Vec<i128>> = times.iter().map(|t| t.parse().ok()).collect();
    match parsed {
        Some(v) => in_ticks(&v),
        None => {
            !times.is_empty()
                && times.iter().all(|t| divisible(t, 2))
                && times.iter().any(|t| !divisible(t, 9))
        }
    }
}

// ---------------------------------------------------------------------------
// The stamp rules.

/// `_ticked(stamps)`: every mtime and ctime a whole tick, not every one a
/// whole second.
pub fn ticked(stamps: &[Stamp]) -> bool {
    let times: Vec<&str> = stamps
        .iter()
        .flat_map(|s| [s.numbers[1].as_str(), s.numbers[2].as_str()])
        .collect();
    times.iter().all(|t| canonical(t)) && in_ticks_text(&times)
}

/// `_in_ticks(k, loc)`: the sighting's word, else the record's, else
/// either side's stamps.
pub fn in_ticks_at(k: &Checkout, loc: &Location) -> bool {
    if let Some(t) = loc.ticks {
        return t;
    }
    if let Some(t) = k.ticks {
        return t;
    }
    ticked(&k.stamps) || ticked(&loc.stamps)
}

type Quad<'a> = (&'a [u32], &'a str, &'a str, &'a str);

/// `_loose(stamps)`: (name, inode, mtime, size).
pub fn loose(stamps: &[Stamp]) -> BTreeSet<Quad<'_>> {
    stamps
        .iter()
        .map(|s| {
            (
                s.name.as_slice(),
                s.numbers[0].as_str(),
                s.numbers[1].as_str(),
                s.numbers[3].as_str(),
            )
        })
        .collect()
}

/// `_exact(stamps)`: (name, mtime, ctime, size).
pub fn exact(stamps: &[Stamp]) -> BTreeSet<Quad<'_>> {
    stamps
        .iter()
        .map(|s| {
            (
                s.name.as_slice(),
                s.numbers[1].as_str(),
                s.numbers[2].as_str(),
                s.numbers[3].as_str(),
            )
        })
        .collect()
}

/// `_carried_in_ticks(c, k, loc)`.
pub fn carried_in_ticks(c: &Checkout, k: &Checkout, loc: &Location) -> bool {
    if let Some(t) = c.ticks {
        return t;
    }
    let mine = exact(&c.stamps);
    if mine
        .iter()
        .any(|s| exact(&k.stamps).contains(s) || exact(&loc.stamps).contains(s))
    {
        return false;
    }
    ticked(&c.stamps)
}

/// `_read_here(k, loc)`: a whole stamp of `k`'s read at the sighting.
pub fn read_here(k: &Checkout, loc: &Location) -> bool {
    k.stamps.iter().any(|s| loc.stamps.contains(s))
}

/// `_moved_under(k, loc)`: every stamp of `k`'s read here but its ctime.
pub fn moved_under(k: &Checkout, loc: &Location) -> bool {
    if k.stamps.is_empty() || loc.stamps.is_empty() {
        return false;
    }
    loose(&k.stamps).is_subset(&loose(&loc.stamps))
}

/// `_read_as_now(k, loc, found, carriers)`.
pub fn read_as_now(k: &Checkout, loc: &Location, found: &[u32], carriers: &[Checkout]) -> bool {
    let here = exact(&loc.stamps);
    carriers.iter().any(|other| {
        other.machine != k.machine
            && opt_is(other.token.as_ref(), found)
            && !here.is_empty()
            && here.is_subset(&exact(&other.stamps))
    })
}

/// `_same_file(a, b)`: one name and size, and the times equal or, where
/// either side keeps both to the whole second, within a second.
pub fn same_file(a: &Stamp, b: &Stamp) -> bool {
    if a.name != b.name || a.numbers[3] != b.numbers[3] {
        return false;
    }
    let (am, ac, bm, bc) = (&a.numbers[1], &a.numbers[2], &b.numbers[1], &b.numbers[2]);
    if ![am, ac, bm, bc].iter().all(|t| canonical(t)) {
        return false;
    }
    if am == bm && ac == bc {
        return true;
    }
    let coarse = (divisible(am, 9) && divisible(ac, 9)) || (divisible(bm, 9) && divisible(bc, 9));
    coarse && under_a_second(am, bm) && under_a_second(ac, bc)
}

/// `_same_files(mine, theirs)`.
pub fn same_files(mine: &[Stamp], theirs: &[Stamp]) -> bool {
    mine.iter().any(|a| theirs.iter().any(|b| same_file(a, b)))
}

fn token_value(t: Option<&PyStr>) -> PyValue {
    t.map_or(PyValue::None, |t| PyValue::Str(t.clone()))
}

/// `_made_by(k, found, carriers)`: a record of another machine that saw
/// the same files records `found` or names the lineage it re-mints.
/// `checkout_token.succeeds`'s exceptions pass through.
pub fn made_by(k: &Checkout, found: &[u32], carriers: &[Checkout]) -> Result<bool, Raised> {
    let found_value = PyValue::Str(found.to_vec());
    for other in carriers {
        let Some(key) = &other.object_key else {
            continue;
        };
        if other.machine == k.machine || !same_files(&k.stamps, &other.stamps) {
            continue;
        }
        if opt_is(other.token.as_ref(), found) {
            return Ok(true);
        }
        let pair = PyValue::Tuple(vec![
            PyValue::Str(key.device.clone()),
            PyValue::Str(key.inode.clone()),
        ]);
        let made = checkout_token::lineage(&token_value(k.token.as_ref()), &pair);
        if checkout_token::succeeds(&found_value, made.as_ref())? {
            return Ok(true);
        }
    }
    Ok(false)
}

/// `_continues(k, loc, found, carriers)`.
pub fn continues(
    k: &Checkout,
    loc: &Location,
    found: &[u32],
    carriers: &[Checkout],
) -> Result<bool, Raised> {
    if in_ticks_at(k, loc) {
        return Ok(false);
    }
    let kept: Vec<Checkout> = carriers
        .iter()
        .filter(|c| !carried_in_ticks(c, k, loc))
        .cloned()
        .collect();
    if read_here(k, loc) {
        return made_by(k, found, &kept);
    }
    Ok(moved_under(k, loc) && read_as_now(k, loc, found, &kept))
}

fn truthy_token(t: Option<&PyStr>) -> Option<&PyStr> {
    t.filter(|t| !t.is_empty())
}

/// `sorted(set((token, found)))`, the two known to differ.
fn asked(token: &PyStr, found: &[u32]) -> Vec<PyStr> {
    let mut v = vec![token.clone(), found.to_vec()];
    v.sort();
    v
}

/// `_shared_remint(self, k, loc, found)`, `carriers` standing for
/// `_carriers(self, ...)`.
pub fn shared_remint(
    k: &Checkout,
    loc: &Location,
    found: Option<&PyStr>,
    carriers: impl FnOnce(&[PyStr]) -> Vec<Checkout>,
) -> Result<bool, Raised> {
    let Some(token) = truthy_token(k.token.as_ref()) else {
        return Ok(false);
    };
    if k.stamps.is_empty() || !checkout_token::valid(&token_value(found)) {
        return Ok(false);
    }
    let Some(found) = found else { return Ok(false) };
    if found == token || !(read_here(k, loc) || moved_under(k, loc)) {
        return Ok(false);
    }
    let got = carriers(&asked(token, found));
    continues(k, loc, found, &got)
}

/// `_view_made(self, k, found)`, `carriers` standing for `_carriers`.
pub fn view_made(
    k: &Checkout,
    found: Option<&PyStr>,
    carriers: impl FnOnce(&[PyStr]) -> Vec<Checkout>,
) -> Result<bool, Raised> {
    let Some(token) = truthy_token(k.token.as_ref()) else {
        return Ok(false);
    };
    if !checkout_token::valid(&token_value(found)) {
        return Ok(false);
    }
    let Some(found) = found else { return Ok(false) };
    if found == token || k.stamps.is_empty() {
        return Ok(false);
    }
    let got = carriers(&asked(token, found));
    made_by(k, found, &got)
}

// ---------------------------------------------------------------------------
// Questions and log rows.

/// What reading a question row raises: a missing column (KeyError) or a
/// Python exception.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RowError {
    Key(&'static str),
    Py(PyError),
    AttributeError,
}

/// Python's truth value.
fn truthy(v: &PyValue) -> bool {
    match v {
        PyValue::None => false,
        PyValue::Bool(b) => *b,
        PyValue::Int(t) => t != "0",
        PyValue::Float(f) => *f != 0.0,
        PyValue::Str(s) | PyValue::Path(s) => !s.is_empty(),
        PyValue::DateTime(_) => true,
        PyValue::Tuple(v) | PyValue::List(v) => !v.is_empty(),
        PyValue::Dict(v) => !v.is_empty(),
    }
}

/// `str(v or "")`, a missing value as None. `str` of an int past 4,300
/// digits is ValueError.
fn str_or_empty(v: Option<&PyValue>) -> Result<PyStr, PyError> {
    let Some(v) = v.filter(|v| truthy(v)) else {
        return Ok(Vec::new());
    };
    if holds_long_int(v) {
        return Err(PyError::ValueError);
    }
    py_str(v)
}

/// Whether `v` is or holds, at any depth, an int past 4,300 digits: `str`
/// of a container writes each item's `repr`, which raises as `str` does.
fn holds_long_int(v: &PyValue) -> bool {
    match v {
        PyValue::Int(t) => parts(t).1.len() > 4300,
        PyValue::Tuple(items) | PyValue::List(items) => items.iter().any(holds_long_int),
        PyValue::Dict(items) => items
            .iter()
            .any(|(k, x)| holds_long_int(k) || holds_long_int(x)),
        _ => false,
    }
}

/// Whether `hash(v)` succeeds: no list or dict inside.
fn hashable(v: &PyValue) -> bool {
    match v {
        PyValue::List(_) | PyValue::Dict(_) => false,
        PyValue::Tuple(items) => items.iter().all(hashable),
        _ => true,
    }
}

/// `v in frozenset(words)`.
fn among(v: &PyValue, words: &[&str]) -> Result<bool, RowError> {
    if !hashable(v) {
        return Err(RowError::Py(PyError::TypeError));
    }
    Ok(matches!(v, PyValue::Str(s) if words.iter().any(|w| is(s, w))))
}

fn column<'a>(row: &'a PyValue, name: &'static str) -> Result<&'a PyValue, RowError> {
    match row {
        PyValue::Dict(_) => row.get(name).ok_or(RowError::Key(name)),
        _ => Err(RowError::Py(PyError::TypeError)),
    }
}

/// `_decides(answer)`.
pub fn decides(answer: &[u32]) -> bool {
    ["new", "join", "rebind"].iter().any(|w| is(answer, w))
        || answer.starts_with(&unicode::cps("join:"))
}

/// `_declared_answer(row)`.
pub fn declared_answer(row: &PyValue) -> Result<bool, RowError> {
    let by = str_or_empty(Some(column(row, "answered_by")?)).map_err(RowError::Py)?;
    Ok(by.starts_with(&unicode::cps("declaration:")))
}

/// `_asked_where(self, checkout)` over the rows of its questions, in the
/// order the store reads them.
pub fn asked_where(rows: &[PyValue]) -> Result<bool, RowError> {
    for row in rows {
        let kind = column(row, "kind")?;
        if among(kind, &PICKING)? {
            return Ok(true);
        }
        if !matches!(column(row, "state")?, PyValue::Str(s) if is(s, ANSWERED)) {
            continue;
        }
        if among(kind, &ABOUT_ELSE)? {
            continue;
        }
        let answer = str_or_empty(Some(column(row, "answer")?)).map_err(RowError::Py)?;
        if decides(&answer) && declared_answer(row)? {
            return Ok(true);
        }
    }
    Ok(false)
}

/// `_by_declaration(payload)`.
pub fn by_declaration(payload: &PyValue) -> Result<bool, PyError> {
    if payload.get("principal").is_some_and(truthy) {
        return Ok(true);
    }
    let evidence = str_or_empty(payload.get("evidence"))?;
    if evidence.starts_with(&unicode::cps("declaration:")) {
        return Ok(true);
    }
    Ok(match payload.get("witnesses") {
        Some(PyValue::List(items)) => items
            .iter()
            .any(|w| matches!(w, PyValue::Str(s) if is(s, "declaration"))),
        _ => false,
    })
}

/// `_joined_silently_by_token(self, checkout)` over the log rows
/// `membership_log(_NAMING, checkout=checkout)` returns, in order. A
/// payload JSON refuses with ValueError is passed over; RecursionError
/// propagates.
pub fn joined_silently_by_token(checkout: &[u32], rows: &[LogRow]) -> Result<bool, PyError> {
    let mut registered = false;
    for row in rows {
        let payload = match json::loads(&unicode::cps(&row.payload)) {
            Ok(p) => p,
            Err(PyError::ValueError) => continue,
            Err(e) => return Err(e),
        };
        if !matches!(&payload, PyValue::Dict(_)) {
            continue;
        }
        if !matches!(payload.get("checkout"), Some(PyValue::Str(s)) if s.as_slice() == checkout) {
            continue;
        }
        if by_declaration(&payload)? {
            return Ok(false);
        }
        if row.kind == "checkout_registered" {
            let rule = str_or_empty(payload.get("rule"))?;
            let head = unicode::cps("join:");
            registered = rule.starts_with(&head)
                && rule[head.len()..]
                    .split(|&c| c == u32::from('+'))
                    .any(|c| is(c, "T"));
        }
    }
    Ok(registered)
}

/// `_asks(pr)`: the kind and `str(evidence.get(subject) or "")`.
pub fn asks(pr: &Proposal) -> Result<(PyStr, PyStr), RowError> {
    let key = SUBJECT
        .iter()
        .find(|(k, _)| is(&pr.kind, k))
        .map_or("", |(_, v)| v);
    if !matches!(pr.evidence, PyValue::Dict(_)) {
        return Err(RowError::AttributeError);
    }
    Ok((
        pr.kind.clone(),
        str_or_empty(pr.evidence.get(key)).map_err(RowError::Py)?,
    ))
}
