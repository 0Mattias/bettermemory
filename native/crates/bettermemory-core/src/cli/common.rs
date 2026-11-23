//! `cli/_common.py`, its POSIX read-only store selection, supplied-path opener,
//! and parser-free leaves: `_refuse`, `exit_if_bundle_file`, `key_unreadable_line`,
//! `exit_if_key_unreadable`, `migration_line` and `parse_iso_cutoff`.
//! The owner-store command context follows separately.

mod read_only;
pub use read_only::{
    open_read_only, open_read_only_with, Diagnostic, ReadOnlyError, ReaderFailure, StatFailure,
};

#[cfg(unix)]
pub mod read_only_path;
#[cfg(unix)]
pub use read_only_path::read_only_store_path;

use std::path::Path;

use super::{Exit, UsageError};
use crate::datetime::{DateError, DateTime};
use crate::log::ring::{KeyUnreadable, RingError};
use crate::printable::printable;
use crate::pyvalue::{repr_str, PyStr};
use crate::unicode::is_re_digit;

fn s(text: &str) -> PyStr {
    text.chars().map(u32::from).collect()
}

fn push(out: &mut PyStr, text: &str) {
    out.extend(text.chars().map(u32::from));
}

/// `_refuse(parser, message, exit_code)` (`_common.py` 44-51): a usage
/// error through the parser when the code is 2, else the message alone.
pub fn refuse(parser: &impl UsageError, message: &[u32], exit_code: i32) -> Exit {
    if exit_code == 2 {
        return parser.error(&message.to_vec());
    }
    let mut stderr = parser.prog().clone();
    push(&mut stderr, ": error: ");
    stderr.extend_from_slice(message);
    push(&mut stderr, "\n");
    Exit {
        code: exit_code,
        stderr,
    }
}

/// `exit_if_bundle_file(path, exit_code=, prefix=)` (`_common.py`
/// 130-145) given `backup.bundle_refusal(path)`'s answer.
pub fn exit_if_bundle_file(
    refusal: Option<PyStr>,
    exit_code: i32,
    prefix: &[u32],
) -> Result<(), Exit> {
    let Some(refusal) = refusal else {
        return Ok(());
    };
    let mut stderr = prefix.to_vec();
    push(&mut stderr, ": ");
    stderr.extend(refusal);
    push(&mut stderr, "\n");
    Err(Exit {
        code: exit_code,
        stderr,
    })
}

/// `str(path)` as Python holds it: `os.fsdecode` of the bytes on POSIX,
/// the UTF-16 units (a lone surrogate kept) on Windows.
fn path_text(path: &Path) -> PyStr {
    crate::fsutil::fsdecode(path.as_os_str())
}

/// `key_unreadable_line(exc, prefix=)` (`_common.py` 148-158): `str(exc)`
/// is `KeyUnreadableError`'s message, built here from its path and reason.
pub fn key_unreadable_line(exc: &KeyUnreadable, prefix: &[u32]) -> PyStr {
    let mut line = prefix.to_vec();
    push(&mut line, ": the key file ");
    line.extend(path_text(&exc.path));
    push(&mut line, " is there and cannot be read (");
    push(&mut line, &exc.reason);
    push(
        &mut line,
        "); the store is not rekeyed over it; make it readable and run the command again",
    );
    printable(&line)
}

/// `with exit_if_key_unreadable(exit_code=, prefix=):` (`_common.py`
/// 161-180) around the block's outcome: a `KeyUnreadableError` ends the
/// command, anything else passes through unchanged.
pub fn exit_if_key_unreadable<T>(
    outcome: Result<T, RingError>,
    exit_code: i32,
    prefix: &[u32],
) -> Result<Result<T, RingError>, Exit> {
    match outcome {
        Err(RingError::KeyUnreadable(exc)) => {
            let mut stderr = key_unreadable_line(&exc, prefix);
            push(&mut stderr, "\n");
            Err(Exit {
                code: exit_code,
                stderr,
            })
        }
        other => Ok(other),
    }
}

/// The totals `migration_line` reads off a `MigrationReport`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct MigrationTotals {
    pub projects: i64,
    pub checkouts: i64,
    pub memory_homes: i64,
    pub tombstone_homes: i64,
    pub episode_homes: i64,
    pub proposals: i64,
    pub log_rows: i64,
}

/// `migration_line(report)` (`_common.py` 320-328); `report.homed` is the
/// sum of the three home counts (Python's integers do not overflow).
pub fn migration_line(t: &MigrationTotals) -> PyStr {
    let homed =
        i128::from(t.memory_homes) + i128::from(t.tombstone_homes) + i128::from(t.episode_homes);
    s(&format!(
        "membership migrated: {homed} records homed ({} memories, {} removed memories, \
         {} episodes), {} projects, {} checkouts, {} legacy questions, {} log rows",
        t.memory_homes,
        t.tombstone_homes,
        t.episode_homes,
        t.projects,
        t.checkouts,
        t.proposals,
        t.log_rows
    ))
}

/// What `parse_iso_cutoff` returned, with the warning it wrote first
/// (empty where it wrote none).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Cutoff {
    pub parsed: DateTime,
    pub canonical: PyStr,
    pub warning: PyStr,
}

/// How `parse_iso_cutoff` ended other than by returning: `SystemExit`, or
/// an exception it does not catch (OverflowError past year 9999), raised
/// after `stderr` was written.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CutoffFault {
    Exit(Exit),
    Raised { error: DateError, stderr: PyStr },
}

/// `re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)`, by hand (decision rule 3:
/// no choice; `\d` is Unicode's decimal digits on a `str` pattern).
fn bare_date(value: &[u32]) -> bool {
    value.len() == 10
        && value.iter().enumerate().all(|(i, &c)| {
            if i == 4 || i == 7 {
                c == u32::from(b'-')
            } else {
                is_re_digit(c)
            }
        })
}

/// `dt.isoformat().replace("+00:00", "Z")`.
fn z_form(dt: &DateTime) -> String {
    dt.isoformat().replace("+00:00", "Z")
}

/// What `parse_iso_cutoff` does when `datetime.fromisoformat` raises:
/// a `ValueError` becomes the exit-1 message, any other exception
/// propagates with nothing written. `repr` is `repr(value)`.
pub fn isoformat_fault(value: &[u32], flag: &[u32], repr: &[u32], error: DateError) -> CutoffFault {
    if error != DateError::ValueError {
        return CutoffFault::Raised {
            error,
            stderr: PyStr::new(),
        };
    }
    let mut stderr = flag.to_vec();
    push(&mut stderr, ": invalid ISO timestamp ");
    stderr.extend_from_slice(repr);
    push(&mut stderr, ". Expected e.g. '2026-05-25T05:25:35Z'");
    if bare_date(value) {
        push(&mut stderr, " (or '");
        stderr.extend_from_slice(value);
        push(&mut stderr, "T00:00:00Z' if you meant midnight UTC)");
    }
    push(&mut stderr, ".\n");
    CutoffFault::Exit(Exit { code: 1, stderr })
}

/// `parse_iso_cutoff(value, flag=, far_future=)` (`_common.py` 376-477)
/// with `datetime.now(timezone.utc)` given as `now` (aware, UTC).
pub fn parse_iso_cutoff(
    value: &[u32],
    flag: &[u32],
    far_future: &str,
    now: &DateTime,
) -> Result<Cutoff, CutoffFault> {
    let mut replaced = PyStr::with_capacity(value.len());
    for &c in value {
        if c == u32::from(b'Z') {
            push(&mut replaced, "+00:00");
        } else {
            replaced.push(c);
        }
    }
    let repr = repr_str(value);
    let parsed = match DateTime::from_isoformat(&replaced) {
        Ok(dt) => dt,
        Err(error) => return Err(isoformat_fault(value, flag, &repr, error)),
    };
    let Some(instant) = parsed.instant_us() else {
        let mut stderr = flag.to_vec();
        push(&mut stderr, ": ISO timestamp ");
        stderr.extend_from_slice(&repr);
        push(
            &mut stderr,
            " is missing a UTC offset. Pass an explicit offset or trailing `Z` (e.g. '",
        );
        stderr.extend_from_slice(value);
        push(
            &mut stderr,
            "T00:00:00Z' for midnight UTC, or '2026-05-25T01:25:35-04:00' for an explicit \
             offset) so the cutoff isn't silently interpreted as your local zone.\n",
        );
        return Err(CutoffFault::Exit(Exit { code: 1, stderr }));
    };
    let mut warning = PyStr::new();
    let bound = now
        .minus_micros(-86_400_000_000)
        .map_err(|error| CutoffFault::Raised {
            error,
            stderr: PyStr::new(),
        })?;
    if bound.instant_us().is_some_and(|b| instant > b) {
        let now_text = z_form(now);
        if far_future == "refuse" {
            let mut stderr = flag.to_vec();
            push(&mut stderr, ": cutoff ");
            stderr.extend_from_slice(&repr);
            push(
                &mut stderr,
                &format!(
                    " is more than 24 hours in the future (now is {now_text}). This is almost \
                     always a typo \u{2014} a year-2126 cutoff would silently hide every \
                     audited event in the log indefinitely. Pass a timestamp at or before {}.\n",
                    z_form(&bound)
                ),
            );
            return Err(CutoffFault::Exit(Exit { code: 1, stderr }));
        }
        warning = flag.to_vec();
        push(&mut warning, ": warning \u{2014} ");
        warning.extend_from_slice(&repr);
        push(
            &mut warning,
            &format!(
                " is more than 24 hours in the future (now is {now_text}), so nothing was \
                 written after it and the selection below will be empty. This is almost \
                 always a typo'd year.\n"
            ),
        );
    }
    match parsed.to_utc() {
        Some(Ok(utc)) => Ok(Cutoff {
            parsed,
            canonical: s(&z_form(&utc)),
            warning,
        }),
        Some(Err(error)) => Err(CutoffFault::Raised {
            error,
            stderr: warning,
        }),
        None => unreachable!("an aware value has an offset"),
    }
}
