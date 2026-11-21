//! The history half of `location_witness.py`: the question the daemon
//! raises (`NeedWitnesses`, 52-93), the readers that answer the membership
//! model from the client's answers (`AnsweredHistory` 95-179,
//! `AnsweredGit` 181-224, `answered_history` 227-239), the client's
//! answers to git's reading of tied witnesses (`_independent_answers`
//! 242-258, `independent_answer` 261-286), and the keeping of one
//! exchange's reads between rounds (`_NONCE`, `EXCHANGE_SECONDS`,
//! `_EXCHANGES_CAP`, `_Kept`, `_EXCHANGES`, `_drop`, `ExchangeReads`,
//! 355-462).
//!
//! The answers are the decoded JSON the client posted (`PyValue`). Each
//! `str(x).lower()` is `capture::str_of` then `unicode::lower`; a failure
//! of `str()` (a value nested past [`STR_DEPTH_TOP`]) or of the
//! reference's iteration and unpacking ends construction with the class
//! the reference raises. The module globals `_EXCHANGES` and its lock are
//! [`Exchanges`], which the caller owns behind its own lock; the clock
//! (`time.monotonic`) is a `now` argument and `secrets.token_hex(16)` is
//! sixteen bytes passed in. `INDEPENDENT_MAX` is an argument (`bound`).

use std::collections::{BTreeMap, BTreeSet, HashMap};

use crate::capture::{str_of, STR_DEPTH_TOP};
use crate::githead::full_hash;
use crate::location_git::Reach;
use crate::pyvalue::{repr, PyError, PyStr, PyValue};
use crate::unicode;

/// How long an exchange's reads are kept for the client's next round.
pub const EXCHANGE_SECONDS: f64 = 8.0;
/// The most exchanges kept; the oldest is dropped first past it.
pub const EXCHANGES_CAP: usize = 256;

/// `NeedWitnesses`: the question the client is to answer (a dict).
#[derive(Clone, Debug, PartialEq)]
pub struct NeedWitnesses {
    pub question: PyValue,
}

fn s(text: &str) -> PyValue {
    PyValue::str(text)
}

fn list_of<'a>(items: impl IntoIterator<Item = &'a PyStr>) -> PyValue {
    PyValue::List(items.into_iter().map(|i| PyValue::Str(i.clone())).collect())
}

fn need(pairs: Vec<(&str, PyValue)>) -> NeedWitnesses {
    NeedWitnesses {
        question: PyValue::Dict(pairs.into_iter().map(|(k, v)| (s(k), v)).collect()),
    }
}

fn dict_items(v: &PyValue) -> Result<&[(PyValue, PyValue)], PyError> {
    match v {
        PyValue::Dict(items) => Ok(items),
        _ => Err(PyError::TypeError),
    }
}

fn has_key(v: &PyValue, key: &str) -> bool {
    v.get(key).is_some()
}

/// Python's `==` over decoded values: numbers by value (`True == 1 ==
/// 1.0`), lists with lists and tuples with tuples item by item, dicts by
/// their keys whatever the order. NaN equals nothing (identity is not
/// modelled).
pub fn py_eq(a: &PyValue, b: &PyValue) -> bool {
    let mut work = vec![(a, b)];
    while let Some((a, b)) = work.pop() {
        use PyValue::*;
        let same = match (a, b) {
            (None, None) => true,
            (Str(x), Str(y)) | (Path(x), Path(y)) => x == y,
            (DateTime(x), DateTime(y)) => x == y,
            (List(x), List(y)) | (Tuple(x), Tuple(y)) => {
                if x.len() != y.len() {
                    return false;
                }
                work.extend(x.iter().zip(y.iter()));
                true
            }
            (Dict(x), Dict(y)) => {
                if x.len() != y.len() {
                    return false;
                }
                for (k, v) in x {
                    match y.iter().rev().find(|(ky, _)| py_eq(k, ky)) {
                        Some((_, vy)) => work.push((v, vy)),
                        Option::None => return false,
                    }
                }
                true
            }
            _ => match (number(a), number(b)) {
                (Some(x), Some(y)) => numbers_equal(&x, &y),
                _ => false,
            },
        };
        if !same {
            return false;
        }
    }
    true
}

enum Num {
    Int(String),
    Float(f64),
}

fn number(v: &PyValue) -> Option<Num> {
    match v {
        PyValue::Bool(b) => Some(Num::Int(if *b { "1" } else { "0" }.into())),
        PyValue::Int(t) => Some(Num::Int(t.clone())),
        PyValue::Float(f) => Some(Num::Float(*f)),
        _ => None,
    }
}

fn numbers_equal(a: &Num, b: &Num) -> bool {
    match (a, b) {
        (Num::Int(x), Num::Int(y)) => x == y,
        (Num::Float(x), Num::Float(y)) => x == y,
        (Num::Int(i), Num::Float(f)) | (Num::Float(f), Num::Int(i)) => {
            if !f.is_finite() || f.fract() != 0.0 {
                return false;
            }
            let text = if *f == 0.0 {
                "0".to_owned()
            } else {
                format!("{f:.0}")
            };
            *i == text
        }
    }
}

/// The position of a dict key under Python's `==` in a growing list of
/// keys: a string key by hash (a string equals only a string), any other
/// by a scan of the keys that are no string.
#[derive(Default)]
struct KeyIndex {
    strings: HashMap<PyStr, usize>,
    others: Vec<(PyValue, usize)>,
}

impl KeyIndex {
    fn find(&self, key: &PyValue) -> Option<usize> {
        match key {
            PyValue::Str(t) => self.strings.get(t).copied(),
            _ => self
                .others
                .iter()
                .find(|(k, _)| py_eq(k, key))
                .map(|(_, at)| *at),
        }
    }

    fn add(&mut self, key: &PyValue, at: usize) {
        match key {
            PyValue::Str(t) => {
                self.strings.insert(t.clone(), at);
            }
            _ => self.others.push((key.clone(), at)),
        }
    }
}

impl NeedWitnesses {
    /// `joined`: one question asking all that each of `needs` asks. A list
    /// a key names holds every item any of them names once, in the order
    /// first named (a dict with a `path` by `repr` of its path, the first
    /// kept; a dict without one by `==` with what is held; any other item
    /// by its `repr`); any other value is the first named.
    pub fn joined(needs: &[NeedWitnesses]) -> Result<NeedWitnesses, PyError> {
        let mut question: Vec<(PyValue, PyValue)> = Vec::new();
        let mut question_at = KeyIndex::default();
        let mut seen: Vec<BTreeSet<(bool, PyStr)>> = Vec::new();
        let mut seen_at = KeyIndex::default();
        for n in needs {
            for (name, value) in dict_items(&n.question)? {
                let at = match question_at.find(name) {
                    Some(at) => at,
                    None => {
                        let first = match value {
                            PyValue::List(_) => PyValue::List(Vec::new()),
                            other => other.clone(),
                        };
                        question.push((name.clone(), first));
                        question_at.add(name, question.len() - 1);
                        question.len() - 1
                    }
                };
                let (PyValue::List(items), PyValue::List(held)) = (value, &mut question[at].1)
                else {
                    continue;
                };
                let m = match seen_at.find(name) {
                    Some(m) => m,
                    None => {
                        seen.push(BTreeSet::new());
                        seen_at.add(name, seen.len() - 1);
                        seen.len() - 1
                    }
                };
                let marks = &mut seen[m];
                for item in items {
                    let mark = match item {
                        PyValue::Dict(pairs) => {
                            match pairs.iter().rev().find(|(k, _)| *k == s("path")) {
                                Some((_, path)) => (true, repr(path)?),
                                None => {
                                    if !held.iter().any(|h| py_eq(item, h)) {
                                        held.push(item.clone());
                                    }
                                    continue;
                                }
                            }
                        }
                        _ => (false, repr(item)?),
                    };
                    if marks.insert(mark) {
                        held.push(item.clone());
                    }
                }
            }
        }
        Ok(NeedWitnesses {
            question: PyValue::Dict(question),
        })
    }
}

fn truthy(v: &PyValue) -> bool {
    match v {
        PyValue::None | PyValue::Bool(false) => false,
        PyValue::Int(t) => t != "0",
        PyValue::Float(f) => *f != 0.0,
        PyValue::Str(t) | PyValue::Path(t) => !t.is_empty(),
        PyValue::List(i) | PyValue::Tuple(i) => !i.is_empty(),
        PyValue::Dict(i) => !i.is_empty(),
        _ => true,
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
    match v {
        Some(v) if truthy(v) => iterate(v),
        _ => Ok(Vec::new()),
    }
}

/// `str(v).lower()`.
fn low(v: &PyValue) -> Result<PyStr, PyError> {
    Ok(unicode::lower(&str_of(v, STR_DEPTH_TOP)?))
}

fn lowered_set(v: Option<&PyValue>) -> Result<BTreeSet<PyStr>, PyError> {
    items_or_none(v)?.iter().map(low).collect()
}

fn lowered_list(v: Option<&PyValue>) -> Result<Vec<PyStr>, PyError> {
    items_or_none(v)?.iter().map(low).collect()
}

/// The lowered full hashes of `commits` (`_FULL_HASH.fullmatch` of each
/// `c.lower()`), sorted, once each.
fn full_hashes(commits: &[PyStr]) -> BTreeSet<String> {
    commits
        .iter()
        .filter_map(|c| {
            let l = unicode::lower(c);
            unicode::to_string(&l).filter(|t| full_hash(t.as_bytes()))
        })
        .collect()
}

fn to_cps(t: &str) -> PyStr {
    unicode::cps(t)
}

/// A walk entry: the fingerprint (the pair's first item where it is a
/// string, as given) and the lowered parents.
pub type WalkEntry = (Option<PyStr>, Vec<PyStr>);

/// `AnsweredHistory`: the history reader over one round's answers.
#[derive(Clone, Debug)]
pub struct AnsweredHistory {
    asked: BTreeSet<PyStr>,
    held: BTreeSet<PyStr>,
    walked: bool,
    walk: BTreeMap<PyStr, WalkEntry>,
    ancestry_of: BTreeSet<PyStr>,
    ancestry: BTreeSet<(PyStr, PyStr)>,
    ancestry_bounded: bool,
    blobs: BTreeMap<PyStr, Vec<PyStr>>,
}

impl AnsweredHistory {
    /// `AnsweredHistory(answers)`, in the reference's order of reading;
    /// a class the reference raises (TypeError for a value it cannot
    /// iterate, ValueError for an `ancestry` item not of two, the class
    /// `str()` raises) is returned.
    pub fn new(answers: &PyValue) -> Result<Self, PyError> {
        let asked = lowered_set(answers.get("asked"))?;
        let held = lowered_set(answers.get("held"))?;
        let raw_walk = answers.get("walk");
        let walked = matches!(raw_walk, Some(PyValue::Dict(_)));
        let mut walk = BTreeMap::new();
        if let Some(PyValue::Dict(pairs)) = raw_walk {
            for (sha, pair) in pairs {
                let (PyValue::List(two) | PyValue::Tuple(two)) = pair else {
                    continue;
                };
                if two.len() != 2 {
                    continue;
                }
                let fp = match &two[0] {
                    PyValue::Str(t) => Some(t.clone()),
                    _ => None,
                };
                let parents = lowered_list(Some(&two[1]))?;
                walk.insert(low(sha)?, (fp, parents));
            }
        }
        let ancestry_of = lowered_set(answers.get("ancestry_of"))?;
        let mut ancestry = BTreeSet::new();
        for item in items_or_none(answers.get("ancestry"))? {
            let two = iterate(&item)?;
            if two.len() != 2 {
                return Err(PyError::ValueError);
            }
            ancestry.insert((low(&two[0])?, low(&two[1])?));
        }
        let ancestry_bounded =
            !matches!(answers.get("ancestry_bounded"), Some(PyValue::Bool(false)));
        let mut blobs = BTreeMap::new();
        if let Some(PyValue::Dict(pairs)) = answers.get("blobs") {
            for (sha, ids) in pairs {
                let ids = lowered_list(Some(ids))?;
                blobs.insert(low(sha)?, ids);
            }
        }
        Ok(AnsweredHistory {
            asked,
            held,
            walked,
            walk,
            ancestry_of,
            ancestry,
            ancestry_bounded,
            blobs,
        })
    }

    /// `_walked`: whether the answers held a `walk` mapping.
    pub fn walked(&self) -> bool {
        self.walked
    }

    fn ids(&self, ids: &[PyStr]) -> Result<BTreeSet<PyStr>, NeedWitnesses> {
        let wanted: BTreeSet<PyStr> = ids.iter().map(|i| unicode::lower(i)).collect();
        let missing: Vec<&PyStr> = wanted.difference(&self.asked).collect();
        if !missing.is_empty() {
            return Err(need(vec![("held", list_of(missing))]));
        }
        Ok(wanted.intersection(&self.held).cloned().collect())
    }

    pub fn holds(&self, commits: &[PyStr]) -> Result<BTreeSet<PyStr>, NeedWitnesses> {
        self.ids(commits)
    }

    pub fn holds_objects(&self, ids: &[PyStr]) -> Result<BTreeSet<PyStr>, NeedWitnesses> {
        self.ids(ids)
    }

    fn walk_entry(&self, commit: &[u32]) -> Result<&WalkEntry, NeedWitnesses> {
        let sha = unicode::lower(commit);
        match self.walk.get(&sha) {
            Some(hit) => Ok(hit),
            None => Err(need(vec![
                ("walk", PyValue::Bool(true)),
                ("commits", list_of([&sha])),
            ])),
        }
    }

    pub fn fingerprint(&self, commit: &[u32]) -> Result<Option<PyStr>, NeedWitnesses> {
        Ok(self.walk_entry(commit)?.0.clone())
    }

    pub fn parents(&self, commit: &[u32]) -> Result<Vec<PyStr>, NeedWitnesses> {
        Ok(self.walk_entry(commit)?.1.clone())
    }

    /// `reachable_among`: each asked full hash with the asked ones it is
    /// reachable from, sorted (the reference's dict and sets carry no
    /// order); `handled` is 0.
    pub fn reachable_among(&self, commits: &[PyStr]) -> Result<Reach, NeedWitnesses> {
        let wanted = full_hashes(commits);
        let wanted_cps: BTreeSet<PyStr> = wanted.iter().map(|w| to_cps(w)).collect();
        if !wanted_cps.is_subset(&self.ancestry_of) {
            let all: BTreeSet<&PyStr> = self.ancestry_of.iter().chain(wanted_cps.iter()).collect();
            return Err(need(vec![("ancestry", list_of(all))]));
        }
        let relations = wanted
            .iter()
            .map(|c| {
                let cc = to_cps(c);
                let from = self
                    .ancestry
                    .iter()
                    .filter(|(a, d)| *a == cc && wanted_cps.contains(d))
                    .filter_map(|(_, d)| unicode::to_string(d))
                    .collect::<BTreeSet<String>>();
                (c.clone(), from.into_iter().collect())
            })
            .collect();
        Ok(Reach {
            relations,
            bounded: self.ancestry_bounded,
            handled: 0,
        })
    }

    pub fn blobs(&self, commit: &[u32]) -> Result<Vec<PyStr>, NeedWitnesses> {
        let sha = unicode::lower(commit);
        match self.blobs.get(&sha) {
            Some(hit) => Ok(hit.clone()),
            None => Err(need(vec![("blobs", list_of([&sha]))])),
        }
    }
}

/// The client's answers by the commits asked: each its sorted named
/// tops, or None (no answer); a later entry for the same set replaces
/// an earlier one.
pub type IndependentAnswers = Vec<(Vec<PyStr>, Option<Vec<PyStr>>)>;

/// `_independent_answers`: the well-formed entries of `raw` (a list of
/// dicts whose `of` is a list), by the sorted lowered set asked; a `top`
/// that is no list, names nothing, or names one not asked is None.
pub fn independent_answers(raw: Option<&PyValue>) -> Result<IndependentAnswers, PyError> {
    let mut out: IndependentAnswers = Vec::new();
    let mut at: HashMap<Vec<PyStr>, usize> = HashMap::new();
    let Some(PyValue::List(entries)) = raw else {
        return Ok(out);
    };
    for entry in entries {
        let (PyValue::Dict(_), Some(PyValue::List(of))) = (entry, entry.get("of")) else {
            continue;
        };
        let of: BTreeSet<PyStr> = of.iter().map(low).collect::<Result<_, _>>()?;
        let named = match entry.get("top") {
            Some(PyValue::List(top)) => Some(
                top.iter()
                    .map(low)
                    .collect::<Result<BTreeSet<PyStr>, _>>()?,
            ),
            _ => None,
        };
        let value = named
            .filter(|n| !n.is_empty() && n.is_subset(&of))
            .map(|n| n.into_iter().collect());
        let key: Vec<PyStr> = of.into_iter().collect();
        match at.get(&key) {
            Some(&i) => out[i].1 = value,
            None => {
                at.insert(key.clone(), out.len());
                out.push((key, value));
            }
        }
    }
    Ok(out)
}

/// The sets of tied witnesses asked of the client's git in one exchange.
pub type Asked = BTreeSet<Vec<String>>;

/// What `AnsweredGit::independent` answers: the tops, None (the
/// traversal decides), or the question.
pub type Independent = Result<Option<Vec<String>>, NeedWitnesses>;

/// `AnsweredGit`: the `AnsweredHistory` of a client that answers git's
/// reading of tied witnesses; `bound` is `INDEPENDENT_MAX`.
#[derive(Clone, Debug)]
pub struct AnsweredGit {
    pub history: AnsweredHistory,
    independent: IndependentAnswers,
    bound: usize,
}

impl AnsweredGit {
    pub fn new(answers: &PyValue, bound: usize) -> Result<Self, PyError> {
        let history = AnsweredHistory::new(answers)?;
        let independent = independent_answers(answers.get("independent"))?;
        Ok(AnsweredGit {
            history,
            independent,
            bound,
        })
    }

    /// `independent`: the asked full hashes no other reaches, as the client
    /// answered; `asked` is the exchange's (`ExchangeReads.asked`), None
    /// for a reader built without one. A set not answered is asked (one
    /// an exchange), sorted.
    pub fn independent(&self, commits: &[PyStr], asked: Option<&mut Asked>) -> Independent {
        let given: Vec<String> = full_hashes(commits).into_iter().collect();
        if given.len() < 2 {
            return Ok(Some(given));
        }
        if given.len() > self.bound {
            return Ok(None);
        }
        let key: Vec<PyStr> = given.iter().map(|g| to_cps(g)).collect();
        if let Some((_, top)) = self.independent.iter().find(|(k, _)| *k == key) {
            return Ok(top
                .as_ref()
                .map(|t| t.iter().filter_map(|c| unicode::to_string(c)).collect()));
        }
        if let Some(asked) = asked {
            if !asked.is_empty() {
                return Ok(None);
            }
            asked.insert(given.clone());
        }
        Err(need(vec![("independent", list_of(key.iter()))]))
    }
}

/// What `answered_history` returns.
#[derive(Clone, Debug)]
pub enum History {
    Plain(AnsweredHistory),
    Git(AnsweredGit),
}

impl History {
    pub fn reader(&self) -> &AnsweredHistory {
        match self {
            History::Plain(h) => h,
            History::Git(g) => &g.history,
        }
    }
}

/// `answered_history`: `AnsweredGit` where `independent_ok` is `true` and
/// the answers name an exchange back (any value under `exchange`), else
/// `AnsweredHistory`.
pub fn answered_history(answers: &PyValue, bound: usize) -> Result<History, PyError> {
    if matches!(answers.get("independent_ok"), Some(PyValue::Bool(true)))
        && has_key(answers, "exchange")
    {
        return Ok(History::Git(AnsweredGit::new(answers, bound)?));
    }
    Ok(History::Plain(AnsweredHistory::new(answers)?))
}

/// The client's git asked `merge-base --independent` of a sorted set:
/// the commits it names, or None where it did not answer.
pub type GitIndependent<'a> = &'a mut dyn FnMut(&[PyStr]) -> Option<Vec<String>>;

/// `independent_answer`: `independent_ok`, and the answers to
/// `independent` questions kept from earlier rounds (the dict entries of
/// the answers' list) with this question's set read by `history` (the
/// reference's `RepositoryHistory.independent`, given the sorted lowered
/// set) put last in place of any entry for the same set.
pub fn independent_answer(
    question: &PyValue,
    answers: &PyValue,
    history: Option<GitIndependent<'_>>,
) -> Result<PyValue, PyError> {
    let mut out = vec![(s("independent_ok"), PyValue::Bool(true))];
    let mut kept: Vec<PyValue> = items_or_none(answers.get("independent"))?
        .into_iter()
        .filter(|e| matches!(e, PyValue::Dict(_)))
        .collect();
    if let Some(PyValue::List(asked)) = question.get("independent") {
        if !asked.is_empty() {
            let of: BTreeSet<PyStr> = asked.iter().map(low).collect::<Result<_, _>>()?;
            let of: Vec<PyStr> = of.into_iter().collect();
            let top = history.and_then(|h| h(&of));
            let of_value = list_of(of.iter());
            kept.retain(|e| !e.get("of").is_some_and(|o| py_eq(o, &of_value)));
            let top_value = match top {
                None => PyValue::None,
                Some(mut t) => {
                    t.sort();
                    t.dedup();
                    PyValue::List(t.iter().map(|c| s(c)).collect())
                }
            };
            kept.push(PyValue::Dict(vec![
                (s("of"), of_value),
                (s("top"), top_value),
            ]));
        }
    }
    if !kept.is_empty() {
        out.push((s("independent"), PyValue::List(kept)));
    }
    Ok(PyValue::Dict(out))
}

/// `_NONCE.fullmatch`: 32 lowercase hexadecimal digits.
pub fn nonce_ok(text: &[u32]) -> bool {
    text.len() == 32 && text.iter().all(|&c| matches!(c, 0x30..=0x39 | 0x61..=0x66))
}

/// The session and machine whose round kept an exchange.
pub type Owner = (Option<PyStr>, PyStr);
/// The folders read, by folder, in the order read (None where the read
/// gave no spelling).
pub type Reads = Vec<(PyStr, Option<PyStr>)>;

/// `_Kept`: one exchange's keeping between its rounds.
#[derive(Clone, Debug, PartialEq)]
pub struct Kept {
    pub owner: Owner,
    pub until: f64,
    pub reads: Reads,
    pub asked: Asked,
}

/// `_EXCHANGES`: the kept exchanges by nonce, oldest first.
#[derive(Clone, Debug, Default)]
pub struct Exchanges {
    kept: Vec<(String, Kept)>,
}

impl Exchanges {
    pub fn new() -> Self {
        Self::default()
    }

    /// The kept exchanges, oldest first.
    pub fn entries(&self) -> &[(String, Kept)] {
        &self.kept
    }

    /// `_drop`: each exchange whose time is up at `now`, and each one
    /// `owner` kept where one is named.
    pub fn drop_due(&mut self, now: f64, owner: Option<&Owner>) {
        self.kept
            .retain(|(_, k)| !(k.until <= now || owner.is_some_and(|o| *o == k.owner)));
    }
}

/// `ExchangeReads`: what the daemon read and asked for one exchange.
#[derive(Clone, Debug)]
pub struct ExchangeReads {
    owner: Owner,
    /// The client answered and named no exchange back.
    pub silent: bool,
    pub reads: Reads,
    pub asked: Asked,
    pub nonce: String,
    pub history: History,
}

impl ExchangeReads {
    /// `ExchangeReads(answers, session=, machine=)` at `now`, with `fresh`
    /// for `secrets.token_hex(16)`. The kept exchange it takes up leaves
    /// `exchanges` before the history reader is built, so a class that
    /// reader raises loses it, as in the reference.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        answers: &PyValue,
        session: Option<PyStr>,
        machine: PyStr,
        exchanges: &mut Exchanges,
        now: f64,
        fresh: [u8; 16],
        bound: usize,
    ) -> Result<Self, PyError> {
        let owner = (session, machine);
        let silent = truthy(answers) && !has_key(answers, "exchange");
        let named = match answers.get("exchange") {
            Some(PyValue::Str(t)) if nonce_ok(t) => unicode::to_string(t),
            _ => None,
        };
        exchanges.drop_due(now, if silent { Some(&owner) } else { None });
        let mut got = None;
        if let Some(n) = &named {
            if let Some(at) = exchanges
                .kept
                .iter()
                .position(|(k, kept)| k == n && kept.owner == owner)
            {
                got = Some(exchanges.kept.remove(at).1);
            }
        }
        let nonce = match (&got, named) {
            (Some(_), Some(n)) => n,
            _ => fresh.iter().map(|b| format!("{b:02x}")).collect(),
        };
        let (reads, asked) = match got {
            Some(k) => (k.reads, k.asked),
            None => (Vec::new(), BTreeSet::new()),
        };
        let history = answered_history(answers, bound)?;
        Ok(ExchangeReads {
            owner,
            silent,
            reads,
            asked,
            nonce,
            history,
        })
    }

    /// `self[folder] = spelling`: a read recorded, in place where the
    /// folder is already held, else last (a dict's order).
    pub fn set_read(&mut self, folder: PyStr, spelling: Option<PyStr>) {
        match self.reads.iter_mut().find(|(f, _)| *f == folder) {
            Some(slot) => slot.1 = spelling,
            None => self.reads.push((folder, spelling)),
        }
    }

    /// `history.independent` asked through this exchange; None where the
    /// history is no `AnsweredGit` (the reference has no such method).
    pub fn independent(&mut self, commits: &[PyStr]) -> Option<Independent> {
        match &self.history {
            History::Git(g) => Some(g.independent(commits, Some(&mut self.asked))),
            History::Plain(_) => None,
        }
    }

    /// `carried`: keep the reads and the sets asked for
    /// `EXCHANGE_SECONDS` from `now` and give the nonce to name back;
    /// None (the reference's `{}`) for a silent client or where nothing
    /// was read or asked.
    pub fn carried(&self, exchanges: &mut Exchanges, now: f64) -> Option<String> {
        if self.silent || (self.reads.is_empty() && self.asked.is_empty()) {
            return None;
        }
        exchanges.drop_due(now, None);
        exchanges.kept.retain(|(k, _)| *k != self.nonce);
        exchanges.kept.push((
            self.nonce.clone(),
            Kept {
                owner: self.owner.clone(),
                until: now + EXCHANGE_SECONDS,
                reads: self.reads.clone(),
                asked: self.asked.clone(),
            },
        ));
        while exchanges.kept.len() > EXCHANGES_CAP {
            exchanges.kept.remove(0);
        }
        Some(self.nonce.clone())
    }
}
