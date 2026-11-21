//! `re._parser.parse` (CPython 3.13.13, `Lib/re/_parser.py`): the repeats
//! of `_parse` (638 to 703, unit pyre-3a), and `Tokenizer.checkgroupname`
//! (303 to 309), `_parse_sub` (451 to 509), the groups, lookarounds and
//! inline flags of `_parse` (708 to 873), the unpacking of plain groups
//! (884 to 892), `_parse_flags`, `fix_flags` and `parse` (894 to 995,
//! unit pyre-3b). [`refusal`] is no line of `re`: the matcher's subset
//! over the finished tree, which `parse` asks last (unit pyre-3c).
//!
//! The repeats: `?`, `*`, `+` and `{m,n}` over the item before them, the
//! lazy `?` after one, and the errors `re` raises there.
//!
//! [`repeat`] is called with the repeat character already taken, as
//! `_parse` holds it in `this`, and the items read so far. It replaces the
//! last item with the repeat over it, or appends a literal `{` where `re`
//! reads one so. A possessive `+` is refused where `re` would accept it,
//! after every check of the repeat, so an error `re` raises on the same
//! text comes first. "the repetition number is too large" is the
//! OverflowError of `re`, not `re.error`, and is [`BuildError::Overflow`];
//! a bound past `int`'s digit limit is its ValueError, [`BuildError::DigitLimit`].

use super::atom::{item_escape, set, uniq, verbose_skips};
use super::token::{Built, Tok, Tokenizer};
use super::tree::{
    At, BuildError, Category, Feature, Member, Node, State, SubPattern, Why, ASCII, DEBUG, LOCALE,
    MAXREPEAT, UNICODE, VERBOSE,
};

/// `while source.next in DIGITS: lo += sourceget()`: the ASCII digits
/// next, each a token without a backslash.
fn digits(source: &mut Tokenizer<'_>) -> Built<Vec<u32>> {
    source.getwhile(usize::MAX, |c| (0x30..=0x39).contains(&c))
}

/// The default of `sys.get_int_max_str_digits()`: `int` refuses a decimal
/// text of more digits than this (leading zeros included) with ValueError.
pub const INT_MAX_STR_DIGITS: usize = 4300;

/// `int(digits)` below `MAXREPEAT`, or the OverflowError `re` raises at
/// it or past it. Leading zeros count for nothing, as in `int`, but count
/// toward its limit: past [`INT_MAX_STR_DIGITS`] digits `int` raises
/// ValueError, [`BuildError::DigitLimit`], before any bound is checked.
fn count(digits: &[u32]) -> Built<u32> {
    if digits.len() > INT_MAX_STR_DIGITS {
        return Err(BuildError::DigitLimit);
    }
    let mut n: u64 = 0;
    for &c in digits {
        n = (n * 10 + u64::from(c - 0x30)).min(u64::from(MAXREPEAT));
    }
    u32::try_from(n)
        .ok()
        .filter(|&n| n < MAXREPEAT)
        .ok_or(BuildError::Overflow)
}

/// The bounds of `{m,n}` after its `{`, which ended at `here`. `None`
/// where `re` reads the `{` as a literal: before a `}`, or where no `}`
/// closes the digits, and then the tokenizer is back after the `{`.
fn braces(source: &mut Tokenizer<'_>, here: isize) -> Built<Option<(u32, u32)>> {
    if source.next.is_some_and(|next| next.is('}')) {
        return Ok(None);
    }
    let lo = digits(source)?;
    let hi = if source.matches(',')? {
        digits(source)?
    } else {
        lo.clone()
    };
    if !source.matches('}')? {
        source.seek(usize::try_from(here).unwrap_or(0))?;
        return Ok(None);
    }
    let (mut min, mut max) = (0, MAXREPEAT);
    if !lo.is_empty() {
        min = count(&lo)?;
    }
    if !hi.is_empty() {
        max = count(&hi)?;
        if max < min {
            let why = Why::MinGreaterThanMax;
            return Err(BuildError::Invalid { at: here, why });
        }
    }
    Ok(Some((min, max)))
}

/// The repeat branch of `_parse`: `this` is the repeat character just
/// taken, `items` the sub-pattern read so far. "nothing to repeat" and
/// "multiple repeat" are placed at the repeat character. A group without
/// a number or flags is repeated as its body, as `_parse` unpacks it.
pub fn repeat(source: &mut Tokenizer<'_>, this: Tok, items: &mut Vec<Node>) -> Built<()> {
    let here = source.tell();
    let (min, max) = match char::from_u32(this.c) {
        Some('?') => (0, 1),
        Some('*') => (0, MAXREPEAT),
        Some('+') => (1, MAXREPEAT),
        _ => match braces(source, here)? {
            Some(bounds) => bounds,
            None => {
                items.push(Node::Literal(this.c));
                return Ok(());
            }
        },
    };
    let at = here.saturating_sub(1);
    let why = match items.last() {
        None | Some(Node::At(_)) => Some(Why::NothingToRepeat),
        Some(Node::MaxRepeat(..) | Node::MinRepeat(..)) => Some(Why::MultipleRepeat),
        Some(_) => None,
    };
    if let Some(why) = why {
        return Err(BuildError::Invalid { at, why });
    }
    let body = match items.pop() {
        Some(Node::Subpattern {
            group: None,
            add_flags: 0,
            del_flags: 0,
            body,
        }) => body,
        last => SubPattern(last.into_iter().collect()),
    };
    let node = if source.matches('?')? {
        Node::MinRepeat(min, max, body)
    } else if source.matches('+')? {
        return Err(source.refuse(Feature::Possessive, 1));
    } else {
        Node::MaxRepeat(min, max, body)
    };
    items.push(node);
    Ok(())
}

/// How deep groups and lookarounds may nest before the parser refuses
/// (`Feature::Nesting`, at the position after the group's opening). The
/// reference recurses twice per level and raises RecursionError past about
/// 495 levels under its default recursion limit, a bound of the caller's
/// stack and not of the text; the port refuses well before either stack
/// is at risk.
pub const MAX_DEPTH: usize = 100;

/// `TYPE_FLAGS`.
const TYPE_FLAGS: u32 = ASCII | LOCALE | UNICODE;

/// `FLAGS[char]`: the flag an inline flag letter names.
fn flag_of(token: Tok) -> Option<u32> {
    if token.esc {
        return None;
    }
    let flag = match char::from_u32(token.c)? {
        'i' => super::tree::IGNORECASE,
        'L' => LOCALE,
        'm' => super::tree::MULTILINE,
        's' => super::tree::DOTALL,
        'x' => VERBOSE,
        'a' => ASCII,
        'u' => UNICODE,
        _ => return None,
    };
    Some(flag)
}

/// `char.isalpha()` of a token: an escaped one is two characters, the
/// first a backslash, and so never alphabetic.
fn isalpha(token: Tok) -> bool {
    !token.esc && crate::unicode::is_alpha(token.c)
}

/// `str.isidentifier` of an ASCII name: a letter or `_`, then letters,
/// digits and `_`. A name past ASCII never reaches it (`checkgroupname`).
fn isidentifier(name: &[u32]) -> bool {
    let word = |c: &u32| char::from_u32(*c).is_some_and(|c| c.is_ascii_alphanumeric() || c == '_');
    match name.split_first() {
        Some((first, rest)) => {
            word(first) && !(0x30..=0x39).contains(first) && rest.iter().all(word)
        }
        None => false,
    }
}

/// `fix_flags`: the flags of a str pattern gain UNICODE unless ASCII is
/// set. The four conflicts are `re`'s ValueError.
pub fn fix_flags(istext: bool, mut flags: u32) -> Result<u32, Why> {
    if istext {
        if flags & LOCALE != 0 {
            return Err(Why::LocaleStr);
        }
        if flags & ASCII == 0 {
            flags |= UNICODE;
        } else if flags & UNICODE != 0 {
            return Err(Why::AsciiUnicode);
        }
    } else {
        if flags & UNICODE != 0 {
            return Err(Why::UnicodeBytes);
        }
        if flags & LOCALE != 0 && flags & ASCII != 0 {
            return Err(Why::AsciiLocale);
        }
    }
    Ok(flags)
}

/// `parse(str, flags)`: the tree and the state it leaves (`state.flags`
/// as `fix_flags` returns them). `istext` is false for a bytes pattern,
/// given as its bytes. DEBUG, whose dump the reference prints, is refused
/// at 0 after every check. A conditional is refused where it begins, so
/// `grouprefpos` is never written and its check is left out.
///
/// What `re` accepts and the matcher has no rule for is then refused by
/// [`refusal`], at 0: the finished tree holds no positions.
pub fn parse(pattern: &[u32], istext: bool, flags: u32) -> Built<(SubPattern, State)> {
    let (tree, state) = parsed(pattern, istext, flags, false)?;
    match refusal(&tree, state.flags) {
        Some(what) => Err(BuildError::Unsupported { at: 0, what }),
        None => Ok((tree, state)),
    }
}

/// How deep sub-patterns may nest for the matcher: a body of an item at
/// the top is at depth 1, and a body at depth 33 is refused
/// (`Feature::Nesting`).
pub const MATCH_DEPTH: usize = 32;

/// Where the walk of [`refusal`] stands: how deep, under a repeat of more
/// than one, inside a lookaround, inside a lookbehind.
#[derive(Clone, Copy, Default)]
struct Within {
    depth: usize,
    repeat: bool,
    look: bool,
    behind: bool,
}

/// What a flag set the matcher has no rule for is refused as: VERBOSE,
/// ASCII, LOCALE, in that order.
fn flag_refusal(flags: u32) -> Option<Feature> {
    [
        (VERBOSE, Feature::Verbose),
        (ASCII, Feature::Ascii),
        (LOCALE, Feature::Locale),
    ]
    .into_iter()
    .find(|(flag, _)| flags & flag != 0)
    .map(|(_, what)| what)
}

/// The refusal of one item, its bodies aside.
fn item_refusal(node: &Node, within: Within) -> Option<Feature> {
    match node {
        Node::At(At::AT_NON_BOUNDARY) => Some(Feature::NotBoundary),
        Node::In(members) => members.iter().find_map(|member| match member {
            Member::Category(Category::CATEGORY_NOT_DIGIT) => Some(Feature::NotDigit),
            Member::Category(Category::CATEGORY_NOT_WORD) => Some(Feature::NotWord),
            _ => None,
        }),
        Node::Subpattern {
            group,
            add_flags,
            del_flags,
            ..
        } => match group {
            Some(_) if within.look => Some(Feature::CaptureInLookaround),
            Some(_) if within.repeat => Some(Feature::CaptureInRepeat),
            _ => flag_refusal(add_flags | del_flags),
        },
        Node::MaxRepeat(_, max, body) | Node::MinRepeat(_, max, body)
            if *max > 1 && body.getwidth().0 == 0 =>
        {
            Some(Feature::NullableLoop)
        }
        Node::Branch(_) if within.behind => Some(Feature::ChoiceInLookbehind),
        _ => None,
    }
}

/// What the matcher refuses in a tree `parse` returns under `flags` (the
/// state's, after `fix_flags`): first the flags, then each item in the
/// order a depth-first walk meets it, an item before its bodies. A
/// refusal belongs to no line of `re`: it is the engine's subset
/// (decision `pyre-design-2026-11-19`). The walk keeps its own stack, so
/// a tree of any depth is read without a call per level.
pub fn refusal(tree: &SubPattern, flags: u32) -> Option<Feature> {
    if let Some(what) = flag_refusal(flags) {
        return Some(what);
    }
    let mut stack = vec![(tree.0.iter(), Within::default())];
    while let Some((items, within)) = stack.last_mut() {
        let within = *within;
        let Some(node) = items.next() else {
            stack.pop();
            continue;
        };
        if let Some(what) = item_refusal(node, within) {
            return Some(what);
        }
        let inner = Within {
            depth: within.depth + 1,
            repeat: within.repeat
                || matches!(node, Node::MaxRepeat(_, max, _) | Node::MinRepeat(_, max, _) if *max > 1),
            look: within.look || matches!(node, Node::Assert(..) | Node::AssertNot(..)),
            behind: within.behind || matches!(node, Node::Assert(-1, _) | Node::AssertNot(-1, _)),
        };
        let bodies = node.bodies();
        if !bodies.is_empty() && inner.depth > MATCH_DEPTH {
            return Some(Feature::Nesting);
        }
        stack.extend(bodies.iter().rev().map(|body| (body.0.iter(), inner)));
    }
    None
}

/// [`parse`], or with `flat` the reader of `atom::parse_flat`.
pub(super) fn parsed(
    pattern: &[u32],
    istext: bool,
    flags: u32,
    flat: bool,
) -> Built<(SubPattern, State)> {
    let source = Tokenizer::new(pattern, istext)?;
    let mut state = State::new();
    state.flags = flags;
    let mut parser = Parser {
        source,
        state,
        flat,
        depth: 0,
    };
    let tree = parser.sub(flags & VERBOSE != 0, false)?;
    let Parser {
        source, mut state, ..
    } = parser;
    state.flags = fix_flags(istext, state.flags).map_err(BuildError::Value)?;
    if source.next.is_some() {
        return Err(source.error(Why::UnbalancedParenthesis, 0));
    }
    if flags & DEBUG != 0 {
        return Err(BuildError::Unsupported {
            at: 0,
            what: Feature::Debug,
        });
    }
    Ok((tree, state))
}

/// The reader's own state: `source` and `state` as `_parse` passes them,
/// whether it reads atoms only, and how deep it is in groups.
struct Parser<'a> {
    source: Tokenizer<'a>,
    state: State,
    flat: bool,
    depth: usize,
}

/// What the `(` branch leaves: an item, or nothing (a comment, global
/// flags).
type Opened = Option<Node>;

impl Parser<'_> {
    fn invalid(&self, at: isize, why: Why) -> BuildError {
        BuildError::Invalid { at, why }
    }

    /// `Tokenizer.checkgroupname(name, offset)`. A str name past ASCII is
    /// refused at the name, where `re` would place its error.
    fn checkgroupname(&self, name: &[u32], offset: usize) -> Built<()> {
        let ascii = name.iter().all(|&c| c < 0x80);
        if !ascii && self.source.istext {
            return Err(self
                .source
                .refuse(Feature::UnicodeGroupName, name.len() + offset));
        }
        if !ascii || !isidentifier(name) {
            return Err(self.source.error(Why::BadGroupName, name.len() + offset));
        }
        Ok(())
    }

    /// `_parse_sub`: alternatives split at `|`, their common prefix taken
    /// out (items compared by `Node::py_eq`), and alternatives of one
    /// literal or one set each folded into one set. `nested` is false at
    /// the top, where a leading `(?x)` turns VERBOSE on for the next
    /// alternative.
    fn sub(&mut self, mut verbose: bool, nested: bool) -> Built<SubPattern> {
        if nested {
            self.depth += 1;
            if self.depth > MAX_DEPTH {
                return Err(self.source.refuse(Feature::Nesting, 0));
            }
        }
        let mut items: Vec<SubPattern> = Vec::new();
        loop {
            if self.flat && !items.is_empty() {
                return Err(self.source.refuse(Feature::Alternation, 1));
            }
            let first = !nested && items.is_empty();
            items.push(self.items(verbose, first)?);
            if !self.source.matches('|')? {
                break;
            }
            if !nested {
                verbose = self.state.flags & VERBOSE != 0;
            }
        }
        if nested {
            self.depth -= 1;
        }
        if items.len() == 1 {
            return Ok(items.pop().unwrap_or_default());
        }
        let mut out = Vec::new();
        // The common prefix, compared at one index after another: the
        // reference deletes the first item of each alternative per step.
        let shortest = items.iter().map(|item| item.0.len()).min().unwrap_or(0);
        let mut k = 0;
        while k < shortest
            && items[1..]
                .iter()
                .all(|item| item.0[k].py_eq(&items[0].0[k]))
        {
            k += 1;
        }
        if k > 0 {
            out.extend(items[0].0.drain(..k));
            for item in &mut items[1..] {
                item.0.drain(..k);
            }
        }
        let mut members = Vec::new();
        let folds = items.iter().all(|item| match item.0.as_slice() {
            [Node::Literal(c)] => {
                members.push(Member::Literal(*c));
                true
            }
            [Node::In(set)] if set.first() != Some(&Member::Negate) => {
                members.extend_from_slice(set);
                true
            }
            _ => false,
        });
        out.push(if folds {
            Node::In(uniq(members))
        } else {
            Node::Branch(items)
        });
        Ok(SubPattern(out))
    }

    /// `_parse`: the items up to a `|`, a `)` or the end, with plain
    /// groups unpacked. `first` is true for the first alternative at the
    /// top, the only place global flags may stand.
    fn items(&mut self, mut verbose: bool, first: bool) -> Built<SubPattern> {
        let mut items = Vec::new();
        while let Some(this) = self.source.next {
            if this.is('|') || this.is(')') {
                break;
            }
            self.source.get()?;
            if verbose && !this.esc && verbose_skips(this.c) {
                return Err(self.source.refuse(Feature::Verbose, 1));
            }
            if this.esc {
                items.push(item_escape(&mut self.source, this.c, &self.state)?);
                continue;
            }
            let item = match char::from_u32(this.c) {
                Some('*' | '+' | '?' | '{') => {
                    repeat(&mut self.source, this, &mut items)?;
                    continue;
                }
                Some('[') => set(&mut self.source)?,
                Some('.') => Node::Any,
                Some('^') => Node::At(At::AT_BEGINNING),
                Some('$') => Node::At(At::AT_END),
                Some('(') if self.flat => return Err(self.source.refuse(Feature::Group, 1)),
                Some('(') => match self.group(&mut verbose, first && items.is_empty())? {
                    Some(item) => item,
                    None => continue,
                },
                _ => Node::Literal(this.c),
            };
            items.push(item);
        }
        let mut unpacked = Vec::with_capacity(items.len());
        for item in items {
            match item {
                Node::Subpattern {
                    group: None,
                    add_flags: 0,
                    del_flags: 0,
                    mut body,
                } => unpacked.append(&mut body.0),
                item => unpacked.push(item),
            }
        }
        Ok(SubPattern(unpacked))
    }

    /// The `(` branch of `_parse`, after the `(`. `start` is the `(`, where
    /// the errors of an unterminated group and the refusals of a
    /// conditional, an atomic group and `(?P=name)` are placed.
    fn group(&mut self, verbose: &mut bool, at_start: bool) -> Built<Opened> {
        let start = self.source.tell() - 1;
        let (mut capture, mut name, mut flags) = (true, None, (0, 0));
        if self.source.matches('?')? {
            let Some(ch) = self.source.get()? else {
                return Err(self.source.error(Why::UnexpectedEnd, 0));
            };
            if ch.is('P') {
                if self.source.matches('<')? {
                    let named = self.source.getuntil('>', Why::MissingGroupName)?;
                    self.checkgroupname(&named, 1)?;
                    name = Some(named);
                } else if self.source.matches('=')? {
                    let named = self.source.getuntil(')', Why::MissingGroupName)?;
                    self.checkgroupname(&named, 1)?;
                    let offset = named.len() + 1;
                    let Some(&gid) = self.state.groupdict.get(&named) else {
                        return Err(self.source.error(Why::UnknownGroupName, offset));
                    };
                    if !self.state.checkgroup(gid) {
                        return Err(self.source.error(Why::OpenGroup, offset));
                    }
                    if let Err(why) = self.state.checklookbehindgroup(gid) {
                        return Err(self.source.error(why, 0));
                    }
                    let what = Feature::Backreference;
                    return Err(BuildError::Unsupported { at: start, what });
                } else {
                    let Some(ch) = self.source.get()? else {
                        return Err(self.source.error(Why::UnexpectedEnd, 0));
                    };
                    return Err(self.source.error(Why::UnknownExtension, ch.width() + 2));
                }
            } else if ch.is(':') {
                capture = false;
            } else if ch.is('#') {
                loop {
                    if self.source.next.is_none() {
                        return Err(self.invalid(start, Why::UnterminatedComment));
                    }
                    if self.source.get()?.is_some_and(|t| t.is(')')) {
                        return Ok(None);
                    }
                }
            } else if ch.is('=') || ch.is('!') || ch.is('<') {
                return self.lookaround(ch, start, *verbose).map(Some);
            } else if ch.is('(') || ch.is('>') {
                let what = if ch.is('(') {
                    Feature::Conditional
                } else {
                    Feature::AtomicGroup
                };
                return Err(BuildError::Unsupported { at: start, what });
            } else if flag_of(ch).is_some() || ch.is('-') {
                match self.parse_flags(ch)? {
                    None => {
                        if !at_start {
                            return Err(self.invalid(start, Why::GlobalFlagsNotAtStart));
                        }
                        *verbose = self.state.flags & VERBOSE != 0;
                        return Ok(None);
                    }
                    Some(scoped) => (capture, flags) = (false, scoped),
                }
            } else {
                return Err(self.source.error(Why::UnknownExtension, ch.width() + 1));
            }
        }
        let group = if capture {
            let gid = self.state.opengroup(name.as_deref());
            // The reference takes `len(name) + 1` here; with no name that
            // is a TypeError (README, reference findings).
            let offset = name.as_ref().map_or(1, |name| name.len() + 1);
            Some(gid.map_err(|why| self.source.error(why, offset))?)
        } else {
            None
        };
        let (add_flags, del_flags) = flags;
        let sub_verbose = (*verbose || add_flags & VERBOSE != 0) && del_flags & VERBOSE == 0;
        let body = self.sub(sub_verbose, true)?;
        if !self.source.matches(')')? {
            return Err(self.invalid(start, Why::UnterminatedSubpattern));
        }
        if let Some(gid) = group {
            self.state.closegroup(gid, body.getwidth());
        }
        Ok(Some(Node::Subpattern {
            group,
            add_flags,
            del_flags,
            body,
        }))
    }

    /// `(?=`, `(?!`, `(?<=`, `(?<!`, after the `=`, `!` or `<`.
    fn lookaround(&mut self, ch: Tok, start: isize, verbose: bool) -> Built<Node> {
        let (mut kind, mut dir, mut outer) = (ch, 1, None);
        if ch.is('<') {
            let Some(next) = self.source.get()? else {
                return Err(self.source.error(Why::UnexpectedEnd, 0));
            };
            if !(next.is('=') || next.is('!')) {
                return Err(self.source.error(Why::UnknownExtension, next.width() + 2));
            }
            (kind, dir) = (next, -1);
            outer = Some(self.state.lookbehindgroups);
            if self.state.lookbehindgroups.is_none() {
                self.state.lookbehindgroups = Some(self.state.groups());
            }
        }
        let body = self.sub(verbose, true)?;
        if outer == Some(None) {
            self.state.lookbehindgroups = None;
        }
        if !self.source.matches(')')? {
            return Err(self.invalid(start, Why::UnterminatedSubpattern));
        }
        Ok(if kind.is('=') {
            Node::Assert(dir, body)
        } else if body.0.is_empty() {
            Node::Failure
        } else {
            Node::AssertNot(dir, body)
        })
    }

    /// `_parse_flags`, after the first flag letter or `-`: `None` for
    /// global flags, which it adds to `state.flags`, or the flags a scoped
    /// group adds and removes. "cannot turn on (off) global flag" is
    /// unreachable: no flag letter names DEBUG, the one global flag.
    fn parse_flags(&mut self, mut ch: Tok) -> Built<Option<(u32, u32)>> {
        let (mut add, mut del) = (0, 0);
        let istext = self.source.istext;
        if !ch.is('-') {
            loop {
                let flag = flag_of(ch).unwrap_or(0);
                if istext && ch.is('L') {
                    return Err(self.source.error(Why::InlineLocaleStr, 0));
                }
                if !istext && ch.is('u') {
                    return Err(self.source.error(Why::InlineUnicodeBytes, 0));
                }
                add |= flag;
                if flag & TYPE_FLAGS != 0 && add & TYPE_FLAGS != flag {
                    return Err(self.source.error(Why::InlineIncompatible, 0));
                }
                ch = self.next_flag(Why::MissingFlagEnd)?;
                if ch.is(')') || ch.is('-') || ch.is(':') {
                    break;
                }
                self.known(ch, Why::MissingFlagEnd)?;
            }
        }
        if ch.is(')') {
            self.state.flags |= add;
            return Ok(None);
        }
        if ch.is('-') {
            ch = self.next_flag(Why::MissingFlag)?;
            self.known(ch, Why::MissingFlag)?;
            loop {
                let flag = flag_of(ch).unwrap_or(0);
                if flag & TYPE_FLAGS != 0 {
                    return Err(self.source.error(Why::TurnOffTypeFlags, 0));
                }
                del |= flag;
                ch = self.next_flag(Why::MissingColon)?;
                if ch.is(':') {
                    break;
                }
                self.known(ch, Why::MissingColon)?;
            }
        }
        if add & del != 0 {
            return Err(self.source.error(Why::FlagOnAndOff, 1));
        }
        Ok(Some((add, del)))
    }

    /// `char = sourceget()`, with `missing` at the end of the pattern.
    fn next_flag(&mut self, missing: Why) -> Built<Tok> {
        match self.source.get()? {
            Some(ch) => Ok(ch),
            None => Err(self.source.error(missing, 0)),
        }
    }

    /// `if char not in FLAGS`: "unknown flag" for a letter, else `other`.
    fn known(&self, ch: Tok, other: Why) -> Built<()> {
        if flag_of(ch).is_some() {
            return Ok(());
        }
        let why = if isalpha(ch) { Why::UnknownFlag } else { other };
        Err(self.source.error(why, ch.width()))
    }
}
