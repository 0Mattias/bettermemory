//! `re._parser`'s tree, its `State`, the text `re` prints for a tree, and
//! the names a pattern is refused by (CPython 3.13.13, `Lib/re/_parser.py`
//! 74 to 176).
//!
//! A [`SubPattern`] is the list `SubPattern.data` holds, and
//! [`SubPattern::dump`] is `str(tree)`: the `repr` of that list, which the
//! fixtures compare with the interpreter's. The codes of [`At`] and
//! [`Category`] carry `re._constants`' own names, so the dump prints them.
//!
//! [`Node`] has no derived equality on purpose. `_parse_sub` compares items
//! as Python compares tuples, where an item that holds a nested sub-pattern
//! equals nothing but itself; [`Node::py_eq`] is that comparison.
//!
//! Nothing here recurses once per level of a tree. The dump, `Debug`,
//! `Clone` and the drop each keep their own stack on the heap, so a tree
//! nested as deep as memory allows is printed, copied and freed on any
//! thread. The reference's `str(tree)` raises RecursionError from 998
//! levels, a bound of the interpreter's stack and not of the text; the
//! dump writes the text at every depth.

use std::collections::hash_map::{Entry, HashMap};
use std::fmt;

/// `re.IGNORECASE` and the other flag bits of `re._constants`.
pub const IGNORECASE: u32 = 2;
pub const LOCALE: u32 = 4;
pub const MULTILINE: u32 = 8;
pub const DOTALL: u32 = 16;
pub const UNICODE: u32 = 32;
pub const VERBOSE: u32 = 64;
pub const DEBUG: u32 = 128;
pub const ASCII: u32 = 256;
/// `_sre.MAXREPEAT`: the maximum of an unbounded repeat, printed by name.
pub const MAXREPEAT: u32 = u32::MAX;
/// `_sre.MAXGROUPS`.
pub const MAXGROUPS: usize = 1_073_741_823;
/// `MAXWIDTH`, the largest width `getwidth` returns.
pub const MAXWIDTH: u128 = 1 << 64;

/// Where an `AT` item holds: the codes the parser writes.
#[allow(non_camel_case_types)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum At {
    AT_BEGINNING,
    AT_BEGINNING_STRING,
    AT_BOUNDARY,
    AT_NON_BOUNDARY,
    AT_END,
    AT_END_STRING,
}

/// The class a `CATEGORY` member names.
#[allow(non_camel_case_types)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Category {
    CATEGORY_DIGIT,
    CATEGORY_NOT_DIGIT,
    CATEGORY_SPACE,
    CATEGORY_NOT_SPACE,
    CATEGORY_WORD,
    CATEGORY_NOT_WORD,
}

/// A member of a set (`IN`). `Negate` is only ever the first.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Member {
    Negate,
    Literal(u32),
    Range(u32, u32),
    Category(Category),
}

/// One item of a sub-pattern: the `(op, av)` tuples the parser appends.
/// `GROUPREF`, `GROUPREF_EXISTS`, `ATOMIC_GROUP` and `POSSESSIVE_REPEAT`
/// have no variant: the engine refuses them, so no tree can hold one.
/// `Debug` writes the item as the dump does.
pub enum Node {
    Literal(u32),
    NotLiteral(u32),
    Any,
    In(Vec<Member>),
    At(At),
    /// `(BRANCH, (None, items))`.
    Branch(Vec<SubPattern>),
    /// `(SUBPATTERN, (group, add_flags, del_flags, p))`.
    Subpattern {
        group: Option<usize>,
        add_flags: u32,
        del_flags: u32,
        body: SubPattern,
    },
    /// `(MAX_REPEAT, (min, max, item))`; an unbounded `max` is [`MAXREPEAT`].
    MaxRepeat(u32, u32, SubPattern),
    MinRepeat(u32, u32, SubPattern),
    /// `(ASSERT, (dir, p))`: `dir` is 1 ahead and -1 behind.
    Assert(i8, SubPattern),
    AssertNot(i8, SubPattern),
    /// `(FAILURE, ())`, an empty negative lookaround.
    Failure,
}

impl Node {
    /// `self == other` as Python compares the two tuples: by value for an
    /// item that holds numbers and codes only. One that holds a
    /// `SubPattern` (an object without `__eq__`) equals only itself, the
    /// same object, which no two items the parser compares are. A branch
    /// holds a list of them, and two lists of none are equal.
    pub fn py_eq(&self, other: &Node) -> bool {
        if std::ptr::eq(self, other) {
            return true;
        }
        match (self, other) {
            (Node::Literal(a), Node::Literal(b)) | (Node::NotLiteral(a), Node::NotLiteral(b)) => {
                a == b
            }
            (Node::Any, Node::Any) | (Node::Failure, Node::Failure) => true,
            (Node::In(a), Node::In(b)) => a == b,
            (Node::At(a), Node::At(b)) => a == b,
            (Node::Branch(a), Node::Branch(b)) => a.is_empty() && b.is_empty(),
            _ => false,
        }
    }

    /// The sub-patterns this item holds, in order: a branch's
    /// alternatives, the body of a group, a repeat or a lookaround.
    fn bodies(&self) -> &[SubPattern] {
        match self {
            Node::Branch(alternatives) => alternatives,
            Node::Subpattern { body, .. }
            | Node::MaxRepeat(_, _, body)
            | Node::MinRepeat(_, _, body)
            | Node::Assert(_, body)
            | Node::AssertNot(_, body) => std::slice::from_ref(body),
            Node::Literal(_)
            | Node::NotLiteral(_)
            | Node::Any
            | Node::In(_)
            | Node::At(_)
            | Node::Failure => &[],
        }
    }

    /// A copy of this item with `bodies` for the sub-patterns
    /// [`Node::bodies`] lists, in the same order.
    fn with_bodies(&self, bodies: Vec<SubPattern>) -> Node {
        let mut bodies = bodies.into_iter();
        let mut body = || bodies.next().unwrap_or_default();
        match self {
            Node::Literal(c) => Node::Literal(*c),
            Node::NotLiteral(c) => Node::NotLiteral(*c),
            Node::Any => Node::Any,
            Node::In(set) => Node::In(set.clone()),
            Node::At(at) => Node::At(*at),
            Node::Failure => Node::Failure,
            Node::Branch(alternatives) => {
                Node::Branch(alternatives.iter().map(|_| body()).collect())
            }
            Node::Subpattern {
                group,
                add_flags,
                del_flags,
                ..
            } => Node::Subpattern {
                group: *group,
                add_flags: *add_flags,
                del_flags: *del_flags,
                body: body(),
            },
            Node::MaxRepeat(min, max, _) => Node::MaxRepeat(*min, *max, body()),
            Node::MinRepeat(min, max, _) => Node::MinRepeat(*min, *max, body()),
            Node::Assert(dir, _) => Node::Assert(*dir, body()),
            Node::AssertNot(dir, _) => Node::AssertNot(*dir, body()),
        }
    }
}

/// `SubPattern.data`. Nesting is bounded by the parser, not here; the
/// operations on a tree hold any depth (see the module's text).
#[derive(Default)]
pub struct SubPattern(pub Vec<Node>);

impl SubPattern {
    /// `str(tree)`: the text `SubPattern.__repr__` returns.
    pub fn dump(&self) -> String {
        self.to_string()
    }
}

/// A list copied with a stack of its own: the items copied so far, and
/// the sub-patterns copied so far of the item being copied.
struct Copying<'a> {
    from: &'a [Node],
    done: Vec<Node>,
    bodies: Vec<SubPattern>,
}

impl<'a> Copying<'a> {
    fn of(from: &'a [Node]) -> Copying<'a> {
        let done = Vec::with_capacity(from.len());
        let bodies = Vec::new();
        Copying { from, done, bodies }
    }
}

/// A copy of `items`. A list waits on `stack` while the sub-patterns of
/// its next item are copied, so the depth of a tree is a length on the
/// heap and not a depth of calls.
fn copied(items: &[Node]) -> Vec<Node> {
    let mut stack: Vec<Copying<'_>> = Vec::new();
    let mut top = Copying::of(items);
    loop {
        let from = top.from;
        match from.get(top.done.len()) {
            Some(node) => match node.bodies().get(top.bodies.len()) {
                Some(body) => stack.push(std::mem::replace(&mut top, Copying::of(&body.0))),
                None => {
                    let bodies = std::mem::take(&mut top.bodies);
                    top.done.push(node.with_bodies(bodies));
                }
            },
            None => match stack.pop() {
                Some(mut below) => {
                    below.bodies.push(SubPattern(std::mem::take(&mut top.done)));
                    top = below;
                }
                None => return top.done,
            },
        }
    }
}

impl Clone for SubPattern {
    fn clone(&self) -> SubPattern {
        SubPattern(copied(&self.0))
    }
}

impl Clone for Node {
    fn clone(&self) -> Node {
        self.with_bodies(self.bodies().to_vec())
    }
}

impl Drop for SubPattern {
    /// The items of every sub-pattern below are moved into one list
    /// first, so each item is freed with empty sub-patterns.
    fn drop(&mut self) {
        let mut items = std::mem::take(&mut self.0);
        while let Some(mut node) = items.pop() {
            match &mut node {
                Node::Branch(alternatives) => {
                    for alternative in alternatives {
                        items.append(&mut alternative.0);
                    }
                }
                Node::Subpattern { body, .. }
                | Node::MaxRepeat(_, _, body)
                | Node::MinRepeat(_, _, body)
                | Node::Assert(_, body)
                | Node::AssertNot(_, body) => items.append(&mut body.0),
                Node::Literal(_)
                | Node::NotLiteral(_)
                | Node::Any
                | Node::In(_)
                | Node::At(_)
                | Node::Failure => {}
            }
        }
    }
}

/// A Python list of `items`, as `repr` writes it.
struct List<'a, T>(&'a [T]);

impl<T: fmt::Display> fmt::Display for List<'_, T> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("[")?;
        for (i, item) in self.0.iter().enumerate() {
            if i > 0 {
                f.write_str(", ")?;
            }
            item.fmt(f)?;
        }
        f.write_str("]")
    }
}

/// What is left to write of a dump.
enum Step<'a> {
    Text(&'static str),
    /// The items of a list from the given one on, each after a comma but
    /// the first.
    Items(&'a [Node], usize),
    /// A branch's alternatives from the given one on, each a list.
    Alternatives(&'a [SubPattern], usize),
    Item(&'a Node),
}

/// Writes `first` and all it holds. What is left waits on a stack of its
/// own, so a deep tree is written without a call per level.
fn write_tree(f: &mut fmt::Formatter<'_>, first: Step<'_>) -> fmt::Result {
    let mut stack = vec![first];
    while let Some(step) = stack.pop() {
        match step {
            Step::Text(text) => f.write_str(text)?,
            Step::Items(items, i) => {
                let Some(item) = items.get(i) else { continue };
                if i > 0 {
                    f.write_str(", ")?;
                }
                stack.push(Step::Items(items, i + 1));
                stack.push(Step::Item(item));
            }
            Step::Alternatives(alternatives, i) => {
                let Some(alternative) = alternatives.get(i) else {
                    continue;
                };
                f.write_str(if i > 0 { ", [" } else { "[" })?;
                stack.push(Step::Alternatives(alternatives, i + 1));
                stack.push(Step::Text("]"));
                stack.push(Step::Items(&alternative.0, 0));
            }
            Step::Item(node) => {
                let body = match node {
                    Node::Literal(c) => {
                        write!(f, "(LITERAL, {c})")?;
                        None
                    }
                    Node::NotLiteral(c) => {
                        write!(f, "(NOT_LITERAL, {c})")?;
                        None
                    }
                    Node::Any => {
                        f.write_str("(ANY, None)")?;
                        None
                    }
                    Node::In(set) => {
                        write!(f, "(IN, {})", List(set))?;
                        None
                    }
                    Node::At(at) => {
                        write!(f, "(AT, {at:?})")?;
                        None
                    }
                    Node::Failure => {
                        f.write_str("(FAILURE, ())")?;
                        None
                    }
                    Node::Branch(alternatives) => {
                        f.write_str("(BRANCH, (None, [")?;
                        stack.push(Step::Text("]))"));
                        stack.push(Step::Alternatives(alternatives, 0));
                        None
                    }
                    Node::Subpattern {
                        group,
                        add_flags,
                        del_flags,
                        body,
                    } => {
                        match group {
                            Some(gid) => write!(f, "(SUBPATTERN, ({gid}, ")?,
                            None => f.write_str("(SUBPATTERN, (None, ")?,
                        }
                        write!(f, "{add_flags}, {del_flags}, ")?;
                        Some(body)
                    }
                    Node::MaxRepeat(min, max, body) => {
                        repeat_head(f, "MAX_REPEAT", *min, *max)?;
                        Some(body)
                    }
                    Node::MinRepeat(min, max, body) => {
                        repeat_head(f, "MIN_REPEAT", *min, *max)?;
                        Some(body)
                    }
                    Node::Assert(dir, body) => {
                        write!(f, "(ASSERT, ({dir}, ")?;
                        Some(body)
                    }
                    Node::AssertNot(dir, body) => {
                        write!(f, "(ASSERT_NOT, ({dir}, ")?;
                        Some(body)
                    }
                };
                if let Some(body) = body {
                    f.write_str("[")?;
                    stack.push(Step::Text("]))"));
                    stack.push(Step::Items(&body.0, 0));
                }
            }
        }
    }
    Ok(())
}

/// `(op, (min, max, ` of a repeat. The parser stores the named constant
/// as the maximum of an unbounded repeat, so it prints by name.
fn repeat_head(f: &mut fmt::Formatter<'_>, op: &str, min: u32, max: u32) -> fmt::Result {
    if max == MAXREPEAT {
        write!(f, "({op}, ({min}, MAXREPEAT, ")
    } else {
        write!(f, "({op}, ({min}, {max}, ")
    }
}

impl fmt::Display for SubPattern {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("[")?;
        write_tree(f, Step::Items(&self.0, 0))?;
        f.write_str("]")
    }
}

impl fmt::Debug for SubPattern {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Display::fmt(self, f)
    }
}

impl fmt::Display for Member {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Member::Negate => f.write_str("(NEGATE, None)"),
            Member::Literal(c) => write!(f, "(LITERAL, {c})"),
            Member::Range(lo, hi) => write!(f, "(RANGE, ({lo}, {hi}))"),
            Member::Category(category) => write!(f, "(CATEGORY, {category:?})"),
        }
    }
}

impl fmt::Display for Node {
    /// The item as `repr` writes its tuple.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write_tree(f, Step::Item(self))
    }
}

impl fmt::Debug for Node {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Display::fmt(self, f)
    }
}

/// The `(lo, hi)` pair `getwidth` returns, each at most [`MAXWIDTH`].
pub type Width = (u128, u128);

impl SubPattern {
    /// `getwidth`: the least and the most code points the sub-pattern can
    /// match, each at most [`MAXWIDTH`] (`_parser.py` 177 to 227). The
    /// sums are Python's unbounded integers until the cap at the end of
    /// each sub-pattern; here they saturate at `u128::MAX`, which the cap
    /// makes the same. An item that holds sub-patterns waits on a stack of
    /// its own while their widths are taken, so any depth is measured
    /// without a call per level. The reference caches the width in the
    /// sub-pattern; nothing here changes a tree, so the cache is left out.
    pub fn getwidth(&self) -> Width {
        /// A sub-pattern being summed, and the bodies of its current item
        /// with the least low and the most high of those measured.
        struct Frame<'a> {
            items: &'a [Node],
            at: usize,
            lo: u128,
            hi: u128,
            done: usize,
            i: u128,
            j: u128,
        }
        fn frame(items: &[Node]) -> Frame<'_> {
            let (at, lo, hi, done, i, j) = (0, 0, 0, 0, MAXWIDTH, 0);
            Frame {
                items,
                at,
                lo,
                hi,
                done,
                i,
                j,
            }
        }
        let mut stack = vec![frame(&self.0)];
        while let Some(top) = stack.last_mut() {
            let items = top.items;
            let Some(node) = items.get(top.at) else {
                let width = (top.lo.min(MAXWIDTH), top.hi.min(MAXWIDTH));
                stack.pop();
                match stack.last_mut() {
                    Some(below) => {
                        below.i = below.i.min(width.0);
                        below.j = below.j.max(width.1);
                        below.done += 1;
                        continue;
                    }
                    None => return width,
                }
            };
            if let Some(body) = node.bodies().get(top.done) {
                stack.push(frame(&body.0));
                continue;
            }
            let (i, j) = (top.i, top.j);
            match node {
                Node::Literal(_) | Node::NotLiteral(_) | Node::Any | Node::In(_) => {
                    top.lo = top.lo.saturating_add(1);
                    top.hi = top.hi.saturating_add(1);
                }
                Node::Branch(_) | Node::Subpattern { .. } => {
                    top.lo = top.lo.saturating_add(i);
                    top.hi = top.hi.saturating_add(j);
                }
                Node::MaxRepeat(min, max, _) | Node::MinRepeat(min, max, _) => {
                    top.lo = top.lo.saturating_add(i.saturating_mul(u128::from(*min)));
                    top.hi = if *max == MAXREPEAT && j > 0 {
                        MAXWIDTH
                    } else {
                        top.hi.saturating_add(j.saturating_mul(u128::from(*max)))
                    };
                }
                Node::At(_) | Node::Assert(..) | Node::AssertNot(..) | Node::Failure => {}
            }
            (top.at, top.done, top.i, top.j) = (top.at + 1, 0, MAXWIDTH, 0);
        }
        (0, 0)
    }
}

/// `State`: what the parser keeps while it reads one pattern.
/// `grouprefpos` is left out: only a conditional writes it, and a
/// conditional is refused.
#[derive(Clone, Debug)]
pub struct State {
    pub flags: u32,
    /// `groupdict`. A dict keeps the order names were added in; here that
    /// order is the order of the group numbers.
    pub groupdict: HashMap<Vec<u32>, usize>,
    /// `groupwidths`: `None` for group 0 and for a group still open.
    pub groupwidths: Vec<Option<Width>>,
    pub lookbehindgroups: Option<usize>,
}

impl Default for State {
    fn default() -> State {
        State::new()
    }
}

impl State {
    pub fn new() -> State {
        State {
            flags: 0,
            groupdict: HashMap::new(),
            groupwidths: vec![None],
            lookbehindgroups: None,
        }
    }

    /// `groups`: the count of groups opened, group 0 included.
    pub fn groups(&self) -> usize {
        self.groupwidths.len()
    }

    /// `opengroup`. The error carries no position: `_parse` adds it.
    pub fn opengroup(&mut self, name: Option<&[u32]>) -> Result<usize, Why> {
        self.opengroup_within(name, MAXGROUPS)
    }

    /// `opengroup` with the module's `MAXGROUPS` as an argument, so that a
    /// test reaches "too many groups". The group is counted before either
    /// refusal, and a name is hashed once.
    pub fn opengroup_within(&mut self, name: Option<&[u32]>, limit: usize) -> Result<usize, Why> {
        let gid = self.groups();
        self.groupwidths.push(None);
        if self.groups() > limit {
            return Err(Why::TooManyGroups);
        }
        if let Some(name) = name {
            match self.groupdict.entry(name.to_vec()) {
                Entry::Occupied(_) => return Err(Why::Redefinition),
                Entry::Vacant(free) => free.insert(gid),
            };
        }
        Ok(gid)
    }

    /// `closegroup(gid, p)`, given the width `p.getwidth()` returns.
    /// False where the reference raises IndexError: no group `gid`.
    pub fn closegroup(&mut self, gid: usize, width: Width) -> bool {
        let slot = self.groupwidths.get_mut(gid);
        slot.map(|slot| *slot = Some(width)).is_some()
    }

    /// `checkgroup`: the group exists and is closed.
    pub fn checkgroup(&self, gid: usize) -> bool {
        self.groupwidths.get(gid).is_some_and(Option::is_some)
    }

    /// `checklookbehindgroup`. The error's position is `source.tell()`.
    pub fn checklookbehindgroup(&self, gid: usize) -> Result<(), Why> {
        if let Some(first) = self.lookbehindgroups {
            if !self.checkgroup(gid) {
                return Err(Why::OpenGroup);
            }
            if gid >= first {
                return Err(Why::LookbehindGroup);
            }
        }
        Ok(())
    }
}

/// Why `re` rejects a pattern: one name for each message `re.error`
/// carries, here those of `State`, the `Tokenizer`, the two escape
/// readers and a set. A later unit adds the names of what it raises.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Why {
    /// "bad escape (end of pattern)".
    BadEscapeEnd,
    /// "bad escape %s".
    BadEscape,
    /// "incomplete escape %s".
    IncompleteEscape,
    /// "octal escape value %s outside of range 0-0o377".
    OctalRange,
    /// "missing {".
    MissingBrace,
    /// "missing character name" and "missing group name".
    MissingCharacterName,
    MissingGroupName,
    /// "missing %s, unterminated name".
    UnterminatedName,
    /// "invalid group reference %d".
    InvalidGroupReference,
    /// "cannot refer to an open group".
    OpenGroup,
    /// "cannot refer to group defined in the same lookbehind subpattern".
    LookbehindGroup,
    /// "unterminated character set".
    UnterminatedSet,
    /// "bad character range %s-%s".
    BadRange,
    /// "unbalanced parenthesis".
    UnbalancedParenthesis,
    /// "too many groups".
    TooManyGroups,
    /// "redefinition of group name %r as group %d; was group %d".
    Redefinition,
    /// "nothing to repeat" (pyre-3).
    NothingToRepeat,
    /// "multiple repeat".
    MultipleRepeat,
    /// "min repeat greater than max repeat".
    MinGreaterThanMax,
    /// "unexpected end of pattern" (pyre-3b, the groups).
    UnexpectedEnd,
    /// "unknown extension ?" with what follows it: `?P`, `?<` and the
    /// character read.
    UnknownExtension,
    /// "unknown group name %r".
    UnknownGroupName,
    /// "missing ), unterminated comment".
    UnterminatedComment,
    /// "missing ), unterminated subpattern".
    UnterminatedSubpattern,
    /// "global flags not at the start of the expression".
    GlobalFlagsNotAtStart,
    /// "bad character in group name %r" (and `%a` in a bytes pattern).
    BadGroupName,
    /// The errors of `_parse_flags`: "bad inline flags: cannot use 'L'
    /// flag with a str pattern", "... 'u' flag with a bytes pattern",
    /// "... flags 'a', 'u' and 'L' are incompatible", "missing -, : or )",
    /// "unknown flag", "missing flag", "bad inline flags: cannot turn off
    /// flags 'a', 'u' and 'L'", "missing :", "bad inline flags: flag
    /// turned on and off".
    InlineLocaleStr,
    InlineUnicodeBytes,
    InlineIncompatible,
    MissingFlagEnd,
    UnknownFlag,
    MissingFlag,
    TurnOffTypeFlags,
    MissingColon,
    FlagOnAndOff,
    /// The ValueErrors of `fix_flags` ([`BuildError::Value`]): "cannot use
    /// LOCALE flag with a str pattern", "ASCII and UNICODE flags are
    /// incompatible", "cannot use UNICODE flag with a bytes pattern",
    /// "ASCII and LOCALE flags are incompatible".
    LocaleStr,
    AsciiUnicode,
    UnicodeBytes,
    AsciiLocale,
}

/// What the engine refuses of the patterns `re` accepts, each by the unit
/// named.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Feature {
    /// `\1` and `(?P=name)`.
    Backreference,
    /// `\N{...}`: the table of character names is not carried.
    NamedCharacter,
    /// A set `re` warns about (FutureWarning): `[[` at its start, `--`,
    /// `&&`, `~~` or `||` after a member, or a range that ends in `-`.
    /// The port cannot warn, so it refuses.
    SetWarning,
    /// `re.VERBOSE`, and below it the other flags the engine has no rule for.
    Verbose,
    Ascii,
    Locale,
    Debug,
    /// `(?(1)...)`, `(?>...)`, `*+` and its kin (pyre-3).
    Conditional,
    AtomicGroup,
    Possessive,
    /// `\B`, `\D`, `\W` (the tree holds them; pyre-3's predicate refuses).
    NotBoundary,
    NotDigit,
    NotWord,
    /// A repeat of more than one over a body that can match empty; a
    /// capture under such a repeat or inside a lookaround; a choice
    /// inside a lookbehind (pyre-3).
    NullableLoop,
    CaptureInRepeat,
    CaptureInLookaround,
    ChoiceInLookbehind,
    /// A class member past the BMP under IGNORECASE; a category, `\b` or
    /// IGNORECASE on a bytes pattern (pyre-4).
    ClassPastBmpIgnorecase,
    BytesCategory,
    BytesBoundary,
    BytesIgnorecase,
    /// Nesting past 32; more than 4,096 nodes (pyre-3, pyre-4).
    Nesting,
    Nodes,
    /// A replacement text that holds a backslash (pyre-6).
    Template,
    /// `(` and a second alternative, refused by `atom::parse_flat` alone,
    /// which reads atoms and repeats only.
    Group,
    Alternation,
    /// A group name in a str pattern that holds a code point past ASCII:
    /// `str.isidentifier` needs the Unicode data of XID, which the crate
    /// does not carry (pyre-3b).
    UnicodeGroupName,
}

/// Why a pattern is not built. `at` is a position as Python counts it
/// (`re.error.pos`), never negative for a pattern read from its start.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BuildError {
    /// `re` rejects it too: `re.error` with this message at this `pos`.
    Invalid { at: isize, why: Why },
    /// `re` accepts it and the engine does not. `at` is where the
    /// feature begins (the backslash of an escape), or for a set `re`
    /// warns about, the position its warning names.
    Unsupported { at: isize, what: Feature },
    /// `re` raises OverflowError, which is not `re.error` and has no
    /// position: "the repetition number is too large", a bound of a
    /// repeat at `MAXREPEAT` or past it.
    Overflow,
    /// `int` raises ValueError, which is not `re.error` and has no
    /// position: a bound of a repeat of more than 4,300 digits, the
    /// default of `sys.get_int_max_str_digits()`.
    DigitLimit,
    /// `re` raises ValueError, which is not `re.error` and has no
    /// position: `fix_flags` on flags that do not fit the pattern's type.
    Value(Why),
}
