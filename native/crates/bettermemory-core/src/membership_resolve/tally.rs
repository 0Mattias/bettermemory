//! The tally, the checkout at the caller's path, independent strength and tie wording.
use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::rc::Rc;

use super::{HistoryOrder, NativeHistory, TallyHistory, TallyModel};
use crate::capture::{
    location::{Location, Sameness},
    paths::norm,
    Kind,
};
use crate::membership::history::ordered_members;
use crate::membership::{column, shared_heal::token_error, str_of, vocab::*, Query, Result2};
use crate::membership_records::{
    identity::WALK_LIMIT,
    records::{Checkout, HistoryReader, Specific, PRIMARY, UNRESOLVED},
};
use crate::membership_rows::{root_file, Tally};
use crate::printable::quoted;
use crate::pyvalue::{PyStr, PyValue};
use crate::session::OrderedMap;
use crate::unicode::cps;

type Codes = BTreeSet<&'static str>;
// The insertion-order slots and hash index share each owned project ID.
type Candidates = OrderedMap<Rc<PyStr>, Codes>;
fn add(candidates: &mut Candidates, project: Rc<PyStr>, code: &'static str) {
    candidates.get_or_insert_default(project).insert(code);
}
fn has_any(codes: &Codes, of: &[&str]) -> bool {
    of.iter().any(|v| codes.contains(v))
}
fn in_query<'a>(column: &str, values: impl IntoIterator<Item = &'a PyStr>) -> Query {
    Query {
        column_in: Some((
            cps(column),
            values.into_iter().cloned().map(PyValue::Str).collect(),
        )),
        ..Query::default()
    }
}
fn row_id<'a, E>(row: &'a PyValue, key: &str) -> Result2<Cow<'a, [u32]>, E> {
    match column(row, key)? {
        PyValue::Str(text) => Ok(Cow::Borrowed(text)),
        other => str_of(other).map(Cow::Owned),
    }
}

/// A live provider's view reads can fail. The model itself remains borrowed from
/// the caller's transaction, including any liveness cache it already holds.
pub trait TallyViews<M: TallyModel> {
    fn view_state(&mut self, model: &mut M, checkout: &Checkout) -> Result2<PyStr, M::Error>;
    fn live_view(&mut self, model: &mut M, checkout: &Checkout)
        -> Result2<Option<PyStr>, M::Error>;
}

pub struct TallyReads<'a, M: TallyModel> {
    pub history: Option<&'a dyn TallyHistory<M::Error>>,
    pub remote_order: &'a [PyStr],
    pub history_order: &'a HistoryOrder,
    pub views: Option<&'a mut dyn TallyViews<M>>,
}

impl<M: TallyModel> TallyReads<'_, M> {
    fn view_state(&mut self, m: &mut M, k: &Checkout) -> Result2<PyStr, M::Error> {
        match &mut self.views {
            Some(v) => v.view_state(m, k),
            None => m.view_state(k),
        }
    }
    fn live_view(&mut self, m: &mut M, k: &Checkout) -> Result2<Option<PyStr>, M::Error> {
        match &mut self.views {
            Some(v) => v.live_view(m, k),
            None => m.live_view(k),
        }
    }
}

/// Tally for a native Location's stored remote set.
pub fn tally<M: TallyModel>(
    model: &mut M,
    loc: &Location,
    history: Option<&dyn HistoryReader>,
    tenant: Option<&[u32]>,
    exclude: &[PyStr],
) -> Result2<Tally, M::Error> {
    tally_in_order(
        model,
        loc,
        history,
        tenant,
        exclude,
        &[],
        &HistoryOrder::default(),
    )
}

/// Tally with the source remote set's iteration order. Queries over every remote
/// are sorted. Individual local path reads retain the supplied order, which can
/// change boundary messages and error order.
pub fn tally_in_order<M: TallyModel>(
    model: &mut M,
    loc: &Location,
    history: Option<&dyn HistoryReader>,
    tenant: Option<&[u32]>,
    exclude: &[PyStr],
    remote_order: &[PyStr],
    order: &HistoryOrder,
) -> Result2<Tally, M::Error> {
    let native = history.map(NativeHistory);
    tally_with_reads(
        model,
        loc,
        tenant,
        exclude,
        &mut TallyReads {
            history: native.as_ref().map(|h| h as &dyn TallyHistory<M::Error>),
            remote_order,
            history_order: order,
            views: None,
        },
    )
}

/// Run the full tally against fallible history and view providers.
pub fn tally_with_reads<M: TallyModel>(
    model: &mut M,
    loc: &Location,
    tenant: Option<&[u32]>,
    exclude: &[PyStr],
    reads: &mut TallyReads<'_, M>,
) -> Result2<Tally, M::Error> {
    let history = reads.history;
    let order = reads.history_order;
    let remote_order = reads.remote_order;
    let mut tally = Tally {
        held: model.held(loc, history, exclude, order)?,
        ..Tally::default()
    };
    let held: HashSet<PyStr> = tally.held.iter().map(|(id, _)| id.clone()).collect();
    let mut candidates = Candidates::default();
    for (pid, _) in &tally.held {
        add(&mut candidates, Rc::new(pid.clone()), H);
    }
    if held.is_empty() && loc.born() {
        for (pid, _) in model.rewritten(loc, history, exclude, order)? {
            add(&mut candidates, Rc::new(pid), HF);
        }
    }
    let excluded: HashSet<&[u32]> = exclude.iter().map(Vec::as_slice).collect();
    if !loc.remote_keys.is_empty() {
        for row in model.rows("project_remotes", &in_query("remote_key", &loc.remote_keys))? {
            let pid = model.canon(&row_id(&row, "project")?)?;
            if !excluded.contains(pid.as_slice()) {
                add(&mut candidates, Rc::new(pid), R);
            }
        }
        let local = cps("local:");
        for key in ordered_members(&loc.remote_keys, remote_order) {
            if let Some(path) = key.strip_prefix(local.as_slice()) {
                let filter = Query {
                    where_: Some(vec![(cps("path"), PyValue::Str(norm(path)))]),
                    ..Query::default()
                };
                for row in model.rows("checkout_views", &filter)? {
                    let seen = model.checkout(&row_id(&row, "checkout")?)?;
                    if seen.kind == cps(UNRESOLVED) {
                        continue;
                    }
                    let pid = model.canon(&seen.project)?;
                    if !excluded.contains(pid.as_slice()) {
                        add(&mut candidates, Rc::new(pid), R);
                    }
                }
            }
        }
    }
    if loc.shallow {
        for row in model.rows(
            "declared_remotes",
            &in_query("remote_key", &loc.remote_keys),
        )? {
            let pid = model.canon(&row_id(&row, "project")?)?;
            if !excluded.contains(pid.as_slice())
                && candidates
                    .get_borrowed(&pid)
                    .is_some_and(|codes| codes.contains(R))
            {
                add(&mut candidates, Rc::new(pid), D);
            }
        }
    }
    let bound = match root_file(loc) {
        Some(file) => model.file_binding(&file.id)?,
        None => None,
    };
    if let Some(pid) = bound.as_ref().filter(|p| !excluded.contains(p.as_slice())) {
        add(&mut candidates, Rc::new(pid.clone()), F);
    }
    let carriers = match loc.token.as_deref().filter(|token| !token.is_empty()) {
        Some(token) => model.checkouts_with_token(token)?,
        None => Vec::new(),
    };
    for k in &carriers {
        let pid = model.canon(&k.project)?;
        if !excluded.contains(pid.as_slice()) {
            add(&mut candidates, Rc::new(pid), T);
        }
    }
    if loc.kind == Some(Kind::Primary) && loc.born() {
        at_the_path(
            model,
            loc,
            &held,
            &excluded,
            &mut candidates,
            &mut tally,
            reads,
        )?;
    }
    if let Some(reader) = history.filter(|_| loc.born()) {
        blob_witnesses(model, reader, &mut candidates)?;
    }
    let projects: Vec<_> = candidates.keys().cloned().collect();
    for pid in projects {
        if model.project_tenant(&pid)?.as_deref() != tenant {
            candidates.remove(&pid);
            tally.dropped.push(Rc::unwrap_or_clone(pid));
        }
    }
    token_rules(
        model,
        loc,
        &carriers,
        &held,
        &mut candidates,
        &mut tally,
        reads,
    )?;
    if let Some(pid) = bound.as_ref().filter(|pid| {
        candidates
            .get_borrowed(*pid)
            .is_some_and(|c| !has_any(c, &HISTORY))
    }) {
        let others: Vec<_> = candidates
            .iter()
            .filter(|(q, c)| q.as_ref() != pid && has_any(c, &[H, HF]))
            .map(|(q, _)| q)
            .collect();
        if !others.is_empty() {
            let mut line = cps("its project file names ");
            line.extend(model.name(pid)?);
            line.extend(cps(" but it holds the commits of "));
            for (i, q) in others.iter().enumerate() {
                if i > 0 {
                    line.extend(cps(", "));
                }
                line.extend(model.name(q)?);
            }
            tally
                .contradictions
                .push((cps("file_names_other_history"), pid.clone(), line));
        }
    }
    tally.cands = candidates
        .into_entries()
        .map(|(p, c)| (Rc::unwrap_or_clone(p), c.into_iter().map(cps).collect()))
        .collect();
    Ok(tally)
}

fn at_the_path<M: TallyModel>(
    model: &mut M,
    loc: &Location,
    held: &HashSet<PyStr>,
    excluded: &HashSet<&[u32]>,
    candidates: &mut Candidates,
    tally: &mut Tally,
    reads: &mut TallyReads<'_, M>,
) -> Result2<(), M::Error> {
    let path = loc
        .root
        .as_deref()
        .filter(|v| !v.is_empty())
        .unwrap_or(&loc.cwd);
    let mut rekey = OrderedMap::default();
    let q = Query {
        where_: Some(vec![
            (cps("machine"), PyValue::Str(loc.machine.clone())),
            (cps("path"), PyValue::Str(norm(path))),
        ]),
        ..Query::default()
    };
    for row in model.rows("checkout_views", &q)? {
        let k = model.checkout(&row_id(&row, "checkout")?)?;
        if k.kind != cps(PRIMARY) || k.machine != loc.machine {
            continue;
        }
        if model.retired_into(&k)?.is_some_and(|id| !id.is_empty()) {
            continue;
        }
        if k.object_key == loc.object_key
            && k.object_key
                .as_ref()
                .is_none_or(|key| key.compare(loc.object_key.as_ref()) != Sameness::Unproven)
        {
            continue;
        }
        let pid = model.canon(&k.project)?;
        if excluded.contains(pid.as_slice()) || !held.contains(&pid) {
            continue;
        }
        if k.token_refutes(loc.token.as_deref())
            .map_err(|e| token_error(&e))?
            && !model.view_made(&k, loc.token.as_ref())?
        {
            continue;
        }
        if reads.live_view(model, &k)?.is_none() {
            add(candidates, Rc::new(pid.clone()), B);
            rekey.insert(pid, k.id);
        }
    }
    tally.rekey = rekey.iter().map(|(p, k)| (p.clone(), k.clone())).collect();
    Ok(())
}

fn blob_witnesses<M: TallyModel>(
    model: &mut M,
    reader: &dyn TallyHistory<M::Error>,
    candidates: &mut Candidates,
) -> Result2<(), M::Error> {
    let mut index = BTreeSet::new();
    let mut per_project = Vec::new();
    for (pid, _) in candidates
        .iter()
        .filter(|(_, c)| has_any(c, &[F, R]) && !has_any(c, &[H, HF]))
    {
        let mut per_witness: BTreeMap<PyStr, BTreeSet<PyStr>> = BTreeMap::new();
        let members = model.members(pid)?;
        for row in model.rows("project_blobs", &in_query("project", &members))? {
            let blob = row_id(&row, "blob")?.into_owned();
            index.insert(blob.clone());
            per_witness
                .entry(row_id(&row, "witness")?.into_owned())
                .or_default()
                .insert(blob);
        }
        per_project.push((pid.clone(), per_witness));
    }
    if !index.is_empty() {
        let got = model.holds_objects(reader, &index.into_iter().collect::<Vec<_>>())?;
        for (pid, per) in per_project {
            if per.values().any(|blobs| {
                let hits = blobs.intersection(&got).count();
                hits >= 2 && hits >= blobs.len().div_ceil(2)
            }) {
                add(candidates, pid, HB);
            }
        }
    }
    Ok(())
}

fn token_rules<M: TallyModel>(
    model: &mut M,
    loc: &Location,
    carriers: &[Checkout],
    held: &HashSet<PyStr>,
    candidates: &mut Candidates,
    tally: &mut Tally,
    reads: &mut TallyReads<'_, M>,
) -> Result2<(), M::Error> {
    let projects: Vec<_> = candidates
        .iter()
        .filter(|(_, c)| c.contains(T))
        .map(|(p, _)| p.clone())
        .collect();
    let history_count = candidates
        .iter()
        .filter(|(_, c)| has_any(c, &HISTORY))
        .count();
    for pid in projects {
        let mut mine = Vec::new();
        for k in carriers {
            if model.canon(&k.project)? == *pid {
                mine.push(k);
            }
        }
        let mut first_live = None;
        for k in mine {
            if k.machine == loc.machine && reads.view_state(model, k)? == cps(LIVE) {
                first_live.get_or_insert(k);
            }
        }
        let shares = held.contains(pid.as_ref());
        let mut codes = candidates.get(&pid).expect("token project").clone();
        let others_hold = history_count > usize::from(has_any(&codes, &HISTORY));
        if loc.born()
            && !shares
            && !has_any(&codes, &[HF, HB])
            && (model.has_witnesses(&pid)? || others_hold)
        {
            codes.remove(T);
            if codes.is_empty() {
                candidates.remove(&pid);
            } else {
                candidates.insert(pid.clone(), codes);
            }
            tally.superseded.push(Rc::unwrap_or_clone(pid));
        } else if let Some(k) = first_live.filter(|_| !shares) {
            let mut line = cps("carries the checkout token of ");
            match k.newest_view() {
                Some(v) => line.extend(quoted(&v.path)),
                None => line.extend_from_slice(&k.id),
            }
            line.extend(cps(", a live checkout with no commit in common"));
            tally.contradictions.push((
                cps("token_shared_no_commit"),
                Rc::unwrap_or_clone(pid),
                line,
            ));
        }
    }
    Ok(())
}

/// Independent witness strength. Unknown codes count independently, repeated codes once.
pub fn strength<M: TallyModel>(
    model: &mut M,
    loc: &Location,
    project: &[u32],
    codes: &[PyStr],
) -> Result2<usize, M::Error> {
    let history = cps(H);
    let mut codes: BTreeSet<&[u32]> = codes.iter().map(Vec::as_slice).collect();
    codes.remove(cps(C).as_slice());
    if ![H, HF, R].iter().any(|v| codes.contains(cps(v).as_slice())) {
        codes.remove(cps(HB).as_slice());
    }
    let rewritten = codes.remove(cps(HF).as_slice());
    let blobs = codes.remove(cps(HB).as_slice());
    if rewritten || blobs {
        codes.insert(&history);
    }
    let mut count = codes.len();
    if codes.contains(cps(F).as_slice())
        && codes.contains(cps(R).as_slice())
        && !codes.contains(history.as_slice())
        && loc.born()
        && !loc.shallow
    {
        count -= 1;
    }
    if codes.len() == 1
        && codes.contains(cps(T).as_slice())
        && !loc.born()
        && !model.has_witnesses(project)?
    {
        count = 2;
    }
    Ok(count)
}

/// Tied witnesses and, only for a bounded walk, the search limit.
pub fn unordered(spec: &Specific, names: &[PyStr]) -> PyStr {
    let sep = cps(if names.len() < 3 { " and " } else { ", " });
    let mut out = names.join(sep.as_slice());
    out.extend(cps(&format!(" each hold a witness commit that no other of the tied projects' {} witness commits was found to descend from",spec.held)));
    if spec.bounded {
        out.extend(cps(&format!(
            " within the walk's bound (at most {WALK_LIMIT} commits read)"
        )));
    }
    out
}
