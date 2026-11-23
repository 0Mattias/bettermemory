//! Product declarations in the order of the Python root and log builders.

use super::{add_hook_subparser, base_parser, ArgumentParser, SpecError, HOOK_HELP};
use crate::cli::log_args::add_verify_parser;

const ROOT_CHOICES: [(&str, &str); 31] = [
    ("health", "Print the aggregate memory health report."),
    (
        "init",
        concat!(
            "Onboard a fresh install: print the MCP config snippet, or auto-patch a known ",
            "client's config. Idempotent.",
        ),
    ),
    (
        "migrate",
        concat!(
            "One-shot data migrations. `migrate v8` reads a v8 store directory into a ",
            "bettermemory 9 store.",
        ),
    ),
    (
        "export",
        concat!(
            "Dump all active memories (and tombstones, by default) to a self-describing JSON ",
            "document, with each record's home, for reading or for an external indexer. It is ",
            "not a backup: it leaves out the signed log, episodes, conflicts and the other ",
            "tables, and no command restores a store from it. `bettermemory backup create` ",
            "makes a backup. Writes to stdout unless --output is given.",
        ),
    ),
    (
        "tombstones",
        concat!(
            "Inspect, restore, and prune the tombstone (removed-memory) audit log. ",
            "Subcommands: list, restore, prune.",
        ),
    ),
    (
        "rollback",
        concat!(
            "Remove the memories one actor wrote, leaving every other actor's in place. ",
            "Dry-run by default; --apply --yes commits. Every removal is a tombstone, so ",
            "`tombstones restore` puts a record back.",
        ),
    ),
    (
        "episodes",
        concat!(
            "Inspect and prune the episode (journal) store — the sibling-to-memory tier for ",
            "loop-iteration takeaways. Subcommands: list, prune.",
        ),
    ),
    (
        "log",
        concat!(
            "The bettermemory 9 store's hash-chained log. `log verify` checks every row's MAC ",
            "against the key kept outside the store, the chain between rows, the head ",
            "checkpoint against the last row, the origin anchor against the row where this ",
            "host's history begins, and every table against a replay of the log. Exit 0 when ",
            "the log verifies, 1 when it is tampered or cannot be verified. `log export` ",
            "writes the log as JSON Lines for an audit pipeline, `log anchor` takes an anchor ",
            "to keep off this host for `log verify --anchor`, and `log accept-history` ",
            "accepts a replaced history on the record.",
        ),
    ),
    (
        "index",
        concat!(
            "The store's full-text search index. `index rebuild` spells every memory's index ",
            "text again from its body and scopes and rebuilds the FTS5 table from it, the ",
            "repair doctor and backup verify name for an index drift.",
        ),
    ),
    (
        "key",
        concat!(
            "The store's key ring: the key outside the store that signs its log, the retired ",
            "keys earlier segments verify under, the head checkpoint, the origin anchor and ",
            "the binding to one store file. `key show` reads it; `key export` and `key ",
            "import` keep and file back keys; `key adopt` binds the ring to this store file; ",
            "`key rotate` writes a new key. Every change is a row in the store's log, signed ",
            "with its key. No key file is placed or deleted by hand.",
        ),
    ),
    (
        "project",
        concat!(
            "The projects membership knows (list, show, why, pending, folder) and the ",
            "declarations that settle a checkout's project (join, new, init).",
        ),
    ),
    (
        "memory",
        concat!(
            "One record at a time. `memory move` re-homes a record to another project (or ",
            "global); `memory history` lists every log row that made, changed or removed it.",
        ),
    ),
    (
        "membership",
        concat!(
            "Membership across stores: export the projects' portable identities and the ",
            "records' homes, import them into this store, and migrate a store's records into ",
            "projects.",
        ),
    ),
    (
        "eval",
        concat!(
            "Compute the three memory-effectiveness rates (memory_helped_rate, ",
            "endorsement_rate, silent_miss_rate) from the event log + active store. ",
            "Methodology in docs/eval.md.",
        ),
    ),
    (
        "review",
        concat!(
            "The write review's grading surface. `review samples` draws reviewed writes from ",
            "a window and shows each one's related set, answers and action; `review grade` ",
            "records the owner's grade of one as a review_grade event, which `bettermemory ",
            "eval` counts.",
        ),
    ),
    (
        "capture",
        concat!(
            "Session capture's batches. `capture review` lists each batch with its outcome, ",
            "how many memories it saved, its refusals by rule and what was undone; `capture ",
            "undo BATCH` tombstones a batch's memories (dry-run unless --apply); `capture ",
            "sample` and `capture grade` draw captured memories and record the owner's grade ",
            "of each against the ten rules. Calls no model.",
        ),
    ),
    (
        "recall",
        concat!(
            "The recall surface's grading surface. `recall review` draws prompt recall ",
            "deliveries, session briefs and silent prompts from a window and shows each one's ",
            "pointers and the prompt it answered; `recall grade` records the owner's grade of ",
            "a sample's pointers as a recall_grade event, which `bettermemory eval` counts.",
        ),
    ),
    (
        "rename-scope",
        concat!(
            "Replace OLD with NEW across every memory's scope list — the cheap fix for a ",
            "typo'd or deprecated scope (e.g. `infra` -> `infrastructure`). The CLI ",
            "counterpart of memory_admin's rename_scope action. Bumps `updated` but preserves ",
            "`last_verified_at`; tombstones are renamed too unless --no-tombstones.",
        ),
    ),
    (
        "try",
        "Instant offline demo: watch a memory go stale when a file it cites moves.",
    ),
    (
        "backup",
        concat!(
            "Verified backups of the bettermemory 9 store. `backup create` writes a ",
            "consistent snapshot bundle while the daemon runs, `backup verify` checks a ",
            "bundle's files, signature and log on a temporary copy, and `backup restore` puts ",
            "one back with the daemon stopped, rewinding the head checkpoint on the record so ",
            "`log verify` passes.",
        ),
    ),
    (
        "service",
        concat!(
            "Run the local daemon as a supervised service: a launchd LaunchAgent on macOS, a ",
            "systemd user unit on Linux, started at login and again after a crash. `install` ",
            "writes and loads the unit, `uninstall` unloads and removes it, `status` reports ",
            "it, `print` writes it to stdout only. Exit 2 on other platforms, with the ",
            "command to supervise yourself, and when the config (config.toml or the managed ",
            "file) does not load, with the loader's message.",
        ),
    ),
    (
        "doctor",
        concat!(
            "Check the install, the config, the store and its key, the daemon, the client ",
            "wiring, the disk and the key rings whose store is gone, read-only, each ok, warn ",
            "or fail with a one-line fix. Exit 0 when every check is ok, 1 on any failure, 2 ",
            "on warnings only. Posture (file modes, the sandbox) is `bettermemory security ",
            "audit`'s.",
        ),
    ),
    (
        "security",
        concat!(
            "Security checks for this install. `security audit` reports whether the store, ",
            "the key and the daemon's files are as SECURITY.md assumes; `security sandbox` ",
            "prints Claude Code sandbox settings for this host's paths; `security scan` finds ",
            "credentials and chat-template control tokens kept in the store. None writes a ",
            "row, a key or a file of its own.",
        ),
    ),
    (
        "stats",
        concat!(
            "Usage and growth of the store, read-only: file sizes, rows per table, memories ",
            "by project, scope, client, model and category, records created and removed and ",
            "log rows written per day over the last DAYS days, tombstones, episodes, log rows ",
            "by kind, and the oldest and newest records. Aggregate SQL only: no MAC is ",
            "checked (`log verify` does that). Exit 0 with the report, 2 when no store is ",
            "found or the config or the store file cannot be read.",
        ),
    ),
    (
        "completion",
        concat!(
            "Print a completion script for bash, zsh or fish, generated from this build's own ",
            "parser, so it completes every subcommand and flag this version has. Prints only; ",
            "regenerate it after an upgrade. bash: add `eval \"$(bettermemory completion ",
            "bash)\"` to ~/.bashrc. zsh: after compinit in ~/.zshrc, add `eval \"$(bettermemory ",
            "completion zsh)\"`, or save the output as _bettermemory in a directory on $fpath. ",
            "fish: `bettermemory completion fish > ",
            "~/.config/fish/completions/bettermemory.fish`.",
        ),
    ),
    (
        "config",
        concat!(
            "Where the config lives (`config path`), the settings in force and where each ",
            "came from (`config show`), and the lines that do not do what they say (`config ",
            "check`). Read-only: never creates the config file, the store or the keys.",
        ),
    ),
    (
        "up",
        concat!(
            "Start the local daemon for this store, detached, or report the one already ",
            "running. --foreground runs it in this process, for supervisors. Exit 0 when a ",
            "daemon runs, 1 when none could be started.",
        ),
    ),
    (
        "down",
        concat!(
            "Stop the local daemon for this store. Exit 0 when it stopped or was not running, ",
            "1 when it did not stop; its state file is then kept while its process holds the ",
            "store's lock.",
        ),
    ),
    (
        "status",
        concat!(
            "Report whether the local daemon for this store is running. Exit 0; with --check ",
            "the exit code is the answer: 0 running and answering as this store's daemon, 1 ",
            "not running, 2 a state file whose daemon does not answer but may be alive (it ",
            "holds the store's lock), 3 the port answers, but not as this store's healthy ",
            "daemon (another store or process, or a reply that is not a daemon's /health).",
        ),
    ),
    (
        "serve",
        concat!(
            "Run the MCP server over stdio in this process, without the daemon. ",
            "`bettermemory` with no arguments is the stdio shim in front of the daemon.",
        ),
    ),
    ("hook", HOOK_HELP),
];

/// The names consumed by the injected command builder and dispatch.
pub const COMMANDS: [&str; ROOT_CHOICES.len()] = {
    let mut names = [""; ROOT_CHOICES.len()];
    let mut index = 0;
    while index < names.len() {
        names[index] = ROOT_CHOICES[index].0;
        index += 1;
    }
    names
};

const DEFERRED_LOG_CHOICES: &[(&str, &str)] = &[
    (
        "export",
        concat!(
            "Write the log as JSON Lines, one object per row in seq order, for a SIEM or an ",
            "audit archive: seq, ts, kind, class, session, actor, record ids, the payload, ",
            "the MAC and the MAC it chains to, the segment's key fingerprint and whether the ",
            "row's MAC verifies under the key held here (null when it is not held). Bodies ",
            "and other free text are replaced by their sha256 and length unless ",
            "--include-bodies. Read-only and deterministic: the same log gives the same ",
            "bytes. Exit 0 when the export is written, 1 when the output closed before it ",
            "was, 2 when there is no store it can read, an argument is wrong or --out cannot ",
            "be written (or names the store's own files or the keys directory). The export is ",
            "a trail, not a verdict: run `log verify` for that.",
        ),
    ),
    (
        "anchor",
        concat!(
            "Take an anchor of the log: the store id, the last row's seq and MAC, a SHA-256 ",
            "over every row's MAC up to it, and the time. Keep it off this host (a ticket, a ",
            "git repository, a WORM bucket); `log verify --anchor FILE` then shows whether ",
            "history up to that seq was rewritten since, even by a process that holds the ",
            "key. Prints the anchor as one JSON line, or writes it to --out. Exit 0 when the ",
            "anchor is taken, 2 when there is no store it can read, its log is empty or --out ",
            "cannot be written (or names the store's own files or the keys directory).",
        ),
    ),
    (
        "accept-history",
        concat!(
            "Accept the store file's history as it stands, on the record, when the origin ",
            "anchor reads origin_mismatch or unverifiable or the head checkpoint names a row ",
            "the log does not hold: a store file copied over this host's copy again, an ",
            "anchor from before 9.0.0rc13 whose key was lost, an older copy put back by hand. ",
            "With the daemon stopped, the key ring is bound to this file and the store opened ",
            "as its owner opens it (a missing or foreign key is settled as any writing ",
            "command settles it); a history_accepted row, signed with the store's key, names ",
            "the anchor, head and binding before and after; then this host's history is ",
            "anchored at the first row of the earliest segment a key it holds signs and the ",
            "head moved to the row, so a crash leaves the acceptance recorded or none; last ",
            "the rewind record beside the head (<store_id>.backup_restore.json) gets the same ",
            "entry. Refused while a daemon serves or starts for the store, or when the key ",
            "file cannot be read. Without --yes prints the plan. Exit 0 accepted (or nothing ",
            "to accept), 1 refused, failed or the log still tampered after it, 2 no store, 3 ",
            "the plan.",
        ),
    ),
];

/// Build the product CLI parser. A registered choice requires its actual
/// parser before it can parse its remaining arguments.
pub fn build_product_parser(version: &str, width: i64) -> Result<ArgumentParser, SpecError> {
    let mut root = base_parser(version, width)?;
    for (name, help) in ROOT_CHOICES {
        match name {
            "log" => {
                let log = root.add_parser(name, Some(help), Some(help))?;
                log.add_subparsers(Some("log_cmd"), width)?;
                add_verify_parser(log)?;
                for &(name, help) in DEFERRED_LOG_CHOICES {
                    log.add_deferred_parser(name, Some(help))?;
                }
            }
            "hook" => add_hook_subparser(&mut root)?,
            _ => root.add_deferred_parser(name, Some(help))?,
        }
    }
    Ok(root)
}
