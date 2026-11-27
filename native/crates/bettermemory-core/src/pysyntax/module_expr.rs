//! CPython 3.13.13's expression grammar over [`super::module_lex`]'s
//! tokens, as `ast.parse(source)` in `claims.check_claim` (`claims.py`
//! 672) reads a module of expression statements: which sources the
//! parser's first pass accepts (the actions that refuse included: bytes
//! beside str, an identifier whose NFKC is `True`, `False` or `None`, an
//! f-string conversion), the parser's stack limit, the depth `ast2obj`
//! allows, and an arena of nodes with the names `symbols._all_names`
//! reads. Nothing recurses: one loop drives a vector of frames, an
//! operator awaiting its operand or a bracket awaiting its next element.

use super::lex::{decode_str, Tok};
use super::module_lex::{module_tokens, Kind, ModToken, Module, ModuleError};
use std::collections::HashMap;

mod compound;
mod definition;
mod matching;
mod stmt;
pub use compound::parse_module_blocks;
pub use stmt::parse_module;

/// `MAXSTACK`: the parser's rule calls nested at once.
pub const MAXSTACK: i32 = 6000;
/// The rule calls from a statement to a name leaf (`-` * 5969 + `x`
/// parses, one more `-` overflows).
const BASE: i32 = 31;
/// The most the assignment-target alternatives a statement tries first
/// lower the depth at which a bracket's contents are first parsed.
const TOP_DISCOUNT: i32 = 60;
/// `Py_C_RECURSION_LIMIT`, which `ast2obj` holds the tree's depth to.
#[cfg(all(windows, target_arch = "aarch64"))]
pub const C_RECURSION_LIMIT: u32 = 1000;
#[cfg(all(windows, not(target_arch = "aarch64")))]
pub const C_RECURSION_LIMIT: u32 = 3000;
#[cfg(not(windows))]
pub const C_RECURSION_LIMIT: u32 = 10_000;
/// The C recursion depth `ast.parse` starts `ast2obj` at when a Python
/// script calls it, and the most further callers may add.
pub const START_DEPTH: u32 = 3;
pub const START_BAND: u32 = 100;

// Operand levels: the loosest construct a slot takes.
const NAMED: u8 = 0;
const EXPR: u8 = 1;
const DISJ: u8 = 2;
const CONJ: u8 = 3;
const INV: u8 = 4;
const CMP: u8 = 5;
const BITOR: u8 = 6;
const FACTOR: u8 = 12;
const POW: u8 = 13;
const AWAIT: u8 = 14;
const PRIMARY: u8 = 15;

// Rule calls from a bracket's level to its first element's (most,
// least); after the first comma the later elements' lie the shift on.
const PAREN: (i32, i32) = (29, 22);
const LIST: (i32, i32) = (29, 23);
const CALL: (i32, i32) = (27, 24);
const SUB: (i32, i32) = (26, 24);
const FIELD: (i32, i32) = (33, 28);
const SPEC: (i32, i32) = (9, -10);
const TARGET: (i32, i32) = (11, -40);
/// The most calls above its atom at which a NAME becomes an identifier:
/// at a slot `:=` may open, `assignment_expression` (and a keyword
/// argument's name) takes the NAME before any alternative descends to
/// the atom (22 in a parenthesis, list or call; 0 in a brace or
/// subscript, whose first alternative descends).
const NAMED_IDENT: i32 = 22;
/// What [`Parser::reach`] adds for an open bracket: the most of any
/// bracket's range ([`FIELD`]) and of its later elements' shift.
const OPEN: i32 = FIELD.0 + 5;
/// The most calls a leaf lies past its slot (an f-string's).
const LEAF: i32 = 7;
/// The calls from a lambda's frame, [`LAMBDA`] past its slot's atom, to
/// a parameter's NAME, one call past that atom.
const PARAM: i32 = 1 - LAMBDA.0;
/// The calls from a lambda's slot to its parameters.
const LAMBDA: (i32, i32) = (8, 8);
/// The most calls the parser's second pass (its `invalid_*` rules) makes
/// past the first pass's path at a token.
const SECOND_PASS: i32 = 20;
/// The calls from an expression opening with `NAME` to the next element
/// of the `star_expressions` `invalid_legacy_expression` reads after it
/// (`x + x, ` * 994 + `x $` raises MemoryError, 993 SyntaxError).
const LEGACY: i32 = 6;
/// The calls `invalid_arithmetic` or `invalid_factor` nests per `not`
/// after an arithmetic operator (`x` + ` + not x` * 543 raises
/// MemoryError, 542 SyntaxError).
const ARITHMETIC: i32 = 11;

// A lambda's parameter lists, in `arguments`' field order.
const POSONLY: usize = 0;
const ARGS: usize = 1;
const VARARG: usize = 2;
const KWONLY: usize = 3;
const KW_DEFAULTS: usize = 4;
const KWARG: usize = 5;
const DEFAULTS: usize = 6;

const KEYWORDS: [&str; 35] = [
    "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class", "continue",
    "def", "del", "elif", "else", "except", "finally", "for", "from", "global", "if", "import",
    "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try", "while",
    "with", "yield",
];
const STATEMENT: [&str; 24] = [
    "assert", "async", "break", "class", "continue", "def", "del", "elif", "else", "except",
    "finally", "for", "from", "global", "if", "import", "nonlocal", "pass", "raise", "return",
    "try", "while", "with", "yield",
];
const MARKERS: [&str; 15] = [
    "=", ";", "+=", "-=", "*=", "/=", "//=", "%=", "@=", "&=", "|=", "^=", ">>=", "<<=", "**=",
];

/// Why a source is not read.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExprError {
    /// `ast.parse` raises: `syntax` (SyntaxError), `value` (ValueError, an
    /// identifier `ast` cannot hold), `stack` (MemoryError, the parser's
    /// stack), `recursion` (RecursionError, `ast2obj`), or the
    /// tokenizer's kind.
    Refused(&'static str),
    /// Not answered here: `name table`, `statement` (a statement, unit
    /// 5c), `stack bound` or `recursion bound` (near a limit whose exact
    /// place this port does not know), `second pass` (a ValueError only
    /// the second pass reaches, past a legacy `print` or `exec` whose
    /// SyntaxError may come first).
    Declined(&'static str),
}

type Res<T> = Result<T, ExprError>;

fn syntax<T>() -> Res<T> {
    Err(ExprError::Refused("syntax"))
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Ctx {
    None,
    Load,
    Store,
    Del,
}

/// One node: an `ast` class (operators and comparison operators are
/// nodes too, as `ast.walk` meets them), its span, its context, the
/// identifier of a Name, Attribute, arg or keyword, and its children in
/// `ast`'s field order. The span is code point offsets into
/// [`Module::source`], as `ast`'s `col_offset` and `end_col_offset` on one
/// line, except that an f-string's literal parts and a specification's
/// JoinedStr carry the string's or the field's start; operators,
/// `arguments` and `comprehension`, which `ast` places nowhere, are empty.
#[derive(Clone, Debug)]
pub struct Node {
    pub kind: &'static str,
    pub start: u32,
    pub end: u32,
    pub ctx: Ctx,
    pub name: Option<Box<[u32]>>,
    /// An `alias`'s `asname`.
    pub asname: Option<Box<[u32]>>,
    /// `AnnAssign.simple` or `ImportFrom.level`.
    pub num: u32,
    first: u32,
    count: u32,
    /// The depth `ast2obj` counts below and at this node (0: an operator).
    pub height: u32,
}

#[derive(Clone, Debug, Default)]
pub struct Tree {
    pub nodes: Vec<Node>,
    edges: Vec<u32>,
    /// The module's Expr statements.
    pub body: Vec<u32>,
}

impl Tree {
    pub fn kids(&self, n: u32) -> &[u32] {
        let n = &self.nodes[n as usize];
        &self.edges[n.first as usize..(n.first + n.count) as usize]
    }
}

#[derive(Clone, Copy)]
enum Part {
    Lit(bool),
    Node(u32),
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Seq {
    Top,
    One(u8),
    Paren,
    List,
    Yield(bool),
}

enum Fr {
    /// A prefix or binary operator, `*`, `:=`, a lambda's or a
    /// conditional's last part, awaiting the operand at level `want`.
    Op {
        kind: &'static str,
        lvl: u8,
        want: u8,
        left: Option<u32>,
        mid: Option<u32>,
        start: u32,
    },
    /// `and`, `or` or comparisons, awaiting the next operand.
    Chain {
        cmp: bool,
        vals: Vec<u32>,
        ops: Vec<u32>,
    },
    Seq {
        k: Seq,
        items: Vec<u32>,
        comma: bool,
        start: u32,
    },
    Brace {
        keys: Vec<u32>,
        vals: Vec<u32>,
        dict: Option<bool>,
        key: Option<u32>,
        dstar: bool,
        start: u32,
    },
    /// A call: `open` is where its `(` starts, `at` where the keyword
    /// argument being read starts.
    Call {
        func: u32,
        args: Vec<u32>,
        kws: Vec<u32>,
        kw: Option<Box<[u32]>>,
        dstar: bool,
        seen: u8,
        open: u32,
        at: u32,
    },
    /// A subscript: `at` is where the element being read starts.
    Sub {
        value: u32,
        items: Vec<u32>,
        comma: bool,
        parts: [Option<u32>; 3],
        colons: usize,
        at: u32,
    },
    Cond {
        body: u32,
    },
    Lam(Box<Lam>),
    Comp(Box<Comp>),
    Str(Strs),
    FStr {
        raw: bool,
    },
    Field(Box<Field>),
}

/// Adjacent strings: whether bytes, a str or an f-string was met, and the
/// literal parts and fields so far.
struct Strs {
    bytes: bool,
    text: bool,
    f: bool,
    parts: Vec<Part>,
    start: u32,
}

/// A replacement field: its expressions (state 0), then its `=`,
/// conversion and `:` (1), then its specification's parts (2).
struct Field {
    items: Vec<u32>,
    comma: bool,
    state: u8,
    value: u32,
    debug: bool,
    conv: bool,
    spec: Option<Vec<Part>>,
    raw: bool,
    start: u32,
}

impl Field {
    fn new(raw: bool, start: u32) -> Box<Self> {
        Box::new(Field {
            items: Vec::new(),
            comma: false,
            state: 0,
            value: 0,
            debug: false,
            conv: false,
            spec: None,
            raw,
            start,
        })
    }
}

#[derive(Default)]
struct Lam {
    start: u32,
    lists: [Vec<u32>; 7],
    pos: usize,
    slash: bool,
    star: bool,
    bare: bool,
    kwonly: usize,
    kwarg: bool,
    defaulted: bool,
    after: bool,
    default: Option<usize>,
}

struct Comp {
    kind: &'static str,
    elts: Vec<u32>,
    gens: Vec<u32>,
    state: u8,
    tgt: Vec<u32>,
    tcomma: bool,
    parts: Vec<u32>,
    is_async: bool,
    close: &'static str,
    call: Option<u32>,
    start: u32,
}

struct Frame {
    fr: Fr,
    hi: i32,
    lo: i32,
    br: bool,
}

enum St {
    Operand,
    Post(u32),
    End(u32),
    Done(u32),
}

/// The grammar over one module's tokens, with the arena it builds.
pub struct Parser<'m> {
    m: &'m Module,
    toks: Vec<u32>,
    pub pos: usize,
    end: u32,
    pub tree: Tree,
    frames: Vec<Frame>,
    /// The parser's depth came within this port's bound of `MAXSTACK`.
    pub unsure: bool,
    /// `from __future__ import barry_as_FLUFL` was read: `<>` is `!=`,
    /// and `!=` refused (`_PyPegen_check_barry_as_flufl`).
    barry: bool,
    /// The refusal is one only the second pass reaches (a ValueError past
    /// a target the first pass refuses): its depth decides as a
    /// SyntaxError's does.
    second: bool,
    /// A line before opens with `match_stmt`'s subject, which the second
    /// pass refuses (`invalid_match_stmt`, "expected ':'", or
    /// `invalid_named_expression`): its SyntaxError comes first.
    match_head: bool,
    /// The place past [`BASE`] of the statement being read: its
    /// expressions' depths count from it (a statement in a block lies
    /// deeper than one at the module's level).
    place: i32,
    /// The deepest place a statement or a compound statement's slot was
    /// read at, which the second pass reads again.
    placed: i32,
    /// The refusal is a forced token's (`else` without its `:`), which
    /// the first pass raises with no second pass.
    forced: bool,
    /// The last node a parenthesis returned as its value: a walrus there
    /// is no longer bare.
    grouped: u32,
    /// Where the outermost parenthesis around a node opens: a node built
    /// on it starts there, as `ast`'s do, while it keeps its own span.
    opened: HashMap<u32, u32>,
}

fn same(a: &[u32], s: &str) -> bool {
    a.len() == s.len() && a.iter().zip(s.bytes()).all(|(&c, b)| c == u32::from(b))
}

/// `ast.parse(source)` of a module of expression statements: the tree, or
/// why it is not read. A logical line opening with a statement keyword or
/// holding a statement's `=`, `:` or `;` after its expressions is
/// declined (unit 5c).
pub fn parse_expression_module(text: &[u32]) -> Res<Tree> {
    parse_with(text, |p| p.lines(), 0)
}

/// A module's tokens read by `read` into a tree, or why not: `margin` is
/// what the second pass of `read`'s grammar may go past the expression
/// statement's.
fn parse_with(text: &[u32], read: impl FnOnce(&mut Parser) -> Res<u32>, margin: i32) -> Res<Tree> {
    let m = module_tokens(text).map_err(|e| match e {
        ModuleError::Syntax(k) => ExprError::Refused(k),
        ModuleError::NeedsNameTable => ExprError::Declined("name table"),
    })?;
    let mut p = Parser::new(&m);
    let deepest = match read(&mut p) {
        Ok(d) => d,
        // A ValueError of the first pass ends the parse; a SyntaxError
        // makes the parser read the source again with its error rules,
        // whose MemoryError then stands (`_PyPegen_run_parser`).
        Err(ExprError::Refused(k))
            if k != "stack"
                && (p.unsure
                    || ((k != "value" || p.second) && p.second_pass_may_overflow(margin))) =>
        {
            return Err(ExprError::Declined("stack bound"))
        }
        Err(e) => return Err(e),
    };
    if p.unsure {
        return Err(ExprError::Declined("stack bound"));
    }
    recursion_answer(1 + deepest, C_RECURSION_LIMIT)?;
    Ok(p.tree)
}

/// Whether `ast2obj` builds a tree `depth` deep (the Module counted)
/// under a C recursion limit of `limit`: refused past it from the least
/// start, declined where a caller's own depth decides.
pub fn recursion_answer(depth: u32, limit: u32) -> Res<()> {
    if depth + START_DEPTH > limit {
        Err(ExprError::Refused("recursion"))
    } else if depth + START_DEPTH + START_BAND > limit {
        Err(ExprError::Declined("recursion bound"))
    } else {
        Ok(())
    }
}

impl<'m> Parser<'m> {
    pub fn new(m: &'m Module) -> Self {
        let toks = (0..m.tokens.len() as u32)
            .filter(|&i| !matches!(m.tokens[i as usize].kind, Kind::Comment | Kind::Nl))
            .collect();
        Parser {
            m,
            toks,
            pos: 0,
            end: 0,
            tree: Tree::default(),
            frames: Vec::new(),
            unsure: false,
            barry: false,
            second: false,
            match_head: false,
            place: 0,
            placed: 0,
            forced: false,
            grouped: u32::MAX,
            opened: HashMap::new(),
        }
    }

    /// On a line opening with the soft keyword `match`, `match_stmt`'s
    /// subject read first ([`Parser::match_subject`]): whether the second
    /// pass reads a constant's identifier in it, and the token its first
    /// pass's reading stopped at (`None` on another line).
    fn subject(&mut self) -> Res<(Option<bool>, Option<usize>)> {
        if !self.soft("match") {
            return Ok((None, None));
        }
        match self.match_subject() {
            Ok((value, reach)) => Ok((value, Some(reach))),
            Err(e) => Err(self.first_value(e)),
        }
    }

    /// A refusal of the line after its subject (`reach`, where that
    /// reading stopped), as the reference answers it: a ValueError at a
    /// constant's identifier only the second pass reads as
    /// [`Parser::first_value`] says, and a MemoryError as follows. The
    /// subject's reading leaves its rules' results
    /// in the parser's memo, where the line's own reading takes them
    /// without descending again, so a MemoryError the port counts at a
    /// token the subject's reading reached may be none of the
    /// reference's (`match *` + `-` * 5974 + `x, y` is read, `a *` + `-` *
    /// 5970 + `x, y` overflows). Past that token no rule's result is kept
    /// (a rule that reads a token there would have read it in the subject
    /// too), so the line's reading descends as the port counts:
    /// `match = ` + `-` * 6100 + `x` overflows as `a = ` does.
    fn after_subject(&mut self, e: ExprError, reach: Option<usize>) -> ExprError {
        if e == ExprError::Refused("stack") && reach.is_some_and(|r| self.pos <= r) {
            ExprError::Declined("stack bound")
        } else {
            self.first_value(e)
        }
    }

    /// Every logical line as `star_expressions NEWLINE` up to ENDMARKER:
    /// the depth of the deepest Expr statement. A line opening with the
    /// soft keyword `match` has `match_stmt`'s subject read first.
    fn lines(&mut self) -> Res<u32> {
        let mut deepest = 0;
        loop {
            match self.t(0).kind {
                Kind::EndMarker => return Ok(deepest),
                Kind::Indent | Kind::Dedent => return Err(ExprError::Refused("indent")),
                _ => {}
            }
            if self.statement_line() {
                return Err(ExprError::Declined("statement"));
            }
            let (value, reach) = self.subject()?;
            let line = |p: &mut Self| {
                let v = p.star_expressions()?;
                if p.t(0).kind != Kind::Newline {
                    return syntax();
                }
                Ok(v)
            };
            let v = match line(self) {
                Ok(v) => v,
                // The second pass reads the subject before the line.
                Err(ExprError::Refused("syntax")) if value.is_some() => {
                    return Err(self.second_value(value == Some(true)))
                }
                Err(e) => return Err(self.after_subject(e, reach)),
            };
            let s = self.mk("Expr", self.start(v), &[v]);
            self.bump();
            deepest = deepest.max(self.tree.nodes[s as usize].height);
            self.tree.body.push(s);
        }
    }

    /// Whether the parser's second pass may pass `MAXSTACK` on a source
    /// its first pass refused: it reads every line again, deeper by its
    /// error rules, and past the first pass's failure (`[*lambda: ...]`
    /// fails at `lambda`; `invalid_starred_expression` then reads the
    /// lambdas). A statement in a block counts from its place
    /// (`placed`, the deepest read); a forced token's refusal has no
    /// second pass.
    fn second_pass_may_overflow(&self, margin: i32) -> bool {
        !self.forced && BASE + self.reach() + SECOND_PASS + margin + self.placed > MAXSTACK
    }

    /// What may follow `NAME` in `invalid_legacy_expression`: a token
    /// `star_expressions` may open with, other than `(`.
    fn opens_operand(&self, t: &ModToken) -> bool {
        let text = &self.m.source[t.start..t.end];
        match t.kind {
            Kind::Lex(Tok::Op) => ["-", "+", "~", "*", "[", "{", "..."]
                .iter()
                .any(|s| same(text, s)),
            Kind::Lex(Tok::Name) => KEYWORDS
                .iter()
                .find(|w| same(text, w))
                .is_none_or(|w| ["not", "lambda", "await", "True", "False", "None"].contains(w)),
            Kind::Lex(_) | Kind::FStringStart => true,
            _ => false,
        }
    }

    /// The most rule calls past [`BASE`] a reading of any logical line may
    /// make: per line, [`OPEN`] for each bracket open at a token and,
    /// within the innermost, the most each prefix, operator and keyword of
    /// the element read may add, with a leaf on top. A comma starts the
    /// next element at its bracket's depth, or a lambda's parameters' or a
    /// comprehension target's until its `:` or `in`. Two error rules nest
    /// further: `invalid_legacy_expression` (`NAME !'(' star_expressions`)
    /// reads the rest of the bracket or line inside an expression that
    /// opens with a NAME a possible operand follows, so every later
    /// element lies [`LEGACY`] calls deeper, and `invalid_arithmetic` and
    /// `invalid_factor` read the inversion after an arithmetic operator's
    /// `not` [`ARITHMETIC`] calls deeper, to the element's end.
    fn reach(&self) -> i32 {
        const COMPARE: [&str; 7] = ["<", ">", "==", ">=", "<=", "!=", "<>"];
        // A bracket: the depth before it opened, at its elements, and at
        // the open lambdas' parameters (true) and targets (false) in it.
        struct Level {
            before: i32,
            base: i32,
            marks: Vec<(bool, i32)>,
        }
        let line = || Level {
            before: 0,
            // A statement's later item lies two calls on.
            base: 2,
            marks: Vec::new(),
        };
        let mut levels = vec![line()];
        let (mut cur, mut most) = (2, 0);
        // Whether the token before was an operand's last, may precede an
        // expression's first, or is an arithmetic operator.
        let (mut after_operand, mut at_start, mut after_arithmetic) = (false, true, false);
        for (j, &i) in self.toks.iter().enumerate() {
            let t = &self.m.tokens[i as usize];
            let text = &self.m.source[t.start..t.end];
            let op = |s: &&str| t.kind == Kind::Lex(Tok::Op) && same(text, s);
            let level = levels.last_mut().expect("a level");
            let (mut operand, mut start, mut arithmetic) = (false, false, false);
            match t.kind {
                Kind::Newline | Kind::EndMarker | Kind::Indent | Kind::Dedent => {
                    levels = vec![line()];
                    cur = 2;
                    start = true;
                }
                Kind::Lex(Tok::Op) => {
                    start = ["(", "[", "{", ",", "=", ":", ":="].iter().any(op)
                        || (!after_operand && ["*", "**"].iter().any(op));
                    arithmetic = ["+", "-", "*", "/", "%", "//", "@", "~"].iter().any(op);
                    if ["(", "[", "{"].iter().any(op) {
                        levels.push(Level {
                            before: cur,
                            base: cur + OPEN,
                            marks: Vec::new(),
                        });
                        cur += OPEN;
                    } else if [")", "]", "}"].iter().any(op) {
                        if levels.len() > 1 {
                            cur = levels.pop().expect("a level").before;
                        }
                        operand = true;
                    } else if op(&",") {
                        cur = level
                            .marks
                            .last()
                            .map_or(level.base, |m| m.1.max(level.base));
                    } else if op(&":") {
                        if level.marks.last().is_some_and(|m| m.0) {
                            level.marks.pop();
                        }
                    } else if ["-", "+", "~"].iter().any(op) {
                        cur += i32::from(!after_operand);
                    } else if op(&"**") {
                        cur += 2;
                    } else if COMPARE.iter().any(op) {
                        cur += 3;
                    } else if op(&":=") {
                        cur += 1;
                    } else {
                        operand = op(&"...");
                    }
                }
                Kind::Lex(Tok::Name) => match KEYWORDS.iter().find(|w| same(text, w)) {
                    None => {
                        operand = true;
                        let next = self.toks.get(j + 1).map(|&k| &self.m.tokens[k as usize]);
                        if at_start && next.is_some_and(|t| self.opens_operand(t)) {
                            cur += LEGACY;
                            level.base = level.base.max(cur);
                        }
                    }
                    Some(&"True" | &"False" | &"None") => operand = true,
                    Some(&"not") if after_arithmetic => cur += ARITHMETIC,
                    Some(&"not") => cur += if after_operand { 3 } else { 1 },
                    Some(&"in") if level.marks.last().is_some_and(|m| !m.0) => {
                        level.marks.pop();
                    }
                    Some(&"in" | &"is") => cur += 3,
                    Some(&"and" | &"or") => cur += 2,
                    Some(&"lambda") => {
                        cur += 8;
                        level.marks.push((true, cur));
                    }
                    // `invalid_for_target` reads `star_expressions` after
                    // `for`.
                    Some(&"for") => {
                        cur += TARGET.0;
                        level.marks.push((false, cur));
                        start = true;
                    }
                    Some(&"else") => {
                        cur += 1;
                        start = true;
                    }
                    Some(&"await") => cur += 1,
                    Some(&"yield" | &"from") => start = true,
                    Some(_) => {}
                },
                Kind::Lex(_) | Kind::FStringEnd => operand = true,
                _ => {}
            }
            (after_operand, at_start, after_arithmetic) = (operand, start, arithmetic);
            most = most.max(cur);
        }
        most + LEAF
    }

    /// Whether the logical line at the cursor is a statement other than
    /// an expression: it opens with a statement keyword or `@`, or holds
    /// outside brackets `;`, an assignment's `=` or `op=`, or a `:` that
    /// no `lambda` before it takes (an annotation, a compound
    /// statement's head, `match`, `case`). A `=` between `lambda` and its
    /// `:` is a default.
    pub fn statement_line(&self) -> bool {
        if self.is(0, "@") || self.kw(0).is_some_and(|k| STATEMENT.contains(&k)) {
            return true;
        }
        let (mut depth, mut lambdas) = (0i32, 0usize);
        for k in 0.. {
            let t = self.t(k);
            if matches!(t.kind, Kind::Newline | Kind::EndMarker) {
                return false;
            }
            if ["(", "[", "{"].iter().any(|o| self.is(k, o)) {
                depth += 1;
            } else if [")", "]", "}"].iter().any(|o| self.is(k, o)) {
                depth -= 1;
            } else if depth == 0 && self.kw(k) == Some("lambda") {
                lambdas += 1;
            } else if depth == 0 && self.is(k, ":") {
                if lambdas == 0 {
                    return true;
                }
                lambdas -= 1;
            } else if depth == 0
                && MARKERS.iter().any(|m| self.is(k, m))
                && (lambdas == 0 || !self.is(k, "="))
            {
                return true;
            }
        }
        unreachable!()
    }

    /// `star_expressions` up to the first token it cannot use.
    pub fn star_expressions(&mut self) -> Res<u32> {
        self.run(Seq::Top, 0)
    }

    /// `named_expression` (`NAMED`) or `expression` (`EXPR`).
    pub fn expression(&mut self, named: bool) -> Res<u32> {
        self.run(Seq::One(if named { NAMED } else { EXPR }), 0)
    }

    /// The owner `k` reads its element, `at` calls past the top frame's.
    fn run(&mut self, k: Seq, at: i32) -> Res<u32> {
        let start = self.t(0).start as u32;
        self.push(
            Fr::Seq {
                k,
                items: Vec::new(),
                comma: false,
                start,
            },
            (at, at),
            false,
        );
        let mut st = St::Operand;
        loop {
            st = match st {
                St::Operand => self.operand()?,
                St::Post(v) => self.post(v)?,
                // A yield is the whole of its parenthesis or field.
                St::End(_) if self.is(0, ",") => return syntax(),
                St::End(v) => self.take(Some(v))?,
                St::Done(v) => return Ok(v),
            };
        }
    }

    fn t(&self, k: usize) -> &'m ModToken {
        let i = (self.pos + k).min(self.toks.len() - 1);
        &self.m.tokens[self.toks[i] as usize]
    }

    fn text(&self, k: usize) -> &'m [u32] {
        let t = self.t(k);
        &self.m.source[t.start..t.end]
    }

    fn is(&self, k: usize, s: &str) -> bool {
        self.t(k).kind == Kind::Lex(Tok::Op) && same(self.text(k), s)
    }

    fn kw(&self, k: usize) -> Option<&'static str> {
        let text = self.text(k);
        (self.t(k).kind == Kind::Lex(Tok::Name))
            .then(|| KEYWORDS.iter().copied().find(|w| same(text, w)))
            .flatten()
    }

    fn name(&self, k: usize) -> bool {
        self.t(k).kind == Kind::Lex(Tok::Name) && self.kw(k).is_none()
    }

    fn bump(&mut self) {
        self.end = self.t(0).end as u32;
        self.pos += 1;
    }

    /// The identifier a NAME token makes (`_PyPegen_new_identifier`).
    fn ident(&mut self) -> Res<Box<[u32]>> {
        if self.constant_name() {
            return Err(ExprError::Refused("value"));
        }
        let id = self.m.name(self.t(0));
        self.bump();
        Ok(id.into())
    }

    /// Whether the NAME at the cursor is one `_PyPegen_new_identifier`
    /// refuses: past ASCII, with an NFKC of `True`, `False` or `None`.
    fn constant_name(&self) -> bool {
        self.text(0).iter().any(|&c| c >= 0x80) && {
            let id = self.m.name(self.t(0));
            ["True", "False", "None"].iter().any(|w| same(&id, w))
        }
    }

    fn mk(&mut self, kind: &'static str, start: u32, kids: &[u32]) -> u32 {
        let nodes = &self.tree.nodes;
        let height = 1 + kids
            .iter()
            .map(|&k| nodes[k as usize].height)
            .max()
            .unwrap_or(0);
        let first = self.tree.edges.len() as u32;
        self.tree.edges.extend_from_slice(kids);
        let (end, count) = (self.end.max(start), kids.len() as u32);
        let node = Node {
            kind,
            start,
            end,
            ctx: Ctx::None,
            name: None,
            asname: None,
            num: 0,
            first,
            count,
            height,
        };
        self.tree.nodes.push(node);
        (self.tree.nodes.len() - 1) as u32
    }

    fn mk_ctx(&mut self, kind: &'static str, start: u32, kids: &[u32]) -> u32 {
        let n = self.mk(kind, start, kids);
        self.tree.nodes[n as usize].ctx = Ctx::Load;
        n
    }

    fn named(
        &mut self,
        kind: &'static str,
        start: u32,
        kids: &[u32],
        id: Option<Box<[u32]>>,
    ) -> u32 {
        let n = if kind == "arg" || kind == "keyword" {
            self.mk(kind, start, kids)
        } else {
            self.mk_ctx(kind, start, kids)
        };
        self.tree.nodes[n as usize].name = id;
        n
    }

    fn op_node(&mut self, kind: &'static str) -> u32 {
        let n = self.mk(kind, self.end, &[]);
        self.tree.nodes[n as usize].height = 0;
        n
    }

    fn kind(&self, n: u32) -> &'static str {
        self.tree.nodes[n as usize].kind
    }

    /// Where a node built on `n` as its first part starts: at the
    /// parenthesis around `n`, if any.
    fn start(&self, n: u32) -> u32 {
        self.opened
            .get(&n)
            .copied()
            .unwrap_or(self.tree.nodes[n as usize].start)
    }

    fn push(&mut self, fr: Fr, edge: (i32, i32), bracket: bool) {
        let (hi, lo, br) = self
            .frames
            .last()
            .map_or((self.place, self.place, false), |f| (f.hi, f.lo, f.br));
        let lo = lo + edge.1 - if bracket && !br { TOP_DISCOUNT } else { 0 };
        self.frames.push(Frame {
            fr,
            hi: hi + edge.0,
            lo,
            br: br || bracket,
        });
        if bracket {
            // The first element is sought to the leaf even where none is.
            self.unsure |= BASE + hi + edge.0 + 1 > MAXSTACK;
        }
    }

    fn pop(&mut self) -> Frame {
        self.frames.pop().expect("a frame")
    }

    /// A leaf `adj` calls past its slot: refused when even the least
    /// estimate passes `MAXSTACK`.
    fn leaf(&mut self, adj: i32) -> Res<()> {
        self.leaf_within(adj, adj)
    }

    /// A rule `least` to `most` calls past the top slot's atom: refused
    /// when even the least estimate passes `MAXSTACK`, unsure when the
    /// most does.
    fn leaf_within(&mut self, least: i32, most: i32) -> Res<()> {
        let f = self.frames.last().expect("a frame");
        if BASE + f.lo + least > MAXSTACK {
            return Err(ExprError::Refused("stack"));
        }
        self.unsure |= BASE + f.hi + most > MAXSTACK;
        Ok(())
    }

    /// The level of the slot the top frame fills, whether `:=` may open
    /// it, and the level `*` takes there.
    fn spec(&self) -> (u8, bool, Option<u8>) {
        match &self.frames.last().expect("a frame").fr {
            Fr::Op { want, .. } => (*want, false, None),
            Fr::Chain { cmp: true, .. } => (BITOR, false, None),
            Fr::Chain { ops, .. } => (
                if self.kind(ops[0]) == "Or" { CONJ } else { INV },
                false,
                None,
            ),
            Fr::Seq {
                k: Seq::Top | Seq::Yield(false),
                ..
            } => (EXPR, false, Some(BITOR)),
            Fr::Seq { k: Seq::One(l), .. } => (*l, *l == NAMED, None),
            Fr::Seq {
                k: Seq::Paren | Seq::List,
                ..
            } => (NAMED, true, Some(BITOR)),
            Fr::Seq {
                k: Seq::Yield(true),
                ..
            }
            | Fr::Lam(_) => (EXPR, false, None),
            Fr::Brace { dstar: true, .. } => (BITOR, false, None),
            Fr::Brace {
                key: None,
                dict: None | Some(false),
                ..
            } => (NAMED, true, Some(BITOR)),
            Fr::Brace { .. } => (EXPR, false, None),
            Fr::Call {
                kw: None,
                dstar: false,
                ..
            } => (NAMED, true, Some(EXPR)),
            Fr::Call { .. } => (EXPR, false, None),
            Fr::Sub { colons: 0, .. } => (NAMED, true, Some(EXPR)),
            Fr::Sub { .. } => (EXPR, false, None),
            Fr::Cond { .. } => (DISJ, false, None),
            Fr::Comp(c) if c.state == 0 => (PRIMARY, false, Some(PRIMARY)),
            Fr::Comp(_) => (DISJ, false, None),
            Fr::Field(_) => (EXPR, false, Some(BITOR)),
            Fr::Str(_) | Fr::FStr { .. } => (PRIMARY + 1, false, None),
        }
    }

    fn operand(&mut self) -> Res<St> {
        match &self.frames.last().expect("a frame").fr {
            Fr::Str(_) | Fr::FStr { .. } => return self.take(None),
            Fr::Field(f) if f.state == 2 => return self.take(None),
            Fr::Lam(l) if l.default.is_none() => return self.lam(),
            _ => {}
        }
        let (slot, named, star) = self.spec();
        // The parser descends to the slot's atom whatever comes next, but
        // a NAME at a slot `:=` may open is an identifier sooner.
        if self.name(0) && self.constant_name() {
            self.leaf_within(if named { -NAMED_IDENT } else { 0 }, 0)?;
            return Err(ExprError::Refused("value"));
        }
        self.leaf(0)?;
        let start = self.t(0).start as u32;
        let top = self.frames.len() - 1;
        let op = |kind, lvl, want| Fr::Op {
            kind,
            lvl,
            want,
            left: None,
            mid: None,
            start,
        };
        if let Some(want) = star.filter(|_| self.is(0, "*")) {
            self.bump();
            let top_level = matches!(self.frames[top].fr, Fr::Seq { k: Seq::Top, .. });
            self.push(
                op("Starred", 0, want),
                if top_level { (-5, -5) } else { (0, 0) },
                false,
            );
            return Ok(St::Operand);
        }
        let (dstar_next, kw_next) = (self.is(0, "**"), self.name(0) && self.is(1, "="));
        match &mut self.frames[top].fr {
            Fr::Brace {
                dict: None | Some(true),
                key: None,
                dstar,
                ..
            }
            | Fr::Call {
                kw: None, dstar, ..
            } if dstar_next && !*dstar => {
                *dstar = true;
                if let Fr::Call { at, .. } = &mut self.frames[top].fr {
                    *at = start;
                }
                self.bump();
                return Ok(St::Operand);
            }
            Fr::Call {
                kw: None,
                dstar: false,
                ..
            } if kw_next => {
                let id = self.ident()?;
                if let Fr::Call { kw, at, .. } = &mut self.frames[top].fr {
                    (*kw, *at) = (Some(id), start);
                }
                self.bump();
                return Ok(St::Operand);
            }
            _ => {}
        }
        if named && self.name(0) && self.is(1, ":=") {
            let id = self.ident()?;
            let target = self.named("Name", start, &[], Some(id));
            self.tree.nodes[target as usize].ctx = Ctx::Store;
            self.bump();
            let walrus = Fr::Op {
                kind: "NamedExpr",
                lvl: NAMED,
                want: EXPR,
                left: Some(target),
                mid: None,
                start,
            };
            self.push(walrus, (1, 1), false);
            return Ok(St::Operand);
        }
        let prefix = match self.kw(0) {
            Some("lambda") if slot <= EXPR => {
                self.bump();
                let lam = Lam {
                    start,
                    ..Lam::default()
                };
                self.push(Fr::Lam(Box::new(lam)), LAMBDA, false);
                return Ok(St::Operand);
            }
            Some("not") if slot <= INV => Some(op("Not", INV, INV)),
            Some("await") if slot <= AWAIT => Some(op("Await", AWAIT, PRIMARY)),
            Some("yield") if self.empty_head() => {
                self.bump();
                let from = self.kw(0) == Some("from");
                if from {
                    self.bump();
                }
                Some(Fr::Seq {
                    k: Seq::Yield(from),
                    items: Vec::new(),
                    comma: false,
                    start,
                })
            }
            _ => ["-", "+", "~"]
                .iter()
                .zip(["USub", "UAdd", "Invert"])
                .find(|(s, _)| slot <= FACTOR && self.is(0, s))
                .map(|(_, kind)| op(kind, FACTOR, FACTOR)),
        };
        if let Some(fr) = prefix {
            let edge = match fr {
                Fr::Seq { .. } => (0, 0),
                Fr::Op { kind: "Await", .. } => {
                    self.bump();
                    (0, 0)
                }
                _ => {
                    self.bump();
                    (1, 1)
                }
            };
            self.push(fr, edge, false);
            return Ok(St::Operand);
        }
        let v = match &self.t(0).kind {
            Kind::Lex(Tok::Name) => match self.kw(0) {
                None => {
                    let id = self.ident()?;
                    self.named("Name", start, &[], Some(id))
                }
                Some("True" | "False" | "None") => {
                    self.bump();
                    self.mk("Constant", start, &[])
                }
                _ => return self.take(None),
            },
            Kind::Lex(Tok::Int(_) | Tok::Float(_) | Tok::Imag(_)) => {
                self.bump();
                self.leaf(1)?;
                self.mk("Constant", start, &[])
            }
            Kind::Lex(Tok::Str(_) | Tok::Bytes(_)) | Kind::FStringStart => {
                let fr = Fr::Str(Strs {
                    bytes: false,
                    text: false,
                    f: false,
                    parts: Vec::new(),
                    start,
                });
                self.push(fr, (0, 0), false);
                return Ok(St::Operand);
            }
            _ if self.is(0, "...") => {
                self.bump();
                self.leaf(1)?;
                self.mk("Constant", start, &[])
            }
            _ => {
                let (fr, edge) = if self.is(0, "(") {
                    (
                        Fr::Seq {
                            k: Seq::Paren,
                            items: Vec::new(),
                            comma: false,
                            start,
                        },
                        PAREN,
                    )
                } else if self.is(0, "[") {
                    (
                        Fr::Seq {
                            k: Seq::List,
                            items: Vec::new(),
                            comma: false,
                            start,
                        },
                        LIST,
                    )
                } else if self.is(0, "{") {
                    let fr = Fr::Brace {
                        keys: Vec::new(),
                        vals: Vec::new(),
                        dict: None,
                        key: None,
                        dstar: false,
                        start,
                    };
                    (fr, LIST)
                } else {
                    return self.take(None);
                };
                self.bump();
                self.push(fr, edge, true);
                return Ok(St::Operand);
            }
        };
        Ok(St::Post(v))
    }

    /// Whether `yield` may open the top frame: a parenthesis or an
    /// f-string field with nothing in it yet.
    fn empty_head(&self) -> bool {
        match &self.frames.last().expect("a frame").fr {
            Fr::Seq {
                k: Seq::Paren,
                items,
                comma: false,
                ..
            } => items.is_empty(),
            Fr::Field(f) => !f.comma && f.state == 0 && f.items.is_empty(),
            _ => false,
        }
    }

    fn post(&mut self, v: u32) -> Res<St> {
        let start = self.start(v);
        if self.is(0, ".") && self.name(1) {
            self.bump();
            let id = self.ident()?;
            return Ok(St::Post(self.named("Attribute", start, &[v], Some(id))));
        }
        if self.is(0, "(") || self.is(0, "[") {
            let open = self.t(0).start as u32;
            let at = self.t(1).start as u32;
            let (fr, edge) = if self.is(0, "(") {
                let fr = Fr::Call {
                    func: v,
                    args: Vec::new(),
                    kws: Vec::new(),
                    kw: None,
                    dstar: false,
                    seen: 0,
                    open,
                    at,
                };
                (fr, CALL)
            } else {
                let fr = Fr::Sub {
                    value: v,
                    items: Vec::new(),
                    comma: false,
                    parts: [None; 3],
                    colons: 0,
                    at,
                };
                (fr, SUB)
            };
            self.bump();
            self.push(fr, edge, true);
            return Ok(St::Operand);
        }
        match self.infix(v)? {
            Ok(st) => Ok(st),
            Err(v) => {
                let v = self.reduce(v, 0);
                self.take(Some(v))
            }
        }
    }

    /// An operator after a complete operand, or the operand back (reduced
    /// as far as the operator went) when none applies here.
    fn infix(&mut self, v: u32) -> Res<Result<St, u32>> {
        const BIN: [(&str, &str, u8); 19] = [
            ("|", "BitOr", 6),
            ("^", "BitXor", 7),
            ("&", "BitAnd", 8),
            ("<<", "LShift", 9),
            (">>", "RShift", 9),
            ("+", "Add", 10),
            ("-", "Sub", 10),
            ("*", "Mult", 11),
            ("/", "Div", 11),
            ("//", "FloorDiv", 11),
            ("%", "Mod", 11),
            ("@", "MatMult", 11),
            ("**", "Pow", POW),
            ("==", "Eq", CMP),
            ("!=", "NotEq", CMP),
            ("<", "Lt", CMP),
            (">", "Gt", CMP),
            ("<=", "LtE", CMP),
            (">=", "GtE", CMP),
        ];
        let (op, p, n) = match self.kw(0) {
            Some("or") => ("Or", DISJ, 1),
            Some("and") => ("And", CONJ, 1),
            Some("in") => ("In", CMP, 1),
            Some("not") if self.kw(1) == Some("in") => ("NotIn", CMP, 2),
            Some("is") if self.kw(1) == Some("not") => ("IsNot", CMP, 2),
            Some("is") => ("Is", CMP, 1),
            Some("if") => {
                let v = self.reduce(v, DISJ);
                if self.spec().0 > EXPR {
                    return Ok(Err(v));
                }
                self.bump();
                self.push(Fr::Cond { body: v }, (0, 0), false);
                return Ok(Ok(St::Operand));
            }
            Some(_) => return Ok(Err(v)),
            None if self.barry && self.is(0, "!=") => return syntax(),
            None if self.barry && self.is(0, "<>") => ("NotEq", CMP, 1),
            None => match BIN.iter().find(|b| self.is(0, b.0)) {
                Some(&(_, op, p)) => (op, p, 1),
                None => return Ok(Err(v)),
            },
        };
        let chain = p <= CMP;
        let v = self.reduce(v, if chain || p == POW { p + 1 } else { p });
        let extends = match self.frames.last() {
            Some(Frame {
                fr: Fr::Chain { cmp, ops, .. },
                ..
            }) => chain && *cmp == (p == CMP) && (*cmp || self.kind(ops[0]) == op),
            _ => false,
        };
        if !extends && self.spec().0 > p {
            return Ok(Err(v));
        }
        for _ in 0..n {
            self.bump();
        }
        let opn = self.op_node(op);
        if extends {
            if let Some(Frame {
                fr: Fr::Chain { vals, ops, .. },
                ..
            }) = self.frames.last_mut()
            {
                vals.push(v);
                ops.push(opn);
            }
            return Ok(Ok(St::Operand));
        }
        let start = self.start(v);
        if chain {
            let cmp = p == CMP;
            self.push(
                Fr::Chain {
                    cmp,
                    vals: vec![v],
                    ops: vec![opn],
                },
                if cmp { (3, 3) } else { (2, 2) },
                false,
            );
        } else {
            let want = if p == POW { FACTOR } else { p + 1 };
            let edge = if p == POW { (2, 2) } else { (0, 0) };
            self.push(
                Fr::Op {
                    kind: "BinOp",
                    lvl: p,
                    want,
                    left: Some(v),
                    mid: Some(opn),
                    start,
                },
                edge,
                false,
            );
        }
        Ok(Ok(St::Operand))
    }

    /// Closes every operator frame on top whose level is at least `thr`.
    fn reduce(&mut self, mut v: u32, thr: u8) -> u32 {
        loop {
            let lvl = match &self.frames.last().expect("a frame").fr {
                Fr::Op { lvl, .. } => *lvl,
                Fr::Chain { cmp: true, .. } => CMP,
                Fr::Chain { ops, .. } => {
                    if self.kind(ops[0]) == "Or" {
                        DISJ
                    } else {
                        CONJ
                    }
                }
                _ => return v,
            };
            if lvl < thr {
                return v;
            }
            v = match self.pop().fr {
                Fr::Op {
                    kind,
                    left,
                    mid,
                    start,
                    ..
                } => match kind {
                    "Starred" => self.mk_ctx(kind, start, &[v]),
                    "Await" => self.mk(kind, start, &[v]),
                    "Not" | "USub" | "UAdd" | "Invert" => {
                        let o = self.op_node(kind);
                        self.mk("UnaryOp", start, &[o, v])
                    }
                    "BinOp" => self.mk(kind, start, &[left.unwrap(), mid.unwrap(), v]),
                    "IfExp" => self.mk(kind, start, &[mid.unwrap(), left.unwrap(), v]),
                    _ => self.mk(kind, start, &[left.unwrap(), v]),
                },
                Fr::Chain { cmp, mut vals, ops } => {
                    let start = self.start(vals[0]);
                    vals.push(v);
                    let kids: Vec<u32> = if cmp {
                        [&vals[..1], &ops, &vals[1..]].concat()
                    } else {
                        [&ops[..1], &vals].concat()
                    };
                    self.mk(if cmp { "Compare" } else { "BoolOp" }, start, &kids)
                }
                _ => unreachable!(),
            };
        }
    }

    /// The top owner takes a complete element (or none) and reads the
    /// token after it.
    fn take(&mut self, v: Option<u32>) -> Res<St> {
        let Frame { fr, hi, lo, br } = self.pop();
        let back = |p: &mut Self, fr| p.frames.push(Frame { fr, hi, lo, br });
        let back_by = |p: &mut Self, fr, (a, b): (i32, i32)| {
            p.frames.push(Frame {
                fr,
                hi: hi + a,
                lo: lo + b,
                br,
            });
        };
        let comma = self.is(0, ",");
        let starred = |p: &Self, v: u32| p.kind(v) == "Starred";
        match fr {
            Fr::Seq {
                k,
                mut items,
                comma: seen,
                start,
            } => {
                let close = match k {
                    Seq::Top | Seq::One(_) | Seq::Yield(_) => {
                        !comma || k == Seq::Yield(true) || matches!(k, Seq::One(_))
                    }
                    Seq::Paren => self.is(0, ")"),
                    Seq::List => self.is(0, "]"),
                };
                match v {
                    Some(v) if comma && !close => {
                        items.push(v);
                        let by = match k {
                            _ if seen => (0, 0),
                            Seq::List => (1, 1),
                            _ => (2, 2),
                        };
                        let fr = Fr::Seq {
                            k,
                            items,
                            comma: true,
                            start,
                        };
                        back_by(self, fr, by);
                        self.bump();
                        return Ok(St::Operand);
                    }
                    Some(v) if close => items.push(v),
                    Some(v)
                        if matches!(k, Seq::Paren | Seq::List)
                            && items.is_empty()
                            && !seen
                            && self.comp_head() =>
                    {
                        let kind = if k == Seq::List {
                            "ListComp"
                        } else {
                            "GeneratorExp"
                        };
                        let close = if k == Seq::List { "]" } else { ")" };
                        return self.comp(kind, vec![v], close, None, start, (hi, lo, br));
                    }
                    None if close
                        && (seen || (items.is_empty() && !matches!(k, Seq::Top | Seq::One(_)))) => {
                    }
                    _ => return syntax(),
                }
                // A display spans its closing bracket.
                if matches!(k, Seq::Paren | Seq::List) {
                    self.bump();
                }
                let node = match k {
                    Seq::Paren if seen || items.is_empty() => self.mk_ctx("Tuple", start, &items),
                    Seq::List => self.mk_ctx("List", start, &items),
                    Seq::Paren if starred(self, items[0]) => return syntax(),
                    Seq::Yield(from) => {
                        if from && items.is_empty() {
                            return syntax();
                        }
                        let value = if seen {
                            vec![self.mk_ctx("Tuple", self.start(items[0]), &items)]
                        } else {
                            items
                        };
                        return Ok(St::End(self.mk(
                            if from { "YieldFrom" } else { "Yield" },
                            start,
                            &value,
                        )));
                    }
                    _ if seen => self.mk_ctx("Tuple", start, &items),
                    _ => {
                        self.grouped = items[0];
                        self.opened.insert(items[0], start);
                        items[0]
                    }
                };
                if matches!(k, Seq::Top | Seq::One(_)) {
                    return Ok(St::Done(node));
                }
                Ok(St::Post(node))
            }
            Fr::Brace {
                mut keys,
                mut vals,
                mut dict,
                mut key,
                mut dstar,
                start,
            } => {
                if let Some(v) = v {
                    if dstar {
                        (dict, dstar) = (Some(true), false);
                        vals.push(v);
                    } else if let Some(k) = key.take() {
                        keys.push(k);
                        vals.push(v);
                    } else if self.is(0, ":") && dict != Some(false) {
                        if starred(self, v) || self.bare_walrus(v) {
                            return syntax();
                        }
                        self.bump();
                        back(
                            self,
                            Fr::Brace {
                                keys,
                                vals,
                                dict: Some(true),
                                key: Some(v),
                                dstar,
                                start,
                            },
                        );
                        return Ok(St::Operand);
                    } else if dict == Some(true) {
                        return syntax();
                    } else {
                        dict = Some(false);
                        vals.push(v);
                    }
                    if self.comp_head()
                        && vals.len() == 1
                        && keys.len() == dict.map_or(0, usize::from)
                    {
                        let (kind, elts) = match dict {
                            Some(true) if !keys.is_empty() => ("DictComp", vec![keys[0], vals[0]]),
                            Some(false) if !starred(self, vals[0]) => ("SetComp", vals),
                            _ => return syntax(),
                        };
                        return self.comp(kind, elts, "}", None, start, (hi, lo, br));
                    }
                } else if dstar || key.is_some() || !self.is(0, "}") {
                    return syntax();
                }
                if comma {
                    self.bump();
                    let by = if vals.len() == 1 { (1, 1) } else { (0, 0) };
                    back_by(
                        self,
                        Fr::Brace {
                            keys,
                            vals,
                            dict,
                            key,
                            dstar,
                            start,
                        },
                        by,
                    );
                    return Ok(St::Operand);
                }
                if !self.is(0, "}") {
                    return syntax();
                }
                self.bump();
                let node = if dict == Some(false) {
                    self.mk("Set", start, &vals)
                } else {
                    self.mk("Dict", start, &[keys, vals].concat())
                };
                Ok(St::Post(node))
            }
            Fr::Call {
                func,
                mut args,
                mut kws,
                kw,
                dstar,
                mut seen,
                open,
                at,
            } => {
                if let Some(v) = v {
                    if let Some(id) = kw {
                        kws.push(self.named("keyword", at, &[v], Some(id)));
                        seen |= 1;
                    } else if dstar {
                        kws.push(self.mk("keyword", at, &[v]));
                        seen |= 2;
                    } else if starred(self, v) {
                        if seen & 2 != 0 {
                            return syntax();
                        }
                        args.push(v);
                    } else if seen != 0 {
                        return syntax();
                    } else if args.is_empty() && kws.is_empty() && self.comp_head() {
                        return self.comp(
                            "GeneratorExp",
                            vec![v],
                            ")",
                            Some(func),
                            open,
                            (hi, lo, br),
                        );
                    } else {
                        args.push(v);
                    }
                    if comma {
                        self.bump();
                        let by = if args.len() + kws.len() == 1 {
                            (2, 3)
                        } else {
                            (0, 0)
                        };
                        let fr = Fr::Call {
                            func,
                            args,
                            kws,
                            kw: None,
                            dstar: false,
                            seen,
                            open,
                            at,
                        };
                        back_by(self, fr, by);
                        return Ok(St::Operand);
                    }
                } else if kw.is_some() || dstar {
                    return syntax();
                }
                if !self.is(0, ")") {
                    return syntax();
                }
                self.bump();
                Ok(St::Post(self.mk(
                    "Call",
                    self.start(func),
                    &[&[func][..], &args, &kws].concat(),
                )))
            }
            Fr::Sub {
                value,
                mut items,
                comma: mut seen,
                mut parts,
                colons,
                at,
            } => {
                parts[colons] = v.or(parts[colons]);
                let star = parts[0].is_some_and(|p| starred(self, p));
                if self.is(0, ":") && colons < 2 && !star {
                    if colons == 0 && parts[0].is_some_and(|p| self.bare_walrus(p)) {
                        return syntax();
                    }
                    self.bump();
                    back(
                        self,
                        Fr::Sub {
                            value,
                            items,
                            comma: seen,
                            parts,
                            colons: colons + 1,
                            at,
                        },
                    );
                    return Ok(St::Operand);
                }
                if !comma && !self.is(0, "]") {
                    return syntax();
                }
                let elem = if colons > 0 {
                    let kids: Vec<u32> = parts.iter().flatten().copied().collect();
                    self.mk("Slice", at, &kids)
                } else {
                    match parts[0] {
                        Some(e) => e,
                        None => return syntax(),
                    }
                };
                items.push(elem);
                if comma {
                    seen = true;
                    self.bump();
                    if !self.is(0, "]") {
                        let by = if items.len() == 1 { (3, 3) } else { (0, 0) };
                        let fr = Fr::Sub {
                            value,
                            items,
                            comma: true,
                            parts: [None; 3],
                            colons: 0,
                            at: self.t(0).start as u32,
                        };
                        back_by(self, fr, by);
                        return Ok(St::Operand);
                    }
                }
                let slice = if items.len() == 1 && !seen && !star {
                    items[0]
                } else {
                    self.mk_ctx("Tuple", self.start(items[0]), &items)
                };
                self.bump();
                let start = self.start(value);
                Ok(St::Post(self.mk_ctx("Subscript", start, &[value, slice])))
            }
            Fr::Cond { body } => {
                let (Some(test), Some("else")) = (v, self.kw(0)) else {
                    return syntax();
                };
                self.bump();
                let start = self.start(body);
                self.push(
                    Fr::Op {
                        kind: "IfExp",
                        lvl: EXPR,
                        want: EXPR,
                        left: Some(body),
                        mid: Some(test),
                        start,
                    },
                    (1, 1),
                    false,
                );
                Ok(St::Operand)
            }
            Fr::Lam(mut l) => {
                let (Some(at), Some(v)) = (l.default.take(), v) else {
                    return syntax();
                };
                l.lists[at].push(v);
                back(self, Fr::Lam(l));
                self.lam()
            }
            Fr::Comp(c) => self.clause_take(c, v, (hi, lo, br)),
            Fr::Str(mut s) => {
                match &self.t(0).kind {
                    Kind::Lex(Tok::Str(t)) => {
                        s.text = true;
                        s.parts.push(Part::Lit(!t.is_empty()));
                    }
                    Kind::Lex(Tok::Bytes(_)) => s.bytes = true,
                    Kind::FStringStart => {
                        s.f = true;
                        let raw = self.text(0).iter().any(|&c| c | 0x20 == 0x72);
                        self.bump();
                        back(self, Fr::Str(s));
                        self.push(Fr::FStr { raw }, (0, 0), false);
                        return Ok(St::Operand);
                    }
                    _ => {
                        // The strings' rules are entered before their
                        // action checks the concatenation.
                        self.leaf(if s.f { 7 } else { 4 })?;
                        if s.bytes && (s.text || s.f) {
                            return syntax();
                        }
                        return Ok(St::Post(if s.f {
                            self.joined(&s.parts, s.start)
                        } else {
                            self.mk("Constant", s.start, &[])
                        }));
                    }
                }
                self.bump();
                back(self, Fr::Str(s));
                Ok(St::Operand)
            }
            Fr::FStr { raw } => {
                match self.t(0).kind {
                    Kind::FStringMiddle => {
                        let lit = Part::Lit(self.middle(raw));
                        self.parts().push(lit);
                    }
                    Kind::FStringEnd => {
                        self.bump();
                        return Ok(St::Operand);
                    }
                    _ if self.is(0, "{") => {
                        let start = self.t(0).start as u32;
                        self.bump();
                        back(self, Fr::FStr { raw });
                        self.push(Fr::Field(Field::new(raw, start)), FIELD, true);
                        return Ok(St::Operand);
                    }
                    _ => return syntax(),
                }
                self.bump();
                back(self, Fr::FStr { raw });
                Ok(St::Operand)
            }
            Fr::Field(mut f) => {
                if f.state == 0 {
                    match v {
                        Some(v) => f.items.push(v),
                        // Only the last element after a comma may be empty.
                        None if f.comma && !comma => {}
                        None => return syntax(),
                    }
                    if comma {
                        self.bump();
                        let by = if f.comma { (0, 0) } else { (4, 5) };
                        f.comma = true;
                        back_by(self, Fr::Field(f), by);
                        return Ok(St::Operand);
                    }
                    f.value = if f.comma {
                        self.mk_ctx("Tuple", self.start(f.items[0]), &f.items)
                    } else {
                        f.items[0]
                    };
                    f.state = 1;
                }
                loop {
                    if f.state == 2 && self.t(0).kind == Kind::FStringMiddle {
                        let lit = Part::Lit(self.middle(f.raw));
                        f.spec.as_mut().expect("a spec").push(lit);
                        self.bump();
                        continue;
                    }
                    if f.state == 2 && self.is(0, "{") {
                        let at = self.t(0).start as u32;
                        let raw = f.raw;
                        self.bump();
                        back(self, Fr::Field(f));
                        self.push(Fr::Field(Field::new(raw, at)), SPEC, true);
                        return Ok(St::Operand);
                    }
                    if self.is(0, "}") {
                        break;
                    }
                    if f.state != 1 {
                        return syntax();
                    }
                    if self.is(0, "=") && !f.debug && !f.conv {
                        f.debug = true;
                    } else if self.is(0, "!") && !f.conv {
                        let bang = self.t(0).end;
                        self.bump();
                        if !self.name(0) || self.t(0).start != bang {
                            return syntax();
                        }
                        let id = self.ident()?;
                        if !(id.len() == 1 && [0x73, 0x72, 0x61].contains(&id[0])) {
                            return syntax();
                        }
                        f.conv = true;
                        continue;
                    } else if self.is(0, ":") {
                        (f.state, f.spec) = (2, Some(Vec::new()));
                    } else {
                        return syntax();
                    }
                    self.bump();
                }
                self.bump();
                let mut kids = vec![f.value];
                if let Some(parts) = &f.spec {
                    kids.push(self.joined(parts, f.start));
                }
                let fv = self.mk("FormattedValue", f.start, &kids);
                let parts = self.parts();
                if f.debug {
                    parts.push(Part::Lit(true));
                }
                parts.push(Part::Node(fv));
                Ok(St::Operand)
            }
            Fr::Op { .. } | Fr::Chain { .. } => syntax(),
        }
    }

    /// Where a closed field's parts go: the string's, or the enclosing
    /// field's specification.
    fn parts(&mut self) -> &mut Vec<Part> {
        let n = self.frames.len();
        let at = if matches!(self.frames[n - 1].fr, Fr::FStr { .. }) {
            n - 2
        } else {
            n - 1
        };
        match &mut self.frames[at].fr {
            Fr::Str(s) => &mut s.parts,
            Fr::Field(f) => f.spec.as_mut().expect("a specification"),
            _ => unreachable!("a string or a specification"),
        }
    }

    /// Whether an FSTRING_MIDDLE decodes to any text.
    fn middle(&self, raw: bool) -> bool {
        let body = self.text(0);
        !body.is_empty()
            && (raw
                || body.last() == Some(&0x5C)
                || decode_str(body).map_or(true, |s| !s.is_empty()))
    }

    /// A JoinedStr of literal parts (adjacent ones joined, empty ones
    /// dropped) and fields.
    fn joined(&mut self, parts: &[Part], start: u32) -> u32 {
        let mut kids = Vec::new();
        let mut lit = false;
        for p in parts {
            match *p {
                Part::Lit(t) => lit |= t,
                Part::Node(n) => {
                    if std::mem::take(&mut lit) {
                        kids.push(self.mk("Constant", start, &[]));
                    }
                    kids.push(n);
                }
            }
        }
        if lit {
            kids.push(self.mk("Constant", start, &[]));
        }
        self.mk("JoinedStr", start, &kids)
    }

    fn bare_walrus(&self, v: u32) -> bool {
        self.kind(v) == "NamedExpr" && self.grouped != v
    }

    fn comp_head(&self) -> bool {
        matches!(self.kw(0), Some("for" | "async"))
    }

    /// A comprehension from its element: the owner becomes a [`Comp`] at
    /// the owner's level.
    fn comp(
        &mut self,
        kind: &'static str,
        elts: Vec<u32>,
        close: &'static str,
        call: Option<u32>,
        start: u32,
        lv: (i32, i32, bool),
    ) -> Res<St> {
        // A yield is an element only in its own parentheses.
        if elts.iter().any(|&e| {
            self.kind(e) == "Starred"
                || (matches!(self.kind(e), "Yield" | "YieldFrom") && self.grouped != e)
        }) {
            return syntax();
        }
        let c = Comp {
            kind,
            elts,
            gens: Vec::new(),
            state: 0,
            tgt: Vec::new(),
            tcomma: false,
            parts: Vec::new(),
            is_async: false,
            close,
            call,
            start,
        };
        self.clause(Box::new(c), lv)
    }

    /// `['async'] 'for'`: the target follows, `TARGET` past the owner.
    fn clause(&mut self, mut c: Box<Comp>, (hi, lo, br): (i32, i32, bool)) -> Res<St> {
        c.is_async = self.kw(0) == Some("async");
        if c.is_async {
            self.bump();
        }
        if self.kw(0) != Some("for") {
            return syntax();
        }
        self.bump();
        (c.state, c.tcomma) = (0, false);
        self.frames.push(Frame {
            fr: Fr::Comp(c),
            hi: hi + TARGET.0,
            lo: lo + TARGET.1,
            br,
        });
        Ok(St::Operand)
    }

    fn clause_take(
        &mut self,
        mut c: Box<Comp>,
        v: Option<u32>,
        (hi, lo, br): (i32, i32, bool),
    ) -> Res<St> {
        if c.state == 0 {
            match v {
                Some(v) => c.tgt.push(v),
                None if c.tcomma && self.kw(0) == Some("in") => {}
                None => return syntax(),
            }
            if self.is(0, ",") {
                c.tcomma = true;
                self.bump();
                self.frames.push(Frame {
                    fr: Fr::Comp(c),
                    hi,
                    lo,
                    br,
                });
                return Ok(St::Operand);
            }
            if self.kw(0) != Some("in") {
                return syntax();
            }
            let tgt = std::mem::take(&mut c.tgt);
            let target = if c.tcomma {
                self.mk_ctx("Tuple", self.start(tgt[0]), &tgt)
            } else {
                tgt[0]
            };
            self.store(target)?;
            (c.parts, c.state) = (vec![target], 1);
            self.bump();
            self.frames.push(Frame {
                fr: Fr::Comp(c),
                hi: hi - TARGET.0,
                lo: lo - TARGET.1,
                br,
            });
            return Ok(St::Operand);
        }
        let Some(v) = v else {
            return syntax();
        };
        c.parts.push(v);
        if self.kw(0) == Some("if") {
            c.state = 2;
            self.bump();
            self.frames.push(Frame {
                fr: Fr::Comp(c),
                hi,
                lo,
                br,
            });
            return Ok(St::Operand);
        }
        let parts = std::mem::take(&mut c.parts);
        let gen = self.mk("comprehension", self.start(parts[0]), &parts);
        c.gens.push(gen);
        if self.comp_head() {
            return self.clause(c, (hi, lo, br));
        }
        if !self.is(0, c.close) {
            return syntax();
        }
        self.bump();
        let node = self.mk(c.kind, c.start, &[&c.elts[..], &c.gens].concat());
        Ok(St::Post(match c.call {
            Some(f) => self.mk("Call", self.start(f), &[f, node]),
            None => node,
        }))
    }

    /// A comprehension target (`star_targets`) given Store context. A
    /// Starred is never built directly inside another: its operand's slot
    /// takes no `*`.
    fn store(&mut self, target: u32) -> Res<()> {
        let mut stack = vec![target];
        while let Some(n) = stack.pop() {
            match self.kind(n) {
                "Name" | "Attribute" | "Subscript" => {}
                "Starred" | "Tuple" | "List" => stack.extend_from_slice(self.tree.kids(n)),
                _ => return syntax(),
            }
            self.tree.nodes[n as usize].ctx = Ctx::Store;
        }
        Ok(())
    }

    /// A lambda's parameters (`lambda_params`) up to its `:`.
    fn lam(&mut self) -> Res<St> {
        let Frame {
            fr: Fr::Lam(mut l),
            hi,
            lo,
            br,
        } = self.pop()
        else {
            unreachable!()
        };
        loop {
            if l.after && self.is(0, ",") {
                (l.after, l.bare) = (false, l.bare && l.kwonly == 0);
                self.bump();
                continue;
            }
            if self.is(0, ":") {
                if l.bare && l.kwonly == 0 {
                    return syntax();
                }
                self.bump();
                let kids = l.lists.concat();
                let a = self.mk("arguments", l.start, &kids);
                self.push(
                    Fr::Op {
                        kind: "Lambda",
                        lvl: EXPR,
                        want: EXPR,
                        left: Some(a),
                        mid: None,
                        start: l.start,
                    },
                    (2, 2),
                    false,
                );
                return Ok(St::Operand);
            }
            if l.after || l.kwarg {
                return syntax();
            }
            if self.is(0, "/") {
                if l.slash || l.star || l.pos == 0 {
                    return syntax();
                }
                let moved = std::mem::take(&mut l.lists[ARGS]);
                l.lists[POSONLY] = moved;
                (l.slash, l.after) = (true, true);
                self.bump();
                continue;
            }
            let (list, star) = if self.is(0, "*") {
                (VARARG, true)
            } else if self.is(0, "**") {
                (KWARG, true)
            } else {
                (if l.star { KWONLY } else { ARGS }, false)
            };
            if star {
                if (list == VARARG && l.star) || (list == KWARG && l.bare && l.kwonly == 0) {
                    return syntax();
                }
                self.bump();
                l.star |= list == VARARG;
                l.kwarg = list == KWARG;
                if list == VARARG && !self.name(0) {
                    (l.bare, l.after) = (true, true);
                    continue;
                }
            }
            if !self.name(0) {
                return syntax();
            }
            if self.constant_name() {
                // The identifier is made at the parameter's NAME, one call
                // past the atom of the lambda's slot.
                if BASE + lo + PARAM > MAXSTACK {
                    return Err(ExprError::Refused("stack"));
                }
                self.unsure |= BASE + hi + PARAM > MAXSTACK;
            }
            let start = self.t(0).start as u32;
            let id = self.ident()?;
            let arg = self.named("arg", start, &[], Some(id));
            l.lists[list].push(arg);
            l.after = true;
            if list == ARGS {
                l.pos += 1;
            } else if list == KWONLY {
                l.kwonly += 1;
            }
            if self.is(0, "=") {
                if star {
                    return syntax();
                }
                self.bump();
                l.defaulted |= list == ARGS;
                l.default = Some(if list == ARGS { DEFAULTS } else { KW_DEFAULTS });
                self.frames.push(Frame {
                    fr: Fr::Lam(l),
                    hi,
                    lo,
                    br,
                });
                return Ok(St::Operand);
            }
            if list == ARGS && l.defaulted {
                return syntax();
            }
        }
    }
}
