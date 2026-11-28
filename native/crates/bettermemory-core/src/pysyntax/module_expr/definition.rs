//! The heads of definitions, decorators and `try` of CPython 3.13.13's
//! grammar (`function_def`, `class_def`, `decorators`, `try_stmt` and its
//! handlers), as `ast.parse(source)` in `claims.check_claim` (`claims.py`
//! 672) and `symbols._all_names` (`symbols.py` 480-507) read them:
//! [`super::compound`] dispatches each head here and reads its block and
//! clauses. Type parameters after a definition's name are declined
//! (`statement`, unit claims-5c2d).

use super::compound::{Branch, Open};
use super::*;

// Where each slot lies past its statement's place, read off the
// interpreter by bisection on a `-` chain (`pysyntax_definition.jsonl`
// pins the first refused source of each).
/// A decorator (`'@' named_expression NEWLINE`), each alike: `@` + `-` *
/// 5967 + `x` then `def f(): pass` parses, one more overflows.
const DECORATOR: i32 = 2;
/// A positional parameter's default and annotation (`default`,
/// `annotation`), first read inside `slash_with_default` or
/// `slash_no_default`; after `/` the rule `parameters` reads them itself,
/// one call nearer. A keyword-only parameter's lie as a positional's
/// before `/`.
const DEFAULT: i32 = 5;
const ANNOTATION: i32 = 6;
const AFTER_SLASH: i32 = 1;
/// `*args`'s annotation, and the operand of a starred one (`'*'
/// bitwise_or`, `param_star_annotation`).
const VARARG_ANNOTATION: i32 = 5;
const STARRED_ANNOTATION: i32 = 1;
/// The return annotation (`'->' expression`).
const RETURNS: i32 = 0;
/// A class's arguments, read by the call's machinery: (most, least) for
/// the first; the call's shift puts each later one at (8, 5), where the
/// interpreter's lie from 5 to 8 by the alternative of `args` and
/// `kwargs` that reads them, so a source within that band of the stack is
/// declined (`stack bound`).
const ARGUMENTS: (i32, i32) = (6, 2);
/// A block on its head's line: a definition's, `try`'s, a handler's.
const DEF_LINE: i32 = 4;
const TRY_LINE: i32 = 3;
const EXCEPT_LINE: i32 = 5;
/// A handler's type (`'except' expression`, `'except' '*' expression`).
const EXCEPT_TYPE: i32 = 0;

impl Parser<'_> {
    /// The head of the `def`, `async def`, `class`, `try` or decorated
    /// statement at the cursor up to its `:`, whose block
    /// [`super::compound`] reads. A definition starts at `def`, `async` or
    /// `class`, its decorators before it.
    pub(super) fn definition(&mut self) -> Res<Open> {
        let mut tail = Vec::new();
        while self.is(0, "@") {
            self.bump();
            tail.push(self.slot(Seq::One(NAMED), DECORATOR)?);
            if self.t(0).kind != Kind::Newline {
                return syntax();
            }
            self.bump();
            // The furthest token read is an INDENT or a DEDENT:
            // `_PyPegen_set_syntax_error`'s IndentationError.
            if matches!(self.t(0).kind, Kind::Indent | Kind::Dedent) {
                return Err(self.second_pass_first("indent"));
            }
        }
        let start = self.t(0).start as u32;
        let kind = match (self.kw(0), self.kw(1)) {
            (Some("def"), _) => "FunctionDef",
            (Some("async"), Some("def")) => {
                self.bump();
                "AsyncFunctionDef"
            }
            (Some("class"), _) => "ClassDef",
            (Some("try"), _) if tail.is_empty() => {
                self.bump();
                // `'try' &&':'`: no second pass follows.
                if !self.is(0, ":") {
                    self.forced = true;
                    return syntax();
                }
                self.bump();
                return Ok(Open::new(
                    "Try",
                    self.place,
                    TRY_LINE,
                    Branch::new(start, Vec::new()),
                ));
            }
            _ => return syntax(),
        };
        self.bump();
        if !self.name(0) {
            return syntax();
        }
        let name = Some(self.ident()?);
        if self.is(0, "[") {
            return Err(ExprError::Declined("statement"));
        }
        let head = if kind == "ClassDef" {
            if self.is(0, "(") {
                self.class_arguments()?
            } else {
                Vec::new()
            }
        } else {
            if !self.is(0, "(") {
                return syntax();
            }
            self.bump();
            let args = self.parameters()?;
            if self.is(0, "->") {
                self.bump();
                tail.push(self.slot(Seq::One(EXPR), RETURNS)?);
            }
            vec![args]
        };
        self.colon()?;
        let mut first = Branch::new(start, head);
        (first.tail, first.name) = (tail, name);
        Ok(Open::new(kind, self.place, DEF_LINE, first))
    }

    /// `params` after a definition's `(`, up to and with its `)`: the
    /// `arguments` node, its lists in `ast`'s field order. A parameter
    /// without a default after one with one, a second `*` or `/`, `/`
    /// first or after `*`, a bare `*` with no keyword-only parameter, a
    /// default on `*args` or `**kwargs` and anything after `**kwargs` are
    /// refused.
    fn parameters(&mut self) -> Res<u32> {
        let start = self.t(0).start as u32;
        let mut lists: [Vec<u32>; 7] = Default::default();
        let (mut slash, mut star, mut bare, mut kwarg, mut defaulted) =
            (false, false, false, false, false);
        let (mut positional, mut kwonly) = (0, 0);
        while !self.is(0, ")") {
            if kwarg {
                // `invalid_kwds` reads a parameter after `**kwargs,`: a
                // constant's identifier there is the second pass's.
                if self.name(0) {
                    self.read_on(|p| p.ident().map(|_| 0))?;
                }
                return syntax();
            }
            if self.is(0, "/") {
                if slash || star || positional == 0 {
                    return syntax();
                }
                lists[POSONLY] = std::mem::take(&mut lists[ARGS]);
                slash = true;
                self.bump();
            } else {
                let list = if self.is(0, "**") {
                    if bare && kwonly == 0 {
                        return syntax();
                    }
                    self.bump();
                    kwarg = true;
                    KWARG
                } else if self.is(0, "*") {
                    if star {
                        return syntax();
                    }
                    self.bump();
                    (star, bare) = (true, self.is(0, ","));
                    VARARG
                } else if star {
                    KWONLY
                } else {
                    ARGS
                };
                if !(list == VARARG && bare) {
                    let nearer = i32::from(slash && list == ARGS) * AFTER_SLASH;
                    let default = self.parameter(list, nearer, &mut lists)?;
                    match list {
                        ARGS if default => (positional, defaulted) = (positional + 1, true),
                        ARGS if defaulted => return syntax(),
                        ARGS => positional += 1,
                        KWONLY => kwonly += 1,
                        _ => {}
                    }
                }
            }
            if self.is(0, ",") {
                self.bump();
            } else if !self.is(0, ")") {
                return syntax();
            }
        }
        if bare && kwonly == 0 {
            return syntax();
        }
        self.bump();
        Ok(self.mk("arguments", start, &lists.concat()))
    }

    /// `param` with its annotation and default into `lists[list]`, its
    /// slots `nearer` calls nearer than before `/`: whether it has a
    /// default.
    fn parameter(&mut self, list: usize, nearer: i32, lists: &mut [Vec<u32>; 7]) -> Res<bool> {
        if !self.name(0) {
            return syntax();
        }
        let start = self.t(0).start as u32;
        let id = self.ident()?;
        let mut kids = Vec::new();
        if self.is(0, ":") {
            self.bump();
            kids.push(if list == VARARG && self.is(0, "*") {
                let at = self.t(0).start as u32;
                self.bump();
                let v = self.slot(Seq::One(BITOR), STARRED_ANNOTATION)?;
                self.mk_ctx("Starred", at, &[v])
            } else {
                let at = if list == VARARG {
                    VARARG_ANNOTATION
                } else {
                    ANNOTATION - nearer
                };
                self.slot(Seq::One(EXPR), at)?
            });
        }
        let arg = self.named("arg", start, &kids, Some(id));
        lists[list].push(arg);
        if !self.is(0, "=") {
            return Ok(false);
        }
        if list == VARARG || list == KWARG {
            return syntax();
        }
        self.bump();
        let v = self.slot(Seq::One(EXPR), DEFAULT - nearer)?;
        lists[if list == ARGS { DEFAULTS } else { KW_DEFAULTS }].push(v);
        Ok(true)
    }

    /// `'(' [arguments] ')'` after a class's name, read as a call's
    /// arguments are (positional and starred, then keywords and `**`), but
    /// for a generator, which needs its own parentheses here: the bases
    /// then the keywords.
    fn class_arguments(&mut self) -> Res<Vec<u32>> {
        let (open, at) = (self.t(0).start as u32, self.t(1).start as u32);
        // The call's callee, which no tree holds.
        let func = self.mk("Name", open, &[]);
        self.bump();
        // The first argument is sought to the leaf even where none is, at
        // most `ARGUMENTS.0 + 1` past the place: `Parser::near` has
        // declined every statement whose place lies within
        // `STATEMENT_PASS` (10) of `MAXSTACK`, so that seeking never
        // passes it and needs no guard of its own.
        self.placed = self.placed.max(self.place + ARGUMENTS.0 + 2);
        let base = self.frames.len();
        let fr = Fr::Call {
            func,
            args: Vec::new(),
            kws: Vec::new(),
            kw: None,
            dstar: false,
            seen: 0,
            open,
            at,
        };
        self.push(fr, ARGUMENTS, false);
        let mut st = St::Operand;
        let call = loop {
            let next = match st {
                St::Post(v) if self.frames.len() == base => break v,
                St::Operand => self.operand(),
                St::Post(v) => self.post(v),
                St::End(_) if self.is(0, ",") => syntax(),
                St::End(v) => self.take(Some(v)),
                St::Done(v) => break v,
            };
            // A ValueError only the second pass reads (an argument after a
            // keyword) is answered as a call's.
            st = next.map_err(|e| self.first_value(e))?;
        };
        let kids = self.tree.kids(call)[1..].to_vec();
        let generator =
            |n: &u32| self.kind(*n) == "GeneratorExp" && self.tree.nodes[*n as usize].start == open;
        if kids.iter().any(generator) {
            return syntax();
        }
        Ok(kids)
    }

    /// One `except` or `except*` clause of the `try` `open` up to its
    /// `:`: its type, if any, and its alias. The first makes the
    /// statement a Try or a TryStar; a clause of the other kind is refused
    /// once `invalid_try_stmt` has read its type, and `except*` takes a
    /// type.
    pub(super) fn handler(&mut self, open: &mut Open) -> Res<()> {
        let start = self.t(0).start as u32;
        self.bump();
        let star = self.is(0, "*");
        if star {
            self.bump();
        }
        if open.handlers() == 0 && star {
            open.kind = "TryStar";
        } else if star != (open.kind == "TryStar") {
            if !self.is(0, ":") {
                self.read_on(|p| p.slot(Seq::One(EXPR), EXCEPT_TYPE))?;
            }
            return syntax();
        }
        let mut head = Vec::new();
        if star || !self.is(0, ":") {
            head.push(self.slot(Seq::One(EXPR), EXCEPT_TYPE)?);
        }
        let mut b = Branch::new(start, head);
        if self.kw(0) == Some("as") {
            self.bump();
            if !self.name(0) {
                return syntax();
            }
            b.name = Some(self.ident()?);
        }
        self.colon()?;
        open.branches.push(b);
        open.line = open.place + EXCEPT_LINE;
        Ok(())
    }
}
