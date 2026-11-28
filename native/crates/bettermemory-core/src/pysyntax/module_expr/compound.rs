//! Blocks and the compound statements `if`, `while`, `for` and `with`
//! (and `async for` and `async with`) of CPython 3.13.13's grammar
//! (`compound_stmt`, `block`), as `ast.parse(source)` in
//! `claims.check_claim` (`claims.py` 672) reads them over [`super`]'s
//! expressions and [`super::stmt`]'s simple statements, and the clauses
//! of `try` (`except`, `except*`, `else`, `finally`). Nothing recurses:
//! the compound statements whose indented block is being read wait on a
//! stack, and an `elif` chain or a `try`'s clauses are read in a loop. The
//! heads of definitions, `try`, decorators and handlers are
//! [`super::definition`]'s, `match` goes to [`super::matching`].

use super::stmt::STATEMENT_PASS;
use super::*;

// Where each slot lies past its statement's place, read off the
// interpreter by bisection on a `-` chain (`pysyntax_compound.jsonl` pins
// each boundary pair).
/// `if`'s and `while`'s test (`named_expression`): `if ` + `-` * 5970 +
/// `x: pass` parses, one more overflows. The k-th `elif`'s test lies k
/// calls further.
const TEST: i32 = -1;
/// A block on its head's line (`simple_stmts`); after the k-th `elif`, k
/// calls further.
const LINE: i32 = 3;
/// An `else` block on its line, one call past the last branch's block.
const ELSE: i32 = 4;
/// An indented block's statements past the same block on the head's line
/// (`NEWLINE INDENT statements DEDENT`): 6 per level of nested `if`.
const INDENTED: i32 = 3;
/// A later element of a `for` target, and a starred one.
const LATER_TARGET: i32 = 2;
/// A `with` item after the first, and an item's target past its item.
const LATER_ITEM: i32 = 1;
const AS_TARGET: i32 = 1;
/// An `else` or `finally` block on its line after a `try`'s clauses,
/// however many handlers there are (`except_block+` is a loop).
const TRY_ELSE: i32 = 4;
/// The second pass's `invalid_for_target`, which reads `star_expressions`
/// after `for` (`for ` + `-` * 5964 + `x in y: pass` raises SyntaxError,
/// one more MemoryError). It changes no answer: `read_on` drops a stack
/// refusal, and the second pass's bound declines first (from 5,920).
const FOR_PASS: i32 = 5;

/// One branch of a compound statement: where it starts, its head's nodes
/// (a test, a target and iterable, the items, a definition's `arguments`
/// or a class's bases and keywords, a handler's type; none for `else`),
/// its block's statements so far, the nodes `ast` holds after the block
/// (a definition's decorators and return annotation), the identifier of
/// a definition or a handler's alias, and where its block ends.
pub(super) struct Branch {
    pub(super) start: u32,
    pub(super) head: Vec<u32>,
    pub(super) body: Vec<u32>,
    pub(super) tail: Vec<u32>,
    pub(super) name: Option<Box<[u32]>>,
    pub(super) end: u32,
}

impl Branch {
    pub(super) fn new(start: u32, head: Vec<u32>) -> Self {
        Branch {
            start,
            head,
            body: Vec::new(),
            tail: Vec::new(),
            name: None,
            end: 0,
        }
    }
}

/// A compound statement being read: its class, its place, its branches
/// (`if` and each `elif`, then `else` when `els`; a `try`'s block, its
/// handlers, then `else` when `els` and `finally` when `fin`) and the
/// place of the block being read on its head's line.
pub(super) struct Open {
    pub(super) kind: &'static str,
    pub(super) place: i32,
    pub(super) line: i32,
    pub(super) els: bool,
    pub(super) fin: bool,
    pub(super) branches: Vec<Branch>,
}

impl Open {
    /// A statement at `place` whose block on its head's line lies `line`
    /// calls past it, with its first branch.
    pub(super) fn new(kind: &'static str, place: i32, line: i32, first: Branch) -> Self {
        Open {
            kind,
            place,
            line: place + line,
            els: false,
            fin: false,
            branches: vec![first],
        }
    }

    /// A `try`'s handlers so far.
    pub(super) fn handlers(&self) -> usize {
        self.branches.len() - 1 - usize::from(self.els) - usize::from(self.fin)
    }
}

/// `ast.parse(source)` of a module of statements in blocks: the tree (its
/// `body` the module's statements), or why it is not read. Type
/// parameters, `match` and `type` statements are declined (`statement`)
/// where every statement before them parses. The 5c1 entry
/// [`super::parse_module`] declines every compound statement.
pub fn parse_module_blocks(text: &[u32]) -> Res<Tree> {
    parse_with(text, |p| p.blocks(), STATEMENT_PASS)
}

impl Parser<'_> {
    /// `statements ENDMARKER`: each statement read into the innermost
    /// block open, which a DEDENT closes. The depth of the deepest
    /// statement.
    fn blocks(&mut self) -> Res<u32> {
        let mut opens: Vec<Open> = Vec::new();
        loop {
            match self.t(0).kind {
                Kind::EndMarker => return Ok(self.deepest()),
                Kind::Dedent if !opens.is_empty() => {
                    self.bump();
                    let open = opens.pop().expect("an open block");
                    self.proceed(open, &mut opens, true)?;
                    continue;
                }
                // `_PyPegen_set_syntax_error`: "unexpected indent".
                Kind::Indent | Kind::Dedent => return Err(self.second_pass_first("indent")),
                _ => {}
            }
            self.place = opens.last().map_or(0, |o| o.line + INDENTED);
            self.statement(&mut opens)?;
        }
    }

    /// One `statement` at the cursor: a compound statement's head, then
    /// its block, or a line of simple statements. A statement whose place
    /// lies within [`STATEMENT_PASS`] of `MAXSTACK` (only an `elif` chain
    /// thousands long reaches it) is declined (`stack bound`): the calls
    /// a statement makes past its place without an operand are not
    /// counted.
    fn statement(&mut self, opens: &mut Vec<Open>) -> Res<()> {
        self.near()?;
        let open = match (self.kw(0), self.kw(1)) {
            (Some("if" | "while" | "for" | "with"), _) | (Some("async"), Some("for" | "with")) => {
                self.head()?
            }
            (Some("def" | "class" | "try"), _) | (Some("async"), Some("def")) => {
                self.definition()?
            }
            _ if self.is(0, "@") => self.definition()?,
            _ if self.soft("match") && self.compound() => self.match_stmt()?,
            _ => {
                let mut out = Vec::new();
                self.line(&mut out)?;
                self.emit(opens, out);
                return Ok(());
            }
        };
        self.proceed(open, opens, false)
    }

    /// The place bound of [`Parser::statement`], and the place recorded
    /// for the second pass.
    fn near(&mut self) -> Res<()> {
        if BASE + self.place + STATEMENT_PASS > MAXSTACK {
            return Err(ExprError::Declined("stack bound"));
        }
        self.placed = self.placed.max(self.place);
        Ok(())
    }

    /// Statements read into the innermost open block, or the module's.
    fn emit(&mut self, opens: &mut [Open], nodes: Vec<u32>) {
        match opens.last_mut() {
            Some(o) => o.branches.last_mut().expect("a branch").body.extend(nodes),
            None => self.tree.body.extend(nodes),
        }
    }

    /// The owner `k` reads a slot `at` calls past the statement's place.
    pub(super) fn slot(&mut self, k: Seq, at: i32) -> Res<u32> {
        self.placed = self.placed.max(self.place + at);
        self.run(k, at)
    }

    /// Where the last token read that is no NEWLINE, INDENT or DEDENT
    /// ends: a compound statement and each of its clauses end there
    /// (`_PyPegen_get_last_nonnwhitespace_token`), a `;` closing its
    /// block's line included.
    fn last_end(&self) -> u32 {
        let toks = &self.toks[..self.pos];
        let t = toks
            .iter()
            .rev()
            .map(|&i| &self.m.tokens[i as usize])
            .find(|t| !matches!(t.kind, Kind::Newline | Kind::Indent | Kind::Dedent));
        t.map_or(0, |t| t.end as u32)
    }

    pub(super) fn colon(&mut self) -> Res<()> {
        if !self.is(0, ":") {
            return syntax();
        }
        self.bump();
        Ok(())
    }

    /// The head of `if`, `while`, `for`, `with`, `async for` or `async
    /// with` up to its `:`.
    fn head(&mut self) -> Res<Open> {
        let start = self.t(0).start as u32;
        let is_async = self.kw(0) == Some("async");
        if is_async {
            self.bump();
        }
        let kw = self.kw(0);
        self.bump();
        let (head, kind) = match (kw, is_async) {
            (Some("if"), _) => (vec![self.slot(Seq::One(NAMED), TEST)?], "If"),
            (Some("while"), _) => (vec![self.slot(Seq::One(NAMED), TEST)?], "While"),
            (Some("for"), a) => (self.for_head()?, if a { "AsyncFor" } else { "For" }),
            (_, a) => (self.with_head()?, if a { "AsyncWith" } else { "With" }),
        };
        self.colon()?;
        Ok(Open::new(kind, self.place, LINE, Branch::new(start, head)))
    }

    /// `block` after a head's `:`, then each `elif`, `except`, `else` and
    /// `finally` that follows it where its statement takes one, until the
    /// statement ends and joins its block's statements. An indented block
    /// puts the statement on `opens`, where its DEDENT resumes it
    /// (`ended`). A definition takes no clause; a `try` takes handlers of
    /// one kind, `else` after one and `finally` last, and is refused where
    /// it ends with neither a handler nor `finally`.
    fn proceed(&mut self, mut open: Open, opens: &mut Vec<Open>, mut ended: bool) -> Res<()> {
        loop {
            if !ended {
                if self.t(0).kind == Kind::Newline {
                    self.bump();
                    // `invalid_if_stmt` and its kin: an IndentationError,
                    // unless the second pass raises at an earlier line.
                    if self.t(0).kind != Kind::Indent {
                        return Err(self.second_pass_first("expected an indented block"));
                    }
                    self.bump();
                    opens.push(open);
                    return Ok(());
                }
                // `simple_stmts`: no `match_stmt` is tried on a head's line.
                self.place = open.line;
                self.near()?;
                let mut out = Vec::new();
                if let Err(e) = self.simple_stmts(&mut out) {
                    return Err(self.first_value(e));
                }
                open.branches.last_mut().expect("a branch").body.extend(out);
            }
            ended = false;
            open.branches.last_mut().expect("a branch").end = self.last_end();
            self.place = open.place;
            // The branches before the next: `if` and its `elif`s.
            let k = open.branches.len() as i32;
            let start = self.t(0).start as u32;
            let tries = open.kind.starts_with("Try");
            let loops = matches!(open.kind, "If" | "While" | "For" | "AsyncFor");
            match self.kw(0) {
                Some("elif") if open.kind == "If" && !open.els => {
                    self.bump();
                    let test = self.slot(Seq::One(NAMED), TEST + k)?;
                    self.colon()?;
                    open.branches.push(Branch::new(start, vec![test]));
                    open.line = open.place + LINE + k;
                }
                Some("else")
                    if !open.els && !open.fin && (loops || (tries && open.handlers() > 0)) =>
                {
                    self.forced_colon()?;
                    open.branches.push(Branch::new(start, Vec::new()));
                    open.els = true;
                    open.line = open.place + if tries { TRY_ELSE } else { ELSE + k - 1 };
                }
                Some("except") if tries && !open.els && !open.fin => self.handler(&mut open)?,
                Some("finally") if tries && !open.fin => {
                    self.forced_colon()?;
                    open.branches.push(Branch::new(start, Vec::new()));
                    (open.fin, open.line) = (true, open.place + TRY_ELSE);
                }
                // `invalid_try_stmt`: "expected 'except' or 'finally' block".
                _ if tries && open.handlers() == 0 && !open.fin => return syntax(),
                _ => {
                    let n = self.finish(open);
                    self.emit(opens, vec![n]);
                    return Ok(());
                }
            }
        }
    }

    /// `'else' &&':'` and `'finally' &&':'`: past its keyword, a missing
    /// `:` is raised by the first pass, and no second pass follows.
    fn forced_colon(&mut self) -> Res<()> {
        self.bump();
        if !self.is(0, ":") {
            self.forced = true;
            return syntax();
        }
        self.bump();
        Ok(())
    }

    /// The statement's node, which ends where its last clause does: an
    /// `elif` is an If alone in its parent's `orelse`, built from the
    /// last; a `try`'s fields are its block, its handlers, `orelse` and
    /// `finalbody`.
    fn finish(&mut self, open: Open) -> u32 {
        let Open {
            kind,
            els,
            fin,
            mut branches,
            ..
        } = open;
        let end = branches.last().expect("a branch").end;
        let mut pop = |on: bool| {
            if on {
                branches.pop().expect("a clause").body
            } else {
                Vec::new()
            }
        };
        let (last, orelse) = (pop(fin), pop(els));
        let n = match kind {
            "Try" | "TryStar" => {
                let handlers: Vec<Branch> = branches.drain(1..).collect();
                let handlers: Vec<u32> = handlers
                    .into_iter()
                    .map(|b| self.part("ExceptHandler", b))
                    .collect();
                let b = branches.pop().expect("a block");
                self.mk(kind, b.start, &[b.body, handlers, orelse, last].concat())
            }
            "FunctionDef" | "AsyncFunctionDef" | "ClassDef" => {
                self.part(kind, branches.pop().expect("a definition"))
            }
            _ => {
                let mut orelse = orelse;
                while let Some(b) = branches.pop() {
                    let n = self.mk(kind, b.start, &[b.head, b.body, orelse].concat());
                    self.tree.nodes[n as usize].end = end;
                    orelse = vec![n];
                }
                orelse[0]
            }
        };
        self.tree.nodes[n as usize].end = end;
        n
    }

    /// The node of one branch with its own fields: its head, its block
    /// and its tail, its identifier and its end.
    fn part(&mut self, kind: &'static str, b: Branch) -> u32 {
        let n = self.mk(kind, b.start, &[b.head, b.body, b.tail].concat());
        let node = &mut self.tree.nodes[n as usize];
        (node.name, node.end) = (b.name, b.end);
        n
    }

    /// `star_targets 'in' ~ star_expressions` after `for`. A target that
    /// is none, or no `in` after it, fails before the cut, and the second
    /// pass's `invalid_for_target` reads `star_expressions` after `for`.
    fn for_head(&mut self) -> Res<Vec<u32>> {
        let after = self.pos;
        match self.for_target() {
            Ok(t) if self.kw(0) == Some("in") => {
                self.bump();
                Ok(vec![t, self.slot(Seq::Top, 0)?])
            }
            Ok(_) | Err(ExprError::Refused("syntax")) => {
                self.pos = after;
                self.frames.clear();
                self.read_on(|p| p.run(Seq::Top, FOR_PASS))?;
                syntax()
            }
            Err(e) => Err(e),
        }
    }

    /// `star_targets`: targets joined by `,` up to `in`, given Store.
    fn for_target(&mut self) -> Res<u32> {
        let mut items = Vec::new();
        let mut comma = false;
        loop {
            let at = if items.is_empty() { 0 } else { LATER_TARGET };
            items.push(self.star_target(at)?);
            if !self.is(0, ",") {
                break;
            }
            self.bump();
            comma = true;
            if self.kw(0) == Some("in") {
                break;
            }
        }
        let n = if comma {
            self.mk_ctx("Tuple", self.start(items[0]), &items)
        } else {
            items[0]
        };
        self.target(n, Ctx::Store)?;
        Ok(n)
    }

    /// `star_target` read as a primary, starred or not, `at` calls past the
    /// statement's place; [`Parser::target`] judges it.
    fn star_target(&mut self, at: i32) -> Res<u32> {
        if !self.is(0, "*") {
            return self.slot(Seq::One(PRIMARY), at);
        }
        let start = self.t(0).start as u32;
        self.bump();
        let v = self.slot(Seq::One(PRIMARY), at + LATER_TARGET)?;
        Ok(self.mk_ctx("Starred", start, &[v]))
    }

    /// The items after `with`: `'(' ','.with_item+ ','? ')'` before `:`,
    /// else `','.with_item+`, which reads a parenthesis as an expression
    /// (`with (a, b) as c:`). A ValueError or a MemoryError in the first
    /// form stands.
    fn with_head(&mut self) -> Res<Vec<u32>> {
        if self.is(0, "(") {
            let (pos, end, grouped) = (self.pos, self.end, self.grouped);
            let (nodes, edges) = (self.tree.nodes.len(), self.tree.edges.len());
            match self.parenthesised_items() {
                Err(ExprError::Refused("syntax")) => {
                    for n in nodes as u32..self.tree.nodes.len() as u32 {
                        self.opened.remove(&n);
                    }
                    (self.pos, self.end, self.grouped) = (pos, end, grouped);
                    self.tree.nodes.truncate(nodes);
                    self.tree.edges.truncate(edges);
                    self.frames.clear();
                }
                read => return read,
            }
        }
        let mut items = vec![self.with_item(0)?];
        while self.is(0, ",") {
            self.bump();
            items.push(self.with_item(LATER_ITEM)?);
        }
        Ok(items)
    }

    fn parenthesised_items(&mut self) -> Res<Vec<u32>> {
        self.bump();
        let mut items = vec![self.with_item(0)?];
        while self.is(0, ",") {
            self.bump();
            if self.is(0, ")") {
                break;
            }
            items.push(self.with_item(LATER_ITEM)?);
        }
        if !self.is(0, ")") || !self.is(1, ":") {
            return syntax();
        }
        self.bump();
        Ok(items)
    }

    /// `with_item`, `at` calls past the statement's place: `expression
    /// 'as' star_target &(',' | ')' | ':')`, else `expression`. A target
    /// that is none or that the lookahead refuses fails the statement,
    /// and the second pass's `invalid_with_item` reads an `expression`
    /// after `as`.
    fn with_item(&mut self, at: i32) -> Res<u32> {
        let start = self.t(0).start as u32;
        let mut kids = vec![self.slot(Seq::One(EXPR), at)?];
        if self.kw(0) == Some("as") {
            self.bump();
            let after = self.pos;
            match self.star_target(at + AS_TARGET) {
                Ok(t)
                    if self.target(t, Ctx::Store).is_ok()
                        && [",", ")", ":"].iter().any(|s| self.is(0, s)) =>
                {
                    kids.push(t)
                }
                Ok(_) | Err(ExprError::Refused("syntax")) => {
                    self.pos = after;
                    self.frames.clear();
                    self.read_on(|p| p.run(Seq::One(EXPR), at + AS_TARGET))?;
                    return syntax();
                }
                Err(e) => return Err(e),
            }
        }
        Ok(self.mk("withitem", start, &kids))
    }
}
