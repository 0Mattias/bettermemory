//! A parsed tree lowered to the engine's arena of nodes threaded by
//! `next`, with each repeat classed as `re._compiler` classes it (CPython
//! 3.13.13, `Lib/re/_compiler.py` 31-35, 37-200 and 401-408; unit pyre-4b).
//!
//! [`lower`] takes the tree `re._parser.parse` returns and the flags
//! `_compile` reads (`re.compile(...).flags`), and answers a [`Program`] or
//! why there is none, in this order: nesting past [`MAX_DEPTH`] (checked
//! first, so that nothing after it recurses deeper), the two errors
//! `_compile` raises for a lookbehind, the engine's refusals in the order
//! `_compile` walks the tree, and an arena past [`MAX_NODES`].
//!
//! A repeat whose body `_simple` accepts is one [`Op::Rep1`] node, sre's
//! REPEAT_ONE or MIN_REPEAT_ONE. Any other repeat is sre's REPEAT with
//! MAX_UNTIL or MIN_UNTIL, unrolled: `min` copies of the body, then a loop
//! for an unbounded `max` or `max - min` nested optionals, each a
//! [`Op::Split`] that tries the body first when greedy and last when lazy.
//! A set is held as an opaque [`Class`] of the tree's members and the
//! flags in force; compiling it is the class unit's work.

use std::collections::HashMap;
use std::sync::Arc;

use super::fold::{Lit, Unreproduced};
use super::tree::{
    At, Feature, Member, Node, SubPattern, ASCII, DOTALL, IGNORECASE, LOCALE, MAXREPEAT, MULTILINE,
    UNICODE,
};

/// The arena's cap: more nodes than this is [`Feature::Nodes`].
pub const MAX_NODES: usize = 4096;
/// The deepest a body may sit below the top sequence (each branch
/// alternative, group, repeat or lookaround body is one level); deeper is
/// [`Feature::Nesting`].
pub const MAX_DEPTH: usize = 32;
/// `_parser.TYPE_FLAGS`.
const TYPE_FLAGS: u32 = ASCII | LOCALE | UNICODE;
/// `_constants.MAXCODE` for a code word of four bytes.
const MAXCODE: u128 = (1 << 32) - 1;

/// One code point a node consumes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Unit {
    /// LITERAL, or under IGNORECASE what `_compile` emits for it.
    Char(Lit),
    /// NOT_LITERAL likewise: any code point the literal does not match.
    NotChar(Lit),
    /// ANY (not a line feed) and ANY_ALL (under DOTALL).
    Any,
    AnyAll,
    Class(Class),
}

/// A set as the tree holds it, with the flags in force where it stands.
/// Every unrolled copy of one set shares its members.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Class {
    pub members: Arc<[Member]>,
    pub flags: u32,
}

/// The AT codes `_compile` emits, by sre's names.
#[allow(non_camel_case_types)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Anchor {
    AT_BEGINNING,
    AT_BEGINNING_LINE,
    AT_BEGINNING_STRING,
    AT_END,
    AT_END_LINE,
    AT_END_STRING,
    AT_UNI_BOUNDARY,
}

/// What a node does; [`Inst::next`] is where it goes on success.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Op {
    Unit(Unit),
    At(Anchor),
    /// sre's MARK: slot `2 * (group - 1)` opens a group, the next closes it.
    Mark(usize),
    /// Tries `next` first and this node second.
    Split(usize),
    /// REPEAT_ONE (`greedy`) or MIN_REPEAT_ONE over one unit.
    Rep1 {
        unit: Unit,
        min: u32,
        max: u32,
        greedy: bool,
    },
    /// ASSERT or ASSERT_NOT: `body` ends in [`Op::LookEnd`]; `behind` is a
    /// lookbehind's fixed width.
    Look {
        body: usize,
        behind: Option<u32>,
        negate: bool,
    },
    LookEnd,
    /// FAILURE, and a branch of no alternatives.
    Fail,
    Match,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Inst {
    pub op: Op,
    pub next: usize,
}

/// A lowered pattern. `trace` names, in the order `_compile` emits them,
/// the opcode of each repeat (REPEAT_ONE, MIN_REPEAT_ONE, or MAX_UNTIL and
/// MIN_UNTIL for REPEAT), anchor (its AT code), ANY or ANY_ALL, ASSERT,
/// ASSERT_NOT and FAILURE, and of each literal under IGNORECASE.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Program {
    pub insts: Vec<Inst>,
    pub start: usize,
    pub trace: Vec<&'static str>,
}

/// Why a tree is not lowered.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LowerError {
    /// `re` builds it and the engine does not.
    Unsupported(Feature),
    /// `_compile`'s PatternError "look-behind requires fixed-width pattern".
    LookbehindWidth,
    /// `_compile`'s error "looks too much behind".
    LooksTooMuchBehind,
}

use LowerError::Unsupported;

/// `_compiler._combine_flags`.
pub fn combine_flags(flags: u32, add_flags: u32, del_flags: u32) -> u32 {
    let base = if add_flags & TYPE_FLAGS != 0 {
        flags & !TYPE_FLAGS
    } else {
        flags
    };
    (base | add_flags) & !del_flags
}

/// `_compiler._simple`: the one unit a repeat's body is, looking through
/// groups whose number is `None`, with the flags in force at the unit.
pub fn simple(body: &SubPattern, mut flags: u32) -> Option<(&Node, u32)> {
    let mut p = body;
    loop {
        let [node] = p.0.as_slice() else {
            return None;
        };
        match node {
            Node::Subpattern {
                group: None,
                add_flags,
                del_flags,
                body,
            } => {
                flags = combine_flags(flags, *add_flags, *del_flags);
                p = body;
            }
            Node::Literal(_) | Node::NotLiteral(_) | Node::Any | Node::In(_) => {
                return Some((node, flags))
            }
            Node::Subpattern { .. }
            | Node::At(_)
            | Node::Branch(_)
            | Node::MaxRepeat(..)
            | Node::MinRepeat(..)
            | Node::Assert(..)
            | Node::AssertNot(..)
            | Node::Failure => return None,
        }
    }
}

/// The sub-patterns an item holds.
fn bodies(node: &Node) -> &[SubPattern] {
    match node {
        Node::Branch(alternatives) => alternatives,
        Node::Subpattern { body, .. }
        | Node::MaxRepeat(_, _, body)
        | Node::MinRepeat(_, _, body)
        | Node::Assert(_, body)
        | Node::AssertNot(_, body) => std::slice::from_ref(body),
        Node::Literal(_) | Node::NotLiteral(_) | Node::Any | Node::In(_) | Node::At(_) => &[],
        Node::Failure => &[],
    }
}

/// Lowers `tree` under `flags`; `bytes` for a bytes pattern.
pub fn lower(tree: &SubPattern, flags: u32, bytes: bool) -> Result<Program, LowerError> {
    // Without recursion: a tree of any depth is measured.
    let mut stack = vec![(tree, 0usize)];
    while let Some((p, depth)) = stack.pop() {
        if depth > MAX_DEPTH {
            return Err(Unsupported(Feature::Nesting));
        }
        for node in &p.0 {
            stack.extend(bodies(node).iter().map(|b| (b, depth + 1)));
        }
    }
    lookbehinds(tree)?;
    let mut trace = Vec::new();
    check(tree, flags, bytes, &mut trace)?;
    let mut arena = Arena {
        insts: Vec::new(),
        bytes,
        sets: HashMap::new(),
    };
    let done = arena.push(Op::Match, 0)?;
    let start = arena.seq(tree, flags, done)?;
    Ok(Program {
        insts: arena.insts,
        start,
        trace,
    })
}

/// `_compile`'s two errors of a lookbehind, the first in its order.
fn lookbehinds(p: &SubPattern) -> Result<(), LowerError> {
    for node in &p.0 {
        if let Node::Assert(dir, body) | Node::AssertNot(dir, body) = node {
            if *dir < 0 {
                let (lo, hi) = body.getwidth();
                if lo > MAXCODE {
                    return Err(LowerError::LooksTooMuchBehind);
                }
                if lo != hi {
                    return Err(LowerError::LookbehindWidth);
                }
            }
        }
        for body in bodies(node) {
            lookbehinds(body)?;
        }
    }
    Ok(())
}

/// The flags the engine has no rule for.
fn check_flags(flags: u32, bytes: bool) -> Result<(), LowerError> {
    if flags & LOCALE != 0 {
        return Err(Unsupported(Feature::Locale));
    }
    if flags & ASCII != 0 || (!bytes && flags & UNICODE == 0) {
        return Err(Unsupported(Feature::Ascii));
    }
    if bytes && flags & IGNORECASE != 0 {
        return Err(Unsupported(Feature::BytesIgnorecase));
    }
    Ok(())
}

fn anchor(at: At, flags: u32, bytes: bool) -> Result<Anchor, LowerError> {
    let multiline = flags & MULTILINE != 0;
    Ok(match at {
        At::AT_BEGINNING if multiline => Anchor::AT_BEGINNING_LINE,
        At::AT_BEGINNING => Anchor::AT_BEGINNING,
        At::AT_BEGINNING_STRING => Anchor::AT_BEGINNING_STRING,
        At::AT_END if multiline => Anchor::AT_END_LINE,
        At::AT_END => Anchor::AT_END,
        At::AT_END_STRING => Anchor::AT_END_STRING,
        At::AT_BOUNDARY if bytes => return Err(Unsupported(Feature::BytesBoundary)),
        At::AT_BOUNDARY => Anchor::AT_UNI_BOUNDARY,
        At::AT_NON_BOUNDARY => return Err(Unsupported(Feature::NotBoundary)),
    })
}

fn anchor_name(anchor: Anchor) -> &'static str {
    match anchor {
        Anchor::AT_BEGINNING => "AT_BEGINNING",
        Anchor::AT_BEGINNING_LINE => "AT_BEGINNING_LINE",
        Anchor::AT_BEGINNING_STRING => "AT_BEGINNING_STRING",
        Anchor::AT_END => "AT_END",
        Anchor::AT_END_LINE => "AT_END_LINE",
        Anchor::AT_END_STRING => "AT_END_STRING",
        Anchor::AT_UNI_BOUNDARY => "AT_UNI_BOUNDARY",
    }
}

fn lit(c: u32, flags: u32, bytes: bool) -> Result<Lit, LowerError> {
    Lit::new(c, flags).map_err(|why| match why {
        Unreproduced::Locale => Unsupported(Feature::Locale),
        Unreproduced::AsciiFold if bytes => Unsupported(Feature::BytesIgnorecase),
        Unreproduced::AsciiFold => Unsupported(Feature::Ascii),
    })
}

/// The opcode `_compile` emits for a literal under IGNORECASE.
fn lit_name(lit: Lit, negated: bool) -> &'static str {
    match (lit, negated) {
        (Lit::Exact(_), false) => "LITERAL",
        (Lit::Exact(_), true) => "NOT_LITERAL",
        (Lit::Fold { extra: [], .. }, false) => "LITERAL_UNI_IGNORE",
        (Lit::Fold { extra: [], .. }, true) => "NOT_LITERAL_UNI_IGNORE",
        (Lit::Fold { .. }, _) => "IN_UNI_IGNORE",
    }
}

/// The engine's refusals, in `_compile`'s order, and the trace.
fn check(
    p: &SubPattern,
    flags: u32,
    bytes: bool,
    trace: &mut Vec<&'static str>,
) -> Result<(), LowerError> {
    check_flags(flags, bytes)?;
    for node in &p.0 {
        match node {
            Node::Literal(c) | Node::NotLiteral(c) => {
                if flags & IGNORECASE != 0 {
                    let negated = matches!(node, Node::NotLiteral(_));
                    trace.push(lit_name(lit(*c, flags, bytes)?, negated));
                }
            }
            Node::Any => trace.push(if flags & DOTALL != 0 {
                "ANY_ALL"
            } else {
                "ANY"
            }),
            Node::In(members) => {
                if bytes && members.iter().any(|m| matches!(m, Member::Category(_))) {
                    return Err(Unsupported(Feature::BytesCategory));
                }
            }
            Node::At(at) => trace.push(anchor_name(anchor(*at, flags, bytes)?)),
            Node::Branch(alternatives) => {
                for alternative in alternatives {
                    check(alternative, flags, bytes, trace)?;
                }
            }
            Node::Subpattern {
                add_flags,
                del_flags,
                body,
                ..
            } => check(
                body,
                combine_flags(flags, *add_flags, *del_flags),
                bytes,
                trace,
            )?,
            Node::MaxRepeat(_, max, body) | Node::MinRepeat(_, max, body) => {
                let greedy = matches!(node, Node::MaxRepeat(..));
                if simple(body, flags).is_some() {
                    trace.push(if greedy {
                        "REPEAT_ONE"
                    } else {
                        "MIN_REPEAT_ONE"
                    });
                } else {
                    if *max > 1 && body.getwidth().0 == 0 {
                        return Err(Unsupported(Feature::NullableLoop));
                    }
                    trace.push(if greedy { "MAX_UNTIL" } else { "MIN_UNTIL" });
                }
                check(body, flags, bytes, trace)?;
            }
            Node::Assert(_, body) | Node::AssertNot(_, body) => {
                let negate = matches!(node, Node::AssertNot(..));
                trace.push(if negate { "ASSERT_NOT" } else { "ASSERT" });
                check(body, flags, bytes, trace)?;
            }
            Node::Failure => trace.push("FAILURE"),
        }
    }
    Ok(())
}

struct Arena {
    insts: Vec<Inst>,
    bytes: bool,
    /// The members of each set of the tree, keyed by the set's address,
    /// so that a copy of a repeat's body does not copy them again.
    sets: HashMap<*const Vec<Member>, Arc<[Member]>>,
}

impl Arena {
    fn push(&mut self, op: Op, next: usize) -> Result<usize, LowerError> {
        if self.insts.len() >= MAX_NODES {
            return Err(Unsupported(Feature::Nodes));
        }
        self.insts.push(Inst { op, next });
        Ok(self.insts.len() - 1)
    }

    /// The items of `p` threaded in order, the last going to `next`.
    fn seq(&mut self, p: &SubPattern, flags: u32, next: usize) -> Result<usize, LowerError> {
        let mut at = next;
        for node in p.0.iter().rev() {
            at = self.item(node, flags, at)?;
        }
        Ok(at)
    }

    /// The unit a node is, or `None` for a node that is not one.
    fn unit(&mut self, node: &Node, flags: u32) -> Result<Option<Unit>, LowerError> {
        Ok(Some(match node {
            Node::Literal(c) => Unit::Char(lit(*c, flags, self.bytes)?),
            Node::NotLiteral(c) => Unit::NotChar(lit(*c, flags, self.bytes)?),
            Node::Any if flags & DOTALL != 0 => Unit::AnyAll,
            Node::Any => Unit::Any,
            Node::In(members) => {
                let shared = self
                    .sets
                    .entry(members as *const Vec<Member>)
                    .or_insert_with(|| Arc::from(members.as_slice()));
                Unit::Class(Class {
                    members: Arc::clone(shared),
                    flags,
                })
            }
            Node::At(_)
            | Node::Branch(_)
            | Node::Subpattern { .. }
            | Node::MaxRepeat(..)
            | Node::MinRepeat(..)
            | Node::Assert(..)
            | Node::AssertNot(..)
            | Node::Failure => return Ok(None),
        }))
    }

    fn item(&mut self, node: &Node, flags: u32, next: usize) -> Result<usize, LowerError> {
        if let Some(unit) = self.unit(node, flags)? {
            return self.push(Op::Unit(unit), next);
        }
        match node {
            Node::At(at) => {
                let anchor = anchor(*at, flags, self.bytes)?;
                self.push(Op::At(anchor), next)
            }
            Node::Failure => self.push(Op::Fail, next),
            Node::Branch(alternatives) => {
                let Some((last, rest)) = alternatives.split_last() else {
                    // BRANCH with no alternative: its FAILURE.
                    return self.push(Op::Fail, next);
                };
                let mut entry = self.seq(last, flags, next)?;
                for alternative in rest.iter().rev() {
                    let first = self.seq(alternative, flags, next)?;
                    entry = self.push(Op::Split(entry), first)?;
                }
                Ok(entry)
            }
            Node::Subpattern {
                group,
                add_flags,
                del_flags,
                body,
            } => {
                let inner = combine_flags(flags, *add_flags, *del_flags);
                // `if group:` in `_compile`: group 0 marks nothing.
                match *group {
                    Some(g) if g > 0 => {
                        let close = self.push(Op::Mark(2 * (g - 1) + 1), next)?;
                        let entry = self.seq(body, inner, close)?;
                        self.push(Op::Mark(2 * (g - 1)), entry)
                    }
                    _ => self.seq(body, inner, next),
                }
            }
            Node::MaxRepeat(min, max, body) | Node::MinRepeat(min, max, body) => {
                let greedy = matches!(node, Node::MaxRepeat(..));
                self.repeat(*min, *max, greedy, body, flags, next)
            }
            Node::Assert(dir, body) | Node::AssertNot(dir, body) => {
                let negate = matches!(node, Node::AssertNot(..));
                let end = self.push(Op::LookEnd, next)?;
                let entry = self.seq(body, flags, end)?;
                // `lookbehinds` has held the width to MAXCODE.
                let behind = (*dir < 0).then(|| body.getwidth().0 as u32);
                let op = Op::Look {
                    body: entry,
                    behind,
                    negate,
                };
                self.push(op, next)
            }
            Node::Literal(_) | Node::NotLiteral(_) | Node::Any | Node::In(_) => {
                unreachable!("a unit is lowered above")
            }
        }
    }

    fn repeat(
        &mut self,
        min: u32,
        max: u32,
        greedy: bool,
        body: &SubPattern,
        flags: u32,
        next: usize,
    ) -> Result<usize, LowerError> {
        if let Some((node, inner)) = simple(body, flags) {
            if let Some(unit) = self.unit(node, inner)? {
                let op = Op::Rep1 {
                    unit,
                    min,
                    max,
                    greedy,
                };
                return self.push(op, next);
            }
        }
        // A split's `next` is tried first.
        let split = |body: usize| {
            if greedy {
                (Op::Split(next), body)
            } else {
                (Op::Split(body), next)
            }
        };
        let mut tail = next;
        if max == MAXREPEAT {
            let lp = self.push(Op::Fail, next)?;
            let entry = self.seq(body, flags, lp)?;
            let (op, first) = split(entry);
            self.insts[lp] = Inst { op, next: first };
            tail = lp;
        } else {
            // Each pass pushes at least one node (`check` refused an empty
            // body for a `max` past 1), so the cap ends a huge count.
            for _ in min..max {
                let entry = self.seq(body, flags, tail)?;
                let (op, first) = split(entry);
                tail = self.push(op, first)?;
            }
        }
        for _ in 0..min {
            let entry = self.seq(body, flags, tail)?;
            if entry == tail {
                // A body of no node: every further copy is none too.
                break;
            }
            tail = entry;
        }
        Ok(tail)
    }
}
