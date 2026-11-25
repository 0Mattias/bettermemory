//! The engine's memo (unit pyre-9): what one call of a pattern remembers
//! of where the rest of the pattern has already failed, so that a hostile
//! text costs steps in proportion to its length. It changes no answer.
//! The design is the decision record's (pyre-design 2026-11-19, section 4
//! and conditions 4 to 8) and the prototype `proto_small_backtracker.py`
//! (`_run`, `_failed_ends`, `_prepare`).
//!
//! Two devices, both owned by one call ([`Memo`]) and handed to each run
//! of [`super::exec::exec_with`] the call makes:
//!
//! - one failure bit per (Split or Rep1 node, position), set when the
//!   marker frame pushed on entering that node is popped by backtracking
//!   (every way on from there was tried and none reached an accepted end);
//!   reaching a set bit fails at once;
//! - per Rep1 node four integers: the run of its unit read last (start and
//!   true end), so a later entry inside it does not read it again, and one
//!   interval of end positions from which the rest is known to fail
//!   ([`Memo::failed_ends`] merges as `_failed_ends` does).
//!
//! The premises ([`premises`]): after lowering no op reads a mark, there is
//! no counter, no backreference and no conditional, and a loop over a
//! nullable body is refused (`lower`'s `NullableLoop`), so whether the rest
//! reaches an accepted end from (node, position) is a function of that pair
//! for one text, one `endpos` and one acceptance rule. A memo never
//! outlives a call. The devices start when the call has spent more than
//! `64 * (len + 64)` steps ([`Way::Auto`], as shipped), so a call below
//! that pays nothing and counts the steps it counted before this unit;
//! above it the bits cost up to one row of `len / 64 + 1` words per Split
//! or Rep1 node, hostile call or not.

use std::cell::Cell;

use super::exec::MatchError;
use super::lower::{Op, Program};

/// When the devices start.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Way {
    /// Never: plain backtracking.
    Off,
    /// From the call's first step.
    On,
    /// As shipped: once the call has spent more than `64 * (len + 64)`.
    Auto,
    /// Once the call has spent more than this many steps.
    After(u64),
}

/// The memo's switches: when it starts, and whether the per-repeat
/// intervals and runs are used beside the bits.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Setting {
    pub way: Way,
    pub intervals: bool,
}

impl Setting {
    /// What the public methods run.
    pub const SHIPPED: Setting = Setting {
        way: Way::Auto,
        intervals: true,
    };
    pub const OFF: Setting = Setting {
        way: Way::Off,
        intervals: false,
    };
}

/// The steps and the most choice points held at once over a whole call
/// (every run of the matcher it made).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Stats {
    pub steps: u64,
    pub peak: usize,
    /// Whether the devices started.
    pub woke: bool,
    /// The bytes the bits and intervals held at the end.
    pub bytes: usize,
}

thread_local! {
    static LAST: Cell<Stats> = const { Cell::new(Stats { steps: 0, peak: 0, woke: false, bytes: 0 }) };
}

/// The [`Stats`] of the last call of a [`super::regex::Regex`] method on
/// this thread (`match` to `split`), finished or refused: a call refused
/// by the type check records zeros.
pub fn last_call() -> Stats {
    LAST.with(Cell::get)
}

/// A run of the unit and an interval of failed ends; `lo > hi` is empty.
#[derive(Clone, Copy, Debug)]
pub(super) struct Rep {
    pub run: (isize, isize),
    pub lo: isize,
    pub hi: isize,
}

const NO_REP: Rep = Rep {
    run: (-1, -1),
    lo: 1,
    hi: 0,
};

/// The memo of one call.
#[derive(Debug)]
pub struct Memo {
    setting: Setting,
    switch: u64,
    words: usize,
    /// Whether the devices run.
    pub(super) active: bool,
    /// The steps of the call's finished runs.
    pub(super) spent: u64,
    pub(super) peak: usize,
    rows: Vec<Vec<u64>>,
    reps: Vec<Rep>,
}

impl Memo {
    /// A memo for a call over a text of `len` units.
    pub fn new(setting: Setting, len: usize) -> Memo {
        let switch = match setting.way {
            Way::Off | Way::On => 0,
            Way::Auto => (len as u64).saturating_add(64).saturating_mul(64),
            Way::After(b) => b,
        };
        Memo {
            setting,
            switch,
            words: len / 64 + 1,
            active: setting.way == Way::On,
            spent: 0,
            peak: 0,
            rows: Vec::new(),
            reps: Vec::new(),
        }
    }

    /// Starts the devices once the call has spent more than the switch.
    pub(super) fn wake(&mut self, run_steps: u64) {
        if !self.active && self.setting.way != Way::Off && self.spent + run_steps > self.switch {
            self.active = true;
        }
    }

    pub(super) fn intervals(&self) -> bool {
        self.active && self.setting.intervals
    }

    /// Whether the bit of (`node`, `pos`) is set.
    pub(super) fn failed(&self, node: usize, pos: isize) -> bool {
        let p = pos as usize;
        self.rows
            .get(node)
            .and_then(|row| row.get(p / 64))
            .is_some_and(|w| w >> (p % 64) & 1 == 1)
    }

    /// Sets the bit of (`node`, `pos`); a row is allocated on its first
    /// bit, and every growth is fallible.
    pub(super) fn set(&mut self, node: usize, pos: isize) -> Result<(), MatchError> {
        if self.rows.len() <= node {
            self.rows
                .try_reserve(node + 1 - self.rows.len())
                .map_err(|_| MatchError::Memory)?;
            self.rows.resize_with(node + 1, Vec::new);
        }
        let row = &mut self.rows[node];
        if row.is_empty() {
            row.try_reserve_exact(self.words)
                .map_err(|_| MatchError::Memory)?;
            row.resize(self.words, 0);
        }
        let p = pos as usize;
        row[p / 64] |= 1 << (p % 64);
        Ok(())
    }

    /// [`Memo::set`] for the tests of refused growth.
    #[doc(hidden)]
    pub fn set_for_test(&mut self, node: usize, pos: isize) -> Result<(), MatchError> {
        self.set(node, pos)
    }

    /// The run and interval of the Rep1 at `node`.
    pub(super) fn rep(&self, node: usize) -> Rep {
        self.reps.get(node).copied().unwrap_or(NO_REP)
    }

    pub(super) fn rep_mut(&mut self, node: usize) -> Result<&mut Rep, MatchError> {
        if self.reps.len() <= node {
            self.reps
                .try_reserve(node + 1 - self.reps.len())
                .map_err(|_| MatchError::Memory)?;
            self.reps.resize(node + 1, NO_REP);
        }
        Ok(&mut self.reps[node])
    }

    /// `_failed_ends`: the ends `a..=b` failed; a range that does not touch
    /// the kept one replaces it, one that does widens it.
    pub(super) fn failed_ends(
        &mut self,
        node: usize,
        a: isize,
        b: isize,
    ) -> Result<(), MatchError> {
        let iv = self.rep_mut(node)?;
        if iv.lo > iv.hi || a > iv.hi + 1 || b < iv.lo - 1 {
            (iv.lo, iv.hi) = (a, b);
        } else {
            iv.lo = iv.lo.min(a);
            iv.hi = iv.hi.max(b);
        }
        Ok(())
    }

    /// What the call spent, recorded for [`last_call`].
    pub(super) fn finish(&self) -> Stats {
        let bytes = self.rows.iter().map(|r| r.len() * 8).sum::<usize>()
            + self.reps.len() * std::mem::size_of::<Rep>();
        let stats = Stats {
            steps: self.spent,
            peak: self.peak,
            woke: self.active,
            bytes,
        };
        LAST.with(|c| c.set(stats));
        stats
    }
}

/// The premises the memo leans on, over a lowered program: every op is one
/// the memo knows, and none reads a mark. The match has no wildcard, so an
/// op added to [`Op`] (a backreference, a conditional) stops the build
/// here until the memo is argued sound for it.
pub fn premises(prog: &Program) -> bool {
    prog.insts.iter().all(|inst| match inst.op {
        Op::Unit(_) | Op::At(_) | Op::Mark(_) | Op::Split(_) | Op::Rep1 { .. } => true,
        Op::Look { .. } | Op::LookEnd | Op::Fail | Op::Match => true,
    })
}
