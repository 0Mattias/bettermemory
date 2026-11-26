//! `durability.py`, unit 2: the transient-marker tables and their patterns
//! (`TRANSIENT_PHRASE_MARKERS`, `_AS_OF_DATE_RE`, `_IN_FLIGHT_WORK_OBJECT`,
//! `_PATTERN_OVERRIDES`, `_PHRASE_REGEXES`, `_TITLECASE_SKIP_MARKERS`),
//! `_anchored_new_reference` and `find_transient_markers` (reference lines
//! 54-268, 303-406 and 573-641).
//!
//! Every pattern is the engine's [`Regex`] built from the reference's own
//! text and flags and called as the reference calls it (`finditer`),
//! compiled once on first use. A generic phrase pattern is built as the
//! reference builds it, `\b` + `re.escape(phrase)` + `\b(?!\.\w)`, with the
//! engine's port of `re.escape`. A pattern the engine does not build
//! answers [`MarkerError::Pattern`], never a panic. The README section
//! "Transient markers (`durability.py`)" says what each reading
//! reproduces.

use std::sync::LazyLock;

use super::{is_titlecase_name, quoted_spans, sentence_around, snippet_around, TransientMatch};
use crate::pyre::atom::escape;
use crate::pyre::regex::{CallError, Match, Regex, RegexError};
use crate::pyre::tree::IGNORECASE;
use crate::search_dedup::raw_content_token_set;
use crate::supersession::{anchor_tokens, change_cues, SupersessionError};
use crate::unicode;

/// `TRANSIENT_PHRASE_MARKERS`, in the reference's order.
pub const TRANSIENT_PHRASE_MARKERS: [&str; 52] = [
    "currently",
    "as of now",
    "as of this writing",
    "as of writing",
    "right now",
    "for now",
    "temporarily",
    "for the time being",
    "at the moment",
    "today",
    "this morning",
    "this afternoon",
    "tonight",
    "yesterday",
    "tomorrow",
    "this week",
    "this month",
    "last week",
    "next week",
    "next month",
    "i just",
    "we just",
    "just shipped",
    "the latest",
    "was recently",
    "were recently",
    "has recently",
    "have recently",
    "recently switched",
    "recently migrated",
    "recently renamed",
    "is in progress",
    "are in progress",
    "in the middle of",
    "halfway through",
    "unpushed",
    "commits ahead",
    "commits behind",
    "has uncommitted changes",
    "have uncommitted changes",
    "has untracked files",
    "have untracked files",
    "are untracked files",
    "is stashed",
    "are stashed",
    "the new",
    "now uses",
    "now use",
    "now using",
    "now relies",
    "now rely",
    "now relying",
];

/// `_TITLECASE_SKIP_MARKERS`: the time words a title-case match exempts.
pub const TITLECASE_SKIP_MARKERS: [&str; 9] = [
    "today",
    "tonight",
    "yesterday",
    "tomorrow",
    "this week",
    "this month",
    "last week",
    "next week",
    "next month",
];

/// `_AS_OF_DATE_MARKER`: the one name every dated snapshot folds onto.
pub const AS_OF_DATE_MARKER: &str = "as of <date>";

/// `_AS_OF_DATE_RE`: its text and flags.
pub const AS_OF_DATE_RE: (&str, u32) = (r"\bas of \d{4}-\d{2}-\d{2}\b", IGNORECASE);

/// The text of `_IN_FLIGHT_WORK_OBJECT`, for the overrides that splice it.
macro_rules! in_flight_work_object {
    () => {
        concat!(
            r"(?:(?!(?:morning|evening)\b)\w+ing\b",
            r"|(?:(?:a|an|the)\s+)?(?:migration|refactor(?:ing)?|rewrite|upgrade",
            r"|rollout|deploy(?:ment)?|rebase|merge|release)\b)"
        )
    };
}

/// `_IN_FLIGHT_WORK_OBJECT`.
pub const IN_FLIGHT_WORK_OBJECT: &str = in_flight_work_object!();

/// `_PATTERN_OVERRIDES`: the phrase, its pattern's text and flags, in the
/// dict's order.
pub const PATTERN_OVERRIDES: [(&str, &str, u32); 6] = [
    ("today", r"\btoday\b(?!['’]s|\.\w)", IGNORECASE),
    ("the new", r"\b[Tt]he new\b", 0),
    (
        "at the moment",
        r"\bat the moment\b(?!\s+(?:of|when|that)\b)",
        IGNORECASE,
    ),
    ("temporarily", r"\btemporarily\b(?!\s+\w+s\b)", IGNORECASE),
    (
        "in the middle of",
        concat!(r"\bin the middle of ", in_flight_work_object!()),
        IGNORECASE,
    ),
    (
        "halfway through",
        concat!(r"\bhalfway through ", in_flight_work_object!()),
        IGNORECASE,
    ),
];

/// The text and flags `_PHRASE_REGEXES` holds for `phrase`: its override,
/// or the generic word-boundary pattern under IGNORECASE. A compiled
/// pattern is always true, so `_PATTERN_OVERRIDES.get(phrase) or ...`
/// takes the override wherever there is one.
pub fn phrase_pattern(phrase: &str) -> (Vec<u32>, u32) {
    if let Some(&(_, text, flags)) = PATTERN_OVERRIDES.iter().find(|(p, _, _)| *p == phrase) {
        return (unicode::cps(text), flags);
    }
    let mut text = unicode::cps(r"\b");
    text.extend(escape(&unicode::cps(phrase)));
    text.extend(unicode::cps(r"\b(?!\.\w)"));
    (text, IGNORECASE)
}

/// `_PHRASE_REGEXES`, in `TRANSIENT_PHRASE_MARKERS`' order.
static PHRASES: LazyLock<Vec<Result<Regex, RegexError>>> = LazyLock::new(|| {
    TRANSIENT_PHRASE_MARKERS
        .iter()
        .map(|phrase| {
            let (text, flags) = phrase_pattern(phrase);
            Regex::new(&text, flags)
        })
        .collect()
});

static AS_OF: LazyLock<Result<Regex, RegexError>> =
    LazyLock::new(|| Regex::new_str(AS_OF_DATE_RE.0, AS_OF_DATE_RE.1));

/// The name `_AS_OF_DATE_RE` goes by in an error.
const AS_OF_NAME: &str = "_AS_OF_DATE_RE";

/// Why the marker check answers no value. None is reachable from the
/// reference's patterns as the engine builds them; each is a fault inside
/// the port.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum MarkerError {
    /// The engine did not build the pattern of this phrase (or of
    /// `_AS_OF_DATE_RE`).
    Pattern(&'static str, RegexError),
    /// A call of the pattern of this phrase answered no value.
    Call(&'static str, CallError),
    /// The supersession vocabulary `_anchored_new_reference` reads.
    Supersession(SupersessionError),
}

impl From<SupersessionError> for MarkerError {
    fn from(e: SupersessionError) -> Self {
        MarkerError::Supersession(e)
    }
}

/// `pattern.finditer(text)` of an engine pattern, collected.
fn matches(
    built: &Result<Regex, RegexError>,
    name: &'static str,
    text: &[u32],
) -> Result<Vec<Match>, MarkerError> {
    built
        .as_ref()
        .map_err(|e| MarkerError::Pattern(name, e.clone()))?
        .finditer(text, 0, isize::MAX)
        .map_err(|e| MarkerError::Call(name, e))
}

/// A match's span in code points. A match of a `str` pattern on a text
/// lies inside it, so neither end is negative.
fn span(m: &Match) -> (usize, usize) {
    let (a, b) = m.span(0).unwrap_or((0, 0));
    (
        usize::try_from(a).unwrap_or(0),
        usize::try_from(b).unwrap_or(0),
    )
}

/// `in_quoted_span` over spans as `quoted_spans` gives them: in text order
/// and disjoint, so the last span that opens at or before `start` is the
/// only one that can hold `[start, end)`, and one binary search answers as
/// the reference's scan over every span does.
fn quoted(spans: &[(usize, usize)], start: usize, end: usize) -> bool {
    let k = spans.partition_point(|&(a, _)| a <= start);
    k > 0 && {
        let (a, b) = spans[k - 1];
        a <= start && end <= b
    }
}

/// `_anchored_new_reference` for a match starting at `start`: the
/// sentence around the start holds a change cue and an anchor token. Both
/// supersession readers take the sentence as it is, as in the reference.
pub fn anchored_new_reference(text: &[u32], start: usize) -> Result<bool, MarkerError> {
    let sentence = sentence_around(text, start);
    if change_cues(&sentence)?.is_empty() {
        return Ok(false);
    }
    let raw = raw_content_token_set(&sentence);
    Ok(!anchor_tokens(&sentence, &raw)?.is_empty())
}

/// `find_transient_markers`: for each phrase in order, the first match of
/// its `finditer` that is outside every quoted span, is no title-case name
/// (the time words only) and is no anchored `the new`; then the first
/// `_AS_OF_DATE_RE` match outside every quoted span. Each hit carries its
/// snippet. The reference's `seen` set never holds a phrase still to come,
/// since the phrases are distinct, so each phrase is read once.
pub fn find_transient_markers(content: &[u32]) -> Result<Vec<TransientMatch>, MarkerError> {
    let spans = quoted_spans(content);
    let mut hits = Vec::new();
    for (phrase, built) in TRANSIENT_PHRASE_MARKERS.iter().zip(PHRASES.iter()) {
        let titled = TITLECASE_SKIP_MARKERS.contains(phrase);
        for m in &matches(built, phrase, content)? {
            let (start, end) = span(m);
            if quoted(&spans, start, end) {
                continue;
            }
            // A phrase match holds a word, so `_is_titlecase_name` never
            // reaches the `IndexError` its `None` stands for.
            if titled && is_titlecase_name(content, start, end) == Some(true) {
                continue;
            }
            if *phrase == "the new" && anchored_new_reference(content, start)? {
                continue;
            }
            hits.push(TransientMatch {
                marker: unicode::cps(phrase),
                snippet: snippet_around(content, start, end),
            });
            break;
        }
    }
    for m in &matches(&AS_OF, AS_OF_NAME, content)? {
        let (start, end) = span(m);
        if quoted(&spans, start, end) {
            continue;
        }
        hits.push(TransientMatch {
            marker: unicode::cps(AS_OF_DATE_MARKER),
            snippet: snippet_around(content, start, end),
        });
        break;
    }
    Ok(hits)
}
