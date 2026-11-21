//! The client's answer to a daemon's question and the liveness reads of
//! `location.py`: `path_liveness` (1188-1199), `path_liveness_many`
//! (1202-1211), `_liveness_read` (1214-1246), `answer` (1249-1336),
//! `Exchange` (1339-1352) and `_history_of` (1414-1417).
//!
//! The effects are arguments. The repository history is an
//! [`AnswerHistory`] a factory builds from the capture's common directory
//! (the reference's `RepositoryHistory(common_dir)`); the re-mint is the
//! closure `remint_answer` takes; a view is read by a [`ViewRead`], which
//! a caller builds over `location_view::view_state` with its machine and
//! effects, and which runs on the bounded-read board's worker threads.

use std::cell::RefCell;
use std::collections::{BTreeSet, HashMap};
use std::sync::Arc;

use crate::capture::location::ObjectKey;
use crate::capture::{str_of, truth, Capture, Key, State, STR_DEPTH_TOP};
use crate::githead::full_hash;
use crate::location_git::{walk, Reach};
use crate::location_reads::{bounded_read, bounded_reads, Board, Job, Read};
use crate::location_view::py_eq;
use crate::location_witness::{independent_answer, GitIndependent};
use crate::membership_records::records::{Checkout, View};
use crate::pyvalue::{lt_keys, PyError, PyStr, PyValue};
use crate::unicode::{self, cps};
use crate::witness_questions::{asked_views, remint_answer, AskedView, Remint};

/// `PROBE_BUDGET` (location_git.py 138): the seconds a bounded read of a
/// view's path waits.
pub const PROBE_BUDGET: f64 = 0.5;

/// What `answer` asks of `RepositoryHistory`: each method as the
/// reference calls it, commits as the text it hands in.
pub trait AnswerHistory {
    /// `holds_objects(ids)`: the ids it holds.
    fn holds_objects(&mut self, ids: &[PyStr]) -> BTreeSet<PyStr>;
    /// `fingerprint(commit)`.
    fn fingerprint(&mut self, commit: &[u32]) -> Option<PyStr>;
    /// `parents(commit)`.
    fn parents(&mut self, commit: &[u32]) -> Vec<PyStr>;
    /// `reachable_among(commits)`.
    fn reachable_among(&mut self, commits: &[PyStr]) -> Reach;
    /// `blobs(commit)`.
    fn blobs(&mut self, commit: &[u32]) -> Vec<PyStr>;
    /// `independent(of)`: the commits `merge-base --independent` names,
    /// or None where git did not answer.
    fn independent(&mut self, of: &[PyStr]) -> Option<Vec<String>>;
}

/// `view_state(path, **kw)` as a caller builds it: the state's text, or
/// the error the reference's read raises.
pub type ViewRead<E> = Arc<dyn Fn(&[u32], &AskedView) -> Result<PyStr, E> + Send + Sync>;

/// The bounded-read board the view reads go through.
pub type ViewBoard<E> = Board<String, PyStr, E>;

/// Why `answer` raised.
#[derive(Clone, Debug, PartialEq)]
pub enum AnswerError<E> {
    /// The answers or the question is no mapping (AttributeError).
    NotMapping,
    /// TypeError, ValueError and the like of the reference's own reads.
    Py(PyError),
    /// What a view's read raised.
    View(E),
}

impl<E> From<PyError> for AnswerError<E> {
    fn from(e: PyError) -> Self {
        AnswerError::Py(e)
    }
}

/// A Python `dict`: insertion order, a `str` key found by its text.
#[derive(Default)]
struct Dict {
    items: Vec<(PyValue, PyValue)>,
    strs: HashMap<PyStr, usize>,
}

impl Dict {
    fn from_items(items: &[(PyValue, PyValue)]) -> Dict {
        let mut d = Dict::default();
        for (k, v) in items {
            d.set(k.clone(), v.clone());
        }
        d
    }

    fn at(&self, k: &PyValue) -> Option<usize> {
        match k {
            PyValue::Str(t) => self.strs.get(t).copied(),
            _ => self.items.iter().position(|(x, _)| py_eq(x, k)),
        }
    }

    /// `d[k] = v`: a key already there keeps its place.
    fn set(&mut self, k: PyValue, v: PyValue) {
        match self.at(&k) {
            Some(i) => self.items[i].1 = v,
            None => {
                if let PyValue::Str(t) = &k {
                    self.strs.insert(t.clone(), self.items.len());
                }
                self.items.push((k, v));
            }
        }
    }

    fn put(&mut self, k: &str, v: PyValue) {
        self.set(PyValue::str(k), v);
    }

    fn get(&self, k: &str) -> Option<&PyValue> {
        self.strs.get(&cps(k)).map(|&i| &self.items[i].1)
    }

    fn value(self) -> PyValue {
        PyValue::Dict(self.items)
    }
}

fn unreachable_text() -> PyStr {
    cps("unreachable")
}

/// Text for the walk, a code point that is no scalar value as U+FFFD:
/// git's hex names hold none.
fn text(s: &[u32]) -> String {
    s.iter()
        .map(|&c| char::from_u32(c).unwrap_or('\u{fffd}'))
        .collect()
}

/// The board key of a path: the reference keys `_FLIGHTS` by the `str`
/// itself, so each code point is kept. A backslash is doubled and a code
/// point that is no scalar value (a lone surrogate from a surrogateescape
/// file name) is spelled `\x{..}`, so two paths share a key only when
/// they are equal.
fn board_key(path: &[u32]) -> String {
    let mut out = String::with_capacity(path.len());
    for &c in path {
        match char::from_u32(c) {
            Some('\\') => out.push_str("\\\\"),
            Some(ch) => out.push(ch),
            None => out.push_str(&format!("\\x{{{c:x}}}")),
        }
    }
    out
}

fn key_of(k: &Option<ObjectKey>) -> Option<Key> {
    k.as_ref()
        .map(|k| (k.device.clone(), k.inode.clone(), k.birth.clone()))
}

/// The keyword arguments `_liveness_read` hands `view_state`.
pub fn liveness_args(checkout: &Checkout) -> AskedView {
    AskedView {
        kind: checkout.kind.clone(),
        key: key_of(&checkout.object_key),
        token: checkout.token.clone(),
        last_head: checkout.last_head.clone(),
        stamps: checkout.stamps.clone(),
        common_key: key_of(&checkout.common_object_key),
        superseded: checkout.token_superseded(),
    }
}

/// One bounded read of `path` with `args`. `tag` keeps the reads of
/// `_liveness_read` and of `answer` apart on the board, as their `what`
/// tuples differ in the reference.
fn job<E>(read: &ViewRead<E>, tag: &str, path: &[u32], args: AskedView) -> Job<String, PyStr, E>
where
    E: Clone + Send + 'static,
{
    let what = format!("{tag}{args:?}");
    let (r, p) = (Arc::clone(read), path.to_vec());
    (board_key(path), what, Box::new(move || r(&p, &args)))
}

/// `_liveness_read(checkout, view)`: the view's path, what the read
/// depends on, and the read.
fn liveness_read<E>(read: &ViewRead<E>, checkout: &Checkout, view: &View) -> Job<String, PyStr, E>
where
    E: Clone + Send + 'static,
{
    job(read, "liveness", &view.path, liveness_args(checkout))
}

/// `path_liveness(checkout, view)`: the view's state, ``unreachable``
/// where the read did not answer within [`PROBE_BUDGET`]; what the read
/// raised passed on.
pub fn path_liveness<E>(
    board: &ViewBoard<E>,
    read: &ViewRead<E>,
    checkout: &Checkout,
    view: &View,
) -> Result<PyStr, E>
where
    E: Clone + Send + 'static,
{
    let (path, what, f) = liveness_read(read, checkout, view);
    Ok(match bounded_read(board, &path, what, f, PROBE_BUDGET)? {
        Read::Value(s) => s,
        Read::Unanswered | Read::NotRead => unreachable_text(),
    })
}

/// `path_liveness_many(pairs)`: each pair's state, ``unreachable`` where
/// the read did not answer, None where it raised.
pub fn path_liveness_many<E>(
    board: &ViewBoard<E>,
    read: &ViewRead<E>,
    pairs: &[(Checkout, View)],
) -> Vec<Option<PyStr>>
where
    E: Clone + Send + 'static,
{
    let jobs = pairs
        .iter()
        .map(|(k, v)| liveness_read(read, k, v))
        .collect();
    match bounded_reads(board, jobs, PROBE_BUDGET, false) {
        Ok(got) => got
            .into_iter()
            .map(|g| match g {
                Read::Value(s) => Some(s),
                Read::Unanswered => Some(unreachable_text()),
                Read::NotRead => None,
            })
            .collect(),
        // Without `raises` no error is passed on.
        Err(_) => vec![None; pairs.len()],
    }
}

/// `_history_of(cap)`: the history of a repository capture that was not
/// evicted and names its common directory.
pub fn history_of<H>(cap: &Capture, factory: &mut dyn FnMut(&[u32]) -> H) -> Option<H> {
    match &cap.common_dir {
        Some(c) if cap.state == State::Repo && !cap.evicted && !c.is_empty() => Some(factory(c)),
        _ => None,
    }
}

/// `Exchange`: the daemon's reply to a hook client it cannot read for.
#[derive(Clone, Debug, PartialEq)]
pub struct Exchange {
    pub question: PyValue,
}

impl Default for Exchange {
    fn default() -> Self {
        Exchange {
            question: PyValue::Dict(Vec::new()),
        }
    }
}

impl Exchange {
    /// `to_reply()`: `dict(self.question)`.
    pub fn to_reply(&self) -> Result<PyValue, PyError> {
        Ok(dict_from(&self.question)?.value())
    }
}

/// `iter(v)` as a list: a string's code points, a dict's keys.
fn iterate(v: &PyValue) -> Result<Vec<PyValue>, PyError> {
    match v {
        PyValue::Str(t) => Ok(t.iter().map(|c| PyValue::Str(vec![*c])).collect()),
        PyValue::List(i) | PyValue::Tuple(i) => Ok(i.clone()),
        PyValue::Dict(i) => Ok(i.iter().map(|(k, _)| k.clone()).collect()),
        _ => Err(PyError::TypeError),
    }
}

/// `v or ()`, iterated.
fn items_or_none(v: Option<&PyValue>) -> Result<Vec<PyValue>, PyError> {
    if truth(v) {
        iterate(v.unwrap_or(&PyValue::None))
    } else {
        Ok(Vec::new())
    }
}

/// `str(v).lower()`.
fn low(v: &PyValue) -> Result<PyStr, PyError> {
    Ok(unicode::lower(&str_of(v, STR_DEPTH_TOP)?))
}

fn lowered(v: Option<&PyValue>) -> Result<Vec<PyStr>, PyError> {
    items_or_none(v)?.iter().map(low).collect()
}

fn hashable(v: &PyValue) -> Result<(), PyError> {
    match v {
        PyValue::List(_) | PyValue::Dict(_) => Err(PyError::TypeError),
        PyValue::Tuple(items) => items.iter().try_for_each(hashable),
        _ => Ok(()),
    }
}

/// `dict(v)`: a mapping copied, or a sequence of pairs.
fn dict_from(v: &PyValue) -> Result<Dict, PyError> {
    if let PyValue::Dict(items) = v {
        return Ok(Dict::from_items(items));
    }
    let mut d = Dict::default();
    for e in iterate(v)? {
        let pair = iterate(&e)?;
        if pair.len() != 2 {
            return Err(PyError::ValueError);
        }
        hashable(&pair[0])?;
        d.set(pair[0].clone(), pair[1].clone());
    }
    Ok(d)
}

/// `dict(v or {})`.
fn dict_of(v: Option<&PyValue>) -> Result<Dict, PyError> {
    match v {
        Some(v) if truth(Some(v)) => dict_from(v),
        _ => Ok(Dict::default()),
    }
}

/// `set(items)`, its hashability checked as it is built.
fn set_of(items: Vec<PyValue>) -> Result<Vec<PyValue>, PyError> {
    items.iter().try_for_each(hashable)?;
    Ok(items)
}

/// `sorted(s)` of a set: once each, in Python's order; TypeError where
/// two members do not compare.
fn sorted_set(items: Vec<PyValue>) -> Result<PyValue, PyError> {
    let strs: Option<BTreeSet<PyStr>> = items
        .iter()
        .map(|x| match x {
            PyValue::Str(s) => Some(s.clone()),
            _ => None,
        })
        .collect();
    if let Some(strs) = strs {
        return Ok(PyValue::List(strs.into_iter().map(PyValue::Str).collect()));
    }
    // `None` is equal only to itself, and the set holds it once.
    let mut none = false;
    let mut once: Vec<PyValue> = items
        .into_iter()
        .filter(|x| !matches!(x, PyValue::None) || !std::mem::replace(&mut none, true))
        .collect();
    if once.len() > 1 && once.iter().any(|x| lt_keys(&once[0], x).is_none()) {
        return Err(PyError::TypeError);
    }
    once.sort_by(|a, b| {
        if lt_keys(a, b) == Some(true) {
            std::cmp::Ordering::Less
        } else if lt_keys(b, a) == Some(true) {
            std::cmp::Ordering::Greater
        } else {
            std::cmp::Ordering::Equal
        }
    });
    once.dedup_by(|later, first| py_eq(first, later));
    Ok(PyValue::List(once))
}

fn strs(items: impl IntoIterator<Item = PyStr>) -> PyValue {
    PyValue::List(items.into_iter().map(PyValue::Str).collect())
}

/// `answer(question, answers, cap)`: the client's answers to a daemon's
/// question added to the answers of earlier rounds.
#[allow(clippy::too_many_arguments)]
pub fn answer<H, E>(
    question: &PyValue,
    answers: &PyValue,
    cap: &Capture,
    history_of: &mut dyn FnMut(&[u32]) -> H,
    remint: &mut Remint<'_>,
    board: &ViewBoard<E>,
    read: &ViewRead<E>,
) -> Result<PyValue, AnswerError<E>>
where
    H: AnswerHistory,
    E: Clone + Send + 'static,
{
    let PyValue::Dict(answers) = answers else {
        return Err(AnswerError::NotMapping);
    };
    if !matches!(question, PyValue::Dict(_)) {
        return Err(AnswerError::NotMapping);
    }
    let mut out = Dict::from_items(answers);
    out.put("remint_ok", PyValue::Bool(true));
    out.put(
        "exchange",
        question.get("exchange").cloned().unwrap_or(PyValue::None),
    );
    if truth(question.get("remint")) && out.get("remint").is_none() {
        let got = remint_answer(cap, question, remint)?;
        out.put("remint", got);
    }
    let history = match &cap.common_dir {
        Some(c) if !c.is_empty() && cap.state == State::Repo => Some(RefCell::new(history_of(c))),
        _ => None,
    };
    let mut independent = |of: &[PyStr]| history.as_ref()?.borrow_mut().independent(of);
    let git: Option<GitIndependent<'_>> = if history.is_some() {
        Some(&mut independent)
    } else {
        None
    };
    let added = independent_answer(question, &PyValue::Dict(out.items.clone()), git)?;
    if let PyValue::Dict(added) = &added {
        for (k, v) in added {
            out.set(k.clone(), v.clone());
        }
    }
    let mut asked = set_of(items_or_none(out.get("asked"))?)?;
    let mut held = set_of(items_or_none(out.get("held"))?)?;
    let wanted = lowered(question.get("held"))?;
    if !wanted.is_empty() {
        let got = match &history {
            Some(h) => h.borrow_mut().holds_objects(&wanted),
            None => BTreeSet::new(),
        };
        asked.extend(wanted.into_iter().map(PyValue::Str));
        held.extend(got.into_iter().map(PyValue::Str));
    }
    out.put("asked", sorted_set(asked)?);
    out.put("held", sorted_set(held)?);
    if let Some(h) = &history {
        if out.get("walk").is_none() || truth(question.get("walk")) {
            let mut walked = dict_of(out.get("walk"))?;
            let head = cap.head.as_deref().map(text);
            let tips: Vec<String> = cap.ref_tips.iter().map(|t| text(t)).collect();
            let got = walk(
                &mut |s| {
                    h.borrow_mut()
                        .parents(&cps(s))
                        .iter()
                        .map(|p| text(p))
                        .collect()
                },
                &mut |s| h.borrow_mut().fingerprint(&cps(s)).map(|f| text(&f)),
                head.as_deref(),
                &tips,
            );
            for (sha, (fp, parents)) in got {
                let fp = fp.map_or(PyValue::None, |f| PyValue::str(&f));
                let parents = strs(parents.iter().map(|p| cps(p)));
                walked.set(PyValue::str(&sha), PyValue::List(vec![fp, parents]));
            }
            for sha in items_or_none(question.get("commits"))? {
                let s = low(&sha)?;
                let fp = h.borrow_mut().fingerprint(&s);
                let parents = h.borrow_mut().parents(&s);
                let fp = fp.map_or(PyValue::None, PyValue::Str);
                walked.set(PyValue::Str(s), PyValue::List(vec![fp, strs(parents)]));
            }
            out.put("walk", walked.value());
        }
        let among = lowered(question.get("ancestry"))?;
        if !among.is_empty() {
            let reach = h.borrow_mut().reachable_among(&among);
            let keys: BTreeSet<PyStr> = reach.relations.iter().map(|(k, _)| cps(k)).collect();
            let pairs: BTreeSet<(PyStr, PyStr)> = reach
                .relations
                .iter()
                .flat_map(|(a, ds)| ds.iter().map(move |d| (cps(a), cps(d))))
                .collect();
            out.put("ancestry_of", strs(keys));
            let pairs = pairs.into_iter().map(|(a, d)| strs([a, d])).collect();
            out.put("ancestry", PyValue::List(pairs));
            out.put("ancestry_bounded", PyValue::Bool(reach.bounded));
        }
        let mut blobs = dict_of(out.get("blobs"))?;
        let mut shas = vec![cap.head.clone().map_or(PyValue::None, PyValue::Str)];
        shas.extend(items_or_none(question.get("blobs"))?);
        for sha in shas {
            if truth(Some(&sha)) {
                let s = low(&sha)?;
                let got = h.borrow_mut().blobs(&s);
                blobs.set(PyValue::Str(s), strs(got));
            }
        }
        out.put("blobs", blobs.value());
    } else {
        if out.get("walk").is_none() {
            out.put("walk", PyValue::Dict(Vec::new()));
        }
        let asked: BTreeSet<PyStr> = lowered(question.get("ancestry"))?.into_iter().collect();
        if !asked.is_empty() {
            let full = asked.into_iter().filter(|s| {
                s.iter().all(|&c| c < 0x80)
                    && full_hash(&s.iter().map(|&c| c as u8).collect::<Vec<u8>>())
            });
            out.put("ancestry_of", strs(full));
            out.put("ancestry", PyValue::List(Vec::new()));
            out.put("ancestry_bounded", PyValue::Bool(false));
        }
    }
    let mut views = dict_of(out.get("views"))?;
    let asked = asked_views(question)?;
    let jobs = asked
        .iter()
        .map(|(p, v)| job(read, "view", p, v.clone()))
        .collect();
    let states = bounded_reads(board, jobs, PROBE_BUDGET, true).map_err(AnswerError::View)?;
    for ((path, _), state) in asked.into_iter().zip(states) {
        let state = match state {
            Read::Value(s) => s,
            Read::Unanswered | Read::NotRead => unreachable_text(),
        };
        views.set(PyValue::Str(path), PyValue::Str(state));
    }
    out.put("views", views.value());
    Ok(out.value())
}
