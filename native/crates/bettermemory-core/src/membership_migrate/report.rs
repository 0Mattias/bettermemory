//! The migration's report (`membership_migrate.py` 132-311): what a run
//! did, as `to_dict` and as the lines `bettermemory membership migrate`
//! prints.

use crate::membership_lines::named;
use crate::printable::quoted_into;
use crate::pyvalue::{PyStr, PyValue};

/// `RootDecision`: one recorded root and what was homed from it.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct RootDecision {
    pub path: PyStr,
    pub state: PyStr,
    pub project: PyStr,
    pub project_name: PyStr,
    pub checkout: Option<PyStr>,
    pub rule: PyStr,
    pub memories: i64,
    pub episodes: i64,
    pub tombstones: i64,
    pub recorded: i64,
    /// A mapping in insertion order.
    pub held: Vec<(PyStr, i64)>,
}

/// `RootlessHome`: a record with no recorded root, homed by its cwd or
/// by a remote several projects carry.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct RootlessHome {
    pub record: PyStr,
    pub kind: PyStr,
    pub cwd: PyStr,
    pub root: PyStr,
    pub project: PyStr,
    pub checkout: Option<PyStr>,
    pub rule: PyStr,
    pub carriers: Vec<PyStr>,
}

/// `LegacyQuestion`: a `legacy` question the run opened.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct LegacyQuestion {
    pub proposal: PyStr,
    pub checkout: PyStr,
    pub at: PyStr,
    pub legacy_project: PyStr,
    pub root: PyStr,
    pub line: PyStr,
}

/// `MigrationTotals`: the rows the run wrote, in field order.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct MigrationTotals {
    pub projects: i64,
    pub checkouts: i64,
    pub views: i64,
    pub memory_homes: i64,
    pub tombstone_homes: i64,
    pub episode_homes: i64,
    pub witnesses: i64,
    pub proposals: i64,
    pub log_rows: i64,
    pub memories_rewritten: i64,
    pub tombstones_rewritten: i64,
    pub episodes_rewritten: i64,
}

/// `MigrationReport`: what a run did or, dry, would do.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct MigrationReport {
    pub dry_run: bool,
    pub kept: PyStr,
    pub machine: PyStr,
    pub roots: Vec<RootDecision>,
    pub rootless: Vec<RootlessHome>,
    pub proposals: Vec<LegacyQuestion>,
    pub totals: MigrationTotals,
    /// A mapping in insertion order.
    pub homes_by_rule: Vec<(PyStr, i64)>,
    pub remint: Vec<PyStr>,
}

fn s(t: &[u32]) -> PyValue {
    PyValue::Str(t.to_vec())
}

fn n(v: i64) -> PyValue {
    PyValue::int(i128::from(v))
}

fn opt(t: &Option<PyStr>) -> PyValue {
    t.as_deref().map_or(PyValue::None, s)
}

fn dict(items: Vec<(&str, PyValue)>) -> PyValue {
    PyValue::Dict(
        items
            .into_iter()
            .map(|(k, v)| (PyValue::str(k), v))
            .collect(),
    )
}

fn mapping(m: &[(PyStr, i64)]) -> PyValue {
    PyValue::Dict(m.iter().map(|(k, v)| (s(k), n(*v))).collect())
}

impl RootDecision {
    /// `dataclasses.asdict(self)`.
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("path", s(&self.path)),
            ("state", s(&self.state)),
            ("project", s(&self.project)),
            ("project_name", s(&self.project_name)),
            ("checkout", opt(&self.checkout)),
            ("rule", s(&self.rule)),
            ("memories", n(self.memories)),
            ("episodes", n(self.episodes)),
            ("tombstones", n(self.tombstones)),
            ("recorded", n(self.recorded)),
            ("held", mapping(&self.held)),
        ])
    }
}

impl RootlessHome {
    /// `dataclasses.asdict(self)`: `carriers` stays a tuple.
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("record", s(&self.record)),
            ("kind", s(&self.kind)),
            ("cwd", s(&self.cwd)),
            ("root", s(&self.root)),
            ("project", s(&self.project)),
            ("checkout", opt(&self.checkout)),
            ("rule", s(&self.rule)),
            (
                "carriers",
                PyValue::Tuple(self.carriers.iter().map(|c| s(c)).collect()),
            ),
        ])
    }
}

impl LegacyQuestion {
    /// `dataclasses.asdict(self)`.
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("proposal", s(&self.proposal)),
            ("checkout", s(&self.checkout)),
            ("at", s(&self.at)),
            ("legacy_project", s(&self.legacy_project)),
            ("root", s(&self.root)),
            ("line", s(&self.line)),
        ])
    }
}

impl MigrationTotals {
    /// `to_dict()`: `dataclasses.asdict`, in field order.
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("projects", n(self.projects)),
            ("checkouts", n(self.checkouts)),
            ("views", n(self.views)),
            ("memory_homes", n(self.memory_homes)),
            ("tombstone_homes", n(self.tombstone_homes)),
            ("episode_homes", n(self.episode_homes)),
            ("witnesses", n(self.witnesses)),
            ("proposals", n(self.proposals)),
            ("log_rows", n(self.log_rows)),
            ("memories_rewritten", n(self.memories_rewritten)),
            ("tombstones_rewritten", n(self.tombstones_rewritten)),
            ("episodes_rewritten", n(self.episodes_rewritten)),
        ])
    }
}

/// A piece of a line: ASCII text, a text as it is, a count, or a text
/// through `quoted` ([`Q`]).
trait Piece {
    fn put(&self, out: &mut PyStr);
}

impl Piece for &str {
    fn put(&self, out: &mut PyStr) {
        out.extend(self.chars().map(u32::from));
    }
}

impl Piece for &PyStr {
    fn put(&self, out: &mut PyStr) {
        out.extend_from_slice(self);
    }
}

impl Piece for i64 {
    fn put(&self, out: &mut PyStr) {
        self.to_string().as_str().put(out);
    }
}

impl Piece for usize {
    fn put(&self, out: &mut PyStr) {
        self.to_string().as_str().put(out);
    }
}

/// `quoted(text)` as a piece.
struct Q<'a>(&'a [u32]);

impl Piece for Q<'_> {
    fn put(&self, out: &mut PyStr) {
        quoted_into(self.0, out);
    }
}

/// `f"checkout {c}" if c else "no checkout"`.
struct Where<'a>(&'a Option<PyStr>);

impl Piece for Where<'_> {
    fn put(&self, out: &mut PyStr) {
        match self.0 {
            Some(c) if !c.is_empty() => {
                "checkout ".put(out);
                out.extend_from_slice(c);
            }
            _ => "no checkout".put(out),
        }
    }
}

/// The pieces, joined into one line.
macro_rules! line {
    ($($p:expr),* $(,)?) => {{
        let mut out: PyStr = Vec::new();
        $(Piece::put(&$p, &mut out);)*
        out
    }};
}

/// `", ".join(texts)`, each text with `f` applied.
fn joined<T>(items: &[T], f: impl Fn(&T, &mut PyStr)) -> PyStr {
    let mut out = Vec::new();
    for (i, item) in items.iter().enumerate() {
        if i > 0 {
            ", ".put(&mut out);
        }
        f(item, &mut out);
    }
    out
}

impl MigrationReport {
    /// `homed`: the home rows the run wrote.
    pub fn homed(&self) -> i128 {
        let t = &self.totals;
        i128::from(t.memory_homes) + i128::from(t.tombstone_homes) + i128::from(t.episode_homes)
    }

    /// `to_dict()`.
    pub fn to_dict(&self) -> PyValue {
        dict(vec![
            ("dry_run", PyValue::Bool(self.dry_run)),
            ("kept", s(&self.kept)),
            ("machine", s(&self.machine)),
            (
                "roots",
                PyValue::List(self.roots.iter().map(RootDecision::to_dict).collect()),
            ),
            (
                "rootless",
                PyValue::List(self.rootless.iter().map(RootlessHome::to_dict).collect()),
            ),
            (
                "proposals",
                PyValue::List(self.proposals.iter().map(LegacyQuestion::to_dict).collect()),
            ),
            ("totals", self.totals.to_dict()),
            ("homes_by_rule", mapping(&self.homes_by_rule)),
            (
                "remint",
                PyValue::List(self.remint.iter().map(|r| s(r)).collect()),
            ),
        ])
    }

    /// `lines()`: the report as `bettermemory membership migrate` prints
    /// it.
    pub fn lines(&self) -> Vec<PyStr> {
        let head = "bettermemory membership migrate";
        let mut out = vec![if self.dry_run {
            line![head, " (dry run: ", &self.kept, ")"]
        } else {
            line![head, ": ", &self.kept]
        }];
        out.push(line!["roots (", self.roots.len(), "):"]);
        for r in &self.roots {
            let mut l = line!["  ", Q(&r.path), ": ", &r.state, ", project "];
            l.extend(named(&r.project_name, &r.project));
            l.extend(line![", ", Where(&r.checkout), ", rule ", &r.rule, "; "]);
            l.extend(line![r.memories, " memories, ", r.episodes, " episodes, "]);
            l.extend(line![r.tombstones, " removed memories homed"]);
            if !r.held.is_empty() {
                let mut held: Vec<&(PyStr, i64)> = r.held.iter().collect();
                held.sort();
                l.extend(line!["; held of its ", r.recorded, " recorded commits: "]);
                l.extend(joined(&held, |(k, n), o| o.extend(line![k, " ", *n])));
            }
            out.push(l);
        }
        out.push(line![
            "legacy questions opened (",
            self.proposals.len(),
            "):"
        ]);
        for p in &self.proposals {
            out.push(line![
                "  ",
                &p.proposal,
                " in checkout ",
                &p.checkout,
                " at ",
                Q(&p.at),
                ": ",
                &p.line
            ]);
        }
        out.push(line![
            "records with no root homed by their cwd, or by a remote several projects carry (",
            self.rootless.len(),
            "):"
        ]);
        for x in &self.rootless {
            let mut l = line!["  ", &x.record, " (", &x.kind, ") at ", Q(&x.cwd), ": in "];
            l.extend(line![
                Q(&x.root),
                ", project ",
                &x.project,
                ", ",
                Where(&x.checkout)
            ]);
            l.extend(line![", rule ", &x.rule]);
            if !x.carriers.is_empty() {
                l.extend(line!["; carried by "]);
                l.extend(joined(&x.carriers, |c, o| o.extend_from_slice(c)));
            }
            out.push(l);
        }
        let t = &self.totals;
        let mut l = line![
            "totals: ",
            t.projects,
            " projects, ",
            t.checkouts,
            " checkouts, "
        ];
        l.extend(line![
            t.views,
            " views, ",
            t.memory_homes,
            " memory homes, "
        ]);
        l.extend(line![t.tombstone_homes, " removed-memory homes, "]);
        l.extend(line![
            t.episode_homes,
            " episode homes, ",
            t.witnesses,
            " witnesses, "
        ]);
        l.extend(line![t.proposals, " questions, ", t.log_rows, " log rows"]);
        out.push(l);
        out.push(line![
            "rows rewritten: ",
            t.memories_rewritten,
            " memories, ",
            t.tombstones_rewritten,
            " removed memories, ",
            t.episodes_rewritten,
            " episodes"
        ]);
        out.push(line!["homes by rule:"]);
        let mut rules: Vec<&(PyStr, i64)> = self.homes_by_rule.iter().collect();
        rules.sort();
        for (rule, n) in rules {
            out.push(line!["  ", rule, ": ", *n]);
        }
        if !self.remint.is_empty() {
            out.push(line![
                "tokens due at the next sighting (",
                self.remint.len(),
                "):"
            ]);
        }
        for k in &self.remint {
            out.push(line![
                "  checkout ",
                k,
                ": carries the token of another line (history disagrees); the migration writes no token"
            ]);
        }
        out
    }
}
