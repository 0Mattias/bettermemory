//! `verify.py`'s claim-level `weak` tier, `_weak_tier_evaluation` (lines
//! 455-576 at v9), with every git read behind [`DriftGit`] and the
//! spelling choice of `verify_spelling.spelled` behind [`Spelling`].
//!
//! `origin.commit_patch_stream`, `origin.resolve_repo_pathspecs`,
//! `origin_dates`' two reads, `origin_patch.too_large` and
//! `local_patch_window`, `verify_layout.literal_floor` and
//! `verify_spelling.spelled` are not built here: each is one method of
//! [`DriftGit`], None where the reference answers None.

use std::collections::BTreeSet;

use crate::claim_drift::{build_binding_index, claim_level_drift, Index, LiteralLines};
use crate::claims::{strip, Claim, IntTooLong, Kind};
use crate::datetime::DateTime;
use crate::pysyntax::literal::LiteralError;
use crate::pyvalue::PyStr;
use crate::verify::status::index_paths;

/// `origin.MAX_PATCH_STREAM_COMMITS`.
pub const MAX_PATCH_STREAM_COMMITS: usize = 256;

/// A set of commits or paths, as the reference's `set[str]`; ordered so
/// that a reader sees one order (the reference's sets have none it reads).
pub type Set = BTreeSet<PyStr>;

/// What stops an answer: what the reference raises (`IntTooLong`, the
/// `int()` ValueError of `build_binding_index`; `NaiveAware`, the
/// TypeError of ordering a naive author date against the aware stamp), or
/// a literal the crate cannot read yet (`Literal`, never `Invalid`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum WeakError {
    IntTooLong,
    NaiveAware,
    Literal(LiteralError),
}

impl From<IntTooLong> for WeakError {
    fn from(_: IntTooLong) -> Self {
        WeakError::IntTooLong
    }
}

impl From<LiteralError> for WeakError {
    fn from(e: LiteralError) -> Self {
        WeakError::Literal(e)
    }
}

/// `verify_spelling.Spelled`: the spellings each spec is read under.
pub trait Spelling {
    /// `Spelled.of(spec)`: its own spelling first.
    fn of(&self, spec: &[u32]) -> Vec<PyStr>;
    /// `Spelled.declined`.
    fn declined(&self) -> bool;
    /// `Spelled.names(specs)`: every spelling, in order, once each.
    fn names(&self, specs: &[PyStr]) -> Vec<PyStr> {
        let mut seen = BTreeSet::new();
        let mut out = Vec::new();
        for spec in specs {
            for name in self.of(spec) {
                if seen.insert(name.clone()) {
                    out.push(name);
                }
            }
        }
        out
    }
}

/// `Spelled()`: each spec under its own spelling, never declined.
pub struct Own;

impl Spelling for Own {
    fn of(&self, spec: &[u32]) -> Vec<PyStr> {
        vec![spec.to_vec()]
    }
    fn declined(&self) -> bool {
        false
    }
}

/// `origin_patch.PatchWindow`.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct PatchWindow {
    pub stream: PyStr,
    pub counted: Set,
    pub touched: Set,
    pub deleted: Set,
}

impl PatchWindow {
    /// `PatchWindow.mark(index)`.
    pub fn mark(&self, index: &mut Index) {
        index.files.extend(self.touched.iter().cloned());
        index.deleted.extend(self.deleted.iter().cloned());
    }
}

/// `verify_layout.LiteralFloor`.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct LiteralFloor {
    pub paths: Set,
    pub commits: Set,
    pub listed: Set,
}

/// The `cwd` and `toplevel` every git read is handed, as code points.
#[derive(Clone, Copy, Debug)]
pub struct Place<'a> {
    pub cwd: &'a [u32],
    pub toplevel: Option<&'a [u32]>,
}

/// Every git read the narrowing makes, one method per reference call.
pub trait DriftGit {
    /// `origin.resolve_repo_pathspecs(cwd, paths, toplevel=)`.
    fn resolve_repo_pathspecs(&self, at: Place, paths: &[PyStr]) -> Option<Vec<PyStr>>;
    /// `origin_dates.commit_author_sha_pairs_touching_pathspecs`.
    fn commit_author_sha_pairs_touching_pathspecs(
        &self,
        at: Place,
        pathspecs: &[PyStr],
    ) -> Option<Vec<(DateTime, PyStr)>>;
    /// `origin_dates.paths_in_history`.
    fn paths_in_history(&self, at: Place, pathspecs: &[PyStr]) -> Option<bool>;
    /// `origin_patch.too_large`.
    fn too_large(&self, at: Place, shas: &[PyStr], pathspecs: &[PyStr]) -> bool;
    /// `origin.commit_patch_stream`.
    fn commit_patch_stream(&self, at: Place, shas: &[PyStr], pathspecs: &[PyStr]) -> Option<PyStr>;
    /// `origin_patch.local_patch_window`.
    fn local_patch_window(
        &self,
        at: Place,
        shas: &[PyStr],
        pathspecs: &[PyStr],
    ) -> Option<PatchWindow>;
    /// `verify_layout.literal_floor`.
    #[allow(clippy::too_many_arguments)]
    fn literal_floor(
        &self,
        at: Place,
        shas: &[PyStr],
        specs: &[PyStr],
        pathspecs: &[PyStr],
        claims: &[Claim],
        spell: &dyn Spelling,
    ) -> Option<LiteralFloor>;
    /// `verify_spelling.spelled(cwd, specs, toplevel=, anchor=, since=)`.
    fn spelled(
        &self,
        at: Place,
        specs: &[PyStr],
        anchor: Option<&[u32]>,
        since: Option<&DateTime>,
    ) -> Box<dyn Spelling>;
}

/// `verify_spelling.literal_pathspecs`, which reads the environment.
pub type LiteralPathspecs<'a> = &'a dyn Fn(&[PyStr]) -> Vec<PyStr>;

/// `_weak_tier_evaluation`: the drifted renders in claim order and the
/// implicated commits of the window (None where the caller counts every
/// commit of the window).
#[allow(clippy::too_many_arguments)]
pub fn weak_tier_evaluation(
    git: &dyn DriftGit,
    at: Place,
    shas: &[PyStr],
    specs: &[PyStr],
    claims: &[Claim],
    spell: Option<&dyn Spelling>,
    literal_pathspecs: LiteralPathspecs,
    lit: &dyn LiteralLines,
) -> Result<(Vec<PyStr>, Option<Set>), WeakError> {
    let declined = Ok((Vec::new(), None));
    if shas.len() > MAX_PATCH_STREAM_COMMITS {
        return declined;
    }
    let spell = spell.unwrap_or(&Own);
    let pathspecs = literal_pathspecs(&spell.names(specs));
    if git.too_large(at, shas, &pathspecs) {
        return declined;
    }
    let mut split = None;
    let stream = match git.commit_patch_stream(at, shas, &pathspecs) {
        Some(stream) => stream,
        None => match git.local_patch_window(at, shas, &pathspecs) {
            None => return declined,
            Some(window) => split.insert(window).stream.clone(),
        },
    };
    let blank = strip(&stream).is_empty();
    if blank && split.is_none() {
        return Ok((Vec::new(), Some(Set::new())));
    }
    let mut index = build_binding_index(&stream, lit)?;
    if index.parse_mismatches != 0 || (!blank && index.files.is_empty()) {
        return declined;
    }
    if let Some(window) = &split {
        window.mark(&mut index);
    }
    let Some(floor) = git.literal_floor(at, shas, specs, &pathspecs, claims, spell) else {
        return declined;
    };
    if index
        .files
        .iter()
        .any(|p| floor.paths.contains(p) && !floor.listed.contains(p))
    {
        return declined;
    }
    let window: Set = shas.iter().cloned().collect();
    let named = index_paths(&index);
    let mut drifted = Vec::new();
    let mut implicated = Set::new();
    let mut bare = false;
    for claim in claims {
        let spellings = spell.of(&claim.rel_path);
        let mut paths: Vec<PyStr> = if claim.kind == Kind::Absent {
            spellings.clone()
        } else {
            let held: Vec<PyStr> = spellings
                .iter()
                .filter(|p| named.contains(*p))
                .cloned()
                .collect();
            if held.is_empty() {
                vec![claim.rel_path.clone()]
            } else {
                held
            }
        };
        let results: Vec<_> = paths
            .iter()
            .map(|p| {
                let mut c = claim.clone();
                c.rel_path = p.clone();
                claim_level_drift(&c, &index, lit)
            })
            .collect();
        if claim.kind == Kind::Absent {
            let prefixes: Vec<PyStr> = spellings
                .iter()
                .map(|p| {
                    let end = p.iter().rposition(|&c| c != 0x2F).map_or(0, |i| i + 1);
                    let mut prefix = p[..end].to_vec();
                    prefix.push(0x2F);
                    prefix
                })
                .collect();
            let mut all: Set = paths.into_iter().collect();
            all.extend(
                named
                    .iter()
                    .filter(|p| prefixes.iter().any(|x| p.starts_with(x)))
                    .cloned(),
            );
            paths = all.into_iter().collect();
        }
        if !results.iter().any(|r| r.weak) {
            continue;
        }
        drifted.push(claim.render());
        let mut claim_shas = Set::new();
        for r in &results {
            claim_shas.extend(r.binding_shas.iter().cloned());
            claim_shas.extend(r.anchor_shas.iter().cloned());
        }
        if matches!(claim.kind, Kind::Path | Kind::Absent) {
            for path in &paths {
                for sha_set in index
                    .changed_text
                    .get(path)
                    .into_iter()
                    .flat_map(|m| m.values())
                {
                    claim_shas.extend(sha_set.iter().cloned());
                }
                if let Some(only) = index.header_only.get(path) {
                    claim_shas.extend(only.iter().cloned());
                }
            }
        }
        bare = bare || claim_shas.is_disjoint(&window);
        implicated.extend(claim_shas);
    }
    if !drifted.is_empty() && (implicated.is_empty() || (split.is_some() && bare)) {
        implicated = window.clone();
    }
    if let Some(w) = &split {
        implicated.extend(w.counted.iter().cloned());
    }
    implicated.extend(floor.commits.iter().cloned());
    implicated.extend(index.unindexed_commits.iter().cloned());
    Ok((
        drifted,
        Some(implicated.intersection(&window).cloned().collect()),
    ))
}
