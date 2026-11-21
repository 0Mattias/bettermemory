//! The stubs of `tests/contract/gen_membership_views.py` and its steps,
//! driven over the native `Membership`: a liveness that answers a state
//! per path and logs each read, a history reader that logs each call, a
//! `_shared_remint` that counts its calls.

use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};
use std::rc::Rc;

use bettermemory_core::capture::location::{Location, ObjectKey, ProjectFileRef};
use bettermemory_core::capture::{Kind, Stamp, State};
use bettermemory_core::datetime::DateTime;
use bettermemory_core::membership::{Error, LivenessSeam, Membership, Options};
use bettermemory_core::membership_records::records::{
    Checkout, HistoryReader, IndependentReader, View,
};
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode::cps;

use super::membership_core::{raised, Fake};
use super::membership_rows::{enc, field, int, list, name};

pub type Model = Membership<Fake>;
pub type Log = Rc<RefCell<Vec<PyValue>>>;

fn s(t: &[u32]) -> PyValue {
    PyValue::Str(t.to_vec())
}

fn strs<'a>(v: impl IntoIterator<Item = &'a PyStr>) -> PyValue {
    PyValue::List(v.into_iter().map(|t| s(t)).collect())
}

fn text(v: &PyValue) -> Option<PyStr> {
    match v {
        PyValue::Str(t) => Some(t.clone()),
        PyValue::None => None,
        other => panic!("not a text: {other:?}"),
    }
}

fn texts(v: Option<&PyValue>) -> Vec<PyStr> {
    v.map_or(Vec::new(), |v| {
        list(v).iter().map(|x| text(x).expect("a text")).collect()
    })
}

fn map_of(v: Option<&PyValue>) -> BTreeMap<PyStr, PyValue> {
    match v {
        Some(PyValue::Dict(items)) => items
            .iter()
            .map(|(k, x)| (text(k).expect("a key"), x.clone()))
            .collect(),
        _ => BTreeMap::new(),
    }
}

/// `_History` / `_GitHistory`.
pub struct History {
    pub holds: BTreeSet<PyStr>,
    pub fp: BTreeMap<PyStr, PyValue>,
    pub parents: BTreeMap<PyStr, PyValue>,
    pub reach: BTreeMap<PyStr, PyValue>,
    pub bounded: Option<bool>,
    pub git: bool,
    pub calls: Log,
}

impl History {
    pub fn of(spec: &PyValue, calls: Log) -> Option<History> {
        if *spec == PyValue::None {
            return None;
        }
        Some(History {
            holds: texts(spec.get("holds")).into_iter().collect(),
            fp: map_of(spec.get("fp")),
            parents: map_of(spec.get("parents")),
            reach: map_of(spec.get("reach")),
            bounded: match spec.get("bounded") {
                Some(PyValue::Bool(b)) => Some(*b),
                None => None,
                other => panic!("bounded {other:?}"),
            },
            git: spec.get("git") == Some(&PyValue::Bool(true)),
            calls,
        })
    }

    fn note(&self, method: &str, arg: PyValue) {
        self.calls
            .borrow_mut()
            .push(PyValue::List(vec![PyValue::str(method), arg]));
    }
}

impl HistoryReader for History {
    fn holds(&self, commits: &[PyStr]) -> BTreeSet<PyStr> {
        self.note("holds", strs(commits));
        commits
            .iter()
            .filter(|c| self.holds.contains(*c))
            .cloned()
            .collect()
    }
    fn holds_objects(&self, _: &[PyStr]) -> BTreeSet<PyStr> {
        panic!("not asked")
    }
    fn fingerprint(&self, commit: &[u32]) -> Option<PyStr> {
        self.note("fingerprint", s(commit));
        self.fp.get(commit).and_then(text)
    }
    fn parents(&self, commit: &[u32]) -> Vec<PyStr> {
        self.note("parents", s(commit));
        texts(self.parents.get(commit))
    }
    fn reachable_among(
        &self,
        commits: &[PyStr],
    ) -> (BTreeMap<PyStr, BTreeSet<PyStr>>, Option<bool>) {
        self.note("reachable_among", strs(commits));
        let out = commits
            .iter()
            .map(|c| {
                let c = bettermemory_core::unicode::lower(c);
                let r = texts(self.reach.get(&c)).into_iter().collect();
                (c, r)
            })
            .collect();
        (out, self.bounded)
    }
    fn blobs(&self, _: &[u32]) -> Vec<PyStr> {
        panic!("not asked")
    }
}

impl IndependentReader for History {
    fn independent(&self, _: &[PyStr]) -> Option<BTreeSet<PyStr>> {
        panic!("not asked")
    }
}

fn key(v: &PyValue) -> Option<ObjectKey> {
    match v {
        PyValue::None => None,
        PyValue::List(x) => Some(ObjectKey {
            device: text(&x[0]).expect("a device"),
            inode: text(&x[1]).expect("an inode"),
            birth: text(&x[2]),
        }),
        other => panic!("a key: {other:?}"),
    }
}

/// `_loc(change)`: the generator's base location with the case's changes.
pub fn location(change: &PyValue) -> Location {
    let get = |k: &str| -> PyValue {
        change.get(k).cloned().unwrap_or_else(|| match k {
            "kind" => PyValue::str("primary"),
            "root" => PyValue::str("/w/r"),
            "git_dir" => PyValue::str("/w/r/.git"),
            "token_status" => PyValue::str("present"),
            "object_key" => {
                PyValue::List(vec![PyValue::str("1"), PyValue::str("100"), PyValue::None])
            }
            "ref_tips" | "stamps" | "files" => PyValue::List(Vec::new()),
            _ => PyValue::None,
        })
    };
    let root = text(&get("root"));
    let kind = text(&get("kind")).map(|k| match name(&PyValue::Str(k)).as_str() {
        "primary" => Kind::Primary,
        "linked" => Kind::Linked,
        "bare" => Kind::Bare,
        "folder" => Kind::Folder,
        other => panic!("kind {other}"),
    });
    let stamps = list(&get("stamps"))
        .iter()
        .map(|x| {
            let x = list(x);
            Stamp {
                name: text(&x[0]).expect("a name"),
                numbers: [1, 2, 3, 4].map(|i| int(&x[i]).to_string()),
            }
        })
        .collect();
    let files = list(&get("files"))
        .iter()
        .map(|f| {
            let f = list(f);
            let dir = text(&f[1]).expect("a dir");
            let mut path = dir.clone();
            path.extend(cps("/bettermemory.toml"));
            ProjectFileRef {
                id: text(&f[0]).expect("an id"),
                name: cps("n"),
                dir,
                path,
                boundary: false,
                object_key: None,
            }
        })
        .collect();
    Location {
        machine: cps("M1"),
        cwd: root.clone().unwrap_or_else(|| cps("/w")),
        state: State::Repo,
        root,
        kind,
        git_dir: text(&get("git_dir")),
        object_key: key(&get("object_key")),
        common_object_key: key(&get("common_object_key")),
        root_object_key: None,
        head: text(&get("head")),
        branch: None,
        ref_tips: texts(Some(&get("ref_tips"))),
        remote_keys: BTreeSet::new(),
        alias_keys: BTreeSet::new(),
        shallow: false,
        token: text(&get("token")),
        token_written: text(&get("token_written")),
        token_status: text(&get("token_status")).expect("a status"),
        stamps,
        ticks: None,
        files,
        file_tracked: None,
        evicted: false,
        detail: Vec::new(),
        manifest: None,
        session: None,
    }
}

/// `_Run`: the model, the paths the liveness read and the history calls.
pub struct Run {
    pub m: Model,
    pub lv: Log,
    pub hc: Log,
}

impl Run {
    pub fn new(tables: BTreeMap<String, Vec<PyValue>>, live: &PyValue) -> Run {
        let live = map_of(Some(live));
        let lv: Log = Rc::default();
        let read = Rc::clone(&lv);
        let liveness = move |_: &Checkout, v: &View| -> PyStr {
            read.borrow_mut().push(s(&v.path));
            live.get(&v.path)
                .and_then(text)
                .unwrap_or_else(|| cps("gone"))
        };
        let now = DateTime::from_isoformat(&cps("2026-01-01T00:00:00+00:00")).expect("now");
        let m = Membership::new(
            Fake::new(tables, None, false),
            cps("M1"),
            Options {
                liveness: LivenessSeam::Given(Box::new(liveness)),
                token_writer: None,
                now: Box::new(move || now),
                session: None,
            },
        );
        Run {
            m,
            lv,
            hc: Rc::default(),
        }
    }
}

/// One step's answer, unspelled.
pub fn answer(m: &mut Model, hc: &Log, op: &str, a: &PyValue) -> Result<PyValue, Error<PyStr>> {
    let hist = History::of(a.get("hist").unwrap_or(&PyValue::None), Rc::clone(hc));
    let h: Option<&dyn HistoryReader> = hist.as_ref().map(|h| h as &dyn HistoryReader);
    let git: Option<&dyn IndependentReader> = hist
        .as_ref()
        .filter(|h| h.git)
        .map(|h| h as &dyn IndependentReader);
    let loc = a.get("loc").map(location);
    let k = match a.get("k") {
        Some(kid) => Some(m.checkout(&text(kid).expect("an id"))?),
        None => None,
    };
    let remints = Rc::new(RefCell::new(0i128));
    let said = a.get("remint") == Some(&PyValue::Bool(true));
    let counted = Rc::clone(&remints);
    let mut remint = move |_: &Checkout, _: &Location, _: Option<&PyStr>| {
        *counted.borrow_mut() += 1;
        Ok(said)
    };
    Ok(match op {
        "view_state" => {
            let k = k.expect("a checkout");
            let view = match a.get("view") {
                Some(PyValue::None) | None => None,
                Some(i) => Some(k.views[int(i) as usize].clone()),
            };
            s(&m.view_state(&k, view.as_ref()))
        }
        "live_view" => m
            .live_view(&k.expect("a checkout"))
            .map_or(PyValue::None, PyValue::Str),
        "held_span" => {
            let ops = list(field(a, "ops")).to_vec();
            let got: Result<Vec<PyValue>, Error<PyStr>> = m.liveness_held(|m| {
                ops.iter()
                    .map(|st| answer(m, hc, &name(field(st, "op")), field(st, "a")))
                    .collect()
            });
            PyValue::List(got?)
        }
        "continuity" => {
            let (ok, how) = m.continuity(
                &k.expect("a checkout"),
                loc.as_ref().expect("a location"),
                h,
                &mut remint,
            )?;
            PyValue::List(vec![
                PyValue::Bool(ok),
                PyValue::str(how),
                PyValue::int(*remints.borrow()),
            ])
        }
        "confirms" => PyValue::Bool(m.confirms(
            &k.expect("a checkout"),
            loc.as_ref().expect("a location"),
            h,
        )?),
        "by_common" => {
            let (got, rule) = m.by_common(loc.as_ref().expect("a location"), h, &mut remint)?;
            PyValue::List(vec![
                got.map_or(PyValue::None, |k| PyValue::Str(k.id)),
                PyValue::str(rule),
                PyValue::int(*remints.borrow()),
            ])
        }
        "by_pointer" => {
            PyValue::Bool(m.by_pointer(&k.expect("a checkout"), loc.as_ref().expect("a location")))
        }
        "holds" => strs(&Model::holds(h, &texts(a.get("commits")))),
        "held" => {
            let ex = texts(a.get("exclude"));
            let got = m.held(loc.as_ref().expect("a location"), h, ex.first())?;
            PyValue::List(
                got.iter()
                    .map(|(p, s_)| PyValue::List(vec![s(p), strs(s_)]))
                    .collect(),
            )
        }
        "rewritten" => {
            let ex = texts(a.get("exclude"));
            let got = m.rewritten(loc.as_ref().expect("a location"), h, ex.first())?;
            PyValue::List(
                got.iter()
                    .map(|(p, n)| PyValue::Tuple(vec![s(p), PyValue::int(i128::from(*n))]))
                    .collect(),
            )
        }
        "specific" | "most_specific" | "independent" => {
            m.ancestry = match a.get("ancestry") {
                None | Some(PyValue::None) => None,
                Some(PyValue::Dict(_)) => Some(BTreeMap::new()),
                Some(items) => Some(
                    list(items)
                        .iter()
                        .map(|it| {
                            let it = list(it);
                            let r = match &it[1] {
                                PyValue::None => None,
                                r => Some(texts(Some(r)).into_iter().collect()),
                            };
                            (texts(Some(&it[0])), r)
                        })
                        .collect(),
                ),
            };
            let need = |e: bettermemory_core::membership_records::records::NeedIndependent| {
                PyValue::Dict(vec![(PyValue::str("need"), strs(&e.commits))])
            };
            let held: BTreeMap<PyStr, BTreeSet<PyStr>> = map_of(a.get("held"))
                .into_iter()
                .map(|(p, v)| (p, texts(Some(&v)).into_iter().collect()))
                .collect();
            let out = match op {
                "independent" => match m.independent(git, &texts(a.get("shas"))) {
                    Ok(got) => got.map_or(PyValue::None, |g| strs(&g)),
                    Err(e) => need(e),
                },
                "most_specific" => match m.most_specific(h, git, &held) {
                    Ok(best) => strs(&best),
                    Err(e) => need(e),
                },
                _ => match m.specific(h, git, &held) {
                    Ok(sp) => PyValue::List(vec![
                        strs(&sp.best),
                        strs(&sp.top),
                        PyValue::int(i128::from(sp.held)),
                        PyValue::Bool(sp.bounded),
                    ]),
                    Err(e) => need(e),
                },
            };
            m.ancestry = None;
            out
        }
        other => panic!("no op {other}"),
    })
}

/// One step: `(o, log, lv, hc)`.
pub fn step(run: &mut Run, st: &PyValue) -> (PyValue, PyValue, PyValue, PyValue) {
    let start = run.m.store().0.borrow().log.len();
    run.lv.borrow_mut().clear();
    run.hc.borrow_mut().clear();
    let o = match answer(&mut run.m, &run.hc, &name(field(st, "op")), field(st, "a")) {
        Ok(v) => enc(&v),
        Err(e) => raised(&e),
    };
    let log = enc(&PyValue::List(
        run.m.store().0.borrow().log[start..].to_vec(),
    ));
    let lv = PyValue::List(run.lv.borrow().clone());
    let hc = PyValue::List(run.hc.borrow().clone());
    (o, log, lv, hc)
}

/// A checkout row of the generator's base (`gen_membership_rows._CHECKOUT`)
/// with its id.
pub fn checkout_row(id: &str) -> PyValue {
    let base = bettermemory_core::json::loads(&cps(
        r#"{"project": "P1", "machine": "M1", "kind": "primary",
        "object_device": "16777220", "object_inode": "123", "object_birth": "1700000000.5",
        "common_device": null, "common_inode": null, "common_birth": null,
        "git_dir": "/work/r/.git", "token": "tok", "token_status": "written",
        "stamps_json": [["description", 1, 2, 3, 4], {"ticks": true}], "last_head": "abc",
        "branch": "main", "remotes_json": ["origin"], "manifest_json": null, "legacy": 0,
        "retired": 0, "registered": "2026-01-01T00:00:00", "unresolved_json": null}"#,
    ))
    .expect("the base row");
    let PyValue::Dict(items) = &base else {
        unreachable!()
    };
    let mut items = items.clone();
    items.insert(0, (PyValue::str("id"), PyValue::str(id)));
    PyValue::Dict(items)
}
