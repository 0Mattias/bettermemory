//! `location.continuity` (`location.py` 1040-1094): witness O where birth
//! time cannot decide (design 2.2, decisions B18 and B20, amendment A2).
//!
//! The readers are closures, asked only where the reference asks them:
//! `holds` (does the history there hold the last HEAD), `confirms`
//! (another witness of the same object) and `reminted` (the token there
//! names the recorded one as the token it replaced). An error of a reader
//! passes through. The init stamps are compared as sets: two sets meet
//! where any item of one equals an item of the other. `T: Ord` puts them
//! in a tree set, where the reference hashes tuples; equality decides in
//! both.

use std::collections::BTreeSet;

/// What decided: `remint`, `token`, `last_head`, `init_stamps`,
/// `same_object` or `nothing_observable`.
pub type Witness = &'static str;

/// The readers `continuity` may ask.
pub struct Readers<'a, E> {
    /// `holds([last_head])`: whether it answered a non-empty collection.
    pub holds: &'a mut dyn FnMut(&[u32]) -> Result<bool, E>,
    /// `confirms`, or None where the caller passes none.
    pub confirms: Option<&'a mut dyn FnMut() -> Result<bool, E>>,
    /// `reminted`, or None where the caller passes none.
    pub reminted: Option<&'a mut dyn FnMut() -> Result<bool, E>>,
}

fn truthy(t: Option<&[u32]>) -> bool {
    t.is_some_and(|t| !t.is_empty())
}

/// `continuity(token, last_head, stamps, token_here=, head_here=,
/// stamps_here=, holds=, confirms=, superseded=, reminted=)`.
#[allow(clippy::too_many_arguments)]
pub fn continuity<T: Ord, E>(
    token: Option<&[u32]>,
    last_head: Option<&[u32]>,
    stamps: &[T],
    token_here: Option<&[u32]>,
    head_here: Option<&[u32]>,
    stamps_here: &[T],
    superseded: bool,
    readers: Readers<'_, E>,
) -> Result<(bool, Witness), E> {
    let Readers {
        holds,
        confirms,
        reminted,
    } = readers;
    let has_token = truthy(token);
    if has_token && token_here.is_some() && token_here != token {
        if let Some(reminted) = reminted {
            if reminted()? {
                return Ok((true, "remint"));
            }
        }
    }
    if has_token && superseded {
        if !truthy(token_here) {
            return Ok((false, "token"));
        }
    } else if has_token {
        return Ok((token_here == token, "token"));
    }
    if let Some(last) = last_head {
        let held = Some(last) == head_here || holds(last)?;
        return Ok((held, "last_head"));
    }
    if !stamps.is_empty() && !stamps_here.is_empty() {
        let mine: BTreeSet<&T> = stamps.iter().collect();
        let meet = stamps_here.iter().any(|s| mine.contains(s));
        return Ok((meet, "init_stamps"));
    }
    if let Some(confirms) = confirms {
        if confirms()? {
            return Ok((true, "same_object"));
        }
    }
    if has_token && token_here == token {
        return Ok((true, "token"));
    }
    Ok((false, "nothing_observable"))
}
