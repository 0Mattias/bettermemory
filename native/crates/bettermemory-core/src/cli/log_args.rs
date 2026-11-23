//! Argument declarations for the `log` command family.

use crate::argparse::{Arg, ArgumentParser, Kind, SpecError};

const VERIFY_HELP: &str = concat!(
    "Verify the log: MACs, chain, head, the origin anchor and fold, and every --anchor ",
    "given. The origin anchor, kept beside the key outside the store (<store_id>.origin ",
    "in the keys directory), names the row where this host's history of the store begins ",
    "and the key that signed it: that row must still be in the log with the same MAC and ",
    "verify under that key. A row gone or changed, or one that key does not verify while ",
    "it is still held here, adds the problem origin_mismatch and the log reads tampered. ",
    "When that key is no longer held (its key file was lost) or no key is held at all, ",
    "nothing here can check the row: the origin reads unverifiable, and so does the log, ",
    "until the key is filed back (`bettermemory key import`). `bettermemory log ",
    "accept-history --yes` accepts a replaced history on purpose, on the record. Prints ",
    "a report and exits 0 only when every check passes; 1 when the log is tampered or ",
    "cannot be verified (an anchor or an origin that no longer matches is tampering); 2 ",
    "when there is no store or an anchor file cannot be read.",
);

const ANCHOR_HELP: &str = concat!(
    "Also check the anchors in FILE (written by `log anchor`; one JSON object, or JSON ",
    "Lines of them): the log must still hold each anchor's seq with the same MAC and the ",
    "same chain digest. A failed anchor adds the problem anchor_mismatch. An anchor of ",
    "another store is listed as other_store and changes nothing. Repeat for several ",
    "files. Without it the report is unchanged.",
);

/// Register `log verify` on the existing log subparsers.
pub fn add_verify_parser(log: &mut ArgumentParser) -> Result<(), SpecError> {
    let verify = log.add_parser("verify", Some(VERIFY_HELP), Some(VERIFY_HELP))?;
    verify.add_argument(
        &["--json"],
        Arg {
            kind: Some(Kind::StoreTrue),
            help: Some("Emit the report as JSON instead of text.".into()),
            ..Arg::default()
        },
    )?;
    verify.add_argument(
        &["--anchor"],
        Arg {
            kind: Some(Kind::Append),
            metavar: Some("FILE".into()),
            help: Some(ANCHOR_HELP.into()),
            ..Arg::default()
        },
    )
}
