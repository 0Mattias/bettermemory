//! The question half of `location_witness.py`: the readers that answer the
//! membership model from what a client sent (`AnsweredToken`,
//! `AnsweredLiveness`), the client's answer to a re-mint question
//! (`remint_answer`) and whether it found another session's re-mint
//! (`remint_refused`), and the client's reading of a view question
//! (`asked_views`, `_asked_head`, `_asked_stamps`).
//!
//! `NeedWitnesses` is built elsewhere: a call the reference makes raise it
//! answers `Err` with the question mapping it would carry, its keys in the
//! order the reference builds the dict.
//!
//! The effects are arguments: the re-mint (`checkout_token.remint`) is a
//! closure over (git directory, policy, bare, replacing), and the model's
//! `checkouts(token=...)` a closure giving each match's machine and
//! object key.

use crate::capture::location::{Location, ObjectKey};
use crate::capture::{str_of, truth, Capture, Key, Kind, Stamp, STR_DEPTH_TOP};
use crate::checkout_token::{lineage, valid, Lineage};
use crate::githead::full_hash;
use crate::membership_records::records::{Checkout, View};
use crate::pyvalue::{py_int, PyError, PyStr, PyValue};
use crate::unicode::cps;

/// The re-mint a caller hands in: `checkout_token.remint(git_dir,
/// policy=..., bare=..., replacing=...)` answering (token, status). The
/// git directory is the capture's text as the reference hands it on: the
/// re-mint skips (policy off, bare) before it spells the path, so the
/// spelling, and its platform, belong to the re-mint.
pub type Remint<'a> =
    dyn FnMut(&[u32], bool, bool, Option<Lineage>) -> (Option<String>, String) + 'a;

/// `model.checkouts(token=token)`: each match's (machine, object key).
pub type CheckoutsOf<'a> = dyn FnMut(&[u32]) -> Vec<(PyStr, Option<ObjectKey>)> + 'a;

/// What `AnsweredToken(...)(checkout)` gives: a re-mint answered (the
/// token where valid, the status), or None (re-mint after the final reply).
pub type TokenAnswer = Option<(Option<PyStr>, PyStr)>;

static NONE: PyValue = PyValue::None;

fn dict(items: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(
        items
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    )
}

fn opt(v: &Option<PyStr>) -> PyValue {
    v.as_ref()
        .map_or(PyValue::None, |s| PyValue::Str(s.clone()))
}

fn is_str(v: Option<&PyValue>, text: &str) -> bool {
    matches!(v, Some(PyValue::Str(s)) if *s == cps(text))
}

/// `AnsweredToken`: the checkout token a client re-minted when asked.
pub struct AnsweredToken<'a> {
    answers: &'a PyValue,
    posted: bool,
}

impl<'a> AnsweredToken<'a> {
    pub fn new(answers: &'a PyValue, posted: bool) -> Self {
        AnsweredToken { answers, posted }
    }

    /// `__call__(checkout)`: the answered re-mint (an invalid token read as
    /// None, a status that is no non-empty `str` as `failed:io`); None for
    /// a client that posted without `remint_ok`; else the question, naming
    /// the lineage the store records.
    pub fn call(&self, checkout: &Checkout) -> Result<TokenAnswer, PyValue> {
        if let Some(got @ PyValue::Dict(_)) = self.answers.get("remint") {
            let token = got.get("token").unwrap_or(&NONE);
            let status = match got.get("status") {
                Some(PyValue::Str(s)) if !s.is_empty() => s.clone(),
                _ => cps("failed:io"),
            };
            let token = match token {
                PyValue::Str(s) if valid(token) => Some(s.clone()),
                _ => None,
            };
            return Ok(Some((token, status)));
        }
        if self.posted && !truth(self.answers.get("remint_ok")) {
            return Ok(None);
        }
        let replacing = checkout.lineage().map_or(PyValue::None, |l| {
            PyValue::List(vec![
                PyValue::Str(l.token),
                PyValue::Str(l.device),
                PyValue::Str(l.inode),
            ])
        });
        Err(dict(vec![
            ("remint", PyValue::Bool(true)),
            ("replacing", replacing),
        ]))
    }

    /// `located(loc, model)`: `loc` holding the written re-mint token where
    /// a checkout of its machine at its object (device and inode) already
    /// records it; else `loc` as posted. The model is asked only past the
    /// five early returns.
    pub fn located(&self, loc: &Location, checkouts: &mut CheckoutsOf<'_>) -> Location {
        let got = match self.answers.get("remint") {
            Some(g @ PyValue::Dict(_)) => g,
            _ => return loc.clone(),
        };
        let token = got.get("token").unwrap_or(&NONE);
        let (Some(key), PyValue::Str(text)) = (&loc.object_key, token) else {
            return loc.clone();
        };
        if !is_str(got.get("status"), "written")
            || !valid(token)
            || loc.token.as_ref() == Some(text)
        {
            return loc.clone();
        }
        let held = checkouts(text).into_iter().any(|(machine, k)| {
            machine == loc.machine
                && k.is_some_and(|k| k.device == key.device && k.inode == key.inode)
        });
        if !held {
            return loc.clone();
        }
        let mut out = loc.clone();
        out.token = Some(text.clone());
        out.token_written = None;
        out.token_status = cps("present");
        out
    }
}

/// `remint_answer(cap, question)`: the capture's token re-minted under the
/// question's policy, naming the question's ``replacing`` where it has the
/// key, else the capture's own lineage. The git directory reaches the
/// re-mint as text, unencoded, as the reference passes its `str`.
pub fn remint_answer(
    cap: &Capture,
    question: &PyValue,
    remint: &mut Remint<'_>,
) -> Result<PyValue, PyError> {
    let Some(git_dir) = cap.git_dir.as_ref().filter(|g| !g.is_empty()) else {
        return Ok(dict(vec![
            ("token", PyValue::None),
            ("status", PyValue::str("skipped:no_git_dir")),
        ]));
    };
    let replacing = match question.get("replacing") {
        Some(value) => Lineage::read(value),
        None => {
            let token = match &cap.token {
                Some(t) if !t.is_empty() => PyValue::Str(t.clone()),
                _ => opt(&cap.token_written),
            };
            let key = cap.object_key.as_ref().map_or(PyValue::None, |k| {
                PyValue::Tuple(vec![
                    PyValue::Str(k.0.clone()),
                    PyValue::Str(k.1.clone()),
                    opt(&k.2),
                ])
            });
            lineage(&token, &key)
        }
    };
    let policy = truth(question.get("write_token"));
    let (token, status) = remint(git_dir, policy, cap.kind == Some(Kind::Bare), replacing);
    Ok(dict(vec![
        ("token", token.map_or(PyValue::None, |t| PyValue::str(&t))),
        ("status", PyValue::str(&status)),
    ]))
}

/// `remint_refused(question, answers)`: the question asked a re-mint and
/// the answer's status is ``exists``.
pub fn remint_refused(question: &PyValue, answers: &PyValue) -> bool {
    truth(question.get("remint"))
        && matches!(answers.get("remint"), Some(g @ PyValue::Dict(_)) if is_str(g.get("status"), "exists"))
}

/// `AnsweredLiveness`: each view's state as the client answered it.
pub struct AnsweredLiveness {
    states: std::collections::HashMap<PyStr, PyStr>,
}

impl AnsweredLiveness {
    /// `AnsweredLiveness(answers)`: ``views`` read as `{str(k): str(v)}`
    /// where it is a mapping; `str()` raising passes through.
    pub fn new(answers: &PyValue) -> Result<Self, PyError> {
        let mut states = std::collections::HashMap::new();
        if let Some(PyValue::Dict(items)) = answers.get("views") {
            for (k, v) in items {
                let k = str_of(k, STR_DEPTH_TOP)?;
                states.insert(k, str_of(v, STR_DEPTH_TOP)?);
            }
        }
        Ok(AnsweredLiveness { states })
    }

    /// `__call__(checkout, view)`: the answered state, or the question
    /// naming the view as the client reads it.
    pub fn call(&self, checkout: &Checkout, view: &View) -> Result<PyStr, PyValue> {
        if let Some(hit) = self.states.get(&view.path) {
            return Ok(hit.clone());
        }
        let key = |k: &Option<ObjectKey>| {
            k.as_ref().map_or(PyValue::None, |k| {
                PyValue::List(vec![
                    PyValue::Str(k.device.clone()),
                    PyValue::Str(k.inode.clone()),
                    opt(&k.birth),
                ])
            })
        };
        let stamps = checkout
            .stamps
            .iter()
            .map(|s| {
                let mut row = vec![PyValue::Str(s.name.clone())];
                row.extend(s.numbers.iter().map(|n| PyValue::Int(n.clone())));
                PyValue::List(row)
            })
            .collect();
        let asked = dict(vec![
            ("path", PyValue::Str(view.path.clone())),
            ("kind", PyValue::Str(checkout.kind.clone())),
            ("object", key(&checkout.object_key)),
            ("token", opt(&checkout.token)),
            ("superseded", PyValue::Bool(checkout.token_superseded())),
            ("last_head", opt(&checkout.last_head)),
            ("stamps", PyValue::List(stamps)),
            ("common_object", key(&checkout.common_object_key)),
        ]);
        Err(dict(vec![("views", PyValue::List(vec![asked]))]))
    }
}

/// One view `asked_views` reads: the keyword arguments of
/// `location.view_state`.
#[derive(Clone, Debug, PartialEq)]
pub struct AskedView {
    pub kind: PyStr,
    pub key: Option<Key>,
    pub token: Option<PyStr>,
    pub last_head: Option<PyStr>,
    pub stamps: Vec<Stamp>,
    pub common_key: Option<Key>,
    pub superseded: bool,
}

/// `location_git._key` (location_git.py 1425-1431): None for None, a list
/// or tuple of three as `str()` of each (a None birth kept), anything else
/// ValueError.
fn key(value: Option<&PyValue>) -> Result<Option<Key>, PyError> {
    let items = match value {
        None | Some(PyValue::None) => return Ok(None),
        Some(PyValue::List(items) | PyValue::Tuple(items)) if items.len() == 3 => items,
        Some(_) => return Err(PyError::ValueError),
    };
    let device = str_of(&items[0], STR_DEPTH_TOP)?;
    let inode = str_of(&items[1], STR_DEPTH_TOP)?;
    let birth = match &items[2] {
        PyValue::None => None,
        b => Some(str_of(b, STR_DEPTH_TOP)?),
    };
    Ok(Some((device, inode, birth)))
}

/// `asked_views(question)`: each view with a `str` path, its fields read
/// in the reference's order; a malformed object key is ValueError and a
/// ``views`` that is truthy and not iterable TypeError.
pub fn asked_views(question: &PyValue) -> Result<Vec<(PyStr, AskedView)>, PyError> {
    let views: &[PyValue] = match question.get("views") {
        Some(v) if !truth(Some(v)) => &[],
        None => &[],
        Some(PyValue::List(items) | PyValue::Tuple(items)) => items,
        // A str iterates its characters and a dict its keys: no mapping.
        Some(PyValue::Str(_) | PyValue::Dict(_)) => &[],
        Some(_) => return Err(PyError::TypeError),
    };
    let mut out = Vec::new();
    for v in views {
        let Some(PyValue::Str(path)) = v.get("path") else {
            continue;
        };
        let kind = match v.get("kind") {
            Some(k) if truth(Some(k)) => str_of(k, STR_DEPTH_TOP)?,
            _ => cps("primary"),
        };
        let object = key(v.get("object"))?;
        let token = match v.get("token") {
            Some(PyValue::Str(t)) => Some(t.clone()),
            _ => None,
        };
        let last_head = asked_head(v.get("last_head"));
        let stamps = asked_stamps(v.get("stamps"))?;
        let common_key = key(v.get("common_object"))?;
        let superseded = matches!(v.get("superseded"), Some(PyValue::Bool(true)));
        out.push((
            path.clone(),
            AskedView {
                kind,
                key: object,
                token,
                last_head,
                stamps,
                common_key,
                superseded,
            },
        ));
    }
    Ok(out)
}

/// `_asked_head(value)`: a `str` whose `lower()` is a full hash, lowered.
/// No code point outside ASCII lowers to ASCII hex digits alone, so a
/// text with one is no hash.
pub fn asked_head(value: Option<&PyValue>) -> Option<PyStr> {
    let Some(PyValue::Str(s)) = value else {
        return None;
    };
    let lowered: Option<Vec<u8>> = s
        .iter()
        .map(|&c| {
            u8::try_from(c)
                .ok()
                .filter(u8::is_ascii)
                .map(|b| b.to_ascii_lowercase())
        })
        .collect();
    let lowered = lowered?;
    full_hash(&lowered).then(|| lowered.iter().map(|&b| u32::from(b)).collect())
}

/// `_asked_stamps(value)`: of a list's first 8 items, each list or tuple
/// of 5 read as (`str`, `int` x4); one whose reading raises TypeError or
/// ValueError is left out, any other raise passes through.
pub fn asked_stamps(value: Option<&PyValue>) -> Result<Vec<Stamp>, PyError> {
    let Some(PyValue::List(items)) = value else {
        return Ok(Vec::new());
    };
    let mut out = Vec::new();
    for s in items.iter().take(8) {
        let (PyValue::List(f) | PyValue::Tuple(f)) = s else {
            continue;
        };
        if f.len() != 5 {
            continue;
        }
        match stamp(f) {
            Ok(st) => out.push(st),
            Err(PyError::TypeError | PyError::ValueError) => continue,
            Err(e) => return Err(e),
        }
    }
    Ok(out)
}

fn stamp(f: &[PyValue]) -> Result<Stamp, PyError> {
    let name = str_of(&f[0], STR_DEPTH_TOP)?;
    let mut numbers: [String; 4] = Default::default();
    for (slot, v) in numbers.iter_mut().zip(&f[1..]) {
        *slot = match &py_int(v)? {
            PyValue::Int(text) => text.clone(),
            _ => return Err(PyError::TypeError),
        };
    }
    Ok(Stamp { name, numbers })
}
