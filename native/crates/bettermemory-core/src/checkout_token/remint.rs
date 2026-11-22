//! `remint` of `checkout_token.py` and its claim: the re-mint that
//! replaces the checkout's token by a rename, and, where it names the
//! lineage it replaces, the claim it reads and renames under (`_claim`,
//! `_wait_for`, `_take`, `_blocked`, `_holder`, `_claim_read`, `_break`,
//! `_break_marks`, `_release`, `_remove_holding`), with
//! `replace_atomic`'s Windows retry (`_fsutil.py` 333-384). The module's
//! fourth and last unit.
//!
//! Effects are arguments, as in [`super::write`]: the file system a
//! [`TokenFs`], the platform, random bytes, clock and pause a
//! [`Machine`]. Inside, an exception the reference raises travels as
//! [`Fault::Raised`]; `remint`'s `except OSError` turns any but a
//! ValueError into `_failed`'s answer.

use super::blake2b::blake2b;
use super::read::{read_at, read_text, Fault, ReadState, REREAD_DELAY_SECONDS};
use super::write::{
    failed, fs_now, guard, info_dir, unlink_quietly, write_temp, Machine, TokenFs, TokenWrite,
    WriteStatus, FILE_MODE, RACE_PAUSE_MAX_SECONDS, RACE_READ_SECONDS, TEMP_PREFIX,
    TEMP_SUFFIX_LENGTH,
};
use super::{
    mint, random_bytes_needed, random_crockford, render, succeeds, successor, Lineage,
    NONCE_LENGTH, TOKEN_LENGTH, TOKEN_NAME,
};
use crate::config::{self, pure_path};
use crate::fsdata::{Flavour, Kind, OsError, PyErr};
use crate::fsutil::{unlink_retrying_by, Remover, REPLACE_ATTEMPTS, REPLACE_BACKOFF_S};
use crate::gitfile_rule::gitdir::Ctx;
use crate::gitfile_rule::hostpath;
use crate::pyvalue::{PyStr, PyValue};

/// `_CLAIM_NAME`: `f"{_TEMP_PREFIX}claim"`.
pub const CLAIM_NAME: &str = "bettermemory.tmp-claim";
/// `_BREAK_PREFIX`: `f"{_TEMP_PREFIX}break-"`.
pub const BREAK_PREFIX: &str = "bettermemory.tmp-break-";
/// `_STALE_BREAK_SECONDS`: a break mark older was left by a killed breaker.
pub const STALE_BREAK_SECONDS: f64 = 5.0;
/// `_CLAIM_WAIT_SECONDS`: the race read's bound.
pub const CLAIM_WAIT_SECONDS: f64 = RACE_READ_SECONDS;
/// `TokenWrite.detail` of `CLAIM_BLOCKED`.
pub const BLOCKED_DETAIL: &str = "no regular file at the claim";
/// `TokenWrite.detail` of `CONTENDED`.
pub const CONTENDED_DETAIL: &str = "other re-mints held the claim";

/// `str(Path(base) / name)` under `flavour`.
fn under(flavour: Flavour, base: &str, name: &str) -> String {
    let joined = hostpath::join(flavour, base.as_bytes(), name.as_bytes());
    let pure = match flavour {
        Flavour::Posix => config::Flavour::Posix,
        Flavour::Windows => config::Flavour::Windows,
    };
    pure_path(&String::from_utf8_lossy(&joined), pure)
}

fn points(text: &str) -> PyStr {
    text.chars().map(u32::from).collect()
}

fn raised(error: OsError) -> Fault {
    Fault::Raised(error)
}

fn status(status: WriteStatus, token: Option<String>, detail: &str) -> TokenWrite {
    TokenWrite {
        status,
        token,
        detail: detail.to_owned(),
    }
}

/// `random_crockford(length)` drawn from the machine.
fn draw(machine: &mut dyn Machine, length: usize) -> Result<String, Fault> {
    let needed = random_bytes_needed(length as i64).unwrap_or(0);
    random_crockford(length as i64, &machine.urandom(needed)).map_err(|_| Fault::NotPorted)
}

/// `remint(git_dir, policy=..., bare=..., replacing=..., only_siblings=...)`.
pub fn remint<F: TokenFs + ?Sized>(
    ctx: &Ctx<'_, F>,
    machine: &mut dyn Machine,
    git_dir: &str,
    policy: bool,
    bare: bool,
    replacing: Option<&Lineage>,
    only_siblings: bool,
) -> Result<TokenWrite, Fault> {
    let fs = ctx.fs;
    if let Some(skipped) = guard(fs, machine, git_dir, policy, bare)? {
        return Ok(skipped);
    }
    let value = match replacing {
        None => {
            let needed = random_bytes_needed(TOKEN_LENGTH as i64).unwrap_or(0);
            mint(&machine.urandom(needed))
        }
        Some(lineage) => {
            let needed = random_bytes_needed(NONCE_LENGTH as i64).unwrap_or(0);
            successor(lineage, &machine.urandom(needed))
        }
    }
    .map_err(|_| Fault::NotPorted)?;
    let body = render(&points(&value)).map_err(|_| Fault::NotPorted)?;
    let info = match info_dir(fs, ctx.flavour, git_dir)? {
        Ok(info) => info,
        Err(error) => return failed(fs, &error),
    };
    let temp = match write_temp(ctx, machine, &info, &body)? {
        Ok(temp) => temp,
        Err(error) => return failed(fs, &error),
    };
    let placed = match replacing {
        None => {
            let path = under(ctx.flavour, &info, TOKEN_NAME);
            replace_atomic(fs, machine, &temp, &path).map(|()| None)
        }
        Some(lineage) => replace_named(ctx, machine, &temp, &info, lineage, only_siblings),
    };
    let answer = match placed {
        Err(Fault::Raised(error)) if error.class != PyErr::Value => failed(fs, &error).map(Some),
        other => other,
    };
    unlink_quietly(fs, machine, temp.as_bytes())?;
    if let Some(answer) = answer? {
        return Ok(answer);
    }
    if !machine.win32() {
        fs.fsync_dir(info.as_bytes());
    }
    Ok(status(WriteStatus::Written, Some(value), ""))
}

/// `replace_atomic(src, dst)`: off Windows one `os.replace`; on Windows
/// up to [`REPLACE_ATTEMPTS`], only a PermissionError tried again after a
/// pause from [`REPLACE_BACKOFF_S`] doubling, the last error raised.
pub fn replace_atomic<F: TokenFs + ?Sized>(
    fs: &F,
    machine: &mut dyn Machine,
    src: &str,
    dst: &str,
) -> Result<(), Fault> {
    if !machine.win32() {
        return fs.replace(src.as_bytes(), dst.as_bytes()).map_err(raised);
    }
    let mut delay = REPLACE_BACKOFF_S;
    let mut attempt = 0;
    loop {
        match fs.replace(src.as_bytes(), dst.as_bytes()) {
            Ok(()) => return Ok(()),
            Err(error) if error.class == PyErr::Permission && attempt + 1 < REPLACE_ATTEMPTS => {
                machine.sleep(delay);
                delay *= 2.0;
            }
            Err(error) => return Err(raised(error)),
        }
        attempt += 1;
    }
}

/// `_replace_named(temp, info, replacing, only_siblings)`: None once
/// renamed, else the token found or what kept the claim; the claim taken
/// is released last, whatever happened.
fn replace_named<F: TokenFs + ?Sized>(
    ctx: &Ctx<'_, F>,
    machine: &mut dyn Machine,
    temp: &str,
    info: &str,
    replacing: &Lineage,
    only_siblings: bool,
) -> Result<Option<TokenWrite>, Fault> {
    let path = under(ctx.flavour, info, TOKEN_NAME);
    let got = claim(ctx, machine, info, temp)?;
    let result = (|| {
        let found = read_at(ctx, path.as_bytes(), &mut |s| machine.sleep(s))?;
        if found.state == ReadState::Present {
            let token = found.token.unwrap_or_default();
            let other = if only_siblings {
                succeeds(&PyValue::Str(points(&token)), Some(replacing))
                    .map_err(|_| Fault::NotPorted)?
            } else {
                points(&token) != replacing.token
            };
            if other {
                return Ok(Some(status(WriteStatus::Exists, Some(token), "")));
            }
        }
        if let Err(kept) = &got {
            return Ok(Some(kept.clone()));
        }
        replace_atomic(ctx.fs, machine, temp, &path)?;
        Ok(None)
    })();
    if let Ok(nonce) = &got {
        let claim = under(ctx.flavour, info, CLAIM_NAME);
        remove_holding(ctx, machine, &claim, nonce)?;
    }
    result
}

/// `_claim(info, temp)`: this re-mint's nonce once the claim is taken,
/// else the answer that kept it (`CONTENDED`, `CLAIM_BLOCKED`). The own
/// file holding the nonce is made first and unlinked after the wait.
fn claim<F: TokenFs + ?Sized>(
    ctx: &Ctx<'_, F>,
    machine: &mut dyn Machine,
    info: &str,
    temp: &str,
) -> Result<Result<String, TokenWrite>, Fault> {
    let fs = ctx.fs;
    let claim = under(ctx.flavour, info, CLAIM_NAME);
    let nonce = draw(machine, TEMP_SUFFIX_LENGTH as usize)?;
    let own = under(ctx.flavour, info, &format!("{TEMP_PREFIX}{nonce}"));
    let mut file = fs.create_excl(own.as_bytes(), FILE_MODE).map_err(raised)?;
    let wrote = fs.write_once(&mut file, nonce.as_bytes());
    // The `finally` close: its error replaces the write's.
    fs.close_created(file).map_err(raised)?;
    wrote.map_err(raised)?;
    let waited = wait_for(ctx, machine, info, &claim, &nonce, &own, temp);
    unlink_quietly(fs, machine, own.as_bytes())?;
    waited
}

/// `_wait_for`: `_take` until the claim is taken, its name is blocked,
/// or [`CLAIM_WAIT_SECONDS`] by `time.monotonic` have passed; a claim
/// holding one nonce from the first wait to the last is broken first.
fn wait_for<F: TokenFs + ?Sized>(
    ctx: &Ctx<'_, F>,
    machine: &mut dyn Machine,
    info: &str,
    claim: &str,
    nonce: &str,
    own: &str,
    temp: &str,
) -> Result<Result<String, TokenWrite>, Fault> {
    let deadline = machine.monotonic() + CLAIM_WAIT_SECONDS;
    let mut pause = REREAD_DELAY_SECONDS;
    let mut first: Option<String> = None;
    let contended = status(WriteStatus::SkippedContended, None, CONTENDED_DETAIL);
    while !take(ctx.fs, machine, claim, nonce, own)? {
        if blocked(ctx, claim)? {
            let blocked = status(WriteStatus::FailedClaimBlocked, None, BLOCKED_DETAIL);
            return Ok(Err(blocked));
        }
        let held = holder(ctx, claim)?;
        let left = deadline - machine.monotonic();
        if left <= 0.0 {
            if let Some(held) = held.as_deref().filter(|h| Some(*h) == first.as_deref()) {
                let now = fs_now(ctx.fs, temp.as_bytes());
                brk(ctx, machine, info, held, now)?;
            }
            let taken = take(ctx.fs, machine, claim, nonce, own)?;
            return Ok(if taken {
                Ok(nonce.to_owned())
            } else {
                Err(contended)
            });
        }
        first = first.or(held);
        machine.sleep(if left < pause { left } else { pause });
        pause = if RACE_PAUSE_MAX_SECONDS < 2.0 * pause {
            RACE_PAUSE_MAX_SECONDS
        } else {
            2.0 * pause
        };
    }
    Ok(Ok(nonce.to_owned()))
}

/// `_take(claim, nonce, own)`: a link of `own`; where links are refused
/// (any error but EEXIST), an exclusive create and one write, a claim
/// left unfinished unlinked. A PermissionError at the create is a claim
/// not yet gone on Windows and raised elsewhere.
fn take<F: TokenFs + ?Sized>(
    fs: &F,
    machine: &mut dyn Machine,
    claim: &str,
    nonce: &str,
    own: &str,
) -> Result<bool, Fault> {
    match fs.link(own.as_bytes(), claim.as_bytes()) {
        Ok(()) => return Ok(true),
        Err(error) if error.class == PyErr::FileExists => return Ok(false),
        Err(error) if error.class == PyErr::Value => return Err(raised(error)),
        Err(_) => {}
    }
    let mut file = match fs.create_excl(claim.as_bytes(), FILE_MODE) {
        Ok(file) => file,
        Err(error) if error.class == PyErr::FileExists => return Ok(false),
        Err(error) if error.class == PyErr::Permission && machine.win32() => return Ok(false),
        Err(error) => return Err(raised(error)),
    };
    let wrote = fs.write_once(&mut file, nonce.as_bytes());
    let closed = fs.close_created(file);
    if let Some(error) = closed.err().or(wrote.err()) {
        unlink_quietly(fs, machine, claim.as_bytes())?;
        return Err(raised(error));
    }
    Ok(true)
}

/// `_blocked(claim)`: whether `os.lstat` finds something other than a
/// regular file; an OSError is False.
fn blocked<F: TokenFs + ?Sized>(ctx: &Ctx<'_, F>, claim: &str) -> Result<bool, Fault> {
    let path = ctx.at(claim.as_bytes()).map_err(|_| Fault::NotPorted)?;
    match ctx.fs.lstat_result(&path) {
        Ok(found) => Ok(found.kind != Kind::Regular),
        Err(error) if error.class == PyErr::Value => Err(raised(error)),
        Err(_) => Ok(false),
    }
}

/// What `_claim_read` finds.
enum Held {
    /// The claim's text, or `"<state>: <detail>"` of a malformed one.
    Text(String),
    Absent,
    Unreadable,
}

/// `_claim_read(claim)`.
fn claim_read<F: TokenFs + ?Sized>(ctx: &Ctx<'_, F>, claim: &str) -> Result<Held, Fault> {
    Ok(match read_text(ctx, claim.as_bytes())? {
        Ok(text) => Held::Text(
            text.iter()
                .map(|&c| char::from_u32(c).unwrap_or('\u{FFFD}'))
                .collect(),
        ),
        Err(found) => match found.state {
            ReadState::Absent => Held::Absent,
            ReadState::Unreadable => Held::Unreadable,
            state => Held::Text(format!("{}: {}", state.name(), found.detail)),
        },
    })
}

/// `_holder(claim)`: the claim's text, else None.
fn holder<F: TokenFs + ?Sized>(ctx: &Ctx<'_, F>, claim: &str) -> Result<Option<String>, Fault> {
    Ok(match claim_read(ctx, claim)? {
        Held::Text(text) => Some(text),
        _ => None,
    })
}

/// `_break(info, held, now)`: the marks, then the claim removed while it
/// holds `held`, then every mark unlinked, even after a raise.
fn brk<F: TokenFs + ?Sized>(
    ctx: &Ctx<'_, F>,
    machine: &mut dyn Machine,
    info: &str,
    held: &str,
    now: Option<f64>,
) -> Result<(), Fault> {
    let Some(marks) = break_marks(ctx, machine, info, held, now)? else {
        return Ok(());
    };
    let claim = under(ctx.flavour, info, CLAIM_NAME);
    let removed = remove_holding(ctx, machine, &claim, held);
    for mark in &marks {
        unlink_quietly(ctx.fs, machine, mark.as_bytes())?;
    }
    removed
}

/// `_break_marks(info, held, now)`: `<prefix><blake2b-8 of held>-<n>`
/// created exclusively from generation 0, past each one more than
/// [`STALE_BREAK_SECONDS`] older than `now`; None where a live mark is
/// found, its age cannot be read, or (Windows) its create is refused.
/// `held` is text a claim held, so its UTF-8 with `replace` is its bytes.
fn break_marks<F: TokenFs + ?Sized>(
    ctx: &Ctx<'_, F>,
    machine: &mut dyn Machine,
    info: &str,
    held: &str,
    now: Option<f64>,
) -> Result<Option<Vec<String>>, Fault> {
    let fs = ctx.fs;
    let digest = blake2b(held.as_bytes(), 8, b"").unwrap_or_default();
    let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    let mut marks = Vec::new();
    let mut generation = 0u64;
    loop {
        let mark = under(
            ctx.flavour,
            info,
            &format!("{BREAK_PREFIX}{hex}-{generation}"),
        );
        marks.push(mark.clone());
        let made = fs
            .create_excl(mark.as_bytes(), FILE_MODE)
            .and_then(|file| fs.close_created(file));
        match made {
            Ok(()) => return Ok(Some(marks)),
            Err(error) if error.class == PyErr::Permission => {
                if !machine.win32() {
                    return Err(raised(error));
                }
                return Ok(None);
            }
            Err(error) if error.class == PyErr::FileExists => {
                let Some(now) = now else { return Ok(None) };
                let age = match fs.stat_mtime(mark.as_bytes()) {
                    Ok(mtime) => now - mtime,
                    Err(error) if error.class == PyErr::Value => return Err(raised(error)),
                    Err(_) => return Ok(None),
                };
                // A NaN age is not at most the bound: the next generation.
                if age <= STALE_BREAK_SECONDS {
                    return Ok(None);
                }
            }
            Err(error) => return Err(raised(error)),
        }
        generation += 1;
    }
}

/// The claim's unlink, for `unlink_retrying`.
struct Claimed<'a, F: ?Sized> {
    fs: &'a F,
    machine: &'a mut dyn Machine,
    path: &'a [u8],
}

impl<F: TokenFs + ?Sized> Remover for Claimed<'_, F> {
    type Error = OsError;
    fn unlink(&mut self) -> Result<(), OsError> {
        self.fs.unlink(self.path)
    }
    fn permission(&self, error: &OsError) -> bool {
        error.class == PyErr::Permission
    }
    fn sleep(&mut self, seconds: f64) {
        self.machine.sleep(seconds);
    }
}

/// `_remove_holding(claim, held)` (and `_release`, which is it): while
/// the claim reads `held`, `unlink_retrying` with `still` the claim
/// holding `held`, then a read; ends where the claim is gone or
/// another's, or after [`RACE_READ_SECONDS`] of pauses.
fn remove_holding<F: TokenFs + ?Sized>(
    ctx: &Ctx<'_, F>,
    machine: &mut dyn Machine,
    claim: &str,
    held: &str,
) -> Result<(), Fault> {
    let deadline = machine.monotonic() + RACE_READ_SECONDS;
    let mut pause = REREAD_DELAY_SECONDS;
    loop {
        match claim_read(ctx, claim)? {
            Held::Text(found) if found != held => return Ok(()),
            Held::Text(_) => {
                if unlink_holding(ctx, machine, claim, held)? {
                    return Ok(());
                }
            }
            Held::Absent => return Ok(()),
            Held::Unreadable => {}
        }
        let left = deadline - machine.monotonic();
        if left <= 0.0 {
            return Ok(());
        }
        machine.sleep(if left < pause { left } else { pause });
        pause = if RACE_PAUSE_MAX_SECONDS < 2.0 * pause {
            RACE_PAUSE_MAX_SECONDS
        } else {
            2.0 * pause
        };
    }
}

/// `_remove_holding`'s `try`: true where it returns, false where an
/// OSError passes to the next read. A raise inside `still` ends the
/// retry at once, as `still` answering false does, and is then raised.
fn unlink_holding<F: TokenFs + ?Sized>(
    ctx: &Ctx<'_, F>,
    machine: &mut dyn Machine,
    claim: &str,
    held: &str,
) -> Result<bool, Fault> {
    let win32 = machine.win32();
    let mut thrown: Option<Fault> = None;
    let unlinked = {
        let mut still = || match holder(ctx, claim) {
            Ok(found) => found.as_deref() == Some(held),
            Err(fault) => {
                thrown = Some(fault);
                false
            }
        };
        let mut remover = Claimed {
            fs: ctx.fs,
            machine,
            path: claim.as_bytes(),
        };
        unlink_retrying_by(&mut remover, win32, Some(&mut still))
    };
    let outcome = match (thrown, unlinked) {
        (Some(fault), _) => Err(fault),
        (None, Err(error)) => Err(raised(error)),
        (None, Ok(())) => claim_read(ctx, claim).map(|after| match after {
            Held::Text(text) => text != held,
            Held::Absent => true,
            Held::Unreadable => false,
        }),
    };
    match outcome {
        Err(Fault::Raised(error)) if error.class == PyErr::FileNotFound => Ok(true),
        Err(Fault::Raised(error)) if error.class != PyErr::Value => Ok(false),
        other => other,
    }
}
