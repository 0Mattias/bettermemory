//! The steps of `tests/contract/gen_membership_writes.py` driven over the
//! native `Membership` and the recording store of `membership_core`: the
//! generator's history readers, its location and project-file specs, and
//! the ops of the writes unit, every other op answered as
//! `membership_core` answers it.

use std::collections::{BTreeMap, BTreeSet};

use bettermemory_core::capture::location::{Location, ObjectKey, ProjectFileRef};
use bettermemory_core::capture::{Kind, State};
use bettermemory_core::membership::Error;
use bettermemory_core::membership_records::records::{manifest_from_json, HistoryReader, View};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;

use super::membership_core::{answer, checkout_out, raised, Model};
use super::membership_rows::{enc, field, int, list, name};

/// `_History`: one fingerprint and the blobs of each commit.
pub struct History {
    pub fingerprint: Option<PyStr>,
    pub blobs: BTreeMap<PyStr, Vec<PyStr>>,
}

impl HistoryReader for History {
    fn holds(&self, _: &[PyStr]) -> BTreeSet<PyStr> {
        BTreeSet::new()
    }
    fn holds_objects(&self, _: &[PyStr]) -> BTreeSet<PyStr> {
        BTreeSet::new()
    }
    fn fingerprint(&self, _: &[u32]) -> Option<PyStr> {
        self.fingerprint.clone()
    }
    fn parents(&self, _: &[u32]) -> Vec<PyStr> {
        Vec::new()
    }
    fn reachable_among(&self, _: &[PyStr]) -> (BTreeMap<PyStr, BTreeSet<PyStr>>, Option<bool>) {
        (BTreeMap::new(), Some(false))
    }
    fn blobs(&self, commit: &[u32]) -> Vec<PyStr> {
        self.blobs.get(commit).cloned().unwrap_or_default()
    }
}

fn text(v: &PyValue) -> Option<PyStr> {
    match v {
        PyValue::Str(t) => Some(t.clone()),
        PyValue::None => None,
        other => panic!("not a str or null: {other:?}"),
    }
}

fn a_str(v: &PyValue) -> PyStr {
    text(v).expect("a str")
}

/// The header's `histories`.
pub fn histories(spec: &PyValue) -> BTreeMap<String, History> {
    let PyValue::Dict(items) = spec else {
        panic!("histories")
    };
    items
        .iter()
        .map(|(k, v)| {
            let v = list(v);
            let PyValue::Dict(bs) = &v[1] else {
                panic!("blobs")
            };
            let blobs = bs
                .iter()
                .map(|(c, b)| (a_str(c), list(b).iter().map(a_str).collect()))
                .collect();
            (
                name(k),
                History {
                    fingerprint: text(&v[0]),
                    blobs,
                },
            )
        })
        .collect()
}

fn key(v: &PyValue) -> Option<ObjectKey> {
    match v {
        PyValue::None => None,
        v => {
            let v = list(v);
            Some(ObjectKey {
                device: a_str(&v[0]),
                inode: a_str(&v[1]),
                birth: text(&v[2]),
            })
        }
    }
}

fn kind(v: &PyValue) -> Option<Kind> {
    text(v).map(|k| {
        let k = name(&PyValue::Str(k));
        *Kind::ALL.iter().find(|x| x.as_str() == k).expect("a kind")
    })
}

/// `_loc(spec)`.
pub fn location(spec: &PyValue) -> Location {
    let g = |k: &str| spec.get(k);
    let t = |k: &str| g(k).and_then(text);
    Location {
        machine: t("machine").unwrap_or_else(|| cps("M1")),
        cwd: t("cwd").unwrap_or_else(|| cps("/w")),
        state: State::Repo,
        root: t("root"),
        kind: g("kind").and_then(kind),
        git_dir: t("git_dir"),
        object_key: g("object_key").and_then(key),
        common_object_key: g("common_object_key").and_then(key),
        root_object_key: None,
        head: t("head"),
        branch: t("branch"),
        ref_tips: g("ref_tips").map_or(Vec::new(), |v| list(v).iter().map(a_str).collect()),
        remote_keys: g("remote_keys")
            .map_or(BTreeSet::new(), |v| list(v).iter().map(a_str).collect()),
        alias_keys: BTreeSet::new(),
        shallow: false,
        token: t("token"),
        token_written: t("token_written"),
        token_status: t("token_status").unwrap_or_else(|| cps("n/a")),
        stamps: Vec::new(),
        ticks: match g("ticks") {
            Some(PyValue::Bool(b)) => Some(*b),
            _ => None,
        },
        files: Vec::new(),
        file_tracked: None,
        evicted: false,
        detail: Vec::new(),
        manifest: g("manifest").and_then(|m| manifest_from_json(m).expect("a manifest")),
        session: None,
    }
}

/// `_file(spec)`.
pub fn file(spec: &PyValue) -> ProjectFileRef {
    let v = list(spec);
    let dir = a_str(&v[2]);
    let mut path = dir.clone();
    path.extend(cps("/bettermemory.toml"));
    ProjectFileRef {
        id: a_str(&v[0]),
        name: a_str(&v[1]),
        dir,
        path,
        boundary: v[3] == PyValue::Bool(true),
        object_key: None,
    }
}

fn strs(v: impl IntoIterator<Item = PyStr>) -> PyValue {
    PyValue::List(v.into_iter().map(PyValue::Str).collect())
}

/// `_answer(m, op, a)` of the writes generator.
pub fn answer_writes(
    m: &mut Model,
    hist: &BTreeMap<String, History>,
    op: &str,
    a: &PyValue,
) -> Result<PyValue, Error<PyStr>> {
    let h = |v: &PyValue| text(v).map(|n| &hist[&name(&PyValue::Str(n))] as &dyn HistoryReader);
    Ok(match op {
        "add_witness" | "add_witness_in" => {
            let a = list(a);
            let (p, c, s) = (a_str(&a[0]), a_str(&a[1]), a_str(&a[2]));
            if op == "add_witness" {
                m.add_witness(&p, &c, &s, h(&a[3]))?;
            } else {
                m.add_witness_in(&p, &c, &s, h(&a[3]))?;
            }
            PyValue::None
        }
        "remotes_of" => strs(m.remotes_of(&a_str(a))?),
        "add_remotes" => {
            let a = list(a);
            let keys: Vec<PyStr> = list(&a[1]).iter().map(a_str).collect();
            m.add_remotes(&a_str(&a[0]), &keys)?;
            PyValue::None
        }
        "bind_file" => {
            let g = |k: &str| a.get(k).and_then(text);
            m.bind_file(
                &g("file_id").expect("an id"),
                &g("project").expect("a project"),
                &g("evidence").expect("evidence"),
                a.get("boundary") == Some(&PyValue::Bool(true)),
                g("checkout").as_deref(),
                g("directory").as_deref(),
            )?;
            PyValue::None
        }
        "file_flag" => {
            let a = list(a);
            strs(m.file_flag(&file(&a[0]), &a_str(&a[1]))?)
        }
        "lift_boundary" | "make_boundary" => {
            let a = list(a);
            let (p, o) = (a_str(&a[0]), text(&a[1]));
            if op == "lift_boundary" {
                m.lift_boundary(&p, o.as_deref())?;
            } else {
                m.make_boundary(&p, o.as_deref())?;
            }
            PyValue::None
        }
        "projects_all" => PyValue::List(
            m.all_projects()?
                .values()
                .map(|p| {
                    PyValue::List(vec![
                        PyValue::Str(p.id.clone()),
                        PyValue::Bool(p.boundary),
                        p.tenant.clone().map_or(PyValue::None, PyValue::Str),
                    ])
                })
                .collect(),
        ),
        "refresh_object" => {
            let a = list(a);
            let k = m.checkout(&a_str(&a[0]))?;
            checkout_out(&m.refresh_object(k, &location(&a[1]))?)
        }
        "put_view" => {
            let v = View {
                checkout: a_str(field(a, "checkout")),
                machine: a_str(field(a, "machine")),
                path: a_str(field(a, "path")),
                first_seen: a_str(field(a, "first_seen")),
                last_seen: a_str(field(a, "last_seen")),
                order: int(field(a, "order")) as i64,
            };
            m.put_view(&v)?;
            PyValue::None
        }
        "register" => {
            let a = list(a);
            checkout_out(&m.register(&location(&a[0]), &a_str(&a[1]), &a_str(&a[2]))?)
        }
        other => answer(m, other, a)?,
    })
}

/// What one step answered, the store calls it made and the rows after it.
pub type Seen = (PyValue, PyValue, PyValue);

fn spelled(e: &Error<PyStr>) -> PyValue {
    match e {
        Error::Raised(r) if r.class == "ValueError" => PyValue::Dict(vec![(
            PyValue::str("raised"),
            PyValue::List(vec![PyValue::str("ValueError"), PyValue::str(&r.message)]),
        )]),
        e => raised(e),
    }
}

/// `_step`: a `call` runs its inner steps in one model call.
pub fn step(m: &mut Model, hist: &BTreeMap<String, History>, st: &PyValue) -> (Seen, Vec<Seen>) {
    let op = name(field(st, "op"));
    let none = PyValue::None;
    let a = st.get("a").unwrap_or(&none);
    let start = m.store().0.borrow().log.len();
    let mut inner = Vec::new();
    let o = if op == "call" {
        let ops = list(field(a, "ops")).to_vec();
        let got = m.call(|m| {
            for s in &ops {
                let sop = name(field(s, "op"));
                let sa = s.get("a").unwrap_or(&PyValue::None).clone();
                let begin = m.store().0.borrow().log.len();
                let r = answer_writes(m, hist, &sop, &sa);
                let o = match &r {
                    Ok(v) => enc(v),
                    Err(e) => spelled(e),
                };
                let log = enc(&PyValue::List(m.store().0.borrow().log[begin..].to_vec()));
                inner.push((o, log, rows(m)));
                r?;
            }
            Ok(())
        });
        match got {
            Ok(()) => PyValue::None,
            Err(e) => spelled(&e),
        }
    } else {
        match answer_writes(m, hist, &op, a) {
            Ok(v) => enc(&v),
            Err(e) => spelled(&e),
        }
    };
    let log = enc(&PyValue::List(m.store().0.borrow().log[start..].to_vec()));
    ((o, log, rows(m)), inner)
}

fn rows(m: &Model) -> PyValue {
    PyValue::List(
        m.rows()
            .iter()
            .map(|&n| PyValue::int(i128::from(n)))
            .collect(),
    )
}
