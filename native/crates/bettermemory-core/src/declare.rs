//! `declare.py`: the membership model a declaration runs on
//! (`declaring_model`, 19-47). The model is `Membership.open` with this
//! process's machine and a token writer that re-mints only in this
//! machine's checkouts, each new token naming the lineage the store
//! records for the checkout.
//!
//! Effects are arguments: the machine identity (`location.process_machine`,
//! a memo of the location unit), the re-mint (`checkout_token.remint`),
//! the machine row's write (`record_machine`), the liveness and the clock
//! the reference takes as `Membership`'s defaults.

use crate::checkout_token::Lineage;
use crate::machine::MachineIdentity;
use crate::membership::{
    LivenessSeam, Membership, MembershipStore, Now, Options, Result2, TokenWriter,
};
use crate::membership_lines::Raised;
use crate::membership_records::records::{Checkout, BARE};
use crate::pyvalue::PyStr;
use crate::unicode::cps;

/// The status a checkout of another machine (or without a git directory)
/// is given, with no token.
pub const SKIPPED_OTHER_MACHINE: &str = "skipped:other_machine";

/// One call of `checkout_token.remint(git_dir, policy=, bare=,
/// replacing=, only_siblings=)`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RemintCall {
    pub git_dir: PyStr,
    pub policy: bool,
    pub bare: bool,
    pub replacing: Option<Lineage>,
    pub only_siblings: bool,
}

/// `write_token` (35-45): for a checkout with a git directory on
/// `machine`, the re-mint's token and status; for any other,
/// `(None, "skipped:other_machine")`. An empty git directory is not None
/// and is re-minted; `bare` is the kind's exact equality with `"bare"`.
/// A raise of the re-mint (`fsync_dir` sits outside its `try`) is the
/// writer's `Err` and passes out of the model call that gave the token.
pub fn write_token(
    machine: PyStr,
    policy: bool,
    remint: impl Fn(&RemintCall) -> Result<(Option<PyStr>, PyStr), Raised> + 'static,
) -> TokenWriter {
    Box::new(move |k: &Checkout| {
        let git_dir = match &k.git_dir {
            Some(g) if k.machine == machine => g.clone(),
            _ => return Ok(Some((None, cps(SKIPPED_OTHER_MACHINE)))),
        };
        let call = RemintCall {
            git_dir,
            policy,
            bare: k.kind.iter().copied().eq(BARE.chars().map(u32::from)),
            replacing: k.lineage(),
            only_siblings: true,
        };
        remint(&call).map(Some)
    })
}

/// `declaring_model(store, config, session=)` (19-47): `Membership.open`
/// with `identity` and [`write_token`] under the `[membership]
/// write_token` policy. A raise of `record` passes through.
#[allow(clippy::too_many_arguments)]
pub fn declaring_model<S: MembershipStore>(
    store: S,
    identity: &MachineIdentity,
    write_token_policy: bool,
    session: Option<PyStr>,
    liveness: LivenessSeam,
    now: Now,
    remint: impl Fn(&RemintCall) -> Result<(Option<PyStr>, PyStr), Raised> + 'static,
    record: impl FnOnce(&mut Membership<S>, &MachineIdentity) -> Result2<(), S::Error>,
) -> Result2<Membership<S>, S::Error> {
    let options = Options {
        liveness,
        token_writer: Some(write_token(identity.id.clone(), write_token_policy, remint)),
        now,
        session,
    };
    Membership::open(store, identity, options, record)
}
