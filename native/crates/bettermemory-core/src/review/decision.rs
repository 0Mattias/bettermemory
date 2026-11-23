//! The pure decision made from the write review's scores and relation answers.

use std::collections::{HashMap, HashSet};

use super::{
    RelatedSet, ReviewAnswers, Tau, ACTION_COMMIT, ACTION_RESTATES, ACTION_SECRET, ACTION_STANDING,
    ACTION_UNSAFE, DETECTOR_REVIEW, DETECTOR_REVIEW_DISAGREES, LABEL_CONFLICT, LABEL_EXTENDS,
    LABEL_RESTATES, LABEL_UNRELATED, LABEL_UPDATE,
};
use crate::pyvalue::{PyStr, PyValue};
use crate::unicode;

/// The fields of ReviewDecision, in the original entry or replacement order.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ReviewDecision {
    pub action: PyStr,
    pub restated: Vec<PyStr>,
    pub links: Vec<PyStr>,
    pub update_suggested: Vec<PyStr>,
    pub filings: Vec<(PyStr, PyStr)>,
    pub cue_dropped: Vec<PyStr>,
    pub extends: Vec<PyStr>,
    pub standing: Vec<PyStr>,
    pub standing_write: bool,
}

impl ReviewDecision {
    fn action(name: &str) -> Self {
        Self {
            action: unicode::cps(name),
            ..Self::default()
        }
    }

    /// The event fields in the same insertion order as ReviewDecision.to_event.
    pub fn to_event(&self) -> PyValue {
        let mut out = vec![(PyValue::str("action"), PyValue::Str(self.action.clone()))];
        let add_ids = |out: &mut Vec<_>, key: &str, ids: &[PyStr]| {
            if !ids.is_empty() {
                out.push((
                    PyValue::str(key),
                    PyValue::List(ids.iter().cloned().map(PyValue::Str).collect()),
                ));
            }
        };
        add_ids(&mut out, "restated", &self.restated);
        add_ids(&mut out, "standing", &self.standing);
        if self.standing_write {
            out.push((PyValue::str("standing_write"), PyValue::Bool(true)));
        }
        add_ids(&mut out, "links", &self.links);
        add_ids(&mut out, "extends", &self.extends);
        add_ids(&mut out, "update_suggested", &self.update_suggested);
        if !self.filings.is_empty() {
            let filings = self
                .filings
                .iter()
                .map(|(id, detector)| {
                    PyValue::Dict(vec![
                        (PyValue::str("id"), PyValue::Str(id.clone())),
                        (PyValue::str("detector"), PyValue::Str(detector.clone())),
                    ])
                })
                .collect();
            out.push((PyValue::str("filings"), PyValue::List(filings)));
        }
        add_ids(&mut out, "cue_dropped", &self.cue_dropped);
        PyValue::Dict(out)
    }
}

/// The keyword arguments of apply_review. The constructor sets the five optional
/// arguments to their Python defaults.
pub struct ReviewOptions<'a> {
    pub tau: Tau,
    pub write_claim_sized: bool,
    pub write_supersession: bool,
    pub declared_ids: &'a [PyStr],
    pub standing_ids: &'a [PyStr],
    pub said: bool,
    pub restates_refuses: bool,
    pub write_standing: bool,
}

impl ReviewOptions<'_> {
    pub fn new(tau: Tau, write_claim_sized: bool, write_supersession: bool) -> Self {
        Self {
            tau,
            write_claim_sized,
            write_supersession,
            declared_ids: &[],
            standing_ids: &[],
            said: false,
            restates_refuses: true,
            write_standing: false,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Label {
    Update,
    Extends,
    Conflict,
    Restates,
    Unrelated,
    Other,
}

impl Label {
    fn of(points: &[u32]) -> Self {
        for (text, label) in [
            (LABEL_UPDATE, Self::Update),
            (LABEL_EXTENDS, Self::Extends),
            (LABEL_CONFLICT, Self::Conflict),
            (LABEL_RESTATES, Self::Restates),
            (LABEL_UNRELATED, Self::Unrelated),
        ] {
            if points.len() == text.len() && points.iter().copied().eq(text.bytes().map(u32::from))
            {
                return label;
            }
        }
        Self::Other
    }

    fn disputed(self) -> bool {
        matches!(self, Self::Conflict | Self::Unrelated | Self::Extends)
    }
}

struct Relations<'a>(HashMap<&'a [u32], Label>);

impl<'a> Relations<'a> {
    fn new(answers: &'a ReviewAnswers) -> Self {
        Self(
            answers
                .relation
                .iter()
                .map(|(id, value)| (id.as_slice(), Label::of(value)))
                .collect(),
        )
    }

    fn get(&self, id: &[u32]) -> Label {
        self.0.get(id).copied().unwrap_or(Label::Other)
    }
}

fn cues(related: &RelatedSet) -> &[PyStr] {
    related
        .report
        .as_ref()
        .map_or(&[], |r| r.supersedes.as_slice())
}

fn replacements<'a>(
    rels: &Relations<'_>,
    related: &'a RelatedSet,
    declared: &'a [PyStr],
) -> Vec<&'a PyStr> {
    let mut out: Vec<_> = related
        .entries
        .iter()
        .filter(|e| rels.get(&e.id) == Label::Update)
        .map(|e| &e.id)
        .collect();
    let mut seen: HashSet<_> = out.iter().copied().collect();
    out.extend(declared.iter().filter(|id| seen.insert(id)));
    // The cue comprehension sees out as it stood before the list addition.
    // Repeated new cue targets retain their positions and multiplicity.
    out.extend(
        cues(related)
            .iter()
            .filter(|id| !seen.contains(id) && !rels.get(id).disputed()),
    );
    out
}

/// The updated related IDs, declared IDs and undisputed cue IDs in replacement order.
pub fn replaced_ids(
    answers: &ReviewAnswers,
    related: &RelatedSet,
    declared_ids: &[PyStr],
) -> Vec<PyStr> {
    if related.entries.is_empty() && cues(related).is_empty() {
        let mut seen = HashSet::new();
        return declared_ids
            .iter()
            .filter(|id| seen.insert(*id))
            .cloned()
            .collect();
    }
    replacements(&Relations::new(answers), related, declared_ids)
        .into_iter()
        .cloned()
        .collect()
}

/// Python's dict.fromkeys exhausts the iterable and retains one copy of each ID.
/// Keep its first position without copying the code-point buffer a second time.
fn distinct_ids<I, E>(source: I) -> Result<Vec<PyStr>, E>
where
    I: IntoIterator<Item = Result<PyStr, E>>,
{
    let mut positions = HashMap::new();
    for item in source {
        let id = item?;
        let next = positions.len();
        positions.entry(id).or_insert(next);
    }
    let mut ordered = vec![Vec::new(); positions.len()];
    for (id, index) in positions {
        ordered[index] = id;
    }
    Ok(ordered)
}

/// Replacements from a fallible, one-shot source. An error obtaining the
/// iterator or reading any item is returned unchanged.
pub fn replaced_ids_iter<I, E>(
    answers: &ReviewAnswers,
    related: &RelatedSet,
    declared_ids: impl FnOnce() -> Result<I, E>,
) -> Result<Vec<PyStr>, E>
where
    I: IntoIterator<Item = Result<PyStr, E>>,
{
    let declared = distinct_ids(declared_ids()?)?;
    Ok(replaced_ids(answers, related, &declared))
}

fn score_refusal(answers: &ReviewAnswers, tau: Tau) -> Option<ReviewDecision> {
    if answers.secret >= tau.secret {
        Some(ReviewDecision::action(ACTION_SECRET))
    } else if answers.r#unsafe >= tau.r#unsafe {
        Some(ReviewDecision::action(ACTION_UNSAFE))
    } else {
        None
    }
}

/// Apply the two thresholds, standing conflicts, restatements and the user's
/// words in order, then collect the links and filings for a committed write.
pub fn apply_review(
    answers: &ReviewAnswers,
    related: &RelatedSet,
    options: &ReviewOptions<'_>,
) -> ReviewDecision {
    score_refusal(answers, options.tau)
        .unwrap_or_else(|| decide_relations(answers, related, options))
}

/// Apply the review with deferred ID iterables in place of the option slices.
/// Neither source is opened on a score refusal. Otherwise standing is read
/// to completion before the first declared ID is requested, as in apply_review.
pub fn apply_review_iter<D, S, E>(
    answers: &ReviewAnswers,
    related: &RelatedSet,
    options: &ReviewOptions<'_>,
    declared_ids: impl FnOnce() -> Result<D, E>,
    standing_ids: impl FnOnce() -> Result<S, E>,
) -> Result<ReviewDecision, E>
where
    D: IntoIterator<Item = Result<PyStr, E>>,
    S: IntoIterator<Item = Result<PyStr, E>>,
{
    if let Some(refusal) = score_refusal(answers, options.tau) {
        return Ok(refusal);
    }
    let standing = distinct_ids(standing_ids()?)?;
    let declared = distinct_ids(declared_ids()?)?;
    let current = ReviewOptions {
        standing_ids: &standing,
        declared_ids: &declared,
        ..*options
    };
    Ok(decide_relations(answers, related, &current))
}

fn decide_relations(
    answers: &ReviewAnswers,
    related: &RelatedSet,
    options: &ReviewOptions<'_>,
) -> ReviewDecision {
    if related.entries.is_empty()
        && (options.said
            || (options.standing_ids.is_empty() && !options.write_standing)
            || (options.declared_ids.is_empty() && cues(related).is_empty()))
    {
        return ReviewDecision::action(ACTION_COMMIT);
    }
    let relations = Relations::new(answers);
    let standing: HashSet<_> = options.standing_ids.iter().collect();
    let conflicted: Vec<_> = related
        .entries
        .iter()
        .filter(|e| standing.contains(&e.id) && relations.get(&e.id) == Label::Conflict)
        .map(|e| e.id.clone())
        .collect();
    if !conflicted.is_empty() {
        return ReviewDecision {
            standing: conflicted,
            ..ReviewDecision::action(ACTION_STANDING)
        };
    }
    let restated: Vec<_> = related
        .entries
        .iter()
        .filter(|e| relations.get(&e.id) == Label::Restates)
        .map(|e| e.id.clone())
        .collect();
    if !restated.is_empty()
        && options.declared_ids.is_empty()
        && options.restates_refuses
        && !related
            .entries
            .iter()
            .any(|e| matches!(relations.get(&e.id), Label::Update | Label::Extends))
    {
        return ReviewDecision {
            restated,
            ..ReviewDecision::action(ACTION_RESTATES)
        };
    }
    if (!standing.is_empty() || options.write_standing) && !options.said {
        let unsaid: Vec<_> = replacements(&relations, related, options.declared_ids)
            .into_iter()
            .filter(|id| options.write_standing || standing.contains(id))
            .cloned()
            .collect();
        if !unsaid.is_empty() {
            return ReviewDecision {
                standing: unsaid,
                standing_write: options.write_standing,
                ..ReviewDecision::action(ACTION_STANDING)
            };
        }
    }
    let cue: HashSet<_> = cues(related).iter().collect();
    let declared: HashSet<_> = options.declared_ids.iter().collect();
    let mut decision = ReviewDecision {
        restated,
        ..ReviewDecision::action(ACTION_COMMIT)
    };
    for entry in &related.entries {
        let id = &entry.id;
        let label = relations.get(id);
        if label == Label::Update {
            if !cue.contains(id) && !declared.contains(id) {
                if options.write_supersession && options.write_claim_sized && entry.claim_sized {
                    decision.links.push(id.clone());
                } else {
                    decision.update_suggested.push(id.clone());
                }
            }
            continue;
        }
        if label.disputed() && cue.contains(id) {
            decision
                .filings
                .push((id.clone(), unicode::cps(DETECTOR_REVIEW_DISAGREES)));
            decision.cue_dropped.push(id.clone());
        } else if label == Label::Conflict {
            decision
                .filings
                .push((id.clone(), unicode::cps(DETECTOR_REVIEW)));
        }
        if label == Label::Extends && !declared.contains(id) {
            decision.extends.push(id.clone());
        }
    }
    decision
}
