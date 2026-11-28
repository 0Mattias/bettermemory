//! The comparison of one `pysyntax_expr*.jsonl` row with
//! `pysyntax::module_expr::parse_expression_module`, for
//! `golden_pysyntax_expr` and `golden_pysyntax_expr_posix`: a source
//! `ast.parse` of 3.13.13 accepts is read into the same tree (its shape in
//! preorder, contexts and operators included) with the same names
//! `symbols._all_names` reads; one it refuses is refused, a parser stack
//! overflow as `stack` and an `ast2obj` RecursionError as `recursion`. A
//! decline is allowed only where the row says: a statement line (`want`
//! `D`), a `\N{...}` escape, or a row near a limit (`band`).

#![allow(dead_code)]

use bettermemory_core::pysyntax::module_expr::{
    parse_expression_module, parse_module, parse_module_blocks, Ctx, ExprError, Tree,
};
use bettermemory_core::pysyntax::module_lex::module_tokens;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;

/// A text as the generator spells it (`gen_pysyntax._built`).
pub fn text(v: &Value) -> Vec<u32> {
    match v {
        Value::String(s) => s.chars().map(u32::from).collect(),
        Value::Array(items) => items.iter().map(|c| c.as_u64().unwrap() as u32).collect(),
        Value::Object(_) => {
            let mut out = Vec::new();
            for pair in v["r"].as_array().unwrap() {
                let piece = text(&pair[0]);
                for _ in 0..pair[1].as_u64().unwrap() {
                    out.extend_from_slice(&piece);
                }
            }
            out
        }
        other => panic!("not a text: {other:?}"),
    }
}

/// The tree in preorder by class, children in field order and the
/// context last (`_shape`).
pub fn shape(t: &Tree) -> String {
    enum It {
        S(&'static str),
        N(u32),
    }
    let mut out = String::from("Module");
    let mut stack: Vec<It> = Vec::new();
    let push_kids = |stack: &mut Vec<It>, kids: Vec<It>| {
        if kids.is_empty() {
            return;
        }
        stack.push(It::S(")"));
        for (i, k) in kids.into_iter().rev().enumerate() {
            if i > 0 {
                stack.push(It::S(","));
            }
            stack.push(k);
        }
        stack.push(It::S("("));
    };
    push_kids(&mut stack, t.body.iter().map(|&b| It::N(b)).collect());
    while let Some(it) = stack.pop() {
        match it {
            It::S(s) => out.push_str(s),
            It::N(n) => {
                let node = &t.nodes[n as usize];
                out.push_str(node.kind);
                let mut kids: Vec<It> = t.kids(n).iter().map(|&k| It::N(k)).collect();
                match node.ctx {
                    Ctx::Load => kids.push(It::S("Load")),
                    Ctx::Store => kids.push(It::S("Store")),
                    Ctx::Del => kids.push(It::S("Del")),
                    Ctx::None => {}
                }
                push_kids(&mut stack, kids);
            }
        }
    }
    out
}

/// Store-context Names, `arg`s, Attribute names and keyword names, each
/// sorted without repeats (`_facts`).
pub fn facts(t: &Tree) -> [Vec<String>; 4] {
    let mut sets: [std::collections::BTreeSet<String>; 4] = Default::default();
    let mut stack: Vec<u32> = t.body.clone();
    while let Some(n) = stack.pop() {
        let node = &t.nodes[n as usize];
        let at = match (node.kind, node.ctx) {
            ("Name", Ctx::Store) => Some(0),
            ("arg", _) => Some(1),
            ("Attribute", _) => Some(2),
            ("keyword", _) => Some(3),
            _ => None,
        };
        if let (Some(at), Some(name)) = (at, &node.name) {
            sets[at].insert(name.iter().map(|&c| char::from_u32(c).unwrap()).collect());
        }
        stack.extend_from_slice(t.kids(n));
    }
    sets.map(|s| s.into_iter().collect())
}

/// One sweep source's answer line (`_line`).
pub fn answer_line(t: &[u32]) -> String {
    match parse_expression_module(t) {
        Ok(tree) => {
            let f = facts(&tree);
            let mut parts: Vec<String> = f.iter().map(|l| l.join(",")).collect();
            parts.push(shape(&tree));
            parts.join("|")
        }
        Err(ExprError::Refused(_)) => "E".to_string(),
        Err(ExprError::Declined("statement")) => "D".to_string(),
        Err(e) => panic!(
            "declined {}: {e:?}",
            String::from_iter(t.iter().map(|&c| char::from_u32(c).unwrap()))
        ),
    }
}

#[derive(Default)]
pub struct Counts {
    pub accepted: usize,
    pub refused: usize,
    pub statements: usize,
    pub banded: usize,
    pub band_declined: usize,
    pub named: usize,
}

pub fn check(row: &Value, n: &mut Counts) {
    let t = text(&row["t"]);
    let parse = row["parse"].as_str().unwrap();
    let band = row["band"].as_bool().unwrap_or(false);
    let want_d = row["want"] == "D";
    n.banded += usize::from(band);
    match parse_expression_module(&t) {
        Ok(tree) => {
            assert!(!want_d, "read a statement");
            assert_eq!(parse, "ok", "accepted");
            let f = facts(&tree);
            let want: Vec<Vec<String>> = serde_json::from_value(row["facts"].clone()).unwrap();
            assert_eq!(f.to_vec(), want, "facts");
            let s = shape(&tree);
            match &row["shape"] {
                Value::String(w) => assert_eq!(&s, w, "shape"),
                d => {
                    assert_eq!(s.len() as u64, d["n"].as_u64().unwrap(), "shape length");
                    assert_eq!(
                        crate::common::hex(&Sha256::digest(s.as_bytes())),
                        d["h"].as_str().unwrap(),
                        "shape"
                    );
                }
            }
            n.accepted += 1;
        }
        Err(ExprError::Refused(k)) => {
            assert!(!want_d, "refused a statement line: {k}");
            assert_ne!(parse, "ok", "refused ({k})");
            match k {
                "stack" => assert_eq!(parse, "MemoryError"),
                "recursion" => assert_eq!(parse, "RecursionError"),
                _ => assert!(
                    !matches!(parse, "MemoryError" | "RecursionError"),
                    "refused as {k}: {parse}"
                ),
            }
            n.refused += 1;
        }
        Err(ExprError::Declined("statement")) => {
            assert!(want_d, "declined as a statement");
            n.statements += 1;
        }
        Err(ExprError::Declined("name table")) => {
            assert!(
                t.windows(2).any(|w| w == [0x5C, 0x4E]),
                "declined for the name table"
            );
            n.named += 1;
        }
        Err(ExprError::Declined(why)) => {
            assert!(band, "declined ({why}) off the band");
            n.band_declined += 1;
        }
    }
}

fn ident(v: &[u32]) -> String {
    v.iter().map(|&c| char::from_u32(c).unwrap()).collect()
}

/// The name an `alias` binds: its asname, else its name's first part.
fn bound(t: &Tree, n: u32) -> String {
    let node = &t.nodes[n as usize];
    let name = ident(node.asname.as_deref().or(node.name.as_deref()).unwrap());
    name.split('.').next().unwrap().to_string()
}

/// `symbols._all_names` and `symbols._top_level_names`, each sorted: the
/// first takes a definition's and a handler's name too; the second takes
/// a definition's name and walks no further into it, walks no Lambda and
/// takes an import's aliases.
pub fn names(t: &Tree) -> [Vec<String>; 2] {
    let (mut all, mut top) = (BTreeSet::new(), BTreeSet::new());
    let mut stack: Vec<(u32, bool)> = t.body.iter().map(|&b| (b, true)).collect();
    while let Some((n, scope)) = stack.pop() {
        let node = &t.nodes[n as usize];
        let name = node.name.as_deref().map(ident);
        match (node.kind, node.ctx) {
            ("Name", Ctx::Store) => {
                all.insert(name.clone().unwrap());
                if scope {
                    top.insert(name.unwrap());
                }
            }
            ("arg" | "Attribute" | "keyword" | "ExceptHandler", _) => {
                if let Some(name) = name {
                    all.insert(name);
                }
            }
            ("FunctionDef" | "AsyncFunctionDef" | "ClassDef", _) => {
                all.insert(name.clone().unwrap());
                if scope {
                    top.insert(name.unwrap());
                }
            }
            ("alias", _) => {
                all.insert(bound(t, n));
            }
            ("Import" | "ImportFrom", _) if scope => {
                top.extend(t.kids(n).iter().map(|&k| bound(t, k)));
            }
            _ => {}
        }
        let inner = scope
            && !matches!(
                node.kind,
                "Lambda"
                    | "Import"
                    | "ImportFrom"
                    | "FunctionDef"
                    | "AsyncFunctionDef"
                    | "ClassDef"
            );
        stack.extend(t.kids(n).iter().map(|&k| (k, inner)));
    }
    [all.into_iter().collect(), top.into_iter().collect()]
}

/// The identifiers and numbers no shape holds, in preorder (`_ids`).
pub fn ids(t: &Tree) -> String {
    let mut out = Vec::new();
    let mut stack: Vec<u32> = t.body.iter().rev().copied().collect();
    while let Some(n) = stack.pop() {
        let node = &t.nodes[n as usize];
        let name = || node.name.as_deref().map(ident).unwrap_or_default();
        match node.kind {
            "AnnAssign" => out.push(format!("AnnAssign {}", node.num)),
            "ImportFrom" => out.push(format!("ImportFrom {} {}", name(), node.num)),
            "alias" => {
                let asname = node.asname.as_deref().map(ident).unwrap_or_default();
                out.push(format!("alias {} {asname}", name()));
            }
            "Global" | "Nonlocal" => out.push(format!("{} {}", node.kind, name())),
            _ => {}
        }
        stack.extend(t.kids(n).iter().rev());
    }
    out.join(";")
}

/// One block sweep source's answer line (`gen_pysyntax_compound._line`).
pub fn block_line(t: &[u32]) -> String {
    match parse_module_blocks(t) {
        Ok(tree) => {
            let mut parts: Vec<String> = names(&tree).iter().map(|l| l.join(",")).collect();
            parts.push(ids(&tree));
            parts.push(shape(&tree));
            parts.join("|")
        }
        // Where the tokenizer refuses, the family (`_line`).
        Err(ExprError::Refused(k)) => match refused_as(k) {
            "IndentationError" | "TabError" if module_tokens(t).is_err() => {
                "E SyntaxError".to_string()
            }
            class => format!("E {class}"),
        },
        Err(ExprError::Declined("statement")) => "D".to_string(),
        Err(e) => panic!("declined {}: {e:?}", ident(t)),
    }
}

/// One statement sweep source's answer line (`gen_pysyntax_stmt._line`).
pub fn stmt_line(t: &[u32]) -> String {
    match parse_module(t) {
        Ok(tree) => {
            let mut parts: Vec<String> = names(&tree).iter().map(|l| l.join(",")).collect();
            parts.push(ids(&tree));
            parts.push(shape(&tree));
            parts.join("|")
        }
        // A subclass of SyntaxError as SyntaxError: the port reads every
        // token first, so a tokenizer refusal may stand where the parser
        // raises IndentationError sooner.
        Err(ExprError::Refused(k)) => match refused_as(k) {
            "IndentationError" | "TabError" => "E SyntaxError".to_string(),
            class => format!("E {class}"),
        },
        Err(ExprError::Declined("statement")) => "D".to_string(),
        Err(e) => panic!("declined {}: {e:?}", ident(t)),
    }
}

/// The class `ast.parse` raises for a refusal of kind `k`: the parser's
/// stack, `ast2obj`'s depth, a constant's identifier, an indentation the
/// tokenizer or the statements refuse, else a SyntaxError.
pub fn refused_as(k: &str) -> &'static str {
    match k {
        "stack" => "MemoryError",
        "recursion" => "RecursionError",
        "value" => "ValueError",
        "indent"
        | "expected an indented block"
        | "unexpected indent"
        | "unindent does not match any outer indentation level"
        | "too many levels of indentation" => "IndentationError",
        "inconsistent use of tabs and spaces" => "TabError",
        _ => "SyntaxError",
    }
}

/// One `pysyntax_stmt.jsonl` row against `parse_module`, as [`check`]
/// compares an expression row, with each refusal's kind tied to the class
/// `ast.parse` raises ([`refused_as`]).
pub fn check_stmt(row: &Value, n: &mut Counts) {
    check_with(row, n, parse_module, false);
}

/// One `pysyntax_compound*.jsonl` row against `parse_module_blocks`, as
/// [`check_stmt`] compares a statement row.
pub fn check_block(row: &Value, n: &mut Counts) {
    check_with(row, n, parse_module_blocks, false);
}

/// One `pysyntax_stmt.jsonl` row against `parse_module_blocks`: a row
/// `parse_module` reads is read alike; one it declines (`want` `D`, a
/// compound statement or a type alias) is declined, or answered with the
/// class `ast.parse` gives (its tree is not in the row).
pub fn check_stmt_as_block(row: &Value, n: &mut Counts) {
    check_with(row, n, parse_module_blocks, true);
}

fn check_with(
    row: &Value,
    n: &mut Counts,
    entry: fn(&[u32]) -> Result<Tree, ExprError>,
    open_d: bool,
) {
    let t = text(&row["t"]);
    let parse = row["parse"].as_str().unwrap();
    let band = row["band"].as_bool().unwrap_or(false);
    let want_d = row["want"] == "D";
    n.banded += usize::from(band);
    if open_d && want_d {
        match entry(&t) {
            Ok(_) => assert_eq!(parse, "ok", "accepted"),
            Err(ExprError::Refused(k)) => assert_eq!(parse, refused_as(k), "refused ({k})"),
            Err(ExprError::Declined(why)) => assert_eq!(why, "statement", "declined"),
        }
        n.statements += 1;
        return;
    }
    match entry(&t) {
        Ok(tree) => {
            assert!(!want_d, "read a compound statement");
            assert_eq!(parse, "ok", "accepted");
            let [all, top] = names(&tree);
            let want = |k: &str| -> Vec<String> { serde_json::from_value(row[k].clone()).unwrap() };
            assert_eq!(all, want("all"), "_all_names");
            assert_eq!(top, want("top"), "_top_level_names");
            assert_eq!(ids(&tree), row["ids"].as_str().unwrap(), "ids");
            let s = shape(&tree);
            match &row["shape"] {
                Value::String(w) => assert_eq!(&s, w, "shape"),
                d => {
                    assert_eq!(s.len() as u64, d["n"].as_u64().unwrap(), "shape length");
                    assert_eq!(
                        crate::common::hex(&Sha256::digest(s.as_bytes())),
                        d["h"].as_str().unwrap(),
                        "shape"
                    );
                }
            }
            n.accepted += 1;
        }
        Err(ExprError::Refused(k)) => {
            assert!(!want_d, "refused a compound statement line: {k}");
            assert_eq!(parse, refused_as(k), "refused ({k})");
            n.refused += 1;
        }
        Err(ExprError::Declined("statement")) => {
            assert!(want_d, "declined as a statement");
            n.statements += 1;
        }
        Err(ExprError::Declined("name table")) => {
            assert!(
                t.windows(2).any(|w| w == [0x5C, 0x4E]),
                "declined for the name table"
            );
            n.named += 1;
        }
        Err(ExprError::Declined(why)) => {
            assert!(band, "declined ({why}) off the band");
            n.band_declined += 1;
        }
    }
}
