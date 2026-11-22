//! A lowered [`Program`] run on a text as `_sre` runs the compiled pattern
//! (CPython 3.13.13, `Modules/_sre/sre_lib.h`, `SRE(match)` and
//! `SRE(search)`; unit pyre-5a). The order of trial is reproduced by
//! differential against `re`, since the C source is not on the machine the
//! unit was built on: the answers of `match`, `fullmatch` and `search` are
//! pinned over every window of short texts (`gen_pyre_exec`).
//!
//! [`exec`] is one loop over the arena with one heap stack of choice
//! points and one trail of mark writes, so the depth of the Rust call
//! stack does not grow with the text or the pattern. Every growing buffer
//! grows through [`grow`], which answers [`MatchError::Memory`] instead of
//! aborting. The step count is always kept; a budget is a declared
//! refusal ([`MatchError::Budget`]).
//!
//! Lookarounds (unit pyre-5b) run as `_sre`'s ASSERT and ASSERT_NOT: the
//! body is a sub-attempt on the same stack under a [`Frame::Look`] marker,
//! never a recursive call. The body's choice points are cut when it
//! reaches [`Op::LookEnd`]. A lookbehind of width `w` fails (ASSERT) or
//! passes (ASSERT_NOT) when fewer than `w` characters lie between the
//! string's beginning and the position, whatever `pos` is. Marks set in a
//! positive lookaround stay on success; a negative one takes back every
//! mark and `lastindex` its body wrote. `parse::refusal` refuses a capture
//! inside a lookaround, so only a program built below the refusal (the
//! tests' `exec.beyond.` rows) holds such a mark; the rule is `_sre`'s
//! and is kept for it.

use super::fold::Lit;
use super::lower::{Anchor, Class, Inst, Op, Program, Unit};
use super::tree::MAXREPEAT;
use crate::unicode::is_word;

/// Class membership, supplied by the caller: a class is held opaque by the
/// arena, and compiling it is the class unit's work (pyre-4a).
pub trait ClassTest {
    fn contains(&self, class: &Class, c: u32) -> bool;
}

/// A text the engine reads: code points of a `str`, or bytes.
pub trait Text {
    fn len(&self) -> usize;
    fn at(&self, i: usize) -> u32;
    fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

impl Text for [u32] {
    fn len(&self) -> usize {
        <[u32]>::len(self)
    }
    fn at(&self, i: usize) -> u32 {
        self[i]
    }
}

impl Text for [u8] {
    fn len(&self) -> usize {
        <[u8]>::len(self)
    }
    fn at(&self, i: usize) -> u32 {
        u32::from(self[i])
    }
}

/// Which of `re`'s three calls the run answers.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    /// `Pattern.match`: one attempt at `pos`, accepted wherever it ends.
    Match,
    /// `Pattern.fullmatch`: one attempt at `pos`, accepted only at `endpos`.
    Fullmatch,
    /// `Pattern.search`: an attempt at each start from `pos` to `endpos`.
    Search,
}

/// A match: its span, the marks (`2 * (group - 1)` opens a group and the
/// next slot closes it, -1 unset), `lastindex` (-1 for `None`), the steps
/// taken and the most choice points held at once.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Found {
    pub start: isize,
    pub end: isize,
    pub marks: Vec<isize>,
    pub lastindex: isize,
    pub steps: u64,
    pub peak: usize,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MatchError {
    /// A buffer could not grow.
    Memory,
    /// The steps passed the budget the caller set.
    Budget,
}

/// What [`exec`] does not run: since unit pyre-5b, nothing a lowered
/// program can hold.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Unsupported {}

/// Whether [`exec`] runs every node of `prog`: always.
pub fn supports(_prog: &Program) -> Result<(), Unsupported> {
    Ok(())
}

/// The trail's slot for `lastindex`.
const LASTINDEX: usize = usize::MAX;

/// A choice point.
enum Frame {
    /// A split's second way: go to `pc` at `pos`.
    Alt { pc: usize, pos: isize, trail: usize },
    /// A greedy single-unit repeat at node `pc` that began at `start` and
    /// now holds `count` units.
    Greedy {
        pc: usize,
        start: isize,
        count: isize,
        trail: usize,
    },
    /// A lazy one: the next try takes one unit more.
    Lazy {
        pc: usize,
        start: isize,
        count: isize,
        trail: usize,
    },
    /// The lookaround at node `pc`, entered at `pos`: popped by its
    /// [`Op::LookEnd`] when the body matches, by backtracking when not.
    Look { pc: usize, pos: isize, trail: usize },
}

/// The one fallible push of every buffer.
fn grow<T>(v: &mut Vec<T>, x: T) -> Result<(), MatchError> {
    if v.len() == v.capacity() {
        v.try_reserve(v.len().max(16))
            .map_err(|_| MatchError::Memory)?;
    }
    v.push(x);
    Ok(())
}

struct Run<'a, C: ?Sized, T: ?Sized> {
    insts: &'a [Inst],
    classes: &'a C,
    text: &'a T,
    /// `len(string)` and the clamped `endpos`.
    len: isize,
    end: isize,
    stack: Vec<Frame>,
    trail: Vec<(usize, isize)>,
    marks: Vec<isize>,
    lastindex: isize,
    steps: u64,
    budget: Option<u64>,
    peak: usize,
}

impl<C: ClassTest + ?Sized, T: Text + ?Sized> Run<'_, C, T> {
    fn ch(&self, pos: isize) -> u32 {
        self.text.at(pos as usize)
    }

    fn unit(&self, unit: &Unit, pos: isize) -> bool {
        if pos >= self.end {
            return false;
        }
        let c = self.ch(pos);
        match unit {
            Unit::Char(lit) => Lit::matches(lit, c),
            Unit::NotChar(lit) => !lit.matches(c),
            Unit::Any => c != 10,
            Unit::AnyAll => true,
            Unit::Class(class) => self.classes.contains(class, c),
        }
    }

    /// `SRE(at)`. `pos` lies in `[0, len]`; `end` may lie before it.
    fn at(&self, anchor: Anchor, pos: isize) -> bool {
        let end = self.end;
        match anchor {
            Anchor::AT_BEGINNING | Anchor::AT_BEGINNING_STRING => pos == 0,
            Anchor::AT_BEGINNING_LINE => pos == 0 || self.ch(pos - 1) == 10,
            Anchor::AT_END => pos == end || (pos + 1 == end && self.ch(pos) == 10),
            // The character at `pos` is read whatever `endpos` is.
            Anchor::AT_END_LINE => pos == end || (pos < self.len && self.ch(pos) == 10),
            Anchor::AT_END_STRING => pos == end,
            Anchor::AT_UNI_BOUNDARY => {
                if end == 0 {
                    return false;
                }
                let before = pos > 0 && is_word(self.ch(pos - 1));
                let after = pos < end && is_word(self.ch(pos));
                before != after
            }
        }
    }

    fn step(&mut self) -> Result<(), MatchError> {
        self.steps += 1;
        match self.budget {
            Some(b) if self.steps > b => Err(MatchError::Budget),
            _ => Ok(()),
        }
    }

    fn set(&mut self, slot: usize, value: isize) -> Result<(), MatchError> {
        let old = if slot == LASTINDEX {
            std::mem::replace(&mut self.lastindex, value)
        } else {
            std::mem::replace(&mut self.marks[slot], value)
        };
        grow(&mut self.trail, (slot, old))
    }

    fn unwind(&mut self, to: usize) {
        while self.trail.len() > to {
            let Some((slot, old)) = self.trail.pop() else {
                break;
            };
            if slot == LASTINDEX {
                self.lastindex = old;
            } else {
                self.marks[slot] = old;
            }
        }
    }

    fn push(&mut self, frame: Frame) -> Result<(), MatchError> {
        grow(&mut self.stack, frame)?;
        self.peak = self.peak.max(self.stack.len());
        Ok(())
    }

    /// How many units of `unit` follow `pos`, at most `limit`.
    fn count(&mut self, unit: &Unit, pos: isize, limit: isize) -> Result<isize, MatchError> {
        let mut k = 0;
        while k < limit && self.unit(unit, pos + k) {
            k += 1;
            self.step()?;
        }
        Ok(k)
    }

    /// One attempt at `start`: the end of the match, or `None`.
    fn attempt(
        &mut self,
        entry: usize,
        start: isize,
        full: bool,
    ) -> Result<Option<isize>, MatchError> {
        let insts = self.insts;
        let (mut pc, mut pos) = (entry, start);
        loop {
            // Forward until a node fails.
            loop {
                self.step()?;
                let inst = &insts[pc];
                let next = inst.next;
                match &inst.op {
                    Op::Unit(unit) => {
                        if !self.unit(unit, pos) {
                            break;
                        }
                        pos += 1;
                    }
                    Op::At(anchor) => {
                        if !self.at(*anchor, pos) {
                            break;
                        }
                    }
                    Op::Mark(slot) => {
                        self.set(*slot, pos)?;
                        if slot & 1 == 1 {
                            self.set(LASTINDEX, (slot / 2 + 1) as isize)?;
                        }
                    }
                    Op::Split(second) => {
                        let trail = self.trail.len();
                        self.push(Frame::Alt {
                            pc: *second,
                            pos,
                            trail,
                        })?;
                    }
                    Op::Rep1 {
                        unit,
                        min,
                        max,
                        greedy,
                    } => {
                        let (min, max) = (*min as isize, *max);
                        let room = self.end - pos;
                        // A minimum past the room fails, even a minimum of
                        // zero when `pos` lies past `endpos`.
                        if min > room {
                            break;
                        }
                        let limit = if max == MAXREPEAT || max as isize > room {
                            room
                        } else {
                            max as isize
                        };
                        let trail = self.trail.len();
                        if *greedy {
                            let k = self.count(unit, pos, limit)?;
                            if k < min {
                                break;
                            }
                            if k > min {
                                self.push(Frame::Greedy {
                                    pc,
                                    start: pos,
                                    count: k,
                                    trail,
                                })?;
                            }
                            pos += k;
                        } else {
                            let k = self.count(unit, pos, min)?;
                            if k < min {
                                break;
                            }
                            if k < limit {
                                self.push(Frame::Lazy {
                                    pc,
                                    start: pos,
                                    count: k,
                                    trail,
                                })?;
                            }
                            pos += k;
                        }
                    }
                    Op::Match => {
                        if full && pos != self.end {
                            break;
                        }
                        return Ok(Some(pos));
                    }
                    Op::Fail => break,
                    Op::Look {
                        body,
                        behind,
                        negate,
                    } => {
                        let back = behind.map_or(0, |w| w as isize);
                        // Measured from the string's beginning, not `pos`.
                        if pos >= back {
                            let trail = self.trail.len();
                            self.push(Frame::Look { pc, pos, trail })?;
                            pc = *body;
                            pos -= back;
                            continue;
                        }
                        if !negate {
                            break;
                        }
                    }
                    Op::LookEnd => {
                        // The body matched: cut its choice points.
                        let (look, at, trail) = loop {
                            match self.stack.pop() {
                                Some(Frame::Look { pc, pos, trail }) => break (pc, pos, trail),
                                Some(_) => {}
                                None => {
                                    // Unreachable: LookEnd runs only inside a
                                    // body whose Look pushed a marker frame.
                                    debug_assert!(false, "LookEnd without a Look frame");
                                    return Ok(None);
                                }
                            }
                        };
                        if let Op::Look { negate: true, .. } = insts[look].op {
                            // Redundant with the backtrack that follows, which
                            // unwinds to a trail no longer than this one; kept
                            // to mirror ASSERT_NOT's LASTMARK_RESTORE.
                            self.unwind(trail);
                            break;
                        }
                        pc = insts[look].next;
                        pos = at;
                        continue;
                    }
                }
                pc = next;
            }
            // Back to the latest choice point.
            loop {
                let Some(frame) = self.stack.pop() else {
                    // The attempt failed: take back the marks and the
                    // lastindex written since the last choice point, as
                    // `_sre` resets them before the next start.
                    self.unwind(0);
                    return Ok(None);
                };
                self.step()?;
                match frame {
                    Frame::Alt {
                        pc: second,
                        pos: at,
                        trail,
                    } => {
                        self.unwind(trail);
                        pc = second;
                        pos = at;
                        break;
                    }
                    Frame::Greedy {
                        pc: node,
                        start: from,
                        count,
                        trail,
                    } => {
                        self.unwind(trail);
                        let Op::Rep1 { min, .. } = insts[node].op else {
                            return Ok(None);
                        };
                        let count = count - 1;
                        if count > min as isize {
                            self.push(Frame::Greedy {
                                pc: node,
                                start: from,
                                count,
                                trail,
                            })?;
                        }
                        pc = insts[node].next;
                        pos = from + count;
                        break;
                    }
                    Frame::Lazy {
                        pc: node,
                        start: from,
                        count,
                        trail,
                    } => {
                        self.unwind(trail);
                        let Op::Rep1 { unit, max, .. } = &insts[node].op else {
                            return Ok(None);
                        };
                        let at = from + count;
                        // A lazy frame is pushed only while `count` is below
                        // the maximum, so one more unit is always allowed.
                        if !self.unit(unit, at) {
                            continue;
                        }
                        let count = count + 1;
                        if *max == MAXREPEAT || count < *max as isize {
                            self.push(Frame::Lazy {
                                pc: node,
                                start: from,
                                count,
                                trail,
                            })?;
                        }
                        pc = insts[node].next;
                        pos = at + 1;
                        break;
                    }
                    Frame::Look {
                        pc: look,
                        pos: at,
                        trail,
                    } => {
                        // The body failed: ASSERT fails, ASSERT_NOT goes on.
                        self.unwind(trail);
                        if let Op::Look { negate: true, .. } = insts[look].op {
                            pc = insts[look].next;
                            pos = at;
                            break;
                        }
                    }
                }
            }
        }
    }
}

/// Runs `prog` on `text` from `pos` to `endpos` as `re`'s `match`,
/// `fullmatch` or `search` with those arguments does. Both are clamped to
/// `[0, len]` as `re` clamps them; `endpos` may lie before `pos`, where
/// `match` still makes its one attempt and the others find nothing.
pub fn exec<C: ClassTest + ?Sized, T: Text + ?Sized>(
    prog: &Program,
    classes: &C,
    text: &T,
    pos: isize,
    endpos: isize,
    mode: Mode,
    budget: Option<u64>,
) -> Result<Option<Found>, MatchError> {
    let len = isize::try_from(text.len()).map_err(|_| MatchError::Memory)?;
    let pos = pos.clamp(0, len);
    let end = endpos.clamp(0, len);
    let slots = prog
        .insts
        .iter()
        .map(|inst| match inst.op {
            Op::Mark(slot) => (slot | 1) + 1,
            Op::Unit(_) | Op::At(_) | Op::Split(_) | Op::Rep1 { .. } => 0,
            Op::Look { .. } | Op::LookEnd | Op::Fail | Op::Match => 0,
        })
        .max()
        .unwrap_or(0);
    let mut marks = Vec::new();
    marks
        .try_reserve_exact(slots)
        .map_err(|_| MatchError::Memory)?;
    marks.resize(slots, -1);
    let mut run = Run {
        insts: &prog.insts,
        classes,
        text,
        len,
        end,
        stack: Vec::new(),
        trail: Vec::new(),
        marks,
        lastindex: -1,
        steps: 0,
        budget,
        peak: 0,
    };
    let starts = match mode {
        Mode::Match | Mode::Fullmatch => pos..=pos,
        // `search` finds nothing in a reversed window: the range is empty.
        Mode::Search => pos..=end,
    };
    for start in starts {
        // A failed attempt unwinds the whole trail before it returns, so
        // every start begins with no mark set and no lastindex.
        if let Some(stop) = run.attempt(prog.start, start, mode == Mode::Fullmatch)? {
            return Ok(Some(Found {
                start,
                end: stop,
                marks: run.marks,
                lastindex: run.lastindex,
                steps: run.steps,
                peak: run.peak,
            }));
        }
    }
    Ok(None)
}
