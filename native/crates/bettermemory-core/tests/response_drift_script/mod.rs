//! The scripted reads, admission and resolution response_drift.jsonl's
//! generator patches into a real `_DriftPass`, and the replay of one
//! case. Shared by the golden test and the kills.
#![allow(dead_code)]

use bettermemory_core::admission::DriftScope;
use bettermemory_core::attestation_paths::Record;
use bettermemory_core::datetime::DateTime;
use bettermemory_core::git_read_budget::{Raise, ReadDeadline, Scope};
use bettermemory_core::githead::signature::{undetermined, First, Signature};
use bettermemory_core::origin_dates::memo::Timestamps;
use bettermemory_core::origin_gitfiles::attrs::AttributeFiles;
use bettermemory_core::origin_gitfiles::facts::Held;
use bettermemory_core::origin_gitfiles::walk::WalkFiles;
use bettermemory_core::origin_history::{author_instant, HistoryUnread, Key, Walked, WalkedValue};
use bettermemory_core::pyvalue::PyStr;
use bettermemory_core::response_drift::{
    Chain, DriftKey, DriftPass, DriftReads, HitArgs, LookupError, Memo, Resolve, Row,
};
use bettermemory_core::response_history::{HistoryReads, Read};
use bettermemory_core::verify::hit::HitDrift;
use bettermemory_core::verify::status::ResolvedCommitDrift;
use bettermemory_core::verify::weak::WeakError;
use bettermemory_core::verify_candidates::scan::UserHome;
use serde_json::{json, Value};
use std::cell::RefCell;
use std::rc::Rc;
use std::sync::Arc;

pub const SINCE: &str = "2024-01-02T03:04:05+00:00";

pub fn cps(s: &str) -> PyStr {
    s.chars().map(u32::from).collect()
}
pub fn text(p: &[u32]) -> String {
    p.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}
fn texts(v: &Value) -> Vec<PyStr> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|s| cps(s.as_str().unwrap()))
        .collect()
}
fn stamp(token: i64) -> Option<[i128; 5]> {
    Some([i128::from(token), 0, 0, 0, 0])
}
fn token(s: &Option<[i128; 5]>) -> i64 {
    s.unwrap()[0] as i64
}
pub fn attributes(token: i64) -> AttributeFiles {
    AttributeFiles {
        config: stamp(token),
        worktree_config: None,
        info_attributes: None,
        global_file: None,
        global_attributes: None,
    }
}
pub fn walk_files(token: i64) -> WalkFiles {
    WalkFiles {
        config: stamp(token),
        worktree_config: None,
        gitmodules: None,
        index: None,
        shallow: None,
    }
}
pub fn signature(token: &str) -> Signature {
    Signature {
        first: if token == "?" {
            undetermined()
        } else {
            First::GitDir(token.as_bytes().to_vec())
        },
        head: None,
        config: None,
        head_file: None,
        refs: None,
        packed: None,
        git_dir: None,
        git_work_tree: None,
        ceilings: None,
    }
}

pub fn drift_value(v: &HitDrift) -> Value {
    match v {
        None => Value::Null,
        Some(Err(why)) => json!(why.value()),
        Some(Ok(r)) => json!([r.count, r.basis]),
    }
}
pub fn key_value(k: &DriftKey) -> Value {
    json!([
        text(&k.root),
        text(&k.head),
        text(&k.cwd),
        k.anchors.iter().map(|a| text(a)).collect::<Vec<_>>(),
        k.claims.iter().map(|a| text(a)).collect::<Vec<_>>(),
        k.since.isoformat(),
        k.verified_head.as_deref().map(text),
        k.attributes
            .as_ref()
            .map(|(a, c)| json!([token(&a.config), token(&c[0])])),
        token(&k.walk_files.config),
    ])
}

/// The scripted state, the trace and the git call tallies.
pub struct Shared {
    pub st: Value,
    pub calls: Vec<Value>,
    pub failed: i64,
    pub timed_out: i64,
    pub res: Value,
}
pub type Cell = Rc<RefCell<Shared>>;

pub fn shared(state: &Value) -> Cell {
    Rc::new(RefCell::new(Shared {
        st: state.clone(),
        calls: Vec::new(),
        failed: 0,
        timed_out: 0,
        res: json!({}),
    }))
}

pub struct Reads(pub Cell);

impl Reads {
    fn effect(&self, scope: &Scope, name: &str, args: Vec<Value>) -> Read<(), &'static str> {
        let place = match &scope.root {
            Some((root, home)) if text(root) == "outside" && *home => json!("o"),
            Some((root, home)) => json!([text(root), home]),
            None => Value::Null,
        };
        let mut s = self.0.borrow_mut();
        let mut entry = vec![json!(name), place, json!(scope.active.is_some())];
        entry.extend(args);
        s.calls.push(Value::Array(entry));
        // A git call the effect stands for that failed (the kills' "fails";
        // the generator's state holds none).
        if let Some(n) =
            s.st.get("fails")
                .and_then(|f| f.get(name))
                .and_then(Value::as_i64)
        {
            s.failed += n;
        }
        if s.st["raise"] == name {
            s.st["raise"] = json!("");
            return Err(if s.st["error"] == "ReadDeadline" {
                Raise::Deadline(ReadDeadline)
            } else {
                Raise::Other("RuntimeError")
            });
        }
        Ok(())
    }
    /// An effect of [`DriftReads`], which no deadline reaches.
    fn plain(&self, scope: &Scope, name: &str, args: Vec<Value>) -> Result<(), &'static str> {
        self.effect(scope, name, args).map_err(|e| match e {
            Raise::Other(e) => e,
            Raise::Deadline(_) => panic!("no deadline reaches {name}"),
        })
    }
    fn st(&self, name: &str) -> Value {
        self.0.borrow().st[name].clone()
    }
    fn pair(&self, name: &str) -> Option<Key> {
        self.st(name).as_str().map(|h| (cps("/r"), cps(h)))
    }
    fn hold(&self, held: &mut Held, name: String, token: i64) {
        let n = name.chars().count() as i128;
        held.entry(name.into_bytes())
            .or_insert((n, i128::from(token)));
    }
}

impl HistoryReads for Reads {
    type Error = &'static str;
    fn files_pair(&mut self, scope: &mut Scope, cwd: &[u32]) -> Read<Option<Key>, &'static str> {
        self.effect(scope, "files", vec![json!(text(cwd))])?;
        Ok(self.pair("files"))
    }
    fn git_pair(&mut self, scope: &mut Scope, cwd: &[u32]) -> Read<Option<Key>, &'static str> {
        self.effect(scope, "git", vec![json!(text(cwd))])?;
        Ok(self.pair("git"))
    }
    fn discovery_moved(&mut self, scope: &mut Scope) -> Read<bool, &'static str> {
        self.effect(scope, "moved", vec![])?;
        Ok(self.st("moved").as_bool().unwrap())
    }
    fn located_head(
        &mut self,
        scope: &mut Scope,
        root: &[u32],
    ) -> Read<Option<PyStr>, &'static str> {
        self.effect(scope, "head", vec![json!(text(root))])?;
        Ok(self.st("head").as_str().map(cps))
    }
    fn dates(
        &mut self,
        scope: &mut Scope,
        cwd: &[u32],
        located: Option<&Key>,
    ) -> Read<Timestamps, &'static str> {
        let head = located.map(|k| text(&k.1));
        self.effect(scope, "dates", vec![json!(text(cwd)), json!(head)])?;
        let answer = self.st("dates");
        Ok(match &answer {
            Value::Null => Timestamps::Absent,
            Value::String(why) => Timestamps::Unread(HistoryUnread::from_value(why).unwrap()),
            days => Timestamps::Dates(Arc::new(
                days.as_array()
                    .unwrap()
                    .iter()
                    .map(|d| author_instant(i128::from(d.as_i64().unwrap()) * 86_400))
                    .collect(),
            )),
        })
    }
    fn note_unread(&mut self, scope: &mut Scope, why: HistoryUnread) -> Read<(), &'static str> {
        self.effect(scope, "note", vec![json!(why.value())])
    }
}

impl DriftReads for Reads {
    fn signature(
        &mut self,
        scope: &mut Scope,
        cwd: &[u32],
        owner: bool,
    ) -> Result<Signature, &'static str> {
        self.plain(scope, "sig", vec![json!(text(cwd)), json!(owner)])?;
        Ok(signature(self.st("sig").as_str().unwrap()))
    }
    fn attribute_files(
        &mut self,
        scope: &mut Scope,
        cwd: &[u32],
        root: &[u32],
        held: &mut Held,
    ) -> Result<Option<AttributeFiles>, &'static str> {
        self.plain(scope, "attrs", vec![json!(text(cwd)), json!(text(root))])?;
        let Some(t) = self.st("attrs").as_i64() else {
            return Ok(None);
        };
        self.hold(held, format!("{}/.git/info", text(root)), t);
        Ok(Some(attributes(t)))
    }
    fn attribute_chain(
        &mut self,
        scope: &mut Scope,
        root: &[u32],
        governed: &[PyStr],
        held: &mut Held,
    ) -> Result<Option<Chain>, &'static str> {
        let specs: Vec<String> = governed.iter().map(|g| text(g)).collect();
        self.plain(scope, "chain", vec![json!(text(root)), json!(specs)])?;
        let token = self
            .st("chains")
            .get(specs.join("|"))
            .cloned()
            .unwrap_or(json!(1));
        let Some(t) = token.as_i64() else {
            return Ok(None);
        };
        self.hold(held, format!("{}/{}", text(root), specs[0]), t);
        Ok(Some(vec![stamp(t)]))
    }
    fn walk_files(
        &mut self,
        scope: &mut Scope,
        root: &[u32],
        held: &mut Held,
    ) -> Result<Option<WalkFiles>, &'static str> {
        self.plain(scope, "walk", vec![json!(text(root))])?;
        let Some(t) = self.st("walk").as_i64() else {
            return Ok(None);
        };
        self.hold(held, text(root), t);
        Ok(Some(walk_files(t)))
    }
    fn files_held(
        &mut self,
        scope: &mut Scope,
        cwd: &[u32],
        root: &[u32],
        repository: Option<&AttributeFiles>,
        chains: &[(Vec<PyStr>, Option<Chain>)],
        walk: Option<&WalkFiles>,
        held: &Held,
    ) -> Result<bool, &'static str> {
        let chains: Vec<Value> = chains
            .iter()
            .map(|(specs, c)| {
                let specs: Vec<String> = specs.iter().map(|g| text(g)).collect();
                json!([specs, c.as_ref().map(|c| token(&c[0]))])
            })
            .collect();
        let dirs: Vec<Value> = held
            .iter()
            .map(|(k, (a, b))| json!([String::from_utf8(k.clone()).unwrap(), *a as i64, *b as i64]))
            .collect();
        self.plain(
            scope,
            "held",
            vec![
                json!(text(cwd)),
                json!(text(root)),
                json!(repository.map(|r| token(&r.config))),
                json!(chains),
                json!(walk.map(|w| token(&w.config))),
                json!(dirs),
            ],
        )?;
        Ok(self.st("held").as_bool().unwrap())
    }
    fn calls(&mut self) -> (i64, i64) {
        let s = self.0.borrow();
        (s.failed, s.timed_out)
    }
    fn now(&mut self) -> f64 {
        self.st("now").as_f64().unwrap()
    }
    /// The kills' "home" and "users"; the generator's state holds neither.
    fn home(&mut self) -> Option<PyStr> {
        self.st("home").as_str().map(cps)
    }
    fn user_home(&mut self, name: &[u32]) -> UserHome {
        match self.st("users").get(text(name)).and_then(Value::as_str) {
            Some(home) => UserHome::Found(cps(home)),
            None => UserHome::Unknown,
        }
    }
}

pub struct Admit(pub Cell);

impl DriftScope for Admit {
    fn prime(&mut self, _: &[String]) {}
    fn drift_counts(&mut self, id: &str) -> bool {
        let mut s = self.0.borrow_mut();
        s.calls.push(json!(["admit", id]));
        !s.st["deny"].as_array().unwrap().contains(&json!(id))
    }
}

/// The generator's `resolve_hit_drift`.
pub struct Resolver(pub Cell);

impl Resolve<&'static str> for Resolver {
    fn resolve(
        &mut self,
        hit: HitArgs<'_>,
        walked: &mut Walked,
        timestamps: &mut dyn FnMut() -> Result<Timestamps, &'static str>,
    ) -> Result<HitDrift, LookupError<&'static str>> {
        let res = {
            let mut s = self.0.borrow_mut();
            let entry = json!([
                "resolve",
                text(hit.cwd),
                hit.located.map(|k| text(&k.0)),
                hit.located.map(|k| text(&k.1)),
                hit.anchors.iter().map(|a| text(a)).collect::<Vec<_>>(),
                hit.claims
                    .iter()
                    .map(|c| text(&c.rel_path))
                    .collect::<Vec<_>>(),
                hit.since.isoformat(),
                hit.verified_head.map(text),
                walked.len(),
            ]);
            s.calls.push(entry);
            s.res.clone()
        };
        if res["ts"] == true {
            timestamps().map_err(LookupError::Read)?;
        }
        if res["walk"] == true {
            let key = (
                hit.located.map_or_else(|| cps("None"), |k| k.0.clone()),
                hit.verified_head.map_or_else(Vec::new, <[u32]>::to_vec),
                hit.located.map_or_else(Vec::new, |k| k.1.clone()),
            );
            walked.insert(key, WalkedValue::Failed(true));
        }
        {
            let mut s = self.0.borrow_mut();
            let fail = res["fail"].as_i64().unwrap_or(0);
            let tout = res["tout"].as_i64().unwrap_or(0);
            s.failed += fail + tout;
            s.timed_out += tout;
        }
        match res["raise"].as_str() {
            Some("ReadDeadline") => return Err(LookupError::Drift(WeakError::ReadDeadline)),
            Some(_) => return Err(LookupError::Read("RuntimeError")),
            None => {}
        }
        Ok(match res.get("v").unwrap_or(&json!(1)) {
            Value::Null => None,
            Value::String(why) => Some(Err(HistoryUnread::from_value(why).unwrap())),
            n => Some(Ok(ResolvedCommitDrift::new(n.as_i64().unwrap()))),
        })
    }
}

/// Replays one case: `[calls, results]`.
pub fn replay(case: &Value) -> Value {
    let cap = case["cap"].as_u64().unwrap() as usize;
    let memo = Memo::with_cap(cap);
    replay_on(case, &memo)
}

pub fn replay_on(case: &Value, memo: &Memo) -> Value {
    let cell = shared(&case["state"]);
    let mut reads = Reads(cell.clone());
    let mut resolver = Resolver(cell.clone());
    let outside = Some((cps("outside"), true));
    let mut scope = Scope {
        root: outside.clone(),
        ..Scope::default()
    };
    let mut pass: Option<DriftPass<'_, Admit>> = None;
    let mut results = Vec::new();
    for step in case["steps"].as_array().unwrap() {
        let kind = step[0].as_str().unwrap();
        let answer = match kind {
            "open" => {
                let cwd = step
                    .get(1)
                    .and_then(Value::as_str)
                    .unwrap_or_else(|| case["cwd"].as_str().unwrap());
                let home = step
                    .get(2)
                    .and_then(Value::as_bool)
                    .unwrap_or(case["at_home"].as_bool().unwrap());
                let opened = DriftPass::open(
                    &mut reads,
                    &mut scope,
                    cps(cwd),
                    home,
                    Admit(cell.clone()),
                    memo,
                );
                match opened {
                    Ok(p) => {
                        pass = Some(p);
                        json!(["return", null])
                    }
                    Err(e) => json!(["raise", raised(e)]),
                }
            }
            "set" => {
                cell.borrow_mut().st[step[1].as_str().unwrap()] = step[2].clone();
                json!(["return", null])
            }
            "lookup" => {
                let p = pass.as_mut().unwrap();
                let opts = &step[1];
                cell.borrow_mut().res = opts.get("res").cloned().unwrap_or(json!({}));
                let paths = opts
                    .get("paths")
                    .map_or_else(|| vec![cps("src/a.py")], texts);
                // The kills' "absent": explicit negative targets, which make
                // the paths literal.
                let absent = opts.get("absent").map(|a| {
                    texts(a)
                        .into_iter()
                        .enumerate()
                        .map(|(i, t)| (cps(&format!("t{i}")), Some(t)))
                        .collect()
                });
                let record = Record {
                    verified_paths: paths,
                    verified_absent_path_targets: absent,
                    ..Record::default()
                };
                let claims = opts.get("claims").map_or_else(Vec::new, texts);
                let body = cps(opts.get("body").and_then(Value::as_str).unwrap_or(""));
                let vh = opts.get("vh").and_then(Value::as_str).map(cps);
                let id = opts.get("id").and_then(Value::as_str).unwrap_or("m1");
                let row = Row {
                    id,
                    origin_repo: opts.get("repo").and_then(Value::as_bool).unwrap_or(true),
                    body: &body,
                    paths: &record,
                    claims: &claims,
                    verified_head: vh.as_deref(),
                };
                let since = match opts.get("since") {
                    None => Some(SINCE.to_owned()),
                    Some(Value::Null) => None,
                    Some(s) => Some(s.as_str().unwrap().to_owned()),
                };
                let since = since.map(|s| DateTime::from_isoformat(&cps(&s)).unwrap());
                let anchors = opts.get("anchors").filter(|a| !a.is_null()).map(texts);
                let resolve = opts.get("resolve").and_then(Value::as_bool).unwrap_or(true);
                match p.lookup(
                    &mut reads,
                    &mut scope,
                    &row,
                    since.as_ref(),
                    resolve,
                    anchors.as_deref(),
                    &mut resolver,
                ) {
                    Ok((found, drift)) => json!(["return", [found, drift_value(&drift)]]),
                    Err(LookupError::Read(e)) => json!(["raise", e]),
                    Err(LookupError::Anchors(_)) => json!(["raise", "Anchors"]),
                    Err(e) => panic!("unexpected {e:?}"),
                }
            }
            "store" => match pass.as_mut().unwrap().store(&mut reads, &mut scope) {
                Ok(b) => json!(["return", b]),
                Err(e) => json!(["raise", raised(e)]),
            },
            "obs" => {
                let p = pass.as_ref().unwrap();
                let [resolved, timed_out] = p.kept();
                let items = |kept: Vec<(DriftKey, HitDrift)>| {
                    kept.iter()
                        .map(|(k, v)| json!([key_value(k), drift_value(v)]))
                        .collect::<Vec<_>>()
                };
                json!([
                    "return",
                    [
                        items(resolved),
                        items(timed_out),
                        memo.memo_keys().iter().map(key_value).collect::<Vec<_>>(),
                        memo.timed_out_keys()
                            .iter()
                            .map(key_value)
                            .collect::<Vec<_>>(),
                        p.walked().len(),
                    ]
                ])
            }
            other => panic!("unknown step {other}"),
        };
        assert_eq!(scope.root, outside);
        assert!(scope.active.is_none());
        let unstored = pass.as_ref().map_or(0, |p| p.unstored());
        results.push(json!([answer, cell.borrow().calls.len(), unstored]));
    }
    let calls = cell.borrow().calls.clone();
    json!([calls, results])
}

fn raised(e: Raise<&'static str>) -> &'static str {
    match e {
        Raise::Deadline(_) => "ReadDeadline",
        Raise::Other(e) => e,
    }
}
