//! Every case of `identity_caller.jsonl` (not gated) and
//! `identity_caller_unicode.jsonl` (gated on the Unicode version) against
//! `identity`. The spelling is the generator's
//! (`tests/contract/gen_identity_caller.py`): a text is a string or
//! `{"r": [[piece, n], ...]}`, and an ordered dict is a list of pairs.
//! The working directory is `str(PurePath(cwd))` of the case's flavour
//! (`PureWindowsPath` where the case says `nt`), as the generator's
//! `Path.cwd` answers on every host.

mod common;

use bettermemory_core::config::{self, Flavour};
use bettermemory_core::identity::{self as id, Actor, Caller, Published, Request, Token};
use bettermemory_core::json;
use bettermemory_core::pyvalue::{PyStr, PyValue};
use bettermemory_core::unicode;
use common::parity::Ledger;

pub fn lines(name: &str) -> Vec<PyValue> {
    let text = std::fs::read_to_string(common::golden_path(name))
        .unwrap_or_else(|e| panic!("cannot read golden file {name}: {e}"));
    text.lines()
        .map(|l| json::loads(&unicode::cps(l)).expect("a JSON line"))
        .collect()
}

pub fn get<'a>(v: &'a PyValue, k: &str) -> &'a PyValue {
    v.get(k).unwrap_or_else(|| panic!("no field {k}"))
}

pub fn list(v: &PyValue) -> &[PyValue] {
    match v {
        PyValue::List(items) => items,
        other => panic!("not a list: {other:?}"),
    }
}

pub fn int(v: &PyValue) -> i64 {
    match v {
        PyValue::Int(t) => t.parse().expect("an int"),
        other => panic!("not an int: {other:?}"),
    }
}

/// A spelled text.
pub fn text(v: &PyValue) -> PyStr {
    match v {
        PyValue::Str(s) => s.clone(),
        PyValue::Dict(_) => {
            let mut out = Vec::new();
            for run in list(get(v, "r")) {
                let run = list(run);
                for _ in 0..int(&run[1]) {
                    out.extend(text(&run[0]));
                }
            }
            out
        }
        other => panic!("not a text: {other:?}"),
    }
}

/// A spelled text, null, or `{"o": kind}` (not a `str`): None for both.
pub fn opt(v: &PyValue) -> Option<PyStr> {
    match v {
        PyValue::None => None,
        PyValue::Dict(_) if v.get("r").is_none() => None,
        _ => Some(text(v)),
    }
}

fn s(t: &str) -> PyValue {
    PyValue::str(t)
}

fn o(t: Option<&[u32]>) -> PyValue {
    t.map_or(PyValue::None, |t| PyValue::Str(t.to_vec()))
}

/// The expected value with every spelled text read back.
pub fn unspell(v: &PyValue) -> PyValue {
    match v {
        PyValue::Dict(_) if v.get("r").is_some() => PyValue::Str(text(v)),
        PyValue::List(items) => PyValue::List(items.iter().map(unspell).collect()),
        other => other.clone(),
    }
}

/// A dict as the generator's list of pairs.
fn pairs(v: &PyValue) -> PyValue {
    match v {
        PyValue::Dict(items) => PyValue::List(
            items
                .iter()
                .map(|(k, v)| PyValue::List(vec![k.clone(), pairs(v)]))
                .collect(),
        ),
        other => other.clone(),
    }
}

pub fn actor_of(a: &PyValue) -> Actor {
    let f = |k: &str| a.get(k).and_then(opt);
    Actor {
        client: f("client"),
        client_version: f("client_version"),
        model: f("model"),
        principal: f("principal"),
        session: f("session"),
        sources: list(get(a, "sources"))
            .iter()
            .map(|p| (text(&list(p)[0]), text(&list(p)[1])))
            .collect(),
    }
}

pub fn request_of(q: &PyValue) -> Option<Request> {
    if matches!(q, PyValue::None) {
        return None;
    }
    let flag = |k: &str| matches!(q.get(k), Some(PyValue::Bool(true)));
    let headers = flag("http").then(|| {
        q.get("h")
            .map(|h| {
                list(h)
                    .iter()
                    .map(|p| (text(&list(p)[0]), text(&list(p)[1])))
                    .collect()
            })
            .unwrap_or_default()
    });
    let session = !flag("nosession");
    let client_info = match q.get("ci") {
        Some(ci) if session && !matches!(ci, PyValue::None) => {
            Some((opt(&list(ci)[0]), opt(&list(ci)[1])))
        }
        _ => None,
    };
    let principal = q.get("p").and_then(opt);
    let request_id = match q.get("rid") {
        None | Some(PyValue::None) => None,
        Some(PyValue::Int(t)) => Some(unicode::cps(t)),
        Some(v) => Some(text(v)),
    };
    let root = if session {
        q.get("root").and_then(opt)
    } else {
        None
    };
    Some(Request {
        headers,
        client_info,
        principal,
        request_id,
        root,
    })
}

pub fn caller_out(c: &Caller) -> PyValue {
    let a = &c.actor;
    PyValue::List(vec![
        PyValue::List(vec![
            o(a.client.as_deref()),
            o(a.client_version.as_deref()),
            o(a.model.as_deref()),
            o(a.principal.as_deref()),
            o(a.session.as_deref()),
        ]),
        PyValue::List(
            a.sources
                .iter()
                .map(|(k, v)| PyValue::List(vec![PyValue::Str(k.clone()), PyValue::Str(v.clone())]))
                .collect(),
        ),
        PyValue::List(vec![
            o(c.workspace.path.as_deref()),
            PyValue::Str(c.workspace.source.clone()),
        ]),
        o(c.request_id.as_deref()),
        o(c.harness_session.as_deref()),
    ])
}

/// An environment of pairs that records the keys it is asked, and a
/// working directory that counts its reads: `str(Path.cwd())`, where the
/// generator's `Path.cwd` answers a pure path of the case's flavour.
pub struct World {
    pub env: Vec<(String, PyStr)>,
    pub asked: Vec<String>,
    pub cwd: Option<PyStr>,
    pub ncwd: i64,
}

impl World {
    /// `cwd` is the text the working directory is built from (`/cwd` where
    /// a step names none), spelled as `str` of `flavour`'s pure path:
    /// `PureWindowsPath("/cwd")` is `\cwd`.
    pub fn new(e: Option<&PyValue>, cwd: Option<&PyValue>, flavour: Flavour) -> World {
        let spelled = |t: &[u32]| {
            let t = unicode::to_string(t).expect("a working directory without a surrogate");
            unicode::cps(&config::pure_path(&t, flavour))
        };
        World {
            env: e.map_or(Vec::new(), |e| {
                list(e)
                    .iter()
                    .map(|p| {
                        let k = unicode::to_string(&text(&list(p)[0])).expect("a key");
                        (k, text(&list(p)[1]))
                    })
                    .collect()
            }),
            asked: Vec::new(),
            cwd: match cwd {
                None => Some(spelled(&unicode::cps("/cwd"))),
                Some(v) => opt(v).map(|t| spelled(&t)),
            },
            ncwd: 0,
        }
    }

    pub fn run<T>(
        &mut self,
        f: impl FnOnce(&mut dyn FnMut(&str) -> Option<PyStr>, &mut dyn FnMut() -> Option<PyStr>) -> T,
    ) -> T {
        let env = &self.env;
        let asked = &mut self.asked;
        let cwd = &self.cwd;
        let ncwd = &mut self.ncwd;
        let mut e = |k: &str| {
            asked.push(k.to_string());
            env.iter()
                .rev()
                .find(|(key, _)| key == k)
                .map(|(_, v)| v.clone())
        };
        let mut c = || {
            *ncwd += 1;
            cwd.clone()
        };
        f(&mut e, &mut c)
    }

    pub fn asked(&self) -> PyValue {
        PyValue::List(self.asked.iter().map(|k| s(k)).collect())
    }
}

/// The answer the native build gives for one case, in the file's shape.
pub fn answer(case: &PyValue) -> PyValue {
    let f = |k: &str| get(case, k);
    match f("f") {
        PyValue::Str(n) if *n == unicode::cps("clean") => {
            let sf = matches!(f("sf"), PyValue::Bool(true));
            o(id::clean(opt(f("v")).as_deref(), sf).as_deref())
        }
        PyValue::Str(n) if *n == unicode::cps("strip") => {
            PyValue::Str(id::strip_format_characters(&text(f("v"))))
        }
        PyValue::Str(n) if *n == unicode::cps("jws") => {
            o(id::json_workspace(opt(f("v")).as_deref()).as_deref())
        }
        PyValue::Str(n) if *n == unicode::cps("actor") => {
            let a = actor_of(f("a"));
            PyValue::List(vec![
                PyValue::Bool(a.is_empty()),
                pairs(&a.to_record()),
                o(id::registry_key(&a).as_deref()),
            ])
        }
        PyValue::Str(n) if *n == unicode::cps("matches") => {
            let a = match f("a") {
                PyValue::None => None,
                a => Some(actor_of(a)),
            };
            PyValue::Bool(id::actor_matches(
                a.as_ref(),
                opt(f("c")).as_deref(),
                opt(f("m")).as_deref(),
            ))
        }
        PyValue::Str(n) if *n == unicode::cps("declared") => {
            let w = list(f("w"));
            let ws = id::Workspace {
                path: opt(&w[0]),
                source: text(&w[1]),
            };
            PyValue::Bool(ws.declared())
        }
        PyValue::Str(n) if *n == unicode::cps("resolve") => {
            let q = request_of(f("q"));
            let flavour = match case.get("nt") {
                Some(PyValue::Bool(true)) => Flavour::Windows,
                _ => Flavour::Posix,
            };
            let mut world = World::new(Some(f("e")), Some(f("cwd")), flavour);
            let caller = world.run(|e, c| id::resolve(q.as_ref(), e, c));
            PyValue::List(vec![
                caller_out(&caller),
                world.asked(),
                PyValue::Int(world.ncwd.to_string()),
            ])
        }
        PyValue::Str(n) if *n == unicode::cps("seq") => {
            let mut published = Published::default();
            let mut tokens: Vec<Option<Token>> = Vec::new();
            let mut out = Vec::new();
            for step in list(f("s")) {
                let mut world = World::new(step.get("e"), step.get("cwd"), Flavour::Posix);
                let does = text(get(step, "do"));
                let does = unicode::to_string(&does).expect("a step");
                match does.as_str() {
                    "bind" => {
                        let q = request_of(get(step, "q"));
                        world.run(|e, c| published.bind(q.as_ref(), e, c));
                    }
                    "transcript" => {
                        let sid = opt(get(step, "sid"));
                        let model = opt(get(step, "model"));
                        let client = opt(get(step, "client"));
                        world.run(|e, c| {
                            published.bind_transcript(
                                sid.as_deref(),
                                model.as_deref(),
                                client.as_deref(),
                                e,
                                c,
                            )
                        });
                    }
                    "publish" => {
                        let q = request_of(get(step, "q"));
                        let caller = world.run(|_, c| id::resolve(q.as_ref(), &mut |_| None, c));
                        tokens.push(Some(published.publish(caller)));
                    }
                    "reset" => {
                        let t = tokens[int(get(step, "t")) as usize]
                            .take()
                            .expect("a token used once");
                        published.reset(t);
                    }
                    other => panic!("unknown step {other}"),
                }
                out.push(PyValue::List(vec![
                    published.current().map_or(PyValue::None, caller_out),
                    published
                        .current_actor()
                        .map_or(PyValue::None, |a| pairs(&a.to_record())),
                    o(published.current_harness_session()),
                    world.asked(),
                    PyValue::Int(world.ncwd.to_string()),
                ]));
            }
            PyValue::List(out)
        }
        other => panic!("unknown case {other:?}"),
    }
}

/// The expected answer, in the shape [`answer`] gives.
pub fn expected(case: &PyValue) -> PyValue {
    let e = unspell(get(case, "o"));
    if get(case, "f") == &s("resolve") {
        return PyValue::List(vec![
            e,
            unspell(get(case, "env")),
            get(case, "ncwd").clone(),
        ]);
    }
    e
}

#[test]
fn identity_caller_every_case() {
    let name = "identity_caller.jsonl";
    let all = lines(name);
    let cases = int(get(&all[0], "cases")) as usize;
    let mut ledger = Ledger::new("golden_identity_caller", name);
    for (i, case) in all[1..].iter().enumerate() {
        ledger.case(format!("case {i}"), || {
            assert_eq!(answer(case), expected(case), "case {i}: {case:?}");
        });
    }
    ledger.finish(cases);
    assert_eq!(all.len() - 1, cases);
}

fn class(got: Option<PyStr>, text: &[u32]) -> i64 {
    match got {
        None => 0,
        Some(g) if g == text => 1,
        Some(_) => 2,
    }
}

#[test]
fn identity_caller_unicode_every_code_point() {
    let name = "identity_caller_unicode.jsonl";
    let all = lines(name);
    let cases = int(get(&all[0], "cases")) as usize;
    let mut ledger = Ledger::new("golden_identity_caller_unicode", name);
    for (i, case) in all[1..].iter().enumerate() {
        ledger.case(format!("sweep {i}"), || {
            let sf = matches!(get(case, "sf"), PyValue::Bool(true));
            let mid = get(case, "at") == &s("mid");
            let runs: Vec<(u32, i64)> = list(get(case, "runs"))
                .iter()
                .map(|r| (int(&list(r)[0]) as u32, int(&list(r)[1])))
                .collect();
            let mut at = 0;
            for cp in 0..0x11_0000u32 {
                if at + 1 < runs.len() && runs[at + 1].0 == cp {
                    at += 1;
                }
                let t: Vec<u32> = if mid {
                    vec![0x61, cp, 0x62]
                } else {
                    vec![0x61, cp]
                };
                let got = class(id::clean(Some(&t), sf), &t);
                assert_eq!(got, runs[at].1, "U+{cp:04X} sf={sf} mid={mid}");
            }
        });
    }
    ledger.finish(cases);
}
