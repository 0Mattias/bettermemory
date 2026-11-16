//! git's `wildmatch` with `WM_PATHNAME` (and `WM_CASEFOLD` on request),
//! as the reference ports it: `src/bettermemory/gitconfig.py` lines
//! 1227-1520 (`wildmatch`, `_dowild`, `_star`, `_bracket`, `_CLASSES`,
//! `Steps`), with `_any_match` (1113-1126) and `_literal_head`
//! (1215-1224), which the `includeIf` conditions call.
//!
//! Bytes in, a plain answer out: match, no match, or [`Spent`] where the
//! match takes more than the steps left ([`WM_STEPS`] in a fresh
//! [`Steps`]) or nests more than [`WM_DEPTH`] stars. Each step is counted
//! where the reference counts it, so the steps a match takes are the same
//! number here and there (the fixture pins them). The recursion is at most
//! `WM_DEPTH + 1` calls of `dowild` deep, each with one `star` frame, so
//! the stack stays small whatever the input.

/// The steps one read's matching may take (`_WM_STEPS`, line 1274).
pub const WM_STEPS: i64 = 200_000;
/// The stars `dowild` may nest (`_WM_DEPTH`, line 1277).
pub const WM_DEPTH: usize = 150;

const MATCH: i32 = 0;
const NOMATCH: i32 = 1;
const ABORT_ALL: i32 = -1;
const ABORT_TO_STARSTAR: i32 = -2;

/// Why a match is not settled (`WildmatchSpent`, line 1280).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Spent {
    /// "the patterns take more than 200000 steps"
    Steps,
    /// "the pattern nests more than 150 stars"
    Depth,
}

impl Spent {
    /// The reference's wording of it.
    pub fn why(self) -> String {
        match self {
            Spent::Steps => format!("the patterns take more than {WM_STEPS} steps"),
            Spent::Depth => format!("the pattern nests more than {WM_DEPTH} stars"),
        }
    }
}

/// The steps a read's matching has left (`Steps`, line 1289), shared by
/// every pattern one read matches.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Steps {
    pub left: i64,
}

impl Default for Steps {
    fn default() -> Steps {
        Steps { left: WM_STEPS }
    }
}

fn take(steps: &mut Steps) -> Result<(), Spent> {
    // Saturating: a budget at `i64::MIN` is spent, as the reference's
    // unbounded integer is, instead of wrapping to a budget of no bound.
    steps.left = steps.left.saturating_sub(1);
    if steps.left < 0 {
        Err(Spent::Steps)
    } else {
        Ok(())
    }
}

fn upper(c: u8) -> bool {
    c.is_ascii_uppercase()
}

fn lower(c: u8) -> bool {
    c.is_ascii_lowercase()
}

fn at(bytes: &[u8], i: usize) -> u8 {
    bytes.get(i).copied().unwrap_or(0)
}

/// git's character classes as its own `ctype` reads them (`_CLASSES`).
fn class(name: &[u8], c: u8) -> Option<bool> {
    Some(match name {
        b"alnum" => c.is_ascii_alphanumeric(),
        b"alpha" => c.is_ascii_alphabetic(),
        b"blank" => c == b' ' || c == b'\t',
        b"cntrl" => c < 0x20 || c == 0x7f,
        b"digit" => c.is_ascii_digit(),
        b"graph" => (0x21..=0x7e).contains(&c),
        b"lower" => lower(c),
        b"print" => (0x20..=0x7e).contains(&c),
        b"punct" => {
            (0x21..=0x2f).contains(&c)
                || (0x3a..=0x40).contains(&c)
                || (0x5b..=0x60).contains(&c)
                || (0x7b..=0x7e).contains(&c)
        }
        b"space" => matches!(c, b' ' | b'\t' | b'\n' | b'\r'),
        b"upper" => upper(c),
        b"xdigit" => c.is_ascii_hexdigit(),
        _ => return None,
    })
}

/// Whether `pattern` matches `text` (`wildmatch`, line 1300): `*` and `?`
/// match no `/`, a `**` that is a whole component matches across it,
/// brackets take ranges, `!` or `^`, escapes and classes; `icase` folds
/// ASCII case where git's `WM_CASEFOLD` does. Each step is taken from
/// `steps`.
pub fn wildmatch(
    pattern: &[u8],
    text: &[u8],
    icase: bool,
    steps: &mut Steps,
) -> Result<bool, Spent> {
    wildmatch_scanned(pattern, text, icase, steps).0
}

/// [`wildmatch`], with the work its searches for a `/` did: the work no
/// step counts, in bytes read (a word of the index read counts as 8).
///
/// The reference searches the text for the next `/` (`text.find(b"/",
/// ti)` and `_SLASH in text[ti:]`, `gitconfig.py` 1396 and 1400) at every
/// star pass that asks, and each search can cover the rest of the text:
/// `[!/]***a?**/**/*a` against 200,000 bytes of `a` reads 8,749,725,002
/// bytes in 43,749 searches, which the reference's `memchr` does in
/// 0.22 s. Here the first search reads the text as the reference does,
/// and its answer is kept: a later search starting within what it read
/// takes that answer. At the first search it cannot answer, the text's
/// `/` positions are indexed once (a bit a byte, and for each 64-byte
/// block the next block holding a `/`), and every later search reads at
/// most two words. The work is then at most two readings of the text
/// and 16 bytes a search, and a search comes at most once a step. The
/// answers and the steps are the reference's.
pub fn wildmatch_scanned(
    pattern: &[u8],
    text: &[u8],
    icase: bool,
    steps: &mut Steps,
) -> (Result<bool, Spent>, u64) {
    let mut scan = Scan::default();
    let found = dowild(pattern, 0, text, 0, icase, steps, 0, &mut scan).map(|r| r == MATCH);
    (found, scan.scanned)
}

/// The searches of one match for a `/` (`wildmatch_scanned`).
#[derive(Default)]
struct Scan {
    /// The first search: from where, and what it found.
    first: Option<(usize, Option<usize>)>,
    /// The index, once built: a bit for each byte that is a `/`, and for
    /// each block of 64 bytes the first block at or after it with one
    /// (the block count where none is).
    bits: Vec<u64>,
    next_block: Vec<usize>,
    indexed: bool,
    scanned: u64,
}

impl Scan {
    /// The first `/` of `text` at or after `from`.
    fn next_slash(&mut self, text: &[u8], from: usize) -> Option<usize> {
        let m = text.len();
        if from >= m {
            return None;
        }
        if !self.indexed {
            match self.first {
                None => {
                    let found = text[from..].iter().position(|&b| b == b'/');
                    self.scanned += found.map_or(m - from, |i| i + 1) as u64;
                    let found = found.map(|i| from + i);
                    self.first = Some((from, found));
                    return found;
                }
                Some((start, found)) if start <= from && found.is_none_or(|f| from <= f) => {
                    return found;
                }
                Some(_) => self.index(text),
            }
        }
        self.scanned += 16;
        let block = from / 64;
        let word = self.bits[block] & (!0u64 << (from % 64));
        if word != 0 {
            return Some(block * 64 + word.trailing_zeros() as usize);
        }
        let next = self.next_block[block + 1];
        (next < self.bits.len()).then(|| next * 64 + self.bits[next].trailing_zeros() as usize)
    }

    fn index(&mut self, text: &[u8]) {
        let blocks = text.len().div_ceil(64);
        self.bits = vec![0; blocks];
        for (i, &b) in text.iter().enumerate() {
            if b == b'/' {
                self.bits[i / 64] |= 1 << (i % 64);
            }
        }
        self.next_block = vec![blocks; blocks + 1];
        for b in (0..blocks).rev() {
            self.next_block[b] = if self.bits[b] != 0 {
                b
            } else {
                self.next_block[b + 1]
            };
        }
        self.scanned += text.len() as u64;
        self.indexed = true;
    }
}

/// Whether `pattern` matches any of `texts` (`_any_match`, line 1113):
/// `Some(true)` at the first that does, `None` where none does and one
/// was not settled, else `Some(false)`.
pub fn any_match<'a>(
    pattern: &[u8],
    texts: impl IntoIterator<Item = &'a [u8]>,
    steps: &mut Steps,
    icase: bool,
) -> Option<bool> {
    let mut spent = false;
    for text in texts {
        match wildmatch(pattern, text, icase, steps) {
            Ok(true) => return Some(true),
            Ok(false) => {}
            Err(_) => spent = true,
        }
    }
    if spent {
        None
    } else {
        Some(false)
    }
}

/// Whether the first `literal` bytes of `pattern` and `text` agree, as
/// they are or without ASCII case (`_literal_head`, line 1215).
pub fn literal_head(pattern: &[u8], text: &[u8], literal: usize, icase: bool) -> bool {
    if literal == 0 {
        return true;
    }
    if text.len() < literal {
        return false;
    }
    let head = &pattern[..literal.min(pattern.len())];
    let given = &text[..literal];
    if icase {
        head.eq_ignore_ascii_case(given)
    } else {
        head == given
    }
}

// The reference's arguments, line for line, and the searches' state.
#[allow(clippy::too_many_arguments)]
fn dowild(
    p: &[u8],
    mut pi: usize,
    text: &[u8],
    mut ti: usize,
    icase: bool,
    steps: &mut Steps,
    depth: usize,
    scan: &mut Scan,
) -> Result<i32, Spent> {
    if depth > WM_DEPTH {
        return Err(Spent::Depth);
    }
    let (n, m) = (p.len(), text.len());
    while pi < n {
        take(steps)?;
        let mut p_ch = p[pi];
        if ti >= m && p_ch != b'*' {
            return Ok(ABORT_ALL);
        }
        let mut t_ch = at(text, ti);
        if icase && upper(t_ch) {
            t_ch += 0x20;
        }
        if icase && upper(p_ch) {
            p_ch += 0x20;
        }
        match p_ch {
            b'\\' => {
                pi += 1;
                if t_ch != at(p, pi) {
                    return Ok(NOMATCH);
                }
            }
            b'?' => {
                if t_ch == b'/' {
                    return Ok(NOMATCH);
                }
            }
            b'*' => match star(p, pi, text, ti, icase, steps, depth, scan)? {
                Star::Done(found) => return Ok(found),
                Star::At(np, nt) => {
                    pi = np;
                    ti = nt;
                }
            },
            b'[' => {
                let found = bracket(p, pi, t_ch, icase, steps)?;
                if found < 0 {
                    return Ok(found as i32);
                }
                if found == 0 {
                    return Ok(NOMATCH);
                }
                pi = found as usize;
            }
            _ => {
                if t_ch != p_ch {
                    return Ok(NOMATCH);
                }
            }
        }
        pi += 1;
        ti += 1;
    }
    Ok(if ti < m { NOMATCH } else { MATCH })
}

enum Star {
    Done(i32),
    At(usize, usize),
}

// The reference's arguments, line for line, and the searches' state.
#[allow(clippy::too_many_arguments)]
fn star(
    p: &[u8],
    mut pi: usize,
    text: &[u8],
    mut ti: usize,
    icase: bool,
    steps: &mut Steps,
    depth: usize,
    scan: &mut Scan,
) -> Result<Star, Spent> {
    let depth = depth + 1;
    let (n, m) = (p.len(), text.len());
    pi += 1;
    let mut match_slash = false;
    if pi < n && p[pi] == b'*' {
        let prev = pi.checked_sub(2);
        while pi < n && p[pi] == b'*' {
            pi += 1;
        }
        let nxt = at(p, pi);
        if prev.is_none_or(|i| p[i] == b'/')
            && (nxt == 0 || nxt == b'/' || (nxt == b'\\' && pi + 1 < n && p[pi + 1] == b'/'))
        {
            if nxt == b'/' && dowild(p, pi + 1, text, ti, icase, steps, depth, scan)? == MATCH {
                return Ok(Star::Done(MATCH));
            }
            match_slash = true;
        }
    }
    if pi >= n {
        if !match_slash && scan.next_slash(text, ti).is_some() {
            return Ok(Star::Done(NOMATCH));
        }
        return Ok(Star::Done(MATCH));
    }
    if !match_slash && p[pi] == b'/' {
        return Ok(match scan.next_slash(text, ti) {
            None => Star::Done(NOMATCH),
            Some(i) => Star::At(pi, i),
        });
    }
    let mut t_ch = at(text, ti);
    while ti < m {
        take(steps)?;
        if !matches!(p[pi], b'*' | b'?' | b'[' | b'\\') {
            let mut p_ch = p[pi];
            if icase && upper(p_ch) {
                p_ch += 0x20;
            }
            let mut broke = false;
            while ti < m && (match_slash || text[ti] != b'/') {
                scan.scanned += 1;
                t_ch = text[ti];
                if icase && upper(t_ch) {
                    t_ch += 0x20;
                }
                if t_ch == p_ch {
                    broke = true;
                    break;
                }
                ti += 1;
            }
            if !broke {
                t_ch = at(text, ti);
            }
            if t_ch != p_ch || ti >= m {
                return Ok(Star::Done(if match_slash {
                    ABORT_ALL
                } else {
                    ABORT_TO_STARSTAR
                }));
            }
        }
        let matched = dowild(p, pi, text, ti, icase, steps, depth, scan)?;
        if matched != NOMATCH {
            if !match_slash || matched != ABORT_TO_STARSTAR {
                return Ok(Star::Done(matched));
            }
        } else if !match_slash && t_ch == b'/' {
            return Ok(Star::Done(ABORT_TO_STARSTAR));
        }
        ti += 1;
        t_ch = at(text, ti);
    }
    Ok(Star::Done(ABORT_ALL))
}

/// A bracket at `pi` against the text's byte `t_ch`: where it closes when
/// it matches, 0 where it does not, `ABORT_ALL` where it is not closed or
/// names no class (`_bracket`, line 1437).
fn bracket(
    p: &[u8],
    mut pi: usize,
    t_ch: u8,
    icase: bool,
    steps: &mut Steps,
) -> Result<isize, Spent> {
    let abort = ABORT_ALL as isize;
    pi += 1;
    let mut p_ch = at(p, pi);
    if p_ch == b'^' {
        p_ch = b'!';
    }
    let negated = p_ch == b'!';
    if negated {
        pi += 1;
        p_ch = at(p, pi);
    }
    let mut prev_ch = 0u8;
    let mut matched = false;
    loop {
        take(steps)?;
        if p_ch == 0 {
            return Ok(abort);
        }
        if p_ch == b'\\' {
            pi += 1;
            p_ch = at(p, pi);
            if p_ch == 0 {
                return Ok(abort);
            }
            if t_ch == p_ch {
                matched = true;
            }
        } else if p_ch == b'-' && prev_ch != 0 && at(p, pi + 1) != 0 && at(p, pi + 1) != b']' {
            pi += 1;
            p_ch = at(p, pi);
            if p_ch == b'\\' {
                pi += 1;
                p_ch = at(p, pi);
                if p_ch == 0 {
                    return Ok(abort);
                }
            }
            if prev_ch <= t_ch && t_ch <= p_ch
                || icase && lower(t_ch) && prev_ch <= t_ch - 0x20 && t_ch - 0x20 <= p_ch
            {
                matched = true;
            }
            p_ch = 0;
        } else if p_ch == b'[' && at(p, pi + 1) == b':' {
            let start = pi + 2;
            let mut end = start;
            while at(p, end) != 0 && at(p, end) != b']' {
                end += 1;
            }
            if at(p, end) == 0 {
                return Ok(abort);
            }
            if end == start || p[end - 1] != b':' {
                // No ":]": a bracket of its own, read as a "[".
                pi = start - 2;
                p_ch = b'[';
                if t_ch == p_ch {
                    matched = true;
                }
                prev_ch = p_ch;
                pi += 1;
                p_ch = at(p, pi);
                if p_ch == b']' {
                    break;
                }
                continue;
            }
            let name = &p[start..end - 1];
            let Some(holds) = class(name, t_ch) else {
                return Ok(abort);
            };
            if holds || (icase && name == b"upper" && lower(t_ch)) {
                matched = true;
            }
            pi = end;
            p_ch = 0;
        } else if t_ch == p_ch {
            matched = true;
        }
        prev_ch = p_ch;
        pi += 1;
        p_ch = at(p, pi);
        if p_ch == b']' {
            break;
        }
    }
    if matched == negated || t_ch == b'/' {
        return Ok(0);
    }
    Ok(pi as isize)
}
