//! A class (`IN`) compiled as `re._compiler` compiles it: the members
//! rewritten by `_optimize_charset` (`_compiler.py` 241-384, with
//! `_mk_bitmap` and `_bytes_to_codes`, 386-399), the opcode the `IN` branch
//! of `_compile` chooses (85-97) and the test `_sre` makes of one code
//! point against the result (`SRE(charset)`, read by differential).
//!
//! [`optimize_charset`] is the reference function for every mode it is
//! called in, the ASCII fold and past the BMP included. [`compile_class`]
//! is what the engine calls: it refuses, by name, the classes whose
//! matching the engine does not reproduce (LOCALE, the ASCII fold, a
//! category without UNICODE, a member past the BMP under IGNORECASE).

use super::fold::{extra_cases, sre_iscased, sre_lower, ASCII, IGNORECASE, LOCALE, UNICODE};
use super::tree::{Category, Feature, Member};
use crate::unicode;

/// One member of a compiled class, as `_optimize_charset` returns it.
/// The words of a bitmap are `_sre.CODESIZE` (four bytes) wide.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Op {
    Negate,
    Literal(u32),
    Range(u32, u32),
    /// A range past the BMP under the Unicode fold: it holds a lowered code
    /// point whose upper case, or which itself, lies in the bounds.
    RangeUniIgnore(u32, u32),
    Category(Category),
    /// `CHARSET`: 256 bits in 8 words.
    Charset([u32; 8]),
    /// `BIGCHARSET`: the number of chunks, the 256 chunk numbers as 64
    /// words (little-endian, as `_bytes_to_codes` casts them on every
    /// platform CI runs), and 8 words per chunk.
    BigCharset(Vec<u32>),
}

/// How `_compile` folds case for `_optimize_charset`: no fixup, sre's
/// ASCII fold (`_sre.ascii_tolower`, no extra cases) or its Unicode fold
/// (`_sre.unicode_tolower` with `_EXTRA_CASES`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Fold {
    None,
    Ascii,
    Unicode,
}

impl Fold {
    /// The fold `_compile` passes for `flags` (`_compiler.py` 48-55).
    pub fn of(flags: u32) -> Fold {
        if flags & IGNORECASE == 0 || flags & LOCALE != 0 {
            Fold::None
        } else if flags & UNICODE != 0 {
            Fold::Unicode
        } else {
            Fold::Ascii
        }
    }

    fn lower(self, c: u32) -> u32 {
        match self {
            Fold::Ascii if (0x41..=0x5A).contains(&c) => c + 0x20,
            Fold::Unicode => sre_lower(c),
            _ => c,
        }
    }

    fn iscased(self, c: u32) -> bool {
        match self {
            Fold::Ascii => c < 0x80 && (c as u8).is_ascii_alphabetic(),
            _ => sre_iscased(c),
        }
    }

    fn fixes(self, c: u32) -> &'static [u32] {
        match self {
            Fold::Unicode => extra_cases(c),
            _ => &[],
        }
    }
}

/// The opcode the `IN` branch of `_compile` emits; `IN_IGNORE` and
/// `IN_LOC_IGNORE` are refused before it is chosen.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InOp {
    In,
    InUniIgnore,
}

/// A class as the engine matches it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Charset {
    pub op: InOp,
    pub ops: Vec<Op>,
}

/// `charmap[i] = 1`, failing as the `IndexError` of the bytearray does.
fn set(map: &mut [u8], i: u32) -> Result<(), ()> {
    *map.get_mut(i as usize).ok_or(())? = 1;
    Ok(())
}

/// The `try` body of `_optimize_charset` for one literal or range. The
/// literal is passed by reference: under a fold it is lowered before the
/// write that may fail, and the retry lowers it again.
fn mark(map: &mut [u8], member: &mut Member, fold: Fold, hascased: &mut bool) -> Result<(), ()> {
    match member {
        Member::Literal(av) => {
            if fold == Fold::None {
                return set(map, *av);
            }
            *av = fold.lower(*av);
            set(map, *av)?;
            for &k in fold.fixes(*av) {
                set(map, k)?;
            }
            if !*hascased && fold.iscased(*av) {
                *hascased = true;
            }
        }
        Member::Range(lo, hi) => {
            for c in *lo..=*hi {
                if fold == Fold::None {
                    set(map, c)?;
                    continue;
                }
                let i = fold.lower(c);
                set(map, i)?;
                for &k in fold.fixes(i) {
                    set(map, k)?;
                }
            }
            if fold != Fold::None && !*hascased {
                *hascased = (*lo..=*hi).any(|c| fold.iscased(c));
            }
        }
        _ => unreachable!("only a literal or a range is marked"),
    }
    Ok(())
}

/// `_mk_bitmap`: bit `j` of word `w` is byte `32 * w + j` of `bits`.
fn mk_bitmap(bits: &[u8]) -> Vec<u32> {
    bits.chunks(32)
        .map(|w| {
            w.iter()
                .enumerate()
                .fold(0, |a, (j, &b)| a | (u32::from(b) << j))
        })
        .collect()
}

fn op_of(member: Member) -> Op {
    match member {
        Member::Negate => Op::Negate,
        Member::Literal(c) => Op::Literal(c),
        Member::Range(lo, hi) => Op::Range(lo, hi),
        Member::Category(c) => Op::Category(c),
    }
}

/// `_optimize_charset(charset, iscased, fixup, fixes)` as `_compile` calls
/// it under `fold`: the members rewritten, and `hascased`.
pub fn optimize_charset(charset: &[Member], fold: Fold) -> (Vec<Op>, bool) {
    let mut out = Vec::new();
    let mut tail = Vec::new();
    let mut map = vec![0u8; 256];
    let mut hascased = false;
    for &member in charset {
        let mut member = member;
        match member {
            Member::Negate => {
                out.push(Op::Negate);
                continue;
            }
            Member::Category(c) => {
                tail.push(Op::Category(c));
                continue;
            }
            _ => {}
        }
        loop {
            if mark(&mut map, &mut member, fold, &mut hascased).is_ok() {
                break;
            }
            if map.len() == 256 {
                map.resize(0x10000, 0);
                continue;
            }
            let op = match member {
                Member::Range(lo, hi) if fold != Fold::None => {
                    hascased = true;
                    if fold == Fold::Unicode {
                        Op::RangeUniIgnore(lo, hi)
                    } else {
                        Op::Range(lo, hi)
                    }
                }
                Member::Literal(av) => {
                    if fold != Fold::None && !hascased && fold.iscased(av) {
                        hascased = true;
                    }
                    Op::Literal(av)
                }
                other => op_of(other),
            };
            tail.push(op);
            break;
        }
    }
    // The runs of the map: at most two, or a bitmap.
    let mut runs = Vec::new();
    let mut q = 0;
    let mut few = true;
    while let Some(p) = map[q..].iter().position(|&b| b == 1).map(|p| p + q) {
        if runs.len() >= 2 {
            few = false;
            break;
        }
        match map[p..].iter().position(|&b| b == 0) {
            None => {
                runs.push((p, map.len()));
                break;
            }
            Some(n) => {
                q = p + n;
                runs.push((p, q));
            }
        }
    }
    if few {
        for (p, q) in runs {
            out.push(if q - p == 1 {
                Op::Literal(p as u32)
            } else {
                Op::Range(p as u32, q as u32 - 1)
            });
        }
        out.extend(tail);
        if hascased || out.len() < charset.len() {
            return (out, hascased);
        }
        return (charset.iter().map(|&m| op_of(m)).collect(), hascased);
    }
    if map.len() == 256 {
        let words = mk_bitmap(&map);
        out.push(Op::Charset(words.try_into().expect("eight words")));
        out.extend(tail);
        return (out, hascased);
    }
    // BIGCHARSET: the chunks of 256 code points numbered by first sight.
    let mut chunks: Vec<&[u8]> = Vec::new();
    let mut mapping = [0u8; 256];
    for (i, chunk) in map.chunks(256).enumerate() {
        let n = chunks.iter().position(|&c| c == chunk).unwrap_or_else(|| {
            chunks.push(chunk);
            chunks.len() - 1
        });
        mapping[i] = n as u8;
    }
    let mut words = vec![chunks.len() as u32];
    words.extend(
        mapping
            .chunks(4)
            .map(|b| u32::from_le_bytes([b[0], b[1], b[2], b[3]])),
    );
    words.extend(mk_bitmap(&chunks.concat()));
    out.push(Op::BigCharset(words));
    out.extend(tail);
    (out, hascased)
}

/// The class `members` of a pattern under `flags` as `_compile` reads them
/// (a `str` pattern carries UNICODE): the rewritten members and the opcode,
/// or the feature the engine refuses.
pub fn compile_class(members: &[Member], flags: u32) -> Result<Charset, Feature> {
    let unicode_mode = flags & UNICODE != 0;
    let not_unicode = if flags & ASCII != 0 {
        Feature::Ascii
    } else {
        Feature::BytesIgnorecase
    };
    if flags & LOCALE != 0 {
        return Err(Feature::Locale);
    }
    if flags & IGNORECASE != 0 && !unicode_mode {
        return Err(not_unicode);
    }
    for m in members {
        match *m {
            Member::Category(_) if !unicode_mode => {
                return Err(if flags & ASCII != 0 {
                    Feature::Ascii
                } else {
                    Feature::BytesCategory
                });
            }
            Member::Literal(c) | Member::Range(_, c) if flags & IGNORECASE != 0 && c > 0xFFFF => {
                return Err(Feature::ClassPastBmpIgnorecase);
            }
            _ => {}
        }
    }
    let (ops, hascased) = optimize_charset(members, Fold::of(flags));
    let op = if hascased {
        InOp::InUniIgnore
    } else {
        InOp::In
    };
    Ok(Charset { op, ops })
}

/// `SRE_UNI_IS_*` of a category under UNICODE (`CH_UNICODE`).
fn category(c: Category, ch: u32) -> bool {
    match c {
        Category::CATEGORY_DIGIT => unicode::is_decimal(ch),
        Category::CATEGORY_NOT_DIGIT => !unicode::is_decimal(ch),
        Category::CATEGORY_SPACE => unicode::is_space(ch),
        Category::CATEGORY_NOT_SPACE => !unicode::is_space(ch),
        Category::CATEGORY_WORD => unicode::is_alnum(ch) || ch == 0x5F,
        Category::CATEGORY_NOT_WORD => !(unicode::is_alnum(ch) || ch == 0x5F),
    }
}

fn bit(words: &[u32], i: usize) -> bool {
    words[i / 32] & (1 << (i % 32)) != 0
}

impl Charset {
    /// Whether the text character `ch` is in the class: `_sre`'s `IN` or
    /// `IN_UNI_IGNORE` (which lowers `ch` first), walking the members with
    /// `ok` turned by each `NEGATE`.
    pub fn contains(&self, ch: u32) -> bool {
        let ch = match self.op {
            InOp::In => ch,
            InOp::InUniIgnore => sre_lower(ch),
        };
        let mut ok = true;
        for op in &self.ops {
            let hit = match op {
                Op::Negate => {
                    ok = !ok;
                    false
                }
                Op::Literal(c) => ch == *c,
                Op::Range(lo, hi) => (*lo..=*hi).contains(&ch),
                Op::RangeUniIgnore(lo, hi) => {
                    (*lo..=*hi).contains(&ch) || (*lo..=*hi).contains(&super::fold::sre_upper(ch))
                }
                Op::Category(c) => category(*c, ch),
                Op::Charset(words) => ch < 256 && bit(words, ch as usize),
                Op::BigCharset(words) => {
                    let block = (ch < 0x10000).then(|| {
                        let w = words[1 + (ch as usize >> 8) / 4];
                        (w >> (8 * ((ch as usize >> 8) % 4))) & 0xFF
                    });
                    block.is_some_and(|b| bit(&words[65..], b as usize * 256 + (ch as usize & 255)))
                }
            };
            if hit {
                return ok;
            }
        }
        !ok
    }
}
