//! The simple statements of CPython 3.13.13's grammar (`simple_stmts`)
//! over [`super`]'s expression parser, as `ast.parse(source)` in
//! `claims.check_claim` (`claims.py` 672) reads a module of them: each
//! logical line is statements joined by `;` (one may close it), each an
//! assignment, an expression, or a keyword statement. A target is read as
//! an expression and then given Store or Del context where the grammar
//! takes it as one. Where it is none the first pass fails there, and what
//! the second pass's `invalid_assignment` and `invalid_del_stmt` read
//! past it is read for a ValueError only. A line opening a compound
//! statement is declined (`statement`).

use super::*;

// Where each statement's expressions lie past [`BASE`], read off the
// interpreter by bisection on a `-` chain (`pysyntax_stmt.jsonl` pins each
// boundary pair).
/// A statement after `;` (`x; ` + `-` * 5967 + `x` parses, one more
/// overflows), on top of its own place.
const LATER: i32 = 2;
/// An assignment's value (and each later target) and an augmented
/// assignment's value.
const VALUE: i32 = 2;
/// An annotation.
const ANNOTATION: i32 = -1;
/// The value after an annotation.
const ANNOTATED: i32 = 3;
const RETURN: i32 = 1;
/// `raise`'s exception and `assert`'s test; the cause and the message lie
/// one call further.
const RAISE: i32 = -1;
/// The `yield` statement's slot: `yield`'s value lies one call past a
/// slot, `yield from`'s one call before it.
const YIELD: i32 = 1;
/// `del`'s first target, and its later ones two calls further. A target
/// that is none (`del -x`) the first pass does not read, and the second
/// pass's `invalid_del_stmt` reads at this depth.
const DEL: i32 = 6;
/// `match_stmt`'s subject (`subject_expr`) past `match`: its first
/// element (`match ` + `-` * 5968 + `x` parses, one more overflows), its
/// second, the first of `star_named_expressions`, and each later one, in
/// that gather's loop.
const SUBJECT: [i32; 3] = [1, 3, 4];
/// A starred element of the subject (`'*' bitwise_or`) against a named
/// one: the six calls from `named_expression` to `bitwise_or` fewer.
const SUBJECT_STAR: i32 = -6;
/// The most a statement's second pass may go past what
/// [`Parser::reach`] counts for an expression statement: `del`'s later
/// targets after `;` (`DEL` + 2 + [`LATER`]).
const STATEMENT_PASS: i32 = DEL + 2 + LATER;

const AUGMENTED: [(&str, &str); 13] = [
    ("+=", "Add"),
    ("-=", "Sub"),
    ("*=", "Mult"),
    ("/=", "Div"),
    ("//=", "FloorDiv"),
    ("%=", "Mod"),
    ("@=", "MatMult"),
    ("&=", "BitAnd"),
    ("|=", "BitOr"),
    ("^=", "BitXor"),
    (">>=", "RShift"),
    ("<<=", "LShift"),
    ("**=", "Pow"),
];
/// The keywords that open a compound statement (`compound_stmt`).
const COMPOUND: [&str; 8] = ["if", "while", "for", "with", "try", "def", "class", "async"];

/// `ast.parse(source)` of a module of simple statements: the tree (its
/// `body` the statements), or why it is not read. A line opening a
/// compound statement, a `match` statement or a `type` statement is
/// declined (`statement`) where every line before it parses.
pub fn parse_module(text: &[u32]) -> Res<Tree> {
    parse_with(text, |p| p.statements(), STATEMENT_PASS)
}

impl Parser<'_> {
    /// Every logical line as `simple_stmts` up to ENDMARKER: the depth of
    /// the deepest statement.
    fn statements(&mut self) -> Res<u32> {
        let mut deepest = 0;
        loop {
            match self.t(0).kind {
                Kind::EndMarker => return Ok(deepest),
                Kind::Indent | Kind::Dedent => return Err(ExprError::Refused("indent")),
                _ => {}
            }
            if self.compound() {
                return Err(ExprError::Declined("statement"));
            }
            let (value, reach) = self.subject()?;
            match self.simple_stmts(&mut deepest) {
                Ok(()) => {}
                // The second pass reads the subject before the line.
                Err(ExprError::Refused("syntax")) if value.is_some() => {
                    return Err(self.second_value(value == Some(true)))
                }
                Err(e) => return Err(self.after_subject(e, reach)),
            }
        }
    }

    /// One logical line's statements and its NEWLINE, each statement's
    /// depth raising `deepest`.
    fn simple_stmts(&mut self, deepest: &mut u32) -> Res<()> {
        let mut at = 0;
        loop {
            let s = self.simple(at)?;
            *deepest = (*deepest).max(self.tree.nodes[s as usize].height);
            self.tree.body.push(s);
            at = LATER;
            if self.is(0, ";") {
                self.bump();
                if self.t(0).kind != Kind::Newline {
                    continue;
                }
            }
            if self.t(0).kind != Kind::Newline {
                return syntax();
            }
            self.bump();
            return Ok(());
        }
    }

    /// Whether the soft keyword `s` (its raw text) is at the cursor.
    pub(super) fn soft(&self, s: &str) -> bool {
        self.t(0).kind == Kind::Lex(Tok::Name) && same(self.text(0), s)
    }

    /// Whether the line at the cursor is a compound statement's head: a
    /// compound keyword or `@` first, or `match` first and `:` last.
    fn compound(&self) -> bool {
        if self.is(0, "@") || self.kw(0).is_some_and(|k| COMPOUND.contains(&k)) {
            return true;
        }
        if self.soft("match") {
            let mut k = 1;
            while !matches!(self.t(k).kind, Kind::Newline | Kind::EndMarker) {
                k += 1;
            }
            return self.is(k - 1, ":");
        }
        false
    }

    /// `match_stmt`, which the first pass tries before `simple_stmts` on a
    /// line opening with `match`, as far as its subject (`subject_expr`):
    /// a constant's identifier there is a ValueError and an overflow a
    /// MemoryError, and anything else leaves the line to the simple
    /// statements, read from `match` again. The second pass refuses a
    /// subject the line ends after (`invalid_match_stmt`), one whose
    /// parenthesis right after `match` it refuses
    /// ([`Parser::group_refused`]), and one whose last element, unless a
    /// walrus no parenthesis holds, `invalid_named_expression` refuses
    /// before `=` or `:=`; where that rule reads a constant's identifier
    /// past them, the answer is `Some`, whether a legacy `print` or `exec`
    /// lies before it. With it, the token the subject's reading stopped
    /// at.
    pub(super) fn match_subject(&mut self) -> Res<(Option<bool>, usize)> {
        let (pos, end, grouped) = (self.pos, self.end, self.grouped);
        let (nodes, edges, frames) = (
            self.tree.nodes.len(),
            self.tree.edges.len(),
            self.frames.len(),
        );
        self.bump();
        // `star_named_expression ',' star_named_expressions?`, else
        // `named_expression`: a starred first element needs its comma, and
        // past a comma the subject ends before an element that is none.
        // `refused`: the second pass refuses the subject; `last`: its last
        // element, a named one.
        let (mut items, mut refused, mut last) = (0, false, None);
        loop {
            let at = SUBJECT[items.min(2)];
            let starred = self.is(0, "*");
            let item = if starred {
                self.bump();
                self.run(Seq::One(BITOR), at + SUBJECT_STAR)
            } else {
                self.run(Seq::One(NAMED), at)
            };
            match item {
                Ok(_) if self.is(0, ",") => {
                    self.bump();
                    refused = self.t(0).kind == Kind::Newline;
                    items += 1;
                    continue;
                }
                Ok(e) if items > 0 || !starred => {
                    refused = self.t(0).kind == Kind::Newline;
                    // A walrus no parenthesis holds is `assignment_expression`'s
                    // node: `named_expression` takes it before its error rule
                    // reads what follows (`match a := 1 := x` reads no `x`).
                    let bare = self.kind(e) == "NamedExpr" && !self.parenthesised(e);
                    last = (!starred && !bare).then_some(e);
                }
                Err(e @ (ExprError::Refused("value" | "stack") | ExprError::Declined(_))) => {
                    return Err(e)
                }
                _ => {}
            }
            break;
        }
        let reach = self.pos;
        // `invalid_named_expression` over the last element: where `:=`
        // follows it, it reads an `expression` after it and raises; where
        // `=` follows it (`match(x).y = 1`), as past a statement's first
        // target, it raises once a `bitwise_or` that no `=` or `:=`
        // follows is read after the `=`. A constant's identifier there is
        // a ValueError. That reading is the second pass's: its depth
        // decides nothing here.
        let mut value = None;
        let walrus = self.is(0, ":=");
        if last.is_some_and(|e| walrus || (self.is(0, "=") && self.named_reads_on(e))) {
            let unsure = self.unsure;
            self.bump();
            let level = if walrus { EXPR } else { BITOR };
            match self.run(Seq::One(level), SUBJECT[items.min(2)] + VALUE) {
                Ok(_) => refused |= walrus || (!self.is(0, "=") && !self.is(0, ":=")),
                Err(ExprError::Refused("value")) => value = Some(self.legacy_before()),
                Err(_) => {}
            }
            self.unsure = unsure;
        }
        // A parenthesis right after `match` is the subject's first atom,
        // which the second pass reads with its error rules.
        self.pos = pos + 1;
        if self.is(0, "(") {
            let unsure = self.unsure;
            refused |= self.group_refused(frames);
            self.unsure = unsure;
        }
        self.match_head |= refused;
        for n in nodes as u32..self.tree.nodes.len() as u32 {
            self.opened.remove(&n);
        }
        (self.pos, self.end, self.grouped) = (pos, end, grouped);
        self.tree.nodes.truncate(nodes);
        self.tree.edges.truncate(edges);
        self.frames.truncate(frames);
        Ok((value, reach))
    }

    /// Whether the second pass refuses the parenthesis at the cursor,
    /// which opens a `match` line's subject and which the first pass
    /// reads as a call's arguments (`match(*a)`, `match(a=1)`): `tuple`
    /// reads its elements, each a `star_named_expression`, until one is
    /// none, and `invalid_named_expression` refuses an element
    /// `NAME '=' bitwise_or` that no `=` or `:=` follows; where `tuple`
    /// fails, `invalid_group` refuses a lone starred or double-starred
    /// element (`'(' starred_expression ')'`, `'(' '**' expression ')'`).
    /// Each reading starts from the frames below the subject's (`frames`);
    /// its depth decides nothing here.
    fn group_refused(&mut self, frames: usize) -> bool {
        self.bump();
        let mut first = true;
        loop {
            self.frames.truncate(frames);
            if self.name(0) && self.is(1, "=") {
                self.bump();
                self.bump();
                return self.run(Seq::One(BITOR), SUBJECT[0]).is_ok()
                    && !self.is(0, "=")
                    && !self.is(0, ":=");
            }
            let star = self.is(0, "*");
            if first && (star || self.is(0, "**")) {
                let at = self.pos;
                self.bump();
                if self.run(Seq::One(EXPR), SUBJECT[0]).is_ok() && self.is(0, ")") {
                    return true;
                }
                if !star {
                    return false;
                }
                self.pos = at;
                self.frames.truncate(frames);
            }
            let item = if star {
                self.bump();
                self.run(Seq::One(BITOR), SUBJECT[0] + SUBJECT_STAR)
            } else {
                self.run(Seq::One(NAMED), SUBJECT[0])
            };
            if item.is_err() || !self.is(0, ",") {
                return false;
            }
            self.bump();
            first = false;
        }
    }

    /// What the second pass reads past a target the first pass refuses:
    /// a constant's identifier there is a ValueError, and anything else
    /// leaves the refusal standing. The second pass reads the module from
    /// its start, so a SyntaxError it raises before the constant comes
    /// first: at a line before whose `match_stmt` subject it refuses
    /// (`match(x)` with no `:`), or at a legacy `print` or `exec` before
    /// the constant (`invalid_legacy_expression`, where an `expression`
    /// opens with it), which the port declines (`second pass`).
    fn read_on(&mut self, read: impl FnOnce(&mut Self) -> Res<u32>) -> Res<()> {
        match read(self) {
            Err(ExprError::Refused("value")) => {
                let legacy = self.legacy_before();
                Err(self.second_value(legacy))
            }
            Err(e @ ExprError::Declined(_)) => Err(e),
            _ => Ok(()),
        }
    }

    /// A refusal `e` of the first pass's reading, or, for a ValueError at
    /// a constant's identifier only the second pass reads
    /// ([`Parser::second_argument`]), its answer by
    /// [`Parser::second_value`]: a SyntaxError the second pass raises
    /// before it comes first.
    pub(super) fn first_value(&mut self, e: ExprError) -> ExprError {
        if e == ExprError::Refused("value") && self.second_argument() {
            let legacy = self.legacy_before();
            self.second_value(legacy)
        } else {
            e
        }
    }

    /// Whether the constant's identifier the reading stopped at lies in a
    /// call's argument the first pass does not read: a positional or
    /// starred one after a keyword or `**` (`f(a=1, x + Ｎｏｎｅ)`), which
    /// the first pass tries as `NAME '='` only (reading the argument's
    /// first token) and, after keywords alone, as `starred_expression`
    /// (reading a starred argument whole). The port reads it before it
    /// refuses the call; the second pass's `invalid_kwarg` reads it.
    fn second_argument(&self) -> bool {
        let prev = &self.m.tokens[self.toks[self.pos.saturating_sub(1)] as usize];
        let first =
            prev.kind == Kind::Lex(Tok::Op) && same(&self.m.source[prev.start..prev.end], ",");
        self.frames.iter().enumerate().any(|(i, f)| match f.fr {
            Fr::Call {
                kw: None,
                dstar: false,
                seen,
                ..
            } if seen != 0 => match self.frames.get(i + 1) {
                None => !first,
                Some(f) => {
                    seen & 2 != 0
                        || !matches!(
                            f.fr,
                            Fr::Op {
                                kind: "Starred",
                                ..
                            }
                        )
                }
            },
            _ => false,
        })
    }

    /// The answer to a constant's identifier only the second pass reads,
    /// with a legacy `print` or `exec` before it or not (`legacy`).
    pub(super) fn second_value(&mut self, legacy: bool) -> ExprError {
        if self.match_head {
            ExprError::Refused("syntax")
        } else if legacy {
            ExprError::Declined("second pass")
        } else {
            self.second = true;
            ExprError::Refused("value")
        }
    }

    /// Whether a NAME whose identifier is `print` or `exec`, and a token
    /// that may open `star_expressions` after it, lie before the cursor.
    fn legacy_before(&self) -> bool {
        (0..self.pos).any(|i| {
            let t = &self.m.tokens[self.toks[i] as usize];
            t.kind == Kind::Lex(Tok::Name)
                && ["print", "exec"].iter().any(|w| same(&self.m.name(t), w))
                && self.opens_operand(&self.m.tokens[self.toks[i + 1] as usize])
        })
    }

    /// Whether the statement ends at the cursor.
    fn ends(&self) -> bool {
        self.is(0, ";") || self.t(0).kind == Kind::Newline
    }

    /// A node with no children and an identifier, in no context.
    fn ident_node(&mut self, kind: &'static str, start: u32, id: Option<Box<[u32]>>) -> u32 {
        let n = self.mk(kind, start, &[]);
        self.tree.nodes[n as usize].name = id;
        n
    }

    /// One `simple_stmt`, `at` calls past the line's place.
    fn simple(&mut self, at: i32) -> Res<u32> {
        // `type`, a name and `=` or `[`: a type alias, unit 5c2's.
        if self.soft("type") && self.name(1) && (self.is(2, "=") || self.is(2, "[")) {
            return Err(ExprError::Declined("statement"));
        }
        let start = self.t(0).start as u32;
        let kw = self.kw(0);
        let leaf = match kw {
            Some("pass") => Some("Pass"),
            Some("break") => Some("Break"),
            Some("continue") => Some("Continue"),
            _ => None,
        };
        if let Some(kind) = leaf {
            self.bump();
            return Ok(self.mk(kind, start, &[]));
        }
        match kw {
            Some("return") => {
                self.bump();
                let mut kids = Vec::new();
                if !self.ends() {
                    kids.push(self.run(Seq::Top, at + RETURN)?);
                }
                Ok(self.mk("Return", start, &kids))
            }
            Some("raise") => {
                self.bump();
                let mut kids = Vec::new();
                if !self.ends() {
                    kids.push(self.run(Seq::One(EXPR), at + RAISE)?);
                    if self.kw(0) == Some("from") {
                        self.bump();
                        kids.push(self.run(Seq::One(EXPR), at + RAISE + 1)?);
                    }
                }
                Ok(self.mk("Raise", start, &kids))
            }
            Some("assert") => {
                self.bump();
                let mut kids = vec![self.run(Seq::One(EXPR), at + RAISE)?];
                if self.is(0, ",") {
                    self.bump();
                    kids.push(self.run(Seq::One(EXPR), at + RAISE + 1)?);
                }
                Ok(self.mk("Assert", start, &kids))
            }
            Some("yield") => {
                let v = self.yield_expr(at + YIELD)?;
                Ok(self.mk("Expr", start, &[v]))
            }
            Some("del") => {
                self.bump();
                let mut kids = Vec::new();
                loop {
                    let by = if kids.is_empty() { 0 } else { 2 };
                    if self.is(0, "*") {
                        // No target is starred: `invalid_del_stmt` reads
                        // on from here.
                        self.read_on(|p| p.run(Seq::Top, at + DEL + by))?;
                        return syntax();
                    }
                    let n = self.run(Seq::One(EXPR), at + DEL + by)?;
                    if let Err(e) = self.target(n, Ctx::Del) {
                        // `invalid_del_stmt` reads the whole list as
                        // `star_expressions`.
                        if self.is(0, ",") {
                            self.bump();
                            if !self.ends() {
                                self.read_on(|p| p.run(Seq::Top, at + DEL + 2))?;
                            }
                        }
                        return Err(e);
                    }
                    kids.push(n);
                    if !self.is(0, ",") {
                        break;
                    }
                    self.bump();
                    if self.ends() {
                        break;
                    }
                }
                Ok(self.mk("Delete", start, &kids))
            }
            Some(k @ ("global" | "nonlocal")) => {
                self.bump();
                // The names joined by `,`, which no identifier holds.
                let mut names: Vec<u32> = Vec::new();
                loop {
                    if !self.name(0) {
                        return syntax();
                    }
                    if !names.is_empty() {
                        names.push(u32::from(b','));
                    }
                    names.extend_from_slice(&self.ident()?);
                    if !self.is(0, ",") {
                        break;
                    }
                    self.bump();
                }
                let kind = if k == "global" { "Global" } else { "Nonlocal" };
                Ok(self.ident_node(kind, start, Some(names.into())))
            }
            Some("import") => {
                self.bump();
                let mut kids = vec![self.alias(true)?];
                while self.is(0, ",") {
                    self.bump();
                    kids.push(self.alias(true)?);
                }
                Ok(self.mk("Import", start, &kids))
            }
            Some("from") => self.import_from(start),
            Some(k) if STATEMENT.contains(&k) => syntax(),
            _ => self.assignment(start, at),
        }
    }

    /// `assignment`, else an expression statement: the first part is read
    /// as `star_expressions`, then a `:`, an augmented operator or `=`
    /// after it makes it a target, refused where it is none once what the
    /// second pass reads past it is read.
    fn assignment(&mut self, start: u32, at: i32) -> Res<u32> {
        let first = self.run(Seq::Top, at)?;
        if self.is(0, ":") {
            let simple = match self.annotated(first, start) {
                Ok(simple) => simple,
                Err(e) => {
                    // `invalid_assignment` reads the annotation as
                    // `expression` after any target but a starred one.
                    if self.kind(first) != "Starred" {
                        self.bump();
                        self.read_on(|p| p.run(Seq::One(EXPR), at + ANNOTATION))?;
                    }
                    return Err(e);
                }
            };
            self.bump();
            let mut kids = vec![first, self.run(Seq::One(EXPR), at + ANNOTATION)?];
            if self.is(0, "=") {
                self.bump();
                kids.push(self.rhs(at + ANNOTATED)?);
            }
            let n = self.mk("AnnAssign", start, &kids);
            self.tree.nodes[n as usize].num = u32::from(simple);
            return Ok(n);
        }
        if let Some(&(_, op)) = AUGMENTED.iter().find(|(s, _)| self.is(0, s)) {
            if let Err(e) = self.single(first) {
                // `invalid_assignment`: `star_expressions augassign
                // annotated_rhs`; a parenthesised starred target is
                // refused before (`invalid_group`).
                if !(self.kind(first) == "Starred" && self.parenthesised(first)) {
                    self.bump();
                    self.read_on(|p| p.rhs(at + VALUE))?;
                }
                return Err(e);
            }
            self.bump();
            let o = self.op_node(op);
            let v = self.rhs(at + VALUE)?;
            return Ok(self.mk("AugAssign", start, &[first, o, v]));
        }
        let mut kids = vec![first];
        while self.is(0, "=") {
            let last = kids[kids.len() - 1];
            if let Err(e) = self.target(last, Ctx::Store) {
                if kids.len() == 1 && self.named_reads_on(first) {
                    self.bump();
                    self.read_on(|p| p.run(Seq::One(BITOR), at + VALUE))?;
                }
                return Err(e);
            }
            self.bump();
            kids.push(self.rhs(at + VALUE)?);
        }
        let kind = if kids.len() == 1 { "Expr" } else { "Assign" };
        Ok(self.mk(kind, start, &kids))
    }

    /// The target of an annotation given Store context: whether it is a
    /// bare Name (`AnnAssign.simple`). Past `NAME ':'`, `assignment` reads
    /// it as the group `'(' single_target ')' |
    /// single_subscript_attribute_target`, whose first branch, once it
    /// matches, the `:` must follow: a target opening with a parenthesised
    /// single target that goes on past it (`(a).b`, `(a.b)[0]`) is
    /// refused, where `((a).b)` and `(f()).b` are not.
    fn annotated(&mut self, first: u32, start: u32) -> Res<bool> {
        // The node the parenthesis at the target's first token encloses,
        // down the chain of primaries the target is built on.
        let mut n = first;
        loop {
            if self.parenthesised(n) && self.start(n) == start {
                if n != first && matches!(self.kind(n), "Name" | "Attribute" | "Subscript") {
                    return syntax();
                }
                break;
            }
            match self.kind(n) {
                "Attribute" | "Subscript" | "Call" => n = self.tree.kids(n)[0],
                _ => break,
            }
        }
        self.single(first)
    }

    /// Whether `invalid_named_expression` reads a `bitwise_or` past the
    /// `=` after `first`, the first target, which is none: in
    /// `invalid_assignment` each element of the target list is a
    /// `named_expression`, and the one the `=` follows (the list's last,
    /// or the target) is read on as `NAME '=' bitwise_or` or, where no
    /// list, tuple, generator or `True`, `None` or `False` opens it, as
    /// `bitwise_or '=' bitwise_or`.
    fn named_reads_on(&self, first: u32) -> bool {
        let mut e = first;
        let items = self.tree.kids(first);
        if self.kind(first) == "Tuple"
            && items
                .first()
                .is_some_and(|&k| self.start(k) == self.start(first))
        {
            // A list without parentheses: its last element, unless a
            // comma closes it.
            let prev = &self.m.tokens[self.toks[self.pos - 1] as usize];
            if prev.kind == Kind::Lex(Tok::Op) && same(&self.m.source[prev.start..prev.end], ",") {
                return false;
            }
            e = items[items.len() - 1];
        }
        match self.kind(e) {
            "Starred" => return false,
            "Name" if !self.parenthesised(e) => return true,
            _ => {}
        }
        let at = self.start(e);
        let mut n = e;
        loop {
            let node = &self.tree.nodes[n as usize];
            if node.start == at {
                match node.kind {
                    "List" | "Tuple" | "GeneratorExp" => return false,
                    // `True`, `None` or `False`: no other constant opens
                    // with these letters.
                    "Constant"
                        if matches!(
                            char::from_u32(self.m.source[at as usize]),
                            Some('T' | 'N' | 'F')
                        ) =>
                    {
                        return false
                    }
                    _ => {}
                }
            }
            match node.kind {
                "BinOp" | "Call" | "Attribute" | "Subscript" => n = self.tree.kids(n)[0],
                _ => break,
            }
        }
        if self.parenthesised(e) {
            return true;
        }
        match self.kind(e) {
            "BoolOp" | "Compare" | "IfExp" | "Lambda" | "Tuple" | "NamedExpr" | "Yield"
            | "YieldFrom" => false,
            "UnaryOp" => self.kind(self.tree.kids(e)[0]) != "Not",
            _ => true,
        }
    }

    /// Whether a parenthesis encloses `n`.
    fn parenthesised(&self, n: u32) -> bool {
        self.start(n) != self.tree.nodes[n as usize].start
    }

    /// `single_target` (a Name, Attribute or Subscript, bare or in
    /// parentheses) given Store context: whether it is a bare Name
    /// (`AnnAssign.simple`).
    fn single(&mut self, n: u32) -> Res<bool> {
        if !matches!(self.kind(n), "Name" | "Attribute" | "Subscript") {
            return syntax();
        }
        self.tree.nodes[n as usize].ctx = Ctx::Store;
        Ok(self.kind(n) == "Name" && !self.parenthesised(n))
    }

    /// `star_targets` (Store) or `del_targets` (Del, which takes no
    /// Starred) given `ctx` through tuples, lists and starred.
    fn target(&mut self, target: u32, ctx: Ctx) -> Res<()> {
        let mut stack = vec![target];
        while let Some(n) = stack.pop() {
            match self.kind(n) {
                "Name" | "Attribute" | "Subscript" => {}
                "Starred" if ctx == Ctx::Store => stack.extend_from_slice(self.tree.kids(n)),
                "Tuple" | "List" => stack.extend_from_slice(self.tree.kids(n)),
                _ => return syntax(),
            }
            self.tree.nodes[n as usize].ctx = ctx;
        }
        Ok(())
    }

    /// `yield_expr | star_expressions` at a slot `at` calls on.
    fn rhs(&mut self, at: i32) -> Res<u32> {
        if self.kw(0) == Some("yield") {
            self.yield_expr(at)
        } else {
            self.run(Seq::Top, at)
        }
    }

    /// `yield_expr` at a slot `at` calls on: `yield` reads
    /// `star_expressions` one call past it, `yield from` an expression one
    /// call before it.
    fn yield_expr(&mut self, at: i32) -> Res<u32> {
        let start = self.t(0).start as u32;
        self.bump();
        if self.kw(0) == Some("from") {
            self.bump();
            let v = self.run(Seq::One(EXPR), at - 1)?;
            return Ok(self.mk("YieldFrom", start, &[v]));
        }
        let mut kids = Vec::new();
        if !self.ends() && !self.is(0, "=") {
            kids.push(self.run(Seq::Top, at + 1)?);
        }
        Ok(self.mk("Yield", start, &kids))
    }

    /// `dotted_name`: its NAMEs' identifiers joined by `.`.
    fn dotted(&mut self) -> Res<Box<[u32]>> {
        if !self.name(0) {
            return syntax();
        }
        let mut id = self.ident()?.into_vec();
        while self.is(0, ".") && self.name(1) {
            self.bump();
            id.push(u32::from(b'.'));
            id.extend_from_slice(&self.ident()?);
        }
        Ok(id.into())
    }

    /// `dotted_as_name` (`dotted`) or `import_from_as_name`.
    fn alias(&mut self, dotted: bool) -> Res<u32> {
        let start = self.t(0).start as u32;
        let id = if dotted {
            self.dotted()?
        } else if self.name(0) {
            self.ident()?
        } else {
            return syntax();
        };
        let mut asname = None;
        if self.kw(0) == Some("as") {
            self.bump();
            if !self.name(0) {
                return syntax();
            }
            asname = Some(self.ident()?);
        }
        let n = self.ident_node("alias", start, Some(id));
        self.tree.nodes[n as usize].asname = asname;
        Ok(n)
    }

    /// `import_from`: its dots (`...` is three), its module, then `*`, or
    /// names with a trailing comma inside parentheses only.
    fn import_from(&mut self, start: u32) -> Res<u32> {
        self.bump();
        let mut level = 0;
        loop {
            level += if self.is(0, ".") {
                1
            } else if self.is(0, "...") {
                3
            } else {
                break;
            };
            self.bump();
        }
        let module = if level == 0 || self.name(0) {
            Some(self.dotted()?)
        } else {
            None
        };
        if self.kw(0) != Some("import") {
            return syntax();
        }
        self.bump();
        let mut kids = Vec::new();
        if self.is(0, "*") {
            let at = self.t(0).start as u32;
            self.bump();
            kids.push(self.ident_node("alias", at, Some([u32::from(b'*')].into())));
        } else {
            let paren = self.is(0, "(");
            if paren {
                self.bump();
            }
            loop {
                kids.push(self.alias(false)?);
                if !self.is(0, ",") {
                    break;
                }
                self.bump();
                if paren && self.is(0, ")") {
                    break;
                }
            }
            if paren {
                if !self.is(0, ")") {
                    return syntax();
                }
                self.bump();
            }
        }
        // `_PyPegen_checked_future_import`.
        let future = level == 0 && module.as_deref().is_some_and(|m| same(m, "__future__"));
        let barry = |p: &Self, &k: &u32| {
            let name = p.tree.nodes[k as usize].name.as_deref();
            name.is_some_and(|n| same(n, "barry_as_FLUFL"))
        };
        let flufl = future && kids.iter().any(|k| barry(self, k));
        self.barry |= flufl;
        let n = self.mk("ImportFrom", start, &kids);
        let node = &mut self.tree.nodes[n as usize];
        (node.name, node.num) = (module, level);
        Ok(n)
    }
}
