//! `membership_rows.py` (278 lines): the membership table rows read into
//! records and written back, the object-key and stamp codecs, `_jsonable`,
//! `_signature` (with `Location.root_file`), `_unwritten` and `_Tally`.
//!
//! A row is a [`PyValue::Dict`] of its columns, the `_json` ones decoded,
//! as `membership_lines::restated_row` takes it. A missing column is
//! KeyError with the key's repr, and the columns are read in the
//! reference's order, so the first exception is the reference's.
//!
//! A column the reference passes through as stored (`git_dir`, `token`,
//! `checkout`, `answer` and the like) holds str or None in the store; the
//! records hold `Option<PyStr>`, so any other value answers [`REFUSED`].
//! The refusal is kept while the rest of the row is read, so a row the
//! reference raises on raises here too. `View.order` is an i64: a
//! `seen_order` outside it is refused the same way.

use std::collections::{BTreeSet, HashMap};

use crate::capture::location::{Location, ObjectKey, ProjectFileRef};
use crate::capture::{Kind, Stamp};
use crate::checkout_token::TOKEN_NAME;
use crate::membership_lines::{dict_of, items, raised, str_raising, truth, type_name, Raised};
use crate::membership_records::paths::path_under;
use crate::membership_records::records::{
    manifest_from_json, manifest_to_json, Checkout, Project, Proposal, View, UNRESOLVED,
};
use crate::printable::{printable, quoted};
use crate::pyvalue::{py_int, repr_str, PyError, PyStr, PyValue};
use crate::unicode::cps;

/// The class of the port's typed refusal: a column the reference passes
/// through holds a value the record cannot hold.
pub const REFUSED: &str = "Refused";

/// `TICKS_KEY`: the key of the `stamps_json` element that holds `ticks`.
pub const TICKS_KEY: &str = "ticks";

/// How deep `_jsonable` descends before it answers RecursionError (the
/// reference meets the interpreter's recursion limit, which follows the
/// Python release; no ungated fixture reaches it).
pub const JSONABLE_DEPTH: usize = 900;

fn s(v: &PyStr) -> PyValue {
    PyValue::Str(v.clone())
}

fn opt(v: &Option<PyStr>) -> PyValue {
    v.as_ref().map_or(PyValue::None, s)
}

fn pairs(items: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(
        items
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    )
}

/// `row[key]`: KeyError with the key's repr where it is missing.
fn col<'a>(row: &'a PyValue, key: &str) -> Result<&'a PyValue, Raised> {
    row.get(key)
        .ok_or_else(|| raised("KeyError", format!("'{key}'")))
}

/// A column passed through as stored: str or None, else the refusal is
/// noted (the first one) and the read goes on.
fn text(row: &PyValue, key: &str, refused: &mut Option<Raised>) -> Result<Option<PyStr>, Raised> {
    let value = col(row, key)?;
    Ok(passed(value, key, refused))
}

fn passed(value: &PyValue, key: &str, refused: &mut Option<Raised>) -> Option<PyStr> {
    match value {
        PyValue::Str(t) => Some(t.clone()),
        PyValue::None => None,
        other => {
            refused.get_or_insert_with(|| {
                raised(REFUSED, format!("{key} holds a {}", type_name(other)))
            });
            None
        }
    }
}

fn finish<T>(value: T, refused: Option<Raised>) -> Result<T, Raised> {
    match refused {
        Some(r) => Err(r),
        None => Ok(value),
    }
}

/// `str(row[key])`.
fn str_col(row: &PyValue, key: &str) -> Result<PyStr, Raised> {
    str_raising(col(row, key)?)
}

/// `int(value)` as canonical decimal text.
fn int_text(value: &PyValue) -> Result<String, Raised> {
    match py_int(value) {
        Ok(PyValue::Int(ref t)) => Ok(t.clone()),
        Ok(_) => Err(raised("TypeError", "")),
        Err(e) => Err(int_raised(value, e)),
    }
}

fn int_raised(value: &PyValue, e: PyError) -> Raised {
    let message = match (e, value) {
        (PyError::ValueError, PyValue::Str(t)) => {
            let shown: String = repr_str(t)
                .iter()
                .map(|&c| char::from_u32(c).unwrap_or('\u{FFFD}'))
                .collect();
            format!("invalid literal for int() with base 10: {shown}")
        }
        (PyError::ValueError, _) => "cannot convert float NaN to integer".to_string(),
        (PyError::OverflowError, _) => "cannot convert float infinity to integer".to_string(),
        (PyError::TypeError, other) => format!(
            "int() argument must be a string, a bytes-like object or a real number, not '{}'",
            type_name(other)
        ),
        (other, _) => other.name().to_string(),
    };
    raised(e.name(), message)
}

/// What `Manifest.from_json` raised, with the text of a non-iterable
/// `entries` (the one text the golden file keeps).
fn manifest_raised(raw: &PyValue, e: PyError) -> Raised {
    if let Some(entries) = raw.get("entries").filter(|v| truth(v)) {
        if let Err(r) = items(entries) {
            return r;
        }
    }
    raised(e.name(), "")
}

/// `str(x) for x in (value or ())`.
fn strs(value: &PyValue) -> Result<Vec<PyStr>, Raised> {
    if !truth(value) {
        return Ok(Vec::new());
    }
    items(value)?.iter().map(str_raising).collect()
}

/// `_key_columns(prefix, key)`: the three columns, None for no key.
pub fn key_columns(prefix: &str, key: Option<&ObjectKey>) -> Vec<(PyValue, PyValue)> {
    let column = |part: &str, v: Option<PyValue>| {
        (
            PyValue::str(&format!("{prefix}_{part}")),
            v.unwrap_or(PyValue::None),
        )
    };
    vec![
        column("device", key.map(|k| s(&k.device))),
        column("inode", key.map(|k| s(&k.inode))),
        column("birth", key.map(|k| opt(&k.birth))),
    ]
}

/// `_key_from(row, prefix)`: device and inode read, None where either is
/// None; else the birth read and each through `str()`.
pub fn key_from(row: &PyValue, prefix: &str) -> Result<Option<ObjectKey>, Raised> {
    let device = col(row, &format!("{prefix}_device"))?;
    let inode = col(row, &format!("{prefix}_inode"))?;
    if matches!(device, PyValue::None) || matches!(inode, PyValue::None) {
        return Ok(None);
    }
    let birth = col(row, &format!("{prefix}_birth"))?;
    let device = str_raising(device)?;
    let inode = str_raising(inode)?;
    let birth = match birth {
        PyValue::None => None,
        b => Some(str_raising(b)?),
    };
    Ok(Some(ObjectKey {
        device,
        inode,
        birth,
    }))
}

/// `_stamps_from(raw)`: each item of `raw or ()` that is a list or tuple
/// of exactly five, `str()` of the first, `int()` of the other four.
pub fn stamps_from(raw: &PyValue) -> Result<Vec<Stamp>, Raised> {
    let mut out = Vec::new();
    if !truth(raw) {
        return Ok(out);
    }
    for item in items(raw)? {
        if let PyValue::List(v) | PyValue::Tuple(v) = &item {
            if v.len() == 5 {
                let name = str_raising(&v[0])?;
                let numbers = [
                    int_text(&v[1])?,
                    int_text(&v[2])?,
                    int_text(&v[3])?,
                    int_text(&v[4])?,
                ];
                out.push(Stamp { name, numbers });
            }
        }
    }
    Ok(out)
}

/// `_ticks_from(raw)`: the first mapping item of `raw or ()` whose
/// `ticks` is a bool.
pub fn ticks_from(raw: &PyValue) -> Result<Option<bool>, Raised> {
    if !truth(raw) {
        return Ok(None);
    }
    for item in items(raw)? {
        if let PyValue::Dict(_) = item {
            if let Some(PyValue::Bool(b)) = item.get(TICKS_KEY) {
                return Ok(Some(*b));
            }
        }
    }
    Ok(None)
}

/// `_stamps_json(k)`: the stamps as lists, then `{ticks: b}` where said.
pub fn stamps_json(k: &Checkout) -> PyValue {
    let mut out: Vec<PyValue> = k
        .stamps
        .iter()
        .map(|st| {
            let mut v = vec![s(&st.name)];
            v.extend(st.numbers.iter().map(|n| PyValue::Int(n.clone())));
            PyValue::List(v)
        })
        .collect();
    if let Some(b) = k.ticks {
        out.push(pairs(vec![(TICKS_KEY, PyValue::Bool(b))]));
    }
    PyValue::List(out)
}

/// `_view_from(row)`. A `seen_order` outside i64 is refused.
pub fn view_from(row: &PyValue) -> Result<View, Raised> {
    let checkout = str_col(row, "checkout")?;
    let machine = str_col(row, "machine")?;
    let path = str_col(row, "path")?;
    let first_seen = str_col(row, "first_seen")?;
    let last_seen = str_col(row, "last_seen")?;
    let order = int_text(col(row, "seen_order")?)?;
    let order = order
        .parse::<i64>()
        .map_err(|_| raised(REFUSED, "seen_order is outside i64"))?;
    Ok(View {
        checkout,
        machine,
        path,
        first_seen,
        last_seen,
        order,
    })
}

/// `_checkout_from(row, views)`: the views sorted stably by order.
pub fn checkout_from(row: &PyValue, views: &[View]) -> Result<Checkout, Raised> {
    let mut refused = None;
    let unresolved = match row.get("unresolved_json") {
        Some(d @ PyValue::Dict(_)) => Some(d),
        _ => None,
    };
    let id = str_col(row, "id")?;
    let project = str_col(row, "project")?;
    let machine = str_col(row, "machine")?;
    let kind = str_col(row, "kind")?;
    let object_key = key_from(row, "object")?;
    let common_object_key = key_from(row, "common")?;
    let git_dir = text(row, "git_dir", &mut refused)?;
    let token = text(row, "token", &mut refused)?;
    let token_status = str_col(row, "token_status")?;
    let stamps = stamps_from(col(row, "stamps_json")?)?;
    let last_head = text(row, "last_head", &mut refused)?;
    let branch = text(row, "branch", &mut refused)?;
    let remotes = strs(col(row, "remotes_json")?)?;
    let raw = col(row, "manifest_json")?;
    let manifest = manifest_from_json(raw).map_err(|e| manifest_raised(raw, e))?;
    let legacy = truth(col(row, "legacy")?);
    let retired = truth(col(row, "retired")?);
    let registered = str_col(row, "registered")?;
    let mut views = views.to_vec();
    views.sort_by_key(|v| v.order);
    let field = |key: &str| unresolved.and_then(|d| d.get(key));
    let sessions = strs(field("sessions").unwrap_or(&PyValue::None))?;
    let replaced_by = passed(
        field("replaced_by").unwrap_or(&PyValue::None),
        "replaced_by",
        &mut refused,
    );
    let ticks = ticks_from(col(row, "stamps_json")?)?;
    let k = Checkout {
        id,
        project,
        machine,
        kind,
        object_key,
        common_object_key,
        git_dir,
        token,
        token_status,
        stamps,
        last_head,
        branch,
        remotes,
        manifest,
        legacy,
        retired,
        registered,
        views,
        sessions,
        replaced_by,
        ticks,
    };
    finish(k, refused)
}

fn flag(b: bool) -> PyValue {
    PyValue::int(i128::from(b))
}

/// `_checkout_row(k)`: the columns in the reference's order.
pub fn checkout_row(k: &Checkout) -> PyValue {
    let mut out = vec![
        (PyValue::str("id"), s(&k.id)),
        (PyValue::str("project"), s(&k.project)),
        (PyValue::str("machine"), s(&k.machine)),
        (PyValue::str("kind"), s(&k.kind)),
    ];
    out.extend(key_columns("object", k.object_key.as_ref()));
    out.extend(key_columns("common", k.common_object_key.as_ref()));
    let unresolved = if k.kind == cps(UNRESOLVED) {
        pairs(vec![
            (
                "sessions",
                PyValue::List(k.sessions.iter().map(s).collect()),
            ),
            ("replaced_by", opt(&k.replaced_by)),
        ])
    } else {
        PyValue::None
    };
    let rest = vec![
        ("git_dir", opt(&k.git_dir)),
        ("token", opt(&k.token)),
        ("token_status", s(&k.token_status)),
        ("stamps_json", stamps_json(k)),
        ("last_head", opt(&k.last_head)),
        ("branch", opt(&k.branch)),
        (
            "remotes_json",
            PyValue::List(k.remotes.iter().map(s).collect()),
        ),
        (
            "manifest_json",
            k.manifest.as_ref().map_or(PyValue::None, manifest_to_json),
        ),
        ("legacy", flag(k.legacy)),
        ("retired", flag(k.retired)),
        ("registered", s(&k.registered)),
        ("unresolved_json", unresolved),
    ];
    out.extend(rest.into_iter().map(|(n, v)| (PyValue::str(n), v)));
    PyValue::Dict(out)
}

/// `_project_from(row)`.
pub fn project_from(row: &PyValue) -> Result<Project, Raised> {
    let mut refused = None;
    let p = Project {
        id: str_col(row, "id")?,
        name: str_col(row, "name")?,
        merged_into: text(row, "merged_into", &mut refused)?,
        provisional: text(row, "provisional", &mut refused)?,
        parent: text(row, "parent", &mut refused)?,
        tenant: text(row, "tenant", &mut refused)?,
        folder: truth(col(row, "folder")?),
        boundary: truth(col(row, "boundary")?),
        minted: str_col(row, "minted")?,
    };
    finish(p, refused)
}

/// `_project_row(p)`.
pub fn project_row(p: &Project) -> PyValue {
    pairs(vec![
        ("id", s(&p.id)),
        ("name", s(&p.name)),
        ("merged_into", opt(&p.merged_into)),
        ("provisional", opt(&p.provisional)),
        ("parent", opt(&p.parent)),
        ("tenant", opt(&p.tenant)),
        ("folder", flag(p.folder)),
        ("boundary", flag(p.boundary)),
        ("minted", s(&p.minted)),
    ])
}

/// `_proposal_from(row)`: `signature_json` read first and iterated by
/// `tuple()` after `answered_by`; `evidence_json` through `dict()` in
/// full; the line through `printable(str())`.
pub fn proposal_from(row: &PyValue) -> Result<Proposal, Raised> {
    let mut refused = None;
    let signature = col(row, "signature_json")?;
    let id = str_col(row, "id")?;
    let checkout = text(row, "checkout", &mut refused)?;
    let kind = str_col(row, "kind")?;
    let candidates = strs(col(row, "candidates_json")?)?;
    let evidence = PyValue::Dict(dict_of(col(row, "evidence_json")?)?);
    let reason = str_col(row, "reason")?;
    let line = printable(&str_col(row, "line")?);
    let state = str_col(row, "state")?;
    let answer = text(row, "answer", &mut refused)?;
    let answered_by = text(row, "answered_by", &mut refused)?;
    let signature = match signature {
        PyValue::None => None,
        v => Some(items(v)?),
    };
    let opened = str_col(row, "opened")?;
    let pr = Proposal {
        id,
        checkout,
        kind,
        candidates,
        evidence,
        reason,
        line,
        state,
        answer,
        answered_by,
        signature,
        opened,
    };
    finish(pr, refused)
}

/// `_proposal_row(pr)`: the evidence through `_jsonable`.
pub fn proposal_row(pr: &Proposal) -> Result<PyValue, Raised> {
    Ok(pairs(vec![
        ("id", s(&pr.id)),
        ("checkout", opt(&pr.checkout)),
        ("kind", s(&pr.kind)),
        (
            "candidates_json",
            PyValue::List(pr.candidates.iter().map(s).collect()),
        ),
        ("evidence_json", jsonable(&pr.evidence)?),
        ("reason", s(&pr.reason)),
        ("line", s(&pr.line)),
        ("state", s(&pr.state)),
        ("answer", opt(&pr.answer)),
        ("answered_by", opt(&pr.answered_by)),
        (
            "signature_json",
            pr.signature.clone().map_or(PyValue::None, PyValue::List),
        ),
        ("opened", s(&pr.opened)),
    ]))
}

/// `_jsonable(value)`: dict keys through `str()` (equal strs collapse:
/// the first position, the last value), tuples and lists to lists. The
/// set branch has no input: [`PyValue`] holds no set.
pub fn jsonable(value: &PyValue) -> Result<PyValue, Raised> {
    // An explicit stack of the containers being copied, so a deep value
    // costs no native stack.
    enum Frame<'a> {
        Dict(
            std::slice::Iter<'a, (PyValue, PyValue)>,
            Vec<(PyValue, PyValue)>,
            HashMap<PyStr, usize>,
            PyStr,
        ),
        Seq(std::slice::Iter<'a, PyValue>, Vec<PyValue>),
    }
    let mut stack: Vec<Frame> = Vec::new();
    let mut next = Some(value);
    loop {
        let mut done = match next.take() {
            Some(v @ (PyValue::Dict(_) | PyValue::List(_) | PyValue::Tuple(_))) => {
                if stack.len() >= JSONABLE_DEPTH {
                    return Err(raised("RecursionError", "maximum recursion depth exceeded"));
                }
                stack.push(match v {
                    PyValue::Dict(items) => {
                        Frame::Dict(items.iter(), Vec::new(), HashMap::new(), Vec::new())
                    }
                    PyValue::List(items) | PyValue::Tuple(items) => {
                        Frame::Seq(items.iter(), Vec::new())
                    }
                    _ => unreachable!("a container"),
                });
                None
            }
            Some(other) => Some(other.clone()),
            None => None,
        };
        // Feed a finished value to its container, then find the next item.
        loop {
            let Some(top) = stack.last_mut() else {
                return Ok(done.expect("a value"));
            };
            match top {
                Frame::Dict(items, out, at, key) => {
                    if let Some(x) = done.take() {
                        let key = std::mem::take(key);
                        match at.get(&key) {
                            Some(&i) => out[i].1 = x,
                            None => {
                                at.insert(key.clone(), out.len());
                                out.push((PyValue::Str(key), x));
                            }
                        }
                    }
                    if let Some((k, x)) = items.next() {
                        *key = str_raising(k)?;
                        next = Some(x);
                        break;
                    }
                    done = Some(PyValue::Dict(std::mem::take(out)));
                }
                Frame::Seq(items, out) => {
                    if let Some(x) = done.take() {
                        out.push(x);
                    }
                    if let Some(x) = items.next() {
                        next = Some(x);
                        break;
                    }
                    done = Some(PyValue::List(std::mem::take(out)));
                }
            }
            stack.pop();
        }
    }
}

/// `Location.root_file`: for a folder checkout its first file; else the
/// file whose folder is the root (`_same_path`).
fn root_file(loc: &Location) -> Option<&ProjectFileRef> {
    if loc.kind == Some(Kind::Folder) {
        return loc.files.first();
    }
    let root = loc.root.as_ref()?;
    loc.files
        .iter()
        .find(|f| crate::membership_records::paths::same_path(&f.dir, root))
}

/// `_signature(loc)`: (head, sorted remote keys, root file id, token or
/// the token written, shallow).
pub fn signature(loc: &Location) -> PyValue {
    let token = match &loc.token {
        Some(t) if !t.is_empty() => Some(t.clone()),
        _ => loc.token_written.clone(),
    };
    PyValue::Tuple(vec![
        opt(&loc.head),
        PyValue::List(loc.remote_keys.iter().map(s).collect()),
        root_file(loc).map_or(PyValue::None, |f| s(&f.id)),
        opt(&token),
        PyValue::Bool(loc.shallow),
    ])
}

/// `_unwritten(loc)`: for a token status starting `failed` or `skipped`,
/// the token file quoted and the status.
pub fn unwritten(loc: &Location) -> Option<PyStr> {
    let status = &loc.token_status;
    if !(status.starts_with(&cps("failed")) || status.starts_with(&cps("skipped"))) {
        return None;
    }
    let base = loc.git_dir.clone().unwrap_or_default();
    let info = cps("info");
    let name = cps(TOKEN_NAME);
    let mut out = quoted(&path_under(&base, &[&info, &name]));
    out.extend(cps(": "));
    out.extend(status.iter().copied());
    Some(out)
}

/// `_Tally`: what the ladder's tally collects. The dicts keep insertion
/// order; the sets are read only after `membership_resolve` sorts them.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Tally {
    pub cands: Vec<(PyStr, BTreeSet<PyStr>)>,
    pub contradictions: Vec<(PyStr, PyStr, PyStr)>,
    pub rekey: Vec<(PyStr, PyStr)>,
    pub superseded: Vec<PyStr>,
    pub dropped: Vec<PyStr>,
    pub held: Vec<(PyStr, BTreeSet<PyStr>)>,
}
