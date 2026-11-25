//! `durability.py`, unit 3: the capture rules (reference lines 643-858):
//! `CaptureRuleHit`, the patterns of rules 1, 3 and 5 with their word
//! lists, `_first_unquoted`, `_unanchored_test_total` and
//! `find_capture_rule_hits`.
//!
//! Every pattern is the engine's [`Regex`] built from the reference's own
//! text and flags (the f-strings expanded) and called as the reference
//! calls it (`finditer` or `search`), compiled once. A pattern the engine
//! does not build answers [`CaptureError::Pattern`], never a panic. The
//! README section "Transient markers (`durability.py`)" says what each
//! reading reproduces.

use std::sync::LazyLock;

use super::{at_sentence_start, quoted_spans, sentence_around, snippet_around};
use crate::pyre::regex::{CallError, Match, Regex, RegexError};
use crate::pyre::tree::{IGNORECASE, MULTILINE};
use crate::pyvalue::{PyStr, PyValue};

/// The text of `_SAYING_VERBS`, for the macros that splice it.
macro_rules! saying_verbs {
    () => {
        concat!(
            "said|says|suggested|suggests|recommended|recommends|explained|explains|",
            "noted|notes|provided|provides|stated|states|mentioned|mentions|proposed|",
            "proposes|told|tells|answered|answers|replied|replies|gave|gives|advised|",
            "advises|claimed|claims|offered|offers|confirmed|confirms|described|",
            "describes|reported|reports|shared|shares|wrote|writes|pointed out"
        )
    };
}

/// The text of `_WEEKDAYS`.
macro_rules! weekdays {
    () => {
        "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    };
}

/// The text of `_NUMBER`.
macro_rules! number {
    () => {
        r"(?:\d[\d,.]*k?|zero|one|two|three|four|five|six|seven|eight|nine|ten)"
    };
}

/// `_SAYING_VERBS`.
pub const SAYING_VERBS: &str = saying_verbs!();
/// `_WEEKDAYS`.
pub const WEEKDAYS: &str = weekdays!();
/// `_NUMBER`.
pub const NUMBER: &str = number!();

const I: u32 = IGNORECASE;

/// Every pattern of the capture rules: its inventory id, its text as
/// `re.Pattern.pattern` holds it, and the flags it was compiled with.
pub const CAPTURE_PATTERNS: [(&str, &str, u32); 21] = [
    ("_USERS_OPENER_RE", r"\bThe user['’]s\b", 0),
    (
        "_ASSISTANT_SAID_RE",
        concat!(
            r"\bthe assistant\b(?:\s+[\w'’-]+){0,2}?\s+(?:",
            saying_verbs!(),
            r")\b"
        ),
        I,
    ),
    (
        "_ASSISTANTS_ANSWER_RE",
        concat!(
            r"\bthe assistant['’]s\s+(?:recommendation|answer|suggestion|advice|",
            r"response|reply|explanation|proposal|view|opinion|summary)s?\b"
        ),
        I,
    ),
    (
        "_TODO_HEADING_RE",
        concat!(
            r"^[ \t>#*_-]*(?:to-?dos?|next steps?|action items?|follow-?ups?|",
            r"remaining work|open items?)[*_]*\s*:"
        ),
        I | MULTILINE,
    ),
    ("_TODO_PATTERNS", r"\bTODO\b", 0),
    ("_TODO_PATTERNS#2", r"\bstill (?:needs? to|to) [a-z]+", I),
    ("_TODO_PATTERNS#3", r"\byet to be [a-z]+", I),
    ("_TODO_PATTERNS#4", r"\bleft to do\b", I),
    ("_TODO_PATTERNS#5", r"^[ \t]*[-*+] \[ \]", MULTILINE),
    (
        "_TODO_PATTERNS#6",
        r"\bin the next session\b|\bnext time\b|\bwhen we resume\b",
        I,
    ),
    (
        "_IMPERATIVE_OPENER_RE",
        concat!(
            r"\A[ \t>#*_-]*(?:release|ship|fix|add|delete|remove|run|rerun|push|tag|merge|",
            r"deploy|update|write|create|rename|revert|bump|publish|commit|file|close|",
            r"check|test|investigate|move|finish|implement|port|revisit|wire|land|",
            r"document|refactor|clean up|drop|retry)\s+(?:the|a|an|this|that|these|",
            r"those|it|them|all|every|each|our|my|your|its)\b"
        ),
        I,
    ),
    (
        "_RELATIVE_DATE_PATTERNS",
        concat!(
            r"(?<!least )(?<!most )(?<!least-)(?<!most-)\brecently\b",
            r"(?![ -](?:used|viewed|opened|accessed|added|closed|modified|played|",
            r"visited|edited))"
        ),
        I,
    ),
    (
        "_RELATIVE_DATE_PATTERNS#2",
        concat!(
            r"\b(?:lately|nowadays|these days|the other day|just now|a while ago|",
            r"not long ago)\b"
        ),
        I,
    ),
    (
        "_RELATIVE_DATE_PATTERNS#3",
        concat!(
            r"\b(?:\d+|an?|one|two|three|four|five|six|seven|eight|nine|ten|a few|",
            r"several|a couple of)\s+(?:seconds?|minutes?|hours?|days?|weeks?|",
            r"months?|years?)\s+ago\b"
        ),
        I,
    ),
    (
        "_RELATIVE_DATE_PATTERNS#4",
        concat!(
            r"\b(?:last|next|this|coming|past)\s+(?:",
            weekdays!(),
            r"|weekend|night|week|",
            r"month|year|quarter|season|spring|summer|autumn|fall|winter)\b"
        ),
        I,
    ),
    (
        "_RELATIVE_DATE_PATTERNS#5",
        r"(?<!what )\b(?:is|are) now\b(?! known as)",
        I,
    ),
    ("_ISO_DATE_RE", r"\b\d{4}-\d{2}-\d{2}\b", 0),
    (
        "_COMMIT_HASH_RE",
        r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b",
        0,
    ),
    (
        "_DRIFTING_COUNT_RE",
        concat!(
            r"\b",
            number!(),
            r"\s+(?:GitHub\s+)?(?:stars?|forks?|followers?|downloads?|",
            r"open issues|watchers?)\b"
        ),
        I,
    ),
    (
        "_TEST_TOTAL_RE",
        concat!(
            r"\b\d[\d,]*\s+(?:tests?\s+)?(?:passed|failed|skipped|passing|failing|",
            r"errors?)\b"
        ),
        I,
    ),
    ("_SUITE_WORD_RE", r"\b(?:tests?|suite|pytest|CI)\b", I),
];

const USERS_OPENER: usize = 0;
const ASSISTANT_SAID: usize = 1;
const ASSISTANTS_ANSWER: usize = 2;
/// `(_TODO_HEADING_RE, *_TODO_PATTERNS)`.
const NO_TODO: [usize; 7] = [3, 4, 5, 6, 7, 8, 9];
const IMPERATIVE_OPENER: usize = 10;
/// `_RELATIVE_DATE_PATTERNS`.
const RELATIVE_DATES: [usize; 5] = [11, 12, 13, 14, 15];
const ISO_DATE: usize = 16;
const COMMIT_HASH: usize = 17;
const DRIFTING_COUNT: usize = 18;
const TEST_TOTAL: usize = 19;
const SUITE_WORD: usize = 20;

static BUILT: LazyLock<Vec<Result<Regex, RegexError>>> = LazyLock::new(|| {
    CAPTURE_PATTERNS
        .iter()
        .map(|&(_, text, flags)| Regex::new_str(text, flags))
        .collect()
});

/// Why the capture rules answer no value.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CaptureError {
    /// The engine did not build the pattern of this inventory id.
    Pattern(&'static str, RegexError),
    /// A call of the pattern of this inventory id answered no value.
    Call(&'static str, CallError),
}

/// `CaptureRuleHit`: the rule's number in `CAPTURE_RULES`, its short
/// name, and the phrase that fired with a few words around it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CaptureRuleHit {
    pub rule: u32,
    pub name: &'static str,
    pub snippet: PyStr,
}

impl CaptureRuleHit {
    /// `to_dict`: `rule`, `name`, `snippet`, in that order.
    pub fn to_dict(&self) -> PyValue {
        PyValue::Dict(vec![
            (PyValue::str("rule"), PyValue::int(i128::from(self.rule))),
            (PyValue::str("name"), PyValue::str(self.name)),
            (PyValue::str("snippet"), PyValue::Str(self.snippet.clone())),
        ])
    }
}

fn regex(i: usize) -> Result<&'static Regex, CaptureError> {
    BUILT[i]
        .as_ref()
        .map_err(|e| CaptureError::Pattern(CAPTURE_PATTERNS[i].0, e.clone()))
}

fn call_error(i: usize) -> impl Fn(CallError) -> CaptureError {
    move |e| CaptureError::Call(CAPTURE_PATTERNS[i].0, e)
}

/// `pattern.search(text)` found something.
fn found(i: usize, text: &[u32]) -> Result<bool, CaptureError> {
    regex(i)?
        .search(text, 0, isize::MAX)
        .map(|m| m.is_some())
        .map_err(call_error(i))
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
/// and disjoint. The last span that opens at or before `start` is the only
/// one that can hold `[start, end)` (an earlier one ends at or before the
/// later one opens), so one binary search answers as the reference's scan
/// over every span does, and a body dense in quotes stays linear.
fn quoted(spans: &[(usize, usize)], start: usize, end: usize) -> bool {
    let k = spans.partition_point(|&(a, _)| a <= start);
    k > 0 && {
        let (a, b) = spans[k - 1];
        a <= start && end <= b
    }
}

/// What `keep` reads of a match: its start and end.
type Keep<'a> = &'a dyn Fn(usize, usize) -> Result<bool, CaptureError>;

/// `_first_unquoted`: for each pattern in order, the first match of its
/// `finditer` outside every quoted span that `keep` accepts (then the next
/// pattern); across patterns the earliest start, the earlier pattern on a
/// tie. The answer is the match's span.
fn first_unquoted(
    patterns: &[usize],
    text: &[u32],
    spans: &[(usize, usize)],
    keep: Option<Keep<'_>>,
) -> Result<Option<(usize, usize)>, CaptureError> {
    let mut best: Option<(usize, usize)> = None;
    for &i in patterns {
        let matches = regex(i)?
            .finditer(text, 0, isize::MAX)
            .map_err(call_error(i))?;
        for m in &matches {
            let (start, end) = span(m);
            if quoted(spans, start, end) {
                continue;
            }
            if let Some(keep) = keep {
                if !keep(start, end)? {
                    continue;
                }
            }
            if best.is_none_or(|(b, _)| start < b) {
                best = Some((start, end));
            }
            break;
        }
    }
    Ok(best)
}

/// `_unanchored_test_total`: a suite word in `sentence`, and neither an
/// ISO date nor a commit hash.
pub fn unanchored_test_total(sentence: &[u32]) -> Result<bool, CaptureError> {
    if !found(SUITE_WORD, sentence)? {
        return Ok(false);
    }
    Ok(!(found(ISO_DATE, sentence)? || found(COMMIT_HASH, sentence)?))
}

/// `find_capture_rule_hits`: the rules `content` breaks, in the
/// reference's order (`plain_subject`, `no_todo`, `drifting_count`,
/// `test_total`, `absolute_date`), each with the snippet of the first
/// phrase that fired. A rule written `a or b` reads `b` only where `a`
/// found nothing.
pub fn find_capture_rule_hits(content: &[u32]) -> Result<Vec<CaptureRuleHit>, CaptureError> {
    let spans = quoted_spans(content);
    let spans = spans.as_slice();
    let opener = |start: usize, _: usize| Ok(at_sentence_start(content, start) == Some(true));
    let plain_subject = match first_unquoted(&[USERS_OPENER], content, spans, Some(&opener))? {
        Some(hit) => Some(hit),
        None => first_unquoted(&[ASSISTANT_SAID, ASSISTANTS_ANSWER], content, spans, None)?,
    };
    let no_todo = match first_unquoted(&NO_TODO, content, spans, None)? {
        Some(hit) => Some(hit),
        None => first_unquoted(&[IMPERATIVE_OPENER], content, spans, None)?,
    };
    let undated = |start: usize, _: usize| Ok(!found(ISO_DATE, &sentence_around(content, start))?);
    let drifting_count = first_unquoted(&[DRIFTING_COUNT], content, spans, Some(&undated))?;
    let unanchored =
        |start: usize, _: usize| unanchored_test_total(&sentence_around(content, start));
    let test_total = first_unquoted(&[TEST_TOTAL], content, spans, Some(&unanchored))?;
    let absolute_date = first_unquoted(&RELATIVE_DATES, content, spans, None)?;
    let found = [
        (3, "plain_subject", plain_subject),
        (1, "no_todo", no_todo),
        (1, "drifting_count", drifting_count),
        (1, "test_total", test_total),
        (5, "absolute_date", absolute_date),
    ];
    Ok(found
        .into_iter()
        .filter_map(|(rule, name, hit)| {
            hit.map(|(start, end)| CaptureRuleHit {
                rule,
                name,
                snippet: snippet_around(content, start, end),
            })
        })
        .collect())
}
